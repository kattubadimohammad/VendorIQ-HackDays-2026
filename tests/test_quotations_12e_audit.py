"""
VendorIQ - Milestone 12E: Comprehensive End-to-End Quotation Testing & Audit Suite.

Conducts an exhaustive automated audit of:
1. End-to-End Workflow & Lifecycle Transitions
2. Data Integrity, Foreign Keys, Immutability & Rollback
3. Commercial Calculations, Landed Costs & FX Normalization
4. Technical Compliance, Non-Compensability & Evidence Grounding
5. Security Defenses, Path Traversal & Cross-Brief Confidentiality Isolation
6. Production Database Zero-Alteration Verification
"""

import io
import re
import shutil
import sqlite3
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.exceptions import DataValidationError
from core.models import (
    ComparisonRecommendationType,
    EvidenceCategory,
    EvidenceStatus,
    ExchangeRateRecord,
    ExchangeRateType,
    ExtractionFieldStatus,
    ExtractionMethod,
    FieldExtractionProvenance,
    Incoterm,
    ProvenanceClassification,
    QuotationCommercialData,
    QuotationComparisonMatrix,
    QuotationComparisonRow,
    QuotationStatus,
    QuotationTechnicalCompliance,
    RFQBuyerInfo,
    RFQCommercialRequirements,
    RFQDocumentRecord,
    RFQProductRequirements,
    RFQStatus,
    RFQSupplierTarget,
    SourcingBrief,
    SourcingCriteria,
    SupplierProfile,
    SupplierQuotation,
)
from db.repository import VendorIQRepository, get_db_connection, init_db
from services.quotation_comparison import (
    CurrencyNormalizationService,
    TechnicalComplianceEvaluator,
    QuotationComparisonEngine,
    DEFAULT_BENCHMARK_RATES,
    BENCHMARK_EFFECTIVE_DATE,
    BENCHMARK_SOURCE,
)
from services.quotation_extractor import QuotationDataExtractor
from services.quotation_service import QuotationService
from ui.data_service import DashboardDataService
from utils.file_storage import (
    MAX_FILE_SIZE_BYTES,
    compute_sha256,
    sanitize_filename,
    save_quotation_file,
    validate_quotation_file,
)


# =============================================================================
# SYNTHETIC DOCUMENT HELPERS
# =============================================================================

def make_synthetic_pdf(lines: list[str]) -> bytes:
    """Build a compliant, valid PDF 1.4 byte stream without external dependencies."""
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


def make_synthetic_xlsx(cells: dict[str, str]) -> bytes:
    """Build a valid OpenXML .xlsx workbook byte stream containing specified cells."""
    buf = io.BytesIO()
    unique_strings = list(dict.fromkeys(cells.values()))
    str_to_idx = {s: i for i, s in enumerate(unique_strings)}

    sst_xml = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(cells)}" uniqueCount="{len(unique_strings)}">',
    ]
    for s in unique_strings:
        sst_xml.append(f'<si><t>{s}</t></si>')
    sst_xml.append('</sst>')

    rows_dict = {}
    for coord, val in cells.items():
        row_num = int("".join(c for c in coord if c.isdigit()))
        rows_dict.setdefault(row_num, []).append((coord, val))

    sheet_xml = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        '<sheetData>',
    ]
    for r_num in sorted(rows_dict.keys()):
        sheet_xml.append(f'<row r="{r_num}">')
        for coord, val in rows_dict[r_num]:
            idx = str_to_idx[val]
            sheet_xml.append(f'<c r="{coord}" t="s"><v>{idx}</v></c>')
        sheet_xml.append('</row>')
    sheet_xml.append('</sheetData></worksheet>')

    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        '</Types>'
    )

    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )

    wb_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>'
    )

    wb_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
        '</Relationships>'
    )

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", rels_xml)
        zf.writestr("xl/workbook.xml", wb_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels_xml)
        zf.writestr("xl/sharedStrings.xml", "\n".join(sst_xml))
        zf.writestr("xl/worksheets/sheet1.xml", "\n".join(sheet_xml))

    return buf.getvalue()


