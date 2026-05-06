"""High-level scraping orchestration.

Two public entry points:
  smart_site_scrape(url, query)  — fetch + LLM selector generation + extraction
  smart_web_search(query)        — LLM site discovery + smart_site_scrape for each
  scrape_with_selectors(url, selectors)  — legacy direct scrape with explicit CSS
"""

import re
import time
import logging
from typing import Optional
from urllib.parse import urljoin

from scrapling.fetchers import Fetcher, StealthyFetcher

from .config import (
    CACERT_PATH,
    HTTP_TIMEOUT,
    BROWSER_TIMEOUT,
    HTTP_RETRIES,
    HTTP_RETRY_DELAY,
    MAX_ROWS_PER_SELECTOR,
    HTML_SNIPPET_MAX_CHARS,
    SMART_SCRAPE_MAX_SITES,
)
from .llm_router import ai_search_plan, extract_selectors_from_html

log = logging.getLogger(__name__)

# Configure at module level — avoids deprecated constructor-arg pattern
Fetcher.configure(adaptive=False)
StealthyFetcher.configure(adaptive=False)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _fetch_with_fallback(url: str, job_id: str = "") -> Optional[object]:
    """Fetch a URL with Fetcher → StealthyFetcher fallback.

    Returns a Scrapling Response object or None on total failure.
    """
    tag = f"[{job_id}] " if job_id else ""
    t0 = time.monotonic()

    # ── Attempt 1: HTTP fetcher (curl_cffi) ──────────────────────────────────
    log.info(f"{tag}Fetcher → {url}")
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            page = Fetcher.get(
                url,
                timeout=HTTP_TIMEOUT,
                stealthy_headers=True,
                verify=CACERT_PATH if CACERT_PATH else True,
                retries=1,
            )
            if page.status and page.status >= 400:
                log.warning(
                    f"{tag}Fetcher attempt {attempt}/{HTTP_RETRIES}: "
                    f"HTTP {page.status} from {url}"
                )
                if attempt < HTTP_RETRIES:
                    time.sleep(HTTP_RETRY_DELAY)
                    continue
                break  # all retries done with 4xx/5xx → fall through to StealthyFetcher
            elapsed = time.monotonic() - t0
            log.info(f"{tag}Fetcher OK in {elapsed:.1f}s — {url}")
            return page
        except Exception as exc:
            elapsed = time.monotonic() - t0
            log.warning(
                f"{tag}Fetcher attempt {attempt}/{HTTP_RETRIES} failed "
                f"after {elapsed:.1f}s: {exc}"
            )
            if attempt < HTTP_RETRIES:
                time.sleep(HTTP_RETRY_DELAY)

    # ── Attempt 2: StealthyFetcher (Playwright headless) ─────────────────────
    log.info(f"{tag}Falling back to StealthyFetcher → {url}")
    t1 = time.monotonic()
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            page = StealthyFetcher.fetch(
                url,
                headless=True,
                timeout=BROWSER_TIMEOUT,   # milliseconds for Playwright
                network_idle=True,
                disable_resources=True,    # skip fonts/images for speed
                google_search=True,
            )
            elapsed = time.monotonic() - t1
            log.info(f"{tag}StealthyFetcher OK in {elapsed:.1f}s — {url}")
            return page
        except Exception as exc:
            elapsed = time.monotonic() - t1
            log.warning(
                f"{tag}StealthyFetcher attempt {attempt}/{HTTP_RETRIES} failed "
                f"after {elapsed:.1f}s: {exc}"
            )
            if attempt < HTTP_RETRIES:
                time.sleep(HTTP_RETRY_DELAY)

    total = time.monotonic() - t0
    log.error(f"{tag}All fetchers failed for {url} after {total:.1f}s — skipping")
    return None


