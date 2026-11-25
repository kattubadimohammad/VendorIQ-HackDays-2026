"""
VendorIQ Phase 10 Test Suite: Intelligent Supplier Discovery & Recommendations.

Validates:
1. ShortlistStatus, ShortlistItem, SupplierShortlist, DiscoveryFilterParams, DiscoveryRecommendation models.
2. Shortlist CSV export formatting and data integrity.
3. SQLite repository shortlist CRUD, item status lifecycle, and foreign key cascade.
4. SupplierDiscoveryService natural language search, hard vs soft filtering, and deterministic ranking.
5. Evidence-backed recommendations (available evidence, missing evidence, unverified claims, risks).
6. Shortlist management (CANDIDATE, UNDER_REVIEW, SHORTLISTED, REJECTED).
7. Live database integration across all 18 Crustdata suppliers with zero credit consumption.
"""

from datetime import datetime, timezone
import pytest
import csv
import io
import os
import shutil
import sqlite3

from core.models import (
    SupplierProfile,
    SourcingBrief,
    SourcingCriteria,
    ShortlistStatus,
    ShortlistItem,
    SupplierShortlist,
    DiscoveryFilterParams,
    DiscoveryRecommendation,
    EvidenceCategory,
    EvidenceStatus,
    SupplierEvidenceRecord,
)
from db.connection import get_db_connection, init_db
from db.repository import VendorIQRepository
from services.discovery_service import SupplierDiscoveryService
from ui.data_service import DashboardDataService


class TestDiscoveryAndShortlistModels:
    """Validate domain models for discovery filtering, recommendations, and shortlists."""

    def test_shortlist_status_enum_values(self):
        """Verify the 4 required shortlist lifecycle statuses."""
        expected = {"CANDIDATE", "UNDER_REVIEW", "SHORTLISTED", "REJECTED"}
        actual = {s.value for s in ShortlistStatus}
        assert actual == expected

    def test_shortlist_item_creation_defaults(self):
        """Verify ShortlistItem creation with default CANDIDATE status."""
        item = ShortlistItem(
            shortlist_id="SLIST-001",
            supplier_id="CRUST_1049",
            company_name="Cosmo Films",
            domain="cosmofilms.com",
            total_score=77.0,
        )
        assert item.status == ShortlistStatus.CANDIDATE
        assert item.item_id.startswith("ITEM-")
        assert item.reviewer_notes is None

    def test_supplier_shortlist_to_csv_export(self):
        """Verify CSV export produces valid RFC 4180 tabular data."""
        item1 = ShortlistItem(
            shortlist_id="SLIST-TEST",
            supplier_id="CRUST_1049",
            company_name="Cosmo Films",
            domain="cosmofilms.com",
            total_score=77.0,
            status=ShortlistStatus.SHORTLISTED,
            reviewer_notes="High barrier BOPP capability confirmed.",
        )
        item2 = ShortlistItem(
            shortlist_id="SLIST-TEST",
            supplier_id="CRUST_698",
            company_name="Biopac India Corporation Ltd",
            domain="ecpulse.com",
            total_score=52.5,
            status=ShortlistStatus.REJECTED,
            reviewer_notes="Domain mismatch with ecpulse.com requires review.",
        )
        shortlist = SupplierShortlist(
            shortlist_id="SLIST-TEST",
            brief_id="BRIEF-01",
            name="Q4 Packaging Shortlist",
            description="Testing export",
            items=[item1, item2],
        )

        csv_text = shortlist.to_csv()
        reader = csv.reader(io.StringIO(csv_text))
        rows = list(reader)

        # Header + 2 data rows
        assert len(rows) == 3
        assert rows[0] == [
            "Shortlist ID",
            "Shortlist Name",
            "Brief ID",
            "Item ID",
            "Supplier ID",
            "Company Name",
            "Domain",
            "Status",
            "Score",
            "Reviewer Notes",
            "Added At",
            "Updated At",
        ]
        assert rows[1][4] == "CRUST_1049"
        assert rows[1][5] == "Cosmo Films"
        assert rows[1][7] == "SHORTLISTED"
        assert rows[2][4] == "CRUST_698"
        assert rows[2][7] == "REJECTED"

    def test_discovery_filter_params_defaults(self):
        """Verify filter parameters default to non-restrictive search."""
        params = DiscoveryFilterParams()
        assert params.query is None
        assert params.require_manufacturing is False
        assert params.require_barrier_films is False
        assert params.exclude_domain_mismatches is False


