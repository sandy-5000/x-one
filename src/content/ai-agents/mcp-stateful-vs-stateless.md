---
title: "MCP: stateful vs stateless"
order: 7
---
# MCP: Stateful vs Stateless Servers

## Key points

- **Stateful vs stateless** on Streamable HTTP historically meant: does the server mint **`MCP-Session-Id`** and pin a transport to it (`2025-11-25`)?
- **`2026-07-28`**: protocol layer is **stateless** — no session header; **MRTR** + **`subscriptions/listen`** replace GET push; app state still lives in **your DB/Redis**.
- Three layers: **(1)** protocol session (removed 2026), **(2)** SSE stream cursors, **(3)** application/domain state (always your job).
- **Default for new HTTP servers**: stateless POST + **explicit IDs** in tool args (`sandbox_id`, `cart_id`) — any pod can serve any call.
- **Legacy stateful** still required for some old hosts: sticky LB or shared session store; GET for push; see comparison tables below.
- Protocol overview (JSON-RPC, tools, OAuth): companion page **MCP (Model Context Protocol)**.

| Spec revision | HTTP session | GET / DELETE on `/mcp` |
| --- | --- | --- |
| **`2025-03-26` … `2025-11-25`** | Optional **`MCP-Session-Id`** | GET = server→client SSE; DELETE = teardown |
| **`2026-07-28`** (current, Oct 2026) | **Removed** | **405** on modern-only servers |

---

## Three layers of "state"

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ Layer 1 — Protocol session (2025-11-25 only)                                │
│   MCP-Session-Id; one StreamableHTTPServerTransport per session (typical)   │
│   Removed in 2026-07-28                                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│ Layer 2 — Transport stream (SSE)                                            │
│   2025: resumable GET via Last-Event-ID                                     │
│   2026: SSE on one POST response; subscriptions/listen for long-lived push  │
├─────────────────────────────────────────────────────────────────────────────┤
│ Layer 3 — Application state (always yours)                                  │
│   DB, Redis, browser pools, OAuth caches, MRTR requestState in Redis        │
│   Explicit tool args OR hidden in legacy session object                     │
└─────────────────────────────────────────────────────────────────────────────┘
```

Stickiness is for **Layer 1/3 in memory**, not for **`2026-07-28`** protocol itself if MRTR state is externalized.

---

## Comparison: session model (`2025-11-25`)

SDKs often expose via `sessionIdGenerator: () => uuid` vs `undefined`.

| Dimension | Stateful (session minted) | Stateless (no session) |
| --- | --- | --- |
| **`MCP-Session-Id`** | Returned on `initialize`; required on later calls | Not issued |
| In-process memory | Transport (+ often one server instance) per session | New transport per POST (or pooled handlers) |
| Server → client RPC | POST SSE **and** standalone **GET** SSE | Only on **current POST** SSE (legacy) |
| Push (`list_changed`) | GET stream | No GET — poll or limited POST SSE |
| **Last-Event-ID** replay | On GET reconnect | N/A |
| Horizontal scale | Sticky sessions **or** shared store | Any instance, any POST |
| Serverless | Poor (memory, idle timeout, no stickiness) | Natural fit |
| Deploy / roll | Sessions die unless drained | In-flight POST fails; retry if idempotent |
| Typical use | Live subscriptions, legacy sampling on GET | CRUD, search, read-only SaaS tools |

Under **`2026-07-28`**: left column session header → **not used**; GET push → **`subscriptions/listen`**; sampling on GET → **MRTR**.

---

## Feature matrix: capabilities vs mode

| Capability | stdio | HTTP stateful `2025-11-25` | HTTP stateless `2025-11-25` | HTTP `2026-07-28` |
| --- | --- | --- | --- | --- |
| `tools/call` | Yes | Yes | Yes | Yes |
| Progress notifications | Yes | POST + GET | POST SSE only | POST SSE |
| Sampling / elicitation (legacy) | Yes | GET or POST SSE | Limited | **MRTR** |
| `tools/list_changed` push | Yes | **GET** | No | **`subscriptions/listen`** |
| Multi-instance LB | N/A | Affinity or store | Easy | Easy ( + external MRTR state) |

**stdio**: one stdin/stdout pair for process lifetime — **connection-stateful**, not a fleet scaling problem.

---

## Streamable HTTP verbs (`2025-11-25`)

Single MCP endpoint, e.g. `https://api.example.com/mcp`:

