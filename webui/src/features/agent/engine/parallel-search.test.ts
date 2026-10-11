import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { freshStore } from "@/test/canvas"
import type { AgentEvent, LlmClient, LlmMessage } from "./types"
import { toolTurn } from "@/test/llm"
import { runAgent } from "./agent-loop"
import { collectSourceUrls, makeWebSearchTool, resolveSearchClient } from "./web-search"
import { parallelSearchPost } from "./services/parallel-search"
import { resolveService } from "./services/resolve"
import { agentResolveContext } from "./services/context"
import { useByokStore } from "../byok/byok-store"


const relay = vi.hoisted(() => vi.fn())
vi.mock("@/api", () => ({ fetchWithAuthRaw: relay }))

const MCP_URL = "https://search.parallel.ai/mcp"
const SOURCE = {
  url: "https://docs.python.org/3/library/asyncio.html",
  title: "asyncio",
  excerpts: ["Async I/O", "Coroutines"],
}
/** What the direct path maps `SOURCE` to — the `/ai/search` result shape. */
const MAPPED = { url: SOURCE.url, title: "asyncio", content: "Async I/O\n\nCoroutines" }


/** One JSON-RPC reply from the MCP endpoint, in either supported framing. */
const mcpReply = (payload: unknown, opts: { sse?: boolean; isError?: boolean } = {}): Response => {
  const body = {
    jsonrpc: "2.0",
    id: "fixture",
    result: {
      content: [{ type: "text", text: JSON.stringify(payload) }],
      ...(opts.isError ? { isError: true } : { structuredContent: payload }),
    },
  }
  return opts.sse
    ? new Response(`event: message\ndata: ${JSON.stringify(body)}\n\n`, {
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
      })
    : new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
}


const direct = vi.fn()

beforeEach(() => {
  relay.mockReset()
  direct.mockReset()
  direct.mockResolvedValue(mcpReply({ results: [SOURCE] }))
  vi.stubGlobal("fetch", direct)
  localStorage.clear()
  useByokStore.setState({ search: {}, searchEngine: "perplexity", llm: {}, codeKey: "", parseKey: "" })
})

afterEach(() => {
  vi.unstubAllGlobals()
})


describe("the direct keyless Parallel client", () => {
  it("sends one anonymous JSON-RPC tools/call and maps sources to search results", async () => {
    const { results } = await parallelSearchPost(direct as unknown as typeof fetch)({ query: "asyncio" })

    expect(results).toEqual([MAPPED])
    const [url, init] = direct.mock.calls[0] as [string, RequestInit]
    expect(url).toBe(MCP_URL)
    const headers = new Headers(init.headers)
    expect(headers.get("Content-Type")).toBe("application/json")
    for (const name of ["Authorization", "Cookie", "X-Api-Key", "X-Provider-Key"]) {
      expect(headers.has(name)).toBe(false)
    }
    const body = JSON.parse(init.body as string)
    expect(body.method).toBe("tools/call")
    expect(body.params).toEqual({
      name: "web_search",
      arguments: { objective: "asyncio", search_queries: ["asyncio"] },
    })
    // The request id is a throwaway, never a run id Parallel could group on.
    expect(body.id).not.toBe("fixture-run")
  })


  it("reads the SSE framing Streamable HTTP may answer with", async () => {
    direct.mockResolvedValue(mcpReply({ results: [SOURCE] }, { sse: true }))
    const { results } = await parallelSearchPost(direct as unknown as typeof fetch)({ query: "asyncio" })
    expect(results).toEqual([MAPPED])
  })


  it("keeps the good sources when one comes back without excerpts", async () => {
    direct.mockResolvedValue(
      mcpReply({ results: [{ url: "https://a.com/scan.pdf", title: "Scan", excerpts: null }, SOURCE] }),
    )
    const { results } = await parallelSearchPost(direct as unknown as typeof fetch)({ query: "asyncio" })
    expect(results).toEqual([{ url: "https://a.com/scan.pdf", title: "Scan", content: "" }, MAPPED])
  })


  it("raises on a tool error instead of reporting an empty result set", async () => {
    direct.mockResolvedValue(mcpReply({ error: "quota exhausted" }, { isError: true }))
    await expect(parallelSearchPost(direct as unknown as typeof fetch)({ query: "q" })).rejects.toThrow(
      /quota exhausted/,
    )
  })
})


