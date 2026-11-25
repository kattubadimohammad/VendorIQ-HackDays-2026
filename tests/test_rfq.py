"""
Unit and Integration Tests for VendorIQ Phase 11:
Professional RFQ Generation & Human Approval Workflow.
"""

from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
import pytest
import sqlite3
import tempfile

from core.models import (
    SourcingBrief,
    SourcingCriteria,
    SupplierProfile,
    DataField,
    EvidenceCategory,
    EvidenceStatus,
    SupplierEvidenceRecord,
    RFQStatus,
    VALID_RFQ_TRANSITIONS,
    RFQAuditEntry,
    RFQBuyerInfo,
    RFQProductRequirements,
    RFQCommercialRequirements,
    RFQSupplierTarget,
    RFQDocumentRecord,
    ShortlistStatus,
)
from core.exceptions import DataValidationError
from db.connection import get_db_connection, init_db
from db.repository import VendorIQRepository
from services.rfq_generator import RFQDocumentGenerator
from services.rfq_service import RFQService


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def test_db_path(tmp_path):
    """Provides a fresh isolated test SQLite database."""
    db_file = tmp_path / "test_vendoriq.sqlite"
    init_db(str(db_file))
    return str(db_file)


@pytest.fixture
def repo(test_db_path):
    """Provides repository attached to fresh test SQLite."""
    return VendorIQRepository(db_path=test_db_path)


@pytest.fixture
def sample_brief(repo):
    """Creates a sample sourcing brief."""
    criteria = SourcingCriteria(
        target_industries=["Packaging and Containers Manufacturing"],
        target_geographies=["India"],
        min_company_size=200,
        required_certifications=["ISO 9001", "BRC Packaging"],
    )
    brief = SourcingBrief(
        brief_id="BRIEF-TEST-RFQ-01",
        title="Barrier Films Sourcing Project",
        buyer_organization="Acme Procurement Corp",
        description="High barrier flexible films for food packaging export.",
        criteria=criteria,
    )
    return repo.create_brief(brief)


@pytest.fixture
def sample_supplier(repo):
    """Creates a sample supplier profile."""
    supp = SupplierProfile(
        supplier_id="SUPP-TEST-001",
        company_name="Alpha Polymers Ltd.",
        domain="alphapolymers.com",
        corporate_email="sales@alphapolymers.com",
        industries=["Packaging and Containers Manufacturing"],
        employee_range="201-500",
        headquarters_country="India",
        headquarters_city="Mumbai",
    )
    return repo.create_supplier(supp)


@pytest.fixture
def sample_rfq_record(sample_brief, sample_supplier):
    """Creates a baseline valid RFQDocumentRecord."""
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(days=21)

    buyer = RFQBuyerInfo(
        buyer_organization="Acme Procurement Corp",
        rfq_reference="RFQ-TEST-001",
        issue_date=now,
        response_deadline=deadline,
        contact_email="rfq@acme.com",
        contact_person="Jane Doe",
    )
    target = RFQSupplierTarget(
        supplier_id=sample_supplier.supplier_id,
        company_name=sample_supplier.company_name,
        domain=sample_supplier.domain,
        headquarters="Mumbai, India",
        domain_mismatch_warning=None,
        missing_evidence_requests=["TDS for EVOH barrier structure"],
        technical_questionnaire=["Confirm maximum line width in mm."],
    )
    prod = RFQProductRequirements(
        category="Flexible Packaging",
        description="Multi-layer high barrier flexible film rolls",
        technical_specifications=["WVTR < 0.5 g/m2/day", "OTR < 1.0 cc/m2/day"],
        barrier_film_requirements="High oxygen and moisture barrier",
        expected_quantity="50,000 meters",
        packaging_requirements="Export palletization",
    )
    comm = RFQCommercialRequirements(
        currency="USD",
        target_unit_price=4.50,
        min_order_quantity=5000,
        production_lead_time_weeks=4,
        payment_terms="Net 30 days",
        incoterms="FOB Mumbai",
        delivery_location="Mumbai Port",
        quotation_validity_days=60,
    )

    return RFQDocumentRecord(
        rfq_id="RFQ-DOC-UNIT-01",
        brief_id=sample_brief.brief_id,
        supplier_id=sample_supplier.supplier_id,
        title=f"RFQ for {sample_brief.title} – {sample_supplier.company_name}",
        buyer_info=buyer,
        supplier=target,
        product_requirements=prod,
        commercial_requirements=comm,
        requested_documents=["TDS", "ASTM F1249", "ASTM D3985", "ISO 9001"],
        status=RFQStatus.DRAFT,
        created_at=now,
        updated_at=now,
    )


