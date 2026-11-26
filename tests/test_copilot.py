"""
Tests for Phase 8A: AI-Powered Sourcing Copilot.

Validates:
- Structured natural language sourcing brief parsing (mocked Gemini and deterministic fallback).
- Grounded supplier recommendations (strictly preserves deterministic scores and ranking).
- Provenance separation (VERIFIED_DATA, BUYER_SPECIFIED, AI_ASSESSMENT, UNVERIFIED_OR_MISSING).
- Missing evidence alerts and domain review warnings.
- Robustness against malformed AI outputs and offline mode.
- Interactive procurement chat assistant.
"""

import json
import pytest
import os
from unittest.mock import MagicMock, patch

from core.models import (
    SourcingBrief,
    SourcingCriteria,
    ScoringWeights,
    SupplierProfile,
    SupplierScore,
    ExtractedSourcingRequirements,
    CopilotRecommendation,
    CopilotQueryResponse,
    SupplierEvidenceRecord,
    EvidenceCategory,
    EvidenceStatus,
)
from core.provenance import ProvenanceClassification
from db.connection import init_db
from db.repository import VendorIQRepository
from services.copilot_service import AISourcingCopilot
from services.evaluation_engine import SupplierEvaluationEngine


@pytest.fixture
def temp_db_path(tmp_path):
    """Create isolated temporary database."""
    db_file = tmp_path / "test_copilot.sqlite"
    db_path = str(db_file)
    init_db(db_path=db_path)
    return db_path


@pytest.fixture
def sample_suppliers():
    """Create sample packaging supplier profiles."""
    return [
        SupplierProfile(
            supplier_id="SUPP_1",
            company_name="Alpha Green Packaging",
            domain="alphagreen.com",
            description="Leading producer of biodegradable films, compostable pouches, and flexible packaging in India.",
            employee_range="51-200",
            headquarters_country="India",
        ),
        SupplierProfile(
            supplier_id="SUPP_2",
            company_name="Beta Corrugated Boxes Ltd",
            domain="betaboxes.in",
            description="High-volume manufacturer of corrugated boxes, cartons, and industrial shipping packaging.",
            employee_range="201-500",
            headquarters_country="India",
        ),
        SupplierProfile(
            supplier_id="SUPP_3",
            company_name="Gamma Discrepant Entity",
            domain="totallyunrelateddomain.xyz",
            description="General packaging items.",
            employee_range="2-10",
            headquarters_country="India",
        ),
    ]


@pytest.fixture
def sample_brief():
    """Create sample sourcing brief."""
    return SourcingBrief(
        brief_id="BRIEF-TEST-COPILOT-01",
        title="Eco-Friendly Packaging Sourcing",
        buyer_organization="Retail Procurement Team",
        description="Sourcing sustainable packaging manufacturers in India.",
        criteria=SourcingCriteria(
            target_industries=["Packaging and Containers Manufacturing"],
            target_geographies=["India"],
            min_company_size=50,
            max_company_size=500,
        ),
    )


class TestExtractedSourcingRequirements:
    """Test Pydantic model methods for ExtractedSourcingRequirements."""

    def test_to_sourcing_brief_conversion(self):
        """Verify converting extracted requirements to a formal SourcingBrief."""
        reqs = ExtractedSourcingRequirements(
            title="Recyclable Flexible Packaging Sourcing",
            category="Flexible Packaging",
            target_industries=["Packaging and Containers Manufacturing"],
            target_geographies=["India"],
            min_company_size=100,
            max_company_size=1000,
            required_specifications=["Moisture Barrier", "High Recyclability"],
            required_certifications=["ISO 9001"],
            summary="Sourcing recyclable films and barrier pouches in India.",
        )

        brief = reqs.to_sourcing_brief(brief_id="BRIEF-CONVERTED-01", buyer_organization="Buyer Corp")
        assert brief.brief_id == "BRIEF-CONVERTED-01"
        assert brief.title == "Recyclable Flexible Packaging Sourcing"
        assert brief.buyer_organization == "Buyer Corp"
        assert brief.criteria.min_company_size == 100
        assert brief.criteria.max_company_size == 1000
        assert "ISO 9001" in brief.criteria.required_certifications


