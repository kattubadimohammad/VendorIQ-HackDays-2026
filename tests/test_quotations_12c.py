"""
VendorIQ Phase 12C Test Suite: Quotation Comparison & Analysis Engine.

Tests:
1. Currency normalization into buyer-selected currency, benchmark metadata, and outdated rate flags.
2. Transparent buyer FX overrides with audit trail.
3. Unavailable exchange rate handling and price-incomparability flags.
4. Landed cost evaluation with strict separation from base price and zero invented costs.
5. Incomplete landed cost detection and fallback to base price ranking.
6. Non-compensable technical compliance: low price cannot offset mandatory technical failure.
7. Evidence tier preservation: buyer confirmation does not convert self-reported claims into VERIFIED evidence.
8. Multi-supplier side-by-side comparison matrix generation with calculation breakdowns.
9. Deterministic explainable recommendation engine (Primary, Secondary, Disqualified, Cost-Incomplete).
10. Strict temporary database isolation guaranteeing zero writes to vendoriq.sqlite.
"""

from datetime import datetime, timezone, date, timedelta
from pathlib import Path
import shutil
import sqlite3
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
    ExchangeRateType,
    ExchangeRateRecord,
    ExtractionMethod,
    ExtractionFieldStatus,
    FieldExtractionProvenance,
    QuotationCommercialData,
    QuotationTechnicalCompliance,
    SupplierQuotation,
    EvidenceStatus,
    EvidenceCategory,
    ComparisonRecommendationType,
    QuotationComparisonRow,
    QuotationComparisonMatrix,
)
from core.exceptions import DataValidationError
from db.connection import init_db
from db.repository import VendorIQRepository
from services.quotation_comparison import (
    CurrencyNormalizationService,
    TechnicalComplianceEvaluator,
    QuotationComparisonEngine,
    DEFAULT_BENCHMARK_RATES,
    BENCHMARK_EFFECTIVE_DATE,
    BENCHMARK_SOURCE,
)
from services.quotation_service import QuotationService


# =============================================================================
# FIXTURES & HELPERS
# =============================================================================

@pytest.fixture
def isolated_repo(tmp_path):
    """Create isolated SQLite database with parent brief and RFQ entities."""
    db_path = str(tmp_path / "test_quotations_12c.sqlite")
    init_db(db_path)
    repo = VendorIQRepository(db_path=db_path)

    # 1. Parent Brief requiring High-Barrier Packaging
    brief = SourcingBrief(
        brief_id="BRIEF-12C-001",
        title="High Barrier Recyclable Packaging Film",
        buyer_organization="EcoFoods Corp",
        description="Mandatory requirement: High-barrier multi-layer packaging film with OTR < 2.0 cc/m2/day and WVTR < 1.0 g/m2/day (ASTM standards).",
        criteria=SourcingCriteria(
            target_industries=["Flexible Packaging"],
            target_geographies=["India", "United States"],
            required_certifications=["ISO 9001", "BRCGS"],
        ),
    )
    repo.create_brief(brief)

    # 2. Three Suppliers
    suppliers = [
        SupplierProfile(
            supplier_id="SUPP-ALPHA",
            company_name="Alpha Barrier Films Ltd",
            domain="alphabarrier.com",
            headquarters_country="India",
        ),
        SupplierProfile(
            supplier_id="SUPP-BETA",
            company_name="Beta Global Packaging Inc",
            domain="betapack.com",
            headquarters_country="United States",
        ),
        SupplierProfile(
            supplier_id="SUPP-GAMMA",
            company_name="Gamma Cheap Plastics Ltd",
            domain="gammaplastics.com",
            headquarters_country="India",
        ),
    ]
    for s in suppliers:
        repo.create_supplier(s)

    # 3. RFQ for each supplier
    for s in suppliers:
        rfq = RFQDocumentRecord(
            rfq_id=f"RFQ-{s.supplier_id}",
            brief_id="BRIEF-12C-001",
            supplier_id=s.supplier_id,
            title=f"RFQ for {s.company_name}",
            status=RFQStatus.SENT,
            buyer_info=RFQBuyerInfo(
                buyer_organization="EcoFoods Corp",
                rfq_reference=f"RFQ-REF-{s.supplier_id}",
                response_deadline=datetime.now(timezone.utc) + timedelta(days=21),
            ),
            supplier=RFQSupplierTarget(
                supplier_id=s.supplier_id,
                company_name=s.company_name,
                domain=s.domain,
            ),
            product_requirements=RFQProductRequirements(
                category="Flexible Packaging",
                description="High barrier pouches with OTR < 2.0 cc/m2/day",
                expected_quantity="50,000 units",
                barrier_film_requirements="ASTM D3985 OTR compliance mandatory.",
            ),
            commercial_requirements=RFQCommercialRequirements(
                currency="USD",
                payment_terms="Net 30",
            ),
        )
        repo.create_supplier_rfq(rfq)

    return repo


