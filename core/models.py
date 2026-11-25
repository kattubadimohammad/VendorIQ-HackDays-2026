"""
VendorIQ Core Domain Models.

Defines strongly-typed, validated Pydantic models for sourcing briefs,
supplier profiles, provenanced data fields, multi-dimensional scoring, and RFQ drafts.
Enforces company-level privacy, provenance integrity, and neutral missing-data handling.
"""

from datetime import datetime, timezone, date
from enum import Enum
import math
from typing import Optional, List, Dict, Any, Tuple
import uuid

from pydantic import BaseModel, Field, field_validator, model_validator

from core.provenance import (
    ProvenanceClassification,
    ProvenanceRecord,
    create_unverified_provenance,
    create_verified_provenance,
    create_ai_assessment_provenance,
    create_synthetic_provenance,
    create_buyer_specified_provenance,
)
from core.exceptions import DataValidationError, ProvenanceIntegrityError


# ---------------------------------------------------------------------------
# Scoring Dimensions and Configurable Weights
# ---------------------------------------------------------------------------

class ScoringDimension(str, Enum):
    """The five agreed scoring dimensions for VendorIQ supplier evaluation."""
    INDUSTRY_RELEVANCE = "industry_relevance"
    GEOGRAPHIC_FIT = "geographic_fit"
    COMPANY_SIZE_FIT = "company_size_fit"
    BUSINESS_SIGNALS = "business_signals"
    DATA_COMPLETENESS = "data_completeness"


class ScoringWeights(BaseModel):
    """
    Configurable weights for the five scoring dimensions.
    Default distribution: 35% / 25% / 20% / 10% / 10%.
    All weights must be non-negative and sum to 1.0.
    """
    industry_relevance: float = Field(default=0.35, ge=0.0, le=1.0, description="Industry relevance weight (default: 35%)")
    geographic_fit: float = Field(default=0.25, ge=0.0, le=1.0, description="Geographic fit weight (default: 25%)")
    company_size_fit: float = Field(default=0.20, ge=0.0, le=1.0, description="Company size fit weight (default: 20%)")
    business_signals: float = Field(default=0.10, ge=0.0, le=1.0, description="Business signals weight (default: 10%)")
    data_completeness: float = Field(default=0.10, ge=0.0, le=1.0, description="Data completeness weight (default: 10%)")

    @model_validator(mode="after")
    def validate_weights_total(self) -> "ScoringWeights":
        total = (
            self.industry_relevance
            + self.geographic_fit
            + self.company_size_fit
            + self.business_signals
            + self.data_completeness
        )
        if not math.isclose(total, 1.0, rel_tol=1e-4, abs_tol=1e-4):
            raise DataValidationError(
                f"Scoring weights must sum to 1.0 (currently sums to {total:.4f})."
            )
        return self

    def as_dict(self) -> Dict[ScoringDimension, float]:
        """Return mapping of dimension enum to its configured weight."""
        return {
            ScoringDimension.INDUSTRY_RELEVANCE: self.industry_relevance,
            ScoringDimension.GEOGRAPHIC_FIT: self.geographic_fit,
            ScoringDimension.COMPANY_SIZE_FIT: self.company_size_fit,
            ScoringDimension.BUSINESS_SIGNALS: self.business_signals,
            ScoringDimension.DATA_COMPLETENESS: self.data_completeness,
        }


# ---------------------------------------------------------------------------
# Provenanced Attribute Field
# ---------------------------------------------------------------------------

class DataField(BaseModel):
    """
    Represents a specific company attribute with associated provenance and supporting evidence.
    """
    name: str = Field(..., min_length=1, description="Attribute name")
    value: Any = Field(default=None, description="Attribute value")
    provenance: ProvenanceRecord = Field(..., description="Audit record for this value")
    is_missing: bool = Field(default=False, description="Whether this field is missing or empty")

    @model_validator(mode="after")
    def check_missing_status(self) -> "DataField":
        if self.value is None or self.value == "":
            self.is_missing = True
        return self

    @classmethod
    def verified(cls, name: str, value: Any, source: str, evidence: str, confidence: float = 1.0) -> "DataField":
        """Factory for verified company attributes."""
        prov = create_verified_provenance(source=source, evidence=evidence, confidence=confidence)
        return cls(name=name, value=value, provenance=prov, is_missing=False)

    @classmethod
    def buyer_specified(cls, name: str, value: Any, source: str = "Buyer Input", notes: Optional[str] = None) -> "DataField":
        """Factory for buyer-specified criteria or attributes."""
        prov = create_buyer_specified_provenance(source=source, notes=notes)
        return cls(name=name, value=value, provenance=prov, is_missing=False)

    @classmethod
    def ai_assessment(cls, name: str, value: Any, source: str = "Gemini AI Assessment", confidence: Optional[float] = None) -> "DataField":
        """Factory for AI inferred or assessed attributes."""
        prov = create_ai_assessment_provenance(source=source, confidence=confidence)
        return cls(name=name, value=value, provenance=prov, is_missing=False)

    @classmethod
    def synthetic(cls, name: str, value: Any, source: str = "Synthetic Seed Dataset") -> "DataField":
        """Factory for synthetic test attributes."""
        prov = create_synthetic_provenance(source=source)
        return cls(name=name, value=value, provenance=prov, is_missing=False)

    @classmethod
    def unverified(cls, name: str, value: Any, source: str = "Unverified Web Scrape") -> "DataField":
        """Factory for unverified attributes."""
        prov = create_unverified_provenance(source=source)
        return cls(name=name, value=value, provenance=prov, is_missing=False)

    @classmethod
    def missing(cls, name: str, notes: Optional[str] = "Data not provided") -> "DataField":
        """Factory for explicitly missing attributes."""
        prov = create_unverified_provenance(source="Missing Data", notes=notes)
        return cls(name=name, value=None, provenance=prov, is_missing=True)


# ---------------------------------------------------------------------------
# Certifications (Verified vs Unverified Claims)
# ---------------------------------------------------------------------------

class Certification(BaseModel):
    """
    Supplier industry certificate or standard (e.g. ISO 9001, SOC 2 Type II, AS9100).
    Distinguishes verified certifications with audit proof from unverified claims.
    """
    name: str = Field(..., min_length=1, description="Standard or certification title")
    issuing_body: Optional[str] = Field(default=None, description="Auditing or issuing registry body")
    is_verified: bool = Field(default=False, description="True only if supported by authoritative verification proof")
    verification_evidence: Optional[str] = Field(default=None, description="Certificate ID, registry link, or audit record")
    provenance: ProvenanceRecord

    @model_validator(mode="after")
    def validate_certification_integrity(self) -> "Certification":
        """
        Enforce strict separation between verified certifications and unverified claims:
        - If is_verified is True, it MUST have non-empty verification_evidence and VERIFIED_DATA provenance.
        - Unverified claims CANNOT be assigned VERIFIED_DATA provenance.
        """
        if self.is_verified:
            if not self.verification_evidence or not self.verification_evidence.strip():
                raise DataValidationError(
                    f"Certification '{self.name}' is marked as verified but lacks supporting verification_evidence."
                )
            if self.provenance.classification != ProvenanceClassification.VERIFIED_DATA:
                raise ProvenanceIntegrityError(
                    f"Certification '{self.name}' is verified but has incorrect provenance classification: "
                    f"'{self.provenance.classification.value}'. Must be '{ProvenanceClassification.VERIFIED_DATA.value}'."
                )
        else:
            if self.provenance.classification == ProvenanceClassification.VERIFIED_DATA:
                raise ProvenanceIntegrityError(
                    f"Certification '{self.name}' is unverified, so it cannot be classified as "
                    f"'{ProvenanceClassification.VERIFIED_DATA.value}'."
                )
        return self


# ---------------------------------------------------------------------------
# Sourcing Criteria and Sourcing Brief
# ---------------------------------------------------------------------------

