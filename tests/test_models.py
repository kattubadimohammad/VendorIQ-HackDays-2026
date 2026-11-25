"""
Tests for VendorIQ Core Domain Models (Phase 3).
"""

from datetime import datetime, timezone, timedelta
import pytest
from core.exceptions import DataValidationError, ProvenanceIntegrityError
from core.provenance import (
    ProvenanceClassification,
    create_verified_provenance,
    create_unverified_provenance,
    create_synthetic_provenance,
    create_ai_assessment_provenance,
)
from core.models import (
    ScoringDimension,
    ScoringWeights,
    DataField,
    Certification,
    SourcingCriteria,
    SourcingBrief,
    SupplierProfile,
    FactorScore,
    SupplierScore,
    RFQDraft,
)


# ---------------------------------------------------------------------------
# Scoring Weights and Dimensions
# ---------------------------------------------------------------------------

def test_default_scoring_weights_total_1():
    """Verify default weights match the five agreed dimensions and total exactly 1.0."""
    weights = ScoringWeights()
    assert weights.industry_relevance == 0.35
    assert weights.geographic_fit == 0.25
    assert weights.company_size_fit == 0.20
    assert weights.business_signals == 0.10
    assert weights.data_completeness == 0.10

    total = sum(weights.as_dict().values())
    assert pytest.approx(total, abs=1e-5) == 1.0


def test_custom_configurable_weights_valid():
    """Verify weights can be configured as long as they sum to 1.0."""
    custom = ScoringWeights(
        industry_relevance=0.40,
        geographic_fit=0.20,
        company_size_fit=0.20,
        business_signals=0.10,
        data_completeness=0.10,
    )
    assert custom.industry_relevance == 0.40
    assert pytest.approx(sum(custom.as_dict().values()), abs=1e-5) == 1.0


def test_invalid_weights_sum_rejected():
    """Verify weights that do not sum to 1.0 raise DataValidationError."""
    with pytest.raises(DataValidationError) as exc:
        ScoringWeights(
            industry_relevance=0.50,
            geographic_fit=0.50,
            company_size_fit=0.20,  # Total = 1.40
            business_signals=0.10,
            data_completeness=0.10,
        )
    assert "weights must sum to 1.0" in str(exc.value)


def test_negative_weight_rejected():
    """Verify weights cannot be negative."""
    with pytest.raises(Exception):
        ScoringWeights(
            industry_relevance=-0.10,
            geographic_fit=0.40,
            company_size_fit=0.30,
            business_signals=0.20,
            data_completeness=0.20,
        )


# ---------------------------------------------------------------------------
# DataField and Provenance
# ---------------------------------------------------------------------------

def test_datafield_verified_factory():
    """Verify DataField.verified properly bundles authoritative provenance and evidence."""
    field = DataField.verified(
        name="annual_revenue",
        value=50_000_000.0,
        source="Audited SEC Filing 10-K",
        evidence="https://sec.gov/edgar/filing/12345",
    )
    assert field.name == "annual_revenue"
    assert field.value == 50_000_000.0
    assert field.provenance.classification == ProvenanceClassification.VERIFIED_DATA
    assert field.provenance.evidence == "https://sec.gov/edgar/filing/12345"
    assert field.is_missing is False


def test_datafield_missing_factory():
    """Verify missing field is correctly tagged without throwing exceptions."""
    field = DataField.missing(name="carbon_emissions_mt", notes="Not disclosed by supplier")
    assert field.name == "carbon_emissions_mt"
    assert field.value is None
    assert field.is_missing is True
    assert field.provenance.classification == ProvenanceClassification.UNVERIFIED_OR_MISSING


# ---------------------------------------------------------------------------
# Certifications: Verified vs Unverified Claims
# ---------------------------------------------------------------------------

def test_verified_certification_with_evidence():
    """Verify legitimate certification with audit evidence passes."""
    prov = create_verified_provenance(
        source="IAF CertSearch Official Database",
        evidence="IAF-ID: 994827104-ISO9001",
    )
    cert = Certification(
        name="ISO 9001:2015",
        issuing_body="TUV SUD",
        is_verified=True,
        verification_evidence="IAF-ID: 994827104-ISO9001",
        provenance=prov,
    )
    assert cert.is_verified is True
    assert cert.verification_evidence == "IAF-ID: 994827104-ISO9001"


