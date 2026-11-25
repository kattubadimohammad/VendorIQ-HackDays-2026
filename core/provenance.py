"""
VendorIQ Provenance Tracking Module.

Implements strict provenance classification, audit trails, and validation rules
to ensure transparent data lineage and prevent misattribution of AI or synthetic data.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, model_validator

from core.exceptions import ProvenanceIntegrityError


class ProvenanceClassification(str, Enum):
    """
    Provenance classifications indicating the origin, authority, and reliability
    of data points in VendorIQ.
    """
    BUYER_SPECIFIED = "BUYER_SPECIFIED"
    VERIFIED_DATA = "VERIFIED_DATA"
    AI_ASSESSMENT = "AI_ASSESSMENT"
    UNVERIFIED_OR_MISSING = "UNVERIFIED_OR_MISSING"
    SYNTHETIC_DATA = "SYNTHETIC_DATA"


# Keywords indicating AI or synthetic origins that can NEVER be classified as VERIFIED_DATA
_AI_SYNTHETIC_KEYWORDS = {
    "ai",
    "llm",
    "gemini",
    "gpt",
    "model",
    "synthetic",
    "mock",
    "simulation",
    "simulated",
    "dummy",
    "generated",
    "heuristic",
    "inferred",
}


def is_ai_or_synthetic_source(source: str) -> bool:
    """Check if a source string suggests an AI, heuristic, or synthetic origin."""
    normalized = source.strip().lower()
    tokens = {t.strip() for t in normalized.replace("-", " ").replace("_", " ").split()}
    return bool(tokens & _AI_SYNTHETIC_KEYWORDS)


class ProvenanceRecord(BaseModel):
    """
    Audit record tracking the provenance, source, and supporting evidence of a data point.
    """
    classification: ProvenanceClassification
    source: str = Field(..., min_length=1, description="Entity, registry, API, or system that provided the data")
    evidence: Optional[str] = Field(default=None, description="Direct supporting evidence, certificate number, or registry link")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="UTC timestamp of extraction or entry")
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Confidence score between 0.0 and 1.0")
    notes: Optional[str] = Field(default=None, description="Optional contextual notes regarding provenance")

    @model_validator(mode="after")
    def validate_provenance_rules(self) -> "ProvenanceRecord":
        """
        Enforce strict provenance invariants:
        1. Never classify an AI-generated assessment or synthetic record as verified data.
        2. VERIFIED_DATA requires explicit supporting evidence and an authoritative source.
        """
        source_lower = self.source.strip().lower()

        # Rule 1: Never classify AI-generated or synthetic sources as VERIFIED_DATA
        if self.classification == ProvenanceClassification.VERIFIED_DATA:
            if is_ai_or_synthetic_source(self.source):
                raise ProvenanceIntegrityError(
                    f"Integrity Violation: Source '{self.source}' indicates AI or synthetic origin, "
                    f"which can NEVER be classified as '{ProvenanceClassification.VERIFIED_DATA.value}'."
                )

            # Rule 2: VERIFIED_DATA must have non-empty supporting evidence
            if not self.evidence or not self.evidence.strip():
                raise ProvenanceIntegrityError(
                    f"Integrity Violation: Classification '{ProvenanceClassification.VERIFIED_DATA.value}' "
                    f"requires non-empty supporting evidence (e.g. registry ID, audit link, certificate number)."
                )

        return self


# ---------------------------------------------------------------------------
# Factory helpers for creating provenance records safely
# ---------------------------------------------------------------------------

def create_buyer_specified_provenance(
    source: str = "Buyer Input",
    evidence: Optional[str] = None,
    notes: Optional[str] = None,
) -> ProvenanceRecord:
    """Create a BUYER_SPECIFIED provenance record."""
    return ProvenanceRecord(
        classification=ProvenanceClassification.BUYER_SPECIFIED,
        source=source,
        evidence=evidence,
        confidence=1.0,
        notes=notes,
    )


def create_verified_provenance(
    source: str,
    evidence: str,
    confidence: float = 1.0,
    notes: Optional[str] = None,
) -> ProvenanceRecord:
    """
    Create a VERIFIED_DATA provenance record.
    Raises ProvenanceIntegrityError if source is AI/synthetic or evidence is missing.
    """
    return ProvenanceRecord(
        classification=ProvenanceClassification.VERIFIED_DATA,
        source=source,
        evidence=evidence,
        confidence=confidence,
        notes=notes,
    )


def create_ai_assessment_provenance(
    source: str = "Gemini LLM Assessment",
    evidence: Optional[str] = None,
    confidence: Optional[float] = None,
    notes: Optional[str] = None,
) -> ProvenanceRecord:
    """Create an AI_ASSESSMENT provenance record."""
    return ProvenanceRecord(
        classification=ProvenanceClassification.AI_ASSESSMENT,
        source=source,
        evidence=evidence,
        confidence=confidence,
        notes=notes,
    )


def create_unverified_provenance(
    source: str = "Unverified Web Scrape / Self-Reported",
    evidence: Optional[str] = None,
    notes: Optional[str] = None,
) -> ProvenanceRecord:
    """Create an UNVERIFIED_OR_MISSING provenance record."""
    return ProvenanceRecord(
        classification=ProvenanceClassification.UNVERIFIED_OR_MISSING,
        source=source,
        evidence=evidence,
        confidence=None,
        notes=notes,
    )


def create_synthetic_provenance(
    source: str = "Synthetic Seed Dataset",
    notes: Optional[str] = None,
) -> ProvenanceRecord:
    """Create a SYNTHETIC_DATA provenance record."""
    return ProvenanceRecord(
        classification=ProvenanceClassification.SYNTHETIC_DATA,
        source=source,
        evidence=None,
        confidence=1.0,
        notes=notes,
    )
