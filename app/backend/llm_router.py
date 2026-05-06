import os
import json
import logging
import httpx
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
if not OPENROUTER_KEY or (OPENROUTER_KEY.startswith("sk-or-") and len(OPENROUTER_KEY) < 30):
    raise ValueError("OPENROUTER_API_KEY mancante o non valida. Impostala nel .env")

OPENROUTER_BASE = "https://openrouter.ai/api/v1"

# Main (quality) model — used for site planning and summarisation
MODEL: str = os.getenv("OPENROUTER_MODEL", "anthropic/claude-opus-4")

# Search (cheap) model — used for HTML analysis and selector generation.
# google/gemini-flash-1.5: ~$0.075/$0.30 per M tokens, 1M context window.
SEARCH_MODEL: str = os.getenv("OPENROUTER_SEARCH_MODEL", "google/gemini-flash-1.5")

_HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_KEY}",
    "Content-Type": "application/json",
    "HTTP-Referer": "https://scrapling-saas.local",
    "X-Title": "Scrapling SaaS",
}


def _chat_model(messages: list[dict], model: str, temperature: float = 0.2) -> str:
    """Call the OpenRouter chat completions API with the specified model."""
    payload = {"model": model, "messages": messages, "temperature": temperature}
    with httpx.Client(timeout=60) as client:
        resp = client.post(
            f"{OPENROUTER_BASE}/chat/completions",
            headers=_HEADERS,
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()


def _chat(messages: list[dict], temperature: float = 0.3) -> str:
    """Call OpenRouter with the main (quality) model."""
    return _chat_model(messages, model=MODEL, temperature=temperature)


def _parse_json_response(raw: str) -> dict | list:
    """Strip markdown fences and parse JSON from an LLM response."""
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip().rstrip("```").strip())


# ── High-level LLM functions ──────────────────────────────────────────────────

def ai_search_plan(prompt: str, max_sites: int = 5) -> dict:
    """Ask the LLM to suggest relevant sites and CSS selectors for a research topic."""
    system = (
        "You are a web research assistant. Given a research topic, return a JSON object with:\n"
        f'- "sites": array of up to {max_sites} relevant URLs to scrape '
        "(real, publicly accessible, prefer official/institutional sources)\n"
        '- "selectors": array of objects with "name" (field label) and "css" '
        "(CSS selector to extract rows/items)\n"
        "Return ONLY valid JSON, no markdown, no explanation."
    )
    log.info(f"LLM site-plan — model={MODEL} max_sites={max_sites} prompt={prompt!r}")
    raw = _chat([{"role": "system", "content": system}, {"role": "user", "content": prompt}])
    try:
        plan = _parse_json_response(raw)
        sites = plan.get("sites", [])
        log.info(f"LLM suggested {len(sites)} site(s): {sites}")
        return plan
    except json.JSONDecodeError as exc:
        log.error(f"LLM returned invalid JSON: {exc}  raw={raw[:200]!r}")
        return {"sites": [], "selectors": []}


def extract_selectors_from_html(html: str, query: str, url: str = "") -> dict:
    """Use the cheap SEARCH_MODEL to generate a CSS selector schema from an HTML snippet.

    Returns a dict:
      {
        "row_container": "CSS selector for each row/item",
        "fields": [
          {"name": "titolo",              "css": "...", "attr": null},
          {"name": "cig",                 "css": "...", "attr": null},
          {"name": "importo",             "css": "...", "attr": null},
          {"name": "scadenza",            "css": "...", "attr": null},
          {"name": "stazione_appaltante", "css": "...", "attr": null},
          {"name": "link",                "css": "a",  "attr": "href"}
        ],
        "pagination_next": "CSS selector for the next-page link (or null)"
      }
    On failure returns {"row_container": null, "fields": [], "pagination_next": null}.
    """
    system = (
        "You are an expert HTML analyst specializing in Italian public procurement portals. "
        "Given an HTML snippet and a data extraction request, generate CSS selectors "
        "to extract ALL structured records visible on the page.\n\n"
        "Return ONLY a valid JSON object with this EXACT structure (no extra keys, no markdown):\n"
        "{\n"
        '  "row_container": "CSS selector matching EACH individual tender/record row or card",\n'
        '  "fields": [\n'
        '    {"name": "titolo",              "css": "<selector>", "attr": null},\n'
        '    {"name": "cig",                 "css": "<selector>", "attr": null},\n'
        '    {"name": "importo",             "css": "<selector>", "attr": null},\n'
        '    {"name": "scadenza",            "css": "<selector>", "attr": null},\n'
        '    {"name": "stazione_appaltante", "css": "<selector>", "attr": null},\n'
        '    {"name": "link",                "css": "a",          "attr": "href"},\n'
        '    {"name": "regione",             "css": "<selector>", "attr": null}\n'
        "  ],\n"
        '  "pagination_next": "CSS selector for the \'next page\' link, or null if not present"\n'
        "}\n\n"
        "RULES:\n"
        "1. row_container: MUST match every individual tender row (not the table/list itself). "
        "   Examples: 'table#results tbody tr', 'div.tender-item', 'li.gara', 'tr.bando'.\n"
        "2. fields css: RELATIVE to row_container. Do NOT include '::text' (added automatically).\n"
        "3. attr: null for text content; 'href' for anchor links.\n"
        "4. Include ONLY fields you can actually identify in the HTML. Skip fields not found.\n"
        "5. pagination_next: look for 'successivo', '>', '›', 'next', 'avanti', rel=next. "
        "   Return the full CSS selector for the <a> element, or null.\n"
        "6. If no structured list/table of items is found, return: "
        '{"row_container": null, "fields": [], "pagination_next": null}'
    )
    url_hint = f"\nSource URL: {url}" if url else ""
    user = f"Extraction request: {query}{url_hint}\n\nHTML:\n{html}"

    log.info(f"LLM selector-gen — model={SEARCH_MODEL} url={url!r}")
    try:
        raw = _chat_model(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=SEARCH_MODEL,
            temperature=0.1,
        )
        schema = _parse_json_response(raw)
        log.info(
            f"LLM schema: container={schema.get('row_container')!r} "
            f"fields={[f['name'] for f in schema.get('fields', [])]} "
            f"pagination={schema.get('pagination_next')!r}"
        )
        # Ensure pagination_next is present (older model responses might omit it)
        schema.setdefault("pagination_next", None)
        return schema
    except Exception as exc:
        log.error(f"Selector extraction failed: {exc}")
        return {"row_container": None, "fields": [], "pagination_next": None}


def llm_summarize(data: list[dict], prompt: str) -> str:
    """Produce an Italian summary of extracted data using the main (quality) model."""
    system = (
        "Sei un analista di mercato. Ricevi dati estratti dal web e devi produrre "
        "un riassunto leggibile in italiano. Sii conciso e informativo."
    )
    user = (
        f"Ricerca: {prompt}\n\n"
        f"Dati estratti:\n{json.dumps(data[:50], ensure_ascii=False, indent=2)}\n\n"
        "Scrivi un'analisi breve (5-8 frasi) dei risultati."
    )
    try:
        return _chat([{"role": "system", "content": system}, {"role": "user", "content": user}])
    except Exception as exc:
        return f"Riassunto non disponibile: {exc}"
