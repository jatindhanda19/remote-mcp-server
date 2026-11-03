
import os
import sqlite3
from pathlib import Path
from datetime import datetime
from fastmcp import FastMCP

# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

# Use persistent directory configured during deployment.
# Local default: project_root/data
PROJECT_ROOT = BASE_DIR.parent.parent.parent

DB_DIR = Path(
    os.getenv("DATABASE_DIR", str(PROJECT_ROOT / "data"))
)

DB_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DB_DIR / "expenses.db"

CATEGORIES_PATH = BASE_DIR / "categories.json"

mcp = FastMCP("ExpenseTracker")

print(f"Expense database: {DB_PATH}")


# --------------------------------------------------
# DATABASE CONNECTION
# --------------------------------------------------

def get_connection():

    conn = sqlite3.connect(
        str(DB_PATH),
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")

    return conn


# --------------------------------------------------
# DATABASE INITIALIZATION
# --------------------------------------------------

def init_db():

    with get_connection() as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS expenses (

                id INTEGER PRIMARY KEY AUTOINCREMENT,

                date TEXT NOT NULL,

                amount REAL NOT NULL
                    CHECK(amount > 0),

                category TEXT NOT NULL,

                subcategory TEXT DEFAULT '',

                note TEXT DEFAULT '',

                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_expenses_date
            ON expenses(date)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_expenses_category
            ON expenses(category)
        """)


init_db()


# --------------------------------------------------
# INPUT VALIDATION
# --------------------------------------------------

def validate_date(date_string):

    try:
        datetime.strptime(date_string, "%Y-%m-%d")
        return date_string

    except ValueError:
        raise ValueError(
            "Invalid date. Use YYYY-MM-DD format."
        )


def validate_amount(amount):

    try:
        amount = float(amount)

        if amount <= 0:
            raise ValueError("Amount must be greater than zero.")

        return amount

    except (TypeError, ValueError) as exc:
        raise ValueError("Please provide a valid positive amount.") from exc


def validate_date_range(start_date, end_date):

    validate_date(start_date)
    validate_date(end_date)

    if start_date > end_date:
        raise ValueError(
            "Start date cannot be greater than end date."
        )


# --------------------------------------------------
# ADD EXPENSE - WRITE
# --------------------------------------------------

@mcp.tool()
def add_expense(
    date: str,
    amount: float,
    category: str,
    subcategory: str = "",
    note: str = ""
):
    """
    Add a new expense to the SQLite database.
    """

    validate_date(date)

    amount = validate_amount(amount)

    if not category or not category.strip():
        raise ValueError("Category cannot be empty.")

    with get_connection() as conn:

        cursor = conn.execute(
            """
            INSERT INTO expenses
            (
                date,
                amount,
                category,
                subcategory,
                note
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                date,
                amount,
                category.strip(),
                subcategory.strip(),
                note.strip()
            )
        )

        return {
            "status": "success",
            "message": "Expense added successfully.",
            "expense_id": cursor.lastrowid
        }


# --------------------------------------------------
# LIST EXPENSES - READ
# --------------------------------------------------

@mcp.tool()
def list_expenses(
    start_date: str,
    end_date: str
):
    """
    Retrieve expenses within a date range.
    """

    validate_date_range(start_date, end_date)

    with get_connection() as conn:

        cursor = conn.execute(
            """
            SELECT
                id,
                date,
                amount,
                category,
                subcategory,
                note,
                created_at

            FROM expenses

            WHERE date BETWEEN ? AND ?

            ORDER BY date ASC, id ASC
            """,
            (start_date, end_date)
        )

        expenses = [
            dict(row)
            for row in cursor.fetchall()
        ]

        return {
            "total_records": len(expenses),
            "expenses": expenses
        }


# --------------------------------------------------
# SUMMARIZE EXPENSES - READ
# --------------------------------------------------

@mcp.tool()
def summarize(
    start_date: str,
    end_date: str,
    category: str = None
):
    """
    Calculate total expenses grouped by category.
    Optionally filter by a specific category.
    """

    validate_date_range(start_date, end_date)

    query = """
        SELECT
            category,
            SUM(amount) AS total_amount,
            COUNT(*) AS transaction_count

        FROM expenses

        WHERE date BETWEEN ? AND ?
    """

    params = [
        start_date,
        end_date
    ]

    if category:

        query += " AND category = ?"
        params.append(category)

    query += """
        GROUP BY category
        ORDER BY total_amount DESC
    """

    with get_connection() as conn:

        cursor = conn.execute(
            query,
            params
        )

        results = [
            dict(row)
            for row in cursor.fetchall()
        ]

        grand_total = sum(
            row["total_amount"]
            for row in results
        )

        return {
            "start_date": start_date,
            "end_date": end_date,
            "grand_total": grand_total,
            "categories": results
        }


# --------------------------------------------------
# UPDATE EXPENSE - WRITE
# --------------------------------------------------

@mcp.tool()
def update_expense(
    expense_id: int,
    date: str = None,
    amount: float = None,
    category: str = None,
    subcategory: str = None,
    note: str = None
):
    """
    Update an existing expense.
    Only supplied fields are modified.
    """

    fields = {}
    allowed_fields = {
        "date",
        "amount",
        "category",
        "subcategory",
        "note"
    }

    values = {
        "date": date,
        "amount": amount,
        "category": category,
        "subcategory": subcategory,
        "note": note
    }

    for key, value in values.items():

        if value is not None:

            if key not in allowed_fields:
                continue

            if key == "date":
                validate_date(value)

            elif key == "amount":
                value = validate_amount(value)

            elif key == "category" and not value.strip():
                raise ValueError("Category cannot be empty.")

            elif isinstance(value, str):
                value = value.strip()

            fields[key] = value

    if not fields:
        raise ValueError("No fields provided for update.")

    set_clause = ", ".join(
        f"{key} = ?" for key in fields
    )

    params = list(fields.values())
    params.append(expense_id)

    with get_connection() as conn:

        cursor = conn.execute(
            f"""
            UPDATE expenses
            SET {set_clause}
            WHERE id = ?
            """,
            params
        )

        if cursor.rowcount == 0:
            return {
                "status": "error",
                "message": "Expense not found."
            }

        return {
            "status": "success",
            "message": "Expense updated successfully.",
            "expense_id": expense_id
        }


# --------------------------------------------------
# DELETE EXPENSE - WRITE
# --------------------------------------------------

@mcp.tool()
def delete_expense(expense_id: int):
    """
    Delete an expense using its ID.
    """

    with get_connection() as conn:

        cursor = conn.execute(
            """
            DELETE FROM expenses
            WHERE id = ?
            """,
            (expense_id,)
        )

        if cursor.rowcount == 0:
            return {
                "status": "error",
                "message": "Expense not found."
            }

        return {
            "status": "success",
            "message": "Expense deleted successfully.",
            "expense_id": expense_id
        }


# --------------------------------------------------
# GET SINGLE EXPENSE - READ
# --------------------------------------------------

@mcp.tool()
def get_expense(expense_id: int):
    """
    Retrieve a single expense by ID.
    """

    with get_connection() as conn:

        cursor = conn.execute(
            """
            SELECT *
            FROM expenses
            WHERE id = ?
            """,
            (expense_id,)
        )

        row = cursor.fetchone()

        if row is None:
            return {
                "status": "error",
                "message": "Expense not found."
            }

        return dict(row)


# --------------------------------------------------
# CATEGORY RESOURCE
# --------------------------------------------------

@mcp.resource(
    "expenses://categories",
    mime_type="application/json"
)
def categories():

    if not CATEGORIES_PATH.exists():
        return '{"categories": []}'

    with open(
        CATEGORIES_PATH,
        "r",
        encoding="utf-8"
    ) as file:

        return file.read()


# --------------------------------------------------
# DATABASE HEALTH CHECK
# --------------------------------------------------

@mcp.tool()
def database_status():
    """
    Check database connectivity and record count.
    """

    with get_connection() as conn:

        cursor = conn.execute(
            "SELECT COUNT(*) FROM expenses"
        )

        count = cursor.fetchone()[0]

        return {
            "status": "connected",
            "database": str(DB_PATH),
            "read_write": os.access(DB_DIR, os.R_OK | os.W_OK),
            "total_expenses": count
        }


# --------------------------------------------------
# SERVER STARTUP
# --------------------------------------------------

if __name__ == "__main__":

    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000"))
    )

# uv run fastmcp run .\src\mcp_server\main.py:mcp --transport http --host 0.0.0.0 --port 8000 
# uv run fastmcp install claude-desktop .\src\mcp_server\main.py