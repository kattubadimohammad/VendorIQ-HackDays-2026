"""
Tests for VendorIQ Provenance System (Phase 3).
"""

import pytest
from core.exceptions import ProvenanceIntegrityError
from core.provenance import (
    ProvenanceClassification,
    ProvenanceRecord,
    create_buyer_specified_provenance,
    create_verified_provenance,
    create_ai_assessment_provenance,
    create_unverified_provenance,
    create_synthetic_provenance,
    is_ai_or_synthetic_source,
)


def test_provenance_classifications_enum_values():
    """Verify all 5 required classifications exist."""
    expected = {
        "BUYER_SPECIFIED",
        "VERIFIED_DATA",
        "AI_ASSESSMENT",
        "UNVERIFIED_OR_MISSING",
        "SYNTHETIC_DATA",
    }
    actual = {c.value for c in ProvenanceClassification}
    assert actual == expected


def test_valid_verified_provenance_creation():
    """Verify authoritative verified data with evidence succeeds."""
    rec = create_verified_provenance(
        source="ISO Official Registry",
        evidence="Cert #ISO-9001-2026-US-8819",
        confidence=1.0,
        notes="Validated against accredited audit registry",
    )
    assert rec.classification == ProvenanceClassification.VERIFIED_DATA
    assert rec.source == "ISO Official Registry"
    assert rec.evidence == "Cert #ISO-9001-2026-US-8819"
    assert rec.confidence == 1.0


def test_ai_source_cannot_be_verified_data():
    """Verify AI models or LLM assessments can NEVER be classified as VERIFIED_DATA."""
    invalid_sources = [
        "Gemini 1.5 Flash",
        "LLM Model Assessment",
        "GPT-4 Extracted",
        "AI Inferred Heuristic",
        "AI-Generated Guess",
    ]
    for src in invalid_sources:
        with pytest.raises(ProvenanceIntegrityError) as exc_info:
            ProvenanceRecord(
                classification=ProvenanceClassification.VERIFIED_DATA,
                source=src,
                evidence="Some simulated text",
            )
        assert "Integrity Violation" in str(exc_info.value)
        assert "can NEVER be classified as 'VERIFIED_DATA'" in str(exc_info.value)


def test_synthetic_source_cannot_be_verified_data():
    """Verify synthetic / simulated mock data can NEVER be classified as VERIFIED_DATA."""
    invalid_sources = [
        "Synthetic Seed Generator",
        "Mock Benchmark Dataset",
        "Simulated Vendor Database",
        "Dummy Seed Data",
    ]
    for src in invalid_sources:
        with pytest.raises(ProvenanceIntegrityError) as exc_info:
            ProvenanceRecord(
                classification=ProvenanceClassification.VERIFIED_DATA,
                source=src,
                evidence="Mock evidence ID",
            )
        assert "Integrity Violation" in str(exc_info.value)


def test_verified_data_requires_non_empty_evidence():
    """Verify VERIFIED_DATA requires non-empty supporting evidence."""
    with pytest.raises(ProvenanceIntegrityError) as exc_info:
        ProvenanceRecord(
            classification=ProvenanceClassification.VERIFIED_DATA,
            source="Official Commercial Registry",
            evidence=None,
        )
    assert "requires non-empty supporting evidence" in str(exc_info.value)

    with pytest.raises(ProvenanceIntegrityError) as exc_info2:
        ProvenanceRecord(
            classification=ProvenanceClassification.VERIFIED_DATA,
            source="Official Commercial Registry",
            evidence="   ",
        )
    assert "requires non-empty supporting evidence" in str(exc_info2.value)


def test_provenance_factory_helpers():
    """Verify convenience factory constructors set expected fields and classifications."""
    buyer = create_buyer_specified_provenance(source="User Brief Intake Form")
    assert buyer.classification == ProvenanceClassification.BUYER_SPECIFIED
    assert buyer.source == "User Brief Intake Form"

    ai = create_ai_assessment_provenance(source="Gemini Model Inference", confidence=0.88)
    assert ai.classification == ProvenanceClassification.AI_ASSESSMENT
    assert ai.confidence == 0.88

    unv = create_unverified_provenance(source="Public Web Directory")
    assert unv.classification == ProvenanceClassification.UNVERIFIED_OR_MISSING

    syn = create_synthetic_provenance(source="Local Test Generator")
    assert syn.classification == ProvenanceClassification.SYNTHETIC_DATA
    assert syn.confidence == 1.0


def test_confidence_range_validation():
    """Verify confidence must be between 0.0 and 1.0."""
    with pytest.raises(Exception):
        ProvenanceRecord(
            classification=ProvenanceClassification.AI_ASSESSMENT,
            source="Gemini",
            confidence=1.5,
        )

    with pytest.raises(Exception):
        ProvenanceRecord(
            classification=ProvenanceClassification.AI_ASSESSMENT,
            source="Gemini",
            confidence=-0.1,
        )


def test_is_ai_or_synthetic_helper():
    """Verify the detector helper identifies AI and synthetic sources."""
    assert is_ai_or_synthetic_source("gemini-1.5-pro") is True
    assert is_ai_or_synthetic_source("Synthetic Seed Set") is True
    assert is_ai_or_synthetic_source("LLM Output") is True
    assert is_ai_or_synthetic_source("Mock Provider") is True
    assert is_ai_or_synthetic_source("Official Chamber of Commerce") is False
    assert is_ai_or_synthetic_source("Dun & Bradstreet Verified Record") is False
