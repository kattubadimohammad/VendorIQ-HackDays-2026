"""
VendorIQ Supplier Evidence & Intelligence Service (Phase 9).

Performs deterministic extraction, audit verification, and evidence lifecycle management
for supplier profiles. Strictly enforces Truth in Evidence guardrails:
- Never marks claims VERIFIED without documentary evidence and authoritative verification proof.
- Distinguishes self-reported directory claims from verified third-party records.
- Flags missing data and domain mismatches with actionable documentary checklists.
- Preserves full lineage with zero external API calls or credit consumption.
"""

from datetime import datetime, timezone
import logging
from typing import List, Dict, Any, Optional

from core.models import (
    SupplierProfile,
    EvidenceCategory,
    EvidenceStatus,
    SupplierEvidenceRecord,
)
from db.repository import VendorIQRepository
from services.data_quality_auditor import (
    SupplierQualityAuditor,
    SupplierAuditItem,
)

logger = logging.getLogger(__name__)

# Keywords for detecting manufacturing capability claims in company descriptions
_MANUFACTURING_KEYWORDS = {
    "manufacturing", "manufacturer", "manufacturers", "manufacture",
    "production", "plant", "plants", "factory", "factories",
    "facility", "facilities", "converter", "converters", "converting",
    "extrusion", "coextrusion", "molding", "moulding", "fabrication",
    "processing", "equipment", "machinery", "capacity",
}

# Keywords for flexible packaging vertical
_FLEXIBLE_PACKAGING_KEYWORDS = {
    "flexible packaging", "flexible", "films", "film", "pouches", "pouch",
    "bags", "bag", "woven", "polymer", "laminate", "laminates", "lamination",
    "tubes", "labels", "wrapping", "shrink",
}

# Keywords for barrier film specialty
_BARRIER_FILM_KEYWORDS = {
    "barrier", "barrier film", "barrier films", "high barrier", "moisture barrier",
    "oxygen barrier", "evoh", "metallized", "foil", "bopet", "bopp",
    "thermal lamination", "specialty film", "specialty films", "coextruded",
}

# Keywords for sustainability claims
_SUSTAINABILITY_KEYWORDS = {
    "sustainable", "sustainability", "green", "recyclable", "recycling",
    "biodegradable", "compostable", "eco friendly", "eco-friendly", "environment",
    "renewable", "pcr", "circular",
}

# Recommended documentary proof mapping for missing/unverified categories
_DOCUMENTARY_REQUIREMENTS = {
    EvidenceCategory.COMPANY_WEBSITE_DOMAIN: {
        "required_document": "Corporate Domain Registration (WHOIS) & Official SSL Certificate",
        "procurement_impact": "Prevents corporate identity spoofing and phishing risks.",
    },
    EvidenceCategory.MANUFACTURING_CAPABILITIES: {
        "required_document": "On-Site Factory Audit Report, ISO 9001 Quality Certificate & Equipment List",
        "procurement_impact": "Verifies true production machinery ownership vs broker/intermediary status.",
    },
    EvidenceCategory.FLEXIBLE_PACKAGING_EXPERIENCE: {
        "required_document": "Finished Goods Product Catalog, Technical Data Sheets & Client Reference List",
        "procurement_impact": "Confirms capability to manufacture commercial-grade packaging lines.",
    },
    EvidenceCategory.BARRIER_FILM_EXPERIENCE: {
        "required_document": "ASTM F1249 (WVTR) & ASTM D3985 (OTR) Gas Barrier Test Reports & TDS",
        "procurement_impact": "Mandatory for food, pharmaceutical, and shelf-life-critical packaging.",
    },
    EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS: {
        "required_document": "Accredited Audit Certificate (ISO 14001, FSC, GRS, or EN 13432 Compostability)",
        "procurement_impact": "Eliminates greenwashing liabilities; required for ESG and regulatory compliance.",
    },
    EvidenceCategory.HEADQUARTERS_LOCATIONS: {
        "required_document": "Government GST / Corporate Affairs Registration & Operating Facility Lease",
        "procurement_impact": "Confirms legal jurisdiction and physical operational shipping locations.",
    },
    EvidenceCategory.COMPANY_SIZE: {
        "required_document": "Statutory Payroll / EPFO Filing or Certified Annual Financial Statement",
        "procurement_impact": "Validates operational scale, direct labor staffing, and shift capacity.",
    },
}


