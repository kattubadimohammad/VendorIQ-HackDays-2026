"""
VendorIQ Data Quality Audit Service.

Performs deterministic data quality audits on supplier records:
- Identifies missing fields (descriptions, employee ranges, headquarters).
- Detects duplicate names and domains.
- Detects domain-to-company-name mismatches for manual review.
- Preserves original data without fabricating missing values.
"""

from dataclasses import dataclass, field
import re
from typing import List, Dict, Any, Optional

from core.models import SupplierProfile

# Common corporate stop-words to strip when matching company names to domains
_CORPORATE_STOPWORDS = {
    "ltd", "limited", "pvt", "private", "inc", "incorporated", "corp",
    "corporation", "llc", "llp", "group", "holdings", "company", "co",
    "india", "technologies", "technology", "international", "industries",
    "industry", "solutions", "services", "mfg", "manufacturing", "packaging",
}


def normalize_tokens(text: str) -> set:
    """Extract clean alphanumeric tokens from text for comparison."""
    clean = re.sub(r"[^a-zA-Z0-9\s]", " ", text.lower())
    tokens = {t.strip() for t in clean.split() if len(t.strip()) > 1}
    return tokens - _CORPORATE_STOPWORDS


def extract_domain_stem(domain: str) -> str:
    """Extract the primary brand stem from a domain name (e.g. 'arrowgreentech' from 'arrowgreentech.com')."""
    clean_domain = domain.strip().lower()
    # Strip protocol if present
    clean_domain = clean_domain.replace("https://", "").replace("http://", "").split("/")[0]
    parts = clean_domain.split(".")
    if len(parts) >= 2:
        return parts[0]
    return clean_domain


@dataclass
class SupplierAuditItem:
    """Audit findings for an individual supplier profile."""
    supplier_id: str
    company_name: str
    domain: str
    is_name_duplicate: bool = False
    is_domain_duplicate: bool = False
    missing_fields: List[str] = field(default_factory=list)
    has_description: bool = True
    description_length: int = 0
    has_employee_range: bool = True
    employee_range: Optional[str] = None
    has_headquarters_city: bool = False
    headquarters_city: Optional[str] = None
    has_headquarters_country: bool = False
    headquarters_country: Optional[str] = None
    suspicious_domain_mismatch: bool = False
    mismatch_reason: Optional[str] = None


@dataclass
class DataQualityAuditReport:
    """Aggregated data quality audit report for a batch of supplier records."""
    total_records: int
    unique_domains: int
    unique_company_names: int
    duplicate_domains: List[str]
    duplicate_names: List[str]
    missing_fields_counts: Dict[str, int]
    suspicious_mismatches_count: int
    flagged_suppliers_for_review: List[Dict[str, Any]]
    supplier_audit_items: List[SupplierAuditItem]


