"""
Tests for VendorIQ SQLite Database and Repository Layer (Phase 4).

Validates table creation, CRUD operations, domain caching, duplicate resolution,
foreign key cascades, transaction rollback, and provenance/weight persistence.
All tests run against isolated temporary databases.
"""

from datetime import datetime, timezone, timedelta
import sqlite3
import pytest

from core.models import (
    SourcingBrief,
    SourcingCriteria,
    SupplierProfile,
    Certification,
    DataField,
    ScoringWeights,
    ScoringDimension,
    FactorScore,
    SupplierScore,
    RFQDraft,
)
from core.provenance import (
    ProvenanceClassification,
    create_verified_provenance,
    create_unverified_provenance,
    create_buyer_specified_provenance,
)
from core.exceptions import DataValidationError, MissingSupplierDataError
from db.connection import get_db_connection, init_db
from db.repository import VendorIQRepository


@pytest.fixture
def test_db_path(tmp_path):
    """Provide an isolated temporary database path for each test."""
    db_file = tmp_path / "test_vendoriq.sqlite"
    return str(db_file)


@pytest.fixture
def repo(test_db_path):
    """Provide an initialized repository using an isolated temporary database."""
    r = VendorIQRepository(db_path=test_db_path)
    r.initialize()
    return r


# ---------------------------------------------------------------------------
# Task 1 & 2: Database Initialization and Schema Verification
# ---------------------------------------------------------------------------

def test_database_initialization_creates_all_tables(repo, test_db_path):
    """Verify that init_db creates all 5 tables and required indexes."""
    expected_tables = {
        "sourcing_briefs",
        "supplier_profiles",
        "scoring_runs",
        "supplier_evaluations",
        "rfq_documents",
    }
    with get_db_connection(test_db_path) as conn:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row["name"] for row in cur.fetchall()}

    assert expected_tables.issubset(tables)

    # Re-running initialization must be idempotent and safe
    repo.initialize()


# ---------------------------------------------------------------------------
# Task 3: Sourcing Brief CRUD Operations
# ---------------------------------------------------------------------------

def test_sourcing_brief_crud(repo):
    """Verify insert, retrieve, update, and delete for sourcing briefs."""
    criteria = SourcingCriteria(
        target_industries=["Additive Manufacturing", "Aerospace CNC"],
        target_geographies=["US", "DE"],
        min_company_size=20,
        max_company_size=250,
        required_certifications=["AS9100D"],
        min_annual_revenue_usd=1_000_000.0,
        max_annual_revenue_usd=50_000_000.0,
    )
    brief = SourcingBrief(
        brief_id="BRIEF-TEST-001",
        title="High Precision Titanium Components",
        buyer_organization="Boeing Defense Systems",
        description="Sourcing tier-1/tier-2 suppliers for aerospace titanium brackets.",
        criteria=criteria,
        budget_range_usd=(250_000.0, 750_000.0),
        timeline_weeks=16,
        notes="High priority aerospace requirement",
    )

    # 1. Create
    created = repo.create_brief(brief)
    assert created.brief_id == "BRIEF-TEST-001"

    # 2. Retrieve
    retrieved = repo.get_brief("BRIEF-TEST-001")
    assert retrieved is not None
    assert retrieved.title == brief.title
    assert retrieved.buyer_organization == brief.buyer_organization
    assert retrieved.criteria.target_industries == ["Additive Manufacturing", "Aerospace CNC"]
    assert retrieved.budget_range_usd == (250_000.0, 750_000.0)

    # 3. Update
    brief.title = "High Precision Titanium Components (Revision 2)"
    brief.notes = "Updated priority"
    updated = repo.update_brief(brief)
    assert updated is not None
    assert updated.title == "High Precision Titanium Components (Revision 2)"

    check_updated = repo.get_brief("BRIEF-TEST-001")
    assert check_updated.title == "High Precision Titanium Components (Revision 2)"
    assert check_updated.notes == "Updated priority"

    # 4. List
    briefs = repo.list_briefs()
    assert len(briefs) == 1
    assert briefs[0].brief_id == "BRIEF-TEST-001"

    # 5. Delete
    deleted = repo.delete_brief("BRIEF-TEST-001")
    assert deleted is True
    assert repo.get_brief("BRIEF-TEST-001") is None


