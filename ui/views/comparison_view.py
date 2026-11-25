"""
VendorIQ Supplier Comparison View.

Allows buyers to select up to five supplier candidates and compare them
side-by-side using multi-dimensional radar/bar charts, attribute comparison tables,
and domain review alerts.
"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from typing import List, Dict

from core.models import SupplierProfile, SupplierScore, ScoringDimension
from services.data_quality_auditor import DataQualityAuditReport
from ui.components import render_domain_mismatch_warning, render_score_legend


def render_comparison_view(
    suppliers: List[SupplierProfile],
    evaluations: List[SupplierScore],
    audit_report: DataQualityAuditReport,
):
    """Render side-by-side comparison for up to 5 suppliers."""
    st.markdown("## ⚖️ Supplier Multi-Candidate Comparison")
    st.caption("Select up to 5 candidate suppliers to evaluate factor strengths, profile completeness, and data review flags.")

    if not suppliers or not evaluations:
        st.info("Suppliers or evaluations not available for comparison.")
        return

    # Map supplier_id to objects for quick lookup
    supp_map: Dict[str, SupplierProfile] = {s.supplier_id: s for s in suppliers}
    eval_map: Dict[str, SupplierScore] = {e.supplier_id: e for e in evaluations}

    # Deterministically sort candidates by total score descending, then company name
    sorted_evals = sorted(evaluations, key=lambda sc: (-sc.total_score, sc.company_name))
    default_candidates = [e.company_name for e in sorted_evals[:3]]
    all_names = [e.company_name for e in sorted_evals]

    selected_names = st.multiselect(
        "Select up to 5 Suppliers to Compare",
        options=all_names,
        default=default_candidates,
        max_selections=5,
    )

    if not selected_names:
        st.warning("Please select at least 1 supplier candidate to view comparison.")
        return

    # Resolve selected candidates in sorted rank order
    selected_evals = [e for e in sorted_evals if e.company_name in selected_names]
    selected_supps = [supp_map[e.supplier_id] for e in selected_evals if e.supplier_id in supp_map]

    # 1. Radar Chart Comparison
    st.subheader("Multi-Dimensional Scoring Radar")

    categories = [
        "Industry Relevance (35%)",
        "Geographic Fit (25%)",
        "Company Size Fit (20%)",
        "Business Signals (10%)",
        "Data Completeness (10%)",
    ]

    fig_radar = go.Figure()
    colors = ["#2563EB", "#10B981", "#F59E0B", "#8B5CF6", "#EC4899"]

    for idx, sc in enumerate(selected_evals):
        f = sc.factor_scores
        r_values = [
            f[ScoringDimension.INDUSTRY_RELEVANCE].score,
            f[ScoringDimension.GEOGRAPHIC_FIT].score,
            f[ScoringDimension.COMPANY_SIZE_FIT].score,
            f[ScoringDimension.BUSINESS_SIGNALS].score,
            f[ScoringDimension.DATA_COMPLETENESS].score,
        ]
        # Close radar loop
        r_values.append(r_values[0])
        theta_cats = categories + [categories[0]]

        fig_radar.add_trace(
            go.Scatterpolar(
                r=r_values,
                theta=theta_cats,
                fill="toself",
                name=sc.company_name,
                line_color=colors[idx % len(colors)],
                opacity=0.6,
            )
        )

    fig_radar.update_layout(
        polar=dict(
            radialaxis=dict(
                visible=True,
                range=[0, 100],
            )
        ),
        showlegend=True,
        height=450,
        margin=dict(l=40, r=40, t=20, b=20),
    )
    st.plotly_chart(fig_radar, width="stretch")

    # 2. Side-by-Side Factor Score Matrix
    st.subheader("Side-by-Side Scoring Breakdown")
    comparison_rows = []
    for sc in selected_evals:
        s = supp_map.get(sc.supplier_id)
        f = sc.factor_scores
        comparison_rows.append({
            "Supplier": sc.company_name,
            "Domain": s.domain if s else "N/A",
            "Total Score": f"{sc.total_score:.2f}",
            "Industry (35%)": f"{f[ScoringDimension.INDUSTRY_RELEVANCE].score:.0f}",
            "Geographic (25%)": f"{f[ScoringDimension.GEOGRAPHIC_FIT].score:.0f}",
            "Size Fit (20%)": f"{f[ScoringDimension.COMPANY_SIZE_FIT].score:.0f}",
            "Signals (10%)": f"{f[ScoringDimension.BUSINESS_SIGNALS].score:.0f}",
            "Completeness (10%)": f"{f[ScoringDimension.DATA_COMPLETENESS].score:.0f}%",
            "Headcount Range": s.employee_range if s else "N/A",
        })

    df_comp = pd.DataFrame(comparison_rows)
    st.dataframe(df_comp, width="stretch", hide_index=True)

    # 3. Missing Information & Domain Flags for Selected Candidates
    st.subheader("Missing Attributes & Review Flags for Selected Candidates")

    flagged_ids = {item["supplier_id"]: item for item in audit_report.flagged_suppliers_for_review}

    for s in selected_supps:
        sc = eval_map.get(s.supplier_id)
        col_name, col_status = st.columns([3, 1])
        with col_name:
            st.markdown(f"#### {s.company_name} (`{s.domain}`)")
        with col_status:
            if s.supplier_id in flagged_ids:
                st.error("⚠️ Review Flag Active")
            else:
                st.success("✅ Domain Matches Brand")

        # Check domain mismatch
        if s.supplier_id in flagged_ids:
            render_domain_mismatch_warning(
                company_name=s.company_name,
                domain=s.domain,
                reason=flagged_ids[s.supplier_id]["reason"],
            )

        # Highlight missing attributes
        missing_attrs = []
        if not s.description:
            missing_attrs.append("Company Overview Description")
        if not s.employee_range and not s.employee_count:
            missing_attrs.append("Headcount / Employee Range")
        if not s.headquarters_city:
            missing_attrs.append("City-level Facility Address (Basic Tier Limit)")
        if not s.certifications:
            missing_attrs.append("Verified Regulatory Certifications (ISO/BRC)")
        if not s.annual_revenue_usd:
            missing_attrs.append("Audited Annual Revenue")

        if missing_attrs:
            st.warning(f"**Missing Data Points (Assigned Neutral Baselines)**: {', '.join(missing_attrs)}")
        else:
            st.info("All basic profile attributes are populated.")

        st.markdown("---")

    render_score_legend()
