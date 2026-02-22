"""
phase3_explainer.py — Phase 3: LLM-powered Explanation Agent

Reads violation_report.json + policy_rules.json and uses the LLM to generate
a plain-English, analyst-ready alert for each rule that triggered violations.

Output: rules/explanations.json
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT             = Path(__file__).parent.resolve()
RULES_JSON       = ROOT / "rules" / "policy_rules.json"
VIOLATIONS_JSON  = ROOT / "rules" / "violation_report.json"
EXPLANATIONS_JSON = ROOT / "rules" / "explanations.json"

RISK_THRESHOLDS = {"HIGH": 500, "MEDIUM": 50, "LOW": 1}

# ── Fallback deterministic explainer (no LLM required) ────────────────────────

_RULE_TYPE_CONTEXT = {
    "threshold":   "exceeds a regulatory reporting threshold",
    "frequency":   "shows unusual transaction frequency patterns",
    "format":      "uses a payment format flagged for AML risk",
    "currency":    "involves a currency mismatch between sender and receiver",
    "laundering":  "is confirmed as a money-laundering transaction",
    "pattern":     "matches a structuring or layering pattern",
    "geo":         "involves a high-risk jurisdiction or bank",
}

_ACTION_MAP = {
    "HIGH":   "Immediately file a Suspicious Activity Report (SAR) and notify compliance officers.",
    "MEDIUM": "Review flagged accounts; file SAR if further indicators are present.",
    "LOW":    "Monitor the accounts and retain records for 5 years per BSA requirements.",
}


def _risk_level(count: int) -> str:
    if count >= RISK_THRESHOLDS["HIGH"]:   return "HIGH"
    if count >= RISK_THRESHOLDS["MEDIUM"]: return "MEDIUM"
    if count >= 1:                          return "LOW"
    return "CLEAR"


def _deterministic_explanation(rule: dict, violation: dict) -> dict:
    """Generate a structured plain-English alert without calling the LLM."""
    rule_id   = rule.get("id", violation.get("rule_id", "?"))
    desc      = rule.get("description", violation.get("rule_description", ""))
    count     = violation.get("violation_count", 0)
    field     = rule.get("condition_field", "transaction amount")
    op        = rule.get("operator", ">")
    threshold = rule.get("threshold_value", "")
    rule_type = rule.get("rule_type", "threshold").lower()
    risk      = _risk_level(count)

    type_ctx = _RULE_TYPE_CONTEXT.get(rule_type, "violates a compliance policy rule")
    action   = _ACTION_MAP.get(risk, "Review the flagged transactions.")

    headline = (
        f"{count:,} Transaction{'s' if count != 1 else ''} "
        f"{'Violate' if count != 1 else 'Violates'} "
        f"{desc[:60].rstrip('.')}."
    )

    plain = (
        f"Our automated compliance scan identified {count:,} transaction"
        f"{'s' if count != 1 else ''} that {type_ctx}. "
        f"Each transaction has {field} {op} {threshold}, which "
        f"triggers the policy rule: \"{desc}\". "
        f"This constitutes a {risk}-severity finding requiring analyst review."
    )

    return {
        "rule_id":           rule_id,
        "alert_headline":    headline,
        "plain_english":     plain,
        "risk_level":        risk,
        "violation_count":   count,
        "recommended_action": action,
        "policy_reference":  desc,
        "generated_by":      "deterministic",
    }


def _llm_explanation(rule: dict, violation: dict, llm) -> dict | None:
    """
    Try to enrich the explanation using the LLM.
    Falls back to None on any error (caller uses deterministic fallback).
    """
    try:
        rule_id = rule.get("id", "?")
        desc    = rule.get("description", "")
        count   = violation.get("violation_count", 0)
        samples = violation.get("sample_violations", [])[:2]
        risk    = _risk_level(count)

        sample_text = json.dumps(samples, indent=2) if samples else "No sample rows."

        prompt = f"""You are a senior AML compliance analyst. Write a concise, professional alert 
for the following compliance rule violation. 

Rule: {desc}
Violation count: {count:,} transactions flagged
Risk level: {risk}
Sample flagged transactions:
{sample_text}

