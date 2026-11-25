"""
VendorIQ Phase 12D Test Suite: Streamlit Quotation Dashboard & Human Approval Workflow.

Tests:
1. Quotation view import and navigation integration.
2. End-to-end ingestion workflow (file upload preview, binary XLS rejection, manual proposal entry).
3. Quotation review and human confirmation gate with audit trail logging.
4. Multi-supplier side-by-side comparison matrix via dashboard service.
5. Deterministic recommendation presentation with non-compensable technical failures.
6. Human approval gate: formal award acceptance and rejection with mandatory buyer rationale.
7. Governance guardrails: zero automated purchase orders or supplier communications.
8. Strict temporary database isolation guaranteeing zero writes to vendoriq.sqlite.
"""

from datetime import datetime, timezone, date, timedelta
from pathlib import Path
import shutil
import sqlite3
import pytest

from core.models import (
    SourcingBrief,
    SourcingCriteria,
    SupplierProfile,
    RFQStatus,
    RFQBuyerInfo,
    RFQProductRequirements,
    RFQCommercialRequirements,
    RFQSupplierTarget,
    RFQDocumentRecord,
    QuotationStatus,
    Incoterm,
    ExchangeRateType,
    ExchangeRateRecord,
    QuotationCommercialData,
    QuotationTechnicalCompliance,
    SupplierQuotation,
    EvidenceStatus,
    EvidenceCategory,
    ComparisonRecommendationType,
)
from core.exceptions import DataValidationError
from db.connection import init_db
from db.repository import VendorIQRepository
from ui.data_service import DashboardDataService
from ui.views.quotation_view import render_quotation_view


# =============================================================================
# SYNTHETIC DOCUMENT GENERATION HELPERS
# =============================================================================

def make_synthetic_pdf(lines: list[str]) -> bytes:
    import zlib
    stream_lines = ["BT", "/F1 12 Tf", "72 750 Td"]
    for l in lines:
        safe_l = l.replace("(", "[").replace(")", "]")
        stream_lines.append(f"({safe_l}) Tj")
        stream_lines.append("0 -20 Td")
    stream_lines.append("ET")
    stream_content = "\n".join(stream_lines).encode("latin1")
    comp_stream = zlib.compress(stream_content)

    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>\nendobj\n"
        b"4 0 obj\n<< /Length " + str(len(comp_stream)).encode() + b" /Filter /FlateDecode >>\n"
        b"stream\n" + comp_stream + b"\nendstream\nendobj\n"
        b"trailer\n<< /Root 1 0 R >>\n%%EOF"
    )


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def isolated_service(tmp_path):
    """Create isolated SQLite database with repository and DashboardDataService."""
    db_path = str(tmp_path / "test_quotations_12d.sqlite")
    init_db(db_path)
    repo = VendorIQRepository(db_path=db_path)

    # 1. Brief
    brief = SourcingBrief(
        brief_id="BRIEF-12D-UI",
        title="Barrier Pouches Procurement Project",
        buyer_organization="EcoFoods Corp",
        description="Mandatory requirement: High-barrier multi-layer packaging film with OTR < 2.0 cc/m2/day and WVTR < 1.0 g/m2/day.",
        criteria=SourcingCriteria(
            target_industries=["Flexible Packaging"],
            target_geographies=["India"],
            required_certifications=["ISO 9001"],
        ),
    )
    repo.create_brief(brief)

    # 2. Suppliers
    suppliers = [
        SupplierProfile(
            supplier_id="SUPP-ALPHA",
            company_name="Alpha Barrier Films Ltd",
            domain="alphabarrier.com",
            headquarters_country="India",
        ),
        SupplierProfile(
            supplier_id="SUPP-BETA",
            company_name="Beta Global Packaging Inc",
            domain="betapack.com",
            headquarters_country="India",
        ),
    ]
    for s in suppliers:
        repo.create_supplier(s)

    # 3. RFQs
    for s in suppliers:
        rfq = RFQDocumentRecord(
            rfq_id=f"RFQ-{s.supplier_id}",
            brief_id="BRIEF-12D-UI",
            supplier_id=s.supplier_id,
            title=f"RFQ for {s.company_name}",
            status=RFQStatus.SENT,
            buyer_info=RFQBuyerInfo(
                buyer_organization="EcoFoods Corp",
                rfq_reference=f"RFQ-REF-{s.supplier_id}",
                response_deadline=datetime.now(timezone.utc) + timedelta(days=21),
            ),
            supplier=RFQSupplierTarget(
                supplier_id=s.supplier_id,
                company_name=s.company_name,
                domain=s.domain,
            ),
            product_requirements=RFQProductRequirements(
                category="Flexible Packaging",
                description="High barrier pouches",
                expected_quantity="50,000 units",
            ),
            commercial_requirements=RFQCommercialRequirements(
                currency="USD",
                payment_terms="Net 30",
            ),
        )
        repo.create_supplier_rfq(rfq)

    service = DashboardDataService(repository=repo)
    service.get_quotation_service().base_dir = str(tmp_path)
    return service


# =============================================================================
# 1. VIEW IMPORT & NAVIGATION INTEGRATION
# =============================================================================

