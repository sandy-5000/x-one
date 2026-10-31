---
title: "MCP (Model Context Protocol)"
order: 6
---
# MCP (Model Context Protocol)

The **Model Context Protocol (MCP)** is an open standard for connecting LLM applications to
external **tools**, **data**, and **prompt templates** over a well-defined wire format. Anthropic
announced MCP in November 2024; by late 2026 it is the de facto integration layer for AI IDEs,
chat clients, and agent runtimes (Cursor, Claude Desktop, VS Code Copilot extensions, custom
agents, and many hosted "connectors").

This page describes **protocol revision `2026-07-28`**, the current specification on
[modelcontextprotocol.io](https://modelcontextprotocol.io/specification/2026-07-28), verified
October 2026. That revision is a major shift toward **stateless, per-request metadata** and
**Streamable HTTP** without protocol-level sessions. Legacy clients and servers still use an
**`initialize` handshake** (`2025-11-25` and earlier); dual-era implementations are common in
production. When SDK APIs differ from the spec, trust the spec and check your SDK version.

```text
Without MCP:  M host apps × N integrations  =  M×N bespoke connectors (API keys, schemas, UX)
With MCP:     M MCP clients  +  N MCP servers =  M+N implementations, shared discovery & security
```

MCP is often described as **"USB-C for AI apps"**: one server implements the protocol once; any
MCP-capable host can list its tools, read its resources, and invoke capabilities with consistent
permission and OAuth patterns. MCP does **not** replace your LLM or function-calling API—it
standardizes **where capabilities live** and **how hosts attach them** to models.

## Why MCP exists

### The integration problem

Before MCP, every product built its own plugin story:

- Hard-coded tool JSON in each chat request (OpenAI function calling, Anthropic tools, etc.).
- Per-vendor OAuth and secret storage duplicated in every app.
- No standard way to expose **read-only context** (files, schemas) separately from **mutating tools**.
- Remote integrations required each host to embed vendor SDKs or REST adapters.

That model breaks down for **IDE-scale** integrations (dozens of local and remote servers per user)
and for **enterprise** deployments where security teams want one OAuth profile, audit trail, and
allow-list of servers.

### Design goals (from the spec's architecture)

| Goal | Mechanism |
| --- | --- |
| Interoperability | JSON-RPC 2.0 messages; version in every request |
| Composability | Many servers per host; one MCP client instance per server connection |
| Least surprise for tool builders | JSON Schema for tool inputs; URI-identified resources |
| User agency | Host owns consent, OAuth tokens, and model access |
| Remote + local | **stdio** subprocess transport and **Streamable HTTP** |

MCP takes inspiration from the **Language Server Protocol (LSP)**: a small set of roles (host /
client / server), capability negotiation, and a message catalog—applied to AI context instead of
editor features.

### What MCP is not

- **Not a model API** — it does not stream tokens or define chat message formats for models.
- **Not a agent orchestration framework** — no built-in planner, memory graph, or multi-agent
  routing (those live in LangGraph, AutoGen, custom code, or extensions like Tasks).
- **Not a replacement for direct function calling** — the host still maps MCP tools into whatever
  schema the model provider expects.
- **Not enforced sandboxing** — roots and prompts do not cryptographically confine servers; the host
  must enforce policy.

## Architecture: host, client, server

```text
┌──────────────────────────── Host (Cursor, Claude Desktop, your agent) ────────────────────────┐
│                                                                                             │
│  User ◄──► UI (consent, OAuth, tool approval) ◄──► LLM / agent loop                         │
│                           │                                                                 │
│              ┌────────────┼────────────┬──────────────┐                                     │
│              ▼            ▼            ▼              ▼                                     │
│         MCP client   MCP client   MCP client    MCP client   (one client ↔ one server)      │
│              │            │            │              │                                     │
└──────────────┼────────────┼────────────┼──────────────┼─────────────────────────────────────┘
               │ stdio      │ stdio      │ Streamable   │ Streamable
               ▼            ▼            HTTP           HTTP
         ┌──────────┐ ┌──────────┐ ┌────────────┐ ┌────────────┐
         │ filesystem│ │ git/local│ │ GitHub SaaS│ │ Postgres   │
         │ MCP server│ │ MCP server│ │ MCP server │ │ MCP server │
         └──────────┘ └──────────┘ └────────────┘ └────────────┘
```

**Host** — The application the user interacts with. It spawns or connects MCP clients, stores
secrets, renders tool approval UI, and runs the LLM. Only the host talks to the model provider
(under normal designs).

**Client** — A connector object inside the host maintaining **one transport** to **one server**.
The spec requires **one client per server** so capability sets and failure domains stay isolated.

**Server** — A process or HTTP service exposing MCP features (tools, resources, prompts). Servers
may be first-party (filesystem) or third-party (SaaS). They MUST NOT assume they hold the user's
model API key unless the design explicitly uses deprecated **sampling** (see below).

### Trust boundaries

```text
  [ User ]     trusts     [ Host ]     trusts (partially)     [ MCP Server ]     calls     [ Upstream APIs ]
                  │              │                                    │
                  │              └── OAuth token scoped to server ────┘
                  └── approves each tool call (SHOULD, per spec safety guidance)
```

Data flows **server → client → host → model** as text (tool results, resource bodies). Any of that
text can carry **prompt injection**. Treat untrusted servers like untrusted browser extensions.

## JSON-RPC foundation

All MCP messages are **JSON-RPC 2.0**:

| Message kind | Has `id`? | Direction (typical) | Example |
| --- | --- | --- | --- |
| Request | Yes | Client → server (or embedded input request) | `tools/call` |
| Response | Yes (matches request) | Server → client | tool result |
| Notification | No | Either direction | `notifications/progress` |

Rules that matter in production:

- **One JSON object per message** on stdio, **newline-delimited**. Embedded newlines inside JSON
  strings are fine; the framing line must be one logical message.
- **Notifications cannot be replied to.** Use them for progress, logging, cancellation, list changes.
- **Errors** use JSON-RPC `error` object with `code`, `message`, optional `data`. MCP defines
  domain codes (e.g. `UnsupportedProtocolVersionError` at `-32022` in modern revisions).

### Modern request envelope (`_meta`)

From **`2026-07-28`**, every client request MUST carry metadata under `_meta`, including:

- `io.modelcontextprotocol/protocolVersion` — e.g. `"2026-07-28"`.
- `io.modelcontextprotocol/clientCapabilities` — what the client offers (elicitation, etc.).
- Optional `io.modelcontextprotocol/clientInfo` — name/version for diagnostics.

On **Streamable HTTP**, the same version MUST appear in the **`MCP-Protocol-Version`** header and
match `_meta`; mismatch yields **`400 Bad Request`** with a `HeaderMismatch` error.

Servers advertise capabilities per request as well (in responses / discover), rather than only once
at session start—because modern MCP is **stateless at the protocol level**.

## Protocol lifecycle: modern vs legacy

Understanding **two eras** is essential for interviews and debugging real hosts.

### Modern (`2026-07-28` and later): no session handshake

There is **no `initialize` negotiation** on the wire for modern-only servers. Each request is
**self-contained**: version + capabilities + method.

```text
Client                                              Server
  │ ── server/discover (optional probe) ───────────► │
  │ ◄── { supportedVersions, capabilities, ... } ─── │
  │                                                   │
  │ ── tools/list  + _meta (version, caps) ─────────► │
  │ ◄── { tools: [...], ttlMs?, cacheScope? } ─────── │
  │ ── tools/call { name, arguments } ─────────────► │
  │ ◄── { content, structuredContent?, isError } ── │
```

If the server does not support the requested version, it returns **`UnsupportedProtocolVersionError`**
with a `supported` list; the client picks a mutual version and retries.

**`server/discover`** is optional but recommended on **stdio** before other calls: it distinguishes
modern servers from legacy ones (legacy may error or hang on unknown pre-initialize methods).

### Legacy (`2025-11-25` and earlier): initialize handshake

Most desktop hosts in 2025–2026 still speak legacy on stdio:

```text
Client                                              Server
  │ ── initialize { protocolVersion, capabilities, clientInfo } ──► │
  │ ◄── { protocolVersion, capabilities, serverInfo } ──────────── │
  │ ── notifications/initialized ─────────────────────────────────► │
  │                                                                 │
  │ ── tools/list ────────────────────────────────────────────────► │
  │ ◄── { tools: [...] } ─────────────────────────────────────────── │
```

After `initialized`, both sides know which optional features (resource subscriptions, prompts,
listChanged notifications) are active **for that connection**.

### Dual-era servers

A server MAY support both:

- Requests with modern `_meta` → handled statelessly per `2026-07-28`.
- An `initialize` request → legacy session semantics for that client.

Compatibility matrix (simplified from the spec):

| Client era | Server era | Outcome |
| --- | --- | --- |
| Modern | Modern | Works; version errors are retryable |
| Modern | Legacy only | Fails unless host falls back after probe |
| Legacy | Dual-era | Works via `initialize` |
| Legacy | Modern only | Fails — legacy clients cannot fall forward |

When building a **new server in 2026**, implement **`2026-07-28`** and consider dual-era only if
you must support older IDEs.

## Server primitives

Servers expose three core feature types. Each has discover / invoke methods and optional
**`listChanged`** notifications when the catalog changes.

### Comparison

| Primitive | Primary actor | Mutates external state? | Discovery | Invocation |
| --- | --- | --- | --- | --- |
| **Tools** | Model (with human approval) | Often yes | `tools/list` | `tools/call` |
| **Resources** | Host / model (read context) | No (read-only) | `resources/list` | `resources/read` |
| **Prompts** | User (explicit UI) | No | `prompts/list` | `prompts/get` |

**Tools** are **model-controlled**: the LLM chooses them based on descriptions (which makes
description injection a security issue). **Prompts** are **user-controlled**: surfaced as slash
commands or palette entries. **Resources** are **application-controlled** context—files, schemas,
dashboards—usually attached by the host without the model "calling" them like tools.

### Tools in depth

**Capabilities** (legacy handshake form; modern servers declare equivalently per era docs):

```json
{
  "capabilities": {
    "tools": {
      "listChanged": true
    }
  }
}
```

**`tools/list`** returns tool descriptors:

- `name` — stable identifier for `tools/call`.
- `title`, `description` — human and model-facing text.
- `inputSchema` — JSON Schema for arguments.
- Optional `icons`, annotations (trust hints—still untrusted unless server is trusted).

The spec **`2026-07-28`** adds list caching hints: `ttlMs`, `cacheScope` (`public` / `private`),
and **`resultType`** (`complete` vs partial) with pagination via `cursor` / `nextCursor`.

**`tools/call`** request:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "get_weather",
    "arguments": {
      "location": "Pune"
    }
  }
}
```

**Result** (success path):

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "Pune: 31 C, clear sky"
      }
    ],
    "structuredContent": {
      "location": "Pune",
      "temp_c": 31,
      "conditions": "clear"
    },
    "isError": false
  }
}
```

