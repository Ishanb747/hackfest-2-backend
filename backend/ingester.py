"""
ingester.py — Live Transaction Stream Simulator for Turgon

Creates a `transactions_live` table in aml.db and continuously inserts
random batches of rows from the main dataset to simulate a live feed.

Usage:
    python ingester.py                        # defaults: 50 rows every 15s
    python ingester.py --batch 100 --interval 10
    python ingester.py --reset                # wipe transactions_live and exit

⚠️  This is the ONLY file that opens DuckDB in write mode.
    All other Turgon components use read_only=True.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime, UTC
from pathlib import Path

import duckdb

# ── Config (with fallback if config.py not importable) ───────────────────────
try:
    from config import DUCKDB_PATH, LIVE_TABLE_NAME, INGESTER_BATCH_SIZE, INGESTER_INTERVAL
except ImportError:
    DUCKDB_PATH          = Path(__file__).parent / "data" / "aml.db"
    LIVE_TABLE_NAME      = "transactions_live"
    INGESTER_BATCH_SIZE  = 50
    INGESTER_INTERVAL    = 15

# ── Globals ───────────────────────────────────────────────────────────────────
_running = True


def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S UTC")


def _signal_handler(sig, frame):
    global _running
    print(f"\n[ingester] Shutting down gracefully…")
    _running = False


def _get_write_conn(retries: int = 8, delay: float = 2.0) -> duckdb.DuckDBPyConnection:
    """
    Open DuckDB in write mode with retry logic.
    On Windows, read-only connections (watchdog/phase2) briefly lock the file.
    We wait them out instead of crashing.
    """
    if not DUCKDB_PATH.exists():
        print(f"[ERROR] Database not found at {DUCKDB_PATH}")
        print("        Run: python data/setup_duckdb.py")
        sys.exit(1)

    for attempt in range(retries):
        try:
            return duckdb.connect(database=str(DUCKDB_PATH), read_only=False)
        except Exception as e:
            if attempt < retries - 1:
                print(f"[ingester] DB locked (attempt {attempt+1}/{retries}), retrying in {delay}s…")
                time.sleep(delay)
            else:
                print(f"[ERROR] Could not open DB after {retries} attempts: {e}")
                sys.exit(1)


def _ensure_live_table(conn: duckdb.DuckDBPyConnection) -> bool:
    """
    Create transactions_live as an empty clone of the transactions table.
    Returns True if created fresh, False if already exists.
    """
    # Check if live table already exists
    tables = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
    if LIVE_TABLE_NAME in tables:
        return False

    # Get column definitions from the source table
    try:
        cols_info = conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'transactions' ORDER BY ordinal_position"
        ).fetchall()
    except Exception as e:
        print(f"[ERROR] Cannot read transactions schema: {e}")
        sys.exit(1)

    if not cols_info:
        print("[ERROR] 'transactions' table has no columns or doesn't exist.")
        sys.exit(1)

    col_defs = ", ".join(f"{name} {dtype}" for name, dtype in cols_info)
    conn.execute(f"CREATE TABLE {LIVE_TABLE_NAME} ({col_defs})")
    conn.commit()
    print(f"[ingester] Created '{LIVE_TABLE_NAME}' table ({len(cols_info)} columns)")
    return True


def _get_total_source_rows(conn: duckdb.DuckDBPyConnection) -> int:
    try:
        return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    except Exception:
        return 0


def _insert_batch(batch_size: int) -> int:
    """
    Open a fresh write connection, insert a batch, then immediately close.
    This releases the write lock so watchdog can read between cycles.
    """
    conn = _get_write_conn()
    try:
        conn.execute(f"""
            INSERT INTO {LIVE_TABLE_NAME}
            SELECT * FROM transactions
            USING SAMPLE {batch_size} ROWS
        """)
        conn.commit()
        return batch_size
    except Exception as e:
        print(f"[ingester] Insert error: {e}")
        return 0
    finally:
        conn.close()  # releases write lock immediately
        time.sleep(0.5)  # brief settle so watchdog can grab read lock cleanly


def _live_row_count() -> int:
    """Open a fresh read connection, get count, close."""
    conn = _get_write_conn()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {LIVE_TABLE_NAME}").fetchone()[0]
    except Exception:
        return 0
    finally:
        conn.close()


def reset_live_table() -> None:
    """Drop and recreate the live table (clean slate)."""
    conn = _get_write_conn()
    try:
        conn.execute(f"DROP TABLE IF EXISTS {LIVE_TABLE_NAME}")
        conn.commit()
        print(f"[ingester] Dropped '{LIVE_TABLE_NAME}'")
    except Exception as e:
        print(f"[ingester] Reset error: {e}")
    finally:
        conn.close()


def run(batch_size: int = INGESTER_BATCH_SIZE, interval: int = INGESTER_INTERVAL) -> None:
    """Main ingester loop — runs until interrupted."""
    global _running

    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    print(f"[ingester] Starting — batch={batch_size} rows, interval={interval}s")
    print(f"[ingester] Database: {DUCKDB_PATH}")
    print(f"[ingester] Target table: {LIVE_TABLE_NAME}")
    print(f"[ingester] Press Ctrl+C to stop\n")

    # Setup — open briefly to create table, then close
    setup_conn = _get_write_conn()
    created     = _ensure_live_table(setup_conn)
    source_rows = _get_total_source_rows(setup_conn)
    setup_conn.close()

    print(f"[ingester] Source table has {source_rows:,} rows to sample from")

    if not created:
        existing = _live_row_count()
        print(f"[ingester] Resuming — {LIVE_TABLE_NAME} already has {existing:,} rows")

    cycle = 0
    while _running:
        cycle += 1

        # Each iteration: open → insert → close (releases lock between cycles)
        inserted = _insert_batch(batch_size)
        total    = _live_row_count()

        print(
            f"[{_now()}] Cycle {cycle:04d} — "
            f"+{inserted} rows inserted → "
            f"{total:,} total in {LIVE_TABLE_NAME}"
        )

        # Sleep in small chunks so Ctrl+C is responsive
        for _ in range(interval * 4):
            if not _running:
                break
            time.sleep(0.25)

    print(f"[ingester] Stopped after {cycle} cycles.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Turgon Live Transaction Stream Simulator")
    parser.add_argument(
        "--batch", type=int, default=INGESTER_BATCH_SIZE,
        help=f"Rows to insert per cycle (default: {INGESTER_BATCH_SIZE})"
    )
    parser.add_argument(
        "--interval", type=int, default=INGESTER_INTERVAL,
        help=f"Seconds between inserts (default: {INGESTER_INTERVAL})"
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Wipe transactions_live table and exit"
    )
    args = parser.parse_args()

    if args.reset:
        reset_live_table()
        print("[ingester] Reset complete.")
        sys.exit(0)

    run(batch_size=args.batch, interval=args.interval)