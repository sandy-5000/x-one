---
title: "A2A (Agent2Agent protocol)"
order: 8
---
# A2A (Agent2Agent protocol)

**A2A** is an open protocol for **independent AI agents to collaborate as peers**: discover what
another agent offers, delegate work, hold multi-turn conversations about that work, track progress,
and collect results—across frameworks, vendors, and infrastructure.

These notes target **A2A specification v1.0** (stable March 2026). Older tutorials often show
**v0.3** wire formats (`message/send`, `"kind": "text"`, lowercase task states). Normative source:
`specification/a2a.proto` in [a2aproject/A2A](https://github.com/a2aproject/A2A); JSON on the wire
is ProtoJSON (camelCase fields, `SCREAMING_SNAKE_CASE` enums).

A2A standardises four things: a **public Agent Card**, a **Task** with lifecycle, a **Message /
Part / Artifact** content model, and **three update paths** (poll, SSE/gRPC stream, push webhook).
Security reuses OAuth 2.0, OIDC, API keys, HTTP auth, and mTLS—declared on the card like OpenAPI.

## Key points

- **Opacity**: clients see skills and exchanged content, not prompts, tools, or internal reasoning.
- **Async-first**: tasks run seconds to days; `INPUT_REQUIRED` and `AUTH_REQUIRED` pause work until
  the client (or human) responds.
- **Work unit**: a **Task** (server-generated `id`); conversations group tasks via **`contextId`**.
- **Deliverables** belong in **artifacts**, not transient status messages.
- **Terminal tasks are immutable**; follow-ups are new tasks in the same `contextId` (optionally
  `referenceTaskIds`).
- **Discovery**: `GET /.well-known/agent-card.json` (IANA registered); registries and direct config
  are also common.
- **Bindings**: JSON-RPC, gRPC, and HTTP+JSON/REST are **functionally equivalent**; pick from
  `supportedInterfaces[]` (first entry preferred).
- **Version header**: clients MUST send `A2A-Version: 1.0`; **missing header is interpreted as 0.3**.
- **MCP vs A2A**: MCP connects an agent to **tools**; A2A connects an agent to **another agent**.
  Use both: MCP vertically inside an agent, A2A horizontally across ownership boundaries.

## Agent Card

The card is everything a client needs before the first request: identity, endpoints, capabilities,
skills, and security.

### Discovery flow

```text
1. GET /.well-known/agent-card.json   (Cache-Control / ETag)
2. Verify JWS signature if policy requires it
3. Pick first supportedInterfaces[] entry you support (binding + protocolVersion)
4. Obtain credentials out of band per securityRequirements
5. Optionally GetExtendedAgentCard when authenticated
6. Call interface URL with A2A-Version: 1.0 (+ tenant if set)
```

Registries and hard-coded card URLs are valid; the protocol does not define a registry API. Legacy
path `/.well-known/agent.json` → **`agent-card.json`** (since v0.3).

### Card structure (essentials)

| Field | Notes |
| --- | --- |
| `name`, `description` | Required; routers and LLMs read `description`—write it clearly. |
| `supportedInterfaces[]` | Required; `{ url, protocolBinding, protocolVersion, tenant? }`. Bindings: `JSONRPC`, `GRPC`, `HTTP+JSON`, or custom URI. |
| `version` | Your agent version, not the protocol. |
| `capabilities` | `streaming`, `pushNotifications`, `extendedAgentCard`, `extensions[]`. |
| `defaultInputModes` / `defaultOutputModes` | Default MIME types. |
| `skills[]` | Descriptive only—**no "call skill X" RPC**. |
| `securitySchemes` / `securityRequirements` | OpenAPI-style; per-skill overrides allowed. |
| `signatures[]` | Optional JWS over canonical card (JCS RFC 8785). |

**Skills** (`id`, `name`, `description`, `tags`, optional `examples`, I/O modes, auth) tell humans
and routing LLMs which agent to use; the server decides how to handle a message.

**Extended card**: when `capabilities.extendedAgentCard` is true, authenticated
`GetExtendedAgentCard` returns partner-only skills and limits. Public cards must not leak internals.

**Capabilities gate operations**: if `streaming` is false, `SendStreamingMessage` and
`SubscribeToTask` MUST return `UnsupportedOperationError`; push CRUD needs
`pushNotifications`.

### Minimal card excerpt

```json
{
  "name": "Flight Booking Agent",
  "description": "Search and book corporate flights.",
  "supportedInterfaces": [{
    "url": "https://flights.example.com/a2a/jsonrpc",
    "protocolBinding": "JSONRPC",
    "protocolVersion": "1.0"
  }],
  "version": "2.4.0",
  "capabilities": { "streaming": true, "pushNotifications": true },
  "defaultInputModes": ["text/plain", "application/json"],
  "defaultOutputModes": ["application/json"],
  "skills": [{
    "id": "search-flights",
    "name": "Search flights",
    "description": "Find flights between cities for dates and budget.",
    "tags": ["travel", "flights"]
  }]
}
```

## Task lifecycle states

A **Task** is the stateful unit of work: `id` (server-generated), optional `contextId`, `status`,
optional `history[]`, `artifacts[]`, `metadata`.

| Field | Role |
| --- | --- |
| `contextId` | Conversation/session; many tasks; agent keys memory here. |
| `taskId` | One job; continue, poll, cancel, subscribe with this id only. |

Rules: client cannot assign task ids on create; if both `contextId` and `taskId` are sent they
MUST match; `taskId` alone lets the server infer context; new work in same chat =
`contextId` without `taskId`.

### TaskState

| State | Kind | Meaning |
| --- | --- | --- |
| `TASK_STATE_SUBMITTED` | active | Accepted, queued. |
| `TASK_STATE_WORKING` | active | Processing. |
| `TASK_STATE_INPUT_REQUIRED` | interrupted | Needs client message to continue. |
| `TASK_STATE_AUTH_REQUIRED` | interrupted | Needs credential / approval out of band. |
| `TASK_STATE_COMPLETED` | terminal | Success; check artifacts. |
| `TASK_STATE_FAILED` | terminal | Error while executing. |
| `TASK_STATE_CANCELED` | terminal | Cancelled. |
| `TASK_STATE_REJECTED` | terminal | Refused (policy, scope, capacity). |

```text
SendMessage ──► SUBMITTED ──► WORKING ──┬──► COMPLETED / FAILED / CANCELED / REJECTED
                         ◄──────────────┤
                         INPUT_REQUIRED / AUTH_REQUIRED (stream/call ends; client resumes)
```

**Interrupted** states end a blocking `SendMessage` and close streams—the agent cannot progress
alone. **Terminal** tasks reject further messages (`UnsupportedOperationError`). `REJECTED` is
deliberate refusal; `FAILED` is an execution error.

`TaskStatus` may include a `message` (why paused) and ISO 8601 UTC `timestamp`.

## Messages, parts, and artifacts

### Message

| Field | Notes |
| --- | --- |
| `messageId` | Sender UUID; dedup optional. |
| `role` | `ROLE_USER` (client) or `ROLE_AGENT` (server). |
| `parts[]` | ≥1 part. |
| `contextId`, `taskId` | Associate with conversation/work. |
| `referenceTaskIds[]` | Prior tasks this message refers to. |

### Part (v1.0: member name is the discriminator—no `"kind"`)

| Field | Use |
| --- | --- |
| `text` | Plain or markdown. |
| `raw` | Inline bytes (base64 in JSON) + `mediaType`, optional `filename`. |
| `url` | File by reference (prefer for large payloads). |
| `data` | JSON value + `mediaType` (e.g. `application/json`). |

Use **`data`** for machine consumption; use **`url`** over **`raw`** for size and SSRF control
(validate URLs before fetch). Set `SendMessageConfiguration.acceptedOutputModes` to declare what
you can receive.

### Artifact

`{ artifactId, name?, description?, parts[], metadata? }` — **deliverables**. Messages are for
communication and progress; **results SHOULD be artifacts**. Clients reconnecting to streams may
miss transient messages; agents choose what enters `history`.

**SendMessage result** is exactly one of `task` or `message` (trivial reply with no task to track).

## JSON-RPC and REST bindings

Three layers: **data model** (proto), **operations**, **bindings** (must be equivalent).

| Binding | `protocolBinding` | Transport | Streaming |
| --- | --- | --- | --- |
| JSON-RPC 2.0 | `JSONRPC` | HTTP POST, `method` = operation | SSE `text/event-stream` |
| gRPC | `GRPC` | HTTP/2 + protobuf | server-streaming RPCs |
| REST | `HTTP+JSON` | Resource URLs (AIP-style `:verb`) | SSE |

**Headers / metadata**: `A2A-Version: 1.0`; `A2A-Extensions: uri,uri` for opted-in extensions;
`Authorization` per card schemes; optional `tenant` on interface and requests (multi-tenancy in v1.0).

### v1.0 operation map

| Operation | JSON-RPC | REST (examples) |
| --- | --- | --- |
| Send message | `SendMessage` | `POST /message:send` |
| Stream send | `SendStreamingMessage` | `POST /message:stream` |
| Get / list / cancel | `GetTask`, `ListTasks`, `CancelTask` | `GET /tasks/{id}`, `GET /tasks`, `POST /tasks/{id}:cancel` |
| Resubscribe | `SubscribeToTask` | SSE on task (check SDK vs spec for GET vs POST) |
| Push CRUD | `Create/Get/List/DeleteTaskPushNotificationConfig(s)` | under `/tasks/{id}/pushNotificationConfigs` |
| Extended card | `GetExtendedAgentCard` | `GET /extendedAgentCard` |

`SendMessageConfiguration`: `returnImmediately` (default false = block until terminal/interrupted),
`historyLength`, `acceptedOutputModes`, optional inline `taskPushNotificationConfig`.

**ListTasks** (v1.0): filters (`contextId`, status, timestamps), cursor pagination, default page
50 max 100, sorted by status time descending; scope results to caller's auth.

### v0.3 → v1.0 (migration checklist)

| v0.3 | v1.0 |
| --- | --- |
| `message/send`, snake paths | `SendMessage`, PascalCase methods |
| `"kind": "text"` | `{"text": "..."}` |
| `"completed"`, `"user"` | `TASK_STATE_COMPLETED`, `ROLE_USER` |
| `configuration.blocking` | `returnImmediately` (inverted default) |
| Card `url` + `preferredTransport` | `supportedInterfaces[]` |
| Stream `final: true` | Stream closes on terminal/interrupted state |

Example request:

```json
{
  "jsonrpc": "2.0",
  "id": "req-1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "9f1c2a7e-0b3d-4c55-9a51-2f4e8f0c1a01",
      "role": "ROLE_USER",
      "parts": [{ "text": "Morning flight BLR to DEL Friday under INR 6000." }]
    },
    "configuration": { "returnImmediately": false }
  }
}
```

## Streaming (SSE)

Why SSE: one-way server→client, works through proxies; client input is always new `SendMessage`
calls.

Each SSE `data:` line (JSON-RPC binding) wraps a **`StreamResponse`** with exactly one member:

| Member | Content |
| --- | --- |
| `task` | Full snapshot (first event in task lifecycle stream). |
| `message` | Single message-only stream, then close. |
| `statusUpdate` | `TaskStatusUpdateEvent` — state/progress. |
| `artifactUpdate` | `TaskArtifactUpdateEvent` — outputs, possibly chunked. |

Patterns: (1) one `message` then close, or (2) `task` first, then `statusUpdate` / `artifactUpdate`,
then close on **terminal or interrupted** state. Events in generation order.

**Chunked artifacts**: first chunk `append: false`; later `append: true` same `artifactId`; parse
only after `lastChunk: true`.

**Reconnect**: `SubscribeToTask` — first event is full `Task` snapshot; if task is terminal, use
`GetTask`. Do not rely on every status message having been received.

Wire sketch:

```text
data: {"jsonrpc":"2.0","id":1,"result":{"task":{"id":"t1","status":{"state":"TASK_STATE_WORKING"}}}}
data: {"jsonrpc":"2.0","id":1,"result":{"statusUpdate":{"taskId":"t1","status":{"state":"TASK_STATE_COMPLETED"}}}}
```

REST streaming payloads are bare `StreamResponse` without the JSON-RPC envelope.

## Push webhooks

Use when the client cannot hold a long SSE connection (hours-long tasks, mobile, serverless).

| Mechanism | Connected? | Best for |
| --- | --- | --- |
| Poll `GetTask` | No | Scripts, strict firewalls |
| SSE / gRPC stream | Yes | Interactive UIs |
| Push | No | Long-running work, wake workers |

Register via `CreateTaskPushNotificationConfig` or `SendMessageConfiguration.taskPushNotificationConfig`
(`taskId` empty on create). Server POSTs a **`StreamResponse`** to the client URL with
`Content-Type: application/a2a+json` and auth from `TaskPushNotificationConfig.authentication`.

Delivery: **at least once**; client MUST **2xx quickly**, process **idempotently**, verify auth
(JWT+JWKS, HMAC, mTLS, per-task bearer), then **`GetTask` for authoritative state**.

**Server SSRF risk**: push URLs and message `url` parts can target metadata IPs or internal services.
Reject private/link-local ranges after DNS resolution, require HTTPS, limit redirects, rate-limit.

```text
Client ──SendMessage+pushConfig──► Server ... work ... ──POST StreamResponse──► webhook
Client ◄──GetTask (after hint)──────────────────────────────────────────────────
```

## A2A vs MCP

| Dimension | MCP | A2A |
| --- | --- | --- |
| Connects | Agent host → **tools, resources, prompts** | Agent → **agent** |
| Other side | Capability provider (functions, data) | Autonomous opaque peer |
| Visibility | Tool list + JSON schemas | Skills description only |
| Unit of work | Tool call / resource read | **Task** with lifecycle |
| Duration | Usually short | Seconds to days |
| Multi-turn | Host LLM drives | Built-in (`INPUT_REQUIRED`, ids) |
| Who plans | Calling LLM | Remote agent |
| Outputs | Tool result content | **Artifacts** + status messages |
| Discovery | Configured servers, `tools/list` | Agent Card, registries |
| Transport | stdio, Streamable HTTP | JSON-RPC, gRPC, REST; SSE streams |
| Boundary | Inside app/team | Across teams and orgs |

Rule of thumb: **exact control → tool (MCP)**; **delegate an outcome → agent (A2A)**.

```text
Concierge agent ──MCP──► calendar, maps
                 └──A2A──► airline agent, expense agent (each may use MCP internally)
```

## Errors and auth (brief)

A2A-specific JSON-RPC codes include `-32001` TaskNotFound (also used when unauthorised—no leak),
`-32002` TaskNotCancelable, `-32003` push unsupported, `-32004` unsupported op (terminal task,
no streaming), `-32009` version unsupported. **JSON-RPC error** = request failed; **`TASK_STATE_FAILED`**
= task ran and failed—different retry semantics.

Credentials live in **HTTP/gRPC metadata**, obtained out of band. For agent A calling B **on behalf
of a user**, prefer **token exchange (RFC 8693)** or auth-code + PKCE—do not forward the user's
original access token. `TASK_STATE_AUTH_REQUIRED` is not itself authorization; resume may happen
without a new client message once credentials arrive.

## Pitfalls

1. **v0.3 wire against v1.0** — wrong methods, `kind`, lowercase enums; missing `A2A-Version: 1.0`.
2. **Critical data only in status messages** — use artifacts; messages may not survive reconnect.
3. **Messaging a terminal task** — start a new task with same `contextId` + `referenceTaskIds`.
4. **Client-generated task IDs** — always server-assigned.
5. **Dropped SSE = failed task** — task keeps running; `SubscribeToTask` or `GetTask`.
6. **Parsing artifacts before `lastChunk`** — incomplete JSON/files.
7. **Webhooks without auth, idempotency, or task-id checks** — or heavy work before 2xx.
8. **SSRF** on push URLs and unfetched `url` parts.
9. **Forwarding user tokens** downstream — use scoped exchange instead.
10. **ListTasks/GetTask without tenant-scoped auth** — classic multi-tenant leak.
11. **In-memory task store in production** — tasks vanish on deploy.
12. **Trusting remote agent text** — prompt injection crosses agent boundaries.
13. **A2A inside one codebase** — function calls or MCP are simpler; A2A pays off at org boundaries.

## Quick check

1. What does the Agent Card provide that MCP's `tools/list` does not, and why does that matter for
   cross-company delegation?

2. Name the two **interrupted** and four **terminal** task states. What happens if you
   `SendMessage` to a terminal task?

3. When should content go in a **`data` part** vs an **artifact**? Why does streaming make that
   distinction important?

4. How does v1.0 signal **end of an SSE task stream** without the v0.3 `final` flag? What should a
   client do after disconnect mid-task?

5. List two **server-side** and two **client-side** risks of push notifications and one mitigation
   for each.
