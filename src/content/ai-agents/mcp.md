---
title: "MCP (Model Context Protocol)"
order: 6
---
# MCP (Model Context Protocol)

## Key points

- **MCP** standardizes how LLM **hosts** attach **external capabilities** (tools, read-only context, prompt templates) over **JSON-RPC 2.0** — not a model API or agent framework.
- **Roles**: **host** (UI + model), **client** (one transport per server inside the host), **server** (capabilities provider).
- **Current spec** (`2026-07-28`): per-request **`_meta`** (version + capabilities); optional **`server/discover`**; **Streamable HTTP** POST-only at protocol layer; **MRTR** for mid-tool client input.
- **Legacy** (`2025-11-25` and earlier): **`initialize`** handshake on stdio and many HTTP hosts; optional **`MCP-Session-Id`** on HTTP — see companion page **MCP: stateful vs stateless**.
- **Transports**: **stdio** (local subprocess, newline JSON); **Streamable HTTP** (remote, OAuth bearer, JSON or SSE per POST).
- **Security**: host owns consent and tokens; treat tool text as **untrusted**; OAuth **resource indicators** (RFC 8707) on HTTP.

```text
Without MCP:  M hosts × N integrations  =  M×N bespoke connectors
With MCP:     M clients  +  N servers     =  M+N shared wire + discovery
```

