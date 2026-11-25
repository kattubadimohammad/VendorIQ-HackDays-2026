"""
VendorIQ Phase 9 Test Suite: Supplier Intelligence & Evidence Verification.

Validates:
1. EvidenceCategory and EvidenceStatus enums.
2. SupplierEvidenceRecord Truth-in-Evidence integrity guardrails (rejection of unverified/AI VERIFIED claims).
3. SQLite schema and repository CRUD operations with foreign key cascade.
4. SupplierEvidenceService deterministic extraction across all 7 categories.
5. Domain mismatch detection integration with SupplierQualityAuditor.
6. Evidence completeness KPI calculations and missing documentary checklists.
7. Multi-supplier coverage matrix for all 18 live suppliers.
8. Non-destruction of existing database records, briefs, evaluations, and profiles.
"""

from datetime import datetime, timezone
import pytest
import os
import shutil

from core.models import (
    SupplierProfile,
    EvidenceCategory,
    EvidenceStatus,
    SupplierEvidenceRecord,
    Certification,
)
from core.provenance import (
    ProvenanceClassification,
    create_unverified_provenance,
    create_verified_provenance,
)
from core.exceptions import DataValidationError, ProvenanceIntegrityError
from db.connection import get_db_connection, init_db
from db.repository import VendorIQRepository
from services.evidence_service import SupplierEvidenceService
from services.data_quality_auditor import SupplierQualityAuditor
from ui.data_service import DashboardDataService