def test_verified_certification_missing_evidence_fails():
    """Verify marking a certification verified without evidence fails."""
    prov = create_verified_provenance(
        source="Accredited Registrar",
        evidence="Valid-Evidence-123",
    )
    with pytest.raises(DataValidationError) as exc:
        Certification(
            name="ISO 14001",
            is_verified=True,
            verification_evidence="",  # Missing evidence
            provenance=prov,
        )
    assert "lacks supporting verification_evidence" in str(exc.value)


def test_unverified_certification_cannot_claim_verified_provenance():
    """Verify unverified claims cannot claim VERIFIED_DATA classification."""
    prov = create_verified_provenance(
        source="Registrar X",
        evidence="Evidence X",
    )
    with pytest.raises(ProvenanceIntegrityError) as exc:
        Certification(
            name="SOC 2 Type II",
            is_verified=False,  # Unverified claim!
            verification_evidence="Self-reported claim",
            provenance=prov,  # But claiming VERIFIED_DATA!
        )
    assert "cannot be classified as 'VERIFIED_DATA'" in str(exc.value)


# ---------------------------------------------------------------------------
# SourcingCriteria & SourcingBrief
# ---------------------------------------------------------------------------

def test_sourcing_criteria_valid():
    """Verify valid SourcingCriteria instantiation."""
    crit = SourcingCriteria(
        target_industries=["Precision Machining", "Aerospace CNC"],
        target_geographies=["US", "CA", "MX"],
        min_company_size=50,
        max_company_size=500,
        required_certifications=["AS9100D"],
    )
    assert len(crit.target_industries) == 2
    assert crit.min_company_size == 50
    assert crit.max_company_size == 500


def test_sourcing_criteria_invalid_size_range():
    """Verify min_company_size > max_company_size fails validation."""
    with pytest.raises(DataValidationError) as exc:
        SourcingCriteria(
            target_industries=["Electronics"],
            target_geographies=["US"],
            min_company_size=500,
            max_company_size=50,
        )
    assert "cannot exceed max_company_size" in str(exc.value)


def test_sourcing_brief_valid():
    """Verify valid SourcingBrief instantiation."""
    crit = SourcingCriteria(
        target_industries=["Industrial Sensors"],
        target_geographies=["DE", "NL"],
    )
    brief = SourcingBrief(
        title="Automotive Tier-2 Sensor Supplier Search",
        buyer_organization="Continental Mobility Systems Inc.",
        description="Seeking qualified European Tier-2 ultrasonic and radar sensor manufacturers.",
        criteria=crit,
        budget_range_usd=(100_000.0, 500_000.0),
        timeline_weeks=12,
    )
    assert brief.title.startswith("Automotive")
    assert brief.buyer_organization == "Continental Mobility Systems Inc."
    assert brief.budget_range_usd == (100_000.0, 500_000.0)


def test_sourcing_brief_invalid_budget():
    """Verify invalid budget range fails validation."""
    crit = SourcingCriteria(target_industries=["Robotics"], target_geographies=["JP"])
    with pytest.raises(DataValidationError) as exc:
        SourcingBrief(
            title="Robotic Arm Procurement",
            buyer_organization="AutoWorks Ltd",
            description="Procuring 6-axis articulation robotic arms.",
            criteria=crit,
            budget_range_usd=(500_000.0, 100_000.0),  # Min > Max!
        )
    assert "exceeds maximum budget" in str(exc.value)


# ---------------------------------------------------------------------------
# SupplierProfile: Company-Level Privacy & Synthetic Separation
# ---------------------------------------------------------------------------

def test_supplier_profile_corporate_email_only():
    """Verify personal email addresses are rejected to enforce company-level data only."""
    with pytest.raises(DataValidationError) as exc:
        SupplierProfile(
            supplier_id="SUP-001",
            company_name="Apex Tooling LLC",
            domain="apextooling.com",
            headquarters_country="US",
            corporate_contact_email="john.doe@gmail.com",  # Disallowed personal PII
        )
    assert "personal email domain" in str(exc.value)