Spec reference: [modelcontextprotocol.io](https://modelcontextprotocol.io/specification/2026-07-28) (`2026-07-28`, verified October 2026). When SDK APIs differ, trust the spec.

---

## Architecture: host, client, server

```text
┌──────────────────────────── Host (Cursor, Claude Desktop, agent runtime) ────────────────────┐
│  User ◄──► consent / OAuth / tool approval ◄──► LLM loop                                   │
│                           │                                                                  │
│              ┌────────────┼────────────┬──────────────┐                                      │
│              ▼            ▼            ▼              ▼                                      │
│         MCP client   MCP client   MCP client    MCP client   (strictly 1 client ↔ 1 server)  │
└──────────────┼────────────┼────────────┼──────────────┼──────────────────────────────────────┘
               │ stdio      │ stdio      │ Streamable   │ Streamable
               ▼            ▼            HTTP           HTTP
         ┌──────────┐ ┌──────────┐ ┌────────────┐ ┌────────────┐
         │ local    │ │ local    │ │ SaaS MCP   │ │ data MCP   │
         │ MCP srv  │ │ MCP srv  │ │ server     │ │ server     │
         └──────────┘ └──────────┘ └────────────┘ └────────────┘
```

| Role | Responsibility |
| --- | --- |
| **Host** | Spawns/connects clients, stores secrets, runs model, maps MCP tools into provider tool schemas |
| **Client** | One JSON-RPC transport to one server; isolated capability set and failure domain |
| **Server** | Exposes tools, resources, prompts; calls upstream APIs; MUST NOT assume it holds the user's model key |

**Trust boundary**

```text
  [ User ] ──trusts──► [ Host ] ──partial trust──► [ MCP Server ] ──► upstream APIs
                              └── OAuth scoped to server; approve side-effecting tools
```

Data flows **server → client → host → model** as text — **prompt injection** via tool results and descriptions is real. Untrusted servers ≈ untrusted browser extensions.

**What MCP is not**: model streaming API; multi-agent orchestration; cryptographic sandbox (roots/paths are hints — enforce in server code).

---

## JSON-RPC foundation

All messages are **JSON-RPC 2.0**:

| Kind | Has `id`? | Typical direction | Example |
| --- | --- | --- | --- |
| Request | Yes | Client → server (or embedded in MRTR) | `tools/call` |
| Response | Yes | Server → client | tool result |
| Notification | No | Either | `notifications/progress` |

Production rules:

- **stdio**: one JSON object per line on **stdout**; server logs only to **stderr**.
- **Notifications** get no reply — progress, cancellation, list changes.
- **Errors**: JSON-RPC `error` with `code`, `message`, optional `data`; MCP adds domain codes (e.g. `UnsupportedProtocolVersionError` **-32022**).

### Modern request envelope (`_meta`, `2026-07-28`)

Every client request MUST include:

- `io.modelcontextprotocol/protocolVersion` — e.g. `"2026-07-28"`.
- `io.modelcontextprotocol/clientCapabilities` — elicitation, etc.
- Optional `io.modelcontextprotocol/clientInfo` — diagnostics.

On **Streamable HTTP**, the same version MUST appear in **`MCP-Protocol-Version`**; mismatch → **400** `HeaderMismatch`.

---

## Protocol lifecycle

Two eras coexist in production (2025–2026). Debug version mismatches before blaming tools.

### Modern (`2026-07-28`): no wire-level session handshake

Each request is **self-contained** (version + capabilities + method):

```text
Client                                              Server
  │ ── server/discover (optional, especially stdio) ─► │
  │ ◄── supportedVersions, capabilities ─────────────── │
  │ ── tools/list + _meta ─────────────────────────────► │
  │ ◄── { tools, ttlMs?, cacheScope? } ───────────────── │
  │ ── tools/call ─────────────────────────────────────► │
  │ ◄── result or InputRequiredResult (MRTR) ─────────── │
```

- Unsupported version → **`UnsupportedProtocolVersionError`** + `supported` list; client retries.
- **`server/discover`** distinguishes modern servers from legacy on stdio before other calls.

### Legacy (`2025-11-25` and earlier): initialize

```text
Client                                              Server
  │ ── initialize { protocolVersion, capabilities } ───► │
  │ ◄── { protocolVersion, capabilities, serverInfo } ─ │
  │ ── notifications/initialized ──────────────────────► │
  │ ── tools/list ─────────────────────────────────────► │
  │ ◄── { tools: [...] } ─────────────────────────────── │
```

HTTP legacy may also mint **`MCP-Session-Id`** — session semantics are covered on the **stateful vs stateless** page, not here.

### Dual-era compatibility

| Client | Server | Outcome |
| --- | --- | --- |
| Modern | Modern | Works; version errors retryable |
| Modern | Legacy only | Fails unless host falls back after discover |
| Legacy | Dual-era | Works via `initialize` |
| Legacy | Modern only | Fails — cannot fall forward |

New servers in 2026: implement **`2026-07-28`**; add dual-era only for older IDE hosts.

---

## Server primitives

| Primitive | Who invokes | Mutates? | List | Invoke |
| --- | --- | --- | --- | --- |
| **Tools** | Model (with approval) | Often | `tools/list` | `tools/call` |
| **Resources** | Host / app (context) | Read-only | `resources/list` | `resources/read` |
| **Prompts** | User (UI) | No | `prompts/list` | `prompts/get` |

Optional **`listChanged`** notifications when catalogs change; **`2026-07-28`** may add list **`ttlMs`**, **`cacheScope`**, pagination via **`cursor`**.

### Tools

Descriptors: `name`, `title`, `description`, **`inputSchema`** (JSON Schema). Descriptions are **prompt surface** — no secrets or instruction injection.

**`tools/call`** result (success):

```json
{
  "content": [{ "type": "text", "text": "Pune: 31 C, clear" }],
  "structuredContent": { "temp_c": 31 },
  "isError": false
}
```

- **`content`**: typed parts (`text`, `image`, `audio`, embedded resource) → model messages.
- **`structuredContent`**: programmatic consumers (UI, logging) without parsing prose.
- Business failures → **`isError: true`** + explanatory `content`; reserve JSON-RPC errors for protocol faults.

Design: small schemas; idempotent side effects where possible; clear names for destructive ops (`delete_*`).

### Resources

URI-identified read-only context (`file:///…`, custom schemes). **`resources/read`** returns `contents[]` with `text` or `blob`.

- Static or large context (OpenAPI, DDL, README) → **resource**, not tool description dump.
- Parameterized actions → **tool**.
- Subscriptions (legacy + modern listen streams) for change notifications.

Resource set MUST NOT vary arbitrarily per connection for same authorization; MAY vary by **OAuth scopes** per HTTP request.

### Prompts

**`prompts/get`** returns templated **`messages`** + optional **`arguments`** schema — user picks `/review-pr` explicitly, reducing accidental model invocation vs tools.

---

## Client capabilities and server-initiated input

Modern **`2026-07-28`** embeds server→client needs in **MRTR** via **`InputRequiredResult`** inside a tool call — not freestanding JSON-RPC on stdio stdout.

### Elicitation (current)

Server asks structured user input mid-tool ("Confirm delete?", "Pick environment"). Host renders UI; user accept/decline/cancel; client retries **`tools/call`** with **`inputResponses`** + **`requestState`**.

```text
Client ── tools/call ──► Server
Client ◄── InputRequiredResult { elicitation/create … } ──
         (host UI)
Client ── tools/call + inputResponses ──► Server
Client ◄── final tool result ──
```

### Sampling (deprecated `2026-07-28`, SEP-2577)

Legacy: server requested LLM completion **through host** (`sampling/createMessage`) — avoided server-side model keys. **Do not adopt** in new servers; call model APIs with user-delegated credentials or redesign without nested LLM calls. Still appears in dual-era / interview context.

### Roots (deprecated `2026-07-28`)

Legacy: client exposed workspace **`file://`** URIs to scope server file ops. Replaced by **explicit tool arguments**, **resource URIs**, and **server config** — roots were guidance, not enforcement. Enforce path allow-lists in the server regardless.

---

## Utilities

| Utility | Mechanism | Use |
| --- | --- | --- |
| Progress | `notifications/progress` + `progressToken` | Long jobs |
| Cancellation | `notifications/cancelled` (legacy); close SSE stream (`2026-07-28`) | User abort |
| Logging | `notifications/message` | Server debug in host UI |
| Pagination | `cursor` / `nextCursor` on lists | Large catalogs |
| List changed | `notifications/tools/list_changed`, etc. | Dynamic SaaS tools |

---

## Transport: stdio

```text
  Host spawns:  python -m my_mcp
    stdin  ◄── JSON-RPC (client → server)
    stdout ──► JSON-RPC (server → client)   MUST be JSON only
    stderr ──► logs (human-readable OK)
```

| Choose stdio when | Avoid when |
| --- | --- |
| Local fs, git, dev DB | Multi-tenant remote access |
| Simple deploy (no TLS) | Centralized OAuth / audit required |
| Parent lifecycle = server lifecycle | Capability must run in shared VPC only |

Secrets via **host-configured env**, not CLI args visible in `ps`. One client per server subprocess.

---

## Transport: Streamable HTTP

Replaces deprecated **HTTP+SSE dual-endpoint** (`2024-11-05`). Single **`POST /mcp`** (typical):

```text
  Client ──HTTPS──► POST /mcp  (one JSON-RPC message per POST)
         ◄────────── application/json  OR  text/event-stream (scoped to this request)
```

**`2026-07-28` highlights**:

- No standalone **GET** push endpoint at protocol layer (see stateful page for legacy GET).
- No **`MCP-Session-Id`** at protocol layer — per-POST metadata; app state in your DB if needed.
- Long-lived notifications → **`subscriptions/listen`** POST stream.
- Server→client during tools → **MRTR** / `InputRequiredResult`.

### Required headers (modern POST)

| Header | Role |
| --- | --- |
| `MCP-Protocol-Version` | Matches `_meta` version |
| `Mcp-Method` | Mirrors JSON-RPC `method` |
| `Mcp-Name` | Tool name / resource URI / prompt when applicable |
| `Accept` | `application/json, text/event-stream` |
| `Authorization` | `Bearer` token (remote servers) |

Notifications accepted → **202 Accepted**, empty body.

**When to choose HTTP**: SaaS connectors, centralized OAuth, horizontal scale behind LBs (stateless POSTs).

---

## MRTR (Multi Round-Trip Requests)

One logical **`tools/call`** may span multiple POSTs (or stdio exchanges) when the server needs client input:

```text
  tools/call
       ▼
  InputRequiredResult  (inputRequests: elicitation, …)
       ▼
  tools/call (requestState, inputResponses)
       ▼
  final CallToolResult
```

Replaces legacy patterns: server JSON-RPC **requests** on a parallel GET SSE channel. Scale MRTR with **external store** for `requestState` if POSTs hit different instances.

---

## Authorization and OAuth (HTTP, high level)

- **MCP server** = OAuth **resource server** (validates bearer tokens).
- **Host** = OAuth **client** for the user.
- Discovery: **OAuth Protected Resource Metadata** (RFC 9728).
- Tokens: **RFC 8707 resource indicator** — `resource` names the MCP server URL → prevents **token mis-redemption** across APIs.

```http
Authorization: Bearer <access-token>
```

Never tokens in query strings. **stdio**: host injects secrets via env or OS store; no OAuth on the wire.

**Confused deputy**: user token forwarded to MCP server must not be reused against unrelated APIs — audience-restricted tokens; server uses its own upstream credentials mapped to user.

---

## Security and trust (practice)

Spec **Trust & Safety**: user **consent** for data exposure and tool execution; treat metadata and results as **untrusted model input**.

| Risk | Mitigation |
| --- | --- |
| Prompt injection in results | Summarize/sanitize; separate display vs model channels |
| Malicious tool descriptions | Allow-list servers; enterprise catalog |
| Over-privileged OAuth | Minimal scopes per tool; audit |
| Path traversal (file tools) | Canonicalize; deny `..`; OS sandbox |
| SSRF in HTTP tools | Block link-local; domain allow-list |
| DoS | Timeouts, concurrency limits, cancellation |

Hosts SHOULD show **server + tool + arguments** before side-effecting **`tools/call`**.

Validate **`Origin`** on HTTP (mitigate DNS rebinding); local dev bind **127.0.0.1** unless intentional.

---

## Host composition with LLM function calling

MCP does **not** replace provider tool calling — it standardizes **discovery and invocation** of external servers.

```text
  for each MCP client: tools/list → merge into host tool namespace
  model chooses tool → host approves → tools/call on correct client
  result.content → injected as tool message → model continues
```

| Dimension | In-process function calling | MCP |
| --- | --- | --- |
| Discovery | Hard-coded per deploy | `tools/list` at runtime |
| Context | Usually tools only | Tools + resources + prompts |
| Reuse | Copy schemas per app | One server, many hosts |
| Auth | App secrets | OAuth + stdio env conventions |

Use **namespaced tool names** (`github_create_issue`) when merging multiple servers.

---

## Debugging checklist

| Symptom | Likely cause |
| --- | --- |
| Zero tools | Legacy: init incomplete; modern: version mismatch |
| stdio parse error | Non-JSON on stdout |
| HTTP 400 HeaderMismatch | Header version ≠ `_meta` |
| OAuth 401 | Wrong audience / missing `resource` at token exchange |
| Hang on startup | Modern client vs legacy server (or vice versa) |

Test with **`npx @modelcontextprotocol/inspector`** before full host integration.

---

## MCP vs HTTP session state

Protocol-level **`MCP-Session-Id`**, GET/DELETE on `/mcp`, **`Last-Event-ID`**, sticky load balancing, and migration to **`2026-07-28`** are covered in **MCP: stateful vs stateless** — read that page when shipping remote HTTP servers or debugging session **404**s.

---

## Summary

- **Wire**: JSON-RPC 2.0; modern **`_meta`** + optional **`server/discover`**; legacy **`initialize`** still common on stdio.
- **Features**: `tools/*`, `resources/*`, `prompts/*`; **elicitation** via MRTR; **sampling/roots** deprecated but know for legacy.
- **Transports**: stdio (newline JSON, stderr logs); Streamable HTTP (POST, OAuth, JSON or SSE per request).
- **Security**: host consent, RFC 8707 tokens, untrusted tool text, Origin validation on HTTP.