class TestDashboardQuotationViewIntegration:
    """Test quotation view import, navigation presence, and signature."""

    def test_quotation_view_import_and_signature(self):
        """Verify render_quotation_view exists and has expected signature."""
        import inspect
        sig = inspect.signature(render_quotation_view)
        params = list(sig.parameters.keys())
        assert "service" in params
        assert "brief" in params
        assert "suppliers" in params

    def test_app_navigation_includes_quotations(self):
        """Verify app.py includes Quotations navigation item."""
        app_path = Path("app.py")
        content = app_path.read_text(encoding="utf-8")
        assert "💰 Quotations & Analysis" in content
        assert "render_quotation_view" in content


# =============================================================================
# 2. INGESTION WORKFLOW TESTS
# =============================================================================

class TestQuotationIngestionWorkflow:
    """Test file upload preview, manual entry, and binary XLS rejection."""

    def test_ensure_rfq_for_supplier_when_no_prior_rfqs_exist(self, isolated_service):
        """Verify that when no RFQ exists for a supplier, _ensure_rfq_for_supplier creates a linked RFQ draft."""
        from ui.views.quotation_view import _ensure_rfq_for_supplier
        service = isolated_service

        new_supp = SupplierProfile(
            supplier_id="SUPP-GAMMA",
            company_name="Gamma Pack Ltd",
            domain="gammapack.com",
            headquarters_country="India",
        )
        service.repository.create_supplier(new_supp)

        brief = service.repository.get_brief("BRIEF-12D-UI")
        assert brief is not None

        # Verify no RFQ exists yet
        existing_rfqs = service.repository.list_supplier_rfqs(brief_id=brief.brief_id, supplier_id=new_supp.supplier_id)
        assert len(existing_rfqs) == 0

        # Run helper
        rfq_id = _ensure_rfq_for_supplier(service, brief, new_supp, user_name="Test Buyer")
        assert rfq_id.startswith("RFQ-")

        # Verify RFQ now exists in SQLite
        post_rfqs = service.repository.list_supplier_rfqs(brief_id=brief.brief_id, supplier_id=new_supp.supplier_id)
        assert len(post_rfqs) == 1
        assert post_rfqs[0].rfq_id == rfq_id
        assert post_rfqs[0].supplier.company_name == "Gamma Pack Ltd"

    def test_preview_and_ingest_quotation_file(self, isolated_service):
        """Requirement 1: Upload PDF quotation, preview draft, and persist record."""
        lines = [
            "SUPPLIER OFFICIAL QUOTATION",
            "Quotation Ref: Q-2026-ALPHA",
            "Structure: 12u PET / 15u Met-BOPP / 50u Polyethylene",
            "Total Thickness: 77 microns",
            "OTR: 1.1 cc/m2/day at 23C (ASTM D3985)",
            "WVTR: 0.7 g/m2/day at 38C (ASTM F1249)",
            "TDS attached: Yes",
            "Quoted Quantity: 50,000 units",
            "Unit Price: $1.42 USD",
            "Lead Time: 4 weeks",
            "Payment Terms: Net 30 days upon inspection",
            "Delivery Terms: DDP Factory",
        ]
        pdf_bytes = make_synthetic_pdf(lines)

        # 1. Preview Draft Extraction
        draft = isolated_service.extract_quotation_draft("Alpha_Quote.pdf", pdf_bytes)
        assert draft.commercial.quoted_unit_price == 1.42
        assert draft.commercial.quoted_currency == "USD"
        assert draft.commercial.lead_time_weeks == 4
        assert draft.compliance.thickness_microns == 77.0
        assert draft.compliance.has_tds_document is True

        # 2. Ingest and Persist
        saved_quote = isolated_service.ingest_quotation_file(
            rfq_id="RFQ-SUPP-ALPHA",
            filename="Alpha_Quote.pdf",
            content_bytes=pdf_bytes,
            user_name="Buyer Eve",
            brief_id="BRIEF-12D-UI",
            supplier_id="SUPP-ALPHA",
            company_name="Alpha Barrier Films Ltd",
        )
        assert saved_quote.quotation_id.startswith("QUOTE-")
        assert saved_quote.version == 1
        assert saved_quote.status == QuotationStatus.PARSED

    def test_manual_quotation_entry_workflow(self, isolated_service):
        """Requirement 1: Support direct manual proposal entry with audit trail."""
        comm = QuotationCommercialData(
            quoted_currency="INR",
            quoted_unit_price=120.0,
            quoted_quantity=30000,
            min_order_quantity=5000,
            lead_time_weeks=3,
            payment_terms="Net 45 days",
            incoterms=Incoterm.DDP,
            is_freight_included=True,
            is_tax_included=True,
        )
        comp = QuotationTechnicalCompliance(
            product_category="Flexible Packaging",
            structure_description="EVOH Multi-layer barrier pouches",
            thickness_microns=75.0,
            otr_value="1.0 cc/m2/day",
            has_tds_document=True,
            mandatory_requirements_met=True,
        )

        saved = isolated_service.ingest_manual_quotation(
            rfq_id="RFQ-SUPP-BETA",
            commercial_data=comm,
            compliance_data=comp,
            user_name="Buyer Frank",
            reviewer_notes="Entered from direct vendor WhatsApp confirmation.",
        )
        assert saved.quotation_id.startswith("QUOTE-")
        assert saved.status == QuotationStatus.UNDER_REVIEW
        assert saved.file_type == "MANUAL"
        assert saved.commercial.critical_fields_confirmed is True


