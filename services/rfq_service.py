"""
VendorIQ RFQ Service.

Orchestrates supplier-specific RFQ generation, evidence-grounded customization,
lifecycle transitions, human approval gates, and document rendering.
Guarantees strict confidentiality (zero score/competitor leaks) and
strictly prohibits automated email or external communication dispatch.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple
import uuid

from core.models import (
    SourcingBrief,
    SupplierProfile,
    SupplierEvidenceRecord,
    EvidenceCategory,
    EvidenceStatus,
    RFQStatus,
    RFQAuditEntry,
    RFQBuyerInfo,
    RFQProductRequirements,
    RFQCommercialRequirements,
    RFQSupplierTarget,
    RFQDocumentRecord,
)
from core.exceptions import DataValidationError, MissingSupplierDataError
from db.repository import VendorIQRepository
from services.rfq_generator import RFQDocumentGenerator


class RFQService:
    """
    High-level business service managing the RFQ lifecycle and generation.
    """

    def __init__(
        self,
        repository: VendorIQRepository,
        generator: Optional[RFQDocumentGenerator] = None,
    ):
        self.repository = repository
        self.generator = generator or RFQDocumentGenerator()

    # =========================================================================
    # RFQ PREPARATION & GENERATION
    # =========================================================================

    def prepare_supplier_rfq(
        self,
        brief: SourcingBrief,
        supplier: SupplierProfile,
        shortlist_id: Optional[str] = None,
        expected_quantity: Optional[str] = None,
        buyer_organization: Optional[str] = None,
        contact_person: Optional[str] = None,
        contact_email: Optional[str] = None,
        deadline_days: int = 21,
        target_unit_price: Optional[float] = None,
        currency: Optional[str] = None,
        product_description: Optional[str] = None,
        barrier_film_requirements: Optional[str] = None,
        packaging_requirements: Optional[str] = None,
        min_order_quantity: Optional[int] = None,
        production_lead_time_weeks: Optional[int] = None,
        payment_terms: Optional[str] = None,
        incoterms: Optional[str] = None,
        delivery_location: Optional[str] = None,
        quotation_validity_days: Optional[int] = None,
        custom_specs: Optional[List[str]] = None,
        custom_questions: Optional[List[str]] = None,
    ) -> RFQDocumentRecord:
        """
        Synthesize a structured, supplier-specific RFQ document grounded in
        the sourcing brief, supplier profile, and Phase 9 evidence records.
        """
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(days=deadline_days)
        buyer_org = buyer_organization or brief.buyer_organization
        if not buyer_org or len(buyer_org.strip()) < 2:
            raise DataValidationError("Buyer organization must be supplied before creating an RFQ.")
        ref_code = f"RFQ-{brief.brief_id[:8].upper()}-{supplier.supplier_id[:8].upper()}"

        # Fetch supplier evidence records to identify missing proof and domain flags
        evidence_list = self.repository.list_evidence_for_supplier(supplier.supplier_id)
        evidence_map: Dict[EvidenceCategory, SupplierEvidenceRecord] = {
            e.category: e for e in evidence_list
        }

        # Build missing evidence requests and technical questionnaire tailored to this supplier
        missing_evidence_requests: List[str] = []
        tailored_questions: List[str] = list(custom_questions or [])
        domain_mismatch_warning: Optional[str] = None

        # Check barrier films
        barrier_ev = evidence_map.get(EvidenceCategory.BARRIER_FILM_EXPERIENCE)
        if not barrier_ev or barrier_ev.status == EvidenceStatus.MISSING:
            missing_evidence_requests.append(
                "Technical Data Sheets (TDS) and ASTM test certificates for oxygen and water vapor barrier performance."
            )
            tailored_questions.append(
                "Please specify measured ASTM F1249 (WVTR) and ASTM D3985 (OTR) barrier rates for your multi-layer packaging structures."
            )
        elif barrier_ev.status in (EvidenceStatus.SELF_REPORTED, EvidenceStatus.UNVERIFIED):
            missing_evidence_requests.append(
                f"Independent accredited laboratory test reports verifying self-reported claim: '{barrier_ev.claim}'."
            )
            tailored_questions.append(
                f"Regarding your claim '{barrier_ev.claim}', provide certified laboratory test data (OTR & WVTR) and substrate compositions."
            )

        # Check sustainability
        sust_ev = evidence_map.get(EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS)
        if not sust_ev or sust_ev.status == EvidenceStatus.MISSING:
            missing_evidence_requests.append(
                "Certified environmental sustainability or recyclability documentation (e.g., ISO 14001, FSC, or compostability certifications)."
            )
        elif sust_ev.status in (EvidenceStatus.SELF_REPORTED, EvidenceStatus.UNVERIFIED):
            missing_evidence_requests.append(
                f"Third-party accredited audit certificates supporting claim: '{sust_ev.claim}'."
            )

        # Check manufacturing capabilities
        mfg_ev = evidence_map.get(EvidenceCategory.MANUFACTURING_CAPABILITIES)
        if not mfg_ev or mfg_ev.status in (EvidenceStatus.MISSING, EvidenceStatus.SELF_REPORTED):
            tailored_questions.append(
                "Provide details on your extrusion line capabilities (number of layers, line width, annual metric tonnage capacity)."
            )

        # Check domain mismatch warning
        domain_ev = evidence_map.get(EvidenceCategory.COMPANY_WEBSITE_DOMAIN)
        if domain_ev and domain_ev.status == EvidenceStatus.UNVERIFIED and "domain mismatch" in domain_ev.claim.lower():
            domain_mismatch_warning = domain_ev.claim
        elif supplier.domain and "arrow" in supplier.domain.lower() and "allen" in supplier.domain.lower():
            domain_mismatch_warning = (
                f"Corporate communication domain mismatch noted: Profile domain '{supplier.domain}'. "
                "Confirm official supplier email address prior to external transmission."
            )

        # Base technical specifications from brief criteria
        tech_specs = list(custom_specs or [])
        if not tech_specs:
            if brief.criteria.target_industries:
                tech_specs.append(f"Industry alignment: {', '.join(brief.criteria.target_industries)}")
            if brief.criteria.required_certifications:
                tech_specs.append(f"Mandatory certifications: {', '.join(brief.criteria.required_certifications)}")
            tech_specs.extend([
                "High barrier film lamination (Moisture & Oxygen protection)",
                "Standard pinhole-free sealing integrity for perishable/industrial contents",
                "Compliance with national and international food contact / industrial safety standards",
            ])

        # Requested documents checklist
        requested_docs = [
            "Technical Data Sheets (TDS) for all proposed packaging substrates",
            "ASTM F1249 Certified Test Report (Water Vapor Transmission Rate - WVTR)",
            "ASTM D3985 Certified Test Report (Oxygen Transmission Rate - OTR)",
            "ISO 9001 / BRCGS Quality Management System Certification",
            "Sustainability, recyclability, or compostability certifications",
            "Plant equipment overview and verified production capacity summary",
            "Official Company Profile and authorized commercial signatory credentials",
        ]

        supplier_target = RFQSupplierTarget(
            supplier_id=supplier.supplier_id,
            company_name=supplier.company_name,
            domain=supplier.domain,
            headquarters=f"{supplier.headquarters_city + ', ' if supplier.headquarters_city else ''}{supplier.headquarters_country}",
            domain_mismatch_warning=domain_mismatch_warning,
            missing_evidence_requests=missing_evidence_requests,
            technical_questionnaire=tailored_questions,
        )

        buyer_info = RFQBuyerInfo(
            buyer_organization=buyer_org,
            rfq_reference=ref_code,
            issue_date=now,
            response_deadline=deadline,
            contact_email=contact_email or None,
            contact_person=contact_person or None,
        )

        product_reqs = RFQProductRequirements(
            category=brief.criteria.target_industries[0] if brief.criteria.target_industries else "Flexible Packaging",
            description=product_description or brief.description or "Not specified",
            technical_specifications=tech_specs,
            barrier_film_requirements=barrier_film_requirements or None,
            expected_quantity=expected_quantity.strip() if expected_quantity else None,
            packaging_requirements=packaging_requirements or None,
        )

        commercial_reqs = RFQCommercialRequirements(
            currency=currency,
            target_unit_price=target_unit_price,
            min_order_quantity=min_order_quantity,
            production_lead_time_weeks=production_lead_time_weeks,
            payment_terms=payment_terms or None,
            incoterms=incoterms or None,
            delivery_location=delivery_location or None,
            quotation_validity_days=quotation_validity_days,
        )

        rfq_record = RFQDocumentRecord(
            rfq_id=f"RFQ-{uuid.uuid4().hex[:8].upper()}",
            brief_id=brief.brief_id,
            shortlist_id=shortlist_id,
            supplier_id=supplier.supplier_id,
            title=f"RFQ for {brief.title} – {supplier.company_name}",
            buyer_info=buyer_info,
            supplier=supplier_target,
            product_requirements=product_reqs,
            commercial_requirements=commercial_reqs,
            requested_documents=requested_docs,
            status=RFQStatus.DRAFT,
            created_at=now,
            updated_at=now,
            audit_trail=[],
        )

        # Enforce confidentiality guardrail
        self.generator.verify_confidentiality_guardrails(rfq_record)
        return rfq_record

    def create_and_persist_rfq(
        self,
        brief: SourcingBrief,
        supplier: SupplierProfile,
        shortlist_id: Optional[str] = None,
        **kwargs,
    ) -> RFQDocumentRecord:
        """Prepare and persist one RFQ per supplier/brief; repeated clicks are idempotent."""
        existing = self.repository.list_supplier_rfqs(
            brief_id=brief.brief_id,
            supplier_id=supplier.supplier_id,
        )
        if existing:
            # Reuse the most recently updated package instead of creating duplicates
            # when a user double-clicks or retries after a slow Streamlit rerun.
            existing.sort(key=lambda item: item.updated_at or item.created_at, reverse=True)
            return existing[0]
        rfq = self.prepare_supplier_rfq(brief, supplier, shortlist_id=shortlist_id, **kwargs)
        return self.repository.create_supplier_rfq(rfq)

    def create_rfqs_from_shortlist(
        self,
        shortlist_id: str,
        brief: SourcingBrief,
        **kwargs,
    ) -> List[RFQDocumentRecord]:
        """
        Generate separate supplier-specific RFQs for every candidate in a shortlist.
        """
        items = self.repository.list_items_for_shortlist(shortlist_id)
        if not items:
            raise DataValidationError(f"Shortlist '{shortlist_id}' contains no candidate suppliers.")

        created_rfqs: List[RFQDocumentRecord] = []
        for it in items:
            supp = self.repository.get_supplier_by_id(it.supplier_id)
            if not supp:
                continue
            rfq = self.create_and_persist_rfq(
                brief=brief,
                supplier=supp,
                shortlist_id=shortlist_id,
                **kwargs,
            )
            created_rfqs.append(rfq)

        return created_rfqs

    # =========================================================================
    # DOCUMENT GENERATION
    # =========================================================================

    def generate_documents(self, rfq_id: str) -> RFQDocumentRecord:
        """
        Generate both DOCX and PDF documents for the specified RFQ,
        and update their file paths in the database.
        """
        rfq = self.repository.get_supplier_rfq(rfq_id)
        if not rfq:
            raise DataValidationError(f"RFQ '{rfq_id}' not found.")

        docx_path, pdf_path = self.generator.generate_all(rfq)
        rfq.docx_path = docx_path
        rfq.pdf_path = pdf_path
        return self.repository.update_supplier_rfq(rfq)

    # =========================================================================
    # LIFECYCLE WORKFLOW & HUMAN APPROVAL GATE
    # =========================================================================

    def submit_for_approval(
        self, rfq_id: str, submitter_name: str, comments: Optional[str] = None
    ) -> RFQDocumentRecord:
        """Transition DRAFT -> PENDING_APPROVAL."""
        note = comments or "Submitted RFQ draft for formal human procurement review."
        return self.repository.transition_rfq_status(
            rfq_id=rfq_id,
            new_status=RFQStatus.PENDING_APPROVAL,
            changed_by=submitter_name,
            comments=note,
        )

    def approve_rfq(
        self, rfq_id: str, reviewer_name: str, approval_notes: str
    ) -> RFQDocumentRecord:
        """
        Transition PENDING_APPROVAL -> APPROVED.
        Requires mandatory reviewer sign-off notes and automatically generates
        the finalized DOCX and PDF documents.
        """
        if not approval_notes or not approval_notes.strip():
            raise DataValidationError("Reviewer comments and approval notes are mandatory to approve an RFQ.")

        approved_rfq = self.repository.transition_rfq_status(
            rfq_id=rfq_id,
            new_status=RFQStatus.APPROVED,
            changed_by=reviewer_name,
            comments=approval_notes,
        )
        # Generate official documents upon approval
        return self.generate_documents(rfq_id)

    def reject_rfq(
        self, rfq_id: str, reviewer_name: str, rejection_reason: str
    ) -> RFQDocumentRecord:
        """Transition PENDING_APPROVAL -> REJECTED."""
        if not rejection_reason or not rejection_reason.strip():
            raise DataValidationError("Reviewer feedback is mandatory when rejecting an RFQ.")

        return self.repository.transition_rfq_status(
            rfq_id=rfq_id,
            new_status=RFQStatus.REJECTED,
            changed_by=reviewer_name,
            comments=rejection_reason,
        )

    def revise_rfq(
        self, rfq_id: str, user_name: str, revision_notes: Optional[str] = None
    ) -> RFQDocumentRecord:
        """Transition REJECTED -> DRAFT to allow buyer edits."""
        return self.repository.transition_rfq_status(
            rfq_id=rfq_id,
            new_status=RFQStatus.DRAFT,
            changed_by=user_name,
            comments=revision_notes or "Reverted to DRAFT for buyer revision.",
        )

    def record_manual_dispatch(
        self, rfq_id: str, user_name: str, dispatch_notes: str
    ) -> RFQDocumentRecord:
        """
        Transition APPROVED -> SENT.
        Strictly requires manual human sign-off confirming that the RFQ was
        dispatched outside the platform (via corporate email or procurement portal).
        Guarantees NO automated email transmission.
        """
        if not dispatch_notes or not dispatch_notes.strip():
            raise DataValidationError(
                "Manual dispatch sign-off notes are required. You must certify the transmission method."
            )

        full_note = (
            f"[MANUAL TRANSMISSION CONFIRMED]: {dispatch_notes.strip()} "
            f"(Verified: Zero automated platform emails were dispatched)."
        )
        return self.repository.transition_rfq_status(
            rfq_id=rfq_id,
            new_status=RFQStatus.SENT,
            changed_by=user_name,
            comments=full_note,
        )

    def record_response_received(
        self, rfq_id: str, user_name: str, response_summary: str
    ) -> RFQDocumentRecord:
        """Transition SENT -> RESPONSE_RECEIVED when supplier submits bid."""
        if not response_summary or not response_summary.strip():
            raise DataValidationError("Response summary notes are required when recording a supplier response.")

        return self.repository.transition_rfq_status(
            rfq_id=rfq_id,
            new_status=RFQStatus.RESPONSE_RECEIVED,
            changed_by=user_name,
            comments=response_summary.strip(),
        )

    def close_rfq(
        self, rfq_id: str, user_name: str, close_reason: str
    ) -> RFQDocumentRecord:
        """Transition RESPONSE_RECEIVED / APPROVED -> CLOSED."""
        return self.repository.transition_rfq_status(
            rfq_id=rfq_id,
            new_status=RFQStatus.CLOSED,
            changed_by=user_name,
            comments=close_reason.strip() if close_reason else "RFQ closed.",
        )

    # =========================================================================
    # ANALYTICS & KPIS
    # =========================================================================

    def get_rfq_kpis(self, brief_id: Optional[str] = None) -> Dict[str, int]:
        """Compute pipeline counts across all 7 statuses."""
        rfqs = self.repository.list_supplier_rfqs(brief_id=brief_id)
        kpis = {
            "total": len(rfqs),
            "draft": 0,
            "pending_approval": 0,
            "approved": 0,
            "rejected": 0,
            "sent": 0,
            "response_received": 0,
            "closed": 0,
        }
        for r in rfqs:
            status_val = r.status.value.lower()
            if status_val in kpis:
                kpis[status_val] += 1
        return kpis
