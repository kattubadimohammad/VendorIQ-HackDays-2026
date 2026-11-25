"""
VendorIQ Deterministic Supplier Evaluation Engine.

Evaluates supplier profiles against buyer sourcing criteria using the five agreed
scoring dimensions:
- Industry relevance: 35%
- Geographic fit: 25%
- Company size fit: 20%
- Business signals: 10%
- Data completeness: 10%

Guarantees:
- Fully deterministic scoring logic.
- Missing data is never treated as proof of poor quality (assigned neutral baseline).
- Data completeness is scored independently in its own 10% dimension.
- Clear flags for insufficient evidence; never labels suppliers as 'verified' or 'approved'.
- Retains fine-grained provenance on every factor score.
"""

from typing import List, Dict, Any, Optional
import re
import uuid

from core.models import (
    SupplierProfile,
    SourcingCriteria,
    ScoringWeights,
    ScoringDimension,
    FactorScore,
    SupplierScore,
)
from core.provenance import ProvenanceClassification
from db.repository import VendorIQRepository


# Packaging-specific capability keyword taxonomy for deterministic relevance scoring
_PACKAGING_CAPABILITY_KEYWORDS = [
    "packaging", "containers", "films", "flexible packaging", "corrugated",
    "cartons", "molding", "bottles", "boxes", "bopp", "polypropylene",
    "pouches", "thermoforming", "tinplate", "extrusion", "laminates",
    "biodegradable", "compostable", "barrier films", "injection molded",
    "hdpe", "dinnerware", "foam", "paper packaging", "specialty films",
]


