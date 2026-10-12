"""Exercise keyless Parallel search over its real JSON-RPC wire and the relay.

The adapter speaks plain Streamable HTTP (one `tools/call` POST), so these tests
drive it through a mock transport rather than any MCP client library.
"""

import json

from unittest.mock import AsyncMock

import httpx
import pytest

from fastapi import FastAPI

from topix.agents.websearch import parallel
from topix.agents.websearch.parallel import UpstreamSearchError, search_parallel
from topix.api.router.ai import optional_user_uid, router

SOURCE = {
    "url": "https://docs.python.org/3/library/asyncio.html",
    "title": "asyncio",
    "excerpts": ["Async I/O", "Coroutines"],
}


def _envelope(payload, *, is_error=False, structured=True):
    """Wrap a tool payload in the JSON-RPC result envelope Parallel returns."""
    result = {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": is_error}
    if structured:
        result["structuredContent"] = payload
    return {"jsonrpc": "2.0", "id": "fixture", "result": result}


@pytest.fixture
def mcp_endpoint(monkeypatch):
    """Serve JSON-RPC replies over a mock transport, recording every request."""
    requests = []
    state = {
        "payload": {"results": [SOURCE]},
        "is_error": False,
        "structured": True,
        "status": 200,
        "timeout": False,
        "sse": False,
        "rpc_error": None,
    }

    def handle(request):
        """Answer one `tools/call`, or the configured fault."""
        requests.append(request)
        assert str(request.url) == parallel.MCP_URL
        assert request.headers["user-agent"] == parallel.USER_AGENT
        for name in ("authorization", "cookie", "x-api-key", "x-provider-key"):
            assert name not in request.headers
        if state["timeout"]:
            raise httpx.ReadTimeout("fixture timeout", request=request)
        if state["status"] != 200:
            return httpx.Response(state["status"], headers={"Retry-After": "60"}, text="rate limited")
        if state["rpc_error"]:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": "fixture", "error": {"message": state["rpc_error"]}})
        body = _envelope(state["payload"], is_error=state["is_error"], structured=state["structured"])
        if state["sse"]:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream", "set-cookie": "server=cookie; Path=/"},
                text=f"event: message\ndata: {json.dumps(body)}\n\n",
            )
        return httpx.Response(200, json=body, headers={"set-cookie": "server=cookie; Path=/"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        headers=parallel._HEADERS,
        event_hooks={"request": [parallel._anonymous_request]},
    )
    monkeypatch.setattr(parallel, "get_client", lambda: client)
    for key in ("PARALLEL_API_KEY", "TAVILY_API_KEY", "PERPLEXITY_API_KEY", "LINKUP_API_KEY", "EXA_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    yield state, requests, client


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [True, False])
async def test_one_round_trip_returns_mapped_sources(mcp_endpoint, structured):
    """One POST is the whole exchange — no initialize, no session teardown."""
    state, requests, _ = mcp_endpoint
    state.update(structured=structured, payload={"results": [SOURCE, SOURCE]})

    output = await search_parallel("Python asyncio documentation", max_results=1)

    assert len(requests) == 1
    assert requests[0].method == "POST"
    call = json.loads(requests[0].content)
    assert call["method"] == "tools/call"
    assert call["params"] == {
        "name": "web_search",
        "arguments": {
            "objective": "Python asyncio documentation",
            "search_queries": ["Python asyncio documentation"],
        },
    }
    # Anonymous: a throwaway request id, and no session_id tying calls together.
    assert "session_id" not in call["params"]["arguments"]
    assert len(output.search_results) == 1
    assert output.search_results[0].url == SOURCE["url"]
    assert output.search_results[0].content == "Async I/O\n\nCoroutines"
    assert output.search_results[0].source_domain == "docs.python.org"


@pytest.mark.asyncio
async def test_sse_framing_is_accepted(mcp_endpoint):
    """Streamable HTTP may answer as an event stream; read that framing too."""
    state, _, _ = mcp_endpoint
    state["sse"] = True
    output = await search_parallel("q")
    assert output.search_results[0].url == SOURCE["url"]


@pytest.mark.asyncio
async def test_a_text_less_source_does_not_lose_the_others(mcp_endpoint):
    """A null/missing `excerpts` defaults to empty instead of failing the payload."""
    state, _, _ = mcp_endpoint
    state["payload"] = {
        "results": [
            {"url": "https://a.com/scan.pdf", "title": "Scan", "excerpts": None},
            {"url": "https://b.com/page", "title": "No field"},
            SOURCE,
        ]
    }
    output = await search_parallel("q")
    assert [r.url for r in output.search_results] == ["https://a.com/scan.pdf", "https://b.com/page", SOURCE["url"]]
    assert output.search_results[0].content == ""


@pytest.mark.asyncio
async def test_empty_results_are_not_an_error(mcp_endpoint):
    """A search that legitimately found nothing returns no sources."""
    state, _, _ = mcp_endpoint
    state["payload"] = {"results": []}
    assert not (await search_parallel("q")).search_results


@pytest.mark.asyncio
async def test_a_credential_never_reaches_the_shared_client(mcp_endpoint):
    """A caller-supplied header is stripped before the anonymous request goes out."""
    _, requests, client = mcp_endpoint
    client.headers["Authorization"] = "Bearer saved-credential"
    await search_parallel("q")
    assert "authorization" not in requests[-1].headers
    client.headers.pop("Authorization")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario, expected",
    [
        ("tool_error", UpstreamSearchError),
        ("rpc_error", UpstreamSearchError),
        ("malformed", Exception),
        ("rate_limit", httpx.HTTPStatusError),
        ("timeout", httpx.ReadTimeout),
    ],
)
async def test_failures_never_look_like_an_empty_result(mcp_endpoint, scenario, expected):
    """Never turn an upstream or malformed response into successful empty output."""
    state, _, _ = mcp_endpoint
    if scenario == "tool_error":
        state.update(is_error=True, structured=False, payload={"error": "search failed"})
    elif scenario == "rpc_error":
        state["rpc_error"] = "bad request"
    elif scenario == "malformed":
        state["payload"] = {"results": [{"title": "no url"}]}
    elif scenario == "timeout":
        state["timeout"] = True
    else:
        state["status"] = 429

    with pytest.raises(expected):
        await search_parallel("q")


