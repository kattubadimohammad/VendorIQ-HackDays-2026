"""
VendorIQ Quotation Extraction Engine.

Provides deterministic data extraction from supplier quotation documents (PDF and Excel).
Supports irregular quotation layouts, extracts commercial and technical parameters,
captures detailed extraction provenance (page/cell references, confidence, raw values),
and flags missing or ambiguous fields for human confirmation.
"""

from datetime import datetime, timezone, date, timedelta
import io
import os
from pathlib import Path
import re
from typing import Optional, List, Dict, Any, Tuple, Union
import xml.etree.ElementTree as ET
import zipfile
import zlib

from core.models import (
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
)
from core.exceptions import DataValidationError


class ExtractedQuotationDraft:
    """
    Intermediate extracted quotation representation presented to the buyer
    for human review, correction, and verification.
    """

    def __init__(
        self,
        commercial: QuotationCommercialData,
        compliance: QuotationTechnicalCompliance,
        provenance_map: Dict[str, FieldExtractionProvenance],
        missing_fields: List[str],
        extraction_warnings: List[str],
        confidence_scores: Dict[str, float],
        file_type: str,
        raw_file_path: Optional[str] = None,
        file_hash_sha256: Optional[str] = None,
    ):
        self.commercial = commercial
        self.compliance = compliance
        self.provenance_map = provenance_map
        self.missing_fields = missing_fields
        self.extraction_warnings = extraction_warnings
        self.confidence_scores = confidence_scores
        self.file_type = file_type
        self.raw_file_path = raw_file_path
        self.file_hash_sha256 = file_hash_sha256
        self.is_ready_for_comparison = len(missing_fields) == 0 and commercial.critical_fields_confirmed

    def to_supplier_quotation(
        self,
        rfq_id: str,
        supplier_id: str,
        brief_id: str,
        company_name: str,
        quotation_id: Optional[str] = None,
        version: int = 1,
        reviewer_notes: Optional[str] = None,
    ) -> SupplierQuotation:
        """Convert extracted draft to a persisted SupplierQuotation domain model."""
        import uuid

        qid = quotation_id or f"QUOTE-{uuid.uuid4().hex[:8].upper()}"
        status = QuotationStatus.UNDER_REVIEW if self.commercial.critical_fields_confirmed else QuotationStatus.PARSED

        # Synchronize provenance map into commercial data
        self.commercial.provenance_map = self.provenance_map

        return SupplierQuotation(
            quotation_id=qid,
            rfq_id=rfq_id,
            supplier_id=supplier_id,
            brief_id=brief_id,
            company_name=company_name,
            version=version,
            status=status,
            submission_date=datetime.now(timezone.utc),
            validity_end_date=self.commercial.validity_end_date,
            file_type=self.file_type,
            raw_file_path=self.raw_file_path,
            file_hash_sha256=self.file_hash_sha256,
            commercial=self.commercial,
            compliance=self.compliance,
            reviewer_notes=reviewer_notes,
        )


class PDFQuotationParser:
    """
    Deterministic text stream parser for PDF quotation documents.
    Extracts text blocks per page, tracking page numbers and coordinates.
    """

    @staticmethod
    def extract_text_by_pages(pdf_bytes: bytes) -> List[Tuple[int, str]]:
        """
        Extract readable text streams from PDF bytes, returning [(page_num, text), ...].
        Handles both FlateDecode (Deflate compressed) and uncompressed PDF streams.
        Accurately associates text streams with PDF page objects where present.
        """
        pages_text: List[Tuple[int, str]] = []

        def decompress_and_extract_text(raw_stream: bytes) -> str:
            decomp = raw_stream
            try:
                decomp = zlib.decompress(raw_stream)
            except Exception:
                try:
                    decomp = zlib.decompress(raw_stream, -zlib.MAX_WBITS)
                except Exception:
                    decomp = raw_stream

            stream_lines: List[str] = []
            # 1. (Text) Tj
            tj_matches = re.findall(rb"\((.*?)\)\s*Tj", decomp)
            for tm in tj_matches:
                t_str = tm.decode("latin1", errors="ignore")
                if t_str.strip():
                    stream_lines.append(t_str)

            # 2. [(Text)...] TJ
            tj_array_matches = re.findall(rb"\[(.*?)\]\s*TJ", decomp)
            for am in tj_array_matches:
                parts = re.findall(rb"\((.*?)\)", am)
                joined_parts = "".join(p.decode("latin1", errors="ignore") for p in parts)
                if joined_parts.strip():
                    stream_lines.append(joined_parts)

            return "\n".join(stream_lines)

        # First, attempt to map page objects: N 0 obj << ... /Type /Page ... /Contents <ref> >>
        page_objs = list(re.finditer(rb"(\d+)\s+0\s+obj\s*<<(?P<dict>[^>]*)>>\s*endobj", pdf_bytes, re.DOTALL))
        stream_objs: Dict[int, bytes] = {}
        for m in re.finditer(rb"(\d+)\s+0\s+obj\s*<<.*?>>\s*stream[\r\n]+(.*?)[\r\n]+endstream", pdf_bytes, re.DOTALL):
            stream_objs[int(m.group(1))] = m.group(2)

        page_content_refs: List[List[int]] = []
        for p in page_objs:
            d = p.group("dict")
            if b"/Type /Page" in d and b"/Pages" not in d:
                m_single = re.search(rb"/Contents\s+(\d+)\s+0\s+R", d)
                if m_single:
                    page_content_refs.append([int(m_single.group(1))])
                else:
                    m_arr = re.search(rb"/Contents\s*\[(.*?)\]", d)
                    if m_arr:
                        refs = [int(r) for r in re.findall(rb"(\d+)\s+0\s+R", m_arr.group(1))]
                        page_content_refs.append(refs)

        if page_content_refs and any(any(r in stream_objs for r in refs) for refs in page_content_refs):
            for idx, refs in enumerate(page_content_refs, start=1):
                p_text_parts = [decompress_and_extract_text(stream_objs[r]) for r in refs if r in stream_objs]
                joined = "\n".join(p for p in p_text_parts if p.strip())
                if joined.strip():
                    pages_text.append((idx, joined))

        # Fallback: if no page objects found or no content extracted from page objects
        if not pages_text:
            stream_matches = list(re.finditer(rb"stream[\r\n]+(.*?)[\r\n]+endstream", pdf_bytes, re.DOTALL))
            p_idx = 1
            for match in stream_matches:
                t = decompress_and_extract_text(match.group(1))
                if t.strip():
                    pages_text.append((p_idx, t))
                    p_idx += 1

        # Final fallback for raw uncompressed text without streams
        if not pages_text:
            cleaned = re.sub(rb"[^\x20-\x7E\r\n\t]", b" ", pdf_bytes).decode("latin1", errors="ignore")
            lines = [l.strip() for l in cleaned.splitlines() if len(l.strip()) > 3]
            pages_text.append((1, "\n".join(lines)))

        return pages_text


