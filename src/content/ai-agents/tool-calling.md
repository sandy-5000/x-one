---
title: "Tool calling"
order: 4
---
# Tool calling (function calling)

Tool calling lets a language model **request that your code run a function**—query a database, search
docs, create a ticket—then continue with the result. The model never executes anything; it emits a
structured call. Your application validates, authorizes, executes, and returns output tagged with a call
id.

Every agent and "chat with your data" product builds on this loop. Understanding wire format, parallel
calls, errors, idempotency, and human gates is most of agent engineering after plain software design.

```text
1. app → model : messages + tool definitions (name, description, JSON Schema)
2. model → app : text (optional) + tool call(s) {id, name, arguments}
3. app         : validate, authorize, execute (or gate), capture result/error
4. app → model : full history + tool results per call id
5. repeat until text answer or iteration limit
```

## Key points

- The model is **stateless**: each request resends history—context, cost, and latency grow every round.
- Tool definitions are **prompt tokens** on every call; prune, namespace, scope by mode, or use tool
  search when lists grow large.
- Tool calls are **untrusted model output**—wrong tool, bad args, injection via tool results; validate
  and authorize in code.
- **JSON Schema** is both contract and documentation for the model; use `enum`, clear descriptions,
  strict mode where supported—still validate semantics after.
- **Parallel tools**: run independent calls concurrently; one result per call id; disable parallel when
  ordering or shared writes matter.
- Return **errors as tool results** (`is_error` / JSON error payload), not thrown exceptions—so the
  model can recover.
- **Idempotency** for writes: keys from run id + call id; prefer declarative updates; prepare/confirm
  for irreversible actions.
- **Human-in-the-loop** for external/irreversible ops—show exact args; rejection becomes a result the
  model can handle.

---

## Under the hood

### Definitions in the prompt

Providers serialize tool schemas into the chat template (special tokens, XML/JSON blocks). Implications:

- Schemas consume input tokens every turn—cache stable system + tools prefix.
- Changing the tool list can invalidate prompt cache; OpenAI `allowed_tools` / deferred loading reduce
  churn.
- Self-hosted models need the matching **tool-call parser** or calls appear as plain text.

### Training behaviour

Capability comes from SFT on tool transcripts and RL on agent tasks (when to call, recover, stop).
Behaviour is **probabilistic**—names, descriptions, and system prompt shift selection probabilities.

### Stop reasons (handle all)

| Situation | Typical signal |
| --- | --- |
| Wants tools | `tool_use` / `tool_calls` / function_call items |
| Done | `end_turn` / `stop` / no calls |
| Truncated mid-call | `max_tokens` / `length`—**do not execute partial JSON**; retry with higher limit |
| Refusal | `refusal` / content filter |

### Strict mode (constrained decoding)

Schema compiles to a grammar; illegal tokens masked each step—**well-formed arguments, not correct
semantics**. Still check IDs exist, user is authorized, paths stay in workspace.

| Guarantees | Does not guarantee |
| --- | --- |
| Parses as JSON matching schema subset | Right tool chosen |
| Enum values from allowed set | Values refer to real, permitted resources |

OpenAI strict: all properties `required`; optional via nullable types. Anthropic strict: subset of JSON
Schema; put numeric bounds in descriptions and validate in code.

---

## Wire formats (conceptual)

### Anthropic Messages

- Tools: `name`, `description`, `input_schema`, optional `strict`.
- Response: `tool_use` blocks with parsed `input` and `id`; parallel calls in one assistant message.
- Results: **`user`** message with `tool_result` blocks first (matched by `tool_use_id`); `is_error:
  true` for failures.

### OpenAI Responses

- Flat function tools: `parameters`, `strict`, `parallel_tool_calls`.
- Output: `function_call` items with **`call_id`** and JSON-string **`arguments`** (you parse).
- Results: `function_call_output` per `call_id`; keep **reasoning** items when replaying history.

### Chat Completions (common in OSS stacks)

- Nested `function.parameters`; reply with `role: tool` messages and `tool_call_id`.

Side-by-side essentials:

| | Anthropic | OpenAI Responses |
| --- | --- | --- |
| Schema field | `input_schema` | `parameters` |
| Args in response | Object `input` | JSON string `arguments` |
| Result carrier | `tool_result` in `user` | `function_call_output` item |
| Parallel off | `disable_parallel_tool_use` | `parallel_tool_calls: false` |

Streaming: buffer argument deltas until block complete—never execute on partial JSON.

---

## The tool loop (production shape)

