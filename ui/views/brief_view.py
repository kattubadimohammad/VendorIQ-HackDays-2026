"""
VendorIQ Sourcing Brief View.

Displays current sourcing brief requirements and allows buyers to create new briefs
or adjust category requirements, country scope, and preferred supplier size parameters.
"""

import streamlit as st
import uuid
from typing import List

from core.models import SourcingBrief, SourcingCriteria, ScoringWeights
from ui.data_service import DashboardDataService
from ui.components import render_view_header, render_next_step_guidance


def render_brief_view(service: DashboardDataService, current_brief: SourcingBrief):
    """Render the sourcing brief management and creation interface."""
    render_view_header(
        title="📋 Sourcing Brief & Project Scope",
        subtitle="Manage procurement project scope, target criteria, and deterministic evaluation weights.",
        stage_key="brief",
    )

    tab_view, tab_create = st.tabs(["Active Sourcing Brief", "Create New Sourcing Brief"])

    with tab_view:
        st.subheader(f"Current Brief: {current_brief.title}")

        col1, col2 = st.columns([2, 1])
        with col1:
            st.markdown(f"**Brief ID**: `{current_brief.brief_id}`")
            st.markdown(f"**Buyer Organization**: {current_brief.buyer_organization}")
            st.markdown(f"**Description & Scope**:\n\n{current_brief.description}")

            st.markdown("#### Target Search & Evaluation Criteria")
            crit = current_brief.criteria
            st.write(f"- **Target Industries**: {', '.join(crit.target_industries)}")
            st.write(f"- **Target Geographies**: {', '.join(crit.target_geographies)}")
            if crit.min_company_size and crit.max_company_size:
                headcount_desc = f"{crit.min_company_size:,} to {crit.max_company_size:,} employees"
            elif crit.min_company_size:
                headcount_desc = f"{crit.min_company_size:,}+ employees"
            elif crit.max_company_size:
                headcount_desc = f"Up to {crit.max_company_size:,} employees"
            else:
                headcount_desc = "Any organizational size"
            st.write(f"- **Preferred Size Range (Headcount)**: {headcount_desc}")
            st.write(f"- **Mandatory Certifications Required**: {', '.join(crit.required_certifications) if crit.required_certifications else 'None specified'}")

        with col2:
            st.markdown("#### Evaluation Weights Configured")
            w = current_brief.criteria.weights
            st.write(f"- **Industry Relevance**: `{w.industry_relevance * 100:.0f}%`")
            st.write(f"- **Geographic Fit**: `{w.geographic_fit * 100:.0f}%`")
            st.write(f"- **Company Size Fit**: `{w.company_size_fit * 100:.0f}%`")
            st.write(f"- **Business Signals**: `{w.business_signals * 100:.0f}%`")
            st.write(f"- **Data Completeness**: `{w.data_completeness * 100:.0f}%`")

            total_w = (
                w.industry_relevance
                + w.geographic_fit
                + w.company_size_fit
                + w.business_signals
                + w.data_completeness
            )
            st.caption(f"Weight Sum Integrity: **{total_w:.2f}** (Strictly 1.0)")

    with tab_create:
        st.subheader("Define a New Sourcing Brief")
        st.info("Creating a new sourcing brief allows you to evaluate candidates against custom product categories and size requirements.")

        if "manual_brief_save_feedback" in st.session_state:
            fb = st.session_state.pop("manual_brief_save_feedback", None)
            if fb:
                if fb.get("type") == "info":
                    st.info(fb.get("message"))
                else:
                    st.success(fb.get("message"))

        with st.form("new_sourcing_brief_form"):
            new_title = st.text_input("Project Title", placeholder="e.g. Eco-friendly Corrugated Cartons Supplier Sourcing")
            new_org = st.text_input("Buyer Organization", value="VendorIQ Procurement Team")
            new_desc = st.text_area(
                "Sourcing Requirements & Problem Statement",
                placeholder="Describe scope, product specifications, volume demands, delivery frequency, and quality expectations (minimum 10 characters)...",
                height=120,
            )

            col_crit1, col_crit2 = st.columns(2)
            with col_crit1:
                target_ind = st.text_input(
                    "Target Industry / Category",
                    value="Packaging and Containers Manufacturing",
                    help="Matches Crustdata industry taxonomy.",
                )
                target_geo = st.text_input(
                    "Target Country / Geography",
                    value="India",
                    help="Country filter for supplier discovery.",
                )

            with col_crit2:
                col_s1, col_s2 = st.columns(2)
                with col_s1:
                    min_size = st.number_input("Min Headcount", min_value=1, value=50, step=10)
                with col_s2:
                    max_size = st.number_input("Max Headcount", min_value=1, value=2000, step=50)

                req_certs_input = st.text_input("Required Certifications (comma separated)", placeholder="e.g. ISO 9001, BRC Packaging")

            st.markdown("##### Custom Scoring Dimension Weights (Must Sum to 100%)")
            w_col1, w_col2, w_col3, w_col4, w_col5 = st.columns(5)
            with w_col1:
                w_ind = st.number_input("Industry %", min_value=0, max_value=100, value=35, step=5)
            with w_col2:
                w_geo = st.number_input("Geography %", min_value=0, max_value=100, value=25, step=5)
            with w_col3:
                w_size = st.number_input("Size Fit %", min_value=0, max_value=100, value=20, step=5)
            with w_col4:
                w_sig = st.number_input("Signals %", min_value=0, max_value=100, value=10, step=5)
            with w_col5:
                w_comp = st.number_input("Completeness %", min_value=0, max_value=100, value=10, step=5)

            total_user_weight = w_ind + w_geo + w_size + w_sig + w_comp
            if total_user_weight != 100:
                st.warning(f"Total weights currently sum to {total_user_weight}%. They must equal exactly 100%.")

            is_saving = st.session_state.get("is_saving_manual_brief", False)
            submitted = st.form_submit_button("Save Sourcing Brief to SQLite", disabled=is_saving)

            if submitted:
                if len(new_title.strip()) < 3:
                    st.error("Title must be at least 3 characters.")
                elif len(new_desc.strip()) < 10:
                    st.error("Description must be at least 10 characters.")
                elif total_user_weight != 100:
                    st.error("Weights must sum to exactly 100%. Please adjust before submitting.")
                elif not is_saving:
                    st.session_state["is_saving_manual_brief"] = True
                    certs_list = [c.strip() for c in req_certs_input.split(",") if c.strip()]
                    new_brief_id = f"BRIEF-{uuid.uuid4().hex[:8].upper()}"
                    criteria = SourcingCriteria(
                        target_industries=[target_ind.strip()],
                        target_geographies=[target_geo.strip()],
                        min_company_size=min_size,
                        max_company_size=max_size,
                        required_certifications=certs_list,
                        weights=ScoringWeights(
                            industry_relevance=w_ind / 100.0,
                            geographic_fit=w_geo / 100.0,
                            company_size_fit=w_size / 100.0,
                            business_signals=w_sig / 100.0,
                            data_completeness=w_comp / 100.0,
                        ),
                    )
                    created_brief = SourcingBrief(
                        brief_id=new_brief_id,
                        title=new_title.strip(),
                        buyer_organization=new_org.strip(),
                        description=new_desc.strip(),
                        criteria=criteria,
                    )
                    saved_brief = service.create_brief(created_brief)
                    if saved_brief.brief_id != created_brief.brief_id:
                        st.session_state["manual_brief_save_feedback"] = {
                            "type": "info",
                            "message": f"Identical sourcing brief already exists: `{saved_brief.brief_id}`. Reusing existing brief.",
                        }
                    else:
                        st.session_state["manual_brief_save_feedback"] = {
                            "type": "success",
                            "message": f"Successfully created and persisted sourcing brief `{saved_brief.brief_id}` to SQLite!",
                        }
                    st.session_state["is_saving_manual_brief"] = False
                    st.rerun()

    st.markdown("---")
    render_next_step_guidance(
        next_view_name="🔍 Supplier Discovery",
        guidance_text="Use natural language queries and filters to identify qualified suppliers matching this brief's target specifications.",
    )