class SourcingCriteria(BaseModel):
    """
    Search and evaluation parameters specified by a buyer.
    """
    target_industries: List[str] = Field(..., min_length=1, description="Target industry vertical(s)")
    target_geographies: List[str] = Field(..., min_length=1, description="Target operating regions or countries")
    min_company_size: Optional[int] = Field(default=None, ge=1, description="Minimum headcount")
    max_company_size: Optional[int] = Field(default=None, ge=1, description="Maximum headcount")
    required_certifications: List[str] = Field(default_factory=list, description="Mandatory certifications required")
    min_annual_revenue_usd: Optional[float] = Field(default=None, ge=0.0)
    max_annual_revenue_usd: Optional[float] = Field(default=None, ge=0.0)
    weights: ScoringWeights = Field(default_factory=ScoringWeights, description="Customizable evaluation weights")

    @model_validator(mode="after")
    def validate_criteria_ranges(self) -> "SourcingCriteria":
        if self.min_company_size is not None and self.max_company_size is not None:
            if self.min_company_size > self.max_company_size:
                raise DataValidationError(
                    f"min_company_size ({self.min_company_size}) cannot exceed max_company_size ({self.max_company_size})."
                )
        if self.min_annual_revenue_usd is not None and self.max_annual_revenue_usd is not None:
            if self.min_annual_revenue_usd > self.max_annual_revenue_usd:
                raise DataValidationError(
                    f"min_annual_revenue_usd ({self.min_annual_revenue_usd}) cannot exceed max_annual_revenue_usd ({self.max_annual_revenue_usd})."
                )
        return self


