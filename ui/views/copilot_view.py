"""
VendorIQ AI Sourcing Copilot View (Phase 8A).

Provides an interactive conversational and structured procurement copilot:
1. Natural Language Sourcing Brief Extractor with editable human review.
2. Grounded candidate recommendations with clear missing evidence flags.
3. Conversational procurement assistant strictly grounded in SQLite records.
"""

import os
import streamlit as st
import pandas as pd
from typing import List, Dict, Any, Optional

from core.models import (
    SourcingBrief,
    SupplierProfile,
    SupplierScore,
    ExtractedSourcingRequirements,
    CopilotRecommendation,
)
from ui.data_service import DashboardDataService


def render_copilot_view(
    service: DashboardDataService,
    current_brief: SourcingBrief,
    suppliers: List[SupplierProfile],
    evaluations: List[SupplierScore],
):
    """Render the AI Sourcing Copilot interface."""
    st.markdown("## 🤖 AI Sourcing Copilot")
    st.caption("AI-assisted procurement intelligence grounded strictly in SQLite supplier records and deterministic scorecards.")

    # Guardrails Banner
    active_db = getattr(service.repository, "db_path", None) or "vendoriq.sqlite"
    active_db_name = os.path.basename(str(active_db)) if active_db != ":memory:" else ":memory:"
    st.info(
        f"🛡️ **Grounded AI Safeguards**: The Copilot operates strictly on candidate records in `{active_db_name}`. "
        "It **never invents supplier facts, certifications, or overrides deterministic scores**. "
        "Self-reported claims are labeled as `UNVERIFIED_OR_MISSING`. All actions require human buyer confirmation."
    )

    tab_nl, tab_recs, tab_chat = st.tabs([
        "📝 Natural Language Brief Builder",
        "🎯 AI Candidate Recommendations",
        "💬 Interactive Procurement Chat",
    ])

    copilot = service.get_copilot()

    # -------------------------------------------------------------------------
    # TAB 1: Natural Language Brief Builder
    # -------------------------------------------------------------------------
    with tab_nl:
        st.subheader("Extract Sourcing Brief from Plain English")
        st.write("Describe your procurement need in natural language. The copilot will extract structured criteria into a validated brief.")

        # Display save status feedback banner if brief was recently saved
        if "brief_save_feedback" in st.session_state:
            fb = st.session_state.pop("brief_save_feedback", None)
            if fb:
                if fb.get("type") == "info":
                    st.info(fb.get("message"))
                else:
                    st.success(fb.get("message"))

        col_sample1, col_sample2, col_sample3 = st.columns(3)
        sample_prompt = ""
        with col_sample1:
            if st.button("Sample: Eco-Friendly Films"):
                st.session_state["copilot_nl_prompt"] = (
                    "We need sustainable, biodegradable packaging and compostable barrier films in India "
                    "with at least 50 employees and ISO 9001 certified production."
                )
        with col_sample2:
            if st.button("Sample: Corrugated Boxes"):
                st.session_state["copilot_nl_prompt"] = (
                    "Looking for high-burst-strength corrugated carton box manufacturers in India "
                    "with 100 to 1000 employees for regional retail distribution."
                )
        with col_sample3:
            if st.button("Sample: Metal Cans & Tins"):
                st.session_state["copilot_nl_prompt"] = (
                    "Seeking metal tin packaging manufacturers for industrial chemicals and food-grade packaging "
                    "located in India with established multi-plant operations."
                )

        nl_input = st.text_area(
            "Enter Buyer Sourcing Requirements",
            value=st.session_state.get("copilot_nl_prompt", ""),
            placeholder="e.g. We require flexible packaging suppliers in India that produce recyclable films and pouches with over 100 employees...",
            height=100,
            key="nl_input_box",
        )

        col_btn, col_mode = st.columns([2, 1])
        with col_btn:
            extract_clicked = st.button("✨ Extract Structured Requirements (AI)")
        with col_mode:
            st.caption(f"Engine: **{'Gemini 2.5 Flash' if copilot.is_online else 'Deterministic Fallback Parser'}**")

        if extract_clicked:
            if not nl_input.strip():
                st.error("Please enter a sourcing prompt before extracting.")
            else:
                with st.spinner("Extracting structured criteria..."):
                    extracted = copilot.parse_natural_language_brief(nl_input.strip())
                    st.session_state["extracted_brief"] = extracted

        # Display and Edit Extracted Requirements
        if "extracted_brief" in st.session_state:
            extracted: ExtractedSourcingRequirements = st.session_state["extracted_brief"]

            st.markdown("---")
            st.markdown("### 📋 Review Extracted Sourcing Criteria")
            st.caption("Human Review Step: Verify or modify the extracted fields before committing to SQLite.")

            with st.form("confirm_brief_form"):
                f_title = st.text_input("Project Title", value=extracted.title)
                f_cat = st.text_input("Product Category", value=extracted.category)

                col_f1, col_f2 = st.columns(2)
                with col_f1:
                    f_inds = st.text_input("Target Industries (Comma-separated)", value=", ".join(extracted.target_industries))
                    f_geos = st.text_input("Target Geographies (Comma-separated)", value=", ".join(extracted.target_geographies))
                with col_f2:
                    col_sz1, col_sz2 = st.columns(2)
                    with col_sz1:
                        f_min_sz = st.number_input("Min Headcount", min_value=1, value=extracted.min_company_size or 50, step=10)
                    with col_sz2:
                        f_max_sz = st.number_input("Max Headcount", min_value=1, value=extracted.max_company_size or 2000, step=50)

                f_specs = st.text_input("Key Specifications", value=", ".join(extracted.required_specifications))
                f_certs = st.text_input("Explicit Certifications", value=", ".join(extracted.required_certifications))
                f_summary = st.text_area("Executive Problem Statement & Scope", value=extracted.summary, height=90)

                st.markdown("##### 🛡️ Human Review Confirmation Gate")
                confirm_check = st.checkbox("I have reviewed the AI-extracted requirements and verify they represent our sourcing scope.")

                is_saving = st.session_state.get("is_saving_brief", False)
                save_brief = st.form_submit_button(
                    "Confirm & Save Sourcing Brief to SQLite",
                    disabled=is_saving,
                )

                if save_brief:
                    if not confirm_check:
                        st.error("Human Review Gate: You must check the confirmation box before saving.")
                    elif not is_saving:
                        st.session_state["is_saving_brief"] = True
                        inds_list = [i.strip() for i in f_inds.split(",") if i.strip()]
                        geos_list = [g.strip() for g in f_geos.split(",") if g.strip()]
                        specs_list = [s.strip() for s in f_specs.split(",") if s.strip()]
                        certs_list = [c.strip() for c in f_certs.split(",") if c.strip()]

                        effective_max_sz = f_max_sz if (extracted.max_company_size is not None or f_max_sz != 2000) else None
                        updated_extracted = ExtractedSourcingRequirements(
                            title=f_title.strip(),
                            category=f_cat.strip(),
                            target_industries=inds_list,
                            target_geographies=geos_list,
                            min_company_size=f_min_sz,
                            max_company_size=effective_max_sz,
                            required_specifications=specs_list,
                            required_certifications=certs_list,
                            summary=f_summary.strip(),
                        )

                        new_brief = updated_extracted.to_sourcing_brief()
                        print(
                            f"[COPILOT VIEW DIAGNOSTIC] Submitting brief ID '{new_brief.brief_id}' | "
                            f"Title='{new_brief.title}' | MinSz={new_brief.criteria.min_company_size} | "
                            f"MaxSz={new_brief.criteria.max_company_size} | Certs={new_brief.criteria.required_certifications}",
                            flush=True,
                        )
                        saved_brief = service.create_brief(new_brief)
                        print(
                            f"[COPILOT VIEW DIAGNOSTIC] service.create_brief returned brief ID '{saved_brief.brief_id}' "
                            f"(was duplicate: {saved_brief.brief_id != new_brief.brief_id})",
                            flush=True,
                        )

                        if saved_brief.brief_id != new_brief.brief_id:
                            st.session_state["brief_save_feedback"] = {
                                "type": "info",
                                "message": f"Identical sourcing brief already exists: `{saved_brief.brief_id}`. Reusing existing brief from SQLite.",
                            }
                        else:
                            st.session_state["brief_save_feedback"] = {
                                "type": "success",
                                "message": f"Successfully saved new brief `{saved_brief.brief_id}` to SQLite! You can select it in the sidebar.",
                            }

                        st.session_state.pop("extracted_brief", None)
                        st.session_state["is_saving_brief"] = False
                        st.rerun()

    # -------------------------------------------------------------------------
    # TAB 2: AI Candidate Recommendations
    # -------------------------------------------------------------------------
    with tab_recs:
        st.subheader(f"Top Recommended Candidates for: {current_brief.title}")
        st.caption("AI-synthesized procurement commentary sitting strictly ON TOP of deterministic factor scores.")

        top_k = st.slider("Number of Candidates to Recommend", min_value=3, max_value=10, value=5)
        recs = copilot.recommend_candidates(brief=current_brief, top_k=top_k)

        if not recs:
            st.info("No supplier records available for recommendation.")
        else:
            for rec in recs:
                with st.container():
                    col_r1, col_r2, col_r3 = st.columns([1, 4, 2])
                    with col_r1:
                        st.markdown(f"<h2 style='color: #2563EB; margin: 0;'>#{rec.rank}</h2>", unsafe_allow_html=True)
                    with col_r2:
                        st.markdown(f"### {rec.company_name}")
                        st.caption(f"Domain: `{rec.domain}` &nbsp;|&nbsp; ID: `{rec.supplier_id}` &nbsp;|&nbsp; Status: `EVALUATED_CANDIDATE`")
                    with col_r3:
                        st.metric("Deterministic Score", f"{rec.total_score:.2f}/100")

                    # Fit Rationale
                    st.markdown(f"**AI Fit Rationale**: {rec.fit_rationale}")

                    # Matched Capabilities and Missing Evidence Flags
                    col_c1, col_c2 = st.columns(2)
                    with col_c1:
                        st.markdown("##### ✅ Matched Capabilities")
                        for cap in rec.matched_capabilities:
                            st.write(f"- {cap}")
                    with col_c2:
                        st.markdown("##### ⚠️ Missing Evidence & Due Diligence Alerts")
                        for flag in rec.missing_evidence_flags:
                            st.write(f"- {flag}")

                    # Provenance Breakdown Expander
                    with st.expander(f"🏷️ View Provenance Lineage for {rec.company_name}"):
                        for dim, prov in rec.provenance_breakdown.items():
                            st.write(f"- **{dim}**: `{prov}`")

                    st.markdown("<hr style='margin: 12px 0;'/>", unsafe_allow_html=True)

    # -------------------------------------------------------------------------
    # TAB 3: Interactive Procurement Assistant Chat
    # -------------------------------------------------------------------------
    with tab_chat:
        st.subheader("Sourcing Intelligence Assistant")
        st.caption("Ask questions about candidate suppliers, capabilities, risk factors, or evaluation factors.")

        # Initialize session chat history if absent
        if "copilot_chat_history" not in st.session_state:
            st.session_state["copilot_chat_history"] = [
                {
                    "role": "assistant",
                    "content": (
                        f"Hello! I am your AI Sourcing Copilot for **{current_brief.title}**. "
                        f"I am grounded in {len(suppliers)} candidate supplier records from the active database `{active_db_name}`. "
                        "You can ask me to analyze candidate capabilities, summarize risk alerts, or compare specific suppliers."
                    ),
                }
            ]

        # Suggested Query Chips
        st.markdown("**Suggested Inquiries**:")
        col_q1, col_q2, col_q3 = st.columns(3)
        with col_q1:
            if st.button("🌿 Who produces sustainable films?"):
                st.session_state["pending_chat_prompt"] = "Which candidate packaging suppliers in India produce biodegradable or compostable films?"
        with col_q2:
            if st.button("🏢 Are any suppliers publicly listed?"):
                st.session_state["pending_chat_prompt"] = "Which suppliers in our database are publicly listed on BSE or NSE?"
        with col_q3:
            if st.button("⚠️ What domain review flags exist?"):
                st.session_state["pending_chat_prompt"] = "What domain review flags and missing address data exist across the 18 suppliers?"

        # Display conversation history
        for msg in st.session_state["copilot_chat_history"]:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

        # Chat Input
        user_prompt = st.chat_input("Ask the AI Sourcing Copilot a question...")
        if not user_prompt and "pending_chat_prompt" in st.session_state:
            user_prompt = st.session_state.pop("pending_chat_prompt")

        if user_prompt:
            # Display user message
            st.session_state["copilot_chat_history"].append({"role": "user", "content": user_prompt})
            with st.chat_message("user"):
                st.markdown(user_prompt)

            # Query copilot
            with st.chat_message("assistant"):
                with st.spinner("Consulting grounded SQLite supplier records..."):
                    resp = copilot.chat_sourcing_assistant(
                        message=user_prompt,
                        current_brief=current_brief,
                        history=st.session_state["copilot_chat_history"],
                    )
                    st.markdown(resp.assistant_message)
                    st.caption(f"Grounded in {resp.grounded_record_count} SQLite records | Model: {resp.model_used}")
                    st.session_state["copilot_chat_history"].append({
                        "role": "assistant",
                        "content": resp.assistant_message,
                    })
