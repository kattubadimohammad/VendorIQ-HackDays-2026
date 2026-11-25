"""
VendorIQ Supplier Quotation Management & Commercial Comparison View.

Enables procurement teams to:
1. Ingest supplier quotations via PDF/XLSX file upload or manual entry.
2. Review extracted parameters with field-level provenance (page/cell references) and human confirmation gates.
3. Compare quotations side-by-side with currency normalization and landed-cost transparency.
4. Evaluate deterministic, explainable recommendations with non-compensable technical compliance.
5. Execute human award sign-off and rejection workflows with complete immutable audit trails.

Guarantees:
- Zero automated purchase orders or supplier emails.
- Low price never offsets mandatory technical failure.
- Buyer confirmation does not fabricate independently verified evidence.
"""

from datetime import datetime, timezone, date, timedelta
from typing import List, Dict, Optional, Any
import io

import pandas as pd
import streamlit as st

from core.models import (
    SupplierProfile,
    SourcingBrief,
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
from ui.data_service import DashboardDataService
from ui.components import (
    get_evidence_status_badge_html,
    render_view_header,
    render_score_legend,
    render_evidence_legend,
)
from utils.file_storage import MAX_FILE_SIZE_BYTES, validate_quotation_file
from services.quotation_comparison import TechnicalComplianceEvaluator


def _ensure_rfq_for_supplier(service: DashboardDataService, brief, supplier, user_name: str) -> str:
    """Ensure an RFQ record exists in SQLite for foreign key integrity, creating a draft if needed."""
    existing = service.repository.list_supplier_rfqs(brief_id=brief.brief_id, supplier_id=supplier.supplier_id)
    if existing:
        return existing[0].rfq_id
    created = service.create_and_persist_supplier_rfq(
        brief=brief,
        supplier=supplier,
        contact_person=user_name,
    )
    return created.rfq_id


def render_quotation_view(
    service: DashboardDataService,
    brief: SourcingBrief,
    suppliers: List[SupplierProfile],
):
    """Render the comprehensive Quotation Management, Comparison, and Award view."""
    render_view_header(
        title="💰 Supplier Quotations & Commercial Analysis",
        subtitle="Ingest supplier proposals, verify extraction provenance, normalize multi-currency pricing, evaluate landed costs and non-compensable technical compliance, and manage human award sign-offs.",
        stage_key="quotation",
    )

    # Top Safeguard Banner
    st.info(
        "🔒 **Procurement Governance Safeguard**: "
        "VendorIQ supports transparent commercial evaluation and decision support. "
        "The system **strictly prohibits automated purchase order creation, automatic vendor awards, or external supplier messaging**. "
        "Final supplier award decisions require explicit human buyer authorization and offline contract execution."
    )

    # Active Project Brief selector
    all_briefs = service.repository.list_briefs()
    brief_map = {b.brief_id: b for b in all_briefs}
    brief_options = [f"{b.brief_id} – {b.title}" for b in all_briefs]

    default_idx = 0
    for idx, b in enumerate(all_briefs):
        if b.brief_id == brief.brief_id:
            default_idx = idx
            break

    col_b1, col_b2 = st.columns([3, 1])
    with col_b1:
        selected_brief_label = st.selectbox(
            "Active Sourcing Project",
            options=brief_options,
            index=default_idx,
            help="Select the sourcing brief to associate with quotation evaluation.",
        )
        selected_brief_id = selected_brief_label.split(" – ")[0]
        active_brief = brief_map.get(selected_brief_id, brief)
    with col_b2:
        st.metric("Buyer Organization", active_brief.buyer_organization or "Enterprise Procurement")

    # Fetch existing quotations for active brief
    all_quotes = service.list_quotations_for_brief(active_brief.brief_id, latest_only=False)
    latest_quotes = service.list_quotations_for_brief(active_brief.brief_id, latest_only=True)

    # High-level KPIs
    k_col1, k_col2, k_col3, k_col4, k_col5, k_col6 = st.columns(6)
    k_col1.metric("Total Submissions", len(all_quotes))
    k_col2.metric("Active Proposals", len(latest_quotes))
    k_col3.metric("Pending Review", sum(1 for q in latest_quotes if q.status == QuotationStatus.PARSED))
    k_col4.metric("Under Comparison", sum(1 for q in latest_quotes if q.status == QuotationStatus.UNDER_REVIEW))
    k_col5.metric("Award Accepted", sum(1 for q in latest_quotes if q.status == QuotationStatus.ACCEPTED))
    k_col6.metric("Rejected", sum(1 for q in latest_quotes if q.status == QuotationStatus.REJECTED))

    st.markdown("---")

    # Five Functional Tabs
    tab_ingest, tab_review, tab_compare, tab_recs, tab_approval = st.tabs([
        "📤 1. Ingestion & Ingestion Preview",
        "🔍 2. Review & Human Confirmation",
        "⚖️ 3. Side-by-Side Comparison",
        "🎯 4. Deterministic Recommendations",
        "✍️ 5. Human Approval & Award Gate",
    ])

    # =========================================================================
    # TAB 1: INGESTION & UPLOAD
    # =========================================================================
    with tab_ingest:
        st.subheader("Quotation Ingestion & Document Intake")
        st.caption("Upload supplier proposals in PDF or OpenXML (.xlsx) format, or enter quotes manually.")

        # Resolve supplier targets under this brief
        rfqs = service.repository.list_supplier_rfqs(brief_id=active_brief.brief_id)
        rfq_map = {f"{r.supplier.company_name} ({r.rfq_id})": r for r in rfqs}

        # If no RFQ exists, fallback to supplier list
        supp_map = {s.supplier_id: s for s in suppliers}
        supp_options = [f"{s.company_name} ({s.supplier_id})" for s in suppliers]

        col_target1, col_target2 = st.columns(2)
        with col_target1:
            if rfq_map:
                selected_rfq_key = st.selectbox("Associated Supplier RFQ", list(rfq_map.keys()))
                target_rfq = rfq_map[selected_rfq_key]
                eff_rfq_id = target_rfq.rfq_id
                eff_supp_id = target_rfq.supplier_id
                eff_company = target_rfq.supplier.company_name
            else:
                st.info("ℹ️ No prior RFQs found for this brief. A linked RFQ record will be automatically initialized for the selected supplier to maintain procurement audit lineage.")
                selected_supp_key = st.selectbox("Select Target Supplier", supp_options)
                eff_supp_id = selected_supp_key.split("(")[-1].strip(")")
                eff_company = supp_map[eff_supp_id].company_name
                eff_rfq_id = None
        with col_target2:
            buyer_user_name = st.text_input("Procurement Lead Name", value="Procurement Specialist")

        sub_tab_file, sub_tab_manual = st.tabs(["📄 Document Upload (PDF / XLSX)", "✍️ Manual Proposal Entry"])

        # Sub-tab: Document Upload
        with sub_tab_file:
            uploaded_file = st.file_uploader(
                "Upload Supplier Quotation Document",
                type=["pdf", "xlsx", "xls"],
                help="Allowed formats: PDF, OpenXML Excel (.xlsx). Maximum file size: 10 MB. Legacy binary .xls will be rejected.",
            )

            if uploaded_file is not None:
                filename = uploaded_file.name
                file_bytes = uploaded_file.getvalue()

                # Size Limit Rejection Check (Standardized 10 MB limit)
                if len(file_bytes) > MAX_FILE_SIZE_BYTES:
                    mb = len(file_bytes) / (1024 * 1024)
                    max_mb = MAX_FILE_SIZE_BYTES / (1024 * 1024)
                    st.error(
                        f"❌ **File Exceeds Maximum Allowed Size ({max_mb:.0f} MB)**\n\n"
                        f"The uploaded document `{filename}` is {mb:.2f} MB, which exceeds the standardized {max_mb:.0f} MB limit. "
                        f"Please compress or reduce document attachments before uploading."
                    )
                # Legacy XLS Rejection check
                elif filename.lower().endswith(".xls"):
                    st.error(
                        "❌ **Legacy Binary Excel (.xls / BIFF8) Is Not Supported**\n\n"
                        "Deterministic cell-coordinate extraction requires modern OpenXML (.xlsx) or text-based PDF format. "
                        "Please re-save or convert the supplier document to `.xlsx` or `.pdf` before uploading."
                    )
                else:
                    is_valid, err_msg = validate_quotation_file(filename, file_bytes)
                    if not is_valid:
                        st.error(f"❌ **Invalid Quotation File**: {err_msg}")
                    else:
                        st.success(f"File uploaded: `{filename}` ({len(file_bytes) / 1024:.1f} KB)")

                    # Action 1: Extract Preview Draft
                    if st.button("🔍 Extract & Preview Draft Parameters", key="btn_preview_draft"):
                        try:
                            draft = service.extract_quotation_draft(filename, file_bytes)
                            st.session_state["active_quotation_draft"] = draft
                            st.session_state["uploaded_file_bytes"] = file_bytes
                            st.session_state["uploaded_filename"] = filename
                            st.toast("Quotation draft extracted successfully!", icon="✅")
                        except Exception as e:
                            st.error(f"Extraction error: {e}")

                    # Render extracted draft if present in session state
                    if "active_quotation_draft" in st.session_state and st.session_state.get("uploaded_filename") == filename:
                        draft = st.session_state["active_quotation_draft"]

                        st.markdown("#### 📋 Extracted Draft Preview")
                        if draft.missing_fields:
                            st.warning(
                                f"⚠️ **Missing Critical Fields Detected**: `{', '.join(draft.missing_fields)}`. "
                                f"Human confirmation will require explicit buyer input for these values before comparison."
                            )

                        if draft.extraction_warnings:
                            with st.expander("⚠️ Extraction Warnings & Disclosures", expanded=True):
                                for w in draft.extraction_warnings:
                                    st.write(f"- {w}")

                        col_d1, col_d2, col_d3, col_d4 = st.columns(4)
                        col_d1.metric("Quoted Price", f"{draft.commercial.quoted_unit_price:,.4f} {draft.commercial.quoted_currency}")
                        col_d2.metric("Quoted Quantity", f"{draft.commercial.quoted_quantity:,} units")
                        col_d3.metric("Lead Time", f"{draft.commercial.lead_time_weeks} weeks")
                        col_d4.metric("Incoterms", draft.commercial.incoterms.value)

                        # Provenance Table
                        st.markdown("##### 🔍 Field Extraction Provenance Lineage")
                        prov_rows = []
                        for f_name, p in draft.provenance_map.items():
                            prov_rows.append({
                                "Field": f_name,
                                "Source Location": p.page_or_cell_reference,
                                "Raw Extracted Value": p.raw_value,
                                "Method": p.extraction_method.value,
                                "Confidence": f"{p.confidence * 100:.0f}%" if p.confidence else "N/A",
                                "Verification Status": p.status.value,
                            })
                        if prov_rows:
                            st.dataframe(pd.DataFrame(prov_rows), use_container_width=True)

                        # Technical Specs
                        st.markdown("##### 🔬 Technical Compliance Indicators")
                        t_col1, t_col2 = st.columns(2)
                        with t_col1:
                            st.write(f"**Structure**: {draft.compliance.structure_description}")
                            st.write(f"**Total Thickness**: {draft.compliance.thickness_microns or 'Not specified'} microns")
                            st.write(f"**OTR**: {draft.compliance.otr_value or 'Not specified'}")
                            st.write(f"**WVTR**: {draft.compliance.wvtr_value or 'Not specified'}")
                        with t_col2:
                            st.write(f"**Standards Cited**: {', '.join(draft.compliance.test_standards_cited) or 'None'}")
                            st.write(f"**Certifications**: {', '.join(draft.compliance.certifications_offered) or 'None'}")
                            st.write(f"**Authentic TDS Document**: `{'Attached' if draft.compliance.has_tds_document else 'Missing'}`")
                            st.write(f"**Mandatory Requirements Met**: `{'Yes' if draft.compliance.mandatory_requirements_met else 'No'}`")

                        # Ingest & Persist Button
                        if st.button("💾 Persist Quotation into SQLite", type="primary", key="btn_persist_quote"):
                            try:
                                target_rfq_id = eff_rfq_id
                                if not target_rfq_id:
                                    supp_obj = supp_map.get(eff_supp_id)
                                    if not supp_obj:
                                        raise ValueError(f"Supplier '{eff_supp_id}' not found.")
                                    target_rfq_id = _ensure_rfq_for_supplier(service, active_brief, supp_obj, buyer_user_name)

                                saved_quote = service.ingest_quotation_file(
                                    rfq_id=target_rfq_id,
                                    filename=filename,
                                    content_bytes=st.session_state["uploaded_file_bytes"],
                                    user_name=buyer_user_name,
                                    brief_id=active_brief.brief_id,
                                    supplier_id=eff_supp_id,
                                    company_name=eff_company,
                                )
                                st.success(f"Quotation `{saved_quote.quotation_id}` (Version {saved_quote.version}) successfully saved in SQLite!")
                                st.session_state.pop("active_quotation_draft", None)
                                st.rerun()
                            except Exception as e:
                                st.error(f"Persistence error: {e}")

        # Sub-tab: Manual Entry
        with sub_tab_manual:
            st.markdown("#### Direct Manual Proposal Entry")
            st.caption("Use when supplier submitted pricing via email, phone, or informal communication.")

            with st.form("manual_quote_form"):
                mc_col1, mc_col2, mc_col3 = st.columns(3)
                with mc_col1:
                    m_price = st.number_input("Unit Price", min_value=0.01, value=1.45, step=0.01, format="%.4f")
                    m_curr = st.selectbox("Quoted Currency", ["USD", "INR", "EUR", "GBP"], index=0)
                    m_qty = st.number_input("Quoted Quantity", min_value=1, value=50000, step=1000)
                with mc_col2:
                    m_moq = st.number_input("Minimum Order Quantity (MOQ)", min_value=1, value=10000, step=1000)
                    m_lead = st.number_input("Production Lead Time (Weeks)", min_value=1, value=4, step=1)
                    m_inco = st.selectbox("Incoterms", [i.value for i in Incoterm], index=1)
                with mc_col3:
                    m_pay = st.text_input("Payment Terms", value="Net 30 days upon inspection")
                    m_freight = st.number_input("Freight Charges (if separate)", min_value=0.0, value=0.0, step=100.0)
                    m_taxes = st.number_input("Duties / Taxes (if separate)", min_value=0.0, value=0.0, step=100.0)

                st.markdown("##### Technical Specifications")
                mt_col1, mt_col2 = st.columns(2)
                with mt_col1:
                    m_struct = st.text_input("Structure Description", value="12u PET / 15u Met-BOPP / 50u Polyethylene")
                    m_thick = st.number_input("Thickness (microns)", min_value=1.0, value=77.0, step=1.0)
                    m_otr = st.text_input("OTR Claim", value="1.1 cc/m2/day (ASTM D3985)")
                with mt_col2:
                    m_wvtr = st.text_input("WVTR Claim", value="0.7 g/m2/day (ASTM F1249)")
                    m_standards = st.multiselect("Test Standards Cited", ["ASTM D3985", "ASTM F1249", "ISO 15105"], default=["ASTM D3985"])
                    m_has_tds = st.checkbox("Authentic TDS Attached", value=False)

                m_notes = st.text_area("Reviewer Notes", value="Manual quotation entered from formal email proposal.")

                submit_manual = st.form_submit_button("Submit Manual Quotation", type="primary")

            if submit_manual:
                try:
                    comm_data = QuotationCommercialData(
                        quoted_currency=m_curr,
                        quoted_unit_price=m_price,
                        quoted_quantity=int(m_qty),
                        min_order_quantity=int(m_moq),
                        lead_time_weeks=int(m_lead),
                        payment_terms=m_pay,
                        incoterms=Incoterm(m_inco),
                        freight_charges=m_freight if m_freight > 0 else None,
                        taxes_and_duties=m_taxes if m_taxes > 0 else None,
                        is_freight_included=(m_inco in ("CIF", "DDP")),
                        is_tax_included=(m_inco == "DDP"),
                    )
                    comp_data = QuotationTechnicalCompliance(
                        product_category="Flexible Packaging",
                        structure_description=m_struct,
                        thickness_microns=m_thick,
                        otr_value=m_otr,
                        wvtr_value=m_wvtr,
                        test_standards_cited=m_standards,
                        certifications_offered=["ISO 9001"],
                        has_tds_document=m_has_tds,
                        mandatory_requirements_met=False,  # Recomputed against the active sourcing brief during comparison.
                    )
                    target_rfq_id = eff_rfq_id
                    if not target_rfq_id:
                        supp_obj = supp_map.get(eff_supp_id)
                        if not supp_obj:
                            raise ValueError(f"Supplier '{eff_supp_id}' not found.")
                        target_rfq_id = _ensure_rfq_for_supplier(service, active_brief, supp_obj, buyer_user_name)

                    saved = service.ingest_manual_quotation(
                        rfq_id=target_rfq_id,
                        commercial_data=comm_data,
                        compliance_data=comp_data,
                        user_name=buyer_user_name,
                        reviewer_notes=m_notes,
                    )
                    st.success(f"Manual quotation `{saved.quotation_id}` recorded successfully!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error saving manual quotation: {e}")

    # =========================================================================
    # TAB 2: REVIEW & HUMAN CONFIRMATION GATE
    # =========================================================================
    with tab_review:
        st.subheader("Quotation Review & Human Confirmation Gate")
        st.caption(
            "Review extracted commercial parameters, verify source document provenance, correct values if necessary, "
            "and execute mandatory human confirmation before multi-candidate comparison."
        )

        if not latest_quotes:
            st.info("No supplier quotations have been submitted yet under this brief.")
        else:
            quote_options = {f"{q.company_name} – Version {q.version} ({q.quotation_id}) [{q.status.value}]": q for q in latest_quotes}
            selected_quote_key = st.selectbox("Select Quotation to Review", list(quote_options.keys()))
            active_quote = quote_options[selected_quote_key]

            # Header info card
            st.markdown(
                f"""
                <div style="background-color: #F8FAFC; border: 1px solid #E2E8F0; padding: 14px; border-radius: 8px; margin-bottom: 15px;">
                    <div style="display: flex; justify-content: space-between; flex-wrap: wrap;">
                        <div>
                            <h4 style="margin: 0; color: #1E293B;">{active_quote.company_name}</h4>
                            <p style="margin: 4px 0 0 0; font-size: 13px; color: #64748B;">
                                Quotation ID: <code>{active_quote.quotation_id}</code> | RFQ ID: <code>{active_quote.rfq_id}</code> | Version: <b>{active_quote.version}</b>
                            </p>
                        </div>
                        <div>
                            <span style="background-color: #1E40AF; color: white; padding: 4px 10px; border-radius: 6px; font-weight: 600; font-size: 13px;">
                                Status: {active_quote.status.value}
                            </span>
                        </div>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            # Display Provenance Table
            st.markdown("##### 📍 Extraction Provenance Lineage")
            if active_quote.commercial.provenance_map:
                prov_df_rows = []
                for f_name, p in active_quote.commercial.provenance_map.items():
                    prov_df_rows.append({
                        "Field": f_name,
                        "Source Reference": p.page_or_cell_reference or "N/A",
                        "Raw Extracted Text": p.raw_value or "N/A",
                        "Method": p.extraction_method.value,
                        "Confidence": f"{p.confidence * 100:.0f}%" if p.confidence else "N/A",
                        "Status": p.status.value,
                        "Confirmed By": p.confirmed_by or "Pending Buyer Review",
                    })
                st.dataframe(pd.DataFrame(prov_df_rows), use_container_width=True)
            else:
                st.caption("Zero provenance records attached (legacy or synthetic submission).")

            # Human Review & Edit Form
            st.markdown("##### ✏️ Review & Field Confirmation Form")
            st.caption("Verify that extracted numbers match the supplier invoice. Corrections will be logged in the audit trail.")

            with st.form("confirm_quote_form"):
                rf_col1, rf_col2, rf_col3 = st.columns(3)
                with rf_col1:
                    conf_price = st.number_input(
                        "Quoted Unit Price",
                        min_value=0.0,
                        value=float(active_quote.commercial.quoted_unit_price),
                        step=0.01,
                        format="%.4f"
                    )
                    conf_curr = st.selectbox(
                        "Quoted Currency",
                        ["USD", "INR", "EUR", "GBP"],
                        index=["USD", "INR", "EUR", "GBP"].index(active_quote.commercial.quoted_currency) if active_quote.commercial.quoted_currency in ["USD", "INR", "EUR", "GBP"] else 0
                    )
                    conf_qty = st.number_input(
                        "Quoted Quantity",
                        min_value=1,
                        value=int(active_quote.commercial.quoted_quantity),
                        step=1000
                    )
                with rf_col2:
                    conf_moq = st.number_input(
                        "Minimum Order Quantity (MOQ)",
                        min_value=1,
                        value=int(active_quote.commercial.min_order_quantity),
                        step=1000
                    )
                    conf_lead = st.number_input(
                        "Lead Time (Weeks)",
                        min_value=1,
                        value=int(active_quote.commercial.lead_time_weeks),
                        step=1
                    )
                    inco_opts = [i.value for i in Incoterm]
                    cur_inco_idx = inco_opts.index(active_quote.commercial.incoterms.value) if active_quote.commercial.incoterms.value in inco_opts else 0
                    conf_inco = st.selectbox("Incoterms", inco_opts, index=cur_inco_idx)
                with rf_col3:
                    conf_pay = st.text_input("Payment Terms", value=active_quote.commercial.payment_terms)
                    conf_freight = st.number_input(
                        "Freight Charges ($ / total)",
                        min_value=0.0,
                        value=float(active_quote.commercial.freight_charges or 0.0),
                        step=100.0
                    )
                    conf_tax = st.number_input(
                        "Taxes / Duties ($ / total)",
                        min_value=0.0,
                        value=float(active_quote.commercial.taxes_and_duties or 0.0),
                        step=100.0
                    )

                conf_notes = st.text_area(
                    "Reviewer Sign-off Notes",
                    value="Buyer verified extracted unit price, currency, lead time, and payment terms against supplier submission."
                )

                submit_confirm = st.form_submit_button("✅ Confirm Critical Fields & Validate Proposal", type="primary")

            if submit_confirm:
                try:
                    updated_comm = active_quote.commercial.model_copy(deep=True)
                    updated_comm.quoted_unit_price = conf_price
                    updated_comm.quoted_currency = conf_curr
                    updated_comm.quoted_quantity = int(conf_qty)
                    updated_comm.min_order_quantity = int(conf_moq)
                    updated_comm.lead_time_weeks = int(conf_lead)
                    updated_comm.payment_terms = conf_pay
                    updated_comm.incoterms = Incoterm(conf_inco)
                    updated_comm.freight_charges = conf_freight if conf_freight > 0 else None
                    updated_comm.taxes_and_duties = conf_tax if conf_tax > 0 else None

                    confirmed = service.confirm_and_validate_quotation(
                        quotation_id=active_quote.quotation_id,
                        user_name=buyer_user_name,
                        confirmed_commercial=updated_comm,
                        notes=conf_notes,
                    )
                    st.success(f"Quotation `{confirmed.quotation_id}` confirmed and promoted to UNDER_REVIEW!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Confirmation failed: {e}")

            # Audit Trail for this quotation
            st.markdown("##### 📜 Chronological Quotation Audit Trail")
            audit_records = service.get_quotation_audit_trail(active_quote.quotation_id)
            if audit_records:
                audit_rows = []
                for a in audit_records:
                    audit_rows.append({
                        "Audit ID": a.audit_id,
                        "Timestamp (UTC)": a.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                        "Action": a.action,
                        "From Status": a.from_status or "INIT",
                        "To Status": a.to_status,
                        "Actor": a.changed_by,
                        "Comments": a.comments,
                    })
                st.dataframe(pd.DataFrame(audit_rows), use_container_width=True)
            else:
                st.caption("No historical audit entries found.")

    # =========================================================================
    # TAB 3: SIDE-BY-SIDE COMPARISON
    # =========================================================================
    with tab_compare:
        st.subheader("Side-by-Side Multi-Supplier Quotation Matrix")
        st.caption(
            "Normalize pricing across multiple currencies, compare quoted base prices against estimated landed costs, "
            "and inspect non-compensable technical compliance."
        )

        if not latest_quotes:
            st.info("No quotations available to compare.")
        else:
            col_c1, col_c2 = st.columns([2, 2])
            with col_c1:
                cmp_currency = st.selectbox(
                    "Select Target Comparison Currency",
                    ["USD", "INR", "EUR", "GBP"],
                    index=0,
                    help="All quotations will be normalized to this currency for side-by-side comparison."
                )

            # Buyer FX Override expander
            buyer_overrides: Dict[str, ExchangeRateRecord] = {}
            with st.expander("⚙️ Advanced: Transparent Buyer FX Overrides", expanded=False):
                st.caption("Specify customized exchange rates to override static reference benchmarks.")
                fx_col1, fx_col2, fx_col3 = st.columns(3)
                with fx_col1:
                    custom_pair = st.selectbox("Currency Pair to Override", ["USD:INR", "USD:EUR", "USD:GBP"], index=0)
                with fx_col2:
                    custom_rate = st.number_input("Custom Exchange Rate", min_value=0.0001, value=85.00, step=0.1)
                with fx_col3:
                    custom_notes = st.text_input("Override Rationale", value="Bank forward contract rate")

                if st.checkbox("Apply This Buyer Override to Comparison"):
                    b_from, b_to = custom_pair.split(":")
                    buyer_overrides[b_to] = ExchangeRateRecord(
                        base_currency=b_from,
                        target_currency=b_to,
                        rate=custom_rate,
                        source=f"Buyer Override by {buyer_user_name}",
                        effective_date=str(date.today()),
                        rate_type=ExchangeRateType.BUYER_OVERRIDE,
                        is_outdated=False,
                        is_buyer_override=True,
                        notes=custom_notes,
                    )
                    st.info(f"Active Override Applied: 1 {b_from} = {custom_rate} {b_to} ({custom_notes})")

            # Run Multi-Supplier Comparison Matrix
            matrix = service.compare_quotations_for_brief(
                brief_id=active_brief.brief_id,
                target_currency=cmp_currency,
                buyer_fx_overrides=buyer_overrides if buyer_overrides else None,
            )

            # Landed Cost Completeness Warning if applicable
            if not matrix.are_all_landed_costs_complete:
                st.warning(
                    f"⚠️ **Landed Cost Data Incomplete Across Candidates**: "
                    f"One or more proposals are missing freight or import tariffs. "
                    f"**Candidates are ranked by Normalized Quoted Base Price** ({matrix.target_currency}/unit). "
                    f"Logistics and customs duties must be quantified prior to final award."
                )
            else:
                st.success(
                    f"✅ **Complete Cost Comparison**: All compliant candidates have complete logistics and tax data. "
                    f"Ranking is based on **Estimated Landed Unit Cost** ({matrix.target_currency}/unit)."
                )

            # Format Comparison Table
            matrix_rows = []
            for r in matrix.rows:
                # Format Landed Cost
                if r.normalized_landed_unit_cost is not None:
                    landed_display = f"{r.normalized_landed_unit_cost:.4f} {r.target_currency}"
                else:
                    landed_display = "Incomplete (Missing Freight/Tax)"

                # Technical Status Badge
                tech_display = "✅ Compliant" if r.mandatory_requirements_met else "❌ Disqualified"

                # Evidence tier
                has_tds = r.has_tds_document
                tier_label = "VERIFIED (TDS)" if has_tds else "SELF-REPORTED"

                matrix_rows.append({
                    "Supplier": r.company_name,
                    "Quotation Ref": r.quotation_id,
                    "Version": r.version,
                    "Status": r.status.value,
                    "Quoted Base Price": f"{r.quoted_unit_price:.4f} {r.quoted_currency}",
                    f"Normalized Base ({matrix.target_currency})": f"{r.normalized_base_unit_price:.4f}" if r.normalized_base_unit_price else "Unavailable FX",
                    f"Estimated Landed Cost ({matrix.target_currency})": landed_display,
                    "Lead Time": f"{r.lead_time_weeks} wks",
                    "MOQ": f"{r.min_order_quantity:,}",
                    "Incoterms": r.incoterms.value,
                    "Technical Status": tech_display,
                    "Evidence Tier": tier_label,
                    "Overall Score": f"{r.overall_comparison_score:.1f}",
                    "Category": r.recommendation_type.value,
                })

            st.dataframe(pd.DataFrame(matrix_rows), use_container_width=True)

            # Transparent Calculation Breakdowns
            with st.expander("🔍 Transparent Mathematical & FX Calculation Breakdowns", expanded=False):
                for r in matrix.rows:
                    st.markdown(f"**{r.company_name} ({r.quotation_id})**:")
                    st.write(f"- **Exchange Rate**: {r.fx_rate_applied.source if r.fx_rate_applied else 'N/A'} (Rate: {r.fx_rate_applied.rate if r.fx_rate_applied else 'N/A'}, Date: {r.fx_rate_applied.effective_date if r.fx_rate_applied else 'N/A'})")
                    st.write(f"- **Landed Cost Breakdown**: {r.landed_cost_explanation}")
                    if r.disqualification_reasons:
                        st.error(f"- **Disqualification Reasons**: {'; '.join(r.disqualification_reasons)}")
                    st.markdown("---")

    # =========================================================================
    # TAB 4: DETERMINISTIC RECOMMENDATIONS
    # =========================================================================
    with tab_recs:
        st.subheader("🎯 Deterministic Sourcing Recommendations")
        st.caption(
            "Explainable, rule-based candidate rankings grounded in commercial value, technical specifications, "
            "and documentary evidence. Recommendations are purely advisory; final supplier award requires human sign-off."
        )

        if not latest_quotes:
            st.info("No quotations available to evaluate.")
        else:
            matrix = service.compare_quotations_for_brief(
                brief_id=active_brief.brief_id,
                target_currency=cmp_currency if "cmp_currency" in locals() else "USD",
                buyer_fx_overrides=buyer_overrides if "buyer_overrides" in locals() else None,
            )

            # Governance Notice
            st.info(f"⚖️ **Governance Protocol**: {matrix.governance_notice}")

            # Primary Recommendation Card
            if matrix.primary_recommendation:
                p_rec = matrix.primary_recommendation
                st.markdown(
                    f"""
                    <div style="background-color: #ECFDF5; border: 2px solid #10B981; border-radius: 10px; padding: 18px; margin-bottom: 20px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap;">
                            <div>
                                <span style="background-color: #059669; color: white; padding: 4px 12px; border-radius: 6px; font-weight: 700; font-size: 13px;">
                                    🏆 PRIMARY RECOMMENDATION: {p_rec.recommendation_type.value}
                                </span>
                                <h3 style="margin: 8px 0 4px 0; color: #064E3B;">{p_rec.company_name}</h3>
                                <p style="margin: 0; font-size: 14px; color: #047857;">
                                    Proposal Ref: <code>{p_rec.quotation_id}</code> (v{p_rec.version}) | Incoterms: <b>{p_rec.incoterms.value}</b>
                                </p>
                            </div>
                            <div style="text-align: right;">
                                <div style="font-size: 26px; font-weight: 800; color: #064E3B;">
                                    {p_rec.overall_comparison_score:.1f} / 100
                                </div>
                                <div style="font-size: 12px; color: #059669; font-weight: 600;">Overall Sourcing Score</div>
                            </div>
                        </div>
                        <hr style="margin: 12px 0; border: none; border-top: 1px solid #A7F3D0;" />
                        <div style="font-size: 14px; color: #065F46; line-height: 1.5;">
                            <b>Deterministic Recommendation Rationale</b>:<br/>
                            {p_rec.recommendation_notes}
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            # Secondary Recommendation Card
            if matrix.secondary_recommendation:
                s_rec = matrix.secondary_recommendation
                st.markdown(
                    f"""
                    <div style="background-color: #EFF6FF; border: 1px solid #3B82F6; border-radius: 8px; padding: 16px; margin-bottom: 20px;">
                        <span style="background-color: #2563EB; color: white; padding: 3px 10px; border-radius: 4px; font-weight: 600; font-size: 12px;">
                            🥈 SECONDARY RECOMMENDATION: {s_rec.recommendation_type.value}
                        </span>
                        <h4 style="margin: 6px 0 2px 0; color: #1E3A8A;">{s_rec.company_name}</h4>
                        <p style="margin: 0 0 8px 0; font-size: 13px; color: #1D4ED8;">
                            Quoted: <b>{s_rec.quoted_unit_price:.4f} {s_rec.quoted_currency}</b> ({s_rec.normalized_base_unit_price:.4f} {matrix.target_currency}) | Lead Time: <b>{s_rec.lead_time_weeks} weeks</b> | Score: <b>{s_rec.overall_comparison_score:.1f}/100</b>
                        </p>
                        <p style="margin: 0; font-size: 13px; color: #1E40AF;">
                            <b>Rationale</b>: {s_rec.recommendation_notes}
                        </p>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            # Disqualified Candidates Section
            if matrix.disqualified_candidates:
                st.markdown("#### 🚫 Disqualified Candidates (Non-Compensable Technical Failures)")
                for disq in matrix.disqualified_candidates:
                    st.error(
                        f"**{disq.company_name}** (`{disq.quotation_id}`):\n\n"
                        f"- **Quoted Base Price**: {disq.quoted_unit_price:.4f} {disq.quoted_currency}\n"
                        f"- **Failure Reason**: {'; '.join(disq.disqualification_reasons)}\n"
                        f"- **Procurement Invariant**: A low commercial unit price cannot offset a mandatory technical specification failure."
                    )

    # =========================================================================
    # TAB 5: HUMAN APPROVAL & AWARD GATE
    # =========================================================================
    with tab_approval:
        st.subheader("✍️ Human Award Sign-Off & Rejection Gate")
        st.caption(
            "Execute legally binding award acceptance or rejection decisions. "
            "All actions are permanently logged in the SQLite audit history with buyer identity and mandatory rationale."
        )

        if not latest_quotes:
            st.info("No supplier proposals submitted yet.")
        else:
            awardable_quotes = [q for q in latest_quotes if q.status in (QuotationStatus.UNDER_REVIEW, QuotationStatus.ACCEPTED, QuotationStatus.REJECTED)]

            if not awardable_quotes:
                st.warning("No quotations currently in UNDER_REVIEW status. Please review and confirm proposals in Tab 2 first.")
            else:
                target_award_options = {f"{q.company_name} – Version {q.version} ({q.quotation_id}) [{q.status.value}]": q for q in awardable_quotes}
                selected_award_key = st.selectbox("Select Candidate for Award Decision", list(target_award_options.keys()))
                award_candidate = target_award_options[selected_award_key]
                award_eligible, award_failures, _, award_technical_notes = TechnicalComplianceEvaluator.evaluate_compliance(
                    award_candidate, active_brief
                )
                barrier_brief = any(
                    token in f"{active_brief.title} {active_brief.description}".lower()
                    for token in ("barrier", "evoh", "alu foil", "high barrier")
                )
                award_evidence_ready = not barrier_brief or award_candidate.compliance.has_tds_document
                award_ready = award_eligible and award_evidence_ready
                if not award_eligible:
                    st.error(
                        "🚫 Award blocked by mandatory technical requirements: "
                        + "; ".join(award_failures)
                    )
                if award_eligible and not award_evidence_ready:
                    st.error(
                        "🚫 Award blocked: an authentic TDS and supporting laboratory evidence must be received and reviewed for barrier-performance claims."
                    )
                elif "unverified" in award_technical_notes.lower() or not award_candidate.compliance.has_tds_document:
                    st.warning(
                        "⚠️ Technical claims are not independently verified. Obtain authentic TDS and accredited laboratory evidence before contract award."
                    )

                st.markdown(
                    f"""
                    <div style="background-color: #F1F5F9; border: 1px solid #CBD5E1; padding: 16px; border-radius: 8px; margin-bottom: 20px;">
                        <h4 style="margin: 0 0 6px 0; color: #0F172A;">Candidate: {award_candidate.company_name}</h4>
                        <div style="display: flex; gap: 24px; flex-wrap: wrap; font-size: 14px; color: #334155;">
                            <div><b>Quotation ID</b>: <code>{award_candidate.quotation_id}</code></div>
                            <div><b>Quoted Price</b>: {award_candidate.commercial.quoted_unit_price:.4f} {award_candidate.commercial.quoted_currency}</div>
                            <div><b>Incoterms</b>: {award_candidate.commercial.incoterms.value}</div>
                            <div><b>Lead Time</b>: {award_candidate.commercial.lead_time_weeks} weeks</div>
                            <div><b>Current Status</b>: <b>{award_candidate.status.value}</b></div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                col_aw1, col_aw2 = st.columns(2)

                # Form 1: Award Acceptance
                with col_aw1:
                    st.markdown("#### 🏆 Accept Candidate for Contract Award")
                    with st.form("award_acceptance_form"):
                        signer_name = st.text_input("Authorizing Buyer Name", value=buyer_user_name)
                        award_notes = st.text_area(
                            "Mandatory Award Justification",
                            value="Candidate selected based on complete technical barrier compliance, verified TDS, and best total landed cost.",
                            help="Mandatory audit trail justification for awarding the contract."
                        )
                        confirm_manual_po = st.checkbox(
                            "I acknowledge that automated POs are disabled and manual contract execution is required.",
                            value=True
                        )
                        btn_award = st.form_submit_button("🏆 Authorize Supplier Award", type="primary", disabled=not award_ready)

                    if btn_award:
                        if not confirm_manual_po:
                            st.error("You must acknowledge the manual PO contract execution policy.")
                        else:
                            try:
                                awarded = service.accept_quotation_for_award(
                                    quotation_id=award_candidate.quotation_id,
                                    user_name=signer_name,
                                    award_notes=award_notes,
                                )
                                st.success(f"Quotation `{awarded.quotation_id}` successfully accepted for award by `{signer_name}`!")
                                st.balloons()
                                st.rerun()
                            except Exception as e:
                                st.error(f"Award sign-off failed: {e}")

                # Form 2: Rejection
                with col_aw2:
                    st.markdown("#### ❌ Reject Supplier Proposal")
                    with st.form("rejection_form"):
                        rej_signer_name = st.text_input("Reviewer Name", value=buyer_user_name, key="rej_signer")
                        rej_reason = st.text_area(
                            "Mandatory Rejection Feedback",
                            value="Proposal declined: Commercial pricing or lead time not competitive with alternative qualified candidates.",
                            help="Reason for rejecting this supplier proposal."
                        )
                        btn_reject = st.form_submit_button("❌ Formally Reject Proposal")

                    if btn_reject:
                        try:
                            rejected = service.reject_quotation(
                                quotation_id=award_candidate.quotation_id,
                                user_name=rej_signer_name,
                                rejection_reason=rej_reason,
                            )
                            st.warning(f"Quotation `{rejected.quotation_id}` transitioned to REJECTED status.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Rejection failed: {e}")

                # Complete Lifecycle History for candidate
                st.markdown("---")
                st.markdown("##### 📜 Complete Audit Trail for Candidate")
                candidate_audit = service.get_quotation_audit_trail(award_candidate.quotation_id)
                if candidate_audit:
                    st.dataframe(pd.DataFrame([
                        {
                            "Timestamp (UTC)": a.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                            "Action": a.action,
                            "From Status": a.from_status,
                            "To Status": a.to_status,
                            "Actor": a.changed_by,
                            "Sign-off Comments": a.comments,
                        }
                        for a in candidate_audit
                    ]), use_container_width=True)
