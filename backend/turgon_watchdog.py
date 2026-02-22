"""
watchdog.py — Real-Time Violation Detection Watchdog for Turgon

Monitors transactions_live for new rows. When new data arrives,
runs Phase 2 in live mode (deterministic, no LLM) and writes
violation_report_live.json for the dashboard to pick up.

Usage:
    python watchdog.py                  # default 20s polling interval
    python watchdog.py --interval 10   # faster polling
    python watchdog.py --once          # single detection cycle then exit

Requires:
    - ingester.py running in another terminal
    - aml.db with transactions_live table
    - policy_rules.json with extracted rules
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, UTC
from pathlib import Path

import duckdb

# ── Config (with fallback) ────────────────────────────────────────────────────
try:
    from config import (
        DUCKDB_PATH,
        LIVE_TABLE_NAME,
        LIVE_REPORT_PATH,
        WATCHDOG_INTERVAL,
        RULES_JSON_PATH,
    )
except ImportError:
    DUCKDB_PATH       = Path(__file__).parent / "data" / "aml.db"
    LIVE_TABLE_NAME   = "transactions_live"
    LIVE_REPORT_PATH  = Path(__file__).parent / "rules" / "violation_report_live.json"
    WATCHDOG_INTERVAL = 20
    RULES_JSON_PATH   = Path(__file__).parent / "rules" / "policy_rules.json"

# ── Globals ───────────────────────────────────────────────────────────────────
_running = True


def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S UTC")


def _signal_handler(sig, frame):
    global _running
    print(f"\n[watchdog] Shutting down gracefully…")
    _running = False


def _get_live_row_count(retries: int = 5, delay: float = 2.0) -> int | None:
    """
    Open a short-lived read connection, get count, close immediately.
    Retries on lock conflicts (ingester holds write lock briefly during insert).
    Returns None if table doesn't exist yet.
    """
    for attempt in range(retries):
        conn = None
        try:
            conn = duckdb.connect(database=str(DUCKDB_PATH), read_only=True)
            count = conn.execute(f"SELECT COUNT(*) FROM {LIVE_TABLE_NAME}").fetchone()[0]
            return count
        except Exception as e:
            err = str(e).lower()
            if "not found" in err or "does not exist" in err:
                return None  # table genuinely doesn't exist
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                return None
        finally:
            if conn:
                try:
                    conn.close()  # always release immediately
                except Exception:
                    pass
    return None


def _run_detection(new_rows: int) -> tuple[int, float]:
    """
    Trigger Phase 2 in live mode.
    Returns (total_violations, duration_seconds).
    """
    from phase2_executor import run as phase2_run

    t0      = time.time()
    results = phase2_run(live_mode=True, output_path=LIVE_REPORT_PATH)
    duration = time.time() - t0

    total_violations = sum(r.get("violation_count", 0) for r in results)

    # Log to audit with the real new_rows count
    try:
        from audit import log_live_detection
        log_live_detection(new_rows, total_violations, duration)
    except Exception:
        pass

    return total_violations, duration


def _preflight_checks() -> bool:
    """Verify prerequisites before starting the watch loop."""
    ok = True

    if not DUCKDB_PATH.exists():
        print(f"[watchdog] ❌ Database not found: {DUCKDB_PATH}")
        print("           Run: python data/setup_duckdb.py")
        ok = False

    if not RULES_JSON_PATH.exists():
        print(f"[watchdog] ❌ No rules found: {RULES_JSON_PATH}")
        print("           Run Phase 1 first to extract policy rules.")
        ok = False

    return ok


def run(interval: int = WATCHDOG_INTERVAL, once: bool = False) -> None:
    """Main watchdog loop."""
    global _running

    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    print(f"[watchdog] Starting — polling every {interval}s")
    print(f"[watchdog] Watching table: {LIVE_TABLE_NAME}")
    print(f"[watchdog] Output: {LIVE_REPORT_PATH}")
    print(f"[watchdog] Press Ctrl+C to stop\n")

    if not _preflight_checks():
        sys.exit(1)

    last_count  = 0
    cycle       = 0
    detections  = 0

    # ── Initial state: figure out how many rows already exist ─────────────────
    initial = _get_live_row_count()
    if initial is None:
        print(f"[watchdog] ⚠️  '{LIVE_TABLE_NAME}' table not found yet.")
        print(f"           Start ingester.py in another terminal, then retry.")
    else:
        last_count = initial
        print(f"[watchdog] Baseline: {last_count:,} rows in {LIVE_TABLE_NAME}")

    print()

    while _running:
        cycle += 1

        current_count = _get_live_row_count()

        if current_count is None:
            print(f"[{_now()}] Cycle {cycle:04d} — table not ready, waiting…")
        elif current_count == last_count:
            print(f"[{_now()}] Cycle {cycle:04d} — no new rows ({current_count:,} total), sleeping…")
        else:
            new_rows   = current_count - last_count
            last_count = current_count
            detections += 1

            print(f"[{_now()}] Cycle {cycle:04d} — 🔔 {new_rows:+,} new transactions detected!")
            print(f"           Running live detection…")

            try:
                violations, duration = _run_detection(new_rows)
                print(
                    f"           ✅ Done in {duration:.1f}s — "
                    f"{violations:,} violations found across all rules"
                )
                print(f"           Report → {LIVE_REPORT_PATH}")
            except Exception as e:
                print(f"           ❌ Detection failed: {e}")

        print()

        if once:
            break

        # Sleep in small increments so Ctrl+C works immediately
        for _ in range(interval * 4):
            if not _running:
                break
            time.sleep(0.25)

    print(f"[watchdog] Stopped — {cycle} cycles, {detections} detection(s) triggered.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Turgon Real-Time Violation Watchdog")
    parser.add_argument(
        "--interval", type=int, default=WATCHDOG_INTERVAL,
        help=f"Polling interval in seconds (default: {WATCHDOG_INTERVAL})"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single detection cycle and exit (useful for testing)"
    )
    args = parser.parse_args()

    run(interval=args.interval, once=args.once)