# =============================================================================
# ISOLATED FIXTURE SETUP
# =============================================================================

@pytest.fixture
def audit_environment(tmp_path):
    """
    Construct an isolated SQLite database environment with two distinct sourcing
    briefs, three suppliers, and independent file storage directories.
    """
    db_path = str(tmp_path / "audit_isolated_vendoriq.sqlite")
    init_db(db_path)
    repo = VendorIQRepository(db_path=db_path)

    # 1. Brief A: Flexible Food Packaging (Target: Barrier film OTR < 2.0 cc/m2/day)
    brief_a = SourcingBrief(
        brief_id="BRIEF-AUDIT-A",
        title="High-Barrier Flexible Packaging Pouches",
        buyer_organization="EcoFoods Global Inc",
        description="Mandatory requirement: Multi-layer barrier film with OTR < 2.0 cc/m2/day and authentic TDS.",
        criteria=SourcingCriteria(
            target_industries=["Flexible Packaging"],
            target_geographies=["India", "Germany"],
            required_certifications=["ISO 9001"],
        ),
    )
    repo.create_brief(brief_a)

    # 2. Brief B: Pharma Blister Packaging (Target: Aluminum Foil)
    brief_b = SourcingBrief(
        brief_id="BRIEF-AUDIT-B",
        title="Pharmaceutical Grade Blister Foil Sourcing",
        buyer_organization="Apex Pharma Ltd",
        description="Confidential pharmaceutical cold-form foil requirements.",
        criteria=SourcingCriteria(
            target_industries=["Pharmaceutical Packaging"],
            target_geographies=["India"],
            required_certifications=["GMP", "ISO 15378"],
        ),
    )
    repo.create_brief(brief_b)

    # 3. Suppliers
    suppliers = [
        SupplierProfile(
            supplier_id="SUPP-ALPHA",
            company_name="Alpha Barrier Films Ltd",
            domain="alphabarrier.com",
            headquarters_country="India",
        ),
        SupplierProfile(
            supplier_id="SUPP-BETA",
            company_name="Beta Polymer Solutions Ltd",
            domain="betapolymer.com",
            headquarters_country="India",
        ),
        SupplierProfile(
            supplier_id="SUPP-GAMMA",
            company_name="Gamma European Films GmbH",
            domain="gammafilms.de",
            headquarters_country="Germany",
        ),
    ]
    for s in suppliers:
        repo.create_supplier(s)

    # 4. Service layer with test tmp_path storage
    service = DashboardDataService(repository=repo)
    service.get_quotation_service().base_dir = str(tmp_path)

    # 5. Create RFQs for Brief A
    for s in suppliers:
        service.create_and_persist_supplier_rfq(
            brief=brief_a,
            supplier=s,
            contact_person="Sarah Auditor",
        )

    # 6. Create RFQ for Brief B (Alpha only)
    service.create_and_persist_supplier_rfq(
        brief=brief_b,
        supplier=suppliers[0],
        contact_person="Pharma Lead",
    )

    return {
        "repo": repo,
        "service": service,
        "brief_a": brief_a,
        "brief_b": brief_b,
        "suppliers": suppliers,
        "tmp_path": tmp_path,
        "db_path": db_path,
    }


# =============================================================================
# 1. END-TO-END WORKFLOW & LIFECYCLE TRANSITION AUDIT
# =============================================================================

