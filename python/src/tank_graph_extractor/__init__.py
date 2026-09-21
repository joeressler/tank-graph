"""Foundational APIs for the Tank Graph extractor."""

__version__ = "0.1.0"

from .config import Config, ExtractorConfig
from .errors import TankGraphError
from .http import HttpClient, RateLimitedHttpClient, get_shared_http_client
from .identity import identity_key, normalize_display, sort_key
from .policy import PolicyChecker, PolicyReport, RobotsPolicy

__all__ = [
    "Config",
    "ExtractorConfig",
    "HttpClient",
    "PolicyChecker",
    "PolicyReport",
    "RateLimitedHttpClient",
    "RobotsPolicy",
    "TankGraphError",
    "__version__",
    "get_shared_http_client",
    "identity_key",
    "normalize_display",
    "sort_key",
]
