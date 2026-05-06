"""Centralised runtime configuration for the smart-scrape backend.

All values can be overridden via environment variables or the .env file that
python-dotenv loads in llm_router.py.
"""

import os
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# ── SSL / CA bundle ────────────────────────────────────────────────────────────

def _resolve_cacert() -> str | None:
    """Return the path to the CA bundle to use for HTTPS, or None to use system default."""
    env_path = os.getenv("CACERT_PATH")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return str(p.resolve())
        log.warning(f"CACERT_PATH={env_path!r} does not point to a file; falling back to auto-detect")

    # Auto-detect cacert.pem sitting next to the repo root
    candidates = [
        Path(__file__).parents[3] / "cacert.pem",   # repo root
        Path(__file__).parents[2] / "cacert.pem",   # app/
        Path(__file__).parent / "cacert.pem",        # app/backend/
    ]
    for candidate in candidates:
        if candidate.is_file():
            log.info(f"Using CA bundle: {candidate}")
            return str(candidate.resolve())

    return None  # fall back to system / curl default


CACERT_PATH: str | None = _resolve_cacert()

# Tell curl (and curl_cffi) about our CA bundle at process level so that ALL
# outgoing TLS connections pick it up, even those made by third-party libraries.
if CACERT_PATH:
    os.environ.setdefault("CURL_CA_BUNDLE", CACERT_PATH)
    os.environ.setdefault("SSL_CERT_FILE", CACERT_PATH)   # used by Python's ssl module
    os.environ.setdefault("REQUESTS_CA_BUNDLE", CACERT_PATH)  # used by requests / httpx

# ── Fetcher timeouts ───────────────────────────────────────────────────────────

# Fetcher (curl_cffi) — seconds
HTTP_TIMEOUT: int = int(os.getenv("HTTP_TIMEOUT", "30"))

# StealthyFetcher / DynamicFetcher (Playwright) — milliseconds
BROWSER_TIMEOUT: int = int(os.getenv("BROWSER_TIMEOUT", "30000"))

# Retry configuration
HTTP_RETRIES: int = int(os.getenv("HTTP_RETRIES", "3"))
HTTP_RETRY_DELAY: int = int(os.getenv("HTTP_RETRY_DELAY", "2"))

# ── Smart-scrape pipeline ──────────────────────────────────────────────────────

# Maximum number of sites to scrape per request (overrides LLM suggestion if lower)
SMART_SCRAPE_MAX_SITES: int = int(os.getenv("SMART_SCRAPE_MAX_SITES", "5"))

# Target number of tenders to return per job (across all pages and sources)
SMART_SCRAPE_MAX_RESULTS: int = int(os.getenv("SMART_SCRAPE_MAX_RESULTS", "50"))

# Maximum pages to follow per source site (prevents infinite crawl)
SMART_SCRAPE_MAX_PAGES: int = int(os.getenv("SMART_SCRAPE_MAX_PAGES", "5"))

# Maximum rows extracted per selector per URL — legacy scrape-direct only
MAX_ROWS_PER_SELECTOR: int = int(os.getenv("MAX_ROWS_PER_SELECTOR", "100"))

# Maximum characters of HTML sent to the LLM for selector extraction
HTML_SNIPPET_MAX_CHARS: int = int(os.getenv("HTML_SNIPPET_MAX_CHARS", "20000"))

# ── LLM model selection ────────────────────────────────────────────────────────

# Main model: used for high-quality summarisation and site planning
# Default: claude-opus-4 via OpenRouter
OPENROUTER_MODEL: str = os.getenv("OPENROUTER_MODEL", "anthropic/claude-opus-4")

# Search model: cheaper, used for HTML analysis and selector generation.
# google/gemini-flash-1.5 is chosen because:
#   - Very cheap (~$0.075/$0.30 per M tokens)
#   - 1M context window → handles large HTML snippets
#   - Fast, good at structured JSON output
OPENROUTER_SEARCH_MODEL: str = os.getenv("OPENROUTER_SEARCH_MODEL", "google/gemini-flash-1.5")

# Embedding model via OpenRouter embeddings endpoint
OPENROUTER_EMBED_MODEL: str = os.getenv("OPENROUTER_EMBED_MODEL", "perplexity/pplx-embed-v1-4b")

# ── Data storage ───────────────────────────────────────────────────────────────

# Directory for the SQLite embedding store
DATA_DIR: Path = Path(os.getenv("DATA_DIR", str(Path(__file__).parents[1] / "data")))