class TestMilestone12EE2EWorkflowAudit:
    """
    Activity 1: End-to-end workflow audit.
    Verify complete process from brief to quotation ingestion, review, comparison,
    recommendation, human award/rejection, revision, and audit log.
    """

    def test_complete_sourcing_lifecycle_transitions(self, audit_environment):
        service = audit_environment["service"]
        brief_a = audit_environment["brief_a"]

        rfqs = service.repository.list_supplier_rfqs(brief_id=brief_a.brief_id)
        rfq_alpha = next(r for r in rfqs if r.supplier_id == "SUPP-ALPHA")
        rfq_beta = next(r for r in rfqs if r.supplier_id == "SUPP-BETA")

        # 1. Ingestion of Alpha PDF ($1.45 USD, DDP, Compliant)
        pdf_bytes = make_synthetic_pdf([
            "OFFICIAL QUOTATION",
            "Quotation Ref: Q-ALPHA-01",
            "Structure: 12u PET / 9u Al / 50u PE High Barrier",
            "Total Thickness: 71 microns",
            "OTR: 0.8 cc/m2/day at 23C (ASTM D3985)",
            "WVTR: 0.4 g/m2/day at 38C (ASTM F1249)",
            "Technical Data Sheet: Attached",
            "Quoted Quantity: 50,000 units",
            "Unit Price: $1.45 USD",
            "Lead Time: 4 weeks",
            "Payment Terms: Net 30 days upon inspection",
            "Incoterms: DDP Buyer Fulfillment Center",
        ])
        q_alpha = service.ingest_quotation_file(
            rfq_id=rfq_alpha.rfq_id,
            filename="Alpha_Q1.pdf",
            content_bytes=pdf_bytes,
            user_name="Sarah Auditor",
        )
        assert q_alpha.status == QuotationStatus.PARSED
        assert q_alpha.version == 1

        # 2. Ingestion of Beta XLSX (INR 120.00, EXW, Non-compliant OTR 12.0)
        xlsx_bytes = make_synthetic_xlsx({
            "A1": "SUPPLIER QUOTATION",
            "A2": "Supplier Name", "B2": "Beta Polymer Solutions Ltd",
            "A3": "Unit Price (INR)", "B3": "120.00",
            "A4": "Order Quantity", "B4": "50000",
            "A5": "Lead Time Weeks", "B5": "6",
            "A6": "Incoterms", "B6": "EXW",
            "A7": "Payment Terms", "B7": "100% advance",
            "A8": "Structure", "B8": "Standard Monolayer PE Film",
            "A9": "Total Thickness", "B9": "50 microns",
            "A10": "OTR", "B10": "12.0 cc/m2/day",  # Exceeds 2.0 max threshold
            "A11": "WVTR", "B11": "4.0 g/m2/day",
            "A12": "Documentation", "B12": "Commercial sheet only",
        })
        q_beta = service.ingest_quotation_file(
            rfq_id=rfq_beta.rfq_id,
            filename="Beta_Q1.xlsx",
            content_bytes=xlsx_bytes,
            user_name="Sarah Auditor",
        )
        assert q_beta.status == QuotationStatus.PARSED

        # 3. Review & Human Confirmation Gate
        # Alpha confirmed with negotiated payment terms
        corrected_comm = q_alpha.commercial.model_copy(deep=True)
        corrected_comm.payment_terms = "Net 45 days (Negotiated)"
        q_alpha_conf = service.confirm_and_validate_quotation(
            quotation_id=q_alpha.quotation_id,
            user_name="Sarah Auditor",
            confirmed_commercial=corrected_comm,
            confirmed_compliance=q_alpha.compliance,
            notes="Verified against official signed PDF.",
        )
        assert q_alpha_conf.status == QuotationStatus.UNDER_REVIEW
        assert q_alpha_conf.commercial.payment_terms == "Net 45 days (Negotiated)"

        # Beta confirmed with non-compliant barrier noted
        beta_comp = q_beta.compliance.model_copy(deep=True)
        beta_comp.has_tds_document = False
        beta_comp.mandatory_requirements_met = False
        beta_comp.disqualification_reasons = ["OTR 12.0 cc/m2/day exceeds mandatory limit of 2.0 cc/m2/day."]
        q_beta_conf = service.confirm_and_validate_quotation(
            quotation_id=q_beta.quotation_id,
            user_name="Sarah Auditor",
            confirmed_commercial=q_beta.commercial,
            confirmed_compliance=beta_comp,
            notes="Confirmed non-compliant technical specifications.",
        )
        assert q_beta_conf.status == QuotationStatus.UNDER_REVIEW

        # 4. Multi-Supplier Comparison & Recommendations
        matrix = service.compare_quotations_for_brief(brief_id=brief_a.brief_id, target_currency="USD")
        assert len(matrix.rows) == 2
        assert matrix.primary_recommendation is not None
        assert matrix.primary_recommendation.supplier_id == "SUPP-ALPHA"
        assert matrix.primary_recommendation.recommendation_type == ComparisonRecommendationType.RECOMMENDED_PRIMARY

        beta_row = next(r for r in matrix.rows if r.supplier_id == "SUPP-BETA")
        assert beta_row.recommendation_type == ComparisonRecommendationType.NON_COMPLIANT_DISQUALIFIED

        # 5. Human Award Approval & Rejection
        awarded = service.accept_quotation_for_award(
            quotation_id=q_alpha.quotation_id,
            user_name="Sarah Auditor",
            award_notes="Approved based on compliant barrier properties and complete DDP landed cost.",
        )
        assert awarded.status == QuotationStatus.ACCEPTED

        rejected = service.reject_quotation(
            quotation_id=q_beta.quotation_id,
            user_name="Sarah Auditor",
            rejection_reason="Disqualified due to high OTR failing mandatory barrier requirements.",
        )
        assert rejected.status == QuotationStatus.REJECTED

        # 6. Audit Trail Lineage Verification
        alpha_audit = service.get_quotation_audit_trail(q_alpha.quotation_id)
        actions = [a.action for a in alpha_audit]
        assert actions == ["INITIAL_SUBMISSION", "CONFIRM_FIELDS", "AWARD_ACCEPTED"]
        assert alpha_audit[0].changed_by in ["Procurement Reviewer", "Sarah Auditor"]
        assert alpha_audit[1].changed_by == "Sarah Auditor"
        assert alpha_audit[2].changed_by == "Sarah Auditor"

        beta_audit = service.get_quotation_audit_trail(q_beta.quotation_id)
        beta_actions = [a.action for a in beta_audit]
        assert beta_actions == ["INITIAL_SUBMISSION", "CONFIRM_FIELDS", "REJECT_QUOTATION"]
        assert beta_audit[0].changed_by in ["Procurement Reviewer", "Sarah Auditor"]
        assert beta_audit[1].changed_by == "Sarah Auditor"
        assert beta_audit[2].changed_by == "Sarah Auditor"

        # 7. Revision Handling (Alpha submits Version 2)
        v2_pdf = make_synthetic_pdf([
            "OFFICIAL REVISED QUOTATION V2",
            "Quotation Ref: Q-ALPHA-02",
            "Unit Price: $1.42 USD",
            "Lead Time: 4 weeks",
            "Incoterms: DDP Buyer Fulfillment Center",
        ])
        q_alpha_v2 = service.ingest_quotation_file(
            rfq_id=rfq_alpha.rfq_id,
            filename="Alpha_Q2.pdf",
            content_bytes=v2_pdf,
            user_name="Sarah Auditor",
            revision_notes="Supplier conceded $0.03/unit volume discount.",
        )
        assert q_alpha_v2.version == 2
        assert q_alpha_v2.commercial.quoted_unit_price == 1.42

        # Verify historical Version 1 remains preserved
        history = service.list_quotations_for_rfq(rfq_alpha.rfq_id)
        assert len(history) == 2
        assert {h.version for h in history} == {1, 2}
        v1 = next(h for h in history if h.version == 1)
        v2 = next(h for h in history if h.version == 2)
        assert v1.commercial.quoted_unit_price == 1.45
        assert v2.commercial.quoted_unit_price == 1.42