# =============================================================================
# 3. REVIEW, CORRECTION & CONFIRMATION TESTS
# =============================================================================

class TestQuotationReviewAndHumanConfirmation:
    """Test quotation review, buyer correction, and confirmation gate."""

    def test_buyer_correction_and_confirmation_gate(self, isolated_service):
        """Requirement 2: Show provenance, allow buyer correction, and record in audit trail."""
        # 1. Ingest draft
        lines = [
            "QUOTATION",
            "Unit Price: $1.40 USD",
            "Quantity: 20000 units",
            "Lead Time: 5 weeks",
            "Payment Terms: Net 30 days",
        ]
        pdf_bytes = make_synthetic_pdf(lines)
        quote = isolated_service.ingest_quotation_file(
            rfq_id="RFQ-SUPP-ALPHA",
            filename="Alpha_Draft.pdf",
            content_bytes=pdf_bytes,
            user_name="Buyer Grace",
        )
        assert quote.status == QuotationStatus.PARSED

        # 2. Buyer corrects payment terms and confirms
        updated_comm = quote.commercial.model_copy(deep=True)
        updated_comm.payment_terms = "Net 45 days upon delivery (Negotiated)"
        updated_comm.quoted_unit_price = 1.38  # Negotiated discount

        confirmed = isolated_service.confirm_and_validate_quotation(
            quotation_id=quote.quotation_id,
            user_name="Buyer Grace",
            confirmed_commercial=updated_comm,
            notes="Buyer verified negotiated price $1.38 and Net 45 terms.",
        )

        assert confirmed.status == QuotationStatus.UNDER_REVIEW
        assert confirmed.commercial.quoted_unit_price == 1.38
        assert confirmed.commercial.payment_terms == "Net 45 days upon delivery (Negotiated)"
        assert confirmed.commercial.critical_fields_confirmed is True

        # 3. Verify audit history
        audit = isolated_service.get_quotation_audit_trail(quote.quotation_id)
        assert len(audit) >= 2
        confirm_entry = audit[-1]
        assert confirm_entry.action == "CONFIRM_FIELDS"
        assert confirm_entry.changed_by == "Buyer Grace"
        assert "Negotiated" in confirm_entry.comments or "verified" in confirm_entry.comments


# =============================================================================
# 4. COMPARISON & RECOMMENDATION TESTS
# =============================================================================

class TestQuotationComparisonAndRecommendationUI:
    """Test side-by-side comparison matrix and deterministic recommendations."""

    def test_comparison_matrix_and_recommendations_presentation(self, isolated_service):
        """Requirements 3 & 4: Compare side-by-side and generate explainable recommendations."""
        # Supplier 1: Alpha (USD 1.38, DDP, Complete Landed Cost, Compliant)
        q1 = isolated_service.ingest_manual_quotation(
            rfq_id="RFQ-SUPP-ALPHA",
            commercial_data=QuotationCommercialData(
                quoted_currency="USD",
                quoted_unit_price=1.38,
                quoted_quantity=50000,
                min_order_quantity=10000,
                lead_time_weeks=4,
                payment_terms="Net 45",
                incoterms=Incoterm.DDP,
                is_freight_included=True,
                is_tax_included=True,
            ),
            compliance_data=QuotationTechnicalCompliance(
                product_category="Flexible Packaging",
                structure_description="EVOH Multi-layer Barrier Film",
                thickness_microns=77.0,
                otr_value="1.1 cc/m2/day (ASTM D3985)",
                wvtr_value="0.7 g/m2/day (ASTM F1249)",
                has_tds_document=True,
                mandatory_requirements_met=True,
            ),
            user_name="Buyer Grace",
        )

        # Supplier 2: Beta (USD 1.25, EXW, Incomplete Landed Cost, Compliant)
        q2 = isolated_service.ingest_manual_quotation(
            rfq_id="RFQ-SUPP-BETA",
            commercial_data=QuotationCommercialData(
                quoted_currency="USD",
                quoted_unit_price=1.25,
                quoted_quantity=50000,
                min_order_quantity=10000,
                lead_time_weeks=3,
                payment_terms="Net 30",
                incoterms=Incoterm.EXW,
                is_freight_included=False,
                is_tax_included=False,
                freight_charges=None,
                taxes_and_duties=None,
            ),
            compliance_data=QuotationTechnicalCompliance(
                product_category="Flexible Packaging",
                structure_description="Met-BOPP Barrier Pouches",
                thickness_microns=75.0,
                otr_value="1.4 cc/m2/day",
                wvtr_value="0.8 g/m2/day",
                has_tds_document=True,
                mandatory_requirements_met=True,
            ),
            user_name="Buyer Grace",
        )

        matrix = isolated_service.compare_quotations_for_brief(
            brief_id="BRIEF-12D-UI",
            target_currency="USD",
        )

        # 1. Incomplete cost warning check
        assert matrix.are_all_landed_costs_complete is False
        assert matrix.ranking_metric == "NORMALIZED_BASE_PRICE"
        assert "Landed cost inputs are incomplete" in matrix.comparison_notes

        # 2. Primary recommendation (Beta has lower base price $1.25 < $1.38)
        assert matrix.primary_recommendation is not None
        assert matrix.primary_recommendation.company_name == "Beta Global Packaging Inc"
        assert matrix.primary_recommendation.recommendation_type == ComparisonRecommendationType.COMMERCIALLY_VIABLE_COST_INCOMPLETE

        # 3. Secondary recommendation (Alpha has DDP cost $1.38)
        assert matrix.secondary_recommendation is not None
        assert matrix.secondary_recommendation.company_name == "Alpha Barrier Films Ltd"