# =============================================================================
# 1. Model & Validation Tests
# =============================================================================

def test_rfq_lifecycle_statuses_and_transitions():
    """Verify all 7 RFQ statuses and transition mapping integrity."""
    expected_statuses = {
        "DRAFT",
        "PENDING_APPROVAL",
        "APPROVED",
        "REJECTED",
        "SENT",
        "RESPONSE_RECEIVED",
        "CLOSED",
    }
    actual_statuses = {s.value for s in RFQStatus}
    assert actual_statuses == expected_statuses

    # Verify transitions dictionary covers every status
    for st in RFQStatus:
        assert st in VALID_RFQ_TRANSITIONS

    # Verify DRAFT can only transition to PENDING_APPROVAL
    assert VALID_RFQ_TRANSITIONS[RFQStatus.DRAFT] == [RFQStatus.PENDING_APPROVAL]

    # Verify PENDING_APPROVAL can transition to APPROVED or REJECTED
    assert set(VALID_RFQ_TRANSITIONS[RFQStatus.PENDING_APPROVAL]) == {
        RFQStatus.APPROVED,
        RFQStatus.REJECTED,
    }


def test_confidentiality_guardrail_enforcement(sample_rfq_record):
    """Verify that forbidden internal scoring or competitor keywords raise DataValidationError."""
    gen = RFQDocumentGenerator()

    # Baseline valid RFQ passes
    gen.verify_confidentiality_guardrails(sample_rfq_record)

    # Injecting forbidden total_score raises error
    sample_rfq_record.title = "RFQ with total_score: 95.0"
    with pytest.raises(DataValidationError, match="Confidentiality guardrail violated"):
        gen.verify_confidentiality_guardrails(sample_rfq_record)

    # Injecting confidential competitor details raises error
    sample_rfq_record.title = "Valid Title"
    sample_rfq_record.product_requirements.description = "Compare with competitor Jumbo Bag"
    with pytest.raises(DataValidationError, match="Confidentiality guardrail violated"):
        gen.verify_confidentiality_guardrails(sample_rfq_record)


# =============================================================================
# 2. Repository Layer Tests
# =============================================================================

def test_rfq_repository_crud_and_audit(repo, sample_rfq_record):
    """Verify persistence of supplier RFQs and automatic audit trail tracking."""
    # 1. Create
    created = repo.create_supplier_rfq(sample_rfq_record)
    assert created.rfq_id == sample_rfq_record.rfq_id
    assert created.status == RFQStatus.DRAFT
    assert len(created.audit_trail) == 1
    assert created.audit_trail[0].to_status == "DRAFT"

    # 2. Retrieve
    retrieved = repo.get_supplier_rfq(sample_rfq_record.rfq_id)
    assert retrieved is not None
    assert retrieved.supplier.company_name == "Alpha Polymers Ltd."
    assert retrieved.product_requirements.expected_quantity == "50,000 meters"
    assert len(retrieved.audit_trail) == 1

    # 3. List
    rfqs = repo.list_supplier_rfqs(brief_id=sample_rfq_record.brief_id)
    assert len(rfqs) == 1

    by_status = repo.list_supplier_rfqs(status="DRAFT")
    assert len(by_status) == 1

    empty_list = repo.list_supplier_rfqs(status="APPROVED")
    assert len(empty_list) == 0

    # 4. Update
    sample_rfq_record.commercial_requirements.target_unit_price = 5.20
    sample_rfq_record.docx_path = "/storage/test.docx"
    repo.update_supplier_rfq(sample_rfq_record)
    updated = repo.get_supplier_rfq(sample_rfq_record.rfq_id)
    assert updated.commercial_requirements.target_unit_price == 5.20
    assert updated.docx_path == "/storage/test.docx"

    # 5. Delete
    assert repo.delete_supplier_rfq(sample_rfq_record.rfq_id) is True
    assert repo.get_supplier_rfq(sample_rfq_record.rfq_id) is None
    # Audit trail cascaded
    assert repo.get_rfq_audit_trail(sample_rfq_record.rfq_id) == []


# =============================================================================
# 3. Lifecycle Status Transitions & Human Gate Tests
# =============================================================================

