"""
Tests for VendorIQ Core Configuration and Dependencies (Phase 2).
"""

import importlib
import pytest
from pydantic import SecretStr
from config import Settings, AppMode, get_settings


REQUIRED_PACKAGES = [
    "streamlit",
    "pandas",
    "google.genai",
    "pydantic",
    "pydantic_settings",
    "dotenv",
    "requests",
    "plotly",
    "pytest",
]


def test_package_imports():
    """Verify all required foundation packages import successfully."""
    for pkg in REQUIRED_PACKAGES:
        mod = importlib.import_module(pkg)
        assert mod is not None, f"Failed to import {pkg}"


def test_default_configuration_safe_loading():
    """Verify configuration loads cleanly with safe defaults without real API keys."""
    # Test instantiated directly with defaults
    cfg = Settings(
        APP_MODE="OFFLINE",
        GEMINI_API_KEY=None,
        CRUSTDATA_API_KEY=None,
    )
    assert cfg.APP_MODE == AppMode.OFFLINE.value
    assert cfg.DATABASE_PATH == "vendoriq.sqlite"
    assert not cfg.is_gemini_configured
    assert not cfg.is_crustdata_configured

    # Check key validation in offline mode
    is_valid, messages = cfg.validate_keys()
    assert is_valid is True
    assert any("OFFLINE mode" in msg for msg in messages)


def test_missing_api_keys_produce_clear_safe_messages():
    """Verify attempting to access missing keys gives clear and safe error messages."""
    cfg = Settings(
        APP_MODE="OFFLINE",
        GEMINI_API_KEY=None,
        CRUSTDATA_API_KEY=None,
    )

    with pytest.raises(ValueError) as exc_gemini:
        cfg.get_gemini_api_key()
    assert "GEMINI_API_KEY is not configured" in str(exc_gemini.value)

    with pytest.raises(ValueError) as exc_crust:
        cfg.get_crustdata_api_key()
    assert "CRUSTDATA_API_KEY is not configured" in str(exc_crust.value)


def test_placeholder_api_keys_treated_as_unconfigured():
    """Verify placeholder values from .env.example are treated as unconfigured."""
    cfg = Settings(
        APP_MODE="OFFLINE",
        GEMINI_API_KEY=SecretStr("your_gemini_api_key_here"),
        CRUSTDATA_API_KEY=SecretStr("your_crustdata_api_key_here"),
    )
    assert not cfg.is_gemini_configured
    assert not cfg.is_crustdata_configured

    with pytest.raises(ValueError) as exc_gemini:
        cfg.get_gemini_api_key()
    assert "GEMINI_API_KEY is not configured" in str(exc_gemini.value)

    with pytest.raises(ValueError) as exc_crust:
        cfg.get_crustdata_api_key()
    assert "CRUSTDATA_API_KEY is not configured" in str(exc_crust.value)


def test_online_mode_validation_flags_missing_keys():
    """Verify that ONLINE mode flags missing keys without throwing unhandled exceptions."""
    cfg = Settings(
        APP_MODE="ONLINE",
        GEMINI_API_KEY=None,
        CRUSTDATA_API_KEY=None,
    )
    is_valid, messages = cfg.validate_keys()
    assert is_valid is False
    assert any("GEMINI_API_KEY is not configured" in msg for msg in messages)
    assert any("CRUSTDATA_API_KEY is not configured" in msg for msg in messages)


def test_secrets_are_masked_in_repr_and_summary():
    """Verify secrets are never exposed when stringified, logged, or summarized."""
    fake_gemini = "AIzaSyDummySecretKey123456"
    fake_crust = "crust_dummy_secret_987654"

    cfg = Settings(
        APP_MODE="ONLINE",
        GEMINI_API_KEY=SecretStr(fake_gemini),
        CRUSTDATA_API_KEY=SecretStr(fake_crust),
    )

    # Key is properly recognized as configured
    assert cfg.is_gemini_configured is True
    assert cfg.is_crustdata_configured is True

    # Representations must NOT contain the plain secrets
    repr_str = repr(cfg)
    assert fake_gemini not in repr_str
    assert fake_crust not in repr_str

    str_val = str(cfg)
    assert fake_gemini not in str_val
    assert fake_crust not in str_val

    # Summary dictionary must not expose secret strings
    summary = cfg.safe_summary()
    assert summary["GEMINI_API_KEY_CONFIGURED"] is True
    assert summary["CRUSTDATA_API_KEY_CONFIGURED"] is True
    assert fake_gemini not in str(summary)
    assert fake_crust not in str(summary)


def test_no_external_api_calls_on_import():
    """Verify importing and instantiating config does not trigger any network requests."""
    # Instantiating default settings
    s = get_settings()
    assert s is not None


def test_vendoriq_db_path_environment_variable_precedence(monkeypatch):
    """Verify VENDORIQ_DB_PATH takes precedence over default DATABASE_PATH."""
    # Default without VENDORIQ_DB_PATH
    monkeypatch.delenv("VENDORIQ_DB_PATH", raising=False)
    cfg_default = Settings(DATABASE_PATH="vendoriq.sqlite", VENDORIQ_DB_PATH=None)
    assert cfg_default.effective_database_path == "vendoriq.sqlite"
    assert cfg_default.is_production_database is True

    # Overridden via environment variable
    custom_path = "data/isolated_test_env.sqlite"
    monkeypatch.setenv("VENDORIQ_DB_PATH", custom_path)
    cfg_env = Settings(DATABASE_PATH="vendoriq.sqlite", VENDORIQ_DB_PATH=None)
    assert cfg_env.effective_database_path == custom_path
    assert cfg_env.is_production_database is False

    # Overridden via direct attribute
    cfg_attr = Settings(DATABASE_PATH="vendoriq.sqlite", VENDORIQ_DB_PATH="data/custom_attr.sqlite")
    assert cfg_attr.effective_database_path == "data/custom_attr.sqlite"
    assert cfg_attr.is_production_database is False


def test_is_production_database_identification(monkeypatch):
    """Verify is_production_database property accurately distinguishes environments."""
    monkeypatch.delenv("VENDORIQ_DB_PATH", raising=False)
    prod_cfg = Settings(DATABASE_PATH="vendoriq.sqlite", VENDORIQ_DB_PATH=None)
    assert prod_cfg.is_production_database is True

    mem_cfg = Settings(DATABASE_PATH=":memory:", VENDORIQ_DB_PATH=None)
    assert mem_cfg.is_production_database is False

    qa_cfg = Settings(DATABASE_PATH="vendoriq.sqlite", VENDORIQ_DB_PATH="data/qa_isolated.sqlite")
    assert qa_cfg.is_production_database is False


def test_safe_summary_includes_database_metadata():
    """Verify safe_summary includes effective database path and production safety flag."""
    cfg = Settings(
        APP_MODE="OFFLINE",
        DATABASE_PATH="vendoriq.sqlite",
        VENDORIQ_DB_PATH="data/secure_sandbox.sqlite",
    )
    summary = cfg.safe_summary()
    assert summary["DATABASE_PATH"] == "data/secure_sandbox.sqlite"
    assert summary["IS_PRODUCTION_DB"] is False