class SupplierQualityAuditor:
    """Audits supplier records for completeness, duplicates, and anomalies."""

    def audit_records(self, suppliers: List[SupplierProfile]) -> DataQualityAuditReport:
        """
        Execute deterministic data quality audit across a collection of supplier profiles.
        """
        total = len(suppliers)
        domain_counts: Dict[str, int] = {}
        name_counts: Dict[str, int] = {}

        # Count frequencies for duplicate detection
        for s in suppliers:
            d = s.domain.strip().lower()
            n = s.company_name.strip().lower()
            domain_counts[d] = domain_counts.get(d, 0) + 1
            name_counts[n] = name_counts.get(n, 0) + 1

        dup_domains = [d for d, c in domain_counts.items() if c > 1]
        dup_names = [n for n, c in name_counts.items() if c > 1]

        missing_counts = {
            "description": 0,
            "employee_range": 0,
            "headquarters_city": 0,
            "headquarters_country": 0,
            "corporate_contact_email": 0,
            "annual_revenue_usd": 0,
            "certifications": 0,
        }

        audit_items: List[SupplierAuditItem] = []
        flagged_mismatches: List[Dict[str, Any]] = []

        for s in suppliers:
            missing: List[str] = []
            has_desc = bool(s.description and s.description.strip())
            desc_len = len(s.description) if has_desc else 0
            if not has_desc:
                missing.append("description")
                missing_counts["description"] += 1

            has_emp = bool(s.employee_range or s.employee_count)
            if not has_emp:
                missing.append("employee_range")
                missing_counts["employee_range"] += 1

            has_city = bool(s.headquarters_city and s.headquarters_city.strip())
            if not has_city:
                missing.append("headquarters_city")
                missing_counts["headquarters_city"] += 1

            has_country = bool(s.headquarters_country and s.headquarters_country.strip() and s.headquarters_country != "Unknown")
            if not has_country:
                missing.append("headquarters_country")
                missing_counts["headquarters_country"] += 1

            if not s.corporate_contact_email:
                missing.append("corporate_contact_email")
                missing_counts["corporate_contact_email"] += 1

            if not s.annual_revenue_usd:
                missing.append("annual_revenue_usd")
                missing_counts["annual_revenue_usd"] += 1

            if not s.certifications:
                missing.append("certifications")
                missing_counts["certifications"] += 1

            # Check domain / company name mismatch
            is_mismatch, reason = self._detect_domain_mismatch(s.company_name, s.domain)
            if is_mismatch:
                flagged_mismatches.append({
                    "supplier_id": s.supplier_id,
                    "company_name": s.company_name,
                    "domain": s.domain,
                    "reason": reason,
                    "action": "FLAGGED_FOR_MANUAL_REVIEW",
                })

            item = SupplierAuditItem(
                supplier_id=s.supplier_id,
                company_name=s.company_name,
                domain=s.domain,
                is_name_duplicate=(name_counts[s.company_name.strip().lower()] > 1),
                is_domain_duplicate=(domain_counts[s.domain.strip().lower()] > 1),
                missing_fields=missing,
                has_description=has_desc,
                description_length=desc_len,
                has_employee_range=has_emp,
                employee_range=s.employee_range,
                has_headquarters_city=has_city,
                headquarters_city=s.headquarters_city,
                has_headquarters_country=has_country,
                headquarters_country=s.headquarters_country,
                suspicious_domain_mismatch=is_mismatch,
                mismatch_reason=reason,
            )
            audit_items.append(item)

        return DataQualityAuditReport(
            total_records=total,
            unique_domains=len(domain_counts),
            unique_company_names=len(name_counts),
            duplicate_domains=dup_domains,
            duplicate_names=dup_names,
            missing_fields_counts=missing_counts,
            suspicious_mismatches_count=len(flagged_mismatches),
            flagged_suppliers_for_review=flagged_mismatches,
            supplier_audit_items=audit_items,
        )

    def _detect_domain_mismatch(self, company_name: str, domain: str) -> tuple[bool, Optional[str]]:
        """
        Detect discrepancies between company name and domain.
        Returns (is_suspicious, reason).
        """
        stem = extract_domain_stem(domain)
        name_tokens = normalize_tokens(company_name)

        if not name_tokens:
            return False, None

        # 1. Direct stem match in name tokens
        for token in name_tokens:
            if token in stem or stem in token:
                return False, None

        # 2. Check acronym / initialism (e.g. ITPL for Innovative Tech Pack Limited)
        initials = "".join([w[0] for w in re.findall(r"[a-zA-Z]", company_name.upper())])
        words = [w for w in re.sub(r"[^a-zA-Z\s]", " ", company_name).split() if w]
        acronym = "".join([w[0].lower() for w in words])
        if acronym in stem or stem in acronym:
            return False, None

        # 3. Concatenated words check (e.g. "Arrow Green Tech" vs "arrowgreentech")
        compact_name = re.sub(r"[^a-zA-Z0-9]", "", company_name.lower())
        for token in name_tokens:
            if token in stem:
                return False, None

        # If domain has zero resemblance to any part of the company name
        return True, (
            f"Domain stem '{stem}' shares no recognizable tokens, initials, or brand elements "
            f"with company name '{company_name}'. Requires manual verification."
        )
