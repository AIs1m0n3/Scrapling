"""FastAPI application — smart-scrape backend.

Endpoints:
  POST /smart-scrape   — auto-detect mode (web_search | site_scrape) from prompt
  POST /scrape-direct  — legacy: explicit CSS selectors
  GET  /search         — semantic search over stored records
  GET  /jobs           — list past jobs
  GET  /report/{id}    — download PDF report
  GET  /health         — health check with active configuration
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
# before any network library is initialised.
from .config import (
    CACERT_PATH,
    HTTP_TIMEOUT,
    BROWSER_TIMEOUT,
    SMART_SCRAPE_MAX_SITES,
    OPENROUTER_SEARCH_MODEL,
    OPENROUTER_EMBED_MODEL,
)
from .scraper import smart_site_scrape, smart_web_search, scrape_with_selectors
from .llm_router import llm_summarize
from .embeddings import get_store  # imported at top so tests can patch app.backend.main.get_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="Scrapling SaaS", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job store (not persisted across restarts)
jobs: dict[str, dict] = {}


# ── Models ────────────────────────────────────────────────────────────────────

class SmartScrapeRequest(BaseModel):
    prompt: str
    url: Optional[str] = None
    mode: Optional[Literal["web_search", "site_scrape", "auto"]] = "auto"


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
            "righe_totali": len(j.get("dati", [])),
        }
        for jid, j in jobs.items()
    ]


@app.post("/smart-scrape")
def smart_scrape(req: SmartScrapeRequest):
    job_id = str(uuid.uuid4())[:8]

    # ── Mode auto-detection ───────────────────────────────────────────────────
    # If the user passes mode="auto" (default), detect from URL presence.
    effective_mode = req.mode
    if effective_mode == "auto":
        effective_mode = "site_scrape" if req.url else "web_search"

    log.info(
        f"[{job_id}] smart-scrape — mode={effective_mode} "
        f"prompt={req.prompt!r} url={req.url}"
    )

    siti_visitati: list[str] = []
    all_data: list[dict] = []

    # ── site_scrape ───────────────────────────────────────────────────────────
    if effective_mode == "site_scrape":
        if not req.url:
            raise HTTPException(
                status_code=400,
                detail="mode='site_scrape' richiede un URL esplicito.",
            )
        siti_visitati = [req.url]
        all_data = smart_site_scrape(req.url, req.prompt, job_id=job_id)

    # ── web_search ────────────────────────────────────────────────────────────
    else:
        siti_visitati, all_data = smart_web_search(
            req.prompt,
            max_sites=SMART_SCRAPE_MAX_SITES,
            job_id=job_id,
        )
        if not siti_visitati:
            raise HTTPException(
                status_code=400,
                detail="LLM non ha trovato siti da analizzare.",
            )

    log.info(
        f"[{job_id}] Pipeline completata — siti={len(siti_visitati)} "
        f"record={len(all_data)}"
    )

    riassunto = ""
    if all_data:
        riassunto = llm_summarize(all_data, req.prompt)

    job = {
        "job_id": job_id,
        "prompt": req.prompt,
        "mode": effective_mode,
        "siti_visitati": siti_visitati,
        "dati": all_data,
        "riassunto": riassunto,
        "created_at": datetime.utcnow().isoformat(),
    }
    jobs[job_id] = job

    return {
        "ok": True,
        "job_id": job_id,
        "mode": effective_mode,
        "siti_visitati": siti_visitati,
        "righe_totali": len(all_data),
        "dati": all_data,
        "riassunto": riassunto,
    }


@app.post("/scrape-direct")
def scrape_direct(req: DirectScrapeRequest):
    """Legacy endpoint: scrape a URL with explicit CSS selectors (no LLM)."""
    sel_dicts = [{"name": s.name, "css": s.css} for s in req.selectors]
    rows = scrape_with_selectors(req.url, sel_dicts)
    return {"ok": True, "url": req.url, "righe_totali": len(rows), "dati": rows}


@app.get("/search")
def semantic_search(
    q: str = Query(..., description="Query in linguaggio naturale"),
    top_k: int = Query(10, ge=1, le=100),
):
    """Semantic search over all records scraped so far.

    Uses embedding-based cosine similarity if numpy + OpenRouter embeddings
    are available, otherwise falls back to keyword matching.
    """
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