def test_duplicate_brief_prevention(repo):
    """Verify repository deduplicates identical briefs and allows genuinely different ones."""
    crit1 = SourcingCriteria(
        target_industries=["Packaging and Containers Manufacturing"],
        target_geographies=["India"],
        min_company_size=200,
        max_company_size=None,
        required_certifications=["ISO 9001"],
    )
    brief1 = SourcingBrief(
        brief_id="BRIEF-ORIGINAL-01",
        title="Sustainable Packaging India",
        buyer_organization="Acme Procurement",
        description="Original brief for sustainable packaging in India.",
        criteria=crit1,
    )
    saved1 = repo.create_brief(brief1)
    assert saved1.brief_id == "BRIEF-ORIGINAL-01"

    # Attempt to insert identical brief with different brief_id and whitespace/case variations in criteria
    crit2 = SourcingCriteria(
        target_industries=[" packaging and containers manufacturing "],
        target_geographies=["INDIA"],
        min_company_size=200,
        max_company_size=None,
        required_certifications=["iso 9001"],
    )
    brief2 = SourcingBrief(
        brief_id="BRIEF-DUPLICATE-02",
        title="Sustainable Packaging India",
        buyer_organization="Acme Procurement",
        description="Duplicate brief submitted via double-click or rerun.",
        criteria=crit2,
    )
    saved2 = repo.create_brief(brief2)
    # Deduplication must return existing brief1 and not insert brief2
    assert saved2.brief_id == "BRIEF-ORIGINAL-01"
    assert len(repo.list_briefs()) == 1

    # Insert a genuinely different brief (different criteria)
    crit3 = SourcingCriteria(
        target_industries=["Aerospace CNC"],
        target_geographies=["Germany"],
        min_company_size=50,
        max_company_size=500,
    )
    brief3 = SourcingBrief(
        brief_id="BRIEF-DIFFERENT-03",
        title="Aerospace CNC Machining",
        buyer_organization="Acme Procurement",
        description="Genuinely different sourcing brief.",
        criteria=crit3,
    )
    saved3 = repo.create_brief(brief3)
    assert saved3.brief_id == "BRIEF-DIFFERENT-03"
    assert len(repo.list_briefs()) == 2


def test_copilot_browser_submission_duplicate_regression(repo):
    """
    Regression test reproducing the exact failure where BRIEF-0311EB47 was created
    despite existing BRIEF-DCD8044A.

    Verifies that differences in:
    - max_company_size (None vs 2000 default)
    - required_certifications ([] vs generic sustainability requirement string)
    - notes (populated vs None)
    - description minor variations
    are recognized as the same semantic brief and reused.
    """
    crit_dcd = SourcingCriteria(
        target_industries=["Packaging and Containers Manufacturing"],
        target_geographies=["India"],
        min_company_size=200,
        max_company_size=None,
        required_certifications=["Sustainability certification (documentary evidence required)"],
    )
    brief_dcd = SourcingBrief(
        brief_id="BRIEF-DCD8044A",
        title="Flexible Packaging & Specialty Films Sourcing Project",
        buyer_organization="VendorIQ Procurement Team",
        description=(
            "Procurement brief extracted from buyer input: I need sustainable flexible packaging manufacturers in India. "
            "Suppliers should have at least 200 employees, experience with barrier films, and relevant sustainability certifications. "
            "Identify suitable candidates and explain missing evidence.. Focuses on Flexible Packaging & Specialty Films "
            "(Specification: Barrier films) with target delivery in India."
        ),
        criteria=crit_dcd,
        notes="Key Specification: Barrier films. Sustainability certifications: buyer requirement; documentary evidence required.",
    )
    saved_dcd = repo.create_brief(brief_dcd)
    assert saved_dcd.brief_id == "BRIEF-DCD8044A"

    # User re-submits in browser test: produces BRIEF-0311EB47 with max_company_size=2000 and required_certifications=[]
    crit_browser = SourcingCriteria(
        target_industries=["Packaging and Containers Manufacturing"],
        target_geographies=["India"],
        min_company_size=200,
        max_company_size=2000,
        required_certifications=[],
    )
    brief_browser = SourcingBrief(
        brief_id="BRIEF-0311EB47",
        title="Flexible Packaging & Specialty Films Sourcing Project",
        buyer_organization="VendorIQ Procurement Team",
        description=(
            "Procurement brief extracted from buyer input: I need sustainable flexible packaging manufacturers in India. "
            "Suppliers should have at least 200 employees, experience with barrier films, and relevant sustainability certifications. "
            "Identify suitable candidates and explain missing evidence.. Focuses on Flexible Packaging & Specialty Films with target delivery in India."
        ),
        criteria=crit_browser,
        notes=None,
    )
    saved_browser = repo.create_brief(brief_browser)

    # Must identify the duplicate and return the existing BRIEF-DCD8044A
    assert saved_browser.brief_id == "BRIEF-DCD8044A"
    all_briefs = repo.list_briefs()
    assert len(all_briefs) == 1
    assert all_briefs[0].brief_id == "BRIEF-DCD8044A"


