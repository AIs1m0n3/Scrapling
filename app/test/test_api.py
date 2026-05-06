"""Test suite for the smart-scrape backend.

Organized as:
  TestConfig          — config module (SSL, model vars, env overrides)
  TestScraper         — unit tests for scraper.py helpers (mocked network)
  TestEmbeddingStore  — unit tests for embeddings.py (in-memory SQLite)
  TestHealthEndpoint  — /health HTTP endpoint
  TestJobsEndpoint    — /jobs HTTP endpoint
  TestSmartScrape     — /smart-scrape HTTP endpoint (both modes)
  TestScrapeDirect    — /scrape-direct HTTP endpoint
  TestSemanticSearch  — /search HTTP endpoint
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_page(texts: list[str] | str, status: int = 200):
    """Return a minimal Scrapling Response mock.

    page.css(any_selector):
      - .getall()  → list of texts
      - .get()     → first text
      - .attrib    → {"href": "https://example.com/dettaglio"}
      - iteration  → yields one per-row mock (so _apply_schema can iterate rows)
    Each row mock supports the same css() interface for field extraction.
    """
    if isinstance(texts, str):
        texts = [texts]

    def _make_row(text: str):
        row = MagicMock()

        def _row_css(selector):
            inner = MagicMock()
            inner.getall.return_value = [text] if text else []
            inner.get.return_value = text
            inner.attrib = {"href": "https://example.com/dettaglio"}
            return inner

        row.css = _row_css
        return row

    row_mocks = [_make_row(t) for t in texts]

    page = MagicMock()
    page.status = status

    def _css(selector):
        sel = MagicMock()
        sel.getall.return_value = texts
        sel.get.return_value = texts[0] if texts else ""
        sel.attrib = {"href": "https://example.com/dettaglio"}
        # Make iterable so `for row in page.css(...)` works in _apply_schema
        sel.__iter__ = lambda s: iter(row_mocks)
        sel.__bool__ = lambda s: bool(texts)
        return sel

    page.css = _css
    page.get.return_value = "<html><body>" + "".join(f"<p>{t}</p>" for t in texts) + "</body></html>"
    return page


# ── Config tests ──────────────────────────────────────────────────────────────

class TestConfig:
    def test_cacert_env_var_valid(self, tmp_path, monkeypatch):
        pem = tmp_path / "test.pem"
        pem.write_text("fake-cert")
        monkeypatch.setenv("CACERT_PATH", str(pem))
        import importlib
        import app.backend.config as cfg_mod
        importlib.reload(cfg_mod)
        assert cfg_mod.CACERT_PATH == str(pem.resolve())

    def test_cacert_env_var_invalid_path(self, monkeypatch, caplog):
        monkeypatch.setenv("CACERT_PATH", "/nonexistent/path.pem")
        import importlib
        import app.backend.config as cfg_mod
        with caplog.at_level("WARNING", logger="app.backend.config"):
            importlib.reload(cfg_mod)
        assert "does not point to a file" in caplog.text

    def test_defaults(self):
        import app.backend.config as cfg
        assert cfg.HTTP_TIMEOUT > 0
        assert cfg.BROWSER_TIMEOUT >= 10_000, "Browser timeout must be in ms"
        assert cfg.SMART_SCRAPE_MAX_SITES >= 1
        assert cfg.OPENROUTER_SEARCH_MODEL  # must be non-empty
        assert cfg.OPENROUTER_EMBED_MODEL   # must be non-empty

    def test_model_vars_configurable(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_SEARCH_MODEL", "google/gemini-flash-2.0")
        monkeypatch.setenv("OPENROUTER_EMBED_MODEL", "perplexity/pplx-embed-v1-4b")
        import importlib
        import app.backend.config as cfg_mod
        importlib.reload(cfg_mod)
        assert cfg_mod.OPENROUTER_SEARCH_MODEL == "google/gemini-flash-2.0"
        assert cfg_mod.OPENROUTER_EMBED_MODEL == "perplexity/pplx-embed-v1-4b"


# ── Scraper unit tests ────────────────────────────────────────────────────────

# Patch heavy deps before importing scraper
with (
    patch("scrapling.fetchers.Fetcher") as _PF,
    patch("scrapling.fetchers.StealthyFetcher") as _PS,
):
    _PF.configure = MagicMock()
    _PS.configure = MagicMock()
    from app.backend.scraper import (
        _html_snippet,
        _apply_schema,
        smart_site_scrape,
        smart_web_search,
        scrape_with_selectors,
    )


class TestScraper:
    def test_apply_schema_extracts_records(self):
        page = _make_page(["Bando Comune di Milano 2024"])
        schema = {
            "row_container": "p",
            "fields": [
                {"name": "titolo", "css": "p", "attr": None},
            ],
        }
        with patch("app.backend.scraper.Fetcher") as MockF:
            MockF.get.return_value = page
            records = _apply_schema(page, schema, "https://example.com")

        assert any(r.get("titolo") for r in records)

    def test_apply_schema_empty_container_returns_empty(self):
        page = _make_page([])
        schema = {"row_container": "table tbody tr", "fields": []}
        records = _apply_schema(page, schema, "https://example.com")
        assert records == []

    def test_apply_schema_null_container_returns_empty(self):
        page = _make_page(["anything"])
        schema = {"row_container": None, "fields": []}
        records = _apply_schema(page, schema, "https://example.com")
        assert records == []

    def test_smart_site_scrape_returns_records(self):
        page = _make_page(["Gara: consulenza IT - CIG 12345"])
        schema = {
            "row_container": "p",
            "fields": [{"name": "titolo", "css": "p", "attr": None}],
        }
        mock_store = MagicMock()
        with (
            patch("app.backend.scraper.Fetcher") as MockF,
            patch("app.backend.scraper.extract_selectors_from_html", return_value=schema),
            patch("app.backend.embeddings.get_store", return_value=mock_store),
        ):
            MockF.get.return_value = page
            records = smart_site_scrape("https://example.com", "gare IT", job_id="t1")

        assert isinstance(records, list)

    def test_smart_site_scrape_fetch_failure_returns_empty(self):
        with (
            patch("app.backend.scraper.Fetcher") as MockF,
            patch("app.backend.scraper.StealthyFetcher") as MockS,
            patch("app.backend.scraper.time") as mock_time,
        ):
            mock_time.monotonic.return_value = 0.0
            mock_time.sleep = MagicMock()
            MockF.get.side_effect = Exception("Connection refused")
            MockS.fetch.side_effect = Exception("Timeout")
            records = smart_site_scrape("https://example.com", "query", job_id="t2")

        assert records == []

    def test_smart_web_search_calls_site_scrape_per_url(self):
        plan = {"sites": ["https://a.com", "https://b.com"]}
        with (
            patch("app.backend.scraper.ai_search_plan", return_value=plan),
            patch("app.backend.scraper.smart_site_scrape", return_value=[{"titolo": "x"}]) as mock_sss,
        ):
            sites, records = smart_web_search("appalti Lombardia", max_sites=5, job_id="t3")

        assert sites == ["https://a.com", "https://b.com"]
        assert mock_sss.call_count == 2
        assert len(records) == 2

    def test_scrape_with_selectors_legacy(self):
        page = _make_page(["Stazione appaltante Milano"])
        selectors = [{"name": "Ente", "css": "p"}]
        with patch("app.backend.scraper.Fetcher") as MockF:
            MockF.get.return_value = page
            rows = scrape_with_selectors("https://example.com", selectors, job_id="t4")

        assert len(rows) > 0
        assert rows[0]["campo"] == "Ente"
        assert rows[0]["fonte"] == "https://example.com"

    def test_fetcher_uses_cacert_path(self):
        page = _make_page(["OK"])
        schema = {"row_container": None, "fields": []}
        mock_store = MagicMock()
        with (
            patch("app.backend.scraper.Fetcher") as MockF,
            patch("app.backend.scraper.extract_selectors_from_html", return_value=schema),
            patch("app.backend.embeddings.get_store", return_value=mock_store),
        ):
            MockF.get.return_value = page
            smart_site_scrape("https://example.com", "test", job_id="t5")

        _, kwargs = MockF.get.call_args
        assert "verify" in kwargs  # cacert is passed

    def test_stealthy_timeout_is_milliseconds(self):
        import app.backend.config as cfg
        page = _make_page(["OK"])
        schema = {"row_container": None, "fields": []}
        mock_store = MagicMock()
        with (
            patch("app.backend.scraper.Fetcher") as MockF,
            patch("app.backend.scraper.StealthyFetcher") as MockS,
            patch("app.backend.scraper.extract_selectors_from_html", return_value=schema),
            patch("app.backend.scraper.time") as mock_time,
            patch("app.backend.embeddings.get_store", return_value=mock_store),
        ):
            mock_time.monotonic.return_value = 0.0
            mock_time.sleep = MagicMock()
            MockF.get.side_effect = Exception("fail")
            MockS.fetch.return_value = page
            smart_site_scrape("https://example.com", "test", job_id="t6")

        _, kwargs = MockS.fetch.call_args
        assert kwargs.get("timeout") == cfg.BROWSER_TIMEOUT
        assert cfg.BROWSER_TIMEOUT >= 1000, "timeout must be milliseconds"


# ── Embedding store unit tests ────────────────────────────────────────────────

class TestEmbeddingStore:
    def test_add_and_count(self, tmp_path):
        from app.backend.embeddings import EmbeddingStore
        store = EmbeddingStore(db_path=tmp_path / "test.db")
        store.add_record(
            {"titolo": "Gara appalto", "fonte": "https://ex.com"},
            compute_embedding=False,
        )
        assert store.count() == 1

    def test_add_multiple_records(self, tmp_path):
        from app.backend.embeddings import EmbeddingStore
        store = EmbeddingStore(db_path=tmp_path / "test.db")
        records = [
            {"titolo": f"Bando {i}", "fonte": "https://ex.com"} for i in range(5)
        ]
        count = store.add_records(records, compute_embeddings=False)
        assert count == 5
        assert store.count() == 5

    def test_keyword_search_finds_record(self, tmp_path):
        from app.backend.embeddings import EmbeddingStore
        store = EmbeddingStore(db_path=tmp_path / "test.db")
        store.add_record(
            {"titolo": "Gara consulenza informatica Lombardia", "fonte": "https://ex.com"},
            compute_embedding=False,
        )
        store.add_record(
            {"titolo": "Bando costruzione ponte", "fonte": "https://ex.com"},
            compute_embedding=False,
        )
        # Force keyword search by making get_embedding return None (no API call in tests)
        with patch("app.backend.embeddings.get_embedding", return_value=None):
            results = store.semantic_search("informatica Lombardia", top_k=5)
        assert len(results) > 0
        assert results[0]["score"] > 0
        # The IT record should rank first
        assert "informatica" in results[0]["text"].lower() or "consulenza" in results[0]["text"].lower()

    def test_clear(self, tmp_path):
        from app.backend.embeddings import EmbeddingStore
        store = EmbeddingStore(db_path=tmp_path / "test.db")
        store.add_record({"titolo": "x", "fonte": "y"}, compute_embedding=False)
        assert store.count() == 1
        store.clear()
        assert store.count() == 0

    def test_cosine_search_with_mocked_embeddings(self, tmp_path):
        from app.backend.embeddings import EmbeddingStore
        import json

        store = EmbeddingStore(db_path=tmp_path / "test.db")

        # Insert two records with fake embeddings
        import sqlite3
        import math
        vec_a = [1.0, 0.0, 0.0]  # cosine sim with [0.9, 0.1, 0] ≈ 0.9
        vec_b = [0.0, 1.0, 0.0]  # cosine sim with [0.9, 0.1, 0] ≈ 0.1
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "INSERT INTO records (text, embedding, source_url) VALUES (?, ?, ?)",
                ("Gara IT", json.dumps(vec_a), "https://a.com"),
            )
            conn.execute(
                "INSERT INTO records (text, embedding, source_url) VALUES (?, ?, ?)",
                ("Gara costruzione", json.dumps(vec_b), "https://b.com"),
            )

        query_vec = [0.9, 0.1, 0.0]
        with patch("app.backend.embeddings.get_embedding", return_value=query_vec):
            results = store._cosine_search("IT", top_k=2)

        assert len(results) == 2
        # Record A should rank higher (closer to query vector)
        assert results[0]["source_url"] == "https://a.com"
        assert results[0]["score"] > results[1]["score"]

    def test_get_embedding_disabled_without_key(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        from app.backend.embeddings import get_embedding
        result = get_embedding("test text")
        assert result is None


# ── FastAPI endpoint tests ────────────────────────────────────────────────────

# Patch heavy deps before importing app
with (
    patch("scrapling.fetchers.Fetcher") as _MockFetcher,
    patch("scrapling.fetchers.StealthyFetcher") as _MockStealthy,
):
    _MockFetcher.configure = MagicMock()
    _MockStealthy.configure = MagicMock()
    from fastapi.testclient import TestClient
    from app.backend.main import app

client = TestClient(app)


class TestHealthEndpoint:
    def test_health_ok(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "config" in data
        cfg = data["config"]
        assert "cacert" in cfg
        assert "search_model" in cfg
        assert "embed_model" in cfg
        assert cfg["browser_timeout_ms"] >= 1000

    def test_health_has_max_sites(self):
        resp = client.get("/health")
        assert resp.json()["config"]["max_sites"] >= 1


class TestJobsEndpoint:
    def test_jobs_empty(self):
        resp = client.get("/jobs")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_jobs_have_righe_totali(self):
        for job in client.get("/jobs").json():
            assert "righe_totali" in job


class TestSmartScrape:
    def test_web_search_mode_no_sites_raises_400(self):
        with patch("app.backend.main.smart_web_search", return_value=([], [])):
            resp = client.post("/smart-scrape", json={"prompt": "appalti"})
        assert resp.status_code == 400

    def test_web_search_mode_success(self):
        records = [{"titolo": "Bando A", "fonte": "https://a.com"}]
        with (
            patch("app.backend.main.smart_web_search", return_value=(["https://a.com"], records)),
            patch("app.backend.main.llm_summarize", return_value="Riassunto."),
        ):
            resp = client.post("/smart-scrape", json={"prompt": "appalti Lombardia"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["mode"] == "web_search"
        assert data["righe_totali"] == 1
        assert len(data["dati"]) == 1

    def test_site_scrape_mode_explicit(self):
        records = [{"titolo": "Gara IT", "fonte": "https://ex.com"}]
        with (
            patch("app.backend.main.smart_site_scrape", return_value=records),
            patch("app.backend.main.llm_summarize", return_value="OK"),
        ):
            resp = client.post(
                "/smart-scrape",
                json={"prompt": "gare IT", "url": "https://ex.com", "mode": "site_scrape"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "site_scrape"
        assert data["siti_visitati"] == ["https://ex.com"]

    def test_auto_mode_with_url_uses_site_scrape(self):
        with (
            patch("app.backend.main.smart_site_scrape", return_value=[]) as mock_sss,
            patch("app.backend.main.llm_summarize", return_value=""),
        ):
            client.post(
                "/smart-scrape",
                json={"prompt": "analisi", "url": "https://ex.com"},
            )
        mock_sss.assert_called_once()

    def test_auto_mode_without_url_uses_web_search(self):
        with (
            patch("app.backend.main.smart_web_search", return_value=(["https://a.com"], [])) as mock_ws,
            patch("app.backend.main.llm_summarize", return_value=""),
        ):
            client.post("/smart-scrape", json={"prompt": "appalti"})
        mock_ws.assert_called_once()

    def test_site_scrape_requires_url(self):
        resp = client.post(
            "/smart-scrape",
            json={"prompt": "gare", "mode": "site_scrape"},
        )
        assert resp.status_code == 400

    def test_response_includes_job_id_and_mode(self):
        with (
            patch("app.backend.main.smart_web_search", return_value=(["https://a.com"], [{"titolo": "x", "fonte": "https://a.com"}])),
            patch("app.backend.main.llm_summarize", return_value="ok"),
        ):
            resp = client.post("/smart-scrape", json={"prompt": "test"})
        data = resp.json()
        assert "job_id" in data
        assert "mode" in data

    def test_site_scrape_with_structured_records(self):
        """Verify the response contains a list of record dicts (not raw key-value pairs)."""
        records = [
            {
                "titolo": "Gara per servizi informatici",
                "cig": "ABC123456",
                "importo": "€ 120.000",
                "scadenza": "2026-06-30",
                "stazione_appaltante": "Regione Lombardia",
                "link": "https://ex.com/gara/1",
                "fonte": "https://ex.com",
            }
        ]
        with (
            patch("app.backend.main.smart_site_scrape", return_value=records),
            patch("app.backend.main.llm_summarize", return_value="Trovata 1 gara."),
        ):
            resp = client.post(
                "/smart-scrape",
                json={"prompt": "gare IT sopra 40k", "url": "https://ex.com"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["righe_totali"] == 1
        first = data["dati"][0]
        assert first["titolo"] == "Gara per servizi informatici"
        assert first["cig"] == "ABC123456"
        assert first["stazione_appaltante"] == "Regione Lombardia"


class TestScrapeDirect:
    def test_scrape_direct_calls_legacy_scraper(self):
        rows = [{"campo": "Ente", "valore": "Comune di Milano", "fonte": "https://ex.com"}]
        with patch("app.backend.main.scrape_with_selectors", return_value=rows) as mock_sel:
            resp = client.post(
                "/scrape-direct",
                json={"url": "https://ex.com", "selectors": [{"name": "Ente", "css": "h1"}]},
            )
        assert resp.status_code == 200
        mock_sel.assert_called_once()
        assert resp.json()["righe_totali"] == 1

    def test_report_not_found(self):
        assert client.get("/report/nonexistent").status_code == 404


class TestSemanticSearch:
    def test_search_returns_structure(self):
        mock_store = MagicMock()
        mock_store.semantic_search.return_value = [
            {"id": 1, "score": 0.92, "text": "Gara IT Lombardia", "source_url": "https://a.com", "record": {}}
        ]
        mock_store.count.return_value = 5
        mock_store.count_with_embeddings.return_value = 3

        with patch("app.backend.main.get_store", return_value=mock_store):
            resp = client.get("/search", params={"q": "consulenza IT", "top_k": 5})

        assert resp.status_code == 200
        data = resp.json()
        assert "query" in data
        assert "risultati" in data
        assert "total_in_store" in data
        assert len(data["risultati"]) == 1
        assert data["risultati"][0]["score"] == 0.92

    def test_search_requires_q_param(self):
        resp = client.get("/search")
        assert resp.status_code == 422  # FastAPI validation error
