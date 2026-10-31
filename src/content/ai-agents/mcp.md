---
title: "MCP (Model Context Protocol)"
order: 6
---
# MCP — Model Context Protocol

MCP is an open protocol, introduced by Anthropic in November 2024 and now adopted across the industry, that standardises how AI applications connect to **tools, data, and prompts**. Think of it as **USB-C for AI apps**: write a server once, and any MCP-capable client (Claude, ChatGPT, Cursor, VS Code, custom agents) can use it.

```text
Without MCP:  M apps × N integrations = M×N custom connectors
With MCP:     M clients + N servers   = M+N implementations
```

## Architecture

```text
┌──────────────── Host (e.g. Cursor, Claude Desktop, your agent) ────────────────┐
│                                                                                │
│   LLM  ◄──►  MCP client 1  ◄──── stdio ────►  MCP server: filesystem           │
│              MCP client 2  ◄──── HTTP  ────►  MCP server: GitHub (remote)      │
│              MCP client 3  ◄──── HTTP  ────►  MCP server: Postgres             │
│                                                                                │
└────────────────────────────────────────────────────────────────────────────────┘
```

- **Host** — the AI application the user interacts with. It manages clients, permissions, and the LLM.
- **Client** — a connector inside the host; **one client per server**, one connection each.
- **Server** — a program exposing capabilities. Can be a local process or a remote web service.

Messages are **JSON-RPC 2.0**: requests (with an `id`), responses, and notifications (no `id`).

## Server primitives

| Primitive | Controlled by | What it is | Example |
| --- | --- | --- | --- |
| **Tools** | Model | Functions the LLM can call | `create_issue`, `run_query` |
| **Resources** | Application | Read-only data identified by URI | `file:///README.md`, `postgres://db/schema` |
| **Prompts** | User | Reusable prompt templates, often shown as slash commands | `/review-pr`, `/summarise-incident` |

## Client primitives

Servers can also ask the client for things:

- **Sampling** — the server asks the host's LLM to generate text (so the server needs no API key of its own).
- **Roots** — the client tells the server which directories or URIs it may work within.
- **Elicitation** — the server asks the *user* for structured input mid-task ("Which branch should I deploy?").

Plus utilities: progress notifications, cancellation, logging, pagination, and `list_changed` notifications when a server's tools change.

## Lifecycle

```text
client                                   server
  │ ── initialize {protocolVersion,         │
  │      capabilities, clientInfo} ───────► │
  │ ◄── result {protocolVersion,            │
  │      capabilities, serverInfo} ──────── │
  │ ── notifications/initialized ─────────► │
  │                                         │
  │ ── tools/list ────────────────────────► │
  │ ◄── [{name, description, inputSchema}]  │
  │ ── tools/call {name, arguments} ──────► │
  │ ◄── {content: [...], isError} ───────── │
  │                ...                      │
  │ ── (close transport) ─────────────────► │
```

**Capability negotiation** in `initialize` decides which features both sides support (tools, resources with subscriptions, prompts, sampling, elicitation, and so on).

### A tool call on the wire

```json
{ "jsonrpc": "2.0", "id": 7, "method": "tools/call",
  "params": { "name": "get_forecast", "arguments": { "city": "Pune" } } }
```

```json
{ "jsonrpc": "2.0", "id": 7,
  "result": {
    "content": [{ "type": "text", "text": "Pune: 31 °C, clear sky" }],
    "structuredContent": { "city": "Pune", "temp_c": 31, "sky": "clear" },
    "isError": false
  } }
```

## Transports

| Transport | How | Use for |
| --- | --- | --- |
| **stdio** | Host launches the server as a child process; JSON-RPC over stdin/stdout | Local tools: files, git, local DBs |
| **Streamable HTTP** | One HTTP endpoint (e.g. `/mcp`); client POSTs messages, server answers with JSON or an SSE stream | Remote / shared / hosted servers |

Streamable HTTP replaced the older "HTTP + SSE" two-endpoint transport. It can run **stateful** (with sessions) or **stateless** — see the next page.

## Writing a server

### TypeScript

```ts
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const server = new McpServer({ name: "weather", version: "1.0.0" });

server.registerTool(
  "get_forecast",
  {
    title: "Get forecast",
    description: "Current weather for a city.",
    inputSchema: { city: z.string().describe("City name") },
  },
  async ({ city }) => {
    const data = await fetchWeather(city);
    return { content: [{ type: "text", text: `${city}: ${data.temp_c} °C, ${data.sky}` }] };
  },
);

server.registerResource(
  "cities",
  "weather://cities",
  { title: "Supported cities", mimeType: "application/json" },
  async (uri) => ({ contents: [{ uri: uri.href, text: JSON.stringify(CITIES) }] }),
);

await server.connect(new StdioServerTransport());
```

### Python (FastMCP)

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("weather")

@mcp.tool()
def get_forecast(city: str) -> str:
    """Current weather for a city."""
    data = fetch_weather(city)
    return f"{city}: {data['temp_c']} °C, {data['sky']}"

@mcp.resource("weather://cities")
def cities() -> list[str]:
    return CITIES

@mcp.prompt()
def trip_brief(city: str) -> str:
    return f"Summarise the weather in {city} for a traveller packing tonight."

if __name__ == "__main__":
    mcp.run()          # stdio by default
```

### Registering it with a host

Most hosts use a JSON config like this (Cursor: `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "weather": {
      "command": "python",
      "args": ["weather_server.py"]
    },
    "github": {
      "url": "https://api.example.com/mcp",
      "headers": { "Authorization": "Bearer ${GITHUB_TOKEN}" }
    }
  }
}
```

Debug servers with the **MCP Inspector**: `npx @modelcontextprotocol/inspector python weather_server.py`.

## Security

- **Authorization** for remote servers uses **OAuth 2.1**: the MCP server is a resource server, tokens come from an authorization server, and clients discover it via protected-resource metadata.
- **Never pass the user's token through** to downstream APIs blindly (the "confused deputy" problem); tokens must be issued *for* your server.
- **Tool poisoning / prompt injection** — tool descriptions and tool results are text the model reads; a malicious server or document can smuggle instructions. Install servers only from sources you trust, and review what they expose.
- **Least privilege** — read-only scopes, allow-listed directories (roots), sandboxed processes.
- **Human approval** for side-effecting tools; hosts usually prompt before each call.
- Validate inputs on the server; never build shell commands or SQL by string concatenation.

## MCP vs plain function calling

| | Function calling | MCP |
| --- | --- | --- |
| Scope | Inside one app's code | Separate, reusable server |
| Discovery | Tools hard-coded in the request | Client lists tools at runtime |
| Extras | Tools only | Tools, resources, prompts, sampling, elicitation |
| Reuse | Re-implement per app | Any MCP client can connect |

MCP doesn't replace function calling — the host still turns MCP tools into function-calling definitions for its model. MCP standardises **where tools come from**.
