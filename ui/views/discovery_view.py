"""
VendorIQ Intelligent Supplier Discovery & Recommendations View (Phase 10).

Provides natural language discovery, multi-faceted filtering (hard requirements vs.
soft preferences), deterministic ranking, evidence-backed candidate recommendations,
side-by-side comparison, and persistent shortlist curation with CSV export.
"""

from typing import List, Optional, Any
import pandas as pd
import streamlit as st

from core.models import (
    SupplierProfile,
    SupplierScore,
    SourcingBrief,
    EvidenceCategory,
    EvidenceStatus,
    ShortlistStatus,
    ShortlistItem,
    SupplierShortlist,
    DiscoveryFilterParams,
    DiscoveryRecommendation,
)
from core.provenance import ProvenanceClassification
from ui.components import (
    get_provenance_badge_html,
    get_evidence_status_badge_html,
    render_domain_mismatch_warning,
    render_view_header,
    render_score_legend,
    render_next_step_guidance,
)


def render_discovery_view(
    suppliers: List[SupplierProfile],
    brief: Optional[SourcingBrief] = None,
    evaluations: Optional[List[SupplierScore]] = None,
    service: Optional[Any] = None,
):
    """Render the intelligent supplier discovery, recommendation, and shortlisting interface."""
    render_view_header(
        title="🔍 Intelligent Supplier Discovery & Recommendations",
        subtitle="Evidence-grounded candidate search, hard requirement constraints, deterministic evaluation ranking, and human-in-the-loop shortlist management.",
        stage_key="discovery",
    )

    if not suppliers:
        st.info("No supplier records found in database.")
        return

    # Fallback initialization if service not passed
    if service is None:
        from ui.data_service import DashboardDataService
        service = DashboardDataService()

    discovery_service = service.get_discovery_service()

    # Active brief context banner
    if brief:
        with st.container():
            st.markdown(
                f"""
                <div style="background-color: #1E293B; border-left: 4px solid #3B82F6; padding: 12px 18px; border-radius: 6px; margin-bottom: 18px; color: #F8FAFC;">
                    <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap;">
                        <div>
                            <strong>Active Sourcing Brief:</strong> {brief.title} (<code>{brief.brief_id}</code>)<br/>
                            <span style="font-size: 13px; color: #94A3B8;">Target Industry: {brief.criteria.target_industries[0] if brief.criteria.target_industries else 'All'} &bull; Geography: {', '.join(brief.criteria.target_geographies)} &bull; Min Scale: {brief.criteria.min_company_size or 'Unconstrained'}</span>
                        </div>
                        <span style="background-color: #0F766E; color: #CCFBF1; padding: 4px 10px; border-radius: 4px; font-size: 12px; font-weight: 600;">
                            Deterministic Scoring Active
                        </span>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    # -------------------------------------------------------------------------
    # 1. Search and Filtering Controls
    # -------------------------------------------------------------------------
    st.markdown("### 🔎 Search & Multi-Faceted Filters")

    col_search, col_size, col_provenance = st.columns([3, 2, 2])
    with col_search:
        search_query = st.text_input(
            "Natural Language Search (Keywords, Capabilities, Materials)",
            placeholder="e.g. barrier films, flexible packaging, extrusion, Cosmo, biodegradable...",
            key="disc_search_query",
        ).strip()

    all_ranges = sorted(list({s.employee_range for s in suppliers if s.employee_range}))
    with col_size:
        selected_ranges = st.multiselect(
            "Employee Headcount Range",
            options=all_ranges,
            default=[],
            key="disc_selected_ranges",
        )

    with col_provenance:
        provenance_choice = st.selectbox(
            "Data Origin Filter",
            options=["All Records", "Live Crustdata API Only", "Synthetic Test Benchmarks"],
            index=0,
            key="disc_provenance_choice",
        )

    # Hard Requirements vs Soft Preferences (Expandable)
    with st.expander("⚙️ Hard Constraints vs. Preferences (Hard Filters Enforce Disqualification)", expanded=False):
        st.caption(
            "Hard requirements strictly disqualify non-compliant suppliers. "
            "Suppliers that do not satisfy hard constraints are flagged with warnings and disqualified from primary ranking."
        )
        col_h1, col_h2, col_h3 = st.columns(3)
        with col_h1:
            req_mfg = st.checkbox(
                "Require Stated Manufacturing Operations (Hard)",
                value=False,
                help="Filters for vendors with self-reported manufacturing/production claims in directory profile. On-site plant inspection remains unverified.",
                key="disc_req_mfg",
            )
            req_flex = st.checkbox(
                "Require Flexible Packaging Product Lines (Hard)",
                value=False,
                help="Filters for vendors reporting flexible polymer/film packaging lines in directory profile.",
                key="disc_req_flex",
            )
        with col_h2:
            req_barrier = st.checkbox(
                "Require Stated Barrier Film Experience (Hard)",
                value=False,
                help="Filters for vendors claiming barrier/specialty film capabilities in directory profile. Technical TDS and ASTM OTR/WVTR test reports remain UNVERIFIED and require documentary submission.",
                key="disc_req_barrier",
            )
            req_sust = st.checkbox(
                "Require Sustainability Claims / Certs (Hard)",
                value=False,
                help="Filters for vendors with green marketing claims or accredited certifications. Note: Directory records contain 0 verified third-party certificates.",
                key="disc_req_sust",
            )
        with col_h3:
            exclude_mismatch = st.checkbox(
                "Exclude Flagged Domain Mismatches (Hard)",
                value=False,
                help="Disqualifies suppliers flagged by the Data Quality Auditor for domain name discrepancies.",
                key="disc_exclude_mismatch",
            )

    # Build Filter Parameters
    filter_params = DiscoveryFilterParams(
        query=search_query if search_query else None,
        brief_id=brief.brief_id if brief else None,
        employee_ranges=selected_ranges,
        require_manufacturing=req_mfg,
        require_flexible_packaging=req_flex,
        require_barrier_films=req_barrier,
        require_sustainability_evidence=req_sust,
        exclude_domain_mismatches=exclude_mismatch,
    )

    # -------------------------------------------------------------------------
    # 2. Execute Deterministic Discovery
    # -------------------------------------------------------------------------
    recommendations = discovery_service.discover_suppliers(brief=brief, params=filter_params)

    # Filter by provenance if requested
    if provenance_choice == "Live Crustdata API Only":
        recommendations = [r for r in recommendations if not r.supplier_id.startswith("SYNTH_")]
    elif provenance_choice == "Synthetic Test Benchmarks":
        recommendations = [r for r in recommendations if r.supplier_id.startswith("SYNTH_")]

    total_matches = len(recommendations)
    hard_matches = sum(1 for r in recommendations if r.is_hard_match)
    disqualified = total_matches - hard_matches

    # KPI Summary Cards
    col_k1, col_k2, col_k3, col_k4 = st.columns(4)
    with col_k1:
        st.metric("Discovered Candidates", f"{total_matches} Vendors", f"From {len(suppliers)} in SQLite")
    with col_k2:
        st.metric("Fully Qualified Matches", f"{hard_matches} Vendors", "Satisfy All Hard Criteria" if req_mfg or req_barrier or req_flex or req_sust or exclude_mismatch else "No Hard Filters Set")
    with col_k3:
        st.metric("Disqualified on Hard Filters", f"{disqualified} Vendors", "Disqualified" if disqualified else "Zero Disqualified")
    with col_k4:
        top_score = recommendations[0].total_score if recommendations else 0.0
        st.metric("Top Candidate Score", f"{top_score:.1f} / 100", "Deterministic Scorecard")

    st.markdown("---")

    # -------------------------------------------------------------------------
    # 3. Main Discovery Interface: Views & Shortlist
    # -------------------------------------------------------------------------
    tab_cards, tab_table, tab_compare, tab_shortlist = st.tabs([
        "📋 Candidate Recommendation Cards",
        "📊 Tabular Discovery Overview",
        "⚖️ Side-by-Side Candidate Comparison",
        "📑 Shortlist & CSV Export Manager",
    ])

    # Ensure session state for active shortlist selection
    brief_id_val = brief.brief_id if brief else "DEFAULT_BRIEF"
    all_shortlists = discovery_service.list_shortlists_for_brief(brief_id_val)
    if "active_shortlist_id" not in st.session_state or not st.session_state.active_shortlist_id:
        if all_shortlists:
            st.session_state.active_shortlist_id = all_shortlists[0].shortlist_id
        else:
            new_slist = discovery_service.create_shortlist(
                brief_id=brief_id_val,
                name="Primary Packaging Shortlist",
                description="Curated candidates discovered for packaging requirements.",
            )
            st.session_state.active_shortlist_id = new_slist.shortlist_id
            all_shortlists = [new_slist]

    # =========================================================================
    # TAB 1: Candidate Recommendation Cards
    # =========================================================================
    with tab_cards:
        st.markdown("### 🏆 Ranked Candidate Supplier Profiles")
        st.caption("Ordered deterministically by evaluation score, evidence completeness, and hard constraint compliance.")

        if not recommendations:
            st.warning("No suppliers match the active search and filter constraints. Try broadening query terms or disabling hard filters.")
        else:
            for rec in recommendations:
                with st.container():
                    # Card Header
                    col_rh1, col_rh2 = st.columns([4, 1])
                    with col_rh1:
                        rank_badge = f"**#{rec.rank}**"
                        hard_pill = "🟢 **MATCHES HARD FILTERS (SELF-REPORTED)**" if rec.is_hard_match else "🔴 **FAILS HARD CONSTRAINT**"
                        st.markdown(f"#### {rank_badge} {rec.company_name} &nbsp; {hard_pill}")
                        st.markdown(
                            f"🌐 [{rec.domain}](https://{rec.domain}) &nbsp;|&nbsp; "
                            f"🏢 {rec.headquarters_country} ({rec.headquarters_city or 'City Unverified in Basic Tier'}) &nbsp;|&nbsp; "
                            f"👥 Headcount: **{rec.employee_range or 'N/A'}** &nbsp;|&nbsp; "
                            f"📋 Completeness: **{rec.evidence_completeness_pct:.0f}%** &nbsp;|&nbsp; "
                            f"🔬 **Verified Third-Party Proof: 0% (Directory Tier)**"
                        )
                    with col_rh2:
                        st.metric("Composite Score", f"{rec.total_score:.1f}/100")

                    # Hard filter failure alert
                    if not rec.is_hard_match and rec.hard_filter_failures:
                        st.error(f"⚠️ **Hard Requirement Disqualification**: {'; '.join(rec.hard_filter_failures)}")

                    # Domain mismatch alert
                    if rec.domain_mismatch_warning:
                        render_domain_mismatch_warning(
                            company_name=rec.company_name,
                            domain=rec.domain,
                            reason=rec.domain_mismatch_warning,
                        )

                    # Match explanation
                    st.info(f"**Match Analysis**: {rec.match_explanation}")

                    # Detailed evidence & risks in 3 columns
                    col_ev1, col_ev2, col_ev3 = st.columns(3)
                    with col_ev1:
                        st.markdown("**Available Supporting Evidence:**")
                        if rec.available_evidence:
                            for ev in rec.available_evidence[:4]:
                                st.write(f"- {ev}")
                        else:
                            st.write("- *No positive directory evidence on file.*")

                    with col_ev2:
                        st.markdown("**Missing Evidence / Unverified Claims:**")
                        combined_gaps = rec.unverified_claims + rec.missing_evidence
                        if combined_gaps:
                            for gap in combined_gaps[:4]:
                                st.write(f"- {gap}")
                        else:
                            st.write("- *All baseline categories populated.*")

                    with col_ev3:
                        st.markdown("**Procurement Risks & Next Steps:**")
                        for rsk in rec.procurement_risks[:3]:
                            st.write(f"- ⚠️ {rsk}")

                    # Shortlist Action Button
                    with st.expander(f"➕ Add / Update '{rec.company_name}' in Shortlist"):
                        col_s1, col_s2, col_s3 = st.columns([2, 1, 1])
                        with col_s1:
                            user_note = st.text_input(
                                "Reviewer Comments / Audit Notes",
                                key=f"note_{rec.supplier_id}",
                                placeholder="e.g. Strong candidate for barrier pouches; request ASTM test reports...",
                            )
                        with col_s2:
                            item_status_choice = st.selectbox(
                                "Candidate Status",
                                options=["CANDIDATE", "UNDER_REVIEW", "SHORTLISTED", "REJECTED"],
                                index=0,
                                key=f"status_{rec.supplier_id}",
                            )
                        with col_s3:
                            st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
                            if st.button("Save to Shortlist", key=f"btn_add_{rec.supplier_id}"):
                                discovery_service.add_supplier_to_shortlist(
                                    shortlist_id=st.session_state.active_shortlist_id,
                                    supplier_id=rec.supplier_id,
                                    reviewer_notes=user_note,
                                    status=ShortlistStatus(item_status_choice),
                                )
                                st.success(f"Saved {rec.company_name} as {item_status_choice}!")
                                st.rerun()

                    st.markdown("---")

    # =========================================================================
    # TAB 2: Tabular Discovery Overview
    # =========================================================================
    with tab_table:
        st.markdown("### 📊 Tabular Sourcing Discovery Overview")
        st.caption("Complete tabular view of all matching supplier profiles, evaluation factors, and evidence coverage.")

        table_data = []
        for rec in recommendations:
            table_data.append({
                "Rank": rec.rank,
                "Company Name": rec.company_name,
                "Domain": rec.domain,
                "Total Score": rec.total_score,
                "Qualified Match": "✅ Qualified" if rec.is_hard_match else "🔴 Disqualified",
                "Headcount Range": rec.employee_range or "Not specified",
                "Country": rec.headquarters_country,
                "City": rec.headquarters_city or "Unverified",
                "Evidence Completeness": f"{rec.evidence_completeness_pct:.0f}%",
                "Domain Audit Flag": "⚠️ Mismatch Flagged" if rec.domain_mismatch_warning else "✅ Aligned",
            })
        st.dataframe(pd.DataFrame(table_data), width="stretch", hide_index=True)

    # =========================================================================
    # TAB 3: Side-by-Side Comparison
    # =========================================================================
    with tab_compare:
        st.markdown("### ⚖️ Side-by-Side Candidate Evaluation")
        st.caption("Compare up to 3 candidate suppliers simultaneously across factor scores, hard requirements, and evidence completeness.")

        supp_choices = [f"{r.company_name} (#{r.rank})" for r in recommendations]
        selected_for_comp = st.multiselect(
            "Select Suppliers to Compare (Up to 3):",
            options=supp_choices,
            default=supp_choices[:min(3, len(supp_choices))],
            max_selections=3,
        )

        if not selected_for_comp:
            st.info("Select at least 1 supplier above to inspect comparative metrics.")
        else:
            comp_recs = [r for r in recommendations if f"{r.company_name} (#{r.rank})" in selected_for_comp]
            comp_cols = st.columns(len(comp_recs))

            for idx, r in enumerate(comp_recs):
                with comp_cols[idx]:
                    st.markdown(f"### #{r.rank} {r.company_name}")
                    st.markdown(f"🌐 `{r.domain}` &bull; 🏢 `{r.headquarters_country}`")
                    st.metric("Composite Score", f"{r.total_score:.1f}/100")
                    st.metric("Evidence Completeness", f"{r.evidence_completeness_pct:.0f}%")
                    st.metric("Hard Match Status", "QUALIFIED" if r.is_hard_match else "DISQUALIFIED")

                    st.markdown("#### Factor Breakdown:")
                    for dim, sc in r.factor_scores.items():
                        st.write(f"- **{dim}**: `{sc:.1f}/100`")

                    st.markdown("#### Primary Risks:")
                    for rsk in r.procurement_risks[:2]:
                        st.write(f"- ⚠️ {rsk}")

    # =========================================================================
    # TAB 4: Shortlist Management & CSV Export
    # =========================================================================
    with tab_shortlist:
        st.markdown("### 📑 Shortlist Management & Human Procurement Review")
        st.caption("Curate vendor shortlists, track candidate review lifecycle status, and export official CSV records.")

        # Shortlist selector & creation bar
        col_sl1, col_sl2 = st.columns([3, 2])
        all_cur_shortlists = discovery_service.list_shortlists_for_brief(brief_id_val)
        shortlist_names = {s.name: s.shortlist_id for s in all_cur_shortlists}

        with col_sl1:
            active_name = st.selectbox(
                "Active Shortlist",
                options=list(shortlist_names.keys()) if shortlist_names else ["None"],
                index=0 if shortlist_names else 0,
            )
            if active_name != "None":
                st.session_state.active_shortlist_id = shortlist_names[active_name]

        with col_sl2:
            with st.expander("➕ Create New Shortlist"):
                new_slist_name = st.text_input("New Shortlist Name", placeholder="e.g. Q4 Packaging RFP Shortlist")
                new_slist_desc = st.text_area("Scope / Notes", placeholder="Shortlisted suppliers for high-barrier films...")
                if st.button("Create Shortlist"):
                    if new_slist_name.strip():
                        created = discovery_service.create_shortlist(
                            brief_id=brief_id_val,
                            name=new_slist_name.strip(),
                            description=new_slist_desc.strip() if new_slist_desc else None,
                        )
                        st.session_state.active_shortlist_id = created.shortlist_id
                        st.success(f"Created shortlist '{created.name}'!")
                        st.rerun()

        # Load active shortlist
        current_shortlist = discovery_service.get_shortlist(st.session_state.active_shortlist_id) if st.session_state.active_shortlist_id else None

        if current_shortlist:
            st.markdown(f"#### Shortlist: **{current_shortlist.name}**")
            if current_shortlist.description:
                st.write(f"*{current_shortlist.description}*")

            items = current_shortlist.items
            total_items = len(items)
            status_counts = {
                "SHORTLISTED": sum(1 for it in items if it.status == ShortlistStatus.SHORTLISTED),
                "UNDER_REVIEW": sum(1 for it in items if it.status == ShortlistStatus.UNDER_REVIEW),
                "CANDIDATE": sum(1 for it in items if it.status == ShortlistStatus.CANDIDATE),
                "REJECTED": sum(1 for it in items if it.status == ShortlistStatus.REJECTED),
            }

            col_sstat1, col_sstat2, col_sstat3, col_sstat4 = st.columns(4)
            with col_sstat1:
                st.metric("Total Candidates", f"{total_items} Suppliers")
            with col_sstat2:
                st.metric("Approved Shortlisted", f"{status_counts['SHORTLISTED']} Suppliers")
            with col_sstat3:
                st.metric("Under Review", f"{status_counts['UNDER_REVIEW']} Suppliers")
            with col_sstat4:
                st.metric("Rejected Candidates", f"{status_counts['REJECTED']} Suppliers")

            # CSV Export Button
            csv_str = current_shortlist.to_csv()
            st.download_button(
                label="📥 Export Shortlist to CSV",
                data=csv_str,
                file_name=f"shortlist_{current_shortlist.name.replace(' ', '_').lower()}.csv",
                mime="text/csv",
                key="btn_download_shortlist_csv",
            )

            st.markdown("---")

            # Editable Shortlist Table
            if not items:
                st.info("No candidates added to this shortlist yet. Browse candidates in Tab 1 and click 'Add to Shortlist'.")
            else:
                st.markdown("##### Curated Candidates:")
                for it in items:
                    with st.container():
                        col_it1, col_it2, col_it3, col_it4 = st.columns([3, 2, 3, 1])
                        with col_it1:
                            st.markdown(f"**{it.company_name or it.supplier_id}**")
                            st.caption(f"Domain: `{it.domain or 'N/A'}` &bull; ID: `{it.supplier_id}`")
                        with col_it2:
                            new_status_val = st.selectbox(
                                "Lifecycle Status",
                                options=["CANDIDATE", "UNDER_REVIEW", "SHORTLISTED", "REJECTED"],
                                index=["CANDIDATE", "UNDER_REVIEW", "SHORTLISTED", "REJECTED"].index(it.status.value),
                                key=f"tbl_stat_{it.item_id}",
                            )
                        with col_it3:
                            new_notes_val = st.text_input(
                                "Reviewer Notes",
                                value=it.reviewer_notes or "",
                                key=f"tbl_notes_{it.item_id}",
                            )
                        with col_it4:
                            st.markdown("<div style='height: 28px;'></div>", unsafe_allow_html=True)
                            if st.button("Update", key=f"btn_upd_{it.item_id}"):
                                discovery_service.update_shortlist_item_status(
                                    item_id=it.item_id,
                                    status=ShortlistStatus(new_status_val),
                                    reviewer_notes=new_notes_val,
                                )
                                st.success("Updated!")
                                st.rerun()

                        st.markdown("---")

    render_score_legend()
    render_next_step_guidance(
        next_view_name="🏆 Supplier Evaluation",
        guidance_text="Inspect detailed factor score breakdowns, weighted category evaluations, and deterministic rankings for all candidate profiles.",
    )