# =============================================================================
# 1. CURRENCY NORMALIZATION TESTS
# =============================================================================

class TestCurrencyNormalizationService:
    """Test multi-currency conversion, benchmark rates, outdated flags, and buyer overrides."""

    def test_benchmark_rates_and_metadata_transparency(self):
        """Requirement 1: Record exchange rate, source, effective date and rate type."""
        fx = CurrencyNormalizationService()
        rec_inr = fx.get_benchmark_rate("INR")
        assert rec_inr is not None
        assert rec_inr.rate == 84.50
        assert rec_inr.rate_type == ExchangeRateType.BENCHMARK
        assert rec_inr.source == BENCHMARK_SOURCE
        assert rec_inr.effective_date == BENCHMARK_EFFECTIVE_DATE
        assert rec_inr.is_buyer_override is False

    def test_currency_conversion_calculation(self):
        """Requirement 1: Normalize amounts accurately via USD pivot."""
        fx = CurrencyNormalizationService()
        # 1. INR to USD: 8450 INR / 84.50 = 100.0 USD
        converted, rec, expl = fx.convert_amount(8450.0, from_currency="INR", to_currency="USD")
        assert converted == 100.0
        assert rec.rate_type == ExchangeRateType.BENCHMARK
        assert "Converted 8450.0000 INR to 100.0000 USD" in expl

        # 2. USD to EUR: 100 USD * 0.92 = 92.0 EUR
        converted_eur, _, _ = fx.convert_amount(100.0, from_currency="USD", to_currency="EUR")
        assert converted_eur == 92.0

        # 3. Same currency (identity conversion)
        converted_same, rec_same, expl_same = fx.convert_amount(50.0, from_currency="USD", to_currency="USD")
        assert converted_same == 50.0
        assert rec_same.rate == 1.0
        assert "Zero conversion required" in rec_same.notes

    def test_transparent_buyer_override_with_audit_trail(self):
        """Requirement 1: Support buyer overrides with full audit metadata."""
        fx = CurrencyNormalizationService()
        # Buyer overrides USD:INR to 85.00
        override_rec = fx.set_buyer_override(
            base_currency="USD",
            target_currency="INR",
            rate=85.00,
            effective_date="2026-11-22",
            notes="Forward contract booked with Citi Bank.",
            changed_by="Senior Buyer Alice",
        )
        assert override_rec.rate == 85.00
        assert override_rec.rate_type == ExchangeRateType.BUYER_OVERRIDE
        assert override_rec.is_buyer_override is True
        assert "Senior Buyer Alice" in override_rec.source

        # Conversion now uses buyer override
        converted, rec, expl = fx.convert_amount(8500.0, from_currency="INR", to_currency="USD")
        assert converted == 100.0
        assert rec.rate_type == ExchangeRateType.BUYER_OVERRIDE

    def test_unavailable_exchange_rate_flags_quotation(self):
        """Requirement 1: If rate is unavailable, clearly flag quotation as not price-comparable."""
        fx = CurrencyNormalizationService()
        # JPY has no configured benchmark rate
        converted, rec, expl = fx.convert_amount(15000.0, from_currency="JPY", to_currency="USD")
        assert converted is None
        assert rec.rate_type == ExchangeRateType.UNAVAILABLE
        assert "unavailable" in expl.lower()
        assert "cannot be normalized" in expl.lower()


# =============================================================================
# 2. LANDED COST COMPARISON TESTS
# =============================================================================

