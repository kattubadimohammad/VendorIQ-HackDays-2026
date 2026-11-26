"""
VendorIQ Professional RFQ Preparation & Human Approval Workflow View.

Enables procurement buyers to:
1. Generate structured, supplier-specific RFQ packages grounded in active sourcing briefs,
   supplier profiles, and Phase 9 evidence records.
2. Review and edit commercial requirements, technical specifications, and document checklists.
3. Manage the complete 7-step RFQ lifecycle (DRAFT -> PENDING_APPROVAL -> APPROVED -> SENT -> RESPONSE_RECEIVED -> CLOSED).
4. Enforce mandatory human approval gates and governance audit trails.
5. Download corporate-grade RFQ documents in both DOCX and PDF formats.

Guarantees:
- External Contact Safeguard: Strictly zero automated emails or external communications.
- Confidentiality Protection: Zero exposure of internal scores, notes, or competitor identities.
- Truth-in-Evidence: Never claims document possession without verified supporting evidence.
"""

from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
from typing import List, Dict, Optional, Any

import pandas as pd
import streamlit as st

from core.models import (
    SupplierProfile,
    SupplierScore,
    SourcingBrief,
    RFQStatus,
    RFQAuditEntry,
    RFQDocumentRecord,
    ShortlistStatus,
)
from core.exceptions import DataValidationError
from ui.data_service import DashboardDataService
from ui.components import render_view_header, render_next_step_guidance


