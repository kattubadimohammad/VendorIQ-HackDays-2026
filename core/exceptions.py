"""
VendorIQ Core Application Exceptions.

Defines the exception hierarchy for VendorIQ data validation,
configuration, supplier processing, external services, and provenance integrity.
"""


class VendorIQError(Exception):
    """Base exception for all VendorIQ domain and operational errors."""
    pass


class ConfigurationError(VendorIQError):
    """Raised when application configuration or environment settings are invalid or missing."""
    pass


class DataValidationError(VendorIQError):
    """Raised when domain data, model attributes, or business rules fail validation."""
    pass


class ProvenanceIntegrityError(DataValidationError):
    """
    Raised when provenance integrity rules are violated.
    For example: attempting to classify AI assessments or synthetic records as verified data.
    """
    pass


class SupplierProviderError(VendorIQError):
    """Raised when a supplier data provider or search service fails."""
    pass


class ExternalAPIError(VendorIQError):
    """Raised when an external API (e.g., Gemini, Crustdata) returns an error or is unreachable."""
    pass


class MissingSupplierDataError(VendorIQError):
    """
    Raised when required supplier information is absent for an operation
    that strictly requires it.
    """
    pass
