"""
data/setup_duckdb.py — One-time setup script to load IBM AML CSV data into DuckDB.

Run this BEFORE the main Turgon pipeline.

Usage:
  cd "d:\Projects\Hackfest 2.0\turgon"
  python data/setup_duckdb.py

Dataset:
  IBM Transactions for Anti-Money Laundering (AML)
  https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml

  After downloading, place the CSV file(s) in the data/ directory.
  Supported filenames (tried in order, first match wins):
    HI-Small_Trans.csv, HI-Medium_Trans.csv, HI-Large_Trans.csv,
    LI-Small_Trans.csv, LI-Medium_Trans.csv, LI-Large_Trans.csv

ADAPTABILITY NOTE:
  This script is schema-agnostic. It detects the actual column names that land
  in DuckDB after loading (regardless of CSV header variations), and builds all
  views and indexes against those real column names. It never assumes a fixed schema.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure turgon root is on the path when running from data/ subdirectory
ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(ROOT))

import duckdb
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from config import AML_CSV_CANDIDATES, DATA_DIR, DUCKDB_PATH

console = Console()

# ── Semantic intent map ────────────────────────────────────────────────────────
# Maps human-readable "roles" to lists of possible column name variants.
# The first column name that exists in the actual table wins.
# This supports any CSV schema variation without hardcoding exact names.
SEMANTIC_COLUMNS = {
    # role            : [candidate names in priority order]
    "from_account"    : ["From_Account", "Account", "from_account", "sender_account", "source_account"],
    "to_account"      : ["To_Account", "Account_1", "Account.1", "to_account", "receiver_account", "dest_account"],
    "from_bank"       : ["From_Bank", "from_bank", "sender_bank", "source_bank"],
    "to_bank"         : ["To_Bank", "to_bank", "receiver_bank", "dest_bank"],
    "amount_paid"     : ["Amount_Paid", "Amount Paid", "amount_paid", "debit_amount", "sent_amount"],
    "amount_received" : ["Amount_Received", "Amount Received", "amount_received", "credit_amount"],
    "pay_currency"    : ["Payment_Currency", "Payment Currency", "payment_currency", "sent_currency"],
    "recv_currency"   : ["Receiving_Currency", "Receiving Currency", "receiving_currency", "recv_currency"],
    "pay_format"      : ["Payment_Format", "Payment Format", "payment_format", "payment_method"],
    "is_laundering"   : ["Is_Laundering", "Is Laundering", "is_laundering", "laundering_flag", "label"],
    "timestamp"       : ["Timestamp", "timestamp", "date", "transaction_date", "txn_date"],
}

# Preferred canonical rename targets (applied during load if the raw name differs)
CANONICAL_NAMES = {
    "from_account"    : "From_Account",
    "to_account"      : "To_Account",
    "from_bank"       : "From_Bank",
    "to_bank"         : "To_Bank",
    "amount_paid"     : "Amount_Paid",
    "amount_received" : "Amount_Received",
    "pay_currency"    : "Payment_Currency",
    "recv_currency"   : "Receiving_Currency",
    "pay_format"      : "Payment_Format",
    "is_laundering"   : "Is_Laundering",
    "timestamp"       : "Timestamp",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def find_csv(data_dir: Path) -> Path | None:
    """Return the first matching IBM AML CSV found in data_dir."""
    for name in AML_CSV_CANDIDATES:
        candidate = data_dir / name
        if candidate.exists():
            return candidate
    return None


def get_actual_columns(conn: duckdb.DuckDBPyConnection, table: str = "transactions") -> list[str]:
    """Return the real column names that exist in the table right now."""
    rows = conn.execute(
        f"SELECT column_name FROM information_schema.columns "
        f"WHERE table_name = '{table}' ORDER BY ordinal_position"
    ).fetchall()
    return [r[0] for r in rows]


def resolve_column(actual_cols: list[str], role: str) -> str | None:
    """
    Find which actual column satisfies a semantic role.
    Returns the actual column name, or None if no candidate matches.
    Case-insensitive matching.
    """
    lower_actual = {c.lower(): c for c in actual_cols}
    for candidate in SEMANTIC_COLUMNS.get(role, []):
        if candidate.lower() in lower_actual:
            return lower_actual[candidate.lower()]
    return None


def build_rename_clause(conn: duckdb.DuckDBPyConnection, csv_path: Path) -> tuple[str, dict[str, str]]:
    """
    Inspect raw CSV headers via a 0-row read, then build a SELECT clause that:
    1. Renames semantically known columns to their canonical names
    2. Cleans all other column names (spaces/dots → underscores)
    3. Deduplicates any collisions with a _N suffix

    Returns (select_clause_str, {raw_col: final_col} mapping)
    """
    result = conn.execute(
        f"SELECT * FROM read_csv_auto('{csv_path.as_posix()}', sample_size=1) LIMIT 0"
    )
    raw_cols: list[str] = [desc[0] for desc in result.description]

    # Build a lookup: raw → canonical (for semantically known columns)
    raw_to_canonical: dict[str, str] = {}
    for role, canonical in CANONICAL_NAMES.items():
        for candidate in SEMANTIC_COLUMNS[role]:
            # Match against raw headers case-insensitively
            for raw in raw_cols:
                if raw.lower() == candidate.lower() and raw not in raw_to_canonical:
                    raw_to_canonical[raw] = canonical
                    break

    # Build SELECT clause, deduplicating final names
    select_parts = []
    final_names: dict[str, str] = {}  # raw → final
    used: set[str] = set()

    for raw in raw_cols:
        if raw in raw_to_canonical:
            target = raw_to_canonical[raw]
        else:
            # Generic sanitise: spaces + dots → underscores
            target = raw.replace(" ", "_").replace(".", "_")

        # Deduplicate
        original_target = target
        suffix = 2
        while target in used:
            target = f"{original_target}_{suffix}"
            suffix += 1

        used.add(target)
        final_names[raw] = target

        if raw == target:
            select_parts.append(f'"{raw}"')
        else:
            select_parts.append(f'"{raw}" AS "{target}"')

    return ", ".join(select_parts), final_names


def create_indexes_adaptive(conn: duckdb.DuckDBPyConnection, actual_cols: list[str]) -> list[str]:
    """
    Create indexes on whatever semantically important columns actually exist.
    Returns list of columns that got indexed.
    """
    index_roles = [
        "amount_paid", "amount_received", "pay_format",
        "is_laundering", "from_bank", "to_bank",
    ]
    indexed = []
    for role in index_roles:
        col = resolve_column(actual_cols, role)
        if col:
            idx_name = f"idx_{col.lower()}"
            try:
                conn.execute(f'CREATE INDEX IF NOT EXISTS "{idx_name}" ON transactions("{col}")')
                indexed.append(col)
            except duckdb.Error:
                pass  # index may already exist or column type not indexable
    return indexed


def create_views_adaptive(conn: duckdb.DuckDBPyConnection, actual_cols: list[str]) -> list[str]:
    """
    Build compliance views using only columns that actually exist.
    Skips any view component that references a missing column.
    Returns list of views successfully created.
    """
    created = []

    # ── Resolve semantic columns ───────────────────────────────────────────────
    c = {role: resolve_column(actual_cols, role) for role in SEMANTIC_COLUMNS}

    # ── account_summary ───────────────────────────────────────────────────────
    if c["from_account"] and c["from_bank"] and c["amount_paid"]:
        parts = [
            f'"{c["from_account"]}"                 AS account_id',
            f'"{c["from_bank"]}"                    AS bank',
            "COUNT(*)                               AS total_transactions",
            f'SUM("{c["amount_paid"]}")              AS total_amount_paid',
            f'AVG("{c["amount_paid"]}")              AS avg_amount_paid',
            f'MAX("{c["amount_paid"]}")              AS max_single_payment',
        ]
        if c["to_account"]:
            parts.append(f'COUNT(DISTINCT "{c["to_account"]}") AS distinct_counterparties')
        if c["is_laundering"]:
            parts.append(
                f'SUM(CASE WHEN "{c["is_laundering"]}"=1 THEN 1 ELSE 0 END) AS confirmed_laundering_txns'
            )

        group_cols = f'"{c["from_account"]}", "{c["from_bank"]}"'
        sql = (
            "CREATE VIEW account_summary AS\n"
            f"SELECT {', '.join(parts)}\n"
            f"FROM transactions\n"
            f"GROUP BY {group_cols}"
        )
        conn.execute("DROP VIEW IF EXISTS account_summary")
        conn.execute(sql)
        created.append("account_summary")

    # ── high_value_transactions ────────────────────────────────────────────────
    if c["amount_paid"]:
        conn.execute("DROP VIEW IF EXISTS high_value_transactions")
        conn.execute(
            f'CREATE VIEW high_value_transactions AS '
            f'SELECT * FROM transactions WHERE "{c["amount_paid"]}" >= 10000'
        )
        created.append("high_value_transactions")

    # ── currency_mismatch ─────────────────────────────────────────────────────
    if c["pay_currency"] and c["recv_currency"]:
        conn.execute("DROP VIEW IF EXISTS currency_mismatch")
        conn.execute(
            f'CREATE VIEW currency_mismatch AS '
            f'SELECT * FROM transactions '
            f'WHERE "{c["pay_currency"]}" != "{c["recv_currency"]}"'
        )
        created.append("currency_mismatch")

    # ── laundering_confirmed ───────────────────────────────────────────────────
    if c["is_laundering"]:
        conn.execute("DROP VIEW IF EXISTS laundering_confirmed")
        conn.execute(
            f'CREATE VIEW laundering_confirmed AS '
            f'SELECT * FROM transactions WHERE "{c["is_laundering"]}" = 1'
        )
        created.append("laundering_confirmed")

    return created


# ── Main setup orchestrator ────────────────────────────────────────────────────

def setup_database(csv_path: Path) -> None:
    console.print(Panel(
        f"[bold cyan]Turgon — DuckDB Setup[/]\n"
        f"CSV: [yellow]{csv_path.name}[/]\n"
        f"Target DB: [yellow]{DUCKDB_PATH}[/]",
        border_style="cyan",
    ))

    conn = duckdb.connect(database=str(DUCKDB_PATH))

    # ── Step 1: Inspect raw schema and build rename clause (before Progress) ────
    rename_clause, col_map = build_rename_clause(conn, csv_path)
    console.print(f"\n[dim]Column mapping ({len(col_map)} columns):[/]")
    for raw, final in col_map.items():
        marker = "->" if raw != final else "  "
        console.print(f"  {marker} {raw!r:30s}  {final}")
    console.print()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:

        # ── Step 1 already done above; jump straight to load ──────────────────
        t1 = progress.add_task(f"Schema inspected ({len(col_map)} columns).", total=None)
        progress.update(t1, completed=1, total=1)

        # ── Step 2: Load data ──────────────────────────────────────────────────
        t2 = progress.add_task("Loading transactions into DuckDB...", total=None)
        conn.execute("DROP TABLE IF EXISTS transactions")
        conn.execute(f"""
            CREATE TABLE transactions AS
            SELECT {rename_clause}
            FROM read_csv_auto(
                '{csv_path.as_posix()}',
                header=true,
                sample_size=100000,
                ignore_errors=true
            )
        """)
        progress.update(t2, description="[green]Transactions loaded.")

        # ── Step 3: Introspect actual table columns AFTER load ─────────────────
        actual_cols = get_actual_columns(conn)

        # ── Step 4: Create indexes on discovered columns ───────────────────────
        t3 = progress.add_task("Creating performance indexes...", total=None)
        indexed = create_indexes_adaptive(conn, actual_cols)
        progress.update(t3, description=f"[green]Indexes created on: {', '.join(indexed) or 'none'}.")

        # ── Step 5: Create compliance views from discovered columns ────────────
        t4 = progress.add_task("Creating compliance views...", total=None)
        views_created = create_views_adaptive(conn, actual_cols)
        progress.update(t4, description=f"[green]Views created: {', '.join(views_created) or 'none'}.")

    # ── Summary ────────────────────────────────────────────────────────────────
    row_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    # Laundering count — only if the column exists
    is_laund_col = resolve_column(actual_cols, "is_laundering")
    if is_laund_col:
        laund_count = conn.execute(
            f'SELECT COUNT(*) FROM transactions WHERE "{is_laund_col}" = 1'
        ).fetchone()[0]
        laund_str = f"Laundering flagged: [red]{laund_count:,}[/] ({100*laund_count/max(row_count,1):.2f}%)\n"
    else:
        laund_str = "[dim]Is_Laundering column not detected in this dataset.[/]\n"

    schema_table = Table(title="transactions — final schema", header_style="bold magenta")
    schema_table.add_column("Column", style="cyan")
    schema_table.add_column("Type", style="yellow")
    for col, dtype in conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name='transactions' ORDER BY ordinal_position"
    ).fetchall():
        schema_table.add_row(col, dtype)

    console.print(schema_table)
    console.print()
    console.print(Panel(
        f"[bold green]Setup complete![/]\n"
        f"Total rows:    [yellow]{row_count:,}[/]\n"
        f"{laund_str}"
        f"Database:      [cyan]{DUCKDB_PATH}[/]\n"
        f"Views:         [dim]{', '.join(views_created) or 'none'}[/]",
        title="DuckDB Ready",
        border_style="green",
    ))

    conn.close()


def main() -> None:
    csv_path = find_csv(DATA_DIR)

    if csv_path is None:
        console.print(Panel(
            "[red]No IBM AML CSV file found in the data/ directory.[/]\n\n"
            "Please download the dataset from:\n"
            "[cyan]https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml[/]\n\n"
            "Then place one of the following files in:\n"
            f"[yellow]{DATA_DIR}[/]\n\n"
            "Supported filenames:\n" +
            "\n".join(f"  • {name}" for name in AML_CSV_CANDIDATES),
            title="Setup Required",
            border_style="red",
        ))
        sys.exit(1)

    setup_database(csv_path)


if __name__ == "__main__":
    main()
