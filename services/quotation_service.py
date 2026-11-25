"""
VendorIQ Quotation Service.

High-level business service orchestrating quotation ingestion (PDF, Excel, Manual),
extraction provenance, immutable versioning, human confirmation gates,
and lifecycle audit logging.
"""

from datetime import datetime, timezone, date
import os
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from core.models import (
    QuotationStatus,
    VALID_QUOTATION_TRANSITIONS,
    Incoterm,
    ExchangeRateType,
    ExchangeRateRecord,
    ExtractionMethod,
    ExtractionFieldStatus,
    FieldExtractionProvenance,
    QuotationCommercialData,
    QuotationTechnicalCompliance,
    QuotationAuditEntry,
    SupplierQuotation,
    RFQStatus,
    QuotationComparisonRow,
    QuotationComparisonMatrix,
    ComparisonRecommendationType,
)
from core.exceptions import DataValidationError
from db.repository import VendorIQRepository
from utils.file_storage import (
    validate_quotation_file,
    compute_sha256,
    save_quotation_file,
    MAX_FILE_SIZE_BYTES,
)
from services.quotation_extractor import (
    QuotationDataExtractor,
    ExtractedQuotationDraft,
)
from services.quotation_comparison import (
    CurrencyNormalizationService,
    TechnicalComplianceEvaluator,
    QuotationComparisonEngine,
)


