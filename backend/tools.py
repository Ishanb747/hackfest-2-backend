"""
tools.py — Custom CrewAI tools for Turgon.

Tools:
  1. DoclingPDFParserTool    — Parse regulatory PDFs with layout awareness
  2. RuleStoreWriterTool     — Save & deduplicate extracted rules to JSON
                               ✨ NEW: Policy versioning with manifest tracking
  3. SecureSQLValidatorTool  — Multi-layer read-only SQL enforcement
  4. DuckDBExecutionSandboxTool — Execute validated SQL against AML database
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Type

import duckdb
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from config import (
    DUCKDB_PATH,
    MAX_VIOLATION_ROWS,
    RULES_JSON_PATH,
)

# ── Versioning paths (sit alongside policy_rules.json) ────────────────────────
_VERSIONS_DIR     = RULES_JSON_PATH.parent / "versions"
_VERSION_MANIFEST = RULES_JSON_PATH.parent / "policy_versions.json"


# ══════════════════════════════════════════════════════════════════════════════
# Versioning helpers
# ══════════════════════════════════════════════════════════════════════════════

def _next_version() -> int:
    """Return the next version number based on the manifest."""
    if _VERSION_MANIFEST.exists():
        try:
            manifest = json.loads(_VERSION_MANIFEST.read_text(encoding="utf-8"))
            return max((e.get("version", 0) for e in manifest), default=0) + 1
        except Exception:
            pass
    return 1


def _snapshot_current_rules(pdf_source: str = "unknown") -> dict | None:
    """
    Copy current policy_rules.json into versions/ and update the manifest.
    Returns the manifest entry dict, or None if there was nothing to snapshot.
    """
    if not RULES_JSON_PATH.exists():
        return None

    _VERSIONS_DIR.mkdir(parents=True, exist_ok=True)

    version = _next_version()
    ts      = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    # Read current rules so we can record rule_count
    try:
        current_rules = json.loads(RULES_JSON_PATH.read_text(encoding="utf-8"))
        rule_count    = len(current_rules)
    except Exception:
        rule_count = 0

    # Copy the file
    archive_name = f"policy_rules_v{version}__{ts}.json"
    archive_path = _VERSIONS_DIR / archive_name
    shutil.copy2(RULES_JSON_PATH, archive_path)

    # Build manifest entry
    entry = {
        "version":    version,
        "timestamp":  datetime.utcnow().isoformat() + "Z",
        "pdf_source": pdf_source,
        "rule_count": rule_count,
        "archive":    archive_name,
    }

    # Load + append manifest
    manifest: list[dict] = []
    if _VERSION_MANIFEST.exists():
        try:
            manifest = json.loads(_VERSION_MANIFEST.read_text(encoding="utf-8"))
        except Exception:
            manifest = []

    manifest.append(entry)
    _VERSION_MANIFEST.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return entry


def load_version_manifest() -> list[dict]:
    """Return the full version manifest (newest first). Safe to call from app.py."""
    if not _VERSION_MANIFEST.exists():
        return []
    try:
        manifest = json.loads(_VERSION_MANIFEST.read_text(encoding="utf-8"))
        # Filter out entries where the archive file no longer exists
        valid_manifest = []
        for entry in manifest:
            archive_name = entry.get("archive", "")
            if archive_name:
                archive_path = _VERSIONS_DIR / archive_name
                if archive_path.exists():
                    valid_manifest.append(entry)
        
        # If we filtered out any entries, update the manifest file
        if len(valid_manifest) < len(manifest):
            _VERSION_MANIFEST.write_text(
                json.dumps(valid_manifest, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        
        return sorted(valid_manifest, key=lambda e: e.get("version", 0), reverse=True)
    except Exception:
        return []


def load_rules_at_version(version: int) -> list[dict]:
    """Load the policy_rules.json snapshot for a specific version number."""
    manifest = load_version_manifest()
    for entry in manifest:
        if entry.get("version") == version:
            archive_path = _VERSIONS_DIR / entry["archive"]
            if archive_path.exists():
                try:
                    return json.loads(archive_path.read_text(encoding="utf-8"))
                except Exception:
                    return []
    return []


# ══════════════════════════════════════════════════════════════════════════════
# 1. DOCLING PDF PARSER TOOL
# ══════════════════════════════════════════════════════════════════════════════


class DoclingPDFParserInput(BaseModel):
    pdf_path: str = Field(..., description="Absolute or relative path to the PDF file to parse.")


class DoclingPDFParserTool(BaseTool):
    """
    Parse a regulatory PDF using Docling, preserving tables and layout.
    Returns a single structured text string suitable for rule extraction.
    """

    name: str = "docling_pdf_parser"
    description: str = (
        "Parse a regulatory or compliance PDF file using the Docling library. "
        "Preserves tables, headings, and layout structure. "
        "Input: the absolute path to a PDF file. "
        "Output: full structured text content of the document."
    )
    args_schema: Type[BaseModel] = DoclingPDFParserInput

    def _run(self, pdf_path: str) -> str:
        try:
            from docling.document_converter import DocumentConverter
        except ImportError:
            return "ERROR: docling is not installed. Run: pip install docling"

        path = Path(pdf_path)
        if not path.exists():
            return f"ERROR: File not found at path '{pdf_path}'"
        if path.suffix.lower() != ".pdf":
            return f"ERROR: Expected a .pdf file, got '{path.suffix}'"

        # ── Attempt 1: Full pipeline (layout-aware, table detection) ─────────
        try:
            converter = DocumentConverter()
            try:
                result = converter.convert(str(path))
                markdown_text = result.document.export_to_markdown()
                pipeline_used = "standard"
            finally:
                del converter
                import gc
                gc.collect()
        except Exception as e1:
            # ── Attempt 2: SimplePipeline (no ML layout model required) ──────
            try:
                from docling.pipeline.simple_pipeline import SimplePipeline
                from docling.datamodel.pipeline_options import PipelineOptions
                from docling.document_converter import DocumentConverter, PdfFormatOption
                from docling.datamodel.base_models import InputFormat

                opts = PipelineOptions(do_ocr=False, do_table_structure=False)
                converter2 = DocumentConverter(
                    format_options={
                        InputFormat.PDF: PdfFormatOption(pipeline_cls=SimplePipeline)
                    }
                )
                try:
                    result2 = converter2.convert(str(path))
                    markdown_text = result2.document.export_to_markdown()
                    pipeline_used = f"simple (full pipeline failed: {str(e1)[:120]})"
                finally:
                    del converter2
                    import gc
                    gc.collect()
            except Exception as e2:
                # ── Attempt 3: Raw pypdfium2 text extraction ──────────────────
                try:
                    import pypdfium2 as pdfium
                    import gc
                    pages = []
                    pdf_doc = pdfium.PdfDocument(str(path))
                    try:
                        for i, page in enumerate(pdf_doc):
                            textpage = page.get_textpage()
                            pages.append(f"## Page {i+1}\n\n{textpage.get_text_range()}")
                            textpage.close()
                            page.close()
                    finally:
                        pdf_doc.close()
                        del pdf_doc
                        gc.collect() # Force cleanup before pdfium destroys library context
                    markdown_text = "\n\n".join(pages)
                    pipeline_used = f"raw-text (docling failed: {str(e2)[:80]})"
                except Exception as e3:
                    return (
                        f"ERROR: All PDF parsing strategies failed.\n"
                        f"  Standard pipeline: {str(e1)[:200]}\n"
                        f"  Simple pipeline:   {str(e2)[:200]}\n"
                        f"  pypdfium2 raw:     {str(e3)[:200]}\n\n"
                        f"{traceback.format_exc()}"
                    )

        header = (
            f"# Document: {path.name}\n"
            f"# Pipeline: {pipeline_used}\n\n"
        )
        full_text = header + markdown_text

        # Safety cap for very large documents
        if len(full_text) > 300_000:
            full_text = full_text[:300_000] + (
                "\n\n[TRUNCATED: Document exceeded 300,000 characters. "
                "Consider splitting the PDF into sections.]"
            )

        return full_text


# ══════════════════════════════════════════════════════════════════════════════
# 2. RULE STORE WRITER TOOL  (✨ versioning added)
# ══════════════════════════════════════════════════════════════════════════════


class RuleStoreWriterInput(BaseModel):
    rules_json: str = Field(
        ...,
        description=(
            "A JSON string containing a list of extracted policy rules. "
            "Each rule must have: id, rule_type, description, "
            "condition_field, operator, threshold_value, sql_hint."
        ),
    )
    pdf_source: str = Field(
        default="unknown",
        description="Name/path of the source PDF these rules came from (used for version tracking).",
    )


class PolicyRule(BaseModel):
    """Pydantic schema for a single extracted rule."""
    id: str
    rule_type: str
    description: str
    condition_field: str
    operator: str
    threshold_value: Any
    sql_hint: str


class RuleStoreWriterTool(BaseTool):
    """
    Validate, deduplicate (via SHA-256 fingerprint), and persist
    extracted policy rules to the local JSON rule store.

    ✨ Versioning: Before overwriting policy_rules.json, the current
    version is archived to rules/versions/ and the manifest is updated.
    """

    name: str = "rule_store_writer"
    description: str = (
        "Save extracted policy rules to the local JSON rule store. "
        "Accepts a JSON string of rule objects. Deduplicates rules automatically. "
        "Archives the previous rule set for version history. "
        "Returns a summary of how many rules were saved or skipped."
    )
    args_schema: Type[BaseModel] = RuleStoreWriterInput

    def _fingerprint(self, rule: dict) -> str:
        """SHA-256 fingerprint based on condition_field + operator + threshold_value."""
        key = f"{rule.get('condition_field','')}.{rule.get('operator','')}.{rule.get('threshold_value','')}"
        return hashlib.sha256(key.encode()).hexdigest()[:16]

    def _run(self, rules_json: str, pdf_source: str = "unknown") -> str:
        # ── Parse input ───────────────────────────────────────────────────────
        try:
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", rules_json.strip(), flags=re.MULTILINE)
            incoming: list[dict] = json.loads(cleaned)
        except json.JSONDecodeError as e:
            return f"ERROR: Invalid JSON input — {e}"

        if not isinstance(incoming, list):
            incoming = [incoming]

        # ── Validate with Pydantic ────────────────────────────────────────────
        valid_rules = []
        validation_errors = []
        for i, raw in enumerate(incoming):
            try:
                rule = PolicyRule(**raw)
                valid_rules.append(rule.model_dump())
            except Exception as e:
                validation_errors.append(f"Rule #{i}: {e}")

        if not valid_rules:
            return f"ERROR: No valid rules found.\nValidation errors:\n" + "\n".join(validation_errors)

        # ── Load existing rules ───────────────────────────────────────────────
        RULES_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing: list[dict] = []
        if RULES_JSON_PATH.exists():
            try:
                existing = json.loads(RULES_JSON_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                existing = []

        # ── ✨ Snapshot BEFORE modifying ──────────────────────────────────────
        snapshot_entry = None
        # Create a version if:
        # 1. The file exists (there's something to archive)
        # 2. AND either:
        #    a. New rules are being added (fingerprints don't exist)
        #    b. OR the PDF source is different (new document)
        if RULES_JSON_PATH.exists():
            new_fingerprints = {self._fingerprint(r) for r in valid_rules}
            existing_fps_set = {e.get("_fingerprint") for e in existing}
            
            # Check if any new rules are being added
            rules_are_changing = bool(new_fingerprints - existing_fps_set)
            
            # Check if this is a completely different rule set (less than 50% overlap)
            if existing_fps_set:
                overlap = len(new_fingerprints & existing_fps_set)
                overlap_ratio = overlap / max(len(existing_fps_set), len(new_fingerprints))
                is_different_document = overlap_ratio < 0.5
            else:
                is_different_document = True
            
            if rules_are_changing or is_different_document:
                snapshot_entry = _snapshot_current_rules(pdf_source=pdf_source)

        existing_fps = {r.get("_fingerprint") for r in existing}

        # ── Deduplicate and merge ─────────────────────────────────────────────
        added = 0
        skipped = 0
        for rule in valid_rules:
            fp = self._fingerprint(rule)
            if fp in existing_fps:
                skipped += 1
            else:
                rule["_fingerprint"] = fp
                existing.append(rule)
                existing_fps.add(fp)
                added += 1

        # ── Persist ───────────────────────────────────────────────────────────
        RULES_JSON_PATH.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        summary = (
            f"Rule store updated: {added} new rule(s) added, "
            f"{skipped} duplicate(s) skipped. "
            f"Total rules in store: {len(existing)}. "
            f"Saved to: {RULES_JSON_PATH}"
        )
        if snapshot_entry:
            summary += (
                f"\n📦 Policy versioned: v{snapshot_entry['version']} archived "
                f"({snapshot_entry['rule_count']} previous rules → {snapshot_entry['archive']})"
            )
        if validation_errors:
            summary += f"\nValidation warnings: {'; '.join(validation_errors)}"
        return summary


# ══════════════════════════════════════════════════════════════════════════════
# 3. SECURE SQL VALIDATOR TOOL
# ══════════════════════════════════════════════════════════════════════════════


# Blocklist — any of these appearing in SQL (even in subqueries) are rejected
_DDL_DML_BLOCKLIST: tuple[str, ...] = (
    "DROP",
    "DELETE",
    "UPDATE",
    "INSERT",
    "CREATE",
    "ALTER",
    "TRUNCATE",
    "REPLACE",
    "MERGE",
    "EXEC",
    "EXECUTE",
    "CALL",
    "GRANT",
    "REVOKE",
    "COPY",
    "ATTACH",
    "DETACH",
    "LOAD",
    "IMPORT",
    "EXPORT",
)

# Allowlist — the only statement type permitted
_SELECT_PATTERN = re.compile(r"^\s*SELECT\b", re.IGNORECASE)

# Comment stripping patterns
_INLINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT  = re.compile(r"/\*.*?\*/", re.DOTALL)


class SecureSQLValidatorInput(BaseModel):
    sql: str = Field(..., description="The SQL query string to validate.")


class SecureSQLValidatorTool(BaseTool):
    """
    Multi-layer security guardrail.

    Layer 1: Strip SQL comments (prevent disguising DDL inside comments)
    Layer 2: Enforce SELECT-only (allowlist approach)
    Layer 3: Token blocklist scan for DDL/DML keywords (even in subqueries)
    Layer 4: Semicolon injection check (prevent multi-statement attacks)

    Returns JSON: {"valid": true} or {"valid": false, "reason": "..."}
    """

    name: str = "secure_sql_validator"
    description: str = (
        "Validate that a SQL query is strictly read-only (SELECT only). "
        "Rejects any DDL or DML operations. "
        "Input: SQL query string. "
        "Output: JSON with 'valid' boolean and optional 'reason' for rejection."
    )
    args_schema: Type[BaseModel] = SecureSQLValidatorInput

    def _run(self, sql: str) -> str:
        if not sql or not sql.strip():
            return json.dumps({"valid": False, "reason": "Empty SQL string provided."})

        # ── Layer 1: Strip comments ───────────────────────────────────────────
        cleaned = _BLOCK_COMMENT.sub(" ", sql)
        cleaned = _INLINE_COMMENT.sub(" ", cleaned)
        cleaned = cleaned.strip()

        # ── Layer 2: Allowlist — must start with SELECT ───────────────────────
        if not _SELECT_PATTERN.match(cleaned):
            first_word = cleaned.split()[0].upper() if cleaned.split() else ""
            return json.dumps({
                "valid": False,
                "reason": (
                    f"Statement begins with '{first_word}', not SELECT. "
                    "Only read-only SELECT statements are permitted."
                ),
            })

        # ── Layer 3: Blocklist token scan ─────────────────────────────────────
        sql_upper = cleaned.upper()
        for blocked in _DDL_DML_BLOCKLIST:
            pattern = re.compile(r"\b" + re.escape(blocked) + r"\b")
            if pattern.search(sql_upper):
                return json.dumps({
                    "valid": False,
                    "reason": (
                        f"Blocked keyword '{blocked}' detected. "
                        "Turgon enforces strictly read-only queries."
                    ),
                })

        # ── Layer 4: Semicolon injection check ────────────────────────────────
        statements = [s.strip() for s in cleaned.split(";") if s.strip()]
        if len(statements) > 1:
            return json.dumps({
                "valid": False,
                "reason": (
                    f"Multiple statements detected ({len(statements)} statements separated by ';'). "
                    "Only a single SELECT statement is permitted per query."
                ),
            })

        return json.dumps({"valid": True})


# ══════════════════════════════════════════════════════════════════════════════
# 4. DUCKDB EXECUTION SANDBOX TOOL
# ══════════════════════════════════════════════════════════════════════════════


class DuckDBExecutionSandboxInput(BaseModel):
    sql: str = Field(..., description="The SELECT SQL query to execute against the AML database.")
    rule_id: str = Field(
        default="unknown",
        description="The rule ID associated with this query (for audit logging).",
    )


class DuckDBExecutionSandboxTool(BaseTool):
    """
    Execute a validated read-only SQL query against the AML DuckDB sandbox.

    Safety guarantees:
    - Runs SecureSQLValidatorTool FIRST — rejects any non-SELECT statement
    - Opens DuckDB in READ-ONLY mode — writes are physically impossible
    - Caps result rows at MAX_VIOLATION_ROWS to prevent memory DoS
    - All errors are caught and returned as JSON (never propagated)
    """

    name: str = "duckdb_execution_sandbox"
    description: str = (
        "Execute a validated read-only SELECT SQL query against the AML DuckDB sandbox. "
        "Always validates the SQL for safety before execution. "
        "Input: SQL query string and optional rule_id. "
        "Output: JSON with execution results or error details."
    )
    args_schema: Type[BaseModel] = DuckDBExecutionSandboxInput

    def _run(self, sql: str, rule_id: str = "unknown") -> str:
        # ── Step 1: Security validation FIRST ────────────────────────────────
        validator = SecureSQLValidatorTool()
        validation_result = json.loads(validator._run(sql))

        if not validation_result.get("valid"):
            return json.dumps({
                "rule_id": rule_id,
                "status": "BLOCKED",
                "reason": validation_result.get("reason"),
                "violations": [],
                "row_count": 0,
            })

        # ── Step 2: Check database exists ─────────────────────────────────────
        if not DUCKDB_PATH.exists():
            return json.dumps({
                "rule_id": rule_id,
                "status": "ERROR",
                "reason": (
                    f"DuckDB database not found at '{DUCKDB_PATH}'. "
                    "Run 'python data/setup_duckdb.py' first to load the AML dataset."
                ),
                "violations": [],
                "row_count": 0,
            })

        # ── Step 3: Execute in read-only sandbox ──────────────────────────────
        conn = None
        try:
            conn = duckdb.connect(database=str(DUCKDB_PATH), read_only=True)

            sql_capped = sql.rstrip().rstrip(";")
            if not re.search(r"\bLIMIT\b", sql_capped, re.IGNORECASE):
                sql_capped = f"{sql_capped} LIMIT {MAX_VIOLATION_ROWS}"

            relation = conn.execute(sql_capped)
            columns  = [desc[0] for desc in relation.description]
            rows     = relation.fetchall()

            violations = [dict(zip(columns, row)) for row in rows]

            def _serialize(obj):
                try:
                    json.dumps(obj)
                    return obj
                except (TypeError, ValueError):
                    return str(obj)

            violations_serialized = [
                {k: _serialize(v) for k, v in row.items()}
                for row in violations
            ]

            return json.dumps({
                "rule_id":      rule_id,
                "status":       "SUCCESS",
                "sql_executed": sql_capped,
                "row_count":    len(violations_serialized),
                "capped_at":    MAX_VIOLATION_ROWS,
                "violations":   violations_serialized,
            }, default=str)

        except duckdb.Error as e:
            return json.dumps({
                "rule_id":    rule_id,
                "status":     "SQL_ERROR",
                "reason":     str(e),
                "violations": [],
                "row_count":  0,
            })
        except Exception as e:
            return json.dumps({
                "rule_id":    rule_id,
                "status":     "ERROR",
                "reason":     f"Unexpected error: {str(e)}",
                "violations": [],
                "row_count":  0,
            })
        finally:
            if conn:
                conn.close()