"""
VendorIQ Dashboard Data Service.

Provides clean interfaces between Streamlit UI views and the underlying
VendorIQRepository, SupplierQualityAuditor, and SupplierEvaluationEngine.
"""

from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
import os

from core.models import (
    SupplierProfile,
    SupplierScore,
    SourcingBrief,
    SourcingCriteria,
    ScoringWeights,
    RFQDraft,
    ExtractedSourcingRequirements,
    CopilotRecommendation,
    CopilotQueryResponse,
    EvidenceCategory,
    EvidenceStatus,
    SupplierEvidenceRecord,
    ShortlistStatus,
    ShortlistItem,
    SupplierShortlist,
    DiscoveryFilterParams,
    DiscoveryRecommendation,
    RFQStatus,
    RFQAuditEntry,
    RFQBuyerInfo,
    RFQProductRequirements,
    RFQCommercialRequirements,
    RFQSupplierTarget,
    RFQDocumentRecord,
    QuotationStatus,
    QuotationCommercialData,
    QuotationTechnicalCompliance,
    SupplierQuotation,
    QuotationAuditEntry,
    ExchangeRateRecord,
    QuotationComparisonRow,
    QuotationComparisonMatrix,
)
from db.repository import VendorIQRepository
from services.data_quality_auditor import SupplierQualityAuditor, DataQualityAuditReport
from services.evaluation_engine import SupplierEvaluationEngine
from services.copilot_service import AISourcingCopilot
from services.evidence_service import SupplierEvidenceService
from services.discovery_service import SupplierDiscoveryService
from services.rfq_service import RFQService
from services.rfq_generator import RFQDocumentGenerator
from services.quotation_service import QuotationService
from services.quotation_extractor import ExtractedQuotationDraft