```text
                    ┌────────────── /mcp ──────────────┐
                    │  POST (required)                 │
                    │  GET  (optional — stateful push) │
                    │  DELETE (optional — teardown)    │
                    └──────────────────────────────────┘
```

**Security (all revisions)**: validate **`Origin`** → **403**; bind local dev to **127.0.0.1**; **`MCP-Session-Id` is not auth** — always validate OAuth bearer.

---

## POST (both eras)

- **`Accept: application/json, text/event-stream`**
- Body: exactly **one** JSON-RPC message per POST.

| Body type | Response |
| --- | --- |
| Notification | **202 Accepted**, empty |
| JSON-RPC response (legacy client answering server RPC) | **202** |
| Request | **200** + JSON **or** SSE for that request |

Legacy: after init, send **`MCP-Session-Id`** on all subsequent requests. Modern: **`Mcp-Method`**, **`Mcp-Name`**, **`MCP-Protocol-Version`**; client JSON-RPC **responses** on HTTP disallowed — use MRTR retries.

SSE on POST: server MAY emit progress/notifications **before** final JSON-RPC response; SHOULD close stream after final result; MAY prime with empty event + **`id:`** for resume (legacy GET flow).

---

## GET: server → client channel (`2025-11-25`)

```text
  Client                                 Server
    │  POST tools/call (long job)  ───────►│
    │◄──── SSE: progress ──────────────────│  (same POST)
    │  GET /mcp + session  ────────────────►│  parallel push lane
    │◄──── SSE: list_changed / sampling ───│
    │  POST (answer server RPC)  ──────────►│  → 202
```

**Why GET existed**: POST is client-initiated; without GET, no async server→client RPC while client idle.

**`2026-07-28`**: standalone GET **removed**. Push → **`subscriptions/listen`**; mid-tool input → **MRTR**.

---

## DELETE and session lifecycle (`2025-11-25`)

- **`DELETE /mcp`** + **`MCP-Session-Id`** — polite teardown; server MAY return **405**.
- Server MAY expire sessions → **404** on later POST.
- Client on **404** with session: new **`initialize`** without session id.

```text
  [none] ──initialize──► ACTIVE ──DELETE──► TERMINATED
              │                              ▲
              └── idle timeout / deploy ─────┘ (404)
```

### Session ID rules

1. Server **MAY** set **`MCP-Session-Id`** on HTTP response containing **`InitializeResult`**.
2. ID SHOULD be unguessable (UUID, signed token).
3. Client **MUST** send header on all subsequent requests if issued.
4. Missing header on protected call → **400**; unknown id → **404**.

| Situation | Status | Client action |
| --- | --- | --- |
| Missing session after init | **400** | Send stored id |
| Unknown / expired | **404** | Re-`initialize` |
| Bad Origin | **403** | Fix config |

**Hijacking**: bind session to **OAuth `sub`** server-side; HTTPS only; do not log session ids at info in multi-tenant systems.

---

## Resumability: Last-Event-ID (`2025-11-25`)

```http
GET /mcp HTTP/1.1
Accept: text/event-stream
MCP-Session-Id: 7f3c9a2e-…
Last-Event-ID: evt-00042
```

Server **MAY** replay events after that **stream-local** cursor. Implement with ring buffer, Redis stream, or SQL — cap retention.

**`2026-07-28`**: **not supported** — design **idempotent tools** and explicit job ids instead.

**Event store sketch**: in-memory ring (dev), Redis stream keyed by `session_id:stream`, or SQL with monotonic `event_id`. Document max gap before full re-init.

---

## HTTP trace: MRTR checkout (`2026-07-28`)

Round 1 — **`tools/call`** returns **`resultType: input_required`** with opaque **`requestState`** and **`inputRequests`** (e.g. elicitation schema).

