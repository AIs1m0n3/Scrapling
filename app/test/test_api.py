"""Test suite for the smart-scrape backend.

TestConfig          — config module (SSL, model vars, env overrides)
TestModels          — normalizer (amount, date, field mapping)
TestScraper         — scraper unit tests (mocked network)
TestPagination      — pagination detection + scrape_tenders with 25 fake rows
TestEmbeddingStore  — embedding store (in-memory SQLite)
TestHealthEndpoint  — /health HTTP
TestJobsEndpoint    — /jobs HTTP
TestSmartScrape     — /smart-scrape HTTP (all modes + structured output)
TestScrapeDirect    — /scrape-direct HTTP
TestSemanticSearch  — /search HTTP
"""

import json
import sqlite3
import pytest
from unittest.mock import patch, MagicMock


# ── Fixtures & helpers ────────────────────────────────────────────────────────

def _make_row(fields: dict):
    """Build a per-row mock that supports row.css(selector).getall/attrib."""
    row = MagicMock()

    def _row_css(selector):
        inner = MagicMock()
        # Strip ::text suffix to get base selector key
        base = selector.replace("::text", "")
        val = fields.get(base, "")
        inner.getall.return_value = [val] if val else []
        inner.get.return_value = val or ""
        inner.attrib = {"href": fields.get("href", "https://example.com/dettaglio")}
        return inner

    row.css = _row_css
    return row


def _make_page(rows_data: list[dict] | None = None, next_href: str = "", status: int = 200):
    """Build a page mock with iterable rows and optional next-page link.

    rows_data: list of dicts mapping CSS selector → text value
    next_href: if set, page.css("a[rel=next]").attrib returns {"href": next_href}
    """
    rows_data = rows_data or []
    row_mocks = [_make_row(d) for d in rows_data]

    page = MagicMock()
    page.status = status

    def _css(selector):
        sel = MagicMock()
        # Container iteration
        sel.__iter__ = lambda s: iter(row_mocks)
        sel.__bool__ = lambda s: bool(row_mocks)
        # getall: for snippet extraction
        sel.getall.return_value = [
            "<tr>" + "".join(f"<td>{v}</td>" for v in d.values()) + "</tr>"
            for d in rows_data
        ]
        sel.get.return_value = sel.getall.return_value[0] if sel.getall.return_value else ""
        # Pagination: respond to next-page selectors
        if next_href and ("next" in selector.lower() or "rel" in selector.lower() or "successiv" in selector.lower()):
            sel.attrib = {"href": next_href}
        else:
            sel.attrib = {}
        return sel

    page.css = _css
    page.get.return_value = "<html><body>" + "".join(
        "<tr>" + "".join(f"<td>{v}</td>" for v in d.values()) + "</tr>"
        for d in rows_data
    ) + "</body></html>"
    return page


def _make_tender_rows(n: int, prefix: str = "") -> list[dict]:
    """Generate n fake tender row data dicts."""
    return [
        {
            "titolo": f"{prefix}Gara fornitura abbigliamento n.{i+1}",
            "cig": f"CIG{i+1:04d}",
            "importo": f"€ {50_000 + i * 1_000:,}".replace(",", "."),
            "scadenza": f"{(i % 28) + 1:02d}/07/2026",
            "stazione_appaltante": f"Comune di Test {i+1}",
            "href": f"https://portale.example.com/gara/{i+1}",
        }
        for i in range(n)
    ]


# ── Patch heavy Scrapling deps at import time ─────────────────────────────────

with (
    patch("scrapling.fetchers.Fetcher") as _PF,
    patch("scrapling.fetchers.StealthyFetcher") as _PS,
):
    _PF.configure = MagicMock()
    _PS.configure = MagicMock()
    from app.backend.scraper import (
        _apply_schema,
        _detect_next_page,
        scrape_tenders,
        find_tenders,
        scrape_with_selectors,
        smart_site_scrape,
        smart_web_search,
    )
    from fastapi.testclient import TestClient
    from app.backend.main import app

client = TestClient(app)


# ── Config tests ──────────────────────────────────────────────────────────────