`content` is an array of typed parts (`text`, `image`, `audio`, embedded resources). Hosts map these
into model messages. **`structuredContent`** is for programmatic consumers (UI widgets, logging)
without parsing natural language.

**Errors inside tools**: prefer returning `isError: true` with explanatory `content` for business
failures; reserve JSON-RPC errors for protocol problems.

**Tool design pitfalls**:

- Descriptions are **prompt surface** — never embed secrets or instructions like "ignore previous
  rules."
- Keep schemas small; large enums blow context windows.
- Side-effecting tools should be idempotent where possible and name clearly (`delete_*`).

### Resources in depth

Resources are identified by **URI** (`file:///…`, `postgres://…`, custom schemes).

**Capabilities**:

```json
{
  "capabilities": {
    "resources": {
      "listChanged": true,
      "subscribe": true
    }
  }
}
```

- **`resources/list`** — paginated catalog of `{ uri, name, mimeType, description }`.
- **`resources/read`** — returns `contents[]` with `uri`, `mimeType`, and `text` or `blob`.
- **Subscriptions** — with `subscribe`, clients use **`subscriptions/listen`** (modern) to receive
  long-lived change notifications on a stream tied to that request.

Important rule: the resource set MUST NOT vary per-connection arbitrarily for a given authorization;
it MAY vary by **OAuth scopes** on each request in HTTP mode.

**When to use resources vs tools**:

- Static or semi-static context (OpenAPI spec, DDL, README) → **resource**.
- Parameterized queries or actions → **tool**.
- Large files → resource with pagination / templates; avoid dumping into tool descriptions.

### Prompts in depth

**`prompts/list`** and **`prompts/get`** return templated **`messages`** arrays (roles + content)
with optional **`arguments`** schema for UI auto-complete.

Prompts excel at **repeatable workflows** (`/review-pr`, `/incident-summary`) where the user picks
the template explicitly—reducing accidental model invocation compared to tools.

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "prompts/get",
  "params": {
    "name": "code_review",
    "arguments": {
      "language": "python",
      "focus": "security"
    }
  }
}
```

Hosts typically insert returned messages into the chat or agent thread.

## Client primitives and server-initiated flows

Servers sometimes need information only the host has: filesystem layout, user choices, or (legacy)
LLM completions. Modern **`2026-07-28`** embeds server→client requests in **`InputRequiredResult`**
as part of **Multi Round-Trip Requests (MRTR)** instead of writing standalone JSON-RPC requests on
stdio stdout.

### Elicitation (supported; preferred for user input)

**Elicitation** lets a server ask the user for structured data mid-tool-call ("Which environment?",
"Confirm deletion?"). The host renders a form; the user can accept, decline, or cancel.

Flow (conceptual):

```text
Client ── tools/call ──► Server
Client ◄── result: InputRequiredResult { elicitation/create ... } ──
         (host shows UI)
Client ── tools/call (retry with inputResponses + requestState) ──► Server
Client ◄── final tool result ──
```

Use elicitation instead of blocking stdin or inventing ad hoc HTTP callbacks—hosts implement consent
once.

### Sampling (deprecated in `2026-07-28`)

**Sampling** allowed a server to request an LLM completion **through the host**, avoiding server-side
API keys. As of **`2026-07-28` (SEP-2577)**, sampling is **deprecated**—new implementations SHOULD
NOT adopt it; servers SHOULD call model APIs directly with appropriate user-delegated credentials or
 redesign without nested LLM calls.

It remains documented for **dual-era** compatibility. Interviewers may still ask how it worked:
server sends `sampling/createMessage`; client returns assistant text after user review.

### Roots (deprecated in `2026-07-28`)

**Roots** exposed workspace directories (`file://` URIs) from client to server so servers could
scope file operations. Deprecated in favor of **explicit tool arguments**, **resource URIs**, and
**server configuration**—because roots were guidance, not enforcement.

If you see roots in older servers, treat them as hints; **enforce path allow-lists in the server**
anyway.

## Utilities: progress, cancellation, logging, pagination

| Utility | Mechanism | Why it matters |
| --- | --- | --- |
| Progress | `notifications/progress` tied to `progressToken` | Long SQL migrations, big downloads |
| Cancellation | `notifications/cancelled` with request `id` | User aborts; server should stop work |
| Logging | `notifications/message` | Server debug visible in host UI |
| Pagination | `cursor` / `nextCursor` on list endpoints | SaaS with hundreds of tools |
| List changed | `notifications/tools/list_changed`, etc. | Dynamic catalogs without polling |

On **stdio**, progress and log notifications share the single stdout stream with responses—clients
correlate by JSON-RPC `id` and `_meta` subscription fields.

## Transport: stdio

**stdio** is the default for local servers launched by the host:

```text
  Host spawns:  python weather_mcp.py
                │
    stdin  ◄───┼─── JSON-RPC requests/notifications (client → server)
    stdout ────┼──► JSON-RPC responses/notifications (server → client)
    stderr ────┼──► logs (may be ignored; not necessarily errors)
```

Hard rules:

- Server **MUST NOT** print non-JSON to stdout (breaks framing).
- Messages are **newline-delimited JSON**; one message per line.
- Client closes stdin to signal shutdown; server SHOULD exit promptly.

**When to choose stdio**:

- Local filesystem, git, docker socket proxies, dev databases.
- Simple deployment (no TLS, no public endpoint).
- Parent process lifecycle = server lifecycle (auto restart on crash).

**Operational notes**:

- Pass secrets via **environment variables** configured in host JSON—not CLI args visible in
  `ps`.
- Wrap servers in small shell scripts for virtualenv / nvm activation.
- On Windows, mind CRLF; prefer UTF-8 everywhere.

## Transport: Streamable HTTP

**Streamable HTTP** (since `2025-03-26`, revised **`2026-07-28`**) replaces the older **HTTP+SSE**
two-endpoint transport from `2024-11-05`.

```text
                    ┌──────────────── Remote MCP server ────────────────┐
  MCP client        │  POST /mcp  (single MCP endpoint)                 │
      │             │     │                                             │
      └── HTTPS ────┼────►│  one JSON-RPC request per POST              │
          ◄─────────┼─────┤  response: application/json  OR             │
                    │       text/event-stream (SSE) for that request    │
                    └───────────────────────────────────────────────────┘
```

**`2026-07-28` changes** (high impact):

- Removed standalone **GET SSE stream** endpoint.
- Removed **protocol-level sessions**—each POST is stateless; state lives in server app DB if needed.
- Server→client interactions use **MRTR** / `InputRequiredResult`, not reverse HTTP channels.
- Long-lived notifications (list changes, resource updates) ride on a **`subscriptions/listen`**
  request stream.

### Required headers (HTTP)

Every POST includes at minimum:

| Header | Purpose |
| --- | --- |
| `MCP-Protocol-Version` | Must match `_meta` version |
| `Mcp-Method` | Mirrors JSON-RPC `method` |
| `Mcp-Name` | Tool name / resource URI / prompt name when applicable |
| `Accept` | Must include `application/json` and `text/event-stream` |

