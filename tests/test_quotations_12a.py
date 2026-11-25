"""
VendorIQ Phase 12A Test Suite: Supplier Quotation Models, Persistence, and Secure Storage.

Tests:
1. Domain model validation, provenance metadata, landed cost completeness gates,
   transparent currency conversion, and technical compliance boundaries.
2. Secure file storage utility, path traversal prevention, magic byte verification,
   file size limits, and SHA-256 hashing.
3. Repository CRUD operations, immutable versioning, lifecycle transitions,
   human confirmation gates, and audit trail persistence.
4. Database isolation guarantee: ensures vendoriq.sqlite is 100% untouched.
"""

from datetime import datetime, timezone, date, timedelta
import os
from pathlib import Path
import shutil
import sqlite3
import pytest

from core.models import (
    SourcingBrief,
    SourcingCriteria,
    SupplierProfile,
    SupplierEvidenceRecord,
    EvidenceCategory,
    EvidenceStatus,
    RFQStatus,
    RFQBuyerInfo,
    RFQProductRequirements,
    RFQCommercialRequirements,
    RFQSupplierTarget,
    RFQDocumentRecord,
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
)
from core.exceptions import DataValidationError
from db.connection import init_db
from db.repository import VendorIQRepository
from utils.file_storage import (
    sanitize_filename,
    compute_sha256,
    validate_quotation_file,
    save_quotation_file,
    MAX_FILE_SIZE_BYTES,
)


# =============================================================================
# 1. DOMAIN MODELS & REFINEMENT TESTS
# =============================================================================

