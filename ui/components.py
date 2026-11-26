"""
VendorIQ UI Reusable Components.

Renders standardized headers, badges, warning callouts, and trust notices
across all dashboard views.
"""

import streamlit as st
from typing import Optional, List, Dict, Any, Tuple
from core.provenance import ProvenanceClassification


def render_header(subtitle: str = "AI-Powered Autonomous Procurement Intelligence"):
    """Render top application banner with system trust metadata."""
    st.markdown(
        f"""
        <div style="background: linear-gradient(90deg, #1E3A8A 0%, #0F172A 100%); padding: 18px 24px; border-radius: 10px; margin-bottom: 20px; color: white;">
            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">
                <div>
                    <h1 style="margin: 0; font-size: 26px; font-weight: 700; color: #FFFFFF;">VendorIQ Procurement Dashboard</h1>
                    <p style="margin: 4px 0 0 0; font-size: 14px; color: #93C5FD;">{subtitle}</p>
                </div>
                <div style="display: flex; gap: 8px; flex-wrap: wrap;">
                    <span style="background-color: #065F46; color: #D1FAE5; padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: 600;">
                        ● Runtime: Local SQLite Read Mode (Zero API Calls)
                    </span>
                    <span style="background-color: #1E40AF; color: #DBEAFE; padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: 600;">
                        Crustdata Search: 0.6 Credits ($0.00 USD)
                    </span>
                    <span style="background-color: #374151; color: #F3F4F6; padding: 4px 10px; border-radius: 6px; font-size: 12px; font-weight: 600;">
                        Active Connection: None
                    </span>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_trust_disclaimer():
    """Render prominent ethical trust disclaimer banner."""
    st.warning(
        "**Evidence & Trust Safeguards**: All displayed records are derived from self-reported Crustdata company overviews. "
        "Evaluated suppliers are designated as **EVALUATED CANDIDATES** and are **not verified, certified, or approved**. "
        "Employee headcounts indicate organizational size, not verified production capacity. "
        "Physical manufacturing audits and compliance verification are mandatory prior to RFP award."
    )


def get_provenance_badge_html(provenance: ProvenanceClassification) -> str:
    """Generate color-coded HTML pill for provenance classification."""
    color_map = {
        ProvenanceClassification.VERIFIED_DATA: ("#065F46", "#D1FAE5", "VERIFIED EVIDENCE"),
        ProvenanceClassification.AI_ASSESSMENT: ("#1E40AF", "#DBEAFE", "DETERMINISTIC EVALUATION"),
        ProvenanceClassification.BUYER_SPECIFIED: ("#6B21A8", "#F3E8FF", "BUYER CRITERIA"),
        ProvenanceClassification.UNVERIFIED_OR_MISSING: ("#B45309", "#FEF3C7", "UNVERIFIED / SELF-REPORTED"),
        ProvenanceClassification.SYNTHETIC_DATA: ("#9A3412", "#FFEDD5", "SYNTHETIC BENCHMARK"),
    }
    bg, fg, label = color_map.get(
        provenance,
        ("#374151", "#F3F4F6", provenance.value if hasattr(provenance, "value") else str(provenance)),
    )
    return f"""<span style="background-color: {bg}; color: {fg}; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;">{label}</span>"""


def render_domain_mismatch_warning(company_name: str, domain: str, reason: str):
    """Render clear and actionable domain review alert callout."""
    st.error(
        f"⚠️ **Manual Review Flag**: `{company_name}` &rarr; Primary Domain: `{domain}`\n\n"
        f"**Audit Finding**: {reason}\n\n"
        f"**Procurement Guidance**: The primary domain stem does not directly match the company's registered legal name. "
        f"Verify whether this domain belongs to a parent holding group (e.g. Kedia Group) or legacy operating entity before issuing binding commercial RFQs."
    )


def get_evidence_status_badge_html(status: str) -> str:
    """Generate color-coded HTML pill for evidence status (VERIFIED, SELF_REPORTED, UNVERIFIED, MISSING)."""
    norm = status.value if hasattr(status, "value") else str(status).upper()
    color_map = {
        "VERIFIED": ("#065F46", "#D1FAE5", "VERIFIED"),
        "SELF_REPORTED": ("#1E40AF", "#DBEAFE", "SELF-REPORTED"),
        "UNVERIFIED": ("#B45309", "#FEF3C7", "UNVERIFIED"),
        "MISSING": ("#991B1B", "#FEE2E2", "MISSING"),
    }
    bg, fg, label = color_map.get(norm, ("#374151", "#F3F4F6", norm))
    return f"""<span style="background-color: {bg}; color: {fg}; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;">{label}</span>"""


WORKFLOW_STAGES = [
    ("brief", "1. Sourcing Brief", "Define requirements & weights"),
    ("discovery", "2. Discovery", "Market search & shortlist"),
    ("evaluation", "3. Evaluation", "Multi-factor scoring"),
    ("evidence", "4. Evidence Review", "Verify claims & audits"),
    ("rfq", "5. RFQ Prep", "Draft & approve packages"),
    ("quotation", "6. Quotations & Analysis", "Ingest, compare & award"),
]


def render_workflow_stepper(current_stage_key: str):
    """
    Render a horizontal procurement workflow progress bar illustrating
    where the current view sits in the end-to-end sourcing lifecycle.
    """
    keys = [s[0] for s in WORKFLOW_STAGES]
    try:
        current_idx = keys.index(current_stage_key)
    except ValueError:
        current_idx = -1

    items_html = []
    for idx, (key, label, desc) in enumerate(WORKFLOW_STAGES):
        if idx < current_idx:
            # Completed stage
            badge = "✓ "
            bg_col = "#ECFDF5"
            border_col = "#059669"
            text_col = "#065F46"
            font_weight = "600"
        elif idx == current_idx:
            # Current active stage
            badge = "● "
            bg_col = "#EFF6FF"
            border_col = "#1E40AF"
            text_col = "#1E3A8A"
            font_weight = "700"
        else:
            # Upcoming stage
            badge = "○ "
            bg_col = "#F8FAFC"
            border_col = "#CBD5E1"
            text_col = "#64748B"
            font_weight = "500"

        active_marker = "box-shadow: 0 1px 3px rgba(30,58,138,0.15);" if idx == current_idx else ""

        item_html = (
            f'<div style="flex: 1 1 0px; min-width: 0; background-color: {bg_col}; '
            f'border: 1.5px solid {border_col}; border-radius: 6px; padding: 4px 6px; margin: 1px; {active_marker}">'
            f'<div style="font-size: 11px; font-weight: {font_weight}; color: {text_col}; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">'
            f'{badge}{label}'
            f'</div>'
            f'<div style="font-size: 9px; color: #64748B; margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">'
            f'{desc}'
            f'</div>'
            f'</div>'
        )
        items_html.append(item_html)

    stepper_html = (
        '<div style="margin-bottom: 16px; padding: 4px; background: #FFFFFF; border-radius: 8px;">'
        '<div style="display: flex; flex-direction: row; flex-wrap: nowrap; gap: 4px; justify-content: space-between; align-items: stretch; width: 100%;">'
        + "".join(items_html)
        + '</div></div>'
    )
    if hasattr(st, "html"):
        st.html(stepper_html)
    else:
        st.markdown(stepper_html, unsafe_allow_html=True)


def render_view_header(title: str, subtitle: str, stage_key: Optional[str] = None):
    """
    Render standardized view header with optional workflow position indicator.
    """
    if stage_key:
        render_workflow_stepper(stage_key)
    st.markdown(f"## {title}")
    st.caption(subtitle)


def render_score_legend():
    """Render collapsable legend explaining deterministic scoring tiers and non-compensability."""
    with st.expander("ℹ️ Understanding Sourcing Scores & Compliance Tiers"):
        st.markdown(
            """
            - **80.0 – 100.0 (Strong Fit)**: High alignment across industry, geography, capacity, and certifications.
            - **65.0 – 79.9 (Viable Fit)**: Meets core requirements with minor gaps in self-reported data or non-critical criteria.
            - **Below 65.0 (Weak / Review Required)**: Significant gaps in geographic proximity, capabilities, or missing attributes.

            > **Non-Compensability Rule**: Fulfilling commercial preferences (e.g., lower unit price) **never offsets or compensates for failing mandatory technical barrier requirements** (such as maximum OTR or WVTR thresholds). Non-compliant candidates are disqualified regardless of overall score.
            """
        )


def render_evidence_legend():
    """Render collapsable legend explaining evidence provenance tiers."""
    with st.expander("ℹ️ Evidence Status & Provenance Tier Explanations"):
        st.markdown(
            """
            - 🟢 **VERIFIED**: Substantiated by authentic documentary proof (e.g., ISO certificate, ASTM/DIN lab test sheet).
            - 🔵 **SELF_REPORTED**: Extracted from supplier claims or public profile overview without independent third-party certification.
            - 🟡 **UNVERIFIED**: Ambiguous claim, domain mismatch flag, or unconfirmed specification.
            - 🔴 **MISSING**: Required attribute completely absent from supplier submission.
            """
        )


def render_next_step_guidance(next_view_name: str, guidance_text: str):
    """Render actionable next-step card at the bottom of a view."""
    guidance_html = (
        '<div style="background-color: #F8FAFC; border: 1px solid #E2E8F0; border-left: 4px solid #1E40AF; padding: 12px 16px; border-radius: 6px; margin-top: 24px;">'
        f'<div style="font-size: 13px; font-weight: 600; color: #1E3A8A;">Next Recommended Step: {next_view_name}</div>'
        f'<div style="font-size: 12px; color: #475569; margin-top: 4px;">{guidance_text}</div>'
        '</div>'
    )
    if hasattr(st, "html"):
        st.html(guidance_html)
    else:
        st.markdown(guidance_html, unsafe_allow_html=True)