def render_rfq_view(
    service: DashboardDataService,
    brief: SourcingBrief,
    suppliers: List[SupplierProfile],
    evaluations: List[SupplierScore],
):
    """Render the professional RFQ generation and governance management view."""
    render_view_header(
        title="📝 Professional RFQ Generation & Governance Workflow",
        subtitle="Generate evidence-grounded, supplier-specific RFQ packages with mandatory human approval gates, comprehensive audit trails, and exportable DOCX and PDF documents.",
        stage_key="rfq",
    )

    # Top Safeguard Banner
    st.info(
        "🔒 **Procurement Integrity & Safeguard Gate**: "
        "All RFQs created in VendorIQ are stored as local procurement packages in SQLite. "
        "The platform **strictly does not transmit unsolicited emails, place phone calls, or contact suppliers automatically**. "
        "Transmission requires explicit human sign-off confirming external manual dispatch. "
        "Internal supplier scores, rankings, and competitor identities are strictly confidential and never exposed."
    )

    # Sourcing Brief Selector if multiple exist
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
            help="Select the sourcing brief to associate with RFQ generation.",
        )
        selected_brief_id = selected_brief_label.split(" – ")[0]
        active_brief = brief_map.get(selected_brief_id, brief)
    with col_b2:
        st.metric("Buyer Organization", active_brief.buyer_organization or "Not set")

    # Fetch RFQ KPIs for active brief
    kpis = service.get_rfq_kpis(brief_id=active_brief.brief_id)
    k_col1, k_col2, k_col3, k_col4, k_col5, k_col6 = st.columns(6)
    k_col1.metric("Total RFQs", kpis["total"])
    k_col2.metric("Drafts", kpis["draft"])
    k_col3.metric("Pending Approval", kpis["pending_approval"])
    k_col4.metric("Approved", kpis["approved"])
    k_col5.metric("Sent (Manual)", kpis["sent"])
    k_col6.metric("Responses Recv", kpis["response_received"])

    st.markdown("---")

    # 4 Main Tabs
    tab_create, tab_approve, tab_documents, tab_analytics = st.tabs([
        "📋 RFQ Preparation & Generator",
        "⚖️ Review & Human Approval Gate",
        "📑 Document Downloads (DOCX/PDF)",
        "📊 Pipeline Analytics & Audit Trail",
    ])

    # -------------------------------------------------------------------------
    # TAB 1: RFQ Preparation & Generator
    # -------------------------------------------------------------------------
    with tab_create:
        st.subheader("Prepare Supplier-Specific RFQ Package")
        st.write(
            "Select candidates from a curated Shortlist or pick individual evaluated suppliers. "
            "VendorIQ will automatically synthesize a customized RFQ based on the supplier's evidence profile."
        )

        # Source Selection (Shortlist vs Individual)
        shortlists = service.list_shortlists_for_brief(active_brief.brief_id)
        shortlist_dict = {f"{s.name} ({len(s.items)} candidates)": s.shortlist_id for s in shortlists}

        prep_source = st.radio(
            "Candidate Selection Method",
            options=["From Existing Supplier Shortlist", "Select Individual Evaluated Suppliers"],
            horizontal=True,
        )

        selected_suppliers: List[SupplierProfile] = []
        originating_shortlist_id: Optional[str] = None
        supp_by_id = {s.supplier_id: s for s in suppliers}

        if prep_source == "From Existing Supplier Shortlist":
            if not shortlists:
                st.warning("No saved supplier shortlists found for this project. Please create a shortlist in the Supplier Discovery tab, or select individual candidates below.")
            else:
                chosen_sl_label = st.selectbox("Select Shortlist", options=list(shortlist_dict.keys()))
                originating_shortlist_id = shortlist_dict[chosen_sl_label]
                sl_items = service.repository.list_items_for_shortlist(originating_shortlist_id)
                for it in sl_items:
                    if it.supplier_id in supp_by_id:
                        selected_suppliers.append(supp_by_id[it.supplier_id])

                st.info(f"Loaded **{len(selected_suppliers)}** supplier candidates from shortlist: `{chosen_sl_label}`.")
        else:
            supplier_names = [f"{s.company_name} ({s.domain})" for s in suppliers]
            name_to_supp = {f"{s.company_name} ({s.domain})": s for s in suppliers}
            picked_names = st.multiselect(
                "Select Candidate Suppliers",
                options=supplier_names,
                default=supplier_names[:2] if len(supplier_names) >= 2 else supplier_names,
                help="Choose evaluated candidates to prepare supplier-specific RFQs for.",
            )
            selected_suppliers = [name_to_supp[n] for n in picked_names]

        if selected_suppliers:
            st.markdown("#### ⚙️ Procurement Specifications & Commercial Terms")

            with st.form("rfq_generation_form"):
                col_b_info1, col_b_info2 = st.columns(2)
                with col_b_info1:
                    buyer_org_input = st.text_input("Buyer Entity Name", value=active_brief.buyer_organization or "")
                    procurement_lead = st.text_input("Procurement Officer / Contact", value="")
                with col_b_info2:
                    contact_inbox = st.text_input("Official Inquiries / Bids Inbox", value="", placeholder="Enter a buyer-controlled business inbox; do not use a placeholder address")
                    deadline_days = st.number_input("Response Deadline (Days from Today)", min_value=7, max_value=90, value=None, step=7, placeholder="Enter buyer-approved deadline")
                    if deadline_days is not None:
                        calc_deadline = datetime.now(timezone.utc) + timedelta(days=int(deadline_days))
                        st.caption(f"Calculated Submission Deadline: **{calc_deadline.strftime('%Y-%m-%d %H:%M UTC')}**")

                st.markdown("---")
                col_p1, col_p2 = st.columns(2)
                with col_p1:
                    target_volume = st.text_input("Expected Quantity / Target Volume", value="", placeholder="Enter the buyer-approved quantity and unit")
                    product_desc = st.text_area(
                        "Product Scope & Description",
                        value=active_brief.description or "",
                        height=100,
                    )
                with col_p2:
                    barrier_req = st.text_input("Barrier Film Requirements", value="", placeholder="Enter approved technical thresholds and test methods")
                    packaging_mode = st.text_input("Packaging & Shipping Requirements", value="", placeholder="Enter approved packaging and shipping requirements")

                st.markdown("---")
                col_c1, col_c2, col_c3 = st.columns(3)
                with col_c1:
                    currency = st.selectbox("Quotation Currency", options=["USD", "INR", "EUR", "GBP"], index=None, placeholder="Select buyer-approved currency")
                    target_price = st.number_input("Target Unit Price (Optional)", min_value=0.0, value=None, step=0.10, placeholder="Optional")
                with col_c2:
                    moq_req = st.number_input("Requested Minimum Order Qty (MOQ)", min_value=0, value=0, step=500)
                    lead_time_wks = st.number_input("Max Production Lead Time (Weeks)", min_value=1, value=None, step=1, help="Optional; enter only a buyer-approved maximum.")
                with col_c3:
                    incoterms = st.selectbox("Incoterms Baseline (Optional)", options=["FOB", "CIF", "DDP", "EXW"], index=None, placeholder="Select only if buyer-approved")
                    validity_days = st.number_input("Quotation Price Validity (Days)", min_value=1, value=None, step=1, help="Optional; enter only a buyer-approved validity period.")

                col_terms1, col_terms2 = st.columns(2)
                with col_terms1:
                    payment_terms = st.text_input("Payment Terms", value="", placeholder="Enter buyer-approved payment terms")
                with col_terms2:
                    delivery_hub = st.text_input("Fulfillment Delivery Location", value="", placeholder="Enter the confirmed delivery location")

                st.markdown("---")
                st.markdown("#### 🛡️ Truth-in-Evidence & Confidentiality Certification")
                st.caption(
                    "VendorIQ automatically generates individualized questionnaires and evidence checklists "
                    "tailored to available supplier records and clearly identified evidence gaps. Internal evaluation scores are never included."
                )

                gate_acknowledged = st.checkbox(
                    "I certify that these RFQ terms have been reviewed. I understand this action creates local draft "
                    "records in SQLite only and does NOT transmit emails or contact suppliers.",
                    value=False,
                )

                generate_submitted = st.form_submit_button("Generate & Persist Supplier-Specific RFQ(s)")

                if generate_submitted:
                    missing_required = []
                    if not buyer_org_input.strip():
                        missing_required.append("Buyer Entity Name")
                    if not procurement_lead.strip():
                        missing_required.append("Procurement Officer / Contact")
                    if not target_volume.strip():
                        missing_required.append("Expected Quantity / Target Volume")
                    if not product_desc.strip():
                        missing_required.append("Product Scope & Description")
                    if deadline_days is None:
                        missing_required.append("Response Deadline")
                    if currency is None:
                        missing_required.append("Quotation Currency")
                    if not gate_acknowledged:
                        st.error("Human Review Gate: You must acknowledge the safeguard checkbox before generating RFQs.")
                    elif not selected_suppliers:
                        st.error("Please select at least 1 supplier candidate.")
                    elif missing_required:
                        st.error("Complete the buyer-approved required fields: " + ", ".join(missing_required))
                    else:
                        created_count = 0
                        for supp in selected_suppliers:
                            service.create_and_persist_supplier_rfq(
                                brief=active_brief,
                                supplier=supp,
                                shortlist_id=originating_shortlist_id,
                                expected_quantity=target_volume.strip(),
                                buyer_organization=buyer_org_input.strip(),
                                contact_person=procurement_lead.strip(),
                                contact_email=contact_inbox.strip(),
                                deadline_days=deadline_days,
                                target_unit_price=target_price if target_price is not None else None,
                                currency=currency,
                                product_description=product_desc.strip(),
                                barrier_film_requirements=barrier_req.strip() or None,
                                packaging_requirements=packaging_mode.strip() or None,
                                min_order_quantity=moq_req if moq_req > 0 else None,
                                production_lead_time_weeks=lead_time_wks if lead_time_wks is not None else None,
                                payment_terms=payment_terms.strip() or None,
                                incoterms=incoterms.strip() if incoterms else None,
                                delivery_location=delivery_hub.strip() or None,
                                quotation_validity_days=validity_days,
                            )
                            created_count += 1

                        st.success(f"Successfully generated and persisted {created_count} supplier-specific RFQ drafts to SQLite!")
                        st.rerun()

    # -------------------------------------------------------------------------
    # TAB 2: Review & Human Approval Gate
    # -------------------------------------------------------------------------
    with tab_approve:
        st.subheader("Human Review & Approval Workflow")
        st.write(
            "Every RFQ must progress through human review before authorization. "
            "Approve, request revisions, or confirm manual external dispatch."
        )

        all_rfqs = service.list_supplier_rfqs(brief_id=active_brief.brief_id)

        if not all_rfqs:
            st.info("No RFQs have been created for this sourcing brief yet. Use Tab 1 to generate RFQ packages.")
        else:
            col_f1, col_f2 = st.columns([2, 1])
            with col_f1:
                status_filter = st.selectbox(
                    "Filter by Lifecycle Status",
                    options=["ALL"] + [s.value for s in RFQStatus],
                    index=0,
                )
            with col_f2:
                st.caption(f"Showing **{len(all_rfqs)}** total RFQ record(s)")

            filtered_rfqs = all_rfqs if status_filter == "ALL" else [r for r in all_rfqs if r.status.value == status_filter]

            for rfq in filtered_rfqs:
                with st.expander(f"📦 {rfq.title} [{rfq.status.value}] – Ref: {rfq.buyer_info.rfq_reference}", expanded=(rfq.status == RFQStatus.PENDING_APPROVAL)):
                    c_card1, c_card2, c_card3 = st.columns(3)
                    c_card1.write(f"**Supplier:** {rfq.supplier.company_name}")
                    c_card1.write(f"**Domain:** `{rfq.supplier.domain}`")
                    c_card2.write(f"**Quantity:** {rfq.product_requirements.expected_quantity or 'Not specified'}")
                    c_card2.write(f"**Deadline:** {rfq.buyer_info.response_deadline.strftime('%Y-%m-%d')}")
                    c_card3.write(f"**Status:** `{rfq.status.value}`")
                    c_card3.write(f"**Created:** {rfq.created_at.strftime('%Y-%m-%d %H:%M')}")

                    if rfq.supplier.domain_mismatch_warning:
                        st.warning(f"⚠️ **Domain Warning**: {rfq.supplier.domain_mismatch_warning}")

                    # Detailed content breakdown
                    d_col1, d_col2 = st.columns(2)
                    with d_col1:
                        st.markdown("**Tailored Questionnaire & Clarifications:**")
                        for idx, q in enumerate(rfq.supplier.technical_questionnaire, 1):
                            st.write(f"{idx}. {q}")
                    with d_col2:
                        st.markdown("**Missing Evidence Requests:**")
                        if rfq.supplier.missing_evidence_requests:
                            for m in rfq.supplier.missing_evidence_requests:
                                st.write(f"• {m}")
                        else:
                            st.caption("No missing technical evidence flagged.")

                    # Audit History summary
                    st.markdown("**Recent Governance History:**")
                    if rfq.audit_trail:
                        for a in rfq.audit_trail[-3:]:
                            st.caption(f"• **{a.to_status}** by {a.changed_by} at {a.timestamp.strftime('%Y-%m-%d %H:%M UTC')}: {a.comments or 'N/A'}")

                    st.markdown("---")

                    # Human Action Gates based on Status
                    if rfq.status == RFQStatus.DRAFT:
                        st.markdown("##### 🚀 Action: Submit for Formal Review")
                        with st.form(f"submit_form_{rfq.rfq_id}"):
                            sub_name = st.text_input("Your Name / Procurement Role", value="", placeholder="Enter your name or role", key=f"sub_name_{rfq.rfq_id}")
                            sub_notes = st.text_input("Submission Notes", value="", placeholder="Enter submission notes", key=f"sub_notes_{rfq.rfq_id}")
                            if st.form_submit_button("Submit for Approval"):
                                service.submit_rfq_for_approval(rfq.rfq_id, sub_name, sub_notes)
                                st.success("RFQ submitted for formal approval!")
                                st.rerun()

                    elif rfq.status == RFQStatus.PENDING_APPROVAL:
                        st.markdown("##### ⚖️ Action: Human Reviewer Sign-Off Gate")
                        col_app, col_rej = st.columns(2)
                        with col_app:
                            with st.form(f"approve_form_{rfq.rfq_id}"):
                                reviewer_name = st.text_input("Reviewer Name", value="", placeholder="Enter reviewer name", key=f"rev_name_{rfq.rfq_id}")
                                app_notes = st.text_area("Approval Authorization Notes", value="", placeholder="Record the actual review and authorization basis", key=f"app_notes_{rfq.rfq_id}")
                                if st.form_submit_button("✅ Approve RFQ & Generate Documents"):
                                    try:
                                        service.approve_rfq(rfq.rfq_id, reviewer_name, app_notes)
                                        st.success(f"RFQ `{rfq.rfq_id}` APPROVED! Official DOCX & PDF generated.")
                                        st.rerun()
                                    except DataValidationError as e:
                                        st.error(str(e))

                        with col_rej:
                            with st.form(f"reject_form_{rfq.rfq_id}"):
                                rej_name = st.text_input("Reviewer Name", value="", placeholder="Enter reviewer name", key=f"rej_name_{rfq.rfq_id}")
                                rej_notes = st.text_area("Rejection Feedback / Reasons", value="", placeholder="Enter actual rejection feedback", key=f"rej_notes_{rfq.rfq_id}")
                                if st.form_submit_button("❌ Reject RFQ (Request Revisions)"):
                                    try:
                                        service.reject_rfq(rfq.rfq_id, rej_name, rej_notes)
                                        st.warning(f"RFQ `{rfq.rfq_id}` REJECTED. Sent back to buyer for revision.")
                                        st.rerun()
                                    except DataValidationError as e:
                                        st.error(str(e))

                    elif rfq.status == RFQStatus.REJECTED:
                        st.markdown("##### 🔄 Action: Revise & Return to Draft")
                        with st.form(f"revise_form_{rfq.rfq_id}"):
                            rev_user = st.text_input("Your Name", value="", placeholder="Enter your name", key=f"rev_user_{rfq.rfq_id}")
                            rev_comments = st.text_input("Revision Notes", value="", placeholder="Enter actual revision notes", key=f"rev_comm_{rfq.rfq_id}")
                            if st.form_submit_button("Revert to DRAFT for Editing"):
                                service.revise_rfq(rfq.rfq_id, rev_user, rev_comments)
                                st.info("RFQ reverted to DRAFT. You may now update its details.")
                                st.rerun()

                    elif rfq.status == RFQStatus.APPROVED:
                        st.markdown("##### 📤 Action: Confirm Manual External Dispatch")
                        st.info("Remember: VendorIQ **never** transmits automated emails. Once you dispatch the RFQ via your company's corporate email or procurement portal, log the confirmation below.")
                        with st.form(f"dispatch_form_{rfq.rfq_id}"):
                            disp_user = st.text_input("Dispatcher Name", value="", placeholder="Enter dispatcher name", key=f"disp_user_{rfq.rfq_id}")
                            disp_notes = st.text_area(
                                "Transmission Confirmation Notes",
                                value="",
                                key=f"disp_notes_{rfq.rfq_id}",
                            )
                            if st.form_submit_button("Confirm Manual Dispatch (Mark as SENT)"):
                                try:
                                    service.record_manual_dispatch(rfq.rfq_id, disp_user, disp_notes)
                                    st.success(f"RFQ `{rfq.rfq_id}` marked as SENT.")
                                    st.rerun()
                                except DataValidationError as e:
                                    st.error(str(e))

                    elif rfq.status == RFQStatus.SENT:
                        st.markdown("##### 📥 Action: Record Supplier Response")
                        with st.form(f"response_form_{rfq.rfq_id}"):
                            resp_user = st.text_input("Receiver Name", value="", placeholder="Enter receiver name", key=f"resp_user_{rfq.rfq_id}")
                            resp_summary = st.text_area(
                                "Supplier Response Summary",
                                value="", placeholder="Summarize the response actually received",
                                key=f"resp_summary_{rfq.rfq_id}",
                            )
                            if st.form_submit_button("Record Response Received"):
                                try:
                                    service.record_response_received(rfq.rfq_id, resp_user, resp_summary)
                                    st.success(f"Recorded supplier response for RFQ `{rfq.rfq_id}`.")
                                    st.rerun()
                                except DataValidationError as e:
                                    st.error(str(e))

                    elif rfq.status in (RFQStatus.RESPONSE_RECEIVED, RFQStatus.CLOSED):
                        if rfq.status == RFQStatus.RESPONSE_RECEIVED:
                            with st.form(f"close_form_{rfq.rfq_id}"):
                                close_user = st.text_input("Closing Official", value="", placeholder="Enter closing official", key=f"close_user_{rfq.rfq_id}")
                                close_reason = st.text_input("Resolution Summary", value="", placeholder="Enter actual resolution summary", key=f"close_reason_{rfq.rfq_id}")
                                if st.form_submit_button("Close RFQ"):
                                    service.close_supplier_rfq(rfq.rfq_id, close_user, close_reason)
                                    st.success("RFQ successfully marked as CLOSED.")
                                    st.rerun()
                        else:
                            st.caption("✅ This RFQ cycle is officially CLOSED.")

    # -------------------------------------------------------------------------
    # TAB 3: Document Downloads (DOCX/PDF)
    # -------------------------------------------------------------------------
    with tab_documents:
        st.subheader("Official Procurement Document Packages")
        st.write("Download finalized, corporate-formatted RFQ packages in Microsoft Word (DOCX) or Adobe PDF format.")

        rfqs_with_docs = [r for r in all_rfqs if r.docx_path and r.pdf_path and os.path.exists(r.docx_path) and os.path.exists(r.pdf_path)]

        if not rfqs_with_docs:
            st.info("No compiled document packages found yet. Documents are automatically generated upon approval in Tab 2, or you can generate them on demand below.")

        for rfq in all_rfqs:
            with st.container():
                st.markdown(f"#### 📄 {rfq.title}")
                c_d1, c_d2, c_d3, c_d4 = st.columns([2, 1, 1, 1])

                with c_d1:
                    st.write(f"**Ref:** `{rfq.buyer_info.rfq_reference}` | **Status:** `{rfq.status.value}` | **Supplier:** {rfq.supplier.company_name}")

                with c_d2:
                    if st.button("🔄 Generate / Refresh Docs", key=f"regen_{rfq.rfq_id}"):
                        updated_rfq = service.generate_rfq_documents(rfq.rfq_id)
                        st.success("Documents compiled successfully!")
                        st.rerun()

                with c_d3:
                    if rfq.docx_path and os.path.exists(rfq.docx_path):
                        with open(rfq.docx_path, "rb") as f_docx:
                            st.download_button(
                                label="📥 Download DOCX",
                                data=f_docx.read(),
                                file_name=f"{rfq.rfq_id}.docx",
                                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                key=f"dl_docx_{rfq.rfq_id}",
                                width="stretch",
                            )
                    else:
                        st.caption("DOCX pending generation")

                with c_d4:
                    if rfq.pdf_path and os.path.exists(rfq.pdf_path):
                        with open(rfq.pdf_path, "rb") as f_pdf:
                            st.download_button(
                                label="📥 Download PDF",
                                data=f_pdf.read(),
                                file_name=f"{rfq.rfq_id}.pdf",
                                mime="application/pdf",
                                key=f"dl_pdf_{rfq.rfq_id}",
                                width="stretch",
                            )
                    else:
                        st.caption("PDF pending generation")

                st.markdown("---")

    # -------------------------------------------------------------------------
    # TAB 4: Pipeline Analytics & Audit Trail
    # -------------------------------------------------------------------------
    with tab_analytics:
        st.subheader("RFQ Pipeline Overview & Governance Audit Trail")

        if not all_rfqs:
            st.info("No RFQ data available for analytics yet.")
        else:
            rfq_table_data = []
            for r in all_rfqs:
                latest_audit = r.audit_trail[-1] if r.audit_trail else None
                rfq_table_data.append({
                    "RFQ ID": r.rfq_id,
                    "Reference": r.buyer_info.rfq_reference,
                    "Supplier": r.supplier.company_name,
                    "Domain": r.supplier.domain,
                    "Status": r.status.value,
                    "Target Qty": r.product_requirements.expected_quantity or "Not specified",
                    "Deadline": r.buyer_info.response_deadline.strftime("%Y-%m-%d"),
                    "Last Actor": latest_audit.changed_by if latest_audit else "N/A",
                    "Updated At": r.updated_at.strftime("%Y-%m-%d %H:%M UTC"),
                })
            df_rfqs = pd.DataFrame(rfq_table_data)
            st.dataframe(df_rfqs, width="stretch")

            st.markdown("---")
            st.markdown("#### 📜 Full Audit Trail Inspector")
            inspected_rfq_id = st.selectbox(
                "Select RFQ to Inspect Complete Audit Trail",
                options=[r.rfq_id for r in all_rfqs],
                format_func=lambda x: f"{x} – {[r.supplier.company_name for r in all_rfqs if r.rfq_id == x][0]}",
            )
            audit_entries = service.get_rfq_audit_trail(inspected_rfq_id)
            if audit_entries:
                audit_records = [
                    {
                        "Audit ID": a.audit_id,
                        "From Status": a.from_status or "None",
                        "To Status": a.to_status,
                        "Changed By": a.changed_by,
                        "Reviewer Comments": a.comments or "",
                        "Timestamp": a.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC"),
                    }
                    for a in audit_entries
                ]
                st.table(pd.DataFrame(audit_records))
            else:
                st.info("No audit trail entries found.")

    st.markdown("---")
    render_next_step_guidance(
        next_view_name="💰 Quotations & Analysis",
        guidance_text="Ingest incoming supplier proposals (PDF/XLSX), review extracted parameters, compare normalized landed costs, and authorize human award sign-offs.",
    )
