"""Domain models and normalization for structured tender (gara d'appalto) data."""

import re
import datetime
import logging
from typing import Optional, TypedDict

log = logging.getLogger(__name__)


# ── Data models ───────────────────────────────────────────────────────────────

class TenderRecord(TypedDict, total=False):
    """Normalized gara d'appalto with standard field names.

    All fields default to None when not found on the source page.
    """
    title: Optional[str]             # Titolo / oggetto della gara
    tender_id: Optional[str]         # CIG o identificativo interno
    procuring_entity: Optional[str]  # Stazione appaltante / ente committente
    amount: Optional[float]          # Importo base d'asta (float)
    amount_raw: Optional[str]        # Importo come stringa originale
    deadline: Optional[str]          # Scadenza offerte ISO 8601
    deadline_raw: Optional[str]      # Scadenza come stringa originale
    url: Optional[str]               # Link diretto alla scheda gara
    location: Optional[str]          # Regione / luogo esecuzione
    cpv: Optional[str]               # Codice CPV
    procedure_type: Optional[str]    # Tipo di procedura (aperta, negoziata, …)
    category: Optional[str]          # Categoria / settore
    raw_source_url: Optional[str]    # URL della pagina elenco sorgente


class SourceInfo(TypedDict):
    url: str
    num_tenders: int
    pages_scraped: int


class TenderSearchResult(TypedDict):
    source_mode: str              # "site_scrape" | "web_search"
    sources: list[SourceInfo]
    tenders: list[TenderRecord]
    raw_records: list[dict]       # raw LLM-extracted records (backward compat)
    pages_scraped: int


# ── Field name alias mapping ──────────────────────────────────────────────────
# Maps LLM-generated field names → standard TenderRecord field name.
# The LLM can use different names depending on the site language/structure.

_ALIASES: dict[str, list[str]] = {
    "title": [
        "titolo", "title", "oggetto", "object", "denominazione",
        "descrizione", "oggetto_appalto", "nome", "subject",
    ],
    "tender_id": [
        "cig", "tender_id", "id_gara", "codice", "numero_gara",
        "identificativo", "id", "cod_cig", "numero",
    ],
    "procuring_entity": [
        "stazione_appaltante", "ente", "procuring_entity", "amministrazione",
        "committente", "ente_appaltante", "acquirente", "soggetto_appaltante",
    ],
    "amount_raw": [
        "importo", "amount", "base_asta", "importo_base", "valore",
        "somma", "budget", "importo_asta", "importo_contratto", "corrispettivo",
    ],
    "deadline_raw": [
        "scadenza", "deadline", "termine", "data_scadenza", "data_termine",
        "scadenza_offerte", "expiry", "scadenza_presentazione",
    ],
    "url": [
        "link", "url", "href", "dettaglio", "scheda",
        "link_dettaglio", "url_gara", "link_gara",
    ],
    "location": [
        "regione", "location", "luogo", "provincia", "comune",
        "area", "territorio", "place", "region",
    ],
    "cpv": ["cpv", "codice_cpv", "categoria_cpv", "cpv_code"],
    "procedure_type": [
        "tipo_procedura", "procedure_type", "procedura",
        "tipo", "modalita", "tipo_affidamento",
    ],
    "category": [
        "categoria", "category", "settore", "tipo_appalto",
        "oggetto_principale", "forniture", "servizi", "lavori",
    ],
}

# Reverse lookup: alias → standard name
_ALIAS_MAP: dict[str, str] = {}
for _std, _als in _ALIASES.items():
    for _a in _als:
        _ALIAS_MAP[_a.lower()] = _std


# ── Amount / date parsers ─────────────────────────────────────────────────────