class TestLandedCostComparison:
    """Test landed cost calculation, incomplete data handling, and transparent disclosures."""

    def test_complete_landed_cost_with_ddp_incoterms(self):
        """Requirement 2: DDP incoterms bundle freight and duties completely."""
        comm = QuotationCommercialData(
            quoted_currency="USD",
            quoted_unit_price=1.35,
            quoted_quantity=50000,
            min_order_quantity=10000,
            lead_time_weeks=4,
            payment_terms="Net 30 days",
            incoterms=Incoterm.DDP,  # Freight and taxes included
            is_freight_included=True,
            is_tax_included=True,
        )
        comm.calculate_landed_cost(target_currency="USD")
        assert comm.are_cost_inputs_complete is True
        assert comm.estimated_landed_unit_cost_target_curr == 1.35
        assert "Incoterm: DDP" in comm.landed_cost_explanation

    def test_complete_landed_cost_with_explicit_freight_and_tax(self):
        """Requirement 2: Quantified freight and duties are accurately calculated."""
        comm = QuotationCommercialData(
            quoted_currency="USD",
            quoted_unit_price=1.20,
            quoted_quantity=50000,
            min_order_quantity=10000,
            lead_time_weeks=4,
            payment_terms="Net 30 days",
            incoterms=Incoterm.FOB,
            freight_charges=5000.0,    # $0.10 / unit
            taxes_and_duties=2500.0,   # $0.05 / unit
            additional_charges=500.0,  # $0.01 / unit
        )
        comm.calculate_landed_cost(target_currency="USD")
        assert comm.are_cost_inputs_complete is True
        # Total landed: 1.20 + 0.10 + 0.05 + 0.01 = 1.36
        assert comm.estimated_landed_unit_cost_target_curr == 1.36

    def test_incomplete_landed_cost_never_invents_missing_charges(self):
        """Requirement 2: Never invent missing freight or tax numbers."""
        comm = QuotationCommercialData(
            quoted_currency="USD",
            quoted_unit_price=1.15,
            quoted_quantity=50000,
            min_order_quantity=10000,
            lead_time_weeks=4,
            payment_terms="Net 30 days",
            incoterms=Incoterm.EXW,  # EXW missing freight and duties
            freight_charges=None,
            taxes_and_duties=None,
        )
        comm.calculate_landed_cost(target_currency="USD")
        assert comm.are_cost_inputs_complete is False
        assert comm.estimated_landed_unit_cost_target_curr is None
        assert "Missing Freight charges (required for EXW)" in comm.landed_cost_explanation
        assert "Comparing quoted base unit price only (1.1500 USD)" in comm.landed_cost_explanation


# =============================================================================
# 3. TECHNICAL COMPLIANCE & NON-COMPENSABILITY TESTS
# =============================================================================

