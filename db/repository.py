"""
VendorIQ Repository Layer.

Provides safe, parameterized CRUD operations and domain caching
for SourcingBriefs, SupplierProfiles, ScoringRuns, SupplierEvaluations, and RFQDrafts.
Ensures transaction safety, provenance retention, and duplicate resolution by company domain.
"""

from datetime import datetime, timezone, date
import json
import logging
import os
import re
import sqlite3
from typing import Optional, List, Dict, Any, Tuple, Set
import uuid

from core.models import (
    SourcingBrief,
    SourcingCriteria,
    SupplierProfile,
    Certification,
    DataField,
    ScoringWeights,
    ScoringDimension,
    FactorScore,
    SupplierScore,
    RFQDraft,
    EvidenceCategory,
    EvidenceStatus,
    SupplierEvidenceRecord,
    ShortlistStatus,
    ShortlistItem,
    SupplierShortlist,
    RFQStatus,
    VALID_RFQ_TRANSITIONS,
    RFQAuditEntry,
    RFQBuyerInfo,
    RFQProductRequirements,
    RFQCommercialRequirements,
    RFQSupplierTarget,
    RFQDocumentRecord,
    QuotationStatus,
    VALID_QUOTATION_TRANSITIONS,
    QuotationCommercialData,
    QuotationTechnicalCompliance,
    QuotationAuditEntry,
    SupplierQuotation,
    ExtractionFieldStatus,
    FieldExtractionProvenance,
)
from core.exceptions import (
    DataValidationError,
    MissingSupplierDataError,
)
from core.provenance import ProvenanceClassification
from db.connection import get_db_connection, init_db, get_default_db_path

logger = logging.getLogger(__name__)


def _normalize_text(text: Optional[str]) -> str:
    """Normalize text: lowercase, replace & with and, strip punctuation and collapse whitespace."""
    if not text:
        return ""
    t = text.lower()
    t = t.replace("&", " and ")
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return " ".join(t.split())


def _extract_buyer_prompt(description: Optional[str]) -> str:
    """Extract raw buyer prompt if present in standard copilot description wrapper."""
    if not description:
        return ""
    m = re.search(r"buyer input:\s*(.*?)(?:\.\s*focuses on|\.\.|$)", description, re.IGNORECASE)
    if m:
        return _normalize_text(m.group(1))
    return _normalize_text(description)


def _normalize_title_or_category(title: Optional[str]) -> str:
    """Normalize project title, removing generic project suffixes."""
    norm = _normalize_text(title)
    for suffix in [
        " sourcing project",
        " sourcing brief",
        " procurement project",
        " procurement brief",
        " project",
        " brief",
    ]:
        if norm.endswith(suffix):
            norm = norm[: -len(suffix)].strip()
    return norm


def _normalize_headcount_max(max_size: Optional[int]) -> Optional[int]:
    """
    Normalize maximum company size.
    In VendorIQ, None, null, 0, or 2000 (the catalog/UI default upper bound)
    represent an unconstrained upper headcount limit.
    """
    if max_size is None or max_size <= 0 or max_size >= 2000:
        return None
    return max_size


def _canonicalize_certifications(
    certs: Any,
    description: Optional[str] = "",
    notes: Optional[str] = "",
    title: Optional[str] = "",
) -> Tuple[Set[str], bool]:
    """
    Extract canonical accredited certifications and sustainability documentary flags.
    Returns:
        (named_standards_set, has_sustainability_requirement_bool)
    """
    named_standards = set()
    has_sustainability = False

    cert_list = []
    if isinstance(certs, (list, tuple, set)):
        cert_list = list(certs)
    elif isinstance(certs, str):
        try:
            parsed = json.loads(certs)
            if isinstance(parsed, list):
                cert_list = parsed
            else:
                cert_list = [certs]
        except Exception:
            cert_list = [certs]

    all_cert_strings = [str(c).strip() for c in cert_list if str(c).strip()]

    # Formal accredited standards
    for cert_str in all_cert_strings:
        cnorm = _normalize_text(cert_str)
        if re.search(r"\biso[- ]?9001\b", cnorm):
            named_standards.add("iso 9001")
        elif re.search(r"\biso[- ]?14001\b", cnorm):
            named_standards.add("iso 14001")
        elif re.search(r"\biso[- ]?22000\b", cnorm):
            named_standards.add("iso 22000")
        elif re.search(r"\bbrc(?:gs)?\b", cnorm):
            named_standards.add("brc packaging")
        elif re.search(r"\b(?:fda|food\s*contact|food\s*grade)\b", cnorm):
            named_standards.add("food grade compliance")
        elif re.search(r"\bfsc\b", cnorm):
            named_standards.add("fsc")
        elif re.search(r"\bas9100\b", cnorm):
            named_standards.add("as9100")
        elif re.search(r"\biatf[- ]?16949\b", cnorm):
            named_standards.add("iatf 16949")
        elif any(w in cnorm for w in ["sustainability", "sustainable", "documentary evidence", "buyer requirement"]):
            has_sustainability = True
        elif len(cnorm) >= 3:
            named_standards.add(cnorm)

    # Check if sustainability certifications or intent is present in description/notes/title
    combined_desc = (description or "") + " " + (notes or "") + " " + (title or "")
    if (
        re.search(r"\b(?:sustainab\w*|eco[- ]friendly|biodegradab\w*|compostab\w*)\b", combined_desc, re.IGNORECASE)
        or re.search(r"documentary\s+evidence", combined_desc, re.IGNORECASE)
    ):
        has_sustainability = True

    return named_standards, has_sustainability


def _canonicalize_specifications(
    description: Optional[str] = "",
    notes: Optional[str] = "",
) -> Set[str]:
    """Extract key technical specifications mentioned in description or notes."""
    specs = set()
    combined_norm = _normalize_text((description or "") + " " + (notes or ""))
    if re.search(r"\bbarrier\b", combined_norm):
        specs.add("barrier")
    if re.search(r"\bcorrugated\b|\bcartons?\b", combined_norm):
        specs.add("corrugated")
    if re.search(r"\bbiodegradable\b|\bcompostable\b", combined_norm):
        specs.add("biodegradable")
    if re.search(r"\b(?:tin|metal)\s*cans?\b", combined_norm):
        specs.add("metal_tin")
    return specs


def are_briefs_semantically_identical(b1: SourcingBrief, b2: SourcingBrief) -> bool:
    """
    Determine if two sourcing briefs represent the same underlying procurement requirement.
    Handles variations in:
    - Title casing, punctuation (& vs and), and generic project suffixes
    - None/null vs unconstrained maximum company size (e.g. 2000 vs None)
    - Empty certification lists vs generic documentary sustainability requirements
    - Equivalent specification and certification wording
    Does NOT merge briefs with genuinely different industries, countries, headcount thresholds,
    or distinct formal certifications.
    """
    # 1. Buyer Organization (if both specified and non-empty, must match)
    org1 = _normalize_text(b1.buyer_organization)
    org2 = _normalize_text(b2.buyer_organization)
    if org1 and org2 and org1 != org2:
        return False

    # 2. Extracted Prompt Match (Strongest signal from copilot input)
    p1 = _extract_buyer_prompt(b1.description)
    p2 = _extract_buyer_prompt(b2.description)
    prompt_match = bool(p1 and p2 and p1 == p2)

    # 3. Title / Category match
    t1 = _normalize_title_or_category(b1.title)
    t2 = _normalize_title_or_category(b2.title)
    title_match = bool(t1 == t2 or (t1 and t2 and (t1 in t2 or t2 in t1)))

    if not (prompt_match or title_match):
        return False

    # 4. Target Industries & Geographies
    c1 = b1.criteria
    c2 = b2.criteria
    ind1 = {_normalize_text(x) for x in c1.target_industries if x}
    ind2 = {_normalize_text(x) for x in c2.target_industries if x}
    if ind1 and ind2 and not (ind1 & ind2):
        return False

    geo1 = {_normalize_text(x) for x in c1.target_geographies if x}
    geo2 = {_normalize_text(x) for x in c2.target_geographies if x}
    if geo1 and geo2 and not (geo1 & geo2):
        return False

    # 5. Headcount Thresholds
    min1 = c1.min_company_size
    min2 = c2.min_company_size
    if min1 != min2:
        return False

    max1 = _normalize_headcount_max(c1.max_company_size)
    max2 = _normalize_headcount_max(c2.max_company_size)
    if max1 != max2:
        return False

    # 6. Certifications & Sustainability Requirements
    stds1, sust1 = _canonicalize_certifications(c1.required_certifications, b1.description, b1.notes, b1.title)
    stds2, sust2 = _canonicalize_certifications(c2.required_certifications, b2.description, b2.notes, b2.title)
    if stds1 != stds2 or sust1 != sust2:
        return False

    # 7. Key Specifications
    specs1 = _canonicalize_specifications(b1.description, b1.notes)
    specs2 = _canonicalize_specifications(b2.description, b2.notes)
    if specs1 and specs2 and not (specs1 & specs2):
        return False

    return True


