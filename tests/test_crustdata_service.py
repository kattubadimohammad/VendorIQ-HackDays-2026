"""
Tests for VendorIQ Crustdata Service Integration (Phase 5).

Validates request authentication, response parsing, rate limits, HTTP error handling,
SQLite caching, offline synthetic fallback, and provenance separation.
All external HTTP requests are mocked.
"""

from unittest.mock import patch, MagicMock
import pytest
import requests

from core.models import SupplierProfile
from core.provenance import ProvenanceClassification
from core.exceptions import ExternalAPIError, ConfigurationError
from db.repository import VendorIQRepository
from services.crustdata_service import (
    CrustdataService,
    PERMITTED_SEARCH_FIELDS,
    PERMITTED_ENRICH_GROUPS,
)


@pytest.fixture
def repo(tmp_path):
    """Provide an isolated temporary database for Crustdata service caching tests."""
    db_file = tmp_path / "test_crustdata_repo.sqlite"
    r = VendorIQRepository(db_path=str(db_file))
    r.initialize()
    return r


@pytest.fixture
def online_service(repo):
    """Provide a CrustdataService configured in ONLINE mode with dummy key."""
    return CrustdataService(
        repository=repo,
        app_mode="ONLINE",
        api_key="mock_crustdata_key_secret_12345",
    )


@pytest.fixture
def offline_service(repo):
    """Provide a CrustdataService configured in OFFLINE mode."""
    return CrustdataService(
        repository=repo,
        app_mode="OFFLINE",
    )


# ---------------------------------------------------------------------------
# Authentication & Secret Protection Tests
# ---------------------------------------------------------------------------

def test_api_key_validation_in_online_mode(repo):
    """Verify that ONLINE mode without an API key raises ConfigurationError."""
    from config import Settings
    with patch.object(Settings, "get_crustdata_api_key", side_effect=ValueError("Missing key")):
        svc = CrustdataService(repository=repo, app_mode="ONLINE", api_key=None)
        with pytest.raises(ConfigurationError) as exc:
            svc.search_companies(industry="Machining")
        assert "Crustdata API key is missing" in str(exc.value)


def test_request_headers_and_secret_masking(online_service):
    """Verify headers include Bearer token and safe_log_headers masks secret."""
    headers = online_service._build_headers()
    assert headers["Authorization"] == "Bearer mock_crustdata_key_secret_12345"
    assert headers["x-api-version"] == "2025-11-01"
    assert headers["Content-Type"] == "application/json"

    # Verify masking in log representation
    safe = online_service._safe_log_headers(headers)
    assert safe["Authorization"] == "Bearer **********"
    assert "mock_crustdata_key_secret_12345" not in str(safe)


# ---------------------------------------------------------------------------
# Company Autocomplete Tests
# ---------------------------------------------------------------------------