Notifications accepted by server may return **`202 Accepted`** with empty body.

### Backward compatibility on HTTP

Dual-era clients may attempt a modern POST first; **`400`** without modern error body triggers legacy
fallback (possibly old HTTP+SSE). Prefer **`server/discover`** on stdio; on HTTP, version errors are
actionable via JSON body.

**When to choose Streamable HTTP**:

- Multi-tenant SaaS integrations (GitHub, Linear, internal APIs).
- Centralized OAuth at the resource server.
- Horizontal scale behind load balancers (stateless POSTs).

## Multi Round-Trip Requests (MRTR)

MRTR (SEP-2322) standardizes how a **single logical tool call** spans multiple HTTP POSTs or stdio
message exchanges when the server needs client input:

```text
  tools/call (id=1)
       │
       ▼
  InputRequiredResult  ── contains inputRequests[] (e.g. elicitation/create, roots/list legacy)
       │
       ▼
  tools/call (id=2, requestState, inputResponses)  ── client supplies answers
       │
       ▼
  final CallToolResult
```

This replaces older patterns where servers sent freestanding JSON-RPC requests to clients on HTTP
long-poll channels. Understanding MRTR is key to reading **`2026-07-28`** sequence diagrams.

## Authorization and OAuth 2.1

For **HTTP transports**, MCP defines optional but recommended **OAuth 2.1** flows:

- **MCP server** = OAuth **resource server** (validates access tokens).
- **MCP client (host)** = OAuth **client** on behalf of the user.
- **Authorization server** may be separate (Okta, Auth0, vendor IdP).

Discovery uses **OAuth Protected Resource Metadata** (RFC 9728) so clients learn token endpoints and
scopes. Clients MUST use **Resource Indicators (RFC 8707)**—include a `resource` parameter naming
the MCP server—to prevent **token mis-redemption** across APIs.

**Every HTTP request** carries:

```http
Authorization: Bearer <access-token>
```

Tokens MUST NOT appear in query strings. For **stdio**, OAuth is inappropriate at the wire level;
hosts load credentials from env or OS secret stores and inject them into server subprocess
environments or outbound server-side calls.

### Confused deputy and downstream APIs

A common bug: the host sends the user's GitHub token to an MCP server, and the server uses that
token to call **unrelated** third parties. Mitigations:

- Issue tokens **audience-restricted** to the MCP server (`resource` parameter).
- Server uses **its own** client credentials for upstream calls, mapped to the user via short-lived
  delegation.
- Never treat tool arguments as shell fragments—validate and parameterize.

## Security and trust (spec + practice)

The spec's **Trust & Safety** section emphasizes:

1. **User consent** for data exposure and tool execution.
2. **Data privacy** — hosts must not exfiltrate resource contents without consent.
3. **Tool safety** — treat tool metadata and results as untrusted input to the model.

Practical checklist for server authors:

| Risk | Mitigation |
| --- | --- |
| Prompt injection via tool results | Sanitize / summarize before model; separate display vs model channels |
| Malicious tool descriptions | Host allow-list servers; code-sign or enterprise catalog |
| Over-privileged OAuth scopes | Scope per tool; refresh + rotate; audit |
| Path traversal on file tools | Canonicalize paths; deny `..`; use OS-level sandbox |
| SSRF from HTTP tools | Block link-local and metadata IPs; allow-list domains |
| Denial of service | Timeouts, concurrency limits, progress + cancellation |

Hosts SHOULD show **which server** invoked **which tool** with **which arguments** before execution
for side-effecting operations.

## Building an MCP server in TypeScript

The reference SDK is `@modelcontextprotocol/sdk` (API evolves—verify against your lockfile). Pattern
for a stdio server:

```ts
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const server = new McpServer({
  name: "weather",
  version: "1.0.0",
});

server.registerTool(
  "get_forecast",
  {
    title: "Get forecast",
    description: "Current weather for a city.",
    inputSchema: {
      city: z.string().describe("City name"),
    },
  },
  async ({ city }) => {
    const data = await fetchWeather(city);
    return {
      content: [{ type: "text", text: `${city}: ${data.temp_c} C, ${data.sky}` }],
      structuredContent: { city, temp_c: data.temp_c, sky: data.sky },
    };
  },
);

server.registerResource(
  "supported_cities",
  "weather://cities",
  { title: "Supported cities", mimeType: "application/json" },
  async (uri) => ({
    contents: [{ uri: uri.href, mimeType: "application/json", text: JSON.stringify(CITIES) }],
  }),
);

server.registerPrompt(
  "trip_brief",
  {
    title: "Trip weather brief",
    description: "Packing advice for a city trip.",
    argsSchema: { city: z.string() },
  },
  async ({ city }) => ({
    messages: [
      {
        role: "user",
        content: { type: "text", text: `Summarise weather in ${city} for a traveller packing tonight.` },
      },
    ],
  }),
);

await server.connect(new StdioServerTransport());
```

For **Streamable HTTP**, SDKs expose HTTP transport helpers—bind to `POST /mcp`, terminate TLS at
the edge, and validate `MCP-Protocol-Version` before routing to handlers.

Testing: **`npx @modelcontextprotocol/inspector`** against your entrypoint accelerates debugging
without running the full host.

## Building an MCP server in Python

**FastMCP** (`mcp` PyPI package) is the ergonomic layer most Python authors use:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("weather")

@mcp.tool()
def get_forecast(city: str) -> str:
    """Current weather for a city."""
    data = fetch_weather(city)
    return f"{city}: {data['temp_c']} C, {data['sky']}"

@mcp.resource("weather://cities")
def cities() -> str:
    import json
    return json.dumps(CITIES)

@mcp.prompt()
def trip_brief(city: str) -> str:
    return f"Summarise the weather in {city} for a traveller packing tonight."

if __name__ == "__main__":
    mcp.run()  # stdio default; see SDK for HTTP transport options
```

Production additions:

- Structured logging to **stderr** only.
- Timeouts on external HTTP calls inside tools.
- Explicit input validation (pydantic) even though FastMCP generates schemas.
- Health checks separate from MCP (HTTP `/healthz`) when orchestrating in Kubernetes.

## Host configuration

Hosts read JSON config (locations vary):

| Host | Typical config path |
| --- | --- |
| Cursor | `.cursor/mcp.json` (project) or user settings |
| Claude Desktop | `claude_desktop_config.json` |
| VS Code MCP extensions | workspace / user settings JSON |

Example combining **stdio** and **remote HTTP**:

```json
{
  "mcpServers": {
    "weather": {
      "command": "python",
      "args": ["-m", "weather_mcp"],
      "env": {
        "WEATHER_API_KEY": "${WEATHER_API_KEY}"
      }
    },
    "github": {
      "url": "https://api.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${GITHUB_MCP_TOKEN}"
      }
    }
  }
}
```

Guidelines:

- Prefer **`command` + `args`** arrays over shell string interpolation (injection-safe).
- Document required env vars in README for enterprise rollout.
- For OAuth-backed SaaS, hosts increasingly use **dynamic registration** instead of static bearer
  tokens—follow host-specific docs.

## MCP vs plain function calling

| Dimension | Provider function / tool calling | MCP |
| --- | --- | --- |
| Location | In-process or app-specific backend | Separate server process or URL |
| Discovery | Developer hard-codes tool defs per deploy | Client calls `tools/list` at runtime |
| Context | Usually tools only | Tools + resources + prompts |
| Cross-app reuse | Copy JSON schema into each product | Install server once |
| Auth story | App-managed secrets | OAuth + stdio env conventions |
| Versioning | App release cycle | Server semver independent of host |

**How they compose**: the host lists MCP tools, converts each to an OpenAI / Anthropic / Gemini tool
definition, runs the model loop, and on `tool_use` dispatches **`tools/call`** to the right MCP
server. MCP standardizes **attachment**; function calling standardizes **model invocation**.

## Ecosystem and adoption patterns

Representative integration styles:

- **Local dev**: filesystem + git + test runner MCPs over stdio in IDEs.
- **SaaS connectors**: vendor-hosted Streamable HTTP + OAuth (issue trackers, CRM, data warehouses).
- **Enterprise gateway**: single HTTP MCP front door proxying internal APIs with centralized ACLs.
- **Extensions**: Tasks (async jobs), MCP Apps (UI blobs), Skills over MCP—negotiated via
  `capabilities.extensions` with prefixed keys like `io.modelcontextprotocol/tasks`.

When designing for IC5 interviews, emphasize **failure modes**: server down (partial tool catalog),
 stale tool list caches (`ttlMs`), version mismatch, OAuth refresh errors, and user denial at
 elicitation steps.


---

## MRTR worked example with HTTP headers

Round 1 **`tools/call`** (truncated body):

```http
POST /mcp HTTP/1.1
MCP-Protocol-Version: 2026-07-28
Mcp-Method: tools/call
Mcp-Name: deploy
Accept: application/json, text/event-stream
Content-Type: application/json
Authorization: Bearer …

