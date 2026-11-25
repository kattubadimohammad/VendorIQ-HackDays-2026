"""
VendorIQ Core Configuration Module.

Provides secure, validated application settings management using Pydantic Settings.
Designed to safely handle environment variables and secrets with zero risk of key leakage.
"""

import os
from enum import Enum
from pathlib import Path
from typing import Optional, List, Tuple, Dict, Any
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppMode(str, Enum):
    OFFLINE = "OFFLINE"
    ONLINE = "ONLINE"


# Known placeholder values that should not be treated as real API keys
_PLACEHOLDER_KEYS = {
    "",
    "your_gemini_api_key_here",
    "your_crustdata_api_key_here",
    "placeholder",
    "none",
    "null",
}


class Settings(BaseSettings):
    """
    VendorIQ application settings loaded from environment variables and/or .env file.

    All secrets are encapsulated in SecretStr to prevent accidental logging or exposure.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_MODE: str = AppMode.OFFLINE.value
    GEMINI_API_KEY: Optional[SecretStr] = None
    CRUSTDATA_API_KEY: Optional[SecretStr] = None
    DATABASE_PATH: str = "vendoriq.sqlite"
    VENDORIQ_DB_PATH: Optional[str] = None

    @property
    def effective_database_path(self) -> str:
        """
        Return the resolved active database path.
        VENDORIQ_DB_PATH (explicit attribute or environment variable) takes precedence over DATABASE_PATH.
        """
        if self.VENDORIQ_DB_PATH and self.VENDORIQ_DB_PATH.strip():
            return self.VENDORIQ_DB_PATH.strip()
        env_val = os.environ.get("VENDORIQ_DB_PATH")
        if env_val and env_val.strip():
            return env_val.strip()
        return self.DATABASE_PATH

    @property
    def is_production_database(self) -> bool:
        """
        Return True if pointing to the default production database ('vendoriq.sqlite').
        """
        eff = self.effective_database_path
        if eff in (":memory:", "") or eff.startswith("file:"):
            return False
        return Path(eff).name.lower() == "vendoriq.sqlite"

    @field_validator("APP_MODE", mode="before")
    @classmethod
    def _normalize_app_mode(cls, value: Any) -> str:
        if isinstance(value, str):
            normalized = value.strip().upper()
            if normalized in (AppMode.OFFLINE.value, AppMode.ONLINE.value):
                return normalized
        return AppMode.OFFLINE.value

    @property
    def is_gemini_configured(self) -> bool:
        """Return True if a non-placeholder GEMINI_API_KEY is configured."""
        if not self.GEMINI_API_KEY:
            return False
        val = self.GEMINI_API_KEY.get_secret_value().strip()
        return val.lower() not in _PLACEHOLDER_KEYS

    @property
    def is_crustdata_configured(self) -> bool:
        """Return True if a non-placeholder CRUSTDATA_API_KEY is configured."""
        if not self.CRUSTDATA_API_KEY:
            return False
        val = self.CRUSTDATA_API_KEY.get_secret_value().strip()
        return val.lower() not in _PLACEHOLDER_KEYS

    def get_gemini_api_key(self) -> str:
        """
        Safely retrieve the raw Gemini API key string.
        Raises ValueError with a clear, safe message if missing or placeholder.
        """
        if not self.is_gemini_configured:
            raise ValueError(
                "GEMINI_API_KEY is not configured. "
                "Please set a valid GEMINI_API_KEY in your environment or .env file."
            )
        assert self.GEMINI_API_KEY is not None
        return self.GEMINI_API_KEY.get_secret_value()

    def get_crustdata_api_key(self) -> str:
        """
        Safely retrieve the raw Crustdata API key string.
        Raises ValueError with a clear, safe message if missing or placeholder.
        """
        if not self.is_crustdata_configured:
            raise ValueError(
                "CRUSTDATA_API_KEY is not configured. "
                "Please set a valid CRUSTDATA_API_KEY in your environment or .env file."
            )
        assert self.CRUSTDATA_API_KEY is not None
        return self.CRUSTDATA_API_KEY.get_secret_value()

    def validate_keys(self) -> Tuple[bool, List[str]]:
        """
        Validate current configuration against the active APP_MODE.
        Returns (is_valid, list_of_messages).
        Does not print or leak secrets.
        """
        messages: List[str] = []
        is_valid = True

        if self.APP_MODE == AppMode.OFFLINE.value:
            messages.append("Operating in OFFLINE mode: external API keys are not required.")
            if not self.is_gemini_configured:
                messages.append("Notice: GEMINI_API_KEY is not configured (offline mock/fallback enabled).")
            if not self.is_crustdata_configured:
                messages.append("Notice: CRUSTDATA_API_KEY is not configured (offline mock/fallback enabled).")
        else:
            # ONLINE mode requires valid keys
            if not self.is_gemini_configured:
                messages.append("Missing required key for ONLINE mode: GEMINI_API_KEY is not configured.")
                is_valid = False
            if not self.is_crustdata_configured:
                messages.append("Missing required key for ONLINE mode: CRUSTDATA_API_KEY is not configured.")
                is_valid = False

        return is_valid, messages

    def safe_summary(self) -> Dict[str, Any]:
        """
        Return safe status summary without exposing any secret values.
        """
        return {
            "APP_MODE": self.APP_MODE,
            "GEMINI_API_KEY_CONFIGURED": self.is_gemini_configured,
            "CRUSTDATA_API_KEY_CONFIGURED": self.is_crustdata_configured,
            "DATABASE_PATH": self.effective_database_path,
            "IS_PRODUCTION_DB": self.is_production_database,
        }

    def __repr__(self) -> str:
        return (
            f"Settings(APP_MODE={self.APP_MODE!r}, "
            f"GEMINI_API_KEY={'[CONFIGURED]' if self.is_gemini_configured else '[NOT SET]'}, "
            f"CRUSTDATA_API_KEY={'[CONFIGURED]' if self.is_crustdata_configured else '[NOT SET]'}, "
            f"DATABASE_PATH={self.effective_database_path!r}, "
            f"IS_PRODUCTION_DB={self.is_production_database})"
        )

    def __str__(self) -> str:
        return self.__repr__()


def get_settings() -> Settings:
    """Return a fresh or singleton Settings instance."""
    return Settings()


# Default singleton instance for convenience
settings = get_settings()