def test_rfq_lifecycle_transitions_and_human_gates(repo, sample_rfq_record):
    """Test full 7-step lifecycle with strict human review rules."""
    repo.create_supplier_rfq(sample_rfq_record)
    rfq_id = sample_rfq_record.rfq_id

    # 1. Invalid jump: DRAFT directly to APPROVED or SENT fails
    with pytest.raises(DataValidationError, match="Invalid RFQ status transition"):
        repo.transition_rfq_status(rfq_id, RFQStatus.APPROVED, changed_by="Buyer")

    with pytest.raises(DataValidationError, match="Invalid RFQ status transition"):
        repo.transition_rfq_status(rfq_id, RFQStatus.SENT, changed_by="Buyer")

    # 2. Transition: DRAFT -> PENDING_APPROVAL
    rfq = repo.transition_rfq_status(
        rfq_id,
        RFQStatus.PENDING_APPROVAL,
        changed_by="Sourcing Manager",
        comments="Ready for compliance review.",
    )
    assert rfq.status == RFQStatus.PENDING_APPROVAL

    # 3. Approval requires reviewer comments
    with pytest.raises(DataValidationError, match="Reviewer comments and approval notes are mandatory"):
        repo.transition_rfq_status(rfq_id, RFQStatus.APPROVED, changed_by="Lead Approver", comments="")

    # 4. Transition: PENDING_APPROVAL -> APPROVED
    rfq = repo.transition_rfq_status(
        rfq_id,
        RFQStatus.APPROVED,
        changed_by="Procurement Director",
        comments="Approved for supplier distribution.",
    )
    assert rfq.status == RFQStatus.APPROVED

    # 5. Transition: APPROVED -> SENT (Manual dispatch sign-off)
    rfq = repo.transition_rfq_status(
        rfq_id,
        RFQStatus.SENT,
        changed_by="Logistics Buyer",
        comments="Manual email transmitted with signed PDF attached.",
    )
    assert rfq.status == RFQStatus.SENT

    # 6. Transition: SENT -> RESPONSE_RECEIVED
    rfq = repo.transition_rfq_status(
        rfq_id,
        RFQStatus.RESPONSE_RECEIVED,
        changed_by="Procurement Buyer",
        comments="Supplier submitted quotation package and TDS.",
    )
    assert rfq.status == RFQStatus.RESPONSE_RECEIVED

    # 7. Transition: RESPONSE_RECEIVED -> CLOSED
    rfq = repo.transition_rfq_status(
        rfq_id,
        RFQStatus.CLOSED,
        changed_by="Procurement Director",
        comments="Bid accepted and contract awarded.",
    )
    assert rfq.status == RFQStatus.CLOSED

    # Verify audit trail length: Initial DRAFT + 5 transitions = 6 entries
    trail = repo.get_rfq_audit_trail(rfq_id)
    assert len(trail) == 6
    assert [e.to_status for e in trail] == [
        "DRAFT",
        "PENDING_APPROVAL",
        "APPROVED",
        "SENT",
        "RESPONSE_RECEIVED",
        "CLOSED",
    ]


def test_rejection_and_revision_loop(repo, sample_rfq_record):
    """Test rejection and reversion to DRAFT for buyer revision."""
    repo.create_supplier_rfq(sample_rfq_record)
    rfq_id = sample_rfq_record.rfq_id

    # DRAFT -> PENDING_APPROVAL
    repo.transition_rfq_status(rfq_id, RFQStatus.PENDING_APPROVAL, changed_by="Buyer")

    # Rejection requires reason
    with pytest.raises(DataValidationError, match="Reviewer feedback is mandatory"):
        repo.transition_rfq_status(rfq_id, RFQStatus.REJECTED, changed_by="Auditor", comments="")

    # Reject
    rejected = repo.transition_rfq_status(
        rfq_id,
        RFQStatus.REJECTED,
        changed_by="Auditor",
        comments="Please increase lead time allowance from 4 to 8 weeks.",
    )
    assert rejected.status == RFQStatus.REJECTED

    # Revert to DRAFT for revision
    reverted = repo.transition_rfq_status(
        rfq_id,
        RFQStatus.DRAFT,
        changed_by="Buyer",
        comments="Updated lead time to 8 weeks as requested.",
    )
    assert reverted.status == RFQStatus.DRAFT


# =============================================================================
# 4. Service Layer & Truth-in-Evidence Customization Tests
# =============================================================================