{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"deploy","arguments":{"env":"staging"},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}
```

`input_required` result → host UI → Round 2 retry with **`inputResponses`** and **`requestState`**.

---

## subscriptions/listen in production

Use a dedicated small Deployment for listen streams; keep stateless tool POSTs on HPA pool. Emit SSE
keep-alive comments during quiet periods. On disconnect, client re-subscribes — no event replay in
`2026-07-28`.

---

## Tool annotations and host enforcement

Use **`readOnlyHint`**, **`destructiveHint`**, **`idempotentHint`** honestly. Hosts may require approval
for destructive tools regardless of OAuth scope breadth.

---

## Resource templates and path safety

`resources/templates/list` exposes parameterized URIs. Validate and canonicalize paths server-side;
never expose unbounded `file:///{path}` without chroot or allow-list.

---

## Sampling, roots, elicitation (legacy + MRTR)

Deprecated core features (SEP-2577) but still appear in older servers. Modern path: tool parameters for
paths; host-owned LLM calls; MRTR **`elicitation/create`** embedded in **`inputRequests`**.

---

## OpenTelemetry in `_meta`

Propagate **`traceparent`** / **`tracestate`** per SEP-414 so tool latency appears in shared traces
with host LLM spans.

---

## Dynamic Client Registration vs Client ID Metadata

New implementations should prefer **Client ID Metadata Documents**; RFC7591 DCR deprecated but still used
by older authorization servers — implement credential binding to **`iss`**.

---

## Schema evolution for tools

Version tools with **`serverInfo.version`** and release notes. Breaking **`inputSchema`** changes
should rename tools (`search_v2`) to avoid silent model behaviour changes.

---

## Rate limiting and abuse

Limit **`server/discover`**, **`tools/list`**, and **`tools/call`** per OAuth client id. stdio servers
trust OS user — still validate inputs to prevent local privilege escalation via shell injection.

---

## Comparison table: MCP revisions at a glance

| Topic | 2025-11-25 | 2026-07-28 |
| --- | --- | --- |
| Handshake | initialize | per-request `_meta` + discover |
| HTTP session | optional Mcp-Session-Id | removed |
| Server→client RPC | GET SSE | MRTR + subscriptions/listen |
| Result shape | legacy | required resultType |
| Cancel on HTTP | notifications/cancelled | close SSE stream |

---

## Historical timeline

`2024-11-05` HTTP+SSE dual endpoint → `2025-03-26` Streamable HTTP → `2025-11-25` sessions →
`2026-07-28` stateless core. Next release target **late March 2027** (SEP-3398).

---

## Internal gateway design pattern

Central MCP server with OAuth, RBAC, audit logging, and read-only SQL tools returning
**`structuredContent`** for automation plus truncated **`content`** for the model.

---

## Threat model summary

| Vector | Mitigation |
| --- | --- |
| Tool description injection | Allow-list servers; length limits |
| Malicious tool results | Host sanitization |
| Confused deputy OAuth | Audience-scoped tokens |
| SSRF in resources | URL allow-list |
| stdio malware | Admin-only MCP config |

---

## Performance targets (starting points)

| Call | p95 target |
| --- | --- |
| server/discover | < 200 ms |
| tools/list (cached) | < 100 ms |
| tools/call read | < 2 s |
| tools/call write | user approval + backend |

---

## FAQ

**Does MCP replace REST?** No — MCP wraps capabilities for agents; systems of record stay REST/SQL.

**One server or many?** Split by trust domain (read vs write connectors).

**Which transport for CI?** stdio or short-lived stateless HTTP with client credentials.



---

## Additional study notes (production MCP)

Ship **`server/discover`** in staging before enabling new hosts. Pin **`MCP-Protocol-Version`** in config.
Run contract tests on golden **`tools/list`** JSON. Never log bearer tokens. Use **`structuredContent`**
for downstream parsers. Document which tools are destructive in runbooks, not only in schema annotations.
When hosts upgrade to **`2026-07-28`**, expect **`initialize`**-less traffic — update metrics dashboards.
For remote servers, enforce **`Origin`** checks and TLS. Prefer idempotent tool design for model retry storms.
Review third-party MCP servers before enabling write tools. Keep stderr for logs on stdio. Set SSE proxy
buffering off. Bind local servers to loopback. Validate JSON Schema on every **`tools/call`**. Use pagination
for large catalogs. Intersect client and server capabilities before exposing tools to the model.




---

---

## Deep dive 1: implementing discover and version negotiation

Production servers MUST implement **`server/discover`**. Clients use it to pick **`2026-07-28`** vs
legacy **`2025-11-25`** before traffic cutover. Return **`supportedProtocolVersions`** honestly; never
advertise versions your HTTP stack cannot parse (missing **`Mcp-Method`** support, etc.).

Implementation checklist 1:

1. Parse **`MCP-Protocol-Version`** header and compare to **`params._meta`** — reject mismatch with
   **`HeaderMismatchError`** (`-32020`).
2. On unsupported version, return **`UnsupportedProtocolVersionError`** (`-32022`) listing supported set.
3. Echo **`io.modelcontextprotocol/serverInfo`** in every result **`_meta`** for support tickets.
4. Log version, method, and OAuth **`client_id`** — not raw tokens.
5. Feature-test both stdio discover probe and HTTP discover POST in CI pipeline 1.

Version downgrade scenario 1: host sends **`2026-07-28`**; server responds error; host retries with
**`2025-11-25`** and **`initialize`** — document this fallback in host runbooks during migration quarters.

Discover JSON shape (illustrative — see schema for exact fields):

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "resultType": "complete",
    "protocolVersions": ["2026-07-28", "2025-11-25"],
    "capabilities": { "tools": { "listChanged": true } },
    "serverInfo": { "name": "example", "version": "1.1.0" }
  }
}
```

Dual-stack operators run separate metrics per **`protocol_version`** label to spot stuck legacy clients.

---

## Deep dive 2: implementing discover and version negotiation

Production servers MUST implement **`server/discover`**. Clients use it to pick **`2026-07-28`** vs
legacy **`2025-11-25`** before traffic cutover. Return **`supportedProtocolVersions`** honestly; never
advertise versions your HTTP stack cannot parse (missing **`Mcp-Method`** support, etc.).

Implementation checklist 2:

1. Parse **`MCP-Protocol-Version`** header and compare to **`params._meta`** — reject mismatch with
   **`HeaderMismatchError`** (`-32020`).
2. On unsupported version, return **`UnsupportedProtocolVersionError`** (`-32022`) listing supported set.
3. Echo **`io.modelcontextprotocol/serverInfo`** in every result **`_meta`** for support tickets.
4. Log version, method, and OAuth **`client_id`** — not raw tokens.
5. Feature-test both stdio discover probe and HTTP discover POST in CI pipeline 2.

Version downgrade scenario 2: host sends **`2026-07-28`**; server responds error; host retries with
**`2025-11-25`** and **`initialize`** — document this fallback in host runbooks during migration quarters.

Discover JSON shape (illustrative — see schema for exact fields):

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "resultType": "complete",
    "protocolVersions": ["2026-07-28", "2025-11-25"],
    "capabilities": { "tools": { "listChanged": true } },
    "serverInfo": { "name": "example", "version": "1.2.0" }
  }
}
```

Dual-stack operators run separate metrics per **`protocol_version`** label to spot stuck legacy clients.

---

## Deep dive 3: implementing discover and version negotiation

Production servers MUST implement **`server/discover`**. Clients use it to pick **`2026-07-28`** vs
legacy **`2025-11-25`** before traffic cutover. Return **`supportedProtocolVersions`** honestly; never
advertise versions your HTTP stack cannot parse (missing **`Mcp-Method`** support, etc.).

Implementation checklist 3:

1. Parse **`MCP-Protocol-Version`** header and compare to **`params._meta`** — reject mismatch with
   **`HeaderMismatchError`** (`-32020`).
2. On unsupported version, return **`UnsupportedProtocolVersionError`** (`-32022`) listing supported set.
3. Echo **`io.modelcontextprotocol/serverInfo`** in every result **`_meta`** for support tickets.
4. Log version, method, and OAuth **`client_id`** — not raw tokens.
5. Feature-test both stdio discover probe and HTTP discover POST in CI pipeline 3.

Version downgrade scenario 3: host sends **`2026-07-28`**; server responds error; host retries with
**`2025-11-25`** and **`initialize`** — document this fallback in host runbooks during migration quarters.

