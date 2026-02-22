"""
phase2_executor.py — Deterministic Phase 2 SQL Execution

Reads policy_rules.json, translates each rule into a DuckDB SELECT query,
validates it with SecureSQLValidatorTool, and writes violation_report.json.

This bypasses the LLM for Phase 2 because the structured rules already
contain all data needed to build SQL without an AI agent.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import duckdb

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.resolve()
RULES_JSON  = ROOT / "rules" / "policy_rules.json"
REPORT_JSON = ROOT / "rules" / "violation_report.json"
LIVE_REPORT_JSON = ROOT / "rules" / "violation_report_live.json"
DB_PATH     = ROOT / "data" / "aml.db"

MAX_SAMPLE_ROWS = 5
SAMPLE_CAP = 100  # number of sample rows to fetch for display (memory-efficient)
# Note: We now use COUNT(*) for accurate totals, and LIMIT for samples


# ── DDL/DML blocklist ─────────────────────────────────────────────────────────
_BLOCKED_KEYWORDS = [
    "DROP", "DELETE", "UPDATE", "INSERT", "CREATE", "ALTER",
    "TRUNCATE", "REPLACE", "MERGE", "EXEC", "EXECUTE", "CALL",
    "GRANT", "REVOKE", "COPY", "ATTACH", "DETACH", "LOAD", "IMPORT", "EXPORT",
]
_SELECT_RE = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
_INLINE_CMT = re.compile(r"--[^\n]*")
_BLOCK_CMT  = re.compile(r"/\*.*?\*/", re.DOTALL)


def _validate_sql(sql: str) -> tuple[bool, str]:
    cleaned = _BLOCK_CMT.sub(" ", sql)
    cleaned = _INLINE_CMT.sub(" ", cleaned).strip()
    if not _SELECT_RE.match(cleaned):
        return False, f"Must start with SELECT, got: {cleaned.split()[0] if cleaned.split() else '(empty)'}"
    sql_up = cleaned.upper()
    for kw in _BLOCKED_KEYWORDS:
        if re.search(r"\b" + kw + r"\b", sql_up):
            return False, f"Blocked keyword: {kw}"
    stmts = [s.strip() for s in cleaned.split(";") if s.strip()]
    if len(stmts) > 1:
        return False, f"Multiple statements ({len(stmts)} found)"
    return True, ""


# ── SQL builder from a PolicyRule dict ────────────────────────────────────────

# Preferred columns to SELECT (in priority order); dynamically filtered to what exists
_PREFERRED_COLS = [
    "From_Bank", "From_Account", "To_Bank", "To_Account",
    "Amount_Paid", "Amount_Received",
    "Payment_Currency", "Receiving_Currency",
    "Payment_Format", "Timestamp", "Is_Laundering",
]


def _get_select_cols(conn: duckdb.DuckDBPyConnection, table_name: str = "transactions") -> str:
    """Build SELECT clause from columns that actually exist in the table."""
    try:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_name = '{table_name}' ORDER BY ordinal_position"
        ).fetchall()
        actual = {r[0] for r in rows}
        matched = [c for c in _PREFERRED_COLS if c in actual]
        if matched:
            return ", ".join(matched)
        # Fallback: select everything
        return "*"
    except Exception:
        return ", ".join(_PREFERRED_COLS)

def _build_sql(rule: dict, select_cols: str = ", ".join(_PREFERRED_COLS), table_name: str = "transactions") -> str | None:
    """Build a SELECT query from a rule dict. Returns None if unsupported."""
    field     = rule.get("condition_field", "").strip()
    operator  = rule.get("operator", "=").strip()
    threshold = rule.get("threshold_value")
    sql_hint  = rule.get("sql_hint", "").strip()

    if not field:
        return None

    # Normalise operator
    op_map = {"==": "=", "!=": "<>", "NOT IN": "NOT IN", "IN": "IN"}
    op = op_map.get(operator.upper(), operator)

    # Format threshold value
    if isinstance(threshold, str):
        val = f"'{threshold}'"
    elif isinstance(threshold, list):
        joined = ", ".join(f"'{x}'" if isinstance(x, str) else str(x) for x in threshold)
        val = f"({joined})"
        op = "IN"
    elif isinstance(threshold, (int, float)):
        val = str(threshold)
    else:
        val = str(threshold)

    # Build WHERE clause
    where_parts = [f"{field} {op} {val}"]

    # Append extra conditions from sql_hint if it looks like a simple condition
    # e.g. "Payment_Format = 'Cash'"  or  "Payment_Currency != Receiving_Currency"
    hint_patterns = [
        r"Payment_Format\s*=\s*'[^']*'",
        r"Payment_Currency\s*!=\s*Receiving_Currency",
        r"Is_Laundering\s*=\s*1",
        r"Amount_Paid\s*%\s*1000\s*=\s*0",
        r"Payment_Format\s*=\s*'[^']*'",
    ]
    for pat in hint_patterns:
        m = re.search(pat, sql_hint, re.IGNORECASE)
        if m:
            condition = m.group(0).strip()
            if condition.upper() not in " ".join(where_parts).upper():
                where_parts.append(condition)

    where_clause = " AND ".join(where_parts)

    return (
        f"SELECT {select_cols}\n"
        f"FROM aml.{table_name}\n"
        f"WHERE {where_clause}"
    )


# ── Serialiser ────────────────────────────────────────────────────────────────

def _serialize(val: Any) -> Any:
    try:
        json.dumps(val)
        return val
    except (TypeError, ValueError):
        return str(val)


# ── Main executor ─────────────────────────────────────────────────────────────

def run(live_mode: bool = False, output_path: Path | None = None) -> list[dict]:
    """
    Execute Phase 2 SQL generation and violation detection.
    
    Args:
        live_mode: If True, query transactions_live table instead of transactions
        output_path: Custom output path for the report (defaults to REPORT_JSON or LIVE_REPORT_JSON)
    
    Returns:
        List of violation report dictionaries
    """
    if not RULES_JSON.exists():
        print(f"[ERROR] Rules file not found: {RULES_JSON}")
        return []

    rules: list[dict] = json.loads(RULES_JSON.read_text(encoding="utf-8"))
    mode_label = "LIVE" if live_mode else "Phase 2"
    table_name = "transactions_live" if live_mode else "transactions"
    
    print(f"[{mode_label}] Loaded {len(rules)} rules from {RULES_JSON.name}")
    print(f"[{mode_label}] Querying table: {table_name}")

    if not DB_PATH.exists():
        print(f"[ERROR] DuckDB not found at {DB_PATH}. Run setup_duckdb.py first.")
        return []

    conn = duckdb.connect(database=str(DB_PATH), read_only=True)
    select_cols = _get_select_cols(conn, table_name)
    report: list[dict] = []
    t0 = time.time()

    for rule in rules:
        rule_id = rule.get("id", "?")
        description = rule.get("description", "")

        sql = _build_sql(rule, select_cols, table_name)
        if sql is None:
            report.append({
                "rule_id": rule_id,
                "rule_description": description,
                "sql": "",
                "violation_count": 0,
                "sample_violations": [],
                "status": "SKIPPED",
                "reason": "Could not build SQL from rule fields",
            })
            continue

        # Validate
        valid, reason = _validate_sql(sql)
        if not valid:
            report.append({
                "rule_id": rule_id,
                "rule_description": description,
                "sql": sql,
                "violation_count": 0,
                "sample_violations": [],
                "status": "BLOCKED",
                "reason": reason,
            })
            print(f"  [{rule_id}] BLOCKED — {reason}")
            continue

        # Execute: Two-query strategy for accuracy + memory efficiency
        try:
            # Query 1: Get accurate count (fast, minimal memory)
            count_sql = sql.replace(f"SELECT {select_cols}", "SELECT COUNT(*)", 1)
            count = conn.execute(count_sql).fetchone()[0]
            
            # Query 2: Get sample rows for display (memory-efficient)
            sample_sql = sql + f"\nLIMIT {SAMPLE_CAP}"
            rel = conn.execute(sample_sql)
            cols = [d[0] for d in rel.description]
            rows = rel.fetchall()
            violations = [{k: _serialize(v) for k, v in zip(cols, r)} for r in rows]

            report.append({
                "rule_id": rule_id,
                "rule_description": description,
                "sql": sql,
                "violation_count": count,  # Accurate total count
                "sample_violations": violations[:MAX_SAMPLE_ROWS],
                "status": "SUCCESS",
            })
            print(f"  [{rule_id}] SUCCESS — {count:,} violations (showing {len(violations)} samples)")
        except Exception as e:
            report.append({
                "rule_id": rule_id,
                "rule_description": description,
                "sql": sql,
                "violation_count": 0,
                "sample_violations": [],
                "status": "SQL_ERROR",
                "reason": str(e),
            })
            print(f"  [{rule_id}] SQL_ERROR — {e}")

    conn.close()
    duration = time.time() - t0

    # Determine output path
    if output_path is None:
        output_path = LIVE_REPORT_JSON if live_mode else REPORT_JSON
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n[{mode_label}] Violation report saved -> {output_path}")

    # Audit log (only for non-live mode)
    if not live_mode:
        try:
            from audit import log_pipeline_run
            triggered = sum(1 for r in report if r.get("violation_count", 0) > 0)
            log_pipeline_run(2, duration, {
                "rules_checked": len(report),
                "rules_triggered": triggered,
                "total_violations": sum(r.get("violation_count", 0) for r in report),
            })
        except Exception:
            pass

    return report


if __name__ == "__main__":
    results = run()
    triggered = sum(1 for r in results if r.get("violation_count", 0) > 0)
    total = sum(r.get("violation_count", 0) for r in results)
    print(f"\nSummary: {triggered}/{len(results)} rules triggered | {total:,} total violations")
