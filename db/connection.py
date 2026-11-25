"""
VendorIQ Database Connection and Initialization Module.

Manages SQLite database connections, schema provisioning, foreign key enforcement,
and transaction safety using Python's built-in sqlite3.
"""

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
from typing import Generator, Optional

from config import settings

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_default_db_path() -> str:
    """
    Retrieve configured database path from environment or application settings.
    VENDORIQ_DB_PATH environment variable takes precedence over default settings.
    Anchored to project root when relative.
    """
    env_p = os.environ.get("VENDORIQ_DB_PATH")
    db_p = env_p.strip() if env_p and env_p.strip() else settings.effective_database_path
    if not os.path.isabs(db_p) and db_p != ":memory:" and not db_p.startswith("file:"):
        project_root = Path(__file__).resolve().parent.parent
        return str((project_root / db_p).resolve())
    return db_p


def create_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """
    Create a configured SQLite connection with foreign keys enabled
    and row_factory set to sqlite3.Row.
    """
    path = db_path if db_path is not None else get_default_db_path()

    # Ensure parent directory exists if path contains directories and is not in-memory
    if path != ":memory:" and not path.startswith("file:"):
        parent_dir = os.path.dirname(os.path.abspath(path))
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def get_db_connection(db_path: Optional[str] = None) -> Generator[sqlite3.Connection, None, None]:
    """
    Context manager providing safe SQLite connection handling.
    Automatically commits on successful block completion and rolls back on exception.
    Ensures the connection is always closed.
    """
    conn = create_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Optional[str] = None) -> None:
    """
    Initialize SQLite database schema if tables do not already exist.
    Does not overwrite existing database records.
    """
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(f"Schema definition file not found at: {SCHEMA_PATH}")

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with get_db_connection(db_path) as conn:
        conn.executescript(schema_sql)