class TestConfig:
    def test_cacert_env_var_valid(self, tmp_path, monkeypatch):
        pem = tmp_path / "test.pem"
        pem.write_text("fake-cert")
        monkeypatch.setenv("CACERT_PATH", str(pem))
        import importlib, app.backend.config as cfg_mod
        importlib.reload(cfg_mod)
        assert cfg_mod.CACERT_PATH == str(pem.resolve())

    def test_cacert_env_var_invalid_path(self, monkeypatch, caplog):
        monkeypatch.setenv("CACERT_PATH", "/nonexistent/path.pem")
        import importlib, app.backend.config as cfg_mod
        with caplog.at_level("WARNING", logger="app.backend.config"):
            importlib.reload(cfg_mod)
        assert "does not point to a file" in caplog.text

    def test_new_constants_present(self):
        import app.backend.config as cfg
        assert cfg.SMART_SCRAPE_MAX_RESULTS >= 20
        assert cfg.SMART_SCRAPE_MAX_PAGES >= 1
        assert cfg.HTTP_TIMEOUT > 0
        assert cfg.BROWSER_TIMEOUT >= 10_000
        assert cfg.OPENROUTER_SEARCH_MODEL
        assert cfg.OPENROUTER_EMBED_MODEL

    def test_model_vars_configurable(self, monkeypatch):
        monkeypatch.setenv("SMART_SCRAPE_MAX_RESULTS", "42")
        monkeypatch.setenv("SMART_SCRAPE_MAX_PAGES", "7")
        import importlib, app.backend.config as cfg_mod
        importlib.reload(cfg_mod)
        assert cfg_mod.SMART_SCRAPE_MAX_RESULTS == 42
        assert cfg_mod.SMART_SCRAPE_MAX_PAGES == 7


# ── Models / normalizer tests ─────────────────────────────────────────────────

class TestModels:
    def test_parse_amount_italian_format(self):
        from app.backend.models import _parse_amount
        assert _parse_amount("€ 1.234.567,89") == pytest.approx(1234567.89)
        assert _parse_amount("120.000,00 €") == pytest.approx(120000.0)
        assert _parse_amount("1.200.000") == pytest.approx(1200000.0)
        assert _parse_amount("€120000") == pytest.approx(120000.0)
        assert _parse_amount("50.000") == pytest.approx(50000.0)

    def test_parse_amount_none_on_empty(self):
        from app.backend.models import _parse_amount
        assert _parse_amount(None) is None
        assert _parse_amount("") is None
        assert _parse_amount("n.d.") is None

    def test_parse_deadline_italian(self):
        from app.backend.models import _parse_deadline
        assert _parse_deadline("30/06/2026") == "2026-06-30"
        assert _parse_deadline("30/06/2026 12:00") == "2026-06-30T12:00:00"
        assert _parse_deadline("2026-06-30") == "2026-06-30"

    def test_normalize_record_field_mapping(self):
        from app.backend.models import normalize_record
        raw = {
            "titolo": "Gara fornitura",
            "cig": "ABC123",
            "importo": "€ 80.000",
            "scadenza": "15/07/2026",
            "stazione_appaltante": "Comune di Milano",
            "link": "https://portale.it/gara/1",
            "fonte": "https://portale.it/elenco",
        }
        t = normalize_record(raw)
        assert t["title"] == "Gara fornitura"
        assert t["tender_id"] == "ABC123"
        assert t["amount"] == pytest.approx(80000.0)
        assert t["deadline"] == "2026-07-15"
        assert t["procuring_entity"] == "Comune di Milano"
        assert t["url"] == "https://portale.it/gara/1"
        assert t["raw_source_url"] == "https://portale.it/elenco"

    def test_normalize_record_all_fields_present(self):
        from app.backend.models import normalize_record, TenderRecord
        raw = {"titolo": "Test", "fonte": "https://a.com"}
        t = normalize_record(raw)
        # All standard fields must be present (even if None)
        for field in TenderRecord.__annotations__:
            assert field in t, f"Missing field: {field}"

    def test_has_minimum_data(self):
        from app.backend.models import has_minimum_data
        assert has_minimum_data({"title": "Gara"}) is True
        assert has_minimum_data({"url": "https://x.com"}) is True
        assert has_minimum_data({"tender_id": "CIG001"}) is True
        assert has_minimum_data({"amount": 50000.0}) is False  # no title, url, or id


# ── Scraper unit tests ────────────────────────────────────────────────────────

