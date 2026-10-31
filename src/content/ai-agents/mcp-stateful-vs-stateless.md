---
title: "MCP: stateful vs stateless"
order: 7
---
# MCP: Stateful vs Stateless Servers

With the **Streamable HTTP** transport, an MCP server chooses whether to keep a **session** for each client. That one decision drives how the server scales, what features it can offer, and how it is deployed.

## Quick comparison

| | Stateful | Stateless |
| --- | --- | --- |
| Session ID | Server issues `Mcp-Session-Id` on `initialize`; client sends it on every request | None |
| Per-client memory | Yes — kept between requests | No — each request stands alone |
| Server → client messages | Yes: notifications, progress, sampling, elicitation, `list_changed` | Only inside the response to the current request |
| Standalone GET stream | Supported | Not used |
| Resumable streams | Possible (event IDs + `Last-Event-ID`) | Not needed |
| Scaling | Sticky sessions or a shared session store | Any instance handles any request; trivial to scale |
| Serverless (Lambda, Cloud Run, Workers) | Awkward | Natural fit |
| Typical use | Long tasks, interactive tools, subscriptions | Simple tool APIs: search, lookup, CRUD |

## How Streamable HTTP works

One endpoint, say `https://api.example.com/mcp`, handles three HTTP methods:

- **POST** — the client sends a JSON-RPC message. For a request, the server replies either with a single JSON response or with an **SSE stream** (useful when it wants to send progress and other messages before the final result).
- **GET** — opens a long-lived SSE stream for **server-initiated** messages (stateful only).
- **DELETE** — the client ends its session (stateful only).

Clients send `Accept: application/json, text/event-stream`, and after initialization an `MCP-Protocol-Version` header.

---

## Stateful mode

```text
client                                              server
  │ POST /mcp  initialize ─────────────────────────►  │  creates session
  │ ◄──────────── 200  Mcp-Session-Id: 7f3c…  ──────  │
  │ POST /mcp  notifications/initialized               │
  │            Mcp-Session-Id: 7f3c… ───────────────►  │
  │ GET  /mcp  (SSE)  Mcp-Session-Id: 7f3c… ───────►  │  server can now push:
  │ ◄───────── progress / sampling / list_changed ──  │
  │ POST /mcp  tools/call  Mcp-Session-Id: 7f3c… ──►  │  uses session state
  │ DELETE /mcp  Mcp-Session-Id: 7f3c… ─────────────►  │  session destroyed
```

Rules from the spec:

- The session ID must be globally unique and hard to guess (UUID or a signed token) and use visible ASCII only.
- If a request has an unknown or expired session ID, the server answers **404**, and the client must start over with a new `initialize`.
- Requests without the session ID (other than `initialize`) should get **400**.

### What state buys you

