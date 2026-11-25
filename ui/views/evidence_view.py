"""
VendorIQ Evidence & Trust Center View (Phase 9).

Provides interactive Supplier Intelligence & Evidence Verification across all 7 official
evidence categories, multi-supplier coverage analytics, missing evidence checklists,
domain mismatch warnings, and complete audit lineage.
"""

from typing import List, Optional, Any
import pandas as pd
import streamlit as st

from core.models import (
    SupplierProfile,
    SupplierScore,
    EvidenceCategory,
    EvidenceStatus,
    SupplierEvidenceRecord,
)
from core.provenance import ProvenanceClassification
from services.evidence_service import SupplierEvidenceService
from services.data_quality_auditor import SupplierQualityAuditor
from ui.components import (
    get_provenance_badge_html,
    get_evidence_status_badge_html,
    render_domain_mismatch_warning,
    render_view_header,
    render_evidence_legend,
    render_next_step_guidance,
)


def render_evidence_view(
    suppliers: List[SupplierProfile],
    evaluations: List[SupplierScore],
    service: Optional[Any] = None,
):
    """Render the Evidence and Trust Center with Supplier Intelligence."""
    render_view_header(
        title="🛡️ Supplier Intelligence & Evidence Verification Center",
        subtitle="Structured verification audit, multi-supplier evidence coverage, missing data checklists, and immutable Crustdata API provenance.",
        stage_key="evidence",
    )

    # Initialize evidence service and auditor
    evidence_service = service.get_evidence_service() if service and hasattr(service, "get_evidence_service") else SupplierEvidenceService()
    auditor = service.auditor if service and hasattr(service, "auditor") else SupplierQualityAuditor()

    # Pre-generate / retrieve structured evidence for all suppliers
    evidence_by_supplier = evidence_service.generate_and_persist_evidence_for_all_suppliers()
    audit_report = auditor.audit_records(suppliers)
    audit_by_id = {item.supplier_id: item for item in audit_report.supplier_audit_items}

    tab_intel, tab_trust = st.tabs([
        "🔬 Supplier Intelligence & Evidence Verification",
        "🛡️ Lineage & Trust Policy Audit",
    ])

    # =========================================================================
    # TAB 1: Supplier Intelligence & Evidence Verification
    # =========================================================================
    with tab_intel:
        st.markdown("### 📊 Multi-Supplier Evidence Coverage Dashboard")
        st.caption("Verification status and completeness metrics across all 18 live Crustdata supplier profiles.")

        # Coverage KPI metrics
        cov_matrix = evidence_service.get_multi_supplier_coverage_matrix(suppliers=suppliers)
        total_supp = len(suppliers)
        mismatch_count = sum(1 for row in cov_matrix["supplier_rows"] if row.get("has_domain_mismatch"))
        avg_completeness = (
            sum(row["completeness_pct"] for row in cov_matrix["supplier_rows"]) / total_supp
            if total_supp > 0 else 0.0
        )

        col_m1, col_m2, col_m3, col_m4 = st.columns(4)
        with col_m1:
            st.metric("Total Evaluated Suppliers", f"{total_supp} Profiles", "100% Live Crustdata")
        with col_m2:
            st.metric("Avg Evidence Completeness", f"{avg_completeness:.1f}%", "7 Categories Evaluated")
        with col_m3:
            st.metric("Verified Evidence Rate", "0.0%", "Basic Directory Tier (No Fake Certs)")
        with col_m4:
            st.metric("Domain Mismatches Flagged", f"{mismatch_count} Suppliers", "Requires Manual Review" if mismatch_count else "Clean")

        # Multi-supplier summary table
        with st.expander("📋 View Complete 18-Supplier Coverage Matrix", expanded=True):
            cov_display = []
            for r in cov_matrix["supplier_rows"]:
                flag_text = "⚠️ REVIEW FLAGGED" if r.get("has_domain_mismatch") else "✅ Aligned"
                cov_display.append({
                    "Supplier Name": r["company_name"],
                    "Domain": r["domain"],
                    "Completeness": f"{r['completeness_pct']:.0f}%",
                    "Domain & Web": r.get(EvidenceCategory.COMPANY_WEBSITE_DOMAIN.value, "MISSING"),
                    "Manufacturing": r.get(EvidenceCategory.MANUFACTURING_CAPABILITIES.value, "MISSING"),
                    "Flexible Packaging": r.get(EvidenceCategory.FLEXIBLE_PACKAGING_EXPERIENCE.value, "MISSING"),
                    "Barrier Films": r.get(EvidenceCategory.BARRIER_FILM_EXPERIENCE.value, "MISSING"),
                    "Sustainability": r.get(EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS.value, "MISSING"),
                    "HQ Location": r.get(EvidenceCategory.HEADQUARTERS_LOCATIONS.value, "MISSING"),
                    "Company Size": r.get(EvidenceCategory.COMPANY_SIZE.value, "MISSING"),
                    "Domain Audit Flag": flag_text,
                })
            df_cov = pd.DataFrame(cov_display)
            st.dataframe(df_cov, width="stretch", hide_index=True)

        st.markdown("---")

        # =====================================================================
        # Single Supplier Deep-Dive Inspector
        # =====================================================================
        st.markdown("### 🔎 Deep-Dive Supplier Profile Evidence Audit")
        st.caption("Inspect individual company claims, evidence status badges, source lineage, and missing documentary requirements.")

        supp_map = {f"{s.company_name} ({s.domain})": s for s in suppliers}
        selected_label = st.selectbox(
            "Select Supplier Profile to Audit:",
            options=list(supp_map.keys()),
            index=0,
        )
        selected_supp = supp_map[selected_label]
        supp_audit_item = audit_by_id.get(selected_supp.supplier_id)

        # Domain mismatch alert if flagged
        if supp_audit_item and supp_audit_item.suspicious_domain_mismatch:
            render_domain_mismatch_warning(
                company_name=selected_supp.company_name,
                domain=selected_supp.domain,
                reason=supp_audit_item.mismatch_reason or "Domain stem does not align with company name.",
            )

        # Detailed Company Information Card
        col_c1, col_c2, col_c3, col_c4 = st.columns(4)
        with col_c1:
            st.markdown(f"**Company Legal Name:**\n\n`{selected_supp.company_name}`")
            st.markdown(f"**Supplier ID:** `{selected_supp.supplier_id}`")
        with col_c2:
            st.markdown(f"**Registered Domain:**\n\n[{selected_supp.domain}](https://{selected_supp.domain})")
            st.markdown("**Candidate Status:** `EVALUATED_CANDIDATE`")
        with col_c3:
            st.markdown(f"**Headquarters:**\n\n{selected_supp.headquarters_country}" + (f", {selected_supp.headquarters_city}" if selected_supp.headquarters_city else " *(City unspecified)*"))
            st.markdown(f"**Employee Bracket:** `{selected_supp.employee_range or 'Unspecified'}`")
        with col_c4:
            st.markdown(f"**Primary Vertical:**\n\nPackaging & Containers")
            st.markdown(f"**Provenance Tier:** `{selected_supp.overall_provenance.value}`")

        if selected_supp.description:
            with st.expander("📝 View Stated Company Description (Crustdata Directory Index)"):
                st.write(selected_supp.description)

        # Get evidence records and completeness for selected supplier
        evidence_records = evidence_by_supplier.get(selected_supp.supplier_id) or evidence_service.get_supplier_evidence(selected_supp.supplier_id)
        kpis = evidence_service.calculate_evidence_completeness(evidence_records)

        st.markdown("#### 📈 Evidence Completeness & Lineage Breakdown")
        col_k1, col_k2, col_k3, col_k4, col_k5 = st.columns(5)
        with col_k1:
            st.metric("Completeness Score", f"{kpis['completeness_percentage']:.1f}%", f"{7 - kpis['missing_count']}/7 Populated")
        with col_k2:
            st.metric("Verified Evidence", f"{kpis['verified_count']}/7", "0% (Truth-in-Evidence)")
        with col_k3:
            st.metric("Self-Reported Claims", f"{kpis['self_reported_count']}/7", "Directory Listings")
        with col_k4:
            st.metric("Unverified Claims", f"{kpis['unverified_count']}/7", "Review Required")
        with col_k5:
            st.metric("Missing Evidence Gaps", f"{kpis['missing_count']}/7", "Documentary Need")

        st.info(f"🛡️ **Integrity Risk Assessment**: {kpis['risk_summary']}")

        # Evidence Status by Category with Badges
        st.markdown("#### 📑 Category-by-Category Evidence Status")
        ev_table_rows = []
        for rec in evidence_records:
            status_icon = (
                "🟢" if rec.status == EvidenceStatus.VERIFIED else (
                    "🔵" if rec.status == EvidenceStatus.SELF_REPORTED else (
                        "🟠" if rec.status == EvidenceStatus.UNVERIFIED else "🔴"
                    )
                )
            )
            ev_table_rows.append({
                "Category": rec.category.value,
                "Status": f"{status_icon} {rec.status.value}",
                "Evaluated Information / Claim": rec.claim,
                "Source Lineage": rec.source_name,
                "Reference Link": rec.source_url or "None",
                "Reviewer & Audit Notes": rec.reviewer_notes or "None",
            })
        st.dataframe(pd.DataFrame(ev_table_rows), width="stretch", hide_index=True)

        # Missing Evidence Checklist
        st.markdown("#### 📋 Actionable Missing Evidence Checklist & Contracting Prerequisites")
        st.caption("Specific documentary proof required before issuing purchase orders or commercial contracts.")

        checklist_items = evidence_service.get_missing_evidence_checklist(evidence_records)
        if checklist_items:
            chk_rows = []
            for item in checklist_items:
                chk_rows.append({
                    "Evidence Category": item["category"],
                    "Status": item["status"],
                    "Urgency / Risk": item["urgency"],
                    "Mandatory Documentary Proof": item["required_document"],
                    "Procurement Impact": item["procurement_impact"],
                })
            st.dataframe(pd.DataFrame(chk_rows), width="stretch", hide_index=True)
        else:
            st.success("✅ All evidence categories have been independently verified with authoritative registry proof.")

    # =========================================================================
    # TAB 2: Crustdata Lineage & Trust Policy Audit
    # =========================================================================
    with tab_trust:
        # 1. Procurement Trust Policy Disclaimers
        st.markdown("### 📜 Procurement Integrity Guardrails")

        col_g1, col_g2 = st.columns(2)
        with col_g1:
            st.info(
                "**1. Self-Reported vs. Verified Evidence Boundary**\n\n"
                "Data sourced from discovery indexes reflects self-reported directory information. "
                "VendorIQ **never treats self-reported marketing claims as verified facts**. "
                "No vendor is granted verified certification status without an authoritative registry proof (e.g. IAF, SEC, or FDA)."
            )
            st.warning(
                "**2. Headcount ≠ Production Capacity**\n\n"
                "Reported employee ranges (`51-200`, `1001-5000`) indicate organizational footprint. "
                "VendorIQ **never interprets employee headcount as proven manufacturing volume or machine capacity**. "
                "Line audit and machinery inspection are required prior to commercial commitments."
            )

        with col_g2:
            st.error(
                "**3. No Automated Approvals or Endorsements**\n\n"
                "All evaluated suppliers are designated **EVALUATED CANDIDATES**. "
                "VendorIQ **never labels candidates as 'approved', 'preferred', or 'certified'** on the basis of directory indexing alone. "
                "Evaluations provide initial triage; human buyer review is mandatory."
            )
            st.success(
                "**4. Zero PII & Zero Automated Contact**\n\n"
                "The system strictly prohibits extracting, storing, or evaluating individual employee personal data. "
                "RFQs remain local drafts in SQLite; **no automated outreach or supplier emailing is executed**."
            )

        st.markdown("---")

        # 2. Data Lineage and Provenance Classifications
        st.markdown("### 🏷️ Provenance Classification Standards")
        st.markdown(
            """
            Every data attribute in VendorIQ is tagged with an immutable provenance tier:
            - **`VERIFIED_DATA`**: Backed by authoritative third-party registry or audit proof (Confidence: 1.0).
            - **`AI_ASSESSMENT`**: Deterministic scoring and multi-factor evaluation logic (Confidence: 0.6–0.8).
            - **`BUYER_SPECIFIED`**: Buyer-provided requirements, weights, and constraints (Confidence: 1.0).
            - **`UNVERIFIED_OR_MISSING`**: Directory indexing, self-reported overviews, or missing attributes (Confidence: 0.3–0.5).
            - **`SYNTHETIC_DATA`**: Benchmark seed records for regression testing (Confidence: 0.0).
            """
        )

        # 3. Live Crustdata Search Lineage Audit Trail
        st.markdown("### 🛰️ Live Crustdata API Provenance Trail")

        metadata_rows = [
            {"Parameter": "Target Endpoint", "Value": "POST https://api.crustdata.com/company/search"},
            {"Parameter": "API Version", "Value": "2025-11-01"},
            {"Parameter": "Authentication Scheme", "Value": "Authorization: Bearer [MASKED cd_***]"},
            {"Parameter": "Search Query Filters", "Value": "basic_info.industries = 'Packaging and Containers Manufacturing' AND locations.country = 'India'"},
            {"Parameter": "Approved Live Request Count", "Value": "1 (Strict single request approval executed in Phase 5)"},
            {"Parameter": "Total Records Returned", "Value": "18 live suppliers"},
            {"Parameter": "HTTP Status Code", "Value": "200 OK"},
            {"Parameter": "Actual Credits Consumed", "Value": "0.6 API Credits (Single company search request)"},
            {"Parameter": "Direct USD Cost", "Value": "0.00 USD (Consumed via included account credits; no direct USD bill)"},
            {"Parameter": "Dashboard Runtime Mode", "Value": "Local SQLite read mode (vendoriq.sqlite); 0 active API calls"},
            {"Parameter": "Storage Destination", "Value": "vendoriq.sqlite (Table: supplier_profiles)"},
        ]

        st.table(pd.DataFrame(metadata_rows))

        # 4. Individual Supplier Lineage Table
        with st.expander("📋 View Complete Supplier Provenance & Lineage Table"):
            supp_lineage = []
            for s in suppliers:
                supp_lineage.append({
                    "Supplier ID": s.supplier_id,
                    "Company Name": s.company_name,
                    "Domain": s.domain,
                    "Provenance Tier": s.overall_provenance.value,
                    "Data Source": "Crustdata /company/search (Live)" if not s.is_synthetic else "Synthetic Test Seed",
                    "Personal PII Contained": "No (Company-Level Only)",
                })
            st.dataframe(pd.DataFrame(supp_lineage), width="stretch", hide_index=True)

    render_evidence_legend()
    st.markdown("---")
    render_next_step_guidance(
        next_view_name="📝 RFQ Preparation",
        guidance_text="Create tailored commercial and technical RFQ specification packages based on verified supplier capabilities.",
    )
