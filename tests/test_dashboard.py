"""
Tests for Phase 7: Interactive Sourcing Intelligence Dashboard Services & RFQ Workflow.

Validates:
- DashboardDataService interactions with SQLite.
- Sourcing brief creation and listing.
- Dynamic re-evaluation with custom weights.
- RFQ drafting workflow and status transitions.
- Component helper rendering.
- Integration test loading live records from vendoriq.sqlite.
"""

import pytest
import os
from datetime import datetime, timezone, timedelta

from core.models import (
    SourcingBrief,
    SourcingCriteria,
    ScoringWeights,
    SupplierProfile,
    RFQDraft,
)
from core.provenance import ProvenanceClassification
from db.connection import init_db
from db.repository import VendorIQRepository
from ui.data_service import DashboardDataService
from ui.components import get_provenance_badge_html


@pytest.fixture
def temp_db_path(tmp_path):
    """Provide an isolated temporary database with schema applied."""
    db_file = tmp_path / "test_dashboard.sqlite"
    db_path = str(db_file)
    init_db(db_path=db_path)
    return db_path


@pytest.fixture
def dashboard_service(temp_db_path):
    """Provide DashboardDataService operating on temporary database."""
    return DashboardDataService(db_path=temp_db_path)


@pytest.fixture
def sample_brief():
    """Create sample sourcing brief."""
    return SourcingBrief(
        brief_id="BRIEF-TEST-UI-01",
        title="Eco-friendly Packaging Sourcing",
        buyer_organization="Test Corp",
        description="Sourcing recyclable paper and carton packaging suppliers in India.",
        criteria=SourcingCriteria(
            target_industries=["Packaging and Containers Manufacturing"],
            target_geographies=["India"],
            min_company_size=50,
            max_company_size=1000,
            weights=ScoringWeights(
                industry_relevance=0.40,
                geographic_fit=0.20,
                company_size_fit=0.20,
                business_signals=0.10,
                data_completeness=0.10,
            ),
        ),
    )


class TestDashboardDataService:
    """Unit tests for DashboardDataService logic."""

    def test_create_and_retrieve_brief(self, dashboard_service, sample_brief):
        """Test creating and retrieving a brief."""
        created = dashboard_service.create_brief(sample_brief)
        assert created.brief_id == "BRIEF-TEST-UI-01"

        retrieved = dashboard_service.get_brief("BRIEF-TEST-UI-01")
        assert retrieved is not None
        assert retrieved.title == "Eco-friendly Packaging Sourcing"

        all_briefs = dashboard_service.get_all_briefs()
        assert len(all_briefs) == 1

    def test_audit_suppliers_integration(self, dashboard_service, temp_db_path):
        """Test running data quality audit through dashboard service."""
        repo = VendorIQRepository(db_path=temp_db_path)
        s1 = SupplierProfile(
            supplier_id="SUPP-1",
            company_name="Apex Packaging Ltd",
            domain="apexpackaging.com",
            description="Leading packaging solutions provider.",
            employee_range="51-200",
            headquarters_country="India",
        )
        s2 = SupplierProfile(
            supplier_id="SUPP-2",
            company_name="Mismatched Brand",
            domain="completelyrandomdomain.xyz",
            description="",  # Missing
            employee_range=None,  # Missing
            headquarters_country="India",
        )
        repo.create_supplier(s1)
        repo.create_supplier(s2)

        report = dashboard_service.audit_suppliers()
        assert report.total_records == 2
        assert report.unique_company_names == 2
        assert report.missing_fields_counts["description"] == 1
        assert report.missing_fields_counts["employee_range"] == 1
        assert report.suspicious_mismatches_count == 1
        assert report.flagged_suppliers_for_review[0]["company_name"] == "Mismatched Brand"

    def test_rfq_drafting_and_lifecycle(self, dashboard_service, sample_brief, temp_db_path):
        """Test creating an RFQ draft and transitioning status."""
        dashboard_service.create_brief(sample_brief)

        repo = VendorIQRepository(db_path=temp_db_path)
        s1 = SupplierProfile(
            supplier_id="SUPP-1",
            company_name="Apex Packaging Ltd",
            domain="apexpackaging.com",
            description="Packaging solutions.",
            employee_range="51-200",
            headquarters_country="India",
        )
        repo.create_supplier(s1)

        deadline = datetime.now(timezone.utc) + timedelta(days=14)
        rfq = dashboard_service.create_rfq_draft(
            brief_id="BRIEF-TEST-UI-01",
            target_supplier_ids=["SUPP-1"],
            title="RFQ for 50,000 Corrugated Boxes",
            scope_of_work="Delivery of 50,000 units of corrugated carton packaging.",
            technical_requirements=["Burst strength > 14 kg/cm2", "Moisture resistant"],
            submission_deadline=deadline,
            response_instructions="Submit proposals to procurement@test.com",
            evaluation_criteria_summary="Evaluation on price, quality, and lead time.",
        )

        assert rfq.rfq_id is not None
        assert rfq.status == "DRAFT"
        assert len(rfq.target_supplier_ids) == 1

        # List RFQs
        rfqs = dashboard_service.list_rfqs("BRIEF-TEST-UI-01")
        assert len(rfqs) == 1
        assert rfqs[0].rfq_id == rfq.rfq_id

        # Update status
        updated = dashboard_service.update_rfq_status(rfq.rfq_id, "READY_FOR_REVIEW")
        assert updated.status == "READY_FOR_REVIEW"

    def test_provenance_badge_html_rendering(self):
        """Verify provenance badge returns valid HTML for all classifications."""
        for p in ProvenanceClassification:
            badge_html = get_provenance_badge_html(p)
            assert "<span" in badge_html
            assert "</span>" in badge_html