class VendorIQRepository:
    """
    Repository class encapsulating database access, SQL execution,
    and Pydantic model serialization/deserialization.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path if db_path is not None else get_default_db_path()
        base_name = os.path.basename(str(self.db_path)).lower() if self.db_path else ""
        if base_name == "vendoriq.sqlite":
            logger.info("VendorIQRepository initialized against PRODUCTION database: %s", self.db_path)
        else:
            logger.debug("VendorIQRepository initialized against isolated database: %s", self.db_path)

    def initialize(self) -> None:
        """Provision database schema if not already present."""
        init_db(self.db_path)

    # =========================================================================
    # SOURCING BRIEFS
    # =========================================================================

    def find_duplicate_brief(self, brief: SourcingBrief) -> Optional[SourcingBrief]:
        """
        Check if an identical brief already exists based on title, buyer organization,
        and semantic criteria (industries, geographies, company size ranges, and certifications).
        Orders by creation time ascending to always return the original canonical brief.
        """
        resolved_db_path = os.path.abspath(self.db_path if self.db_path is not None else get_default_db_path())
        query = "SELECT * FROM sourcing_briefs ORDER BY created_at ASC;"
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query)
            rows = cur.fetchall()
            candidate_ids = [r["brief_id"] for r in rows]
            diag_header = (
                f"[DEDUPLICATION DIAGNOSTIC] Active SQLite path: {resolved_db_path} | "
                f"Candidate brief IDs: {candidate_ids} | "
                f"Incoming brief: ID={brief.brief_id}, Title='{brief.title}'"
            )
            logger.info(diag_header)
            print(diag_header, flush=True)

            for row in rows:
                try:
                    existing_brief = self._row_to_brief(row)
                    is_match = are_briefs_semantically_identical(existing_brief, brief)
                    diag_comp = (
                        f"[DEDUPLICATION DIAGNOSTIC] Comparing: '{existing_brief.brief_id}' ('{existing_brief.title}') "
                        f"vs incoming '{brief.brief_id}' ('{brief.title}') -> MATCH: {is_match}"
                    )
                    logger.info(diag_comp)
                    print(diag_comp, flush=True)

                    if is_match:
                        diag_found = (
                            f"[DEDUPLICATION DIAGNOSTIC] Duplicate detection result: MATCH FOUND! "
                            f"Reusing canonical brief '{existing_brief.brief_id}'"
                        )
                        logger.info(diag_found)
                        print(diag_found, flush=True)
                        return existing_brief
                except Exception as exc:
                    logger.error(
                        f"[DEDUPLICATION DIAGNOSTIC] Error comparing candidate '{row['brief_id']}': {exc}",
                        exc_info=True,
                    )
                    print(
                        f"[DEDUPLICATION DIAGNOSTIC] Error comparing candidate '{row['brief_id']}': {exc}",
                        flush=True,
                    )
                    continue

        diag_none = f"[DEDUPLICATION DIAGNOSTIC] Duplicate detection result: NO MATCH for '{brief.brief_id}'. Proceeding to insert."
        logger.info(diag_none)
        print(diag_none, flush=True)
        return None

    def create_brief(self, brief: SourcingBrief) -> SourcingBrief:
        """
        Insert a new SourcingBrief, or return an existing brief if an identical one
        (same title, buyer organization, and normalized criteria) is already present.
        """
        existing = self.find_duplicate_brief(brief)
        if existing is not None:
            diag_ret = f"[DEDUPLICATION DIAGNOSTIC] create_brief() returning existing brief ID: '{existing.brief_id}'"
            logger.info(diag_ret)
            print(diag_ret, flush=True)
            return existing

        b_min = brief.budget_range_usd[0] if brief.budget_range_usd else None
        b_max = brief.budget_range_usd[1] if brief.budget_range_usd else None
        criteria_json = brief.criteria.model_dump_json()

        query = """
            INSERT INTO sourcing_briefs (
                brief_id, title, buyer_organization, description,
                criteria_json, budget_min, budget_max, timeline_weeks,
                notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            brief.brief_id,
            brief.title,
            brief.buyer_organization,
            brief.description,
            criteria_json,
            b_min,
            b_max,
            brief.timeline_weeks,
            brief.notes,
            brief.created_at.isoformat(),
        )

        with get_db_connection(self.db_path) as conn:
            try:
                conn.execute(query, params)
            except sqlite3.IntegrityError as e:
                raise DataValidationError(f"Failed to create brief '{brief.brief_id}': {e}") from e

        return brief

    def get_brief(self, brief_id: str) -> Optional[SourcingBrief]:
        """Retrieve a SourcingBrief by ID, or None if not found."""
        query = "SELECT * FROM sourcing_briefs WHERE brief_id = ?;"
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query, (brief_id,))
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_brief(row)

    def list_briefs(self, limit: int = 100, offset: int = 0) -> List[SourcingBrief]:
        """List sourcing briefs ordered by creation time descending."""
        query = "SELECT * FROM sourcing_briefs ORDER BY created_at DESC LIMIT ? OFFSET ?;"
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query, (limit, offset))
            rows = cur.fetchall()
            return [self._row_to_brief(r) for r in rows]

    def update_brief(self, brief: SourcingBrief) -> Optional[SourcingBrief]:
        """Update an existing SourcingBrief. Returns updated brief or None if not found."""
        b_min = brief.budget_range_usd[0] if brief.budget_range_usd else None
        b_max = brief.budget_range_usd[1] if brief.budget_range_usd else None
        criteria_json = brief.criteria.model_dump_json()

        query = """
            UPDATE sourcing_briefs
            SET title = ?, buyer_organization = ?, description = ?,
                criteria_json = ?, budget_min = ?, budget_max = ?,
                timeline_weeks = ?, notes = ?
            WHERE brief_id = ?;
        """
        params = (
            brief.title,
            brief.buyer_organization,
            brief.description,
            criteria_json,
            b_min,
            b_max,
            brief.timeline_weeks,
            brief.notes,
            brief.brief_id,
        )

        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query, params)
            if cur.rowcount == 0:
                return None

        return brief

    def delete_brief(self, brief_id: str) -> bool:
        """Delete a SourcingBrief by ID. Returns True if deleted, False if not found."""
        query = "DELETE FROM sourcing_briefs WHERE brief_id = ?;"
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query, (brief_id,))
            return cur.rowcount > 0

    # =========================================================================
    # SUPPLIER PROFILES (with domain-based caching & duplicate handling)
    # =========================================================================

    def create_supplier(self, supplier: SupplierProfile) -> SupplierProfile:
        """
        Insert a new SupplierProfile.
        Enforces domain uniqueness. If domain exists, raises DataValidationError.
        """
        normalized_domain = supplier.domain.strip().lower()
        now_iso = datetime.now(timezone.utc).isoformat()

        # Serialize nested collections
        industries_json = json.dumps(supplier.industries)
        certs_json = json.dumps([c.model_dump(mode="json") for c in supplier.certifications])
        signals_json = json.dumps(supplier.business_signals)
        attrs_json = json.dumps({k: v.model_dump(mode="json") for k, v in supplier.attributes.items()})

        query = """
            INSERT INTO supplier_profiles (
                supplier_id, company_name, domain, description,
                headquarters_country, headquarters_city, employee_count,
                employee_range, industries_json, certifications_json,
                annual_revenue_usd, year_founded, business_signals_json,
                corporate_contact_email, corporate_website, attributes_json,
                is_synthetic, overall_provenance, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            supplier.supplier_id,
            supplier.company_name,
            normalized_domain,
            supplier.description,
            supplier.headquarters_country,
            supplier.headquarters_city,
            supplier.employee_count,
            supplier.employee_range,
            industries_json,
            certs_json,
            supplier.annual_revenue_usd,
            supplier.year_founded,
            signals_json,
            supplier.corporate_contact_email,
            supplier.corporate_website,
            attrs_json,
            1 if supplier.is_synthetic else 0,
            supplier.overall_provenance.value,
            now_iso,
            now_iso,
        )

        with get_db_connection(self.db_path) as conn:
            try:
                conn.execute(query, params)
            except sqlite3.IntegrityError as e:
                if "UNIQUE constraint failed: supplier_profiles.domain" in str(e):
                    raise DataValidationError(
                        f"Duplicate supplier domain: '{supplier.domain}' is already registered."
                    ) from e
                raise DataValidationError(f"Failed to create supplier profile: {e}") from e

        return supplier

    def upsert_supplier(self, supplier: SupplierProfile) -> SupplierProfile:
        """
        Insert or update a SupplierProfile using company domain as unique key.
        If a record with the same domain exists, updates all company attributes
        and timestamps, preserving the existing record identifier.
        """
        existing = self.get_supplier_by_domain(supplier.domain)
        if not existing:
            return self.create_supplier(supplier)

        # Update existing record
        target_id = existing.supplier_id
        now_iso = datetime.now(timezone.utc).isoformat()

        industries_json = json.dumps(supplier.industries)
        certs_json = json.dumps([c.model_dump(mode="json") for c in supplier.certifications])
        signals_json = json.dumps(supplier.business_signals)
        attrs_json = json.dumps({k: v.model_dump(mode="json") for k, v in supplier.attributes.items()})

        query = """
            UPDATE supplier_profiles
            SET company_name = ?, description = ?, headquarters_country = ?,
                headquarters_city = ?, employee_count = ?, employee_range = ?,
                industries_json = ?, certifications_json = ?, annual_revenue_usd = ?,
                year_founded = ?, business_signals_json = ?, corporate_contact_email = ?,
                corporate_website = ?, attributes_json = ?, is_synthetic = ?,
                overall_provenance = ?, updated_at = ?
            WHERE supplier_id = ?;
        """
        params = (
            supplier.company_name,
            supplier.description,
            supplier.headquarters_country,
            supplier.headquarters_city,
            supplier.employee_count,
            supplier.employee_range,
            industries_json,
            certs_json,
            supplier.annual_revenue_usd,
            supplier.year_founded,
            signals_json,
            supplier.corporate_contact_email,
            supplier.corporate_website,
            attrs_json,
            1 if supplier.is_synthetic else 0,
            supplier.overall_provenance.value,
            now_iso,
            target_id,
        )

        with get_db_connection(self.db_path) as conn:
            conn.execute(query, params)

        # Return updated supplier with target_id
        updated_dict = supplier.model_dump()
        updated_dict["supplier_id"] = target_id
        return SupplierProfile(**updated_dict)

    def get_supplier_by_id(self, supplier_id: str) -> Optional[SupplierProfile]:
        """Retrieve a SupplierProfile by ID, or None if not found."""
        query = "SELECT * FROM supplier_profiles WHERE supplier_id = ?;"
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query, (supplier_id,))
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_supplier(row)

    get_supplier = get_supplier_by_id

    def get_supplier_by_domain(self, domain: str) -> Optional[SupplierProfile]:
        """
        Cache lookup: Retrieve a SupplierProfile by domain.
        Returns cached SupplierProfile if present, or None if missing.
        """
        normalized_domain = domain.strip().lower()
        query = "SELECT * FROM supplier_profiles WHERE domain = ?;"
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query, (normalized_domain,))
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_supplier(row)

    def list_suppliers(self, limit: int = 100, offset: int = 0) -> List[SupplierProfile]:
        """List suppliers ordered by company name ascending."""
        query = "SELECT * FROM supplier_profiles ORDER BY company_name ASC LIMIT ? OFFSET ?;"
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query, (limit, offset))
            rows = cur.fetchall()
            return [self._row_to_supplier(r) for r in rows]

    def delete_supplier(self, supplier_id: str) -> bool:
        """Delete a SupplierProfile by ID. Returns True if deleted, False if not found."""
        query = "DELETE FROM supplier_profiles WHERE supplier_id = ?;"
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query, (supplier_id,))
            return cur.rowcount > 0

    # =========================================================================
    # SCORING RUNS
    # =========================================================================

    def create_scoring_run(
        self,
        brief_id: str,
        weights: ScoringWeights,
        notes: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> str:
        """
        Record a new scoring run configured with specific scoring weights.
        Returns the created run_id.
        """
        # Ensure brief exists (foreign key check verification)
        if not self.get_brief(brief_id):
            raise MissingSupplierDataError(f"Cannot create scoring run: brief_id '{brief_id}' not found.")

        target_run_id = run_id or str(uuid.uuid4())
        weights_json = weights.model_dump_json()
        now_iso = datetime.now(timezone.utc).isoformat()

        query = """
            INSERT INTO scoring_runs (run_id, brief_id, weights_json, notes, created_at)
            VALUES (?, ?, ?, ?, ?);
        """
        with get_db_connection(self.db_path) as conn:
            try:
                conn.execute(query, (target_run_id, brief_id, weights_json, notes, now_iso))
            except sqlite3.IntegrityError as e:
                raise DataValidationError(f"Failed to record scoring run: {e}") from e

        return target_run_id

    def get_scoring_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve scoring run metadata and scoring weights by run_id."""
        query = "SELECT * FROM scoring_runs WHERE run_id = ?;"
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query, (run_id,))
            row = cur.fetchone()
            if not row:
                return None
            weights_data = json.loads(row["weights_json"])
            return {
                "run_id": row["run_id"],
                "brief_id": row["brief_id"],
                "weights": ScoringWeights(**weights_data),
                "notes": row["notes"],
                "created_at": datetime.fromisoformat(row["created_at"]),
            }

    def list_scoring_runs_for_brief(self, brief_id: str) -> List[Dict[str, Any]]:
        """List all scoring runs associated with a specific brief."""
        query = "SELECT * FROM scoring_runs WHERE brief_id = ? ORDER BY created_at DESC;"
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query, (brief_id,))
            rows = cur.fetchall()
            results = []
            for r in rows:
                weights_data = json.loads(r["weights_json"])
                results.append({
                    "run_id": r["run_id"],
                    "brief_id": r["brief_id"],
                    "weights": ScoringWeights(**weights_data),
                    "notes": r["notes"],
                    "created_at": datetime.fromisoformat(r["created_at"]),
                })
            return results

    # =========================================================================
    # SUPPLIER EVALUATIONS
    # =========================================================================

    def create_evaluation(
        self,
        run_id: str,
        score: SupplierScore,
        evaluation_id: Optional[str] = None,
    ) -> str:
        """
        Record a supplier scorecard within a scoring run.
        Persists composite total_score, completeness %, individual factor scores with provenance.
        """
        target_eval_id = evaluation_id or str(uuid.uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()

        # Serialize factor scores map: dimension -> FactorScore JSON
        factor_scores_dict = {
            dim.value: factor.model_dump(mode="json")
            for dim, factor in score.factor_scores.items()
        }
        factor_scores_json = json.dumps(factor_scores_dict)

        query = """
            INSERT INTO supplier_evaluations (
                evaluation_id, run_id, supplier_id, total_score,
                data_completeness_percentage, factor_scores_json,
                summary, provenance, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            target_eval_id,
            run_id,
            score.supplier_id,
            score.total_score,
            score.data_completeness_percentage,
            factor_scores_json,
            score.summary,
            score.provenance.value,
            now_iso,
        )

        with get_db_connection(self.db_path) as conn:
            try:
                conn.execute(query, params)
            except sqlite3.IntegrityError as e:
                raise DataValidationError(f"Failed to insert supplier evaluation: {e}") from e

        return target_eval_id

    def get_evaluation(self, evaluation_id: str) -> Optional[SupplierScore]:
        """Retrieve a supplier evaluation by evaluation ID."""
        query = "SELECT * FROM supplier_evaluations WHERE evaluation_id = ?;"
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query, (evaluation_id,))
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_evaluation(row)

    def list_evaluations_for_run(self, run_id: str) -> List[SupplierScore]:
        """List all supplier evaluations for a scoring run, ranked by total_score descending, then company name ascending."""
        query = """
            SELECT se.*, sp.company_name
            FROM supplier_evaluations se
            LEFT JOIN supplier_profiles sp ON se.supplier_id = sp.supplier_id
            WHERE se.run_id = ?
            ORDER BY se.total_score DESC, sp.company_name ASC;
        """
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query, (run_id,))
            rows = cur.fetchall()
            return [self._row_to_evaluation(r) for r in rows]

    def list_evaluations_for_brief(self, brief_id: str) -> List[SupplierScore]:
        """List evaluations for the latest scoring run of a sourcing brief."""
        runs = self.list_scoring_runs_for_brief(brief_id)
        if runs:
            latest_run_id = runs[0]["run_id"]
            return self.list_evaluations_for_run(latest_run_id)
        return []


    # =========================================================================
    # RFQ DOCUMENTS
    # =========================================================================

    def create_rfq(self, rfq: RFQDraft) -> RFQDraft:
        """Insert a new RFQ draft document."""
        now_iso = datetime.now(timezone.utc).isoformat()
        targets_json = json.dumps(rfq.target_supplier_ids)
        tech_reqs_json = json.dumps(rfq.technical_requirements)

        query = """
            INSERT INTO rfq_documents (
                rfq_id, brief_id, target_supplier_ids_json, title,
                scope_of_work, technical_requirements_json, submission_deadline,
                response_instructions, evaluation_criteria_summary, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            rfq.rfq_id,
            rfq.brief_id,
            targets_json,
            rfq.title,
            rfq.scope_of_work,
            tech_reqs_json,
            rfq.submission_deadline.isoformat(),
            rfq.response_instructions,
            rfq.evaluation_criteria_summary,
            rfq.status,
            rfq.created_at.isoformat(),
            now_iso,
        )

        with get_db_connection(self.db_path) as conn:
            try:
                conn.execute(query, params)
            except sqlite3.IntegrityError as e:
                raise DataValidationError(f"Failed to record RFQ draft: {e}") from e

        return rfq

    def get_rfq(self, rfq_id: str) -> Optional[RFQDraft]:
        """Retrieve an RFQ document by ID, or None if not found."""
        query = "SELECT * FROM rfq_documents WHERE rfq_id = ?;"
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query, (rfq_id,))
            row = cur.fetchone()
            if not row:
                return None
            return self._row_to_rfq(row)

    def list_rfqs_for_brief(self, brief_id: str) -> List[RFQDraft]:
        """List all RFQ documents generated for a specific sourcing brief."""
        query = "SELECT * FROM rfq_documents WHERE brief_id = ? ORDER BY created_at DESC;"
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query, (brief_id,))
            rows = cur.fetchall()
            return [self._row_to_rfq(r) for r in rows]

    def update_rfq_status(self, rfq_id: str, status: str) -> Optional[RFQDraft]:
        """Update status of an RFQ document (e.g. DRAFT -> READY_FOR_REVIEW -> SENT)."""
        allowed = {"DRAFT", "READY_FOR_REVIEW", "SENT", "ARCHIVED"}
        norm_status = status.strip().upper()
        if norm_status not in allowed:
            raise DataValidationError(f"Invalid status '{status}'. Must be one of {allowed}.")

        now_iso = datetime.now(timezone.utc).isoformat()
        query = """
            UPDATE rfq_documents
            SET status = ?, updated_at = ?
            WHERE rfq_id = ?;
        """
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query, (norm_status, now_iso, rfq_id))
            if cur.rowcount == 0:
                return None

        return self.get_rfq(rfq_id)

    def delete_rfq(self, rfq_id: str) -> bool:
        """Delete an RFQ document by ID. Returns True if deleted, False if not found."""
        query = "DELETE FROM rfq_documents WHERE rfq_id = ?;"
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query, (rfq_id,))
            return cur.rowcount > 0

    # =========================================================================
    # INTERNAL ROW DESERIALIZERS
    # =========================================================================

    def _row_to_brief(self, row: sqlite3.Row) -> SourcingBrief:
        criteria_data = json.loads(row["criteria_json"])
        criteria = SourcingCriteria(**criteria_data)

        b_min = row["budget_min"]
        b_max = row["budget_max"]
        budget_range = (b_min, b_max) if b_min is not None and b_max is not None else None

        return SourcingBrief(
            brief_id=row["brief_id"],
            title=row["title"],
            buyer_organization=row["buyer_organization"],
            description=row["description"],
            criteria=criteria,
            budget_range_usd=budget_range,
            timeline_weeks=row["timeline_weeks"],
            notes=row["notes"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _row_to_supplier(self, row: sqlite3.Row) -> SupplierProfile:
        industries = json.loads(row["industries_json"]) if row["industries_json"] else []
        certs_raw = json.loads(row["certifications_json"]) if row["certifications_json"] else []
        signals = json.loads(row["business_signals_json"]) if row["business_signals_json"] else []
        attrs_raw = json.loads(row["attributes_json"]) if row["attributes_json"] else {}

        certifications = [Certification(**c) for c in certs_raw]
        attributes = {k: DataField(**v) for k, v in attrs_raw.items()}

        return SupplierProfile(
            supplier_id=row["supplier_id"],
            company_name=row["company_name"],
            domain=row["domain"],
            description=row["description"],
            headquarters_country=row["headquarters_country"],
            headquarters_city=row["headquarters_city"],
            employee_count=row["employee_count"],
            employee_range=row["employee_range"],
            industries=industries,
            certifications=certifications,
            annual_revenue_usd=row["annual_revenue_usd"],
            year_founded=row["year_founded"],
            business_signals=signals,
            corporate_contact_email=row["corporate_contact_email"],
            corporate_website=row["corporate_website"],
            attributes=attributes,
            is_synthetic=bool(row["is_synthetic"]),
            overall_provenance=ProvenanceClassification(row["overall_provenance"]),
        )

    def _row_to_evaluation(self, row: sqlite3.Row) -> SupplierScore:
        factors_raw = json.loads(row["factor_scores_json"])
        factor_scores: Dict[ScoringDimension, FactorScore] = {}
        for dim_str, f_data in factors_raw.items():
            dim = ScoringDimension(dim_str)
            factor_scores[dim] = FactorScore(**f_data)

        # Retrieve supplier company name (from JOIN or fallback query)
        company_name = None
        if "company_name" in row.keys() and row["company_name"]:
            company_name = row["company_name"]
        else:
            sup = self.get_supplier_by_id(row["supplier_id"])
            company_name = sup.company_name if sup else "Unknown Supplier"

        return SupplierScore(
            supplier_id=row["supplier_id"],
            company_name=company_name,
            total_score=row["total_score"],
            factor_scores=factor_scores,
            data_completeness_percentage=row["data_completeness_percentage"],
            summary=row["summary"],
            provenance=ProvenanceClassification(row["provenance"]),
        )

    def _row_to_rfq(self, row: sqlite3.Row) -> RFQDraft:
        target_ids = json.loads(row["target_supplier_ids_json"])
        tech_reqs = json.loads(row["technical_requirements_json"])

        return RFQDraft(
            rfq_id=row["rfq_id"],
            brief_id=row["brief_id"],
            target_supplier_ids=target_ids,
            title=row["title"],
            scope_of_work=row["scope_of_work"],
            technical_requirements=tech_reqs,
            submission_deadline=datetime.fromisoformat(row["submission_deadline"]),
            response_instructions=row["response_instructions"],
            evaluation_criteria_summary=row["evaluation_criteria_summary"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # =========================================================================
    # Phase 9: Supplier Evidence & Intelligence CRUD
    # =========================================================================

    def create_evidence(self, record: SupplierEvidenceRecord) -> SupplierEvidenceRecord:
        """
        Persist a new supplier evidence record in SQLite.
        Enforces Truth in Evidence boundaries and foreign key constraints.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        verified_iso = record.verified_at.isoformat() if record.verified_at else None

        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO supplier_evidence (
                    evidence_id, supplier_id, category, claim, status,
                    source_name, source_url, verified_at, reviewer_notes,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.evidence_id,
                    record.supplier_id,
                    record.category.value if hasattr(record.category, "value") else str(record.category),
                    record.claim,
                    record.status.value if hasattr(record.status, "value") else str(record.status),
                    record.source_name,
                    record.source_url,
                    verified_iso,
                    record.reviewer_notes,
                    record.created_at.isoformat() if record.created_at else now_iso,
                    record.updated_at.isoformat() if record.updated_at else now_iso,
                ),
            )
        return record

    def upsert_evidence(self, record: SupplierEvidenceRecord) -> SupplierEvidenceRecord:
        """
        Insert or update a supplier evidence record based on (supplier_id, category).
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        verified_iso = record.verified_at.isoformat() if record.verified_at else None

        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO supplier_evidence (
                    evidence_id, supplier_id, category, claim, status,
                    source_name, source_url, verified_at, reviewer_notes,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(supplier_id, category) DO UPDATE SET
                    claim = excluded.claim,
                    status = excluded.status,
                    source_name = excluded.source_name,
                    source_url = excluded.source_url,
                    verified_at = excluded.verified_at,
                    reviewer_notes = excluded.reviewer_notes,
                    updated_at = excluded.updated_at
                """,
                (
                    record.evidence_id,
                    record.supplier_id,
                    record.category.value if hasattr(record.category, "value") else str(record.category),
                    record.claim,
                    record.status.value if hasattr(record.status, "value") else str(record.status),
                    record.source_name,
                    record.source_url,
                    verified_iso,
                    record.reviewer_notes,
                    record.created_at.isoformat() if record.created_at else now_iso,
                    now_iso,
                ),
            )
        return record

    def get_evidence(self, evidence_id: str) -> Optional[SupplierEvidenceRecord]:
        """Retrieve a specific evidence record by ID."""
        with get_db_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM supplier_evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            if not row:
                return None
            return self._row_to_evidence(row)

    def list_evidence_for_supplier(self, supplier_id: str) -> List[SupplierEvidenceRecord]:
        """Retrieve all evidence records for a given supplier profile."""
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM supplier_evidence WHERE supplier_id = ? ORDER BY category ASC",
                (supplier_id,),
            ).fetchall()
            return [self._row_to_evidence(r) for r in rows]

    def list_all_evidence(self) -> List[SupplierEvidenceRecord]:
        """Retrieve all evidence records across all suppliers."""
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM supplier_evidence ORDER BY supplier_id ASC, category ASC"
            ).fetchall()
            return [self._row_to_evidence(r) for r in rows]

    def delete_evidence(self, evidence_id: str) -> bool:
        """Delete a single evidence record by ID."""
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute("DELETE FROM supplier_evidence WHERE evidence_id = ?", (evidence_id,))
            return cur.rowcount > 0

    def delete_evidence_for_supplier(self, supplier_id: str) -> int:
        """Delete all evidence records for a given supplier."""
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute("DELETE FROM supplier_evidence WHERE supplier_id = ?", (supplier_id,))
            return cur.rowcount

    def _row_to_evidence(self, row: sqlite3.Row) -> SupplierEvidenceRecord:
        verified_at = datetime.fromisoformat(row["verified_at"]) if row["verified_at"] else None
        return SupplierEvidenceRecord(
            evidence_id=row["evidence_id"],
            supplier_id=row["supplier_id"],
            category=EvidenceCategory(row["category"]),
            claim=row["claim"],
            status=EvidenceStatus(row["status"]),
            source_name=row["source_name"],
            source_url=row["source_url"],
            verified_at=verified_at,
            reviewer_notes=row["reviewer_notes"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    # =========================================================================
    # Phase 10: Supplier Shortlists & Shortlist Items CRUD
    # =========================================================================

    def create_shortlist(self, shortlist: SupplierShortlist) -> SupplierShortlist:
        """Create and persist a new supplier shortlist in SQLite."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO supplier_shortlists (
                    shortlist_id, brief_id, name, description, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    shortlist.shortlist_id,
                    shortlist.brief_id,
                    shortlist.name,
                    shortlist.description,
                    shortlist.created_at.isoformat() if shortlist.created_at else now_iso,
                    shortlist.updated_at.isoformat() if shortlist.updated_at else now_iso,
                ),
            )
            # Persist items if provided
            for it in shortlist.items:
                conn.execute(
                    """
                    INSERT INTO shortlist_items (
                        item_id, shortlist_id, supplier_id, status, reviewer_notes, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(shortlist_id, supplier_id) DO UPDATE SET
                        status = excluded.status,
                        reviewer_notes = excluded.reviewer_notes,
                        updated_at = excluded.updated_at
                    """,
                    (
                        it.item_id,
                        shortlist.shortlist_id,
                        it.supplier_id,
                        it.status.value if hasattr(it.status, "value") else str(it.status),
                        it.reviewer_notes,
                        it.created_at.isoformat() if it.created_at else now_iso,
                        it.updated_at.isoformat() if it.updated_at else now_iso,
                    ),
                )
        return self.get_shortlist(shortlist.shortlist_id) or shortlist

    def get_shortlist(self, shortlist_id: str) -> Optional[SupplierShortlist]:
        """Retrieve a specific shortlist by ID, including all items and supplier metadata."""
        with get_db_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM supplier_shortlists WHERE shortlist_id = ?",
                (shortlist_id,),
            ).fetchone()
            if not row:
                return None

            items = self.list_items_for_shortlist(shortlist_id)
            return SupplierShortlist(
                shortlist_id=row["shortlist_id"],
                brief_id=row["brief_id"],
                name=row["name"],
                description=row["description"],
                items=items,
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )

    def list_shortlists_for_brief(self, brief_id: str) -> List[SupplierShortlist]:
        """List all shortlists associated with a specific sourcing brief."""
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM supplier_shortlists WHERE brief_id = ? ORDER BY created_at DESC",
                (brief_id,),
            ).fetchall()
            results = []
            for r in rows:
                items = self.list_items_for_shortlist(r["shortlist_id"])
                results.append(
                    SupplierShortlist(
                        shortlist_id=r["shortlist_id"],
                        brief_id=r["brief_id"],
                        name=r["name"],
                        description=r["description"],
                        items=items,
                        created_at=datetime.fromisoformat(r["created_at"]),
                        updated_at=datetime.fromisoformat(r["updated_at"]),
                    )
                )
            return results

    def list_all_shortlists(self) -> List[SupplierShortlist]:
        """List all shortlists across the system."""
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM supplier_shortlists ORDER BY created_at DESC"
            ).fetchall()
            results = []
            for r in rows:
                items = self.list_items_for_shortlist(r["shortlist_id"])
                results.append(
                    SupplierShortlist(
                        shortlist_id=r["shortlist_id"],
                        brief_id=r["brief_id"],
                        name=r["name"],
                        description=r["description"],
                        items=items,
                        created_at=datetime.fromisoformat(r["created_at"]),
                        updated_at=datetime.fromisoformat(r["updated_at"]),
                    )
                )
            return results

    def update_shortlist(self, shortlist: SupplierShortlist) -> SupplierShortlist:
        """Update shortlist metadata (name, description, timestamp)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                UPDATE supplier_shortlists
                SET name = ?, description = ?, updated_at = ?
                WHERE shortlist_id = ?
                """,
                (shortlist.name, shortlist.description, now_iso, shortlist.shortlist_id),
            )
        return self.get_shortlist(shortlist.shortlist_id) or shortlist

    def delete_shortlist(self, shortlist_id: str) -> bool:
        """Delete a shortlist and cascade delete all its items."""
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute("DELETE FROM supplier_shortlists WHERE shortlist_id = ?", (shortlist_id,))
            return cur.rowcount > 0

    def add_item_to_shortlist(self, item: ShortlistItem) -> ShortlistItem:
        """Add an item to a shortlist."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO shortlist_items (
                    item_id, shortlist_id, supplier_id, status, reviewer_notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.item_id,
                    item.shortlist_id,
                    item.supplier_id,
                    item.status.value if hasattr(item.status, "value") else str(item.status),
                    item.reviewer_notes,
                    item.created_at.isoformat() if item.created_at else now_iso,
                    item.updated_at.isoformat() if item.updated_at else now_iso,
                ),
            )
        return item

    def upsert_shortlist_item(self, item: ShortlistItem) -> ShortlistItem:
        """Insert or update a shortlist item on conflict (shortlist_id, supplier_id)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO shortlist_items (
                    item_id, shortlist_id, supplier_id, status, reviewer_notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(shortlist_id, supplier_id) DO UPDATE SET
                    status = excluded.status,
                    reviewer_notes = excluded.reviewer_notes,
                    updated_at = excluded.updated_at
                """,
                (
                    item.item_id,
                    item.shortlist_id,
                    item.supplier_id,
                    item.status.value if hasattr(item.status, "value") else str(item.status),
                    item.reviewer_notes,
                    item.created_at.isoformat() if item.created_at else now_iso,
                    now_iso,
                ),
            )
        return item

    def update_shortlist_item_status(
        self,
        item_id: str,
        status: ShortlistStatus,
        reviewer_notes: Optional[str] = None,
    ) -> Optional[ShortlistItem]:
        """Update candidate status and optional reviewer notes for a shortlist item."""
        now_iso = datetime.now(timezone.utc).isoformat()
        status_val = status.value if hasattr(status, "value") else str(status)
        with get_db_connection(self.db_path) as conn:
            if reviewer_notes is not None:
                conn.execute(
                    """
                    UPDATE shortlist_items
                    SET status = ?, reviewer_notes = ?, updated_at = ?
                    WHERE item_id = ?
                    """,
                    (status_val, reviewer_notes, now_iso, item_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE shortlist_items
                    SET status = ?, updated_at = ?
                    WHERE item_id = ?
                    """,
                    (status_val, now_iso, item_id),
                )
            row = conn.execute(
                """
                SELECT si.*, sp.company_name, sp.domain
                FROM shortlist_items si
                LEFT JOIN supplier_profiles sp ON si.supplier_id = sp.supplier_id
                WHERE si.item_id = ?
                """,
                (item_id,),
            ).fetchone()
            if not row:
                return None
            return ShortlistItem(
                item_id=row["item_id"],
                shortlist_id=row["shortlist_id"],
                supplier_id=row["supplier_id"],
                company_name=row["company_name"],
                domain=row["domain"],
                status=ShortlistStatus(row["status"]),
                reviewer_notes=row["reviewer_notes"],
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )

    def remove_item_from_shortlist(self, item_id: str) -> bool:
        """Remove an item from a shortlist."""
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute("DELETE FROM shortlist_items WHERE item_id = ?", (item_id,))
            return cur.rowcount > 0

    def list_items_for_shortlist(self, shortlist_id: str) -> List[ShortlistItem]:
        """List all items in a shortlist with supplier company_name and domain joined."""
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT si.*, sp.company_name, sp.domain
                FROM shortlist_items si
                LEFT JOIN supplier_profiles sp ON si.supplier_id = sp.supplier_id
                WHERE si.shortlist_id = ?
                ORDER BY si.created_at ASC
                """,
                (shortlist_id,),
            ).fetchall()
            items = []
            for r in rows:
                items.append(
                    ShortlistItem(
                        item_id=r["item_id"],
                        shortlist_id=r["shortlist_id"],
                        supplier_id=r["supplier_id"],
                        company_name=r["company_name"],
                        domain=r["domain"],
                        status=ShortlistStatus(r["status"]),
                        reviewer_notes=r["reviewer_notes"],
                        created_at=datetime.fromisoformat(r["created_at"]),
                        updated_at=datetime.fromisoformat(r["updated_at"]),
                    )
                )
            return items

    # =========================================================================
    # Phase 11: Supplier-Specific RFQs & Human Approval Workflow
    # =========================================================================

    def _row_to_supplier_rfq(
        self, row: sqlite3.Row, audit_trail: Optional[List[RFQAuditEntry]] = None
    ) -> RFQDocumentRecord:
        buyer_info = RFQBuyerInfo(**json.loads(row["buyer_info_json"]))
        supplier_target = RFQSupplierTarget(**json.loads(row["supplier_target_json"]))
        prod_reqs = RFQProductRequirements(**json.loads(row["product_requirements_json"]))
        comm_reqs = RFQCommercialRequirements(**json.loads(row["commercial_requirements_json"]))
        req_docs = json.loads(row["requested_documents_json"])

        return RFQDocumentRecord(
            rfq_id=row["rfq_id"],
            brief_id=row["brief_id"],
            shortlist_id=row["shortlist_id"],
            supplier_id=row["supplier_id"],
            title=row["title"],
            status=RFQStatus(row["status"]),
            buyer_info=buyer_info,
            supplier=supplier_target,
            product_requirements=prod_reqs,
            commercial_requirements=comm_reqs,
            requested_documents=req_docs,
            docx_path=row["docx_path"],
            pdf_path=row["pdf_path"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            audit_trail=audit_trail or [],
        )

    def create_supplier_rfq(self, rfq: RFQDocumentRecord) -> RFQDocumentRecord:
        """
        Persist a new supplier-specific RFQ in SQLite with initial audit entry.
        Enforces Truth in Evidence and foreign key integrity.
        """
        buyer_json = json.dumps(rfq.buyer_info.model_dump(mode="json"))
        supplier_json = json.dumps(rfq.supplier.model_dump(mode="json"))
        prod_json = json.dumps(rfq.product_requirements.model_dump(mode="json"))
        comm_json = json.dumps(rfq.commercial_requirements.model_dump(mode="json"))
        docs_json = json.dumps(rfq.requested_documents)

        now_iso = datetime.now(timezone.utc).isoformat()
        initial_audit = RFQAuditEntry(
            rfq_id=rfq.rfq_id,
            from_status=None,
            to_status=rfq.status.value,
            changed_by=rfq.buyer_info.contact_person or "Procurement Lead",
            comments="Initial RFQ document prepared and saved as DRAFT.",
            timestamp=datetime.now(timezone.utc),
        )

        query = """
            INSERT INTO supplier_rfqs (
                rfq_id, brief_id, shortlist_id, supplier_id, title,
                status, buyer_info_json, supplier_target_json,
                product_requirements_json, commercial_requirements_json,
                requested_documents_json, docx_path, pdf_path,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            rfq.rfq_id,
            rfq.brief_id,
            rfq.shortlist_id,
            rfq.supplier_id,
            rfq.title,
            rfq.status.value if hasattr(rfq.status, "value") else str(rfq.status),
            buyer_json,
            supplier_json,
            prod_json,
            comm_json,
            docs_json,
            rfq.docx_path,
            rfq.pdf_path,
            rfq.created_at.isoformat(),
            now_iso,
        )

        with get_db_connection(self.db_path) as conn:
            try:
                conn.execute(query, params)
                conn.execute(
                    """
                    INSERT INTO rfq_audit_trail (audit_id, rfq_id, from_status, to_status, changed_by, comments, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        initial_audit.audit_id,
                        initial_audit.rfq_id,
                        initial_audit.from_status,
                        initial_audit.to_status,
                        initial_audit.changed_by,
                        initial_audit.comments,
                        initial_audit.timestamp.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as e:
                raise DataValidationError(f"Failed to record supplier RFQ: {e}") from e

        rfq.audit_trail = [initial_audit]
        return rfq

    def get_supplier_rfq(self, rfq_id: str) -> Optional[RFQDocumentRecord]:
        """Retrieve a supplier RFQ by its identifier with full audit trail."""
        with get_db_connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM supplier_rfqs WHERE rfq_id = ?", (rfq_id,)).fetchone()
            if not row:
                return None
            audit_rows = conn.execute(
                "SELECT * FROM rfq_audit_trail WHERE rfq_id = ? ORDER BY timestamp ASC",
                (rfq_id,),
            ).fetchall()
            audit_trail = [
                RFQAuditEntry(
                    audit_id=ar["audit_id"],
                    rfq_id=ar["rfq_id"],
                    from_status=ar["from_status"],
                    to_status=ar["to_status"],
                    changed_by=ar["changed_by"],
                    comments=ar["comments"],
                    timestamp=datetime.fromisoformat(ar["timestamp"]),
                )
                for ar in audit_rows
            ]
            return self._row_to_supplier_rfq(row, audit_trail=audit_trail)

    def list_supplier_rfqs(
        self,
        brief_id: Optional[str] = None,
        supplier_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[RFQDocumentRecord]:
        """List supplier-specific RFQs with optional filters, sorted newest first."""
        conditions = []
        params = []
        if brief_id:
            conditions.append("brief_id = ?")
            params.append(brief_id)
        if supplier_id:
            conditions.append("supplier_id = ?")
            params.append(supplier_id)
        if status:
            norm_status = status.value if hasattr(status, "value") else str(status)
            conditions.append("status = ?")
            params.append(norm_status)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"SELECT * FROM supplier_rfqs {where_clause} ORDER BY created_at DESC;"

        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            results = []
            for r in rows:
                results.append(self._row_to_supplier_rfq(r))
            return results

    def update_supplier_rfq(self, rfq: RFQDocumentRecord) -> RFQDocumentRecord:
        """Update an existing supplier RFQ's details and file paths."""
        now_iso = datetime.now(timezone.utc).isoformat()
        buyer_json = json.dumps(rfq.buyer_info.model_dump(mode="json"))
        supplier_json = json.dumps(rfq.supplier.model_dump(mode="json"))
        prod_json = json.dumps(rfq.product_requirements.model_dump(mode="json"))
        comm_json = json.dumps(rfq.commercial_requirements.model_dump(mode="json"))
        docs_json = json.dumps(rfq.requested_documents)

        query = """
            UPDATE supplier_rfqs
            SET title = ?,
                buyer_info_json = ?,
                supplier_target_json = ?,
                product_requirements_json = ?,
                commercial_requirements_json = ?,
                requested_documents_json = ?,
                docx_path = ?,
                pdf_path = ?,
                updated_at = ?
            WHERE rfq_id = ?;
        """
        params = (
            rfq.title,
            buyer_json,
            supplier_json,
            prod_json,
            comm_json,
            docs_json,
            rfq.docx_path,
            rfq.pdf_path,
            now_iso,
            rfq.rfq_id,
        )
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query, params)
            if cur.rowcount == 0:
                raise DataValidationError(f"RFQ '{rfq.rfq_id}' not found for update.")

        rfq.updated_at = datetime.fromisoformat(now_iso)
        return rfq

    def transition_rfq_status(
        self,
        rfq_id: str,
        new_status: RFQStatus,
        changed_by: str,
        comments: Optional[str] = None,
    ) -> RFQDocumentRecord:
        """
        Safely transition an RFQ's lifecycle status with mandatory audit logging and guardrails.
        Rejects invalid transitions and enforces human signoff.
        """
        rfq = self.get_supplier_rfq(rfq_id)
        if not rfq:
            raise DataValidationError(f"RFQ '{rfq_id}' not found.")

        current_status = rfq.status
        allowed_targets = VALID_RFQ_TRANSITIONS.get(current_status, [])

        if new_status not in allowed_targets:
            raise DataValidationError(
                f"Invalid RFQ status transition: Cannot transition from '{current_status.value}' to '{new_status.value}'. "
                f"Allowed transitions from '{current_status.value}' are: {[s.value for s in allowed_targets]}."
            )

        # Enforce review comments on approval, rejection, and manual dispatch
        if new_status == RFQStatus.APPROVED and (not comments or not comments.strip()):
            raise DataValidationError("Reviewer comments and approval notes are mandatory to approve an RFQ.")

        if new_status == RFQStatus.REJECTED and (not comments or not comments.strip()):
            raise DataValidationError("Reviewer feedback is mandatory when rejecting an RFQ.")

        if new_status == RFQStatus.SENT and (not comments or not comments.strip()):
            raise DataValidationError("Manual dispatch sign-off notes are mandatory when marking an RFQ as SENT.")

        if not changed_by or not changed_by.strip():
            raise DataValidationError("A valid reviewer or user identity ('changed_by') is mandatory for RFQ status transitions.")

        now = datetime.now(timezone.utc)
        audit = RFQAuditEntry(
            rfq_id=rfq_id,
            from_status=current_status.value,
            to_status=new_status.value,
            changed_by=changed_by.strip(),
            comments=comments.strip() if comments else None,
            timestamp=now,
        )

        with get_db_connection(self.db_path) as conn:
            conn.execute(
                "UPDATE supplier_rfqs SET status = ?, updated_at = ? WHERE rfq_id = ?",
                (new_status.value, now.isoformat(), rfq_id),
            )
            conn.execute(
                """
                INSERT INTO rfq_audit_trail (audit_id, rfq_id, from_status, to_status, changed_by, comments, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit.audit_id,
                    audit.rfq_id,
                    audit.from_status,
                    audit.to_status,
                    audit.changed_by,
                    audit.comments,
                    audit.timestamp.isoformat(),
                ),
            )

        return self.get_supplier_rfq(rfq_id)

    def get_rfq_audit_trail(self, rfq_id: str) -> List[RFQAuditEntry]:
        """Fetch audit trail for an RFQ ordered chronologically."""
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM rfq_audit_trail WHERE rfq_id = ? ORDER BY timestamp ASC",
                (rfq_id,),
            ).fetchall()
            return [
                RFQAuditEntry(
                    audit_id=r["audit_id"],
                    rfq_id=r["rfq_id"],
                    from_status=r["from_status"],
                    to_status=r["to_status"],
                    changed_by=r["changed_by"],
                    comments=r["comments"],
                    timestamp=datetime.fromisoformat(r["timestamp"]),
                )
                for r in rows
            ]

    def delete_supplier_rfq(self, rfq_id: str) -> bool:
        """Delete a supplier RFQ and its cascaded audit trail."""
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute("DELETE FROM supplier_rfqs WHERE rfq_id = ?", (rfq_id,))
            return cur.rowcount > 0

    # =========================================================================
    # PHASE 12: SUPPLIER QUOTATION MANAGEMENT & AUDIT TRAIL
    # =========================================================================

    def _row_to_supplier_quotation(
        self, row: sqlite3.Row, audit_trail: Optional[List[QuotationAuditEntry]] = None
    ) -> SupplierQuotation:
        comm_dict = json.loads(row["commercial_json"])
        comp_dict = json.loads(row["compliance_json"])

        validity_end = None
        if row["validity_end_date"]:
            try:
                validity_end = date.fromisoformat(row["validity_end_date"])
            except (ValueError, TypeError):
                pass

        return SupplierQuotation(
            quotation_id=row["quotation_id"],
            rfq_id=row["rfq_id"],
            supplier_id=row["supplier_id"],
            brief_id=row["brief_id"],
            company_name=row["company_name"],
            version=row["version"],
            status=QuotationStatus(row["status"]),
            submission_date=datetime.fromisoformat(row["submission_date"]),
            validity_end_date=validity_end,
            file_type=row["file_type"],
            raw_file_path=row["raw_file_path"],
            file_hash_sha256=row["file_hash_sha256"],
            commercial=QuotationCommercialData(**comm_dict),
            compliance=QuotationTechnicalCompliance(**comp_dict),
            reviewer_notes=row["reviewer_notes"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            audit_trail=audit_trail or [],
        )

    def create_quotation(self, quotation: SupplierQuotation) -> SupplierQuotation:
        """
        Persist a new supplier quotation in SQLite with initial audit entry.
        Guarantees foreign key integrity and immutable audit logging.
        """
        comm_json = json.dumps(quotation.commercial.model_dump(mode="json"))
        comp_json = json.dumps(quotation.compliance.model_dump(mode="json"))
        now_iso = datetime.now(timezone.utc).isoformat()
        sub_iso = quotation.submission_date.isoformat()
        val_date_str = quotation.validity_end_date.isoformat() if quotation.validity_end_date else None

        initial_audit = QuotationAuditEntry(
            quotation_id=quotation.quotation_id,
            from_status=None,
            to_status=quotation.status.value,
            action="INITIAL_SUBMISSION",
            changed_by="Procurement Reviewer",
            comments=f"Initial quotation (Version {quotation.version}) received via {quotation.file_type}.",
            timestamp=datetime.now(timezone.utc),
        )

        query = """
            INSERT INTO supplier_quotations (
                quotation_id, rfq_id, supplier_id, brief_id, company_name,
                version, status, submission_date, validity_end_date,
                file_type, raw_file_path, file_hash_sha256,
                commercial_json, compliance_json, reviewer_notes,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        params = (
            quotation.quotation_id,
            quotation.rfq_id,
            quotation.supplier_id,
            quotation.brief_id,
            quotation.company_name,
            quotation.version,
            quotation.status.value,
            sub_iso,
            val_date_str,
            quotation.file_type,
            quotation.raw_file_path,
            quotation.file_hash_sha256,
            comm_json,
            comp_json,
            quotation.reviewer_notes,
            quotation.created_at.isoformat(),
            now_iso,
        )

        with get_db_connection(self.db_path) as conn:
            try:
                conn.execute(query, params)
                conn.execute(
                    """
                    INSERT INTO quotation_audit_trail (audit_id, quotation_id, from_status, to_status, action, changed_by, comments, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        initial_audit.audit_id,
                        initial_audit.quotation_id,
                        initial_audit.from_status,
                        initial_audit.to_status,
                        initial_audit.action,
                        initial_audit.changed_by,
                        initial_audit.comments,
                        initial_audit.timestamp.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as e:
                raise DataValidationError(f"Failed to record supplier quotation: {e}") from e

        quotation.audit_trail = [initial_audit]
        return quotation

    def create_quotation_version(
        self,
        rfq_id: str,
        new_quotation: SupplierQuotation,
        changed_by: str,
        revision_notes: str,
    ) -> SupplierQuotation:
        """
        Create a new immutable revision of a quotation for an RFQ.
        Preserves all historical submissions. Sets the previous version to SUPERSEDED.
        """
        if not revision_notes or not revision_notes.strip():
            raise DataValidationError("Revision notes are mandatory when creating a new quotation version.")

        with get_db_connection(self.db_path) as conn:
            latest_row = conn.execute(
                "SELECT * FROM supplier_quotations WHERE rfq_id = ? ORDER BY version DESC LIMIT 1",
                (rfq_id,),
            ).fetchone()

            prev_version = 0
            if latest_row:
                prev_version = latest_row["version"]
                prev_quote_id = latest_row["quotation_id"]
                prev_status = latest_row["status"]

                if prev_status != QuotationStatus.SUPERSEDED.value:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    conn.execute(
                        "UPDATE supplier_quotations SET status = ?, updated_at = ? WHERE quotation_id = ?",
                        (QuotationStatus.SUPERSEDED.value, now_iso, prev_quote_id),
                    )
                    sup_audit = QuotationAuditEntry(
                        quotation_id=prev_quote_id,
                        from_status=prev_status,
                        to_status=QuotationStatus.SUPERSEDED.value,
                        action="SUPERSEDED",
                        changed_by=changed_by,
                        comments=f"Superseded by incoming Revision Version {prev_version + 1}: {revision_notes.strip()}",
                        timestamp=datetime.now(timezone.utc),
                    )
                    conn.execute(
                        """
                        INSERT INTO quotation_audit_trail (audit_id, quotation_id, from_status, to_status, action, changed_by, comments, timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sup_audit.audit_id,
                            sup_audit.quotation_id,
                            sup_audit.from_status,
                            sup_audit.to_status,
                            sup_audit.action,
                            sup_audit.changed_by,
                            sup_audit.comments,
                            sup_audit.timestamp.isoformat(),
                        ),
                    )

            new_quotation.version = prev_version + 1
            new_quotation.rfq_id = rfq_id

            comm_json = json.dumps(new_quotation.commercial.model_dump(mode="json"))
            comp_json = json.dumps(new_quotation.compliance.model_dump(mode="json"))
            now_iso = datetime.now(timezone.utc).isoformat()
            sub_iso = new_quotation.submission_date.isoformat()
            val_date_str = new_quotation.validity_end_date.isoformat() if new_quotation.validity_end_date else None

            new_audit = QuotationAuditEntry(
                quotation_id=new_quotation.quotation_id,
                from_status=None,
                to_status=new_quotation.status.value,
                action="REVISION_SUBMITTED",
                changed_by=changed_by,
                comments=f"Quotation Version {new_quotation.version} created: {revision_notes.strip()}",
                timestamp=datetime.now(timezone.utc),
            )

            query = """
                INSERT INTO supplier_quotations (
                    quotation_id, rfq_id, supplier_id, brief_id, company_name,
                    version, status, submission_date, validity_end_date,
                    file_type, raw_file_path, file_hash_sha256,
                    commercial_json, compliance_json, reviewer_notes,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """
            params = (
                new_quotation.quotation_id,
                new_quotation.rfq_id,
                new_quotation.supplier_id,
                new_quotation.brief_id,
                new_quotation.company_name,
                new_quotation.version,
                new_quotation.status.value,
                sub_iso,
                val_date_str,
                new_quotation.file_type,
                new_quotation.raw_file_path,
                new_quotation.file_hash_sha256,
                comm_json,
                comp_json,
                new_quotation.reviewer_notes,
                new_quotation.created_at.isoformat(),
                now_iso,
            )

            try:
                conn.execute(query, params)
                conn.execute(
                    """
                    INSERT INTO quotation_audit_trail (audit_id, quotation_id, from_status, to_status, action, changed_by, comments, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        new_audit.audit_id,
                        new_audit.quotation_id,
                        new_audit.from_status,
                        new_audit.to_status,
                        new_audit.action,
                        new_audit.changed_by,
                        new_audit.comments,
                        new_audit.timestamp.isoformat(),
                    ),
                )

                # If parent RFQ is in SENT status, transition to RESPONSE_RECEIVED
                rfq_row = conn.execute("SELECT status FROM supplier_rfqs WHERE rfq_id = ?", (rfq_id,)).fetchone()
                if rfq_row and rfq_row["status"] == RFQStatus.SENT.value:
                    conn.execute(
                        "UPDATE supplier_rfqs SET status = ?, updated_at = ? WHERE rfq_id = ?",
                        (RFQStatus.RESPONSE_RECEIVED.value, now_iso, rfq_id),
                    )
                    conn.execute(
                        """
                        INSERT INTO rfq_audit_trail (audit_id, rfq_id, from_status, to_status, changed_by, comments, timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"AUDIT-{uuid.uuid4().hex[:8].upper()}",
                            rfq_id,
                            RFQStatus.SENT.value,
                            RFQStatus.RESPONSE_RECEIVED.value,
                            changed_by,
                            f"Supplier quotation Version {new_quotation.version} received.",
                            now_iso,
                        ),
                    )
            except sqlite3.IntegrityError as e:
                raise DataValidationError(f"Failed to record new quotation version: {e}") from e

        new_quotation.audit_trail = [new_audit]
        return new_quotation

    def get_quotation(self, quotation_id: str) -> Optional[SupplierQuotation]:
        """Fetch a quotation by ID with its complete audit history."""
        with get_db_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM supplier_quotations WHERE quotation_id = ?",
                (quotation_id,),
            ).fetchone()
            if not row:
                return None
            audit = self.get_quotation_audit_trail(quotation_id)
            return self._row_to_supplier_quotation(row, audit)

    def get_latest_quotation_for_rfq(self, rfq_id: str) -> Optional[SupplierQuotation]:
        """Fetch the latest version of quotation for a given RFQ."""
        with get_db_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM supplier_quotations WHERE rfq_id = ? ORDER BY version DESC LIMIT 1",
                (rfq_id,),
            ).fetchone()
            if not row:
                return None
            audit = self.get_quotation_audit_trail(row["quotation_id"])
            return self._row_to_supplier_quotation(row, audit)

    def list_quotations_for_rfq(self, rfq_id: str) -> List[SupplierQuotation]:
        """Fetch all historical quotation versions for an RFQ ordered by version."""
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM supplier_quotations WHERE rfq_id = ? ORDER BY version ASC",
                (rfq_id,),
            ).fetchall()
            return [self._row_to_supplier_quotation(r) for r in rows]

    def list_quotations_for_brief(
        self, brief_id: str, latest_only: bool = True
    ) -> List[SupplierQuotation]:
        """Fetch quotations for a sourcing brief, optionally filtering to latest version only."""
        with get_db_connection(self.db_path) as conn:
            if latest_only:
                query = """
                    SELECT sq.* FROM supplier_quotations sq
                    INNER JOIN (
                        SELECT rfq_id, MAX(version) as max_version
                        FROM supplier_quotations
                        WHERE brief_id = ?
                        GROUP BY rfq_id
                    ) latest ON sq.rfq_id = latest.rfq_id AND sq.version = latest.max_version
                    ORDER BY sq.created_at DESC;
                """
                rows = conn.execute(query, (brief_id,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM supplier_quotations WHERE brief_id = ? ORDER BY created_at DESC",
                    (brief_id,),
                ).fetchall()
            return [self._row_to_supplier_quotation(r) for r in rows]

    def list_quotations_for_supplier(self, supplier_id: str) -> List[SupplierQuotation]:
        """Fetch quotations for a specific supplier."""
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM supplier_quotations WHERE supplier_id = ? ORDER BY created_at DESC",
                (supplier_id,),
            ).fetchall()
            return [self._row_to_supplier_quotation(r) for r in rows]

    def update_quotation(self, quotation: SupplierQuotation) -> SupplierQuotation:
        """Update existing quotation parameters (commercial, compliance, notes)."""
        comm_json = json.dumps(quotation.commercial.model_dump(mode="json"))
        comp_json = json.dumps(quotation.compliance.model_dump(mode="json"))
        now_iso = datetime.now(timezone.utc).isoformat()
        val_date_str = quotation.validity_end_date.isoformat() if quotation.validity_end_date else None

        query = """
            UPDATE supplier_quotations
            SET validity_end_date = ?,
                commercial_json = ?,
                compliance_json = ?,
                reviewer_notes = ?,
                updated_at = ?
            WHERE quotation_id = ?;
        """
        params = (
            val_date_str,
            comm_json,
            comp_json,
            quotation.reviewer_notes,
            now_iso,
            quotation.quotation_id,
        )

        with get_db_connection(self.db_path) as conn:
            cur = conn.execute(query, params)
            if cur.rowcount == 0:
                raise DataValidationError(f"Quotation '{quotation.quotation_id}' not found for update.")

        return self.get_quotation(quotation.quotation_id) or quotation

    def transition_quotation_status(
        self,
        quotation_id: str,
        new_status: QuotationStatus,
        changed_by: str,
        comments: str,
        action: str = "STATUS_CHANGE",
    ) -> SupplierQuotation:
        """
        Transition quotation lifecycle state with validation against allowed transitions,
        mandatory reviewer comments, and audit logging.
        """
        if not comments or not comments.strip():
            raise DataValidationError("Reviewer comments are mandatory for any quotation lifecycle transition.")

        with get_db_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM supplier_quotations WHERE quotation_id = ?",
                (quotation_id,),
            ).fetchone()
            if not row:
                raise DataValidationError(f"Quotation '{quotation_id}' not found.")

            curr_status = QuotationStatus(row["status"])
            valid_next = VALID_QUOTATION_TRANSITIONS.get(curr_status, [])
            if new_status not in valid_next:
                valid_str = ", ".join(s.value for s in valid_next) if valid_next else "None (Terminal State)"
                raise DataValidationError(
                    f"Invalid quotation transition from '{curr_status.value}' to '{new_status.value}'. "
                    f"Allowed transitions: {valid_str}."
                )

            now_iso = datetime.now(timezone.utc).isoformat()
            audit_entry = QuotationAuditEntry(
                quotation_id=quotation_id,
                from_status=curr_status.value,
                to_status=new_status.value,
                action=action,
                changed_by=changed_by,
                comments=comments.strip(),
                timestamp=datetime.now(timezone.utc),
            )

            conn.execute(
                "UPDATE supplier_quotations SET status = ?, updated_at = ? WHERE quotation_id = ?",
                (new_status.value, now_iso, quotation_id),
            )
            conn.execute(
                """
                INSERT INTO quotation_audit_trail (audit_id, quotation_id, from_status, to_status, action, changed_by, comments, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_entry.audit_id,
                    audit_entry.quotation_id,
                    audit_entry.from_status,
                    audit_entry.to_status,
                    audit_entry.action,
                    audit_entry.changed_by,
                    audit_entry.comments,
                    audit_entry.timestamp.isoformat(),
                ),
            )

        return self.get_quotation(quotation_id)

    def confirm_critical_fields(
        self, quotation_id: str, confirmed_by: str, notes: Optional[str] = None
    ) -> SupplierQuotation:
        """
        Human confirmation gate: validates that critical commercial fields have been
        reviewed and verified by the buyer before side-by-side comparison.
        """
        quotation = self.get_quotation(quotation_id)
        if not quotation:
            raise DataValidationError(f"Quotation '{quotation_id}' not found.")

        quotation.commercial.critical_fields_confirmed = True
        now = datetime.now(timezone.utc)
        for field_name, prov in quotation.commercial.provenance_map.items():
            prov.status = ExtractionFieldStatus.CONFIRMED
            prov.confirmed_by = confirmed_by
            prov.confirmed_at = now

        note_text = notes or "Critical commercial and technical inputs verified and confirmed by buyer."
        self.update_quotation(quotation)

        audit_entry = QuotationAuditEntry(
            quotation_id=quotation_id,
            from_status=quotation.status.value,
            to_status=quotation.status.value,
            action="CONFIRM_FIELDS",
            changed_by=confirmed_by,
            comments=note_text,
            timestamp=now,
        )
        with get_db_connection(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO quotation_audit_trail (audit_id, quotation_id, from_status, to_status, action, changed_by, comments, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_entry.audit_id,
                    audit_entry.quotation_id,
                    audit_entry.from_status,
                    audit_entry.to_status,
                    audit_entry.action,
                    audit_entry.changed_by,
                    audit_entry.comments,
                    audit_entry.timestamp.isoformat(),
                ),
            )

        return self.get_quotation(quotation_id)

    def get_quotation_audit_trail(self, quotation_id: str) -> List[QuotationAuditEntry]:
        """Fetch audit trail for a quotation ordered chronologically."""
        with get_db_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM quotation_audit_trail WHERE quotation_id = ? ORDER BY timestamp ASC",
                (quotation_id,),
            ).fetchall()
            return sorted(
                [
                    QuotationAuditEntry(
                        audit_id=r["audit_id"],
                        quotation_id=r["quotation_id"],
                        from_status=r["from_status"],
                        to_status=r["to_status"],
                        action=r["action"],
                        changed_by=r["changed_by"],
                        comments=r["comments"],
                        timestamp=datetime.fromisoformat(r["timestamp"]),
                    )
                    for r in rows
                ],
                key=lambda x: x.timestamp,
            )

    def delete_quotation(self, quotation_id: str) -> bool:
        """Delete a quotation and its cascaded audit trail."""
        with get_db_connection(self.db_path) as conn:
            cur = conn.execute("DELETE FROM supplier_quotations WHERE quotation_id = ?", (quotation_id,))
            return cur.rowcount > 0