Round 2 — same tool name/args plus **`inputResponses`** + same **`requestState`**. May land on a **different pod** — server loads partial state from Redis, completes checkout, returns **`resultType: complete`**.

Round 1 and 2 are independent POSTs; no **`MCP-Session-Id`**.

---

## HTTP traces (legacy stateful)

**Init** → **200** with response header **`MCP-Session-Id: <uuid>`** and **`InitializeResult`**. Later **POST/GET/DELETE** repeat that header. **GET** returns SSE (`id:` + `notifications/tools/list_changed`). **Stateless** tool call: **POST** without session → **200** JSON only.

---

## When stateful sessions help (and cost)

### Benefits (legacy model)

- Server-initiated RPC on idle **GET** (legacy sampling, elicitation).
- Push without polling — tool list / resource updates.
- Hide handles in session (browser, temp dir) without model-visible ids.

### Costs

- RAM per session; **sticky LB** or Redis session registry.
- Rolling deploy kills in-memory sessions unless drained.
- Hard on **Lambda / Workers** (no long-lived GET, no shared memory).

### When stateless wins

- Tools are pure functions of args + token identity.
- State in Postgres/S3/Redis with **explicit ids** in tool args.
- Autoscaling, multi-region, serverless.

**Recommendation**: **`2026-07-28`** or `sessionIdGenerator: undefined` + explicit domain state.

---

## Stateless / MRTR model (`2026-07-28`)

No **`MCP-Session-Id`**. Interactive flows use **multiple POSTs** on one logical tool:

```text
  Host                                    Server
    │  POST tools/call (checkout)  ─────────►│
    │◄── InputRequiredResult + requestState ─│
    │       inputRequests: [ elicitation ]   │
    │  POST tools/call + inputResponses  ───►│  (may hit different pod)
    │◄── resultType: complete ───────────────│
```

**`requestState`** MUST resolve from **Redis/DB**, not pod RAM, if load-balanced.

**Push without GET**:

```http
POST /mcp
Mcp-Method: subscriptions/listen
MCP-Protocol-Version: 2026-07-28
```

→ long-lived SSE on **this POST** for subscribed notifications (`tools/list_changed`).

Modern-only servers: ignore legacy session header; **405** on GET/DELETE.

---

## Hybrid: stateless transport, explicit domain state

```text
  tools/call create_sandbox  ◄── { "sandbox_id": "sb_81f2", "expires_at": "…" }
  tools/call run_command(sandbox_id, cmd)
  tools/call destroy_sandbox(sandbox_id)
```

| Concern | Hidden session | Explicit id |
| --- | --- | --- |
| Any pod serves call | No | Yes |
| Model retry with same id | Opaque | Yes (id in transcript) |
| Audit / support | Hard | Log id per call |
| Survives host reconnect | If session alive | If TTL valid |

Same for carts, ETL jobs, browser automation (`browser_id` + worker pool, not Playwright on transport).

**Worked pattern (browser)**

```text
  Legacy:  initialize → session S → one browser on transport → navigate → screenshot
  Preferred: start_browser → { browser_id } → navigate(browser_id) → screenshot(browser_id)
             Redis: browser:{id} → { owner_sub, worker, ttl }
```

Any stateless POST can serve later steps if **`browser_id`** is valid — no stickiness.

---

## Scaling legacy stateful sessions

```text
         ┌──────── LB ────────┐
    ┌────▼────┐          ┌────▼────┐
    │ Pod A   │          │ Pod B   │
    │ sess-1  │          │ sess-2  │
    └─────────┘          └─────────┘
  POST MCP-Session-Id: sess-1  ──must──► Pod A
```

| Approach | Pros | Cons |
| --- | --- | --- |
| **Sticky** (hash on `MCP-Session-Id`) | Simple in-memory map | Hot pods; pod loss kills sessions |
| **Shared store** (Redis metadata + event replay) | Any pod | Live resources (browser) still need leader or external pool |
| **Drain on deploy** | Clean roll | Requires grace ≥ longest tool |