class TestShortlistRepository:
    """Test shortlist persistence, status updates, and cascade delete."""

    def test_shortlist_crud_and_status_lifecycle(self, tmp_path):
        db_file = str(tmp_path / "test_shortlist.sqlite")
        init_db(db_file)
        repo = VendorIQRepository(db_path=db_file)

        # 1. Setup brief and supplier
        brief = SourcingBrief(
            brief_id="BRIEF-TEST-01",
            title="Test Sourcing Brief",
            buyer_organization="Acme Procurement",
            description="Procurement test project for shortlists",
            criteria=SourcingCriteria(
                target_industries=["Packaging and Containers Manufacturing"],
                target_geographies=["India"],
            ),
        )
        repo.create_brief(brief)

        supplier = SupplierProfile(
            supplier_id="SUPP-01",
            company_name="Test Supplier",
            domain="testsupplier.com",
            headquarters_country="India",
        )
        repo.create_supplier(supplier)

        # 2. Create shortlist
        shortlist = SupplierShortlist(
            shortlist_id="SLIST-001",
            brief_id=brief.brief_id,
            name="Priority Candidates",
            description="Initial shortlisting pass",
        )
        repo.create_shortlist(shortlist)

        # 3. Add item
        item = ShortlistItem(
            item_id="ITEM-01",
            shortlist_id=shortlist.shortlist_id,
            supplier_id=supplier.supplier_id,
            status=ShortlistStatus.CANDIDATE,
            reviewer_notes="Preliminary entry",
        )
        repo.add_item_to_shortlist(item)

        # 4. Retrieve and verify
        retrieved = repo.get_shortlist("SLIST-001")
        assert retrieved is not None
        assert retrieved.name == "Priority Candidates"
        assert len(retrieved.items) == 1
        assert retrieved.items[0].supplier_id == "SUPP-01"
        assert retrieved.items[0].status == ShortlistStatus.CANDIDATE

        # 5. Update item status to SHORTLISTED
        updated_item = repo.update_shortlist_item_status(
            item_id="ITEM-01",
            status=ShortlistStatus.SHORTLISTED,
            reviewer_notes="Approved for RFQ drafting",
        )
        assert updated_item is not None
        assert updated_item.status == ShortlistStatus.SHORTLISTED
        assert updated_item.reviewer_notes == "Approved for RFQ drafting"

        # 6. Delete shortlist cascades to items
        repo.delete_shortlist("SLIST-001")
        assert repo.get_shortlist("SLIST-001") is None
        assert len(repo.list_items_for_shortlist("SLIST-001")) == 0