Discover JSON shape (illustrative — see schema for exact fields):

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "resultType": "complete",
    "protocolVersions": ["2026-07-28", "2025-11-25"],
    "capabilities": { "tools": { "listChanged": true } },
    "serverInfo": { "name": "example", "version": "1.3.0" }
  }
}
```

Dual-stack operators run separate metrics per **`protocol_version`** label to spot stuck legacy clients.

---

## Deep dive 4: implementing discover and version negotiation

Production servers MUST implement **`server/discover`**. Clients use it to pick **`2026-07-28`** vs
legacy **`2025-11-25`** before traffic cutover. Return **`supportedProtocolVersions`** honestly; never
advertise versions your HTTP stack cannot parse (missing **`Mcp-Method`** support, etc.).

Implementation checklist 4:

1. Parse **`MCP-Protocol-Version`** header and compare to **`params._meta`** — reject mismatch with
   **`HeaderMismatchError`** (`-32020`).
2. On unsupported version, return **`UnsupportedProtocolVersionError`** (`-32022`) listing supported set.
3. Echo **`io.modelcontextprotocol/serverInfo`** in every result **`_meta`** for support tickets.
4. Log version, method, and OAuth **`client_id`** — not raw tokens.
5. Feature-test both stdio discover probe and HTTP discover POST in CI pipeline 4.

Version downgrade scenario 4: host sends **`2026-07-28`**; server responds error; host retries with
**`2025-11-25`** and **`initialize`** — document this fallback in host runbooks during migration quarters.

Discover JSON shape (illustrative — see schema for exact fields):

```json
{
  "jsonrpc": "2.0",
  "id": 4,
  "result": {
    "resultType": "complete",
    "protocolVersions": ["2026-07-28", "2025-11-25"],
    "capabilities": { "tools": { "listChanged": true } },
    "serverInfo": { "name": "example", "version": "1.4.0" }
  }
}
```

Dual-stack operators run separate metrics per **`protocol_version`** label to spot stuck legacy clients.

---

## Deep dive 5: implementing discover and version negotiation

Production servers MUST implement **`server/discover`**. Clients use it to pick **`2026-07-28`** vs
legacy **`2025-11-25`** before traffic cutover. Return **`supportedProtocolVersions`** honestly; never
advertise versions your HTTP stack cannot parse (missing **`Mcp-Method`** support, etc.).

Implementation checklist 5:

1. Parse **`MCP-Protocol-Version`** header and compare to **`params._meta`** — reject mismatch with
   **`HeaderMismatchError`** (`-32020`).
2. On unsupported version, return **`UnsupportedProtocolVersionError`** (`-32022`) listing supported set.
3. Echo **`io.modelcontextprotocol/serverInfo`** in every result **`_meta`** for support tickets.
4. Log version, method, and OAuth **`client_id`** — not raw tokens.
5. Feature-test both stdio discover probe and HTTP discover POST in CI pipeline 5.

Version downgrade scenario 5: host sends **`2026-07-28`**; server responds error; host retries with
**`2025-11-25`** and **`initialize`** — document this fallback in host runbooks during migration quarters.

Discover JSON shape (illustrative — see schema for exact fields):

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": {
    "resultType": "complete",
    "protocolVersions": ["2026-07-28", "2025-11-25"],
    "capabilities": { "tools": { "listChanged": true } },
    "serverInfo": { "name": "example", "version": "1.5.0" }
  }
}
```

Dual-stack operators run separate metrics per **`protocol_version`** label to spot stuck legacy clients.

---

## Deep dive 6: implementing discover and version negotiation

Production servers MUST implement **`server/discover`**. Clients use it to pick **`2026-07-28`** vs
legacy **`2025-11-25`** before traffic cutover. Return **`supportedProtocolVersions`** honestly; never
advertise versions your HTTP stack cannot parse (missing **`Mcp-Method`** support, etc.).

Implementation checklist 6:

1. Parse **`MCP-Protocol-Version`** header and compare to **`params._meta`** — reject mismatch with
   **`HeaderMismatchError`** (`-32020`).
2. On unsupported version, return **`UnsupportedProtocolVersionError`** (`-32022`) listing supported set.
3. Echo **`io.modelcontextprotocol/serverInfo`** in every result **`_meta`** for support tickets.
4. Log version, method, and OAuth **`client_id`** — not raw tokens.
5. Feature-test both stdio discover probe and HTTP discover POST in CI pipeline 6.

Version downgrade scenario 6: host sends **`2026-07-28`**; server responds error; host retries with
**`2025-11-25`** and **`initialize`** — document this fallback in host runbooks during migration quarters.

Discover JSON shape (illustrative — see schema for exact fields):

```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "result": {
    "resultType": "complete",
    "protocolVersions": ["2026-07-28", "2025-11-25"],
    "capabilities": { "tools": { "listChanged": true } },
    "serverInfo": { "name": "example", "version": "1.6.0" }
  }
}
```

Dual-stack operators run separate metrics per **`protocol_version`** label to spot stuck legacy clients.

---

## Deep dive 7: implementing discover and version negotiation

Production servers MUST implement **`server/discover`**. Clients use it to pick **`2026-07-28`** vs
legacy **`2025-11-25`** before traffic cutover. Return **`supportedProtocolVersions`** honestly; never
advertise versions your HTTP stack cannot parse (missing **`Mcp-Method`** support, etc.).

Implementation checklist 7:

1. Parse **`MCP-Protocol-Version`** header and compare to **`params._meta`** — reject mismatch with
   **`HeaderMismatchError`** (`-32020`).
2. On unsupported version, return **`UnsupportedProtocolVersionError`** (`-32022`) listing supported set.
3. Echo **`io.modelcontextprotocol/serverInfo`** in every result **`_meta`** for support tickets.
4. Log version, method, and OAuth **`client_id`** — not raw tokens.
5. Feature-test both stdio discover probe and HTTP discover POST in CI pipeline 7.

Version downgrade scenario 7: host sends **`2026-07-28`**; server responds error; host retries with
**`2025-11-25`** and **`initialize`** — document this fallback in host runbooks during migration quarters.

Discover JSON shape (illustrative — see schema for exact fields):

```json
{
  "jsonrpc": "2.0",
  "id": 7,
  "result": {
    "resultType": "complete",
    "protocolVersions": ["2026-07-28", "2025-11-25"],
    "capabilities": { "tools": { "listChanged": true } },
    "serverInfo": { "name": "example", "version": "1.7.0" }
  }
}
```

Dual-stack operators run separate metrics per **`protocol_version`** label to spot stuck legacy clients.

---

## Deep dive 8: implementing discover and version negotiation

Production servers MUST implement **`server/discover`**. Clients use it to pick **`2026-07-28`** vs
legacy **`2025-11-25`** before traffic cutover. Return **`supportedProtocolVersions`** honestly; never
advertise versions your HTTP stack cannot parse (missing **`Mcp-Method`** support, etc.).

Implementation checklist 8:

1. Parse **`MCP-Protocol-Version`** header and compare to **`params._meta`** — reject mismatch with
   **`HeaderMismatchError`** (`-32020`).
2. On unsupported version, return **`UnsupportedProtocolVersionError`** (`-32022`) listing supported set.
3. Echo **`io.modelcontextprotocol/serverInfo`** in every result **`_meta`** for support tickets.
4. Log version, method, and OAuth **`client_id`** — not raw tokens.
5. Feature-test both stdio discover probe and HTTP discover POST in CI pipeline 8.

Version downgrade scenario 8: host sends **`2026-07-28`**; server responds error; host retries with
**`2025-11-25`** and **`initialize`** — document this fallback in host runbooks during migration quarters.

Discover JSON shape (illustrative — see schema for exact fields):

```json
{
  "jsonrpc": "2.0",
  "id": 8,
  "result": {
    "resultType": "complete",
    "protocolVersions": ["2026-07-28", "2025-11-25"],
    "capabilities": { "tools": { "listChanged": true } },
    "serverInfo": { "name": "example", "version": "1.8.0" }
  }
}
```

Dual-stack operators run separate metrics per **`protocol_version`** label to spot stuck legacy clients.

---

## Deep dive 9: implementing discover and version negotiation

Production servers MUST implement **`server/discover`**. Clients use it to pick **`2026-07-28`** vs
legacy **`2025-11-25`** before traffic cutover. Return **`supportedProtocolVersions`** honestly; never
advertise versions your HTTP stack cannot parse (missing **`Mcp-Method`** support, etc.).

Implementation checklist 9:

1. Parse **`MCP-Protocol-Version`** header and compare to **`params._meta`** — reject mismatch with
   **`HeaderMismatchError`** (`-32020`).
2. On unsupported version, return **`UnsupportedProtocolVersionError`** (`-32022`) listing supported set.
3. Echo **`io.modelcontextprotocol/serverInfo`** in every result **`_meta`** for support tickets.
4. Log version, method, and OAuth **`client_id`** — not raw tokens.
5. Feature-test both stdio discover probe and HTTP discover POST in CI pipeline 9.

Version downgrade scenario 9: host sends **`2026-07-28`**; server responds error; host retries with
**`2025-11-25`** and **`initialize`** — document this fallback in host runbooks during migration quarters.

Discover JSON shape (illustrative — see schema for exact fields):