class SourcingBrief(BaseModel):
    """
    Buyer sourcing project brief containing criteria, scope, and target milestones.
    """
    brief_id: str = Field(default_factory=lambda: str(uuid.uuid4()), description="Unique brief identifier")
    title: str = Field(..., min_length=3, max_length=200, description="Project title")
    buyer_organization: str = Field(..., min_length=1, description="Corporate entity commissioning the brief")
    description: str = Field(..., min_length=10, description="Detailed problem statement and sourcing requirements")
    criteria: SourcingCriteria = Field(..., description="Structured evaluation criteria")
    budget_range_usd: Optional[Tuple[float, float]] = Field(default=None, description="Optional min/max budget in USD")
    timeline_weeks: Optional[int] = Field(default=None, ge=1, description="Expected delivery or procurement timeline")
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("budget_range_usd")
    @classmethod
    def validate_budget_range(cls, v: Optional[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
        if v is not None:
            min_b, max_b = v
            if min_b < 0.0 or max_b < 0.0:
                raise DataValidationError("Budget values must be non-negative.")
            if min_b > max_b:
                raise DataValidationError(f"Minimum budget (${min_b:,.2f}) exceeds maximum budget (${max_b:,.2f}).")
        return v


# ---------------------------------------------------------------------------
# Supplier Profile (Company-Level Only; No Personal PII)
# ---------------------------------------------------------------------------

class SupplierProfile(BaseModel):
    """
    Company-level profile for an evaluated vendor.
    Contains strict company-level attributes only; prohibits individual PII.
    """
    supplier_id: str = Field(..., min_length=1, description="Unique supplier identifier (e.g. CRUST_123 or VEND_456)")
    company_name: str = Field(..., min_length=1, description="Legal or registered business name")
    domain: str = Field(..., min_length=3, description="Company web domain (e.g. acme-mfg.com)")
    description: Optional[str] = Field(default=None, description="Company overview / business focus")
    headquarters_country: str = Field(..., min_length=2, description="Country of corporate headquarters")
    headquarters_city: Optional[str] = None
    employee_count: Optional[int] = Field(default=None, ge=1, description="Estimated total employee headcount")
    employee_range: Optional[str] = None  # e.g., "50-200"
    industries: List[str] = Field(default_factory=list, description="Industry sectors and NAICS categories")
    certifications: List[Certification] = Field(default_factory=list, description="List of vendor certifications")
    annual_revenue_usd: Optional[float] = Field(default=None, ge=0.0, description="Reported or estimated annual revenue in USD")
    year_founded: Optional[int] = Field(default=None, ge=1800, le=2100)
    business_signals: List[str] = Field(default_factory=list, description="Company-level growth or market signals")
    corporate_contact_email: Optional[str] = Field(default=None, description="Official company inquiry email (e.g. info@domain.com)")
    corporate_website: Optional[str] = None

    # Attribute-level provenance mapping
    attributes: Dict[str, DataField] = Field(default_factory=dict, description="Fine-grained provenanced company attributes")

    # Profile flags and lineage
    is_synthetic: bool = Field(default=False, description="Flag indicating synthetic / simulated record")
    overall_provenance: ProvenanceClassification = Field(default=ProvenanceClassification.UNVERIFIED_OR_MISSING)

    @field_validator("corporate_contact_email")
    @classmethod
    def validate_corporate_email_only(cls, email: Optional[str]) -> Optional[str]:
        """
        Enforce company-level data privacy: Personal email providers (Gmail, Yahoo, etc.)
        are rejected to ensure no individual PII is stored.
        """
        if email:
            normalized = email.strip().lower()
            disallowed_domains = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com", "protonmail.com"}
            domain_part = normalized.split("@")[-1] if "@" in normalized else ""
            if domain_part in disallowed_domains:
                raise DataValidationError(
                    f"Email '{email}' uses a personal email domain. "
                    f"VendorIQ strictly enforces company-level data; personal email addresses are not permitted."
                )
        return email

    @model_validator(mode="after")
    def validate_profile_provenance_integrity(self) -> "SupplierProfile":
        """
        Enforce provenance rules on the entire profile:
        - Synthetic profiles cannot claim VERIFIED_DATA.
        - Synthetic profiles cannot possess verified certifications.
        """
        if self.is_synthetic:
            if self.overall_provenance == ProvenanceClassification.VERIFIED_DATA:
                raise ProvenanceIntegrityError(
                    "Synthetic supplier profile cannot have VERIFIED_DATA overall provenance."
                )
            self.overall_provenance = ProvenanceClassification.SYNTHETIC_DATA

            for cert in self.certifications:
                if cert.is_verified or cert.provenance.classification == ProvenanceClassification.VERIFIED_DATA:
                    raise ProvenanceIntegrityError(
                        f"Synthetic supplier '{self.company_name}' cannot possess verified certification '{cert.name}'."
                    )
        return self

    @property
    def verified_certifications(self) -> List[Certification]:
        """List of certifications that have been independently verified."""
        return [c for c in self.certifications if c.is_verified]

    @property
    def unverified_certifications(self) -> List[Certification]:
        """List of unverified self-reported certification claims."""
        return [c for c in self.certifications if not c.is_verified]

    def completeness_percentage(self) -> float:
        """
        Calculate the percentage of key company-level fields that are populated.
        Returns a float between 0.0 and 100.0.
        """
        key_fields = [
            self.company_name,
            self.domain,
            self.description,
            self.headquarters_country,
            self.employee_count or self.employee_range,
            self.industries,
            self.annual_revenue_usd,
            self.year_founded,
            self.business_signals,
            self.certifications,
        ]
        populated = sum(1 for item in key_fields if item)
        return round((populated / len(key_fields)) * 100.0, 2)


# ---------------------------------------------------------------------------
# Factor Scores and Supplier Scoring (0 to 100 Range)
# ---------------------------------------------------------------------------

class FactorScore(BaseModel):
    """
    Score for an individual dimension in the evaluation.
    Enforces 0.0 to 100.0 range and fair treatment of missing data.
    """
    dimension: ScoringDimension
    score: float = Field(..., ge=0.0, le=100.0, description="Normalized score between 0.0 and 100.0")
    weight: float = Field(..., ge=0.0, le=1.0, description="Dimension weight between 0.0 and 1.0")
    weighted_score: float = Field(default=0.0, ge=0.0, le=100.0, description="Computed score * weight")
    reasoning: str = Field(..., min_length=1, description="Audit rationale explaining the score")
    evidence_sources: List[str] = Field(default_factory=list, description="Data points or sources referenced")
    is_missing_data: bool = Field(default=False, description="Flag indicating if the attribute was missing")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Confidence in this assessment (0.0 to 1.0)")
    provenance: ProvenanceClassification = Field(default=ProvenanceClassification.AI_ASSESSMENT)

    @model_validator(mode="after")
    def calculate_weighted_score(self) -> "FactorScore":
        self.weighted_score = round(self.score * self.weight, 4)
        return self

    @classmethod
    def missing_data_neutral(
        cls,
        dimension: ScoringDimension,
        weight: float,
        reasoning: Optional[str] = None,
        baseline_score: float = 50.0,
    ) -> "FactorScore":
        """
        Factory creating a neutral assessment when supplier data is missing.
        Prevents treating missing information as proof of poor supplier quality.
        The data completeness dimension independently penalizes overall certainty.
        """
        default_reasoning = (
            f"Neutral baseline score ({baseline_score:.0f}/100) applied for {dimension.value}: "
            f"Information is not yet available in registry records; missing data is not penalized as poor quality."
        )
        return cls(
            dimension=dimension,
            score=baseline_score,
            weight=weight,
            reasoning=reasoning or default_reasoning,
            evidence_sources=["Missing Data Policy: Neutral Baseline"],
            is_missing_data=True,
            confidence=0.3,
            provenance=ProvenanceClassification.UNVERIFIED_OR_MISSING,
        )


class SupplierScore(BaseModel):
    """
    Overall evaluated supplier scorecard across all five dimensions.
    """
    supplier_id: str = Field(..., min_length=1)
    company_name: str = Field(..., min_length=1)
    total_score: float = Field(..., ge=0.0, le=100.0, description="Overall composite score (0.0 to 100.0)")
    factor_scores: Dict[ScoringDimension, FactorScore] = Field(..., description="Breakdown across all 5 dimensions")
    weights_used: ScoringWeights = Field(default_factory=ScoringWeights, description="Weights applied during evaluation")
    data_completeness_percentage: float = Field(..., ge=0.0, le=100.0, description="Completeness of supplier profile")
    summary: str = Field(..., min_length=5, description="Executive scoring summary")
    provenance: ProvenanceClassification = Field(default=ProvenanceClassification.AI_ASSESSMENT)

    @model_validator(mode="after")
    def validate_score_integrity(self) -> "SupplierScore":
        """
        Validate that:
        1. All five scoring dimensions are evaluated.
        2. total_score matches the sum of the factor weighted scores.
        """
        for dim in ScoringDimension:
            if dim not in self.factor_scores:
                raise DataValidationError(f"Missing evaluation factor for required dimension: {dim.value}")

        calculated_total = sum(f.weighted_score for f in self.factor_scores.values())
        if not math.isclose(self.total_score, calculated_total, rel_tol=1e-2, abs_tol=0.25):
            raise DataValidationError(
                f"Composite total_score ({self.total_score}) must equal sum of weighted factor scores ({calculated_total:.2f})."
            )
        return self

    @classmethod
    def compute(
        cls,
        supplier_id: str,
        company_name: str,
        factor_scores: Dict[ScoringDimension, FactorScore],
        data_completeness_percentage: float,
        summary: str,
        weights_used: Optional[ScoringWeights] = None,
        provenance: ProvenanceClassification = ProvenanceClassification.AI_ASSESSMENT,
    ) -> "SupplierScore":
        """Convenience constructor that automatically calculates total_score from factors."""
        weights = weights_used or ScoringWeights()
        total = round(sum(f.weighted_score for f in factor_scores.values()), 2)
        # Clamp to [0, 100] just in case of float rounding
        total = max(0.0, min(100.0, total))
        return cls(
            supplier_id=supplier_id,
            company_name=company_name,
            total_score=total,
            factor_scores=factor_scores,
            weights_used=weights,
            data_completeness_percentage=data_completeness_percentage,
            summary=summary,
            provenance=provenance,
        )


# ---------------------------------------------------------------------------
# RFQ Draft Model
# ---------------------------------------------------------------------------

class RFQDraft(BaseModel):
    """
    Request for Quotation (RFQ) draft prepared for shortlisted suppliers.
    """
    rfq_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    brief_id: str = Field(..., min_length=1, description="Associated SourcingBrief identifier")
    target_supplier_ids: List[str] = Field(..., min_length=1, description="List of shortlisted supplier IDs")
    title: str = Field(..., min_length=3, max_length=200, description="RFQ title")
    scope_of_work: str = Field(..., min_length=10, description="Detailed statement of required products/services")
    technical_requirements: List[str] = Field(..., min_length=1, description="List of technical specifications")
    submission_deadline: datetime = Field(..., description="Deadline for quote submission")
    response_instructions: str = Field(..., min_length=10, description="Submission format and contact instructions")
    evaluation_criteria_summary: str = Field(..., min_length=10, description="Summary of how proposals will be evaluated")
    status: str = Field(default="DRAFT", description="DRAFT, READY_FOR_REVIEW, SENT, or ARCHIVED")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("status")
    @classmethod
    def validate_rfq_status(cls, val: str) -> str:
        allowed = {"DRAFT", "READY_FOR_REVIEW", "SENT", "ARCHIVED"}
        normalized = val.strip().upper()
        if normalized not in allowed:
            raise DataValidationError(f"Invalid RFQ status '{val}'. Must be one of {allowed}.")
        return normalized


# ---------------------------------------------------------------------------
# Phase 8A: AI Sourcing Copilot Models
# ---------------------------------------------------------------------------

class ExtractedSourcingRequirements(BaseModel):
    """
    Structured sourcing requirements extracted from natural language buyer input.
    """
    title: str = Field(..., min_length=3, max_length=200, description="Inferred project brief title")
    category: str = Field(..., min_length=2, description="Target product category or industry sector")
    target_industries: List[str] = Field(default_factory=lambda: ["Packaging and Containers Manufacturing"])
    target_geographies: List[str] = Field(default_factory=lambda: ["India"])
    min_company_size: Optional[int] = Field(default=None, ge=1)
    max_company_size: Optional[int] = Field(default=None, ge=1)
    required_specifications: List[str] = Field(default_factory=list, description="Key technical specifications requested")
    required_certifications: List[str] = Field(default_factory=list, description="Explicit certifications mentioned (ISO, BRC, etc.)")
    budget_range_usd: Optional[Tuple[float, float]] = None
    timeline_weeks: Optional[int] = Field(default=None, ge=1)
    summary: str = Field(..., min_length=10, description="Executive procurement scope summary")

    def to_sourcing_brief(
        self,
        brief_id: Optional[str] = None,
        buyer_organization: str = "VendorIQ Procurement Team",
        custom_weights: Optional[ScoringWeights] = None,
    ) -> SourcingBrief:
        """Convert extracted requirements to a formal SourcingBrief."""
        b_id = brief_id or f"BRIEF-{uuid.uuid4().hex[:8].upper()}"
        weights = custom_weights or ScoringWeights()
        criteria = SourcingCriteria(
            target_industries=self.target_industries if self.target_industries else [self.category],
            target_geographies=self.target_geographies if self.target_geographies else ["India"],
            min_company_size=self.min_company_size,
            max_company_size=self.max_company_size,
            required_certifications=self.required_certifications,
            weights=weights,
        )
        return SourcingBrief(
            brief_id=b_id,
            title=self.title,
            buyer_organization=buyer_organization,
            description=self.summary,
            criteria=criteria,
            budget_range_usd=self.budget_range_usd,
            timeline_weeks=self.timeline_weeks,
        )


class CopilotRecommendation(BaseModel):
    """
    AI Sourcing Copilot candidate recommendation grounded strictly in SQLite records.
    Never invents facts or alters deterministic evaluation scores.
    """
    supplier_id: str = Field(..., min_length=1)
    company_name: str = Field(..., min_length=1)
    domain: str = Field(..., min_length=3)
    rank: int = Field(..., ge=1)
    total_score: float = Field(..., ge=0.0, le=100.0)
    fit_rationale: str = Field(..., min_length=10, description="AI-synthesized fit rationale grounded in scorecards")
    matched_capabilities: List[str] = Field(default_factory=list)
    missing_evidence_flags: List[str] = Field(default_factory=list)
    provenance_breakdown: Dict[str, str] = Field(default_factory=dict)


class CopilotQueryResponse(BaseModel):
    """
    Structured response returned by AI Sourcing Copilot for interactive procurement queries.
    """
    assistant_message: str = Field(..., min_length=1)
    extracted_requirements: Optional[ExtractedSourcingRequirements] = None
    recommended_suppliers: List[CopilotRecommendation] = Field(default_factory=list)
    provenance: ProvenanceClassification = Field(default=ProvenanceClassification.AI_ASSESSMENT)
    evidence_sources: List[str] = Field(default_factory=list)
    grounded_record_count: int = Field(default=0, ge=0)
    model_used: str = Field(default="gemini-2.5-flash (Grounded)")


# ---------------------------------------------------------------------------
# Phase 9: Supplier Intelligence & Evidence Verification Models
# ---------------------------------------------------------------------------

class EvidenceCategory(str, Enum):
    """The 7 official evidence categories for supplier intelligence and audit verification."""
    COMPANY_WEBSITE_DOMAIN = "Company website and domain"
    MANUFACTURING_CAPABILITIES = "Manufacturing capabilities"
    FLEXIBLE_PACKAGING_EXPERIENCE = "Flexible packaging experience"
    BARRIER_FILM_EXPERIENCE = "Barrier film experience"
    SUSTAINABILITY_CERTIFICATIONS = "Sustainability certifications"
    HEADQUARTERS_LOCATIONS = "Company headquarters and operating locations"
    COMPANY_SIZE = "Company size"


class EvidenceStatus(str, Enum):
    """Verification status of a supplier evidence claim."""
    VERIFIED = "VERIFIED"
    SELF_REPORTED = "SELF_REPORTED"
    UNVERIFIED = "UNVERIFIED"
    MISSING = "MISSING"


class SupplierEvidenceRecord(BaseModel):
    """
    Structured supplier evidence record tracking claim verification, source lineage,
    and reviewer audit notes.
    Enforces Truth-in-Evidence integrity rules.
    """
    evidence_id: str = Field(default_factory=lambda: f"EVID-{uuid.uuid4().hex[:8].upper()}", description="Unique evidence identifier")
    supplier_id: str = Field(..., min_length=1, description="Associated supplier identifier")
    category: EvidenceCategory = Field(..., description="Evidence category")
    claim: str = Field(..., min_length=1, description="Claim or information being evaluated")
    status: EvidenceStatus = Field(..., description="Verification status: VERIFIED, SELF_REPORTED, UNVERIFIED, or MISSING")
    source_name: str = Field(..., min_length=1, description="Source or registry entity providing the data")
    source_url: Optional[str] = Field(default=None, description="Direct URL, audit link, or registry reference")
    verified_at: Optional[datetime] = Field(default=None, description="Timestamp when verification occurred")
    reviewer_notes: Optional[str] = Field(default=None, description="Auditor assessment or verification notes")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_truth_in_evidence_guardrails(self) -> "SupplierEvidenceRecord":
        """
        Enforce strict Truth-in-Evidence integrity rules:
        1. A claim can NEVER be marked VERIFIED without supporting documentary evidence
           and a non-empty, authoritative verification source.
        2. AI, LLM, synthetic, or basic unverified web directory scrapes cannot claim VERIFIED status.
        3. If status is VERIFIED, source_name must not indicate AI/synthetic origins,
           and either source_url or explicit documentary proof in reviewer_notes must be present,
           and verified_at must be provided.
        4. If status is MISSING, the claim should reflect missing or unconfirmed data.
        """
        from core.provenance import is_ai_or_synthetic_source

        if self.status == EvidenceStatus.VERIFIED:
            if not self.source_name or not self.source_name.strip():
                raise DataValidationError("Verified evidence requires an explicit source name.")
            if is_ai_or_synthetic_source(self.source_name):
                raise ProvenanceIntegrityError(
                    f"Truth-in-Evidence Violation: Source '{self.source_name}' indicates AI or synthetic origin, "
                    f"which can NEVER be marked as VERIFIED."
                )
            if not (self.source_url and self.source_url.strip()) and not (self.reviewer_notes and self.reviewer_notes.strip()):
                raise DataValidationError(
                    "Verified evidence requires either a direct source_url or explicit documentary proof in reviewer_notes."
                )
            if self.verified_at is None:
                raise DataValidationError(
                    "Verified evidence requires an explicit verification date (verified_at)."
                )

        return self

    @property
    def is_verified(self) -> bool:
        return self.status == EvidenceStatus.VERIFIED

    @property
    def is_self_reported(self) -> bool:
        return self.status == EvidenceStatus.SELF_REPORTED

    @property
    def is_unverified(self) -> bool:
        return self.status == EvidenceStatus.UNVERIFIED

    @property
    def is_missing(self) -> bool:
        return self.status == EvidenceStatus.MISSING


# ---------------------------------------------------------------------------
# Phase 10: Intelligent Supplier Discovery & Shortlisting Models
# ---------------------------------------------------------------------------

class ShortlistStatus(str, Enum):
    """Lifecycle status for a candidate supplier in a buyer's shortlist."""
    CANDIDATE = "CANDIDATE"
    UNDER_REVIEW = "UNDER_REVIEW"
    SHORTLISTED = "SHORTLISTED"
    REJECTED = "REJECTED"


class ShortlistItem(BaseModel):
    """An individual supplier entry within a buyer's shortlist."""
    item_id: str = Field(default_factory=lambda: f"ITEM-{uuid.uuid4().hex[:8].upper()}", description="Unique item identifier")
    shortlist_id: str = Field(..., min_length=1, description="Associated shortlist ID")
    supplier_id: str = Field(..., min_length=1, description="Associated supplier ID")
    company_name: Optional[str] = Field(default=None, description="Company name snapshot")
    domain: Optional[str] = Field(default=None, description="Company domain snapshot")
    total_score: Optional[float] = Field(default=None, ge=0.0, le=100.0, description="Evaluation score snapshot")
    status: ShortlistStatus = Field(default=ShortlistStatus.CANDIDATE, description="Shortlist candidate status")
    reviewer_notes: Optional[str] = Field(default=None, description="Auditor/buyer qualitative comments")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SupplierShortlist(BaseModel):
    """
    Named collection of shortlisted candidate suppliers curated by a procurement team.
    Supports persistent status tracking, notes, and CSV export.
    """
    shortlist_id: str = Field(default_factory=lambda: f"SLIST-{uuid.uuid4().hex[:8].upper()}", description="Unique shortlist ID")
    brief_id: str = Field(..., min_length=1, description="Associated sourcing brief ID")
    name: str = Field(..., min_length=2, max_length=150, description="Shortlist name (e.g. Primary Packaging Candidates)")
    description: Optional[str] = Field(default=None, description="Shortlist scope or purpose notes")
    items: List[ShortlistItem] = Field(default_factory=list, description="List of shortlisted suppliers")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def item_count(self) -> int:
        return len(self.items)

    def get_item(self, supplier_id: str) -> Optional[ShortlistItem]:
        for it in self.items:
            if it.supplier_id == supplier_id:
                return it
        return None

    def to_csv(self) -> str:
        """Export the shortlist to a standard RFC 4180 CSV string."""
        import io
        import csv

        output = io.StringIO()
        writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)
        writer.writerow([
            "Shortlist ID",
            "Shortlist Name",
            "Brief ID",
            "Item ID",
            "Supplier ID",
            "Company Name",
            "Domain",
            "Status",
            "Score",
            "Reviewer Notes",
            "Added At",
            "Updated At",
        ])
        for it in self.items:
            writer.writerow([
                self.shortlist_id,
                self.name,
                self.brief_id,
                it.item_id,
                it.supplier_id,
                it.company_name or "",
                it.domain or "",
                it.status.value if hasattr(it.status, "value") else str(it.status),
                f"{it.total_score:.1f}" if it.total_score is not None else "N/A",
                it.reviewer_notes or "",
                it.created_at.isoformat(),
                it.updated_at.isoformat(),
            ])
        return output.getvalue()


class DiscoveryFilterParams(BaseModel):
    """
    Structured search and filter parameters for intelligent supplier discovery.
    Distinguishes hard requirements from soft preferences.
    """
    query: Optional[str] = Field(default=None, description="Natural language search terms")
    brief_id: Optional[str] = Field(default=None, description="Active sourcing brief ID")
    industries: List[str] = Field(default_factory=list, description="Target industry vertical filters")
    geographies: List[str] = Field(default_factory=list, description="Target country filters")
    min_company_size: Optional[int] = Field(default=None, ge=1, description="Minimum headcount bound")
    max_company_size: Optional[int] = Field(default=None, ge=1, description="Maximum headcount bound")
    employee_ranges: List[str] = Field(default_factory=list, description="Allowed employee range brackets")

    # Hard requirements (must be met or flagged as failure)
    require_manufacturing: bool = Field(default=False, description="Hard requirement: Must have manufacturing capability")
    require_flexible_packaging: bool = Field(default=False, description="Hard requirement: Must have flexible packaging experience")
    require_barrier_films: bool = Field(default=False, description="Hard requirement: Must have barrier film experience")
    require_sustainability_evidence: bool = Field(default=False, description="Hard requirement: Must have sustainability evidence or cert")
    exclude_domain_mismatches: bool = Field(default=False, description="Hard requirement: Exclude suspicious domain mismatches")

    # Soft preferences (affect ranking/tie-breaking, not inclusion)
    prefer_barrier_films: bool = Field(default=False, description="Soft preference for barrier film experience")
    prefer_sustainability: bool = Field(default=False, description="Soft preference for sustainability evidence")


class DiscoveryRecommendation(BaseModel):
    """
    Evidence-grounded supplier recommendation candidate.
    Provides complete provenance, factor scores, risk explanations, and hard-filter flags.
    """
    supplier_id: str = Field(..., min_length=1)
    company_name: str = Field(..., min_length=1)
    domain: str = Field(..., min_length=3)
    headquarters_country: str = Field(..., min_length=2)
    headquarters_city: Optional[str] = None
    employee_range: Optional[str] = None
    total_score: float = Field(..., ge=0.0, le=100.0)
    rank: int = Field(..., ge=1)
    match_explanation: str = Field(..., min_length=10, description="Deterministic match explanation")
    available_evidence: List[str] = Field(default_factory=list)
    missing_evidence: List[str] = Field(default_factory=list)
    unverified_claims: List[str] = Field(default_factory=list)
    domain_mismatch_warning: Optional[str] = None
    procurement_risks: List[str] = Field(default_factory=list)
    evidence_completeness_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    is_hard_match: bool = Field(default=True, description="Whether all specified hard filter constraints are satisfied")
    hard_filter_failures: List[str] = Field(default_factory=list, description="List of violated hard constraints")
    factor_scores: Dict[str, float] = Field(default_factory=dict, description="Numerical scores per dimension")


# ===========================================================================
# Phase 11: Professional RFQ Generation & Human Approval Workflow Models
# ===========================================================================

class RFQStatus(str, Enum):
    """
    Seven lifecycle statuses for professional RFQ workflow.
    Requires mandatory human gate before approval or transmission.
    """
    DRAFT = "DRAFT"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SENT = "SENT"
    RESPONSE_RECEIVED = "RESPONSE_RECEIVED"
    CLOSED = "CLOSED"


VALID_RFQ_TRANSITIONS: Dict[RFQStatus, List[RFQStatus]] = {
    RFQStatus.DRAFT: [RFQStatus.PENDING_APPROVAL],
    RFQStatus.PENDING_APPROVAL: [RFQStatus.APPROVED, RFQStatus.REJECTED],
    RFQStatus.REJECTED: [RFQStatus.DRAFT, RFQStatus.CLOSED],
    RFQStatus.APPROVED: [RFQStatus.SENT, RFQStatus.CLOSED],
    RFQStatus.SENT: [RFQStatus.RESPONSE_RECEIVED, RFQStatus.CLOSED],
    RFQStatus.RESPONSE_RECEIVED: [RFQStatus.CLOSED],
    RFQStatus.CLOSED: [],
}


class RFQAuditEntry(BaseModel):
    """
    Audit log entry recording an RFQ lifecycle status transition.
    Captures actor, timestamps, and reviewer notes.
    """
    audit_id: str = Field(default_factory=lambda: f"AUDIT-{uuid.uuid4().hex[:8].upper()}")
    rfq_id: str = Field(..., min_length=1)
    from_status: Optional[str] = None
    to_status: str = Field(..., min_length=1)
    changed_by: str = Field(..., min_length=1)
    comments: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RFQBuyerInfo(BaseModel):
    """
    Structured buyer organization information for official RFQ package.
    """
    buyer_organization: str = Field(..., min_length=2, description="Legal buyer entity")
    rfq_reference: str = Field(..., min_length=3, description="Official procurement reference code")
    issue_date: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    response_deadline: datetime = Field(..., description="Date and time quotations must be submitted by")
    contact_email: Optional[str] = Field(default=None, description="Official response inbox")
    contact_person: Optional[str] = Field(default=None, description="Procurement lead")


class RFQProductRequirements(BaseModel):
    """
    Detailed technical product requirements specified by the buyer.
    """
    category: str = Field(..., min_length=2)
    description: str = Field(..., min_length=5)
    technical_specifications: List[str] = Field(default_factory=list)
    barrier_film_requirements: Optional[str] = None
    expected_quantity: str = Field(..., min_length=1, description="Target volume (e.g. 50,000 units)")
    packaging_requirements: Optional[str] = None


class RFQCommercialRequirements(BaseModel):
    """
    Commercial parameters and terms requested from the supplier.
    """
    currency: str = Field(default="USD")
    target_unit_price: Optional[float] = Field(default=None, ge=0.0)
    min_order_quantity: Optional[int] = Field(default=None, ge=1)
    production_lead_time_weeks: Optional[int] = Field(default=None, ge=1)
    payment_terms: str = Field(default="Net 30 days upon delivery and inspection")
    incoterms: str = Field(default="FOB Port of Origin / CIF Destination")
    delivery_location: str = Field(default="Standard fulfilment hub")
    quotation_validity_days: int = Field(default=60, ge=1)


class RFQSupplierTarget(BaseModel):
    """
    Supplier-specific details and customized requests.
    Guarantees zero exposure of internal scores or competitor details.
    """
    supplier_id: str = Field(..., min_length=1)
    company_name: str = Field(..., min_length=1)
    domain: str = Field(..., min_length=3)
    headquarters: Optional[str] = None
    domain_mismatch_warning: Optional[str] = None
    missing_evidence_requests: List[str] = Field(default_factory=list)
    technical_questionnaire: List[str] = Field(default_factory=list)


class RFQDocumentRecord(BaseModel):
    """
    Complete supplier-specific RFQ document instance with lifecycle tracking.
    """
    rfq_id: str = Field(default_factory=lambda: f"RFQ-{uuid.uuid4().hex[:8].upper()}")
    brief_id: str = Field(..., min_length=1)
    shortlist_id: Optional[str] = None
    supplier_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=3)
    buyer_info: RFQBuyerInfo
    supplier: RFQSupplierTarget
    product_requirements: RFQProductRequirements
    commercial_requirements: RFQCommercialRequirements
    requested_documents: List[str] = Field(default_factory=list)
    status: RFQStatus = Field(default=RFQStatus.DRAFT)
    docx_path: Optional[str] = None
    pdf_path: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    audit_trail: List[RFQAuditEntry] = Field(default_factory=list)

    @field_validator("status", mode="before")
    @classmethod
    def parse_rfq_status(cls, val: Any) -> RFQStatus:
        if isinstance(val, RFQStatus):
            return val
        if isinstance(val, str):
            norm = val.strip().upper()
            try:
                return RFQStatus(norm)
            except ValueError:
                raise DataValidationError(f"Invalid RFQ status '{val}'.")
        raise DataValidationError(f"Invalid RFQ status type: {type(val)}")


