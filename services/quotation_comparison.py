"""
VendorIQ Quotation Comparison & Analysis Engine.

Provides deterministic multi-supplier quotation comparison, currency normalization
with transparent benchmark tracking and buyer overrides, landed cost evaluation with
incomplete-data disclosure, non-compensable technical compliance evaluation,
and explainable deterministic recommendation generation.
"""

from datetime import datetime, timezone, date, timedelta
from typing import Optional, List, Dict, Any, Tuple
import re

from core.models import (
    SourcingBrief,
    SourcingCriteria,
    SupplierQuotation,
    QuotationStatus,
    Incoterm,
    ExchangeRateType,
    ExchangeRateRecord,
    QuotationCommercialData,
    QuotationTechnicalCompliance,
    EvidenceStatus,
    EvidenceCategory,
    ComparisonRecommendationType,
    QuotationComparisonRow,
    QuotationComparisonMatrix,
)
from core.exceptions import DataValidationError


# =============================================================================
# 1. CURRENCY NORMALIZATION SERVICE
# =============================================================================

# Benchmark reference exchange rates against USD baseline (1 USD = rate Target)
DEFAULT_BENCHMARK_RATES: Dict[str, float] = {
    "USD": 1.0,
    "INR": 84.50,
    "EUR": 0.92,
    "GBP": 0.79,
}

BENCHMARK_EFFECTIVE_DATE = "2026-11-20"
BENCHMARK_SOURCE = "Static Reference Benchmark (Not Live Market Rate)"
BENCHMARK_MAX_AGE_DAYS = 60


