"""
agents.py — CrewAI Agent definitions for Turgon.

Phase 1: RuleArchitectAgent  — ingests PDF, extracts structured policy rules
Phase 2: QueryEngineerAgent  — translates rules to SQL, executes safely
"""

from crewai import Agent

from config import AGENT_MAX_ITER, AGENT_VERBOSE, get_llm
from tools import (
    DoclingPDFParserTool,
    DuckDBExecutionSandboxTool,
    RuleStoreWriterTool,
    SecureSQLValidatorTool,
)


def build_rule_architect_agent() -> Agent:
    """
    Phase 1 — RuleArchitectAgent.

    Expert at reading legal and regulatory text (AML directives, FinCEN guidance,
    Basel III frameworks) and converting natural language rules into structured
    Policy-as-Code JSON.
    """
    return Agent(
        role="Regulatory Intelligence Analyst",
        goal=(
            "Transform unstructured regulatory PDF documents into precise, structured "
            "Policy-as-Code JSON rules. Each rule must capture the exact condition, "
            "threshold, and enforcement logic embedded in the legal text, ready for "
            "automated SQL generation."
        ),
        backstory=(
            "You are a world-class compliance expert with 20 years of experience "
            "in financial regulation — AML/CFT frameworks, FinCEN advisories, FATF "
            "recommendations, and Basel III compliance. You are obsessively precise: "
            "you never paraphrase when the law states a specific number, and you never "
            "miss an IF/THEN condition buried in a footnote. You have also mastered "
            "structured data modelling and can express any legal rule as a clean "
            "machine-readable JSON object with field: id, rule_type, description, "
            "condition_field, operator, threshold_value, and sql_hint."
        ),
        tools=[DoclingPDFParserTool(), RuleStoreWriterTool()],
        llm=get_llm(),
        max_iter=AGENT_MAX_ITER,
        verbose=AGENT_VERBOSE,
        allow_delegation=False,
        memory=False,  # stateless — all state lives in JSON store
    )


def build_query_engineer_agent() -> Agent:
    """
    Phase 2 — QueryEngineerAgent.

    Reads the structured Policy-as-Code JSON and translates each rule into
    precise, read-only SQL queries for the AML DuckDB sandbox.
    """
    return Agent(
        role="Secure Database Query Engineer",
        goal=(
            "Read structured policy rules from the JSON rule store and translate each "
            "rule's logic into a precise, read-only SELECT SQL query. Validate every "
            "query for safety before execution. Execute each validated query against "
            "the AML transaction database and return a structured violation report."
        ),
        backstory=(
            "You are a senior data engineer specialising in financial crime analytics "
            "and compliance SQL. You know the IBM AML transaction database schema "
            "intimately — columns like Timestamp, From Bank, Account, To Bank, "
            "Account.1, Amount Received, Receiving Currency, Amount Paid, "
            "Payment Currency, Payment Format, and Is Laundering. "
            "You write SQL that is razor-sharp and read-only. You never touch DDL. "
            "You think in terms of thresholds, aggregations, and transaction patterns. "
            "Every query you write is reviewed by a security validator before it "
            "reaches the database — you welcome this and write SQL that will pass "
            "validation on the first attempt."
        ),
        tools=[
            SecureSQLValidatorTool(),
            DuckDBExecutionSandboxTool(),
        ],
        llm=get_llm(),
        max_iter=AGENT_MAX_ITER,
        verbose=AGENT_VERBOSE,
        allow_delegation=False,
        memory=False,
    )