Minimum loop responsibilities:

1. **Registry** mapping name → handler + Pydantic/Zod schema.
2. **Validate** args even with strict mode (patterns, ranges, business rules).
3. **Authorize** as end user—inject tenant/user from session, not from model args.
4. **Execute** with per-tool timeout; truncate large results with guidance to narrow.
5. **Return** structured result or actionable error text; never leak stack traces.
6. **Cap iterations**; log hitting cap as incident (loop or unrecoverable error).
7. **Handle** truncated tool calls, refusals, and provider-specific pause turns (server tools).

Parallel pattern: `asyncio.gather` / `Promise.all` with isolated failures; results ordered by call id
matching, not execution order.

Separate **SDK transport retries** (429/5xx) from **model logical retries** after reading error results.

---

## JSON Schema design for tools

The schema is read by the model like API docs.

Guidelines:

- **`enum`** for every closed set; shallow nesting; avoid deep `oneOf` trees in strict modes.
- **Descriptions** say what, when to use, when **not** to use, units, pagination behaviour.
- **Required** only what the tool truly needs; inject server-known fields (user id, tenant) in code.
- Generate schema from types (Pydantic/Zod) so validator and definition never drift.
- Task-shaped tools beat 40 REST mirrors; avoid god-tool `execute(action, blob)`.

Return values:

- High signal JSON or concise text; stable ids (`ORD-48121`); `has_more` + cursor for lists.
- Actionable errors: "No order ORD-123; ids look like ORD-#####; use orders_search by email."

Verbosity enum (`concise` / `detailed`) saves tokens on hot paths.

---

## tool_choice

| Intent | Anthropic | OpenAI Responses |
| --- | --- | --- |
| Model decides | `auto` | `auto` |
| Must call some tool | `any` | `required` |
| Force one tool | `tool` + name | `function` + name |
| No calls | `none` | `none` |
| Subset without rewriting defs | send fewer tools | `allowed_tools` |

Forced tool use skips preamble and may skip extended thinking; some Claude models reject forced choice
with 400—use structured outputs for fixed JSON shape, tools when execution is required. Changing
`tool_choice` between turns can break prompt cache for message blocks; keep policy stable within a
session when caching.

---

## Parallel tool calls

**When parallel**: independent reads (weather in two cities, two lookups).

**When serial**: shared mutable state, ordering dependencies, non-idempotent writes without keys.

Rules:

- Exactly **one result per call id**—missing result → API 400 on Anthropic.
- Anthropic: `tool_result` blocks before any user text in that message.
- Timeouts per call so one hung HTTP does not stall the turn.
- Start slow tools as soon as each streamed call completes (latency win).

---

## Error handling back to the model

| Error class | Return to model | Retry policy |
| --- | --- | --- |
| Validation / not found | `is_error` + fix hint | Model may correct args |
| Rate limit / timeout | `is_error` + retry or alternate | Model or backoff |
| Internal crash | Generic "do not retry" | Fail closed; log incident |

Never raise through the loop for expected failures. Distinguish **model-fixable** vs **infrastructure**
so the model does not spin forever.

Truncate results with explicit marker: `[truncated: use filter X or page+1]`.

---

## Idempotency and side effects

Models retry; loops resume; parallel duplicates happen.

- **Reads**: pure, cacheable where safe.
- **Writes**: idempotency key `f"{run_id}:{tool_call_id}"` stored server-side.
- Prefer **declarative** `set_status(closed)` over relative `increment`.
- **Prepare / confirm**: `prepare_refund` returns preview token; `confirm_refund(token)` after human
  approval.

Persist executed call ids so crash recovery does not double-charge.

---

## Human-in-the-loop

| Risk tier | Examples | Policy |
| --- | --- | --- |
| Read-only internal | search docs, get order | Auto-run |
| Reversible write | draft email, branch | Auto-run + audit |
| Irreversible / external | send email, pay, deploy | Confirm exact args |
| Catastrophic | drop table, large transfer | Do not expose as tool |

Flow: loop pauses on gated tool → UI shows preview → approve executes / reject returns
`User declined: reason` as tool result.

Avoid confirmation fatigue—narrow what requires a click.

---

## Security essentials

```text
untrusted (user, web, email, tool results) → LLM → tool call → YOUR validate/authorize/execute
```

- **Indirect injection** in tool results: architectural defenses—least capability, no lethal trifecta
  (private data + untrusted content + exfiltration channel), egress allow-lists, confirmations.