# =============================================================================
# 2. DATA INTEGRITY, FOREIGN KEYS & ROLLBACK AUDIT
# =============================================================================

class TestMilestone12EDataIntegrityAudit:
    """
    Activity 2: Data integrity audit.
    Verify SQLite foreign keys, duplicate prevention, immutable history,
    transaction rollback, and absence of orphaned records.
    """

    def test_foreign_key_enforcement_and_orphaned_records(self, audit_environment):
        db_path = audit_environment["db_path"]

        # 1. Direct SQLite test with PRAGMA foreign_keys = ON
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA foreign_keys = ON;")
            cursor = conn.cursor()

            # Attempt to insert quotation referencing non-existent RFQ
            with pytest.raises(sqlite3.IntegrityError):
                cursor.execute(
                    """
                    INSERT INTO supplier_quotations (
                        quotation_id, rfq_id, supplier_id, brief_id, company_name,
                        version, status, submission_date, validity_end_date, file_type,
                        raw_file_path, file_hash_sha256, commercial_json, compliance_json,
                        reviewer_notes, created_at, updated_at
                    ) VALUES (
                        'QUOTE-ORPHAN', 'NON-EXISTENT-RFQ', 'SUPP-ALPHA', 'BRIEF-AUDIT-A', 'Alpha',
                        1, 'PARSED', datetime('now'), datetime('now'), 'PDF',
                        'path', 'hash', '{}', '{}',
                        NULL, datetime('now'), datetime('now')
                    );
                    """
                )

    def test_atomic_transaction_rollback_leaves_zero_state(self, audit_environment):
        service = audit_environment["service"]
        repo = audit_environment["repo"]

        initial_quotes = len(repo.list_quotations_for_brief("BRIEF-AUDIT-A", latest_only=False))

        # Attempt to ingest an oversized / corrupt file
        corrupt_bytes = b"%PDF-1.4\n" + b" " * (MAX_FILE_SIZE_BYTES + 1000)
        with pytest.raises(DataValidationError):
            service.ingest_quotation_file(
                rfq_id="NON_EXISTENT",
                filename="Exploit.pdf",
                content_bytes=corrupt_bytes,
                user_name="Malicious User",
            )

        post_quotes = len(repo.list_quotations_for_brief("BRIEF-AUDIT-A", latest_only=False))
        assert initial_quotes == post_quotes

    def test_duplicate_document_upload_idempotency(self, audit_environment):
        service = audit_environment["service"]
        rfq = service.repository.list_supplier_rfqs(brief_id="BRIEF-AUDIT-A")[0]

        pdf_bytes = make_synthetic_pdf(["QUOTATION", "Unit Price: $1.45 USD"])

        # Upload 1
        q1 = service.ingest_quotation_file(
            rfq_id=rfq.rfq_id,
            filename="Standard_Quote.pdf",
            content_bytes=pdf_bytes,
            user_name="Buyer Sarah",
        )

        # Upload 2 (identical file for same RFQ)
        q2 = service.ingest_quotation_file(
            rfq_id=rfq.rfq_id,
            filename="Standard_Quote_Copy.pdf",
            content_bytes=pdf_bytes,
            user_name="Buyer Sarah",
        )

        # Must return the existing quotation without creating duplicate rows
        assert q1.quotation_id == q2.quotation_id
        rfq_quotes = service.list_quotations_for_rfq(rfq.rfq_id)
        assert len(rfq_quotes) == 1