def _app(uid: str | None = None) -> FastAPI:
    """Build a bare app with the AI router and a permissive quota store.

    `uid` stands in for a validated bearer token; None leaves the caller signed
    out, as a self-hosted relay call is.
    """
    app = FastAPI()
    app.include_router(router)
    redis = AsyncMock()
    redis.check_fixed_window_quota.return_value = (True, 1)
    app.redis_store = redis

    async def _uid() -> str | None:
        return uid

    app.dependency_overrides[optional_user_uid] = _uid
    return app


def _window_keys(app: FastAPI) -> list[str]:
    """Return the key each keyless-relay window check was counted against."""
    return [call.args[0] for call in app.redis_store.check_fixed_window_quota.await_args_list]


@pytest.mark.asyncio
async def test_relay_runs_keyless_without_sign_in_when_self_hosted(mcp_endpoint, monkeypatch):
    """Self-hosted (billing off): the fallback relay stays open, metered by IP."""
    _, requests, _ = mcp_endpoint
    monkeypatch.setattr("topix.api.router.ai.is_billing_active", lambda: False)
    app = _app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://dim0") as caller:
        response = await caller.post(
            "/ai/search",
            json={"query": "Python asyncio documentation", "engine": "parallel"},
            headers={"X-Run-Id": "fixture-run", "X-Provider-Key": "must-not-reach-parallel"},
        )
    assert response.status_code == 200
    assert response.json()["data"]["results"][0]["content"] == "Async I/O\n\nCoroutines"
    app.redis_store.check_fixed_window_quota.assert_awaited_once()
    assert app.redis_store.check_fixed_window_quota.call_args.kwargs["scope"] == "keyless_search"
    # Keyless burns no key of ours, so nothing is charged against the run quota.
    app.redis_store.set_if_absent.assert_not_awaited()
    # The internal run id is never forwarded as an upstream session handle.
    assert "fixture-run" not in requests[-1].content.decode()