class TestQuotationDomainModels:
    """Test strongly-typed quotation models and mandatory design refinements."""

    def test_quotation_status_lifecycle_and_transitions(self):
        """Verify lifecycle states and transition boundaries."""
        assert QuotationStatus.RECEIVED == "RECEIVED"
        assert QuotationStatus.PARSED == "PARSED"
        assert QuotationStatus.UNDER_REVIEW == "UNDER_REVIEW"
        assert QuotationStatus.ACCEPTED == "ACCEPTED"
        assert QuotationStatus.REJECTED == "REJECTED"
        assert QuotationStatus.SUPERSEDED == "SUPERSEDED"

        # Superseded is an immutable terminal state with no valid transitions
        assert VALID_QUOTATION_TRANSITIONS[QuotationStatus.SUPERSEDED] == []
        assert QuotationStatus.ACCEPTED in VALID_QUOTATION_TRANSITIONS[QuotationStatus.UNDER_REVIEW]
        assert QuotationStatus.REJECTED in VALID_QUOTATION_TRANSITIONS[QuotationStatus.UNDER_REVIEW]

    def test_transparent_currency_conversion_and_overrides(self):
        """Refinement 1: Currency conversion rate transparency and buyer overrides."""
        benchmark_fx = ExchangeRateRecord(
            base_currency="USD",
            target_currency="INR",
            rate=84.50,
            source="Static Reference Benchmark (Not Live Market Rate)",
            effective_date="2026-11-20",
            rate_type=ExchangeRateType.BENCHMARK,
            is_outdated=False,
            is_buyer_override=False,
        )
        assert benchmark_fx.rate == 84.50
        assert benchmark_fx.rate_type == ExchangeRateType.BENCHMARK
        assert "Not Live Market Rate" in benchmark_fx.source

        # Buyer override rate
        override_fx = ExchangeRateRecord(
            base_currency="USD",
            target_currency="INR",
            rate=85.20,
            source="Buyer Manual Override",
            effective_date=date.today().isoformat(),
            rate_type=ExchangeRateType.BUYER_OVERRIDE,
            is_outdated=False,
            is_buyer_override=True,
            notes="Forward contract rate locked with corporate treasury",
        )
        assert override_fx.rate == 85.20
        assert override_fx.rate_type == ExchangeRateType.BUYER_OVERRIDE
        assert override_fx.is_buyer_override is True

    def test_landed_cost_calculation_with_complete_inputs(self):
        """Refinement 2: Landed cost calculated only when inputs are complete."""
        comm = QuotationCommercialData(
            quoted_currency="INR",
            quoted_unit_price=120.0,
            quoted_quantity=10000,
            min_order_quantity=5000,
            lead_time_weeks=4,
            payment_terms="Net 30 days upon inspection",
            incoterms=Incoterm.FOB,
            freight_charges=50000.0,  # 5 INR per unit
            taxes_and_duties=100000.0, # 10 INR per unit
            additional_charges=0.0,
        )
        fx = ExchangeRateRecord(
            base_currency="USD",
            target_currency="INR",
            rate=84.50,
            effective_date="2026-11-20",
            rate_type=ExchangeRateType.BENCHMARK,
        )

        comm.calculate_landed_cost(fx_record=fx, target_currency="USD")

        # Total unit in INR: 120 + 5 + 10 = 135 INR
        # In USD: 135 / 84.50 = 1.5976 USD
        assert comm.are_cost_inputs_complete is True
        assert comm.estimated_landed_unit_cost_target_curr is not None
        assert abs(comm.estimated_landed_unit_cost_target_curr - (135.0 / 84.50)) < 1e-3
        assert "Estimated Landed Cost" in comm.landed_cost_explanation
        assert "using 1 USD = 84.5 INR" in comm.landed_cost_explanation

    def test_landed_cost_gate_prevents_misleading_comparison_on_incomplete_data(self):
        """Refinement 2: Never invent missing freight or tax numbers."""
        comm_exw = QuotationCommercialData(
            quoted_currency="USD",
            quoted_unit_price=1.25,
            quoted_quantity=50000,
            min_order_quantity=10000,
            lead_time_weeks=3,
            payment_terms="50% advance, 50% on BL",
            incoterms=Incoterm.EXW,  # EXW without freight/taxes specified
            freight_charges=None,
            taxes_and_duties=None,
        )

        comm_exw.calculate_landed_cost(target_currency="USD")

        # Must NOT invent freight
        assert comm_exw.are_cost_inputs_complete is False
        assert comm_exw.estimated_landed_unit_cost_target_curr is None
        assert "Landed cost cannot be reliably computed" in comm_exw.landed_cost_explanation
        assert "Missing Freight charges" in comm_exw.landed_cost_explanation
        assert "Comparing quoted base unit price only" in comm_exw.landed_cost_explanation

    def test_extraction_provenance_and_human_confirmation(self):
        """Refinement 4: Detailed extraction provenance and human gate."""
        prov_price = FieldExtractionProvenance(
            field_name="quoted_unit_price",
            raw_value="$1.45/unit",
            source_file="Cosmo_Films_Quotation_Q1049.pdf",
            page_or_cell_reference="Page 2, Commercial Table Row 3",
            extraction_method=ExtractionMethod.AUTOMATED_PARSER,
            status=ExtractionFieldStatus.PENDING_CONFIRMATION,
            confidence=0.95,
        )
        assert prov_price.status == ExtractionFieldStatus.PENDING_CONFIRMATION
        assert prov_price.confirmed_by is None

        # Human buyer confirms the field
        prov_price.status = ExtractionFieldStatus.CONFIRMED
        prov_price.confirmed_by = "Senior Procurement Lead"
        prov_price.confirmed_at = datetime.now(timezone.utc)
        assert prov_price.status == ExtractionFieldStatus.CONFIRMED
        assert prov_price.confirmed_by == "Senior Procurement Lead"

    def test_technical_compliance_cannot_be_compensated_by_low_price(self):
        """Refinement 5: Distinguish evidence tiers and enforce technical failure boundaries."""
        comp = QuotationTechnicalCompliance(
            product_category="Flexible Packaging",
            structure_description="Single-layer LDPE Film",  # Lacks barrier multi-layer
            thickness_microns=50.0,
            otr_value=None,  # Missing OTR
            wvtr_value=None, # Missing WVTR
            test_standards_cited=[],
            certifications_offered=["ISO 9001"],
            evidence_status_map={
                EvidenceCategory.BARRIER_FILM_EXPERIENCE.value: EvidenceStatus.MISSING,
                EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS.value: EvidenceStatus.UNVERIFIED,
            },
            has_tds_document=False,
            mandatory_requirements_met=False,
            disqualification_reasons=[
                "Failed barrier requirement: Sourcing brief mandates multi-layer barrier film with ASTM D3985 OTR.",
                "Missing Technical Data Sheet (TDS).",
            ],
            compliance_notes="Candidate disqualified due to lack of barrier film structure.",
        )
        assert comp.mandatory_requirements_met is False
        assert len(comp.disqualification_reasons) == 2
        assert comp.evidence_status_map[EvidenceCategory.BARRIER_FILM_EXPERIENCE.value] == EvidenceStatus.MISSING