# =============================================================================
# 3. COMMERCIAL CALCULATION & LANDED COST AUDIT
# =============================================================================

class TestMilestone12ECommercialCalculationAudit:
    """
    Activity 3: Commercial calculation audit.
    Verify independent currency conversion formulas, base vs landed cost separation,
    incomplete cost disclosures, and override audit trails.
    """

    def test_currency_conversion_high_precision_formula(self):
        curr_svc = CurrencyNormalizationService()

        # Test USD -> INR conversion (benchmark ~84.50)
        usd_amount = 100.0
        inr_converted, rec_inr, _ = curr_svc.convert_amount(usd_amount, "USD", "INR")
        assert inr_converted == pytest.approx(8450.0, rel=1e-3)
        assert rec_inr.rate_type == ExchangeRateType.BENCHMARK

        # Test INR -> USD conversion (inverse)
        usd_back, _, _ = curr_svc.convert_amount(inr_converted, "INR", "USD")
        assert usd_back == pytest.approx(usd_amount, rel=1e-4)

        # Test EUR -> GBP cross-rate conversion via USD base
        eur_amount = 100.0
        gbp_converted, _, _ = curr_svc.convert_amount(eur_amount, "EUR", "GBP")
        assert gbp_converted == pytest.approx(85.8696, rel=1e-3)

    def test_landed_cost_strict_separation_and_disclosure(self):
        # 1. DDP Incoterm: Complete Landed Cost
        comm_ddp = QuotationCommercialData(
            quoted_currency="USD",
            quoted_unit_price=1.50,
            quoted_quantity=50000,
            min_order_quantity=10000,
            lead_time_weeks=4,
            payment_terms="Net 30",
            incoterms=Incoterm.DDP,
            freight_charges=0.0,
            taxes_and_duties=0.0,
            is_freight_included=True,
            is_tax_included=True,
        )
        comm_ddp.calculate_landed_cost(fx_record=None, target_currency="USD")
        assert comm_ddp.are_cost_inputs_complete is True
        assert comm_ddp.estimated_landed_unit_cost_target_curr == 1.50
        assert "DDP" in comm_ddp.landed_cost_explanation

        # 2. EXW Incoterm: Incomplete Landed Cost (Missing freight & taxes)
        comm_exw = QuotationCommercialData(
            quoted_currency="USD",
            quoted_unit_price=1.20,
            quoted_quantity=50000,
            min_order_quantity=10000,
            lead_time_weeks=4,
            payment_terms="Net 30",
            incoterms=Incoterm.EXW,
            freight_charges=None,
            taxes_and_duties=None,
            is_freight_included=False,
            is_tax_included=False,
        )
        comm_exw.calculate_landed_cost(fx_record=None, target_currency="USD")
        assert comm_exw.are_cost_inputs_complete is False
        assert comm_exw.estimated_landed_unit_cost_target_curr is None
        assert "cannot be reliably computed" in comm_exw.landed_cost_explanation.lower()
        # Verify missing costs are NEVER invented or silently zeroed
        assert "missing freight" in comm_exw.landed_cost_explanation.lower()

    def test_unavailable_fx_rate_disqualifies_price_comparison(self):
        curr_svc = CurrencyNormalizationService()

        # Non-existent currency code JPY
        converted, rec, expl = curr_svc.convert_amount(15000.0, "JPY", "USD")
        assert converted is None
        assert rec.rate_type == ExchangeRateType.UNAVAILABLE
        assert "unavailable" in expl.lower()