class DashboardDataService:
    """Service layer for Streamlit views to access persistence and domain engines."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        repository: Optional[VendorIQRepository] = None,
    ):
        self.repository = repository or VendorIQRepository(db_path=db_path)
        self.auditor = SupplierQualityAuditor()
        self.evaluation_engine = SupplierEvaluationEngine(repository=self.repository)

    def get_all_briefs(self) -> List[SourcingBrief]:
        """Retrieve all sourcing briefs from database."""
        return self.repository.list_briefs()

    def get_brief(self, brief_id: str) -> Optional[SourcingBrief]:
        """Retrieve a specific sourcing brief by ID."""
        return self.repository.get_brief(brief_id)

    def create_brief(self, brief: SourcingBrief) -> SourcingBrief:
        """Create and persist a new sourcing brief."""
        return self.repository.create_brief(brief)

    def get_all_suppliers(self, limit: int = 100) -> List[SupplierProfile]:
        """Retrieve all suppliers stored in SQLite."""
        return self.repository.list_suppliers(limit=limit)

    def audit_suppliers(self, suppliers: Optional[List[SupplierProfile]] = None) -> DataQualityAuditReport:
        """Run quality audit across supplier profiles."""
        supps = suppliers if suppliers is not None else self.get_all_suppliers()
        return self.auditor.audit_records(supps)

    def get_evaluations(self, brief_id: str) -> List[SupplierScore]:
        """
        Get latest evaluations for a sourcing brief.
        If no evaluations exist yet in DB, computes and persists them.
        """
        runs = self.repository.list_scoring_runs_for_brief(brief_id)
        if runs:
            latest_run_id = runs[0]["run_id"]
            return self.repository.list_evaluations_for_run(latest_run_id)

        # If not evaluated yet, evaluate existing suppliers and persist
        suppliers = self.get_all_suppliers()
        if suppliers:
            return self.evaluation_engine.evaluate_and_rank_batch(
                suppliers=suppliers,
                target_industry="Packaging and Containers Manufacturing",
                target_country="India",
                persist_to_db=True,
                brief_id=brief_id,
            )
        return []

    def reevaluate_suppliers(
        self,
        brief_id: str,
        weights: Optional[ScoringWeights] = None,
        notes: str = "Interactive Dashboard Re-evaluation",
    ) -> List[SupplierScore]:
        """Re-evaluate suppliers with updated weights and persist as a new scoring run."""
        suppliers = self.get_all_suppliers()
        engine = SupplierEvaluationEngine(repository=self.repository, weights=weights)
        return engine.evaluate_and_rank_batch(
            suppliers=suppliers,
            target_industry="Packaging and Containers Manufacturing",
            target_country="India",
            persist_to_db=True,
            brief_id=brief_id,
        )

    def create_rfq_draft(
        self,
        brief_id: str,
        target_supplier_ids: List[str],
        title: str,
        scope_of_work: str,
        technical_requirements: List[str],
        submission_deadline: datetime,
        response_instructions: str,
        evaluation_criteria_summary: str,
    ) -> RFQDraft:
        """Create and persist a draft RFQ document in SQLite."""
        rfq = RFQDraft(
            brief_id=brief_id,
            target_supplier_ids=target_supplier_ids,
            title=title,
            scope_of_work=scope_of_work,
            technical_requirements=technical_requirements,
            submission_deadline=submission_deadline,
            response_instructions=response_instructions,
            evaluation_criteria_summary=evaluation_criteria_summary,
            status="DRAFT",
        )
        return self.repository.create_rfq(rfq)

    def list_rfqs(self, brief_id: str) -> List[RFQDraft]:
        """Retrieve all RFQs associated with a sourcing brief."""
        return self.repository.list_rfqs_for_brief(brief_id)

    def update_rfq_status(self, rfq_id: str, status: str) -> Optional[RFQDraft]:
        """Update RFQ lifecycle status."""
        return self.repository.update_rfq_status(rfq_id, status)

    # =========================================================================
    # Phase 8A: AI Sourcing Copilot
    # =========================================================================

    def get_copilot(self) -> AISourcingCopilot:
        """Get or lazily initialize the AI Sourcing Copilot service."""
        if not hasattr(self, "_copilot") or self._copilot is None:
            self._copilot = AISourcingCopilot(
                repository=self.repository,
                evaluation_engine=self.evaluation_engine,
            )
        return self._copilot

    def parse_brief_with_copilot(self, prompt: str) -> ExtractedSourcingRequirements:
        """Parse natural language sourcing brief into structured requirements."""
        return self.get_copilot().parse_natural_language_brief(prompt)

    def get_copilot_recommendations(
        self,
        brief: SourcingBrief,
        query: Optional[str] = None,
        top_k: int = 5,
    ) -> List[CopilotRecommendation]:
        """Get AI-synthesized supplier recommendations grounded in scorecards."""
        return self.get_copilot().recommend_candidates(brief=brief, query=query, top_k=top_k)

    def chat_with_copilot(
        self,
        message: str,
        brief: SourcingBrief,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> CopilotQueryResponse:
        """Query the AI Copilot for grounded procurement answers."""
        return self.get_copilot().chat_sourcing_assistant(message, brief, history)

    # =========================================================================
    # Phase 9: Supplier Intelligence & Evidence Verification
    # =========================================================================

    def get_evidence_service(self) -> SupplierEvidenceService:
        """Get or lazily initialize the SupplierEvidenceService."""
        if not hasattr(self, "_evidence_service") or self._evidence_service is None:
            self._evidence_service = SupplierEvidenceService(
                repository=self.repository,
                auditor=self.auditor,
            )
        return self._evidence_service

    def get_supplier_evidence(self, supplier_id: str) -> List[SupplierEvidenceRecord]:
        """Retrieve or evaluate structured evidence records for a supplier."""
        return self.get_evidence_service().get_supplier_evidence(supplier_id)

    def generate_and_persist_all_evidence(self, force_refresh: bool = False) -> Dict[str, List[SupplierEvidenceRecord]]:
        """Evaluate and persist evidence records for all suppliers in SQLite."""
        return self.get_evidence_service().generate_and_persist_evidence_for_all_suppliers(force_refresh=force_refresh)

    def calculate_evidence_completeness(self, evidence_records: List[SupplierEvidenceRecord]) -> Dict[str, Any]:
        """Calculate evidence completeness metrics."""
        return self.get_evidence_service().calculate_evidence_completeness(evidence_records)

    def get_missing_evidence_checklist(self, evidence_records: List[SupplierEvidenceRecord]) -> List[Dict[str, str]]:
        """Retrieve missing evidence checklist with required documentary proof."""
        return self.get_evidence_service().get_missing_evidence_checklist(evidence_records)

    def get_multi_supplier_coverage_matrix(self, suppliers: Optional[List[SupplierProfile]] = None) -> Dict[str, Any]:
        """Compute multi-supplier evidence coverage across all suppliers."""
        return self.get_evidence_service().get_multi_supplier_coverage_matrix(suppliers=suppliers)

    # =========================================================================
    # Phase 10: Intelligent Discovery & Shortlist Management
    # =========================================================================

    def get_discovery_service(self) -> SupplierDiscoveryService:
        """Get or lazily initialize the SupplierDiscoveryService."""
        if not hasattr(self, "_discovery_service") or self._discovery_service is None:
            self._discovery_service = SupplierDiscoveryService(
                repository=self.repository,
                auditor=self.auditor,
                evaluation_engine=self.evaluation_engine,
                evidence_service=self.get_evidence_service(),
            )
        return self._discovery_service

    def discover_suppliers(
        self,
        brief: Optional[SourcingBrief] = None,
        params: Optional[DiscoveryFilterParams] = None,
    ) -> List[DiscoveryRecommendation]:
        """Discover and rank candidate suppliers using deterministic filters and evidence."""
        return self.get_discovery_service().discover_suppliers(brief=brief, params=params)

    def create_shortlist(
        self,
        brief_id: str,
        name: str,
        description: Optional[str] = None,
    ) -> SupplierShortlist:
        """Create and persist a new buyer shortlist."""
        return self.get_discovery_service().create_shortlist(
            brief_id=brief_id,
            name=name,
            description=description,
        )

    def get_shortlist(self, shortlist_id: str) -> Optional[SupplierShortlist]:
        """Retrieve a specific shortlist by ID."""
        return self.get_discovery_service().get_shortlist(shortlist_id)

    def list_shortlists_for_brief(self, brief_id: str) -> List[SupplierShortlist]:
        """List all shortlists for a sourcing brief."""
        return self.get_discovery_service().list_shortlists_for_brief(brief_id)

    def add_supplier_to_shortlist(
        self,
        shortlist_id: str,
        supplier_id: str,
        reviewer_notes: Optional[str] = None,
        status: ShortlistStatus = ShortlistStatus.CANDIDATE,
    ) -> ShortlistItem:
        """Add candidate supplier to a shortlist."""
        return self.get_discovery_service().add_supplier_to_shortlist(
            shortlist_id=shortlist_id,
            supplier_id=supplier_id,
            reviewer_notes=reviewer_notes,
            status=status,
        )

    def update_shortlist_item_status(
        self,
        item_id: str,
        status: ShortlistStatus,
        reviewer_notes: Optional[str] = None,
    ) -> Optional[ShortlistItem]:
        """Update review status or notes for a shortlisted candidate."""
        return self.get_discovery_service().update_shortlist_item_status(
            item_id=item_id,
            status=status,
            reviewer_notes=reviewer_notes,
        )

    def remove_supplier_from_shortlist(self, item_id: str) -> bool:
        """Remove a candidate supplier from a shortlist."""
        return self.get_discovery_service().remove_supplier_from_shortlist(item_id)

    def export_shortlist_csv(self, shortlist_id: str) -> str:
        """Export shortlist candidates, scores, and reviewer notes as CSV."""
        return self.get_discovery_service().export_shortlist_to_csv(shortlist_id)

    # =========================================================================
    # Phase 11: Professional RFQ Generation & Human Approval Workflow
    # =========================================================================

    def get_rfq_service(self) -> RFQService:
        """Get or lazily initialize the RFQ service."""
        if not hasattr(self, "_rfq_service") or self._rfq_service is None:
            self._rfq_service = RFQService(repository=self.repository)
        return self._rfq_service

    def prepare_supplier_rfq(
        self, brief: SourcingBrief, supplier: SupplierProfile, **kwargs
    ) -> RFQDocumentRecord:
        """Prepare a supplier-specific RFQ in memory."""
        return self.get_rfq_service().prepare_supplier_rfq(brief, supplier, **kwargs)

    def create_and_persist_supplier_rfq(
        self, brief: SourcingBrief, supplier: SupplierProfile, **kwargs
    ) -> RFQDocumentRecord:
        """Prepare and immediately persist a supplier-specific RFQ draft in SQLite."""
        return self.get_rfq_service().create_and_persist_rfq(brief, supplier, **kwargs)

    def create_rfqs_from_shortlist(
        self, shortlist_id: str, brief: SourcingBrief, **kwargs
    ) -> List[RFQDocumentRecord]:
        """Generate separate supplier-specific RFQs for every candidate in a shortlist."""
        return self.get_rfq_service().create_rfqs_from_shortlist(shortlist_id, brief, **kwargs)

    def get_supplier_rfq(self, rfq_id: str) -> Optional[RFQDocumentRecord]:
        """Fetch a supplier RFQ by its identifier with full audit trail."""
        return self.repository.get_supplier_rfq(rfq_id)

    def list_supplier_rfqs(
        self,
        brief_id: Optional[str] = None,
        supplier_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[RFQDocumentRecord]:
        """List supplier-specific RFQs matching filters."""
        return self.repository.list_supplier_rfqs(brief_id=brief_id, supplier_id=supplier_id, status=status)

    def update_supplier_rfq(self, rfq: RFQDocumentRecord) -> RFQDocumentRecord:
        """Update an existing supplier RFQ's details and file paths."""
        return self.repository.update_supplier_rfq(rfq)

    def submit_rfq_for_approval(
        self, rfq_id: str, user_name: str, comments: Optional[str] = None
    ) -> RFQDocumentRecord:
        """Transition DRAFT -> PENDING_APPROVAL."""
        return self.get_rfq_service().submit_for_approval(rfq_id, user_name, comments)

    def approve_rfq(
        self, rfq_id: str, reviewer_name: str, approval_notes: str
    ) -> RFQDocumentRecord:
        """Transition PENDING_APPROVAL -> APPROVED and generate DOCX/PDF."""
        return self.get_rfq_service().approve_rfq(rfq_id, reviewer_name, approval_notes)

    def reject_rfq(
        self, rfq_id: str, reviewer_name: str, rejection_reason: str
    ) -> RFQDocumentRecord:
        """Transition PENDING_APPROVAL -> REJECTED."""
        return self.get_rfq_service().reject_rfq(rfq_id, reviewer_name, rejection_reason)

    def revise_rfq(
        self, rfq_id: str, user_name: str, notes: Optional[str] = None
    ) -> RFQDocumentRecord:
        """Transition REJECTED -> DRAFT."""
        return self.get_rfq_service().revise_rfq(rfq_id, user_name, notes)

    def record_manual_dispatch(
        self, rfq_id: str, user_name: str, notes: str
    ) -> RFQDocumentRecord:
        """Transition APPROVED -> SENT (Manual dispatch sign-off)."""
        return self.get_rfq_service().record_manual_dispatch(rfq_id, user_name, notes)

    def record_response_received(
        self, rfq_id: str, user_name: str, notes: str
    ) -> RFQDocumentRecord:
        """Transition SENT -> RESPONSE_RECEIVED."""
        return self.get_rfq_service().record_response_received(rfq_id, user_name, notes)

    def close_supplier_rfq(
        self, rfq_id: str, user_name: str, reason: str
    ) -> RFQDocumentRecord:
        """Transition to CLOSED."""
        return self.get_rfq_service().close_rfq(rfq_id, user_name, reason)

    def generate_rfq_documents(self, rfq_id: str) -> RFQDocumentRecord:
        """Generate DOCX and PDF documents for an RFQ."""
        return self.get_rfq_service().generate_documents(rfq_id)

    def get_rfq_kpis(self, brief_id: Optional[str] = None) -> Dict[str, int]:
        """Compute pipeline counts across all 7 statuses."""
        return self.get_rfq_service().get_rfq_kpis(brief_id)

    def get_rfq_audit_trail(self, rfq_id: str) -> List[RFQAuditEntry]:
        """Fetch audit trail for an RFQ."""
        return self.repository.get_rfq_audit_trail(rfq_id)

    # =========================================================================
    # PHASE 12: SUPPLIER QUOTATION MANAGEMENT FACADE
    # =========================================================================

    def get_quotation_service(self) -> QuotationService:
        """Initialize or retrieve singleton quotation service instance."""
        if not hasattr(self, "_quotation_service") or self._quotation_service is None:
            self._quotation_service = QuotationService(repository=self.repository)
        return self._quotation_service

    def extract_quotation_draft(
        self, filename: str, content_bytes: bytes
    ) -> ExtractedQuotationDraft:
        """Extract draft parameters from a quotation document without saving."""
        return self.get_quotation_service().extract_quotation_draft(filename, content_bytes)

    def ingest_quotation_file(
        self,
        rfq_id: str,
        filename: str,
        content_bytes: bytes,
        user_name: str,
        **kwargs,
    ) -> SupplierQuotation:
        """Process, validate, extract, and persist an uploaded quotation file."""
        return self.get_quotation_service().ingest_quotation_file(
            rfq_id=rfq_id,
            filename=filename,
            content_bytes=content_bytes,
            user_name=user_name,
            **kwargs,
        )

    def ingest_manual_quotation(
        self,
        rfq_id: str,
        commercial_data: QuotationCommercialData,
        compliance_data: QuotationTechnicalCompliance,
        user_name: str,
        **kwargs,
    ) -> SupplierQuotation:
        """Persist a manually entered supplier quotation."""
        return self.get_quotation_service().ingest_manual_quotation(
            rfq_id=rfq_id,
            commercial_data=commercial_data,
            compliance_data=compliance_data,
            user_name=user_name,
            **kwargs,
        )

    def confirm_and_validate_quotation(
        self,
        quotation_id: str,
        user_name: str,
        confirmed_commercial: QuotationCommercialData,
        confirmed_compliance: Optional[QuotationTechnicalCompliance] = None,
        notes: Optional[str] = None,
    ) -> SupplierQuotation:
        """Human confirmation gate for quotation commercial and compliance parameters."""
        return self.get_quotation_service().confirm_and_validate_quotation(
            quotation_id=quotation_id,
            user_name=user_name,
            confirmed_commercial=confirmed_commercial,
            confirmed_compliance=confirmed_compliance,
            notes=notes,
        )

    def get_quotation(self, quotation_id: str) -> Optional[SupplierQuotation]:
        """Fetch a quotation by ID with its audit history."""
        return self.repository.get_quotation(quotation_id)

    def list_quotations_for_rfq(self, rfq_id: str) -> List[SupplierQuotation]:
        """Fetch all versions of quotations for an RFQ."""
        return self.repository.list_quotations_for_rfq(rfq_id)

    def list_quotations_for_brief(
        self, brief_id: str, latest_only: bool = True
    ) -> List[SupplierQuotation]:
        """Fetch quotations for a sourcing brief."""
        return self.repository.list_quotations_for_brief(brief_id, latest_only=latest_only)

    def get_quotation_audit_trail(self, quotation_id: str) -> List[QuotationAuditEntry]:
        """Fetch chronological audit trail for a quotation."""
        return self.repository.get_quotation_audit_trail(quotation_id)

    def compare_quotations_for_brief(
        self,
        brief_id: str,
        target_currency: str = "USD",
        buyer_fx_overrides: Optional[Dict[str, ExchangeRateRecord]] = None,
    ) -> QuotationComparisonMatrix:
        """Facade method to execute multi-supplier quotation comparison under a brief."""
        return self.get_quotation_service().compare_quotations_for_brief(
            brief_id=brief_id,
            target_currency=target_currency,
            buyer_fx_overrides=buyer_fx_overrides,
        )

    def compare_quotations_for_rfq(
        self,
        rfq_id: str,
        target_currency: str = "USD",
        buyer_fx_overrides: Optional[Dict[str, ExchangeRateRecord]] = None,
    ) -> QuotationComparisonMatrix:
        """Facade method to compare quotation versions under an RFQ."""
        return self.get_quotation_service().compare_quotations_for_rfq(
            rfq_id=rfq_id,
            target_currency=target_currency,
            buyer_fx_overrides=buyer_fx_overrides,
        )

    def reject_quotation(
        self,
        quotation_id: str,
        user_name: str,
        rejection_reason: str,
    ) -> SupplierQuotation:
        """Reject a quotation with mandatory reviewer feedback."""
        return self.get_quotation_service().reject_quotation(
            quotation_id=quotation_id,
            user_name=user_name,
            rejection_reason=rejection_reason,
        )

    def accept_quotation_for_award(
        self,
        quotation_id: str,
        user_name: str,
        award_notes: str,
    ) -> SupplierQuotation:
        """Accept a quotation candidate for award sign-off."""
        return self.get_quotation_service().accept_quotation_for_award(
            quotation_id=quotation_id,
            user_name=user_name,
            award_notes=award_notes,
        )