class CurrencyNormalizationService:
    """
    Manages currency conversion into a buyer-selected comparison currency.
    Enforces transparent rate classification (BENCHMARK vs BUYER_OVERRIDE),
    outdated rate flagging, and explicit refusal on unavailable rates.
    """

    def __init__(self, max_age_days: int = BENCHMARK_MAX_AGE_DAYS):
        self.max_age_days = max_age_days
        self._overrides: Dict[str, ExchangeRateRecord] = {}

    def get_benchmark_rate(self, target_currency: str) -> Optional[ExchangeRateRecord]:
        """Fetch static benchmark rate for a currency relative to USD."""
        curr = target_currency.upper().strip()
        if curr not in DEFAULT_BENCHMARK_RATES:
            return None

        # Check if rate is considered outdated
        eff_date = date.fromisoformat(BENCHMARK_EFFECTIVE_DATE)
        age_days = (date.today() - eff_date).days
        is_outdated = age_days > self.max_age_days

        return ExchangeRateRecord(
            base_currency="USD",
            target_currency=curr,
            rate=DEFAULT_BENCHMARK_RATES[curr],
            source=BENCHMARK_SOURCE,
            effective_date=BENCHMARK_EFFECTIVE_DATE,
            rate_type=ExchangeRateType.BENCHMARK,
            is_outdated=is_outdated,
            is_buyer_override=False,
            notes=f"Reference benchmark rate. Age: {age_days} days."
        )

    def set_buyer_override(
        self,
        base_currency: str,
        target_currency: str,
        rate: float,
        effective_date: str,
        notes: str,
        changed_by: str,
    ) -> ExchangeRateRecord:
        """
        Record a transparent buyer-specified exchange rate override with audit metadata.
        """
        if rate <= 0.0:
            raise DataValidationError(f"Exchange rate must be positive (> 0.0), got {rate}.")

        b_curr = base_currency.upper().strip()
        t_curr = target_currency.upper().strip()
        key = f"{b_curr}:{t_curr}"

        rec = ExchangeRateRecord(
            base_currency=b_curr,
            target_currency=t_curr,
            rate=round(rate, 4),
            source=f"Buyer Override by {changed_by}",
            effective_date=effective_date,
            rate_type=ExchangeRateType.BUYER_OVERRIDE,
            is_outdated=False,
            is_buyer_override=True,
            notes=notes.strip() if notes else "Custom buyer exchange rate override.",
        )
        self._overrides[key] = rec
        return rec

    def get_rate_record(
        self,
        from_currency: str,
        to_currency: str,
        custom_override: Optional[ExchangeRateRecord] = None,
    ) -> ExchangeRateRecord:
        """
        Resolve exchange rate record between two currencies.
        Precedence: custom_override > registered buyer override > benchmark rate.
        """
        f_curr = from_currency.upper().strip()
        t_curr = to_currency.upper().strip()

        if f_curr == t_curr:
            return ExchangeRateRecord(
                base_currency=f_curr,
                target_currency=t_curr,
                rate=1.0,
                source="Identity (Same Currency)",
                effective_date=str(date.today()),
                rate_type=ExchangeRateType.FIXED_ESTIMATE,
                is_outdated=False,
                is_buyer_override=False,
                notes="Zero conversion required."
            )

        # 1. Custom override passed explicitly
        if custom_override and custom_override.rate and custom_override.rate > 0:
            return custom_override

        # 2. Registered buyer override
        override_key = f"{f_curr}:{t_curr}"
        if override_key in self._overrides:
            return self._overrides[override_key]

        # Inverted override key (e.g. registered USD:INR, querying INR:USD)
        inv_key = f"{t_curr}:{f_curr}"
        if inv_key in self._overrides:
            inv = self._overrides[inv_key]
            if inv.rate and inv.rate > 0:
                return ExchangeRateRecord(
                    base_currency=f_curr,
                    target_currency=t_curr,
                    rate=round(1.0 / inv.rate, 6),
                    source=f"Inverted {inv.source}",
                    effective_date=inv.effective_date,
                    rate_type=ExchangeRateType.BUYER_OVERRIDE,
                    is_outdated=inv.is_outdated,
                    is_buyer_override=True,
                    notes=f"Calculated as 1 / {inv.rate} from registered override."
                )

        # 3. Benchmark rates (via USD pivot)
        rec_from = self.get_benchmark_rate(f_curr)
        rec_to = self.get_benchmark_rate(t_curr)

        if rec_from and rec_to and rec_from.rate and rec_to.rate:
            # 1 USD = rec_from.rate FROM, 1 USD = rec_to.rate TO
            # Rate FROM to TO = (1 / rec_from.rate) * rec_to.rate = rec_to.rate / rec_from.rate
            effective_rate = round(rec_to.rate / rec_from.rate, 6)
            is_outdated = rec_from.is_outdated or rec_to.is_outdated
            return ExchangeRateRecord(
                base_currency=f_curr,
                target_currency=t_curr,
                rate=effective_rate,
                source=BENCHMARK_SOURCE,
                effective_date=BENCHMARK_EFFECTIVE_DATE,
                rate_type=ExchangeRateType.BENCHMARK,
                is_outdated=is_outdated,
                is_buyer_override=False,
                notes=f"Derived from benchmark rates: 1 USD = {rec_from.rate} {f_curr}, 1 USD = {rec_to.rate} {t_curr}."
            )

        # 4. Unavailable
        return ExchangeRateRecord(
            base_currency=f_curr,
            target_currency=t_curr,
            rate=None,
            source="No Benchmark Available",
            effective_date=str(date.today()),
            rate_type=ExchangeRateType.UNAVAILABLE,
            is_outdated=False,
            is_buyer_override=False,
            notes=f"Exchange rate for {f_curr} to {t_curr} is not configured."
        )

    def convert_amount(
        self,
        amount: float,
        from_currency: str,
        to_currency: str,
        custom_override: Optional[ExchangeRateRecord] = None,
    ) -> Tuple[Optional[float], ExchangeRateRecord, str]:
        """
        Convert monetary amount to target currency.
        Returns (converted_amount, rate_record, explanation).
        If rate is UNAVAILABLE, returns (None, rate_record, explanation).
        """
        rec = self.get_rate_record(from_currency, to_currency, custom_override)
        f_curr = from_currency.upper().strip()
        t_curr = to_currency.upper().strip()

        if rec.rate_type == ExchangeRateType.UNAVAILABLE or rec.rate is None or rec.rate <= 0:
            expl = (
                f"Currency conversion failed: Exchange rate from {f_curr} to {t_curr} is unavailable. "
                f"Quotation cannot be normalized on price without an explicit buyer FX override."
            )
            return None, rec, expl

        # High-precision calculation avoiding inversion rounding artifacts
        if custom_override and custom_override.rate and custom_override.rate > 0:
            converted = round(amount * custom_override.rate, 4)
        elif f"{f_curr}:{t_curr}" in self._overrides:
            converted = round(amount * self._overrides[f"{f_curr}:{t_curr}"].rate, 4)
        elif f"{t_curr}:{f_curr}" in self._overrides:
            converted = round(amount / self._overrides[f"{t_curr}:{f_curr}"].rate, 4)
        else:
            rec_from = self.get_benchmark_rate(f_curr)
            rec_to = self.get_benchmark_rate(t_curr)
            if rec_from and rec_to and rec_from.rate and rec_to.rate:
                converted = round((amount / rec_from.rate) * rec_to.rate, 4)
            else:
                converted = round(amount * rec.rate, 4)

        outdated_warning = " [WARNING: Rate is older than 60 days]" if rec.is_outdated else ""
        expl = (
            f"Converted {amount:.4f} {f_curr} to {converted:.4f} {t_curr} at rate {rec.rate} "
            f"({rec.source}, Date: {rec.effective_date}){outdated_warning}."
        )
        return converted, rec, expl