# =============================================================================
# 4. TECHNICAL COMPLIANCE & EVIDENCE GROUNDING AUDIT
# =============================================================================

class TestMilestone12ETechnicalComplianceAudit:
    """
    Activity 4: Technical compliance audit.
    Verify mandatory specification enforcement, non-compensability of failure by low price,
    and preservation of evidence tiers.
    """

    def test_mandatory_barrier_failure_is_strictly_non_compensable(self, audit_environment):
        service = audit_environment["service"]
        brief_a = audit_environment["brief_a"]

        rfqs = service.repository.list_supplier_rfqs(brief_id=brief_a.brief_id)
        rfq_alpha = next(r for r in rfqs if r.supplier_id == "SUPP-ALPHA")
        rfq_beta = next(r for r in rfqs if r.supplier_id == "SUPP-BETA")

        # Candidate 1: High Price ($2.00 USD), Fully Compliant (OTR 0.9, TDS Attached)
        comm_1 = QuotationCommercialData(
            quoted_currency="USD",
            quoted_unit_price=2.00,
            quoted_quantity=50000,
            min_order_quantity=10000,
            lead_time_weeks=4,
            payment_terms="Net 30",
            incoterms=Incoterm.DDP,
            is_freight_included=True,
            is_tax_included=True,
        )
        comp_1 = QuotationTechnicalCompliance(
            product_category="Flexible Packaging",
            structure_description="High Barrier AlOx Pouch",
            thickness_microns=70.0,
            otr_value="0.9 cc/m2/day",
            wvtr_value="0.5 g/m2/day",
            has_tds_document=True,
            mandatory_requirements_met=True,
        )
        q1 = service.ingest_manual_quotation(
            rfq_id=rfq_alpha.rfq_id,
            commercial_data=comm_1,
            compliance_data=comp_1,
            user_name="Buyer Sarah",
        )

        # Candidate 2: Ultra Cheap Price ($0.20 USD), Disqualified (OTR 25.0 cc/m2/day, No TDS)
        comm_2 = QuotationCommercialData(
            quoted_currency="USD",
            quoted_unit_price=0.20,
            quoted_quantity=50000,
            min_order_quantity=10000,
            lead_time_weeks=4,
            payment_terms="Net 30",
            incoterms=Incoterm.DDP,
            is_freight_included=True,
            is_tax_included=True,
        )
        comp_2 = QuotationTechnicalCompliance(
            product_category="Flexible Packaging",
            structure_description="Non-barrier LDPE Bag",
            thickness_microns=30.0,
            otr_value="25.0 cc/m2/day",  # Disqualifying failure
            wvtr_value="10.0 g/m2/day",
            has_tds_document=False,
            mandatory_requirements_met=False,
            disqualification_reasons=["OTR 25.0 cc/m2/day violates mandatory maximum 2.0 cc/m2/day."],
        )
        q2 = service.ingest_manual_quotation(
            rfq_id=rfq_beta.rfq_id,
            commercial_data=comm_2,
            compliance_data=comp_2,
            user_name="Buyer Sarah",
        )

        matrix = service.get_quotation_service().comparison_engine.compare_quotations(
            quotations=[q1, q2],
            brief=brief_a,
            target_currency="USD",
        )

        # Verify Candidate 2 cannot win despite 90% cheaper price
        assert matrix.primary_recommendation is not None
        assert matrix.primary_recommendation.quotation_id == q1.quotation_id
        assert matrix.primary_recommendation.recommendation_type == ComparisonRecommendationType.RECOMMENDED_PRIMARY

        row_q2 = next(r for r in matrix.rows if r.quotation_id == q2.quotation_id)
        assert row_q2.recommendation_type == ComparisonRecommendationType.NON_COMPLIANT_DISQUALIFIED
        assert row_q2.overall_comparison_score == 0.0

    def test_evidence_tier_classification_distinctions(self):
        extractor = QuotationDataExtractor()

        # 1. Spec with authentic TDS -> VERIFIED
        comp_verified = extractor._match_technical_compliance(
            "Structure: 12u PET / 50u PE. OTR: 1.2 cc/m2/day. ASTM D3985 test report and technical data sheet attached."
        )
        assert comp_verified.has_tds_document is True
        assert comp_verified.evidence_status_map[EvidenceCategory.BARRIER_FILM_EXPERIENCE.value] == EvidenceStatus.VERIFIED

        # 2. Spec with verbal claim only -> SELF_REPORTED
        comp_self_reported = extractor._match_technical_compliance(
            "Structure: 12u PET / 50u PE. We claim OTR is 1.2 cc/m2/day without official laboratory test report."
        )
        assert comp_self_reported.has_tds_document is False
        assert comp_self_reported.evidence_status_map[EvidenceCategory.BARRIER_FILM_EXPERIENCE.value] == EvidenceStatus.SELF_REPORTED


