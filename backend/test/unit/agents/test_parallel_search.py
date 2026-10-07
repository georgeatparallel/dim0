"""Exercise native Parallel search through the real MCP transport and dispatch."""

import asyncio
import json

from unittest.mock import AsyncMock

import httpx
import pytest

from agents.tool_context import ToolContext
from fastapi import FastAPI

from topix.agents.config import WebSearchConfig
from topix.agents.datatypes.context import Context
from topix.agents.websearch import parallel
from topix.agents.websearch.handler import WebSearchHandler
from topix.api.router.ai import router
from topix.config import catalog
from topix.config.services import service_config

HTTP_CLIENT = httpx.AsyncClient
SOURCE = {"url": "https://docs.python.org/3/library/asyncio.html", "title": "asyncio", "excerpts": ["Async I/O", "Coroutines"]}


@pytest.fixture
def mcp_server(monkeypatch):  # noqa: C901 - protocol fixture covers lifecycle and fault frames
    """Serve real MCP frames while recording requests and owned client cleanup."""
    requests = []
    clients = []
    state = {"output": {"results": [SOURCE]}, "structured": True, "error": False, "status": 200, "wait": False, "timeout": False}
    started = asyncio.Event()

    async def handle(request):  # noqa: C901 - independent MCP wire cases
        """Return initialization and tool results in MCP's JSON wire envelope."""
        requests.append(request)
        assert str(request.url) == parallel.MCP_URL
        assert request.headers["user-agent"] == parallel.USER_AGENT
        for name in ("authorization", "cookie", "x-api-key", "x-provider-key"):
            assert name not in request.headers
        if request.method == "GET":
            return httpx.Response(405)
        if request.method == "DELETE":
            return httpx.Response(200)
        message = json.loads(request.content)
        if "id" not in message:
            return httpx.Response(202)
        if message["method"] == "initialize":
            result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": "1"}}
        elif message["method"] == "tools/list":
            result = {"tools": [{"name": "web_search", "inputSchema": {"type": "object"}}]}
        else:
            assert message["method"] == "tools/call"
            assert message["params"]["name"] == "web_search"
            started.set()
            if state["wait"]:
                await asyncio.Event().wait()
            if state["timeout"]:
                raise httpx.ReadTimeout("fixture timeout", request=request)
            if state["status"] != 200:
                return httpx.Response(state["status"], headers={"Retry-After": "60"}, text="rate limited")
            result = {"content": [{"type": "text", "text": json.dumps(state["output"])}], "isError": state["error"]}
            if state["structured"]:
                result["structuredContent"] = state["output"]
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": message["id"], "result": result},
            headers={"mcp-session-id": "fixture-session", "set-cookie": "server=cookie; Path=/"},
        )

    def make_client(**kwargs):
        """Inject the wire fixture without bypassing the SDK or request headers."""
        client = HTTP_CLIENT(transport=httpx.MockTransport(handle), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(parallel.httpx, "AsyncClient", make_client)
    for key in ("PARALLEL_API_KEY", "TAVILY_API_KEY", "PERPLEXITY_API_KEY", "LINKUP_API_KEY", "EXA_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return state, requests, clients, started


@pytest.mark.asyncio
async def test_configured_native_tool(mcp_server, monkeypatch):
    """Load explicit Parallel with no search keys and invoke the SDK FunctionTool."""
    state, requests, clients, _ = mcp_server
    monkeypatch.setattr(service_config, "search", [])
    # Inference is separate: the model catalog is a deterministic fixture here.
    monkeypatch.setattr(catalog, "normalize_code", lambda model: model)
    config = WebSearchConfig(
        model="fixture-model", instructions_template="web_search.jinja",
        search_engine="parallel", search_context_size="medium", max_results=1,
    )
    borrowed = HTTP_CLIENT(headers={"Authorization": "Bearer saved-credential", "Cookie": "saved=cookie"})
    tool = WebSearchHandler.from_config(config, client=borrowed)
    context = Context()
    arguments = json.dumps({"query": "Python asyncio documentation"})
    output = await tool.on_invoke_tool(ToolContext(context, tool_name="web_search", tool_call_id="fixture-tool", tool_arguments=arguments), arguments)
    assert not borrowed.is_closed
    await borrowed.aclose()
    assert output.search_results[0].url == SOURCE["url"]
    assert output.search_results[0].content == "Async I/O\n\nCoroutines"
    assert output.search_results[0].source_domain == "docs.python.org"
    assert context.tool_calls[-1].output == output
    call = next(json.loads(r.content) for r in requests if r.method == "POST" and json.loads(r.content).get("method") == "tools/call")
    assert call["params"]["arguments"] == {"objective": "Python asyncio documentation", "search_queries": ["Python asyncio documentation"]}
    assert all(c.is_closed for c in clients)
    assert requests[-1].method == "DELETE"


@pytest.mark.asyncio
async def test_anonymous_api_route_and_existing_default(mcp_server):
    """Reach /ai/search without sign-in, meter by IP and keep the old default."""
    _, requests, clients, _ = mcp_server
    app = FastAPI()
    app.include_router(router)
    redis = AsyncMock()
    redis.check_fixed_window_quota.return_value = (True, 1)
    app.redis_store = redis
    async with HTTP_CLIENT(transport=httpx.ASGITransport(app), base_url="http://dim0") as caller:
        response = await caller.post("/ai/search", json={"query": "Python asyncio documentation", "engine": "parallel"},
                                     headers={"X-Run-Id": "fixture-run", "X-Provider-Key": "must-not-reach-mcp"})
        assert response.status_code == 200
        assert response.json()["data"]["results"][0]["content"] == "Async I/O\n\nCoroutines"
        redis.check_fixed_window_quota.assert_awaited_once()
        assert redis.check_fixed_window_quota.call_args.kwargs["scope"] == "keyless_search"
        redis.set_if_absent.assert_not_awaited()
        call = next(json.loads(r.content) for r in requests if r.method == "POST" and json.loads(r.content).get("method") == "tools/call")
        assert call["params"]["arguments"]["session_id"] == "fixture-run"
        default = await caller.post("/ai/search", json={"query": "same default"})
        assert default.status_code == 401
        redis.check_fixed_window_quota.return_value = (False, 120)
        limited = await caller.post("/ai/search", json={"query": "q", "engine": "parallel"})
        assert limited.status_code == 429
    assert all(c.is_closed for c in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario, code", [("rate_limit", 429), ("timeout", 504)])
async def test_api_transport_errors(mcp_server, scenario, code):
    """Preserve useful rate-limit and timeout status through the actual route."""
    state, _, clients, _ = mcp_server
    state.update(status=429 if scenario == "rate_limit" else 200, timeout=scenario == "timeout")
    app = FastAPI()
    app.include_router(router)
    async with HTTP_CLIENT(transport=httpx.ASGITransport(app), base_url="http://dim0") as caller:
        response = await caller.post("/ai/search", json={"query": "q", "engine": "parallel"})
        assert response.status_code == code
        if code == 429:
            assert response.headers["Retry-After"] == "60"
        assert "Parallel search" in response.json()["data"]["message"]
    assert all(c.is_closed for c in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("structured", [True, False])
async def test_text_and_structured_results(mcp_server, structured):
    """Accept both supported MCP result envelopes and bound returned sources."""
    state, _, clients, _ = mcp_server
    state.update(structured=structured, output={"results": [SOURCE, SOURCE]})
    output = await parallel.search_parallel("q", max_results=1)
    assert len(output.search_results) == 1
    assert all(c.is_closed for c in clients)
    state["output"] = {"results": []}
    assert not (await parallel.search_parallel("q")).search_results


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["tool_error", "malformed", "rate_limit", "timeout"])
async def test_errors_close_session(mcp_server, scenario):
    """Never turn an upstream or malformed response into successful empty output."""
    state, requests, clients, _ = mcp_server
    if scenario == "tool_error":
        state.update(error=True, output={"error": "search failed"})
    elif scenario == "malformed":
        state["output"] = {"unexpected": []}
    elif scenario == "timeout":
        state["timeout"] = True
    else:
        state["status"] = 429
    expected = httpx.HTTPStatusError if scenario == "rate_limit" else httpx.ReadTimeout if scenario == "timeout" else Exception
    with pytest.raises(expected):
        await parallel.search_parallel("q")
    assert all(c.is_closed for c in clients)
    assert requests[-1].method == "DELETE"
    calls = [r for r in requests if r.method == "POST" and json.loads(r.content).get("method") == "tools/call"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancel_closes_client(mcp_server):
    """Cancellation propagates while the owned HTTP client is closed."""
    state, requests, clients, started = mcp_server
    state["wait"] = True
    task = asyncio.create_task(parallel.search_parallel("q"))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(c.is_closed for c in clients)
    assert requests[-1].method == "DELETE"

