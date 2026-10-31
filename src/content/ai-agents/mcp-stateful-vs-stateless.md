---
title: "MCP: stateful vs stateless"
order: 7
---
# MCP: Stateful vs Stateless Servers

Remote **MCP (Model Context Protocol)** servers over **Streamable HTTP** must decide how much
**connection-scoped state** they keep between HTTP requests. That decision is not cosmetic: it
controls whether you can use **server-initiated JSON-RPC** (sampling, elicitation, progress on a
side channel), how you **scale horizontally**, whether **AWS Lambda / Cloudflare Workers** are viable,
and how you recover from **proxy timeouts** and **deployments**.

This page is written for backend engineers shipping MCP in production and for senior interviews.
It explains **both** transport eras you will encounter in the wild:

| Spec revision | Streamable HTTP shape | Session header | GET / DELETE on MCP endpoint |
| --- | --- | --- | --- |
| **`2025-03-26` … `2025-11-25`** | Optional **protocol sessions**; standalone GET SSE | **`MCP-Session-Id`** (optional) | GET opens server→client stream; DELETE ends session |
| **`2026-07-28`** (current as of Oct 2026) | **Per-request** POST only; no protocol session | **Removed** — ignore legacy header | **405** for GET/DELETE on modern-only servers |

The title **stateful vs stateless** historically meant: does the server mint a **`MCP-Session-Id`**
and pin a transport instance to it (`2025-11-25`)? In **`2026-07-28`**, the protocol itself is
**stateless at the HTTP session layer** — but your **application** may still hold databases, caches,
and explicit handles. You still choose **stateful application design** vs **stateless tool APIs**;
only the **wire mechanism** changed.

Cross-reference: the main MCP overview lives in the companion page **MCP (Model Context Protocol)**.
Here we go deep on HTTP transport state, scaling, and deployment patterns.

---

## Mental model: three layers of "state"

Engineers conflate three different notions. Separate them before picking a design.

```text
┌────────────────────────────────────────────────────────────────────────────────────────────┐
│ Layer 1 — Protocol session (2025-11-25 only)                                               │
│   MCP-Session-Id header; one StreamableHTTPServerTransport per session in many SDKs        │
│   Removed in 2026-07-28                                                                    │
├────────────────────────────────────────────────────────────────────────────────────────────┤
│ Layer 2 — Transport stream state (SSE cursors, open GET connection)                        │
│   2025: resumable SSE via Last-Event-ID on GET                                             │
│   2026: SSE scoped to one POST response; subscriptions/listen for long-lived notifications │
├────────────────────────────────────────────────────────────────────────────────────────────┤
│ Layer 3 — Application / domain state (always your responsibility)                          │
│   DB rows, Redis carts, browser automation contexts, open file handles, OAuth token cache  │
│   Can be explicit IDs in tool args (stateless HTTP) or hidden in session object (legacy)   │
└────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Interview sound bite:** Protocol sessions solved **where to attach** server→client messaging in
`2025-11-25`. **`2026-07-28`** moved that to **Multi-Round-Trip Requests (MRTR)** and
**`subscriptions/listen`**, so load balancers no longer need stickiness for the protocol itself.
You may still need stickiness for **Layer 3** if you store handles in memory without a shared store.

---

## Quick comparison (2025-11-25 session model)

This table is the classic **stateful vs stateless MCP HTTP server** split under revisions through
**`2025-11-25`**. SDKs expose it via `sessionIdGenerator: () => uuid` vs `undefined`.

| Dimension | Stateful (session minted) | Stateless (no session) |
| --- | --- | --- |
| **`MCP-Session-Id`** | Server returns on `initialize`; client sends on all later calls | Not issued; each POST stands alone |
| **In-process memory** | Transport + often one `McpServer` instance per session | Build server + transport per POST (or pool read-only handlers) |
| **Server → client RPC** | On POST response SSE **and** standalone **GET** SSE stream | Only messages tied to the **current POST** response stream |
| **Notifications** | `tools/list_changed`, resource subscriptions, progress on GET | Progress only if current POST uses SSE; no standalone push channel |
| **Resumable SSE** | Optional **`Last-Event-ID`** replay on GET | Usually N/A (no long-lived GET) |
| **Horizontal scale** | Sticky sessions **or** shared session store + event store | Any instance serves any POST |
| **Serverless** | Hard (memory limits, idle timeouts, no stickiness) | Natural fit |
| **Failure on deploy** | Sessions die unless externalized | In-flight POST fails; retry is safe if tools are idempotent |
| **Typical products** | Interactive automation, long tasks, live subscriptions | CRUD tools, search, read-only APIs |

Under **`2026-07-28`**, replace the left column's **session header** row with "not used"; replace
standalone GET with **`subscriptions/listen`** POST streams; replace cross-request sampling on GET
with **MRTR** embedded in tool results.

---

## Feature matrix: capabilities vs deployment mode

| Capability | stdio (local) | HTTP stateful `2025-11-25` | HTTP stateless `2025-11-25` | HTTP `2026-07-28` |
| --- | --- | --- | --- | --- |
| **tools/call** | Yes | Yes | Yes | Yes |
| **resources/read** | Yes | Yes | Yes | Yes |
| **notifications/progress** | Yes | Yes (POST SSE + GET) | Yes (POST SSE only) | Yes (POST SSE only) |
| **Sampling (`sampling/createMessage`)** | Yes | Yes (server request on SSE) | No standalone channel | MRTR **`InputRequiredResult`** |
| **Elicitation** | Yes | Yes | Limited | MRTR |
| **Roots** | Yes | Yes | Limited | MRTR |
| **`tools/list_changed` push** | Yes | Yes (GET stream) | No | **`subscriptions/listen`** |
| **Multi-instance LB** | N/A (1 process) | Needs affinity or store | Easy | Easy |
| **Cold start friendly** | N/A | Poor | Good | Good |

**stdio note:** Local subprocess transport is **connection-stateful** for the life of the process —
one stdin/stdout pair — but that is a **single user machine**, not a fleet scaling problem.

---

## Streamable HTTP: one endpoint, three verbs (`2025-11-25`)

The server exposes a **single URL** (the **MCP endpoint**), e.g. `https://api.example.com/mcp`.

```text
                    ┌─────────────────────────────────────┐
                    │         MCP endpoint /mcp           │
                    │  Methods: POST (required)           │
                    │           GET  (optional, stateful) │
                    │           DELETE (optional)         │
                    └─────────────────────────────────────┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          ▼                           ▼                           ▼
    JSON-RPC over POST          SSE server push            Session teardown
    (every client message)      (server-initiated)         (client cleanup)
```

### Security baseline (all revisions)

From the MCP transport spec:

1. Validate **`Origin`** on HTTP connections; invalid → **403** (mitigates DNS rebinding).
2. Local dev servers **SHOULD** bind **127.0.0.1**, not `0.0.0.0`, unless intentional.
3. **Authentication** (OAuth 2.1 bearer tokens for remote servers) is independent of session IDs.

Never treat **`MCP-Session-Id`** as proof of identity — it is a **routing / correlation** key.

---

## POST: client → server (both eras)

Every client JSON-RPC message is a **new HTTP POST** (not a WebSocket upgrade).

**Client requirements (`2025-11-25`):**

1. **`Accept: application/json, text/event-stream`** — must handle either response type.
2. Body: exactly **one** JSON-RPC request, notification, or response.
3. After initialization, include negotiated **`MCP-Protocol-Version`** header on requests.
4. If server returned **`MCP-Session-Id`** at init, send it on **all** subsequent requests.

**Server behaviour for POST body types:**

| Body type | Success response | Notes |
| --- | --- | --- |
| JSON-RPC **notification** | **202 Accepted**, empty body | e.g. `notifications/initialized` |
| JSON-RPC **response** | **202 Accepted** | Client answering server-initiated RPC |
| JSON-RPC **request** | **200** + JSON **or** SSE stream | Server chooses stream for long work |

When the server opens SSE on POST:

- It **MAY** send JSON-RPC requests/notifications **before** the final JSON-RPC response for that
  client request (progress, related messages).
- It **SHOULD** terminate the SSE stream after the final response.
- It **MAY** prime the stream with an empty SSE event + **event id** so clients can reconnect
  (`Last-Event-ID` flow).

**`2026-07-28` additions:** **`Mcp-Method`**, **`Mcp-Name`**, and related metadata headers mirror
JSON-RPC fields for routing at proxies. Client JSON-RPC **responses** over HTTP are disallowed in the
modern revision — MRTR carries replies inside retried requests.

---

## GET: server → client channel (`2025-11-25`, stateful)

Optional but **essential for full MCP feature parity** in the session era:

1. Client **MAY** `GET /mcp` with **`Accept: text/event-stream`** and session header.
2. Server returns SSE **or** **405** if it does not offer a standalone stream.
3. Server **MAY** send JSON-RPC **requests** and **notifications** unrelated to an in-flight POST.
4. Server **MUST NOT** send a JSON-RPC **response** on GET except when **resuming** a prior stream.