def _parse_amount(raw: str | None) -> Optional[float]:
    """Parse Italian-format currency strings to float.

    Examples:
      "€ 1.234.567,89"  → 1234567.89
      "1.200.000"       → 1200000.0
      "120.000,00 €"    → 120000.0
      "€120000"         → 120000.0
    """
    if not raw:
        return None
    # Strip currency symbols, spaces, and non-numeric chars
    s = re.sub(r"[€$£\s\xa0]", "", str(raw))
    s = re.sub(r"[^\d.,]", "", s)
    if not s:
        return None
    try:
        if "," in s and "." in s:
            # Italian thousand+decimal: 1.234.567,89
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            # Comma only: might be decimal (1234,89) or separator (1.234)
            parts = s.split(",")
            if len(parts[-1]) <= 2:
                s = s.replace(",", ".")
            else:
                s = s.replace(",", "")
        elif s.count(".") > 1:
            # Multiple dots → thousands separators: 1.234.567
            s = s.replace(".", "")
        elif s.count(".") == 1 and len(s.split(".")[1]) == 3:
            # Single dot with 3-digit fractional part → Italian thousands: 50.000
            s = s.replace(".", "")
        return float(s)
    except ValueError:
        log.debug(f"Cannot parse amount: {raw!r}")
        return None


def _parse_deadline(raw: str | None) -> Optional[str]:
    """Parse various date formats to ISO 8601.

    Returns date string (YYYY-MM-DD) or datetime string (YYYY-MM-DDTHH:MM:SS).
    Returns the original string if parsing fails.
    """
    if not raw:
        return None
    s = str(raw).strip()
    # Normalize date separators in the date portion only
    date_part_raw = s.split("T")[0].split(" ")[0].strip()
    time_part = s.split(" ", 1)[1] if " " in s and "T" not in s else (s.split("T")[1] if "T" in s else "")
    s_norm = re.sub(r"[-/.]", "/", date_part_raw)

    formats_date = [
        ("%d/%m/%Y %H:%M:%S", True),
        ("%d/%m/%Y %H:%M", True),
        ("%d/%m/%Y", False),
        ("%Y/%m/%d %H:%M:%S", True),
        ("%Y/%m/%d %H:%M", True),
        ("%Y/%m/%d", False),
    ]
    for fmt, has_time in formats_date:
        try:
            candidate = s_norm
            if has_time and time_part:
                candidate = s_norm + " " + time_part
            dt = datetime.datetime.strptime(candidate, fmt)
            return dt.isoformat() if has_time else dt.date().isoformat()
        except ValueError:
            continue
    return s  # return original if unparseable


# ── Normalizer ────────────────────────────────────────────────────────────────

_TENDER_FIELDS = set(TenderRecord.__annotations__.keys())


def normalize_record(raw: dict) -> TenderRecord:
    """Convert a raw scraped dict to a normalized TenderRecord.

    - Maps LLM field names to standard names via _ALIAS_MAP
    - Parses amounts and dates
    - Fills missing required fields with None for a consistent schema
    """
    # Initialize with all fields = None
    tender: dict = {k: None for k in _TENDER_FIELDS}
    tender["raw_source_url"] = raw.get("fonte")

    for key, val in raw.items():
        if key == "fonte":
            continue
        # Skip empty values
        if val is None or (isinstance(val, str) and not val.strip()):
            continue

        std = _ALIAS_MAP.get(key.lower())

        if std == "amount_raw":
            if not tender["amount_raw"]:
                tender["amount_raw"] = str(val).strip()
                tender["amount"] = _parse_amount(val)
        elif std == "deadline_raw":
            if not tender["deadline_raw"]:
                tender["deadline_raw"] = str(val).strip()
                tender["deadline"] = _parse_deadline(val)
        elif std and std in _TENDER_FIELDS and not tender[std]:
            tender[std] = str(val).strip() or None
        elif not std:
            # Non-standard field: keep it under its original name if not already set
            if key not in tender:
                tender[key] = str(val).strip() or None

    return tender  # type: ignore[return-value]


def has_minimum_data(t: TenderRecord) -> bool:
    """Return True if a TenderRecord has at least a title or a url."""
    return bool(t.get("title") or t.get("url") or t.get("tender_id"))