# =============================================================================
# 5. SECURITY DEFENSES & CROSS-BRIEF ISOLATION AUDIT
# =============================================================================

class TestMilestone12ESecurityAudit:
    """
    Activity 5: Security audit.
    Verify upload validation, magic byte checking, path traversal protection,
    cross-brief confidentiality isolation, and zero unauthorized external messaging.
    """

    def test_file_upload_security_and_magic_byte_validation(self):
        # 1. Valid PDF with %PDF- header
        valid_pdf = b"%PDF-1.4\nSafe content"
        assert validate_quotation_file("quote.pdf", valid_pdf)[0] is True

        # 2. Executable disguised as PDF (magic byte mismatch)
        fake_pdf = b"MZ\x90\x00\x03\x00\x00\x00BinaryExeContent"
        is_valid, msg = validate_quotation_file("trojan.pdf", fake_pdf)
        assert is_valid is False
        assert "magic signature" in msg.lower() or "header does not match" in msg.lower()

        # 3. Disallowed extension
        assert validate_quotation_file("script.py", b"print('hack')")[0] is False
        assert validate_quotation_file("payload.exe", b"MZ")[0] is False

        # 4. Path traversal in sanitize_filename
        assert sanitize_filename("../../../../etc/shadow.pdf") == "shadow.pdf"
        assert sanitize_filename("..\\..\\boot.ini") == "boot.ini"

    def test_cross_brief_confidentiality_isolation(self, audit_environment):
        """
        Verify that quotations submitted under Brief A are strictly isolated
        and never leak into comparisons or queries for Brief B.
        """
        service = audit_environment["service"]
        brief_a = audit_environment["brief_a"]
        brief_b = audit_environment["brief_b"]

        rfq_a = service.repository.list_supplier_rfqs(brief_id=brief_a.brief_id)[0]
        rfq_b = service.repository.list_supplier_rfqs(brief_id=brief_b.brief_id)[0]

        # Submit Quote under Brief A
        pdf_a = make_synthetic_pdf(["BRIEF A CONFIDENTIAL QUOTE", "Unit Price: $1.45 USD"])
        q_a = service.ingest_quotation_file(
            rfq_id=rfq_a.rfq_id,
            filename="BriefA_Quote.pdf",
            content_bytes=pdf_a,
            user_name="Sarah Auditor",
        )

        # Submit Quote under Brief B
        pdf_b = make_synthetic_pdf(["BRIEF B PHARMA CONFIDENTIAL QUOTE", "Unit Price: $8.50 USD"])
        q_b = service.ingest_quotation_file(
            rfq_id=rfq_b.rfq_id,
            filename="BriefB_Quote.pdf",
            content_bytes=pdf_b,
            user_name="Pharma Auditor",
        )

        # 1. Comparison for Brief A must contain ONLY Brief A's quotation
        matrix_a = service.compare_quotations_for_brief(brief_id=brief_a.brief_id)
        assert len(matrix_a.rows) == 1
        assert matrix_a.rows[0].quotation_id == q_a.quotation_id
        assert all(r.quotation_id != q_b.quotation_id for r in matrix_a.rows)

        # 2. Comparison for Brief B must contain ONLY Brief B's quotation
        matrix_b = service.compare_quotations_for_brief(brief_id=brief_b.brief_id)
        assert len(matrix_b.rows) == 1
        assert matrix_b.rows[0].quotation_id == q_b.quotation_id
        assert all(r.quotation_id != q_a.quotation_id for r in matrix_b.rows)