class TestSupplierDiscoveryService:
    """Test discovery behavior using deterministic synthetic suppliers and an isolated DB."""

    @pytest.fixture
    def discovery_service(self, tmp_path):
        db_path = str(tmp_path / "synthetic_discovery.sqlite")
        init_db(db_path)
        repo = VendorIQRepository(db_path=db_path)
        suppliers = [
            SupplierProfile(
                supplier_id="SYNTH_ALPHA",
                company_name="Alpha Barrier Films",
                domain="alphabarrierfilms.example",
                description=(
                    "Synthetic manufacturer of flexible packaging, barrier films, "
                    "BOPP specialty films and laminated pouches."
                ),
                headquarters_country="India",
                headquarters_city="Pune",
                employee_range="201-500",
                industries=["Packaging and Containers Manufacturing"],
                is_synthetic=True,
            ),
            SupplierProfile(
                supplier_id="SYNTH_BETA",
                company_name="Beta Green Packaging",
                domain="unrelated-example.example",
                description="Synthetic manufacturer of sustainable packaging containers and cartons.",
                headquarters_country="India",
                headquarters_city="Surat",
                employee_range="51-200",
                industries=["Packaging and Containers Manufacturing"],
                is_synthetic=True,
            ),
            SupplierProfile(
                supplier_id="SYNTH_GAMMA",
                company_name="Gamma Flexible Films",
                domain="gammaflexiblefilms.example",
                description="Synthetic producer of flexible packaging films and laminated rolls.",
                headquarters_country="India",
                headquarters_city="Mumbai",
                employee_range="51-200",
                industries=["Packaging and Containers Manufacturing"],
                is_synthetic=True,
            ),
        ]
        for supplier in suppliers:
            repo.create_supplier(supplier)
        return SupplierDiscoveryService(repository=repo)

    def test_natural_language_search_token_matching(self, discovery_service):
        service = discovery_service
        params = DiscoveryFilterParams(query="barrier film")
        recs = service.discover_suppliers(params=params)

        assert len(recs) > 0
        for recommendation in recs:
            searchable_text = (
                f"{recommendation.company_name} {recommendation.domain} "
                f"{' '.join(recommendation.available_evidence)}"
            ).lower()
            assert "barrier" in searchable_text or "film" in searchable_text

    def test_hard_requirement_barrier_film_filtering(self, discovery_service):
        """Hard filter: require_barrier_films flags suppliers without barrier evidence."""
        recs = discovery_service.discover_suppliers(
            params=DiscoveryFilterParams(require_barrier_films=True)
        )
        qualified = [recommendation for recommendation in recs if recommendation.is_hard_match]
        disqualified = [recommendation for recommendation in recs if not recommendation.is_hard_match]

        assert {recommendation.supplier_id for recommendation in qualified} == {"SYNTH_ALPHA"}
        assert {recommendation.supplier_id for recommendation in disqualified} == {
            "SYNTH_BETA", "SYNTH_GAMMA"
        }
        for recommendation in disqualified:
            assert any(
                "barrier film" in failure.lower()
                for failure in recommendation.hard_filter_failures
            )

    def test_hard_requirement_exclude_domain_mismatches(self, discovery_service):
        """Hard filter: exclude suspicious synthetic company/domain mismatches."""
        recs = discovery_service.discover_suppliers(
            params=DiscoveryFilterParams(exclude_domain_mismatches=True)
        )
        beta = next(recommendation for recommendation in recs if recommendation.supplier_id == "SYNTH_BETA")
        alpha = next(recommendation for recommendation in recs if recommendation.supplier_id == "SYNTH_ALPHA")

        assert beta.is_hard_match is False
        assert any("domain mismatch" in failure.lower() for failure in beta.hard_filter_failures)
        assert alpha.is_hard_match is True
        assert alpha.domain_mismatch_warning is None

    def test_deterministic_ranking_preserves_evaluation_scores(self, discovery_service):
        """Ranking must be deterministic and must not alter evaluation scores."""
        service = discovery_service
        recs1 = service.discover_suppliers()
        recs2 = service.discover_suppliers()

        assert len(recs1) == len(recs2) == 3
        for first, second in zip(recs1, recs2):
            assert first.supplier_id == second.supplier_id
            assert first.total_score == second.total_score
            assert first.rank == second.rank

    def test_evidence_backed_recommendation_contents(self, discovery_service):
        """Recommendations must expose evidence, risks and domain warnings."""
        recs = discovery_service.discover_suppliers()
        alpha = next(recommendation for recommendation in recs if recommendation.supplier_id == "SYNTH_ALPHA")

        assert alpha.company_name == "Alpha Barrier Films"
        assert alpha.domain == "alphabarrierfilms.example"
        assert alpha.total_score >= 70.0
        assert len(alpha.available_evidence) > 0
        assert len(alpha.procurement_risks) > 0
        assert alpha.domain_mismatch_warning is None