# ===========================================================================
# Phase 12: Supplier Quotation Management & Comparison Models
# ===========================================================================

class ExtractionMethod(str, Enum):
    """Method used to extract field data from supplier quotation documents."""
    AUTOMATED_PARSER = "AUTOMATED_PARSER"
    MANUAL_ENTRY = "MANUAL_ENTRY"
    BUYER_VERIFIED = "BUYER_VERIFIED"


class ExtractionFieldStatus(str, Enum):
    """Verification and confirmation state of an individual extracted field."""
    EXTRACTED = "EXTRACTED"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    CONFIRMED = "CONFIRMED"
    CORRECTED = "CORRECTED"
    MISSING = "MISSING"


class FieldExtractionProvenance(BaseModel):
    """
    Provenance lineage metadata for an individual quotation data field.
    Captures source document, page/cell coordinates, extraction method,
    confidence score, and human reviewer confirmation gate.
    """
    field_name: str = Field(..., min_length=1, description="Target commercial or technical field name")
    raw_value: Optional[str] = Field(default=None, description="Exact raw text or number extracted from document")
    source_file: Optional[str] = Field(default=None, description="Filename or path of source quote document")
    page_or_cell_reference: Optional[str] = Field(default=None, description="Page number (PDF) or Cell coordinate (Excel)")
    extraction_method: ExtractionMethod = Field(default=ExtractionMethod.AUTOMATED_PARSER)
    status: ExtractionFieldStatus = Field(default=ExtractionFieldStatus.PENDING_CONFIRMATION)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0, description="Extraction confidence score (0.0 to 1.0)")
    confirmed_by: Optional[str] = Field(default=None, description="Procurement reviewer who confirmed or corrected this field")
    confirmed_at: Optional[datetime] = Field(default=None, description="Timestamp of human confirmation")