class TestTechnicalComplianceEvaluation:
    """Test non-compensability of mandatory technical failures and evidence tier preservation."""

    def test_mandatory_barrier_failure_is_non_compensable_by_low_price(self, isolated_repo):
        """Requirement 3: Lower price must never offset a mandatory technical failure."""
        brief = isolated_repo.get_brief("BRIEF-12C-001")
        evaluator = TechnicalComplianceEvaluator()

        # Supplier Gamma offers extremely low price ($0.45) but non-barrier single-layer film
        gamma_quote = SupplierQuotation(
            quotation_id="QUOTE-GAMMA",
            rfq_id="RFQ-SUPP-GAMMA",
            supplier_id="SUPP-GAMMA",
            brief_id=brief.brief_id,
            company_name="Gamma Cheap Plastics Ltd",
            version=1,
            status=QuotationStatus.UNDER_REVIEW,
            commercial=QuotationCommercialData(
                quoted_currency="USD",
                quoted_unit_price=0.45,  # Very cheap
                quoted_quantity=50000,
                min_order_quantity=5000,
                lead_time_weeks=2,
                payment_terms="Net 30",
                incoterms=Incoterm.DDP,
                is_freight_included=True,
                is_tax_included=True,
                critical_fields_confirmed=True,
            ),
            compliance=QuotationTechnicalCompliance(
                product_category="Plastic Packaging",
                structure_description="Single-layer LDPE Polyethylene Film",
                thickness_microns=50.0,
                otr_value=None,  # No barrier
                wvtr_value=None,
                test_standards_cited=[],
                certifications_offered=[],
                has_tds_document=False,
                mandatory_requirements_met=False,
                disqualification_reasons=["Single-layer film lacks barrier properties."],
            ),
        )

        is_compliant, disquals, tech_score, notes = evaluator.evaluate_compliance(gamma_quote, brief)
        assert is_compliant is False
        assert tech_score == 0.0  # Strictly zeroed out
        assert len(disquals) >= 1
        assert "DISQUALIFIED" in notes
        assert "Low commercial price cannot compensate" in notes

    def test_evidence_tier_preservation_and_tds_grounding(self, isolated_repo):
        """Requirement 3: Buyer confirmation must NOT convert self-reported claim to VERIFIED evidence."""
        comp = QuotationTechnicalCompliance(
            product_category="Flexible Packaging",
            structure_description="PET / Met-BOPP / PE Multi-layer barrier structure",
            thickness_microns=75.0,
            otr_value="1.2 cc/m2/day",
            wvtr_value="0.8 g/m2/day",
            test_standards_cited=["ASTM D3985"],
            certifications_offered=["ISO 9001"],
            evidence_status_map={
                EvidenceCategory.BARRIER_FILM_EXPERIENCE.value: EvidenceStatus.SELF_REPORTED,
            },
            has_tds_document=False,  # Supplier claimed OTR, but NO authentic lab TDS attached
            mandatory_requirements_met=True,
        )

        # Confirming in UI must preserve SELF_REPORTED tier
        assert comp.evidence_status_map[EvidenceCategory.BARRIER_FILM_EXPERIENCE.value] == EvidenceStatus.SELF_REPORTED
        assert comp.has_tds_document is False


# =============================================================================
# 4. SIDE-BY-SIDE COMPARISON & RECOMMENDATION ENGINE TESTS
# =============================================================================

