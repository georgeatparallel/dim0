# ADR-AGENT-003: Per-capability service resolution (BYOK / managed / off) + per-run metering

**Status:** Accepted · 2026-07-31
**Applies to:** `webui/src/features/agent/engine/services/**`, `webui/src/features/agent/byok/**`, `webui/src/features/agent/local/use-local-submit-prompt.ts`, `backend/topix/api/router/ai.py`

## Decision
Each external capability is an independent **service kind** — `llm`, `search`,
`code`, `fetch`, `parse` — and each resolves on its own to a **mode** via the pure
`resolveService` (`engine/services/resolve.ts`):

- **Precedence (MUST):** (1) `preferManaged` + managed available → `managed`;
  (2) a usable BYOK key present → **`byok`** (a configured key is *never* silently
  metered); (3) managed available → `managed`; (4) else `off` (tool hidden).
  "managed available" = `signedIn && managedAllowed(kind)` (the per-plan veto seam).
- **Transport split (MUST):** LLM BYOK keys go **direct from the browser to the
  provider** and MUST NOT touch our servers. `search`/`code`/`fetch`/`parse` are
  not browser-reachable → always via the `/ai/*` proxy; a BYOK key for those is
  **relayed per-request** as the `X-Provider-Key` header (used for that one call,
  never stored). `fetch` is managed-only (no key relay).
- **"Ours first, yours as 429-fallback" (search/code/parse):** a signed-in call
  hits our keys; on `429` (over quota) **and** a BYOK key present, the *same call*
  retries with `X-Provider-Key`.
- **Per-run metering (MUST):** the client mints one `X-Run-Id` per user message
  and threads it through every managed call; a whole agent run = **one** billable
  unit. The server dedups the id (Redis SET-NX, `_meter_run`) and charges the
  plan's AI quota **once**; over-quota surfaces `429`.

## Why
Splitting per capability lets a user bring only the keys they have (their own
search key, our managed LLM) and lets each be gated/priced independently — the
foundation for "BYOK-or-managed". BYOK winning by default is a **billing-trust**
invariant: a user who configured a key must never be quietly charged against our
quota. The transport split is a **security** invariant: LLM keys are the sensitive
ones and stay client-side; the others physically can't be (CORS) so they're
relayed for one call and never persisted. Per-run metering makes a multi-tool
agent turn cost one unit regardless of how many tool calls it fans out to — so
pricing tracks user intent, not implementation detail — and the SET-NX dedup makes
that idempotent under retries and parallel tool calls.

## Consequences
- **Release-on-reject:** if the first run call is rejected for quota, the SET-NX
  slot MUST be deleted so a retry with the same id isn't marked already-metered and
  can't ride free. Best-effort; a delete failure never masks the `429`.
- **Two metering dependencies:** `meter_run` (search/code/fetch/parse) allows a
  BYOK-relay escape hatch — `X-Provider-Key` calls run on the user's key, skip our
  quota, and are guarded only by a per-IP cap. `meter_run_managed` (`/ai/llm[/stream]`)
  has **no** escape hatch: always our keys, always charged, auth required. Don't
  merge them — a stray provider-key header must never let a managed LLM ride free.
- **`auto` model:** `classify_auto_model_complexity` picks `pro` only if
  complex **and** the plan allows `pro`, else `lite`. Explicit out-of-tier model →
  `403`; unconfigured/unreachable → `503`. Tiers per plan via `resolve_allowed_model_tiers`.
- Keys live in `localStorage` (`dim0.byok`), per-provider/per-engine, opt-in.
- `navigate` was renamed `fetch` in the engine; image generation was dropped.
  Off-board tools (`fetch`/`web_search`/`code_interpreter`) stay confirm-gated —
  see [`ADR-AGENT-002`](./ADR-AGENT-002-tool-confirm-gate-and-result-contract.md).
- Managed plan resolution follows OSS mode — see [`ADR-BILLING-001`](./ADR-BILLING-001-oss-mode-when-billing-inactive.md).

## Rejected alternatives
- **One global "BYOK vs managed" toggle** — a user rarely has every key; per-kind
  resolution lets them mix (their search key, our LLM).
- **Relay LLM keys through our proxy too** — needless exposure of the most
  sensitive key; the browser can reach LLM providers directly.
- **Meter per tool call** — punishes multi-step reasoning and is non-deterministic
  under retries; one run-id = one unit is intent-aligned and idempotent.

## Verify
`grep -rn "preferManaged\|managedAllowed\|byok" webui/src/features/agent/engine/services/resolve.ts` — the precedence matrix lives in one pure function.
`grep -rn "X-Run-Id\|set_if_absent\|_meter_run\|meter_run_managed" backend/topix/api/router/ai.py` — run-id dedup + the two metering deps (only `meter_run` has the `X-Provider-Key` escape hatch).

## Optional keyless search

A search engine in `KEYLESS_SEARCH_ENGINES` (today: `parallel`) needs no key from
anyone, so an explicit selection of one resolves to **`keyless`** before the
BYOK/managed/off precedence — but only while the network is reachable, so it
falls to `off` offline like any other unreachable provider. Other services and
search selections keep their existing precedence.

- **Direct, not relayed (MUST):** `search.parallel.ai/mcp` sends
  `Access-Control-Allow-Origin: *`, so a keyless engine is the one non-LLM
  service the client calls **directly** — browser `fetch`, desktop plugin-http.
  A local board therefore searches with zero calls to our backend.
- **`/ai/search` is a fallback only:** used when the direct call cannot get
  through. It charges no quota (there is no key of ours to spend), so on a
  hosted deploy (`is_billing_active()`) it **requires a signed-in user** — a
  per-IP window alone is forgeable through `X-Forwarded-For`, and relaying every
  user's search from one IP would burn one shared anonymous allowance. A
  self-hosted deploy keeps it open behind that per-IP window.
- **Wire shape:** Streamable HTTP is JSON-RPC over POST, so one `tools/call` is
  the whole exchange — no MCP SDK, no `initialize`, no session to delete, and
  the same handful of lines on both sides. The request id is a throwaway: an
  internal run id must never become an upstream session handle on a path whose
  whole point is anonymity.
