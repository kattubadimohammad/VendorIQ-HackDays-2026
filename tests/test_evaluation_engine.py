"""
Tests for Phase 6: Supplier Data Quality Audit and Deterministic Evaluation Engine.

Covers:
- SupplierQualityAuditor: duplicate detection, missing field identification, domain mismatch flags.
- SupplierEvaluationEngine: deterministic factor scoring (35/25/20/10/10), neutral baseline for missing data.
- Provenance and evidence tracking on all evaluations.
- Batch ranking and SQLite persistence.
"""

import pytest
import sqlite3
import tempfile
import os
from pathlib import Path

from core.models import (
    SupplierProfile,
    SourcingCriteria,
    ScoringWeights,
    ScoringDimension,
    SupplierScore,
    FactorScore,
    SourcingBrief,
)
from core.provenance import ProvenanceClassification
from db.connection import get_db_connection, init_db
from db.repository import VendorIQRepository
from services.data_quality_auditor import (
    SupplierQualityAuditor,
    DataQualityAuditReport,
    normalize_tokens,
    extract_domain_stem,
)
from services.evaluation_engine import SupplierEvaluationEngine


@pytest.fixture
def temp_db(tmp_path):
    """Create an isolated temporary database with schema applied."""
    db_file = tmp_path / "test_eval_engine.sqlite"
    db_path = str(db_file)
    init_db(db_path=db_path)
    return db_path


@pytest.fixture
def repository(temp_db):
    """Create repository pointing to temp database."""
    r = VendorIQRepository(db_path=temp_db)
    r.initialize()
    return r


@pytest.fixture
def sample_suppliers():
    """Create a diverse set of synthetic supplier profiles for testing."""
    return [
        SupplierProfile(
            supplier_id="SUPP-ALPHA-01",
            company_name="Alpha Packaging Pvt Ltd",
            domain="alphapackaging.com",
            description="Leading manufacturer of corrugated boxes, packaging cartons, and flexible films in India.",
            employee_range="201-500",
            headquarters_city="Mumbai",
            headquarters_country="India",
            certifications=[],
            corporate_contact_email="sales@alphapackaging.com",
        ),
        SupplierProfile(
            supplier_id="SUPP-BETA-02",
            company_name="Beta Plastics & Polymers Limited",
            domain="betaplastics.in",
            description="Produces biodegradable packaging, thermoforming dinnerware, and barrier films. Listed on BSE.",
            employee_range="51-200",
            headquarters_city="Ahmedabad",
            headquarters_country="India",
        ),
        SupplierProfile(
            supplier_id="SUPP-GAMMA-03",
            company_name="Gamma Industries",
            domain="totallyunrelateddomain.xyz",
            description="",  # Missing description
            employee_range=None,  # Missing employee range
            headquarters_city="",  # Missing city
            headquarters_country="India",
        ),
    ]


class TestDataQualityAuditor:
    """Tests for SupplierQualityAuditor."""

    def test_domain_stem_extraction(self):
        """Test brand stem extraction from various domain formats."""
        assert extract_domain_stem("arrowgreentech.com") == "arrowgreentech"
        assert extract_domain_stem("https://tcpl.in/about") == "tcpl"
        assert extract_domain_stem("sub.example.co.in") == "sub"

    def test_token_normalization(self):
        """Test token normalization removes corporate suffixes and punctuation."""
        tokens = normalize_tokens("Biopac India Corporation Ltd.")
        assert "biopac" in tokens
        assert "ltd" not in tokens
        assert "corporation" not in tokens
        assert "india" not in tokens

    def test_detect_domain_mismatch_flags_unrelated_domain(self):
        """Test that completely unrelated domains are flagged for manual review."""
        auditor = SupplierQualityAuditor()
        is_mismatch, reason = auditor._detect_domain_mismatch(
            company_name="Biopac India Corporation Ltd",
            domain="ecpulse.com",
        )
        assert is_mismatch is True
        assert "ecpulse" in reason
        assert "Biopac India Corporation Ltd" in reason

    def test_detect_domain_mismatch_clears_legitimate_match(self):
        """Test that legitimate domain and company matches are not flagged."""
        auditor = SupplierQualityAuditor()
        is_mismatch, _ = auditor._detect_domain_mismatch(
            company_name="Arrow Greentech Limited",
            domain="arrowgreentech.com",
        )
        assert is_mismatch is False

    def test_detect_domain_mismatch_handles_acronyms(self):
        """Test that acronyms/initialisms (e.g. ITPL for Innovative Tech Pack Limited) are accepted."""
        auditor = SupplierQualityAuditor()
        is_mismatch, _ = auditor._detect_domain_mismatch(
            company_name="INNOVATIVE TECH PACK LIMITED",
            domain="itplgroup.com",
        )
        assert is_mismatch is False

    def test_audit_records_detects_duplicates_and_missing_fields(self, sample_suppliers):
        """Test full batch audit correctly reports duplicates, missing fields, and anomalies."""
        # Add a duplicate supplier
        dup = SupplierProfile(
            supplier_id="SUPP-DUP-01",
            company_name="Alpha Packaging Pvt Ltd",
            domain="alphapackaging.com",
            description="Duplicate record",
            employee_range="201-500",
            headquarters_country="India",
        )
        suppliers = sample_suppliers + [dup]

        auditor = SupplierQualityAuditor()
        report = auditor.audit_records(suppliers)

        assert report.total_records == 4
        assert report.unique_domains == 3
        assert report.unique_company_names == 3
        assert "alphapackaging.com" in report.duplicate_domains
        assert "alpha packaging pvt ltd" in report.duplicate_names

        # Missing counts
        assert report.missing_fields_counts["description"] == 1
        assert report.missing_fields_counts["employee_range"] == 1

        # Suspicious mismatch should flag Gamma Industries -> totallyunrelateddomain.xyz
        assert report.suspicious_mismatches_count >= 1
        mismatched_names = [f["company_name"] for f in report.flagged_suppliers_for_review]
        assert "Gamma Industries" in mismatched_names