```json
{
  "jsonrpc": "2.0",
  "id": 9,
  "result": {
    "resultType": "complete",
    "protocolVersions": ["2026-07-28", "2025-11-25"],
    "capabilities": { "tools": { "listChanged": true } },
    "serverInfo": { "name": "example", "version": "1.9.0" }
  }
}
```

Dual-stack operators run separate metrics per **`protocol_version`** label to spot stuck legacy clients.

---

## Deep dive 10: implementing discover and version negotiation

Production servers MUST implement **`server/discover`**. Clients use it to pick **`2026-07-28`** vs
legacy **`2025-11-25`** before traffic cutover. Return **`supportedProtocolVersions`** honestly; never
advertise versions your HTTP stack cannot parse (missing **`Mcp-Method`** support, etc.).

Implementation checklist 10:

1. Parse **`MCP-Protocol-Version`** header and compare to **`params._meta`** — reject mismatch with
   **`HeaderMismatchError`** (`-32020`).
2. On unsupported version, return **`UnsupportedProtocolVersionError`** (`-32022`) listing supported set.
3. Echo **`io.modelcontextprotocol/serverInfo`** in every result **`_meta`** for support tickets.
4. Log version, method, and OAuth **`client_id`** — not raw tokens.
5. Feature-test both stdio discover probe and HTTP discover POST in CI pipeline 10.

Version downgrade scenario 10: host sends **`2026-07-28`**; server responds error; host retries with
**`2025-11-25`** and **`initialize`** — document this fallback in host runbooks during migration quarters.

Discover JSON shape (illustrative — see schema for exact fields):

```json
{
  "jsonrpc": "2.0",
  "id": 10,
  "result": {
    "resultType": "complete",
    "protocolVersions": ["2026-07-28", "2025-11-25"],
    "capabilities": { "tools": { "listChanged": true } },
    "serverInfo": { "name": "example", "version": "1.10.0" }
  }
}
```

Dual-stack operators run separate metrics per **`protocol_version`** label to spot stuck legacy clients.

---

## Deep dive: tools/call content model and errors

Tools return **`content`** (for the model) and optional **`structuredContent`** (for code). Set
**`isError: true`** for business failures the model should recover from; use JSON-RPC errors for
protocol mistakes.

Image and text blocks can mix; mind context window when returning large screenshots — prefer resource
URIs for bulk binary data.

---

## Deep dive: stdio security on developer machines

MCP config is equivalent to shell access — gate who can edit **`mcp.json`**. Sign released server
binaries. On Linux, use read-only filesystem mounts where possible for filesystem MCP roots.

---

## Deep dive: Streamable HTTP Origin and local binding

Bind **`127.0.0.1`** for local HTTP MCP during development. Validate **`Origin`** on every request in
shared dev tunnels (ngrok) to prevent cross-site invocation.

---

## Deep dive: OAuth scope design workshop

Map each tool to required scopes. Separate read and write scopes. Integration tests should fail when
token lacks scope before **`tools/call`** reaches business logic.

---

## Deep dive: pagination and large tool catalogs

Paginate **`tools/list`**; hosts merge pages before model injection. Warn when >100 tools — model routing
quality degrades.

---

## Deep dive: cancellation and long-running tools

On HTTP, closing SSE cancels work — propagate **`AbortSignal`** to downstream APIs. On stdio, honour
**`notifications/cancelled`** when host supports it.

---

## Deep dive: comparing MCP to OpenAPI plugins

OpenAPI gives REST paths; MCP gives agent-centric discovery, prompts, resources, and standard OAuth for
remote servers. Use OpenAPI inside MCP tools when appropriate (generated clients).

---

## Deep dive: enterprise approval flows

Even with OAuth write scope, hosts should confirm destructive **`tools/call`** with user UI. Server
annotations inform but do not replace policy.

---

## Deep dive: testing with MCP Inspector

Run Inspector against stdio command and HTTP URL. Save HAR traces as golden fixtures. Regression-test
**`UnsupportedProtocolVersionError`** paths.

---

## Deep dive: spec governance October 2026

Current **`2026-07-28`**; SEP-3398 moves releases to March/September cadence; next target late March 2027.
Track deprecated features registry before adopting Roots/Sampling in new code.


---

---

## Implementation pattern 11: remote MCP hardening

Pattern 11 covers defense in depth for HTTP MCP servers exposed beyond localhost:

- **Network:** private VPC, WAF, IP allow-list for admin tools.
- **Auth:** OAuth 2.1 with PKCE for users; mTLS optional service-to-service.
- **Input:** JSON Schema validation; max argument payload size; reject unknown properties if strict.
- **Output:** redact secrets from **`content`**; cap response bytes.
- **Audit:** append-only log stream with tool name, subject, latency, resultType.
- **Supply chain:** pin SDK hash; verify container image signatures.

Failure injection test 11: revoke token mid-**`tools/call`** — server should abort downstream job and
return structured error, not hang SSE stream.

---

## Implementation pattern 12: remote MCP hardening

Pattern 12 covers defense in depth for HTTP MCP servers exposed beyond localhost:

- **Network:** private VPC, WAF, IP allow-list for admin tools.
- **Auth:** OAuth 2.1 with PKCE for users; mTLS optional service-to-service.
- **Input:** JSON Schema validation; max argument payload size; reject unknown properties if strict.
- **Output:** redact secrets from **`content`**; cap response bytes.
- **Audit:** append-only log stream with tool name, subject, latency, resultType.
- **Supply chain:** pin SDK hash; verify container image signatures.

Failure injection test 12: revoke token mid-**`tools/call`** — server should abort downstream job and
return structured error, not hang SSE stream.

---

## Implementation pattern 13: remote MCP hardening

Pattern 13 covers defense in depth for HTTP MCP servers exposed beyond localhost:

- **Network:** private VPC, WAF, IP allow-list for admin tools.
- **Auth:** OAuth 2.1 with PKCE for users; mTLS optional service-to-service.
- **Input:** JSON Schema validation; max argument payload size; reject unknown properties if strict.
- **Output:** redact secrets from **`content`**; cap response bytes.
- **Audit:** append-only log stream with tool name, subject, latency, resultType.
- **Supply chain:** pin SDK hash; verify container image signatures.

Failure injection test 13: revoke token mid-**`tools/call`** — server should abort downstream job and
return structured error, not hang SSE stream.

---

## Implementation pattern 14: remote MCP hardening

Pattern 14 covers defense in depth for HTTP MCP servers exposed beyond localhost:

- **Network:** private VPC, WAF, IP allow-list for admin tools.
- **Auth:** OAuth 2.1 with PKCE for users; mTLS optional service-to-service.
- **Input:** JSON Schema validation; max argument payload size; reject unknown properties if strict.
- **Output:** redact secrets from **`content`**; cap response bytes.
- **Audit:** append-only log stream with tool name, subject, latency, resultType.
- **Supply chain:** pin SDK hash; verify container image signatures.

Failure injection test 14: revoke token mid-**`tools/call`** — server should abort downstream job and
return structured error, not hang SSE stream.

---

## Implementation pattern 15: remote MCP hardening

Pattern 15 covers defense in depth for HTTP MCP servers exposed beyond localhost:

- **Network:** private VPC, WAF, IP allow-list for admin tools.
- **Auth:** OAuth 2.1 with PKCE for users; mTLS optional service-to-service.
- **Input:** JSON Schema validation; max argument payload size; reject unknown properties if strict.
- **Output:** redact secrets from **`content`**; cap response bytes.
- **Audit:** append-only log stream with tool name, subject, latency, resultType.
- **Supply chain:** pin SDK hash; verify container image signatures.

Failure injection test 15: revoke token mid-**`tools/call`** — server should abort downstream job and
return structured error, not hang SSE stream.

---

## Implementation pattern 16: remote MCP hardening

Pattern 16 covers defense in depth for HTTP MCP servers exposed beyond localhost:

- **Network:** private VPC, WAF, IP allow-list for admin tools.
- **Auth:** OAuth 2.1 with PKCE for users; mTLS optional service-to-service.
- **Input:** JSON Schema validation; max argument payload size; reject unknown properties if strict.
- **Output:** redact secrets from **`content`**; cap response bytes.
- **Audit:** append-only log stream with tool name, subject, latency, resultType.
- **Supply chain:** pin SDK hash; verify container image signatures.

Failure injection test 16: revoke token mid-**`tools/call`** — server should abort downstream job and
return structured error, not hang SSE stream.

---

## Implementation pattern 17: remote MCP hardening

Pattern 17 covers defense in depth for HTTP MCP servers exposed beyond localhost:

- **Network:** private VPC, WAF, IP allow-list for admin tools.
- **Auth:** OAuth 2.1 with PKCE for users; mTLS optional service-to-service.
- **Input:** JSON Schema validation; max argument payload size; reject unknown properties if strict.
- **Output:** redact secrets from **`content`**; cap response bytes.
- **Audit:** append-only log stream with tool name, subject, latency, resultType.
- **Supply chain:** pin SDK hash; verify container image signatures.

