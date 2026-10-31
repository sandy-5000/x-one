---
title: "A2A (Agent2Agent protocol)"
order: 8
---
# A2A — Agent2Agent Protocol

A2A is an open protocol for **agents talking to other agents**, even when they are built with different frameworks, by different companies, and run on different servers. Google announced it in April 2025 and donated it to the **Linux Foundation** shortly after; many vendors now support it.

## MCP vs A2A

They solve different problems and are used together.

| | MCP | A2A |
| --- | --- | --- |
| Connects | Agent ↔ tools and data | Agent ↔ agent |
| Other side is | A tool server: predictable functions | An autonomous agent: reasons, may ask questions, takes time |
| Unit of work | Tool call (request → result) | Task with a lifecycle (submitted → working → completed) |
| Interaction | Mostly single request/response | Multi-turn, long-running, streaming, async |
| Opacity | Server exposes its tools | Remote agent keeps its tools, memory, and prompts private |

```text
                 A2A                              A2A
 User ─► Travel agent ◄──────────► Flight agent        Hotel agent
          │    (client)              │                   ▲
          │ MCP                      │ MCP               │ A2A
          ▼                          ▼                   │
    calendar, maps            airline APIs        Travel agent
```

## Core concepts

- **Agent Card** — a public JSON document describing the agent: name, endpoint, skills, capabilities, and auth requirements. Clients use it for discovery.
- **Client agent / remote agent** — the one asking and the one doing the work.
- **Task** — the unit of work, with an ID and a state. Tasks can be short or run for hours.
- **Message** — one turn of communication, from `user` (the client) or `agent` (the remote), made of **parts**.
- **Part** — a piece of content: `text`, `file` (bytes or URI), or `data` (structured JSON).
- **Artifact** — an output the task produces (a report, an image, a JSON result), also made of parts.
- **Context ID** — groups related tasks and messages into one conversation.

## The Agent Card

Published at a well-known URL, `https://<host>/.well-known/agent-card.json`:

```json
{
  "protocolVersion": "0.3.0",
  "name": "Flight Booking Agent",
  "description": "Searches and books flights; handles changes and cancellations.",
  "url": "https://flights.example.com/a2a",
  "version": "2.1.0",
  "capabilities": { "streaming": true, "pushNotifications": true },
  "defaultInputModes": ["text/plain", "application/json"],
  "defaultOutputModes": ["text/plain", "application/json"],
  "skills": [
    {
      "id": "search-flights",
      "name": "Search flights",
      "description": "Find flights between two cities for given dates and budget.",
      "tags": ["travel", "flights"],
      "examples": ["Find a Bangalore to Delhi flight on Friday under ₹6000"]
    },
    {
      "id": "book-flight",
      "name": "Book a flight",
      "description": "Book a previously found itinerary for a passenger.",
      "tags": ["travel", "booking"]
    }
  ],
  "securitySchemes": {
    "oauth": { "type": "oauth2", "flows": { "clientCredentials": { "tokenUrl": "https://auth.example.com/token", "scopes": {} } } }
  },
  "security": [{ "oauth": [] }]
}
```

An authenticated **extended card** can reveal extra skills to authorised clients.

## Task lifecycle

```text
submitted ──► working ──┬──► completed
                        ├──► failed
                        ├──► canceled
                        ├──► rejected
                        ├──► input-required ──(client replies)──► working
                        └──► auth-required  ──(client authorises)──► working
```

`input-required` is what makes A2A conversational: the remote agent can stop and ask "Window or aisle?" and resume when the client answers in the same task.

## Protocol operations

A2A runs over HTTPS. The original binding is **JSON-RPC 2.0**; gRPC and HTTP+JSON (REST-style) bindings were added later. The JSON-RPC method names (v0.3) are:

| Method | Purpose |
| --- | --- |
| `message/send` | Send a message; get back a Task (or a direct Message) |
| `message/stream` | Same, but stream updates over SSE |
| `tasks/get` | Poll a task's current state and artifacts |
| `tasks/cancel` | Ask the agent to stop |
| `tasks/resubscribe` | Re-attach to a task's stream after a disconnect |
| `tasks/pushNotificationConfig/set` | Register a webhook for updates on long tasks |

Method names and versions evolve; check the current spec at a2a-protocol.org before building.

### Sending a message

```json
{
  "jsonrpc": "2.0",
  "id": "req-1",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "messageId": "msg-001",
      "parts": [
        { "kind": "text", "text": "Find a flight from Bangalore to Delhi this Friday, morning, under ₹6000." }
      ]
    }
  }
}
```

### Response: the agent needs more input

```json
{
  "jsonrpc": "2.0",
  "id": "req-1",
  "result": {
    "kind": "task",
    "id": "task-42",
    "contextId": "ctx-9",
    "status": {
      "state": "input-required",
      "message": {
        "role": "agent",
        "messageId": "msg-002",
        "parts": [{ "kind": "text", "text": "Found 3 options. Do you want a non-stop flight only?" }]
      }
    }
  }
}
```

The client answers with another `message/send` carrying `"taskId": "task-42"` and `"contextId": "ctx-9"`. When done, the task's state is `completed` and `artifacts` hold the result:

```json
"artifacts": [
  {
    "artifactId": "itinerary-1",
    "name": "Selected itinerary",
    "parts": [
      { "kind": "data", "data": { "flight": "6E-2041", "departs": "07:10", "price_inr": 5420 } }
    ]
  }
]
```

## Interaction styles

- **Request/response** — `message/send`, then poll `tasks/get` if it's not finished.
- **Streaming** — `message/stream` returns an SSE stream of `TaskStatusUpdateEvent` and `TaskArtifactUpdateEvent` items until the task ends.
- **Push notifications** — for very long tasks, the client registers a webhook; the remote agent POSTs updates there (signed / authenticated), and the client fetches the task when notified.

## Building with A2A

Official SDKs exist for Python, JavaScript/TypeScript, Java, Go, and .NET, and frameworks such as Google ADK, LangGraph, CrewAI, and others can expose agents over A2A.

A server roughly needs:

1. An **Agent Card** endpoint.
2. An **executor** that receives a message and drives your agent (any framework).
3. A **task store** (in-memory for dev, a database for production) so tasks survive restarts and can be polled.
4. An event queue to emit status and artifact updates for streaming.

```python
# Sketch based on the a2a-sdk Python package; check the SDK docs for exact names.
class FlightAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue):
        query = context.get_user_input()
        result = await flight_agent.run(query)        # your LangGraph/ADK/etc. agent
        await event_queue.enqueue_event(new_agent_text_message(result))

    async def cancel(self, context, event_queue):
        raise UnsupportedOperationError()
```

## Security and operations

- Standard web security: HTTPS, OAuth 2.0 / OpenID Connect, API keys, or mTLS, declared in the Agent Card.
- Authorise **per skill** — booking needs more privilege than searching.
- Treat messages from other agents as **untrusted input** (prompt injection works agent-to-agent too).
- Put timeouts and budgets on delegated tasks; a remote agent can run for a long time.
- Trace across agents by propagating trace IDs (OpenTelemetry).

## When to use A2A

- Delegating to an agent owned by **another team or company**, where you can't (and shouldn't) see its internals.
- Long-running, multi-turn work that needs status, cancellation, and follow-up questions.
- Building a marketplace or directory of agents discovered through Agent Cards.

For calling a deterministic function inside your own system, a plain tool or an MCP server is simpler.