class TestLiveDashboardIntegration:
    """Integration tests verifying dashboard loads live data from vendoriq.sqlite."""

    def test_live_database_loading(self):
        """Verify DashboardDataService loads 18 live suppliers and existing brief."""
        if not os.path.exists("vendoriq.sqlite"):
            pytest.skip("vendoriq.sqlite not found.")

        service = DashboardDataService(db_path="vendoriq.sqlite")

        # Suppliers
        suppliers = service.get_all_suppliers()
        assert len(suppliers) == 18

        # Briefs
        briefs = service.get_all_briefs()
        assert len(briefs) >= 1
        brief_ids = [b.brief_id for b in briefs]
        assert "BRIEF-PACKAGING-IN-01" in brief_ids

        # Evaluations
        evals = service.get_evaluations("BRIEF-PACKAGING-IN-01")
        assert len(evals) == 18
        assert evals[0].total_score >= evals[-1].total_score

    def test_score_consistency_and_deterministic_ranking(self):
        """Phase 7.1: Verify all 18 scores recalculate accurately, average is exact, and ties are sorted deterministically."""
        if not os.path.exists("vendoriq.sqlite"):
            pytest.skip("vendoriq.sqlite not found.")

        service = DashboardDataService(db_path="vendoriq.sqlite")
        evals = service.get_evaluations("BRIEF-PACKAGING-IN-01")
        assert len(evals) == 18

        # 1. Recalculate each score from individual factor scores
        total_sum = 0.0
        for sc in evals:
            calc_score = sum(f.weighted_score for f in sc.factor_scores.values())
            assert abs(sc.total_score - calc_score) < 0.05
            total_sum += sc.total_score

        # 2. Verify exact dashboard average score (1318.5 / 18 == 73.25)
        avg_score = total_sum / len(evals)
        assert abs(avg_score - 73.25) < 1e-4

        # 3. Verify deterministic descending order with alphabetical tie-breaking
        for i in range(len(evals) - 1):
            curr_e = evals[i]
            next_e = evals[i + 1]
            if curr_e.total_score == next_e.total_score:
                assert curr_e.company_name <= next_e.company_name
            else:
                assert curr_e.total_score > next_e.total_score

    def test_data_consistency_and_zero_duplicates(self):
        """Phase 7.1: Verify all 18 suppliers have unique names and domains with valid ranges."""
        if not os.path.exists("vendoriq.sqlite"):
            pytest.skip("vendoriq.sqlite not found.")

        service = DashboardDataService(db_path="vendoriq.sqlite")
        suppliers = service.get_all_suppliers()
        assert len(suppliers) == 18

        names = [s.company_name for s in suppliers]
        domains = [s.domain for s in suppliers]

        # No duplicate names or domains
        assert len(set(names)) == 18
        assert len(set(domains)) == 18

        # All 18 have valid employee ranges
        for s in suppliers:
            assert s.employee_range is not None and len(s.employee_range) > 0
            assert s.is_synthetic is False
            assert s.overall_provenance == ProvenanceClassification.UNVERIFIED_OR_MISSING

    def test_credit_reconciliation_and_system_status(self):
        """Phase 7.1: Verify credit reconciliation (0.6 API credits, $0.00 USD) and local read status."""
        from ui.views.evidence_view import render_evidence_view
        import inspect

        source_code = inspect.getsource(render_evidence_view)
        # Confirm 0.6 API credits is displayed and 20 credits is removed
        assert "0.6 API Credits" in source_code
        assert "20 credits" not in source_code
        assert "0.00 USD" in source_code
        assert "Local SQLite read mode" in source_code

    def test_app_module_import_and_app_mode_type(self):
        """Verify app module imports cleanly and settings.APP_MODE handles string type without error."""
        import app
        from config import settings

        assert isinstance(settings.APP_MODE, str)
        # Verify app_mode_display logic from app.py
        app_mode_display = settings.APP_MODE.value if hasattr(settings.APP_MODE, "value") else str(settings.APP_MODE)
        assert app_mode_display in ("ONLINE", "OFFLINE")
        assert app.get_service() is not None

    def test_browser_copilot_brief_save_flow_integration(self):
        """
        Integration test verifying the exact application save path used by the browser:
        - Uses app.get_service() (same entrypoint and database path as Streamlit).
        - Verifies that repeated/identical browser submissions reuse BRIEF-DCD8044A.
        - Verifies that different sourcing requirements create separate briefs.
        - Verifies that repeated submissions never create duplicate records.
        - Verifies that the application and tests use the intended database path.
        """
        if not os.path.exists("vendoriq.sqlite"):
            pytest.skip("vendoriq.sqlite not found.")

        import app
        from core.models import ExtractedSourcingRequirements
        from db.connection import get_default_db_path

        service = app.get_service()

        # 1. Database path resolution verification
        resolved_db_path = service.repository.db_path or get_default_db_path()
        assert "vendoriq.sqlite" in resolved_db_path
        assert os.path.isabs(resolved_db_path)

        # 2. Verify canonical BRIEF-DCD8044A exists
        canonical = service.get_brief("BRIEF-DCD8044A")
        assert canonical is not None

        # 3. Simulate browser natural language extraction and submission
        prompt = (
            "I need sustainable flexible packaging manufacturers in India. "
            "Suppliers should have at least 200 employees, experience with barrier films, "
            "and relevant sustainability certifications. Identify suitable candidates and explain missing evidence."
        )
        extracted = service.parse_brief_with_copilot(prompt)

        # Simulate copilot_view form submission logic
        f_max_sz = extracted.max_company_size or 2000
        effective_max_sz = f_max_sz if (extracted.max_company_size is not None or f_max_sz != 2000) else None

        updated = ExtractedSourcingRequirements(
            title=extracted.title.strip(),
            category=extracted.category.strip(),
            target_industries=extracted.target_industries,
            target_geographies=extracted.target_geographies,
            min_company_size=extracted.min_company_size,
            max_company_size=effective_max_sz,
            required_specifications=extracted.required_specifications,
            required_certifications=extracted.required_certifications,
            summary=extracted.summary.strip(),
        )

        new_brief1 = updated.to_sourcing_brief()
        saved1 = service.create_brief(new_brief1)
        assert saved1.brief_id == "BRIEF-DCD8044A", f"Expected BRIEF-DCD8044A, got {saved1.brief_id}"

        # 4. Simulate a second submission where max_size defaults to 2000
        updated_2000 = ExtractedSourcingRequirements(
            title=extracted.title.strip(),
            category=extracted.category.strip(),
            target_industries=extracted.target_industries,
            target_geographies=extracted.target_geographies,
            min_company_size=200,
            max_company_size=2000,
            required_specifications=extracted.required_specifications,
            required_certifications=[],
            summary=extracted.summary.strip(),
        )
        new_brief2 = updated_2000.to_sourcing_brief()
        saved2 = service.create_brief(new_brief2)
        assert saved2.brief_id == "BRIEF-DCD8044A", f"Expected BRIEF-DCD8044A, got {saved2.brief_id}"

        # 5. Verify database still contains exactly one Flexible Packaging brief
        all_briefs = service.get_all_briefs()
        flex_briefs = [b for b in all_briefs if "Flexible Packaging" in b.title]
        assert len(flex_briefs) == 1
        assert flex_briefs[0].brief_id == "BRIEF-DCD8044A"

        # 6. Verify genuinely different brief creates a separate record
        diff_crit = SourcingCriteria(
            target_industries=["Metal & Tin Can Packaging"],
            target_geographies=["India"],
            min_company_size=100,
        )
        diff_brief = SourcingBrief(
            brief_id="BRIEF-TEST-DIFF-CAN-01",
            title="Metal Tin Cans Packaging Sourcing",
            buyer_organization="VendorIQ Procurement Team",
            description="Industrial tin can and metal container suppliers in India.",
            criteria=diff_crit,
        )
        try:
            saved_diff = service.create_brief(diff_brief)
            assert saved_diff.brief_id == "BRIEF-TEST-DIFF-CAN-01"
        finally:
            # Clean up the test brief
            service.repository.delete_brief("BRIEF-TEST-DIFF-CAN-01")
