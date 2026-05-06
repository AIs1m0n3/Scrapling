# Smart-Scrape — Guida all'uso e architettura

## Due modalità di operazione

| Modalità       | Quando si attiva               | Cosa fa                                          |
|----------------|-------------------------------|--------------------------------------------------|
| `web_search`   | Nessun URL nel prompt          | LLM suggerisce siti → scraping + LLM selettori  |
| `site_scrape`  | URL esplicito nella richiesta  | Fetch URL → LLM analizza HTML → estrae record   |

Con `mode="auto"` (default) il sistema rileva la modalità automaticamente.

---

## Pipeline completa

### `web_search` (nessun URL)

```
Prompt utente
      │
      ▼
  [1] LLM principale (OPENROUTER_MODEL)
      │  → suggerisce N siti rilevanti (default max 5)
      ▼
  [2] Per ogni sito → pipeline site_scrape (vedi sotto)
      ▼
  [3] LLM principale → riassunto dei dati estratti
      ▼
  Risposta JSON + record salvati in embedding store
```

### `site_scrape` (URL esplicito)

```
URL + Prompt
      │
      ▼
  [1] Scrapling — Fetcher HTTP (curl_cffi)
      │  se fallisce (SSL / 403 / timeout) →
      ▼
  [2] Scrapling — StealthyFetcher (Playwright headless)
      │
      ▼
  [3] LLM economico (OPENROUTER_SEARCH_MODEL)
      │  Analizza HTML della pagina (snippet ~12KB)
      │  → genera schema CSS: {row_container, fields[]}
      ▼
  [4] Scrapling applica lo schema CSS ed estrae record strutturati:
      {titolo, cig, importo, scadenza, stazione_appaltante, link, ...}
      ▼
  [5] Record salvati in SQLite + embedding calcolati (async)
      ▼
  Risposta JSON
```

---

## Modelli LLM usati

| Variabile env              | Default                      | Uso                                   |
|---------------------------|------------------------------|---------------------------------------|
| `OPENROUTER_MODEL`         | `anthropic/claude-opus-4`    | Pianificazione siti + riassunti       |
| `OPENROUTER_SEARCH_MODEL`  | `google/gemini-flash-1.5`    | Analisi HTML + generazione selettori  |
| `OPENROUTER_EMBED_MODEL`   | `perplexity/pplx-embed-v1-4b`| Embedding per ricerca semantica       |

**Perché `google/gemini-flash-1.5` per l'analisi HTML?**
- Costo ~$0.075/$0.30 per milione di token (fino a 40x più economico di Opus)
- Finestra di contesto da 1M token → gestisce pagine HTML grandi senza troncature
- Veloce e affidabile per output JSON strutturato

---

## API HTTP

### `POST /smart-scrape`

```json
{
  "prompt": "string",
  "url": "string (opzionale)",
  "mode": "auto | web_search | site_scrape (default: auto)"
}
```

**Risposta:**
```json
{
  "ok": true,
  "job_id": "a1b2c3d4",
  "mode": "site_scrape",
  "siti_visitati": ["https://..."],
  "righe_totali": 42,
  "dati": [
    {
      "titolo": "Gara per servizi cloud",
      "cig": "A1B2C3D4",
      "importo": "€ 120.000",
      "scadenza": "2026-06-30",
      "stazione_appaltante": "ARIA Lombardia",
      "link": "https://...dettaglio",
      "fonte": "https://..."
    }
  ],
  "riassunto": "Sono state trovate 42 gare..."
}
```

### `GET /search` — Ricerca semantica

```
GET /search?q=<query>&top_k=<n>
```

Cerca tra tutti i record già estratti usando cosine similarity sugli embedding.
Vedi [docs/EMBEDDINGS.md](EMBEDDINGS.md) per i dettagli.

### `POST /scrape-direct` — Selettori CSS espliciti (legacy)

```json
{
  "url": "https://...",
  "selectors": [
    {"name": "Titolo bando", "css": "h2.bando-title"},
    {"name": "Stazione appaltante", "css": ".ente-appaltante"}
  ]
}
```

Usa questa modalità solo se conosci già i selettori CSS della pagina. Altrimenti
usa `/smart-scrape` che li genera automaticamente via LLM.