class TestNaturalLanguageBriefParsing:
    """Tests parsing buyer plain-text requirements using mocked Gemini and fallback."""

    def test_parse_brief_with_mocked_gemini(self):
        """Test structured extraction when Gemini returns valid JSON."""
        mock_response = MagicMock()
        mock_response.text = json.dumps({
            "title": "Sustainable Compostable Packaging Sourcing",
            "category": "Biodegradable Packaging",
            "target_industries": ["Packaging and Containers Manufacturing"],
            "target_geographies": ["India"],
            "min_company_size": 50,
            "max_company_size": 500,
            "required_specifications": ["Compostable", "Water-soluble"],
            "required_certifications": ["ISO 9001"],
            "summary": "Procurement of compostable films and barrier pouches in India.",
        })

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        copilot = AISourcingCopilot(client=mock_client)
        result = copilot.parse_natural_language_brief(
            "We need sustainable compostable films in India with 50-500 employees and ISO 9001."
        )

        assert isinstance(result, ExtractedSourcingRequirements)
        assert result.title == "Sustainable Compostable Packaging Sourcing"
        assert result.min_company_size == 50
        assert result.max_company_size == 500
        assert "ISO 9001" in result.required_certifications
        assert "Compostable" in result.required_specifications
        mock_client.models.generate_content.assert_called_once()

    def test_parse_brief_malformed_json_triggers_deterministic_fallback(self):
        """Test graceful fallback when Gemini returns invalid non-JSON output."""
        mock_response = MagicMock()
        mock_response.text = "I cannot fulfill this request as JSON: {invalid json..."

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        copilot = AISourcingCopilot(client=mock_client)
        # Should NOT raise an exception, must gracefully return deterministic fallback
        result = copilot.parse_natural_language_brief(
            "We need corrugated boxes in India with at least 100 employees and ISO certification."
        )

        assert isinstance(result, ExtractedSourcingRequirements)
        assert "Corrugated" in result.category or "Packaging" in result.category
        assert result.min_company_size == 100
        assert "ISO 9001" in result.required_certifications

    def test_deterministic_offline_parser(self):
        """Test purely offline deterministic parser without any client."""
        copilot = AISourcingCopilot(client=None)
        assert not copilot.is_online

        result = copilot.parse_natural_language_brief(
            "Looking for biodegradable compostable films and packaging in India with 50 to 500 employees"
        )
        assert isinstance(result, ExtractedSourcingRequirements)
        assert result.min_company_size == 50
        assert result.max_company_size == 500
        assert "India" in result.target_geographies


class TestGroundedRecommendationsAndIntegrity:
    """Tests supplier recommendations grounded in SQLite evaluations."""

    def test_recommendations_preserve_deterministic_scores_and_ranking(
        self, temp_db_path, sample_suppliers, sample_brief
    ):
        """Confirm AI Copilot never alters or overrides deterministic evaluation scores."""
        repo = VendorIQRepository(db_path=temp_db_path)
        repo.create_brief(sample_brief)
        for s in sample_suppliers:
            repo.create_supplier(s)

        engine = SupplierEvaluationEngine(repository=repo)
        copilot = AISourcingCopilot(repository=repo, evaluation_engine=engine, client=None)

        recs = copilot.recommend_candidates(brief=sample_brief, top_k=3)

        assert len(recs) == 3
        # Verify strictly descending order
        for i in range(len(recs) - 1):
            assert recs[i].total_score >= recs[i + 1].total_score
            assert recs[i].rank == i + 1

        # Check that top candidate has highest score and matched capabilities
        top = recs[0]
        assert top.total_score > 0.0
        assert len(top.matched_capabilities) > 0
        assert len(top.missing_evidence_flags) > 0

    def test_missing_evidence_and_domain_mismatch_flags(
        self, temp_db_path, sample_suppliers, sample_brief
    ):
        """Confirm missing evidence and domain mismatch flags are surfaced in recommendations."""
        repo = VendorIQRepository(db_path=temp_db_path)
        repo.create_brief(sample_brief)
        for s in sample_suppliers:
            repo.create_supplier(s)

        engine = SupplierEvaluationEngine(repository=repo)
        copilot = AISourcingCopilot(repository=repo, evaluation_engine=engine, client=None)

        recs = copilot.recommend_candidates(brief=sample_brief, top_k=3)

        # Gamma Discrepant Entity should have a domain mismatch flag
        gamma_rec = next((r for r in recs if r.supplier_id == "SUPP_3"), None)
        assert gamma_rec is not None
        flags_text = " ".join(gamma_rec.missing_evidence_flags).lower()
        assert "domain mismatch" in flags_text or "city" in flags_text


