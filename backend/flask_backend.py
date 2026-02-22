"""
flask_backend.py — RuleForge REST API Backend (Flask)

Run:  python flask_backend.py
API served at: http://localhost:5000
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import psutil
from flask import Flask, jsonify, request, Response, stream_with_context
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins=[
    "http://localhost:3000", 
    "http://127.0.0.1:3000",
    "https://hackfest-2-backend.vercel.app"
])

@app.route("/")
def index():
    """Root route for health checks."""
    return jsonify({"status": "running", "message": "RuleForge Backend API is active"}), 200

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT           = Path(__file__).parent.resolve()
RULES_JSON     = ROOT / "rules" / "policy_rules.json"
VIOLATION_JSON = ROOT / "rules" / "violation_report.json"
EXPLAIN_JSON   = ROOT / "rules" / "explanations.json"
UPLOADS_DIR    = ROOT / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
DATA_DIR       = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# ── Pipeline job state (in-memory) ────────────────────────────────────────────
_pipeline_lock   = threading.Lock()
_pipeline_status = {
    "running": False,
    "phase": None,
    "log_lines": [],
    "returncode": None,
    "started_at": None,
    "finished_at": None,
}


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_json_list(text: str) -> list | None:
    m = re.search(r"```json\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        try: return json.loads(m.group(1))
        except Exception: pass
    m = re.search(r"```\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        try: return json.loads(m.group(1))
        except Exception: pass
    start = text.find("["); end = text.rfind("]") + 1
    if start >= 0 and end > start:
        try: return json.loads(text[start:end])
        except Exception: pass
    return None


def load_rules() -> list[dict]:
    if RULES_JSON.exists():
        try: return json.loads(RULES_JSON.read_text(encoding="utf-8"))
        except Exception: pass
    return []


def load_violations() -> list[dict]:
    if VIOLATION_JSON.exists():
        try:
            raw  = VIOLATION_JSON.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, list): return data
            if isinstance(data, dict) and "violations" in data: return data["violations"]
        except json.JSONDecodeError:
            raw       = VIOLATION_JSON.read_text(encoding="utf-8")
            extracted = _extract_json_list(raw)
            if extracted: return extracted
        except Exception: pass
    return []


def load_explanations() -> list[dict]:
    p = ROOT / "rules" / "explanations.json"
    if p.exists():
        try: return json.loads(p.read_text(encoding="utf-8"))
        except Exception: pass
    return []


def load_hitl_decisions() -> dict[str, dict]:
    try:
        from hitl import load_decisions
        return load_decisions()
    except Exception:
        return {}


def load_audit_log(limit: int = 200) -> list[dict]:
    try:
        from audit import get_log
        return get_log(limit=limit)
    except Exception:
        return []


def get_audit_stats() -> dict:
    try:
        from audit import get_stats
        return get_stats()
    except Exception:
        return {"total_events": 0, "pipeline_runs": 0, "hitl_decisions": 0}


def load_version_manifest() -> list[dict]:
    try:
        from tools import load_version_manifest as _lvm
        manifest       = _lvm()
        versions_dir   = ROOT / "rules" / "versions"
        valid_manifest = []
        for entry in manifest:
            archive_name = entry.get("archive", "")
            if archive_name and (versions_dir / archive_name).exists():
                valid_manifest.append(entry)
        return valid_manifest
    except Exception:
        return []


def severity_cls(count: int) -> str:
    if count == 0:   return "CLEAR"
    if count < 50:   return "LOW"
    if count < 500:  return "MEDIUM"
    return "HIGH"


def is_process_running(script_name: str) -> bool:
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            cmdline = proc.info.get('cmdline') or []
            if any(script_name in str(arg) for arg in cmdline):
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False


def last_run_str() -> str:
    if VIOLATION_JSON.exists():
        t = VIOLATION_JSON.stat().st_mtime
        return datetime.fromtimestamp(t).strftime("%d %b %Y · %H:%M:%S")
    return "Never"


# ═══════════════════════════════════════════════════════════════════════════════
# API Endpoints
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/stats", methods=["GET"])
def get_stats():
    """KPI summary — mirrors the Streamlit overview tab."""
    rules      = load_rules()
    violations = load_violations()
    hitl       = load_hitl_decisions()

    total_v   = sum(v.get("violation_count", 0) for v in violations)
    triggered = sum(1 for v in violations if v.get("violation_count", 0) > 0)
    high_sev  = sum(1 for v in violations if v.get("violation_count", 0) >= 500)
    med_sev   = sum(1 for v in violations if 50 <= v.get("violation_count", 0) < 500)
    low_sev   = sum(1 for v in violations if 0 < v.get("violation_count", 0) < 50)
    blocked   = sum(1 for v in violations if v.get("status") == "BLOCKED")

    return jsonify({
        "total_rules":      len(rules),
        "total_violations": total_v,
        "rules_triggered":  triggered,
        "rules_checked":    len(violations),
        "high_severity":    high_sev,
        "medium_severity":  med_sev,
        "low_severity":     low_sev,
        "clear":            len(violations) - triggered,
        "queries_blocked":  blocked,
        "last_run":         last_run_str(),
        "hitl_summary": {
            "confirmed": sum(1 for d in hitl.values() if d.get("action") == "CONFIRMED"),
            "dismissed": sum(1 for d in hitl.values() if d.get("action") == "DISMISSED"),
            "escalated": sum(1 for d in hitl.values() if d.get("action") == "ESCALATED"),
            "pending":   sum(1 for v in violations if v.get("rule_id") not in hitl),
        },
    })


@app.route("/api/rules", methods=["GET"])
def get_rules():
    """All policy rules with optional filtering."""
    rules       = load_rules()
    violations  = load_violations()
    v_map       = {v.get("rule_id"): v.get("violation_count", 0) for v in violations}

    search   = request.args.get("search", "").lower()
    rtype    = request.args.get("type", "")
    operator = request.args.get("operator", "")

    filtered = rules
    if search:
        filtered = [r for r in filtered if
                    search in r.get("description", "").lower() or
                    search in r.get("condition_field", "").lower() or
                    search in r.get("sql_hint", "").lower()]
    if rtype:
        filtered = [r for r in filtered if r.get("rule_type") == rtype]
    if operator:
        filtered = [r for r in filtered if r.get("operator") == operator]

    result = []
    for r in filtered:
        rid = r.get("id", "")
        result.append({
            "id":          rid,
            "rule_type":   r.get("rule_type", "—"),
            "condition_field": r.get("condition_field", "—"),
            "operator":    r.get("operator", "—"),
            "threshold_value": r.get("threshold_value", "—"),
            "description": r.get("description", ""),
            "sql_hint":    r.get("sql_hint", ""),
            "violations":  v_map.get(rid, 0),
            "_fingerprint": r.get("_fingerprint"),
        })

    types     = sorted(set(r.get("rule_type", "unknown") for r in rules))
    operators = sorted(set(r.get("operator", "?") for r in rules))

    return jsonify({"rules": result, "types": types, "operators": operators, "total": len(rules)})


@app.route("/api/violations", methods=["GET"])
def get_violations():
    """Violations list with HITL decisions merged in."""
    violations = load_violations()
    hitl       = load_hitl_decisions()
    explanations = {e.get("rule_id"): e for e in load_explanations()}

    sort_by = request.args.get("sort", "desc")
    show_triggered = request.args.get("triggered_only", "false").lower() == "true"

    result = []
    for v in violations:
        rule_id = v.get("rule_id", "?")
        count   = v.get("violation_count", 0)

        if show_triggered and count == 0:
            continue

        hitl_data = hitl.get(rule_id, {})
        exp       = explanations.get(rule_id, {})

        result.append({
            "rule_id":          rule_id,
            "description":      v.get("rule_description", "No description"),
            "violation_count":  count,
            "status":           v.get("status", "?"),
            "sql":              v.get("sql", ""),
            "sample_violations": v.get("sample_violations", [])[:5],
            "reason":           v.get("reason", ""),
            "severity":         severity_cls(count),
            "hitl_action":      hitl_data.get("action", "PENDING"),
            "hitl_analyst":     hitl_data.get("analyst", ""),
            "hitl_timestamp":   hitl_data.get("timestamp", ""),
            "hitl_notes":       hitl_data.get("notes", ""),
            # AI explanation data if available
            "alert_headline":   exp.get("alert_headline", ""),
            "plain_english":    exp.get("plain_english", ""),
            "recommended_action": exp.get("recommended_action", ""),
            "policy_reference": exp.get("policy_reference", ""),
            "risk_level":       exp.get("risk_level", ""),
            "generated_by":     exp.get("generated_by", ""),
        })

    if sort_by == "asc":
        result.sort(key=lambda x: x["violation_count"])
    elif sort_by == "rule":
        result.sort(key=lambda x: x["rule_id"])
    else:
        result.sort(key=lambda x: x["violation_count"], reverse=True)

    return jsonify({"violations": result, "total": len(result)})


@app.route("/api/explanations", methods=["GET"])
def get_explanations():
    """AI explanations (Phase 3 output)."""
    explanations = load_explanations()
    return jsonify({"explanations": explanations, "total": len(explanations)})


@app.route("/api/audit-log", methods=["GET"])
def get_audit_log():
    """Audit log entries + stats."""
    limit = int(request.args.get("limit", 200))
    rows  = load_audit_log(limit=limit)
    stats = get_audit_stats()

    formatted = []
    for row in rows:
        try:
            details = json.loads(row.get("details_json") or "{}")
        except Exception:
            details = {}
        formatted.append({
            "id":         row.get("id"),
            "ts":         row.get("ts", ""),
            "phase":      row.get("phase", ""),
            "event_type": row.get("event_type", ""),
            "rule_id":    row.get("rule_id"),
            "details":    details,
        })

    return jsonify({"logs": formatted, "stats": stats})


@app.route("/api/versions", methods=["GET"])
def get_versions():
    """Policy version history."""
    manifest = load_version_manifest()
    rules    = load_rules()
    current_fps = {r.get("_fingerprint") for r in rules}

    result = []
    for entry in manifest:
        result.append({
            "version":    entry.get("version"),
            "timestamp":  entry.get("timestamp", ""),
            "rule_count": entry.get("rule_count", 0),
            "pdf_source": entry.get("pdf_source", ""),
            "archive":    entry.get("archive", ""),
        })
    return jsonify({"versions": result})


@app.route("/api/versions/<int:version_num>", methods=["GET"])
def get_version_rules(version_num):
    """Load rules from a specific archived policy version."""
    print(f"[DEBUG] get_version_rules called with version_num={version_num}")
    try:
        from tools import load_rules_at_version
        rules = load_rules_at_version(version_num)
        manifest = load_version_manifest()
        entry = next((e for e in manifest if e.get("version") == version_num), {})
        print(f"[DEBUG] Found {len(rules)} rules for version {version_num}")
        return jsonify({
            "version":    version_num,
            "timestamp":  entry.get("timestamp", ""),
            "pdf_source": entry.get("pdf_source", ""),
            "rule_count": len(rules),
            "rules":      rules,
        })
    except Exception as e:
        print(f"[ERROR] get_version_rules failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/live-status", methods=["GET"])
def get_live_status():
    """Live monitor — process status + live violations + config."""
    from config import INGESTER_BATCH_SIZE, INGESTER_INTERVAL, WATCHDOG_INTERVAL, LIVE_REPORT_PATH

    ingester_running = is_process_running("ingester.py")
    watchdog_running = is_process_running("turgon_watchdog.py")

    live_violations = []
    live_count_db   = None

    if LIVE_REPORT_PATH.exists():
        try:
            data = json.loads(LIVE_REPORT_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                live_violations = data
        except Exception:
            pass

    try:
        import duckdb
        from config import DUCKDB_PATH, LIVE_TABLE_NAME
        if DUCKDB_PATH.exists():
            conn = duckdb.connect(database=str(DUCKDB_PATH), read_only=True)
            try:
                live_count_db = conn.execute(f"SELECT COUNT(*) FROM {LIVE_TABLE_NAME}").fetchone()[0]
            except Exception:
                live_count_db = None
            finally:
                conn.close()
    except Exception:
        live_count_db = None

    # Annotate live violations with severity
    annotated = []
    for v in live_violations:
        count = v.get("violation_count", 0)
        annotated.append({**v, "severity": severity_cls(count)})

    return jsonify({
        "ingester_running": ingester_running,
        "watchdog_running": watchdog_running,
        "live_transaction_count": live_count_db,
        "live_violations": annotated,
        "config": {
            "batch_size":      INGESTER_BATCH_SIZE,
            "ingester_interval": INGESTER_INTERVAL,
            "watchdog_interval": WATCHDOG_INTERVAL,
        },
        "last_update": datetime.fromtimestamp(
            LIVE_REPORT_PATH.stat().st_mtime
        ).strftime("%H:%M:%S") if LIVE_REPORT_PATH.exists() else None,
    })


@app.route("/api/hitl-decision", methods=["POST"])
def post_hitl_decision():
    """Save an analyst HITL decision (confirm / dismiss / escalate)."""
    data    = request.get_json()
    rule_id = data.get("rule_id", "").strip()
    action  = data.get("action", "").strip().upper()
    analyst = data.get("analyst", "analyst")
    notes   = data.get("notes", "")

    if not rule_id or not action:
        return jsonify({"error": "rule_id and action are required"}), 400

    valid = {"CONFIRMED", "DISMISSED", "ESCALATED", "PENDING"}
    if action not in valid:
        return jsonify({"error": f"action must be one of {valid}"}), 400

    try:
        from hitl import save_decision
        from audit import log_hitl_decision
        decision = save_decision(rule_id, action, analyst, notes)
        log_hitl_decision(rule_id, action, analyst, notes)
        return jsonify({"success": True, "decision": decision})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/upload", methods=["POST"])
def upload_file():
    """Upload a PDF file to the uploads/ directory."""
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400
    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error": "No selected file"}), 400

    save_path = UPLOADS_DIR / f.filename
    f.save(str(save_path))
    return jsonify({
        "success":  True,
        "filename": f.filename,
        "path":     str(save_path),
        "size":     save_path.stat().st_size,
    })


@app.route("/api/upload-csv", methods=["POST"])
def upload_csv():
    """Upload a CSV dataset to the data/ directory and run setup_duckdb.py."""
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400
    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error": "No selected file"}), 400
    if not f.filename.endswith('.csv'):
        return jsonify({"error": "Only CSV files are allowed"}), 400

    save_path = DATA_DIR / f.filename
    f.save(str(save_path))
    
    # Run setup_duckdb.py
    try:
        venv_python = ROOT / "venv" / "Scripts" / "python.exe"
        python_exe  = str(venv_python) if venv_python.exists() else sys.executable
        cmd = [python_exe, str(ROOT / "data" / "setup_duckdb.py")]
        
        process = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
        if process.returncode != 0:
            return jsonify({"error": "Database setup failed", "logs": process.stderr + "\n" + process.stdout}), 500
            
        return jsonify({
            "success":  True,
            "filename": f.filename,
            "message": "Dataset uploaded and DuckDB setup completed."
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _run_pipeline_thread(cmd: list[str], phase: str):
    """Background thread that runs main.py and captures output."""
    global _pipeline_status
    with _pipeline_lock:
        _pipeline_status["running"]     = True
        _pipeline_status["phase"]       = phase
        _pipeline_status["log_lines"]   = []
        _pipeline_status["returncode"]  = None
        _pipeline_status["started_at"]  = datetime.utcnow().isoformat() + "Z"
        _pipeline_status["finished_at"] = None

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(ROOT),
            encoding="utf-8",
            errors="replace",
        )
        for line in process.stdout:
            with _pipeline_lock:
                _pipeline_status["log_lines"].append(line.rstrip())
        process.wait()
        with _pipeline_lock:
            _pipeline_status["returncode"]  = process.returncode
            _pipeline_status["running"]     = False
            _pipeline_status["finished_at"] = datetime.utcnow().isoformat() + "Z"
    except Exception as e:
        with _pipeline_lock:
            _pipeline_status["log_lines"].append(f"[ERROR] {e}")
            _pipeline_status["running"]     = False
            _pipeline_status["returncode"]  = -1
            _pipeline_status["finished_at"] = datetime.utcnow().isoformat() + "Z"


@app.route("/api/run", methods=["POST"])
def run_pipeline():
    """Trigger a pipeline run in a background thread."""
    global _pipeline_status
    with _pipeline_lock:
        if _pipeline_status["running"]:
            return jsonify({"error": "Pipeline is already running"}), 409

    data       = request.get_json() or {}
    phase_flag = data.get("phase", "123")  # "1", "12", "123", "2", "3"
    pdf_name   = data.get("pdf", "")

    venv_python = ROOT / "venv" / "Scripts" / "python.exe"
    python_exe  = str(venv_python) if venv_python.exists() else sys.executable

    cmd = [python_exe, str(ROOT / "main.py"), "--phase", phase_flag]
    if pdf_name:
        pdf_path = UPLOADS_DIR / pdf_name
        if not pdf_path.exists():
            return jsonify({"error": f"PDF not found: {pdf_name}"}), 400
        cmd += ["--pdf", str(pdf_path)]
    elif phase_flag in ("1", "12", "123"):
        return jsonify({"error": "PDF required for Phase 1"}), 400

    t = threading.Thread(target=_run_pipeline_thread, args=(cmd, phase_flag), daemon=True)
    t.start()

    return jsonify({"success": True, "phase": phase_flag, "message": "Pipeline started"})


@app.route("/api/pipeline-status", methods=["GET"])
def pipeline_status():
    """Poll pipeline status and log lines."""
    offset = int(request.args.get("offset", 0))
    with _pipeline_lock:
        status = {
            "running":     _pipeline_status["running"],
            "phase":       _pipeline_status["phase"],
            "returncode":  _pipeline_status["returncode"],
            "started_at":  _pipeline_status["started_at"],
            "finished_at": _pipeline_status["finished_at"],
            "log_lines":   _pipeline_status["log_lines"][offset:],
            "total_lines": len(_pipeline_status["log_lines"]),
        }
    return jsonify(status)


@app.route("/api/export/violations", methods=["GET"])
def export_violations_csv():
    """Export violations as CSV download."""
    violations   = load_violations()
    hitl         = load_hitl_decisions()

    rows = []
    for v in violations:
        rule_id = v.get("rule_id", "?")
        count   = v.get("violation_count", 0)
        hitl_action = hitl.get(rule_id, {}).get("action", "PENDING")
        rows.append({
            "rule_id":         rule_id,
            "description":     v.get("rule_description", ""),
            "violation_count": count,
            "severity":        severity_cls(count),
            "status":          v.get("status", ""),
            "hitl_action":     hitl_action,
            "sql":             v.get("sql", ""),
        })

    df  = pd.DataFrame(rows)
    out = io.StringIO()
    df.to_csv(out, index=False)
    csv_data = out.getvalue()

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="ruleforge_violations_{datetime.now().strftime("%Y%m%d_%H%M")}.csv"'
        },
    )


@app.route("/api/export/report", methods=["GET"])
def export_compliance_report():
    """Export compliance report as JSON download."""
    violations   = load_violations()
    explanations = load_explanations()
    hitl         = load_hitl_decisions()

    report = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "pipeline_summary": {
            "rules_checked":    len(violations),
            "rules_triggered":  sum(1 for v in violations if v.get("violation_count", 0) > 0),
            "total_violations": sum(v.get("violation_count", 0) for v in violations),
        },
        "hitl_summary": {
            "confirmed": sum(1 for d in hitl.values() if d.get("action") == "CONFIRMED"),
            "dismissed": sum(1 for d in hitl.values() if d.get("action") == "DISMISSED"),
            "escalated": sum(1 for d in hitl.values() if d.get("action") == "ESCALATED"),
            "pending":   sum(1 for v in violations if v.get("rule_id") not in hitl),
        },
        "violations":    violations,
        "explanations":  explanations,
        "hitl_decisions": list(hitl.values()),
    }

    return Response(
        json.dumps(report, indent=2, ensure_ascii=False),
        mimetype="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="ruleforge_compliance_report_{datetime.now().strftime("%Y%m%d_%H%M")}.json"'
        },
    )


@app.route("/api/health", methods=["GET"])
def health():
    """Health check."""
    return jsonify({
        "status": "ok",
        "rules_loaded":      len(load_rules()),
        "violations_loaded": len(load_violations()),
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  RuleForge Flask Backend")
    print("  API available at: http://localhost:5000")
    print("  React frontend:   http://localhost:3000")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