class TestSupplierEvidenceModels:
    """Test domain models and Truth-in-Evidence integrity guardrails."""

    def test_all_seven_evidence_categories_defined(self):
        """Verify all 7 official evidence categories are supported."""
        expected_categories = {
            "Company website and domain",
            "Manufacturing capabilities",
            "Flexible packaging experience",
            "Barrier film experience",
            "Sustainability certifications",
            "Company headquarters and operating locations",
            "Company size",
        }
        actual_categories = {c.value for c in EvidenceCategory}
        assert actual_categories == expected_categories
        assert len(EvidenceCategory) == 7

    def test_all_four_evidence_statuses_defined(self):
        """Verify all 4 required evidence statuses exist."""
        expected_statuses = {"VERIFIED", "SELF_REPORTED", "UNVERIFIED", "MISSING"}
        actual_statuses = {s.value for s in EvidenceStatus}
        assert actual_statuses == expected_statuses

    def test_valid_self_reported_evidence_creation(self):
        """Verify standard self-reported directory claim is valid."""
        rec = SupplierEvidenceRecord(
            supplier_id="CRUST_1049",
            category=EvidenceCategory.COMPANY_WEBSITE_DOMAIN,
            claim="Corporate domain: cosmofilms.com",
            status=EvidenceStatus.SELF_REPORTED,
            source_name="Crustdata Company Search",
            source_url="https://cosmofilms.com",
            reviewer_notes="Self-reported directory record.",
        )
        assert rec.is_self_reported
        assert not rec.is_verified
        assert not rec.is_missing
        assert rec.status == EvidenceStatus.SELF_REPORTED

    def test_valid_missing_evidence_creation(self):
        """Verify missing evidence claim is valid."""
        rec = SupplierEvidenceRecord(
            supplier_id="CRUST_1049",
            category=EvidenceCategory.BARRIER_FILM_EXPERIENCE,
            claim="No barrier film specifications on file.",
            status=EvidenceStatus.MISSING,
            source_name="Missing Data Assessment",
            reviewer_notes="ASTM gas transmission lab tests required.",
        )
        assert rec.is_missing
        assert not rec.is_verified
        assert rec.source_url is None

    def test_truth_in_evidence_verified_without_source_name_rejected(self):
        """Guardrail: Marking as VERIFIED without explicit source_name is rejected."""
        with pytest.raises(DataValidationError):
            SupplierEvidenceRecord(
                supplier_id="CRUST_1049",
                category=EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS,
                claim="ISO 14001:2015 Certified",
                status=EvidenceStatus.VERIFIED,
                source_name="   ",  # Blank
                source_url="https://iaf.nu/cert/123",
                verified_at=datetime.now(timezone.utc),
            )

    def test_truth_in_evidence_ai_source_cannot_be_verified(self):
        """Guardrail: AI or LLM sources cannot be classified as VERIFIED evidence."""
        with pytest.raises(ProvenanceIntegrityError) as exc_info:
            SupplierEvidenceRecord(
                supplier_id="CRUST_1049",
                category=EvidenceCategory.MANUFACTURING_CAPABILITIES,
                claim="Coextrusion plant verified",
                status=EvidenceStatus.VERIFIED,
                source_name="Gemini AI Assessment",
                source_url="https://example.com/ai",
                verified_at=datetime.now(timezone.utc),
            )
        assert "can NEVER be marked as VERIFIED" in str(exc_info.value)

    def test_truth_in_evidence_synthetic_source_cannot_be_verified(self):
        """Guardrail: Synthetic seed data cannot be marked as VERIFIED evidence."""
        with pytest.raises(ProvenanceIntegrityError):
            SupplierEvidenceRecord(
                supplier_id="CRUST_1049",
                category=EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS,
                claim="FSC Certified",
                status=EvidenceStatus.VERIFIED,
                source_name="Synthetic Mock Seed",
                source_url="https://example.com/mock",
                verified_at=datetime.now(timezone.utc),
            )

    def test_truth_in_evidence_verified_without_url_or_documentary_proof_rejected(self):
        """Guardrail: VERIFIED status requires documentary proof or URL."""
        with pytest.raises(DataValidationError):
            SupplierEvidenceRecord(
                supplier_id="CRUST_1049",
                category=EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS,
                claim="ISO 14001:2015 Certified",
                status=EvidenceStatus.VERIFIED,
                source_name="Bureau Veritas",
                source_url=None,
                reviewer_notes=None,
                verified_at=datetime.now(timezone.utc),
            )

    def test_truth_in_evidence_verified_without_verified_at_rejected(self):
        """Guardrail: VERIFIED status requires a recorded verification timestamp."""
        with pytest.raises(DataValidationError):
            SupplierEvidenceRecord(
                supplier_id="CRUST_1049",
                category=EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS,
                claim="ISO 14001:2015 Certified",
                status=EvidenceStatus.VERIFIED,
                source_name="Bureau Veritas",
                source_url="https://certificate.bureauveritas.com/12345",
                verified_at=None,
            )

    def test_valid_verified_evidence_with_documentary_proof_succeeds(self):
        """Verified record with authoritative registrar and proof passes validation."""
        now = datetime.now(timezone.utc)
        rec = SupplierEvidenceRecord(
            supplier_id="CRUST_1049",
            category=EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS,
            claim="ISO 14001:2015 Environmental Management System",
            status=EvidenceStatus.VERIFIED,
            source_name="TUV SUD Certification Body",
            source_url="https://tuvsud.com/cert/IND-14001-998",
            verified_at=now,
            reviewer_notes="Accredited certificate valid through 2028.",
        )
        assert rec.is_verified
        assert rec.status == EvidenceStatus.VERIFIED