class TestInteractiveChatAssistant:
    """Tests interactive chat assistant functionality."""

    def test_chat_assistant_with_mocked_gemini(self, temp_db_path, sample_suppliers, sample_brief):
        """Test grounded chat response generation using mocked Gemini."""
        repo = VendorIQRepository(db_path=temp_db_path)
        repo.create_brief(sample_brief)
        for s in sample_suppliers:
            repo.create_supplier(s)

        mock_response = MagicMock()
        mock_response.text = (
            "Based on the active brief criteria, Alpha Green Packaging is the top candidate "
            "for biodegradable films. Note that city headquarters and capacity are unverified."
        )
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        copilot = AISourcingCopilot(repository=repo, client=mock_client)
        resp = copilot.chat_sourcing_assistant(
            message="Which supplier is best for sustainable packaging?",
            current_brief=sample_brief,
        )

        assert isinstance(resp, CopilotQueryResponse)
        assert "Alpha Green Packaging" in resp.assistant_message
        assert resp.grounded_record_count == 3
        mock_client.models.generate_content.assert_called_once()

    def test_chat_assistant_deterministic_fallback(self, temp_db_path, sample_suppliers, sample_brief):
        """Test chat assistant deterministic fallback when client is None."""
        repo = VendorIQRepository(db_path=temp_db_path)
        repo.create_brief(sample_brief)
        for s in sample_suppliers:
            repo.create_supplier(s)

        copilot = AISourcingCopilot(repository=repo, client=None)
        resp = copilot.chat_sourcing_assistant(
            message="What are the risk factors and review flags?",
            current_brief=sample_brief,
        )

        assert isinstance(resp, CopilotQueryResponse)
        assert "Data Quality" in resp.assistant_message or "Alerts" in resp.assistant_message
        assert resp.grounded_record_count == 3


class TestLiveDatabaseCopilotIntegration:
    """Integration tests running copilot against live records in vendoriq.sqlite."""

    def test_live_recommendations_match_live_evaluations(self):
        """Confirm copilot recommendations against vendoriq.sqlite match deterministic rankings."""
        if not os.path.exists("vendoriq.sqlite"):
            pytest.skip("vendoriq.sqlite not found.")

        repo = VendorIQRepository(db_path="vendoriq.sqlite")
        briefs = repo.list_briefs()
        assert len(briefs) >= 1
        brief = briefs[0]

        copilot = AISourcingCopilot(repository=repo, client=None)
        recs = copilot.recommend_candidates(brief=brief, top_k=5)

        assert len(recs) == 5
        # Verify Top 3 match exact deterministic evaluation engine
        assert recs[0].company_name == "Cosmo Films"
        assert recs[0].total_score == 81.50
        assert recs[1].company_name == "Arrow Greentech Ltd."
        assert recs[1].total_score == 79.00
        assert recs[2].company_name == "Biopac India Corporation Ltd"
        assert recs[2].total_score == 76.50