class ExchangeRateType(str, Enum):
    """Classification of foreign exchange rate applied for normalization."""
    BENCHMARK = "BENCHMARK"                # Static reference benchmark rate with explicit date
    BUYER_OVERRIDE = "BUYER_OVERRIDE"      # Transparent buyer-provided rate
    FIXED_ESTIMATE = "FIXED_ESTIMATE"      # Internal procurement standard estimate
    UNAVAILABLE = "UNAVAILABLE"            # Rate not available; conversion refused


class ExchangeRateRecord(BaseModel):
    """
    Transparent record of foreign exchange rate used for currency conversion.
    Strictly prohibits representing static or benchmark rates as live market rates.
    """
    base_currency: str = Field(default="USD", min_length=3, max_length=3, description="Base currency (e.g. USD)")
    target_currency: str = Field(..., min_length=3, max_length=3, description="Target currency (e.g. INR, EUR)")
    rate: Optional[float] = Field(default=None, gt=0.0, description="Exchange rate: 1 Base = rate Target")
    source: str = Field(
        default="Static Reference Benchmark (Not Live Market Rate)",
        description="Transparent disclosure of rate provider or reference baseline"
    )
    effective_date: str = Field(default="2026-11-20", description="Date as of which this reference rate applies")
    rate_type: ExchangeRateType = Field(default=ExchangeRateType.BENCHMARK)
    is_outdated: bool = Field(default=False, description="Flagged true if rate exceeds acceptable age limit")
    is_buyer_override: bool = Field(default=False, description="Flagged true if manually specified by buyer")
    notes: Optional[str] = Field(default=None, description="Reviewer or contextual notes regarding rate")