def test_semantic_duplicate_detection_boundaries(repo):
    """
    Verify boundary conditions for semantic duplicate detection:
    - Identifies duplicates despite whitespace, casing, & vs and, and empty values.
    - Accurately distinguishes and preserves genuinely different briefs.
    """
    base_crit = SourcingCriteria(
        target_industries=["Packaging and Containers Manufacturing"],
        target_geographies=["India"],
        min_company_size=200,
        max_company_size=None,
        required_certifications=["Sustainability certification (documentary evidence required)"],
    )
    base_brief = SourcingBrief(
        brief_id="BRIEF-CANONICAL-1",
        title="Flexible Packaging & Specialty Films Sourcing Project",
        buyer_organization="VendorIQ Procurement Team",
        description="I need sustainable flexible packaging manufacturers in India with barrier films.",
        criteria=base_crit,
        notes="Key Specification: Barrier films.",
    )
    repo.create_brief(base_brief)

    # 1. Equivalent variant with different casing, & -> and, and no suffix
    variant_crit = SourcingCriteria(
        target_industries=["packaging & containers manufacturing"],
        target_geographies=["india"],
        min_company_size=200,
        max_company_size=2000,
        required_certifications=[],
    )
    variant_brief = SourcingBrief(
        brief_id="BRIEF-VARIANT-2",
        title="flexible packaging and specialty films",
        buyer_organization="vendoriq procurement team",
        description="I need sustainable flexible packaging manufacturers in India with barrier films.",
        criteria=variant_crit,
    )
    res_var = repo.create_brief(variant_brief)
    assert res_var.brief_id == "BRIEF-CANONICAL-1", "Failed to detect semantic equivalent title and unconstrained max size"

    # 2. Genuinely different: different industry (Corrugated Box)
    diff_ind_crit = SourcingCriteria(
        target_industries=["Corrugated Paper & Carton Packaging"],
        target_geographies=["India"],
        min_company_size=200,
    )
    diff_ind_brief = SourcingBrief(
        brief_id="BRIEF-DIFF-IND",
        title="Corrugated Paper & Carton Packaging Sourcing Project",
        buyer_organization="VendorIQ Procurement Team",
        description="High burst strength corrugated carton boxes in India.",
        criteria=diff_ind_crit,
    )
    res_ind = repo.create_brief(diff_ind_brief)
    assert res_ind.brief_id == "BRIEF-DIFF-IND", "Incorrectly merged brief with different industry"

    # 3. Genuinely different: different geography (Germany)
    diff_geo_crit = SourcingCriteria(
        target_industries=["Packaging and Containers Manufacturing"],
        target_geographies=["Germany"],
        min_company_size=200,
    )
    diff_geo_brief = SourcingBrief(
        brief_id="BRIEF-DIFF-GEO",
        title="Flexible Packaging in Germany",
        buyer_organization="VendorIQ Procurement Team",
        description="Sustainable flexible packaging in Germany.",
        criteria=diff_geo_crit,
    )
    res_geo = repo.create_brief(diff_geo_brief)
    assert res_geo.brief_id == "BRIEF-DIFF-GEO", "Incorrectly merged brief with different geography"

    # 4. Genuinely different: different minimum headcount threshold (50 vs 200)
    diff_sz_crit = SourcingCriteria(
        target_industries=["Packaging and Containers Manufacturing"],
        target_geographies=["India"],
        min_company_size=50,
    )
    diff_sz_brief = SourcingBrief(
        brief_id="BRIEF-DIFF-SIZE",
        title="Flexible Packaging for Small Suppliers",
        buyer_organization="VendorIQ Procurement Team",
        description="Flexible packaging for suppliers with at least 50 headcount.",
        criteria=diff_sz_crit,
    )
    res_sz = repo.create_brief(diff_sz_brief)
    assert res_sz.brief_id == "BRIEF-DIFF-SIZE", "Incorrectly merged brief with different minimum headcount"

    # 5. Genuinely different: specific formal standard (ISO 9001 mandatory)
    diff_cert_crit = SourcingCriteria(
        target_industries=["Packaging and Containers Manufacturing"],
        target_geographies=["India"],
        min_company_size=200,
        required_certifications=["ISO 9001"],
    )
    diff_cert_brief = SourcingBrief(
        brief_id="BRIEF-DIFF-ISO",
        title="Flexible Packaging with Mandatory ISO 9001",
        buyer_organization="VendorIQ Procurement Team",
        description="Requires accredited ISO 9001 registered facilities.",
        criteria=diff_cert_crit,
    )
    res_cert = repo.create_brief(diff_cert_brief)
    assert res_cert.brief_id == "BRIEF-DIFF-ISO", "Incorrectly merged brief with specific formal accredited certification"


