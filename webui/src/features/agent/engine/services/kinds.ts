/**
 * Service layer (G1) — the vocabulary for "every capability is a service, each
 * independently BYOK or managed".
 *
 * A `ServiceKind` is a capability the agent can reach off-device. Each kind is
 * resolved (per request) to a `ServiceMode`:
 *   - "byok"    → call the provider directly with the USER's key,
 *   - "managed" → call our server proxy, which forwards with OUR keys (metered),
 *   - "keyless" → explicitly selected anonymous Parallel search via our relay,
 *   - "off"     → unavailable (the corresponding tool is hidden).
 *
 * This module is pure types + constants — no React, no network — so the
 * resolution logic (`resolve.ts`) is trivially unit-testable.
 */

export type ServiceKind = "llm" | "search" | "code" | "fetch" | "parse"


export const SERVICE_KINDS: readonly ServiceKind[] = ["llm", "search", "code", "fetch", "parse"]


export type ServiceMode = "byok" | "managed" | "keyless" | "off"


/**
 * A user-supplied credential for one service (BYOK). `model` applies to the LLM
 * service (and any provider that needs a model id); other kinds may omit it.
 */
export type ByokCredential = {
  provider: string
  apiKey: string
  model?: string
}


/** The user's BYOK keys, keyed by service. Any subset may be present. */
export type PerKindByok = Partial<Record<ServiceKind, ByokCredential>>


/** The resolved decision for one service kind. */
export type ServiceResolution =
  | { kind: ServiceKind; mode: "byok"; credential: ByokCredential }
  | { kind: ServiceKind; mode: "managed"; provider?: string; model?: string }
  | { kind: "search"; mode: "keyless"; provider: "parallel" }
  | { kind: ServiceKind; mode: "off" }


/**
 * Inputs the resolver decides from. `managedAllowed` lets entitlement/plan gating
 * (G4) veto managed per-kind; default is "allowed when signed in". `preferManaged`
 * is the user preference to use our keys even when a BYOK key exists (default: BYOK
 * wins, so a configured key is never silently metered).
 */
export type ResolveContext = {
  signedIn: boolean
  /** Explicit keyless search selection, independent of keys and sign-in. */
  searchEngine?: string
  byok: PerKindByok
  managedAllowed?: (kind: ServiceKind) => boolean
  preferManaged?: boolean
}