def test_rfq_service_truth_in_evidence_customization(repo, sample_brief, sample_supplier, tmp_path):
    """
    Verify that RFQService tailors missing evidence requests and questions
    grounded in Phase 9 evidence records.
    """
    # Create missing barrier film evidence for Alpha Polymers
    ev = SupplierEvidenceRecord(
        evidence_id="EV-TEST-001",
        supplier_id=sample_supplier.supplier_id,
        category=EvidenceCategory.BARRIER_FILM_EXPERIENCE,
        claim="No barrier film experience identified in public profile.",
        status=EvidenceStatus.MISSING,
        source_name="Crustdata Profile",
        verified_at=datetime.now(timezone.utc),
    )
    repo.create_evidence(ev)

    service = RFQService(repository=repo, generator=RFQDocumentGenerator(str(tmp_path)))

    rfq = service.prepare_supplier_rfq(
        brief=sample_brief,
        supplier=sample_supplier,
        expected_quantity="100,000 units",
    )

    # Truth in evidence: Since barrier film evidence is MISSING, RFQ must request TDS / ASTM
    assert len(rfq.supplier.missing_evidence_requests) > 0
    assert any("ASTM" in req or "TDS" in req for req in rfq.supplier.missing_evidence_requests)

    # Questionnaire must contain barrier inquiries
    assert any("ASTM" in q or "barrier" in q.lower() for q in rfq.supplier.technical_questionnaire)

    # Confidentiality: verify no score fields present
    assert not hasattr(rfq, "total_score")
    assert not hasattr(rfq.supplier, "total_score")


# =============================================================================
# 5. Document Generation Tests (DOCX & PDF)
# =============================================================================

def test_document_generation_docx_and_pdf(sample_rfq_record, tmp_path):
    """Verify that both DOCX and PDF documents are generated and well-formed."""
    gen = RFQDocumentGenerator(base_storage_dir=str(tmp_path))
    docx_path, pdf_path = gen.generate_all(sample_rfq_record)

    # Check files exist on disk
    assert os.path.exists(docx_path)
    assert os.path.exists(pdf_path)

    # Check non-zero size
    assert os.path.getsize(docx_path) > 1000
    assert os.path.getsize(pdf_path) > 1000

    # Verify PDF starts with %PDF magic bytes
    with open(pdf_path, "rb") as f:
        header = f.read(5)
        assert header.startswith(b"%PDF")

    # Verify DOCX content
    import docx
    doc = docx.Document(docx_path)
    full_text = " ".join([p.text for p in doc.paragraphs])
    assert "REQUEST FOR QUOTATION (RFQ)" in full_text
    assert sample_rfq_record.title in full_text
    assert "Mandatory Technical & Compliance Documentation Checklist" in full_text
    assert "External Contact Safeguard" in full_text
    assert sample_rfq_record.product_requirements.expected_quantity in full_text


def test_service_approval_triggers_document_generation(repo, sample_brief, sample_supplier, tmp_path):
    """Verify that approving an RFQ via RFQService automatically generates DOCX and PDF."""
    service = RFQService(repository=repo, generator=RFQDocumentGenerator(str(tmp_path)))

    rfq = service.create_and_persist_rfq(
        brief=sample_brief,
        supplier=sample_supplier,
        expected_quantity="25,000 rolls",
    )
    assert rfq.docx_path is None
    assert rfq.pdf_path is None

    # Submit for approval
    service.submit_for_approval(rfq.rfq_id, "Buyer Alice", "Ready for review")

    # Approve
    approved = service.approve_rfq(
        rfq.rfq_id,
        reviewer_name="Director Bob",
        approval_notes="Approved for official procurement.",
    )
    assert approved.status == RFQStatus.APPROVED
    assert approved.docx_path is not None
    assert approved.pdf_path is not None
    assert os.path.exists(approved.docx_path)
    assert os.path.exists(approved.pdf_path)


# =============================================================================
# 6. Analytics KPIs Test
# =============================================================================

def test_rfq_kpis_computation(repo, sample_brief, sample_supplier, tmp_path):
    """Verify that get_rfq_kpis returns accurate counts across all 7 statuses."""
    service = RFQService(repository=repo, generator=RFQDocumentGenerator(str(tmp_path)))

    rfq1 = service.create_and_persist_rfq(sample_brief, sample_supplier)
    rfq2 = service.create_and_persist_rfq(sample_brief, sample_supplier)

    # Creation is intentionally idempotent for the same brief/supplier pair:
    # a repeated request reuses the existing RFQ rather than duplicating it.
    assert rfq2.rfq_id == rfq1.rfq_id

    service.submit_for_approval(rfq2.rfq_id, "Buyer", "Submit")
    service.approve_rfq(rfq2.rfq_id, "Director", "Approved")

    kpis = service.get_rfq_kpis(sample_brief.brief_id)
    assert kpis["total"] == 1
    assert kpis["draft"] == 0
    assert kpis["approved"] == 1
    assert kpis["pending_approval"] == 0
    assert kpis["sent"] == 0


