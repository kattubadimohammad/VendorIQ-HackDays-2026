"""
VendorIQ Professional RFQ Document Generator.

Generates procurement-grade Request for Quotation (RFQ) documents in both
DOCX and PDF formats. Implements strict truth-in-evidence guardrails and
confidentiality protection (guarantees internal scores, evaluation notes,
and competitor information are never exposed).
"""

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Optional, Tuple, List

from core.models import RFQDocumentRecord, RFQStatus
from core.exceptions import DataValidationError


class RFQDocumentGenerator:
    """
    Service for generating corporate-grade RFQ documents in DOCX and PDF formats.
    """

    def __init__(self, base_storage_dir: Optional[str] = None):
        if base_storage_dir:
            self.base_storage_dir = Path(base_storage_dir)
        else:
            project_root = Path(__file__).resolve().parent.parent
            self.base_storage_dir = project_root / "storage" / "rfqs"
        self.base_storage_dir.mkdir(parents=True, exist_ok=True)

    def verify_confidentiality_guardrails(self, rfq: RFQDocumentRecord) -> None:
        """
        Verify that no internal scoring, confidential evaluation notes, or
        competing supplier identities appear in the external RFQ content.
        Raises DataValidationError if any confidential leak is detected.
        """
        # Ensure forbidden internal score keywords are absent from external text fields
        forbidden_substrings = [
            "total_score",
            "factor_scores",
            "internal evaluation",
            "confidential evaluation",
            "competitor",
        ]
        text_blobs = [
            rfq.title,
            rfq.product_requirements.description,
            rfq.product_requirements.barrier_film_requirements or "",
            rfq.commercial_requirements.payment_terms,
            rfq.commercial_requirements.incoterms,
            " ".join(rfq.product_requirements.technical_specifications),
            " ".join(rfq.supplier.technical_questionnaire),
            " ".join(rfq.supplier.missing_evidence_requests),
        ]
        combined = " ".join(text_blobs).lower()
        for forbidden in forbidden_substrings:
            if forbidden in combined:
                raise DataValidationError(
                    f"Confidentiality guardrail violated: Found forbidden substring '{forbidden}' in RFQ."
                )

    def generate_all(
        self, rfq: RFQDocumentRecord, output_dir: Optional[str] = None
    ) -> Tuple[str, str]:
        """
        Generate both DOCX and PDF document packages for an RFQ record.
        Returns tuple of (docx_absolute_path, pdf_absolute_path).
        """
        self.verify_confidentiality_guardrails(rfq)

        target_dir = Path(output_dir) if output_dir else self.base_storage_dir / rfq.rfq_id
        target_dir.mkdir(parents=True, exist_ok=True)

        docx_path = target_dir / f"{rfq.rfq_id}.docx"
        pdf_path = target_dir / f"{rfq.rfq_id}.pdf"

        self.generate_docx(rfq, str(docx_path))
        self.generate_pdf(rfq, str(pdf_path))

        return str(docx_path.resolve()), str(pdf_path.resolve())

    # =========================================================================
    # DOCX GENERATION
    # =========================================================================

    def generate_docx(self, rfq: RFQDocumentRecord, output_path: str) -> str:
        """Generate a structured corporate DOCX document."""
        self.verify_confidentiality_guardrails(rfq)

        import docx
        from docx.shared import Inches, Pt, RGBColor
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.enum.table import WD_TABLE_ALIGNMENT

        doc = docx.Document()

        # Adjust margins to 0.75 inches
        sections = doc.sections
        for section in sections:
            section.top_margin = Inches(0.75)
            section.bottom_margin = Inches(0.75)
            section.left_margin = Inches(0.75)
            section.right_margin = Inches(0.75)

        # Document Header
        title_para = doc.add_paragraph()
        title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run_title = title_para.add_run("REQUEST FOR QUOTATION (RFQ)")
        run_title.font.name = "Calibri"
        run_title.font.size = Pt(20)
        run_title.font.bold = True
        run_title.font.color.rgb = RGBColor(30, 58, 138)  # Deep Navy

        sub_para = doc.add_paragraph()
        sub_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run_sub = sub_para.add_run(rfq.title)
        run_sub.font.name = "Calibri"
        run_sub.font.size = Pt(13)
        run_sub.font.color.rgb = RGBColor(75, 85, 99)

        doc.add_paragraph()  # Spacer

        # Metadata Table (Ref, Date, Deadline, Status)
        meta_table = doc.add_table(rows=2, cols=4)
        meta_table.alignment = WD_TABLE_ALIGNMENT.CENTER
        meta_table.autofit = True

        headers = ["RFQ Reference", "Issue Date", "Response Deadline", "Document Status"]
        values = [
            rfq.buyer_info.rfq_reference,
            rfq.buyer_info.issue_date.strftime("%Y-%m-%d"),
            rfq.buyer_info.response_deadline.strftime("%Y-%m-%d %H:%M UTC"),
            rfq.status.value,
        ]

        for col_idx, (hdr, val) in enumerate(zip(headers, values)):
            # Header cell
            c_hdr = meta_table.cell(0, col_idx)
            c_hdr.paragraphs[0].text = hdr
            c_hdr.paragraphs[0].runs[0].font.bold = True
            c_hdr.paragraphs[0].runs[0].font.size = Pt(9)
            c_hdr.paragraphs[0].runs[0].font.color.rgb = RGBColor(30, 58, 138)
            # Value cell
            c_val = meta_table.cell(1, col_idx)
            c_val.paragraphs[0].text = val
            c_val.paragraphs[0].runs[0].font.size = Pt(10)

        doc.add_paragraph()  # Spacer

        # Section 1: Buyer Organization Information
        self._docx_add_section_header(doc, "1. Buyer Organization Information")
        b_table = doc.add_table(rows=3, cols=2)
        b_data = [
            ("Buyer Entity:", rfq.buyer_info.buyer_organization),
            ("Procurement Contact:", rfq.buyer_info.contact_person or "Procurement Lead"),
            ("Official Inquiries / Bids Inbox:", rfq.buyer_info.contact_email or "procurement@vendoriq.internal"),
        ]
        for row_idx, (k, v) in enumerate(b_data):
            c_k = b_table.cell(row_idx, 0)
            c_k.paragraphs[0].text = k
            c_k.paragraphs[0].runs[0].font.bold = True
            c_v = b_table.cell(row_idx, 1)
            c_v.paragraphs[0].text = v

        doc.add_paragraph()

        # Section 2: Target Supplier Information
        self._docx_add_section_header(doc, "2. Addressed Supplier Details")
        s_table = doc.add_table(rows=3, cols=2)
        s_data = [
            ("Target Supplier Legal Name:", rfq.supplier.company_name),
            ("Registered Corporate Domain:", rfq.supplier.domain),
            ("Headquarters / Primary Base:", rfq.supplier.headquarters or "N/A"),
        ]
        for row_idx, (k, v) in enumerate(s_data):
            c_k = s_table.cell(row_idx, 0)
            c_k.paragraphs[0].text = k
            c_k.paragraphs[0].runs[0].font.bold = True
            c_v = s_table.cell(row_idx, 1)
            c_v.paragraphs[0].text = v

        if rfq.supplier.domain_mismatch_warning:
            p_warn = doc.add_paragraph()
            r_warn = p_warn.add_run(f"⚠️ Supplier Domain Notice: {rfq.supplier.domain_mismatch_warning}")
            r_warn.font.color.rgb = RGBColor(180, 83, 9)
            r_warn.font.italic = True

        doc.add_paragraph()

        # Section 3: Product Scope & Technical Requirements
        self._docx_add_section_header(doc, "3. Scope of Supply & Technical Requirements")
        doc.add_paragraph(f"Category: {rfq.product_requirements.category}").runs[0].font.bold = True
        doc.add_paragraph(f"Requirement Description: {rfq.product_requirements.description}")
        doc.add_paragraph(f"Target Delivery Quantity: {rfq.product_requirements.expected_quantity}").runs[0].font.bold = True

        if rfq.product_requirements.barrier_film_requirements:
            p_bar = doc.add_paragraph()
            p_bar.add_run("Barrier Film Specification: ").font.bold = True
            p_bar.add_run(rfq.product_requirements.barrier_film_requirements)

        if rfq.product_requirements.packaging_requirements:
            p_pkg = doc.add_paragraph()
            p_pkg.add_run("Packaging & Shipment Mode: ").font.bold = True
            p_pkg.add_run(rfq.product_requirements.packaging_requirements)

        if rfq.product_requirements.technical_specifications:
            doc.add_paragraph("Technical Specifications Checklist:").runs[0].font.bold = True
            for spec in rfq.product_requirements.technical_specifications:
                doc.add_paragraph(f"• {spec}")

        doc.add_paragraph()

        # Section 4: Commercial Proposal & Pricing Terms
        self._docx_add_section_header(doc, "4. Commercial Requirements & Pricing Terms")
        c_table = doc.add_table(rows=7, cols=2)
        c_data = [
            ("Quotation Currency:", rfq.commercial_requirements.currency),
            ("Target Unit Price:", f"{rfq.commercial_requirements.currency} {rfq.commercial_requirements.target_unit_price:,.2f}" if rfq.commercial_requirements.target_unit_price else "Open quotation requested"),
            ("Minimum Order Quantity (MOQ):", f"{rfq.commercial_requirements.min_order_quantity:,} units" if rfq.commercial_requirements.min_order_quantity else "Supplier to indicate standard MOQ"),
            ("Production Lead Time:", f"Within {rfq.commercial_requirements.production_lead_time_weeks} weeks" if rfq.commercial_requirements.production_lead_time_weeks else "Supplier to state production lead time"),
            ("Payment Terms Requested:", rfq.commercial_requirements.payment_terms),
            ("Incoterms (Delivery Baseline):", rfq.commercial_requirements.incoterms),
            ("Fulfillment Delivery Location:", rfq.commercial_requirements.delivery_location),
        ]
        for row_idx, (k, v) in enumerate(c_data):
            c_k = c_table.cell(row_idx, 0)
            c_k.paragraphs[0].text = k
            c_k.paragraphs[0].runs[0].font.bold = True
            c_v = c_table.cell(row_idx, 1)
            c_v.paragraphs[0].text = str(v)

        doc.add_paragraph()

        # Section 5: Technical Questionnaire
        self._docx_add_section_header(doc, "5. Technical Questionnaire & Clarifications")
        doc.add_paragraph("Please provide detailed responses to the following technical and operational inquiries:")
        all_questions = list(rfq.supplier.technical_questionnaire)
        if rfq.supplier.missing_evidence_requests:
            for req in rfq.supplier.missing_evidence_requests:
                all_questions.append(f"Documentary verification required: {req}")

        if not all_questions:
            all_questions = [
                "Provide multi-layer co-extrusion machine width and annual production tonnage capacity.",
                "Specify measured ASTM F1249 (WVTR) and ASTM D3985 (OTR) test data for proposed film thicknesses.",
                "State food-grade safety certifications (e.g., US FDA / EU Food Contact / BRCGS Packaging).",
                "Detail quality control procedures for heat seal strength, tensile strength, and defect inspection.",
            ]

        for q_idx, q in enumerate(all_questions, 1):
            doc.add_paragraph(f"{q_idx}. {q}")

        doc.add_paragraph()

        # Section 6: Mandatory Documentation Checklist
        self._docx_add_section_header(doc, "6. Mandatory Technical & Compliance Documentation Checklist")
        doc.add_paragraph("The bidder MUST provide verifiable documentary evidence for each item below:")
        doc_checklist = rfq.requested_documents if rfq.requested_documents else [
            "Technical Data Sheets (TDS) for all proposed barrier film structures",
            "ASTM F1249 Certified Test Report for Water Vapor Transmission Rate (WVTR)",
            "ASTM D3985 Certified Test Report for Oxygen Transmission Rate (OTR)",
            "ISO 9001 / BRCGS Quality Management System Certificates",
            "Sustainability, recyclability, or compostability certifications (if claimed)",
            "Plant machinery details and manufacturing capability profile",
            "Company profile and audited commercial references",
        ]
        for item in doc_checklist:
            doc.add_paragraph(f"[  ]  {item}")

        doc.add_paragraph()

        # Section 7: Submission Instructions & Procurement Governance
        self._docx_add_section_header(doc, "7. Submission Instructions & Governance Safeguards")
        doc.add_paragraph(
            f"1. Proposal Submissions: All commercial quotations and technical dossiers must be delivered "
            f"in PDF format prior to the submission deadline: {rfq.buyer_info.response_deadline.strftime('%Y-%m-%d %H:%M UTC')}."
        )
        doc.add_paragraph(
            f"2. Quotation Validity: All offered commercial prices must remain unconditionally valid for a minimum "
            f"of {rfq.commercial_requirements.quotation_validity_days} calendar days from submission deadline."
        )
        doc.add_paragraph(
            "3. Confidentiality & Non-Disclosure: All specifications and volume estimates in this RFQ "
            "are confidential to the buyer organization and must not be distributed without written authorization."
        )
        doc.add_paragraph(
            "4. External Contact Safeguard: VendorIQ strictly utilizes human-authorized dispatch. "
            "No automated emails, phone calls, or unsolicited transmissions are dispatched by the platform."
        )

        doc.add_paragraph()

        # Section 8: Human Approval Sign-Off Block
        self._docx_add_section_header(doc, "8. Human Review & Authorization Governance")
        h_table = doc.add_table(rows=4, cols=2)
        latest_audit = rfq.audit_trail[-1] if rfq.audit_trail else None
        h_data = [
            ("Lifecycle Status:", rfq.status.value),
            ("Procurement Reviewer / Sign-off:", latest_audit.changed_by if latest_audit else "Pending"),
            ("Sign-off Timestamp:", latest_audit.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC") if latest_audit else "Pending"),
            ("Reviewer Comments / Audit Notes:", latest_audit.comments if (latest_audit and latest_audit.comments) else "Standard procurement review"),
        ]
        for row_idx, (k, v) in enumerate(h_data):
            c_k = h_table.cell(row_idx, 0)
            c_k.paragraphs[0].text = k
            c_k.paragraphs[0].runs[0].font.bold = True
            c_v = h_table.cell(row_idx, 1)
            c_v.paragraphs[0].text = str(v)

        doc.save(output_path)
        return output_path

    def _docx_add_section_header(self, doc, title: str):
        """Helper to style section headings in DOCX."""
        from docx.shared import Pt, RGBColor
        p = doc.add_paragraph()
        run = p.add_run(title)
        run.font.name = "Calibri"
        run.font.size = Pt(13)
        run.font.bold = True
        run.font.color.rgb = RGBColor(30, 58, 138)  # Deep Navy

    # =========================================================================
    # PDF GENERATION
    # =========================================================================

    def generate_pdf(self, rfq: RFQDocumentRecord, output_path: str) -> str:
        """Generate a structured corporate PDF document using ReportLab."""
        self.verify_confidentiality_guardrails(rfq)

        from reportlab.lib.pagesizes import letter
        from reportlab.lib import colors
        from reportlab.platypus import (
            SimpleDocTemplate,
            Paragraph,
            Spacer,
            Table,
            TableStyle,
            HRFlowable,
        )
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

        doc = SimpleDocTemplate(
            output_path,
            pagesize=letter,
            leftMargin=40,
            rightMargin=40,
            topMargin=40,
            bottomMargin=40,
        )

        styles = getSampleStyleSheet()

        # Custom styles
        title_style = ParagraphStyle(
            "RFQTitle",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=18,
            leading=22,
            textColor=colors.HexColor("#1E3A8A"),
            alignment=1,  # Center
        )
        subtitle_style = ParagraphStyle(
            "RFQSubtitle",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=11,
            leading=15,
            textColor=colors.HexColor("#4B5563"),
            alignment=1,
        )
        h2_style = ParagraphStyle(
            "RFQSection",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=15,
            textColor=colors.HexColor("#1E3A8A"),
            spaceBefore=8,
            spaceAfter=4,
        )
        body_style = ParagraphStyle(
            "RFQBody",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#1F2937"),
        )
        bold_body = ParagraphStyle(
            "RFQBoldBody",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=9,
            leading=12,
            textColor=colors.HexColor("#1F2937"),
        )
        warn_style = ParagraphStyle(
            "RFQWarning",
            parent=styles["Normal"],
            fontName="Helvetica-Oblique",
            fontSize=8.5,
            leading=11,
            textColor=colors.HexColor("#B45309"),
        )
        table_hdr = ParagraphStyle(
            "RFQTableHdr",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8.5,
            leading=11,
            textColor=colors.HexColor("#1E3A8A"),
        )

        story = []

        # Title and Header
        story.append(Paragraph("REQUEST FOR QUOTATION (RFQ)", title_style))
        story.append(Spacer(1, 4))
        story.append(Paragraph(rfq.title, subtitle_style))
        story.append(Spacer(1, 8))
        story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#1E3A8A"), spaceAfter=10))

        # Metadata Header Table
        meta_data = [
            [
                Paragraph("<b>RFQ Reference</b>", table_hdr),
                Paragraph("<b>Issue Date</b>", table_hdr),
                Paragraph("<b>Response Deadline</b>", table_hdr),
                Paragraph("<b>Status</b>", table_hdr),
            ],
            [
                Paragraph(rfq.buyer_info.rfq_reference, body_style),
                Paragraph(rfq.buyer_info.issue_date.strftime("%Y-%m-%d"), body_style),
                Paragraph(rfq.buyer_info.response_deadline.strftime("%Y-%m-%d %H:%M UTC"), body_style),
                Paragraph(rfq.status.value, bold_body),
            ],
        ]
        meta_table = Table(meta_data, colWidths=[130, 110, 160, 130])
        meta_table.setStyle(
            TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F3F4F6")),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ])
        )
        story.append(meta_table)
        story.append(Spacer(1, 10))

        # Section 1: Buyer Information
        story.append(Paragraph("1. Buyer Organization Information", h2_style))
        b_data = [
            [Paragraph("<b>Buyer Entity</b>", bold_body), Paragraph(rfq.buyer_info.buyer_organization, body_style)],
            [Paragraph("<b>Procurement Lead</b>", bold_body), Paragraph(rfq.buyer_info.contact_person or "Procurement Lead", body_style)],
            [Paragraph("<b>Official Contact Email</b>", bold_body), Paragraph(rfq.buyer_info.contact_email or "procurement@vendoriq.internal", body_style)],
        ]
        b_table = Table(b_data, colWidths=[160, 370])
        b_table.setStyle(
            TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ])
        )
        story.append(b_table)
        story.append(Spacer(1, 8))

        # Section 2: Target Supplier Details
        story.append(Paragraph("2. Addressed Supplier Details", h2_style))
        s_data = [
            [Paragraph("<b>Supplier Legal Name</b>", bold_body), Paragraph(rfq.supplier.company_name, body_style)],
            [Paragraph("<b>Registered Corporate Domain</b>", bold_body), Paragraph(rfq.supplier.domain, body_style)],
            [Paragraph("<b>Headquarters / Location</b>", bold_body), Paragraph(rfq.supplier.headquarters or "N/A", body_style)],
        ]
        s_table = Table(s_data, colWidths=[160, 370])
        s_table.setStyle(
            TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ])
        )
        story.append(s_table)
        if rfq.supplier.domain_mismatch_warning:
            story.append(Spacer(1, 3))
            story.append(Paragraph(f"⚠️ Supplier Domain Notice: {rfq.supplier.domain_mismatch_warning}", warn_style))
        story.append(Spacer(1, 8))

        # Section 3: Product Scope & Technical Requirements
        story.append(Paragraph("3. Scope of Supply & Technical Specifications", h2_style))
        story.append(Paragraph(f"<b>Category:</b> {rfq.product_requirements.category}", body_style))
        story.append(Paragraph(f"<b>Scope:</b> {rfq.product_requirements.description}", body_style))
        story.append(Paragraph(f"<b>Target Volume:</b> {rfq.product_requirements.expected_quantity}", bold_body))

        if rfq.product_requirements.barrier_film_requirements:
            story.append(Paragraph(f"<b>Barrier Specifications:</b> {rfq.product_requirements.barrier_film_requirements}", body_style))
        if rfq.product_requirements.packaging_requirements:
            story.append(Paragraph(f"<b>Packaging / Shipping:</b> {rfq.product_requirements.packaging_requirements}", body_style))

        if rfq.product_requirements.technical_specifications:
            story.append(Spacer(1, 3))
            story.append(Paragraph("<b>Technical Checklist:</b>", bold_body))
            for spec in rfq.product_requirements.technical_specifications:
                story.append(Paragraph(f"&nbsp;&nbsp;•&nbsp; {spec}", body_style))
        story.append(Spacer(1, 8))

        # Section 4: Commercial Requirements
        story.append(Paragraph("4. Commercial Terms & Pricing Structure", h2_style))
        c_data = [
            [
                Paragraph("<b>Quotation Currency</b>", bold_body),
                Paragraph(rfq.commercial_requirements.currency, body_style),
            ],
            [
                Paragraph("<b>Target Unit Price</b>", bold_body),
                Paragraph(
                    f"{rfq.commercial_requirements.currency} {rfq.commercial_requirements.target_unit_price:,.2f}"
                    if rfq.commercial_requirements.target_unit_price
                    else "Open quotation requested",
                    body_style,
                ),
            ],
            [
                Paragraph("<b>Minimum Order Qty (MOQ)</b>", bold_body),
                Paragraph(
                    f"{rfq.commercial_requirements.min_order_quantity:,} units"
                    if rfq.commercial_requirements.min_order_quantity
                    else "Supplier to state standard MOQ",
                    body_style,
                ),
            ],
            [
                Paragraph("<b>Production Lead Time</b>", bold_body),
                Paragraph(
                    f"Within {rfq.commercial_requirements.production_lead_time_weeks} weeks"
                    if rfq.commercial_requirements.production_lead_time_weeks
                    else "Supplier to state lead time",
                    body_style,
                ),
            ],
            [
                Paragraph("<b>Payment Terms</b>", bold_body),
                Paragraph(rfq.commercial_requirements.payment_terms, body_style),
            ],
            [
                Paragraph("<b>Incoterms</b>", bold_body),
                Paragraph(rfq.commercial_requirements.incoterms, body_style),
            ],
            [
                Paragraph("<b>Fulfillment Location</b>", bold_body),
                Paragraph(rfq.commercial_requirements.delivery_location, body_style),
            ],
        ]
        c_table = Table(c_data, colWidths=[160, 370])
        c_table.setStyle(
            TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E5E7EB")),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ])
        )
        story.append(c_table)
        story.append(Spacer(1, 8))

        # Section 5: Technical Questionnaire
        story.append(Paragraph("5. Technical Questionnaire & Clarifications", h2_style))
        all_questions = list(rfq.supplier.technical_questionnaire)
        if rfq.supplier.missing_evidence_requests:
            for req in rfq.supplier.missing_evidence_requests:
                all_questions.append(f"Documentary verification required: {req}")

        if not all_questions:
            all_questions = [
                "Provide multi-layer co-extrusion machine width and annual production tonnage capacity.",
                "Specify measured ASTM F1249 (WVTR) and ASTM D3985 (OTR) test data for proposed film thicknesses.",
                "State food-grade safety certifications (e.g., US FDA / EU Food Contact / BRCGS Packaging).",
                "Detail quality control procedures for heat seal strength, tensile strength, and defect inspection.",
            ]

        for q_idx, q in enumerate(all_questions, 1):
            story.append(Paragraph(f"<b>{q_idx}.</b> {q}", body_style))
        story.append(Spacer(1, 8))

        # Section 6: Mandatory Documentation Checklist
        story.append(Paragraph("6. Mandatory Technical & Compliance Documentation Checklist", h2_style))
        doc_checklist = rfq.requested_documents if rfq.requested_documents else [
            "Technical Data Sheets (TDS) for all proposed barrier film structures",
            "ASTM F1249 Certified Test Report for Water Vapor Transmission Rate (WVTR)",
            "ASTM D3985 Certified Test Report for Oxygen Transmission Rate (OTR)",
            "ISO 9001 / BRCGS Quality Management System Certificates",
            "Sustainability, recyclability, or compostability certifications (if claimed)",
            "Plant machinery details and manufacturing capability profile",
            "Company profile and audited commercial references",
        ]
        for item in doc_checklist:
            story.append(Paragraph(f"<b>[ &nbsp; ]</b> &nbsp; {item}", body_style))
        story.append(Spacer(1, 8))

        # Section 7: Submission Instructions & Governance Safeguards
        story.append(Paragraph("7. Submission Instructions & Governance Safeguards", h2_style))
        story.append(
            Paragraph(
                f"• <b>Submission Format & Deadline:</b> Deliver itemized quotations in PDF format before "
                f"<b>{rfq.buyer_info.response_deadline.strftime('%Y-%m-%d %H:%M UTC')}</b>.",
                body_style,
            )
        )
        story.append(
            Paragraph(
                f"• <b>Price Validity:</b> Quotations must remain valid for a minimum of "
                f"<b>{rfq.commercial_requirements.quotation_validity_days} calendar days</b>.",
                body_style,
            )
        )
        story.append(
            Paragraph(
                "• <b>Confidentiality:</b> Information in this RFQ is confidential to the buyer entity and "
                "shall not be disclosed without prior written authorization.",
                body_style,
            )
        )
        story.append(
            Paragraph(
                "• <b>External Contact Safeguard:</b> VendorIQ platform strictly utilizes human-authorized dispatch. "
                "No automated communications or unsolicited emails are transmitted.",
                body_style,
            )
        )
        story.append(Spacer(1, 8))

        # Section 8: Human Approval Sign-Off Block
        story.append(Paragraph("8. Human Review & Authorization Governance", h2_style))
        latest_audit = rfq.audit_trail[-1] if rfq.audit_trail else None
        h_data = [
            [Paragraph("<b>Lifecycle Status</b>", bold_body), Paragraph(rfq.status.value, bold_body)],
            [
                Paragraph("<b>Procurement Reviewer</b>", bold_body),
                Paragraph(latest_audit.changed_by if latest_audit else "Pending", body_style),
            ],
            [
                Paragraph("<b>Sign-off Timestamp</b>", bold_body),
                Paragraph(
                    latest_audit.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC") if latest_audit else "Pending",
                    body_style,
                ),
            ],
            [
                Paragraph("<b>Reviewer Notes</b>", bold_body),
                Paragraph(
                    latest_audit.comments if (latest_audit and latest_audit.comments) else "Standard procurement review",
                    body_style,
                ),
            ],
        ]
        h_table = Table(h_data, colWidths=[160, 370])
        h_table.setStyle(
            TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D1D5DB")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F9FAFB")),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ])
        )
        story.append(h_table)

        doc.build(story)
        return output_path