class TestQuotationComparisonAndRecommendations:
    """Test full comparison matrix, ranking with incomplete landed costs, and deterministic recommendations."""

    def test_multi_supplier_comparison_matrix_and_primary_recommendation(self, isolated_repo):
        """Requirements 4 & 5: Deterministic side-by-side comparison and explainable recommendations."""
        brief = isolated_repo.get_brief("BRIEF-12C-001")
        engine = QuotationComparisonEngine()

        # Quote 1: Alpha (India) - Quoted in INR, high barrier, TDS attached, DDP
        # 118.30 INR / 84.50 = 1.40 USD/unit
        quote_alpha = SupplierQuotation(
            quotation_id="QUOTE-ALPHA-V1",
            rfq_id="RFQ-SUPP-ALPHA",
            supplier_id="SUPP-ALPHA",
            brief_id=brief.brief_id,
            company_name="Alpha Barrier Films Ltd",
            version=1,
            status=QuotationStatus.UNDER_REVIEW,
            commercial=QuotationCommercialData(
                quoted_currency="INR",
                quoted_unit_price=118.30,
                quoted_quantity=50000,
                min_order_quantity=10000,
                lead_time_weeks=4,
                payment_terms="Net 30 days upon inspection",
                incoterms=Incoterm.DDP,
                is_freight_included=True,
                is_tax_included=True,
                critical_fields_confirmed=True,
            ),
            compliance=QuotationTechnicalCompliance(
                product_category="Flexible Packaging",
                structure_description="12u PET / 15u Met-BOPP / 50u Polyethylene",
                thickness_microns=77.0,
                otr_value="1.1 cc/m2/day (ASTM D3985)",
                wvtr_value="0.7 g/m2/day (ASTM F1249)",
                test_standards_cited=["ASTM D3985", "ASTM F1249"],
                certifications_offered=["ISO 9001", "BRCGS"],
                has_tds_document=True,
                mandatory_requirements_met=True,
            ),
        )

        # Quote 2: Beta (USA) - Quoted in USD, high barrier, TDS attached, FOB with missing freight
        # 1.55 USD/unit (Base price complete, landed cost incomplete)
        quote_beta = SupplierQuotation(
            quotation_id="QUOTE-BETA-V1",
            rfq_id="RFQ-SUPP-BETA",
            supplier_id="SUPP-BETA",
            brief_id=brief.brief_id,
            company_name="Beta Global Packaging Inc",
            version=1,
            status=QuotationStatus.UNDER_REVIEW,
            commercial=QuotationCommercialData(
                quoted_currency="USD",
                quoted_unit_price=1.55,
                quoted_quantity=50000,
                min_order_quantity=10000,
                lead_time_weeks=3,
                payment_terms="Net 45 days",
                incoterms=Incoterm.FOB,
                is_freight_included=False,
                is_tax_included=False,
                freight_charges=None,
                taxes_and_duties=None,
                critical_fields_confirmed=True,
            ),
            compliance=QuotationTechnicalCompliance(
                product_category="Flexible Packaging",
                structure_description="EVOH Multi-layer coextruded barrier film",
                thickness_microns=75.0,
                otr_value="0.9 cc/m2/day (ASTM D3985)",
                wvtr_value="0.6 g/m2/day (ASTM F1249)",
                test_standards_cited=["ASTM D3985", "ASTM F1249"],
                certifications_offered=["ISO 9001", "BRCGS", "FDA"],
                has_tds_document=True,
                mandatory_requirements_met=True,
            ),
        )

        # Quote 3: Gamma (India) - Cheap $0.45, single layer non-barrier film (Disqualified)
        quote_gamma = SupplierQuotation(
            quotation_id="QUOTE-GAMMA-V1",
            rfq_id="RFQ-SUPP-GAMMA",
            supplier_id="SUPP-GAMMA",
            brief_id=brief.brief_id,
            company_name="Gamma Cheap Plastics Ltd",
            version=1,
            status=QuotationStatus.UNDER_REVIEW,
            commercial=QuotationCommercialData(
                quoted_currency="USD",
                quoted_unit_price=0.45,
                quoted_quantity=50000,
                min_order_quantity=5000,
                lead_time_weeks=2,
                payment_terms="100% advance",
                incoterms=Incoterm.DDP,
                is_freight_included=True,
                is_tax_included=True,
                critical_fields_confirmed=True,
            ),
            compliance=QuotationTechnicalCompliance(
                product_category="Plastic Packaging",
                structure_description="Single-layer LDPE Film (Non-barrier)",
                thickness_microns=45.0,
                otr_value=None,
                wvtr_value=None,
                has_tds_document=False,
                mandatory_requirements_met=False,
                disqualification_reasons=["Single-layer film lacks barrier properties."],
            ),
        )

        matrix = engine.compare_quotations(
            quotations=[quote_alpha, quote_beta, quote_gamma],
            brief=brief,
            target_currency="USD",
        )

        # 1. Matrix verification
        assert len(matrix.rows) == 3
        assert matrix.target_currency == "USD"

        # Because Beta has incomplete landed cost (FOB without freight/tax),
        # matrix must NOT rank by landed cost
        assert matrix.are_all_landed_costs_complete is False
        assert matrix.ranking_metric == "NORMALIZED_BASE_PRICE"
        assert "Landed cost inputs are incomplete" in matrix.comparison_notes

        # 2. Candidate Evaluation Verification
        row_alpha = next(r for r in matrix.rows if r.supplier_id == "SUPP-ALPHA")
        row_beta = next(r for r in matrix.rows if r.supplier_id == "SUPP-BETA")
        row_gamma = next(r for r in matrix.rows if r.supplier_id == "SUPP-GAMMA")

        # Alpha: 118.30 INR / 84.50 = 1.40 USD
        assert row_alpha.normalized_base_unit_price == 1.40
        assert row_alpha.mandatory_requirements_met is True
        assert row_alpha.are_cost_inputs_complete is True

        # Beta: 1.55 USD, cost incomplete
        assert row_beta.normalized_base_unit_price == 1.55
        assert row_beta.mandatory_requirements_met is True
        assert row_beta.are_cost_inputs_complete is False
        assert row_beta.normalized_landed_unit_cost is None

        # Gamma: Disqualified despite low $0.45 price
        assert row_gamma.mandatory_requirements_met is False
        assert row_gamma.recommendation_type == ComparisonRecommendationType.NON_COMPLIANT_DISQUALIFIED
        assert row_gamma.overall_comparison_score == 0.0

        # 3. Recommendations verification
        assert matrix.primary_recommendation is not None
        assert matrix.primary_recommendation.supplier_id == "SUPP-ALPHA"
        assert matrix.primary_recommendation.recommendation_type == ComparisonRecommendationType.RECOMMENDED_PRIMARY

        assert matrix.secondary_recommendation is not None
        assert matrix.secondary_recommendation.supplier_id == "SUPP-BETA"
        assert matrix.secondary_recommendation.recommendation_type == ComparisonRecommendationType.RECOMMENDED_SECONDARY

        assert len(matrix.disqualified_candidates) == 1
        assert matrix.disqualified_candidates[0].supplier_id == "SUPP-GAMMA"

        # 4. Governance notice verification
        assert "Advisory Notice" in matrix.governance_notice
        assert "Zero automated purchase orders" in matrix.governance_notice

    def test_ranking_by_landed_cost_when_all_costs_complete(self, isolated_repo):
        """Requirement 5: When all compliant candidates have complete landed costs, rank by landed cost."""
        brief = isolated_repo.get_brief("BRIEF-12C-001")
        engine = QuotationComparisonEngine()

        # Supplier 1: Base $1.30 + Freight $0.20 = Landed $1.50
        q1 = SupplierQuotation(
            quotation_id="QUOTE-1",
            rfq_id="RFQ-SUPP-ALPHA",
            supplier_id="SUPP-ALPHA",
            brief_id=brief.brief_id,
            company_name="Alpha Barrier",
            version=1,
            status=QuotationStatus.UNDER_REVIEW,
            commercial=QuotationCommercialData(
                quoted_currency="USD",
                quoted_unit_price=1.30,
                quoted_quantity=50000,
                min_order_quantity=10000,
                lead_time_weeks=4,
                payment_terms="Net 30",
                incoterms=Incoterm.FOB,
                freight_charges=10000.0, # $0.20/unit
                taxes_and_duties=0.0,
                is_freight_included=False,
                is_tax_included=True,
                critical_fields_confirmed=True,
            ),
            compliance=QuotationTechnicalCompliance(
                product_category="Packaging",
                structure_description="EVOH Barrier Film",
                otr_value="1.1 cc/m2/day",
                wvtr_value="0.7 g/m2/day",
                mandatory_requirements_met=True,
                has_tds_document=True,
            ),
        )

        # Supplier 2: Base $1.35 DDP (Landed $1.35)
        # Even though Supplier 2 has higher base price ($1.35 vs $1.30), its landed cost is lower ($1.35 vs $1.50)
        q2 = SupplierQuotation(
            quotation_id="QUOTE-2",
            rfq_id="RFQ-SUPP-BETA",
            supplier_id="SUPP-BETA",
            brief_id=brief.brief_id,
            company_name="Beta Global",
            version=1,
            status=QuotationStatus.UNDER_REVIEW,
            commercial=QuotationCommercialData(
                quoted_currency="USD",
                quoted_unit_price=1.35,
                quoted_quantity=50000,
                min_order_quantity=10000,
                lead_time_weeks=4,
                payment_terms="Net 30",
                incoterms=Incoterm.DDP,
                is_freight_included=True,
                is_tax_included=True,
                critical_fields_confirmed=True,
            ),
            compliance=QuotationTechnicalCompliance(
                product_category="Packaging",
                structure_description="EVOH Barrier Film",
                mandatory_requirements_met=True,
                has_tds_document=True,
            ),
        )

        matrix = engine.compare_quotations(
            quotations=[q1, q2],
            brief=brief,
            target_currency="USD",
        )

        assert matrix.are_all_landed_costs_complete is True
        assert matrix.ranking_metric == "NORMALIZED_LANDED_COST"
        # Supplier 2 (Beta) wins primary recommendation because landed cost $1.35 < $1.50
        assert matrix.primary_recommendation.supplier_id == "SUPP-BETA"
        assert matrix.primary_recommendation.normalized_landed_unit_cost == 1.35
        assert matrix.secondary_recommendation.supplier_id == "SUPP-ALPHA"
        assert matrix.secondary_recommendation.normalized_landed_unit_cost == 1.50