class SupplierEvaluationEngine:
    """
    Deterministic scoring and evaluation engine for supplier profiles.
    """

    def __init__(
        self,
        repository: Optional[VendorIQRepository] = None,
        weights: Optional[ScoringWeights] = None,
    ):
        self.repository = repository or VendorIQRepository()
        self.weights = weights or ScoringWeights()

    def evaluate_supplier(
        self,
        supplier: SupplierProfile,
        target_industry: str = "Packaging and Containers Manufacturing",
        target_country: str = "India",
    ) -> SupplierScore:
        """
        Evaluate an individual supplier deterministically across all five dimensions.
        """
        w = self.weights

        # 1. Industry Relevance (35%)
        f_industry = self._score_industry_relevance(
            supplier=supplier,
            target_industry=target_industry,
            weight=w.industry_relevance,
        )

        # 2. Geographic Fit (25%)
        f_geo = self._score_geographic_fit(
            supplier=supplier,
            target_country=target_country,
            weight=w.geographic_fit,
        )

        # 3. Company Size Fit (20%)
        f_size = self._score_company_size_fit(
            supplier=supplier,
            weight=w.company_size_fit,
        )

        # 4. Business Signals (10%)
        f_signals = self._score_business_signals(
            supplier=supplier,
            weight=w.business_signals,
        )

        # 5. Data Completeness (10%)
        f_completeness = self._score_data_completeness(
            supplier=supplier,
            weight=w.data_completeness,
        )

        factor_scores = {
            ScoringDimension.INDUSTRY_RELEVANCE: f_industry,
            ScoringDimension.GEOGRAPHIC_FIT: f_geo,
            ScoringDimension.COMPANY_SIZE_FIT: f_size,
            ScoringDimension.BUSINESS_SIGNALS: f_signals,
            ScoringDimension.DATA_COMPLETENESS: f_completeness,
        }

        completeness_pct = supplier.completeness_percentage()

        # Audit Summary Generation
        summary = (
            f"Candidate evaluation for {supplier.company_name} ({supplier.domain}). "
            f"Evaluated against target criteria ({target_industry}, {target_country}). "
            f"EVIDENCE LEVEL: UNVERIFIED_SELF_REPORTED (Crustdata company discovery). "
            f"STATUS: EVALUATED_CANDIDATE (Not verified/approved; technical & compliance audit required before RFP)."
        )

        return SupplierScore.compute(
            supplier_id=supplier.supplier_id,
            company_name=supplier.company_name,
            factor_scores=factor_scores,
            data_completeness_percentage=completeness_pct,
            summary=summary,
            weights_used=self.weights,
            provenance=ProvenanceClassification.AI_ASSESSMENT,
        )

    def evaluate_and_rank_batch(
        self,
        suppliers: List[SupplierProfile],
        target_industry: str = "Packaging and Containers Manufacturing",
        target_country: str = "India",
        persist_to_db: bool = False,
        brief_id: Optional[str] = None,
    ) -> List[SupplierScore]:
        """
        Evaluate a batch of suppliers, rank by total score descending, and optionally
        persist scoring run and scorecards to SQLite.
        """
        scorecards: List[SupplierScore] = []
        for s in suppliers:
            scorecard = self.evaluate_supplier(
                supplier=s,
                target_industry=target_industry,
                target_country=target_country,
            )
            scorecards.append(scorecard)

        # Rank deterministically by total_score descending, then company_name
        scorecards.sort(key=lambda sc: (-sc.total_score, sc.company_name))

        if persist_to_db and brief_id:
            run_id = self.repository.create_scoring_run(
                brief_id=brief_id,
                weights=self.weights,
                notes="Phase 6 Deterministic Supplier Scoring Run",
            )
            for sc in scorecards:
                self.repository.create_evaluation(run_id=run_id, score=sc)

        return scorecards

    # -------------------------------------------------------------------------
    # Individual Dimension Scoring Methods (Deterministic)
    # -------------------------------------------------------------------------

    def _score_industry_relevance(
        self,
        supplier: SupplierProfile,
        target_industry: str,
        weight: float,
    ) -> FactorScore:
        """
        Score industry relevance (35%).
        Evaluates packaging capability keywords in description and industry tags.
        Missing descriptions are assigned neutral baseline 50.0 without penalty.
        """
        desc = supplier.description or ""
        if not desc.strip():
            return FactorScore.missing_data_neutral(
                dimension=ScoringDimension.INDUSTRY_RELEVANCE,
                weight=weight,
                reasoning=(
                    "Neutral baseline score (50.0/100) applied: Company description is missing. "
                    "Missing data is not treated as poor supplier capability. Sample audit required."
                ),
            )

        desc_lower = desc.lower()
        matched_keywords = [kw for kw in _PACKAGING_CAPABILITY_KEYWORDS if kw in desc_lower]
        match_count = len(matched_keywords)

        if match_count >= 5:
            score = 95.0
            tier = "High"
        elif match_count >= 3:
            score = 85.0
            tier = "Strong"
        elif match_count >= 1:
            score = 75.0
            tier = "Moderate"
        else:
            score = 60.0
            tier = "General"

        reasoning = (
            f"{tier} packaging industry alignment ({score:.0f}/100). Matched {match_count} capability keywords "
            f"({', '.join(matched_keywords[:4])}) in self-reported Crustdata company overview. "
            f"INSUFFICIENT EVIDENCE: Independent manufacturing facility inspection required."
        )

        return FactorScore(
            dimension=ScoringDimension.INDUSTRY_RELEVANCE,
            score=score,
            weight=weight,
            reasoning=reasoning,
            evidence_sources=["Crustdata basic_info.description (Self-Reported)"],
            is_missing_data=False,
            confidence=0.75,
            provenance=ProvenanceClassification.AI_ASSESSMENT,
        )

    def _score_geographic_fit(
        self,
        supplier: SupplierProfile,
        target_country: str,
        weight: float,
    ) -> FactorScore:
        """
        Score geographic fit (25%).
        Recognizes discovery index match for target country.
        Marks city location as unverified/missing if not provided.
        """
        target_lower = target_country.strip().lower()
        country_lower = supplier.headquarters_country.strip().lower()

        # Check if country matches target or was matched via discovery query filter
        is_country_match = (
            country_lower == target_lower
            or "india" in (supplier.description or "").lower()
            or supplier.headquarters_country == "Unknown"  # Matched via India discovery query
        )

        has_city = bool(supplier.headquarters_city and supplier.headquarters_city.strip())

        if is_country_match and has_city:
            score = 95.0
            reasoning = (
                f"High geographic fit ({score:.0f}/100): Confirmed presence in {target_country} "
                f"with city headquarters ({supplier.headquarters_city})."
            )
            is_missing = False
        elif is_country_match:
            score = 75.0
            reasoning = (
                f"Moderate geographic fit ({score:.0f}/100): Sourced under {target_country} discovery filter. "
                f"City-level facility address is unverified in basic discovery tier; on-site audit required."
            )
            is_missing = True
        else:
            score = 50.0
            reasoning = (
                f"Neutral baseline score ({score:.0f}/100): Headquarters location ({supplier.headquarters_country}) "
                f"differs from target ({target_country}) or is unconfirmed."
            )
            is_missing = True

        return FactorScore(
            dimension=ScoringDimension.GEOGRAPHIC_FIT,
            score=score,
            weight=weight,
            reasoning=reasoning,
            evidence_sources=["Crustdata locations discovery index"],
            is_missing_data=is_missing,
            confidence=0.70 if not is_missing else 0.50,
            provenance=ProvenanceClassification.AI_ASSESSMENT,
        )

    def _score_company_size_fit(
        self,
        supplier: SupplierProfile,
        weight: float,
    ) -> FactorScore:
        """
        Score company size fit (20%).
        Maps employee ranges deterministically to production readiness tiers.
        """
        emp_range = (supplier.employee_range or "").strip()

        # Deterministic size mapping table
        size_scoring_table = {
            "201-500": (90.0, "Optimal mid-to-large capacity; balanced volume capability and operational agility"),
            "51-200": (85.0, "Established mid-market manufacturer with flexible production runs"),
            "501-1000": (85.0, "Large-scale industrial production capacity with multi-line capabilities"),
            "1001-5000": (80.0, "Major enterprise manufacturer; high capacity but potential high minimum order quantities"),
            "5001-10000": (75.0, "Conglomerate scale; high volume capabilities, potentially rigid procurement procedures"),
            "11-50": (65.0, "Small-scale manufacturer; suitable for specialized or localized batches"),
            "2-10": (45.0, "Micro-scale enterprise; potential capacity and scaling constraints for volume demand"),
        }

        if emp_range in size_scoring_table:
            score, explanation = size_scoring_table[emp_range]
            reasoning = f"Employee headcount range '{emp_range}': {explanation}."
            is_missing = False
        elif supplier.employee_count:
            count = supplier.employee_count
            if 200 <= count <= 500:
                score = 90.0
            elif 50 <= count < 200 or 500 < count <= 1000:
                score = 85.0
            elif count > 1000:
                score = 80.0
            else:
                score = 60.0
            reasoning = f"Reported headcount of {count} employees."
            is_missing = False
        else:
            return FactorScore.missing_data_neutral(
                dimension=ScoringDimension.COMPANY_SIZE_FIT,
                weight=weight,
                reasoning=(
                    "Neutral baseline score (50.0/100) applied: Employee size range is not provided. "
                    "Missing data is not penalized as poor quality."
                ),
            )

        return FactorScore(
            dimension=ScoringDimension.COMPANY_SIZE_FIT,
            score=score,
            weight=weight,
            reasoning=reasoning,
            evidence_sources=["Crustdata basic_info.employee_count_range"],
            is_missing_data=is_missing,
            confidence=0.80,
            provenance=ProvenanceClassification.AI_ASSESSMENT,
        )

    def _score_business_signals(
        self,
        supplier: SupplierProfile,
        weight: float,
    ) -> FactorScore:
        """
        Score business signals (10%).
        Evaluates verifiable business maturity indicators (public market listings, patents, multi-facility footprint).
        Missing signals receive neutral baseline (50.0) without penalty.
        """
        desc = (supplier.description or "").lower()
        signals: List[str] = []

        if any(term in desc for term in ["bse", "nse", "mumbai stock exchange", "listed on"]):
            signals.append("Publicly listed entity (BSE/NSE)")
        if any(term in desc for term in ["patent", "patented", "intellectual property"]):
            signals.append("Proprietary patent portfolio mentioned")
        if any(term in desc for term in ["facilities in", "manufacturing sites", "plants across", "state of the art"]):
            signals.append("Multi-facility manufacturing footprint")
        if any(term in desc for term in ["export", "global leader", "market leader", "largest producer"]):
            signals.append("Established market leadership / export orientation")

        if len(signals) >= 3:
            score = 85.0
        elif len(signals) >= 2:
            score = 75.0
        elif len(signals) == 1:
            score = 65.0
        else:
            # Baseline neutral score
            score = 50.0

        if signals:
            reasoning = (
                f"Business maturity indicators ({score:.0f}/100): Detected {', '.join(signals)}. "
                f"NOTE: Claims derived from company overview text; financial audit required."
            )
        else:
            reasoning = (
                f"Neutral baseline score (50.0/100): No explicit maturity signals found in basic discovery overview. "
                f"Absence of mentions is not penalized as poor supplier quality."
            )

        return FactorScore(
            dimension=ScoringDimension.BUSINESS_SIGNALS,
            score=score,
            weight=weight,
            reasoning=reasoning,
            evidence_sources=["Crustdata company overview signals"],
            is_missing_data=(len(signals) == 0),
            confidence=0.65,
            provenance=ProvenanceClassification.AI_ASSESSMENT,
        )

    def _score_data_completeness(
        self,
        supplier: SupplierProfile,
        weight: float,
    ) -> FactorScore:
        """
        Score data completeness (10%).
        Directly measures the percentage of populated company attributes.
        Isolates missing data penalties to this dimension alone.
        """
        pct = supplier.completeness_percentage()
        # Scale completeness directly to 0-100 score
        score = round(pct, 1)

        reasoning = (
            f"Profile data completeness score: {score:.1f}/100 based on populated attributes. "
            f"Evaluates presence of company name, domain, description, size, location, and signals."
        )

        return FactorScore(
            dimension=ScoringDimension.DATA_COMPLETENESS,
            score=score,
            weight=weight,
            reasoning=reasoning,
            evidence_sources=["VendorIQ Data Completeness Metric"],
            is_missing_data=False,
            confidence=1.0,
            provenance=ProvenanceClassification.AI_ASSESSMENT,
        )