```text
  Client                                 Server
    │  POST tools/call (long job)  ───────►│
    │◄──── SSE: progress notifications ────│  (same POST response)
    │                                      │
    │  GET /mcp  (parallel)  ─────────────►│  dedicated push lane
    │◄──── SSE: sampling/createMessage ────│  server asks host LLM
    │  POST (JSON-RPC response)  ─────────►│  client answers on POST
    │◄──── 202 Accepted ───────────────────│
    │◄──── POST stream: final result ──────│
```

**Why GET existed:** HTTP POST is client-initiated. Without GET, the server cannot push
**asynchronous** server→client RPC while the client is idle — only while a POST response stream is
open. Production hosts (Cursor, Claude Desktop) often keep GET open for **`list_changed`** and
**resource updated** notifications.

**`2026-07-28`:** standalone GET removed. Long-lived notifications use **`subscriptions/listen`**;
sampling/elicitation uses **MRTR** on the tool call POST stream.

---

## DELETE: session termination (`2025-11-25`)

When the client no longer needs a session:

- **`DELETE /mcp`** with **`MCP-Session-Id`** header.
- Server **MAY** respond **405** if it does not allow client-initiated teardown (server still **MAY**
  expire sessions server-side → **404** on later POST).

Client **MUST** on **404** with session header: start fresh **`initialize`** without session id.

```text
  User closes IDE tab
        │
        ▼
  Host sends DELETE /mcp  +  MCP-Session-Id: 7f3c…
        │
        ▼
  Server drops transport, closes GET SSE, frees sandbox memory
```

---

## Session IDs: rules, lifecycle, and errors

Spec text (**`2025-11-25`**, Streamable HTTP — Session Management):

1. Server **MAY** assign session ID in response to **`initialize`**, header **`MCP-Session-Id`** on
   the HTTP response containing **`InitializeResult`**.
2. ID **SHOULD** be globally unique and cryptographically secure (UUID, signed JWT, hash).
3. ID **MUST** use visible ASCII only (0x21–0x7E).
4. Client **MUST** send header on all subsequent HTTP requests if server issued one.
5. Server requiring sessions **SHOULD** return **400** on non-init requests **without** header.
6. Server **MAY** terminate anytime → **404** for that session id thereafter.
7. Client on **404** **MUST** re-**initialize** without session header.

```text
  Session lifecycle (happy path)
  ───────────────────────────────

  [none] ──initialize──► ACTIVE ──DELETE──► TERMINATED
              │                              ▲
              │ idle timeout / deploy        │
              └──────────404─────────────────┘
```

### HTTP status cheat sheet (legacy session servers)

| Situation | Status | Client action |
| --- | --- | --- |
| Missing session on protected call | **400** | Re-send with stored session id |
| Unknown / expired session | **404** | New `initialize` |
| Valid notification | **202** | Continue |
| Origin attack | **403** | Fix client / server config |
| Server at capacity | **503** (app convention) | Backoff, retry init |

### Session hijacking mitigations

If an attacker steals **`MCP-Session-Id`**, they can impersonate that MCP session on the wire.
Mitigations from MCP security guidance:

- Always use **HTTPS**; mark cookies **Secure** if sessions ever overlap with browser cookies.
- Bind sessions to **OAuth subject** server-side — reject session id if bearer token sub mismatches.
- Rotate or invalidate sessions on **token refresh** or **logout**.
- Do not expose session ids in URLs or logs at **info** level in multi-tenant systems.

---

## Resumability: Last-Event-ID (`2025-11-25`)

SSE events may carry **`id:`** fields. Clients reconnect with:

```http
GET /mcp HTTP/1.1
Accept: text/event-stream
MCP-Session-Id: 7f3c9a2e-…
Last-Event-ID: evt-00042
```

Server **MAY** replay messages after that id **on the same logical stream**. Event ids are **per-stream
cursors**, not global broadcast offsets.

Servers implement replay with an **event store** (in-memory ring buffer, Redis streams, SQL table).
The TypeScript SDK documents a pluggable event store for this purpose — verify API names in your
pinned SDK version.

**Operational cost:** replay buffers consume memory; cap retention and document max gap before clients
must full re-init.

**`2026-07-28`:** **`Last-Event-ID`** resumption **not supported** — design for idempotent retries at
the tool layer instead.

---

## HTTP traces: stateful initialization and tool call

### Trace A — Initialize and session minting

```http
POST /mcp HTTP/1.1
Host: mcp.example.com
Content-Type: application/json
Accept: application/json, text/event-stream
Authorization: Bearer eyJhbG…

{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
  "protocolVersion":"2025-11-25",
  "capabilities":{},
  "clientInfo":{"name":"study-host","version":"1.0.0"}
}}
```

```http
HTTP/1.1 200 OK
Content-Type: application/json
MCP-Session-Id: 1868a90c-4b2d-4f1a-9c3e-1d8f6a2b4c5d
MCP-Protocol-Version: 2025-11-25

{"jsonrpc":"2.0","id":1,"result":{
  "protocolVersion":"2025-11-25",
  "capabilities":{"tools":{"listChanged":true}},
  "serverInfo":{"name":"orders-mcp","version":"2.1.0"}
}}
```

```http
POST /mcp HTTP/1.1
MCP-Session-Id: 1868a90c-4b2d-4f1a-9c3e-1d8f6a2b4c5d
Content-Type: application/json
Accept: application/json, text/event-stream

{"jsonrpc":"2.0","method":"notifications/initialized"}
```

```http
HTTP/1.1 202 Accepted
```

### Trace B — Open GET notification stream

```http
GET /mcp HTTP/1.1
Accept: text/event-stream
MCP-Session-Id: 1868a90c-4b2d-4f1a-9c3e-1d8f6a2b4c5d
```

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache
X-Accel-Buffering: no

id: evt-00001
data:

id: evt-00002
data: {"jsonrpc":"2.0","method":"notifications/tools/list_changed"}
```

### Trace C — tools/call with progress on POST SSE

```http
POST /mcp HTTP/1.1
MCP-Session-Id: 1868a90c-…
Accept: application/json, text/event-stream
Content-Type: application/json

{"jsonrpc":"2.0","id":42,"method":"tools/call","params":{
  "name":"reindex_repository",
  "arguments":{"repo":"acme/web"}
}}
```

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream

id: evt-100
data: {"jsonrpc":"2.0","method":"notifications/progress","params":{
  "progressToken":"tok-9","progress":0.25,"total":1.0,"message":"Scanning"
}}

id: evt-101
data: {"jsonrpc":"2.0","id":42,"result":{
  "content":[{"type":"text","text":"Indexed 12400 files"}]
}}
```

### Trace D — Stateless: single POST tool call (no session header)

```http
POST /mcp HTTP/1.1
Accept: application/json, text/event-stream
Content-Type: application/json
Authorization: Bearer eyJhbG…

{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{
  "name":"get_order","arguments":{"order_id":"ORD-10293"}
}}
```

```http
HTTP/1.1 200 OK
Content-Type: application/json

{"jsonrpc":"2.0","id":7,"result":{
  "content":[{"type":"text","text":"{"status":"SHIPPED"}"}]
}}
```

### Trace E — Session teardown

```http
DELETE /mcp HTTP/1.1
MCP-Session-Id: 1868a90c-4b2d-4f1a-9c3e-1d8f6a2b4c5d
```

```http
HTTP/1.1 200 OK
```

---

## What stateful sessions buy you (and what they cost)

### Benefits

- **Server-initiated RPC** during idle periods (GET stream) — sampling, elicitation, roots in the
  legacy model.
- **Push notifications** — tool list changes, resource subscriptions without polling.
- **Convenient hiding place** for handles — open Playwright browser, JDBC connection, temp directory
  keyed only by session, not passed through the model.
- **Ordered conversational context** inside the server object — caches of prior tool results per
  session (use carefully; prefer explicit stores for auditability).

### Costs

- **Memory per session** — idle sessions hold transports, goroutines, browser instances. Cap
  **`MAX_SESSIONS`** and idle TTL (TypeScript SDK examples use timers → **404**).
- **Sticky load balancing** — header-based affinity or centralized session registry.
- **Deploy fragility** — rolling restart kills in-process sessions unless drained or externalized.
- **Debugging complexity** — correlate POST + GET streams + session id in logs.

### When stateless wins

- Tools are **pure functions** of arguments + bearer token identity.
- Data lives in **Postgres / S3 / Redis** with explicit keys the model passes back.
- You need **autoscaling** and **multi-region** without affinity tables.
- You run on **Lambda** (15 min max), **Cloud Run** (request-bound), or **Workers** (CPU limits).

**Default recommendation:** start **stateless at the HTTP layer** (`2026-07-28` or
`sessionIdGenerator: undefined`). Add **explicit domain state** before adding protocol sessions.

---

## TypeScript: stateful Streamable HTTP server