# ---------------------------------------------------------------------------
# Task 3: Supplier Profiles, Domain Caching & Duplicate Handling
# ---------------------------------------------------------------------------

def test_supplier_profile_crud_and_provenance(repo):
    """Verify supplier profile storage, provenance persistence, and certification integrity."""
    # Build verified certification with registry evidence
    prov_verified = create_verified_provenance(
        source="IAF CertSearch",
        evidence="IAF-REG-991244",
        confidence=1.0,
    )
    cert_verified = Certification(
        name="ISO 9001:2015",
        issuing_body="BSI Group",
        is_verified=True,
        verification_evidence="IAF-REG-991244",
        provenance=prov_verified,
    )

    # Build unverified self-reported certification
    prov_unverified = create_unverified_provenance(source="Vendor Website Claim")
    cert_unverified = Certification(
        name="ISO 14001:2015",
        issuing_body="Self Reported",
        is_verified=False,
        verification_evidence=None,
        provenance=prov_unverified,
    )

    # Provenanced company attribute
    revenue_attr = DataField.verified(
        name="audited_annual_revenue",
        value=15_000_000.0,
        source="SEC 10-K Filing",
        evidence="https://sec.gov/filing/987654",
    )

    supplier = SupplierProfile(
        supplier_id="SUPP-ACME-01",
        company_name="Acme Precision Machining Inc.",
        domain="acme-machining.com",
        description="AS9100 certified 5-axis CNC titanium machining.",
        headquarters_country="US",
        headquarters_city="Wichita",
        employee_count=85,
        employee_range="50-100",
        industries=["Aerospace CNC", "Tool & Die"],
        certifications=[cert_verified, cert_unverified],
        annual_revenue_usd=15_000_000.0,
        year_founded=1998,
        business_signals=["Recent facility expansion", "AS9100 recertified 2026"],
        corporate_contact_email="sales@acme-machining.com",
        corporate_website="https://acme-machining.com",
        attributes={"revenue": revenue_attr},
        is_synthetic=False,
        overall_provenance=ProvenanceClassification.UNVERIFIED_OR_MISSING,
    )

    # 1. Create
    repo.create_supplier(supplier)

    # 2. Retrieve by ID
    retrieved = repo.get_supplier_by_id("SUPP-ACME-01")
    assert retrieved is not None
    assert retrieved.company_name == "Acme Precision Machining Inc."
    assert retrieved.domain == "acme-machining.com"
    assert len(retrieved.certifications) == 2

    # Verify separation of verified vs unverified certifications
    assert len(retrieved.verified_certifications) == 1
    assert retrieved.verified_certifications[0].name == "ISO 9001:2015"
    assert retrieved.verified_certifications[0].verification_evidence == "IAF-REG-991244"

    assert len(retrieved.unverified_certifications) == 1
    assert retrieved.unverified_certifications[0].name == "ISO 14001:2015"
    assert retrieved.unverified_certifications[0].is_verified is False

    # Verify attribute provenance persistence
    assert "revenue" in retrieved.attributes
    attr = retrieved.attributes["revenue"]
    assert attr.value == 15_000_000.0
    assert attr.provenance.classification == ProvenanceClassification.VERIFIED_DATA
    assert attr.provenance.evidence == "https://sec.gov/filing/987654"


