"""
VendorIQ Crustdata Service Module.

Provides authenticated, rate-limit aware integration with the Crustdata Company
Intelligence API (Version 2025-11-01).
Implements discovery (Company Search), entity resolution (Company Enrich),
domain-level SQLite caching, and an isolated offline synthetic mode.
Enforces strict company-level privacy and zero-secret logging.
"""

from datetime import datetime, timezone
import logging
from typing import Optional, List, Dict, Any, Tuple
import requests

from config import settings, AppMode
from core.models import (
    SupplierProfile,
    DataField,
    Certification,
)
from core.provenance import (
    ProvenanceClassification,
    create_unverified_provenance,
    create_synthetic_provenance,
)
from core.exceptions import (
    ExternalAPIError,
    ConfigurationError,
    DataValidationError,
)
from db.repository import VendorIQRepository

logger = logging.getLogger("vendoriq.services.crustdata")

# ---------------------------------------------------------------------------
# API Constants (Pinned to Crustdata Documentation)
# ---------------------------------------------------------------------------
CRUSTDATA_BASE_URL = "https://api.crustdata.com"
CRUSTDATA_API_VERSION = "2025-11-01"
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_DISCOVERY_SEARCH_LIMIT = 20
MAX_SHORTLIST_ENRICH_LIMIT = 5

# Permitted non-premium company search fields
PERMITTED_SEARCH_FIELDS = [
    "basic_info.name",
    "basic_info.primary_domain",
    "basic_info.description",
    "basic_info.employee_count_range",
    "locations.headquarters",
]

# Permitted company-level enrichment groups (Excludes people/PII/social)
PERMITTED_ENRICH_GROUPS = [
    "basic_info",
    "headcount",
    "locations",
    "taxonomy",
    "funding",
]

# Explicitly prohibited fields containing personal PII or social feeds
PROHIBITED_PII_FIELDS = {
    "people",
    "decision_makers",
    "contacts",
    "executive_team",
    "personal_emails",
    "social_posts",
}