class TestSupplierEvaluationEngine:
    """Tests for deterministic scoring, neutral baselines, and ranking."""

    def test_default_weights_sum_to_one(self):
        """Confirm scoring weights strictly sum to 1.0."""
        weights = ScoringWeights()
        total = (
            weights.industry_relevance
            + weights.geographic_fit
            + weights.company_size_fit
            + weights.business_signals
            + weights.data_completeness
        )
        assert abs(total - 1.0) < 1e-6

    def test_deterministic_scoring_produces_identical_results(self, sample_suppliers):
        """Confirm evaluation is purely deterministic (same profile -> identical score)."""
        engine = SupplierEvaluationEngine()
        s = sample_suppliers[0]

        score1 = engine.evaluate_supplier(s)
        score2 = engine.evaluate_supplier(s)

        assert score1.total_score == score2.total_score
        for dim in ScoringDimension:
            assert score1.factor_scores[dim].score == score2.factor_scores[dim].score
            assert score1.factor_scores[dim].weighted_score == score2.factor_scores[dim].weighted_score

    def test_missing_data_neutral_baseline(self):
        """Confirm missing data receives neutral 50.0 score without penalty."""
        engine = SupplierEvaluationEngine()
        sparse_supplier = SupplierProfile(
            supplier_id="SUPP-SPARSE-01",
            company_name="Sparse Packaging Co",
            domain="sparsepackaging.com",
            description=None,  # Missing
            employee_range=None,  # Missing
            headquarters_city=None,
            headquarters_country="Unknown",
        )

        score = engine.evaluate_supplier(sparse_supplier)

        # Industry relevance factor must use neutral baseline
        f_ind = score.factor_scores[ScoringDimension.INDUSTRY_RELEVANCE]
        assert f_ind.score == 50.0
        assert f_ind.is_missing_data is True
        assert "Neutral baseline" in f_ind.reasoning

        # Size fit factor must use neutral baseline
        f_size = score.factor_scores[ScoringDimension.COMPANY_SIZE_FIT]
        assert f_size.score == 50.0
        assert f_size.is_missing_data is True

        # Total score should still be computed deterministically
        assert 0.0 <= score.total_score <= 100.0

    def test_provenance_and_evidence_tracking(self, sample_suppliers):
        """Confirm every factor score includes evidence source and AI_ASSESSMENT provenance."""
        engine = SupplierEvaluationEngine()
        score = engine.evaluate_supplier(sample_suppliers[0])

        assert score.provenance == ProvenanceClassification.AI_ASSESSMENT
        for dim, f in score.factor_scores.items():
            assert len(f.evidence_sources) > 0
            assert f.reasoning is not None and len(f.reasoning) > 10

    def test_batch_ranking_order(self, sample_suppliers):
        """Confirm evaluate_and_rank_batch sorts suppliers descending by total score."""
        engine = SupplierEvaluationEngine()
        ranked = engine.evaluate_and_rank_batch(sample_suppliers)

        assert len(ranked) == 3
        # Check descending order
        for i in range(len(ranked) - 1):
            assert ranked[i].total_score >= ranked[i + 1].total_score

        # Supplier with complete info, matched keywords, and signals should outrank sparse supplier
        assert ranked[0].company_name != "Gamma Industries"
        assert ranked[-1].company_name == "Gamma Industries"

    def test_persistence_to_sqlite(self, repository, sample_suppliers):
        """Confirm evaluation results can be persisted and retrieved from SQLite."""
        # Create a brief first for foreign key integrity
        criteria = SourcingCriteria(
            target_industries=["Packaging and Containers Manufacturing"],
            target_geographies=["India"],
        )
        brief = SourcingBrief(
            brief_id="TEST-BRIEF-01",
            title="Packaging Suppliers India",
            buyer_organization="Test Org",
            description="Sourcing packaging suppliers in India for unit testing",
            criteria=criteria,
        )
        repository.create_brief(brief)

        # Save sample suppliers to DB
        for s in sample_suppliers:
            repository.create_supplier(s)

        engine = SupplierEvaluationEngine(repository=repository)
        ranked = engine.evaluate_and_rank_batch(
            sample_suppliers,
            persist_to_db=True,
            brief_id="TEST-BRIEF-01",
        )

        runs = repository.list_scoring_runs_for_brief("TEST-BRIEF-01")
        assert len(runs) == 1
        run_id = runs[0]["run_id"]

        evals = repository.list_evaluations_for_run(run_id)
        assert len(evals) == 3
        saved_scores = [e.total_score for e in evals]
        # Should be stored descending
        for i in range(len(saved_scores) - 1):
            assert saved_scores[i] >= saved_scores[i + 1]