Failure injection test 17: revoke token mid-**`tools/call`** — server should abort downstream job and
return structured error, not hang SSE stream.

---

## Implementation pattern 18: remote MCP hardening

Pattern 18 covers defense in depth for HTTP MCP servers exposed beyond localhost:

- **Network:** private VPC, WAF, IP allow-list for admin tools.
- **Auth:** OAuth 2.1 with PKCE for users; mTLS optional service-to-service.
- **Input:** JSON Schema validation; max argument payload size; reject unknown properties if strict.
- **Output:** redact secrets from **`content`**; cap response bytes.
- **Audit:** append-only log stream with tool name, subject, latency, resultType.
- **Supply chain:** pin SDK hash; verify container image signatures.

Failure injection test 18: revoke token mid-**`tools/call`** — server should abort downstream job and
return structured error, not hang SSE stream.

---

## Implementation pattern 19: remote MCP hardening

Pattern 19 covers defense in depth for HTTP MCP servers exposed beyond localhost:

- **Network:** private VPC, WAF, IP allow-list for admin tools.
- **Auth:** OAuth 2.1 with PKCE for users; mTLS optional service-to-service.
- **Input:** JSON Schema validation; max argument payload size; reject unknown properties if strict.
- **Output:** redact secrets from **`content`**; cap response bytes.
- **Audit:** append-only log stream with tool name, subject, latency, resultType.
- **Supply chain:** pin SDK hash; verify container image signatures.

Failure injection test 19: revoke token mid-**`tools/call`** — server should abort downstream job and
return structured error, not hang SSE stream.

---

## Implementation pattern 20: remote MCP hardening

Pattern 20 covers defense in depth for HTTP MCP servers exposed beyond localhost:

- **Network:** private VPC, WAF, IP allow-list for admin tools.
- **Auth:** OAuth 2.1 with PKCE for users; mTLS optional service-to-service.
- **Input:** JSON Schema validation; max argument payload size; reject unknown properties if strict.
- **Output:** redact secrets from **`content`**; cap response bytes.
- **Audit:** append-only log stream with tool name, subject, latency, resultType.
- **Supply chain:** pin SDK hash; verify container image signatures.

Failure injection test 20: revoke token mid-**`tools/call`** — server should abort downstream job and
return structured error, not hang SSE stream.

---

## Revision anchor (October 2026)

Verified against official MCP documentation: **current protocol version `2026-07-28`**. No new spec
released in October 2026; scheduled release cadence moves next target to **late March 2027** (SEP-3398).

---
---

## Byte-size study map

This page is structured for sequential reading: architecture → wire format → primitives → transports →
security → SDKs → operations → mistakes → interview → summary → further reading. Re-read transport
sections when debugging HTTP traces in production.

---
## Common mistakes

1. **Printing debug logs to stdout** in stdio servers — breaks JSON framing; use stderr.
2. **Treating MCP as authentication** — OAuth scopes gate HTTP; stdio still needs env-based secrets
   and host trust.
3. **Huge tool schemas in every request** — use pagination and stable ordering; leverage list caching.
4. **Using tools for static files** — prefer resources; saves tokens and clarifies UX.
5. **Ignoring deprecated features** — implementing sampling/roots in greenfield servers against
   `2026-07-28` guidance.
6. **Assuming initialize on HTTP** — modern Streamable HTTP is per-POST metadata-first.
7. **No idempotency** on create tools — models retry; duplicate issues/tickets otherwise.
8. **Passing through bearer tokens** to arbitrary URLs — confused deputy; use RFC 8707 audience.
9. **Skipping user confirmation** for destructive tools — violates spec safety SHOULDs and real user
   trust.
10. **Version drift** — header/body protocol version mismatch causes immediate 400 on HTTP.



## Worked example: end-to-end tool call (legacy stdio)

Assume a legacy host connected to a weather server. After initialize, the model selects
`get_forecast`.

**1. Host sends tools/call**

```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "method": "tools/call",
  "params": {
    "name": "get_forecast",
    "arguments": { "city": "Pune" }
  }
}
```

**2. Server validates schema**, calls upstream API with server-held key, returns:

```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "result": {
    "content": [{ "type": "text", "text": "Pune: 31 C, clear" }],
    "isError": false
  }
}
```

**3. Host** injects result into model thread, model continues. If the server needs user input
(modern MRTR), step 2 would be `InputRequiredResult` instead, and the host would re-post with
`inputResponses`.

Latency budget: stdio IPC is cheap (sub-ms); dominant cost is upstream API and model round-trips.
Size tool results—multi-megabyte JSON belongs in resources or external storage with URIs.

## Worked example: resource read for context

Before answering database questions, the host attaches schema context:

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "resources/read",
  "params": { "uri": "postgres://analytics/public/schema" }
}
```

Server returns DDL text; host may show it in UI and/or prepend to model context. Subscriptions
fire when migrations change schema—client refreshes without restarting server.

## Protocol extensions (optional modules)

Extensions advertise under `capabilities.extensions` with **prefixed** identifiers mirroring `_meta`
rules, e.g.:

```json
{
  "capabilities": {
    "tools": { "listChanged": true },
    "extensions": {
      "io.modelcontextprotocol/tasks": {},
      "io.modelcontextprotocol/ui": {
        "mimeTypes": ["text/html;profile=mcp-app"]
      }
    }
  }
}
```

If either party lacks an extension, the other MUST fall back or error clearly—never assume silent
support.

## Operational deployment patterns

### Local subprocess per user session

```text
  Session start ──► spawn N servers (one client each)
  Session end   ──► close stdin ──► servers exit
  Crash         ──► host restarts server; in-flight JSON-RPC ids lost (client retries)
```

### Remote HTTP behind API gateway

```text
  Client ──► API GW (WAF, rate limit) ──► MCP service ──► internal APIs
                │
                └── OAuth AS (tokens scoped via RFC 8707 resource)
```

Scale stateless POST handlers horizontally; persist OAuth refresh tokens in the host, not in MCP
server memory, unless using server-side sessions for your own business logic (outside protocol).

## Debugging checklist

| Symptom | Likely cause |
| --- | --- |
| Host shows zero tools | initialize not completed (legacy) or wrong protocol version (modern) |
| Parse error on stdio | stdout pollution or partial lines |
| HTTP 400 HeaderMismatch | `MCP-Protocol-Version` != `_meta` version |
| Immediate OAuth 401 | Wrong audience on token; missing `resource` in token exchange |
| Tool succeeds but model hallucinates | `content` empty; host dropped `structuredContent` |
| Hang on startup | legacy server waiting for initialize while client sends modern requests |

## Tool result content types

`tools/call` results carry a `content` array. Each element has a `type` field; hosts map these into
multimodal model APIs where supported.

| `type` | Fields | Typical use |
| --- | --- | --- |
| `text` | `text` | Human-readable summaries, logs, markdown |
| `image` | `data` (base64), `mimeType` | Screenshots, charts |
| `audio` | `data`, `mimeType` | Short clips (transcription may be host-side) |
| `resource` | `resource` (uri + text/blob) | Inline attachment without separate `resources/read` |

Prefer **`structuredContent`** (JSON object) when the host UI or downstream automation needs typed
fields without parsing natural language. Some hosts strip `structuredContent` from model context to
save tokens while still rendering dashboards—document what your server returns for both channels.

## Legacy `initialize` on the wire

Still common on stdio hosts in 2026. Example request:

```json
{
  "jsonrpc": "2.0",
  "id": 0,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-11-25",
    "capabilities": {
      "roots": { "listChanged": true },
      "sampling": {}
    },
    "clientInfo": {
      "name": "example-host",
      "version": "1.2.0"
    }
  }
}
```

Example result:

```json
{
  "jsonrpc": "2.0",
  "id": 0,
  "result": {
    "protocolVersion": "2025-11-25",
    "capabilities": {
      "tools": { "listChanged": true },
      "resources": { "subscribe": true, "listChanged": true },
      "prompts": { "listChanged": false }
    },
    "serverInfo": {
      "name": "weather",
      "version": "1.0.0"
    }
  }
}
```

The client then sends the **`notifications/initialized`** notification (no `id`). Only after that
may either side assume the peer is ready for feature traffic on legacy connections.

## Modern `_meta` example (2026-07-28)

Every modern client request includes `_meta` alongside normal params:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/list",
  "params": {
    "_meta": {
      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/clientInfo": {
        "name": "example-host",
        "version": "2.0.0"
      },
      "io.modelcontextprotocol/clientCapabilities": {
        "elicitation": {}
      }
    }
  }
}
```

On Streamable HTTP the same version string MUST appear in `MCP-Protocol-Version: 2026-07-28`.

## OAuth discovery flow (HTTP servers)