def test_supplier_profile_valid_corporate():
    """Verify valid corporate profile with company email passes."""
    sup = SupplierProfile(
        supplier_id="SUP-001",
        company_name="Apex Tooling LLC",
        domain="apextooling.com",
        headquarters_country="US",
        corporate_contact_email="procurement@apextooling.com",
        employee_count=120,
        industries=["Tool & Die", "CNC Milling"],
    )
    assert sup.supplier_id == "SUP-001"
    assert sup.corporate_contact_email == "procurement@apextooling.com"
    assert sup.completeness_percentage() > 0.0


def test_synthetic_profile_cannot_claim_verified_data():
    """Verify a synthetic supplier profile cannot claim VERIFIED_DATA overall provenance."""
    with pytest.raises(ProvenanceIntegrityError) as exc:
        SupplierProfile(
            supplier_id="SYNTH-99",
            company_name="Simulated Tech Inc",
            domain="simulated-tech.local",
            headquarters_country="US",
            is_synthetic=True,
            overall_provenance=ProvenanceClassification.VERIFIED_DATA,  # Violation!
        )
    assert "Synthetic supplier profile cannot have VERIFIED_DATA" in str(exc.value)


def test_synthetic_profile_cannot_have_verified_certifications():
    """Verify synthetic profiles cannot hold verified certifications."""
    prov = create_verified_provenance(
        source="Fake Registry",
        evidence="Cert #123",
    )
    cert = Certification(
        name="ISO 9001",
        is_verified=True,
        verification_evidence="Cert #123",
        provenance=prov,
    )
    with pytest.raises(ProvenanceIntegrityError) as exc:
        SupplierProfile(
            supplier_id="SYNTH-100",
            company_name="Simulated Fasteners Ltd",
            domain="sim-fasteners.local",
            headquarters_country="CA",
            is_synthetic=True,
            certifications=[cert],
        )
    assert "cannot possess verified certification" in str(exc.value)


# ---------------------------------------------------------------------------
# FactorScore and Missing Data Handling (0 to 100 Range)
# ---------------------------------------------------------------------------

def test_factorscore_valid_range():
    """Verify factor scores between 0.0 and 100.0 calculate weighted score properly."""
    factor = FactorScore(
        dimension=ScoringDimension.INDUSTRY_RELEVANCE,
        score=85.0,
        weight=0.35,
        reasoning="Strong alignment with aerospace machining standards",
    )
    assert factor.score == 85.0
    assert factor.weight == 0.35
    assert factor.weighted_score == round(85.0 * 0.35, 4)


def test_factorscore_out_of_range_rejected():
    """Verify scores below 0 or above 100 are rejected."""
    with pytest.raises(Exception):
        FactorScore(
            dimension=ScoringDimension.GEOGRAPHIC_FIT,
            score=105.0,  # > 100
            weight=0.25,
            reasoning="Too high",
        )

    with pytest.raises(Exception):
        FactorScore(
            dimension=ScoringDimension.GEOGRAPHIC_FIT,
            score=-5.0,  # < 0
            weight=0.25,
            reasoning="Negative score",
        )


def test_missing_data_neutral_factor_score():
    """
    Verify missing information is not treated as proof of poor supplier quality.
    Neutral baseline score (50.0) is assigned with explicit missing data flag.
    """
    factor = FactorScore.missing_data_neutral(
        dimension=ScoringDimension.BUSINESS_SIGNALS,
        weight=0.10,
    )
    assert factor.score == 50.0
    assert factor.is_missing_data is True
    assert factor.provenance == ProvenanceClassification.UNVERIFIED_OR_MISSING
    assert "missing data is not penalized as poor quality" in factor.reasoning


# ---------------------------------------------------------------------------
# SupplierScore Validation
# ---------------------------------------------------------------------------