class TestCopilotDomainAttributionIntegrity:
    """Isolated tests verifying domain attribution accuracy without accessing live production database."""

    def test_copilot_domain_attribution_accuracy_isolated_database(self, temp_db_path):
        """
        Verify that Copilot domain mismatch and risk answers accurately attribute domains
        using an isolated temporary test database populated with supplier profiles and evidence:
        - ecpulse.com belongs to Biopac India Corporation Ltd (CRUST_698)
        - kediainfo.com belongs to VIKAAS PAPER FLEXO PACKAGING LIMITED (CRUST_2552)
        - cosmofilms.com belongs to Cosmo Films (no mismatch)
        - arrowgreentech.com belongs to Arrow Greentech Ltd. (no mismatch)
        - Never incorrectly attributes ecpulse.com or kediainfo.com to Cosmo Films or Arrow Greentech.
        - Strictly isolated: never reads, writes, or opens vendoriq.sqlite.
        """
        repo = VendorIQRepository(db_path=temp_db_path)
        assert repo.db_path == temp_db_path
        assert "vendoriq.sqlite" not in repo.db_path

        brief = SourcingBrief(
            brief_id="BRIEF-ISOLATED-TEST",
            title="Sustainable Packaging Sourcing Project",
            buyer_organization="EcoProcure Corp",
            description="Testing isolated copilot domain risk attribution.",
            criteria=SourcingCriteria(
                target_industries=["Packaging and Containers Manufacturing"],
                target_geographies=["India"],
                min_company_size=50,
            ),
        )
        repo.create_brief(brief)

        test_suppliers = [
            SupplierProfile(
                supplier_id="CRUST_698",
                company_name="Biopac India Corporation Ltd",
                domain="ecpulse.com",
                description="Manufacturer of biodegradable packaging and food dinnerware.",
                employee_range="201-500",
                headquarters_country="India",
                headquarters_city="Silvassa",
            ),
            SupplierProfile(
                supplier_id="CRUST_2552",
                company_name="VIKAAS PAPER FLEXO PACKAGING LIMITED",
                domain="kediainfo.com",
                description="Corrugated box division of Kedia Group.",
                employee_range="51-200",
                headquarters_country="India",
                headquarters_city="Mumbai",
            ),
            SupplierProfile(
                supplier_id="CRUST_1049",
                company_name="Cosmo Films",
                domain="cosmofilms.com",
                description="Global leader in specialty films, flexible packaging, and barrier applications.",
                employee_range="1001-5000",
                headquarters_country="India",
                headquarters_city="New Delhi",
            ),
            SupplierProfile(
                supplier_id="CRUST_356",
                company_name="Arrow Greentech Ltd.",
                domain="arrowgreentech.com",
                description="Pioneer in water-soluble films and bio-based packaging.",
                employee_range="51-200",
                headquarters_country="India",
                headquarters_city="Mumbai",
            ),
        ]
        for s in test_suppliers:
            repo.create_supplier(s)

        test_evidence = [
            SupplierEvidenceRecord(
                evidence_id="EVID-TEST-698-DOM",
                supplier_id="CRUST_698",
                category=EvidenceCategory.COMPANY_WEBSITE_DOMAIN,
                claim="Domain 'ecpulse.com' is associated with Biopac India Corporation Ltd in Crustdata directory index, but flagged for domain mismatch.",
                status=EvidenceStatus.UNVERIFIED,
                source_name="Crustdata Directory Index (Flagged)",
                reviewer_notes="Stem 'ecpulse' shares no tokens with company name.",
            ),
            SupplierEvidenceRecord(
                evidence_id="EVID-TEST-2552-DOM",
                supplier_id="CRUST_2552",
                category=EvidenceCategory.COMPANY_WEBSITE_DOMAIN,
                claim="Domain 'kediainfo.com' is associated with VIKAAS PAPER FLEXO PACKAGING LIMITED in Crustdata directory index, but flagged for domain mismatch.",
                status=EvidenceStatus.UNVERIFIED,
                source_name="Crustdata Directory Index (Flagged)",
                reviewer_notes="Stem 'kediainfo' shares no tokens with company name.",
            ),
            SupplierEvidenceRecord(
                evidence_id="EVID-TEST-1049-DOM",
                supplier_id="CRUST_1049",
                category=EvidenceCategory.COMPANY_WEBSITE_DOMAIN,
                claim="Corporate web domain: cosmofilms.com",
                status=EvidenceStatus.SELF_REPORTED,
                source_name="Crustdata Company Search",
                reviewer_notes="Domain matches company name.",
            ),
            SupplierEvidenceRecord(
                evidence_id="EVID-TEST-356-DOM",
                supplier_id="CRUST_356",
                category=EvidenceCategory.COMPANY_WEBSITE_DOMAIN,
                claim="Corporate web domain: arrowgreentech.com",
                status=EvidenceStatus.SELF_REPORTED,
                source_name="Crustdata Company Search",
                reviewer_notes="Domain matches company name.",
            ),
        ]
        for ev in test_evidence:
            repo.create_evidence(ev)

        copilot = AISourcingCopilot(repository=repo, client=None)

        resp = copilot.chat_sourcing_assistant(
            message="What are the domain mismatch risks and website alerts?",
            current_brief=brief,
        )
        msg = resp.assistant_message

        # Assert correct attribution backed by isolated database records
        assert "Biopac India Corporation Ltd" in msg and "ecpulse.com" in msg
        assert "VIKAAS PAPER FLEXO PACKAGING LIMITED" in msg and "kediainfo.com" in msg
        assert "Cosmo Films" in msg and "cosmofilms.com" in msg
        assert "Arrow Greentech Ltd." in msg and "arrowgreentech.com" in msg

        # Assert strict non-misattribution
        assert "Cosmo Films: Registered directory domain is `ecpulse.com`" not in msg
        assert "Arrow Greentech Ltd.: Registered directory domain is `kediainfo.com`" not in msg

        # Assert database isolation: evidence source reflects temp db, never production db
        assert os.path.basename(temp_db_path) in resp.evidence_sources[0]
        assert "vendoriq.sqlite" not in resp.evidence_sources[0]