def test_duplicate_supplier_domain_handling(repo):
    """Verify duplicate supplier domains are rejected on create but handled gracefully via upsert."""
    sup1 = SupplierProfile(
        supplier_id="SUPP-001",
        company_name="Apex Tooling",
        domain="apex-tooling.com",
        headquarters_country="US",
        employee_count=50,
    )
    repo.create_supplier(sup1)

    # Attempting to create duplicate domain with different ID must raise DataValidationError
    sup2 = SupplierProfile(
        supplier_id="SUPP-002",
        company_name="Apex Tooling Alternate",
        domain="apex-tooling.com",  # Duplicate domain!
        headquarters_country="US",
        employee_count=60,
    )
    with pytest.raises(DataValidationError) as exc:
        repo.create_supplier(sup2)
    assert "Duplicate supplier domain" in str(exc.value)

    # Using upsert_supplier with duplicate domain must update existing record
    sup2_updated = SupplierProfile(
        supplier_id="IGNORED_ID",
        company_name="Apex Tooling Updated Corp",
        domain="apex-tooling.com",
        headquarters_country="US",
        employee_count=120,
    )
    upserted = repo.upsert_supplier(sup2_updated)
    assert upserted.supplier_id == "SUPP-001"  # Retains original supplier_id
    assert upserted.employee_count == 120

    cached = repo.get_supplier_by_domain("apex-tooling.com")
    assert cached.employee_count == 120
    assert cached.company_name == "Apex Tooling Updated Corp"


def test_cache_retrieval_by_domain(repo):
    """Verify cache retrieval works with whitespace/case normalization and returns None on miss."""
    supplier = SupplierProfile(
        supplier_id="SUPP-CACHE-1",
        company_name="Cache Tech Ltd",
        domain="cachetech.io",
        headquarters_country="DE",
    )
    repo.create_supplier(supplier)

    # Lookup by identical domain
    assert repo.get_supplier_by_domain("cachetech.io") is not None

    # Lookup with whitespace and uppercase
    found = repo.get_supplier_by_domain("  CACHETECH.IO  ")
    assert found is not None
    assert found.supplier_id == "SUPP-CACHE-1"

    # Cache miss
    assert repo.get_supplier_by_domain("nonexistent-domain.org") is None


# ---------------------------------------------------------------------------
# Task 2 & 3: Scoring Runs and Weight Preservation
# ---------------------------------------------------------------------------

def test_scoring_runs_and_weight_preservation(repo):
    """Verify scoring run creation, weights preservation, and association with brief."""
    criteria = SourcingCriteria(target_industries=["Plastics"], target_geographies=["MX"])
    brief = SourcingBrief(
        brief_id="BRIEF-SCORING-01",
        title="Plastics Molding",
        buyer_organization="Buyer Inc",
        description="Injection molding suppliers",
        criteria=criteria,
    )
    repo.create_brief(brief)

    # Configured scoring weights (differing slightly from defaults)
    custom_weights = ScoringWeights(
        industry_relevance=0.40,
        geographic_fit=0.20,
        company_size_fit=0.20,
        business_signals=0.10,
        data_completeness=0.10,
    )

    run_id = repo.create_scoring_run(
        brief_id="BRIEF-SCORING-01",
        weights=custom_weights,
        notes="Automated Phase 4 scoring run test",
    )
    assert run_id is not None

    # Retrieve scoring run
    run = repo.get_scoring_run(run_id)
    assert run is not None
    assert run["brief_id"] == "BRIEF-SCORING-01"
    assert run["weights"].industry_relevance == 0.40
    assert run["weights"].geographic_fit == 0.20
    assert run["notes"] == "Automated Phase 4 scoring run test"

    # List runs for brief
    runs = repo.list_scoring_runs_for_brief("BRIEF-SCORING-01")
    assert len(runs) == 1
    assert runs[0]["run_id"] == run_id


# ---------------------------------------------------------------------------
# Task 2 & 3: Supplier Evaluations Persistence
# ---------------------------------------------------------------------------