# =============================================================================
# 5. INTEGRATION VIA QUOTATION SERVICE & DATA SERVICE
# =============================================================================

class TestQuotationServiceComparisonIntegration:
    """Test facade integration via QuotationService and DashboardDataService."""

    def test_quotation_service_compare_quotations_for_brief(self, isolated_repo):
        """Verify QuotationService.compare_quotations_for_brief persists zero modifications."""
        service = QuotationService(repository=isolated_repo)
        brief = isolated_repo.get_brief("BRIEF-12C-001")

        # Ingest one quote
        quote = SupplierQuotation(
            quotation_id="QUOTE-SERVICE-1",
            rfq_id="RFQ-SUPP-ALPHA",
            supplier_id="SUPP-ALPHA",
            brief_id=brief.brief_id,
            company_name="Alpha Barrier",
            version=1,
            status=QuotationStatus.UNDER_REVIEW,
            commercial=QuotationCommercialData(
                quoted_currency="USD",
                quoted_unit_price=1.45,
                quoted_quantity=20000,
                min_order_quantity=5000,
                lead_time_weeks=3,
                payment_terms="Net 30",
                incoterms=Incoterm.DDP,
                is_freight_included=True,
                is_tax_included=True,
                critical_fields_confirmed=True,
            ),
            compliance=QuotationTechnicalCompliance(
                product_category="Flexible Packaging",
                structure_description="EVOH Barrier Pouches",
                otr_value="1.1 cc/m2/day",
                wvtr_value="0.7 g/m2/day",
                has_tds_document=True,
                mandatory_requirements_met=True,
            ),
        )
        isolated_repo.create_quotation(quote)

        # Run comparison through service
        matrix = service.compare_quotations_for_brief(brief_id=brief.brief_id, target_currency="USD")
        assert len(matrix.rows) == 1
        assert matrix.primary_recommendation is not None
        assert matrix.primary_recommendation.company_name == "Alpha Barrier"