# =============================================================================
# 6. PRODUCTION DATABASE ZERO-ALTERATION AUDIT
# =============================================================================

class TestMilestone12EProductionDatabaseIntegrity:
    """
    Activity 7: Production database integrity audit.
    Verify that vendoriq.sqlite baseline records are strictly untouched.
    """

    def test_production_database_strictly_unmodified(self):
        prod_db = "vendoriq.sqlite"
        assert Path(prod_db).exists(), "Production database vendoriq.sqlite must exist."

        with sqlite3.connect(prod_db) as conn:
            cursor = conn.cursor()

            # Integrity check
            integrity = cursor.execute("PRAGMA integrity_check").fetchone()
            assert integrity[0] == "ok", f"Integrity check failed: {integrity}"

            # Strict baseline table counts verification
            counts = {
                "sourcing_briefs": 2,
                "supplier_profiles": 18,
                "scoring_runs": 2,
                "supplier_evaluations": 36,
                "supplier_evidence": 126,
                "supplier_shortlists": 9,
                "shortlist_items": 18,
                "rfq_documents": 0,
                "supplier_rfqs": 0,
                "rfq_audit_trail": 0,
                "supplier_quotations": 0,
                "quotation_audit_trail": 0,
            }

            for table_name, expected_count in counts.items():
                actual = cursor.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                assert actual == expected_count, (
                    f"Table '{table_name}' count mismatch! Expected {expected_count}, got {actual}."
                )