class Incoterm(str, Enum):
    """Standard Incoterms 2020 rules for international commercial transactions."""
    EXW = "EXW"      # Ex Works (Buyer pays all transport)
    FOB = "FOB"      # Free on Board (Supplier covers loading at origin)
    CIF = "CIF"      # Cost, Insurance, and Freight (Supplier covers main carriage & insurance)
    CFR = "CFR"      # Cost and Freight (Supplier covers main carriage without insurance)
    DDP = "DDP"      # Delivered Duty Paid (Supplier covers all freight and import duties)
    OTHER = "OTHER"  # Non-standard or custom commercial delivery terms


class QuotationStatus(str, Enum):
    """
    Six lifecycle statuses for supplier quotation evaluation and award workflow.
    Guarantees immutable quotation history and human gate for award decisions.
    """
    RECEIVED = "RECEIVED"         # Document uploaded or manual draft created
    PARSED = "PARSED"             # Automated data extraction completed; awaiting human confirmation
    UNDER_REVIEW = "UNDER_REVIEW" # Critical fields confirmed; actively compared and evaluated
    ACCEPTED = "ACCEPTED"         # Human accepted candidate for contract award (requires manual PO)
    REJECTED = "REJECTED"         # Quotation rejected on price, technical failure, or commercial terms
    SUPERSEDED = "SUPERSEDED"     # Replaced by a newer version (immutable historical archive)


VALID_QUOTATION_TRANSITIONS: Dict[QuotationStatus, List[QuotationStatus]] = {
    QuotationStatus.RECEIVED: [QuotationStatus.PARSED, QuotationStatus.UNDER_REVIEW, QuotationStatus.REJECTED],
    QuotationStatus.PARSED: [QuotationStatus.UNDER_REVIEW, QuotationStatus.REJECTED],
    QuotationStatus.UNDER_REVIEW: [QuotationStatus.ACCEPTED, QuotationStatus.REJECTED, QuotationStatus.SUPERSEDED],
    QuotationStatus.ACCEPTED: [QuotationStatus.UNDER_REVIEW, QuotationStatus.SUPERSEDED],
    QuotationStatus.REJECTED: [QuotationStatus.UNDER_REVIEW, QuotationStatus.SUPERSEDED],
    QuotationStatus.SUPERSEDED: [],  # Immutable terminal state
}