Pattern from **`@modelcontextprotocol/sdk`** (verify imports for your pinned version — v1/v2 differ).
This mirrors official **sessions-state-scaling** documentation for the **`2025-11-25`** transport.

```ts
import express from "express";
import { randomUUID } from "node:crypto";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";

const app = express();
app.use(express.json({ limit: "4mb" }));

const transports = new Map<string, StreamableHTTPServerTransport>();
const MAX_SESSIONS = 10_000;
const IDLE_MS = 30 * 60 * 1000;

function buildServer(): McpServer {
  const server = new McpServer({ name: "demo-stateful", version: "1.0.0" });
  // register tools, resources, prompts …
  return server;
}

app.post("/mcp", async (req, res) => {
  const sessionId = req.headers["mcp-session-id"] as string | undefined;
  let transport = sessionId ? transports.get(sessionId) : undefined;

  if (!transport) {
    if (sessionId || !isInitializeRequest(req.body)) {
      res.status(sessionId ? 404 : 400).json({
        jsonrpc: "2.0",
        error: { code: -32000, message: "No valid session" },
        id: null,
      });
      return;
    }
    if (transports.size >= MAX_SESSIONS) {
      res.status(503).json({
        jsonrpc: "2.0",
        error: { code: -32000, message: "Server at session capacity" },
        id: null,
      });
      return;
    }
    transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: () => randomUUID(),
      onsessioninitialized: (id) => {
        transports.set(id, transport!);
      },
    });
    const idle = setTimeout(() => transport?.close(), IDLE_MS);
    transport.onclose = () => {
      clearTimeout(idle);
      const sid = transport!.sessionId;
      if (sid) transports.delete(sid);
    };
    await buildServer().connect(transport);
  }

  await transport.handleRequest(req, res, req.body);
});

async function bySession(req: express.Request, res: express.Response) {
  const sessionId = req.headers["mcp-session-id"] as string | undefined;
  if (!sessionId) {
    res.status(400).send("Missing MCP-Session-Id");
    return;
  }
  const transport = transports.get(sessionId);
  if (!transport) {
    res.status(404).send("Unknown session");
    return;
  }
  await transport.handleRequest(req, res);
}

app.get("/mcp", bySession);
app.delete("/mcp", bySession);

app.listen(3000, "127.0.0.1");
```

**Key invariant:** one **`StreamableHTTPServerTransport`** instance per session; route **POST / GET /
DELETE** sharing the same **`mcp-session-id`** to that instance.

---

## TypeScript: stateless Streamable HTTP server

```ts
app.post("/mcp", async (req, res) => {
  const server = buildServer();
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: undefined,
    enableJsonResponse: true,
  });
  res.on("close", () => {
    transport.close();
    server.close();
  });
  await server.connect(transport);
  await transport.handleRequest(req, res, req.body);
});

app.get("/mcp", (_req, res) => res.status(405).end());
app.delete("/mcp", (_req, res) => res.status(405).end());
```

**Performance:** constructing `McpServer` per request adds CPU. Production pattern:

- Register tool handlers **once** in a factory reading shared **`DbPool`**, **`Redis`**, config.
- Avoid reconnecting to upstream APIs on every POST — use connection pools created at module load.

---

## Python: stateful vs stateless (FastMCP)

Python **`mcp`** package **FastMCP** exposes HTTP mode flags (check pinned docs — names stabilized
around **`stateless_http`** and **`json_response`**):

```python
# Stateless — good for Lambda-style deploys
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("orders", stateless_http=True, json_response=True)

@mcp.tool()
def get_order(order_id: str) -> dict:
    """Fetch order by business id."""
    return db.fetch_order(order_id)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

```python
# Stateful session mode (legacy transport semantics) — enables full notification patterns
# when host speaks 2025-11-25 and SDK session generator is enabled.
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("browser-automation", stateless_http=False)

@mcp.tool()
async def open_page(url: str, ctx) -> str:
    """Uses session-scoped browser stored on server instance."""
    browser = ctx.request_context.session_data["browser"]
    page = await browser.new_page()
    await page.goto(url)
    return await page.title()

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
```

For **`2026-07-28`**, prefer **`stateless_http=True`** and pass **`browser_id`** explicitly between
tool calls backed by Redis, rather than hiding browsers in a protocol session.

---

## Scaling stateful sessions

### Problem statement

```text
         ┌──────── LB ────────┐
         │                    │
    ┌────▼────┐          ┌────▼────┐
    │ Pod A   │          │ Pod B   │
    │ sess-1  │          │ sess-2  │
    └─────────┘          └─────────┘

  POST with MCP-Session-Id: sess-1  ──must──► Pod A only
```

If Pod B receives `sess-1`, it returns **404** → host re-inits → user-visible glitch or lost handles.

### Option 1 — Sticky sessions (session affinity)

Configure load balancer on **`MCP-Session-Id`** (or cookie derived at init time).

| Pros | Cons |
| --- | --- |
| Simple app code (in-memory map) | Uneven load; hot pods |
| Low latency (no remote session fetch) | Pod loss kills sessions on that pod |
| Works with GET long-poll | Hard with autoscale scale-to-zero |

**NGINX example (illustrative — test in staging):**

```nginx
upstream mcp_backend {
    hash $http_mcp_session_id consistent;
    server 10.0.0.11:3000;
    server 10.0.0.12:3000;
}
```

Also set **`proxy_buffering off`** and **`proxy_read_timeout`** high for SSE GET streams.

### Option 2 — Shared session store (any pod serves any session)

Store session metadata + serialized handles in **Redis** / **DynamoDB**:

```text
  Key: mcp:session:{uuid}
  Fields: oauth_sub, created_at, pod_id (optional leader), serialized_handle_refs
```

Only one pod should own **live** resources (browser). Patterns:

- **Leader pod** field + redirect (complex).
- **Externalize the expensive state** (browser in Browserless.io; session store only IDs).
- **Pub/sub** for SSE fan-out (Redis streams → all pods subscribed to session channel).

Event **replay** for SSE uses a shared **event store** (Redis stream keyed by session + stream id).

### Option 3 — Drain on deploy

1. Mark pod **not ready** (stop new inits).
2. Wait **`IDLE_MS`** or client DELETEs.
3. Kill pod.

Kubernetes: **`preStop`** hook + **`terminationGracePeriodSeconds`** ≥ longest tool call.

### Capacity planning numbers (rules of thumb)

| Resource | Order of magnitude |
| --- | --- |
| Idle session (transport only) | ~ tens of KB RAM + one SSE socket |
| Headful browser session | ~ 200–500+ MB RAM |
| SSE connections per pod | Watch **ulimit** / ENI limits (often 10k+ theoretical, far less practical) |

Enforce **`MAX_SESSIONS`** and per-user quotas at **`initialize`** — the SDK pattern returns **503**.

---

## Serverless and stateless deployment

### AWS Lambda / Function URL

- One invocation = one POST handler; **no GET** listener for legacy push.
- **`sessionIdGenerator: undefined`**; **`enableJsonResponse: true`** to avoid SSE on Lambda unless
  using **`response streaming`** (API Gateway/Lambda response streaming — verify current AWS feature).
- Keep tool execution **< timeout**; offload long work to **SQS + webhook** returning job id.

```text
  tools/call start_export ──► { "job_id": "job-88", "status": "QUEUED" }
  tools/call get_export_status(job_id) ──► polled by model/host
```

### Cloud Run / Knative

- Request-scoped instances scale well for stateless POST.
- Legacy **GET SSE** needs **`min-instances ≥ 1`** or users lose push channel when scaled to zero.

### Cloudflare Workers

- CPU/time limits; no long-lived in-memory sessions.
- Stateless tools + **Durable Objects** if you need strongly consistent per-user state (explicit, not
  MCP protocol session).

### Fly.io / Render

- Stateful possible on single machine; for multi-region use **hybrid explicit state** in Postgres.

---

## Hybrid: stateless transport, explicit domain state

Recommended production pattern for multi-step workflows without protocol sessions:

```text
  Step 1   tools/call create_sandbox
           ◄── { "sandbox_id": "sb_81f2", "expires_at": "…" }

  Step 2   tools/call run_command(sandbox_id, cmd)
           ◄── stdout/stderr

  Step 3   tools/call destroy_sandbox(sandbox_id)
```

**Storage:**

```text
  Redis:  sandbox:sb_81f2  →  { worker_url, ttl, owner_sub }
  or
  Postgres: sandboxes table with row-level security on owner_sub