@pytest.mark.asyncio
async def test_hosted_relay_requires_a_signed_in_user(mcp_endpoint, monkeypatch):
    """Hosted (billing active): the keyless relay is not an open proxy."""
    _, requests, _ = mcp_endpoint
    monkeypatch.setattr("topix.api.router.ai.is_billing_active", lambda: True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(_app()), base_url="http://dim0") as caller:
        response = await caller.post("/ai/search", json={"query": "q", "engine": "parallel"})
    assert response.status_code == 401
    assert not requests


@pytest.mark.asyncio
async def test_keyed_engines_keep_their_existing_metering(mcp_endpoint):
    """The default engine still needs auth — the keyless branch changes nothing."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(_app()), base_url="http://dim0") as caller:
        response = await caller.post("/ai/search", json={"query": "same default"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_relay_windows_a_signed_in_caller_on_the_user_id(mcp_endpoint, monkeypatch):
    """`X-Forwarded-For` is caller-supplied: rotating it must not reset the window."""
    monkeypatch.setattr("topix.api.router.ai.is_billing_active", lambda: True)
    app = _app(uid="user-1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://dim0") as caller:
        for hop in ("203.0.113.1", "198.51.100.7"):
            response = await caller.post(
                "/ai/search",
                json={"query": "q", "engine": "parallel"},
                headers={"X-Forwarded-For": hop},
            )
            assert response.status_code == 200
    assert _window_keys(app) == ["user:user-1", "user:user-1"]


@pytest.mark.asyncio
async def test_relay_windows_a_signed_out_caller_on_the_ip(mcp_endpoint, monkeypatch):
    """Signed out on a self-host there is no uid, so the first forwarded hop is it."""
    monkeypatch.setattr("topix.api.router.ai.is_billing_active", lambda: False)
    app = _app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://dim0") as caller:
        response = await caller.post(
            "/ai/search",
            json={"query": "q", "engine": "parallel"},
            headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"},
        )
    assert response.status_code == 200
    assert _window_keys(app) == ["ip:203.0.113.9"]


@pytest.mark.asyncio
async def test_relay_rate_limits_by_ip(mcp_endpoint, monkeypatch):
    """A self-hosted relay over its per-IP window answers 429 before calling out."""
    _, requests, _ = mcp_endpoint
    monkeypatch.setattr("topix.api.router.ai.is_billing_active", lambda: False)
    app = _app()
    app.redis_store.check_fixed_window_quota.return_value = (False, 120)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://dim0") as caller:
        response = await caller.post("/ai/search", json={"query": "q", "engine": "parallel"})
    assert response.status_code == 429
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario, code",
    [("rate_limit", 429), ("timeout", 504), ("tool_error", 502), ("malformed", 502)],
)
async def test_relay_maps_upstream_failures_to_a_generic_status(mcp_endpoint, monkeypatch, scenario, code):
    """Upstream trouble becomes an actionable status, never a 500 with raw details."""
    state, _, _ = mcp_endpoint
    monkeypatch.setattr("topix.api.router.ai.is_billing_active", lambda: False)
    if scenario == "rate_limit":
        state["status"] = 429
    elif scenario == "timeout":
        state["timeout"] = True
    elif scenario == "tool_error":
        state.update(is_error=True, structured=False, payload={"error": "upstream detail"})
    else:
        state["payload"] = {"results": [{"title": "no url"}]}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(_app()), base_url="http://dim0") as caller:
        response = await caller.post("/ai/search", json={"query": "q", "engine": "parallel"})

    assert response.status_code == code
    if code == 429:
        assert response.headers["Retry-After"] == "60"
    message = response.json()["data"]["message"]
    assert "parallel" in message
    assert "upstream detail" not in message
