# Gestione SSL e certificati CA — Guida per sviluppatori

## Perché servono i certificati CA?

Quando Scrapling fa richieste HTTPS usa **curl_cffi** (fetcher HTTP) e
**Playwright/patchright** (fetcher browser). Entrambe le librerie verificano il
certificato del server confrontandolo con un elenco di Autorità di
Certificazione (CA) fidate.

In ambienti containerizzati (Codespaces, Docker) il bundle di CA di sistema
può essere incompleto o obsoleto, causando errori del tipo:

```
curl: (60) SSL certificate problem: unable to get local issuer certificate
```

Questo accade spesso sui portali della Pubblica Amministrazione italiana che
usano CA intermedie firmate da **AgID** (Agenzia per l'Italia Digitale) o da
provider non presenti nel bundle di sistema.

---

## Il file `cacert.pem`

Il file `cacert.pem` nella root del repository contiene il **Mozilla CA
Certificate Store**, il bundle standard usato da Firefox e dalla libreria
`curl`. È lo stesso bundle distribuito dal progetto [curl](https://curl.se/docs/caextract.html).

### Come aggiornarlo

```bash
# Scarica l'ultima versione dal sito ufficiale curl
curl -o cacert.pem https://curl.se/ca/cacert.pem

# Oppure con Python (richiede certifi installato)
python -c "import certifi, shutil; shutil.copy(certifi.where(), 'cacert.pem')"
```

Aggiorna `cacert.pem` periodicamente (ogni 3-6 mesi) per includere nuove CA.

---

## Come funziona l'auto-rilevamento

Il modulo `app/backend/config.py` cerca il file CA bundle nel seguente ordine:

1. Variabile d'ambiente `CACERT_PATH` (percorso assoluto o relativo)
2. `cacert.pem` nella root del repository (auto-rilevato)
3. `cacert.pem` nella directory `app/`
4. Fallback al bundle di sistema (default curl/OpenSSL)

Quando viene trovato un bundle valido, `config.py` imposta automaticamente:

| Variabile d'ambiente  | Usata da                        |
|-----------------------|---------------------------------|
| `CURL_CA_BUNDLE`      | curl, curl_cffi, libcurl        |
| `SSL_CERT_FILE`       | Python `ssl` module, OpenSSL    |
| `REQUESTS_CA_BUNDLE`  | requests, httpx                 |

Queste variabili vengono impostate **all'avvio del processo**, prima che
qualsiasi libreria di rete venga inizializzata.

---

## Configurazione in Codespaces

### Opzione A — Auto-rilevamento (consigliato)

Il file `cacert.pem` nella root del repo viene rilevato automaticamente. Non
è necessaria nessuna configurazione aggiuntiva.

```bash
# Verifica che il file esista
ls -lh /workspaces/Scrapling/cacert.pem

# Avvia il server: cacert.pem viene usato automaticamente
cd /workspaces/Scrapling/app
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

### Opzione B — Variabile d'ambiente esplicita

Aggiunge nel file `app/.env`:

```dotenv
CACERT_PATH=/workspaces/Scrapling/cacert.pem
```

### Opzione C — A livello di shell (per sessioni temporanee)

```bash
export CURL_CA_BUNDLE=/workspaces/Scrapling/cacert.pem
export SSL_CERT_FILE=/workspaces/Scrapling/cacert.pem
```

---

## Siti che restituiscono 403 Forbidden

Il codice HTTP 403 non è un problema SSL — il server ha ricevuto la richiesta
ma ha rifiutato l'accesso (es. protezione anti-bot). In questo caso:

- `Fetcher` (HTTP) fallisce con `HTTP 403` → il backend passa a `StealthyFetcher`
- `StealthyFetcher` usa un browser headless che supera la maggior parte delle
  protezioni WAF/Cloudflare

Se anche `StealthyFetcher` fallisce, l'URL viene saltato con un log d'errore
e la pipeline continua con gli altri siti.

---

## Verifica rapida

```bash
# Test SSL da riga di comando
curl -v --cacert /workspaces/Scrapling/cacert.pem https://www.agenziaentrate.gov.it/

# Test via endpoint /health (mostra il path del CA bundle usato)
curl http://localhost:8000/health | python3 -m json.tool
```

L'endpoint `/health` risponde con:

```json
{
  "status": "ok",
  "config": {
    "cacert": "/workspaces/Scrapling/cacert.pem",
    "http_timeout": 30,
    "browser_timeout_ms": 30000,
    "max_sites": 5
  }
}
```
