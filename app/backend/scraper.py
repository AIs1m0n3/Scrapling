"""High-level scraping orchestration.

Public entry points:
  find_tenders(query, url, ...)   → TenderSearchResult   ← main entry point
  scrape_with_selectors(url, ...) → list[dict]           ← legacy /scrape-direct

Kept for backward compat (used by older tests / callers):
  smart_site_scrape(url, query)   → list[dict]
  smart_web_search(query)         → tuple[list[str], list[dict]]
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
    SMART_SCRAPE_MAX_RESULTS,
    SMART_SCRAPE_MAX_PAGES,
)
from .llm_router import ai_search_plan, extract_selectors_from_html
from .models import TenderRecord, TenderSearchResult, SourceInfo, normalize_record, has_minimum_data

log = logging.getLogger(__name__)

# Configure at module level — avoids deprecated constructor-arg pattern
Fetcher.configure(adaptive=False)
StealthyFetcher.configure(adaptive=False)


# ── Fetch helper ──────────────────────────────────────────────────────────────

def _fetch_with_fallback(url: str, job_id: str = "") -> Optional[object]:
    """Fetch a URL with Fetcher → StealthyFetcher fallback.

    Returns a Scrapling Response object or None on total failure.
    """
    tag = f"[{job_id}] " if job_id else ""
    t0 = time.monotonic()

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
                log.warning(f"{tag}Fetcher attempt {attempt}/{HTTP_RETRIES}: HTTP {page.status}")
                if attempt < HTTP_RETRIES:
                    time.sleep(HTTP_RETRY_DELAY)
                    continue
                break
            log.info(f"{tag}Fetcher OK in {time.monotonic()-t0:.1f}s — {url}")
            return page
        except Exception as exc:
            log.warning(f"{tag}Fetcher attempt {attempt}/{HTTP_RETRIES} failed: {exc}")
            if attempt < HTTP_RETRIES:
                time.sleep(HTTP_RETRY_DELAY)

    log.info(f"{tag}Falling back to StealthyFetcher → {url}")
    t1 = time.monotonic()
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            page = StealthyFetcher.fetch(
                url,
                headless=True,
                timeout=BROWSER_TIMEOUT,
                network_idle=True,
                disable_resources=True,
                google_search=True,
            )
            log.info(f"{tag}StealthyFetcher OK in {time.monotonic()-t1:.1f}s — {url}")
            return page
        except Exception as exc:
            log.warning(f"{tag}StealthyFetcher attempt {attempt}/{HTTP_RETRIES} failed: {exc}")
            if attempt < HTTP_RETRIES:
                time.sleep(HTTP_RETRY_DELAY)

    log.error(f"{tag}All fetchers failed for {url} after {time.monotonic()-t0:.1f}s — skipping")
    return None


# ── HTML snippet ──────────────────────────────────────────────────────────────

def _html_snippet(page, max_chars: int = HTML_SNIPPET_MAX_CHARS) -> str:
    """Extract a compact HTML snippet for LLM analysis (scripts/styles stripped)."""
    selectors = [
        "main, [role=main], #main-content, #content, #main",
        "article, section",
        ".results, .list-results, .bandi-list, .gare-list, .appalti, .tenders",
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

    full = page.css("body").get("") or page.get("")
    full = re.sub(
        r"<(script|style)[^>]*>.*?</(script|style)>",
        "",
        full,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return full[:max_chars]


# ── Schema application ────────────────────────────────────────────────────────

def _apply_schema(page, schema: dict, url: str) -> list[dict]:
    """Apply an LLM-generated selector schema and return raw records."""
    row_container = schema.get("row_container")
    fields = schema.get("fields") or []

    if not row_container:
        log.warning(f"LLM returned no row_container for {url}")
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

        if any(v for k, v in record.items() if k != "fonte" and isinstance(v, str) and v):
            records.append(record)

    return records


# ── Pagination ────────────────────────────────────────────────────────────────

# Common Italian PA site "next page" patterns (tried in order)
_NEXT_PAGE_SELECTORS = [
    "a[rel='next']",
    "a[rel=next]",
    ".pagination a.next",
    ".pagination li.next a",
    "li.next a",
    "li.pagination-next a",
    "a.pager__link--next",
    ".pager-next a",
    "a[aria-label*='successiv']",
    "a[aria-label*='next' i]",
    "a[title*='Successiv']",
    "a[title*='Next' i]",
    "a[title*='Avanti']",
    "nav.pagination a:last-child",
]


def _detect_next_page(page, current_url: str, schema: dict) -> Optional[str]:
    """Find the URL for the next listing page, or None if not found."""
    # 1. LLM-suggested selector
    llm_sel = schema.get("pagination_next")
    if llm_sel:
        try:
            href = page.css(llm_sel).attrib.get("href", "")
            if href:
                full = urljoin(current_url, href)
                if full != current_url:
                    log.debug(f"Pagination (LLM '{llm_sel}'): {full}")
                    return full
        except Exception:
            pass

    # 2. Common patterns
    for sel in _NEXT_PAGE_SELECTORS:
        try:
            href = page.css(sel).attrib.get("href", "")
            if href:
                full = urljoin(current_url, href)
                if full != current_url:
                    log.debug(f"Pagination (common '{sel}'): {full}")
                    return full
        except Exception:
            continue

    return None


# ── Core tender scraper (with pagination) ────────────────────────────────────

def scrape_tenders(
    url: str,
    query: str,
    max_results: int = SMART_SCRAPE_MAX_RESULTS,
    max_pages: int = SMART_SCRAPE_MAX_PAGES,
    job_id: str = "",
) -> tuple[list[TenderRecord], list[dict], int]:
    """Scrape tenders from a listing URL, following pagination until max_results.

    Returns (normalized_tenders, raw_records, pages_visited).
    """
    tag = f"[{job_id}] " if job_id else ""
    all_tenders: list[TenderRecord] = []
    all_raw: list[dict] = []
    current_url: str = url
    pages_visited = 0
    schema: dict = {}

    while current_url and pages_visited < max_pages and len(all_tenders) < max_results:
        page = _fetch_with_fallback(current_url, job_id)
        if page is None:
            break

        snippet = _html_snippet(page)
        log.info(f"{tag}Page {pages_visited+1}: HTML snippet {len(snippet)} chars → LLM")

        # Re-generate schema only on the first page (reuse for subsequent pages)
        if pages_visited == 0:
            schema = extract_selectors_from_html(snippet, query, url=current_url)

        raw_records = _apply_schema(page, schema, current_url)
        tenders = [normalize_record(r) for r in raw_records]
        tenders = [t for t in tenders if has_minimum_data(t)]

        all_raw.extend(raw_records)
        all_tenders.extend(tenders)
        pages_visited += 1

        log.info(
            f"{tag}Page {pages_visited}: {len(tenders)} tenders extracted "
            f"(total so far: {len(all_tenders)})"
        )

        if len(all_tenders) >= max_results:
            log.info(f"{tag}Reached target of {max_results} tenders — stopping pagination")
            break

        next_url = _detect_next_page(page, current_url, schema)
        if not next_url:
            log.info(f"{tag}No next page found after page {pages_visited}")
            break
        log.info(f"{tag}Following pagination → {next_url}")
        current_url = next_url

    # Store in embedding store (best-effort)
    if all_raw:
        try:
            from .embeddings import get_store
            get_store().add_records(all_raw, compute_embeddings=True)
        except Exception as exc:
            log.warning(f"{tag}Embedding store unavailable: {exc}")

    log.info(
        f"{tag}scrape_tenders done: url={url} "
        f"pages={pages_visited} tenders={len(all_tenders)}"
        + (" [LIMIT REACHED]" if len(all_tenders) >= max_results else "")
    )
    return all_tenders[:max_results], all_raw[:max_results], pages_visited


# ── Main entry point ──────────────────────────────────────────────────────────

def find_tenders(
    query: str,
    url: Optional[str] = None,
    max_results: int = SMART_SCRAPE_MAX_RESULTS,
    max_sites: int = SMART_SCRAPE_MAX_SITES,
    max_pages: int = SMART_SCRAPE_MAX_PAGES,
    job_id: str = "",
) -> TenderSearchResult:
    """Main entry point for tender search.

    Detects mode automatically:
      url provided → site_scrape (one source, with pagination)
      no url       → web_search  (LLM discovers sites, scrapes each)

    Returns a TenderSearchResult dict.
    """
    tag = f"[{job_id}] " if job_id else ""

    if url:
        # ── site_scrape ───────────────────────────────────────────────────────
        log.info(f"{tag}find_tenders: site_scrape mode — {url}")
        tenders, raw, pages = scrape_tenders(url, query, max_results, max_pages, job_id)
        return TenderSearchResult(
            source_mode="site_scrape",
            sources=[SourceInfo(url=url, num_tenders=len(tenders), pages_scraped=pages)],
            tenders=tenders,
            raw_records=raw,
            pages_scraped=pages,
        )

    # ── web_search ────────────────────────────────────────────────────────────
    log.info(f"{tag}find_tenders: web_search mode")
    plan = ai_search_plan(query, max_sites=max_sites)
    sites = plan.get("sites", [])[:max_sites]
    log.info(f"{tag}LLM suggested {len(sites)} site(s): {sites}")

    all_tenders: list[TenderRecord] = []
    all_raw: list[dict] = []
    sources: list[SourceInfo] = []
    total_pages = 0

    for site_url in sites:
        remaining = max_results - len(all_tenders)
        if remaining <= 0:
            log.info(f"{tag}Reached {max_results} tenders — skipping remaining sites")
            break
        tenders, raw, pages = scrape_tenders(
            site_url, query, remaining, max_pages, job_id
        )
        all_tenders.extend(tenders)
        all_raw.extend(raw)
        total_pages += pages
        sources.append(SourceInfo(url=site_url, num_tenders=len(tenders), pages_scraped=pages))

    log.info(
        f"{tag}find_tenders complete: {len(sites)} siti, "
        f"{total_pages} pagine, {len(all_tenders)} gare totali"
    )
    return TenderSearchResult(
        source_mode="web_search",
        sources=sources,
        tenders=all_tenders[:max_results],
        raw_records=all_raw[:max_results],
        pages_scraped=total_pages,
    )


# ── Backward-compat wrappers ──────────────────────────────────────────────────

def smart_site_scrape(url: str, query: str, job_id: str = "") -> list[dict]:
    """Backward compat: returns raw records from a single URL (no pagination)."""
    tenders, raw, _ = scrape_tenders(url, query, max_results=100, max_pages=1, job_id=job_id)
    return raw


def smart_web_search(
    query: str,
    max_sites: int = SMART_SCRAPE_MAX_SITES,
    job_id: str = "",
) -> tuple[list[str], list[dict]]:
    """Backward compat: returns (sites_list, raw_records)."""
    result = find_tenders(query, max_sites=max_sites, job_id=job_id)
    sites = [s["url"] for s in result["sources"]]
    return sites, result["raw_records"]


def scrape_with_selectors(
    url: str,
    selectors: list[dict],
    job_id: str = "",
) -> list[dict]:
    """Legacy direct scrape using explicit CSS selectors (used by /scrape-direct)."""
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