**Proxies**: `X-Accel-Buffering: no`; long `proxy_read_timeout`; SSE keep-alive comment lines (`:`).

**Capacity**: idle transport ~ tens of KB; headful browser ~ hundreds of MB — enforce **`MAX_SESSIONS`**, idle TTL, **503** at cap.

**NGINX (illustrative)**: `hash $http_mcp_session_id consistent;` on upstream; `proxy_buffering off;` for SSE GET.

---

## Serverless and stateless

| Platform | Guidance |
| --- | --- |
| **Lambda** | One POST per invocation; JSON response mode; no session map in globals; long work → **`job_id`** + poll tool |
| **Cloud Run** | Stateless POST scales; legacy GET needs **min-instances ≥ 1** or lose push on scale-to-zero |
| **Workers** | Stateless tools; **Durable Objects** only if you need strongly consistent per-user state (explicit, not MCP session) |

Long **`reindex`** jobs: progress on POST SSE; cancel via **`notifications/cancelled`** (2025) or close stream (2026).

---

## Migration pitfalls (`2025-11-25` → `2026-07-28`)

| Pitfall | What goes wrong | Fix |
| --- | --- | --- |
| Session id treated as auth | Hijack if id leaks | Always validate bearer + bind session to `sub` |
| Stateful on serverless | 404 after cold start | Stateless + external store |
| Missing GET route (legacy) | Host never gets `list_changed` | Enable GET or upgrade host to `listen` |
| Unbounded session map | OOM | TTL + MAX_SESSIONS |
| Non-idempotent tools + stateless retries | Duplicate charges | Idempotency keys |
| SSE behind nginx buffer | "Streaming broken in prod" | Disable buffering |
| MRTR state in pod RAM | Second POST 404 / stuck loop | Redis `requestState` |
| Assumed JSON-RPC response POST (2026) | Protocol violation | MRTR `inputResponses` only |
| Legacy sampling on GET only | Breaks on modern hosts | MRTR elicitation |
| Stickiness on client IP | Wrong routing | Hash **`mcp-session-id`** header |
| Deploy without drain | Spike in session **404** | preStop + grace; metrics on 404 rate |

**Migration checklist**: externalize in-session handles → explicit id tools → **`subscriptions/listen`** instead of GET push → disable `sessionIdGenerator` → load-test MRTR → document **404 → re-init**.

**MRTR vs legacy sampling**: GET carried `sampling/createMessage`; modern embeds **`InputRequiredResult`** in **`tools/call`** — store **`requestState`** in Redis if POSTs are load-balanced.

---

## Observability

| Signal | Legacy stateful | Stateless / modern |
| --- | --- | --- |
| Correlation | `MCP-Session-Id` + JSON-RPC `id` | Trace id per POST |
| Metrics | `sessions_active`, session age | `post_duration`, tool errors |
| Cancel | `notifications/cancelled` (2025) | Close response SSE stream (2026) |

Log: `oauth_sub`, method, protocol version, hashed session id — not bearer tokens or PII tool args.

---

## HTTP status quick reference

| Status | Legacy stateful | `2026-07-28` |
| --- | --- | --- |
| 200 | JSON or SSE on POST | Same |
| 202 | Notification accepted | Same |
| 400 | Bad/missing session | HeaderMismatch, bad `_meta` |
| 404 | Unknown session | Often method-not-found semantics |
| 405 | GET on stateless legacy | GET/DELETE unsupported |

---

## Summary

```text
  Legacy (≤ 2025-11-25)              Modern (2026-07-28)
  ─────────────────────                ───────────────────
  MCP-Session-Id optional              No protocol session
  POST + GET + DELETE                  POST core (GET/DELETE → 405)
  Push on GET SSE                      subscriptions/listen
  Last-Event-ID resume                 Not supported — idempotent tools
  Scale: sticky or Redis               Scale: round robin + external state
  Sampling on GET SSE                  MRTR InputRequiredResult

  Default: stateless HTTP + explicit IDs in tool args + shared DB/Redis
```

Use legacy stateful only for **unupgraded hosts** or unreplicated live resources — prefer explicit handles for everything else.
