"""Keyless Parallel Search adapter — a plain JSON-RPC call to its MCP endpoint.

Streamable HTTP is JSON-RPC over POST, so one `tools/call` request is the whole
exchange: no `initialize`, no `notifications/initialized`, no session DELETE.
The browser/desktop agent calls Parallel directly; this module backs the
`/ai/search` relay kept as a fallback (and any server-side caller).
"""

import json

from typing import Any, Optional
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from pydantic import BaseModel, Field, field_validator

from topix.agents.datatypes.annotations import SearchResult
from topix.agents.datatypes.outputs import WebSearchOutput
from topix.agents.datatypes.web_search import WebSearchContextSize
from topix.agents.websearch.utils import get_from_date
from topix.datatypes.recurrence import Recurrence

MCP_URL = "https://search.parallel.ai/mcp"
USER_AGENT = "Dim0 (https://github.com/vcmf/dim0)"
DEFAULT_TIMEOUT = httpx.Timeout(60)

# What a Streamable HTTP server expects, plus an explicit anonymous identity.
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-06-18",
    "User-Agent": USER_AGENT,
}

_client: httpx.AsyncClient | None = None


class UpstreamSearchError(RuntimeError):
    """An upstream search provider answered, but the answer is not usable."""


class _ParallelResult(BaseModel):
    """Validate the fields Dim0 consumes from a Parallel source.

    `excerpts` tolerates a missing or null field so one text-less source (a
    scanned PDF, say) does not invalidate every other result in the payload.
    """

    url: str
    title: str | None = None
    publish_date: str | None = None
    excerpts: list[str] = Field(default_factory=list)

    @field_validator("excerpts", mode="before")
    @classmethod
    def _default_excerpts(cls, value: Any) -> Any:
        """Treat an explicit `null` as "no excerpts" rather than a type error."""
        return [] if value is None else value


class _ParallelOutput(BaseModel):
    """Validate source output, including legitimate empty search results."""

    results: list[_ParallelResult] = Field(default_factory=list)


async def _anonymous_request(request: httpx.Request) -> None:
    """Keep every request keyless, including after a server tries to set cookies."""
    for header in ("Authorization", "Cookie", "X-Api-Key", "X-Provider-Key"):
        request.headers.pop(header, None)


def get_client() -> httpx.AsyncClient:
    """Return the shared anonymous HTTP client, creating it on first use.

    Reusing one client keeps the connection pool warm, so a search costs one
    round trip instead of a fresh TLS handshake. The request hook strips any
    credential header, so sharing it never leaks one into an anonymous call.
    """
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            headers=_HEADERS,
            timeout=DEFAULT_TIMEOUT,
            event_hooks={"request": [_anonymous_request]},
        )
    return _client


async def close_client() -> None:
    """Close the shared client (app shutdown, test teardown)."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _decode(response: httpx.Response) -> dict[str, Any]:
    """Read one JSON-RPC reply, accepting the SSE framing Streamable HTTP allows."""
    if "text/event-stream" not in response.headers.get("content-type", ""):
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    raise UpstreamSearchError("empty event stream")


def _tool_payload(body: dict[str, Any]) -> dict[str, Any]:
    """Unwrap a JSON-RPC reply into the tool's own JSON payload."""
    if body.get("error"):
        raise UpstreamSearchError(str(body["error"].get("message", "search failed")))
    result = body.get("result") or {}
    content = result.get("content") or []
    text = "\n".join(block.get("text", "") for block in content if block.get("type") == "text")
    if result.get("isError"):
        raise UpstreamSearchError(text or "search failed")
    payload = result.get("structuredContent")
    return json.loads(text) if payload is None else payload


async def search_parallel(
    query: str,
    max_results: int = 10,
    search_context_size: WebSearchContextSize = WebSearchContextSize.MEDIUM,
    recency: Recurrence | None = None,
    *,
    api_key: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    timeout: Optional[httpx.Timeout] = None,
) -> WebSearchOutput:
    """Search anonymously over Parallel's MCP endpoint, keeping URLs and excerpts.

    The signature matches the other `websearch` engines so the relay dispatches
    to it by name. `api_key` is accepted and ignored: this engine is keyless by
    definition and a relayed caller key must never reach it. Recency is guidance
    in the objective, not a hard filter, and excerpt size is the anonymous
    endpoint's own (so `search_context_size` does not override it).
    """
    objective = query
    if recency:
        objective += f". Prefer sources published since {get_from_date(recency)}."
    request = {
        "jsonrpc": "2.0",
        # Anonymous by construction: a throwaway id per call, never a run id.
        "id": uuid4().hex,
        "method": "tools/call",
        "params": {
            "name": "web_search",
            "arguments": {"objective": objective, "search_queries": [query]},
        },
    }
    http = client or get_client()
    response = await http.post(MCP_URL, json=request, headers=_HEADERS, timeout=timeout or DEFAULT_TIMEOUT)
    response.raise_for_status()
    parsed = _ParallelOutput.model_validate(_tool_payload(_decode(response)))
    return WebSearchOutput(search_results=[
        SearchResult(
            url=source.url,
            title=source.title,
            content="\n\n".join(source.excerpts),
            published_at=source.publish_date,
            source_domain=urlsplit(source.url).hostname,
        )
        for source in parsed.results[:max_results]
    ])