```

**Why this beats hidden session state:**

| Concern | Hidden session | Explicit `sandbox_id` |
| --- | --- | --- |
| Any pod serves call | No | Yes |
| Model can retry with same id | Opaque | Yes — id in chat transcript |
| Audit / support | Hard | Log id per tool call |
| Survives host reconnect | If session alive | If TTL not expired |

Same pattern for **shopping carts**, **ETL jobs**, **draft documents**, **multi-step approvals**.

---

## Decision flowchart

```text
                        Start: remote MCP over HTTP
                                    │
                                    ▼
               Host/protocol version negotiated?
                                    │
            ┌───────────────────────┴───────────────────────┐
            ▼                                               ▼
     2026-07-28 or later                              2025-11-25 legacy
            │                                               │
            ▼                                               ▼
   Protocol session N/A                          Need GET push or session-
   Use MRTR + subscriptions/listen               scoped resources?
   for interactive / push features                         │
            │                                    ┌──────────┴──────────┐
            │                                    ▼                     ▼
            │                                   yes                   no
            │                                    │                     │
            │                                    ▼                     ▼
            │                          Stateful session +          Stateless POST
            │                          affinity or Redis            + explicit IDs
            │                                    │                     │
            └────────────────────────────────────┴─────────────────────┘
                                    │
                                    ▼
              Do tools mutate external systems?
                                    │
                         ┌──────────┴──────────┐
                         ▼                     ▼
                    idempotent              side effects
                    reads OK                require OAuth scopes,
                                            approval in host, audit logs
```

---

## 2026-07-28: protocol-stateless but operationally stateful

Even without **`MCP-Session-Id`**, servers remain **stateful at the application layer**:

- **OAuth token caches** keyed by client id + subject.
- **`subscriptions/listen`** SSE stream open for minutes — ties to that HTTP connection, not a
  session header.
- **MRTR** tool calls may span **multiple POST round trips** with the **same logical tool invocation**
  — host retries with **`inputResponses`**.

Load balancers can use **round robin** for MRTR **if** each POST is self-contained and server stores
intermediate MRTR state in **Redis** keyed by **`progressToken` / request id** from `_meta`.

Modern-only servers receiving legacy traffic (**spec guidance**):

| Legacy client behaviour | Modern-only server response |
| --- | --- |
| GET / DELETE on MCP endpoint | **405 Method Not Allowed** |
| **`MCP-Session-Id`** header | Ignore; do not mint |
| **`Last-Event-ID`** | Ignore |

Support **version negotiation** during init (legacy) or **`MCP-Protocol-Version`** header (modern).

---

## Observability and operations

| Signal | Stateful legacy | Stateless |
| --- | --- | --- |
| **Correlation id** | `MCP-Session-Id` + JSON-RPC `id` | Request id / trace id per POST |
| **Metrics** | `mcp_sessions_active`, `mcp_session_age_seconds` | `mcp_post_duration`, `mcp_tool_errors` |
| **Alerts** | Session count near `MAX_SESSIONS` | p99 latency, 5xx rate |
| **Logs** | Never log bearer tokens; session id ok at debug | Log tool name + business ids |

**Proxies:** SSE requires **`X-Accel-Buffering: no`**, long **`proxy_read_timeout`**, comment-line
keep-alives on quiet streams (`2026-07-28` spec encourages `:` comment lines).

**Cancellation:**

- **`2025-11-25`:** client sends **`notifications/cancelled`**; disconnect ≠ cancel.
- **`2026-07-28`:** closing the **response SSE stream** **is** the cancel signal for that request.

---

## Migration checklist: sessions → explicit state + MRTR

1. Inventory tools that relied on **in-session** objects (browser, temp files).
2. Add **explicit id** parameters + TTL-backed store.
3. Replace GET-push **`list_changed`** with **`subscriptions/listen`** (2026 hosts).
4. Turn off **`sessionIdGenerator`**; return **405** on GET/DELETE if safe.
5. Load-test **MRTR** latency (extra POST round trips for sampling).
6. Document **404 session** → re-init behaviour for legacy clients during transition.

---

## Comparison to HTTP+SSE transport (2024-11-05, deprecated)

Older MCP used **two URLs** — SSE stream for server→client plus separate POST endpoint discovered via
SSE `endpoint` event. Streamable HTTP unified to **one MCP endpoint** (`2025-03-26+`), reducing
configuration errors. Backward-compatible clients probe POST init first, fall back to legacy dual
endpoint pattern.

When reading old blog posts, translate "SSE endpoint + messages endpoint" → modern **`/mcp`**.

---

## Cost model (rough)

Assume 1000 concurrent legacy sessions, 50 KB RAM each transport overhead → ~50 MB plus baseline —
 manageable. 1000 headful browsers → **hundreds of GB** — unacceptable without external browser
farm. Explicit **`browser_id`** + worker pool isolates cost.

---

## Deep dive: POST response mode selection

Servers choose JSON vs SSE per request. Understanding **why** helps you tune latency and proxy
behaviour.

| Server choice | When it fits | Client expectation |
| --- | --- | --- |
| **`application/json`** | Fast tools (< few hundred ms), stateless CRUD | Simple HTTP client stacks |
| **`text/event-stream`** | Long tools, progress notifications, legacy inline server→client messages | Must parse SSE until final `id` response |

**Anti-pattern:** always streaming SSE — intermediaries hold connections longer, costing file
descriptors; JSON is cheaper for `get_weather`-class tools.

**Stateful nuance:** even with GET open, POST may still stream progress for **that specific request**
while unrelated notifications ride GET — do not duplicate the same notification on both unless your
host expects it (most do not).

---

## Deep dive: host-side behaviour you must expect

MCP **hosts** (Cursor, Claude Desktop, custom agents) differ:

- Some always open **GET** after init when speaking `2025-11-25`.
- Some retry **`initialize`** on any **404** session — your server should idempotently accept init.
- Hosts enforce **tool approval** before **`tools/call`** — session state does not bypass policy.
- On **`2026-07-28`**, hosts maintain **`subscriptions/listen`** in parallel with tool POSTs.

Server authors should test with **MCP Inspector** and at least one real host before production.

---

## Deep dive: mapping SDK objects to HTTP sessions

In **`@modelcontextprotocol/sdk`** legacy HTTP:

```text
  initialize POST
       │
       ▼
  new StreamableHTTPServerTransport({ sessionIdGenerator })
       │
       ├── onsessioninitialized(id) ──► app-level Map id → transport
       │
       ▼
  connect(McpServer)  ── often ONE server instance per session
```

**Stateless:**

```text
  each POST ──► new Transport (no generator) + new McpServer ──► handle ──► close
```

The **`McpServer`** holds registered tools; expensive shared clients (DB pool) should be **module
scope**, not constructed inside **`buildServer()`** per tool registration if that rebuilds pools.

---

## Feature flag: dual-protocol server

Production migration pattern:

```text
  if MCP-Protocol-Version >= 2026-07-28:
      post_only_stateless_core()
  elif session_id_generator_enabled:
      legacy_session_post_get_delete()
  else:
      legacy_stateless_post()
```

Run shadow traffic comparing latency MRTR vs legacy GET sampling before cutover.

---

## Glossary

| Term | Meaning |
| --- | --- |
| **MCP endpoint** | Single HTTP path supporting Streamable HTTP verbs |
| **Protocol session** | `2025-11-25` session id + paired transport (removed 2026) |
| **MRTR** | Multi-round-trip requests — embed client input in retried tool POST |
| **Affinity** | Load balancer routes same session header to same backend |
| **Event store** | Persistence for SSE replay (`Last-Event-ID`) |
| **Host** | User-facing app embedding MCP client |
| **Explicit state** | Business ids in tool arguments + external store |

---

## Interview rapid-fire (short answers)

| Question | Answer |
| --- | --- |
| Stateless means no server memory? | No — means no protocol session; DB still stateful |
| GET required? | Only legacy push; optional (405 if absent) |
| DELETE required? | Optional polite teardown |
| Header name case? | HTTP case-insensitive; spec shows `MCP-Session-Id` |
| stdio stateful? | Process lifetime connection state |
| Best default? | Stateless HTTP + explicit ids (`2026` aligned) |

## Multi-tenant SaaS considerations

Running one MCP server for many customers:

1. **Tenant id** from OAuth **`sub`** / **`org_id`** claim — never from tool args alone.
2. Session maps keyed **`(tenant, session_id)`**; reject cross-tenant session ids.
3. Rate limit **`initialize`** per tenant to prevent session exhaustion DoS.
4. Separate **`MAX_SESSIONS`** per tier (free vs enterprise).

Stateless tools simplify tenancy — each POST carries bearer proving tenant; query DB with RLS.

---

## Pitfalls: SSE and HTTP/2

Some clients multiplex SSE GET + POST on HTTP/2 to same host. Ensure your framework does not block
GET handler thread pool with POST work — use separate worker pools or async I/O (Node event loop,
Python asyncio).

---

## Redis-backed session registry (sketch)

```text
  POST /mcp initialize on Pod B (new session)
       │
       ▼
  SET mcp:sess:{id} = { pod: "B", sub: "user-1", at: ts } EX 1800
  SADD mcp:pod:B:sessions {id}
       │
  Later POST on Pod A
       │
       ▼
  GET mcp:sess:{id} ──► pod B ──► internal redirect or gRPC forward to B