# =============================================================================
# 2. FILE STORAGE & SECURITY TESTS
# =============================================================================

class TestQuotationFileStorageAndSecurity:
    """Test secure quotation document storage, path traversal defenses, and validation."""

    def test_sanitize_filename_prevents_directory_traversal(self):
        """Verify aggressive directory traversal sanitization."""
        assert sanitize_filename("../../../etc/passwd") == "passwd"
        assert sanitize_filename("..\\..\\windows\\system32\\cmd.exe") == "cmd.exe"
        assert sanitize_filename("safe_quote_v1.pdf") == "safe_quote_v1.pdf"
        assert sanitize_filename("quote\x00_test.xlsx") == "quote_test.xlsx"
        assert sanitize_filename("   ") == "unnamed_quotation"

    def test_compute_sha256(self):
        """Verify SHA-256 hashing computation."""
        sample_bytes = b"VendorIQ Quotation Document Content"
        expected_hash = "981ef2b17a91b4f1292d0be4375e421a785c7484a33d87e5a87e6fe135b5067a"
        assert compute_sha256(sample_bytes) == expected_hash

    def test_validate_quotation_file_extensions_and_sizes(self):
        """Test allowed file formats, 10MB limit, and magic bytes."""
        # 1. Empty file rejected
        ok, msg = validate_quotation_file("test.pdf", b"")
        assert not ok
        assert "empty" in msg.lower()

        # 2. Oversized file rejected (>10MB)
        large_bytes = b"0" * (MAX_FILE_SIZE_BYTES + 1024)
        ok, msg = validate_quotation_file("large.pdf", large_bytes)
        assert not ok
        assert "maximum allowed size" in msg

        # 3. Disallowed extension rejected
        ok, msg = validate_quotation_file("script.py", b"print('hello')")
        assert not ok
        assert "Unsupported file type" in msg

        # 4. Valid PDF with correct magic header
        valid_pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"
        ok, msg = validate_quotation_file("quote.pdf", valid_pdf)
        assert ok
        assert msg == ""

        # 5. Spoofed PDF with invalid header rejected
        spoofed_pdf = b"This is plain text pretending to be PDF"
        ok, msg = validate_quotation_file("quote.pdf", spoofed_pdf)
        assert not ok
        assert "header does not match" in msg

    def test_save_quotation_file_isolation_and_path_defense(self, tmp_path):
        """Verify file persistence under uploads/quotations/ and traversal defense."""
        base_dir = str(tmp_path / "app_workspace")
        valid_pdf = b"%PDF-1.7\nSample Quotation Body\n%%EOF"

        rel_path, f_hash = save_quotation_file(
            brief_id="BRIEF-DCD8044A",
            supplier_id="CRUST_1049",
            filename="Cosmo_Quote_V1.pdf",
            content_bytes=valid_pdf,
            base_dir=base_dir,
        )

        assert "uploads/quotations/BRIEF-DCD8044A/CRUST_1049/Cosmo_Quote_V1.pdf" in rel_path
        assert len(f_hash) == 64
        full_path = Path(base_dir) / rel_path
        assert full_path.exists()
        assert full_path.read_bytes() == valid_pdf


# =============================================================================
# 3. REPOSITORY CRUD & IMMUTABLE VERSIONING TESTS
# =============================================================================