class QuotationCommercialData(BaseModel):
    """
    Structured commercial parameters and pricing extracted from supplier quotation.
    Strictly separates quoted unit price from estimated landed cost and prevents
    misleading comparisons when cost inputs are incomplete.
    """
    quoted_currency: str = Field(default="USD", min_length=3, max_length=3, description="Currency quoted by supplier")
    quoted_unit_price: float = Field(..., ge=0.0, description="Base unit price in quoted currency")
    quoted_quantity: int = Field(..., ge=1, description="Quantity quoted by supplier")
    min_order_quantity: int = Field(..., ge=1, description="Minimum Order Quantity (MOQ)")
    lead_time_weeks: int = Field(..., ge=1, description="Production and delivery lead time in weeks")
    payment_terms: str = Field(..., min_length=2, description="Payment terms specified by supplier")
    incoterms: Incoterm = Field(default=Incoterm.FOB, description="Incoterms commercial delivery rule")
    delivery_destination: Optional[str] = Field(default=None, description="Agreed delivery location")
    freight_charges: Optional[float] = Field(default=None, ge=0.0, description="Explicit shipping/freight charges if specified")
    taxes_and_duties: Optional[float] = Field(default=None, ge=0.0, description="Applicable taxes, tariffs or duties if specified")
    additional_charges: Optional[float] = Field(default=None, ge=0.0, description="Tooling, cylinder, or handling charges")
    is_freight_included: bool = Field(default=False, description="True if freight is bundled into quoted price (e.g. CIF, DDP)")
    is_tax_included: bool = Field(default=False, description="True if taxes/duties are bundled into quoted price (e.g. DDP)")
    are_cost_inputs_complete: bool = Field(
        default=False,
        description="True only when all logistics/duty components are known for landed cost"
    )
    estimated_landed_unit_cost_target_curr: Optional[float] = Field(
        default=None,
        ge=0.0,
        description="Estimated landed unit cost in target currency (None if incomplete inputs)"
    )
    target_currency: str = Field(default="USD", min_length=3, max_length=3, description="Currency used for landed cost normalization")
    fx_rate_applied: Optional[ExchangeRateRecord] = Field(
        default=None,
        description="Explicit record of exchange rate applied"
    )
    landed_cost_explanation: str = Field(
        default="",
        description="Detailed explanation of landed cost formula or reason for incompleteness"
    )
    validity_days: int = Field(default=60, ge=1, description="Validity period of the quote in days")
    validity_end_date: Optional[date] = Field(default=None, description="Date when quote validity expires")
    critical_fields_confirmed: bool = Field(
        default=False,
        description="Human confirmation status of critical commercial inputs before comparison"
    )
    provenance_map: Dict[str, FieldExtractionProvenance] = Field(
        default_factory=dict,
        description="Lineage metadata per extracted field"
    )

    def calculate_landed_cost(
        self,
        fx_record: Optional[ExchangeRateRecord] = None,
        target_currency: str = "USD",
    ) -> None:
        """
        Calculate estimated landed unit cost in target currency strictly when all inputs are complete.
        Never invents missing freight or tax numbers.
        """
        self.target_currency = target_currency
        self.fx_rate_applied = fx_record

        # Determine if cost components are complete
        # Complete if DDP (everything bundled), or both freight and tax are either bundled or explicitly quantified
        freight_known = self.is_freight_included or (self.incoterms == Incoterm.DDP) or (self.freight_charges is not None)
        tax_known = self.is_tax_included or (self.incoterms == Incoterm.DDP) or (self.taxes_and_duties is not None)

        if not (freight_known and tax_known):
            self.are_cost_inputs_complete = False
            self.estimated_landed_unit_cost_target_curr = None
            missing = []
            if not freight_known:
                missing.append(f"Freight charges (required for {self.incoterms.value})")
            if not tax_known:
                missing.append(f"Import duties/taxes (required for {self.incoterms.value})")
            self.landed_cost_explanation = (
                f"Landed cost cannot be reliably computed: Missing {', '.join(missing)}. "
                f"Comparing quoted base unit price only ({self.quoted_unit_price:.4f} {self.quoted_currency})."
            )
            return

        # Inputs are complete
        self.are_cost_inputs_complete = True

        # Calculate unit freight
        unit_freight = 0.0
        if not self.is_freight_included and self.incoterms != Incoterm.DDP and self.freight_charges:
            unit_freight = self.freight_charges / max(self.quoted_quantity, 1)

        # Calculate unit tax/duty
        unit_tax = 0.0
        if not self.is_tax_included and self.incoterms != Incoterm.DDP and self.taxes_and_duties:
            unit_tax = self.taxes_and_duties / max(self.quoted_quantity, 1)

        # Additional per-unit charges
        unit_add = 0.0
        if self.additional_charges:
            unit_add = self.additional_charges / max(self.quoted_quantity, 1)

        total_unit_quoted_curr = self.quoted_unit_price + unit_freight + unit_tax + unit_add

        # Convert to target currency if needed
        if self.quoted_currency.upper() == target_currency.upper():
            self.estimated_landed_unit_cost_target_curr = round(total_unit_quoted_curr, 4)
            self.landed_cost_explanation = (
                f"Estimated Landed Cost: Quoted Base ({self.quoted_unit_price:.4f}) + Freight ({unit_freight:.4f}) + "
                f"Duties/Taxes ({unit_tax:.4f}) + Other ({unit_add:.4f}) = "
                f"{self.estimated_landed_unit_cost_target_curr:.4f} {target_currency}/unit (Incoterm: {self.incoterms.value})."
            )
        else:
            if not fx_record or not fx_record.rate or fx_record.rate <= 0:
                self.are_cost_inputs_complete = False
                self.estimated_landed_unit_cost_target_curr = None
                self.landed_cost_explanation = (
                    f"Conversion rate from {self.quoted_currency} to {target_currency} is unavailable or unconfigured. "
                    f"Cannot compute normalized landed cost."
                )
                return

            # Apply exchange rate
            # fx_record.rate represents 1 Base = rate Target (e.g. 1 USD = 84.50 INR)
            if fx_record.base_currency.upper() == target_currency.upper() and fx_record.target_currency.upper() == self.quoted_currency.upper():
                converted_unit = total_unit_quoted_curr / fx_record.rate
            elif fx_record.target_currency.upper() == target_currency.upper() and fx_record.base_currency.upper() == self.quoted_currency.upper():
                converted_unit = total_unit_quoted_curr * fx_record.rate
            else:
                converted_unit = total_unit_quoted_curr * fx_record.rate

            self.estimated_landed_unit_cost_target_curr = round(converted_unit, 4)
            rate_desc = f"1 {fx_record.base_currency} = {fx_record.rate} {fx_record.target_currency} ({fx_record.source})"
            self.landed_cost_explanation = (
                f"Estimated Landed Cost: Quoted Base ({self.quoted_unit_price:.4f} {self.quoted_currency}) + "
                f"Freight ({unit_freight:.4f}) + Duties/Taxes ({unit_tax:.4f}) + Other ({unit_add:.4f}) = "
                f"{total_unit_quoted_curr:.4f} {self.quoted_currency}. "
                f"Converted: {self.estimated_landed_unit_cost_target_curr:.4f} {target_currency}/unit "
                f"using {rate_desc} (Effective: {fx_record.effective_date})."
            )


class QuotationTechnicalCompliance(BaseModel):
    """
    Technical specifications and documentary compliance extracted from quotation.
    Strictly distinguishes VERIFIED, SELF_REPORTED, UNVERIFIED, and MISSING evidence.
    Mandatory technical failures cannot be compensated by a low commercial price.
    """
    product_category: str = Field(..., min_length=2, description="Target product category (e.g. Flexible Packaging)")
    structure_description: str = Field(..., min_length=3, description="Substrate structure (e.g. PET/Met-BOPP/LDPE)")
    thickness_microns: Optional[float] = Field(default=None, gt=0.0, description="Total structure thickness in microns")
    otr_value: Optional[str] = Field(default=None, description="Oxygen Transmission Rate claim or test value")
    wvtr_value: Optional[str] = Field(default=None, description="Water Vapor Transmission Rate claim or test value")
    test_standards_cited: List[str] = Field(default_factory=list, description="Referenced ASTM/ISO standards (e.g. ASTM D3985)")
    certifications_offered: List[str] = Field(default_factory=list, description="Accompanying quality/sustainability certs")
    evidence_status_map: Dict[str, EvidenceStatus] = Field(
        default_factory=dict,
        description="Grounding against Phase 9 evidence tiers (VERIFIED, SELF_REPORTED, UNVERIFIED, MISSING)"
    )
    has_tds_document: bool = Field(default=False, description="True if authentic Technical Data Sheet is attached")
    mandatory_requirements_met: bool = Field(
        default=False,
        description="True only if all hard RFQ specifications (barrier, standards) are fully satisfied"
    )
    disqualification_reasons: List[str] = Field(
        default_factory=list,
        description="Explicit technical failure reasons disqualifying quotation"
    )
    compliance_notes: str = Field(default="", description="Detailed engineering compliance review notes")