class TestLiveSupplierEvaluation:
    """Tests auditing and evaluating the 18 live Crustdata supplier records from SQLite."""

    @pytest.fixture
    def live_suppliers(self):
        """Load live supplier records from vendoriq.sqlite if present."""
        live_db_path = "vendoriq.sqlite"
        if not os.path.exists(live_db_path):
            pytest.skip("vendoriq.sqlite does not exist in workspace.")
        repo = VendorIQRepository(db_path=live_db_path)
        suppliers = repo.list_suppliers(limit=100)
        return suppliers

    def test_live_data_quality_audit(self, live_suppliers):
        """Audit the live suppliers and verify expected quality metrics."""
        assert len(live_suppliers) == 18

        auditor = SupplierQualityAuditor()
        report = auditor.audit_records(live_suppliers)

        assert report.total_records == 18
        assert report.unique_domains == 18
        assert report.unique_company_names == 18
        assert len(report.duplicate_domains) == 0
        assert len(report.duplicate_names) == 0

        # All 18 have descriptions and employee ranges
        assert report.missing_fields_counts["description"] == 0
        assert report.missing_fields_counts["employee_range"] == 0

        # All 18 have missing headquarters city in basic discovery tier
        assert report.missing_fields_counts["headquarters_city"] == 18

        # Verify flagged domain mismatches include ecpulse.com and kediainfo.com
        flagged_domains = [f["domain"] for f in report.flagged_suppliers_for_review]
        assert "ecpulse.com" in flagged_domains
        assert "kediainfo.com" in flagged_domains

    def test_live_supplier_scoring_and_ranking(self, live_suppliers):
        """Evaluate and rank all 18 live suppliers deterministically."""
        engine = SupplierEvaluationEngine()
        ranked = engine.evaluate_and_rank_batch(
            suppliers=live_suppliers,
            target_industry="Packaging and Containers Manufacturing",
            target_country="India",
        )

        assert len(ranked) == 18

        # Verify descending order of total_score
        for i in range(len(ranked) - 1):
            assert ranked[i].total_score >= ranked[i + 1].total_score

        # Verify each scorecard has all 5 factor scores and valid ranges
        for sc in ranked:
            assert 0.0 <= sc.total_score <= 100.0
            assert len(sc.factor_scores) == 5
            for dim, factor in sc.factor_scores.items():
                assert 0.0 <= factor.score <= 100.0
                assert factor.weight > 0.0
                assert factor.reasoning is not None

            # Confirm no supplier is labeled as verified or approved
            assert "evaluated_candidate" in sc.summary.lower()
            assert "not verified/approved" in sc.summary.lower()
