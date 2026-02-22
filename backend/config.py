"""
config.py — Central configuration for Turgon.

All constants and environment loading live here.
Other modules import from this file; never import dotenv elsewhere.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# ── Load .env ─────────────────────────────────────────────────────────────────
load_dotenv()

# ── Base Paths ─────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.resolve()
DATA_DIR = BASE_DIR / "data"
RULES_DIR = BASE_DIR / "rules"
UPLOADS_DIR = BASE_DIR / "uploads"

# Create directories if they don't exist
for _dir in [DATA_DIR, RULES_DIR, UPLOADS_DIR]:
    _dir.mkdir(exist_ok=True)

# ── Groq / LLM ────────────────────────────────────────────────────────────────
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")

# Model to use via Groq.
# Examples: llama-3.1-8b-instant, mixtral-8x7b-32768, llama-3.1-70b-versatile
DEFAULT_MODEL: str = os.getenv("TURGON_MODEL", "llama-3.1-8b-instant")

# CrewAI agent settings
AGENT_MAX_ITER: int = 15
AGENT_VERBOSE: bool = True

# ── DuckDB ─────────────────────────────────────────────────────────────────────
DUCKDB_PATH: Path = DATA_DIR / "aml.db"

# IBM AML dataset — place CSV files in the data/ directory.
# Download from: https://www.kaggle.com/datasets/ealtman2019/ibm-transactions-for-anti-money-laundering-aml
# Supported file names (script will try each in order):
AML_CSV_CANDIDATES: list[str] = [
    "HI-Small_Trans.csv",
    "HI-Medium_Trans.csv",
    "HI-Large_Trans.csv",
    "LI-Small_Trans.csv",
    "LI-Medium_Trans.csv",
    "LI-Large_Trans.csv",
]

# ── Rules Store ────────────────────────────────────────────────────────────────
RULES_JSON_PATH: Path = RULES_DIR / "policy_rules.json"

# ── Live Monitoring ────────────────────────────────────────────────────────────
LIVE_TABLE_NAME: str = "transactions_live"
LIVE_REPORT_PATH: Path = RULES_DIR / "violation_report_live.json"
WATCHDOG_INTERVAL: int = 20
INGESTER_BATCH_SIZE: int = 50
INGESTER_INTERVAL: int = 15

# ── Execution Sandbox ──────────────────────────────────────────────────────────
# Maximum rows returned from a single SQL violation query
MAX_VIOLATION_ROWS: int = 500

# Maximum rules to process in a single CrewAI run (0 = unlimited)
MAX_RULES_PER_RUN: int = 0

# Maximum rules the QueryEngineerAgent processes per batch
SQL_BATCH_SIZE: int = 10

# ── Validation ─────────────────────────────────────────────────────────────────
if not GROQ_API_KEY:
    import warnings
    warnings.warn(
        "GROQ_API_KEY is not set. "
        "Set it in your .env file before running the pipeline.",
        stacklevel=2,
    )


def get_llm():
    """
    Return a CrewAI LLM instance that routes through Groq.
    
    Uses Groq's fast inference API with Llama models.
    """
    from langchain_groq import ChatGroq

    return ChatGroq(
    temperature=0,
    model_name="groq/llama-3.3-70b-versatile",
)

