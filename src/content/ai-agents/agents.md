---
title: "AI agents"
order: 5
---
# AI Agents

An **agent** is an LLM running in a loop: it decides what to do next, uses tools to act, observes the results, and continues until the goal is reached. The model chooses the path; your code provides the tools and the guardrails.

> Workflow: the steps are fixed in code, and the LLM fills in pieces.
> Agent: the LLM decides the steps at runtime.

Use the simplest thing that works. Many "agent" problems are solved better by a fixed workflow.

## The agent loop

```text
          ┌──────────────────────────────────────────────┐
          ▼                                              │
 goal ─► THINK (model plans next step)                   │
          │                                              │
          ├── needs a tool? ─► ACT (call tool) ─► OBSERVE (result added to context)
          │
          └── done? ─► final answer
```

This is the **ReAct** pattern (Reason + Act). Stopping conditions matter: a final answer, a max number of steps, a budget, or a human hand-off.

## Building blocks

| Block | What it is | Example |
| --- | --- | --- |
| Model | The reasoning engine | A frontier LLM |
| Instructions | System prompt: role, goals, rules | "You are a support agent for…" |
| Tools | Functions to act on the world | search, run_sql, create_ticket |
| Memory | What it remembers across steps and sessions | Conversation, notes file, vector store |
| Environment | Where actions happen | Codebase, browser, terminal, APIs |
| Guardrails | Limits and checks | Allowed tools, approvals, budgets |

## Memory

- **Short-term (working) memory** — the current context window: messages, tool results.
- **Long-term memory** — persisted outside the model: a database of facts, a vector store of past conversations, or simply a `NOTES.md` file the agent reads and updates.
- **Context management** for long tasks:
  - Summarise or drop old tool outputs (compaction).
  - Keep a running to-do list or plan file.
  - Hand big sub-tasks to sub-agents with fresh context, and keep only their summaries.

## Workflow patterns

These come from practical guides such as Anthropic's "Building effective agents".

**Prompt chaining** — fixed sequence of LLM calls, each checking or transforming the last.

**Routing** — classify the input, then send it to a specialised prompt or model.

```text
input ─► router ─┬─► refund flow
                 ├─► technical flow
                 └─► general FAQ
```

**Parallelisation** — run independent sub-tasks at once (sectioning), or run the same task several times and vote (ensembling).

**Orchestrator–workers** — a lead model breaks the task into sub-tasks at runtime, delegates them, and merges the results.

**Evaluator–optimizer** — one model produces, another critiques, loop until the critique passes.

```text
generator ──► draft ──► evaluator ──► feedback ──┐
    ▲                                            │
    └────────────────────────────────────────────┘   until "approved"
```

## Planning strategies

- **Plan-and-execute** — write the full plan first, then run steps; re-plan when something fails.
- **Reflection** — after a step or at the end, ask the model to critique its own work and fix it.
- **Tree / graph search** — explore several candidate paths and keep the best (costly; used for hard reasoning tasks).

## Multi-agent systems

Several agents, each with its own instructions, tools, and context, cooperate.

- **Supervisor (hierarchical):** a manager agent delegates to specialists (researcher, coder, reviewer).
- **Handoffs:** an agent transfers the conversation to another agent better suited to it.
- **Peer-to-peer:** agents message each other directly — across organisations this is what the **A2A protocol** standardises.

Benefits: separation of concerns, parallel work, smaller focused contexts. Costs: more tokens, harder debugging, error propagation between agents. Start with one agent; split only when it clearly struggles.

## A minimal agent

```python
SYSTEM = """You are a research assistant. Use tools to find facts.
Stop and answer when you have enough evidence. Cite URLs."""

def run_agent(goal: str, max_steps: int = 10) -> str:
    messages = [{"role": "user", "content": goal}]
    for _ in range(max_steps):
        reply = llm(system=SYSTEM, messages=messages, tools=[web_search, fetch_page])
        messages.append(reply.as_message())
        if not reply.tool_calls:
            return reply.text
        for call in reply.tool_calls:
            result = execute(call)                  # sandboxed, logged
            messages.append(tool_result(call.id, result))
    return "Stopped: step limit reached."
```

## Frameworks

- **Provider SDKs** — OpenAI Agents SDK, Claude Agent SDK, Google ADK.
- **Graph / orchestration** — LangGraph, LlamaIndex workflows, Microsoft Agent Framework (successor to AutoGen and Semantic Kernel), CrewAI.
- **Protocols** — MCP for agent ↔ tools, A2A for agent ↔ agent.

Frameworks speed up prototypes but can hide prompts and control flow. Make sure you can see the exact messages being sent.

## Making agents reliable

- Give tools **clear descriptions and good error messages**.
- Cap steps, time, and spend.
- **Human-in-the-loop** for irreversible actions.
- Run in a **sandbox** (container, restricted file system, network allow-list).
- Trace every step (inputs, tool calls, outputs) — OpenTelemetry or an LLM tracing tool.
- Evaluate on realistic end-to-end tasks, not just single prompts.