# =============================================================================
# 2. TECHNICAL COMPLIANCE EVALUATOR
# =============================================================================

class TechnicalComplianceEvaluator:
    """
    Evaluates quotation technical compliance against sourcing brief requirements.
    Enforces non-compensability of mandatory technical specifications:
    a lower price must NEVER offset a mandatory technical failure.
    """

    @staticmethod
    def evaluate_compliance(
        quotation: SupplierQuotation,
        brief: SourcingBrief,
    ) -> Tuple[bool, List[str], float, str]:
        """
        Evaluate quotation compliance against brief requirements.
        Returns (is_compliant, disqualification_reasons, technical_score, notes).
        """
        comp = quotation.compliance
        disqualifications: List[str] = []
        notes_parts: List[str] = []
        brief_desc_lower = (brief.description or "").lower()
        brief_title_lower = (brief.title or "").lower()

        # Check if brief mandates barrier film packaging
        is_barrier_mandated = (
            "barrier" in brief_desc_lower
            or "barrier" in brief_title_lower
            or "evoh" in brief_desc_lower
            or "alu foil" in brief_desc_lower
            or "high barrier" in brief_desc_lower
            or any("barrier" in ind.lower() for ind in brief.criteria.target_industries)
        )

        # 1. Mandatory Barrier Film Requirement
        if is_barrier_mandated:
            struct_lower = (comp.structure_description or "").lower()
            has_barrier_tokens = any(
                tok in struct_lower
                for tok in ("barrier", "evoh", "met-bopp", "alu foil", "foil", "nylon", "bopa", "alox", "siox")
            ) or (comp.otr_value is not None)

            # Detect explicit non-barrier single layer structures
            is_single_layer_non_barrier = (
                "single-layer" in struct_lower or "single layer" in struct_lower
            ) and not has_barrier_tokens

            if is_single_layer_non_barrier or not has_barrier_tokens:
                disqualifications.append(
                    "Mandatory Barrier Film Failure: Sourcing brief mandates high-barrier packaging structure, "
                    f"but quotation offers '{comp.structure_description}' without verified barrier layers."
                )

        # 2. Inherit existing disqualification reasons from quotation parsing
        if comp.disqualification_reasons:
            for r in comp.disqualification_reasons:
                if r not in disqualifications:
                    disqualifications.append(r)

        # 3. Check required certifications from brief criteria
        offered_certs = [c.upper().strip() for c in comp.certifications_offered]
        for req_cert in brief.criteria.required_certifications:
            req_clean = req_cert.upper().strip()
            if not any(req_clean in off or off in req_clean for off in offered_certs):
                notes_parts.append(f"Missing required brief certification: '{req_cert}'.")

        # 4. Mandatory compliance determination (Non-Compensable Gate)
        is_compliant = len(disqualifications) == 0

        # 5. Technical score calculation (0.0 to 100.0)
        # Non-compensability rule: If mandatory requirements fail, score is strictly 0.0
        if not is_compliant:
            technical_score = 0.0
            notes_parts.append(
                "[DISQUALIFIED]: Mandatory technical failure. Low commercial price cannot compensate for technical non-compliance."
            )
        else:
            base_score = 70.0
            # Bonus for quantified barrier metrics
            if comp.otr_value:
                base_score += 10.0
            if comp.wvtr_value:
                base_score += 5.0
            # Bonus for cited ASTM/ISO standards
            if comp.test_standards_cited:
                base_score += min(10.0, len(comp.test_standards_cited) * 5.0)
            # Bonus for documentary TDS
            if comp.has_tds_document:
                base_score += 5.0

            technical_score = min(100.0, base_score)

        explanation = " | ".join(notes_parts) if notes_parts else "Technical specifications fully compliant with brief."
        return is_compliant, disqualifications, technical_score, explanation