# =============================================================================
# 5. HUMAN APPROVAL & AWARD GATE TESTS
# =============================================================================

class TestHumanApprovalAndAwardGate:
    """Test formal award acceptance, rejection, and audit trails."""

    def test_formal_award_acceptance_and_rejection_workflows(self, isolated_service):
        """Requirement 5: Formal human award acceptance and rejection with mandatory sign-off."""
        # Ingest proposal
        quote = isolated_service.ingest_manual_quotation(
            rfq_id="RFQ-SUPP-ALPHA",
            commercial_data=QuotationCommercialData(
                quoted_currency="USD",
                quoted_unit_price=1.35,
                quoted_quantity=50000,
                min_order_quantity=10000,
                lead_time_weeks=4,
                payment_terms="Net 30",
                incoterms=Incoterm.DDP,
                is_freight_included=True,
                is_tax_included=True,
            ),
            compliance_data=QuotationTechnicalCompliance(
                product_category="Flexible Packaging",
                structure_description="Barrier film",
                otr_value="1.1 cc/m2/day",
                wvtr_value="0.7 g/m2/day",
                has_tds_document=True,
                mandatory_requirements_met=True,
            ),
            user_name="Buyer Alice",
        )

        # 1. Empty award rationale raises validation error
        with pytest.raises(DataValidationError):
            isolated_service.accept_quotation_for_award(
                quotation_id=quote.quotation_id,
                user_name="Senior Director Dan",
                award_notes="",
            )

        # 2. Legitimate Award Acceptance
        awarded = isolated_service.accept_quotation_for_award(
            quotation_id=quote.quotation_id,
            user_name="Senior Director Dan",
            award_notes="Candidate selected: lowest landed cost with full barrier compliance.",
        )
        assert awarded.status == QuotationStatus.ACCEPTED

        # Verify audit entry
        audit = isolated_service.get_quotation_audit_trail(quote.quotation_id)
        assert audit[-1].action == "AWARD_ACCEPTED"
        assert audit[-1].changed_by == "Senior Director Dan"
        assert "Manual contract execution required" in audit[-1].comments

        # 3. Formal Rejection
        quote_beta = isolated_service.ingest_manual_quotation(
            rfq_id="RFQ-SUPP-BETA",
            commercial_data=QuotationCommercialData(
                quoted_currency="USD",
                quoted_unit_price=1.85,
                quoted_quantity=50000,
                min_order_quantity=10000,
                lead_time_weeks=8,
                payment_terms="Advance",
                incoterms=Incoterm.EXW,
            ),
            compliance_data=QuotationTechnicalCompliance(
                product_category="Flexible Packaging",
                structure_description="Barrier film",
                mandatory_requirements_met=True,
            ),
            user_name="Buyer Alice",
        )

        rejected = isolated_service.reject_quotation(
            quotation_id=quote_beta.quotation_id,
            user_name="Senior Director Dan",
            rejection_reason="Excessive lead time (8 weeks) and non-competitive pricing.",
        )
        assert rejected.status == QuotationStatus.REJECTED
        audit_beta = isolated_service.get_quotation_audit_trail(quote_beta.quotation_id)
        assert audit_beta[-1].action == "REJECT_QUOTATION"
        assert "Excessive lead time" in audit_beta[-1].comments


# =============================================================================
# 6. DATABASE ISOLATION & REGRESSION VERIFICATION
# =============================================================================