class TestSupplierEvidenceRepository:
    """Test repository persistence, upsert, and cascade delete for supplier evidence."""

    def test_create_and_retrieve_evidence(self, tmp_path):
        db_file = str(tmp_path / "test_evidence.sqlite")
        init_db(db_file)
        repo = VendorIQRepository(db_path=db_file)

        # Create supplier first (foreign key requirement)
        supplier = SupplierProfile(
            supplier_id="TEST_SUPP_01",
            company_name="Test Packaging Ltd",
            domain="testpack.com",
            headquarters_country="India",
        )
        repo.create_supplier(supplier)

        record = SupplierEvidenceRecord(
            supplier_id="TEST_SUPP_01",
            category=EvidenceCategory.COMPANY_WEBSITE_DOMAIN,
            claim="Domain testpack.com active",
            status=EvidenceStatus.SELF_REPORTED,
            source_name="Crustdata Company Search",
            source_url="https://testpack.com",
            reviewer_notes="Self-reported domain.",
        )
        saved = repo.create_evidence(record)
        assert saved.evidence_id == record.evidence_id

        retrieved = repo.get_evidence(record.evidence_id)
        assert retrieved is not None
        assert retrieved.supplier_id == "TEST_SUPP_01"
        assert retrieved.category == EvidenceCategory.COMPANY_WEBSITE_DOMAIN
        assert retrieved.status == EvidenceStatus.SELF_REPORTED
        assert retrieved.source_name == "Crustdata Company Search"

    def test_upsert_evidence_updates_existing_record(self, tmp_path):
        db_file = str(tmp_path / "test_evidence.sqlite")
        init_db(db_file)
        repo = VendorIQRepository(db_path=db_file)

        supplier = SupplierProfile(
            supplier_id="TEST_SUPP_02",
            company_name="Acme Containers",
            domain="acmecontainers.com",
            headquarters_country="India",
        )
        repo.create_supplier(supplier)

        # Initial insert
        rec1 = SupplierEvidenceRecord(
            supplier_id="TEST_SUPP_02",
            category=EvidenceCategory.MANUFACTURING_CAPABILITIES,
            claim="Initial unconfirmed claim",
            status=EvidenceStatus.UNVERIFIED,
            source_name="Directory",
            reviewer_notes="Preliminary entry.",
        )
        repo.upsert_evidence(rec1)

        # Upsert with updated information
        rec2 = SupplierEvidenceRecord(
            evidence_id=rec1.evidence_id,
            supplier_id="TEST_SUPP_02",
            category=EvidenceCategory.MANUFACTURING_CAPABILITIES,
            claim="Extrusion and blow molding operations reported",
            status=EvidenceStatus.SELF_REPORTED,
            source_name="Company Directory Overview",
            reviewer_notes="Updated with technical overview.",
        )
        repo.upsert_evidence(rec2)

        records = repo.list_evidence_for_supplier("TEST_SUPP_02")
        assert len(records) == 1
        assert records[0].status == EvidenceStatus.SELF_REPORTED
        assert "blow molding" in records[0].claim

    def test_cascade_deletion_on_supplier_delete(self, tmp_path):
        db_file = str(tmp_path / "test_evidence.sqlite")
        init_db(db_file)
        repo = VendorIQRepository(db_path=db_file)

        supplier = SupplierProfile(
            supplier_id="TEST_SUPP_03",
            company_name="Temp Supplier",
            domain="tempsupplier.com",
            headquarters_country="India",
        )
        repo.create_supplier(supplier)

        rec = SupplierEvidenceRecord(
            supplier_id="TEST_SUPP_03",
            category=EvidenceCategory.COMPANY_SIZE,
            claim="51-200 employees",
            status=EvidenceStatus.SELF_REPORTED,
            source_name="Crustdata",
        )
        repo.create_evidence(rec)

        assert len(repo.list_evidence_for_supplier("TEST_SUPP_03")) == 1

        # Delete supplier
        repo.delete_supplier("TEST_SUPP_03")

        # Evidence should cascade-delete
        assert len(repo.list_evidence_for_supplier("TEST_SUPP_03")) == 0