def _html_snippet(page, max_chars: int = HTML_SNIPPET_MAX_CHARS) -> str:
    """Extract a compact HTML snippet for LLM analysis.

    Tries progressively broader selectors to find relevant content,
    stripping scripts/styles to save tokens.
    """
    # From most specific to most general
    selectors = [
        "main, [role=main], #main-content, #content, #main",
        "article, section",
        ".results, .list-results, .bandi-list, .gare-list, .appalti",
        "table",
        "ul, ol",
    ]
    for sel in selectors:
        parts = page.css(sel).getall()
        if parts:
            combined = "\n".join(parts)
            if len(combined) > 300:
                cleaned = re.sub(
                    r"<(script|style)[^>]*>.*?</(script|style)>",
                    "",
                    combined,
                    flags=re.DOTALL | re.IGNORECASE,
                )
                return cleaned[:max_chars]

    # Fallback: full body, scripts stripped
    full = page.css("body").get("") or page.get("")
    full = re.sub(
        r"<(script|style)[^>]*>.*?</(script|style)>",
        "",
        full,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return full[:max_chars]


def _apply_schema(page, schema: dict, url: str) -> list[dict]:
    """Apply an LLM-generated selector schema and return structured records.

    Each record is a dict of field_name → value, plus "fonte" = url.
    """
    row_container = schema.get("row_container")
    fields = schema.get("fields") or []

    if not row_container:
        log.warning(f"LLM returned no row_container for {url} — schema empty")
        return []

    try:
        rows = page.css(row_container)
    except Exception as exc:
        log.warning(f"Container selector '{row_container}' failed on {url}: {exc}")
        return []

    if not rows:
        log.info(f"Selector '{row_container}' matched 0 rows on {url}")
        return []

    records: list[dict] = []
    for row in rows:
        record: dict = {"fonte": url}
        for field in fields:
            css = field.get("css", "").strip()
            attr = field.get("attr")
            name = field.get("name", "campo")
            if not css:
                continue
            try:
                if attr:
                    val: str = row.css(css).attrib.get(attr, "") or ""
                    if attr == "href" and val and not val.startswith(("http://", "https://")):
                        val = urljoin(url, val)
                else:
                    # Try ::text first (pure text content), then strip tags from HTML
                    texts = row.css(css + "::text").getall()
                    if texts:
                        val = " ".join(t.strip() for t in texts if t.strip())
                    else:
                        raw_html = row.css(css).get("") or ""
                        val = re.sub(r"<[^>]+>", " ", raw_html).strip()
                record[name] = val.strip() if isinstance(val, str) else str(val).strip()
            except Exception as exc:
                log.debug(f"Field '{name}' css='{css}' failed on {url}: {exc}")
                record[name] = ""

        # Only keep rows that have at least one non-empty, non-fonte field
        if any(v for k, v in record.items() if k != "fonte" and isinstance(v, str) and v):
            records.append(record)

    return records


# ── Public API ────────────────────────────────────────────────────────────────

def smart_site_scrape(url: str, query: str, job_id: str = "") -> list[dict]:
    """Fetch a URL, use LLM to generate CSS selectors, extract structured records.

    This is the "site_scrape" mode: no manual selectors needed.
    The LLM analyses the page HTML and generates a field-extraction schema.
    """
    tag = f"[{job_id}] " if job_id else ""

    page = _fetch_with_fallback(url, job_id)
    if page is None:
        return []

    snippet = _html_snippet(page)
    log.info(f"{tag}HTML snippet: {len(snippet)} chars → sending to LLM for {url}")

    schema = extract_selectors_from_html(snippet, query, url=url)
    records = _apply_schema(page, schema, url)
    log.info(f"{tag}Estratti {len(records)} record da {url}")

    # Persist to embedding store (best-effort, non-blocking)
    if records:
        try:
            from .embeddings import get_store
            get_store().add_records(records, compute_embeddings=True)
        except Exception as exc:
            log.warning(f"{tag}Embedding store unavailable: {exc}")

    return records


def smart_web_search(
    query: str,
    max_sites: int = SMART_SCRAPE_MAX_SITES,
    job_id: str = "",
) -> tuple[list[str], list[dict]]:
    """Discover relevant sites via LLM, then smart_site_scrape each one.

    Returns (sites_visited, all_records).
    """
    tag = f"[{job_id}] " if job_id else ""

    plan = ai_search_plan(query, max_sites=max_sites)
    sites = plan.get("sites", [])[:max_sites]
    log.info(f"{tag}LLM suggested {len(sites)} site(s): {sites}")

    all_records: list[dict] = []
    for url in sites:
        records = smart_site_scrape(url, query, job_id=job_id)
        all_records.extend(records)

    log.info(
        f"{tag}Web-search complete: {len(sites)} siti visitati, "
        f"{len(all_records)} record totali"
    )
    return sites, all_records


def scrape_with_selectors(
    url: str,
    selectors: list[dict],
    job_id: str = "",
) -> list[dict]:
    """Legacy direct scrape using explicit CSS selectors (used by /scrape-direct).

    Each selector dict must have "name" and "css" keys.
    Returns flat list of {"campo", "valore", "fonte"} dicts.
    """
    tag = f"[{job_id}] " if job_id else ""
    t0 = time.monotonic()

    page = _fetch_with_fallback(url, job_id)
    if page is None:
        return []

    rows: list[dict] = []
    for sel in selectors:
        css = sel.get("css", "")
        name = sel.get("name", "campo")
        if not css:
            continue
        try:
            values = page.css(css).getall()
            added = 0
            for v in values:
                text = v.strip() if isinstance(v, str) else str(v).strip()
                if text and added < MAX_ROWS_PER_SELECTOR:
                    rows.append({"campo": name, "valore": text, "fonte": url})
                    added += 1
            if added:
                log.info(f"{tag}  selector '{css}' → {added} valori")
        except Exception as exc:
            log.warning(f"{tag}Selector '{css}' failed on {url}: {exc}")

    log.info(f"{tag}Estratte {len(rows)} righe da {url} in {time.monotonic()-t0:.1f}s")
    return rows