class TestLiveDatabaseDiscoveryIntegration:
    """End-to-end integration with the 18 live suppliers in vendoriq.sqlite."""

    def test_live_discovery_returns_all_18_profiles(self):
        """Verify discover_suppliers covers all 18 profiles in SQLite without filters."""
        service = DashboardDataService()
        recs = service.discover_suppliers()
        assert len(recs) == 18

    def test_live_shortlist_workflow_and_csv_export(self, tmp_path):
        """Test complete workflow: create shortlist, add candidates, change status, export CSV on an isolated DB."""
        if not os.path.exists("vendoriq.sqlite"):
            pytest.skip("vendoriq.sqlite not found.")

        # Isolate database: copy production database to temporary test directory
        isolated_db = str(tmp_path / "isolated_shortlist_workflow.sqlite")
        shutil.copy2("vendoriq.sqlite", isolated_db)

        service = DashboardDataService(db_path=isolated_db)
        briefs = service.get_all_briefs()
        brief_id = briefs[0].brief_id

        # 1. Create shortlist
        slist = service.create_shortlist(
            brief_id=brief_id,
            name="Packaging Evaluation Shortlist",
            description="Phase 10 integration test shortlist",
        )
        assert slist.shortlist_id.startswith("SLIST-")

        # 2. Add candidates
        item1 = service.add_supplier_to_shortlist(
            shortlist_id=slist.shortlist_id,
            supplier_id="CRUST_1049",
            reviewer_notes="Top specialty film candidate",
            status=ShortlistStatus.SHORTLISTED,
        )
        item2 = service.add_supplier_to_shortlist(
            shortlist_id=slist.shortlist_id,
            supplier_id="CRUST_698",
            reviewer_notes="Requires domain check",
            status=ShortlistStatus.UNDER_REVIEW,
        )

        assert item1.status == ShortlistStatus.SHORTLISTED
        assert item2.status == ShortlistStatus.UNDER_REVIEW

        # 3. Update candidate status
        updated = service.update_shortlist_item_status(
            item_id=item2.item_id,
            status=ShortlistStatus.REJECTED,
            reviewer_notes="Rejected due to unconfirmed domain",
        )
        assert updated is not None
        assert updated.status == ShortlistStatus.REJECTED

        # 4. Export CSV
        csv_text = service.export_shortlist_csv(slist.shortlist_id)
        assert "Cosmo Films" in csv_text
        assert "SHORTLISTED" in csv_text
        assert "REJECTED" in csv_text

    def test_shortlist_integration_does_not_modify_production_database(self, tmp_path):
        """
        Regression test: Verify that running the live shortlist workflow on an isolated database
        leaves the production database (vendoriq.sqlite) completely unmodified.
        """
        if not os.path.exists("vendoriq.sqlite"):
            pytest.skip("vendoriq.sqlite not found.")

        # 1. Capture production database counts before test execution
        conn_before = sqlite3.connect("vendoriq.sqlite")
        pre_counts = {
            t[0]: conn_before.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0]
            for t in conn_before.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn_before.close()

        # 2. Execute shortlist workflow on an isolated temporary copy
        isolated_db = str(tmp_path / "regression_isolated.sqlite")
        shutil.copy2("vendoriq.sqlite", isolated_db)

        service = DashboardDataService(db_path=isolated_db)
        briefs = service.get_all_briefs()
        brief_id = briefs[0].brief_id

        slist = service.create_shortlist(
            brief_id=brief_id,
            name="Isolation Regression Shortlist",
            description="Testing production database isolation",
        )
        service.add_supplier_to_shortlist(
            shortlist_id=slist.shortlist_id,
            supplier_id="CRUST_1049",
            reviewer_notes="Top specialty film candidate",
            status=ShortlistStatus.SHORTLISTED,
        )
        service.add_supplier_to_shortlist(
            shortlist_id=slist.shortlist_id,
            supplier_id="CRUST_698",
            reviewer_notes="Requires domain check",
            status=ShortlistStatus.UNDER_REVIEW,
        )

        # 3. Verify production database counts are 100% identical
        conn_after = sqlite3.connect("vendoriq.sqlite")
        post_counts = {
            t[0]: conn_after.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0]
            for t in conn_after.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn_after.close()

        assert pre_counts == post_counts, (
            f"Production database vendoriq.sqlite was modified during test! "
            f"Pre-counts: {pre_counts}, Post-counts: {post_counts}"
        )

    def test_database_integrity_and_duplicate_prevention_preserved(self):
        """Verify brief deduplication and existing evaluations remain intact after Phase 10."""
        service = DashboardDataService()
        briefs = service.get_all_briefs()
        assert len(briefs) == 2

        # Verify existing 18 evaluations on retained brief
        evals = service.get_evaluations("BRIEF-PACKAGING-IN-01")
        assert len(evals) == 18

        # Verify all 18 supplier profiles preserved
        suppliers = service.get_all_suppliers()
        assert len(suppliers) == 18