# =============================================================================
# 3. QUOTATION COMPARISON & RECOMMENDATION ENGINE
# =============================================================================

class QuotationComparisonEngine:
    """
    Orchestrates currency normalization, landed cost analysis, technical evaluation,
    side-by-side matrix generation, and deterministic recommendation synthesis.
    """

    def __init__(
        self,
        fx_service: Optional[CurrencyNormalizationService] = None,
        evaluator: Optional[TechnicalComplianceEvaluator] = None,
    ):
        self.fx_service = fx_service or CurrencyNormalizationService()
        self.evaluator = evaluator or TechnicalComplianceEvaluator()

    def compare_quotations(
        self,
        quotations: List[SupplierQuotation],
        brief: SourcingBrief,
        target_currency: str = "USD",
        buyer_fx_overrides: Optional[Dict[str, ExchangeRateRecord]] = None,
    ) -> QuotationComparisonMatrix:
        """
        Execute comprehensive multi-supplier comparison and generate deterministic recommendations.
        """
        t_curr = target_currency.upper().strip()
        rows: List[QuotationComparisonRow] = []
        fx_records_used: Dict[str, ExchangeRateRecord] = {}

        if not quotations:
            return QuotationComparisonMatrix(
                brief_id=brief.brief_id,
                target_currency=t_curr,
                rows=[],
                fx_rates_used={},
                are_all_landed_costs_complete=False,
                ranking_metric="NONE",
                comparison_notes="No supplier quotations available for comparison under this brief.",
            )

        # Phase 1: Normalize and evaluate each individual quotation
        for quote in quotations:
            comm = quote.commercial
            comp = quote.compliance
            q_curr = comm.quoted_currency.upper().strip()

            # 1. Currency Normalization for Base Unit Price
            custom_fx = None
            if buyer_fx_overrides:
                custom_fx = buyer_fx_overrides.get(q_curr) or buyer_fx_overrides.get(f"{q_curr}:{t_curr}")

            norm_base_price, fx_rec, fx_expl = self.fx_service.convert_amount(
                amount=comm.quoted_unit_price,
                from_currency=q_curr,
                to_currency=t_curr,
                custom_override=custom_fx,
            )
            fx_records_used[q_curr] = fx_rec
            is_price_comparable = (norm_base_price is not None)

            # 2. Landed Cost Calculation
            # Calculate landed cost strictly using genuine commercial inputs and fx_rec
            comm.calculate_landed_cost(fx_record=fx_rec, target_currency=t_curr)
            norm_landed_cost = comm.estimated_landed_unit_cost_target_curr
            are_costs_complete = comm.are_cost_inputs_complete
            landed_expl = comm.landed_cost_explanation

            # 3. Technical Compliance Evaluation
            is_compliant, disquals, tech_score, tech_notes = self.evaluator.evaluate_compliance(quote, brief)

            # 4. Credibility & Evidence Score
            cred_score = 50.0
            # TDS attached provides verified tier
            if comp.has_tds_document:
                cred_score += 30.0
            # ISO/BRCGS certifications
            if comp.certifications_offered:
                cred_score += min(20.0, len(comp.certifications_offered) * 5.0)
            cred_score = min(100.0, cred_score)

            # Build initial comparison row
            row = QuotationComparisonRow(
                quotation_id=quote.quotation_id,
                rfq_id=quote.rfq_id,
                supplier_id=quote.supplier_id,
                company_name=quote.company_name,
                version=quote.version,
                status=quote.status,
                quoted_currency=q_curr,
                quoted_unit_price=comm.quoted_unit_price,
                quoted_quantity=comm.quoted_quantity,
                min_order_quantity=comm.min_order_quantity,
                lead_time_weeks=comm.lead_time_weeks,
                payment_terms=comm.payment_terms,
                incoterms=comm.incoterms,
                is_freight_included=comm.is_freight_included,
                is_tax_included=comm.is_tax_included,
                freight_charges=comm.freight_charges,
                taxes_and_duties=comm.taxes_and_duties,
                additional_charges=comm.additional_charges,
                validity_days=comm.validity_days,
                validity_end_date=comm.validity_end_date,
                target_currency=t_curr,
                normalized_base_unit_price=norm_base_price,
                normalized_landed_unit_cost=norm_landed_cost,
                is_price_comparable=is_price_comparable,
                are_cost_inputs_complete=are_costs_complete,
                landed_cost_explanation=landed_expl,
                fx_rate_applied=fx_rec,
                product_category=comp.product_category,
                structure_description=comp.structure_description,
                thickness_microns=comp.thickness_microns,
                otr_value=comp.otr_value,
                wvtr_value=comp.wvtr_value,
                test_standards_cited=comp.test_standards_cited,
                certifications_offered=comp.certifications_offered,
                has_tds_document=comp.has_tds_document,
                evidence_status_map=comp.evidence_status_map,
                mandatory_requirements_met=is_compliant,
                disqualification_reasons=disquals,
                commercial_score=0.0,  # Computed relative to cohort in Phase 2
                technical_score=tech_score,
                credibility_score=cred_score,
                overall_comparison_score=0.0,
                recommendation_type=ComparisonRecommendationType.UNCONFIRMED_DRAFT,
                recommendation_notes=tech_notes,
            )
            rows.append(row)

        # Phase 2: Cohort Commercial Scoring and Ranking Metric Determination
        # Separate compliant vs disqualified vs unconfirmed
        compliant_rows = [r for r in rows if r.mandatory_requirements_met and r.is_price_comparable]
        disqualified_rows = [r for r in rows if not r.mandatory_requirements_met]
        incomparable_rows = [r for r in rows if not r.is_price_comparable]

        # Determine if all compliant candidates have complete landed costs
        all_compliant_landed_complete = len(compliant_rows) > 0 and all(r.are_cost_inputs_complete for r in compliant_rows)
        ranking_metric = "NORMALIZED_LANDED_COST" if all_compliant_landed_complete else "NORMALIZED_BASE_PRICE"

        # Compute relative commercial score for compliant candidates
        if compliant_rows:
            # Find best (lowest) price for relative price scoring
            best_base_price = min(r.normalized_base_unit_price for r in compliant_rows if r.normalized_base_unit_price is not None)
            best_lead_time = min(r.lead_time_weeks for r in compliant_rows)
            best_moq = min(r.min_order_quantity for r in compliant_rows)

            for r in compliant_rows:
                # Price component (50% of commercial score)
                # Score = 50 * (best_price / candidate_price)
                price_score = 50.0
                if r.normalized_base_unit_price and r.normalized_base_unit_price > 0:
                    price_score = 50.0 * (best_base_price / r.normalized_base_unit_price)

                # Lead time component (20% of commercial score)
                lead_score = 20.0 * (best_lead_time / max(r.lead_time_weeks, 1))

                # MOQ component (15% of commercial score)
                moq_score = 15.0 * (best_moq / max(r.min_order_quantity, 1))

                # Payment terms component (15% of commercial score)
                term_score = 10.0
                p_lower = r.payment_terms.lower()
                if "net 60" in p_lower or "net 45" in p_lower:
                    term_score = 15.0
                elif "net 30" in p_lower:
                    term_score = 12.0
                elif "advance" in p_lower:
                    term_score = 5.0

                r.commercial_score = round(price_score + lead_score + moq_score + term_score, 1)

                # Overall comparison score (40% commercial, 35% technical, 25% credibility)
                r.overall_comparison_score = round(
                    0.40 * r.commercial_score + 0.35 * r.technical_score + 0.25 * r.credibility_score,
                    1
                )

        # Mark disqualified rows
        for r in disqualified_rows:
            r.commercial_score = 0.0
            r.technical_score = 0.0
            r.overall_comparison_score = 0.0
            r.recommendation_type = ComparisonRecommendationType.NON_COMPLIANT_DISQUALIFIED
            r.recommendation_notes = f"[DISQUALIFIED]: {'; '.join(r.disqualification_reasons)}"

        # Mark incomparable rows
        for r in incomparable_rows:
            r.recommendation_notes += " [EXCLUDED FROM PRICE RANKING: Unavailable FX rate]."

        # Phase 3: Deterministic Candidate Ranking
        # Sort compliant candidates deterministically
        if ranking_metric == "NORMALIZED_LANDED_COST":
            # Primary sort by landed unit cost, secondary by overall score
            compliant_rows.sort(
                key=lambda x: (
                    x.normalized_landed_unit_cost if x.normalized_landed_unit_cost is not None else float("inf"),
                    -x.overall_comparison_score
                )
            )
        else:
            # Primary sort by normalized base unit price, secondary by overall score
            compliant_rows.sort(
                key=lambda x: (
                    x.normalized_base_unit_price if x.normalized_base_unit_price is not None else float("inf"),
                    -x.overall_comparison_score
                )
            )

        # Phase 4: Assign Recommendation Categories
        primary_rec = None
        secondary_rec = None

        if compliant_rows:
            primary_rec = compliant_rows[0]
            if not primary_rec.are_cost_inputs_complete:
                primary_rec.recommendation_type = ComparisonRecommendationType.COMMERCIALLY_VIABLE_COST_INCOMPLETE
                primary_rec.recommendation_notes = (
                    f"[RECOMMENDED PRIMARY - COST INCOMPLETE]: Lowest normalized quoted price "
                    f"({primary_rec.normalized_base_unit_price:.4f} {t_curr}/unit) and complete technical compliance. "
                    f"Notice: Landed cost inputs (freight/duties) are incomplete and must be verified prior to contract."
                )
            else:
                primary_rec.recommendation_type = ComparisonRecommendationType.RECOMMENDED_PRIMARY
                primary_rec.recommendation_notes = (
                    f"[RECOMMENDED PRIMARY]: Best overall commercial value "
                    f"(Landed Cost: {primary_rec.normalized_landed_unit_cost:.4f} {t_curr}/unit) "
                    f"with verified technical compliance and authentic documentary TDS."
                )

            if len(compliant_rows) > 1:
                secondary_rec = compliant_rows[1]
                secondary_rec.recommendation_type = ComparisonRecommendationType.RECOMMENDED_SECONDARY
                secondary_rec.recommendation_notes = (
                    f"[RECOMMENDED SECONDARY - RUNNER UP]: Strong alternative candidate "
                    f"(Quoted: {secondary_rec.normalized_base_unit_price:.4f} {t_curr}/unit, Lead Time: {secondary_rec.lead_time_weeks} weeks)."
                )

            for other_row in compliant_rows[2:]:
                if not other_row.are_cost_inputs_complete:
                    other_row.recommendation_type = ComparisonRecommendationType.COMMERCIALLY_VIABLE_COST_INCOMPLETE
                else:
                    other_row.recommendation_type = ComparisonRecommendationType.RECOMMENDED_SECONDARY
                other_row.recommendation_notes = (
                    f"Compliant alternative candidate. Ranked #{compliant_rows.index(other_row) + 1} on commercial evaluation."
                )

        # Assemble summary comparison notes
        notes_list = []
        if all_compliant_landed_complete:
            notes_list.append("All compliant candidates have complete logistics/duty inputs. Ranking based on Estimated Landed Unit Cost.")
        else:
            notes_list.append(
                "Landed cost inputs are incomplete for one or more candidates (missing freight/tariffs). "
                "Ranking based on Normalized Base Unit Price. Logistics costs must be confirmed manually."
            )
        if disqualified_rows:
            notes_list.append(f"{len(disqualified_rows)} candidate(s) disqualified due to mandatory technical non-compliance.")
        if incomparable_rows:
            notes_list.append(f"{len(incomparable_rows)} candidate(s) could not be normalized due to unconfigured currency exchange rates.")

        comparison_notes = " ".join(notes_list)

        return QuotationComparisonMatrix(
            brief_id=brief.brief_id,
            target_currency=t_curr,
            rows=rows,
            fx_rates_used=fx_records_used,
            are_all_landed_costs_complete=all_compliant_landed_complete,
            ranking_metric=ranking_metric,
            comparison_notes=comparison_notes,
            primary_recommendation=primary_rec,
            secondary_recommendation=secondary_rec,
            disqualified_candidates=disqualified_rows,
        )