class SupplierEvidenceService:
    """
    Deterministic evidence evaluation and lifecycle management service for VendorIQ.
    Extracts, audits, and persists structured supplier evidence.
    """

    def __init__(
        self,
        repository: Optional[VendorIQRepository] = None,
        auditor: Optional[SupplierQualityAuditor] = None,
    ):
        self.repository = repository or VendorIQRepository()
        self.auditor = auditor or SupplierQualityAuditor()

    def evaluate_supplier_evidence(
        self,
        supplier: SupplierProfile,
        audit_item: Optional[SupplierAuditItem] = None,
    ) -> List[SupplierEvidenceRecord]:
        """
        Evaluate a single supplier profile across all 7 official evidence categories.
        Enforces Truth in Evidence boundaries.
        """
        evidence_list: List[SupplierEvidenceRecord] = []
        now = datetime.now(timezone.utc)

        # Retrieve audit item if not provided
        if audit_item is None:
            audit_report = self.auditor.audit_records([supplier])
            audit_item = audit_report.supplier_audit_items[0] if audit_report.supplier_audit_items else None

        desc_text = (supplier.description or "").lower()

        # ---------------------------------------------------------------------
        # 1. Company website and domain
        # ---------------------------------------------------------------------
        if audit_item and audit_item.suspicious_domain_mismatch:
            dom_status = EvidenceStatus.UNVERIFIED
            dom_claim = (
                f"Domain '{supplier.domain}' is associated with company '{supplier.company_name}' "
                f"in Crustdata directory index, but flagged for domain mismatch."
            )
            dom_source = "Crustdata Directory Index (Flagged)"
            dom_notes = (
                f"Quality Audit Warning: {audit_item.mismatch_reason or 'Domain stem does not align with company name.'} "
                f"Manual DNS and corporate identity verification required."
            )
            dom_url = f"https://{supplier.domain}" if not supplier.domain.startswith("http") else supplier.domain
        else:
            dom_status = EvidenceStatus.SELF_REPORTED
            dom_claim = f"Corporate web domain: {supplier.domain}"
            dom_source = "Crustdata Company Search"
            dom_url = f"https://{supplier.domain}" if not supplier.domain.startswith("http") else supplier.domain
            dom_notes = (
                "Domain stem aligns with company name tokens. Self-reported directory record; "
                "independent registry/DNS audit pending."
            )

        evidence_list.append(
            SupplierEvidenceRecord(
                supplier_id=supplier.supplier_id,
                category=EvidenceCategory.COMPANY_WEBSITE_DOMAIN,
                claim=dom_claim,
                status=dom_status,
                source_name=dom_source,
                source_url=dom_url,
                reviewer_notes=dom_notes,
                created_at=now,
                updated_at=now,
            )
        )

        # ---------------------------------------------------------------------
        # 2. Manufacturing capabilities
        # ---------------------------------------------------------------------
        has_mfg = any(kw in desc_text for kw in _MANUFACTURING_KEYWORDS)
        if has_mfg and supplier.description:
            # Extract first sentence or snippet highlighting manufacturing
            snippet = supplier.description.strip()
            if len(snippet) > 160:
                snippet = snippet[:157] + "..."
            mfg_status = EvidenceStatus.SELF_REPORTED
            mfg_claim = f"Self-reported manufacturing operations stated in company overview: \"{snippet}\""
            mfg_source = "Company Directory Overview"
            mfg_notes = (
                "Self-reported marketing description from Crustdata directory index. "
                "Physical plant audit, machinery manifest, and machine line inspection not yet conducted."
            )
            mfg_url = dom_url
        else:
            mfg_status = EvidenceStatus.MISSING
            mfg_claim = "No manufacturing plant, converting, or production capabilities reported in directory profile."
            mfg_source = "Missing Data Assessment"
            mfg_notes = "Manufacturing capability unconfirmed in public registry records. On-site technical audit required."
            mfg_url = None

        evidence_list.append(
            SupplierEvidenceRecord(
                supplier_id=supplier.supplier_id,
                category=EvidenceCategory.MANUFACTURING_CAPABILITIES,
                claim=mfg_claim,
                status=mfg_status,
                source_name=mfg_source,
                source_url=mfg_url,
                reviewer_notes=mfg_notes,
                created_at=now,
                updated_at=now,
            )
        )

        # ---------------------------------------------------------------------
        # 3. Flexible packaging experience
        # ---------------------------------------------------------------------
        has_flex = any(kw in desc_text for kw in _FLEXIBLE_PACKAGING_KEYWORDS) or any(
            "packaging" in ind.lower() for ind in supplier.industries
        )
        if has_flex:
            flex_status = EvidenceStatus.SELF_REPORTED
            flex_claim = f"Company reports flexible packaging / polymer film product lines ({supplier.company_name})."
            flex_source = "Company Directory Overview"
            flex_notes = (
                "Self-reported industry focus from Crustdata basic profile. "
                "Commercial customer references and production sample qualification required."
            )
            flex_url = dom_url
        else:
            flex_status = EvidenceStatus.MISSING
            flex_claim = "No flexible packaging experience or product lines documented in profile."
            flex_source = "Missing Data Assessment"
            flex_notes = "Experience in flexible packaging vertical is unverified in public records."
            flex_url = None

        evidence_list.append(
            SupplierEvidenceRecord(
                supplier_id=supplier.supplier_id,
                category=EvidenceCategory.FLEXIBLE_PACKAGING_EXPERIENCE,
                claim=flex_claim,
                status=flex_status,
                source_name=flex_source,
                source_url=flex_url,
                reviewer_notes=flex_notes,
                created_at=now,
                updated_at=now,
            )
        )

        # ---------------------------------------------------------------------
        # 4. Barrier film experience
        # ---------------------------------------------------------------------
        has_barrier = any(kw in desc_text for kw in _BARRIER_FILM_KEYWORDS)
        if has_barrier:
            barrier_status = EvidenceStatus.SELF_REPORTED
            barrier_claim = f"Specialty / barrier film capability indicated in company profile ({supplier.company_name})."
            barrier_source = "Company Directory Overview"
            barrier_notes = (
                "Specialty film capabilities indicated in self-reported overview. Technical data sheets (TDS) "
                "and ASTM barrier test reports (OTR/WVTR transmission rates) required prior to contracting."
            )
            barrier_url = dom_url
        else:
            barrier_status = EvidenceStatus.MISSING
            barrier_claim = "No barrier film specifications, transmission rates, or testing data on file."
            barrier_source = "Missing Data Assessment"
            barrier_notes = (
                "No barrier film capabilities detected in profile. "
                "Requires submission of technical data sheets and oxygen/moisture barrier lab testing."
            )
            barrier_url = None

        evidence_list.append(
            SupplierEvidenceRecord(
                supplier_id=supplier.supplier_id,
                category=EvidenceCategory.BARRIER_FILM_EXPERIENCE,
                claim=barrier_claim,
                status=barrier_status,
                source_name=barrier_source,
                source_url=barrier_url,
                reviewer_notes=barrier_notes,
                created_at=now,
                updated_at=now,
            )
        )

        # ---------------------------------------------------------------------
        # 5. Sustainability certifications
        # ---------------------------------------------------------------------
        # Check if supplier has verified certifications
        verified_certs = supplier.verified_certifications
        if verified_certs:
            sust_cert = verified_certs[0]
            sust_status = EvidenceStatus.VERIFIED
            sust_claim = f"Verified certification on file: {sust_cert.name}"
            sust_source = sust_cert.issuing_body or "Accredited Certification Registrar"
            sust_url = sust_cert.verification_evidence
            sust_notes = f"Authoritative audit proof verified: {sust_cert.verification_evidence}"
            sust_verified_at = now
        else:
            # Check for unverified sustainability claims in text
            has_sust_claim = any(kw in desc_text for kw in _SUSTAINABILITY_KEYWORDS)
            if has_sust_claim:
                sust_status = EvidenceStatus.UNVERIFIED
                sust_claim = (
                    "Self-reported sustainability / green packaging claims mentioned in company overview "
                    "without accredited third-party certification."
                )
                sust_source = "Company Marketing Overview (Unverified)"
                sust_notes = (
                    "Marketing claims in company description. Mandatory third-party certification "
                    "(e.g. ISO 14001, FSC, GRS, or compostability lab certificate) is MISSING."
                )
                sust_url = dom_url
                sust_verified_at = None
            else:
                sust_status = EvidenceStatus.MISSING
                sust_claim = "No sustainability certifications, environmental standards, or green claims on file."
                sust_source = "Missing Data Assessment"
                sust_notes = "No accredited environmental or ESG certifications found in public registry records."
                sust_url = None
                sust_verified_at = None

        evidence_list.append(
            SupplierEvidenceRecord(
                supplier_id=supplier.supplier_id,
                category=EvidenceCategory.SUSTAINABILITY_CERTIFICATIONS,
                claim=sust_claim,
                status=sust_status,
                source_name=sust_source,
                source_url=sust_url,
                verified_at=sust_verified_at,
                reviewer_notes=sust_notes,
                created_at=now,
                updated_at=now,
            )
        )

        # ---------------------------------------------------------------------
        # 6. Company headquarters and operating locations
        # ---------------------------------------------------------------------
        country = supplier.headquarters_country or "Unknown"
        city = supplier.headquarters_city
        loc_str = f"Headquarters: {country}" + (f", City: {city}" if city else " (City unspecified in directory)")
        loc_notes = (
            "Self-reported corporate headquarters jurisdiction from Crustdata directory index. "
            "Physical operational plant and shipping warehouse locations require municipal registration and tax verification."
        )

        evidence_list.append(
            SupplierEvidenceRecord(
                supplier_id=supplier.supplier_id,
                category=EvidenceCategory.HEADQUARTERS_LOCATIONS,
                claim=loc_str,
                status=EvidenceStatus.SELF_REPORTED,
                source_name="Crustdata Company Search",
                source_url=dom_url,
                reviewer_notes=loc_notes,
                created_at=now,
                updated_at=now,
            )
        )

        # ---------------------------------------------------------------------
        # 7. Company size
        # ---------------------------------------------------------------------
        emp_range = supplier.employee_range or (f"{supplier.employee_count}" if supplier.employee_count else None)
        if emp_range:
            size_status = EvidenceStatus.SELF_REPORTED
            size_claim = f"Headcount bracket: {emp_range} employees"
            size_notes = (
                "Self-reported headcount bracket from Crustdata directory index. "
                "Does not verify direct manufacturing workforce ratio, shift schedules, or plant floor labor."
            )
            size_source = "Crustdata Company Search"
            size_url = dom_url
        else:
            size_status = EvidenceStatus.MISSING
            size_claim = "Employee headcount range not provided in profile."
            size_notes = "Headcount information missing from directory records."
            size_source = "Missing Data Assessment"
            size_url = None

        evidence_list.append(
            SupplierEvidenceRecord(
                supplier_id=supplier.supplier_id,
                category=EvidenceCategory.COMPANY_SIZE,
                claim=size_claim,
                status=size_status,
                source_name=size_source,
                source_url=size_url,
                reviewer_notes=size_notes,
                created_at=now,
                updated_at=now,
            )
        )

        return evidence_list

    def generate_and_persist_evidence_for_all_suppliers(
        self,
        force_refresh: bool = False,
    ) -> Dict[str, List[SupplierEvidenceRecord]]:
        """
        Evaluate and persist structured evidence for all suppliers in SQLite.
        If records already exist and force_refresh is False, returns existing records.
        """
        suppliers = self.repository.list_suppliers()
        if not suppliers:
            return {}

        existing_all = self.repository.list_all_evidence()
        existing_by_supplier: Dict[str, List[SupplierEvidenceRecord]] = {}
        for rec in existing_all:
            existing_by_supplier.setdefault(rec.supplier_id, []).append(rec)

        # Check if all suppliers already have 7 categories populated
        if not force_refresh and len(existing_by_supplier) == len(suppliers):
            all_complete = all(len(recs) == len(EvidenceCategory) for recs in existing_by_supplier.values())
            if all_complete:
                return existing_by_supplier

        # Audit suppliers for accurate domain mismatch signals
        audit_report = self.auditor.audit_records(suppliers)
        audit_by_supplier = {item.supplier_id: item for item in audit_report.supplier_audit_items}

        result: Dict[str, List[SupplierEvidenceRecord]] = {}
        for s in suppliers:
            audit_item = audit_by_supplier.get(s.supplier_id)
            ev_list = self.evaluate_supplier_evidence(s, audit_item=audit_item)
            persisted_list: List[SupplierEvidenceRecord] = []
            for ev in ev_list:
                persisted = self.repository.upsert_evidence(ev)
                persisted_list.append(persisted)
            result[s.supplier_id] = persisted_list

        return result

    def get_supplier_evidence(
        self,
        supplier_id: str,
        auto_generate: bool = True,
    ) -> List[SupplierEvidenceRecord]:
        """
        Retrieve evidence records for a single supplier.
        If none exist and auto_generate is True, evaluates and persists them.
        """
        records = self.repository.list_evidence_for_supplier(supplier_id)
        if not records and auto_generate:
            supplier = self.repository.get_supplier_by_id(supplier_id)
            if supplier:
                records = self.evaluate_supplier_evidence(supplier)
                persisted: List[SupplierEvidenceRecord] = []
                for rec in records:
                    persisted.append(self.repository.upsert_evidence(rec))
                return persisted
        return records

    def calculate_evidence_completeness(
        self,
        evidence_records: List[SupplierEvidenceRecord],
    ) -> Dict[str, Any]:
        """
        Calculate evidence completeness KPIs across the 7 categories for a supplier.
        """
        total = len(EvidenceCategory)
        verified_count = sum(1 for r in evidence_records if r.status == EvidenceStatus.VERIFIED)
        self_reported_count = sum(1 for r in evidence_records if r.status == EvidenceStatus.SELF_REPORTED)
        unverified_count = sum(1 for r in evidence_records if r.status == EvidenceStatus.UNVERIFIED)
        missing_count = sum(1 for r in evidence_records if r.status == EvidenceStatus.MISSING)

        # Completeness percentage: categories populated with data (not MISSING)
        populated_count = total - missing_count
        completeness_pct = round((populated_count / total) * 100.0, 1) if total > 0 else 0.0
        verified_pct = round((verified_count / total) * 100.0, 1) if total > 0 else 0.0

        if verified_count > 0:
            risk_summary = f"Verified: {verified_count}/{total} ({verified_pct}%) | Directory Claims: {self_reported_count + unverified_count} | Missing: {missing_count}"
        else:
            risk_summary = f"Zero Third-Party Verification (0/{total}) | Directory Claims: {self_reported_count + unverified_count} | Missing: {missing_count}"

        return {
            "total_categories": total,
            "verified_count": verified_count,
            "self_reported_count": self_reported_count,
            "unverified_count": unverified_count,
            "missing_count": missing_count,
            "completeness_percentage": completeness_pct,
            "verified_percentage": verified_pct,
            "risk_summary": risk_summary,
        }

    def get_missing_evidence_checklist(
        self,
        evidence_records: List[SupplierEvidenceRecord],
    ) -> List[Dict[str, str]]:
        """
        Generate actionable documentary checklist for categories that are MISSING,
        UNVERIFIED, or SELF_REPORTED.
        """
        checklist: List[Dict[str, str]] = []
        for rec in evidence_records:
            if rec.status in (EvidenceStatus.MISSING, EvidenceStatus.UNVERIFIED, EvidenceStatus.SELF_REPORTED):
                req_info = _DOCUMENTARY_REQUIREMENTS.get(
                    rec.category,
                    {
                        "required_document": "Documentary proof and official registry verification",
                        "procurement_impact": "Procurement verification required.",
                    },
                )
                urgency = "HIGH (Missing Claim)" if rec.status == EvidenceStatus.MISSING else (
                    "CRITICAL (Anomaly / Warning)" if rec.status == EvidenceStatus.UNVERIFIED else "MEDIUM (Directory Claim)"
                )
                checklist.append({
                    "category": rec.category.value,
                    "status": rec.status.value,
                    "claim": rec.claim,
                    "urgency": urgency,
                    "required_document": req_info["required_document"],
                    "procurement_impact": req_info["procurement_impact"],
                    "reviewer_notes": rec.reviewer_notes or "Verification required.",
                })
        return checklist

    def get_multi_supplier_coverage_matrix(
        self,
        suppliers: Optional[List[SupplierProfile]] = None,
    ) -> Dict[str, Any]:
        """
        Compute multi-supplier evidence coverage across all 18 suppliers.
        Returns tabular rows and category aggregate statistics.
        """
        supps = suppliers if suppliers is not None else self.repository.list_suppliers()
        if not supps:
            return {"supplier_rows": [], "category_aggregates": {}, "total_suppliers": 0}

        # Ensure evidence is loaded/cached
        evidence_by_supp = self.generate_and_persist_evidence_for_all_suppliers()

        # Audit report for domain mismatch flags
        audit_report = self.auditor.audit_records(supps)
        audit_by_supp = {item.supplier_id: item for item in audit_report.supplier_audit_items}

        supplier_rows: List[Dict[str, Any]] = []
        category_counts: Dict[str, Dict[str, int]] = {
            cat.value: {"VERIFIED": 0, "SELF_REPORTED": 0, "UNVERIFIED": 0, "MISSING": 0}
            for cat in EvidenceCategory
        }

        for s in supps:
            records = evidence_by_supp.get(s.supplier_id, [])
            rec_map = {r.category: r for r in records}
            audit_item = audit_by_supp.get(s.supplier_id)
            has_mismatch = audit_item.suspicious_domain_mismatch if audit_item else False

            row: Dict[str, Any] = {
                "supplier_id": s.supplier_id,
                "company_name": s.company_name,
                "domain": s.domain,
                "has_domain_mismatch": has_mismatch,
            }

            missing_count = 0
            for cat in EvidenceCategory:
                rec = rec_map.get(cat)
                st_val = rec.status.value if rec else EvidenceStatus.MISSING.value
                row[cat.value] = st_val
                category_counts[cat.value][st_val] = category_counts[cat.value].get(st_val, 0) + 1
                if st_val == EvidenceStatus.MISSING.value:
                    missing_count += 1

            total_cats = len(EvidenceCategory)
            row["completeness_pct"] = round(((total_cats - missing_count) / total_cats) * 100.0, 1)
            supplier_rows.append(row)

        return {
            "supplier_rows": supplier_rows,
            "category_aggregates": category_counts,
            "total_suppliers": len(supps),
        }
