"""
main.py — Turgon pipeline entrypoint.

Usage:
  python main.py --pdf uploads/aml_policy.pdf

Runs a sequential CrewAI pipeline:
  Phase 1: RuleArchitectAgent  → parses PDF → saves policy_rules.json
  Phase 2: QueryEngineerAgent  → generates SQL → executes → saves violation_report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# ── Windows UTF-8 fix ──────────────────────────────────────────────────────────
# CrewAI's internal event bus prints emoji (🚀, 🔧) which crash on Windows cp1252.
# Reconfigure stdout/stderr to UTF-8 so these are safe to print.
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    os.environ.setdefault("PYTHONUTF8", "1")

from crewai import Crew, Process
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from agents import build_query_engineer_agent, build_rule_architect_agent
from config import RULES_DIR, UPLOADS_DIR
from tasks import build_ingest_task, build_sql_generation_task
import re

console = Console()


# ── Internal Helpers ───────────────────────────────────────────────────────────

def _extract_json_array(text: str) -> str | None:
    """Extract standard JSON array from LLM output, resilient to markdown blocks."""
    # Try fully-fenced ```json [ ... ] ```
    match = re.search(r"```json\s*(\[\s*\{.*?\}\s*\])\s*```", text, re.DOTALL)
    if match: return match.group(1)
    
    # Try generic fenced ``` [ ... ] ```
    match = re.search(r"```\s*(\[\s*\{.*?\}\s*\])\s*```", text, re.DOTALL)
    if match: return match.group(1)
    
    # Fallback: grab from first [ to last ]
    start = text.find("[")
    end = text.rfind("]") + 1
    if start >= 0 and end > start:
        return text[start:end]
    
    return None

# ── CLI argument parsing ───────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="turgon",
        description="Turgon — Autonomous Policy-to-Enforcement Engine",
    )
    parser.add_argument(
        "--pdf",
        type=str,
        required=False,
        default=None,
        help="Path to the regulatory PDF to ingest (required for Phase 1)",
    )
    parser.add_argument(
        "--phase",
        type=int,
        choices=[1, 2, 3, 12, 23, 123],
        default=123,
        help="Which phase(s) to run: 1=RuleForge, 2=SecureMonitor, 3=Explainer, 12/23/123=combined (default: 123)",
    )
    parser.add_argument(
        "--skip-phase1",
        action="store_true",
        help="Skip Phase 1 (use existing rule store)",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Phase 3: use deterministic explanation (no LLM call)",
    )
    return parser.parse_args()


# ── Phase runners ─────────────────────────────────────────────────────────────

def run_phase1(pdf_path: Path) -> dict:
    """Phase 1: Ingest PDF and extract structured policy rules."""
    console.print(Rule("[bold cyan]Phase 1 — RuleForge: PDF Ingestion & Structuring[/]"))

    from config import RULES_JSON_PATH, RULES_DIR
    from tools import _snapshot_current_rules

    # Archive and wipe previous rules and reports so we start fresh
    if RULES_JSON_PATH.exists():
        _snapshot_current_rules(pdf_source=str(pdf_path))
        
        # Files to clear to reset dashboard state
        to_clear = [
            RULES_JSON_PATH,
            RULES_DIR / "violation_report.json",
            RULES_DIR / "explanations.json",
            RULES_DIR / "violation_report_live.json"
        ]
        for p in to_clear:
            if p.exists():
                p.unlink()

    agent = build_rule_architect_agent()
    task = build_ingest_task(agent, str(pdf_path))

    crew = Crew(
        agents=[agent],
        tasks=[task],
        process=Process.sequential,
        verbose=True,
    )

    start = time.time()
    result = crew.kickoff()
    elapsed = time.time() - start

    raw_output = str(result)
    saved = False

    # Check if policy_rules.json was actually created
    from config import RULES_JSON_PATH
    if RULES_JSON_PATH.exists():
        try:
            # Maybe it already has our rules
            rules = json.loads(RULES_JSON_PATH.read_text(encoding="utf-8"))
            if len(rules) > 0:
                saved = True
        except Exception:
            pass

    # Defensive fallback: If agent forgot to use rule_store_writer but outputted JSON, extract it
    if not saved:
        try:
            json_str = _extract_json_array(raw_output)
            if json_str:
                rules = json.loads(json_str)
                RULES_JSON_PATH.write_text(
                    json.dumps(rules, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                saved = True
                console.print(f"[green]Extracted {len(rules)} rules from output and saved to {RULES_JSON_PATH}[/]")
        except Exception as e:
            console.print(f"[red]Could not extract rules from output: {e}[/]")

    console.print(Panel(
        f"[green]Phase 1 complete in {elapsed:.1f}s[/]\n\n{raw_output[:2000]}{'...' if len(raw_output) > 2000 else ''}",
        title="RuleForge Output",
        border_style="green",
    ))
    return {"phase": 1, "output": raw_output, "elapsed_s": elapsed, "rules_saved": saved}


def run_phase2() -> dict:
    """
    Phase 2: Deterministic SQL generation and DuckDB execution.

    Reads policy_rules.json, builds validated SELECT queries for each rule,
    executes them against the AML sandbox, and writes violation_report.json.
    Uses the deterministic phase2_executor (no LLM) for reliability.
    """
    console.print(Rule("[bold yellow]Phase 2 — Secure Monitor: SQL Generation & Execution[/]"))

    from phase2_executor import run as executor_run, REPORT_JSON

    start = time.time()
    report = executor_run()   # ← deterministic, no LLM required
    elapsed = time.time() - start

    raw_output = json.dumps(report, indent=2)
    report_path = RULES_DIR / "violation_report.json"     # already written by executor

    triggered = sum(1 for r in report if r.get("violation_count", 0) > 0)
    total_v   = sum(r.get("violation_count", 0) for r in report)

    console.print(Panel(
        f"[yellow]Phase 2 complete in {elapsed:.1f}s[/]\n"
        f"Rules checked:   {len(report)}\n"
        f"Rules triggered: [red]{triggered}[/]\n"
        f"Total violations:[red] {total_v:,}[/]\n"
        f"Report saved to: {report_path}",
        title="Secure Monitor Output",
        border_style="yellow",
    ))

    return {"phase": 2, "output": raw_output, "elapsed_s": elapsed, "report_saved": True}


def run_phase3(use_llm: bool = True) -> dict:
    """Phase 3: LLM Explanation Agent — maps violations to plain-English alerts."""
    console.print(Rule("[bold blue]Phase 3 — Explanation Agent: Plain-English Alerts[/]"))

    from phase3_explainer import run as explainer_run

    start = time.time()
    explanations = explainer_run(use_llm=use_llm)
    elapsed = time.time() - start

    triggered = sum(1 for e in explanations if e.get("risk_level") not in ("CLEAR", None))
    console.print(Panel(
        f"[blue]Phase 3 complete in {elapsed:.1f}s[/]\n"
        f"Rules explained:  {len(explanations)}\n"
        f"Active alerts:    [red]{triggered}[/]\n"
        f"Saved to: rules/explanations.json",
        title="Explanation Agent Output",
        border_style="blue",
    ))
    return {"phase": 3, "output": f"{len(explanations)} explanations", "elapsed_s": elapsed}


# ── Summary printer ───────────────────────────────────────────────────────────

def print_summary(results: list[dict]) -> None:
    console.print(Rule("[bold white]Pipeline Summary[/]"))

    table = Table(title="Turgon Run Summary", show_header=True, header_style="bold magenta")
    table.add_column("Phase", style="cyan", width=10)
    table.add_column("Status", width=12)
    table.add_column("Duration", width=12)
    table.add_column("Details", width=50)

    for r in results:
        table.add_row(
            f"Phase {r['phase']}",
            "[green]DONE[/]",
            f"{r['elapsed_s']:.1f}s",
            r.get("output", "")[:80] + "...",
        )

    console.print(table)
    console.print()
    console.print(Panel(
        "[bold]Next step:[/] Launch the Streamlit dashboard to explore violations:\n"
        "[cyan]streamlit run app.py[/]",
        title="What's Next",
        border_style="blue",
    ))


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    # Validate PDF path (only required for Phase 1)
    pdf_path = None
    if args.pdf:
        pdf_path = Path(args.pdf)
        if not pdf_path.is_absolute():
            pdf_path = Path.cwd() / pdf_path

    console.print(Panel(
        f"[bold white]Turgon[/] — Autonomous Policy-to-Enforcement Engine\n"
        f"PDF: [cyan]{pdf_path or 'N/A (Phase 2/3 only)'}[/]\n"
        f"Phase(s): [yellow]{args.phase}[/]",
        border_style="magenta",
    ))

    if args.phase in (1, 12, 123) and not args.skip_phase1:
        if not pdf_path or not pdf_path.exists():
            console.print(f"[red]ERROR: --pdf is required for Phase 1. Got: '{args.pdf}'[/]")
            sys.exit(1)

    results = []

    # Phase 1
    if args.phase in (1, 12, 123) and not args.skip_phase1:
        r1 = run_phase1(pdf_path)
        results.append(r1)

    # Phase 2
    if args.phase in (2, 12, 23, 123):
        r2 = run_phase2()
        results.append(r2)

    # Phase 3
    if args.phase in (3, 23, 123):
        r3 = run_phase3(use_llm=not args.no_llm)
        results.append(r3)

    print_summary(results)


if __name__ == "__main__":
    main()