class QuotationAuditEntry(BaseModel):
    """
    Immutable audit trail record for quotation lifecycle events, human gate sign-offs,
    and field confirmation actions.
    """
    audit_id: str = Field(default_factory=lambda: f"QAUDIT-{uuid.uuid4().hex[:8].upper()}")
    quotation_id: str = Field(..., min_length=1)
    from_status: Optional[str] = None
    to_status: str = Field(..., min_length=1)
    action: str = Field(default="STATUS_CHANGE", description="UPLOAD, MANUAL_ENTRY, CONFIRM_FIELDS, AWARD_ACCEPT, REJECT, SUPERSEDE")
    changed_by: str = Field(..., min_length=1, description="Procurement lead or system agent identity")
    comments: str = Field(..., min_length=1, description="Mandatory rationale or sign-off notes")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SupplierQuotation(BaseModel):
    """
    Supplier quotation instance with versioning, extraction provenance,
    commercial pricing, technical compliance, and human audit history.
    Preserves all historical versions immutably.
    """
    quotation_id: str = Field(default_factory=lambda: f"QUOTE-{uuid.uuid4().hex[:8].upper()}")
    rfq_id: str = Field(..., min_length=1, description="Reference to parent supplier_rfqs record")
    supplier_id: str = Field(..., min_length=1, description="Reference to supplier_profiles record")
    brief_id: str = Field(..., min_length=1, description="Reference to sourcing_briefs record")
    company_name: str = Field(..., min_length=1, description="Supplier corporate entity name")
    version: int = Field(default=1, ge=1, description="Sequential version counter (1, 2, ...)")
    status: QuotationStatus = Field(default=QuotationStatus.RECEIVED)
    submission_date: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    validity_end_date: Optional[date] = Field(default=None)
    file_type: str = Field(default="MANUAL", description="'PDF', 'EXCEL', or 'MANUAL'")
    raw_file_path: Optional[str] = Field(default=None, description="Storage path of uploaded source document")
    file_hash_sha256: Optional[str] = Field(default=None, description="SHA-256 hash of uploaded file")
    commercial: QuotationCommercialData
    compliance: QuotationTechnicalCompliance
    reviewer_notes: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    audit_trail: List[QuotationAuditEntry] = Field(default_factory=list)

    @field_validator("status", mode="before")
    @classmethod
    def parse_quotation_status(cls, val: Any) -> QuotationStatus:
        if isinstance(val, QuotationStatus):
            return val
        if isinstance(val, str):
            norm = val.strip().upper()
            try:
                return QuotationStatus(norm)
            except ValueError:
                raise DataValidationError(f"Invalid Quotation status '{val}'.")
        raise DataValidationError(f"Invalid Quotation status type: {type(val)}")


# ===========================================================================
# Phase 12C: Quotation Comparison & Analysis Models
# ===========================================================================

class ComparisonRecommendationType(str, Enum):
    """Classification of quotation candidate in deterministic comparison."""
    RECOMMENDED_PRIMARY = "RECOMMENDED_PRIMARY"
    RECOMMENDED_SECONDARY = "RECOMMENDED_SECONDARY"
    COMMERCIALLY_VIABLE_COST_INCOMPLETE = "COMMERCIALLY_VIABLE_COST_INCOMPLETE"
    NON_COMPLIANT_DISQUALIFIED = "NON_COMPLIANT_DISQUALIFIED"
    UNCONFIRMED_DRAFT = "UNCONFIRMED_DRAFT"


class QuotationComparisonRow(BaseModel):
    """
    Standardized, multi-dimensional comparison row for an individual quotation.
    Separates quoted base unit price from landed cost, records FX normalization,
    and captures non-compensable technical compliance.
    """
    quotation_id: str = Field(..., min_length=1)
    rfq_id: str = Field(..., min_length=1)
    supplier_id: str = Field(..., min_length=1)
    company_name: str = Field(..., min_length=1)
    version: int = Field(default=1, ge=1)
    status: QuotationStatus = Field(...)

    # Commercial parameters
    quoted_currency: str = Field(..., min_length=3, max_length=3)
    quoted_unit_price: float = Field(..., ge=0.0)
    quoted_quantity: int = Field(..., ge=1)
    min_order_quantity: int = Field(..., ge=1)
    lead_time_weeks: int = Field(..., ge=1)
    payment_terms: str = Field(...)
    incoterms: Incoterm = Field(...)
    is_freight_included: bool = Field(default=False)
    is_tax_included: bool = Field(default=False)
    freight_charges: Optional[float] = Field(default=None)
    taxes_and_duties: Optional[float] = Field(default=None)
    additional_charges: Optional[float] = Field(default=None)
    validity_days: int = Field(default=60)
    validity_end_date: Optional[date] = Field(default=None)

    # Normalization & Landed cost
    target_currency: str = Field(default="USD", min_length=3, max_length=3)
    normalized_base_unit_price: Optional[float] = Field(default=None, description="Base price in target comparison currency")
    normalized_landed_unit_cost: Optional[float] = Field(default=None, description="Estimated landed cost in target currency (None if incomplete)")
    is_price_comparable: bool = Field(default=True, description="False if FX rate is unavailable")
    are_cost_inputs_complete: bool = Field(default=False, description="True only if all logistics/duty components are known")
    landed_cost_explanation: str = Field(default="")
    fx_rate_applied: Optional[ExchangeRateRecord] = Field(default=None)

    # Technical compliance
    product_category: str = Field(default="")
    structure_description: str = Field(default="")
    thickness_microns: Optional[float] = Field(default=None)
    otr_value: Optional[str] = Field(default=None)
    wvtr_value: Optional[str] = Field(default=None)
    test_standards_cited: List[str] = Field(default_factory=list)
    certifications_offered: List[str] = Field(default_factory=list)
    has_tds_document: bool = Field(default=False)
    evidence_status_map: Dict[str, EvidenceStatus] = Field(default_factory=dict)
    mandatory_requirements_met: bool = Field(default=True)
    disqualification_reasons: List[str] = Field(default_factory=list)

    # Deterministic scoring (0-100 scale)
    commercial_score: float = Field(default=0.0, ge=0.0, le=100.0)
    technical_score: float = Field(default=0.0, ge=0.0, le=100.0)
    credibility_score: float = Field(default=0.0, ge=0.0, le=100.0)
    overall_comparison_score: float = Field(default=0.0, ge=0.0, le=100.0)

    # Recommendation
    recommendation_type: ComparisonRecommendationType = Field(default=ComparisonRecommendationType.UNCONFIRMED_DRAFT)
    recommendation_notes: str = Field(default="")


class QuotationComparisonMatrix(BaseModel):
    """
    Complete side-by-side comparison matrix and deterministic recommendation package
    for multiple supplier quotations under a brief or RFQ.
    """
    matrix_id: str = Field(default_factory=lambda: f"MATRIX-{uuid.uuid4().hex[:8].upper()}")
    brief_id: str = Field(..., min_length=1)
    target_currency: str = Field(default="USD", min_length=3, max_length=3)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    rows: List[QuotationComparisonRow] = Field(default_factory=list)
    fx_rates_used: Dict[str, ExchangeRateRecord] = Field(default_factory=dict)
    are_all_landed_costs_complete: bool = Field(default=False)
    ranking_metric: str = Field(
        default="NORMALIZED_BASE_PRICE",
        description="'NORMALIZED_LANDED_COST' if complete across compliant candidates, else 'NORMALIZED_BASE_PRICE'"
    )
    comparison_notes: str = Field(default="")
    primary_recommendation: Optional[QuotationComparisonRow] = Field(default=None)
    secondary_recommendation: Optional[QuotationComparisonRow] = Field(default=None)
    disqualified_candidates: List[QuotationComparisonRow] = Field(default_factory=list)
    governance_notice: str = Field(
        default=(
            "Advisory Notice: Quotation comparison and recommendations are deterministically computed "
            "decision-support evaluations. Final supplier award strictly requires explicit human buyer sign-off. "
            "Zero automated purchase orders or contract commitments are generated."
        )
    )
