"""Keyless Parallel Search MCP adapter for Dim0's native search contract."""

import json

from datetime import timedelta
from urllib.parse import urlsplit

import httpx

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel

from topix.agents.datatypes.annotations import SearchResult
from topix.agents.datatypes.outputs import WebSearchOutput
from topix.agents.datatypes.web_search import WebSearchContextSize
from topix.agents.websearch.utils import get_from_date
from topix.datatypes.recurrence import Recurrence

MCP_URL = "https://search.parallel.ai/mcp"
USER_AGENT = "Dim0 (https://github.com/vcmf/dim0)"


class _ParallelResult(BaseModel):
    """Validate the fields Dim0 consumes from a Parallel source."""

    url: str
    title: str | None = None
    publish_date: str | None = None
    excerpts: list[str]


class _ParallelOutput(BaseModel):
    """Validate source output, including legitimate empty search results."""

    results: list[_ParallelResult]


def _transport_error(error: ExceptionGroup) -> Exception:
    """Expose HTTP failures nested by the MCP transport's task groups."""
    leaves = []
    pending = list(error.exceptions)
    while pending:
        child = pending.pop(0)
        if isinstance(child, ExceptionGroup):
            pending.extend(child.exceptions)
        else:
            leaves.append(child)
    return next((child for child in leaves if isinstance(child, httpx.HTTPError)), leaves[0])


async def _anonymous_request(request: httpx.Request) -> None:
    """Keep every MCP request keyless, including after server-set cookies."""
    for header in ("Authorization", "Cookie", "X-Api-Key", "X-Provider-Key"):
        request.headers.pop(header, None)
    request.headers["User-Agent"] = USER_AGENT


async def search_parallel(
    query: str,
    max_results: int = 10,
    search_context_size: WebSearchContextSize = WebSearchContextSize.MEDIUM,
    recency: Recurrence | None = None,
    *,
    timeout: httpx.Timeout | None = None,
    session_id: str | None = None,
) -> WebSearchOutput:
    """Search anonymously over Streamable HTTP, retaining source URLs and excerpts.

    Each call owns its MCP session and HTTP client so saved credentials cannot
    enter the anonymous connection. Recency is objective guidance, not a filter.
    """
    if not query.strip():
        raise ValueError("A search query is required")
    if max_results < 1:
        raise ValueError("max_results must be positive")
    objective = query
    if recency:
        objective += f". Prefer sources published since {get_from_date(recency)}."
    arguments = {"objective": objective, "search_queries": [query]}
    if session_id:
        arguments["session_id"] = session_id
    # A dedicated client has no auth, cookies, API keys or shared-client defaults.
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT}, timeout=timeout or httpx.Timeout(30),
            event_hooks={"request": [_anonymous_request]},
        ) as client:
            async with streamable_http_client(MCP_URL, http_client=client) as (read, write, _):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=60)) as session:
                    await session.initialize()
                    result = await session.call_tool("web_search", arguments)
                    if result.isError:
                        message = "\n".join(block.text for block in result.content if block.type == "text")
                        raise RuntimeError(f"Parallel search failed: {message}")
                    payload = result.structuredContent
                    if payload is None:
                        text = "\n".join(block.text for block in result.content if block.type == "text")
                        payload = json.loads(text)
                    parsed = _ParallelOutput.model_validate(payload)
    except ExceptionGroup as error:
        raise _transport_error(error) from error
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
