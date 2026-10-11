/**
 * Pure service resolution (G1): decide per kind whether to use the user's key
 * (byok), our metered proxy (managed), or nothing (off).
 *
 * An explicitly selected keyless search engine resolves before this matrix —
 * but only while the network is reachable, so it goes `off` (and `web_search`
 * is hidden) offline, exactly like the engines that need a key or our server.
 * Precedence for other selections:
 *   1. preferManaged + managed available → managed (opt-in metering),
 *   2. a usable BYOK key present         → byok (BYOK wins by default; a
 *      configured key is never silently metered),
 *   3. managed available                 → managed,
 *   4. otherwise                         → off.
 *
 * "managed available" = signed in AND `managedAllowed(kind)` (default true). No
 * network, no React — one deterministic function so the whole matrix is tested.
 */
import type {
  ResolveContext,
  ServiceKind,
  ServiceResolution,
} from "./kinds"
import { isKeylessSearchEngine, SERVICE_KINDS } from "./kinds"


const hasUsableKey = (ctx: ResolveContext, kind: ServiceKind): boolean => {
  const cred = ctx.byok[kind]
  return !!cred && cred.apiKey.trim().length > 0
}


const managedAvailable = (ctx: ResolveContext, kind: ServiceKind): boolean =>
  ctx.signedIn && (ctx.managedAllowed ? ctx.managedAllowed(kind) : true)


/** Resolve one service kind against the context. */
export const resolveService = (
  kind: ServiceKind,
  ctx: ResolveContext,
): ServiceResolution => {
  if (kind === "search" && isKeylessSearchEngine(ctx.searchEngine)) {
    return ctx.online === false
      ? { kind, mode: "off" }
      : { kind: "search", mode: "keyless", provider: ctx.searchEngine }
  }
  const byokReady = hasUsableKey(ctx, kind)
  const managedReady = managedAvailable(ctx, kind)
  const cred = ctx.byok[kind]

  if (ctx.preferManaged && managedReady) {
    return { kind, mode: "managed", provider: cred?.provider, model: cred?.model }
  }
  if (byokReady && cred) {
    return { kind, mode: "byok", credential: cred }
  }
  if (managedReady) {
    return { kind, mode: "managed", provider: cred?.provider, model: cred?.model }
  }
  return { kind, mode: "off" }
}


/** Resolve every service kind at once. */
export const resolveAllServices = (
  ctx: ResolveContext,
): Record<ServiceKind, ServiceResolution> => {
  const out = {} as Record<ServiceKind, ServiceResolution>
  for (const kind of SERVICE_KINDS) out[kind] = resolveService(kind, ctx)
  return out
}
