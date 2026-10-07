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

An explicit `parallel` search selection resolves to `keyless` before the existing
BYOK/managed/off precedence. It uses `/ai/search` without a provider key or a
managed quota charge, guarded by the relay's per-IP limit and Parallel's anonymous
limits. Other services and search selections retain their existing precedence.
The backend owns a dedicated anonymous Streamable HTTP MCP client. It does not
reuse saved credentials or the shared keyed HTTP client. Desktop also uses the
relay for this option; desktop-direct Parallel search is not implemented.
