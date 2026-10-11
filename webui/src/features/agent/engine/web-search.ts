/**
 * Web search as a service — a managed `SearchClient` + the `web_search` agent
 * tool + resolution.
 *
 * The keyed provider APIs aren't browser-reachable (CORS), so they go through
 * `/ai/search`. "Our keys first, yours as fallback": a signed-in user hits our
 * proxy with our keys; if that's over quota (429) and the user has a BYOK
 * search key, the same call is retried with `X-Provider-Key` (relayed, not
 * stored). Signed-out with no key has no authed proxy → off.
 *
 * A keyless engine is the exception: it sends CORS headers, so the client calls
 * it directly (plugin-http on desktop) and a local board reaches search with no
 * call to our backend at all. `/ai/search` is only its fallback, for a client
 * that cannot reach the provider — and on hosted that relay wants a sign-in.
 */
import { z } from "zod"
import { defineTool, type AgentEvent, type Tool } from "./types"
import type { SearchClient, SearchResult } from "./services/clients"
import { resolveService } from "./services/resolve"
import { isOverQuotaError, runIdHeaders } from "./services/run"
import { servicesPost } from "./services/transport"
import { desktopSearchPost } from "./services/desktop-search"
import { keylessSearchPost, KeylessUpstreamError } from "./services/parallel-search"
import { isOnline } from "./services/context"
import { isTauri } from "@/platform"


type SearchResponse = { answer: string; results: SearchResult[] }


/** POST a query to `/ai/search`. Injectable for tests. */
export type SearchPost = (body: { query: string; engine?: string }) => Promise<SearchResponse>


const makeDefaultSearchPost = (runId?: string, byokKey?: string, alwaysByok = false): SearchPost => async (body) => {
  const call = (withKey: boolean) =>
    servicesPost<SearchResponse>("/ai/search", body, {
      ...runIdHeaders(runId),
      ...(withKey && byokKey ? { "X-Provider-Key": byokKey } : {}),
    })
  // byok mode: the user's key IS the source → send it up front. managed mode:
  // our keys first, fall back to the user's key only if we're over quota (429).
  if (alwaysByok && byokKey) return call(true)
  try {
    return await call(false)
  } catch (e) {
    if (byokKey && isOverQuotaError(e)) return call(true)
    throw e
  }
}


/** Managed web-search client — our keys via `/ai/search`, BYOK key as fallback (or the source in byok mode). */
export const managedSearchClient = (
  opts: { runId?: string; engine?: string; byokKey?: string; alwaysByok?: boolean; post?: SearchPost } = {},
): SearchClient => {
  const post = opts.post ?? makeDefaultSearchPost(opts.runId, opts.byokKey, opts.alwaysByok)
  return {
    async search(query: string): Promise<SearchResult[]> {
      const { results } = await post({ query, ...(opts.engine ? { engine: opts.engine } : {}) })
      return results
    },
  }
}


/** Prefer the direct provider call; relay only when it can't be reached at all. */
const keylessPost = (engine: string, runId?: string): SearchPost => {
  const direct = keylessSearchPost(engine)
  const relay = makeDefaultSearchPost(runId)
  if (!direct) return relay
  return async (body) => {
    try {
      return await direct(body)
    } catch (e) {
      // The provider answered and refused (a rate limit, say). Relaying the
      // same query would only spend the same shared anonymous allowance.
      if (e instanceof KeylessUpstreamError) throw e
      // Unreachable from this client — a blocked network, a webview that
      // can't do the cross-origin call. Let the relay's own error surface.
      return await relay(body)
    }
  }
}


/** Resolve the search service to a client, or null when unavailable. A saved
 *  key makes search available even signed-out (relayed); signed-in prefers our
 *  keys with the key as the over-limit fallback; a keyless engine goes direct. */
export const resolveSearchClient = (opts: {
  signedIn: boolean
  runId?: string
  /** The user-selected engine (used on the managed path too). */
  engine?: string
  /** Defaults to the live `navigator.onLine`; pass it explicitly in tests. */
  online?: boolean
  byok?: { engine: string; apiKey: string } | null
}): SearchClient | null => {
  const cred = opts.byok?.apiKey ? { provider: opts.byok.engine, apiKey: opts.byok.apiKey } : undefined
  const resolution = resolveService("search", {
    signedIn: opts.signedIn,
    searchEngine: opts.engine,
    online: opts.online ?? isOnline(),
    preferManaged: opts.signedIn,
    byok: cred ? { search: cred } : {},
  })
  if (resolution.mode === "off") return null
  if (resolution.mode === "keyless") {
    return managedSearchClient({
      runId: opts.runId,
      engine: resolution.provider,
      post: keylessPost(resolution.provider, opts.runId),
    })
  }
  // Desktop BYOK: reach the provider directly (CORS-free) instead of our proxy,
  // so search works offline-of-our-servers. Unported engines keep the /ai/search
  // path (online). In the browser this is always null → unchanged behaviour.
  const desktopPost =
    isTauri() && resolution.mode === "byok" && opts.byok?.apiKey
      ? desktopSearchPost(opts.byok.engine, opts.byok.apiKey)
      : null
  return managedSearchClient({
    runId: opts.runId,
    engine: opts.byok?.engine ?? opts.engine,
    byokKey: opts.byok?.apiKey,
    alwaysByok: resolution.mode === "byok",
    ...(desktopPost ? { post: desktopPost } : {}),
  })
}


/** Collect the source URLs a run surfaced across `web_search` (results[].url) and
 *  `fetch` (the fetched page's url), deduped and in order — fed to citation
 *  correction so the answer's links are the real ones. */
export const collectSourceUrls = (events: AgentEvent[]): string[] => {
  const urls: string[] = []
  const seen = new Set<string>()
  const add = (url: unknown) => {
    if (typeof url === "string" && url && !seen.has(url)) {
      seen.add(url)
      urls.push(url)
    }
  }
  for (const ev of events) {
    if (ev.type !== "tool_result") continue
    if (ev.toolName === "web_search") {
      const results = (ev.result as { results?: { url?: unknown }[] } | undefined)?.results
      for (const r of results ?? []) add(r.url)
    } else if (ev.toolName === "fetch") {
      add((ev.result as { url?: unknown } | undefined)?.url)
    }
  }
  return urls
}


/** The `web_search` agent tool, backed by a resolved search client. */
export const makeWebSearchTool = (client: SearchClient): Tool =>
  defineTool({
    name: "web_search",
    description:
      "Search the web for current or external information. Returns sources (url + title + snippet) to ground and cite the answer.",
    parameters: z.object({ query: z.string().describe("The search query") }),
    run: async ({ query }) => {
      const results = await client.search(query)
      return {
        results: results.map((r) => ({ url: r.url, title: r.title, content: r.content })),
      }
    },
  })