```

Forwarding is harder than stickiness — many teams stick first, refactor to explicit state later.

---

## Reference: JSON-RPC error shapes on HTTP failures

Transport errors may still wrap JSON-RPC:

```json
{
  "jsonrpc": "2.0",
  "id": null,
  "error": { "code": -32000, "message": "No valid session" }
}
```

Distinguish:

- **HTTP 404** — session routing (legacy).
- **JSON-RPC -32601** — method not found (2026 may use HTTP 404 with this code per modern spec).
- **JSON-RPC -32602** — invalid params on tool arguments.

Log both HTTP status and JSON-RPC error code for support tickets.

---

## Reference: initialize and capability negotiation (session era)

Before any session id is minted, client and server negotiate **`protocolVersion`** in the
**`initialize`** exchange. The agreed version governs whether **`MCP-Session-Id`** semantics apply.

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2025-11-25",
    "capabilities": {
      "roots": { "listChanged": true },
      "sampling": {}
    },
    "clientInfo": { "name": "example-host", "version": "3.2.0" }
  }
}
```

Server **`InitializeResult`** echoes **`protocolVersion`** (possibly downgraded), advertises
**`capabilities`**, and returns **`serverInfo`**. Only after **`notifications/initialized`** may
tools be invoked (lifecycle rules in MCP basic lifecycle spec).

If client proposes **`2026-07-28`** but server only implements **`2025-11-25`**, negotiation fails or
downgrades — your deployment must document supported versions explicitly in **`serverInfo`** and
monitor **`400`** on version mismatch.

---

## Serverless stateless: API Gateway + Lambda sketch

```text
  Client POST /mcp
       │
       ▼
  API Gateway (JWT authorizer validates OAuth)
       │
       ▼
  Lambda: stateless handler
       - parse tools/call
       - query RDS / DynamoDB
       - return JSON (enableJsonResponse)
```

Avoid **`initialize`** session maps in Lambda globals — globals do not survive across concurrent
execution environments reliably for session affinity.

---

## Sticky sessions: ALB and Envoy notes

**AWS ALB:** use target group stickiness on application cookie or custom header — ALB historically
sticks on cookie; for **`MCP-Session-Id`**, you may set a **`Set-Cookie`** at init mapping to session
or use **NLB + Envoy** with hash policy.

**Envoy:**

```yaml
# Illustrative route hash policy on header
hash_policy:
  - header:
      header_name: mcp-session-id
```

Validate failover: when backend dies, clients see **404** → re-init storm — mitigate with connection
draining and sufficient **`MAX_SESSIONS`** headroom on remaining pods.

---

## Testing matrix (manual)

| Test | Stateful legacy | Stateless |
| --- | --- | --- |
| Init + initialized notification | Required sequence | Same |
| Tool call without session | 400 after init | 200 |
| Unknown session id | 404 | N/A |
| GET without session | 400/404 | 405 |
| DELETE cleanup | 200/405 | 405 |
| Parallel tool calls same session | Same transport serializes JSON-RPC | Each POST independent — watch race on shared external store |

Automate with **`curl`** traces in CI for regression; use Inspector for JSON-RPC semantics.

---

## Worked example: explicit state for browser automation

**Problem:** Playwright browser must survive multiple tool calls.

**Legacy (session-bound):**

```text
  initialize ──► session S ──► browser launched once, stored on transport
  tools/call navigate(url)
  tools/call screenshot()
```

**Hybrid (preferred):**

```python
# Pseudocode — store in Redis, not protocol session
def start_browser() -> dict:
    bid = f"br_{secrets.token_urlsafe(8)}"
    redis.hset(f"browser:{bid}", mapping={"owner": current_sub(), "created": time.time()})
    queue.publish(worker_channel(bid), "launch")
    return {"browser_id": bid, "ttl_seconds": 900}

def screenshot(browser_id: str) -> bytes:
    assert redis.hget(f"browser:{browser_id}", "owner") == current_sub()
    return workers.rpc(browser_id, "screenshot")
```

Any stateless HTTP POST can serve **`screenshot`** if **`browser_id`** is valid — no stickiness.

---

## Worked example: progress + cancellation

Long **`reindex`** job sends **`notifications/progress`**. Client disconnects mid-stream.

| Era | Cancel signal |
| --- | --- |
| `2025-11-25` | Explicit **`notifications/cancelled`** — disconnect alone is ambiguous |
| `2026-07-28` | Close SSE response stream → server treats as cancel |

Implement cooperative cancellation: check **`AbortSignal`** in tool handler loop; stop CPU work.

---

---

## MRTR vs legacy GET sampling (sequence)

Under **`2026-07-28`**, when a tool needs the host LLM, the server does **not** open a parallel server
JSON-RPC request on GET. Instead the **`tools/call`** POST response (often SSE) eventually returns a
result whose structure tells the host to supply input, then **retry** the same logical call.

```text
  Host                                    MCP server
    │  POST tools/call (run_analysis)  ────────►│
    │◄── SSE: notifications/progress ───────────│
    │◄── result: InputRequiredResult ───────────│
    │       inputRequests: [ sampling … ]       │
    │  POST tools/call (same) + inputResponses ►│
    │◄── final ToolResult ──────────────────────│
```

**Scaling implication:** each MRTR round trip is a normal POST — no session stickiness. Store partial
tool state in your database keyed by progress tokens or a server-generated **`job_id`** returned in
the first partial result.

Legacy **`2025-11-25`** used **`sampling/createMessage`** as a JSON-RPC **request** on the GET stream
(or POST SSE), answered by a client POST carrying JSON-RPC **response** → **202**.

---

## HTTP trace: sampling via legacy GET stream

```http
GET /mcp HTTP/1.1
Accept: text/event-stream
MCP-Session-Id: 1868a90c-4b2d-4f1a-9c3e-1d8f6a2b4c5d
```

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream

id: evt-s1
data: {"jsonrpc":"2.0","id":"srv-9","method":"sampling/createMessage","params":{
  "messages":[{"role":"user","content":{"type":"text","text":"Summarize logs"}}],
  "maxTokens":512
}}
```

```http
POST /mcp HTTP/1.1
MCP-Session-Id: 1868a90c-4b2d-4f1a-9c3e-1d8f6a2b4c5d
Content-Type: application/json

{"jsonrpc":"2.0","id":"srv-9","result":{
  "role":"assistant",
  "content":{"type":"text","text":"Three errors in auth service …"}
}}
```

```http
HTTP/1.1 202 Accepted
```

---

## HTTP trace: resumable GET after disconnect

```http
GET /mcp HTTP/1.1
Accept: text/event-stream
MCP-Session-Id: 1868a90c-…
Last-Event-ID: evt-00042
```

Server replays events with id greater than the cursor on **that** stream only, then continues live.

---

## subscriptions/listen trace (`2026-07-28`)

```http
POST /mcp HTTP/1.1
Accept: application/json, text/event-stream
MCP-Protocol-Version: 2026-07-28
Mcp-Method: subscriptions/listen
Content-Type: application/json

{"jsonrpc":"2.0","id":50,"method":"subscriptions/listen","params":{
  "subscriptions":[{"type":"tools/list_changed"}],
  "_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28"}
}}
```

```http
HTTP/1.1 200 OK
Content-Type: text/event-stream
X-Accel-Buffering: no

: keep-alive

data: {"jsonrpc":"2.0","method":"notifications/tools/list_changed"}
```

This replaces legacy **GET** for tool-list push — long-lived SSE on a **subscription POST**, not a
session header.

---

## Python: Starlette stateless outline

```python
# Illustrative — verify against pinned mcp Python SDK APIs
from starlette.applications import Starlette
from starlette.routing import Route

async def mcp_post(request):
    body = await request.json()
    # Build server + Streamable HTTP transport with stateless_http=True
    ...

