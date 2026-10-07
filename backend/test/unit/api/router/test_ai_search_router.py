"""Unit tests for the managed web-search proxy (`POST /ai/search`).

Monkeypatches the per-engine search functions so the endpoint's dispatch +
result mapping is exercised without hitting a real provider.
"""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import topix.api.router.ai as ai_module

from topix.api.router.ai import meter_search, router
from topix.api.utils.security import get_current_user_uid


async def _no_meter() -> None:
    return None


def _client() -> TestClient:
    """Isolate search dispatch from the endpoint-specific quota dependency."""
    app = FastAPI()
    app.include_router(router)

    async def _uid():
        return "user-1"

    app.dependency_overrides[get_current_user_uid] = _uid
    app.dependency_overrides[meter_search] = _no_meter
    return TestClient(app)


def _fake_output():
    return SimpleNamespace(
        answer="cats are great",
        search_results=[
            SimpleNamespace(url="https://a.com", title="A", content="body", source_domain="a.com"),
        ],
    )


def test_search_dispatches_to_the_engine_and_maps_results(monkeypatch):
    """A known engine runs and its results map to {answer, results}."""
    calls = {}

    async def _fake(query, max_results=10, api_key=None):
        calls["query"] = query
        calls["max_results"] = max_results
        calls["api_key"] = api_key
        return _fake_output()

    monkeypatch.setitem(ai_module._SEARCH_FNS, "perplexity", _fake)

    res = _client().post("/ai/search", json={"query": "cats", "engine": "perplexity"})
    assert res.status_code == 200
    data = res.json()["data"]
    assert data["answer"] == "cats are great"
    assert data["results"][0]["url"] == "https://a.com"
    assert data["results"][0]["title"] == "A"
    # managed call: no BYOK key forwarded
    assert calls == {"query": "cats", "max_results": 10, "api_key": None}


def test_search_defaults_to_perplexity(monkeypatch):
    """Omitting the engine uses perplexity."""
    async def _fake(query, max_results=10, api_key=None):
        return _fake_output()

    monkeypatch.setitem(ai_module._SEARCH_FNS, "perplexity", _fake)
    res = _client().post("/ai/search", json={"query": "cats"})
    assert res.status_code == 200


def test_search_relays_the_byok_provider_key(monkeypatch):
    """An `X-Provider-Key` header is forwarded to the provider fn (BYOK relay)."""
    calls = {}

    async def _fake(query, max_results=10, api_key=None):
        calls["api_key"] = api_key
        return _fake_output()

    monkeypatch.setitem(ai_module._SEARCH_FNS, "perplexity", _fake)
    res = _client().post(
        "/ai/search", json={"query": "cats"}, headers={"X-Provider-Key": "pplx-user"}
    )
    assert res.status_code == 200
    assert calls["api_key"] == "pplx-user"


def test_unknown_engine_is_400(monkeypatch):
    """An unsupported engine is rejected before any provider call."""
    res = _client().post("/ai/search", json={"query": "cats", "engine": "bing"})
    assert res.status_code == 400