class TestSupplierEvidenceService:
    """Test deterministic evidence evaluation and checklist logic."""

    def test_evaluate_supplier_with_manufacturing_and_barrier_keywords(self):
        supplier = SupplierProfile(
            supplier_id="TEST_EVAL_01",
            company_name="Advanced Barrier Films Ltd",
            domain="advancedbarrier.com",
            description="Leading manufacturer of high barrier specialty films, multi-layer coextruded pouches, and recyclable packaging.",
            headquarters_country="India",
            headquarters_city="Mumbai",
            employee_range="501-1000",
            industries=["Packaging and Containers Manufacturing"],
        )
        service = SupplierEvidenceService()
        evidence_list = service.evaluate_supplier_evidence(supplier)

        assert len(evidence_list) == 7
        ev_map = {e.category: e for e in evidence_list}

        # Manufacturing
        assert ev_map[EvidenceCategory.MANUFACTURING_CAPABILITIES].status == EvidenceStatus.SELF_REPORTED
        assert "manufacturing operations" in ev_map[EvidenceCategory.MANUFACTURING_CAPABILITIES].claim

        # Flexible packaging
        assert ev_map[EvidenceCategory.FLEXIBLE_PACKAGING_EXPERIENCE].status == EvidenceStatus.SELF_REPORTED

        # Barrier film
        assert ev_map[EvidenceCategory.BARRIER_FILM_EXPERIENCE].status == EvidenceStatus.SELF_REPORTED
        assert "barrier" in ev_map[EvidenceCategory.BARRIER_FILM_EXPERIENCE].claim.lower()

        # Sustainability (mentions recyclable in description but no accredited cert)
        assert ev_map[EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS].status == EvidenceStatus.UNVERIFIED
        assert "Self-reported sustainability" in ev_map[EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS].claim

        # Location
        assert ev_map[EvidenceCategory.HEADQUARTERS_LOCATIONS].status == EvidenceStatus.SELF_REPORTED
        assert "Mumbai" in ev_map[EvidenceCategory.HEADQUARTERS_LOCATIONS].claim

        # Size
        assert ev_map[EvidenceCategory.COMPANY_SIZE].status == EvidenceStatus.SELF_REPORTED
        assert "501-1000" in ev_map[EvidenceCategory.COMPANY_SIZE].claim

    def test_evaluate_supplier_with_missing_fields(self):
        supplier = SupplierProfile(
            supplier_id="TEST_EVAL_02",
            company_name="Sparse Info Packaging",
            domain="sparsepack.com",
            description=None,
            headquarters_country="India",
            headquarters_city=None,
            employee_range=None,
        )
        service = SupplierEvidenceService()
        evidence_list = service.evaluate_supplier_evidence(supplier)

        ev_map = {e.category: e for e in evidence_list}

        assert ev_map[EvidenceCategory.MANUFACTURING_CAPABILITIES].status == EvidenceStatus.MISSING
        assert ev_map[EvidenceCategory.BARRIER_FILM_EXPERIENCE].status == EvidenceStatus.MISSING
        assert ev_map[EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS].status == EvidenceStatus.MISSING
        assert ev_map[EvidenceCategory.COMPANY_SIZE].status == EvidenceStatus.MISSING

    def test_domain_mismatch_flagged_as_unverified(self):
        """Verify suspicious domain mismatch is correctly flagged as UNVERIFIED."""
        supplier = SupplierProfile(
            supplier_id="CRUST_698",
            company_name="Biopac India Corporation Ltd",
            domain="ecpulse.com",
            description="Manufacturing biodegradable packaging.",
            headquarters_country="India",
        )
        auditor = SupplierQualityAuditor()
        service = SupplierEvidenceService(auditor=auditor)
        evidence_list = service.evaluate_supplier_evidence(supplier)

        dom_ev = next(e for e in evidence_list if e.category == EvidenceCategory.COMPANY_WEBSITE_DOMAIN)
        assert dom_ev.status == EvidenceStatus.UNVERIFIED
        assert "flagged for domain mismatch" in dom_ev.claim
        assert "Quality Audit Warning" in dom_ev.reviewer_notes

    def test_completeness_calculation(self):
        service = SupplierEvidenceService()
        now = datetime.now(timezone.utc)
        records = [
            SupplierEvidenceRecord(
                supplier_id="S1",
                category=EvidenceCategory.COMPANY_WEBSITE_DOMAIN,
                claim="Claim 1",
                status=EvidenceStatus.SELF_REPORTED,
                source_name="Source 1",
            ),
            SupplierEvidenceRecord(
                supplier_id="S1",
                category=EvidenceCategory.MANUFACTURING_CAPABILITIES,
                claim="Claim 2",
                status=EvidenceStatus.SELF_REPORTED,
                source_name="Source 2",
            ),
            SupplierEvidenceRecord(
                supplier_id="S1",
                category=EvidenceCategory.BARRIER_FILM_EXPERIENCE,
                claim="Claim 3",
                status=EvidenceStatus.MISSING,
                source_name="Missing",
            ),
        ]
        # 2 populated out of 7 total categories -> ~28.6%
        kpis = service.calculate_evidence_completeness(records)
        assert kpis["total_categories"] == 7
        assert kpis["self_reported_count"] == 2
        assert kpis["missing_count"] == 1
        assert kpis["verified_count"] == 0

    def test_missing_evidence_checklist_generation(self):
        service = SupplierEvidenceService()
        records = [
            SupplierEvidenceRecord(
                supplier_id="S1",
                category=EvidenceCategory.BARRIER_FILM_EXPERIENCE,
                claim="No barrier specifications on file",
                status=EvidenceStatus.MISSING,
                source_name="Missing",
            ),
            SupplierEvidenceRecord(
                supplier_id="S1",
                category=EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS,
                claim="Unverified green claim",
                status=EvidenceStatus.UNVERIFIED,
                source_name="Marketing",
            ),
        ]
        checklist = service.get_missing_evidence_checklist(records)
        assert len(checklist) == 2
        categories = [item["category"] for item in checklist]
        assert EvidenceCategory.BARRIER_FILM_EXPERIENCE.value in categories
        assert EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS.value in categories
        assert "ASTM" in checklist[0]["required_document"]