- **Server → client requests:** sampling (ask the host's LLM), elicitation (ask the user a question), roots.
- **Notifications:** progress on long jobs, logs, `tools/list_changed` when available tools change, resource subscriptions.
- **Conversation-scoped data:** an open DB transaction, a browser page, a sandbox, a shopping cart, cached auth.
- **Resumability:** SSE events carry IDs; after a dropped connection the client reconnects with `Last-Event-ID` and missed messages are replayed.

### Stateful server (TypeScript SDK)

```ts
import express from "express";
import { randomUUID } from "node:crypto";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { isInitializeRequest } from "@modelcontextprotocol/sdk/types.js";

const app = express();
app.use(express.json());

const transports: Record<string, StreamableHTTPServerTransport> = {};

app.post("/mcp", async (req, res) => {
  const sessionId = req.headers["mcp-session-id"] as string | undefined;
  let transport = sessionId ? transports[sessionId] : undefined;

  if (!transport) {
    if (sessionId || !isInitializeRequest(req.body)) {
      res.status(sessionId ? 404 : 400).json({
        jsonrpc: "2.0",
        error: { code: -32000, message: "No valid session" },
        id: null,
      });
      return;
    }
    transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: () => randomUUID(),
      onsessioninitialized: (id) => { transports[id] = transport!; },
    });
    transport.onclose = () => {
      if (transport!.sessionId) delete transports[transport!.sessionId];
    };
    await buildServer().connect(transport);      // one McpServer per session
  }

  await transport.handleRequest(req, res, req.body);
});

// GET opens the server→client SSE stream; DELETE ends the session.
const bySession = async (req: express.Request, res: express.Response) => {
  const transport = transports[req.headers["mcp-session-id"] as string];
  if (!transport) return res.status(404).send("Unknown session");
  await transport.handleRequest(req, res);
};
app.get("/mcp", bySession);
app.delete("/mcp", bySession);

app.listen(3000);
```

The `transports` map lives in **this process's memory**. With more than one instance you need either:

- **Sticky sessions** at the load balancer, routing by the `Mcp-Session-Id` header, or
- An **external session store / message bus** (Redis, a database) so any instance can serve the session — the SDK supports a pluggable **event store** for resumability.

---

## Stateless mode

```text
client                                      any instance
  │ POST /mcp  tools/call ───────────────────►  │  build server, handle, discard
  │ ◄──────────────── JSON result ───────────  │
  │ POST /mcp  tools/call ───────────────────►  │  (maybe a different instance)
  │ ◄──────────────── JSON result ───────────  │
```

No session ID is issued. Every POST is self-contained: the server builds what it needs, answers, and forgets. Anything that must persist goes into the **arguments** (an explicit ID the tool returned earlier) or your **own database**, never the connection.

### Stateless server (TypeScript SDK)

```ts
app.post("/mcp", async (req, res) => {
  const server = buildServer();
  const transport = new StreamableHTTPServerTransport({
    sessionIdGenerator: undefined,          // undefined = stateless
    enableJsonResponse: true,               // plain JSON instead of SSE
  });
  res.on("close", () => {
    transport.close();
    server.close();
  });
  await server.connect(transport);
  await transport.handleRequest(req, res, req.body);
});

// No server-initiated stream and no sessions to delete.
app.get("/mcp", (_req, res) => res.status(405).end());
app.delete("/mcp", (_req, res) => res.status(405).end());
```

### Stateless server (Python FastMCP)

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("orders", stateless_http=True, json_response=True)

@mcp.tool()
def get_order(order_id: str) -> dict:
    """Look up an order by id, e.g. 'ORD-10293'."""
    return db.fetch_order(order_id)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")      # serves /mcp
```

### What you give up

- No sampling, elicitation, or roots requests to the client.
- No `list_changed` or resource-subscription notifications.
- Progress can only be streamed within the single response, if at all.
- Per-request setup cost — keep `buildServer()` cheap (create DB pools once, outside the handler).

---

## Choosing

```text
Does a tool need to talk back to the client mid-task
(sampling, elicitation, progress, notifications)? ── yes ──► stateful
            │ no
Does the server hold per-client resources between calls
(browser page, sandbox, open transaction)? ─────────── yes ──► stateful
            │ no
                                                          ──► stateless
```

**Default to stateless** for remote tool APIs: it scales horizontally, survives restarts and deploys, and runs on serverless platforms. Go stateful only for features that genuinely need a session.

### Hybrid approach

Keep the transport stateless but model state **explicitly** in your domain:

```text
create_cart()            → { "cart_id": "c_81f2" }
add_item(cart_id, sku)   → stored in Redis / DB under c_81f2
checkout(cart_id)
```

The model carries the `cart_id` between calls, any instance can serve each call, and state survives reconnects.

## Deployment notes

- **Authentication** is per request either way (OAuth bearer token); don't treat a session ID as authentication.
- **Timeouts:** proxies and serverless platforms cut long requests; stateful SSE streams need keep-alives and generous idle timeouts.
- **Load balancer:** stateful needs header-based affinity; stateless can use plain round-robin.
- **Observability:** log the session ID (stateful) or a request ID (stateless) with every tool call.
- **stdio servers** are inherently "stateful" — one process per client for the life of the connection — but they run locally, so scaling isn't a concern.
- The protocol keeps evolving toward making stateless operation easier; check the current spec version when you build.