class QuotationService:
    """
    High-level business service for quotation ingestion, extraction, and lifecycle governance.
    """

    def __init__(
        self,
        repository: VendorIQRepository,
        extractor: Optional[QuotationDataExtractor] = None,
        comparison_engine: Optional[QuotationComparisonEngine] = None,
        base_dir: Optional[str] = None,
    ):
        self.repository = repository
        self.extractor = extractor or QuotationDataExtractor()
        self.comparison_engine = comparison_engine or QuotationComparisonEngine()
        self.base_dir = base_dir

    # =========================================================================
    # INGESTION & EXTRACTION WORKFLOW
    # =========================================================================

    def extract_quotation_draft(
        self,
        filename: str,
        content_bytes: bytes,
    ) -> ExtractedQuotationDraft:
        """
        Extract structured draft parameters from an uploaded quotation file
        without persisting to the database. Used for UI preview and human confirmation.
        """
        is_valid, err_msg = validate_quotation_file(filename, content_bytes)
        if not is_valid:
            raise DataValidationError(f"Invalid quotation file: {err_msg}")

        file_hash = compute_sha256(content_bytes)
        return self.extractor.extract_from_file(
            file_bytes=content_bytes,
            filename=filename,
            file_hash_sha256=file_hash,
        )

    def ingest_quotation_file(
        self,
        rfq_id: str,
        filename: str,
        content_bytes: bytes,
        user_name: str,
        brief_id: Optional[str] = None,
        supplier_id: Optional[str] = None,
        company_name: Optional[str] = None,
        manual_overrides: Optional[Dict[str, Any]] = None,
        revision_notes: Optional[str] = None,
    ) -> SupplierQuotation:
        """
        Complete end-to-end ingestion of a quotation file:
        1. Validate file format and size limits.
        2. Compute SHA-256 hash.
        3. Prevent duplicate uploads where appropriate without overwriting historical versions.
        4. Securely store document on filesystem.
        5. Extract commercial parameters and technical compliance.
        6. Apply any buyer overrides.
        7. Persist quotation in SQLite with initial audit entry.
        """
        # Validate parent RFQ
        rfq = self.repository.get_supplier_rfq(rfq_id)
        if not rfq:
            raise DataValidationError(f"Referenced RFQ '{rfq_id}' does not exist.")

        eff_brief_id = brief_id or rfq.brief_id
        eff_supplier_id = supplier_id or rfq.supplier_id
        eff_company_name = company_name or rfq.supplier.company_name

        # Validate file
        is_valid, err_msg = validate_quotation_file(filename, content_bytes)
        if not is_valid:
            raise DataValidationError(f"Quotation file validation error: {err_msg}")

        file_hash = compute_sha256(content_bytes)

        # Requirement 10: Check for duplicate uploads for this RFQ
        existing_quotes = self.repository.list_quotations_for_rfq(rfq_id)
        for eq in existing_quotes:
            if eq.file_hash_sha256 == file_hash:
                # Return existing quotation to prevent duplicate file uploads
                return eq

        # Save document securely
        rel_path, _ = save_quotation_file(
            brief_id=eff_brief_id,
            supplier_id=eff_supplier_id,
            filename=filename,
            content_bytes=content_bytes,
            base_dir=self.base_dir,
        )

        # Extract draft
        draft = self.extractor.extract_from_file(
            file_bytes=content_bytes,
            filename=filename,
            raw_file_path=rel_path,
            file_hash_sha256=file_hash,
        )

        # Apply buyer overrides if provided during ingestion
        if manual_overrides:
            for k, v in manual_overrides.items():
                if hasattr(draft.commercial, k):
                    setattr(draft.commercial, k, v)
                    if k in draft.provenance_map:
                        draft.provenance_map[k].status = ExtractionFieldStatus.CORRECTED
                        draft.provenance_map[k].confirmed_by = user_name
                        draft.provenance_map[k].confirmed_at = datetime.now(timezone.utc)
                elif hasattr(draft.compliance, k):
                    setattr(draft.compliance, k, v)

        # Check if this is an initial submission or revision
        if existing_quotes:
            new_quote = draft.to_supplier_quotation(
                rfq_id=rfq_id,
                supplier_id=eff_supplier_id,
                brief_id=eff_brief_id,
                company_name=eff_company_name,
                version=len(existing_quotes) + 1,
            )
            note = revision_notes or f"New quotation version uploaded via {draft.file_type}: {filename}"
            return self.repository.create_quotation_version(
                rfq_id=rfq_id,
                new_quotation=new_quote,
                changed_by=user_name,
                revision_notes=note,
            )
        else:
            new_quote = draft.to_supplier_quotation(
                rfq_id=rfq_id,
                supplier_id=eff_supplier_id,
                brief_id=eff_brief_id,
                company_name=eff_company_name,
                version=1,
            )
            saved = self.repository.create_quotation(new_quote)

            # Transition parent RFQ to RESPONSE_RECEIVED if in SENT
            if rfq.status == RFQStatus.SENT:
                self.repository.transition_rfq_status(
                    rfq_id=rfq_id,
                    new_status=RFQStatus.RESPONSE_RECEIVED,
                    changed_by=user_name,
                    comments=f"Supplier quotation received ({filename}).",
                )
            return saved

    def ingest_manual_quotation(
        self,
        rfq_id: str,
        commercial_data: QuotationCommercialData,
        compliance_data: QuotationTechnicalCompliance,
        user_name: str,
        reviewer_notes: Optional[str] = None,
        revision_notes: Optional[str] = None,
    ) -> SupplierQuotation:
        """
        Support direct manual quotation entry.
        Marks extraction method as MANUAL_ENTRY.
        """
        rfq = self.repository.get_supplier_rfq(rfq_id)
        if not rfq:
            raise DataValidationError(f"Referenced RFQ '{rfq_id}' does not exist.")

        # Tag provenance as manual
        now = datetime.now(timezone.utc)
        for field_name in ("quoted_unit_price", "quoted_quantity", "lead_time_weeks", "payment_terms", "incoterms"):
            commercial_data.provenance_map[field_name] = FieldExtractionProvenance(
                field_name=field_name,
                raw_value=str(getattr(commercial_data, field_name, "")),
                source_file="Manual Buyer Input",
                extraction_method=ExtractionMethod.MANUAL_ENTRY,
                status=ExtractionFieldStatus.CONFIRMED,
                confirmed_by=user_name,
                confirmed_at=now,
                confidence=1.0,
            )
        commercial_data.critical_fields_confirmed = True
        commercial_data.calculate_landed_cost()

        existing_quotes = self.repository.list_quotations_for_rfq(rfq_id)
        if existing_quotes:
            quote = SupplierQuotation(
                rfq_id=rfq_id,
                supplier_id=rfq.supplier_id,
                brief_id=rfq.brief_id,
                company_name=rfq.supplier.company_name,
                version=len(existing_quotes) + 1,
                status=QuotationStatus.UNDER_REVIEW,
                file_type="MANUAL",
                commercial=commercial_data,
                compliance=compliance_data,
                reviewer_notes=reviewer_notes,
            )
            note = revision_notes or "Manual quotation revision recorded."
            return self.repository.create_quotation_version(
                rfq_id=rfq_id,
                new_quotation=quote,
                changed_by=user_name,
                revision_notes=note,
            )
        else:
            quote = SupplierQuotation(
                rfq_id=rfq_id,
                supplier_id=rfq.supplier_id,
                brief_id=rfq.brief_id,
                company_name=rfq.supplier.company_name,
                version=1,
                status=QuotationStatus.UNDER_REVIEW,
                file_type="MANUAL",
                commercial=commercial_data,
                compliance=compliance_data,
                reviewer_notes=reviewer_notes,
            )
            saved = self.repository.create_quotation(quote)
            if rfq.status == RFQStatus.SENT:
                self.repository.transition_rfq_status(
                    rfq_id=rfq_id,
                    new_status=RFQStatus.RESPONSE_RECEIVED,
                    changed_by=user_name,
                    comments="Manual supplier quotation recorded.",
                )
            return saved

    # =========================================================================
    # HUMAN CONFIRMATION & REVIEW WORKFLOW
    # =========================================================================

    def confirm_and_validate_quotation(
        self,
        quotation_id: str,
        user_name: str,
        confirmed_commercial: QuotationCommercialData,
        confirmed_compliance: Optional[QuotationTechnicalCompliance] = None,
        notes: Optional[str] = None,
    ) -> SupplierQuotation:
        """
        Human confirmation gate: validates that critical commercial fields have been
        reviewed and verified by the buyer before side-by-side comparison.
        Transitions quotation status from PARSED to UNDER_REVIEW.
        """
        quotation = self.repository.get_quotation(quotation_id)
        if not quotation:
            raise DataValidationError(f"Quotation '{quotation_id}' not found.")

        # Human confirmation validation gate:
        # Enforce that critical commercial parameters have valid, positive, non-placeholder values
        if confirmed_commercial.quoted_unit_price <= 0.0:
            raise DataValidationError(
                "Cannot confirm quotation: Critical commercial field 'quoted_unit_price' must be greater than 0.0. "
                "Explicit buyer input is required."
            )
        if confirmed_commercial.quoted_quantity <= 0:
            raise DataValidationError(
                "Cannot confirm quotation: Critical commercial field 'quoted_quantity' must be greater than 0. "
                "Explicit buyer input is required."
            )
        if confirmed_commercial.lead_time_weeks <= 0:
            raise DataValidationError(
                "Cannot confirm quotation: Critical commercial field 'lead_time_weeks' must be greater than 0. "
                "Explicit buyer input is required."
            )
        if not confirmed_commercial.payment_terms or not confirmed_commercial.payment_terms.strip() or "unspecified" in confirmed_commercial.payment_terms.lower():
            raise DataValidationError(
                "Cannot confirm quotation: Critical commercial field 'payment_terms' is missing or unspecified. "
                "Explicit buyer input is required."
            )

        now = datetime.now(timezone.utc)

        # Ensure all critical commercial fields are represented in the provenance map
        for crit_fld in ("quoted_unit_price", "quoted_quantity", "lead_time_weeks", "payment_terms", "incoterms"):
            if crit_fld not in confirmed_commercial.provenance_map:
                confirmed_commercial.provenance_map[crit_fld] = FieldExtractionProvenance(
                    field_name=crit_fld,
                    raw_value=str(getattr(confirmed_commercial, crit_fld, "")),
                    source_file="Human Buyer Input",
                    extraction_method=ExtractionMethod.BUYER_VERIFIED,
                    status=ExtractionFieldStatus.CONFIRMED,
                    confirmed_by=user_name,
                    confirmed_at=now,
                    confidence=1.0,
                )

        # Update commercial and compliance parameters
        confirmed_commercial.critical_fields_confirmed = True
        confirmed_commercial.calculate_landed_cost()
        quotation.commercial = confirmed_commercial

        if confirmed_compliance:
            quotation.compliance = confirmed_compliance

        for _, prov in quotation.commercial.provenance_map.items():
            prov.status = ExtractionFieldStatus.CONFIRMED
            prov.confirmed_by = user_name
            prov.confirmed_at = now

        # Update DB record
        self.repository.update_quotation(quotation)

        # Transition status to UNDER_REVIEW if currently PARSED or RECEIVED
        if quotation.status in (QuotationStatus.PARSED, QuotationStatus.RECEIVED):
            note = notes or "Critical commercial and technical inputs verified and confirmed by buyer."
            return self.repository.transition_quotation_status(
                quotation_id=quotation_id,
                new_status=QuotationStatus.UNDER_REVIEW,
                changed_by=user_name,
                comments=note,
                action="CONFIRM_FIELDS",
            )

        return self.repository.confirm_critical_fields(quotation_id, user_name, notes)

    def reject_quotation(
        self,
        quotation_id: str,
        user_name: str,
        rejection_reason: str,
    ) -> SupplierQuotation:
        """Transition quotation to REJECTED with mandatory reviewer comments."""
        if not rejection_reason or not rejection_reason.strip():
            raise DataValidationError("Reviewer feedback is mandatory when rejecting a quotation.")

        return self.repository.transition_quotation_status(
            quotation_id=quotation_id,
            new_status=QuotationStatus.REJECTED,
            changed_by=user_name,
            comments=rejection_reason.strip(),
            action="REJECT_QUOTATION",
        )

    def accept_quotation_for_award(
        self,
        quotation_id: str,
        user_name: str,
        award_notes: str,
    ) -> SupplierQuotation:
        """
        Transition quotation to ACCEPTED with mandatory reviewer sign-off.
        Strictly reminds the buyer that PO creation must be performed manually.
        """
        if not award_notes or not award_notes.strip():
            raise DataValidationError("Award rationale is mandatory when accepting a quotation.")

        full_note = (
            f"[AWARD CANDIDATE ACCEPTED]: {award_notes.strip()} "
            f"(Governance Notice: Zero automated POs were created. Manual contract execution required)."
        )
        return self.repository.transition_quotation_status(
            quotation_id=quotation_id,
            new_status=QuotationStatus.ACCEPTED,
            changed_by=user_name,
            comments=full_note,
            action="AWARD_ACCEPTED",
        )

    # =========================================================================
    # QUOTATION COMPARISON & ANALYSIS WORKFLOW
    # =========================================================================

    def compare_quotations_for_brief(
        self,
        brief_id: str,
        target_currency: str = "USD",
        buyer_fx_overrides: Optional[Dict[str, ExchangeRateRecord]] = None,
    ) -> QuotationComparisonMatrix:
        """
        Compare active quotations submitted across all suppliers for a sourcing brief.
        Normalizes currencies, evaluates landed costs, checks technical compliance,
        and generates deterministic recommendations.
        """
        brief = self.repository.get_brief(brief_id)
        if not brief:
            raise DataValidationError(f"Sourcing brief '{brief_id}' not found.")

        # Fetch latest confirmed or active quotations for the brief
        quotes = self.repository.list_quotations_for_brief(brief_id, latest_only=True)
        return self.comparison_engine.compare_quotations(
            quotations=quotes,
            brief=brief,
            target_currency=target_currency,
            buyer_fx_overrides=buyer_fx_overrides,
        )

    def compare_quotations_for_rfq(
        self,
        rfq_id: str,
        target_currency: str = "USD",
        buyer_fx_overrides: Optional[Dict[str, ExchangeRateRecord]] = None,
    ) -> QuotationComparisonMatrix:
        """
        Compare quotation versions submitted for a specific supplier RFQ.
        """
        rfq = self.repository.get_supplier_rfq(rfq_id)
        if not rfq:
            raise DataValidationError(f"Supplier RFQ '{rfq_id}' not found.")

        brief = self.repository.get_brief(rfq.brief_id)
        if not brief:
            raise DataValidationError(f"Parent brief '{rfq.brief_id}' for RFQ '{rfq_id}' not found.")

        quotes = self.repository.list_quotations_for_rfq(rfq_id)
        return self.comparison_engine.compare_quotations(
            quotations=quotes,
            brief=brief,
            target_currency=target_currency,
            buyer_fx_overrides=buyer_fx_overrides,
        )