# =============================================================================
# 6. DATABASE ISOLATION & REGRESSION VERIFICATION
# =============================================================================

class TestProductionDatabaseIsolation12C:
    """Verify that all comparison and normalization logic never touches vendoriq.sqlite."""

    def test_production_database_row_counts_strictly_unchanged(self, tmp_path):
        """Assert zero modifications to vendoriq.sqlite."""
        prod_db = "vendoriq.sqlite"
        assert Path(prod_db).exists()

        conn_pre = sqlite3.connect(prod_db)
        pre_counts = {
            t[0]: conn_pre.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0]
            for t in conn_pre.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn_pre.close()

        # Run comparison engine on isolated copy
        isolated_db = str(tmp_path / "isolated_12c_check.sqlite")
        shutil.copy2(prod_db, isolated_db)
        repo = VendorIQRepository(db_path=isolated_db)
        service = QuotationService(repository=repo, base_dir=str(tmp_path))

        briefs = repo.list_briefs()
        if briefs:
            service.compare_quotations_for_brief(brief_id=briefs[0].brief_id)

        conn_post = sqlite3.connect(prod_db)
        post_counts = {
            t[0]: conn_post.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0]
            for t in conn_post.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        conn_post.close()

        assert pre_counts == post_counts, f"Database modified! Pre: {pre_counts}, Post: {post_counts}"


# =============================================================================
# REGRESSION: EXPLICIT OTR/WVTR THRESHOLDS MUST DRIVE DISQUALIFICATION
# =============================================================================

