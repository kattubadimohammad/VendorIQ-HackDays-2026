"""
VendorIQ Phase 12B Test Suite: Quotation Ingestion & Data Extraction Engine.

Tests:
1. Deterministic PDF quotation extraction with extraction provenance.
2. Excel quotation extraction (.xlsx and .xls) with cell coordinates.
3. Irregular, incomplete, and misleading quotation layouts.
4. Missing, ambiguous, and low-confidence field detection.
5. Human confirmation gate and editable draft workflow.
6. End-to-end quotation ingestion, versioning, and duplicate upload prevention.
7. Strict database isolation guaranteeing zero writes to vendoriq.sqlite.
"""

from datetime import datetime, timezone, date, timedelta
import io
import os
from pathlib import Path
import shutil
import sqlite3
import xml.etree.ElementTree as ET
import zipfile
import zlib
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
    ExtractionMethod,
    ExtractionFieldStatus,
    QuotationCommercialData,
    QuotationTechnicalCompliance,
    SupplierQuotation,
    EvidenceCategory,
    EvidenceStatus,
)
from core.exceptions import DataValidationError
from db.connection import init_db
from db.repository import VendorIQRepository
from services.quotation_extractor import (
    PDFQuotationParser,
    ExcelQuotationParser,
    QuotationDataExtractor,
    ExtractedQuotationDraft,
)
from services.quotation_service import QuotationService
from utils.file_storage import (
    compute_sha256,
    validate_quotation_file,
    sanitize_filename,
    MAX_FILE_SIZE_BYTES,
)


# =============================================================================
# SYNTHETIC DOCUMENT GENERATION HELPERS
# =============================================================================

def make_synthetic_pdf(lines: list[str]) -> bytes:
    """Generate a valid PDF containing text streams in Tj operators."""
    stream_lines = ["BT", "/F1 12 Tf", "72 750 Td"]
    for l in lines:
        safe_l = l.replace("(", "[").replace(")", "]")
        stream_lines.append(f"({safe_l}) Tj")
        stream_lines.append("0 -20 Td")
    stream_lines.append("ET")

    stream_content = "\n".join(stream_lines).encode("latin1")
    comp_stream = zlib.compress(stream_content)

    pdf = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>\nendobj\n"
        b"4 0 obj\n<< /Length " + str(len(comp_stream)).encode() + b" /Filter /FlateDecode >>\n"
        b"stream\n" + comp_stream + b"\nendstream\nendobj\n"
        b"trailer\n<< /Root 1 0 R >>\n%%EOF"
    )
    return pdf


def make_synthetic_xlsx(cells: dict[str, str]) -> bytes:
    """Generate a valid OpenXML .xlsx workbook containing specified cells."""
    buf = io.BytesIO()
    # Build shared strings
    unique_strings = list(dict.fromkeys(cells.values()))
    str_to_idx = {s: i for i, s in enumerate(unique_strings)}

    sst_xml = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
               f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(cells)}" uniqueCount="{len(unique_strings)}">']
    for s in unique_strings:
        sst_xml.append(f'<si><t>{s}</t></si>')
    sst_xml.append('</sst>')

    # Build sheetData
    # Group cells by row
    rows_dict = {}
    for coord, val in cells.items():
        row_num = int("".join(c for c in coord if c.isdigit()))
        rows_dict.setdefault(row_num, []).append((coord, val))

    sheet_xml = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
                 '<sheetData>']
    for r_num in sorted(rows_dict.keys()):
        sheet_xml.append(f'<row r="{r_num}">')
        for coord, val in rows_dict[r_num]:
            idx = str_to_idx[val]
            sheet_xml.append(f'<c r="{coord}" t="s"><v>{idx}</v></c>')
        sheet_xml.append('</row>')
    sheet_xml.append('</sheetData></worksheet>')

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>
</Types>""")
        z.writestr("xl/workbook.xml", """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<sheets><sheet name="Quotation" sheetId="1" r:id="rId1" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/></sheets>
