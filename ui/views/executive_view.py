"""
VendorIQ Executive Dashboard View.

Provides high-level KPI cards, score distributions, data quality audit metrics,
and live vs synthetic record breakdowns.
"""

import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from typing import List

from core.models import SupplierProfile, SupplierScore, SourcingBrief
from services.data_quality_auditor import DataQualityAuditReport
from ui.components import (
    render_domain_mismatch_warning,
    get_provenance_badge_html,
    render_workflow_stepper,
    render_next_step_guidance,
)


def render_executive_view(
    brief: SourcingBrief,
    suppliers: List[SupplierProfile],
    evaluations: List[SupplierScore],
    audit_report: DataQualityAuditReport,
):
    """Render executive dashboard metrics and overview charts."""
    st.markdown("## 📊 Executive Sourcing Dashboard")
    st.caption(f"Active Sourcing Project: **{brief.title}** ({brief.brief_id})")
    render_workflow_stepper("brief")

    # 1. Top KPI Metric Cards
    total_suppliers = len(suppliers)
    evaluated_count = len(evaluations)
    avg_score = (
        sum(e.total_score for e in evaluations) / evaluated_count
        if evaluated_count > 0
        else 0.0
    )
    max_score = max((e.total_score for e in evaluations), default=0.0)
    live_count = sum(1 for s in suppliers if not s.is_synthetic)
    synthetic_count = sum(1 for s in suppliers if s.is_synthetic)
    warning_count = audit_report.suspicious_mismatches_count

    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("Discovered Suppliers", total_suppliers, f"{live_count} Live / {synthetic_count} Synth")
    with col2:
        st.metric("Evaluated Candidates", evaluated_count, "100% Deterministic")
    with col3:
        st.metric("Average Score", f"{avg_score:.2f}/100", f"Max: {max_score:.1f}")
    with col4:
        st.metric("Data Quality Alerts", warning_count, "Domain Review Flags")
    with col5:
        st.metric("Crustdata Search Usage", "0.6 Credits", "Single Live Search ($0.00 USD)")

    st.markdown("---")

    # 2. Charts Row: Score Distribution & Data Completeness
    chart_col1, chart_col2 = st.columns([3, 2])

    with chart_col1:
        st.subheader("Evaluated Candidate Score Ranking")
        if evaluations:
            df_evals = pd.DataFrame([
                {
                    "Company Name": e.company_name,
                    "Total Score": e.total_score,
                    "Completeness": e.data_completeness_percentage,
                }
                for e in evaluations
            ])

            # Color by score tier
            df_evals["Tier"] = df_evals["Total Score"].apply(
                lambda s: "Top Tier (>=75)" if s >= 75 else ("Mid Tier (70-74)" if s >= 70 else "Low Tier (<70)")
            )
            color_map = {
                "Top Tier (>=75)": "#10B981",
                "Mid Tier (70-74)": "#3B82F6",
                "Low Tier (<70)": "#F59E0B",
            }

            fig = px.bar(
                df_evals,
                x="Total Score",
                y="Company Name",
                orientation="h",
                color="Tier",
                color_discrete_map=color_map,
                text="Total Score",
                height=580,
            )
            fig.update_layout(
                yaxis={"categoryorder": "total ascending", "tickfont": {"size": 12}},
                xaxis={"range": [0, 105], "title": "Deterministic Total Score (0-100)"},
                margin=dict(l=230, r=40, t=30, b=40),
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            )
            fig.update_traces(texttemplate="%{text:.1f}", textposition="outside")
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("No evaluations computed yet.")

    with chart_col2:
        st.subheader("Dataset Composition & Audit Health")
        # Donut Chart of Data Origin
        labels = ["Live Crustdata Discovery", "Synthetic Benchmarks"]
        values = [live_count, synthetic_count]
        fig_donut = go.Figure(
            data=[
                go.Pie(
                    labels=labels,
                    values=values,
                    hole=0.6,
                    marker=dict(colors=["#2563EB", "#9CA3AF"]),
                )
            ]
        )
        fig_donut.update_layout(
            margin=dict(l=10, r=10, t=10, b=10),
            height=240,
            showlegend=True,
        )
        st.plotly_chart(fig_donut, width="stretch")

        st.markdown("#### Quality Audit Highlights")
        st.write(f"- **Unique Company Names**: {audit_report.unique_company_names} / {audit_report.total_records}")
        st.write(f"- **Unique Domains**: {audit_report.unique_domains} / {audit_report.total_records}")
        st.write(f"- **Duplicate Entities**: {len(audit_report.duplicate_domains)} detected")
        st.write(f"- **Missing Descriptions**: {audit_report.missing_fields_counts.get('description', 0)}")
        st.write(f"- **Missing Headcount Ranges**: {audit_report.missing_fields_counts.get('employee_range', 0)}")
        st.write(f"- **Missing City Data**: {audit_report.missing_fields_counts.get('headquarters_city', 0)} (Discovery tier limit)")
        st.write(f"- **Crustdata Search Usage**: `0.6 API Credits` ($0.00 USD direct cost)")
        st.write(f"- **Dashboard Execution**: `0 Credits` (Local SQLite read mode)")

    # 3. Data Quality Alerts Section
    if audit_report.flagged_suppliers_for_review:
        st.markdown("### ⚠️ Data Quality Manual Review Alerts")
        st.info("The auditor identified domain stems that share no tokens or acronyms with company names. These require manual confirmation before sending RFQs.")
        for item in audit_report.flagged_suppliers_for_review:
            render_domain_mismatch_warning(
                company_name=item["company_name"],
                domain=item["domain"],
                reason=item["reason"],
            )

    st.markdown("---")
    render_next_step_guidance(
        next_view_name="📋 Sourcing Brief",
        guidance_text="Review the project scope, criteria definitions, and weighting configurations for the active brief.",
    )
