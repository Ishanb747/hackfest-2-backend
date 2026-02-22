"""
audit.py — Immutable append-only audit log for Turgon (SQLite-backed).

Tables:
  audit_log(id, ts, phase, event_type, rule_id, details_json)

Never UPDATE or DELETE — only INSERT.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT      = Path(__file__).parent.resolve()
AUDIT_DB  = ROOT / "rules" / "audit.db"
AUDIT_DB.parent.mkdir(parents=True, exist_ok=True)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(AUDIT_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT    NOT NULL,
            phase        TEXT    NOT NULL,
            event_type   TEXT    NOT NULL,
            rule_id      TEXT,
            details_json TEXT
        )
    """)
    conn.commit()
    return conn


def _now() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Public writers ─────────────────────────────────────────────────────────────

def log_pipeline_run(phase: int, duration_s: float, stats: dict) -> None:
    """Log a pipeline phase completion event."""
    conn = _connect()
    conn.execute(
        "INSERT INTO audit_log(ts, phase, event_type, rule_id, details_json) VALUES(?,?,?,?,?)",
        (_now(), f"Phase {phase}", "PIPELINE_RUN", None, json.dumps(stats)),
    )
    conn.commit()
    conn.close()


def log_hitl_decision(rule_id: str, action: str, analyst: str, notes: str) -> None:
    """Log a human analyst decision."""
    conn = _connect()
    conn.execute(
        "INSERT INTO audit_log(ts, phase, event_type, rule_id, details_json) VALUES(?,?,?,?,?)",
        (
            _now(),
            "Phase 3",
            f"HITL_{action}",
            rule_id,
            json.dumps({"analyst": analyst, "notes": notes, "action": action}),
        ),
    )
    conn.commit()
    conn.close()


def log_explanation_run(rule_count: int, duration_s: float) -> None:
    """Log a Phase 3 explanation generation run."""
    conn = _connect()
    conn.execute(
        "INSERT INTO audit_log(ts, phase, event_type, rule_id, details_json) VALUES(?,?,?,?,?)",
        (
            _now(), "Phase 3", "EXPLANATION_RUN", None,
            json.dumps({"rules_explained": rule_count, "duration_s": round(duration_s, 2)}),
        ),
    )
    conn.commit()
    conn.close()


# ── Public readers ─────────────────────────────────────────────────────────────

def get_log(limit: int = 200) -> list[dict]:
    """Return the most recent audit log entries (newest first)."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    conn = _connect()
    total     = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    runs      = conn.execute("SELECT COUNT(*) FROM audit_log WHERE event_type='PIPELINE_RUN'").fetchone()[0]
    decisions = conn.execute("SELECT COUNT(*) FROM audit_log WHERE event_type LIKE 'HITL_%'").fetchone()[0]
    conn.close()
    return {"total_events": total, "pipeline_runs": runs, "hitl_decisions": decisions}