def test_supplier_evaluations_persistence(repo):
    """Verify evaluation scorecards, 5 scoring factors, total_score, and provenance persistence."""
    # Setup prerequisite brief, supplier, and run
    criteria = SourcingCriteria(target_industries=["Optics"], target_geographies=["JP"])
    brief = SourcingBrief(
        brief_id="BRIEF-EVAL-01",
        title="Optics Sourcing",
        buyer_organization="Olympus Medical Corp",
        description="Endoscope lens assemblies",
        criteria=criteria,
    )
    repo.create_brief(brief)

    supplier = SupplierProfile(
        supplier_id="SUPP-OPTIC-01",
        company_name="Kyoto Precision Optics",
        domain="kyoto-optics.jp",
        headquarters_country="JP",
    )
    repo.create_supplier(supplier)

    weights = ScoringWeights()
    run_id = repo.create_scoring_run(brief_id="BRIEF-EVAL-01", weights=weights)

    factors = {
        ScoringDimension.INDUSTRY_RELEVANCE: FactorScore(
            dimension=ScoringDimension.INDUSTRY_RELEVANCE,
            score=95.0,
            weight=weights.industry_relevance,
            reasoning="Exceptional lens grinding capabilities",
            evidence_sources=["Registry ISO 13485"],
        ),
        ScoringDimension.GEOGRAPHIC_FIT: FactorScore(
            dimension=ScoringDimension.GEOGRAPHIC_FIT,
            score=90.0,
            weight=weights.geographic_fit,
            reasoning="Direct domestic logistics",
            evidence_sources=["Headquarters registry Kyoto"],
        ),
        ScoringDimension.COMPANY_SIZE_FIT: FactorScore(
            dimension=ScoringDimension.COMPANY_SIZE_FIT,
            score=80.0,
            weight=weights.company_size_fit,
            reasoning="Headcount of 150 fits ideal range",
        ),
        ScoringDimension.BUSINESS_SIGNALS: FactorScore(
            dimension=ScoringDimension.BUSINESS_SIGNALS,
            score=70.0,
            weight=weights.business_signals,
            reasoning="Stable optical patent portfolio",
        ),
        ScoringDimension.DATA_COMPLETENESS: FactorScore(
            dimension=ScoringDimension.DATA_COMPLETENESS,
            score=100.0,
            weight=weights.data_completeness,
            reasoning="All required corporate parameters populated",
        ),
    }

    score = SupplierScore.compute(
        supplier_id="SUPP-OPTIC-01",
        company_name="Kyoto Precision Optics",
        factor_scores=factors,
        data_completeness_percentage=100.0,
        summary="Top candidate for precision medical optics.",
        weights_used=weights,
    )

    eval_id = repo.create_evaluation(run_id=run_id, score=score)
    assert eval_id is not None

    # Retrieve evaluation
    retrieved_eval = repo.get_evaluation(eval_id)
    assert retrieved_eval is not None
    assert retrieved_eval.supplier_id == "SUPP-OPTIC-01"
    assert pytest.approx(retrieved_eval.total_score, abs=0.1) == score.total_score
    assert len(retrieved_eval.factor_scores) == 5
    assert retrieved_eval.factor_scores[ScoringDimension.INDUSTRY_RELEVANCE].score == 95.0

    # List evaluations in run
    evals = repo.list_evaluations_for_run(run_id)
    assert len(evals) == 1
    assert evals[0].total_score == retrieved_eval.total_score


# ---------------------------------------------------------------------------
# Task 3: RFQ Documents CRUD and Lifecycle
# ---------------------------------------------------------------------------

def test_rfq_document_crud_and_lifecycle(repo):
    """Verify RFQ document creation, retrieval, and status updates."""
    brief = SourcingBrief(
        brief_id="BRIEF-RFQ-01",
        title="Battery Connectors",
        buyer_organization="Tesla Motors",
        description="HV battery copper connectors",
        criteria=SourcingCriteria(target_industries=["Copper"], target_geographies=["US"]),
    )
    repo.create_brief(brief)

    deadline = datetime.now(timezone.utc) + timedelta(days=21)
    rfq = RFQDraft(
        rfq_id="RFQ-DOC-001",
        brief_id="BRIEF-RFQ-01",
        target_supplier_ids=["SUPP-001", "SUPP-002"],
        title="RFQ for High Voltage Battery Copper Connectors",
        scope_of_work="Manufacture 100,000 units of stamped copper connectors.",
        technical_requirements=["CDA 110 Copper", "99.9% electrical conductivity", "Automotive PPAP Level 3"],
        submission_deadline=deadline,
        response_instructions="Submit full quote and material safety data sheet via portal.",
        evaluation_criteria_summary="Cost 40%, Quality/Certifications 40%, Lead time 20%.",
        status="DRAFT",
    )

    # 1. Create
    repo.create_rfq(rfq)

    # 2. Retrieve
    retrieved = repo.get_rfq("RFQ-DOC-001")
    assert retrieved is not None
    assert retrieved.title == rfq.title
    assert len(retrieved.target_supplier_ids) == 2
    assert retrieved.status == "DRAFT"

    # 3. Update status
    updated = repo.update_rfq_status("RFQ-DOC-001", "READY_FOR_REVIEW")
    assert updated.status == "READY_FOR_REVIEW"

    sent = repo.update_rfq_status("RFQ-DOC-001", "SENT")
    assert sent.status == "SENT"

    # Invalid status rejected
    with pytest.raises(DataValidationError):
        repo.update_rfq_status("RFQ-DOC-001", "INVALID_STATUS")

    # 4. List RFQs for brief
    rfqs = repo.list_rfqs_for_brief("BRIEF-RFQ-01")
    assert len(rfqs) == 1

    # 5. Delete
    assert repo.delete_rfq("RFQ-DOC-001") is True
    assert repo.get_rfq("RFQ-DOC-001") is None