describe("explicit keyless Parallel search", () => {
  it("keeps the default and saved keys, persisting a keyless selection without fake credentials", () => {
    const store = useByokStore.getState()
    expect(store.searchEngine).toBe("perplexity")
    store.setSearchKey("tavily", "saved-tavily-key")
    store.setSearchEngine("parallel")
    expect(useByokStore.getState().searchByok()).toBeNull()
    expect(useByokStore.getState().search.tavily).toBe("saved-tavily-key")
    store.setSearchEngine("tavily")
    expect(useByokStore.getState().searchByok()).toEqual({ engine: "tavily", apiKey: "saved-tavily-key" })
    useByokStore.setState({ search: {} })
    store.setSearchEngine("parallel")
    expect(JSON.parse(localStorage.getItem("dim0.byok") ?? "{}").searchEngine).toBe("parallel")
  })


  it("drops the keyless selection on clear(), so memory and storage agree", () => {
    const store = useByokStore.getState()
    store.setSearchEngine("parallel")
    store.clear()
    expect(useByokStore.getState().searchEngine).toBe("perplexity")
    expect(localStorage.getItem("dim0.byok")).toBeNull()
  })


  it("resolves keyless for an explicit selection, and off when the network is down", () => {
    for (const signedIn of [false, true]) {
      const ctx = agentResolveContext({ signedIn, searchEngine: "parallel", online: true })
      expect(resolveService("search", ctx)).toEqual({ kind: "search", mode: "keyless", provider: "parallel" })
      expect(resolveService("llm", ctx).mode).toBe(signedIn ? "managed" : "off")
      // Offline the tool is hidden, like every engine that cannot be reached.
      const offline = agentResolveContext({ signedIn, searchEngine: "parallel", online: false })
      expect(resolveService("search", offline).mode).toBe("off")
      expect(resolveSearchClient({ signedIn, engine: "parallel", online: false })).toBeNull()
    }
    expect(resolveSearchClient({ signedIn: false, engine: "perplexity" })).toBeNull()
  })


  it("runs through the agent loop against Parallel directly, never touching the relay", async () => {
    const client = resolveSearchClient({ signedIn: false, engine: "parallel", runId: "fixture-run", online: true })
    expect(client).not.toBeNull()
    let turns = 0
    let received: LlmMessage[] = []
    const llm: LlmClient = {
      async complete(messages) {
        if (turns++ === 0) return toolTurn("web_search", { query: "Python asyncio documentation" })
        received = messages
        return { kind: "text", text: `See ${SOURCE.url}` }
      },
    }
    const events: AgentEvent[] = []
    for await (const event of runAgent({
      userMessage: "Find asyncio documentation", llm,
      tools: [makeWebSearchTool(client!)],
      ctx: { store: freshStore("parallel"), confirmTool: async () => "once" },
    })) events.push(event)

    expect(direct).toHaveBeenCalledTimes(1)
    expect((direct.mock.calls[0] as [string])[0]).toBe(MCP_URL)
    expect(relay).not.toHaveBeenCalled()
    expect(events.find((e) => e.type === "tool_result")).toMatchObject({
      toolName: "web_search", result: { results: [MAPPED] },
    })
    expect(JSON.stringify(received.filter((m) => m.role === "tool"))).toContain(SOURCE.url)
    expect(collectSourceUrls(events)).toEqual([SOURCE.url])
    expect(events.at(-1)).toEqual({ type: "done" })
  })


  it("falls back to the /ai/search relay when the direct call cannot get through", async () => {
    direct.mockRejectedValue(new TypeError("Failed to fetch"))
    relay.mockResolvedValue(
      new Response(JSON.stringify({ data: { answer: "", results: [MAPPED] } }), { status: 200 }),
    )
    const client = resolveSearchClient({ signedIn: false, engine: "parallel", runId: "fixture-run", online: true })!

    expect(await client.search("asyncio")).toEqual([MAPPED])
    expect(relay).toHaveBeenCalledTimes(1)
    const [url, request] = relay.mock.calls[0] as [string, RequestInit]
    expect(new URL(url).pathname).toBe("/ai/search")
    expect(JSON.parse(request.body as string)).toEqual({ query: "asyncio", engine: "parallel" })
    expect(new Headers(request.headers).has("X-Provider-Key")).toBe(false)
  })


  it("does not relay when Parallel itself refused, so the shared allowance is spent once", async () => {
    direct.mockResolvedValue(new Response("rate limited", { status: 429 }))
    const client = resolveSearchClient({ signedIn: false, engine: "parallel", online: true })!

    await expect(client.search("asyncio")).rejects.toThrow(/429/)
    expect(relay).not.toHaveBeenCalled()
  })


  it("keeps the confirmation gate and reports a total failure to the agent", async () => {
    direct.mockRejectedValue(new TypeError("Failed to fetch"))
    relay.mockResolvedValue(new Response("Parallel rate limit", { status: 429 }))
    const client = resolveSearchClient({ signedIn: false, engine: "parallel", online: true })!
    const events: AgentEvent[] = []
    let turns = 0
    const llm: LlmClient = { complete: async () => turns++ === 0
      ? toolTurn("web_search", { query: "q" }) : { kind: "text", text: "Search unavailable" } }
    for await (const event of runAgent({
      userMessage: "search", llm, tools: [makeWebSearchTool(client)],
      ctx: { store: freshStore("failure"), confirmTool: async () => "once" },
    })) events.push(event)
    expect(events.find((e) => e.type === "tool_result")).toMatchObject({
      result: { ok: false, tool: "web_search", message: expect.stringContaining("429") },
    })
    expect(relay).toHaveBeenCalledTimes(1)
  })
})