def test_supplierscore_compute_and_validation():
    """Verify full scorecard computation across all 5 dimensions."""
    weights = ScoringWeights()
    factors = {
        ScoringDimension.INDUSTRY_RELEVANCE: FactorScore(
            dimension=ScoringDimension.INDUSTRY_RELEVANCE,
            score=90.0,
            weight=weights.industry_relevance,
            reasoning="Top tier precision capability",
        ),
        ScoringDimension.GEOGRAPHIC_FIT: FactorScore(
            dimension=ScoringDimension.GEOGRAPHIC_FIT,
            score=80.0,
            weight=weights.geographic_fit,
            reasoning="Domestic US supplier",
        ),
        ScoringDimension.COMPANY_SIZE_FIT: FactorScore(
            dimension=ScoringDimension.COMPANY_SIZE_FIT,
            score=70.0,
            weight=weights.company_size_fit,
            reasoning="Mid-sized team",
        ),
        ScoringDimension.BUSINESS_SIGNALS: FactorScore(
            dimension=ScoringDimension.BUSINESS_SIGNALS,
            score=60.0,
            weight=weights.business_signals,
            reasoning="Modest patent activity",
        ),
        ScoringDimension.DATA_COMPLETENESS: FactorScore(
            dimension=ScoringDimension.DATA_COMPLETENESS,
            score=100.0,
            weight=weights.data_completeness,
            reasoning="Full profile populated",
        ),
    }

    # Expected total: 90*0.35 + 80*0.25 + 70*0.20 + 60*0.10 + 100*0.10
    # = 31.5 + 20.0 + 14.0 + 6.0 + 10.0 = 81.5
    score = SupplierScore.compute(
        supplier_id="SUP-001",
        company_name="Apex Tooling LLC",
        factor_scores=factors,
        data_completeness_percentage=100.0,
        summary="Strong candidate with high technical fit.",
        weights_used=weights,
    )

    assert pytest.approx(score.total_score, abs=0.1) == 81.5
    assert score.data_completeness_percentage == 100.0
    assert len(score.factor_scores) == 5


def test_supplierscore_missing_dimension_raises():
    """Verify omitting any of the 5 required dimensions raises DataValidationError."""
    weights = ScoringWeights()
    incomplete_factors = {
        ScoringDimension.INDUSTRY_RELEVANCE: FactorScore(
            dimension=ScoringDimension.INDUSTRY_RELEVANCE,
            score=90.0,
            weight=weights.industry_relevance,
            reasoning="Fit",
        ),
        # 4 dimensions missing!
    }
    with pytest.raises(DataValidationError) as exc:
        SupplierScore(
            supplier_id="SUP-002",
            company_name="Partial Co",
            total_score=31.5,
            factor_scores=incomplete_factors,
            data_completeness_percentage=50.0,
            summary="Incomplete scorecard",
        )
    assert "Missing evaluation factor for required dimension" in str(exc.value)


# ---------------------------------------------------------------------------
# RFQDraft Model
# ---------------------------------------------------------------------------

def test_rfq_draft_lifecycle():
    """Verify RFQDraft model instantiation and status validation."""
    deadline = datetime.now(timezone.utc) + timedelta(days=14)
    rfq = RFQDraft(
        brief_id="BRIEF-1234",
        target_supplier_ids=["SUP-001", "SUP-002"],
        title="RFQ for High-Precision Titanium Bracket Machining",
        scope_of_work="Manufacture and delivery of 500 units of titanium structural brackets.",
        technical_requirements=["Titanium Ti-6Al-4V Grade 5", "Tolerance: +/- 0.005mm", "AS9100 certified facility"],
        submission_deadline=deadline,
        response_instructions="Submit quotation and material spec sheets via VendorIQ portal.",
        evaluation_criteria_summary="70% technical compliance & lead time, 30% pricing.",
        status="DRAFT",
    )
    assert rfq.title.startswith("RFQ for High-Precision")
    assert len(rfq.target_supplier_ids) == 2
    assert rfq.status == "DRAFT"


def test_rfq_draft_invalid_status_rejected():
    """Verify invalid RFQ status values are rejected."""
    deadline = datetime.now(timezone.utc) + timedelta(days=7)
    with pytest.raises(DataValidationError) as exc:
        RFQDraft(
            brief_id="BRIEF-1234",
            target_supplier_ids=["SUP-001"],
            title="RFQ Bracket Machining",
            scope_of_work="Manufacture of parts.",
            technical_requirements=["Spec A"],
            submission_deadline=deadline,
            response_instructions="Submit details.",
            evaluation_criteria_summary="Criteria A.",
            status="INVALID_STATUS_XYZ",
        )
    assert "Invalid RFQ status" in str(exc.value)
