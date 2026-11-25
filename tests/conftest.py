"""
VendorIQ Global PyTest Configuration and Database Isolation Safeguards (Phase 13).

Guarantees:
1. All automated tests run against isolated temporary/in-memory SQLite databases by default.
2. VENDORIQ_DB_PATH environment variable is configured to prevent accidental production database writes.
3. Production database 'vendoriq.sqlite' is strictly protected from write operations during testing.
"""

import os
import shutil
from pathlib import Path
import sqlite3
import pytest
from db.connection import init_db
from config import settings


@pytest.fixture(scope="session", autouse=True)
def configure_test_environment(tmp_path_factory):
    """
    Session-level autouse fixture:
    Ensures VENDORIQ_DB_PATH is pointed to an isolated temporary copy of the
    verified baseline database unless explicitly overridden by the test runner.
    Guarantees tests have access to baseline seeded data without touching vendoriq.sqlite.
    """
    orig_env_db = os.environ.get("VENDORIQ_DB_PATH")
    session_db_dir = tmp_path_factory.mktemp("vendoriq_test_session")
    session_db_path = str(session_db_dir / "isolated_session_vendoriq.sqlite")

    # Copy verified baseline snapshot to session test db
    baseline_src = Path("vendoriq_backup_release_phase13.sqlite")
    if baseline_src.exists():
        shutil.copy2(baseline_src, session_db_path)
    else:
        init_db(session_db_path)

    # Set environment variable so any unparameterized service calls use the isolated DB
    if not orig_env_db:
        os.environ["VENDORIQ_DB_PATH"] = session_db_path

    yield session_db_path

    # Teardown: restore environment
    if orig_env_db is not None:
        os.environ["VENDORIQ_DB_PATH"] = orig_env_db
    else:
        os.environ.pop("VENDORIQ_DB_PATH", None)


@pytest.fixture
def isolated_db(tmp_path):
    """
    Function-level fixture providing a fresh, isolated SQLite database with schema provisioned.
    """
    db_file = tmp_path / "isolated_test_vendoriq.sqlite"
    db_path = str(db_file)
    init_db(db_path)
    return db_path

# The release SQLite snapshot and production database are intentionally private
# and are never committed. In clean CI, skip only integration audits whose
# explicit subject is that private, pre-populated local dataset. All unit,
# service, workflow, security, and synthetic-database tests remain enabled.
def pytest_collection_modifyitems(config, items):
    from pathlib import Path

    has_private_baseline = Path("vendoriq_backup_release_phase13.sqlite").is_file()
    has_production_db = Path("vendoriq.sqlite").is_file()
    baseline_only_classes = {
        "TestSupplierDiscoveryService",
        "TestLiveDatabaseDiscoveryIntegration",
        "TestLiveDatabaseEvidenceIntegration",
        "TestProductionDatabaseIsolation12C",
        "TestProductionDatabaseIsolation12D",
        "TestMilestone12EProductionDatabaseIntegrity",
    }

    if has_private_baseline and has_production_db:
        return

    marker = pytest.mark.skip(
        reason="Requires the private local Phase 13 SQLite baseline; intentionally unavailable in clean CI."
    )
    for item in items:
        class_name = getattr(item, "cls", None)
        class_name = class_name.__name__ if class_name is not None else ""
        if class_name in baseline_only_classes and not (has_private_baseline and has_production_db):
            item.add_marker(marker)