class CrustdataService:
    """
    Client for interacting with Crustdata API and caching results in SQLite.
    """

    def __init__(
        self,
        repository: Optional[VendorIQRepository] = None,
        app_mode: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: str = CRUSTDATA_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.repository = repository or VendorIQRepository()
        self.app_mode = (app_mode or settings.APP_MODE).strip().upper()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._api_key = api_key
        self.last_response_metadata: Dict[str, Any] = {}

    def _get_api_key(self) -> str:
        """
        Retrieve Crustdata API key safely.
        Raises ConfigurationError if running in ONLINE mode without an API key.
        """
        if self._api_key:
            return self._api_key

        if self.app_mode == AppMode.ONLINE.value:
            try:
                return settings.get_crustdata_api_key()
            except ValueError as e:
                raise ConfigurationError(
                    "Crustdata API key is missing or unconfigured in ONLINE mode. "
                    "Please set CRUSTDATA_API_KEY in your environment or .env file."
                ) from e

        # In OFFLINE mode, no key is needed
        return "OFFLINE_DUMMY_KEY"

    def _build_headers(self) -> Dict[str, str]:
        """Construct standard HTTP headers for Crustdata requests."""
        key = self._get_api_key()
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "x-api-version": CRUSTDATA_API_VERSION,
        }

    def _safe_log_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """Return masked headers for safe logging without exposing secrets."""
        safe = dict(headers)
        if "Authorization" in safe:
            safe["Authorization"] = "Bearer **********"
        return safe

    def _execute_request(
        self,
        endpoint_path: str,
        payload: Dict[str, Any],
        max_retries: int = 2,
    ) -> Any:
        """
        Execute an HTTP POST request to Crustdata with rate-limit and error handling.
        """
        url = f"{self.base_url}{endpoint_path}"
        headers = self._build_headers()

        logger.info(
            "Sending Crustdata request to endpoint: %s with headers: %s",
            endpoint_path,
            self._safe_log_headers(headers),
        )

        for attempt in range(max_retries + 1):
            try:
                response = requests.post(
                    url=url,
                    json=payload,
                    headers=headers,
                    timeout=self.timeout,
                )

                self.last_response_metadata = {
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "credits_used": response.headers.get("X-Credits-Used") or response.headers.get("x-credits-used"),
                    "request_id": response.headers.get("X-Request-Id") or response.headers.get("x-request-id"),
                }

                # HTTP 429: Rate-limit handling
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After", "1")
                    if attempt < max_retries:
                        logger.warning(
                            "Crustdata rate limit (HTTP 429) encountered. Retrying in %s seconds...",
                            retry_after,
                        )
                        continue
                    raise ExternalAPIError(
                        "Crustdata API rate limit exceeded (HTTP 429). Please slow down requests."
                    )

                # HTTP 401/403: Authentication errors
                if response.status_code in (401, 403):
                    raise ExternalAPIError(
                        f"Crustdata API authentication failure (HTTP {response.status_code}). "
                        "Please verify that your CRUSTDATA_API_KEY is active and valid."
                    )

                # HTTP 5xx: Server-side errors
                if response.status_code >= 500:
                    if attempt < max_retries:
                        logger.warning(
                            "Crustdata server error (HTTP %d). Retrying...", response.status_code
                        )
                        continue
                    raise ExternalAPIError(
                        f"Crustdata API server error (HTTP {response.status_code}). Service temporarily unavailable."
                    )

                # Other HTTP client errors
                if response.status_code >= 400:
                    try:
                        err_detail = response.json()
                    except Exception:
                        err_detail = response.text
                    raise ExternalAPIError(
                        f"Crustdata API returned HTTP {response.status_code}: {err_detail}"
                    )

                # Success
                try:
                    return response.json()
                except ValueError as exc:
                    raise ExternalAPIError("Crustdata API returned malformed, non-JSON response.") from exc

            except requests.exceptions.Timeout as exc:
                if attempt < max_retries:
                    continue
                raise ExternalAPIError(
                    f"Crustdata API request timed out after {self.timeout} seconds."
                ) from exc
            except requests.exceptions.RequestException as exc:
                if attempt < max_retries:
                    continue
                raise ExternalAPIError(f"Network error connecting to Crustdata API: {exc}") from exc

        raise ExternalAPIError("Crustdata API request failed after maximum retries.")

    # =========================================================================
    # Task 2 & 3: Company Autocomplete & Search (Discovery)
    # =========================================================================

    def autocomplete(self, field: str, query: str = "", limit: int = 10) -> List[str]:
        """
        Discover valid category and taxonomy filter parameters using the Autocomplete endpoint.
        Free endpoint with 0 credit consumption.
        """
        normalized_field = field.strip()
        normalized_query = query.strip()

        if self.app_mode == AppMode.OFFLINE.value:
            # Synthetic offline autocomplete suggestions
            offline_taxonomy = {
                "basic_info.industries": [
                    "Precision CNC Machining",
                    "Aerospace & Defense Components",
                    "Additive Manufacturing (3D Metal Printing)",
                    "Electronics Manufacturing Services (EMS)",
                    "Medical Device Contract Manufacturing",
                    "Sheet Metal Fabrication",
                    "Injection Molding & Tooling",
                ],
                "locations.country": ["USA", "Germany", "Japan", "Canada", "Mexico", "United Kingdom"],
            }
            results = offline_taxonomy.get(normalized_field, ["Machining", "Aerospace", "Manufacturing"])
            if normalized_query:
                results = [r for r in results if normalized_query.lower() in r.lower()]
            return results[:limit]

        payload = {
            "field": normalized_field,
            "query": normalized_query,
            "limit": limit,
        }
        resp = self._execute_request("/company/search/autocomplete", payload)
        if isinstance(resp, list):
            return resp[:limit]
        if isinstance(resp, dict) and "values" in resp:
            return resp["values"][:limit]
        return []

    def search_companies(
        self,
        industry: Optional[str] = None,
        country: Optional[str] = None,
        min_employees: Optional[int] = None,
        max_employees: Optional[int] = None,
        query: Optional[str] = None,
        limit: int = MAX_DISCOVERY_SEARCH_LIMIT,
        max_retries: int = 0,
    ) -> List[SupplierProfile]:
        """
        Discover suppliers matching buyer criteria using Crustdata Company Search.
        Limits live search to 20 results using basic non-premium fields.
        """
        clamped_limit = min(max(1, limit), MAX_DISCOVERY_SEARCH_LIMIT)

        # In OFFLINE mode: return pure synthetic data clearly labeled
        if self.app_mode == AppMode.OFFLINE.value:
            return self._generate_synthetic_search_results(
                industry=industry,
                country=country,
                limit=clamped_limit,
            )

        # Build conditions payload
        conditions: List[Dict[str, Any]] = []
        if industry and industry.strip():
            conditions.append({
                "field": "basic_info.industries",
                "type": "=",
                "value": industry.strip(),
            })
        if country and country.strip():
            conditions.append({
                "field": "locations.country",
                "type": "=",
                "value": country.strip(),
            })
        if min_employees is not None and min_employees > 0:
            conditions.append({
                "field": "headcount.total",
                "type": "=>",
                "value": min_employees,
            })
        if max_employees is not None and max_employees > 0:
            conditions.append({
                "field": "headcount.total",
                "type": "=<",
                "value": max_employees,
            })

        filters: Dict[str, Any] = {"op": "and", "conditions": conditions} if conditions else {}
        payload: Dict[str, Any] = {
            "fields": PERMITTED_SEARCH_FIELDS,
            "limit": clamped_limit,
        }
        if filters:
            payload["filters"] = filters

        resp = self._execute_request("/company/search", payload, max_retries=max_retries)
        companies_raw = resp.get("companies", []) if isinstance(resp, dict) else []

        suppliers: List[SupplierProfile] = []
        for raw in companies_raw:
            supplier = self._parse_search_company(raw)
            if supplier:
                # Save to repository cache
                self.repository.upsert_supplier(supplier)
                suppliers.append(supplier)

        return suppliers

    def _parse_search_company(self, raw: Dict[str, Any]) -> Optional[SupplierProfile]:
        """
        Parse a single company record from Company Search into a SupplierProfile.
        Classifies incoming Crustdata data as UNVERIFIED_OR_MISSING per integrity rules.
        """
        crust_id = raw.get("crustdata_company_id")
        basic = raw.get("basic_info", {})
        locations = raw.get("locations", {})
        hq = locations.get("headquarters", {}) if isinstance(locations, dict) else {}

        name = basic.get("name")
        domain = basic.get("primary_domain")
        if not name or not domain:
            return None

        clean_domain = domain.strip().lower()
        supplier_id = f"CRUST_{crust_id}" if crust_id else f"SUPP_{clean_domain.replace('.', '_')}"

        country = hq.get("country", "Unknown") if isinstance(hq, dict) else "Unknown"
        city = hq.get("city") if isinstance(hq, dict) else None

        # Build attribute-level provenance
        prov = create_unverified_provenance(
            source="Crustdata Company Search",
            notes="Retrieved from Crustdata discovery database",
        )
        attributes = {
            "primary_domain": DataField(name="primary_domain", value=clean_domain, provenance=prov),
            "company_name": DataField(name="company_name", value=name, provenance=prov),
        }

        return SupplierProfile(
            supplier_id=supplier_id,
            company_name=name,
            domain=clean_domain,
            description=basic.get("description"),
            headquarters_country=country,
            headquarters_city=city,
            employee_range=basic.get("employee_count_range"),
            corporate_website=basic.get("website", f"https://{clean_domain}"),
            attributes=attributes,
            is_synthetic=False,
            overall_provenance=ProvenanceClassification.UNVERIFIED_OR_MISSING,
        )

    # =========================================================================
    # Task 4 & 5: Company Enrichment & Caching
    # =========================================================================

    def enrich_companies(
        self,
        domains: List[str],
        force_refresh: bool = False,
    ) -> List[SupplierProfile]:
        """
        Enrich shortlisted companies (up to 5) with deep firmographics.
        Checks SQLite cache first to prevent duplicate API spend.
        Requests permitted company-level fields only (no people, personal PII, or social feeds).
        """
        if not domains:
            return []

        # Enforce max 5 domains per test/shortlist run
        target_domains = [d.strip().lower() for d in domains if d and d.strip()]
        unique_domains = list(dict.fromkeys(target_domains))[:MAX_SHORTLIST_ENRICH_LIMIT]

        enriched_results: List[SupplierProfile] = []
        uncached_domains: List[str] = []

        # 1. Check SQLite repository cache
        for dom in unique_domains:
            if not force_refresh:
                cached = self.repository.get_supplier_by_domain(dom)
                # If cached profile has enriched details (e.g., employee count or industries), use it
                if cached and (cached.employee_count or cached.industries or cached.annual_revenue_usd):
                    logger.info("Cache hit for domain: %s. Skipping external Crustdata call.", dom)
                    enriched_results.append(cached)
                    continue
            uncached_domains.append(dom)

        if not uncached_domains:
            return enriched_results

        # 2. In OFFLINE mode: generate synthetic enriched fallback
        if self.app_mode == AppMode.OFFLINE.value:
            for dom in uncached_domains:
                synthetic_sup = self._generate_synthetic_enriched_profile(dom)
                self.repository.upsert_supplier(synthetic_sup)
                enriched_results.append(synthetic_sup)
            return enriched_results

        # 3. In ONLINE mode: execute Company Enrich API call
        # Guard against prohibited personal data fields
        for field in PERMITTED_ENRICH_GROUPS:
            if field in PROHIBITED_PII_FIELDS:
                raise DataValidationError(f"Prohibited field group '{field}' cannot be requested.")

        payload = {
            "domains": uncached_domains,
            "fields": PERMITTED_ENRICH_GROUPS,
        }

        resp = self._execute_request("/company/enrich", payload)
        if not isinstance(resp, list):
            raise ExternalAPIError("Crustdata /company/enrich did not return expected array response.")

        for item in resp:
            supplier = self._parse_enrich_item(item)
            if supplier:
                self.repository.upsert_supplier(supplier)
                enriched_results.append(supplier)

        return enriched_results

    def _parse_enrich_item(self, item: Dict[str, Any]) -> Optional[SupplierProfile]:
        """
        Parse a single enrichment match from Crustdata into a SupplierProfile.
        Ensures company-level data only and unverified provenance without external evidence.
        """
        matches = item.get("matches", [])
        if not matches:
            return None

        # Take the best matching company data
        best_match = matches[0]
        data = best_match.get("company_data", {})
        if not data:
            return None

        crust_id = data.get("crustdata_company_id")
        basic = data.get("basic_info", {})
        headcount = data.get("headcount", {})
        locations = data.get("locations", {})
        taxonomy = data.get("taxonomy", {})
        funding = data.get("funding", {})

        name = basic.get("name") or item.get("matched_on", "Unknown Company")
        domain = basic.get("primary_domain") or item.get("matched_on")
        if not domain:
            return None

        clean_domain = domain.strip().lower()
        supplier_id = f"CRUST_{crust_id}" if crust_id else f"SUPP_{clean_domain.replace('.', '_')}"

        hq = locations.get("headquarters", {}) if isinstance(locations, dict) else {}
        country = hq.get("country", "Unknown") if isinstance(hq, dict) else "Unknown"
        city = hq.get("city") if isinstance(hq, dict) else None

        # Extract corporate signals (e.g. funding stage)
        signals: List[str] = []
        if isinstance(funding, dict):
            last_round = funding.get("last_funding_round")
            if last_round:
                signals.append(f"Funding: {last_round}")

        # Extract self-reported certifications (if any) as UNVERIFIED claims
        certifications: List[Certification] = []
        unverified_prov = create_unverified_provenance(
            source="Crustdata Company Enrichment",
            notes="Unverified self-reported claim from Crustdata firmographic record",
        )

        raw_certs = basic.get("certifications", [])
        if isinstance(raw_certs, list):
            for c_name in raw_certs:
                if isinstance(c_name, str) and c_name.strip():
                    certifications.append(
                        Certification(
                            name=c_name.strip(),
                            issuing_body="Self Reported via Registry/Directory",
                            is_verified=False,
                            verification_evidence=None,
                            provenance=unverified_prov,
                        )
                    )

        # Attribute-level provenance
        prov = create_unverified_provenance(
            source="Crustdata Company Enrichment",
            notes="Enriched firmographics from Crustdata API",
        )
        attributes: Dict[str, DataField] = {
            "company_name": DataField(name="company_name", value=name, provenance=prov),
            "domain": DataField(name="domain", value=clean_domain, provenance=prov),
        }
        if headcount.get("total_employees"):
            attributes["headcount"] = DataField(
                name="headcount", value=headcount.get("total_employees"), provenance=prov
            )

        return SupplierProfile(
            supplier_id=supplier_id,
            company_name=name,
            domain=clean_domain,
            description=basic.get("description"),
            headquarters_country=country,
            headquarters_city=city,
            employee_count=headcount.get("total_employees"),
            employee_range=headcount.get("employee_count_range") or basic.get("employee_count_range"),
            industries=taxonomy.get("industries", []) if isinstance(taxonomy, dict) else [],
            certifications=certifications,
            annual_revenue_usd=basic.get("estimated_revenue_usd"),
            year_founded=basic.get("year_founded"),
            business_signals=signals,
            corporate_website=basic.get("website", f"https://{clean_domain}"),
            attributes=attributes,
            is_synthetic=False,
            overall_provenance=ProvenanceClassification.UNVERIFIED_OR_MISSING,
        )

    # =========================================================================
    # Synthetic Generators (For Offline / Demo Mode Only)
    # =========================================================================

    def _generate_synthetic_search_results(
        self,
        industry: Optional[str] = None,
        country: Optional[str] = None,
        limit: int = 10,
    ) -> List[SupplierProfile]:
        """Generate explicitly labeled synthetic supplier discovery results for offline testing."""
        benchmark_vendors = [
            ("Titanium Dynamics Corp", "titaniumdynamics.com", "US", "Wichita", "51-200", 110, ["Aerospace CNC", "Precision Machining"]),
            ("AeroPrecision GmbH", "aeroprecision.de", "DE", "Stuttgart", "201-500", 340, ["Precision Machining", "Additive Manufacturing"]),
            ("Kyoto Nano Machining", "kyoto-nanomfg.jp", "JP", "Kyoto", "11-50", 45, ["Optics", "Precision Machining"]),
            ("Nordic Metalworks AB", "nordicmetalworks.se", "SE", "Gothenburg", "51-200", 85, ["Tool & Die", "Sheet Metal Fabrication"]),
            ("Apex Aerospace Systems", "apexaero.com", "US", "Seattle", "501-1000", 720, ["Aerospace CNC", "Defense Components"]),
        ]

        results: List[SupplierProfile] = []
        for name, domain, hq_c, city, emp_r, emp_c, inds in benchmark_vendors[:limit]:
            prov = create_synthetic_provenance(source="VendorIQ Synthetic Benchmark Generator")
            sup = SupplierProfile(
                supplier_id=f"SYNTH_{domain.replace('.', '_')}",
                company_name=name,
                domain=domain,
                description=f"Synthetic benchmark manufacturer specializing in {', '.join(inds)}.",
                headquarters_country=country or hq_c,
                headquarters_city=city,
                employee_count=emp_c,
                employee_range=emp_r,
                industries=inds,
                business_signals=["Synthetic Benchmark Profile: Offline Mode Active"],
                corporate_website=f"https://{domain}",
                attributes={"domain": DataField(name="domain", value=domain, provenance=prov)},
                is_synthetic=True,
                overall_provenance=ProvenanceClassification.SYNTHETIC_DATA,
            )
            results.append(sup)
        return results

    def _generate_synthetic_enriched_profile(self, domain: str) -> SupplierProfile:
        """Generate an explicitly labeled synthetic enriched profile for offline mode."""
        clean = domain.strip().lower()
        company_name = clean.split(".")[0].replace("-", " ").title() + " (Synthetic)"
        prov = create_synthetic_provenance(source="VendorIQ Synthetic Demo Generator")

        return SupplierProfile(
            supplier_id=f"SYNTH_{clean.replace('.', '_')}",
            company_name=company_name,
            domain=clean,
            description=f"Simulated benchmark supplier profile for {clean} in offline testing mode.",
            headquarters_country="US",
            headquarters_city="Denver",
            employee_count=150,
            employee_range="101-250",
            industries=["Precision Machining", "Aerospace CNC"],
            annual_revenue_usd=25_000_000.0,
            year_founded=2008,
            business_signals=["Offline demo mode synthetic record"],
            corporate_website=f"https://{clean}",
            attributes={"domain": DataField(name="domain", value=clean, provenance=prov)},
            is_synthetic=True,
            overall_provenance=ProvenanceClassification.SYNTHETIC_DATA,
        )

    # =========================================================================
    # Task 7: Controlled Live Request Preview Helper
    # =========================================================================

    def preview_search_request(
        self,
        industry: str = "Machining",
        country: str = "USA",
        limit: int = 20,
    ) -> Dict[str, Any]:
        """
        Generate inspection details for the controlled live test preview.
        Does not execute any HTTP request.
        """
        clamped_limit = min(limit, MAX_DISCOVERY_SEARCH_LIMIT)
        endpoint = f"{self.base_url}/company/search"
        payload = {
            "filters": {
                "op": "and",
                "conditions": [
                    {"field": "basic_info.industries", "type": "=", "value": industry},
                    {"field": "locations.country", "type": "=", "value": country},
                ],
            },
            "fields": PERMITTED_SEARCH_FIELDS,
            "limit": clamped_limit,
        }
        # Base Crustdata search cost: 0.03 credits per result returned
        estimated_cost_credits = round(clamped_limit * 0.03, 2)

        return {
            "endpoint": endpoint,
            "method": "POST",
            "headers": {
                "Authorization": "Bearer ********** (CRUSTDATA_API_KEY from config)",
                "Content-Type": "application/json",
                "x-api-version": CRUSTDATA_API_VERSION,
            },
            "payload": payload,
            "requested_fields": PERMITTED_SEARCH_FIELDS,
            "requested_count": clamped_limit,
            "expected_cost_credits": estimated_cost_credits,
            "cost_basis": "0.03 credits per returned result (0 credits if 0 results returned)",
        }
