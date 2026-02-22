"""
hitl.py — Human-in-the-Loop state management for Turgon.

Stores analyst decisions (CONFIRMED / DISMISSED / ESCALATED) for each rule
in a JSON file. Append-only per rule_id — latest decision wins.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

ROOT           = Path(__file__).parent.resolve()
HITL_JSON      = ROOT / "rules" / "hitl_decisions.json"
HITL_JSON.parent.mkdir(parents=True, exist_ok=True)

VALID_ACTIONS = {"CONFIRMED", "DISMISSED", "ESCALATED", "PENDING"}


def load_decisions() -> dict[str, dict]:
    """Return {rule_id: decision_dict}."""
    if HITL_JSON.exists():
        try:
            return json.loads(HITL_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_decision(
    rule_id: str,
    action: str,
    analyst: str = "analyst",
    notes: str = "",
) -> dict:
    """
    Upsert a decision for rule_id.
    Returns the saved decision dict.
    """
    if action not in VALID_ACTIONS:
        raise ValueError(f"Invalid action '{action}'. Must be one of {VALID_ACTIONS}")

    decisions = load_decisions()
    decision = {
        "rule_id":   rule_id,
        "action":    action,
        "analyst":   analyst,
        "notes":     notes,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    decisions[rule_id] = decision
    HITL_JSON.write_text(
        json.dumps(decisions, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return decision


def get_decision(rule_id: str) -> dict | None:
    return load_decisions().get(rule_id)


def clear_decision(rule_id: str) -> None:
    decisions = load_decisions()
    decisions.pop(rule_id, None)
    HITL_JSON.write_text(json.dumps(decisions, indent=2), encoding="utf-8")


def summary() -> dict:
    decisions = load_decisions()
    counts: dict[str, int] = {}
    for d in decisions.values():
        a = d.get("action", "UNKNOWN")
        counts[a] = counts.get(a, 0) + 1
    return counts