class TestLiveDatabaseEvidenceIntegration:
    """Validate all 18 live suppliers in vendoriq.sqlite under Phase 9."""

    def test_live_database_all_18_suppliers_have_evidence_generated(self, tmp_path):
        """Ensure all 18 live Crustdata profiles in SQLite have 7 structured evidence categories on an isolated DB."""
        if not os.path.exists("vendoriq.sqlite"):
            pytest.skip("vendoriq.sqlite not found.")

        isolated_db = str(tmp_path / "isolated_evidence.sqlite")
        shutil.copy2("vendoriq.sqlite", isolated_db)

        service = DashboardDataService(db_path=isolated_db)
        suppliers = service.get_all_suppliers()
        assert len(suppliers) == 18

        evidence_dict = service.generate_and_persist_all_evidence(force_refresh=True)
        assert len(evidence_dict) == 18

        for supp in suppliers:
            records = evidence_dict.get(supp.supplier_id)
            assert records is not None
            assert len(records) == 7
            # Guardrail: zero records can claim VERIFIED in basic directory tier
            for rec in records:
                assert rec.status != EvidenceStatus.VERIFIED, (
                    f"Supplier {supp.company_name} has invalid VERIFIED claim on {rec.category.value} "
                    f"without authoritative verification proof!"
                )

    def test_live_database_domain_mismatches_properly_flagged(self):
        """Confirm exactly the 2 known suspicious domain mismatches are flagged as UNVERIFIED."""
        service = DashboardDataService()
        mismatched_ids = {"CRUST_698", "CRUST_2552"}

        for s_id in mismatched_ids:
            records = service.get_supplier_evidence(s_id)
            dom_rec = next(r for r in records if r.category == EvidenceCategory.COMPANY_WEBSITE_DOMAIN)
            assert dom_rec.status == EvidenceStatus.UNVERIFIED
            assert "flagged for domain mismatch" in dom_rec.claim

    def test_multi_supplier_coverage_matrix_live(self):
        """Verify multi-supplier coverage matrix operates cleanly across all 18 live suppliers."""
        service = DashboardDataService()
        matrix = service.get_multi_supplier_coverage_matrix()

        assert matrix["total_suppliers"] == 18
        assert len(matrix["supplier_rows"]) == 18
        assert len(matrix["category_aggregates"]) == 7

        # Check aggregate stats for sustainability
        sust_agg = matrix["category_aggregates"][EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS.value]
        assert sust_agg["VERIFIED"] == 0  # Zero fake verified certs!
        assert (sust_agg["UNVERIFIED"] + sust_agg["MISSING"]) == 18

    def test_database_integrity_and_zero_duplicate_briefs_preserved(self):
        """Verify brief deduplication and existing evaluations remain intact after evidence migration."""
        service = DashboardDataService()
        briefs = service.get_all_briefs()
        # Exactly 2 briefs preserved
        assert len(briefs) == 2

        # Check retained packaging brief
        packaging_brief = service.get_brief("BRIEF-PACKAGING-IN-01")
        assert packaging_brief is not None
        evals = service.get_evaluations(packaging_brief.brief_id)
        assert len(evals) == 18

        copilot_brief = service.get_brief("BRIEF-DCD8044A")
        assert copilot_brief is not None
        copilot_evals = service.get_evaluations(copilot_brief.brief_id)
        assert len(copilot_evals) == 18