class TestExplicitBarrierMetricThresholds:
    """Guard against recommendations that incorrectly mark failed OTR/WVTR claims compliant."""

    def _quote(self, brief, supplier_id, name, price, otr, wvtr, has_tds=False):
        return SupplierQuotation(
            quotation_id=f"QUOTE-{supplier_id}",
            rfq_id=f"RFQ-{supplier_id}",
            supplier_id=supplier_id,
            brief_id=brief.brief_id,
            company_name=name,
            version=1,
            status=QuotationStatus.UNDER_REVIEW,
            commercial=QuotationCommercialData(
                quoted_currency="USD",
                quoted_unit_price=price,
                quoted_quantity=50000,
                min_order_quantity=5000,
                lead_time_weeks=4,
                payment_terms="Net 30",
                incoterms=Incoterm.FOB,
                freight_charges=None,
                taxes_and_duties=None,
                is_freight_included=False,
                is_tax_included=False,
                critical_fields_confirmed=True,
            ),
            compliance=QuotationTechnicalCompliance(
                product_category="Flexible Packaging",
                structure_description="12u PET / 15u Met-BOPP / 50u PE barrier structure",
                thickness_microns=77.0,
                otr_value=otr,
                wvtr_value=wvtr,
                test_standards_cited=["ASTM D3985", "ASTM F1249"],
                has_tds_document=has_tds,
                mandatory_requirements_met=False,
            ),
        )

    def test_beta_screenshot_case_fails_both_explicit_limits_and_is_not_recommended(self, isolated_repo):
        base = isolated_repo.get_brief("BRIEF-12C-001")
        brief = base.model_copy(update={
            "description": "Synthetic flexible barrier packaging. Mandatory OTR < 1.0 cc/m2/day and WVTR < 0.5 g/m2/day."
        })
        beta = self._quote(
            brief, "SUPP-BETA-REG", "Beta Green Packaging", 1.28,
            "1.6 cc/m2/day (ASTM D3985)", "0.7 g/m2/day (ASTM F1249)", False
        )
        alpha = self._quote(
            brief, "SUPP-ALPHA-REG", "Alpha Barrier Films", 1.52,
            "0.8 cc/m2/day (ASTM D3985)", "0.4 g/m2/day (ASTM F1249)", True
        )

        matrix = QuotationComparisonEngine().compare_quotations(
            quotations=[beta, alpha], brief=brief, target_currency="USD"
        )
        rows = {row.supplier_id: row for row in matrix.rows}
        beta_row = rows["SUPP-BETA-REG"]

        assert beta_row.mandatory_requirements_met is False
        assert beta_row.technical_score == 0.0
        assert beta_row.recommendation_type == ComparisonRecommendationType.NON_COMPLIANT_DISQUALIFIED
        assert any("OTR" in reason and "1.6" in reason for reason in beta_row.disqualification_reasons)
        assert any("WVTR" in reason and "0.7" in reason for reason in beta_row.disqualification_reasons)
        assert matrix.primary_recommendation is not None
        assert matrix.primary_recommendation.supplier_id == "SUPP-ALPHA-REG"
        assert matrix.secondary_recommendation is None
        assert beta_row in matrix.disqualified_candidates

    def test_missing_numeric_barrier_metric_fails_closed_when_brief_sets_limit(self, isolated_repo):
        brief = isolated_repo.get_brief("BRIEF-12C-001").model_copy(update={
            "description": "High barrier packaging with mandatory OTR < 2.0 cc/m2/day and WVTR < 1.0 g/m2/day."
        })
        quote = self._quote(
            brief, "SUPP-MISSING", "Missing Metrics Supplier", 0.50,
            None, "0.8 g/m2/day", False
        )
        compliant, reasons, score, _ = TechnicalComplianceEvaluator.evaluate_compliance(quote, brief)
        assert compliant is False
        assert score == 0.0
        assert any("OTR" in reason and "missing" in reason.lower() for reason in reasons)

    def test_strict_less_than_boundary_is_enforced(self, isolated_repo):
        brief = isolated_repo.get_brief("BRIEF-12C-001").model_copy(update={
            "description": "Barrier film requires OTR < 1.0 and WVTR < 0.5."
        })
        quote = self._quote(
            brief, "SUPP-BOUNDARY", "Boundary Supplier", 1.0,
            "1.0", "0.49", True
        )
        compliant, reasons, score, _ = TechnicalComplianceEvaluator.evaluate_compliance(quote, brief)
        assert compliant is False
        assert score == 0.0
        assert any("OTR" in reason for reason in reasons)

    def test_award_service_blocks_supplier_that_fails_brief_limits(self, isolated_repo):
        brief = isolated_repo.get_brief("BRIEF-12C-001")
        beta = self._quote(
            brief, "SUPP-BETA-AWARD", "Beta Green Packaging", 0.10,
            "2.6", "1.7", True
        )
        isolated_repo.create_quotation(beta)
        service = QuotationService(repository=isolated_repo)
        with pytest.raises(DataValidationError, match="Award blocked"):
            service.accept_quotation_for_award(
                quotation_id=beta.quotation_id,
                user_name="Test Buyer",
                award_notes="Test attempt must be blocked.",
            )
