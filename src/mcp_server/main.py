import calendar
import json
import logging
import math
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

# Log to stderr: stdout is reserved for the protocol when using stdio transport
# (e.g. `fastmcp install claude-desktop`).
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("expense-tracker")

BASE_DIR = Path(__file__).resolve().parent

# In production, set DATABASE_DIR to a writable, persistent volume
# (e.g. DATABASE_DIR=/app/data with a volume mounted there).
# Default: ./data relative to the working directory.
DB_DIR = Path(os.getenv("DATABASE_DIR", str(Path.cwd() / "data")))

try:
    DB_DIR.mkdir(parents=True, exist_ok=True)
except OSError as exc:
    if os.getenv("DATABASE_DIR"):
        # The configured path is wrong; fail loudly instead of hiding it.
        raise RuntimeError(
            f"DATABASE_DIR={DB_DIR} is not writable ({exc}). "
            "Point it at a writable, persistent volume."
        ) from exc
    # No explicit config: fall back to a temp dir so the server still starts,
    # but warn because data here is NOT persistent across restarts.
    import tempfile

    DB_DIR = Path(tempfile.gettempdir()) / "expense-tracker"
    DB_DIR.mkdir(parents=True, exist_ok=True)
    logging.getLogger("expense-tracker").warning(
        "Default data dir not writable (%s). Using %s, which is NOT persistent. "
        "Set DATABASE_DIR to a persistent volume.",
        exc,
        DB_DIR,
    )

DB_PATH = DB_DIR / "expenses.db"
CATEGORIES_PATH = BASE_DIR / "categories.json"

MAX_LIST_LIMIT = 500

mcp = FastMCP("ExpenseTracker")

log.info("Expense database: %s", DB_PATH)


# --------------------------------------------------
# DATABASE
# --------------------------------------------------

@contextmanager
def db():
    """Open a connection, commit on success, roll back on error, always close."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    # WAL mode is persistent in the database file, so set it once here.
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                amount REAL NOT NULL CHECK(amount > 0),
                category TEXT NOT NULL,
                subcategory TEXT DEFAULT '',
                note TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(date)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses(category)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_expenses_date_category "
            "ON expenses(date, category)"
        )
        conn.commit()
    finally:
        conn.close()


init_db()


# --------------------------------------------------
# VALIDATION
# --------------------------------------------------

def validate_date(date_string: str) -> str:
    """Validate a date and return it normalized to YYYY-MM-DD.

    Normalizing matters: strptime accepts '2026-1-5', but SQLite compares
    dates as strings, so a non-padded date would break BETWEEN queries.
    """
    try:
        return datetime.strptime(date_string, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        raise ValueError("Invalid date. Use YYYY-MM-DD format.")


def validate_amount(amount) -> float:
    try:
        amount = float(amount)
    except (TypeError, ValueError) as exc:
        raise ValueError("Please provide a valid positive amount.") from exc

    # nan and inf slip past `amount <= 0`, so check them explicitly.
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("Amount must be a finite number greater than zero.")

    return round(amount, 2)


def validate_date_range(start_date: str, end_date: str) -> tuple[str, str]:
    start = validate_date(start_date)
    end = validate_date(end_date)
    if start > end:
        raise ValueError("Start date cannot be after end date.")
    return start, end


def validate_category(category: str) -> str:
    if not category or not category.strip():
        raise ValueError("Category cannot be empty.")
    return category.strip()


# --------------------------------------------------
# ADD EXPENSE - WRITE
# --------------------------------------------------

@mcp.tool()
def add_expense(
    date: str,
    amount: float,
    category: str,
    subcategory: str = "",
    note: str = "",
) -> dict:
    """
    Add a new expense. Date must be YYYY-MM-DD and amount must be positive.
    Check the expenses://categories resource for suggested categories.
    """
    date = validate_date(date)
    amount = validate_amount(amount)
    category = validate_category(category)

    with db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO expenses (date, amount, category, subcategory, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (date, amount, category, subcategory.strip(), note.strip()),
        )
        return {
            "status": "success",
            "message": "Expense added successfully.",
            "expense_id": cursor.lastrowid,
        }


# --------------------------------------------------
# LIST EXPENSES - READ
# --------------------------------------------------

@mcp.tool()
def list_expenses(
    start_date: str,
    end_date: str,
    category: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """
    Retrieve expenses within a date range (inclusive), oldest first.
    Optionally filter by category. Results are paginated with limit/offset
    (limit is capped at 500); check `has_more` to know if more rows exist.
    """
    start_date, end_date = validate_date_range(start_date, end_date)

    if limit < 1:
        raise ValueError("limit must be at least 1.")
    if offset < 0:
        raise ValueError("offset cannot be negative.")
    limit = min(limit, MAX_LIST_LIMIT)

    where = "date BETWEEN ? AND ?"
    params: list = [start_date, end_date]
    if category and category.strip():
        where += " AND category = ?"
        params.append(category.strip())

    with db() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM expenses WHERE {where}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"""
            SELECT id, date, amount, category, subcategory, note, created_at
            FROM expenses
            WHERE {where}
            ORDER BY date ASC, id ASC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()

        expenses = [dict(row) for row in rows]
        return {
            "total_matching": total,
            "returned": len(expenses),
            "offset": offset,
            "has_more": offset + len(expenses) < total,
            "expenses": expenses,
        }


# --------------------------------------------------
# SUMMARIZE EXPENSES - READ
# --------------------------------------------------