---

## Esempi curl

```bash
# 1) web_search — LLM sceglie i siti e i selettori
curl -X POST http://localhost:8000/smart-scrape \
  -H "Content-Type: application/json" \
  -d '{"prompt": "stazioni appaltanti Lombardia Piemonte Liguria Valle d Aosta"}'

# 2) site_scrape — URL esplicito, LLM genera i selettori automaticamente
curl -X POST http://localhost:8000/smart-scrape \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "gare servizi consulenza informatica sopra 40000 euro scadenza 60 giorni",
    "url": "https://www.aria.regione.lombardia.it/bandi",
    "mode": "site_scrape"
  }'

# 3) scrape diretto con selettori espliciti
curl -X POST http://localhost:8000/scrape-direct \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://www.aria.regione.lombardia.it/bandi",
    "selectors": [
      {"name": "Titolo", "css": "h2.bando-title"},
      {"name": "CIG", "css": ".cig"}
    ]
  }'

# 4) ricerca semantica sui dati già estratti
curl "http://localhost:8000/search?q=consulenza+informatica+Lombardia+sopra+40k&top_k=10"
```

---

## Configurazione

Tutte le variabili si impostano in `app/.env`:

| Variabile               | Default | Descrizione |
|-------------------------|---------|-------------|
| `OPENROUTER_MODEL`      | `anthropic/claude-opus-4` | Modello principale |
| `OPENROUTER_SEARCH_MODEL` | `google/gemini-flash-1.5` | Modello economico per HTML |
| `OPENROUTER_EMBED_MODEL`  | `perplexity/pplx-embed-v1-4b` | Embedding |
| `SMART_SCRAPE_MAX_SITES`| `5`     | Max siti per richiesta web_search |
| `MAX_ROWS_PER_SELECTOR` | `20`    | Max righe per selettore (modalità legacy) |
| `HTML_SNIPPET_MAX_CHARS`| `12000` | Max caratteri HTML inviati all'LLM |
| `HTTP_TIMEOUT`          | `30`    | Timeout Fetcher (secondi) |
| `BROWSER_TIMEOUT`       | `30000` | Timeout StealthyFetcher (millisecondi) |
| `HTTP_RETRIES`          | `3`     | Tentativi per fetcher |
| `HTTP_RETRY_DELAY`      | `2`     | Attesa tra retry (secondi) |
| `DATA_DIR`              | `./data` | Cartella SQLite embedding store |

---

## Gestione degli errori

| Situazione | Comportamento |
|---|---|
| SSL error | Usa `cacert.pem` → fallback a StealthyFetcher |
| HTTP 403 | Fallback immediato a StealthyFetcher |
| Timeout | Retry con backoff → fallback a StealthyFetcher |
| StealthyFetcher fallisce | URL skippato, log errore, pipeline continua |
| LLM non trova selettori | `row_container: null` → 0 record per quel sito |
| LLM non trova siti (web_search) | HTTP 400 |

---

## Avvio in Codespaces

```bash
cd /workspaces/Scrapling/app

# Installa dipendenze
pip install -r requirements.txt

# Avvia il server
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

# Verifica configurazione
curl http://localhost:8000/health | python3 -m json.tool
```

---

## Log strutturati

```
2026-05-06 10:00:01 [INFO] [abc12345] smart-scrape — mode=site_scrape prompt='gare IT' url='https://...'
2026-05-06 10:00:02 [INFO] [abc12345] Fetcher → https://...
2026-05-06 10:00:05 [INFO] [abc12345] Fetcher OK in 3.1s — https://...
2026-05-06 10:00:05 [INFO] [abc12345] HTML snippet: 8432 chars → sending to LLM
2026-05-06 10:00:06 [INFO] LLM selector-gen — model=google/gemini-flash-1.5 url='https://...'
2026-05-06 10:00:07 [INFO] LLM schema: container='table tbody tr' fields=['titolo','cig','importo',...]
2026-05-06 10:00:07 [INFO] [abc12345] Estratti 28 record da https://...
2026-05-06 10:00:07 [INFO] Stored 28/28 records in embedding store
2026-05-06 10:00:07 [INFO] [abc12345] Pipeline completata — siti=1 record=28
```