class ExcelQuotationParser:
    """
    Deterministic parser for Excel quotation documents (.xlsx and .xls).
    Extracts cell coordinates, sheet names, formulas, and values.
    """

    @staticmethod
    def extract_cells_and_sheets(file_bytes: bytes, filename: str) -> Dict[str, Dict[str, str]]:
        """
        Extract cells from Excel file, returning {sheet_name: {cell_coord: cell_value}}.
        """
        ext = Path(filename).suffix.lower()
        if ext == ".xlsx":
            return ExcelQuotationParser._extract_xlsx(file_bytes)
        elif ext == ".xls":
            return ExcelQuotationParser._extract_xls_or_xml(file_bytes)
        else:
            raise ValueError(f"Unsupported Excel format: {ext}")

    @staticmethod
    def _extract_xlsx(file_bytes: bytes) -> Dict[str, Dict[str, str]]:
        """Parse OpenXML .xlsx archive."""
        sheets_data: Dict[str, Dict[str, str]] = {}

        try:
            with zipfile.ZipFile(io.BytesIO(file_bytes), "r") as z:
                # 1. Load shared strings
                sst: List[str] = []
                if "xl/sharedStrings.xml" in z.namelist():
                    tree = ET.fromstring(z.read("xl/sharedStrings.xml"))
                    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
                    for si in tree.findall(f"{ns}si"):
                        # Concatenate all t tags inside si (handle formatted runs)
                        t_tags = si.findall(f".//{ns}t")
                        text_val = "".join(t.text or "" for t in t_tags)
                        sst.append(text_val)

                # 2. Find sheets in workbook
                sheet_files = [f for f in z.namelist() if f.startswith("xl/worksheets/sheet") and f.endswith(".xml")]
                if not sheet_files:
                    sheet_files = [f for f in z.namelist() if "sheet" in f.lower() and f.endswith(".xml")]

                for s_file in sheet_files:
                    sheet_name = Path(s_file).stem.capitalize()
                    ws_tree = ET.fromstring(z.read(s_file))
                    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
                    cells: Dict[str, str] = {}

                    for c in ws_tree.findall(f".//{ns}c"):
                        ref = c.get("r")
                        if not ref:
                            continue
                        t_attr = c.get("t")
                        v_el = c.find(f"{ns}v")
                        val = ""

                        if t_attr == "s" and v_el is not None and v_el.text:
                            idx = int(v_el.text)
                            val = sst[idx] if idx < len(sst) else ""
                        elif t_attr == "inlineStr":
                            is_t = c.find(f".//{ns}t")
                            val = is_t.text if is_t is not None else ""
                        elif v_el is not None and v_el.text:
                            val = v_el.text

                        if val.strip():
                            cells[ref] = val.strip()

                    sheets_data[sheet_name] = cells

        except Exception as e:
            # Fallback for corrupt or non-standard xlsx
            raise DataValidationError(f"Failed to parse XLSX document: {e}") from e

        return sheets_data

    @staticmethod
    def _extract_xls_or_xml(file_bytes: bytes) -> Dict[str, Dict[str, str]]:
        """Parse SpreadsheetML or tab-delimited legacy XLS."""
        # Detect legacy binary BIFF8 / OLE2 structured storage
        if file_bytes.startswith(b"\xd0\xcf\x11\xe0"):
            raise DataValidationError(
                "Legacy binary Excel format (.xls / BIFF8) is not supported for deterministic "
                "cell-coordinate extraction without external libraries. "
                "Please re-save or convert the quotation to modern Excel (.xlsx) or text-based PDF format."
            )

        sheets_data: Dict[str, Dict[str, str]] = {}
        text_content = file_bytes.decode("latin1", errors="ignore")

        # Check if SpreadsheetML (XML)
        if "<?xml" in text_content and ("Workbook" in text_content or "ss:Workbook" in text_content):
            try:
                # Strip namespace prefixes for robust parsing
                clean_xml = re.sub(r'xmlns(:\w+)?="[^"]+"', '', text_content)
                clean_xml = re.sub(r'ss:', '', clean_xml)
                tree = ET.fromstring(clean_xml)

                for ws in tree.findall(".//Worksheet"):
                    s_name = ws.get("Name", "Sheet1")
                    cells: Dict[str, str] = {}
                    r_idx = 1
                    for row in ws.findall(".//Row"):
                        c_idx = 1
                        for cell in row.findall(".//Cell"):
                            d = cell.find(".//Data")
                            if d is not None and d.text and d.text.strip():
                                col_letter = chr(64 + min(c_idx, 26))
                                coord = f"{col_letter}{r_idx}"
                                cells[coord] = d.text.strip()
                            c_idx += 1
                        r_idx += 1
                    sheets_data[s_name] = cells
                return sheets_data
            except Exception:
                pass

        # Plain tab/csv-based fallback
        lines = [l for l in text_content.splitlines() if l.strip()]
        cells: Dict[str, str] = {}
        for r_idx, line in enumerate(lines, start=1):
            parts = re.split(r"[\t;,]", line)
            for c_idx, p in enumerate(parts, start=1):
                if p.strip():
                    col_letter = chr(64 + min(c_idx, 26))
                    cells[f"{col_letter}{r_idx}"] = p.strip()

        sheets_data["Sheet1"] = cells
        return sheets_data