class TestScraper:
    def test_apply_schema_extracts_records(self):
        rows_data = _make_tender_rows(3)
        page = _make_page(rows_data)
        schema = {
            "row_container": "tr",
            "fields": [
                {"name": "titolo", "css": "titolo", "attr": None},
                {"name": "cig",    "css": "cig",    "attr": None},
                {"name": "link",   "css": "a",      "attr": "href"},
            ],
            "pagination_next": None,
        }
        records = _apply_schema(page, schema, "https://example.com")
        assert len(records) == 3
        assert any(r.get("titolo") for r in records)

    def test_apply_schema_null_container(self):
        page = _make_page(_make_tender_rows(3))
        records = _apply_schema(page, {"row_container": None, "fields": []}, "https://ex.com")
        assert records == []

    def test_detect_next_page_from_schema(self):
        page = _make_page([], next_href="/page/2")
        schema = {"pagination_next": "a[rel=next]", "row_container": "tr", "fields": []}
        result = _detect_next_page(page, "https://ex.com/page/1", schema)
        assert result == "https://ex.com/page/2"

    def test_detect_next_page_none_when_absent(self):
        page = _make_page([])
        schema = {"pagination_next": None, "row_container": "tr", "fields": []}
        result = _detect_next_page(page, "https://ex.com/page/1", schema)
        assert result is None

    def test_scrape_tenders_25_rows_single_page(self):
        """Given HTML with 25 rows, scrape_tenders should return ≥ 20 tenders."""
        rows_data = _make_tender_rows(25)
        page = _make_page(rows_data)
        schema = {
            "row_container": "tr",
            "fields": [
                {"name": "titolo",              "css": "titolo",              "attr": None},
                {"name": "cig",                 "css": "cig",                 "attr": None},
                {"name": "importo",             "css": "importo",             "attr": None},
                {"name": "scadenza",            "css": "scadenza",            "attr": None},
                {"name": "stazione_appaltante", "css": "stazione_appaltante", "attr": None},
                {"name": "link",                "css": "a",                   "attr": "href"},
            ],
            "pagination_next": None,
        }
        mock_store = MagicMock()
        with (
            patch("app.backend.scraper.Fetcher") as MockF,
            patch("app.backend.scraper.extract_selectors_from_html", return_value=schema),
            patch("app.backend.embeddings.get_store", return_value=mock_store),
        ):
            MockF.get.return_value = page
            tenders, raw, pages = scrape_tenders(
                "https://ex.com", "abbigliamento Lombardia", max_results=50, max_pages=1, job_id="t1"
            )

        assert len(tenders) >= 20, f"Expected ≥ 20 tenders, got {len(tenders)}"
        assert pages == 1

        # Verify each tender has the required fields
        for t in tenders:
            assert "title" in t
            assert "tender_id" in t
            assert "procuring_entity" in t
            assert "url" in t
            assert "amount" in t
            assert "deadline" in t

    def test_scrape_tenders_structured_fields(self):
        """Verify normalized field values (amount parsed, deadline in ISO format)."""
        rows_data = [{
            "titolo": "Fornitura divise",
            "cig": "CIG9999",
            "importo": "€ 95.000",
            "scadenza": "30/07/2026",
            "stazione_appaltante": "ASL Brescia",
            "href": "https://portale.it/gara/99",
        }]
        page = _make_page(rows_data)
        schema = {
            "row_container": "tr",
            "fields": [
                {"name": "titolo",              "css": "titolo",              "attr": None},
                {"name": "cig",                 "css": "cig",                 "attr": None},
                {"name": "importo",             "css": "importo",             "attr": None},
                {"name": "scadenza",            "css": "scadenza",            "attr": None},
                {"name": "stazione_appaltante", "css": "stazione_appaltante", "attr": None},
                {"name": "link",                "css": "a",                   "attr": "href"},
            ],
            "pagination_next": None,
        }
        mock_store = MagicMock()
        with (
            patch("app.backend.scraper.Fetcher") as MockF,
            patch("app.backend.scraper.extract_selectors_from_html", return_value=schema),
            patch("app.backend.embeddings.get_store", return_value=mock_store),
        ):
            MockF.get.return_value = page
            tenders, _, _ = scrape_tenders("https://ex.com", "divise", max_results=10, max_pages=1)

        assert len(tenders) == 1
        t = tenders[0]
        assert t["title"] == "Fornitura divise"
        assert t["tender_id"] == "CIG9999"
        assert t["amount"] == pytest.approx(95000.0)
        assert t["deadline"] == "2026-07-30"
        assert t["procuring_entity"] == "ASL Brescia"

    def test_scrape_tenders_follows_pagination(self):
        """scrape_tenders should follow next-page links to accumulate more results."""
        # Page 1: 10 rows, points to page 2
        rows_p1 = _make_tender_rows(10, "P1-")
        page1 = _make_page(rows_p1, next_href="/gare?page=2")
        # Page 2: 10 rows, no next page
        rows_p2 = _make_tender_rows(10, "P2-")
        page2 = _make_page(rows_p2)

        schema = {
            "row_container": "tr",
            "fields": [
                {"name": "titolo", "css": "titolo", "attr": None},
                {"name": "link",   "css": "a",      "attr": "href"},
            ],
            "pagination_next": "a[rel=next]",
        }
        mock_store = MagicMock()
        call_count = {"n": 0}
        pages = [page1, page2]

        def _fake_fetch(url, *a, **kw):
            i = call_count["n"]
            call_count["n"] += 1
            return pages[i] if i < len(pages) else None

        with (
            patch("app.backend.scraper.Fetcher") as MockF,
            patch("app.backend.scraper.extract_selectors_from_html", return_value=schema),
            patch("app.backend.embeddings.get_store", return_value=mock_store),
        ):
            MockF.get.side_effect = _fake_fetch
            tenders, _, pages_visited = scrape_tenders(
                "https://ex.com/gare", "abbigliamento", max_results=50, max_pages=3
            )

        assert pages_visited == 2
        assert len(tenders) == 20

    def test_scrape_tenders_respects_max_results(self):
        rows_data = _make_tender_rows(30)
        page = _make_page(rows_data)
        schema = {
            "row_container": "tr",
            "fields": [{"name": "titolo", "css": "titolo", "attr": None}],
            "pagination_next": None,
        }
        mock_store = MagicMock()
        with (
            patch("app.backend.scraper.Fetcher") as MockF,
            patch("app.backend.scraper.extract_selectors_from_html", return_value=schema),
            patch("app.backend.embeddings.get_store", return_value=mock_store),
        ):
            MockF.get.return_value = page
            tenders, _, _ = scrape_tenders("https://ex.com", "test", max_results=15, max_pages=5)

        assert len(tenders) <= 15

    def test_find_tenders_site_scrape_mode(self):
        records = [{"titolo": "Gara 1", "cig": "CIG001", "fonte": "https://ex.com"}]
        with patch("app.backend.scraper.scrape_tenders") as mock_st:
            from app.backend.models import normalize_record
            mock_st.return_value = ([normalize_record(r) for r in records], records, 1)
            result = find_tenders("abbigliamento", url="https://ex.com", job_id="t2")

        assert result["source_mode"] == "site_scrape"
        assert len(result["sources"]) == 1
        assert result["sources"][0]["url"] == "https://ex.com"
        assert len(result["tenders"]) == 1

    def test_find_tenders_web_search_mode(self):
        plan = {"sites": ["https://a.com", "https://b.com"]}
        records = [{"titolo": "Gara web", "fonte": "https://a.com"}]
        with (
            patch("app.backend.scraper.ai_search_plan", return_value=plan),
            patch("app.backend.scraper.scrape_tenders") as mock_st,
        ):
            from app.backend.models import normalize_record
            mock_st.return_value = ([normalize_record(r) for r in records], records, 1)
            result = find_tenders("abbigliamento", job_id="t3")

        assert result["source_mode"] == "web_search"
        assert len(result["sources"]) == 2
        assert len(result["tenders"]) == 2  # 1 per site

    def test_scrape_tenders_fetch_failure_returns_empty(self):
        with (
            patch("app.backend.scraper.Fetcher") as MockF,
            patch("app.backend.scraper.StealthyFetcher") as MockS,
            patch("app.backend.scraper.time") as mock_time,
        ):
            mock_time.monotonic.return_value = 0.0
            mock_time.sleep = MagicMock()
            MockF.get.side_effect = Exception("Connection refused")
            MockS.fetch.side_effect = Exception("Timeout")
            tenders, raw, pages = scrape_tenders("https://ex.com", "test", job_id="t4")

        assert tenders == []
        assert raw == []
        assert pages == 0

    def test_scrape_with_selectors_legacy(self):
        page = _make_page([{"ente": "Comune di Roma"}])
        with patch("app.backend.scraper.Fetcher") as MockF:
            MockF.get.return_value = page
            rows = scrape_with_selectors(
                "https://ex.com", [{"name": "Ente", "css": "ente"}], job_id="t5"
            )
        assert any(r["campo"] == "Ente" for r in rows)