# ---------------------------------------------------------------------------
# Task 4: Foreign Key Enforcement & Cascading Deletions
# ---------------------------------------------------------------------------

def test_foreign_key_enforcement(repo, test_db_path):
    """Verify that foreign keys prevent orphaned records and enforce cascade on delete."""
    # Attempting to create scoring run for non-existent brief must fail
    with pytest.raises(MissingSupplierDataError):
        repo.create_scoring_run(
            brief_id="NON_EXISTENT_BRIEF",
            weights=ScoringWeights(),
        )

    # Direct SQL foreign key constraint check
    with get_db_connection(test_db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO scoring_runs (run_id, brief_id, weights_json, created_at) VALUES ('R1', 'BAD_BRIEF', '{}', '2026-01-01');"
            )

    # Test cascade deletion: deleting brief deletes child scoring runs and RFQs
    criteria = SourcingCriteria(target_industries=["Fasteners"], target_geographies=["US"])
    brief = SourcingBrief(
        brief_id="BRIEF-CASCADE-01",
        title="Fasteners Brief",
        buyer_organization="Cascade Buyer",
        description="Testing cascade delete",
        criteria=criteria,
    )
    repo.create_brief(brief)
    run_id = repo.create_scoring_run(brief_id="BRIEF-CASCADE-01", weights=ScoringWeights())

    deadline = datetime.now(timezone.utc) + timedelta(days=10)
    rfq = RFQDraft(
        rfq_id="RFQ-CASCADE-01",
        brief_id="BRIEF-CASCADE-01",
        target_supplier_ids=["S1"],
        title="Cascade RFQ",
        scope_of_work="Testing cascade delete scope of work",
        technical_requirements=["Spec A"],
        submission_deadline=deadline,
        response_instructions="Instructions for submitting",
        evaluation_criteria_summary="Detailed evaluation criteria summary",
    )
    repo.create_rfq(rfq)

    # Delete brief
    repo.delete_brief("BRIEF-CASCADE-01")

    # Associated run and RFQ must be cascaded away
    assert repo.get_scoring_run(run_id) is None
    assert repo.get_rfq("RFQ-CASCADE-01") is None


# ---------------------------------------------------------------------------
# Task 4: Transaction Rollback
# ---------------------------------------------------------------------------

def test_transaction_rollback_on_failure(test_db_path):
    """Verify that an error inside get_db_connection triggers rollback and discards changes."""
    init_db(test_db_path)

    try:
        with get_db_connection(test_db_path) as conn:
            conn.execute(
                """
                INSERT INTO sourcing_briefs (
                    brief_id, title, buyer_organization, description,
                    criteria_json, created_at
                ) VALUES ('BRIEF-ROLLBACK', 'Rollback Brief', 'Org', 'Desc', '{}', '2026-01-01');
                """
            )
            # Intentionally raise an exception to abort transaction
            raise RuntimeError("Forced simulation error during transaction")
    except RuntimeError:
        pass

    # Record must NOT exist in the database due to rollback
    with get_db_connection(test_db_path) as conn:
        cur = conn.execute("SELECT * FROM sourcing_briefs WHERE brief_id = 'BRIEF-ROLLBACK';")
        assert cur.fetchone() is None
