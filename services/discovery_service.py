"""
VendorIQ Supplier Discovery & Recommendation Service (Phase 10).

Provides intelligent, deterministic supplier discovery, multi-faceted filtering
(distinguishing hard requirements from soft preferences), evidence-backed recommendations,
and shortlist management with CSV export.

Truth-in-Evidence Guardrails:
- Deterministic ranking: Never alters deterministic evaluation scores.
- Transparent tie-breaking: total_score -> evidence_completeness -> query_relevance -> company_name.
- Missing information is never treated as confirmed eligibility or proof of poor quality.
- Zero external API calls or credit consumption.
"""

from datetime import datetime, timezone
import logging
import re
from typing import List, Dict, Any, Optional, Set

from core.models import (
    SupplierProfile,
    SupplierScore,
    SourcingBrief,
    EvidenceCategory,
    EvidenceStatus,
    SupplierEvidenceRecord,
    ShortlistStatus,
    ShortlistItem,
    SupplierShortlist,
    DiscoveryFilterParams,
    DiscoveryRecommendation,
)
from db.repository import VendorIQRepository
from services.data_quality_auditor import SupplierQualityAuditor, SupplierAuditItem
from services.evaluation_engine import SupplierEvaluationEngine
from services.evidence_service import SupplierEvidenceService

logger = logging.getLogger(__name__)


def _employee_range_bounds(value: Optional[str]) -> Optional[tuple[int, int]]:
    """Parse a directory headcount band such as '51-200' without treating its upper bound as actual headcount."""
    if not value:
        return None
    normalized = value.strip().replace(",", "")
    match = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", normalized)
    if match:
        lower, upper = int(match.group(1)), int(match.group(2))
        return (lower, upper) if lower <= upper else None
    match = re.fullmatch(r"(\d+)\s*\+", normalized)
    if match:
        return int(match.group(1)), 2**31 - 1
    match = re.fullmatch(r"(?:over|more than)\s*(\d+)", normalized, re.IGNORECASE)
    if match:
        return int(match.group(1)) + 1, 2**31 - 1
    return None


def _extract_query_tokens(text: Optional[str]) -> Set[str]:
    """Extract clean alphanumeric query tokens for local matching."""
    if not text:
        return set()
    clean = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
    stop_words = {"and", "or", "the", "in", "for", "with", "a", "an", "of", "to", "at", "by", "from"}
    return {t.strip() for t in clean.split() if len(t.strip()) > 1 and t.strip() not in stop_words}