# ── Embedding store unit tests ────────────────────────────────────────────────

class TestEmbeddingStore:
    def test_add_and_count(self, tmp_path):
        from app.backend.embeddings import EmbeddingStore
        store = EmbeddingStore(db_path=tmp_path / "test.db")
        store.add_record({"titolo": "Gara appalto", "fonte": "https://ex.com"}, compute_embedding=False)
        assert store.count() == 1

    def test_add_multiple_records(self, tmp_path):
        from app.backend.embeddings import EmbeddingStore
        store = EmbeddingStore(db_path=tmp_path / "test.db")
        records = [{"titolo": f"Bando {i}", "fonte": "https://ex.com"} for i in range(5)]
        assert store.add_records(records, compute_embeddings=False) == 5

    def test_keyword_search(self, tmp_path):
        from app.backend.embeddings import EmbeddingStore
        store = EmbeddingStore(db_path=tmp_path / "test.db")
        store.add_record({"titolo": "Gara consulenza informatica Lombardia", "fonte": "https://ex.com"}, compute_embedding=False)
        store.add_record({"titolo": "Bando costruzione ponte", "fonte": "https://ex.com"}, compute_embedding=False)
        with patch("app.backend.embeddings.get_embedding", return_value=None):
            results = store.semantic_search("informatica Lombardia", top_k=5)
        assert len(results) > 0
        assert "informatica" in results[0]["text"].lower()

    def test_cosine_search_ranks_correctly(self, tmp_path):
        from app.backend.embeddings import EmbeddingStore
        store = EmbeddingStore(db_path=tmp_path / "test.db")
        vec_a = [1.0, 0.0, 0.0]
        vec_b = [0.0, 1.0, 0.0]
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("INSERT INTO records (text, embedding, source_url) VALUES (?, ?, ?)",
                         ("Gara IT", json.dumps(vec_a), "https://a.com"))
            conn.execute("INSERT INTO records (text, embedding, source_url) VALUES (?, ?, ?)",
                         ("Gara costruzione", json.dumps(vec_b), "https://b.com"))
        with patch("app.backend.embeddings.get_embedding", return_value=[0.9, 0.1, 0.0]):
            results = store._cosine_search("IT", top_k=2)
        assert results[0]["source_url"] == "https://a.com"

    def test_clear(self, tmp_path):
        from app.backend.embeddings import EmbeddingStore
        store = EmbeddingStore(db_path=tmp_path / "test.db")
        store.add_record({"titolo": "x", "fonte": "y"}, compute_embedding=False)
        store.clear()
        assert store.count() == 0


