"""
tasks.py — CrewAI Task definitions for Turgon.

Task 1: IngestAndStructureRulesTask — Phase 1 RuleForge
Task 2: GenerateAndExecuteSQLTask   — Phase 2 Secure Monitor
"""

import json
from pathlib import Path

from crewai import Task

from config import RULES_JSON_PATH


# AML Database schema — injected into Phase 2 task context so the agent
# knows the exact column names without needing to query information_schema
AML_DB_SCHEMA = """
TABLE: transactions
  - Timestamp            TEXT     (format: "2022/09/01 00:00" or epoch seconds)
  - From_Bank            TEXT     (originating bank name/code)
  - From_Account         TEXT     (originating account identifier)
  - To_Bank              TEXT     (receiving bank name/code)
  - To_Account           TEXT     (receiving account identifier)
  - Amount_Received      DOUBLE   (amount credited to the receiving account)
  - Receiving_Currency   TEXT     (ISO currency code of received amount)
  - Amount_Paid          DOUBLE   (amount debited from the sending account)
  - Payment_Currency     TEXT     (ISO currency code of paid amount)
  - Payment_Format       TEXT     (e.g., Reinvestment, Wire, ACH, Cheque, Credit Card, Cash)
  - Is_Laundering        INTEGER  (1 = confirmed money laundering, 0 = legitimate)

NOTE: Column names in SQL must use the exact names above (with underscores).
      The table is in the 'aml' schema — always use: FROM aml.transactions
      All monetary thresholds in the rules should be compared against Amount_Paid or Amount_Received.
"""

# Batch size hint passed to agent — process rules in groups for efficiency
BATCH_HINT = 5


def build_ingest_task(agent, pdf_path: str) -> Task:
    """
    Phase 1: Parse PDF and extract structured policy rules.

    The agent will:
    1. Parse the PDF using docling_pdf_parser
    2. Extract all IF/THEN regulatory rules
    3. Format them as JSON with the PolicyRule schema
    4. Save them using rule_store_writer
    """
    return Task(
        description=f"""
You are given the path to a regulatory compliance PDF document.

**Your objective** is to extract every actionable policy rule from the document and convert it into structured Policy-as-Code JSON.

**Step 1 — Parse the document**
Use the `docling_pdf_parser` tool with:
  pdf_path: "{pdf_path}"

**Step 2 — Extract all rules**
Read the parsed text carefully. Identify every rule, threshold, or compliance condition. Common patterns to look for:
- "Transactions exceeding $X must be reported"
- "Any account with more than N transactions in Y period is flagged"
- "Transfers to/from high-risk jurisdictions are suspicious"
- Numeric thresholds tied to specific actions or conditions
- IF/THEN conditional logic

**Step 3 — Structure as JSON**
Convert EACH rule into a JSON object with these EXACT fields:
```json
{{
  "id": "RULE_001",
  "rule_type": "threshold | pattern | frequency | jurisdiction | ratio",
  "description": "Plain-English description of the rule",
  "condition_field": "Amount_Paid | Amount_Received | From_Bank | Payment_Format | etc.",
  "operator": "> | < | >= | <= | == | != | IN | NOT IN",
  "threshold_value": 10000,
  "sql_hint": "Brief hint to the SQL engineer about how to query this rule"
}}
```

**Step 4 — Save the rules**
You MUST use the `rule_store_writer` tool, passing the complete JSON array of ALL extracted rules as a single string argument. If for any reason the tool fails, you MUST output the raw JSON array in your final answer so the system can save it.

**Important**: 
- Extract EVERY rule, even minor ones. Thoroughness is critical.
- If the document has no explicit threshold, use contextual inference.
- Assign sequential IDs: RULE_001, RULE_002, etc.
- The sql_hint should reference actual column names from the AML schema: Amount_Paid, Amount_Received, From_Bank, To_Bank, Payment_Format, Payment_Currency, Receiving_Currency, To_Account, From_Account, Is_Laundering.
- Make sure to output standard, strictly valid JSON.
""",
        expected_output=(
            "A complete, valid JSON array containing all extracted rules matching the PolicyRule schema. "
            "Whether you successfully call rule_store_writer or not, your final answer MUST contain "
            "the raw JSON array block so the system can parse it as a fallback."
        ),
        agent=agent,
    )


def build_sql_generation_task(agent, context_tasks: list | None = None) -> Task:
    """
    Phase 2: Translate rules to SQL and execute against the AML sandbox.

    The agent will:
    1. Read the policy_rules.json file
    2. For each rule, generate a precise SELECT query
    3. Validate each query via secure_sql_validator
    4. Execute via duckdb_execution_sandbox
    5. Return a comprehensive violation report
    """
    rules_content = "No rules file found — run Phase 1 first."
    if RULES_JSON_PATH.exists():
        try:
            rules = json.loads(RULES_JSON_PATH.read_text(encoding="utf-8"))
            rules_content = json.dumps(rules, indent=2)
        except Exception:
            pass

    return Task(
        description=f"""
You are a compliance SQL engineer. Your job is to execute automated compliance checks 
against the AML transaction database using structured policy rules.

**Database Schema**:
{AML_DB_SCHEMA}

**Policy Rules (from Rule Store)**:
```json
{rules_content}
```

**Your objective**: For EACH rule in the list above, generate and execute a SQL query.

**Process for each rule** (process in batches of {BATCH_HINT}):

1. **Generate SQL**: Write a SELECT query that finds transactions VIOLATING the rule.
   - Use the exact column names from the schema above
   - Use the rule's `condition_field`, `operator`, and `threshold_value`
   - Include relevant columns: From_Account, To_Account, Amount_Paid, Amount_Received, 
     Payment_Format, Is_Laundering, and any condition fields
   - Add `WHERE Is_Laundering = 1` only for validation cross-checks, not for the actual filter
   
   Example for a threshold rule (amount > 10000):
   ```sql
   SELECT From_Bank, From_Account, To_Bank, To_Account, 
          Amount_Paid, Payment_Currency, Payment_Format, Timestamp
   FROM aml.transactions
   WHERE Amount_Paid > 10000
   ```

2. **Validate**: Use `secure_sql_validator` with your generated SQL.
   - If validation fails, rewrite the query and try again
   - NEVER use DROP, DELETE, UPDATE, INSERT, or any DDL

3. **Execute**: Use `duckdb_execution_sandbox` with the validated SQL and the rule's `id`.

4. **Record results**: Note the rule_id, SQL used, row_count, and first few violation rows.

**Final output**: A JSON array where each element is:
```json
{{
  "rule_id": "RULE_001",
  "rule_description": "...",
  "sql": "SELECT ...",
  "violation_count": 42,
  "sample_violations": [{{...}}, {{...}}],
  "status": "SUCCESS | SQL_ERROR | BLOCKED"
}}
```

Save this JSON array to a file called `rules/violation_report.json` using Python's 
file writing (you can write it inline in your final answer or instruct the system to save it,
but make sure the final output contains the complete JSON).
""",
        expected_output=(
            "A complete JSON violation report array containing one entry per rule, "
            "each with rule_id, rule_description, sql, violation_count, sample_violations, "
            "and status. The report must cover ALL rules in the rule store."
        ),
        agent=agent,
        context=context_tasks,
    )