@patch("requests.post")
def test_autocomplete_online(mock_post, online_service):
    """Verify autocomplete endpoint parses responses properly."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = ["Aerospace CNC", "Precision Machining"]
    mock_post.return_value = mock_resp

    results = online_service.autocomplete(field="basic_info.industries", query="Aero", limit=5)
    assert len(results) == 2
    assert "Aerospace CNC" in results

    # Verify request payload
    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["field"] == "basic_info.industries"
    assert kwargs["json"]["query"] == "Aero"


def test_autocomplete_offline(offline_service):
    """Verify autocomplete in offline mode returns synthetic categories without network calls."""
    results = offline_service.autocomplete(field="basic_info.industries", query="Precision", limit=5)
    assert len(results) >= 1
    assert any("Precision" in r for r in results)


# ---------------------------------------------------------------------------
# Company Search (Discovery) Tests
# ---------------------------------------------------------------------------

@patch("requests.post")
def test_search_companies_success_and_caching(mock_post, online_service, repo):
    """Verify Company Search parses records, assigns UNVERIFIED provenance, and caches in SQLite."""
    mock_api_data = {
        "companies": [
            {
                "crustdata_company_id": 99281,
                "basic_info": {
                    "name": "Precision Aero CNC Inc",
                    "primary_domain": "precision-aerocnc.com",
                    "description": "AS9100 CNC supplier in Ohio",
                    "employee_count_range": "51-200",
                    "website": "https://precision-aerocnc.com",
                },
                "locations": {
                    "headquarters": {
                        "country": "USA",
                        "city": "Cleveland",
                    }
                },
            }
        ],
        "total_results": 1,
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_api_data
    mock_post.return_value = mock_resp

    suppliers = online_service.search_companies(
        industry="Aerospace CNC",
        country="USA",
        min_employees=50,
        limit=10,
    )

    assert len(suppliers) == 1
    sup = suppliers[0]
    assert sup.supplier_id == "CRUST_99281"
    assert sup.company_name == "Precision Aero CNC Inc"
    assert sup.domain == "precision-aerocnc.com"
    assert sup.headquarters_country == "USA"
    assert sup.headquarters_city == "Cleveland"
    assert sup.employee_range == "51-200"

    # Integrity rules: live Crustdata company data is not independently verified
    assert sup.is_synthetic is False
    assert sup.overall_provenance == ProvenanceClassification.UNVERIFIED_OR_MISSING

    # Verify supplier is cached in SQLite repository
    cached = repo.get_supplier_by_domain("precision-aerocnc.com")
    assert cached is not None
    assert cached.supplier_id == "CRUST_99281"


# ---------------------------------------------------------------------------
# Company Enrichment Tests
# ---------------------------------------------------------------------------

@patch("requests.post")
def test_enrich_companies_success_and_provenance(mock_post, online_service, repo):
    """Verify Company Enrich parses deep firmographics without claiming verified certs."""
    mock_enrich_response = [
        {
            "matched_on": "titanium-works.com",
            "match_type": "domain",
            "matches": [
                {
                    "confidence_score": 1.0,
                    "company_data": {
                        "crustdata_company_id": 54321,
                        "basic_info": {
                            "name": "Titanium Works LLC",
                            "primary_domain": "titanium-works.com",
                            "description": "5-axis precision milling",
                            "year_founded": 2004,
                            "website": "https://titanium-works.com",
                            "certifications": ["ISO 9001:2015"],
                        },
                        "headcount": {
                            "total_employees": 125,
                            "employee_count_range": "101-250",
                        },
                        "locations": {
                            "headquarters": {
                                "country": "USA",
                                "city": "Denver",
                            }
                        },
                        "taxonomy": {
                            "industries": ["Machining", "Aerospace"],
                        },
                        "funding": {
                            "last_funding_round": "Series A",
                        },
                    },
                }
            ],
        }
    ]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = mock_enrich_response
    mock_post.return_value = mock_resp

    enriched = online_service.enrich_companies(domains=["titanium-works.com"])
    assert len(enriched) == 1
    sup = enriched[0]

    assert sup.supplier_id == "CRUST_54321"
    assert sup.employee_count == 125
    assert sup.year_founded == 2004
    assert "Series A" in sup.business_signals[0]

    # Integrity rules: certifications from Crustdata are UNVERIFIED claims unless supported by independent evidence
    assert len(sup.certifications) == 1
    cert = sup.certifications[0]
    assert cert.name == "ISO 9001:2015"
    assert cert.is_verified is False
    assert cert.provenance.classification == ProvenanceClassification.UNVERIFIED_OR_MISSING

    # Verify cached in repository
    cached = repo.get_supplier_by_domain("titanium-works.com")
    assert cached is not None
    assert cached.employee_count == 125


def test_enrichment_cache_hit_skips_external_api(online_service, repo):
    """Verify that when a supplier is already enriched in SQLite, no HTTP call is made."""
    # Pre-populate repository with enriched supplier
    sup = SupplierProfile(
        supplier_id="CACHED_001",
        company_name="Pre-Enriched Fasteners",
        domain="pre-enriched.com",
        headquarters_country="DE",
        employee_count=75,
        industries=["Fasteners"],
    )
    repo.create_supplier(sup)

    with patch("requests.post") as mock_post:
        results = online_service.enrich_companies(domains=["pre-enriched.com"])
        assert len(results) == 1
        assert results[0].domain == "pre-enriched.com"
        assert results[0].employee_count == 75

        # Must NOT call external API on cache hit
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Offline Fallback and Synthetic Separation Tests
# ---------------------------------------------------------------------------

def test_offline_mode_returns_synthetic_data_cleanly(offline_service):
    """Verify offline mode returns synthetic records labeled explicitly."""
    # Search
    search_results = offline_service.search_companies(industry="Aerospace", limit=3)
    assert len(search_results) == 3
    for s in search_results:
        assert s.is_synthetic is True
        assert s.overall_provenance == ProvenanceClassification.SYNTHETIC_DATA
        assert "Synthetic" in s.description or "Synthetic" in s.business_signals[0]

    # Enrich
    enrich_results = offline_service.enrich_companies(domains=["demo-supplier.com"])
    assert len(enrich_results) == 1
    enr = enrich_results[0]
    assert enr.is_synthetic is True
    assert enr.overall_provenance == ProvenanceClassification.SYNTHETIC_DATA


# ---------------------------------------------------------------------------
# HTTP Error, Rate-Limit & Timeout Handling Tests
# ---------------------------------------------------------------------------

@patch("requests.post")
def test_rate_limit_429_raises_external_api_error(mock_post, online_service):
    """Verify HTTP 429 rate limit raises ExternalAPIError."""
    mock_resp = MagicMock()
    mock_resp.status_code = 429
    mock_resp.headers = {"Retry-After": "0"}
    mock_post.return_value = mock_resp

    with pytest.raises(ExternalAPIError) as exc:
        online_service.search_companies(industry="Machining")
    assert "rate limit exceeded (HTTP 429)" in str(exc.value)


@patch("requests.post")
def test_auth_error_401_raises_clear_message(mock_post, online_service):
    """Verify HTTP 401 returns informative authentication error."""
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_post.return_value = mock_resp

    with pytest.raises(ExternalAPIError) as exc:
        online_service.search_companies(industry="Machining")
    assert "authentication failure (HTTP 401)" in str(exc.value)


@patch("requests.post")
def test_timeout_raises_external_api_error(mock_post, online_service):
    """Verify request timeout raises ExternalAPIError."""
    mock_post.side_effect = requests.exceptions.Timeout("Connection timed out")

    with pytest.raises(ExternalAPIError) as exc:
        online_service.search_companies(industry="Machining")
    assert "timed out" in str(exc.value)


@patch("requests.post")
def test_malformed_non_json_raises_external_api_error(mock_post, online_service):
    """Verify non-JSON response raises ExternalAPIError."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.side_effect = ValueError("Invalid JSON string")
    mock_post.return_value = mock_resp

    with pytest.raises(ExternalAPIError) as exc:
        online_service.search_companies(industry="Machining")
    assert "malformed, non-JSON response" in str(exc.value)


# ---------------------------------------------------------------------------
# Controlled Live Preview Generation
# ---------------------------------------------------------------------------

def test_preview_search_request_structure(online_service):
    """Verify preview_search_request generates exact parameters without making calls."""
    preview = online_service.preview_search_request(
        industry="Precision CNC Machining",
        country="USA",
        limit=20,
    )
    assert preview["endpoint"] == "https://api.crustdata.com/company/search"
    assert preview["requested_count"] == 20
    assert preview["expected_cost_credits"] == 0.60
    assert preview["requested_fields"] == PERMITTED_SEARCH_FIELDS
    assert "Authorization" in preview["headers"]
    assert "Bearer **********" in preview["headers"]["Authorization"]