app = Starlette(routes=[Route("/mcp", mcp_post, methods=["POST"])])
```

---

## Failure modes catalogue

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Host never sees tool updates | No GET (legacy) or no `subscriptions/listen` (2026) | Enable push channel |
| 404 after deploy | In-memory sessions lost | Drain + explicit state |
| SSE stalls behind nginx | Buffering on | `X-Accel-Buffering: no` |
| Duplicate tool side effects | Stateless POST retried | Idempotency keys |
| 400 after init | Missing session header | Fix client or proxy |
| MRTR loop stuck | Host not sending `inputResponses` | Fix capabilities |

---

## Load test starting points

| Profile | Legacy sessions | POST RPS | GET/`listen` streams |
| --- | --- | --- | --- |
| Dev | 10 | 5 | 10 |
| Staging | 500 | 100 | 500 |
| Stateless prod | 0 | 500 | 0 |

Watch **file descriptor** limits: legacy ≈ one GET per session.

---

## Design exercise: warehouse SQL MCP

Use stateless POST + **`query_id`** and polling, or SSE progress on one POST — avoid session-scoped
JDBC **`ResultSet`** objects in memory.

---

## Design exercise: elicitation wizards

Modern MRTR lets hosts render forms without legacy GET **`elicitation/create`** push — enables
stateless deploy for multi-step user input.

---

## Integration checklist

1. Document supported protocol version(s).
2. Origin validation + OAuth on HTTP.
3. Prefer stateless HTTP + explicit domain ids.
4. TTL external stores; idempotent mutating tools.
5. Proxy tuning for SSE.
6. Metrics and curl/Inspector CI traces.
7. Plan migration off **`MCP-Session-Id`** when hosts speak **`2026-07-28`**.

---

---

---

## Operational deep dive: Regional failover for legacy stateful clusters

When a whole region fails, **`Mcp-Session-Id`** values stored only in that region's Redis are lost. Clients receive **404** and must **`initialize`** again, losing in-memory browsers unless handles were externalized. Runbook: (1) drain DNS weight to healthy region, (2) accept re-init storm with rate limits, (3) communicate that long-running automations need explicit **`run_id`** in tool args to resume from DB. For **`2026-07-28`**, **`requestState`** in global Redis with multi-region replication avoids region-local session loss but adds latency — measure MRTR p95 cross-region before enabling.
---

## Operational deep dive: Capacity formula worked example

Suppose 200 concurrent legacy GET streams, each 40 KB socket buffers → ~8 MB kernel buffers plus 200 × 2 MB Python object overhead ≈ 400 MB before any browser.work. Add 30% headroom → plan 520 MB per pod; with 4 pods, 2000 sessions max before new shard. Stateless **`tools/call`** at 50 RPS, 100 ms average, 20 worker threads per pod → 2 pods suffice if OAuth cache hit rate >90%.
---

## Operational deep dive: Incident postmortem template

Timeline: spike in **404** on **`Mcp-Session-Id`** after deploy → root cause sticky cookie not propagated to new Ingress annotation → fix: header-based affinity on **`mcp-session-id`**. Action items: add CI lint on Ingress stickiness; dashboard panel **`mcp_session_404_rate`**.
---

## Operational deep dive: Contract testing legacy vs modern

Golden files: **`initialize.json`**, **`tools_list.json`**, **`tools_call_input_required.json`**. CI runs against Docker image with **`MCP_PROTOCOL_VERSION`** env toggling behaviour. Assert HTTP status and header sets differ by version but business tool results identical for read tools.
---

## Operational deep dive: Browser pool architecture

Separate **Session API** (legacy sticky) from **Worker pool** gRPC. Tools only pass **`browser_id`**. Even on legacy MCP, do not store Playwright on transport object >5 min; persist **`browser_id`** early to survive session 404.
---

## Operational deep dive: Elicitation UX under MRTR

Host must map **`inputRequests`** to modal UI with schema validation before retry. Timeout 10 minutes → abort and return **`isError`** to model. Log MRTR round count metric — values >5 indicate model confusion loop.
---

## Operational deep dive: subscriptions/listen fan-out

One listen stream per host connection; server fan-out **`list_changed`** to all listeners. Implement back-pressure: drop slow listeners with **`429`** on new listen POST rather than blocking tool threads.
---

## Operational deep dive: Cloud Run session anti-pattern

Cloud Run + legacy GET SSE + scale-to-zero → cold start mid-session. Minimum instances ≥1 for stateful legacy; prefer modern stateless + external state for scale-to-zero economics.
---

## Operational deep dive: Compliance logging

Log fields: **`oauth_sub`**, **`mcp_method`**, **`mcp_name`**, **`protocol_version`**, **`session_id_hash`**, **`latency_ms`**, **`result_type`**. Never log tool argument bodies containing PII — hash or omit.
---

## Operational deep dive: Load balancer health checks

HTTP GET **`/health`** must not require MCP session. Readiness fails when **`sessions_active > MAX`** to protect pod. Liveness independent of SSE count.
---

## Full HTTP trace: MRTR two-round checkout (2026-07-28)

```http
POST /mcp HTTP/1.1
Host: shop.example.com
Authorization: Bearer eyJ...
Content-Type: application/json
Accept: application/json, text/event-stream
MCP-Protocol-Version: 2026-07-28
Mcp-Method: tools/call
Mcp-Name: checkout

{"jsonrpc":"2.0","id":50,"method":"tools/call","params":{"name":"checkout","arguments":{"cart_id":"c_7712"},"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}
```

```http
HTTP/1.1 200 OK
Content-Type: application/json

{"jsonrpc":"2.0","id":50,"result":{"resultType":"input_required","requestState":"chk_abc","inputRequests":[{"method":"elicitation/create","params":{"message":"Confirm purchase 249 USD?","requestedSchema":{"type":"object","properties":{"confirm":{"type":"boolean"}},"required":["confirm"]}}}]}}
```

After user confirms:

```http
POST /mcp HTTP/1.1
Mcp-Method: tools/call
Mcp-Name: checkout
(... same headers ...)

{"jsonrpc":"2.0","id":51,"method":"tools/call","params":{"name":"checkout","arguments":{"cart_id":"c_7712"},"requestState":"chk_abc","inputResponses":[{"response":{"confirm":true}}],"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28"}}}
```

```http
HTTP/1.1 200 OK
Content-Type: application/json

{"jsonrpc":"2.0","id":51,"result":{"resultType":"complete","content":[{"type":"text","text":"Order ord_991 placed"}],"structuredContent":{"orderId":"ord_991"},"isError":false}}
```

Note second POST may hit a different Cloud Run instance — **`requestState`** must resolve via Redis, not pod RAM.

---

---

## Legacy stateful trace variant 1: error paths

**Unknown session on tool call**

```http
POST /mcp HTTP/1.1
Mcp-Session-Id: dead-beef-dead-beef
Content-Type: application/json

{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"ping","arguments":{}}}
```

```http
HTTP/1.1 404 Not Found
Content-Type: application/json

{"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"Session not found"}}
```

Client behaviour: discard session id, run full **`initialize`** sequence again. Server behaviour: do not
partially execute tool — fail closed.

**Missing session header after init**

Returns **400** — distinguishes misconfigured client from expired session (**404**).

**GET stream without init**

Returns **400/404** depending on server — hosts should always init before GET.

**DELETE idempotent retry**

Second DELETE → **404** acceptable; client clears local session id regardless.

Operational insight 1: alert when **404/400 ratio** exceeds baseline after deploy — classic stickiness
regression or session TTL too aggressive for long IDE sessions.
---

## Legacy stateful trace variant 2: error paths

**Unknown session on tool call**

```http
POST /mcp HTTP/1.1
Mcp-Session-Id: dead-beef-dead-beef
Content-Type: application/json

{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"ping","arguments":{}}}
```

```http
HTTP/1.1 404 Not Found
Content-Type: application/json

{"jsonrpc":"2.0","id":2,"error":{"code":-32000,"message":"Session not found"}}
```

Client behaviour: discard session id, run full **`initialize`** sequence again. Server behaviour: do not
partially execute tool — fail closed.

**Missing session header after init**

Returns **400** — distinguishes misconfigured client from expired session (**404**).

**GET stream without init**

Returns **400/404** depending on server — hosts should always init before GET.

**DELETE idempotent retry**

Second DELETE → **404** acceptable; client clears local session id regardless.

Operational insight 2: alert when **404/400 ratio** exceeds baseline after deploy — classic stickiness
regression or session TTL too aggressive for long IDE sessions.
---

## Legacy stateful trace variant 3: error paths

**Unknown session on tool call**

```http
POST /mcp HTTP/1.1
Mcp-Session-Id: dead-beef-dead-beef
Content-Type: application/json

{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"ping","arguments":{}}}
```

```http
HTTP/1.1 404 Not Found
Content-Type: application/json

{"jsonrpc":"2.0","id":3,"error":{"code":-32000,"message":"Session not found"}}
```

Client behaviour: discard session id, run full **`initialize`** sequence again. Server behaviour: do not
partially execute tool — fail closed.

**Missing session header after init**

Returns **400** — distinguishes misconfigured client from expired session (**404**).

**GET stream without init**

Returns **400/404** depending on server — hosts should always init before GET.

**DELETE idempotent retry**

Second DELETE → **404** acceptable; client clears local session id regardless.

Operational insight 3: alert when **404/400 ratio** exceeds baseline after deploy — classic stickiness
regression or session TTL too aggressive for long IDE sessions.
---

## Legacy stateful trace variant 4: error paths

**Unknown session on tool call**

```http
POST /mcp HTTP/1.1
Mcp-Session-Id: dead-beef-dead-beef
Content-Type: application/json

{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"ping","arguments":{}}}
```

```http
HTTP/1.1 404 Not Found
Content-Type: application/json

{"jsonrpc":"2.0","id":4,"error":{"code":-32000,"message":"Session not found"}}
```

Client behaviour: discard session id, run full **`initialize`** sequence again. Server behaviour: do not
partially execute tool — fail closed.

**Missing session header after init**

Returns **400** — distinguishes misconfigured client from expired session (**404**).

**GET stream without init**

Returns **400/404** depending on server — hosts should always init before GET.

**DELETE idempotent retry**

Second DELETE → **404** acceptable; client clears local session id regardless.

Operational insight 4: alert when **404/400 ratio** exceeds baseline after deploy — classic stickiness
regression or session TTL too aggressive for long IDE sessions.
---

## Legacy stateful trace variant 5: error paths

**Unknown session on tool call**

```http
POST /mcp HTTP/1.1
Mcp-Session-Id: dead-beef-dead-beef
Content-Type: application/json