# =============================================================================
# 7. Shortlist to RFQ Batch Generation Test
# =============================================================================

def test_create_rfqs_from_shortlist(repo, sample_brief, sample_supplier, tmp_path):
    """Verify batch generation of RFQs from a supplier shortlist."""
    from core.models import SupplierShortlist
    from services.discovery_service import SupplierDiscoveryService

    disc_service = SupplierDiscoveryService(repository=repo)
    shortlist = disc_service.create_shortlist(
        brief_id=sample_brief.brief_id,
        name="Phase 11 Primary Shortlist",
        description="Shortlist for RFQ batch generation",
    )
    disc_service.add_supplier_to_shortlist(
        shortlist_id=shortlist.shortlist_id,
        supplier_id=sample_supplier.supplier_id,
        reviewer_notes="High priority candidate",
    )

    rfq_service = RFQService(repository=repo, generator=RFQDocumentGenerator(str(tmp_path)))
    rfqs = rfq_service.create_rfqs_from_shortlist(
        shortlist_id=shortlist.shortlist_id,
        brief=sample_brief,
        expected_quantity="75,000 meters",
    )

    assert len(rfqs) == 1
    assert rfqs[0].supplier_id == sample_supplier.supplier_id
    assert rfqs[0].shortlist_id == shortlist.shortlist_id
    assert rfqs[0].product_requirements.expected_quantity == "75,000 meters"
    assert rfqs[0].status == RFQStatus.DRAFT


# =============================================================================
# 8. Dashboard Data Service Integration Test
# =============================================================================

def test_dashboard_data_service_rfq_integration(test_db_path, sample_brief, sample_supplier):
    """Verify DashboardDataService properly exposes all RFQ operations."""
    from ui.data_service import DashboardDataService
    dash_service = DashboardDataService(db_path=test_db_path)

    # 1. Create RFQ via data service
    rfq = dash_service.create_and_persist_supplier_rfq(
        brief=sample_brief,
        supplier=sample_supplier,
        expected_quantity="30,000 units",
    )
    assert rfq.rfq_id is not None
    assert rfq.status == RFQStatus.DRAFT

    # 2. List RFQs
    rfqs = dash_service.list_supplier_rfqs(brief_id=sample_brief.brief_id)
    assert len(rfqs) == 1

    # 3. Submit for approval
    dash_service.submit_rfq_for_approval(rfq.rfq_id, "Buyer Jane", "Ready for review")
    updated = dash_service.get_supplier_rfq(rfq.rfq_id)
    assert updated.status == RFQStatus.PENDING_APPROVAL

    # 4. Approve
    approved = dash_service.approve_rfq(rfq.rfq_id, "Director Dan", "Approved for dispatch")
    assert approved.status == RFQStatus.APPROVED
    assert approved.docx_path is not None
    assert approved.pdf_path is not None

    # 5. Manual dispatch sign-off
    sent = dash_service.record_manual_dispatch(rfq.rfq_id, "Logistics Lead", "Dispatched via corporate procurement portal")
    assert sent.status == RFQStatus.SENT

    # 6. Response received
    responded = dash_service.record_response_received(rfq.rfq_id, "Buyer Jane", "Supplier quoted $4.10 FOB")
    assert responded.status == RFQStatus.RESPONSE_RECEIVED

    # 7. Close
    closed = dash_service.close_supplier_rfq(rfq.rfq_id, "Director Dan", "Contract executed")
    assert closed.status == RFQStatus.CLOSED

    # 8. Audit trail
    trail = dash_service.get_rfq_audit_trail(rfq.rfq_id)
    assert len(trail) == 6


# =============================================================================
# 9. RFQ View Module Import & Signature Check
# =============================================================================

def test_rfq_view_import_and_signature():
    """Verify that ui.views.rfq_view imports cleanly and exposes render_rfq_view."""
    from ui.views.rfq_view import render_rfq_view
    import inspect

    sig = inspect.signature(render_rfq_view)
    params = list(sig.parameters.keys())
    assert params == ["service", "brief", "suppliers", "evaluations"]