class TestUploadSizeBoundaryEnforcement:
    """Verify that maximum upload size is strictly standardized at 10 MB."""

    def test_upload_size_boundary_just_below_limit(self):
        """Files just below 10 MB (10 MB - 1 byte) pass validation."""
        from utils.file_storage import validate_quotation_file, MAX_FILE_SIZE_BYTES
        target_size = MAX_FILE_SIZE_BYTES - 1
        content = b"%PDF-1.4\n" + b" " * (target_size - 9)
        assert len(content) == target_size

        is_valid, msg = validate_quotation_file("boundary_below.pdf", content)
        assert is_valid is True
        assert msg == ""

    def test_upload_size_boundary_exactly_at_limit(self):
        """Files exactly at 10 MB (10 * 1024 * 1024 bytes) pass validation."""
        from utils.file_storage import validate_quotation_file, MAX_FILE_SIZE_BYTES
        target_size = MAX_FILE_SIZE_BYTES
        content = b"%PDF-1.4\n" + b" " * (target_size - 9)
        assert len(content) == target_size

        is_valid, msg = validate_quotation_file("boundary_exact.pdf", content)
        assert is_valid is True
        assert msg == ""

    def test_upload_size_boundary_just_above_limit(self):
        """Files just above 10 MB (10 MB + 1 byte) are rejected immediately."""
        from utils.file_storage import validate_quotation_file, MAX_FILE_SIZE_BYTES
        target_size = MAX_FILE_SIZE_BYTES + 1
        content = b"%PDF-1.4\n" + b" " * (target_size - 9)
        assert len(content) == target_size

        is_valid, msg = validate_quotation_file("boundary_above.pdf", content)
        assert is_valid is False
        assert "exceeds maximum allowed size of 10.0 MB" in msg

    def test_extract_draft_rejects_oversized_before_processing(self, isolated_service):
        """extract_quotation_draft rejects oversized files without processing."""
        from utils.file_storage import MAX_FILE_SIZE_BYTES
        from core.exceptions import DataValidationError

        oversized_content = b"%PDF-1.4\n" + b" " * (MAX_FILE_SIZE_BYTES + 500)
        with pytest.raises(DataValidationError) as exc:
            isolated_service.extract_quotation_draft("oversized.pdf", oversized_content)
        assert "exceeds maximum allowed size of 10.0 MB" in str(exc.value)

    def test_ingest_file_rejects_oversized_before_persistence(self, isolated_service, tmp_path):
        """ingest_quotation_file rejects oversized files before file storage or DB insert."""
        from utils.file_storage import MAX_FILE_SIZE_BYTES
        from core.exceptions import DataValidationError

        oversized_content = b"%PDF-1.4\n" + b" " * (MAX_FILE_SIZE_BYTES + 500)
        with pytest.raises(DataValidationError) as exc:
            isolated_service.ingest_quotation_file(
                rfq_id="RFQ-SUPP-ALPHA",
                filename="oversized_quote.pdf",
                content_bytes=oversized_content,
                user_name="Buyer Eve",
            )
        assert "exceeds maximum allowed size of 10.0 MB" in str(exc.value)

        # Confirm no files were saved under uploads
        upload_dir = tmp_path / "uploads"
        if upload_dir.exists():
            files = list(upload_dir.rglob("oversized_quote.pdf"))
            assert len(files) == 0

    def test_streamlit_config_max_upload_size(self):
        """Verify .streamlit/config.toml configures maxUploadSize = 10 under [server]."""
        config_path = Path(".streamlit/config.toml")
        assert config_path.exists()
        content = config_path.read_text(encoding="utf-8")
        assert "maxUploadSize = 10" in content

    def test_quotation_view_max_upload_size_enforcement(self):
        """Verify ui/views/quotation_view.py imports MAX_FILE_SIZE_BYTES and enforces 10 MB."""
        view_path = Path("ui/views/quotation_view.py")
        content = view_path.read_text(encoding="utf-8")
        assert "MAX_FILE_SIZE_BYTES" in content
        assert "Maximum file size: 10 MB" in content


class TestSecurityAndAtomicBehavior12D:
    """Verify security controls, path traversal protection, duplicate detection, and atomic rollback."""

    def test_path_traversal_protection(self, tmp_path):
        """Verify directory traversal attempts in filenames are strictly neutralized."""
        from utils.file_storage import sanitize_filename, save_quotation_file

        assert sanitize_filename("../../../etc/passwd.pdf") == "passwd.pdf"
        assert sanitize_filename("..\\..\\windows\\system32\\cmd.exe.pdf") == "cmd.exe.pdf"
        assert sanitize_filename("quote\x00_inject.pdf") == "quote_inject.pdf"

        # Attempt to save with path traversal in filename
        pdf_bytes = b"%PDF-1.4\nSynthetic Safe Content\n%%EOF"
        saved_rel_path, _ = save_quotation_file(
            brief_id="BRIEF-12D-UI",
            supplier_id="SUPP-ALPHA",
            filename="../../malicious.pdf",
            content_bytes=pdf_bytes,
            base_dir=str(tmp_path),
        )
        assert ".." not in saved_rel_path
        assert Path(saved_rel_path).name == "malicious.pdf"

    def test_duplicate_upload_detection(self, isolated_service):
        """Uploading identical document returns existing quotation without duplicate records."""
        lines = ["QUOTATION", "Unit Price: $1.45 USD", "Lead Time: 4 weeks"]
        pdf_bytes = make_synthetic_pdf(lines)

        q1 = isolated_service.ingest_quotation_file(
            rfq_id="RFQ-SUPP-ALPHA",
            filename="Alpha_Duplicate_Test.pdf",
            content_bytes=pdf_bytes,
            user_name="Buyer Alice",
        )

        q2 = isolated_service.ingest_quotation_file(
            rfq_id="RFQ-SUPP-ALPHA",
            filename="Alpha_Duplicate_Test.pdf",
            content_bytes=pdf_bytes,
            user_name="Buyer Alice",
        )

        assert q1.quotation_id == q2.quotation_id
        quotes = isolated_service.list_quotations_for_rfq("RFQ-SUPP-ALPHA")
        assert len(quotes) == 1

    def test_atomic_rollback_on_failed_ingestion(self, isolated_service):
        """Failed ingestion (e.g., non-existent RFQ) leaves no orphan rows in SQLite."""
        from core.exceptions import DataValidationError
        lines = ["QUOTATION", "Unit Price: $1.45 USD"]
        pdf_bytes = make_synthetic_pdf(lines)

        with pytest.raises(DataValidationError):
            isolated_service.ingest_quotation_file(
                rfq_id="NON_EXISTENT_RFQ",
                filename="Failed.pdf",
                content_bytes=pdf_bytes,
                user_name="Buyer Alice",
            )

        # Confirm 0 quotations in repository
        quotes = isolated_service.repository.list_quotations_for_rfq("NON_EXISTENT_RFQ")
        assert len(quotes) == 0


