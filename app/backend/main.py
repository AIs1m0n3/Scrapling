"""FastAPI application — smart-scrape backend.

Endpoints:
  POST /smart-scrape   — structured tender report (web_search | site_scrape)
  POST /scrape-direct  — legacy: explicit CSS selectors
  GET  /search         — semantic search over stored records
  GET  /jobs           — list past jobs
  GET  /report/{id}    — download PDF report
  GET  /health         — health + active config
"""

import os
import uuid
import logging
from datetime import datetime
from typing import Optional, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# config must be imported first — sets CURL_CA_BUNDLE / SSL_CERT_FILE env vars
from .config import (
    CACERT_PATH,
    HTTP_TIMEOUT,
    BROWSER_TIMEOUT,
    SMART_SCRAPE_MAX_SITES,
    SMART_SCRAPE_MAX_RESULTS,
    SMART_SCRAPE_MAX_PAGES,
    OPENROUTER_SEARCH_MODEL,
    OPENROUTER_EMBED_MODEL,
)
from .scraper import find_tenders, scrape_with_selectors
from .llm_router import llm_summarize
from .embeddings import get_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Scrapling Tender SaaS", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job store
jobs: dict[str, dict] = {}


# ── Models ────────────────────────────────────────────────────────────────────

class SmartScrapeRequest(BaseModel):
    prompt: str
    url: Optional[str] = None
    mode: Optional[Literal["web_search", "site_scrape", "auto"]] = "auto"
    max_results: Optional[int] = None   # override SMART_SCRAPE_MAX_RESULTS
    max_pages: Optional[int] = None     # override SMART_SCRAPE_MAX_PAGES


class SelectorItem(BaseModel):
    name: str
    css: str


class DirectScrapeRequest(BaseModel):
    url: str
    selectors: list[SelectorItem]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "config": {
            "cacert": CACERT_PATH or "system default",
            "http_timeout_s": HTTP_TIMEOUT,
            "browser_timeout_ms": BROWSER_TIMEOUT,
            "max_sites": SMART_SCRAPE_MAX_SITES,
            "max_results": SMART_SCRAPE_MAX_RESULTS,
            "max_pages": SMART_SCRAPE_MAX_PAGES,
            "search_model": OPENROUTER_SEARCH_MODEL,
            "embed_model": OPENROUTER_EMBED_MODEL,
        },
    }


@app.get("/jobs")
def list_jobs():
    return [
        {
            "job_id": jid,
            "prompt": j.get("prompt"),
            "mode": j.get("mode"),
            "created_at": j.get("created_at"),
            "siti_visitati": j.get("siti_visitati", []),
            "tenders_count": j.get("tenders_count", 0),
            "pages_scraped": j.get("pages_scraped", 0),
        }
        for jid, j in jobs.items()
    ]


@app.post("/smart-scrape")
def smart_scrape(req: SmartScrapeRequest):
    job_id = str(uuid.uuid4())[:8]

    # Mode detection
    effective_mode = req.mode
    if effective_mode == "auto":
        effective_mode = "site_scrape" if req.url else "web_search"

    if effective_mode == "site_scrape" and not req.url:
        raise HTTPException(
            status_code=400,
            detail="mode='site_scrape' richiede un URL esplicito.",
        )

    max_results = req.max_results or SMART_SCRAPE_MAX_RESULTS
    max_pages = req.max_pages or SMART_SCRAPE_MAX_PAGES

    log.info(
        f"[{job_id}] smart-scrape — mode={effective_mode} "
        f"max_results={max_results} max_pages={max_pages} "
        f"prompt={req.prompt!r} url={req.url}"
    )

    result = find_tenders(
        query=req.prompt,
        url=req.url if effective_mode == "site_scrape" else None,
        max_results=max_results,
        max_sites=SMART_SCRAPE_MAX_SITES,
        max_pages=max_pages,
        job_id=job_id,
    )

    tenders = result["tenders"]
    raw_records = result["raw_records"]
    sources = result["sources"]
    pages_scraped = result["pages_scraped"]

    if effective_mode == "web_search" and not sources:
        raise HTTPException(
            status_code=400,
            detail="LLM non ha trovato siti da analizzare.",
        )

    log.info(
        f"[{job_id}] Pipeline completata — "
        f"siti={len(sources)} pagine={pages_scraped} gare={len(tenders)}"
    )

    # Build summary only if we have data
    riassunto = ""
    if tenders:
        try:
            riassunto = llm_summarize(raw_records[:30], req.prompt)
        except Exception as exc:
            log.warning(f"[{job_id}] Summarize failed: {exc}")

    siti_visitati = [s["url"] for s in sources]

    job = {
        "job_id": job_id,
        "prompt": req.prompt,
        "mode": effective_mode,
        "siti_visitati": siti_visitati,
        "tenders_count": len(tenders),
        "pages_scraped": pages_scraped,
        "dati": raw_records,
        "riassunto": riassunto,
        "created_at": datetime.utcnow().isoformat(),
    }
    jobs[job_id] = job

    return {
        "ok": True,
        "job_id": job_id,
        "query": req.prompt,
        "mode": effective_mode,
        # Structured tender report
        "sources": sources,
        "tenders_count": len(tenders),
        "pages_scraped": pages_scraped,
        "tenders": tenders,
        # Backward compat
        "siti_visitati": siti_visitati,
        "dati": raw_records,
        "riassunto": riassunto,
    }


@app.post("/scrape-direct")
def scrape_direct(req: DirectScrapeRequest):
    """Legacy endpoint: explicit CSS selectors (no LLM selector generation)."""
    sel_dicts = [{"name": s.name, "css": s.css} for s in req.selectors]
    rows = scrape_with_selectors(req.url, sel_dicts)
    return {"ok": True, "url": req.url, "righe_totali": len(rows), "dati": rows}


@app.get("/search")
def semantic_search(
    q: str = Query(..., description="Query in linguaggio naturale"),
    top_k: int = Query(10, ge=1, le=100),
):
    """Semantic search over all tenders scraped so far."""
    store = get_store()
    results = store.semantic_search(q, top_k=top_k)
    return {
        "query": q,
        "total_in_store": store.count(),
        "total_with_embeddings": store.count_with_embeddings(),
        "risultati": results,
    }


@app.get("/report/{job_id}")
def download_report(job_id: str):
    from .pdf_report import generate_pdf
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job non trovato")
    pdf_bytes = generate_pdf(job)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="report_{job_id}.pdf"'},
    )


# Serve frontend (if present)
_frontend = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_frontend):
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")