@mcp.tool()
def summarize(
    start_date: str,
    end_date: str,
    category: Optional[str] = None,
) -> dict:
    """
    Calculate total expenses grouped by category for a date range.
    Optionally filter by a specific category.
    """
    start_date, end_date = validate_date_range(start_date, end_date)
    return _summarize(start_date, end_date, category)


def _summarize(start_date: str, end_date: str, category: Optional[str]) -> dict:
    query = """
        SELECT
            category,
            SUM(amount) AS total_amount,
            COUNT(*) AS transaction_count
        FROM expenses
        WHERE date BETWEEN ? AND ?
    """
    params: list = [start_date, end_date]

    if category and category.strip():
        query += " AND category = ?"
        params.append(category.strip())

    query += " GROUP BY category ORDER BY total_amount DESC"

    with db() as conn:
        rows = conn.execute(query, params).fetchall()

    results = []
    for row in rows:
        item = dict(row)
        item["total_amount"] = round(item["total_amount"], 2)
        results.append(item)

    return {
        "start_date": start_date,
        "end_date": end_date,
        "grand_total": round(sum(r["total_amount"] for r in results), 2),
        "categories": results,
    }


# --------------------------------------------------
# MONTHLY SUMMARY - READ
# --------------------------------------------------

@mcp.tool()
def monthly_summary(year: int, month: int) -> dict:
    """
    Summarize a whole calendar month by category. Month is 1-12.
    """
    if not 1 <= month <= 12:
        raise ValueError("Month must be between 1 and 12.")
    if not 1900 <= year <= 9999:
        raise ValueError("Year must be between 1900 and 9999.")

    last_day = calendar.monthrange(year, month)[1]
    start = f"{year:04d}-{month:02d}-01"
    end = f"{year:04d}-{month:02d}-{last_day:02d}"
    return _summarize(start, end, None)


# --------------------------------------------------
# UPDATE EXPENSE - WRITE
# --------------------------------------------------

@mcp.tool()
def update_expense(
    expense_id: int,
    date: Optional[str] = None,
    amount: Optional[float] = None,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
    note: Optional[str] = None,
) -> dict:
    """
    Update an existing expense. Only the fields you supply are modified.
    """
    fields: dict = {}

    if date is not None:
        fields["date"] = validate_date(date)
    if amount is not None:
        fields["amount"] = validate_amount(amount)
    if category is not None:
        fields["category"] = validate_category(category)
    if subcategory is not None:
        fields["subcategory"] = subcategory.strip()
    if note is not None:
        fields["note"] = note.strip()

    if not fields:
        raise ValueError("No fields provided for update.")

    # Safe from SQL injection: the column names come from the fixed keys above,
    # never from user input. Values are always bound as parameters.
    set_clause = ", ".join(f"{key} = ?" for key in fields)
    params = list(fields.values()) + [expense_id]

    with db() as conn:
        cursor = conn.execute(
            f"UPDATE expenses SET {set_clause} WHERE id = ?", params
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Expense {expense_id} not found.")

    return {
        "status": "success",
        "message": "Expense updated successfully.",
        "expense_id": expense_id,
    }


# --------------------------------------------------
# DELETE EXPENSE - WRITE
# --------------------------------------------------

@mcp.tool()
def delete_expense(expense_id: int) -> dict:
    """
    Delete an expense by its ID. This cannot be undone.
    """
    with db() as conn:
        cursor = conn.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
        if cursor.rowcount == 0:
            raise ValueError(f"Expense {expense_id} not found.")

    return {
        "status": "success",
        "message": "Expense deleted successfully.",
        "expense_id": expense_id,
    }


# --------------------------------------------------
# GET SINGLE EXPENSE - READ
# --------------------------------------------------

@mcp.tool()
def get_expense(expense_id: int) -> dict:
    """
    Retrieve a single expense by ID.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM expenses WHERE id = ?", (expense_id,)
        ).fetchone()

    if row is None:
        raise ValueError(f"Expense {expense_id} not found.")

    return dict(row)


# --------------------------------------------------
# CATEGORY RESOURCE
# --------------------------------------------------

@mcp.resource("expenses://categories", mime_type="application/json")
def categories() -> str:
    """Suggested expense categories (advisory; not enforced)."""
    if not CATEGORIES_PATH.exists():
        return json.dumps({"categories": []})

    return CATEGORIES_PATH.read_text(encoding="utf-8")


# --------------------------------------------------
# DATABASE HEALTH CHECK
# --------------------------------------------------

@mcp.tool()
def database_status() -> dict:
    """
    Check database connectivity and record count.
    """
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]

    return {
        "status": "connected",
        "database": str(DB_PATH),
        "read_write": os.access(DB_DIR, os.R_OK | os.W_OK),
        "total_expenses": count,
    }


# --------------------------------------------------
# SERVER STARTUP
# --------------------------------------------------

if __name__ == "__main__":
    # Binds to localhost by default. This server has NO authentication, so only
    # set HOST=0.0.0.0 behind a reverse proxy / platform that adds auth, or
    # after adding a FastMCP auth provider.
    mcp.run(
        transport="http",
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "8000")),
    )

# Run locally:
#   uv run fastmcp run ./src/mcp_server/main.py:mcp --transport http --host 127.0.0.1 --port 8000
# Install into Claude Desktop:
#   uv run fastmcp install claude-desktop ./src/mcp_server/main.py