# ── HTTP endpoint tests ───────────────────────────────────────────────────────

class TestHealthEndpoint:
    def test_health_ok(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        cfg = resp.json()["config"]
        assert cfg["browser_timeout_ms"] >= 1000
        assert "max_results" in cfg
        assert "max_pages" in cfg

    def test_health_max_results_at_least_20(self):
        assert client.get("/health").json()["config"]["max_results"] >= 20


class TestJobsEndpoint:
    def test_jobs_structure(self):
        resp = client.get("/jobs")
        assert resp.status_code == 200
        for j in resp.json():
            assert "tenders_count" in j
            assert "pages_scraped" in j


class TestSmartScrape:
    def _tender_result(self, n=5, url="https://ex.com"):
        from app.backend.models import normalize_record
        from app.backend.scraper import TenderSearchResult, SourceInfo
        raw = [{"titolo": f"Gara {i}", "cig": f"CIG{i:04d}", "fonte": url} for i in range(n)]
        tenders = [normalize_record(r) for r in raw]
        return TenderSearchResult(
            source_mode="web_search",
            sources=[SourceInfo(url=url, num_tenders=n, pages_scraped=1)],
            tenders=tenders,
            raw_records=raw,
            pages_scraped=1,
        )

    def test_web_search_no_sites_raises_400(self):
        from app.backend.models import TenderSearchResult, SourceInfo
        empty = TenderSearchResult(
            source_mode="web_search", sources=[], tenders=[], raw_records=[], pages_scraped=0
        )
        with patch("app.backend.main.find_tenders", return_value=empty):
            resp = client.post("/smart-scrape", json={"prompt": "test"})
        assert resp.status_code == 400

    def test_web_search_returns_structured_tenders(self):
        result = self._tender_result(n=5)
        with (
            patch("app.backend.main.find_tenders", return_value=result),
            patch("app.backend.main.llm_summarize", return_value="OK"),
        ):
            resp = client.post("/smart-scrape", json={"prompt": "abbigliamento Lombardia"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["mode"] == "web_search"
        assert "tenders" in data
        assert "sources" in data
        assert "tenders_count" in data
        assert "pages_scraped" in data
        assert len(data["tenders"]) == 5
        # Backward compat fields still present
        assert "dati" in data
        assert "siti_visitati" in data

    def test_site_scrape_returns_20_tenders(self):
        result = self._tender_result(n=20, url="https://portale.it")
        result["source_mode"] = "site_scrape"
        with (
            patch("app.backend.main.find_tenders", return_value=result),
            patch("app.backend.main.llm_summarize", return_value="Trovate 20 gare."),
        ):
            resp = client.post(
                "/smart-scrape",
                json={"prompt": "fornitura abbigliamento sopra 40k", "url": "https://portale.it"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "site_scrape"
        assert data["tenders_count"] == 20
        assert len(data["tenders"]) == 20

    def test_tender_fields_all_present(self):
        """Every tender in the response must have all TenderRecord fields."""
        from app.backend.models import TenderRecord
        result = self._tender_result(n=3)
        with (
            patch("app.backend.main.find_tenders", return_value=result),
            patch("app.backend.main.llm_summarize", return_value=""),
        ):
            resp = client.post("/smart-scrape", json={"prompt": "test"})
        assert resp.status_code == 200
        for tender in resp.json()["tenders"]:
            for field in TenderRecord.__annotations__:
                assert field in tender, f"Tender missing field: {field}"

    def test_sources_structure(self):
        result = self._tender_result(n=3, url="https://aria.regione.lombardia.it")
        with (
            patch("app.backend.main.find_tenders", return_value=result),
            patch("app.backend.main.llm_summarize", return_value=""),
        ):
            resp = client.post("/smart-scrape", json={"prompt": "gare"})
        sources = resp.json()["sources"]
        assert len(sources) > 0
        assert sources[0]["url"] == "https://aria.regione.lombardia.it"
        assert "num_tenders" in sources[0]
        assert "pages_scraped" in sources[0]

    def test_auto_mode_with_url_uses_site_scrape(self):
        result = self._tender_result(n=2)
        result["source_mode"] = "site_scrape"
        with (
            patch("app.backend.main.find_tenders", return_value=result) as mock_ft,
            patch("app.backend.main.llm_summarize", return_value=""),
        ):
            client.post("/smart-scrape", json={"prompt": "test", "url": "https://ex.com"})
        _, kwargs = mock_ft.call_args
        assert kwargs["url"] == "https://ex.com"

    def test_site_scrape_requires_url(self):
        resp = client.post("/smart-scrape", json={"prompt": "gare", "mode": "site_scrape"})
        assert resp.status_code == 400

    def test_custom_max_results_respected(self):
        result = self._tender_result(n=10)
        with (
            patch("app.backend.main.find_tenders", return_value=result) as mock_ft,
            patch("app.backend.main.llm_summarize", return_value=""),
        ):
            client.post("/smart-scrape", json={"prompt": "test", "max_results": 7})
        _, kwargs = mock_ft.call_args
        assert kwargs["max_results"] == 7


class TestScrapeDirect:
    def test_scrape_direct_legacy(self):
        rows = [{"campo": "Ente", "valore": "Comune", "fonte": "https://ex.com"}]
        with patch("app.backend.main.scrape_with_selectors", return_value=rows):
            resp = client.post(
                "/scrape-direct",
                json={"url": "https://ex.com", "selectors": [{"name": "Ente", "css": "h1"}]},
            )
        assert resp.status_code == 200
        assert resp.json()["righe_totali"] == 1

    def test_report_not_found(self):
        assert client.get("/report/nonexistent").status_code == 404


class TestSemanticSearch:
    def test_search_structure(self):
        mock_store = MagicMock()
        mock_store.semantic_search.return_value = [
            {"id": 1, "score": 0.92, "text": "Gara IT Lombardia", "source_url": "https://a.com", "record": {}}
        ]
        mock_store.count.return_value = 5
        mock_store.count_with_embeddings.return_value = 3
        with patch("app.backend.main.get_store", return_value=mock_store):
            resp = client.get("/search", params={"q": "consulenza IT"})
        data = resp.json()
        assert "risultati" in data
        assert data["risultati"][0]["score"] == 0.92

    def test_search_requires_q(self):
        assert client.get("/search").status_code == 422