</workbook>""")
        z.writestr("xl/sharedStrings.xml", "\n".join(sst_xml))
        z.writestr("xl/worksheets/sheet1.xml", "\n".join(sheet_xml))

    buf.seek(0)
    return buf.getvalue()


# =============================================================================
# 1. DETERMINISTIC PDF EXTRACTION TESTS
# =============================================================================

class TestPDFQuotationExtraction:
    """Test PDF quotation parsing, commercial parameters, technical specs, and provenance."""

    def test_standard_pdf_quotation_extraction_accuracy(self):
        """Standard PDF: Extracts all commercial parameters, specs, and field provenance."""
        lines = [
            "COSMO FILMS OFFICIAL COMMERCIAL QUOTATION",
            "Quotation Ref: Q-2026-IN-1049",
            "Product Description: High Barrier Multi-layer Recyclable Stand-up Pouches",
            "Material Structure: 12u PET / 15u Met-BOPP / 50u Polyethylene",
            "Thickness: 77 microns",
            "Barrier Performance: OTR < 1.2 cc/m2/day at 23C, 0% RH (ASTM D3985)",
            "Moisture Barrier: WVTR < 0.8 g/m2/day at 38C, 90% RH (ASTM F1249)",
            "Accredited TDS and laboratory reports attached.",
            "Certifications: ISO 9001:2015, ISO 14001, BRCGS Packaging Certified",
            "Quoted Quantity: 50,000 units",
            "Minimum Order Quantity: 10,000 units",
            "Unit Price: $1.45 USD per unit",
            "Total Price: $72,500 USD",
            "Delivery Terms: FOB Mumbai Port",
            "Production Lead Time: 4 weeks from approved artwork",
            "Payment Terms: Net 30 days upon delivery and technical inspection",
            "Quote Validity: 60 days from issue",
        ]
        pdf_bytes = make_synthetic_pdf(lines)
        extractor = QuotationDataExtractor()

        draft = extractor.extract_from_file(
            file_bytes=pdf_bytes,
            filename="Cosmo_Quotation_Standard.pdf",
            raw_file_path="uploads/quotations/test.pdf",
            file_hash_sha256="abc123hash",
        )

        # 1. Commercial verification
        assert draft.commercial.quoted_unit_price == 1.45
        assert draft.commercial.quoted_currency == "USD"
        assert draft.commercial.quoted_quantity == 50000
        assert draft.commercial.min_order_quantity == 10000
        assert draft.commercial.lead_time_weeks == 4
        assert draft.commercial.incoterms == Incoterm.FOB
        assert "Net 30" in draft.commercial.payment_terms
        assert draft.commercial.validity_days == 60

        # 2. Provenance verification
        price_prov = draft.provenance_map.get("quoted_unit_price")
        assert price_prov is not None
        assert "1.45" in price_prov.raw_value
        assert "Page 1" in price_prov.page_or_cell_reference
        assert price_prov.extraction_method == ExtractionMethod.AUTOMATED_PARSER
        assert price_prov.confidence >= 0.90
        assert price_prov.status == ExtractionFieldStatus.PENDING_CONFIRMATION

        # 3. Technical compliance verification
        assert draft.compliance.thickness_microns == 77.0
        assert "ASTM D3985" in draft.compliance.test_standards_cited
        assert "ASTM F1249" in draft.compliance.test_standards_cited
        assert "ISO 9001" in draft.compliance.certifications_offered
        assert draft.compliance.has_tds_document is True
        assert draft.compliance.mandatory_requirements_met is True
        assert draft.compliance.evidence_status_map[EvidenceCategory.BARRIER_FILM_EXPERIENCE.value] == EvidenceStatus.VERIFIED

        # Zero missing fields on standard quote
        assert len(draft.missing_fields) == 0

    def test_inr_currency_and_days_lead_time_pdf(self):
        """Test Indian Rupee (INR / Rs.) quotation and days-to-weeks conversion."""
        lines = [
            "ARROW GREENTECH PACKAGING PROPOSAL",
            "Product: Water-soluble multi-layer barrier film",
            "Rate per unit: Rs. 125.00 INR",
            "Order Volume: 25,000 units",
            "MOQ: 5,000 units",
            "Delivery Time: 28 days",
            "Payment: 30% advance, 70% against Bill of Lading",
            "Terms: CIF Nhava Sheva",
            "Freight included in price.",
        ]
        pdf_bytes = make_synthetic_pdf(lines)
        extractor = QuotationDataExtractor()

        draft = extractor.extract_from_file(
            file_bytes=pdf_bytes,
            filename="Arrow_Quote_INR.pdf",
        )

        assert draft.commercial.quoted_unit_price == 125.00
        assert draft.commercial.quoted_currency == "INR"
        assert draft.commercial.quoted_quantity == 25000
        assert draft.commercial.lead_time_weeks == 4  # 28 days -> 4 weeks
        assert draft.commercial.incoterms == Incoterm.CIF
        assert draft.commercial.is_freight_included is True


# =============================================================================
# 2. EXCEL EXTRACTION TESTS (.XLSX & .XLS)
# =============================================================================

class TestExcelQuotationExtraction:
    """Test Excel quotation extraction, cell coordinates, and non-adjacent layouts."""

    def test_standard_xlsx_extraction_with_cell_coordinates(self):
        """Test standard Excel grid with exact cell coordinate provenance."""
        cells = {
            "A1": "Supplier Quotation Matrix",
            "A2": "Item Description",
            "B2": "High Barrier EVOH Laminated Packaging Pouches",
            "A3": "Unit Price (USD)",
            "B3": "1.38",
            "A4": "Quoted Quantity",
            "B4": "50,000",
            "A5": "MOQ",
            "B5": "10,000",
            "A6": "Lead Time",
            "B6": "3 weeks",
            "A7": "Payment Terms",
            "B7": "Net 45 days upon receipt",
            "A8": "Incoterms",
            "B8": "DDP Buyer Hub",
            "A9": "Barrier Spec",
            "B9": "OTR: 0.9 cc/m2/day, WVTR: 0.5 g/m2/day, ASTM D3985 certified with TDS attached",
            "A10": "Certifications",
            "B10": "BRCGS, ISO 9001:2015, FSC",
        }
        xlsx_bytes = make_synthetic_xlsx(cells)
        extractor = QuotationDataExtractor()

        draft = extractor.extract_from_file(
            file_bytes=xlsx_bytes,
            filename="Supplier_Quote_Grid.xlsx",
        )

        assert draft.commercial.quoted_unit_price == 1.38
        assert draft.commercial.quoted_currency == "USD"
        assert draft.commercial.quoted_quantity == 50000
        assert draft.commercial.lead_time_weeks == 3
        assert draft.commercial.incoterms == Incoterm.DDP
        assert draft.commercial.is_tax_included is True

        # Check cell coordinates provenance
        price_prov = draft.provenance_map["quoted_unit_price"]
        assert "B3" in price_prov.page_or_cell_reference
        assert price_prov.confidence >= 0.95

        qty_prov = draft.provenance_map["quoted_quantity"]
        assert "B4" in qty_prov.page_or_cell_reference

    def test_irregular_excel_layout_non_adjacent_columns(self):
        """Requirement 3: Support irregular layouts (Col A has label, Col C has value)."""
        cells = {
            "A2": "Commercial Proposal",
            "A5": "Price per Unit",
            "B5": "",  # Empty column
            "C5": "1.52",  # Value placed in Col C
            "A6": "Order Quantity",
            "C6": "20,000",
            "A7": "Production Lead Time",
            "C7": "5 weeks",
            "A8": "Payment Condition",
            "C8": "100% Letter of Credit at sight",
            "A9": "Delivery Terms",
            "C9": "EXW Factory Warehouse",
        }
        xlsx_bytes = make_synthetic_xlsx(cells)
        extractor = QuotationDataExtractor()

        draft = extractor.extract_from_file(
            file_bytes=xlsx_bytes,
            filename="Irregular_Spreadsheet.xlsx",
        )

        assert draft.commercial.quoted_unit_price == 1.52
        assert draft.commercial.quoted_quantity == 20000
        assert draft.commercial.lead_time_weeks == 5
        assert draft.commercial.incoterms == Incoterm.EXW


# =============================================================================
# 3. IRREGULAR, INCOMPLETE, AND MISLEADING LAYOUTS
# =============================================================================

class TestIrregularAndIncompleteQuotationLayouts:
    """Test flagging of missing fields, misleading totals, and ambiguity detection."""

    def test_incomplete_quotation_flags_missing_fields(self):
        """Requirement 6: Incomplete document flags missing fields and warns buyer."""
        lines = [
            "INFORMAL PRICE ESTIMATE",
            "We can supply standard pouches.",
            "Unit Price: $1.20 USD",
            # Missing: quantity, lead time, payment terms
        ]
        pdf_bytes = make_synthetic_pdf(lines)
        extractor = QuotationDataExtractor()

        draft = extractor.extract_from_file(
            file_bytes=pdf_bytes,
            filename="Incomplete_Quote.pdf",
        )

        assert draft.commercial.quoted_unit_price == 1.20
        assert "quoted_quantity" in draft.missing_fields
        assert "lead_time_weeks" in draft.missing_fields
        assert "payment_terms" in draft.missing_fields
        assert draft.is_ready_for_comparison is False

    def test_misleading_total_amount_does_not_override_unit_price(self):
        """Ensure large total contract price is never misidentified as unit price."""
        lines = [
            "COMMERCIAL OFFER",
            "Unit Price: $1.60 per unit",
            "Order Volume: 50,000 units",
            "Total Contract Amount: $80,000.00 USD",
            "Subtotal: $80,000.00 USD",
        ]
        pdf_bytes = make_synthetic_pdf(lines)
        extractor = QuotationDataExtractor()

        draft = extractor.extract_from_file(
            file_bytes=pdf_bytes,
            filename="Total_vs_Unit_Price.pdf",
        )

        assert draft.commercial.quoted_unit_price == 1.60
        assert draft.commercial.quoted_unit_price != 80000.0


# =============================================================================
# 4. INGESTION SERVICE & HUMAN CONFIRMATION WORKFLOW
# =============================================================================

# =============================================================================
# ISOLATED FIXTURE
# =============================================================================

@pytest.fixture
def isolated_service(tmp_path):
    """Create isolated test environment with repository and parent entities."""
    db_path = str(tmp_path / "test_ingestion_isolated.sqlite")
    init_db(db_path)
    repo = VendorIQRepository(db_path=db_path)

    # Parent brief and supplier
    brief = SourcingBrief(
        brief_id="BRIEF-INGEST-01",
        title="Ingestion Test Brief",
        buyer_organization="VendorIQ QA",
        description="Testing Brief",
        criteria=SourcingCriteria(
            target_industries=["Packaging"],
            target_geographies=["India"],
        ),
    )
    repo.create_brief(brief)

    supplier = SupplierProfile(
        supplier_id="SUPP-INGEST-01",
        company_name="Ingestion Test Packaging Ltd",
        domain="ingestpackaging.com",
        headquarters_country="India",
    )
    repo.create_supplier(supplier)

    rfq = RFQDocumentRecord(
        rfq_id="RFQ-INGEST-01",
        brief_id="BRIEF-INGEST-01",
        supplier_id="SUPP-INGEST-01",
        title="RFQ for Barrier Packaging",
        status=RFQStatus.SENT,
        buyer_info=RFQBuyerInfo(
            buyer_organization="VendorIQ QA",
            rfq_reference="RFQ-REF-INGEST",
            response_deadline=datetime.now(timezone.utc) + timedelta(days=14),
        ),
        supplier=RFQSupplierTarget(
            supplier_id="SUPP-INGEST-01",
            company_name="Ingestion Test Packaging Ltd",
            domain="ingestpackaging.com",
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

    return QuotationService(repository=repo, base_dir=str(tmp_path))


class TestQuotationServiceEndToEndIngestion:
    """Test full ingestion workflow, duplicate upload prevention, and human gates."""

    def test_file_ingestion_and_human_confirmation_gate(self, isolated_service):
        """Requirements 7 & 8: Ingestion, editable draft, and mandatory confirmation gate."""
        lines = [
            "SUPPLIER PROPOSAL",
            "Unit Price: $1.42 USD",
            "Quantity: 50,000 units",
            "MOQ: 10,000 units",
            "Lead Time: 4 weeks",
            "Payment Terms: Net 30 days",
            "Delivery: FOB Nhava Sheva",
        ]
        pdf_bytes = make_synthetic_pdf(lines)

        # 1. Ingest file into system
        quote = isolated_service.ingest_quotation_file(
            rfq_id="RFQ-INGEST-01",
            filename="Supplier_Quote_V1.pdf",
            content_bytes=pdf_bytes,
            user_name="Buyer Charlie",
        )

        assert quote.quotation_id.startswith("QUOTE-")
        assert quote.version == 1
        assert quote.status == QuotationStatus.PARSED
        assert quote.commercial.critical_fields_confirmed is False
        assert quote.commercial.quoted_unit_price == 1.42

        # 2. Human confirmation gate: Buyer reviews and confirms critical fields
        confirmed_comm = quote.commercial.model_copy(deep=True)
        confirmed_comm.payment_terms = "Net 30 days upon inspection (Confirmed)"
        confirmed_quote = isolated_service.confirm_and_validate_quotation(
            quotation_id=quote.quotation_id,
            user_name="Buyer Charlie",
            confirmed_commercial=confirmed_comm,
            notes="Buyer verified prices, terms, and specifications against invoice.",
        )

        assert confirmed_quote.status == QuotationStatus.UNDER_REVIEW
        assert confirmed_quote.commercial.critical_fields_confirmed is True
        assert "Confirmed" in confirmed_quote.commercial.payment_terms
        assert len(confirmed_quote.audit_trail) >= 2
        assert confirmed_quote.audit_trail[-1].action == "CONFIRM_FIELDS"

    def test_duplicate_upload_prevention(self, isolated_service):
        """Requirement 10: Prevent duplicate file uploads without corrupting history."""
        lines = ["QUOTATION", "Unit Price: $1.50 USD", "Quantity: 20000"]
        pdf_bytes = make_synthetic_pdf(lines)

        # Upload initial quote
        quote1 = isolated_service.ingest_quotation_file(
            rfq_id="RFQ-INGEST-01",
            filename="Identical_Quote.pdf",
            content_bytes=pdf_bytes,
            user_name="Buyer Alice",
        )
        assert quote1.version == 1

        # Upload exact same file again for the same RFQ
        quote2 = isolated_service.ingest_quotation_file(
            rfq_id="RFQ-INGEST-01",
            filename="Identical_Quote_Copy.pdf",
            content_bytes=pdf_bytes,
            user_name="Buyer Alice",
        )

        # Must recognize duplicate file hash and return existing quotation
        assert quote2.quotation_id == quote1.quotation_id
        assert quote2.file_hash_sha256 == quote1.file_hash_sha256

        # Total quotes in repository for this RFQ must still be 1
        all_quotes = isolated_service.repository.list_quotations_for_rfq("RFQ-INGEST-01")
        assert len(all_quotes) == 1

    def test_revised_document_creates_version_2(self, isolated_service):
        """Requirement 9 & 10: Revised file creates new immutable version."""
        lines_v1 = ["QUOTATION V1", "Unit Price: $1.75 USD", "Lead Time: 6 weeks"]
        pdf_v1 = make_synthetic_pdf(lines_v1)
        q1 = isolated_service.ingest_quotation_file(
            rfq_id="RFQ-INGEST-01",
            filename="Quote_V1.pdf",
            content_bytes=pdf_v1,
            user_name="Buyer Alice",
        )
        assert q1.version == 1

        lines_v2 = ["QUOTATION V2 REVISION", "Unit Price: $1.40 USD", "Lead Time: 4 weeks"]
        pdf_v2 = make_synthetic_pdf(lines_v2)
        q2 = isolated_service.ingest_quotation_file(
            rfq_id="RFQ-INGEST-01",
            filename="Quote_V2_Discounted.pdf",
            content_bytes=pdf_v2,
            user_name="Buyer Alice",
            revision_notes="Supplier accepted volume discount counter-offer.",
        )

        assert q2.version == 2
        assert q2.commercial.quoted_unit_price == 1.40
        q1_recheck = isolated_service.repository.get_quotation(q1.quotation_id)
        assert q1_recheck.status == QuotationStatus.SUPERSEDED


# =============================================================================
# 5. DATABASE ISOLATION VERIFICATION
# =============================================================================

class TestProductionDatabaseIsolation12B:
    """Ensure running Milestone 12B tests leaves vendoriq.sqlite 100% untouched."""

    def test_production_database_row_counts_strictly_unchanged(self, tmp_path):
        """Verify zero table row count changes in vendoriq.sqlite."""
        prod_db = "vendoriq.sqlite"
        if not os.path.exists(prod_db):
            pytest.skip("vendoriq.sqlite not found.")

        conn_pre = sqlite3.connect(prod_db)
        pre_counts = {
            t[0]: conn_pre.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0]
            for t in conn_pre.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn_pre.close()

        # Run extraction and service ingestion on an isolated database
        isolated_db = str(tmp_path / "isolated_12b_check.sqlite")
        shutil.copy2(prod_db, isolated_db)
        repo = VendorIQRepository(db_path=isolated_db)
        service = QuotationService(repository=repo, base_dir=str(tmp_path))

        brief = repo.list_briefs()[0]
        supp = repo.list_suppliers()[0]
        rfq = repo.create_supplier_rfq(
            RFQDocumentRecord(
                rfq_id="RFQ-12B-ISO",
                brief_id=brief.brief_id,
                supplier_id=supp.supplier_id,
                title="Isolation RFQ",
                buyer_info=RFQBuyerInfo(
                    buyer_organization="Test Org",
                    rfq_reference="RFQ-ISO-REF",
                    response_deadline=datetime.now(timezone.utc) + timedelta(days=14),
                ),
                supplier=RFQSupplierTarget(
                    supplier_id=supp.supplier_id,
                    company_name=supp.company_name,
                    domain=supp.domain,
                ),
                product_requirements=RFQProductRequirements(
                    category="Testing",
                    description="Testing",
                    expected_quantity="10,000",
                ),
                commercial_requirements=RFQCommercialRequirements(
                    currency="USD",
                    payment_terms="Net 30",
                ),
            )
        )

        pdf_bytes = make_synthetic_pdf(["QUOTATION", "Unit Price: $1.15 USD", "Quantity: 10,000 units"])
        service.ingest_quotation_file(
            rfq_id=rfq.rfq_id,
            filename="Isolation_Quote.pdf",
            content_bytes=pdf_bytes,
            user_name="Isolation Tester",
        )

        conn_post = sqlite3.connect(prod_db)
        post_counts = {
            t[0]: conn_post.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0]
            for t in conn_post.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn_post.close()

        assert pre_counts == post_counts, (
            f"Production database was modified! Pre: {pre_counts}, Post: {post_counts}"
        )


# =============================================================================
# 6. TARGETED VERIFICATION TESTS (MILESTONE 12B FINAL VERIFICATION)
# =============================================================================

class TestMilestone12BTargetedVerification:
    """
    Targeted verification tests for Milestone 12B:
    - Legacy binary .xls unsupported detection and honest rejection.
    - PDF multi-page provenance and unsupported structure handling.
    - Scanned/image PDF missing field flagging without fabrication.
    - Human confirmation gate defense against missing/ambiguous critical fields.
    - File storage security consistency (10 MB boundary, signatures, traversal).
    """

    def test_binary_xls_format_honestly_rejected(self):
        """
        Legacy XLS: Verify that binary BIFF8 / OLE2 .xls files are honestly detected
        and rejected with a clear DataValidationError rather than extracting binary garbage.
        """
        # Genuine OLE2 / BIFF8 binary header
        binary_biff8_sample = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 1024
        extractor = QuotationDataExtractor()

        with pytest.raises(DataValidationError) as exc_info:
            extractor.extract_from_file(binary_biff8_sample, "supplier_quote.xls")

        err = str(exc_info.value)
        assert "Legacy binary Excel format (.xls / BIFF8) is not supported" in err
        assert "convert" in err.lower() or "xlsx" in err.lower()

    def test_multipage_pdf_page_number_tracking(self):
        """
        PDF Parser: Verify that multi-page PDFs with separate page objects and streams
        correctly assign and track page numbers (e.g. Page 2 for price on second page).
        """
        stream1 = zlib.compress(b"BT /F1 12 Tf (Supplier Header Information) Tj 0 -20 Td (Product: Standup Barrier Pouches) Tj ET")
        stream2 = zlib.compress(b"BT /F1 12 Tf (Unit Price: $1.95 USD) Tj 0 -20 Td (Quoted Quantity: 25000 units) Tj ET")

        multipage_pdf = (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /Contents 5 0 R >>\nendobj\n"
            b"4 0 obj\n<< /Type /Page /Parent 2 0 R /Contents 6 0 R >>\nendobj\n"
            b"5 0 obj\n<< /Length " + str(len(stream1)).encode() + b" /Filter /FlateDecode >>\nstream\n" + stream1 + b"\nendstream\nendobj\n"
            b"6 0 obj\n<< /Length " + str(len(stream2)).encode() + b" /Filter /FlateDecode >>\nstream\n" + stream2 + b"\nendstream\nendobj\n"
            b"trailer\n<< /Root 1 0 R >>\n%%EOF"
        )

        extractor = QuotationDataExtractor()
        draft = extractor.extract_from_file(multipage_pdf, "multipage_quote.pdf")

        assert draft.commercial.quoted_unit_price == 1.95
        price_prov = draft.provenance_map.get("quoted_unit_price")
        assert price_prov is not None
        assert price_prov.page_or_cell_reference == "Page 2"
        assert price_prov.confidence >= 0.90

    def test_scanned_pdf_flags_missing_fields_without_fabrication(self):
        """
        PDF Parser: Scanned/image-only PDFs (no text streams) must flag missing fields,
        generate clear extraction warnings, set is_ready_for_comparison=False,
        and never fabricate commercial values.
        """
        # Scanned PDF with XObject Image and no Tj/TJ text operators
        img_dummy = b"\xFF\xD8\xFF\xE0\x00\x10JFIF" + b"\x00" * 300
        scanned_pdf = (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
            b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
            b"3 0 obj\n<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>\nendobj\n"
            b"4 0 obj\n<< /Length 20 >>\nstream\nq /Im1 Do Q\nendstream\nendobj\n"
            b"trailer\n<< /Root 1 0 R >>\n%%EOF"
        )

        extractor = QuotationDataExtractor()
        draft = extractor.extract_from_file(scanned_pdf, "scanned_invoice.pdf")

        # Zero fabricated commercial data
        assert "quoted_unit_price" in draft.missing_fields
        assert "quoted_quantity" in draft.missing_fields
        assert "lead_time_weeks" in draft.missing_fields
        assert "payment_terms" in draft.missing_fields
        assert draft.is_ready_for_comparison is False
        assert draft.commercial.critical_fields_confirmed is False
        assert any("could not be deterministically extracted" in w for w in draft.extraction_warnings)

    def test_human_confirmation_gate_blocks_zero_price_and_missing_terms(self, isolated_service):
        """
        Human Confirmation Gate: Verify that incomplete, zero, or ambiguous critical
        fields CANNOT pass confirmation without explicit buyer input.
        """
        # 1. Ingest an incomplete quotation
        incomplete_pdf = make_synthetic_pdf(["INFORMAL NOTE", "General packaging supply"])
        quote = isolated_service.ingest_quotation_file(
            rfq_id="RFQ-INGEST-01",
            filename="Incomplete_Note.pdf",
            content_bytes=incomplete_pdf,
            user_name="Buyer Eve",
        )
        assert quote.commercial.critical_fields_confirmed is False

        # 2. Attempt confirmation with zero price -> BLOCKED
        bad_comm_price = quote.commercial.model_copy(deep=True)
        bad_comm_price.quoted_unit_price = 0.0
        with pytest.raises(DataValidationError) as exc_price:
            isolated_service.confirm_and_validate_quotation(
                quotation_id=quote.quotation_id,
                user_name="Buyer Eve",
                confirmed_commercial=bad_comm_price,
            )
        assert "quoted_unit_price" in str(exc_price.value)

        # 3. Attempt confirmation with unspecified payment terms -> BLOCKED
        bad_comm_terms = quote.commercial.model_copy(deep=True)
        bad_comm_terms.quoted_unit_price = 1.30
        bad_comm_terms.payment_terms = "Defaulted/Unspecified"
        with pytest.raises(DataValidationError) as exc_terms:
            isolated_service.confirm_and_validate_quotation(
                quotation_id=quote.quotation_id,
                user_name="Buyer Eve",
                confirmed_commercial=bad_comm_terms,
            )
        assert "payment_terms" in str(exc_terms.value)

        # 4. Successful confirmation with explicit buyer input
        valid_comm = quote.commercial.model_copy(deep=True)
        valid_comm.quoted_unit_price = 1.30
        valid_comm.quoted_quantity = 25000
        valid_comm.lead_time_weeks = 4
        valid_comm.payment_terms = "Net 30 days upon inspection"

        confirmed_quote = isolated_service.confirm_and_validate_quotation(
            quotation_id=quote.quotation_id,
            user_name="Buyer Eve",
            confirmed_commercial=valid_comm,
            notes="Buyer manually confirmed pricing and terms from supplier email.",
        )
        assert confirmed_quote.status == QuotationStatus.UNDER_REVIEW
        assert confirmed_quote.commercial.critical_fields_confirmed is True
        assert confirmed_quote.commercial.provenance_map["quoted_unit_price"].status == ExtractionFieldStatus.CONFIRMED
        assert confirmed_quote.commercial.provenance_map["quoted_unit_price"].confirmed_by == "Buyer Eve"

    def test_file_storage_security_consistency(self):
        """
        File Security: Verify that utils/file_storage and services/quotation_service
        consistently enforce the 10 MB maximum file size and strict defenses.
        """
        from utils.file_storage import MAX_FILE_SIZE_BYTES as UTILS_MAX_SIZE
        from services.quotation_service import MAX_FILE_SIZE_BYTES as SERVICE_MAX_SIZE

        # 1. 10 MB consistency check
        assert UTILS_MAX_SIZE == 10 * 1024 * 1024
        assert SERVICE_MAX_SIZE == 10 * 1024 * 1024

        # 2. Extension validation
        assert validate_quotation_file("quote.exe", b"%PDF-1.4")[0] is False
        assert validate_quotation_file("quote.pdf", b"%PDF-1.4")[0] is True
        assert validate_quotation_file("quote.xlsx", b"PK\x03\x04")[0] is True

        # 3. Size boundary rejection (>10 MB)
        oversized = b"%PDF-1.4" + b"0" * (10 * 1024 * 1024 + 100)
        assert validate_quotation_file("quote.pdf", oversized)[0] is False

        # 4. Path traversal sanitization
        assert sanitize_filename("../../etc/shadow") == "shadow"
        assert sanitize_filename("..\\..\\boot.ini") == "boot.ini"