- **Confused deputy**: never trust `user_id` / `tenant_id` from arguments; use session.
- Argument hygiene: parameterized SQL, no `shell=True`, path prefix checks, SSRF blocks on URL tools.
- Sandbox code execution: isolated VM/container, no secrets inside, network off by default.
- Audit log: user, tenant, tool, call id, redacted args, decision, latency.

---

## Scale: how many tools?

Selection accuracy degrades past roughly **20–50** tools in context (provider guidance varies).

Mitigations (in order):

1. Consolidate and remove unused tools.
2. Scope tools by page, role, or agent mode.
3. Route: cheap classifier picks domain tool subset.
4. **Tool search / defer_loading**: catalog searched on demand; only matched defs enter prompt.
5. Programmatic calling: model writes short script in sandbox—intermediate results stay out of context.

---

## Server-side vs client tools

| | Client (yours) | Server (provider) |
| --- | --- | --- |
| Execution | Your infra | Provider sandbox/search |
| Control | Auth, logging, data residency | Config only |
| Examples | CRM, internal DB | Web search, code interpreter |

Built-ins still produce content that is **untrusted** for downstream client tools—apply same injection
mindset.

---

## Testing and evaluation

1. **Unit tests** per tool: validation, auth, pagination, idempotency, error messages.
2. **Scripted fake model** tests loop: parallel, missing tool, iteration cap, confirmation gate.
3. **Model evals**: tool selection accuracy, argument structural match, should-not-call cases, recovery
   after injected failure, end-to-end success—multiple runs per case in CI.

Read transcripts when metrics regress—descriptions and overlap dominate many failures.

---

## Latency and cost

Each iteration resends system + tools + **all prior calls and results**.

Levers: prompt cache on stable prefix; parallel reads; task-shaped tools; paginate results; summarize
or drop stale tool outputs in long sessions; smaller model for simple tool turns; stream UI progress.

---

## Relation to MCP

Tool calling is the **model-facing** API: how the LLM requests an action in provider syntax. The
**Model Context Protocol (MCP)** is the **integration-facing** standard for discovering and invoking
tools hosted in separate processes over JSON-RPC (`tools/list`, `tools/call`). Your agent maps MCP tool
schemas to provider tool definitions, routes model-chosen calls to the right MCP server, and wraps
responses as `tool_result` / `function_call_output`. MCP standardizes plug-in servers (GitHub, Postgres,
internal APIs); it does **not** make tool results trusted—same validation, authorization, confirmation,
and injection defenses apply. Malicious or mis-described MCP servers remain a supply-chain risk: pin,
review, and least-privilege each connection.

---

## Pitfalls

- Missing `tool_result` for one of several parallel calls.
- Executing arguments truncated by `max_tokens`.
- Exceptions escaping the loop instead of error results.
- Taking identity fields from model arguments.
- Mirroring every REST endpoint as a thin tool.
- Returning 200 KB raw API payloads without pagination.
- Vague errors causing blind retry loops.
- No iteration cap or per-tool timeout.
- Reordering tool defs every request and breaking prompt cache.
- Dropping OpenAI reasoning items when continuing a session.
- Trusting strict mode for correctness or authorization.
- Evaluating only happy path once per prompt.

---

## Quick check

**1. End-to-end, what happens on a tool call?**  
App sends schemas + messages; model emits structured call; app validates, authorizes, executes; app
appends assistant turn and results by call id; repeats until text answer or limit. Model never runs code.

**2. What does strict mode guarantee?**  
Syntactically valid arguments per supported schema—not semantic truth, authorization, or correct tool
choice.

**3. How handle parallel calls safely?**  
Concurrent execution with per-call timeouts; isolated failures; one result per id; disable parallel when
writes depend on order or shared state.

**4. How should tools report failure?**  
Normal result with error flag/payload explaining what failed and what to try next; no stack traces;
separate infra failures from fixable user/input errors.

**5. Why idempotency keys on writes?**  
Retries and resumed loops replay calls; keys derived from run + call id make duplicate execution safe.

**6. When require human confirmation?**  
Irreversible or external effects (send, pay, delete, deploy)—show exact arguments; rejection returned as
tool result for the model to adapt.

**7. What is indirect prompt injection via tools?**  
Instructions embedded in fetched content (web, email) that the model may obey; mitigate with least
privilege, confirmations, egress controls, quarantine patterns—not prompt wording alone.

**8. MCP vs function calling in one sentence?**  
Function calling is how the model requests an action; MCP is how your app discovers and invokes tools on
external servers—your loop still validates, authorizes, and formats results for the provider API.