class QuotationDataExtractor:
    """
    Intelligent, deterministic quotation extraction engine.
    Extracts commercial parameters and technical compliance from PDF and Excel files,
    captures field provenance, and flags missing or ambiguous items.
    """

    def __init__(self):
        self.pdf_parser = PDFQuotationParser()
        self.excel_parser = ExcelQuotationParser()

    def extract_from_file(
        self,
        file_bytes: bytes,
        filename: str,
        raw_file_path: Optional[str] = None,
        file_hash_sha256: Optional[str] = None,
    ) -> ExtractedQuotationDraft:
        """
        Orchestrate complete extraction pipeline for PDF or Excel document.
        """
        ext = Path(filename).suffix.lower()
        if ext == ".pdf":
            pages = self.pdf_parser.extract_text_by_pages(file_bytes)
            return self._extract_from_pdf_pages(pages, filename, raw_file_path, file_hash_sha256)
        elif ext in (".xlsx", ".xls"):
            sheets = self.excel_parser.extract_cells_and_sheets(file_bytes, filename)
            return self._extract_from_excel_sheets(sheets, filename, raw_file_path, file_hash_sha256)
        else:
            raise DataValidationError(f"Unsupported quotation file extension '{ext}'.")

    # =========================================================================
    # PDF EXTRACTION LOGIC
    # =========================================================================

    def _extract_from_pdf_pages(
        self,
        pages: List[Tuple[int, str]],
        filename: str,
        raw_file_path: Optional[str],
        file_hash_sha256: Optional[str],
    ) -> ExtractedQuotationDraft:
        """Extract all quotation fields from multi-page PDF text blocks."""
        all_text = "\n".join(text for _, text in pages)
        provenance: Dict[str, FieldExtractionProvenance] = {}
        warnings: List[str] = []
        confidences: Dict[str, float] = {}

        # 1. Price & Currency
        price, curr, p_raw, p_page, p_conf = self._match_unit_price(pages)
        if price is not None:
            provenance["quoted_unit_price"] = FieldExtractionProvenance(
                field_name="quoted_unit_price",
                raw_value=p_raw,
                source_file=filename,
                page_or_cell_reference=p_page,
                extraction_method=ExtractionMethod.AUTOMATED_PARSER,
                status=ExtractionFieldStatus.PENDING_CONFIRMATION,
                confidence=p_conf,
            )
            confidences["quoted_unit_price"] = p_conf
        else:
            warnings.append("Unit price could not be deterministically extracted from PDF.")

        # 2. Quantity & MOQ
        qty, q_raw, q_page, q_conf = self._match_quantity(pages)
        if qty:
            provenance["quoted_quantity"] = FieldExtractionProvenance(
                field_name="quoted_quantity",
                raw_value=q_raw,
                source_file=filename,
                page_or_cell_reference=q_page,
                extraction_method=ExtractionMethod.AUTOMATED_PARSER,
                status=ExtractionFieldStatus.PENDING_CONFIRMATION,
                confidence=q_conf,
            )
            confidences["quoted_quantity"] = q_conf

        moq, moq_raw, moq_page, moq_conf = self._match_moq(pages)
        if moq:
            provenance["min_order_quantity"] = FieldExtractionProvenance(
                field_name="min_order_quantity",
                raw_value=moq_raw,
                source_file=filename,
                page_or_cell_reference=moq_page,
                extraction_method=ExtractionMethod.AUTOMATED_PARSER,
                status=ExtractionFieldStatus.PENDING_CONFIRMATION,
                confidence=moq_conf,
            )
            confidences["min_order_quantity"] = moq_conf

        # 3. Lead Time
        lead_time, lt_raw, lt_page, lt_conf = self._match_lead_time(pages)
        if lead_time:
            provenance["lead_time_weeks"] = FieldExtractionProvenance(
                field_name="lead_time_weeks",
                raw_value=lt_raw,
                source_file=filename,
                page_or_cell_reference=lt_page,
                extraction_method=ExtractionMethod.AUTOMATED_PARSER,
                status=ExtractionFieldStatus.PENDING_CONFIRMATION,
                confidence=lt_conf,
            )
            confidences["lead_time_weeks"] = lt_conf

        # 4. Payment Terms
        pay_terms, pt_raw, pt_page, pt_conf = self._match_payment_terms(pages)
        if pay_terms:
            provenance["payment_terms"] = FieldExtractionProvenance(
                field_name="payment_terms",
                raw_value=pt_raw,
                source_file=filename,
                page_or_cell_reference=pt_page,
                extraction_method=ExtractionMethod.AUTOMATED_PARSER,
                status=ExtractionFieldStatus.PENDING_CONFIRMATION,
                confidence=pt_conf,
            )
            confidences["payment_terms"] = pt_conf

        # 5. Incoterms
        inco, inco_raw, inco_page, inco_conf = self._match_incoterms(pages)
        provenance["incoterms"] = FieldExtractionProvenance(
            field_name="incoterms",
            raw_value=inco_raw or inco.value,
            source_file=filename,
            page_or_cell_reference=inco_page or "Defaulted / Pattern matched",
            extraction_method=ExtractionMethod.AUTOMATED_PARSER,
            status=ExtractionFieldStatus.PENDING_CONFIRMATION,
            confidence=inco_conf,
        )

        # 6. Freight & Taxes
        freight, is_fr_inc, fr_raw = self._match_freight(pages, inco)
        tax, is_tx_inc, tx_raw = self._match_taxes(pages, inco)

        # 7. Validity
        val_days, val_date, v_raw = self._match_validity(pages)

        # 8. Technical Compliance & Specifications
        compliance = self._match_technical_compliance(all_text)

        # Construct commercial data model
        effective_price = price if price is not None else 0.0
        effective_qty = qty if qty is not None else 10000
        effective_moq = moq if moq is not None else (qty or 5000)
        effective_lead = lead_time if lead_time is not None else 4
        effective_pay = pay_terms or "Net 30 days upon inspection (Defaulted/Unspecified)"

        commercial = QuotationCommercialData(
            quoted_currency=curr or "USD",
            quoted_unit_price=effective_price,
            quoted_quantity=effective_qty,
            min_order_quantity=effective_moq,
            lead_time_weeks=effective_lead,
            payment_terms=effective_pay,
            incoterms=inco,
            freight_charges=freight,
            taxes_and_duties=tax,
            is_freight_included=is_fr_inc,
            is_tax_included=is_tx_inc,
            validity_days=val_days or 60,
            validity_end_date=val_date,
            critical_fields_confirmed=False,
            provenance_map=provenance,
        )

        # Compute initial landed cost if inputs permit
        commercial.calculate_landed_cost()

        # Identify missing mandatory fields
        missing_fields: List[str] = []
        if price is None:
            missing_fields.append("quoted_unit_price")
        if qty is None:
            missing_fields.append("quoted_quantity")
        if lead_time is None:
            missing_fields.append("lead_time_weeks")
        if not pay_terms:
            missing_fields.append("payment_terms")

        return ExtractedQuotationDraft(
            commercial=commercial,
            compliance=compliance,
            provenance_map=provenance,
            missing_fields=missing_fields,
            extraction_warnings=warnings,
            confidence_scores=confidences,
            file_type="PDF",
            raw_file_path=raw_file_path,
            file_hash_sha256=file_hash_sha256,
        )

    # =========================================================================
    # EXCEL EXTRACTION LOGIC
    # =========================================================================

    def _extract_from_excel_sheets(
        self,
        sheets: Dict[str, Dict[str, str]],
        filename: str,
        raw_file_path: Optional[str],
        file_hash_sha256: Optional[str],
    ) -> ExtractedQuotationDraft:
        """Extract all quotation fields from Excel cell coordinates."""
        provenance: Dict[str, FieldExtractionProvenance] = {}
        warnings: List[str] = []
        confidences: Dict[str, float] = {}

        # Flatten sheet cells with coordinate labels
        flat_cells: List[Tuple[str, str, str]] = []  # (sheet, coord, value)
        for s_name, cell_dict in sheets.items():
            for coord, val in cell_dict.items():
                flat_cells.append((s_name, coord, val))

        # 1. Price & Currency from Excel cells
        price, curr, p_raw, p_ref, p_conf = self._match_unit_price_excel(flat_cells, sheets)
        if price is not None:
            provenance["quoted_unit_price"] = FieldExtractionProvenance(
                field_name="quoted_unit_price",
                raw_value=p_raw,
                source_file=filename,
                page_or_cell_reference=p_ref,
                extraction_method=ExtractionMethod.AUTOMATED_PARSER,
                status=ExtractionFieldStatus.PENDING_CONFIRMATION,
                confidence=p_conf,
            )
            confidences["quoted_unit_price"] = p_conf
        else:
            warnings.append("Unit price could not be deterministically identified in spreadsheet cells.")

        # 2. Quantity & MOQ from Excel
        qty, q_raw, q_ref, q_conf = self._match_quantity_excel(flat_cells, sheets)
        if qty:
            provenance["quoted_quantity"] = FieldExtractionProvenance(
                field_name="quoted_quantity",
                raw_value=q_raw,
                source_file=filename,
                page_or_cell_reference=q_ref,
                extraction_method=ExtractionMethod.AUTOMATED_PARSER,
                status=ExtractionFieldStatus.PENDING_CONFIRMATION,
                confidence=q_conf,
            )
            confidences["quoted_quantity"] = q_conf

        moq, moq_raw, moq_ref, moq_conf = self._match_moq_excel(flat_cells, sheets)
        if moq:
            provenance["min_order_quantity"] = FieldExtractionProvenance(
                field_name="min_order_quantity",
                raw_value=moq_raw,
                source_file=filename,
                page_or_cell_reference=moq_ref,
                extraction_method=ExtractionMethod.AUTOMATED_PARSER,
                status=ExtractionFieldStatus.PENDING_CONFIRMATION,
                confidence=moq_conf,
            )
            confidences["min_order_quantity"] = moq_conf

        # 3. Lead Time from Excel
        lead_time, lt_raw, lt_ref, lt_conf = self._match_lead_time_excel(flat_cells, sheets)
        if lead_time:
            provenance["lead_time_weeks"] = FieldExtractionProvenance(
                field_name="lead_time_weeks",
                raw_value=lt_raw,
                source_file=filename,
                page_or_cell_reference=lt_ref,
                extraction_method=ExtractionMethod.AUTOMATED_PARSER,
                status=ExtractionFieldStatus.PENDING_CONFIRMATION,
                confidence=lt_conf,
            )
            confidences["lead_time_weeks"] = lt_conf

        # 4. Payment Terms from Excel
        pay_terms, pt_raw, pt_ref, pt_conf = self._match_payment_terms_excel(flat_cells, sheets)
        if pay_terms:
            provenance["payment_terms"] = FieldExtractionProvenance(
                field_name="payment_terms",
                raw_value=pt_raw,
                source_file=filename,
                page_or_cell_reference=pt_ref,
                extraction_method=ExtractionMethod.AUTOMATED_PARSER,
                status=ExtractionFieldStatus.PENDING_CONFIRMATION,
                confidence=pt_conf,
            )
            confidences["payment_terms"] = pt_conf

        # 5. Incoterms from Excel
        inco, inco_raw, inco_ref, inco_conf = self._match_incoterms_excel(flat_cells)
        provenance["incoterms"] = FieldExtractionProvenance(
            field_name="incoterms",
            raw_value=inco_raw or inco.value,
            source_file=filename,
            page_or_cell_reference=inco_ref or "Defaulted / Pattern matched",
            extraction_method=ExtractionMethod.AUTOMATED_PARSER,
            status=ExtractionFieldStatus.PENDING_CONFIRMATION,
            confidence=inco_conf,
        )

        # 6. Freight & Taxes
        freight, is_fr_inc = self._match_freight_excel(flat_cells, inco)
        tax, is_tx_inc = self._match_taxes_excel(flat_cells, inco)

        # 7. Combined text for technical compliance
        all_excel_text = " ".join(val for _, _, val in flat_cells)
        compliance = self._match_technical_compliance(all_excel_text)

        effective_price = price if price is not None else 0.0
        effective_qty = qty if qty is not None else 10000
        effective_moq = moq if moq is not None else (qty or 5000)
        effective_lead = lead_time if lead_time is not None else 4
        effective_pay = pay_terms or "Net 30 days upon inspection (Defaulted/Unspecified)"

        commercial = QuotationCommercialData(
            quoted_currency=curr or "USD",
            quoted_unit_price=effective_price,
            quoted_quantity=effective_qty,
            min_order_quantity=effective_moq,
            lead_time_weeks=effective_lead,
            payment_terms=effective_pay,
            incoterms=inco,
            freight_charges=freight,
            taxes_and_duties=tax,
            is_freight_included=is_fr_inc,
            is_tax_included=is_tx_inc,
            validity_days=60,
            critical_fields_confirmed=False,
            provenance_map=provenance,
        )
        commercial.calculate_landed_cost()

        missing_fields: List[str] = []
        if price is None:
            missing_fields.append("quoted_unit_price")
        if qty is None:
            missing_fields.append("quoted_quantity")
        if lead_time is None:
            missing_fields.append("lead_time_weeks")
        if not pay_terms:
            missing_fields.append("payment_terms")

        return ExtractedQuotationDraft(
            commercial=commercial,
            compliance=compliance,
            provenance_map=provenance,
            missing_fields=missing_fields,
            extraction_warnings=warnings,
            confidence_scores=confidences,
            file_type="EXCEL",
            raw_file_path=raw_file_path,
            file_hash_sha256=file_hash_sha256,
        )

    # =========================================================================
    # SEMANTIC PATTERN MATCHING HELPERS
    # =========================================================================

    def _match_unit_price(
        self, pages: List[Tuple[int, str]]
    ) -> Tuple[Optional[float], Optional[str], Optional[str], Optional[str], float]:
        """
        Extract unit price and currency from PDF text blocks.
        Returns: (price, currency, raw_match, page_ref, confidence)
        """
        # Patterns for unit price lines
        price_patterns = [
            # "Unit Price: $1.45" or "Unit Rate: USD 1.45" or "Price/Unit: 1.45 USD"
            r"(?:unit\s*price|price\s*per\s*unit|rate\s*per\s*unit|unit\s*rate|price\s*\/\s*(?:pc|unit|bag|pouch|kg))\s*[:=-]?\s*([$₹€£]|USD|INR|EUR|GBP|Rs\.?)?\s*([0-9]+(?:\.[0-9]{1,4})?)\s*([$₹€£]|USD|INR|EUR|GBP)?",
            # "$1.45 per unit" or "USD 1.45/unit"
            r"([$₹€£]|USD|INR|EUR|GBP|Rs\.?)?\s*([0-9]+(?:\.[0-9]{1,4})?)\s*(?:USD|INR|EUR|GBP)?\s*(?:per\s*unit|\/\s*(?:unit|pc|pouch|kg))",
            # General "Price: $1.45"
            r"(?:price|rate)\s*[:=-]\s*([$₹€£]|USD|INR|EUR|GBP|Rs\.?)?\s*([0-9]+(?:\.[0-9]{1,4})?)\s*([$₹€£]|USD|INR|EUR|GBP)?",
        ]

        for page_num, text in pages:
            lines = text.splitlines()
            for line in lines:
                l_lower = line.lower()
                # Skip total price or tooling charges
                if "total price" in l_lower or "total amount" in l_lower or "subtotal" in l_lower:
                    continue

                for pat in price_patterns:
                    m = re.search(pat, line, re.IGNORECASE)
                    if m:
                        groups = m.groups()
                        curr_token = groups[0] or (groups[2] if len(groups) > 2 else None)
                        val_token = groups[1]

                        try:
                            val = float(val_token)
                            if val > 0:
                                curr = self._normalize_currency(curr_token, line)
                                confidence = 0.95 if "unit" in l_lower else 0.80
                                return val, curr, m.group(0).strip(), f"Page {page_num}", confidence
                        except ValueError:
                            continue

        return None, None, None, None, 0.0

    def _match_unit_price_excel(
        self, flat_cells: List[Tuple[str, str, str]], sheets: Dict[str, Dict[str, str]]
    ) -> Tuple[Optional[float], Optional[str], Optional[str], Optional[str], float]:
        """Extract unit price from Excel cell layout (table header or adjacent label)."""
        # 1. Search for adjacent cell: Col A = "Unit Price", Col B = "1.45"
        for s_name, coord, val in flat_cells:
            val_lower = val.lower()
            if any(k in val_lower for k in ("unit price", "price per unit", "rate/unit", "unit rate")):
                # Check adjacent cell in same row (e.g. A2 -> B2 or C2)
                col_match = re.match(r"([A-Z]+)([0-9]+)", coord)
                if col_match:
                    col, row_num = col_match.groups()
                    next_col = chr(ord(col[-1]) + 1)
                    cand_coords = [f"{next_col}{row_num}", f"{chr(ord(col[-1]) + 2)}{row_num}"]
                    for c_coord in cand_coords:
                        cand_val = sheets.get(s_name, {}).get(c_coord)
                        if cand_val:
                            p, curr = self._parse_numeric_and_curr(cand_val, val)
                            if p is not None:
                                return p, curr, f"{val} -> {cand_val}", f"{s_name}:{c_coord}", 0.98

        # 2. Search for direct numeric cell with currency symbol
        for s_name, coord, val in flat_cells:
            if re.match(r"^[$₹€£]\s*[0-9]+(?:\.[0-9]{1,4})?$", val) or re.match(r"^[0-9]+(?:\.[0-9]{1,4})?\s*USD$", val, re.I):
                p, curr = self._parse_numeric_and_curr(val)
                if p is not None and 0.01 <= p <= 10000.0:
                    return p, curr, val, f"{s_name}:{coord}", 0.85

        return None, None, None, None, 0.0

    def _match_quantity(
        self, pages: List[Tuple[int, str]]
    ) -> Tuple[Optional[int], Optional[str], Optional[str], float]:
        """Extract quoted quantity from PDF text."""
        pat = r"(?:quoted\s*quantity|order\s*quantity|order\s*volume|target\s*quantity|quantity|volume|qty)\s*[:=-]?\s*([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)"
        for page_num, text in pages:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                raw_num = m.group(1).replace(",", "")
                try:
                    q = int(raw_num)
                    if q >= 10:
                        return q, m.group(0).strip(), f"Page {page_num}", 0.90
                except ValueError:
                    pass
        return None, None, None, 0.0

    def _match_quantity_excel(
        self, flat_cells: List[Tuple[str, str, str]], sheets: Dict[str, Dict[str, str]]
    ) -> Tuple[Optional[int], Optional[str], Optional[str], float]:
        """Extract quoted quantity from Excel cells."""
        for s_name, coord, val in flat_cells:
            val_lower = val.lower()
            if any(k in val_lower for k in ("quoted quantity", "order quantity", "order volume", "quantity", "volume", "qty")) and "moq" not in val_lower:
                col_match = re.match(r"([A-Z]+)([0-9]+)", coord)
                if col_match:
                    col, row_num = col_match.groups()
                    col_code = ord(col[-1])
                    for offset in (1, 2, 3, 4):
                        cand_col = chr(col_code + offset)
                        cand_val = sheets.get(s_name, {}).get(f"{cand_col}{row_num}")
                        if cand_val and cand_val.strip():
                            cleaned = re.sub(r"[^\d]", "", cand_val)
                            if cleaned:
                                try:
                                    return int(cleaned), f"{val} -> {cand_val}", f"{s_name}:{cand_col}{row_num}", 0.95
                                except ValueError:
                                    pass
        return None, None, None, 0.0

    def _match_moq(
        self, pages: List[Tuple[int, str]]
    ) -> Tuple[Optional[int], Optional[str], Optional[str], float]:
        """Extract MOQ from PDF text."""
        pat = r"(?:minimum\s*order\s*quantity|minimum\s*order|moq)\s*[:=-]?\s*([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)"
        for page_num, text in pages:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                raw_num = m.group(1).replace(",", "")
                try:
                    moq = int(raw_num)
                    if moq >= 1:
                        return moq, m.group(0).strip(), f"Page {page_num}", 0.95
                except ValueError:
                    pass
        return None, None, None, 0.0

    def _match_moq_excel(
        self, flat_cells: List[Tuple[str, str, str]], sheets: Dict[str, Dict[str, str]]
    ) -> Tuple[Optional[int], Optional[str], Optional[str], float]:
        """Extract MOQ from Excel cells."""
        for s_name, coord, val in flat_cells:
            val_lower = val.lower()
            if "moq" in val_lower or "minimum order" in val_lower:
                col_match = re.match(r"([A-Z]+)([0-9]+)", coord)
                if col_match:
                    col, row_num = col_match.groups()
                    col_code = ord(col[-1])
                    for offset in (1, 2, 3, 4):
                        cand_col = chr(col_code + offset)
                        cand_val = sheets.get(s_name, {}).get(f"{cand_col}{row_num}")
                        if cand_val and cand_val.strip():
                            cleaned = re.sub(r"[^\d]", "", cand_val)
                            if cleaned:
                                try:
                                    return int(cleaned), f"{val} -> {cand_val}", f"{s_name}:{cand_col}{row_num}", 0.95
                                except ValueError:
                                    pass
        return None, None, None, 0.0

    def _match_lead_time(
        self, pages: List[Tuple[int, str]]
    ) -> Tuple[Optional[int], Optional[str], Optional[str], float]:
        """Extract lead time in weeks from PDF."""
        # "Lead Time: 4 weeks" or "Delivery: 3-4 weeks" or "Lead Time: 28 days"
        weeks_pat = r"(?:lead\s*time|production\s*time|delivery\s*time|delivery)\s*[:=-]?\s*([0-9]+)(?:\s*-\s*[0-9]+)?\s*weeks?"
        days_pat = r"(?:lead\s*time|production\s*time|delivery\s*time|delivery)\s*[:=-]?\s*([0-9]+)(?:\s*-\s*([0-9]+))?\s*days?"

        for page_num, text in pages:
            m_wk = re.search(weeks_pat, text, re.IGNORECASE)
            if m_wk:
                return int(m_wk.group(1)), m_wk.group(0).strip(), f"Page {page_num}", 0.95

            m_dy = re.search(days_pat, text, re.IGNORECASE)
            if m_dy:
                days = int(m_dy.group(1))
                weeks = max(1, round(days / 7))
                return weeks, m_dy.group(0).strip(), f"Page {page_num}", 0.90

        return None, None, None, 0.0

    def _match_lead_time_excel(
        self, flat_cells: List[Tuple[str, str, str]], sheets: Dict[str, Dict[str, str]]
    ) -> Tuple[Optional[int], Optional[str], Optional[str], float]:
        """Extract lead time in weeks from Excel."""
        for s_name, coord, val in flat_cells:
            val_lower = val.lower()
            if "lead time" in val_lower or "production time" in val_lower or "delivery time" in val_lower:
                col_match = re.match(r"([A-Z]+)([0-9]+)", coord)
                if col_match:
                    col, row_num = col_match.groups()
                    col_code = ord(col[-1])
                    for offset in (1, 2, 3, 4):
                        cand_col = chr(col_code + offset)
                        cand_val = sheets.get(s_name, {}).get(f"{cand_col}{row_num}")
                        if cand_val and cand_val.strip():
                            # Parse weeks or days
                            m_w = re.search(r"([0-9]+)\s*week", cand_val, re.I)
                            if m_w:
                                return int(m_w.group(1)), f"{val} -> {cand_val}", f"{s_name}:{cand_col}{row_num}", 0.95
                            m_d = re.search(r"([0-9]+)\s*day", cand_val, re.I)
                            if m_d:
                                return max(1, round(int(m_d.group(1)) / 7)), f"{val} -> {cand_val}", f"{s_name}:{cand_col}{row_num}", 0.90
                            # Pure integer
                            cleaned = re.sub(r"[^\d]", "", cand_val)
                            if cleaned and int(cleaned) <= 52:
                                return int(cleaned), f"{val} -> {cand_val}", f"{s_name}:{cand_col}{row_num}", 0.80

        return None, None, None, 0.0

    def _match_payment_terms(
        self, pages: List[Tuple[int, str]]
    ) -> Tuple[Optional[str], Optional[str], Optional[str], float]:
        """Extract payment terms from PDF."""
        pat = r"(?:payment\s*terms|terms\s*of\s*payment|payment)\s*[:=-]?\s*([^\n\r.]+)"
        for page_num, text in pages:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                raw = m.group(1).strip()
                if len(raw) > 2 and any(k in raw.lower() for k in ("net", "advance", "lc", "days", "bl", "delivery")):
                    return raw, m.group(0).strip(), f"Page {page_num}", 0.90
        return None, None, None, 0.0

    def _match_payment_terms_excel(
        self, flat_cells: List[Tuple[str, str, str]], sheets: Dict[str, Dict[str, str]]
    ) -> Tuple[Optional[str], Optional[str], Optional[str], float]:
        """Extract payment terms from Excel."""
        for s_name, coord, val in flat_cells:
            val_lower = val.lower()
            if "payment" in val_lower and ("term" in val_lower or "condition" in val_lower):
                col_match = re.match(r"([A-Z]+)([0-9]+)", coord)
                if col_match:
                    col, row_num = col_match.groups()
                    col_code = ord(col[-1])
                    for offset in (1, 2, 3, 4):
                        cand_col = chr(col_code + offset)
                        cand_val = sheets.get(s_name, {}).get(f"{cand_col}{row_num}")
                        if cand_val and len(cand_val.strip()) > 2:
                            return cand_val.strip(), f"{val} -> {cand_val}", f"{s_name}:{cand_col}{row_num}", 0.95
        return None, None, None, 0.0

    def _match_incoterms(
        self, pages: List[Tuple[int, str]]
    ) -> Tuple[Incoterm, Optional[str], Optional[str], float]:
        """Extract Incoterms from PDF."""
        for page_num, text in pages:
            for term in (Incoterm.DDP, Incoterm.CIF, Incoterm.FOB, Incoterm.CFR, Incoterm.EXW):
                # Search for term with word boundaries
                pat = rf"\b{term.value}\b"
                if re.search(pat, text, re.IGNORECASE):
                    return term, term.value, f"Page {page_num}", 0.95
                if term == Incoterm.EXW and re.search(r"ex\s*[-_]?\s*works", text, re.I):
                    return Incoterm.EXW, "Ex-Works", f"Page {page_num}", 0.95
                if term == Incoterm.FOB and re.search(r"f\.o\.b\.", text, re.I):
                    return Incoterm.FOB, "F.O.B.", f"Page {page_num}", 0.95

        return Incoterm.FOB, None, None, 0.50

    def _match_incoterms_excel(
        self, flat_cells: List[Tuple[str, str, str]]
    ) -> Tuple[Incoterm, Optional[str], Optional[str], float]:
        """Extract Incoterms from Excel cells."""
        for s_name, coord, val in flat_cells:
            v_clean = val.strip().upper()
            for term in (Incoterm.DDP, Incoterm.CIF, Incoterm.FOB, Incoterm.CFR, Incoterm.EXW):
                if term.value in v_clean:
                    return term, val, f"{s_name}:{coord}", 0.95
                if "EX-WORKS" in v_clean or "EX WORKS" in v_clean:
                    return Incoterm.EXW, val, f"{s_name}:{coord}", 0.95

        return Incoterm.FOB, None, None, 0.50

    def _match_freight(self, pages: List[Tuple[int, str]], inco: Incoterm) -> Tuple[Optional[float], bool, Optional[str]]:
        """Extract freight charges or freight-included status from PDF."""
        is_included = inco in (Incoterm.CIF, Incoterm.DDP, Incoterm.CFR)
        for _, text in pages:
            if "freight included" in text.lower() or "shipping included" in text.lower() or "free shipping" in text.lower():
                return None, True, "Freight Included"
            m = re.search(r"(?:freight|shipping|transport)\s*[:=-]?\s*([$₹€£]|USD|INR)?\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
            if m:
                try:
                    val = float(m.group(2))
                    return val, False, m.group(0).strip()
                except ValueError:
                    pass
        return None, is_included, None

    def _match_freight_excel(self, flat_cells: List[Tuple[str, str, str]], inco: Incoterm) -> Tuple[Optional[float], bool]:
        is_included = inco in (Incoterm.CIF, Incoterm.DDP, Incoterm.CFR)
        for _, _, val in flat_cells:
            if "freight included" in val.lower() or "shipping included" in val.lower():
                return None, True
            m = re.search(r"(?:freight|shipping)\s*[:=-]?\s*[$₹€£]?\s*([0-9]+(?:\.[0-9]+)?)", val, re.I)
            if m:
                try:
                    return float(m.group(1)), False
                except ValueError:
                    pass
        return None, is_included

    def _match_taxes(self, pages: List[Tuple[int, str]], inco: Incoterm) -> Tuple[Optional[float], bool, Optional[str]]:
        """Extract taxes/duties or tax-included status from PDF."""
        is_included = (inco == Incoterm.DDP)
        for _, text in pages:
            if "taxes included" in text.lower() or "duty paid" in text.lower() or "all taxes inclusive" in text.lower():
                return None, True, "Taxes Included"
            m = re.search(r"(?:taxes|tax|duties|duty|gst|vat)\s*[:=-]?\s*([$₹€£]|USD|INR)?\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
            if m:
                try:
                    val = float(m.group(2))
                    return val, False, m.group(0).strip()
                except ValueError:
                    pass
        return None, is_included, None

    def _match_taxes_excel(self, flat_cells: List[Tuple[str, str, str]], inco: Incoterm) -> Tuple[Optional[float], bool]:
        is_included = (inco == Incoterm.DDP)
        for _, _, val in flat_cells:
            if "tax included" in val.lower() or "duty paid" in val.lower() or "inclusive of all taxes" in val.lower():
                return None, True
        return None, is_included

    def _match_validity(self, pages: List[Tuple[int, str]]) -> Tuple[int, Optional[date], Optional[str]]:
        """Extract quote validity days or end date."""
        for _, text in pages:
            m_days = re.search(r"(?:validity|valid\s*for)\s*[:=-]?\s*([0-9]+)\s*days?", text, re.I)
            if m_days:
                days = int(m_days.group(1))
                end_d = date.today() + timedelta(days=days)
                return days, end_d, m_days.group(0).strip()
            m_date = re.search(r"(?:valid\s*until|expiry\s*date|valid\s*thru)\s*[:=-]?\s*([0-9]{4}-[0-9]{2}-[0-9]{2})", text, re.I)
            if m_date:
                try:
                    end_d = date.fromisoformat(m_date.group(1))
                    days = max(1, (end_d - date.today()).days)
                    return days, end_d, m_date.group(0).strip()
                except ValueError:
                    pass
        return 60, date.today() + timedelta(days=60), None

    def _match_technical_compliance(self, text: str) -> QuotationTechnicalCompliance:
        """Extract technical specifications, barrier metrics, and certification claims."""
        text_lower = text.lower()

        # Structure
        struct = "Multi-layer barrier packaging structure"
        m_struct = re.search(r"(?:structure|material|substrate|composition)\s*[:=-]?\s*([^\n\r.]+)", text, re.I)
        if m_struct:
            struct = m_struct.group(1).strip()
        elif "pet" in text_lower or "bopp" in text_lower or "ldpe" in text_lower or "evoh" in text_lower:
            parts = []
            for token in ("PET", "Met-BOPP", "BOPP", "PE", "LDPE", "EVOH", "Alu Foil", "Nylon", "BOPA"):
                if token.lower() in text_lower:
                    parts.append(token)
            if parts:
                struct = " / ".join(parts) + " multi-layer structure"

        # Thickness
        thickness = None
        m_thick_explicit = re.search(r"(?:total\s*thickness|thickness|film\s*thickness|gauge)\s*[:=-]?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:microns?|micron|µm|u)?\b", text, re.I)
        if m_thick_explicit:
            try:
                thickness = float(m_thick_explicit.group(1))
            except ValueError:
                pass
        if thickness is None:
            m_thick = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:microns?|micron|µm|u)\b", text, re.I)
            if m_thick:
                try:
                    thickness = float(m_thick.group(1))
                except ValueError:
                    pass

        # OTR
        otr = None
        m_otr = re.search(r"(?:otr|oxygen\s*transmission\s*rate)\s*[:=-]?\s*([^\n\r,;]+)", text, re.I)
        if m_otr:
            otr = m_otr.group(1).strip()
        elif "cc/m" in text_lower:
            m_cc = re.search(r"([0-9]+(?:\.[0-9]+)?\s*cc\/m[2²]\/(?:day|24h))", text, re.I)
            if m_cc:
                otr = m_cc.group(1)

        # WVTR
        wvtr = None
        m_wvtr = re.search(r"(?:wvtr|water\s*vapor\s*transmission\s*rate|mvtr)\s*[:=-]?\s*([^\n\r,;]+)", text, re.I)
        if m_wvtr:
            wvtr = m_wvtr.group(1).strip()
        elif "g/m" in text_lower:
            m_g = re.search(r"([0-9]+(?:\.[0-9]+)?\s*g\/m[2²]\/(?:day|24h))", text, re.I)
            if m_g:
                wvtr = m_g.group(1)

        # Test standards
        standards = []
        for std in ("ASTM D3985", "ASTM F1249", "ASTM F1927", "ASTM E96", "ISO 15105", "ISO 2556"):
            if std.lower() in text_lower:
                standards.append(std)

        # Certifications
        certs = []
        for cert in ("ISO 9001", "ISO 14001", "BRCGS", "FSC", "HACCP", "FDA", "BRC"):
            if cert.lower() in text_lower:
                certs.append(cert)

        # TDS attachment
        has_tds = ("technical data sheet" in text_lower or "tds attached" in text_lower or "test certificate" in text_lower or "laboratory report" in text_lower)

        # Mandatory requirements check (barrier requirement)
        has_barrier = (otr is not None) or ("barrier" in text_lower) or any("astm" in s.lower() for s in standards)
        disqualifications = []
        if not has_barrier:
            disqualifications.append("Quotation does not confirm required barrier film properties.")

        # Grounding against Phase 9 evidence categories
        evidence_map: Dict[str, EvidenceStatus] = {}
        if has_barrier and has_tds:
            evidence_map[EvidenceCategory.BARRIER_FILM_EXPERIENCE.value] = EvidenceStatus.VERIFIED
        elif has_barrier:
            evidence_map[EvidenceCategory.BARRIER_FILM_EXPERIENCE.value] = EvidenceStatus.SELF_REPORTED
        else:
            evidence_map[EvidenceCategory.BARRIER_FILM_EXPERIENCE.value] = EvidenceStatus.MISSING

        if certs and has_tds:
            evidence_map[EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS.value] = EvidenceStatus.VERIFIED
        elif certs:
            evidence_map[EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS.value] = EvidenceStatus.SELF_REPORTED
        else:
            evidence_map[EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS.value] = EvidenceStatus.MISSING

        return QuotationTechnicalCompliance(
            product_category="Flexible Packaging",
            structure_description=struct,
            thickness_microns=thickness,
            otr_value=otr,
            wvtr_value=wvtr,
            test_standards_cited=standards,
            certifications_offered=certs,
            evidence_status_map=evidence_map,
            has_tds_document=has_tds,
            mandatory_requirements_met=len(disqualifications) == 0,
            disqualification_reasons=disqualifications,
            compliance_notes="Automated technical spec extraction.",
        )

    # =========================================================================
    # PARSING UTILITY HELPERS
    # =========================================================================

    def _normalize_currency(self, token: Optional[str], context_line: str) -> str:
        """Resolve standard 3-letter currency code."""
        if not token:
            ctx = context_line.upper()
            if "INR" in ctx or "RS" in ctx or "RUPEES" in ctx:
                return "INR"
            if "EUR" in ctx or "EURO" in ctx:
                return "EUR"
            if "GBP" in ctx or "POUND" in ctx:
                return "GBP"
            return "USD"

        t_up = token.strip().upper()
        if t_up in ("$", "USD"):
            return "USD"
        if t_up in ("₹", "RS", "RS.", "INR"):
            return "INR"
        if t_up in ("€", "EUR"):
            return "EUR"
        if t_up in ("£", "GBP"):
            return "GBP"
        return "USD"

    def _parse_numeric_and_curr(self, text: str, label_hint: str = "") -> Tuple[Optional[float], str]:
        """Parse float and currency from raw cell string."""
        curr = self._normalize_currency(None, f"{text} {label_hint}")
        cleaned = re.sub(r"[^\d.]", "", text)
        if not cleaned:
            return None, curr
        try:
            return float(cleaned), curr
        except ValueError:
            return None, curr