class SupplierDiscoveryService:
    """
    Intelligent supplier discovery and recommendation engine.
    Integrates SQLite profiles, evaluation scorecards, and Phase 9 evidence records.
    """

    def __init__(
        self,
        repository: Optional[VendorIQRepository] = None,
        auditor: Optional[SupplierQualityAuditor] = None,
        evaluation_engine: Optional[SupplierEvaluationEngine] = None,
        evidence_service: Optional[SupplierEvidenceService] = None,
    ):
        self.repository = repository or VendorIQRepository()
        self.auditor = auditor or SupplierQualityAuditor()
        self.evaluation_engine = evaluation_engine or SupplierEvaluationEngine(repository=self.repository)
        self.evidence_service = evidence_service or SupplierEvidenceService(
            repository=self.repository,
            auditor=self.auditor,
        )

    def discover_suppliers(
        self,
        brief: Optional[SourcingBrief] = None,
        params: Optional[DiscoveryFilterParams] = None,
    ) -> List[DiscoveryRecommendation]:
        """
        Discover, filter, and rank suppliers deterministically based on brief criteria,
        natural language query, and hard/soft constraints.
        """
        filter_params = params or DiscoveryFilterParams()
        suppliers = self.repository.list_suppliers()
        if not suppliers:
            return []

        # 1. Retrieve structured evidence for all suppliers
        evidence_by_supp = self.evidence_service.generate_and_persist_evidence_for_all_suppliers()

        # 2. Retrieve data quality audit items for domain mismatch flags
        audit_report = self.auditor.audit_records(suppliers)
        audit_by_id = {item.supplier_id: item for item in audit_report.supplier_audit_items}

        # 3. Retrieve or compute deterministic evaluation scorecards
        eval_scores: Dict[str, SupplierScore] = {}
        if brief:
            existing_evals = self.repository.list_evaluations_for_brief(brief.brief_id)
            if existing_evals:
                eval_scores = {e.supplier_id: e for e in existing_evals}
            else:
                computed_evals = self.evaluation_engine.evaluate_and_rank_batch(
                    suppliers=suppliers,
                    target_industry=brief.criteria.target_industries[0] if brief.criteria.target_industries else "Packaging and Containers Manufacturing",
                    target_country=brief.criteria.target_geographies[0] if brief.criteria.target_geographies else "India",
                    persist_to_db=True,
                    brief_id=brief.brief_id,
                )
                eval_scores = {e.supplier_id: e for e in computed_evals}
        else:
            # Default evaluation if no brief provided
            computed_evals = self.evaluation_engine.evaluate_and_rank_batch(
                suppliers=suppliers,
                target_industry="Packaging and Containers Manufacturing",
                target_country="India",
                persist_to_db=False,
            )
            eval_scores = {e.supplier_id: e for e in computed_evals}

        # 4. Tokenize search query if present
        query_tokens = _extract_query_tokens(filter_params.query)

        # 5. Evaluate and filter candidates
        candidates: List[Dict[str, Any]] = []

        for supp in suppliers:
            supp_evidence = evidence_by_supp.get(supp.supplier_id, [])
            ev_map = {e.category: e for e in supp_evidence}
            audit_item = audit_by_id.get(supp.supplier_id)
            score_obj = eval_scores.get(supp.supplier_id)
            total_score = score_obj.total_score if score_obj else 50.0

            # -------------------------------------------------------------
            # Keyword / Semantic Query Matching
            # -------------------------------------------------------------
            query_match_count = 0
            if query_tokens:
                # Ground search matching strictly in company profile and positive/reported evidence
                # Exclude negative/missing data labels to avoid matching missing claims
                positive_claims = " ".join([
                    e.claim for e in supp_evidence
                    if e.status in (EvidenceStatus.SELF_REPORTED, EvidenceStatus.VERIFIED, EvidenceStatus.UNVERIFIED)
                    and not e.claim.lower().startswith("no ")
                ])
                supp_text = (
                    f"{supp.company_name} {supp.domain} {supp.description or ''} "
                    f"{' '.join(supp.industries)} {positive_claims}"
                ).lower()
                matched_tokens = {tok for tok in query_tokens if tok in supp_text}
                query_match_count = len(matched_tokens)

                # If query is provided, require at least one token match
                if query_match_count == 0:
                    continue

            # -------------------------------------------------------------
            # Hard Requirements Check
            # -------------------------------------------------------------
            hard_failures: List[str] = []

            # Industry filter
            if filter_params.industries:
                supp_inds = {i.lower() for i in supp.industries}
                target_inds = {i.lower() for i in filter_params.industries}
                if not (supp_inds & target_inds):
                    # Also check description
                    desc_lower = (supp.description or "").lower()
                    if not any(ti in desc_lower for ti in target_inds):
                        hard_failures.append(f"Industry vertical mismatch (Target: {', '.join(filter_params.industries)})")

            # Geography filter
            if filter_params.geographies:
                supp_geo = supp.headquarters_country.strip().lower()
                target_geos = {g.strip().lower() for g in filter_params.geographies}
                if supp_geo not in target_geos:
                    hard_failures.append(f"Country mismatch: {supp.headquarters_country} (Target: {', '.join(filter_params.geographies)})")

            # Employee range filter
            if filter_params.employee_ranges:
                if supp.employee_range not in filter_params.employee_ranges:
                    hard_failures.append(f"Employee range {supp.employee_range or 'N/A'} not in selected filters")

            # Minimum company size: a directory range is not an exact employee count.
            # A band that overlaps the threshold is not a confirmed pass.
            min_size = filter_params.min_company_size
            if brief and brief.criteria.min_company_size is not None:
                min_size = max(min_size or 0, brief.criteria.min_company_size)
            if min_size is not None:
                if supp.employee_count is not None:
                    if supp.employee_count < min_size:
                        hard_failures.append(
                            f"Employee count ({supp.employee_count}) below minimum ({min_size})"
                        )
                else:
                    bounds = _employee_range_bounds(supp.employee_range)
                    if bounds is None:
                        hard_failures.append(
                            f"Cannot confirm minimum headcount of {min_size}: employee count/range is missing or unparseable"
                        )
                    elif bounds[1] < min_size:
                        hard_failures.append(
                            f"Employee range {supp.employee_range} is below minimum headcount ({min_size})"
                        )
                    elif bounds[0] < min_size:
                        hard_failures.append(
                            f"Cannot confirm minimum headcount of {min_size} from directory range {supp.employee_range}"
                        )

            # Hard: Manufacturing capability
            if filter_params.require_manufacturing:
                mfg_ev = ev_map.get(EvidenceCategory.MANUFACTURING_CAPABILITIES)
                if not mfg_ev or mfg_ev.status == EvidenceStatus.MISSING:
                    hard_failures.append("Missing confirmed manufacturing / converting capability")

            # Hard: Flexible packaging experience
            if filter_params.require_flexible_packaging:
                flex_ev = ev_map.get(EvidenceCategory.FLEXIBLE_PACKAGING_EXPERIENCE)
                if not flex_ev or flex_ev.status == EvidenceStatus.MISSING:
                    hard_failures.append("Missing documented flexible packaging experience")

            # Hard: Barrier film experience
            if filter_params.require_barrier_films:
                barrier_ev = ev_map.get(EvidenceCategory.BARRIER_FILM_EXPERIENCE)
                if not barrier_ev or barrier_ev.status == EvidenceStatus.MISSING:
                    hard_failures.append("Missing barrier film technical experience / TDS on file")

            # Hard: Sustainability evidence
            if filter_params.require_sustainability_evidence:
                sust_ev = ev_map.get(EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS)
                if not sust_ev or sust_ev.status == EvidenceStatus.MISSING:
                    hard_failures.append("Missing sustainability certifications or verified environmental evidence")

            # Hard: Exclude domain mismatches
            if filter_params.exclude_domain_mismatches:
                if audit_item and audit_item.suspicious_domain_mismatch:
                    hard_failures.append(f"Domain mismatch flagged by auditor: {audit_item.mismatch_reason}")

            is_hard_match = (len(hard_failures) == 0)

            # -------------------------------------------------------------
            # Evidence Breakdown
            # -------------------------------------------------------------
            available_ev: List[str] = []
            missing_ev: List[str] = []
            unverified_claims: List[str] = []

            for ev in supp_evidence:
                if ev.status in (EvidenceStatus.SELF_REPORTED, EvidenceStatus.VERIFIED):
                    prefix = "Verified" if ev.status == EvidenceStatus.VERIFIED else "Self-Reported"
                    available_ev.append(f"[{prefix}] {ev.category.value}: {ev.claim}")
                elif ev.status == EvidenceStatus.UNVERIFIED:
                    unverified_claims.append(f"[Unverified Claim] {ev.category.value}: {ev.claim}")
                elif ev.status == EvidenceStatus.MISSING:
                    missing_ev.append(f"[Missing] {ev.category.value}: {ev.claim}")

            # Evidence completeness KPI
            kpis = self.evidence_service.calculate_evidence_completeness(supp_evidence)
            completeness_pct = kpis["completeness_percentage"]

            # Domain mismatch warning
            domain_warning = None
            if audit_item and audit_item.suspicious_domain_mismatch:
                domain_warning = (
                    f"Domain stem '{audit_item.domain}' does not align with legal name '{supp.company_name}'. "
                    f"Audit note: {audit_item.mismatch_reason}"
                )

            # Procurement risks
            procurement_risks: List[str] = [
                "Basic directory indexing tier: Physical factory audit and equipment verification required prior to contracting."
            ]
            if domain_warning:
                procurement_risks.append("Corporate identity / domain anomaly: Manual verification of holding company needed.")

            # Barrier film risk differentiation:
            barrier_ev_rec = ev_map.get(EvidenceCategory.BARRIER_FILM_EXPERIENCE)
            if barrier_ev_rec and barrier_ev_rec.status == EvidenceStatus.SELF_REPORTED:
                procurement_risks.append(
                    "Barrier film capability is based on self-reported directory description; "
                    "documentary technical data sheets (TDS) and ASTM F1249/D3985 gas transmission rate test reports (OTR/WVTR) are NOT on file."
                )
                if supp.supplier_id == "CRUST_2440":
                    procurement_risks.append(
                        "Scope discrepancy note: Company is primarily an FIBC industrial bulk sack maker; "
                        "'Al foil bags' reference relates to bulk sack liners rather than thin flexible barrier film converting."
                    )
            elif not barrier_ev_rec or barrier_ev_rec.status == EvidenceStatus.MISSING:
                procurement_risks.append("No barrier film technical specifications, transmission rates, or TDS on file.")

            if ev_map.get(EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS) and ev_map[EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS].status in (EvidenceStatus.UNVERIFIED, EvidenceStatus.MISSING):
                procurement_risks.append("Zero accredited environmental certificates (ISO 14001 / FSC) on file.")
            if not supp.headquarters_city:
                procurement_risks.append("Headquarters city unspecified in public registry record.")

            # Factor scores breakdown
            factor_scores_dict: Dict[str, float] = {}
            if score_obj:
                for dim, fscore in score_obj.factor_scores.items():
                    dim_key = dim.value if hasattr(dim, "value") else str(dim)
                    factor_scores_dict[dim_key] = fscore.score

            # Match explanation
            brief_title = brief.title if brief else "Packaging Sourcing Project"
            hard_match_str = "Meets all specified search criteria." if is_hard_match else f"Fails {len(hard_failures)} hard requirement(s)."
            explanation = (
                f"Evaluated against '{brief_title}'. Composite score: {total_score:.1f}/100. "
                f"Organizational size: {supp.employee_range or 'N/A'}. "
                f"Evidence completeness: {completeness_pct:.0f}%. {hard_match_str}"
            )

            candidates.append({
                "supplier": supp,
                "total_score": total_score,
                "is_hard_match": is_hard_match,
                "hard_failures": hard_failures,
                "completeness_pct": completeness_pct,
                "query_match_count": query_match_count,
                "available_ev": available_ev,
                "missing_ev": missing_ev,
                "unverified_claims": unverified_claims,
                "domain_warning": domain_warning,
                "procurement_risks": procurement_risks,
                "factor_scores": factor_scores_dict,
                "explanation": explanation,
            })

        # -------------------------------------------------------------
        # 6. Deterministic Ranking & Transparent Tie-Breaking
        # Order:
        # 1. is_hard_match (True first)
        # 2. total_score DESC
        # 3. completeness_pct DESC
        # 4. query_match_count DESC
        # 5. company_name ASC
        # -------------------------------------------------------------
        candidates.sort(
            key=lambda c: (
                1 if c["is_hard_match"] else 0,
                c["total_score"],
                c["completeness_pct"],
                c["query_match_count"],
                -ord(c["supplier"].company_name[0].lower()) if c["supplier"].company_name else 0,
            ),
            reverse=True,
        )

        # Build formal DiscoveryRecommendation objects
        recommendations: List[DiscoveryRecommendation] = []
        for rank_idx, cand in enumerate(candidates, start=1):
            supp = cand["supplier"]
            recommendations.append(
                DiscoveryRecommendation(
                    supplier_id=supp.supplier_id,
                    company_name=supp.company_name,
                    domain=supp.domain,
                    headquarters_country=supp.headquarters_country,
                    headquarters_city=supp.headquarters_city,
                    employee_range=supp.employee_range,
                    total_score=cand["total_score"],
                    rank=rank_idx,
                    match_explanation=cand["explanation"],
                    available_evidence=cand["available_ev"],
                    missing_evidence=cand["missing_ev"],
                    unverified_claims=cand["unverified_claims"],
                    domain_mismatch_warning=cand["domain_warning"],
                    procurement_risks=cand["procurement_risks"],
                    evidence_completeness_pct=cand["completeness_pct"],
                    is_hard_match=cand["is_hard_match"],
                    hard_filter_failures=cand["hard_failures"],
                    factor_scores=cand["factor_scores"],
                )
            )

        return recommendations

    # =========================================================================
    # Shortlist Management & CSV Export
    # =========================================================================

    def create_shortlist(
        self,
        brief_id: str,
        name: str,
        description: Optional[str] = None,
    ) -> SupplierShortlist:
        """Create a new buyer shortlist for a sourcing brief."""
        shortlist = SupplierShortlist(
            brief_id=brief_id,
            name=name,
            description=description,
        )
        return self.repository.create_shortlist(shortlist)

    def get_shortlist(self, shortlist_id: str) -> Optional[SupplierShortlist]:
        """Retrieve a shortlist by ID."""
        return self.repository.get_shortlist(shortlist_id)

    def list_shortlists_for_brief(self, brief_id: str) -> List[SupplierShortlist]:
        """List all shortlists for a sourcing brief."""
        return self.repository.list_shortlists_for_brief(brief_id)

    def add_supplier_to_shortlist(
        self,
        shortlist_id: str,
        supplier_id: str,
        reviewer_notes: Optional[str] = None,
        status: ShortlistStatus = ShortlistStatus.CANDIDATE,
    ) -> ShortlistItem:
        """Add or update a supplier in a shortlist."""
        supp = self.repository.get_supplier_by_id(supplier_id)
        comp_name = supp.company_name if supp else None
        domain = supp.domain if supp else None

        item = ShortlistItem(
            shortlist_id=shortlist_id,
            supplier_id=supplier_id,
            company_name=comp_name,
            domain=domain,
            status=status,
            reviewer_notes=reviewer_notes,
        )
        return self.repository.upsert_shortlist_item(item)

    def update_shortlist_item_status(
        self,
        item_id: str,
        status: ShortlistStatus,
        reviewer_notes: Optional[str] = None,
    ) -> Optional[ShortlistItem]:
        """Update review status and notes for a shortlisted candidate."""
        return self.repository.update_shortlist_item_status(item_id, status, reviewer_notes)

    def remove_supplier_from_shortlist(self, item_id: str) -> bool:
        """Remove a candidate supplier from a shortlist."""
        return self.repository.remove_item_from_shortlist(item_id)

    def export_shortlist_to_csv(self, shortlist_id: str) -> str:
        """Export shortlist candidates, scores, and reviewer notes as a CSV string."""
        shortlist = self.get_shortlist(shortlist_id)
        if not shortlist:
            return ""
        return shortlist.to_csv()