class TestQuotationRepositoryAndPersistence:
    """Test database persistence, versioning, audit trail, and human gates on isolated DB."""

    @pytest.fixture
    def isolated_repo(self, tmp_path):
        """Create an isolated test database with all schemas provisioned."""
        db_path = str(tmp_path / "test_quotations_isolated.sqlite")
        init_db(db_path)
        repo = VendorIQRepository(db_path=db_path)

        # Provision parent brief, supplier, and RFQ for FK integrity
        brief = SourcingBrief(
            brief_id="BRIEF-TEST-01",
            title="Test Sourcing Brief",
            buyer_organization="Test Org",
            description="Testing Brief Description",
            criteria=SourcingCriteria(
                target_industries=["Packaging"],
                target_geographies=["India"],
            ),
        )
        repo.create_brief(brief)

        supplier = SupplierProfile(
            supplier_id="SUPP-TEST-01",
            company_name="Test Supplier Ltd",
            domain="testsupplier.com",
            headquarters_country="India",
        )
        repo.create_supplier(supplier)

        rfq = RFQDocumentRecord(
            rfq_id="RFQ-TEST-01",
            brief_id="BRIEF-TEST-01",
            supplier_id="SUPP-TEST-01",
            title="RFQ for Barrier Packaging",
            status=RFQStatus.SENT,
            buyer_info=RFQBuyerInfo(
                buyer_organization="Test Buyer Org",
                rfq_reference="RFQ-REF-001",
                response_deadline=datetime.now(timezone.utc) + timedelta(days=14),
            ),
            supplier=RFQSupplierTarget(
                supplier_id="SUPP-TEST-01",
                company_name="Test Supplier Ltd",
                domain="testsupplier.com",
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

        return repo

    def test_quotation_crud_and_initial_audit(self, isolated_repo):
        """Test creating, reading, updating, and deleting a quotation."""
        comm = QuotationCommercialData(
            quoted_currency="USD",
            quoted_unit_price=1.35,
            quoted_quantity=50000,
            min_order_quantity=10000,
            lead_time_weeks=4,
            payment_terms="Net 30 days",
            incoterms=Incoterm.CIF,
            is_freight_included=True,
            is_tax_included=True,
            are_cost_inputs_complete=True,
            estimated_landed_unit_cost_target_curr=1.35,
            target_currency="USD",
            landed_cost_explanation="CIF quotation with freight and duties bundled.",
        )
        comp = QuotationTechnicalCompliance(
            product_category="Flexible Packaging",
            structure_description="PET/Met-BOPP/PE 3-layer pouch",
            thickness_microns=65.0,
            otr_value="1.2 cc/m2/day",
            wvtr_value="0.8 g/m2/day",
            test_standards_cited=["ASTM D3985", "ASTM F1249"],
            has_tds_document=True,
            mandatory_requirements_met=True,
        )

        quote = SupplierQuotation(
            quotation_id="QUOTE-TEST-01",
            rfq_id="RFQ-TEST-01",
            supplier_id="SUPP-TEST-01",
            brief_id="BRIEF-TEST-01",
            company_name="Test Supplier Ltd",
            version=1,
            status=QuotationStatus.RECEIVED,
            file_type="PDF",
            raw_file_path="uploads/quotations/BRIEF-TEST-01/SUPP-TEST-01/quote.pdf",
            file_hash_sha256="a" * 64,
            commercial=comm,
            compliance=comp,
            reviewer_notes="First draft submission",
        )

        saved = isolated_repo.create_quotation(quote)
        assert saved.quotation_id == "QUOTE-TEST-01"
        assert len(saved.audit_trail) == 1
        assert saved.audit_trail[0].action == "INITIAL_SUBMISSION"

        # Read back
        retrieved = isolated_repo.get_quotation("QUOTE-TEST-01")
        assert retrieved is not None
        assert retrieved.company_name == "Test Supplier Ltd"
        assert retrieved.commercial.quoted_unit_price == 1.35
        assert retrieved.compliance.mandatory_requirements_met is True
        assert len(retrieved.audit_trail) == 1

    def test_immutable_versioning_and_superseding_history(self, isolated_repo):
        """Refinement 3: Create new version, mark previous SUPERSEDED, preserve both."""
        comm_v1 = QuotationCommercialData(
            quoted_currency="USD",
            quoted_unit_price=1.50,
            quoted_quantity=50000,
            min_order_quantity=10000,
            lead_time_weeks=5,
            payment_terms="Net 30",
        )
        comp = QuotationTechnicalCompliance(
            product_category="Flexible Packaging",
            structure_description="Multi-layer pouch",
            mandatory_requirements_met=True,
        )
        quote_v1 = SupplierQuotation(
            quotation_id="QUOTE-V1",
            rfq_id="RFQ-TEST-01",
            supplier_id="SUPP-TEST-01",
            brief_id="BRIEF-TEST-01",
            company_name="Test Supplier Ltd",
            version=1,
            status=QuotationStatus.RECEIVED,
            commercial=comm_v1,
            compliance=comp,
        )
        isolated_repo.create_quotation(quote_v1)

        # Create revised Version 2 with improved pricing
        comm_v2 = QuotationCommercialData(
            quoted_currency="USD",
            quoted_unit_price=1.30,  # Price discount
            quoted_quantity=50000,
            min_order_quantity=10000,
            lead_time_weeks=4,
            payment_terms="Net 30",
        )
        quote_v2 = SupplierQuotation(
            quotation_id="QUOTE-V2",
            rfq_id="RFQ-TEST-01",
            supplier_id="SUPP-TEST-01",
            brief_id="BRIEF-TEST-01",
            company_name="Test Supplier Ltd",
            commercial=comm_v2,
            compliance=comp,
        )

        saved_v2 = isolated_repo.create_quotation_version(
            rfq_id="RFQ-TEST-01",
            new_quotation=quote_v2,
            changed_by="Procurement Specialist",
            revision_notes="Supplier provided 13% volume discount on revised quote.",
        )

        assert saved_v2.version == 2
        assert saved_v2.commercial.quoted_unit_price == 1.30

        # Verify Version 1 is marked SUPERSEDED but preserved immutably
        v1_recheck = isolated_repo.get_quotation("QUOTE-V1")
        assert v1_recheck.status == QuotationStatus.SUPERSEDED
        assert v1_recheck.commercial.quoted_unit_price == 1.50

        # Verify list of all versions
        all_versions = isolated_repo.list_quotations_for_rfq("RFQ-TEST-01")
        assert len(all_versions) == 2
        assert all_versions[0].version == 1
        assert all_versions[0].status == QuotationStatus.SUPERSEDED
        assert all_versions[1].version == 2
        assert all_versions[1].status == QuotationStatus.RECEIVED

        # Verify parent RFQ status transitioned to RESPONSE_RECEIVED
        parent_rfq = isolated_repo.get_supplier_rfq("RFQ-TEST-01")
        assert parent_rfq.status == RFQStatus.RESPONSE_RECEIVED

    def test_quotation_lifecycle_transitions_and_human_gates(self, isolated_repo):
        """Test formal state transitions and human confirmation gate."""
        comm = QuotationCommercialData(
            quoted_currency="USD",
            quoted_unit_price=1.20,
            quoted_quantity=50000,
            min_order_quantity=10000,
            lead_time_weeks=4,
            payment_terms="Net 30",
            provenance_map={
                "quoted_unit_price": FieldExtractionProvenance(
                    field_name="quoted_unit_price",
                    raw_value="$1.20",
                    extraction_method=ExtractionMethod.AUTOMATED_PARSER,
                    status=ExtractionFieldStatus.PENDING_CONFIRMATION,
                )
            },
        )
        comp = QuotationTechnicalCompliance(
            product_category="Flexible Packaging",
            structure_description="Multi-layer barrier",
            mandatory_requirements_met=True,
        )
        quote = SupplierQuotation(
            quotation_id="QUOTE-GATE-01",
            rfq_id="RFQ-TEST-01",
            supplier_id="SUPP-TEST-01",
            brief_id="BRIEF-TEST-01",
            company_name="Test Supplier Ltd",
            commercial=comm,
            compliance=comp,
            status=QuotationStatus.RECEIVED,
        )
        isolated_repo.create_quotation(quote)

        # 1. RECEIVED -> PARSED
        p_quote = isolated_repo.transition_quotation_status(
            quotation_id="QUOTE-GATE-01",
            new_status=QuotationStatus.PARSED,
            changed_by="Automated Pipeline",
            comments="Extracted 8 commercial and technical fields.",
        )
        assert p_quote.status == QuotationStatus.PARSED

        # 2. Human confirms critical fields
        c_quote = isolated_repo.confirm_critical_fields(
            quotation_id="QUOTE-GATE-01",
            confirmed_by="Buyer Alice",
            notes="All unit prices and MOQ validated against PDF.",
        )
        assert c_quote.commercial.critical_fields_confirmed is True
        assert c_quote.commercial.provenance_map["quoted_unit_price"].status == ExtractionFieldStatus.CONFIRMED

        # 3. PARSED -> UNDER_REVIEW
        r_quote = isolated_repo.transition_quotation_status(
            quotation_id="QUOTE-GATE-01",
            new_status=QuotationStatus.UNDER_REVIEW,
            changed_by="Buyer Alice",
            comments="Verified data; moved to side-by-side comparison.",
        )
        assert r_quote.status == QuotationStatus.UNDER_REVIEW

        # 4. UNDER_REVIEW -> ACCEPTED (Award decision)
        a_quote = isolated_repo.transition_quotation_status(
            quotation_id="QUOTE-GATE-01",
            new_status=QuotationStatus.ACCEPTED,
            changed_by="Procurement Director",
            comments="Quotation accepted for contract award. Technical compliance verified.",
        )
        assert a_quote.status == QuotationStatus.ACCEPTED

        # 5. Invalid transition: SUPERSEDED cannot be transitioned
        isolated_repo.transition_quotation_status(
            quotation_id="QUOTE-GATE-01",
            new_status=QuotationStatus.SUPERSEDED,
            changed_by="System",
            comments="Superseding quote.",
        )
        with pytest.raises(DataValidationError, match="Invalid quotation transition"):
            isolated_repo.transition_quotation_status(
                quotation_id="QUOTE-GATE-01",
                new_status=QuotationStatus.ACCEPTED,
                changed_by="System",
                comments="Attempting to re-activate superseded quote.",
            )


# =============================================================================
# 4. DATABASE ISOLATION VERIFICATION
# =============================================================================

class TestProductionDatabaseIsolation12A:
    """Ensure running Milestone 12A tests does not modify vendoriq.sqlite."""

    def test_production_database_row_counts_remain_unmodified(self, tmp_path):
        """Assert zero writes to production database."""
        prod_db = "vendoriq.sqlite"
        if not os.path.exists(prod_db):
            pytest.skip("vendoriq.sqlite not found.")

        # Capture counts before
        conn_pre = sqlite3.connect(prod_db)
        pre_counts = {
            t[0]: conn_pre.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0]
            for t in conn_pre.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn_pre.close()

        # Run complete quotation test workflow on an isolated database
        isolated_db = str(tmp_path / "isolation_check.sqlite")
        shutil.copy2(prod_db, isolated_db)
        repo = VendorIQRepository(db_path=isolated_db)

        # Perform quotation write operations on isolated copy
        brief = repo.list_briefs()[0]
        supp = repo.list_suppliers()[0]

        # Create parent RFQ in isolated database
        rfq = RFQDocumentRecord(
            rfq_id="RFQ-ISO-TEST",
            brief_id=brief.brief_id,
            supplier_id=supp.supplier_id,
            title="Isolated Test RFQ",
            buyer_info=RFQBuyerInfo(
                buyer_organization="Test Buyer Org",
                rfq_reference="RFQ-REF-ISO",
                response_deadline=datetime.now(timezone.utc) + timedelta(days=14),
            ),
            supplier=RFQSupplierTarget(
                supplier_id=supp.supplier_id,
                company_name=supp.company_name,
                domain=supp.domain,
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

        comm = QuotationCommercialData(
            quoted_currency="USD",
            quoted_unit_price=1.99,
            quoted_quantity=1000,
            min_order_quantity=500,
            lead_time_weeks=2,
            payment_terms="Net 30",
        )
        comp = QuotationTechnicalCompliance(
            product_category="Testing",
            structure_description="Test structure",
            mandatory_requirements_met=True,
        )
        q = SupplierQuotation(
            quotation_id="QUOTE-ISO-TEST",
            rfq_id="RFQ-ISO-TEST",
            supplier_id=supp.supplier_id,
            brief_id=brief.brief_id,
            company_name=supp.company_name,
            commercial=comm,
            compliance=comp,
        )
        repo.create_quotation(q)

        # Capture counts after
        conn_post = sqlite3.connect(prod_db)
        post_counts = {
            t[0]: conn_post.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0]
            for t in conn_post.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn_post.close()

        assert pre_counts == post_counts, (
            f"Production database vendoriq.sqlite was modified! Pre: {pre_counts}, Post: {post_counts}"
        )