Return ONLY a JSON object with these exact keys:
  "alert_headline" - one-sentence headline (max 15 words)
  "plain_english"  - 2-3 sentence plain-English explanation for a human analyst
  "recommended_action" - one concrete next step the analyst should take
  "policy_reference" - the exact policy clause or rule being violated

Return ONLY the JSON object, no other text."""

        response = llm.call([{"role": "user", "content": prompt}])
        content  = response if isinstance(response, str) else str(response)

        # Extract JSON from response
        import re
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if m:
            data = json.loads(m.group(0))
            return {
                "rule_id":           rule_id,
                "alert_headline":    data.get("alert_headline", ""),
                "plain_english":     data.get("plain_english", ""),
                "risk_level":        risk,
                "violation_count":   count,
                "recommended_action": data.get("recommended_action", ""),
                "policy_reference":  data.get("policy_reference", desc),
                "generated_by":      "llm",
            }
    except Exception:
        pass
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def run(use_llm: bool = True) -> list[dict]:
    if not VIOLATIONS_JSON.exists():
        print("[Phase 3] No violation report found. Run Phase 2 first.")
        return []
    if not RULES_JSON.exists():
        print("[Phase 3] No rules file found. Run Phase 1 first.")
        return []

    violations: list[dict] = json.loads(VIOLATIONS_JSON.read_text(encoding="utf-8"))
    rules_raw:  list[dict] = json.loads(RULES_JSON.read_text(encoding="utf-8"))

    # Build rule lookup
    rule_map: dict[str, dict] = {r.get("id", ""): r for r in rules_raw}

    # Only explain triggered violations
    triggered = [v for v in violations if v.get("violation_count", 0) > 0]
    print(f"[Phase 3] Generating explanations for {len(triggered)} triggered rules...")

    llm = None
    if use_llm:
        try:
            from config import get_llm
            llm = get_llm()
            print("[Phase 3] LLM loaded — using AI-enriched explanations.")
        except Exception as e:
            print(f"[Phase 3] LLM unavailable ({e}), using deterministic fallback.")
            llm = None

    t0 = time.time()
    explanations: list[dict] = []

    for v in triggered:
        rule_id = v.get("rule_id", "")
        rule    = rule_map.get(rule_id, {})
        rule.setdefault("id", rule_id)

        explanation = None
        if llm and use_llm:
            explanation = _llm_explanation(rule, v, llm)

        if explanation is None:
            explanation = _deterministic_explanation(rule, v)

        explanations.append(explanation)
        marker = "AI" if explanation.get("generated_by") == "llm" else "DET"
        print(f"  [{rule_id}] {marker} — {explanation['risk_level']} risk — {explanation['alert_headline'][:60]}")

    duration = time.time() - t0

    # Also include CLEAR rules (count=0) with minimal entries
    for v in violations:
        if v.get("violation_count", 0) == 0:
            rule_id = v.get("rule_id", "")
            explanations.append({
                "rule_id":            rule_id,
                "alert_headline":     "No violations detected",
                "plain_english":      f"Rule {rule_id} found no matching transactions in the dataset. This policy appears to be compliant.",
                "risk_level":         "CLEAR",
                "violation_count":    0,
                "recommended_action": "No action required. Continue routine monitoring.",
                "policy_reference":   v.get("rule_description", ""),
                "generated_by":       "deterministic",
            })

    EXPLANATIONS_JSON.parent.mkdir(parents=True, exist_ok=True)
    EXPLANATIONS_JSON.write_text(
        json.dumps(explanations, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n[Phase 3] Explanations saved -> {EXPLANATIONS_JSON}")
    print(f"[Phase 3] {len(triggered)} rules explained in {duration:.1f}s")

    # Audit log
    try:
        from audit import log_explanation_run
        log_explanation_run(len(triggered), duration)
    except Exception:
        pass

    return explanations


if __name__ == "__main__":
    import sys
    use_llm = "--no-llm" not in sys.argv
    results = run(use_llm=use_llm)
    print(f"\nDone. {len(results)} explanations generated.")