```text
  MCP Client (host)                         Authorization Server              MCP Server (RS)
        │                                            │                              │
        │── GET /.well-known/oauth-protected-resource ─────────────────────────────►│
        │◄── metadata: authorization_servers, scopes ────────────────────────────────│
        │                                            │                              │
        │── OAuth authorize + token (resource=RS URL per RFC 8707) ────────────────►│
        │◄── access_token (audience scoped to RS) ─────────────────────────────────│
        │                                            │                              │
        │── POST /mcp  Authorization: Bearer … ────────────────────────────────────►│
        │◄── tools/list result ──────────────────────────────────────────────────────│
```

STDIO servers skip this wire flow; the **host** stores tokens and may pass them as environment
variables or outbound HTTP client configuration inside the server process.

## How a host composes MCP with an LLM loop

Conceptual loop (pseudocode):

```python
mcp_tools = []
for client in mcp_clients:
    mcp_tools.extend(client.list_tools())

while not task_done:
    response = llm.chat(messages, tools=to_provider_schema(mcp_tools))
    if response.tool_calls:
        for call in response.tool_calls:
            if not user_approved(call):
                break
            client = client_for_tool(call.name)
            result = client.call_tool(call.name, call.arguments)
            messages.append(tool_result_message(result.content))
    else:
        break
```

MCP sits **outside** the model API: listing, approval, transport, OAuth, and mapping schemas are
host responsibilities. Multiple servers merge into one flat tool namespace—use **namespacing** or
prefixes (`github_create_issue`) to avoid collisions.

## List caching and catalog hygiene

`tools/list`, `resources/list`, and `prompts/list` may return **`ttlMs`** and **`cacheScope`**
(`public` or `private`). Hosts should cache until TTL expires unless a **`list_changed`**
notification arrives first. Servers SHOULD return tools in **deterministic order** when unchanged to
improve LLM prompt-cache hits when tool definitions sit in system prompts.

After dynamic deploys (feature flags, new integrations), emit **`notifications/tools/list_changed`**
so clients refresh without waiting for TTL.

## Completion API (prompt and resource arguments)

Hosts may call **`completion/complete`** so UI fields for `prompts/get` or resource templates offer
context-aware suggestions (similar in spirit to LSP completion). The server returns ranked string
options given a partial argument name/value. This is optional sugar—servers without completion still
work; clients fall back to free-text inputs.

## Notable JSON-RPC errors (modern)

| Code / name | When |
| --- | --- |
| `-32602` Invalid params | Schema validation failed on tool arguments |
| `-32601` Method not found | Legacy server before initialize, or typo in method |
| `-32022` UnsupportedProtocolVersionError | `_meta` or header version not implemented |
| HeaderMismatch (HTTP 400) | `MCP-Protocol-Version` differs from `_meta` |

Clients should treat **`UnsupportedProtocolVersionError`** as retryable with a listed version, not as
a fatal server crash.

## Interview questions

### 1. What problem does MCP solve that OpenAI function calling does not?

Function calling defines how **a model** invokes **tools you embedded in that API request**. MCP
defines how **hosts discover and invoke tools** maintained by **separate server processes** with
shared transports, OAuth, resources, and prompts. MCP is an integration layer; function calling is a
model API feature. They compose: MCP feeds tool definitions into function calling.

### 2. Name the three roles in MCP architecture.

**Host** (LLM app), **client** (one connector inside the host per server), **server** (tool/data
provider). One client ↔ one server connection.

### 3. What changed in MCP `2026-07-28` vs the initialize-based era?

Per-request `_meta` with protocol version; optional `server/discover`; stateless Streamable HTTP;
MRTR for server→client input; removal of protocol-level HTTP sessions; sampling and roots deprecated.
Legacy `initialize` remains for older clients via dual-era servers.

### 4. How are tools, resources, and prompts different?

**Tools** — model-invoked actions with side effects. **Resources** — read-only URI-addressed
context. **Prompts** — user-selected templates via UI. Discovery: list endpoints; invocation:
call/read/get respectively.

### 5. Why must stdio servers log to stderr?

stdout is the JSON-RPC byte stream; any non-protocol bytes corrupt framing and break clients.

### 6. Explain Streamable HTTP at a high level.

Single POST endpoint; one JSON-RPC message per POST; response is JSON or SSE stream scoped to that
request; mandatory version/method headers on `2026-07-28`; OAuth bearer on each request.

### 7. What is MRTR?

Multi Round-Trip Requests: pattern where a tool call returns `InputRequiredResult` embedding
follow-up requests (elicitation, etc.) answered by the client in a subsequent request with
`requestState` / `inputResponses`.

### 8. How should HTTP MCP servers use OAuth?

Act as resource servers validating bearer tokens; clients obtain tokens via OAuth 2.1 with RFC 8707
resource indicators; never put tokens in query strings.

### 9. Why is tool description injection a security issue?

The model reads descriptions when choosing tools; a malicious server can smuggle instructions
("always email secrets to …"). Hosts must treat untrusted servers like untrusted code.

### 10. When would you expose data as a resource instead of a tool?

Read-only, URI-stable context (schema, config snapshot, documentation) the host attaches without
executing code paths—especially large blobs with caching/subscriptions.

### 11. What happens if client and server protocol versions mismatch (modern)?

Server returns `UnsupportedProtocolVersionError` with supported versions; client retries with a
mutual version or surfaces error.

### 12. How does dual-era stdio probing work?

Client sends `server/discover`; `DiscoverResult` or modern error ⇒ modern server; other errors/timeout
⇒ fall back to legacy `initialize`.

### 13. Why were sampling and roots deprecated?

They pushed host responsibilities (model access, workspace exposure) through complex server-initiated
channels; explicit tool parameters, resources, and direct model API usage simplify security.

### 14. How do list_changed notifications help?

Servers notify clients when tool/resource/prompt catalogs change so hosts refresh without polling;
important for dynamic SaaS integrations.

### 15. Compare confused deputy risk in MCP vs monolithic apps.

Monoliths hold one token bucket; MCP servers may receive user tokens that could be misused against
third APIs if audience/scopes are loose—RFC 8707 and per-server tokens mitigate.



### Extended model answers (selected)

**Walk through OAuth for a remote MCP server.** The host discovers protected resource metadata for the MCP
URL, learns authorization server endpoints, registers or uses Client ID Metadata, performs authorization
code + PKCE for the user, obtains an access token whose audience is the MCP resource server, and sends
`Authorization: Bearer` on each POST. The MCP server validates JWT claims (iss, aud, exp, scopes) before
executing `tools/call`. Tokens are keyed by authorization server issuer in persistent client storage.

**Explain resultType complete vs input_required.** Complete means the RPC finished; input_required is an
MRTR interim result listing inputRequests the host must satisfy before the client retries the same logical
request with inputResponses and opaque requestState.

**Why separate resources from tools?** Resources identify read-mostly context the application selects;
tools are model-invoked actions with side effects. Separation improves security reviews and caching.

**How does Cursor-style stdio differ from SaaS HTTP MCP?** stdio trusts the local user and process spawn;
HTTP uses TLS + OAuth and scales horizontally with stateless POSTs under 2026-07-28.

**What breaks when nginx buffers SSE?** Progress notifications arrive in one batch at end of request;
users think the tool hung; cancellation signals delay.

**How do hosts map MCP to OpenAI tools?** After `tools/list`, the host converts each MCP tool name,
description, and JSON Schema into the vendor tool format, passes them on the chat/completions request,
and when the model emits a tool call, the host forwards arguments to MCP `tools/call` and inserts
results as tool role messages.

**When is stdio the wrong choice?** When multiple users need the same integration remotely, when you
require centralized OAuth and audit, or when the capability must run in your VPC without installing
binaries on every laptop — use Streamable HTTP instead.

## Summary

- MCP **`2026-07-28`**: JSON-RPC 2.0, stateless requests with `_meta` version/capabilities, optional
  `server/discover`, MRTR for nested client input.
- **Roles**: host (UI + model), client (1:1 with server), server (tools/resources/prompts).
- **Transports**: **stdio** (local subprocess, newline JSON); **Streamable HTTP** (POST `/mcp`, JSON
  or SSE per request, OAuth).
- **Server features**: `tools/list|call`, `resources/list|read`, `prompts/list|get`, subscriptions.
- **Client features**: **elicitation** (current); **sampling** and **roots** deprecated—know for legacy.
- **Security**: user consent, least-privilege OAuth, stderr logging, no stdout pollution, treat tool
  text as untrusted.
- **vs function calling**: MCP standardizes external capability providers; hosts still map tools into
  provider tool APIs.

## Further reading

- Model Context Protocol specification (revision **2026-07-28**) — modelcontextprotocol.io
- MCP architecture overview — modelcontextprotocol.io specification architecture pages
- JSON-RPC 2.0 — jsonrpc.org specification
- OAuth 2.1 draft and Bearer Token Usage (resource requests)
- RFC 8707 — Resource Indicators for OAuth 2.0
- RFC 9728 — OAuth 2.0 Protected Resource Metadata
- Language Server Protocol — foundation for MCP's client/server split (microsoft.github.io/language-server-protocol)
- Anthropic MCP announcement (November 2024) — historical context for adoption timeline