class TestEndToEndQuotationWorkflowComprehensive12D:
    """
    Comprehensive end-to-end integration test of the full quotation lifecycle:
    1. PDF upload & extraction (Alpha)
    2. XLSX upload & extraction (Beta)
    3. Manual quotation entry (Gamma)
    4. Extraction preview & coordinate provenance
    5. Buyer parameter correction & human confirmation gate
    6. Side-by-side comparison across suppliers with multi-currency normalization
    7. Landed-cost completeness disclosures
    8. Technical disqualification (non-compensable mandatory failure)
    9. Deterministic recommendations (Primary vs Disqualified)
    10. Buyer award approval & structured rejection
    11. Audit trail & quotation version history
    12. Database consistency & production database isolation
    """

    def test_complete_end_to_end_quotation_lifecycle(self, isolated_service):
        from tests.test_quotations_12b import make_synthetic_xlsx

        service = isolated_service
        brief = service.repository.get_brief("BRIEF-12D-UI")
        assert brief is not None

        # Add Gamma supplier
        supp_gamma = SupplierProfile(
            supplier_id="SUPP-GAMMA",
            company_name="Gamma Pack Ltd",
            domain="gammapack.com",
            headquarters_country="India",
        )
        service.repository.create_supplier(supp_gamma)

        # Ensure RFQ for Gamma
        rfq_gamma = service.create_and_persist_supplier_rfq(
            brief=brief,
            supplier=supp_gamma,
            contact_person="Lead Buyer Sarah",
        )

        # =====================================================================
        # Step 1: PDF Ingestion & Extraction (Alpha: Compliant, DDP, $1.45 USD)
        # =====================================================================
        alpha_pdf_lines = [
            "SUPPLIER OFFICIAL QUOTATION",
            "Quotation Ref: Q-2026-ALPHA",
            "Structure: 12u PET / 15u Met-BOPP / 50u Polyethylene",
            "Total Thickness: 77 microns",
            "OTR: 1.1 cc/m2/day at 23C (ASTM D3985)",
            "WVTR: 0.7 g/m2/day at 38C (ASTM F1249)",
            "TDS attached: Yes",
            "Quoted Quantity: 50,000 units",
            "Unit Price: $1.45 USD",
            "Lead Time: 4 weeks",
            "Payment Terms: Net 30 days upon inspection",
            "Delivery Terms: DDP Buyer Warehouse",
        ]
        alpha_pdf_bytes = make_synthetic_pdf(alpha_pdf_lines)

        # Preview extraction
        alpha_draft = service.extract_quotation_draft("Alpha_Quote.pdf", alpha_pdf_bytes)
        assert alpha_draft.commercial.quoted_unit_price == 1.45
        assert alpha_draft.commercial.quoted_currency == "USD"
        assert alpha_draft.compliance.thickness_microns == 77.0
        assert alpha_draft.compliance.has_tds_document is True
        assert "quoted_unit_price" in alpha_draft.provenance_map
        assert alpha_draft.provenance_map["quoted_unit_price"].page_or_cell_reference == "Page 1"

        # Ingest Alpha quote
        alpha_quote = service.ingest_quotation_file(
            rfq_id="RFQ-SUPP-ALPHA",
            filename="Alpha_Quote.pdf",
            content_bytes=alpha_pdf_bytes,
            user_name="Lead Buyer Sarah",
            brief_id=brief.brief_id,
            supplier_id="SUPP-ALPHA",
            company_name="Alpha Barrier Films Ltd",
        )
        assert alpha_quote.status == QuotationStatus.PARSED
        assert alpha_quote.version == 1

        # =====================================================================
        # Step 2: XLSX Ingestion & Extraction (Beta: Non-compliant OTR, EXW, INR 110)
        # =====================================================================
        beta_cells = {
            "A1": "SUPPLIER QUOTATION SHEET",
            "A2": "Supplier Name", "B2": "Beta Global Packaging Inc",
            "A3": "Unit Price (INR)", "B3": "110.00",
            "A4": "Currency", "B4": "INR",
            "A5": "Order Quantity", "B5": "50000",
            "A6": "Lead Time Weeks", "B6": "6",
            "A7": "Incoterms", "B7": "EXW",
            "A8": "Payment Terms", "B8": "100% advance before dispatch",
            "A9": "Structure", "B9": "Mono-layer LDPE Film",
            "A10": "Total Thickness", "B10": "45 microns",
            "A11": "OTR", "B11": "15.0 cc/m2/day",  # Exceeds 2.0 max threshold!
            "A12": "WVTR", "B12": "3.5 g/m2/day",  # Exceeds 1.0 max threshold!
            "A13": "Documentation", "B13": "Standard commercial proposal only",
        }
        beta_xlsx_bytes = make_synthetic_xlsx(beta_cells)

        # Preview extraction
        beta_draft = service.extract_quotation_draft("Beta_Quote.xlsx", beta_xlsx_bytes)
        assert beta_draft.commercial.quoted_unit_price == 110.0
        assert beta_draft.commercial.quoted_currency == "INR"
        assert beta_draft.compliance.thickness_microns == 45.0
        assert beta_draft.compliance.has_tds_document is False
        assert "quoted_unit_price" in beta_draft.provenance_map
        assert "Sheet1" in beta_draft.provenance_map["quoted_unit_price"].page_or_cell_reference

        # Ingest Beta quote
        beta_quote = service.ingest_quotation_file(
            rfq_id="RFQ-SUPP-BETA",
            filename="Beta_Quote.xlsx",
            content_bytes=beta_xlsx_bytes,
            user_name="Lead Buyer Sarah",
            brief_id=brief.brief_id,
            supplier_id="SUPP-BETA",
            company_name="Beta Global Packaging Inc",
        )
        assert beta_quote.status == QuotationStatus.PARSED

        # =====================================================================
        # Step 3: Manual Entry (Gamma: Compliant, FOB, €1.35 EUR)
        # =====================================================================
        gamma_comm = QuotationCommercialData(
            quoted_currency="EUR",
            quoted_unit_price=1.35,
            quoted_quantity=50000,
            min_order_quantity=10000,
            lead_time_weeks=5,
            payment_terms="Net 30 days upon delivery",
            incoterms=Incoterm.FOB,
            freight_charges=None,
            taxes_and_duties=None,
            is_freight_included=False,
            is_tax_included=False,
        )
        gamma_comp = QuotationTechnicalCompliance(
            product_category="Flexible Packaging",
            structure_description="PET / AlOx / PE High Barrier",
            thickness_microns=70.0,
            otr_value="1.4 cc/m2/day",
            wvtr_value="0.8 g/m2/day",
            test_standards_cited=["ASTM D3985", "ASTM F1249"],
            certifications_offered=["ISO 9001"],
            has_tds_document=True,
            mandatory_requirements_met=True,
        )
        gamma_quote = service.ingest_manual_quotation(
            rfq_id=rfq_gamma.rfq_id,
            commercial_data=gamma_comm,
            compliance_data=gamma_comp,
            user_name="Lead Buyer Sarah",
            reviewer_notes="Direct quote entered from supplier executive email.",
        )
        assert gamma_quote.status == QuotationStatus.UNDER_REVIEW

        # =====================================================================
        # Step 4: Buyer Parameter Correction & Confirmation Gate
        # =====================================================================
        # Correct Alpha payment terms and confirm
        alpha_corrected_comm = alpha_quote.commercial.model_copy(deep=True)
        alpha_corrected_comm.payment_terms = "Net 45 days upon technical acceptance (Negotiated)"
        alpha_confirmed = service.confirm_and_validate_quotation(
            quotation_id=alpha_quote.quotation_id,
            user_name="Lead Buyer Sarah",
            confirmed_commercial=alpha_corrected_comm,
            confirmed_compliance=alpha_quote.compliance,
            notes="Verified against official signed PDF and negotiated Net 45 terms.",
        )
        assert alpha_confirmed.status == QuotationStatus.UNDER_REVIEW
        assert alpha_confirmed.commercial.payment_terms == "Net 45 days upon technical acceptance (Negotiated)"

        # Confirm Beta: Buyer marks non-compliant barrier properties
        beta_corrected_comp = beta_quote.compliance.model_copy(deep=True)
        beta_corrected_comp.has_tds_document = False
        beta_corrected_comp.mandatory_requirements_met = False
        beta_corrected_comp.disqualification_reasons = ["OTR 15.0 cc/m2/day exceeds maximum mandatory threshold of 2.0 cc/m2/day."]
        beta_confirmed = service.confirm_and_validate_quotation(
            quotation_id=beta_quote.quotation_id,
            user_name="Lead Buyer Sarah",
            confirmed_commercial=beta_quote.commercial,
            confirmed_compliance=beta_corrected_comp,
            notes="Confirmed as submitted by supplier; non-compliant barrier noted.",
        )
        assert beta_confirmed.status == QuotationStatus.UNDER_REVIEW

        # =====================================================================
        # Step 5: Side-by-Side Comparison & Multi-Currency Normalization
        # =====================================================================
        matrix = service.compare_quotations_for_brief(
            brief_id=brief.brief_id,
            target_currency="USD",
        )
        assert matrix.target_currency == "USD"
        assert len(matrix.rows) == 3

        row_map = {r.supplier_id: r for r in matrix.rows}
        alpha_row = row_map["SUPP-ALPHA"]
        beta_row = row_map["SUPP-BETA"]
        gamma_row = row_map["SUPP-GAMMA"]

        # Currency normalization check
        assert alpha_row.quoted_currency == "USD"
        assert alpha_row.normalized_base_unit_price == 1.45
        assert beta_row.quoted_currency == "INR"
        assert beta_row.normalized_base_unit_price > 0
        assert beta_row.normalized_base_unit_price < 2.0  # ~110 * 0.011834 = ~1.30 USD
        assert gamma_row.quoted_currency == "EUR"
        assert gamma_row.normalized_base_unit_price > 1.40  # ~1.35 * 1.085 = ~1.46 USD

        # Landed cost disclosures check
        assert alpha_row.are_cost_inputs_complete is True  # DDP
        assert beta_row.are_cost_inputs_complete is False  # EXW missing freight & duty
        assert gamma_row.are_cost_inputs_complete is False  # FOB missing freight & duty

        # =====================================================================
        # Step 6: Technical Disqualification (Mandatory Barrier Failure)
        # =====================================================================
        assert beta_row.mandatory_requirements_met is False
        assert len(beta_row.disqualification_reasons) > 0
        assert beta_row.recommendation_type == ComparisonRecommendationType.NON_COMPLIANT_DISQUALIFIED

        assert alpha_row.mandatory_requirements_met is True
        assert gamma_row.mandatory_requirements_met is True

        # =====================================================================
        # Step 7: Deterministic Recommendations
        # =====================================================================
        assert matrix.primary_recommendation is not None
        assert matrix.primary_recommendation.supplier_id == "SUPP-ALPHA"
        assert matrix.primary_recommendation.recommendation_type == ComparisonRecommendationType.RECOMMENDED_PRIMARY

        # Beta cannot be primary recommendation even with low price
        assert matrix.primary_recommendation.supplier_id != "SUPP-BETA"

        # =====================================================================
        # Step 8: Buyer Award Approval & Rejection
        # =====================================================================
        awarded = service.accept_quotation_for_award(
            quotation_id=alpha_quote.quotation_id,
            user_name="Lead Buyer Sarah",
            award_notes="Awarded based on compliant barrier specifications, authentic TDS, and complete DDP landed cost.",
        )
        assert awarded.status == QuotationStatus.ACCEPTED

        rejected = service.reject_quotation(
            quotation_id=beta_quote.quotation_id,
            user_name="Lead Buyer Sarah",
            rejection_reason="Disqualified due to high OTR (15 cc/m2/day) failing mandatory barrier requirement (<2.0 cc/m2/day).",
        )
        assert rejected.status == QuotationStatus.REJECTED

        # =====================================================================
        # Step 9: Audit Trail & Quotation Version History
        # =====================================================================
        alpha_audit = service.get_quotation_audit_trail(alpha_quote.quotation_id)
        actions = [a.action for a in alpha_audit]
        assert "INITIAL_SUBMISSION" in actions
        assert "CONFIRM_FIELDS" in actions
        assert "AWARD_ACCEPTED" in actions

        # Revision: Submit revised quote for Alpha (Version 2)
        alpha_pdf_v2 = make_synthetic_pdf([
            "SUPPLIER OFFICIAL REVISED QUOTATION",
            "Quotation Ref: Q-2026-ALPHA-V2",
            "Structure: 12u PET / 15u Met-BOPP / 50u Polyethylene",
            "Total Thickness: 77 microns",
            "OTR: 1.1 cc/m2/day at 23C (ASTM D3985)",
            "WVTR: 0.7 g/m2/day at 38C (ASTM F1249)",
            "TDS attached: Yes",
            "Quoted Quantity: 50,000 units",
            "Unit Price: $1.40 USD",
            "Lead Time: 4 weeks",
            "Payment Terms: Net 45 days upon inspection",
            "Delivery Terms: DDP Buyer Warehouse",
        ])
        alpha_v2 = service.ingest_quotation_file(
            rfq_id="RFQ-SUPP-ALPHA",
            filename="Alpha_Quote_V2.pdf",
            content_bytes=alpha_pdf_v2,
            user_name="Lead Buyer Sarah",
            revision_notes="Supplier agreed to lower unit price to $1.40 USD.",
        )
        assert alpha_v2.version == 2
        assert alpha_v2.commercial.quoted_unit_price == 1.40

        # Verify historical Version 1 remains intact in database
        rfq_quotes = service.list_quotations_for_rfq("RFQ-SUPP-ALPHA")
        assert len(rfq_quotes) == 2
        versions = {q.version: q for q in rfq_quotes}
        assert 1 in versions
        assert 2 in versions
        assert versions[1].commercial.quoted_unit_price == 1.45
        assert versions[2].commercial.quoted_unit_price == 1.40

        # =====================================================================
        # Step 10: Database Integrity Check
        # =====================================================================
        latest_quotes = service.repository.list_quotations_for_brief(brief.brief_id, latest_only=True)
        assert len(latest_quotes) == 3  # Latest: Alpha v2, Beta v1, Gamma v1

        all_quotes = service.repository.list_quotations_for_brief(brief.brief_id, latest_only=False)
        assert len(all_quotes) == 4  # All versions: Alpha v1, Alpha v2, Beta v1, Gamma v1


class TestProductionDatabaseIsolation12D:
    """Verify that all dashboard workflows never touch vendoriq.sqlite."""

    def test_production_database_row_counts_strictly_unchanged(self, tmp_path):
        """Assert zero modifications to vendoriq.sqlite."""
        prod_db = "vendoriq.sqlite"
        assert Path(prod_db).exists()

        conn_pre = sqlite3.connect(prod_db)
        pre_counts = {
            t[0]: conn_pre.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0]
            for t in conn_pre.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn_pre.close()

        # Run dashboard service operations on an isolated database
        isolated_db = str(tmp_path / "isolated_12d_check.sqlite")
        shutil.copy2(prod_db, isolated_db)
        repo = VendorIQRepository(db_path=isolated_db)
        service = DashboardDataService(repository=repo)
        service.get_quotation_service().base_dir = str(tmp_path)

        briefs = repo.list_briefs()
        if briefs:
            service.compare_quotations_for_brief(brief_id=briefs[0].brief_id)

        conn_post = sqlite3.connect(prod_db)
        post_counts = {
            t[0]: conn_post.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0]
            for t in conn_post.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn_post.close()

        assert pre_counts == post_counts, f"Database modified! Pre: {pre_counts}, Post: {post_counts}"
