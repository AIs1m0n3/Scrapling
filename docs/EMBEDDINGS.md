# Sistema di Embedding e Ricerca Semantica

## Cos'è e a cosa serve

Ogni record estratto da Scrapling (bando, stazione appaltante, gara, ecc.)
viene salvato in un database SQLite locale (`app/data/embeddings.db`) insieme
alla sua rappresentazione vettoriale (**embedding**).

Questo permette di fare **ricerca semantica**: invece di cercare per parole
chiave esatte, puoi chiedere in linguaggio naturale e ottenere i record più
simili semanticamente alla tua query.

Esempio:
```bash
# Cerca gare simili a "consulenza informatica sopra 40k"
curl "http://localhost:8000/search?q=consulenza%20informatica%20sopra%2040k&top_k=10"
```

---

## Architettura

```
Scrapling estrae record
      │
      ▼
EmbeddingStore.add_records(records)
      │
      ├── Per ogni record:
      │     testo = "titolo: X | cig: Y | importo: Z | ..."
      │     embedding = OpenRouter API → perplexity/pplx-embed-v1-4b → vettore float[]
      │
      └── SQLite: records(id, text, embedding JSON, source_url, metadata JSON)
                                │
GET /search?q=...               ▼
      │           EmbeddingStore.semantic_search(query)
      │                 │
      │                 ├── get_embedding(query)  → vettore query
      │                 ├── carica tutti gli embedding dal DB
      │                 ├── cosine similarity (numpy)
      │                 └── ritorna top-K per similarità decrescente
      └──────────────────────────────────────────────────────────→ JSON
```

---

## Modello di embedding

Il modello predefinito è **`perplexity/pplx-embed-v1-4b`** via OpenRouter.

Configurabile via `.env`:
```dotenv
OPENROUTER_EMBED_MODEL=perplexity/pplx-embed-v1-4b
```

### Perché questo modello?
- 4 miliardi di parametri, ottimizzato per embedding di testi in più lingue
- Disponibile su OpenRouter senza infrastruttura proprietaria
- Dimensione vettore bilanciata (qualità vs costo storage)

---

## Endpoint `/search`

```
GET /search?q=<query>&top_k=<n>
```

| Parametro | Default | Descrizione                         |
|-----------|---------|-------------------------------------|
| `q`       | —       | Query in linguaggio naturale (obbligatorio) |
| `top_k`   | `10`    | Numero massimo di risultati (1–100) |

### Esempio di risposta:
```json
{
  "query": "gare servizi IT Lombardia sopra 40k",
  "total_in_store": 142,
  "total_with_embeddings": 138,
  "risultati": [
    {
      "id": 47,
      "score": 0.9312,
      "text": "titolo: Fornitura servizi cloud | importo: 85.000 | ...",
      "source_url": "https://www.aria.regione.lombardia.it/bandi/1234",
      "record": {
        "titolo": "Fornitura servizi cloud",
        "cig": "A1B2C3D4",
        "importo": "85.000",
        "scadenza": "2026-06-15",
        "stazione_appaltante": "ARIA Lombardia"
      }
    }
  ]
}
```

---

## Modulo `app/backend/embeddings.py`

### Funzioni principali

| Funzione / Classe | Descrizione |
|---|---|
| `get_embedding(text)` | Chiama OpenRouter → restituisce `list[float]` o `None` |
| `EmbeddingStore` | Classe principale, wrappa SQLite |
| `EmbeddingStore.add_record(record)` | Aggiunge 1 record + embedding |
| `EmbeddingStore.add_records(records)` | Aggiunge N record |
| `EmbeddingStore.semantic_search(query, top_k)` | Ricerca semantica |
| `EmbeddingStore.count()` | Numero totale di record |
| `EmbeddingStore.clear()` | Cancella tutti i record |
| `get_store()` | Singleton globale dell'EmbeddingStore |

### Uso standalone:
```python
from app.backend.embeddings import get_store

store = get_store()

# Aggiungi record manualmente
store.add_records([
    {"titolo": "Gara cloud", "importo": "85.000", "fonte": "https://..."}
])

# Ricerca semantica
results = store.semantic_search("servizi cloud", top_k=5)
for r in results:
    print(f"[{r['score']:.3f}] {r['record'].get('titolo')}")
```

---

## Dipendenze

| Pacchetto | Ruolo | Obbligatorio |
|-----------|-------|--------------|
| `numpy`   | Cosine similarity veloce | Raccomandato |
| `httpx`   | Chiamata API embeddings | Sì (già in requirements) |
| `sqlite3` | Storage locale | Sì (built-in Python) |

Se `numpy` non è installato, il sistema usa automaticamente un fallback a
**keyword matching** (più lento, meno preciso, ma funzionale).

---

## Attivazione e configurazione

### 1. Installa numpy (se non già fatto)
```bash
pip install numpy
```

### 2. Configura l'API key nel `.env`
```dotenv
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_EMBED_MODEL=perplexity/pplx-embed-v1-4b
DATA_DIR=./data
```

### 3. Avvia il server
Gli embedding vengono calcolati automaticamente dopo ogni scraping.

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

### 4. Popola il database facendo scraping
```bash
curl -X POST http://localhost:8000/smart-scrape \
  -H "Content-Type: application/json" \
  -d '{"prompt": "stazioni appaltanti Lombardia"}'
```

### 5. Cerca semanticamente
```bash
curl "http://localhost:8000/search?q=consulenza+informatica+sopra+40k&top_k=10" \
  | python3 -m json.tool
```

---

## Dove viene salvato il database

Default: `app/data/embeddings.db`  
Configurabile via env var `DATA_DIR`.

Il file `.gitignore` esclude automaticamente `*.db` da git (ma mantiene
`app/data/.gitkeep` per creare la directory nel repo).

---

## Considerazioni sui costi

Ogni record viene embeddato con una chiamata separata all'API.
Con **`perplexity/pplx-embed-v1-4b`** i costi sono molto bassi (centesimi per
migliaia di record). Se il volume è elevato, puoi disabilitare l'embedding
automatico:

```python
store.add_records(records, compute_embeddings=False)
```

e calcolarli in batch in un secondo momento.