{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"ping","arguments":{}}}
```

```http
HTTP/1.1 404 Not Found
Content-Type: application/json

{"jsonrpc":"2.0","id":5,"error":{"code":-32000,"message":"Session not found"}}
```

Client behaviour: discard session id, run full **`initialize`** sequence again. Server behaviour: do not
partially execute tool — fail closed.

**Missing session header after init**

Returns **400** — distinguishes misconfigured client from expired session (**404**).

**GET stream without init**

Returns **400/404** depending on server — hosts should always init before GET.

**DELETE idempotent retry**

Second DELETE → **404** acceptable; client clears local session id regardless.

Operational insight 5: alert when **404/400 ratio** exceeds baseline after deploy — classic stickiness
regression or session TTL too aggressive for long IDE sessions.
---

## Legacy stateful trace variant 6: error paths

**Unknown session on tool call**

```http
POST /mcp HTTP/1.1
Mcp-Session-Id: dead-beef-dead-beef
Content-Type: application/json

{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"ping","arguments":{}}}
```

```http
HTTP/1.1 404 Not Found
Content-Type: application/json

{"jsonrpc":"2.0","id":6,"error":{"code":-32000,"message":"Session not found"}}
```

Client behaviour: discard session id, run full **`initialize`** sequence again. Server behaviour: do not
partially execute tool — fail closed.

**Missing session header after init**

Returns **400** — distinguishes misconfigured client from expired session (**404**).

**GET stream without init**

Returns **400/404** depending on server — hosts should always init before GET.

**DELETE idempotent retry**

Second DELETE → **404** acceptable; client clears local session id regardless.

Operational insight 6: alert when **404/400 ratio** exceeds baseline after deploy — classic stickiness
regression or session TTL too aggressive for long IDE sessions.
---

## Legacy stateful trace variant 7: error paths

**Unknown session on tool call**

```http
POST /mcp HTTP/1.1
Mcp-Session-Id: dead-beef-dead-beef
Content-Type: application/json

{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"ping","arguments":{}}}
```

```http
HTTP/1.1 404 Not Found
Content-Type: application/json

{"jsonrpc":"2.0","id":7,"error":{"code":-32000,"message":"Session not found"}}
```

Client behaviour: discard session id, run full **`initialize`** sequence again. Server behaviour: do not
partially execute tool — fail closed.

**Missing session header after init**

Returns **400** — distinguishes misconfigured client from expired session (**404**).

**GET stream without init**

Returns **400/404** depending on server — hosts should always init before GET.

**DELETE idempotent retry**

Second DELETE → **404** acceptable; client clears local session id regardless.

Operational insight 7: alert when **404/400 ratio** exceeds baseline after deploy — classic stickiness
regression or session TTL too aggressive for long IDE sessions.
---

## Legacy stateful trace variant 8: error paths

**Unknown session on tool call**

```http
POST /mcp HTTP/1.1
Mcp-Session-Id: dead-beef-dead-beef
Content-Type: application/json

{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"ping","arguments":{}}}
```

```http
HTTP/1.1 404 Not Found
Content-Type: application/json

{"jsonrpc":"2.0","id":8,"error":{"code":-32000,"message":"Session not found"}}
```

Client behaviour: discard session id, run full **`initialize`** sequence again. Server behaviour: do not
partially execute tool — fail closed.

**Missing session header after init**

Returns **400** — distinguishes misconfigured client from expired session (**404**).

**GET stream without init**

Returns **400/404** depending on server — hosts should always init before GET.

**DELETE idempotent retry**

Second DELETE → **404** acceptable; client clears local session id regardless.

Operational insight 8: alert when **404/400 ratio** exceeds baseline after deploy — classic stickiness
regression or session TTL too aggressive for long IDE sessions.
---

## Legacy stateful trace variant 9: error paths

**Unknown session on tool call**

```http
POST /mcp HTTP/1.1
Mcp-Session-Id: dead-beef-dead-beef
Content-Type: application/json

{"jsonrpc":"2.0","id":9,"method":"tools/call","params":{"name":"ping","arguments":{}}}
```

```http
HTTP/1.1 404 Not Found
Content-Type: application/json

{"jsonrpc":"2.0","id":9,"error":{"code":-32000,"message":"Session not found"}}
```

Client behaviour: discard session id, run full **`initialize`** sequence again. Server behaviour: do not
partially execute tool — fail closed.

**Missing session header after init**

Returns **400** — distinguishes misconfigured client from expired session (**404**).

**GET stream without init**

Returns **400/404** depending on server — hosts should always init before GET.

**DELETE idempotent retry**

Second DELETE → **404** acceptable; client clears local session id regardless.

Operational insight 9: alert when **404/400 ratio** exceeds baseline after deploy — classic stickiness
regression or session TTL too aggressive for long IDE sessions.
---

## Legacy stateful trace variant 10: error paths

**Unknown session on tool call**

```http
POST /mcp HTTP/1.1
Mcp-Session-Id: dead-beef-dead-beef
Content-Type: application/json

{"jsonrpc":"2.0","id":10,"method":"tools/call","params":{"name":"ping","arguments":{}}}
```

```http
HTTP/1.1 404 Not Found
Content-Type: application/json

{"jsonrpc":"2.0","id":10,"error":{"code":-32000,"message":"Session not found"}}
```

Client behaviour: discard session id, run full **`initialize`** sequence again. Server behaviour: do not
partially execute tool — fail closed.

**Missing session header after init**

Returns **400** — distinguishes misconfigured client from expired session (**404**).

**GET stream without init**

Returns **400/404** depending on server — hosts should always init before GET.

**DELETE idempotent retry**

Second DELETE → **404** acceptable; client clears local session id regardless.

Operational insight 10: alert when **404/400 ratio** exceeds baseline after deploy — classic stickiness
regression or session TTL too aggressive for long IDE sessions.
---

## Legacy stateful trace variant 11: error paths

**Unknown session on tool call**

```http
POST /mcp HTTP/1.1
Mcp-Session-Id: dead-beef-dead-beef
Content-Type: application/json

