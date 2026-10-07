import { beforeEach, describe, expect, it, vi } from "vitest"
import { freshStore } from "@/test/canvas"
import type { AgentEvent, LlmClient, LlmMessage } from "./types"
import { toolTurn } from "@/test/llm"
import { runAgent } from "./agent-loop"
import { collectSourceUrls, makeWebSearchTool, resolveSearchClient } from "./web-search"
import { resolveService } from "./services/resolve"
import { agentResolveContext } from "./services/context"
import { useByokStore } from "../byok/byok-store"


const relay = vi.hoisted(() => vi.fn())
vi.mock("@/api", () => ({ fetchWithAuthRaw: relay }))


beforeEach(() => {
  relay.mockReset()
  localStorage.clear()
  useByokStore.setState({ search: {}, searchEngine: "perplexity", llm: {}, codeKey: "", parseKey: "" })
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

  it("resolves keyless only for an explicit search selection", () => {
    for (const signedIn of [false, true]) {
      const ctx = agentResolveContext({ signedIn, searchEngine: "parallel" })
      expect(resolveService("search", ctx)).toEqual({ kind: "search", mode: "keyless", provider: "parallel" })
      expect(resolveService("llm", ctx).mode).toBe(signedIn ? "managed" : "off")
    }
    expect(resolveSearchClient({ signedIn: false, engine: "perplexity" })).toBeNull()
  })

  it("dispatches through the agent loop and relay and feeds cited results into the next model turn", async () => {
    const source = { url: "https://docs.python.org/3/library/asyncio.html", title: "asyncio", content: "Async I/O" }
    relay.mockResolvedValue(new Response(JSON.stringify({ data: { answer: "", results: [source] } }), { status: 200 }))
    const client = resolveSearchClient({ signedIn: false, engine: "parallel", runId: "fixture-run" })
    expect(client).not.toBeNull()
    let turns = 0
    let received: LlmMessage[] = []
    const llm: LlmClient = {
      async complete(messages) {
        if (turns++ === 0) return toolTurn("web_search", { query: "Python asyncio documentation" })
        received = messages
        return { kind: "text", text: `See ${source.url}` }
      },
    }
    const events: AgentEvent[] = []
    for await (const event of runAgent({
      userMessage: "Find asyncio documentation", llm,
      tools: [makeWebSearchTool(client!)],
      ctx: { store: freshStore("parallel"), confirmTool: async () => "once" },
    })) events.push(event)
    expect(relay).toHaveBeenCalledTimes(1)
    const [url, request] = relay.mock.calls[0] as [string, RequestInit]
    expect(new URL(url).pathname).toBe("/ai/search")
    expect(JSON.parse(request.body as string)).toEqual({ query: "Python asyncio documentation", engine: "parallel" })
    const headers = new Headers(request.headers)
    expect(headers.has("X-Provider-Key")).toBe(false)
    expect(headers.get("X-Run-Id")).toBe("fixture-run")
    expect(events.find((e) => e.type === "tool_result")).toMatchObject({ toolName: "web_search", result: { results: [source] } })
    expect(JSON.stringify(received.filter((m) => m.role === "tool"))).toContain(source.url)
    expect(collectSourceUrls(events)).toEqual([source.url])
    expect(events.at(-1)).toEqual({ type: "done" })
  })

  it("keeps the confirmation gate and reports upstream failure to the agent", async () => {
    relay.mockResolvedValue(new Response("Parallel rate limit", { status: 429 }))
    const client = resolveSearchClient({ signedIn: false, engine: "parallel" })!
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
