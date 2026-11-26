"""
VendorIQ Supplier Evaluation View.

Displays ranked supplier scorecards across all five deterministic scoring dimensions:
- Industry Relevance: 35%
- Geographic Fit: 25%
- Company Size Fit: 20%
- Business Signals: 10%
- Data Completeness: 10%

Provides fine-grained factor score explanations, neutral baseline labels for missing data,
and clear evidence boundaries.
"""

import streamlit as st
import pandas as pd
from typing import List

from core.models import SupplierScore, ScoringDimension
from ui.components import (
    get_provenance_badge_html,
    render_view_header,
    render_score_legend,
    render_next_step_guidance,
)


def render_evaluation_view(evaluations: List[SupplierScore]):
    """Render ranked scorecards with factor breakdowns and evidence reasoning."""
    render_view_header(
        title="🏆 Ranked Supplier Evaluation Scorecards",
        subtitle="Deterministic evaluation against Packaging + India sourcing criteria. Missing evidence is assigned neutral baselines (50.0) without penalty.",
        stage_key="evaluation",
    )

    if not evaluations:
        st.info("No evaluations found. Please ensure suppliers are evaluated against an active brief.")
        return

    # Banner reminding users that these are candidate evaluations
    st.info(
        "ℹ️ **Evaluation Status Notice**: All vendors are classified as **EVALUATED CANDIDATES**. "
        "Scores evaluate self-reported Crustdata attributes. No vendor is approved, certified, or guaranteed without an on-site audit."
    )

    # Sort filter
    col_sort, col_filter = st.columns([2, 1])
    with col_sort:
        search_kw = st.text_input("Filter Ranked Candidates", placeholder="Search evaluated vendor name...").strip().lower()

    # Deterministic sort: total score descending, then company name ascending for ties
    sorted_evals = sorted(evaluations, key=lambda sc: (-sc.total_score, sc.company_name))
    filtered_evals = [e for e in sorted_evals if search_kw in e.company_name.lower()] if search_kw else sorted_evals

    for rank, sc in enumerate(filtered_evals, 1):
        f = sc.factor_scores
        total = sc.total_score

        # Card container
        with st.container():
            col_rank, col_name, col_score, col_comp = st.columns([1, 4, 2, 2])

            with col_rank:
                st.markdown(f"<h2 style='color: #2563EB; margin: 0;'>#{rank}</h2>", unsafe_allow_html=True)

            with col_name:
                st.markdown(f"### {sc.company_name}")
                st.caption(f"ID: `{sc.supplier_id}` &nbsp;|&nbsp; Status: `EVALUATED_CANDIDATE`")

            with col_score:
                st.metric("Total Score", f"{total:.2f}/100", f"Weights: 35/25/20/10/10")

            with col_comp:
                st.metric("Data Completeness", f"{sc.data_completeness_percentage:.0f}%", "Profile Health")

            # 5-factor progress / score strip
            col_f1, col_f2, col_f3, col_f4, col_f5 = st.columns(5)
            with col_f1:
                f_ind = f[ScoringDimension.INDUSTRY_RELEVANCE]
                st.metric("Industry (35%)", f"{f_ind.score:.0f}", f"{f_ind.weighted_score:.1f} pts", delta_color="off")
            with col_f2:
                f_geo = f[ScoringDimension.GEOGRAPHIC_FIT]
                st.metric("Geography (25%)", f"{f_geo.score:.0f}", f"{f_geo.weighted_score:.1f} pts", delta_color="off")
            with col_f3:
                f_size = f[ScoringDimension.COMPANY_SIZE_FIT]
                st.metric("Size Fit (20%)", f"{f_size.score:.0f}", f"{f_size.weighted_score:.1f} pts", delta_color="off")
            with col_f4:
                f_sig = f[ScoringDimension.BUSINESS_SIGNALS]
                st.metric("Signals (10%)", f"{f_sig.score:.0f}", f"{f_sig.weighted_score:.1f} pts", delta_color="off")
            with col_f5:
                f_comp = f[ScoringDimension.DATA_COMPLETENESS]
                st.metric("Complete (10%)", f"{f_comp.score:.0f}", f"{f_comp.weighted_score:.1f} pts", delta_color="off")

            # Factor Reasoning Expander
            with st.expander(f"🔍 View Detailed Factor Breakdown & Reasoning for {sc.company_name}"):
                for dim, factor in f.items():
                    dim_label = dim.value.replace("_", " ").title()
                    missing_badge = "⚠️ (Neutral Baseline: Data Not Provided)" if factor.is_missing_data else "✅ (Evidence Evaluated)"
                    st.markdown(f"**{dim_label} (Weight: {factor.weight * 100:.0f}%) — Score: {factor.score:.1f}/100** &nbsp; {missing_badge}")
                    st.write(f"- *Reasoning*: {factor.reasoning}")
                    st.write(f"- *Evidence Source*: {', '.join(factor.evidence_sources)}")
                    st.write(f"- *Confidence Level*: {factor.confidence * 100:.0f}%")
                    st.markdown("---")

                st.caption(f"**Audit Summary**: {sc.summary}")

            st.markdown("<hr style='margin: 12px 0;'/>", unsafe_allow_html=True)

    render_score_legend()
    render_next_step_guidance(
        next_view_name="🛡️ Evidence & Trust",
        guidance_text="Verify underlying documentary proof, audit self-reported claims, and review the actionable missing evidence checklist before issuing RFQs.",
    )