{"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"ping","arguments":{}}}
```

```http
HTTP/1.1 404 Not Found
Content-Type: application/json

{"jsonrpc":"2.0","id":11,"error":{"code":-32000,"message":"Session not found"}}
```

Client behaviour: discard session id, run full **`initialize`** sequence again. Server behaviour: do not
partially execute tool — fail closed.

**Missing session header after init**

Returns **400** — distinguishes misconfigured client from expired session (**404**).

**GET stream without init**

Returns **400/404** depending on server — hosts should always init before GET.

**DELETE idempotent retry**

Second DELETE → **404** acceptable; client clears local session id regardless.

Operational insight 11: alert when **404/400 ratio** exceeds baseline after deploy — classic stickiness
regression or session TTL too aggressive for long IDE sessions.
---

## Legacy stateful trace variant 12: error paths

**Unknown session on tool call**

```http
POST /mcp HTTP/1.1
Mcp-Session-Id: dead-beef-dead-beef
Content-Type: application/json

{"jsonrpc":"2.0","id":12,"method":"tools/call","params":{"name":"ping","arguments":{}}}
```

```http
HTTP/1.1 404 Not Found
Content-Type: application/json

{"jsonrpc":"2.0","id":12,"error":{"code":-32000,"message":"Session not found"}}
```

Client behaviour: discard session id, run full **`initialize`** sequence again. Server behaviour: do not
partially execute tool — fail closed.

**Missing session header after init**

Returns **400** — distinguishes misconfigured client from expired session (**404**).

**GET stream without init**

Returns **400/404** depending on server — hosts should always init before GET.

**DELETE idempotent retry**

Second DELETE → **404** acceptable; client clears local session id regardless.

Operational insight 12: alert when **404/400 ratio** exceeds baseline after deploy — classic stickiness
regression or session TTL too aggressive for long IDE sessions.
---

## Decision record template (architecture review)

Copy into internal design docs when choosing transport state mode.

| Field | Your answer |
| --- | --- |
| Target MCP protocol version | e.g. 2026-07-28 |
| Legacy host support required? | Y/N — if Y, dual-stack timeline |
| Long-lived server push needed? | subscriptions/listen vs poll |
| Multi-step user input | MRTR vs host-native forms |
| Cross-call mutable resources | explicit handle + store |
| Peak concurrent connections | drives FD and RAM |
| Deployment platform | K8s / Lambda / Cloud Run |
| Load balancer stickiness | header name + TTL |
| Session or requestState store | Redis/Dynamo + TTL |
| Rollback plan | URL or flag revert |

Sign-off: service owner, security, SRE.

---

## Spec references (October 2026)

- **Current version:** `2026-07-28` (no October 2026 release; next target late March 2027 per SEP-3398).
- **Legacy session transport:** `2025-11-25` Streamable HTTP chapter — `Mcp-Session-Id`, GET, DELETE, resumability.
- **Migration:** changelog items SEP-2567 (explicit handles), SEP-2322 (MRTR), SEP-2575 (stateless core).

---

## ASCII decision flowchart (printable)

```text
                        START: new MCP HTTP server
                                    │
                                    ▼
               Must support hosts stuck on 2025-11-25 only?
                          /                    \
                        yes                     no
                         │                       │
                         ▼                       ▼
              Implement legacy session      Target 2026-07-28
              POST+GET+DELETE               POST-only core
                         │                       │
                         ▼                       ▼
         Need push without client poll?    Need push notifications?
                /         \                      /         \
              yes          no                   yes          no
               │            │                    │            │
               ▼            ▼                    ▼            ▼
          GET SSE +    stateless POST      subscriptions/   ttlMs cache
          sticky LB    JSON tools          listen SSE       on lists only
               │            │                    │            │
               └────────────┴────────────────────┴────────────┘
                                    │
                                    ▼
              Multi-step input during single tool?
                         /                    \
                       yes                     no
                        │                       │
                        ▼                       ▼
               MRTR (2026) or legacy      single POST
               server RPC on GET (legacy)   tools/call
                        │                       │
                        └───────────┬───────────┘
                                    ▼
              Cross-call mutable resource (browser, cart)?
                         /                    \
                       yes                     no
                        │                       │
                        ▼                       ▼
            explicit handle tools +          pure stateless
            Redis/DB (any era)               CRUD/search
                        │                       │
                        └───────────┬───────────┘
                                    ▼
                         Document in decision record
                         Ship with metrics + runbooks
```

---

---

## Quick reference: HTTP status codes by era

| Status | Legacy stateful | 2026-07-28 |
| --- | --- | --- |
| 200 | JSON or SSE on POST | Same |
| 202 | Notification accepted | Same |
| 400 | Bad session / bad request | HeaderMismatch, bad meta |
| 403 | Bad Origin | Bad Origin |
| 404 | Unknown session | Method not found (JSON-RPC) |
| 405 | GET on stateless | GET/DELETE unsupported |

Keep this table in on-call runbooks next to **`Mcp-Session-Id`** playbooks.

---
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

1. **Treating session id as auth** — always validate OAuth bearer independently.
2. **Stateful sessions on serverless** without Durable Objects / external store — sessions vanish on
   cold start.
3. **Forgetting GET route** on stateful server — host never receives **`list_changed`**.
4. **Unbounded session map** — memory leak; always TTL + **`MAX_SESSIONS`**.
5. **Sticky LB on client IP** instead of session header — breaks mobile NAT switching less often but
   still wrong key; use **`MCP-Session-Id`**.
6. **Non-idempotent tools in stateless mode** — host retries POST on network blip → duplicate charges.
7. **Logging full tool arguments** containing PII — compliance incident.
8. **Assuming 2026 servers accept JSON-RPC responses on POST** — MRTR embeds responses differently.
9. **SSE without disabling proxy buffering** — "streaming broken in prod, works locally."
10. **Keeping headful browsers in session** without quota — one user exhausts pod RAM.

---

## Interview questions

**1. What is the difference between a stateful and stateless MCP Streamable HTTP server in
`2025-11-25`?**

A stateful server returns **`MCP-Session-Id`** from **`initialize`** and expects it on later POST/GET/
DELETE; it typically keeps a transport instance and may expose a GET SSE push channel. A stateless
server omits session ids, handles each POST independently (often with JSON responses), and returns
**405** on GET/DELETE.

**2. Why did MCP remove protocol-level sessions in `2026-07-28`?**

Session stickiness complicates horizontal scaling, serverless, and zero-downtime deploys. Moving to
per-request POST with MRTR and subscription streams removes mandatory affinity while keeping
interactive features.

**3. How does the client recover when the server returns 404 for a session id?**

It must send a new **`initialize`** without a session header, then re-run capability discovery; domain
handles in explicit stores may survive if TTL not expired, but in-memory session objects are lost.

**4. When is sticky load balancing required?**

When session-scoped state (legacy transport instance or unreplicated in-memory handles) has no shared
store — route by **`MCP-Session-Id`**. Not required for fully stateless tools or for `2026-07-28`
POST-only servers that externalize MRTR state.

**5. Can you run a stateful MCP server on AWS Lambda?**

Practically no for legacy GET SSE and session maps — execution environment is ephemeral and concurrent
invocations do not share memory. Use stateless POST + external store, or containers (ECS/EKS) for
stateful legacy mode.

**6. What security issue does the Origin header mitigate?**

DNS rebinding: a malicious site tricking a browser into calling a local MCP server. Validate Origin →
403.

**7. How do server-initiated LLM requests work without GET in `2026-07-28`?**

Via **MRTR**: tool result includes **`InputRequiredResult`** with **`inputRequests`** for sampling;
the host retries the tool call with **`inputResponses`**.

**8. What is Last-Event-ID for?**

In `2025-11-25`, SSE resumption — replay events after disconnect on the same stream. Not in
`2026-07-28`.

**9. How would you implement a multi-step shopping cart without protocol sessions?**

`create_cart` returns **`cart_id`**; mutate cart in Redis/DB keyed by id; **`checkout(cart_id)`**;
pass ids through tool arguments; any pod can serve.

**10. DELETE vs letting sessions expire?**

DELETE is polite cleanup (free resources now). Server may still expire idle sessions → 404; clients
must handle both.

**11. Difference between stdio and HTTP state models?**

stdio is one long-lived process pair per local launch — inherently connection-stateful. HTTP scaling
forces the stateless/explicit-state trade-off.

**12. What HTTP status for client notifications the server accepted?**

**202 Accepted** with empty body.

**13. Why enable JSON response mode in stateless servers?**

Avoids SSE overhead when responses are single-shot — simpler for Lambda and API gateways.

**14. How do you cap resource exhaustion from too many sessions?**

Reject **`initialize`** with **503** when at **`MAX_SESSIONS`**; idle TTL; per-subject quotas after
OAuth validation.

**15. What headers must clients send on POST in modern MCP?**

**`Accept: application/json, text/event-stream`**, **`MCP-Protocol-Version`**, plus **`Mcp-Method`**
/ **`Mcp-Name`** for applicable calls — exact rules in `2026-07-28` transport spec.

---

**16. What happens if two POSTs with the same session id hit different pods without sticky routing?**

The second pod likely returns **404** (unknown session). Host should re-initialize; user may lose
unreplicated session state — fix routing or centralize sessions.

**17. Explain 202 vs 200 in MCP HTTP.**

**202** acknowledges notifications/responses with no JSON body; **200** carries JSON-RPC result or
opens SSE for requests.

**18. Why must session IDs be hard to guess?**

Prevent session hijacking if an attacker obtains or brute-forces ids — use UUID v4+ or signed tokens.

**19. How does subscriptions/listen replace GET push in 2026?**

Client sends POST **`subscriptions/listen`**; response body is long-lived SSE filtered to subscribed
notification types like **`tools/list_changed`**.

**20. When would you still run a 2025-11-25 stateful server in 2026?**

When supporting legacy hosts/binaries you cannot upgrade yet — dual-stack servers implement both
behaviour sets gated on **`MCP-Protocol-Version`**.


---



### Extended model answers (selected)

**Walk through stateful vs stateless choice server.** The host discovers protected resource metadata for the MCP
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


## Summary cheat sheet

```text
  Legacy (≤ 2025-11-25)                         Modern (2026-07-28)
  ─────────────────────                         ───────────────────
  MCP-Session-Id optional                       No protocol session
  POST + optional GET + DELETE                  POST only (GET/DELETE → 405)
  Push on GET SSE                               subscriptions/listen SSE
  Sampling on SSE request                       MRTR InputRequiredResult
  Last-Event-ID resume                          Not supported — idempotent tools
  Scale: sticky or Redis                        Scale: round robin + external state

  Default architecture: stateless HTTP + explicit IDs in tool args + shared DB/Redis
  Go stateful legacy only when operating old hosts or unreplicated live resources
```

---

## Further reading

- Model Context Protocol specification — **Transports** (`2025-11-25`): Streamable HTTP, session
  management, resumability (GitHub: `modelcontextprotocol/modelcontextprotocol` tag `2025-11-25`).
- Model Context Protocol specification — **Streamable HTTP** (`2026-07-28`): MRTR, subscriptions,
  request metadata headers — [modelcontextprotocol.io](https://modelcontextprotocol.io).
- MCP TypeScript SDK — **Sessions, state, and scaling** (legacy session transport patterns).
- HTML Living Standard — **Server-Sent Events** (event id, retry field, comment lines).
- OAuth 2.1 and MCP authorization spec for remote servers (Bearer tokens independent of sessions).

---