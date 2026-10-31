---
title: "AI agents"
order: 5
---
# AI agents

An **agent** is an LLM that runs in a loop. At each step it decides what to do next, uses tools
to act on an environment, observes the results, and continues until the goal is met or a limit
stops it. The model chooses the path at runtime; your code supplies the tools, the context, the
limits, and the guardrails.

These notes assume the mechanics of tool calling (see the Tool calling page) and focus on
everything built on top: loops, memory, planning, multi-agent design, reliability, evaluation,
security, and operations.

> Ecosystem facts (framework versions, protocol revisions) were verified in October 2026. They
> change quickly, so check the current docs before relying on a specific API.

## What an agent is (and isn't)

### Chatbot vs workflow vs agent

| | Chatbot | Workflow | Agent |
| --- | --- | --- | --- |
| Who decides the steps | Nobody: one prompt, one reply | **Your code** (fixed DAG) | **The model** at runtime |
| Tools | None or one | Called at known points | Chosen dynamically, in a loop |
| Number of LLM calls | 1 | Known upper bound | Unbounded (capped by limits) |
| Predictability | High | High | Lower |
| Cost and latency | Lowest | Predictable | Variable, often 4–15x a chat |
| Good for | Q&A, drafting | Well-understood, repeatable processes | Open-ended tasks where the path can't be known in advance |
| Example | FAQ bot | Classify ticket, then retrieve, then draft reply | "Fix this failing test", "research this market" |

Anthropic's *Building effective agents* draws the same line: **workflows** orchestrate LLMs and
tools through predefined code paths; **agents** dynamically direct their own processes and tool
usage. Its main advice is to **use the simplest thing that works**. Start with a single LLM
call plus retrieval, move to a workflow when the steps are known, and use an agent only when
flexibility is worth the cost, latency, and risk.

### Autonomy levels (an informal scale)

| Level | Name | What the model controls | Human role |
| --- | --- | --- | --- |
| 0 | Chat | Text only | Does everything |
| 1 | Tool-augmented | One tool call per request | Initiates each request |
| 2 | Workflow | Fills steps in a code-defined graph | Designs the graph |
| 3 | Supervised agent | Chooses steps; side effects need approval | Approves risky actions |
| 4 | Bounded autonomous agent | Chooses and executes within sandbox, budget, and permissions | Sets goals; reviews outcomes |
| 5 | Open-ended agent | Sets sub-goals over long horizons | Occasional oversight |

Most production systems sit at levels 2–4, and the level can vary *per action*. A coding agent
edits files autonomously (level 4) but asks before `git push` (level 3).

## The agent loop in depth

```text
        ┌──────────────────────────────────────────────────────────┐
        │                                                          │
        ▼                                                          │
 ┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌───────────┴─┐
 │  PERCEIVE   │──►│    PLAN     │──►│     ACT     │──►│   OBSERVE   │
 │ goal, ctx,  │   │ choose next │   │ tool call / │   │ result into │
 │ memory      │   │ step        │   │ ask human   │   │ context     │
 └─────────────┘   └──────┬──────┘   └─────────────┘   └─────────────┘
                          │ done, or a limit is hit
                          ▼
                     final answer / handoff / escalation
```

- **Perceive.** Assemble the context for this step: system instructions, the goal, relevant
  memory, recent history, and the latest observations. *What you put in the window is the main
  lever you control* (see Context engineering below).
- **Plan.** The model reasons about the next step. This may be implicit (thinking tokens) or
  explicit (an updated to-do list).
- **Act.** The model emits tool calls (or a question to the user). The runtime validates,
  authorizes, and executes them.
- **Observe.** Results, including errors, are appended to the context. The environment, not the
  model's belief, is the source of truth: tests pass or fail, the API returns 404.

### ReAct: reasoning and acting interleaved

**ReAct** (Yao et al., 2022) showed that interleaving free-text *reasoning traces* with
*actions* beats both reasoning-only (chain-of-thought hallucinates facts) and acting-only (no
plan, no error analysis). Modern tool-calling models do ReAct natively: thinking or text
blocks, then tool calls, then results, then more thinking.

#### A full worked trace

Task: *"How many years older is the Eiffel Tower than the Golden Gate Bridge?"*
Tools: `web_search(query)`, `calculator(expression)`.

```text
[user]        How many years older is the Eiffel Tower than the Golden Gate Bridge?

[thought]     I need the completion/opening year of each structure, then subtract.
[action]      web_search({"query": "Eiffel Tower completion year"})
[observation] "The Eiffel Tower was completed in March 1889 for the Exposition
               Universelle..."

[thought]     Eiffel Tower: 1889. Now the Golden Gate Bridge.
[action]      web_search({"query": "Golden Gate Bridge opening"})
[observation] ERROR: search backend timeout (retry once or rephrase)

[thought]     Transient error. Retry with a more specific query.
[action]      web_search({"query": "Golden Gate Bridge opened to traffic year"})
[observation] "The Golden Gate Bridge opened on May 27, 1937 (pedestrians) and
               May 28, 1937 (vehicles)."

[thought]     1937 - 1889. Use the calculator rather than mental arithmetic.
[action]      calculator({"expression": "1937 - 1889"})
[observation] 48

[thought]     Both facts are sourced; the answer is consistent. Done.
[answer]      The Eiffel Tower (completed 1889) is 48 years older than the Golden
              Gate Bridge (opened 1937).
```

What to notice:

- Each action is **grounded** in an observation. The model does not assert years from memory.
- An **error is an observation**. The model adapted (rephrased and retried) because the tool
  said what to do.
- The model **decided when it was done**. That decision is exactly what can go wrong (premature
  "done", or never done), which is why you need external stopping conditions.

### Stopping conditions

| Condition | Type | Notes |
| --- | --- | --- |
| Model returns a final answer (no tool calls) | Natural | The normal exit |
| Explicit `finish` / `submit` tool | Natural, structured | Lets you validate the final output against a schema |
| Success check passes (tests green, state matches) | Verified | Best when available: the environment decides, not the model |
| Max steps / iterations | Safety | Typical values 10–50 for assistants, hundreds for coding agents |
| Token or cost budget | Safety | Track cumulative input plus output tokens |
| Wall-clock timeout | Safety | For user-facing latency SLOs |
| Repetition detected (same call + args N times) | Safety | Catches loops on persistent errors |
| Human interrupt / approval rejected | Control | Pause, persist, resume or abort |
| Policy violation (guardrail tripwire) | Control | Abort and escalate |

On a safety stop, **return a structured partial result** ("stopped after 30 steps; completed
X and Y; blocked on Z") instead of failing silently.

## Architecture components

```text
┌──────────────────────────────────────────────────────────────────────────┐
│                      ORCHESTRATOR / RUNTIME                              │
│  loop control · limits & budgets · checkpoints · retries · tracing       │
│                                                                          │
│  ┌──────────────┐    ┌──────────────────────┐    ┌────────────────────┐  │
│  │ INSTRUCTIONS │───►│        MODEL         │───►│      PLANNER       │  │
│  │ role, goals, │    │ reasons, picks next  │    │ todo list / plan / │  │
│  │ policies     │    │ action, writes answer│    │ sub-task graph     │  │
│  └──────────────┘    └──────────┬───────────┘    └────────────────────┘  │
│                                 │ tool calls                             │
│  ┌──────────────┐    ┌──────────▼───────────┐    ┌────────────────────┐  │
│  │    MEMORY    │◄──►│      GUARDRAILS      │───►│       TOOLS        │  │
│  │ context,     │    │ validate, authorize, │    │ search, code, APIs,│  │
│  │ notes, store │    │ confirm, budget      │    │ MCP servers        │  │
│  └──────────────┘    └──────────────────────┘    └─────────┬──────────┘  │
└────────────────────────────────────────────────────────────┼─────────────┘
                                                             ▼
                                          ENVIRONMENT: repo, browser, DB,
                                          SaaS APIs, OS, other agents, people
```

| Component | Responsibility | Design questions |
| --- | --- | --- |
| Model | Reasoning and choosing actions | Which model per step? Thinking budget? Fallback model? |
| Instructions | Role, goal, policies, output format, examples | What is static (cacheable) vs dynamic? |
| Tools | Act on and read the environment | Granularity, schemas, errors, permissions (see Tool calling) |
| Memory | What persists within and across runs | What is stored, retrieved, forgotten? Who can write? |
| Planner | Decomposes goals and tracks progress | Implicit (in model) or explicit (plan object / todo file)? |
| Environment | Where actions take effect | Sandbox? Reversible? Observable? |
| Guardrails | Input/output checks, tool policies, approvals | Which checks block vs log? Run in parallel or before? |
| Orchestrator | The loop: limits, state, persistence, tracing | Durable? Resumable? Multi-agent routing? |

## Memory in depth

The CoALA paper (*Cognitive Architectures for Language Agents*, Sumers et al., 2023) gives a
useful taxonomy borrowed from cognitive science:

| Memory type | Holds | Agent implementation | Example |
| --- | --- | --- | --- |
| **Working** | The current task state | The context window: messages, tool results, scratchpad | Last 20 turns, the current plan |
| **Episodic** | Past experiences | Logs of past runs and trajectories, retrievable | "Last time this deploy failed because of X" |
| **Semantic** | Facts about the world and user | Knowledge base, vector store, key-value facts, RAG | "User prefers metric units"; product docs |
| **Procedural** | How to do things | System prompt, tool code, skills / playbooks, model weights | "To release: run tests, bump version, tag" |

### Working memory: the context window

The context window is the only memory the model can *directly* use. Everything else must be
**retrieved into it**. Properties:

- **Finite and expensive.** Every token is re-processed on every step (prompt caching reduces
  the cost, not the attention load).
- **Degrades with length.** Long contexts suffer from "context rot": recall and instruction
  following get worse as irrelevant tokens accumulate, and information in the middle is used
  less reliably than at the edges.
- **Order matters for caching.** Put stable content (system prompt, tools) first and volatile
  content last.

### Episodic and semantic memory: retrieval stores

- **Vector memory.** Embed memories (facts, summaries of past sessions) and retrieve the top-k by
  similarity to the current query. Retrieval quality improves when you **score by more than
  similarity**. The *Generative Agents* paper (Park et al., 2023) combined **recency,
  importance, and relevance** into one retrieval score.
- **Structured memory.** Key-value or relational facts (`user.timezone = "Asia/Kolkata"`), or
  a knowledge graph for entities and relations. These are exact, updatable, and easy to show
  to the user.
- **Hybrid.** Structured profile facts, plus vector search over episodes, plus keyword search
  for exact ids.

### Summarization and compaction

Long-running agents exceed any window. Compaction strategies, from cheapest to richest:

1. **Drop or elide old tool results.** Keep the call ("read_file(src/app.py)") and replace the
   bulky result with a stub ("[elided; re-read if needed]"). This is cheap and very effective,
   because tool results dominate token usage.
2. **Sliding window.** Keep the last N turns plus the original task.
3. **Rolling summary.** When usage passes a threshold (say 70–80% of the window), ask the model
   to summarize the conversation so far: decisions made, open questions, current state, and
   next steps. Then continue from *summary + recent turns*. Several agent SDKs and harnesses
   ship this built in.
4. **Hierarchical memory.** MemGPT (Packer et al., 2023) treats the context like RAM and
   external storage like disk, with the model paging information in and out via tools.

Compaction is lossy. Preserve the **original goal verbatim**, decisions and their reasons,
unresolved errors, and ids or paths needed to resume. Compaction also invalidates the prompt
cache from the edit point onward, so do it in occasional large steps rather than on every turn.

### Scratchpads and files as memory

The most robust long-horizon memory is often **plain files the agent reads and writes**:

- `PLAN.md` / a todo list: the current plan with checkboxes, re-read at every step.
- `NOTES.md` / `MEMORY.md`: durable learnings ("tests need `DB_URL` set").
- Project instruction files (for example `AGENTS.md` or `CLAUDE.md` in coding agents):
  procedural memory curated by humans and agents.

Files are inspectable, diffable, version-controlled, survive compaction and crashes, and need no
vector database. Anthropic's context-engineering guidance calls this **structured
note-taking**.

### Memory write policies

Reading memory is easy; deciding what to *write* is the hard part.

- **What to store**: stable preferences, verified facts, outcomes of past tasks, and lessons from
  failures. **Not** raw transcripts by default, transient state, or secrets.
- **When**: at the end of a task (reflection step), on explicit user statements ("remember
  that…"), or through a dedicated `remember(fact)` tool with a schema.
- **Metadata**: source, timestamp, confidence, scope (user / team / global), and TTL.
- **Conflicts**: newer facts supersede older ones; deduplicate on write (embedding similarity
  above a threshold means update, not insert).
- **Forgetting**: TTLs, decay, and user-visible delete. Privacy laws may require deletion.
- **Poisoning risk**: anything written from untrusted content (a web page) can plant
  instructions that fire in future sessions. Treat memory written from untrusted sources as
  untrusted, scope memory per user, and consider human review for shared or global memory.

## Context engineering for long-running agents

**Context engineering** is the discipline of curating the smallest set of high-signal tokens that
makes the next step likely to succeed. For agents it matters more than prompt wording.

Techniques:

- **Right-altitude instructions.** Write clear principles and heuristics, not brittle if-else
  scripts, and not vague platitudes either.
- **Minimal, non-overlapping tools.** If a human can't say which tool applies, neither can the
  model.
- **Just-in-time retrieval.** Keep lightweight references (paths, URLs, ids) in context and load
  the content only when needed, with `grep`, `head`, or a query. Coding agents navigate repos
  this way instead of embedding them up front.
- **Progressive disclosure.** Expose summaries first, with a way to drill down (directory
  listing, then file, then function).
- **Compaction plus note-taking** (above) for tasks that outlive the window.
- **Sub-agents for isolation.** Delegate a bulky sub-task (searching 50 files, reading 30 web
  pages) to a sub-agent with a fresh context. It returns a condensed result of a few hundred to
  a few thousand tokens, and the main agent's context stays clean.
- **Keep the prefix stable** for cache hits: don't inject timestamps or random ids into the
  system prompt. Append dynamic state late.
- **Re-state the goal and plan near the end of the context** on long runs (for example by
  re-reading the todo file). Recent tokens get the most attention.

## Planning techniques

| Technique | Idea | Strength | Weakness | Use when |
| --- | --- | --- | --- | --- |
| Implicit (ReAct) | Plan one step at a time inside the loop | Adaptive, simple | Myopic on long tasks | Default for most agents |
| Plan-and-execute | Write a full plan, execute steps, re-plan on failure | Global coherence; cheaper executor models | Plans go stale; re-plan logic needed | Multi-step tasks with clear structure |
| ReWOO | Planner writes the whole tool plan with placeholders up front; workers run it without LLM calls; solver combines | Few LLM calls, low tokens | Can't adapt mid-run to surprises | Predictable tool chains, cost-sensitive |
| Reflection / Reflexion | Critique own output or trajectory; store lessons; retry | Fixes errors; learns across trials | Extra calls; self-critique can be confidently wrong | Tasks with a feedback signal (tests, evaluator) |
| Self-critique / Self-Refine | Generate, critique, revise in one session | Easy quality boost | Diminishing returns; same blind spots | Writing, code review, formatting |
| Tree search (ToT, LATS) | Explore multiple branches; score; backtrack | Best on hard reasoning and search | Expensive (many rollouts); needs a value signal | Puzzles, hard planning, offline tasks |

### Plan-and-execute

```python
def plan_and_execute(goal: str, max_replans: int = 3) -> str:
    plan = llm_json(f"Break this goal into 3-7 concrete steps. Goal: {goal}",
                    schema={"steps": ["string"]})["steps"]
    done: list[tuple[str, str]] = []
    for _ in range(max_replans + 1):
        while plan:
            step = plan.pop(0)
            result = run_agent(step, context=done)          # a small ReAct loop per step
            done.append((step, result))
            if result.startswith("FAILED"):
                break
        else:
            return llm(f"Goal: {goal}\nCompleted steps: {done}\nWrite the final answer.")
        plan = llm_json(f"Goal: {goal}\nDone so far: {done}\nThe last step failed. "
                        "Write the remaining steps.", schema={"steps": ["string"]})["steps"]
    return f"Gave up after {max_replans} replans. Progress: {done}"
```

### ReWOO (Reasoning WithOut Observation)

The planner writes the plan *and* the tool calls with variables in a single LLM call:

```text
Plan: Find the Eiffel Tower completion year.       #E1 = web_search["Eiffel Tower completed"]
Plan: Find the Golden Gate Bridge opening year.     #E2 = web_search["Golden Gate opened"]
Plan: Subtract.                                     #E3 = calculator["#E2 - #E1"]
```

A worker substitutes the variables and executes the calls with no LLM involvement, and a
*solver* call writes the answer from the evidence. That is three or fewer LLM calls total,
versus one per step in ReAct. The trade-off: if `#E2` comes back empty, nothing adapts.

### Reflexion

**Reflexion** (Shinn et al., 2023) runs a trial, gets a signal (unit tests, an environment
reward, or a self-evaluation), and asks the model to write a **verbal reflection** on what went
wrong. It stores that reflection in episodic memory and retries with the reflections in
context. No weight updates are involved; it is "learning" via memory. It works best when the
feedback signal is **external and reliable** (tests), and much less well when the model must
judge its own success.

### Tree search: ToT and LATS

**Tree of Thoughts** (Yao et al., 2023) explores multiple reasoning branches with BFS or DFS
and self-evaluation. **LATS** (Language Agent Tree Search, Zhou et al., 2023) applies Monte
Carlo tree search over ReAct trajectories, using the LLM as both policy and value function, plus
reflections. These are powerful but cost many times more. In practice, "sample N candidates
and pick the best with a verifier" captures much of the benefit cheaply.

## Workflow patterns (from *Building effective agents*)

All five patterns build on the **augmented LLM**: one model call with retrieval, tools, and
memory available. The sketches assume two helpers: `llm(prompt) -> str` and
`llm_json(prompt, schema) -> dict`.

### 1. Prompt chaining

A fixed sequence of calls in which each step processes the previous output, with optional
programmatic **gates** between steps. It trades latency for accuracy by making each call easier.

```text
input ─► [LLM: outline] ─► gate: outline ok? ─► [LLM: draft] ─► [LLM: translate] ─► out
                               │ no
                               ▼
                          fail / retry
```

```python
def write_doc(topic: str) -> str:
    outline = llm(f"Write a 5-bullet outline for a doc about: {topic}")
    if outline.count("\n") < 4:                               # programmatic gate
        raise ValueError("outline too short")
    draft = llm(f"Write the doc following this outline:\n{outline}")
    return llm(f"Translate to French, keep markdown:\n{draft}")
```

Use it when the task decomposes cleanly into fixed subtasks (generate, then check, then
transform).

### 2. Routing

Classify the input, then dispatch to a specialized prompt, tool set, or model. This gives
separation of concerns, and it lets cheap models handle easy cases.

```text
            ┌─► [refund flow: billing tools, strict policy]
input ─► [router] ─┼─► [tech support: docs RAG, logs tool]
            └─► [general FAQ: small fast model]
```

```python
ROUTES = {"refund": handle_refund, "technical": handle_tech, "general": handle_faq}

def route(message: str) -> str:
    label = llm_json(f"Classify the support message: {message}",
                     schema={"label": {"enum": list(ROUTES)}})["label"]
    return ROUTES[label](message)
```

Use it when inputs fall into distinct categories that are better handled separately. A
classifier (LLM or traditional ML) can do the routing.

### 3. Parallelization

There are two variants. **Sectioning** splits a task into independent subtasks that run
concurrently. **Voting** runs the same task several times and aggregates the results.

```text
              ┌─► [LLM: check security] ──┐
input ──fan──►├─► [LLM: check perf]     ──├──► aggregate ─► report
              └─► [LLM: check style]    ──┘
```

```python
from concurrent.futures import ThreadPoolExecutor

ASPECTS = ["security vulnerabilities", "performance problems", "readability"]

def review(code: str) -> str:
    with ThreadPoolExecutor() as pool:
        findings = list(pool.map(
            lambda a: llm(f"Review only for {a}. List issues or say NONE.\n{code}"), ASPECTS))
    return llm("Merge these reviews into one prioritized list:\n" + "\n---\n".join(findings))

def is_unsafe(text: str, votes: int = 3) -> bool:              # voting variant
    with ThreadPoolExecutor() as pool:
        answers = list(pool.map(lambda _: llm(f"Is this unsafe? yes/no\n{text}"), range(votes)))
    return sum(a.strip().lower().startswith("yes") for a in answers) >= 2
```

Use it for independent aspects (guardrails running alongside the main response is a classic
case) or when you need confidence from multiple attempts.

### 4. Orchestrator–workers

A central LLM **decides the subtasks at runtime**, delegates them to workers, and synthesizes
the results. Unlike sectioning, the subtasks are not known in advance.

```text
                       ┌─► [worker: change file A] ─┐
task ─► [orchestrator] ┼─► [worker: change file B] ─┼─► [orchestrator: synthesize]
        plans subtasks └─► [worker: update tests]  ─┘
```

```python
def orchestrate(task: str) -> str:
    subtasks = llm_json(f"Split into independent subtasks: {task}",
                        schema={"subtasks": [{"id": "string", "instruction": "string"}]})
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda s: run_agent(s["instruction"]), subtasks["subtasks"]))
    return llm(f"Task: {task}\nWorker results:\n" + "\n\n".join(results) +
               "\nSynthesize the final result; note any conflicts.")
```

Use it for complex tasks whose decomposition depends on the input: multi-file code changes,
or research across an unknown number of sources.

### 5. Evaluator–optimizer

One call generates, another evaluates against criteria and gives feedback, and the loop runs
until the output passes or the iteration budget runs out.

```text
        ┌──────────────── feedback ────────────────┐
        ▼                                          │
task ─► [generator] ─► candidate ─► [evaluator] ───┤
                                       │ pass      │
                                       ▼           │ fail (max N rounds)
                                     output ◄──────┘ (best so far)
```

```python
def refine(task: str, rounds: int = 3) -> str:
    candidate = llm(task)
    for _ in range(rounds):
        verdict = llm_json(f"Task: {task}\nCandidate:\n{candidate}\n"
                           "Grade against the requirements.",
                           schema={"pass": "boolean", "feedback": "string"})
        if verdict["pass"]:
            return candidate
        candidate = llm(f"Task: {task}\nPrevious:\n{candidate}\n"
                        f"Fix these issues:\n{verdict['feedback']}")
    return candidate
```

Use it when there are clear evaluation criteria and iteration measurably helps (translation,
code that must pass checks, search that must be comprehensive). Prefer **programmatic
evaluators** (tests, linters, validators) over LLM judges where possible.

## Multi-agent systems

A multi-agent system is several LLM loops, each with its own instructions, tools, and **its own
context window**, that cooperate on a task.

### Topologies

```text
 SUPERVISOR (hierarchical)        HANDOFF (swarm)              BLACKBOARD
 ┌────────────┐                   [triage] ──► [billing]       ┌───────────────────┐
 │ supervisor │                       │            │           │  shared workspace │
 └─────┬──────┘                       └─► [tech] ◄─┘           │  (facts, drafts,  │
   ┌───┼────────┐                 control moves; the           │   task board)     │
   ▼   ▼        ▼                 active agent owns the        └──▲─────▲──────▲───┘
 [res] [code] [review]            conversation                    │     │      │
 workers return results                                         [A]   [B]    [C]
 to the supervisor                                              agents read/write
```

| Pattern | How it works | Good for | Watch out for |
| --- | --- | --- | --- |
| **Supervisor / hierarchical** | A manager plans and delegates to specialists (often exposed as tools, "agents as tools"), then integrates results | Research, broad tasks, enforcing a single owner of the answer | Supervisor becomes a bottleneck; summaries lose detail |
| **Handoffs / swarm** | The active agent transfers control (and the conversation) to another agent via a `transfer_to_x` tool | Customer support triage, phase changes (sales to support) | Ping-pong between agents; what history to pass |
| **Debate** | Several agents argue or critique; a judge decides | Reducing reasoning errors, red-teaming | Costly; can converge on a confident wrong answer |
| **Blackboard** | Agents read from and write to a shared store; a controller schedules them | Loosely coupled contributions | Coordination and consistency of shared state |
| **Pipeline** | Fixed sequence of agents (planner, coder, reviewer) | Known stages | Really a workflow; errors propagate downstream |

In the OpenAI Agents SDK, for example, a handoff is literally a tool named
`transfer_to_<agent_name>`, and **agents-as-tools** (`Agent.as_tool()`) keeps the manager in
control. That illustrates the key design choice: *does control transfer, or does the caller
keep ownership?*

### When multi-agent helps vs hurts

**Helps:**

- **Breadth-first, parallelizable work.** Research across many independent sources, where
  sub-agents explore in parallel with separate contexts.
- **Context isolation.** Each sub-agent burns its own window on raw material and returns only a
  condensed result, so the lead's context stays clean.
- **Separation of permissions.** A browsing sub-agent with no access to private data; a
  privileged agent that never sees raw web content.
- **Different models per role.** A strong model for the lead, cheaper models for workers.

**Hurts:**

- **Tightly coupled tasks** such as most coding, where every decision depends on the others.
  Cognition's essay *Don't Build Multi-Agents* (2025) argues that sub-agents lacking full
  context make **conflicting implicit decisions** (one builds a Mario-style background, another a
  mismatched bird) and that context should be shared as full traces, not summaries.
- **Token cost.** Anthropic reported that agents use about **4x** the tokens of chat, and its
  multi-agent research system about **15x**. That is only economical for high-value tasks.
- **Debugging and evaluation.** Non-determinism compounds across agents, and failures emerge
  from the interactions.
- **Error propagation.** A wrong sub-agent summary becomes a trusted "fact" for the lead.

Rule of thumb: **start with one agent and good tools.** Split when you hit context limits on
parallelizable work, when you need permission separation, or when one agent clearly struggles
with too many tools.

### How a research multi-agent system is structured

Based on Anthropic's write-up of its Research feature (2025):

```text
 user query
     │
     ▼
┌──────────────────────────────────────┐   saves plan to memory (survives
│ LEAD RESEARCHER (strong model)       │   context truncation)
│ - analyze query, write plan          │
│ - decide number of subagents and     │
│   effort per subagent                │
│ - spawn subagents with clear tasks:  │
│   objective, output format, sources, │
│   tool guidance, boundaries          │
└──┬──────────────┬──────────────┬─────┘
   ▼              ▼              ▼            (run in parallel, each with its own
┌──────────┐ ┌──────────┐ ┌──────────┐        context window and search tools)
│ subagent │ │ subagent │ │ subagent │
│ search → │ │ search → │ │ search → │        interleaved thinking: evaluate results,
│ evaluate │ │ evaluate │ │ evaluate │        refine queries, stop when sufficient
│ → report │ │ → report │ │ → report │
└────┬─────┘ └────┬─────┘ └────┬─────┘
     └────────────┼────────────┘
                  ▼
     lead synthesizes; decides if more research is needed (loop)
                  │
                  ▼
     CITATION AGENT: attaches sources to claims ──► final report
```

Reported lessons:

- The multi-agent setup (an Opus 4 lead with Sonnet 4 subagents) outperformed single-agent Opus 4
  by **90.2%** on Anthropic's internal research eval, mostly on breadth-first queries.
- **Token usage alone explained about 80% of the performance variance** on BrowseComp.
  Multi-agent works largely because it lets you *spend more tokens in parallel* across separate
  windows.
- Lead prompts must teach **delegation**: vague subtasks ("research chips") caused duplicated
  work and gaps. Scale effort to complexity (1 agent for a simple fact, 10+ for complex
  research).
- Production issues: stateful long runs need **resumable execution** (errors compound),
  deployments must not break in-flight agents, and full tracing is essential for debugging.

## Coding agents and computer-use agents

### Coding agents

A coding agent is the most mature agent category because the environment provides **cheap,
objective feedback**: compilers, linters, and tests.

Typical tool set:

| Capability | Tool examples | Notes |
| --- | --- | --- |
| Navigate | `list_dir`, `glob`, `grep` / ripgrep | Just-in-time exploration instead of loading the repo |
| Read | `read_file(path, offset, limit)` | Paginated reads of large files |
| Edit | `str_replace(path, old, new)`, `apply_patch(diff)`, `write_file` | Exact-match replace is robust; whole-file rewrites waste tokens and risk clobbering |
| Execute | `bash(cmd)` / `run_tests` | The feedback engine; must be sandboxed with timeouts |
| VCS | `git diff`, `git status`, commit / PR | Diffs make changes reviewable and reversible |
| Knowledge | web search, docs fetch, project instruction files | For APIs the model doesn't know |

The loop: **explore** (find relevant files), **plan** (often a todo list), **edit**, **verify**
(build, run tests, lint), **iterate on failures**, then **summarize the diff**. Practices that
matter:

- **Verification by tests is the stopping condition.** "Make this failing test pass without
  modifying the test" is a far better task than "fix the bug". Agents that write a reproduction
  test first are more reliable.
- **Sandbox everything.** A container or microVM per task, a scratch copy of the repo, no
  production credentials, network egress restricted, and resource limits.
- **Permission tiers.** Read and edit freely in the workspace; ask before installing
  dependencies, running migrations, `git push`, or touching paths outside the repo.
- **Review the diff.** Humans review agent PRs like any other PR; CI is the final gate.
- Watch for **reward hacking**: deleting or weakening tests, special-casing test inputs, or
  catching and swallowing exceptions to go green.

### Computer-use agents

These control a GUI like a person would:

```text
┌──────────┐ screenshot (+ accessibility tree) ┌──────────────┐
│  model   │◄──────────────────────────────────│  VM/browser  │
│          │──────────────────────────────────►│   harness    │
└──────────┘ action: click(x,y), type, key,    └──────────────┘
             scroll, wait     (executed by the harness, not the model)
```

- The model sees **pixels** (and sometimes the DOM or accessibility tree) and outputs actions
  with coordinates. The harness executes them and returns a new screenshot.
- They are slow (one screenshot round trip per action) and brittle (layout changes, pop-ups,
  timing), and the screenshots are token-heavy.
- **Prefer APIs when they exist.** Use GUI control for legacy systems without APIs.
- Security: every web page is untrusted input, so prompt injection via on-screen text is a real
  risk. Run in a disposable VM, with no logged-in sessions to sensitive accounts, a domain
  allow-list, and confirmation for purchases, sends, and logins.
- Benchmarks: OSWorld (desktop tasks in real OS VMs), WebArena and its variants (web).

## A complete minimal agent (Python)

About 150 lines, with no framework. It has file tools sandboxed to a workspace, long-term memory
in `MEMORY.md`, compaction of old tool results, a step limit, a token budget, repetition
detection, and JSONL tracing.

```python
"""minimal_agent.py - a small, complete tool-using agent (Anthropic Messages API)."""
import json
import os
import sys
import time
import uuid
from pathlib import Path

import anthropic

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
WORKSPACE = Path(os.environ.get("AGENT_WORKSPACE", "./workspace")).resolve()
MEMORY_FILE = WORKSPACE / "MEMORY.md"
client = anthropic.Anthropic(max_retries=3)


class Tracer:
    """Append-only JSONL trace: one line per event, grouped by run_id."""

    def __init__(self, path: str = "trace.jsonl"):
        self.run_id = uuid.uuid4().hex[:12]
        self.f = open(path, "a", encoding="utf-8")

    def event(self, kind: str, **data) -> None:
        rec = {"ts": round(time.time(), 3), "run_id": self.run_id, "kind": kind, **data}
        self.f.write(json.dumps(rec, default=str) + "\n")
        self.f.flush()


class ToolError(Exception):
    pass


def _safe(path: str) -> Path:
    p = (WORKSPACE / path).resolve()
    if not p.is_relative_to(WORKSPACE):
        raise ToolError(f"'{path}' is outside the workspace; use a relative path.")
    return p


def list_files(subdir: str = ".") -> str:
    root = _safe(subdir)
    files = sorted(str(p.relative_to(WORKSPACE)) for p in root.rglob("*") if p.is_file())
    return "\n".join(files[:200]) or "(no files)"


def read_file(path: str, offset: int = 0, limit: int = 200) -> str:
    p = _safe(path)
    if not p.is_file():
        raise ToolError(f"No file '{path}'. Call list_files to see what exists.")
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    chunk = lines[offset:offset + limit]
    more = f"\n[lines {offset}-{offset + len(chunk)} of {len(lines)}]" if len(lines) > limit else ""
    return "\n".join(chunk) + more


def write_file(path: str, content: str) -> str:
    p = _safe(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {path}"


def remember(fact: str) -> str:
    with MEMORY_FILE.open("a", encoding="utf-8") as f:
        f.write(f"- {fact.strip()}\n")
    return "Saved to long-term memory."


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


TOOLS = {
    "list_files": (list_files, "List files under a workspace subdirectory (max 200).",
                   _obj({"subdir": {"type": "string"}}, [])),
    "read_file": (read_file, "Read a text file by line range. Use offset/limit for big files.",
                  _obj({"path": {"type": "string"}, "offset": {"type": "integer"},
                        "limit": {"type": "integer"}}, ["path"])),
    "write_file": (write_file, "Create or overwrite a text file in the workspace.",
                   _obj({"path": {"type": "string"}, "content": {"type": "string"}},
                        ["path", "content"])),
    "remember": (remember, "Save a durable fact for future sessions (user prefs, lessons). "
                           "Never store secrets.", _obj({"fact": {"type": "string"}}, ["fact"])),
}
TOOL_DEFS = [{"name": n, "description": d, "input_schema": s} for n, (_, d, s) in TOOLS.items()]


def system_prompt() -> str:
    memory = MEMORY_FILE.read_text(encoding="utf-8") if MEMORY_FILE.exists() else "(empty)"
    return ("You are a careful assistant working in a file workspace. Plan briefly, use tools "
            "to check facts instead of guessing, and stop when the task is done.\n\n"
            f"<long_term_memory>\n{memory}\n</long_term_memory>")


def execute(call, tracer: Tracer) -> dict:
    fn = TOOLS[call.name][0] if call.name in TOOLS else None
    start = time.time()
    try:
        if fn is None:
            raise ToolError(f"Unknown tool {call.name}; available: {list(TOOLS)}")
        out, is_error = fn(**call.input), False
    except ToolError as e:
        out, is_error = str(e), True
    except TypeError as e:
        out, is_error = f"Bad arguments: {e}", True
    tracer.event("tool", name=call.name, args=call.input, error=is_error,
                 ms=int((time.time() - start) * 1000), result_chars=len(out))
    return {"type": "tool_result", "tool_use_id": call.id, "content": out[:20_000],
            "is_error": is_error}


def compact(messages: list[dict], keep_last: int = 3) -> None:
    """Elide bulky results from older tool-result turns; keep the calls themselves."""
    turns = [m for m in messages if m["role"] == "user" and isinstance(m["content"], list)]
    for m in turns[:-keep_last]:
        for block in m["content"]:
            text = block.get("content", "")
            if isinstance(text, str) and len(text) > 500 and not text.startswith("[elided"):
                block["content"] = f"[elided {len(text)} chars; re-run the tool if needed]"


def run(task: str, max_steps: int = 25, token_budget: int = 400_000) -> str:
    tracer, messages, used, seen = Tracer(), [{"role": "user", "content": task}], 0, {}
    tracer.event("start", task=task, model=MODEL)
    for step in range(max_steps):
        resp = client.messages.create(model=MODEL, max_tokens=4096, system=system_prompt(),
                                      tools=TOOL_DEFS, messages=messages)
        used += resp.usage.input_tokens + resp.usage.output_tokens
        tracer.event("llm", step=step, stop=resp.stop_reason, in_tok=resp.usage.input_tokens,
                     out_tok=resp.usage.output_tokens)
        messages.append({"role": "assistant", "content": resp.content})

        calls = [b for b in resp.content if b.type == "tool_use"]
        if resp.stop_reason != "tool_use" or not calls:
            answer = "".join(b.text for b in resp.content if b.type == "text")
            tracer.event("finish", step=step, tokens=used)
            return answer
        for c in calls:                                   # repetition guard
            key = c.name + json.dumps(c.input, sort_keys=True)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 3:
                tracer.event("abort", reason="repeated_call", call=key)
                return f"Stopped: repeated the same call {c.name} 4 times."
        messages.append({"role": "user", "content": [execute(c, tracer) for c in calls]})

        if used > token_budget:
            tracer.event("abort", reason="budget", tokens=used)
            return f"Stopped: token budget exhausted after {step + 1} steps."
        if resp.usage.input_tokens > 100_000:
            compact(messages)
    tracer.event("abort", reason="max_steps")
    return f"Stopped: reached {max_steps} steps without finishing."


if __name__ == "__main__":
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    print(run(" ".join(sys.argv[1:]) or "Summarize the files in the workspace into SUMMARY.md"))
```

What it deliberately leaves out, and what you would add next:

- **Parallel tool execution.** It runs calls sequentially for simplicity; see the async loop on
  the Tool calling page.
- **Durable state.** Persist `messages` after each step (to a DB or file) so a crash can resume
  without redoing side effects.
- **Approvals.** Gate `write_file` outside a `drafts/` folder behind a confirmation callback.
- **Smarter compaction.** Summarize with the model when elision isn't enough.
- **OpenTelemetry instead of JSONL** (see Observability below).

### TypeScript equivalent: notes

The structure maps one-to-one:

- Use `@anthropic-ai/sdk` (`client.messages.create`) or the OpenAI Responses API. With
  OpenAI, append `response.output` items and `function_call_output` items instead of
  `tool_result` blocks.
- Use **Zod** schemas per tool for validation (`schema.safeParse(input)`), and keep the
  hand-written or generated JSON Schema in the tool definition.
- Use `path.resolve` plus a `startsWith(WORKSPACE + path.sep)` check for the sandbox guard, and
  `fs/promises` for file I/O.
- Use `Promise.all` for parallel calls with a per-call timeout wrapper, and an `AbortController`
  to cancel the run on budget or timeout.
- For tracing, use `@opentelemetry/api` spans named per the GenAI conventions.

## Frameworks landscape (October 2026)

You do not need a framework to build an agent: the loop above is about 150 lines. Frameworks
earn their place through **durable execution, human-in-the-loop, multi-agent orchestration,
tracing, and integrations**. They cost you abstraction: hidden prompts, framework-specific
state, and upgrade churn.

| Framework | Status (Oct 2026) | Shape | Pros | Cons |
| --- | --- | --- | --- | --- |
| **LangGraph** (LangChain) | 1.x (1.2.x) | Explicit state graph; nodes and edges; checkpointers | Fine-grained control; durable execution with durability modes; `interrupt()` for HITL; time travel; mature ecosystem and tracing (LangSmith) | Graph boilerplate for simple agents; LangChain-ecosystem churn; resumed nodes re-run from the start, so side effects must be idempotent |
| **OpenAI Agents SDK** | Python 0.24, TS 0.21 (pre-1.0) | `Agent` + `Runner` loop; handoffs; agents-as-tools; guardrails; sessions | Small, readable; good defaults; built-in tracing; strong on the Responses API; sandboxed workspaces | Pre-1.0 (breaking changes in minors); best on OpenAI models; guardrail scoping subtleties (input guardrails only on the first agent) |
| **Claude Agent SDK** (formerly Claude Code SDK) | Python 0.2.x, TS 0.3.x (pre-1.0) | The Claude Code harness as a library: file, bash, and web tools; sub-agents; hooks; permissions; MCP | Production-grade coding/computer harness with compaction, permissions, and sub-agents out of the box | Claude-centric; opinionated around a CLI runtime; pre-1.0 |
| **Google ADK** | 2.x (2.13) | Agents + graph workflows; sessions and memory services; eval tooling | Multi-agent primitives; built-in evaluation; deploys to Vertex AI Agent Engine; A2A-native | Best experience on Google Cloud and Gemini; fast-moving API |
| **CrewAI** | 1.15.x | Role-based "crews" plus event-driven Flows | Quick to prototype multi-agent role-play; Flows add deterministic control; large community | Role abstractions can hide prompts and inflate tokens; less control than graph frameworks |
| **LlamaIndex Workflows** | `llama-index-workflows` 2.x | Event-driven, async steps; typed state | Excellent when the agent is RAG-heavy; durable journaling and checkpoints in recent releases | Best value inside the LlamaIndex ecosystem |
| **Microsoft Agent Framework** | **1.0 GA (April 2026)**, .NET and Python | Successor to Semantic Kernel and AutoGen: agents, graph workflows, orchestrations (sequential, concurrent, handoff, group chat, Magentic-One) | Enterprise features (middleware, checkpointing, declarative YAML), multi-provider, MCP and A2A, LTS commitment | Heavier; Azure/Foundry gravity; AutoGen and SK users must migrate |
| **Pydantic AI** | 2.x (v2 major shipped mid-2026; v1 still receives security patches) | Type-safe agents with Pydantic models for tools and outputs; dependency injection | Strong typing and validation; model-agnostic; good testing story; OTel via Logfire | Python-only; smaller multi-agent surface than the graph frameworks |

How to choose:

- **Prototype or simple single agent**: provider SDK or no framework.
- **Long-running, must survive crashes, needs approvals**: LangGraph, Microsoft Agent Framework,
  or a general durable-execution engine (Temporal, DBOS, Restate) wrapping your own loop.
- **Coding or computer-use agent**: Claude Agent SDK or the OpenAI Agents SDK sandbox features.
- **Typed Python service**: Pydantic AI.
- **.NET enterprise**: Microsoft Agent Framework.
- Whatever you pick, make sure you can **see the exact messages sent to the model**.

## Protocols: MCP and A2A

```text
          ┌───────────────── A2A (agent ↔ agent, opaque peers) ─────────────────┐
          ▼                                                                      ▼
┌───────────────────┐                                                 ┌───────────────────┐
│   Agent A (org 1) │                                                 │  Agent B (org 2)  │
│   any framework   │                                                 │  any framework    │
└─────────┬─────────┘                                                 └─────────┬─────────┘
          │ MCP (agent ↔ tools/data)                                            │ MCP
   ┌──────┴───────┐                                                      ┌──────┴───────┐
   ▼              ▼                                                      ▼              ▼
 GitHub MCP   Postgres MCP                                            CRM MCP      Files MCP
```

| | MCP (Model Context Protocol) | A2A (Agent2Agent) |
| --- | --- | --- |
| Connects | An AI application to **tools, resources, prompts** | An agent to **another agent** |
| Peer visibility | Server exposes concrete tools with schemas | Remote agent is **opaque**: its internals, memory, and tools are not shared |
| Unit of work | Tool call (request/response) | **Task** with lifecycle states, messages, artifacts; streaming and push notifications |
| Discovery | `tools/list`, `server/discover` | **Agent Card** (capabilities, skills, interfaces, auth) |
| Current version | Spec revision **2026-07-28** (stateless core: no `initialize` handshake or sessions; per-request version and capabilities in `_meta`; Multi Round-Trip Requests) | **v1.0** (March 2026; 1.0.1 patch in May 2026), Linux Foundation project; bindings JSON-RPC, gRPC, HTTP+JSON |
| Use when | Giving your agent capabilities | Delegating to an agent you don't control, across teams, vendors, or frameworks |

They are complementary. **MCP is how an agent gets hands; A2A is how agents talk to each
other.** Inside one codebase you usually need neither for agent-to-agent communication:
sub-agents are function calls. A2A earns its keep across organizational or framework
boundaries.

## Reliability engineering

Agents are distributed systems with a non-deterministic component. Apply distributed-systems
discipline.

### Failure modes

| Failure | Symptom | Mitigation |
| --- | --- | --- |
| Transient API errors (429, 5xx, timeouts) | Run crashes mid-way | SDK retries with exponential backoff and jitter; fallback model; per-call timeouts |
| Tool errors | Model loops on the same failing call | Actionable error messages; repetition detector; circuit breaker per tool |
| Lost progress on crash or deploy | Long runs restart from zero | **Checkpointing / durable execution** |
| Duplicate side effects on retry or resume | Two emails sent, double refund | **Idempotency keys**; record executed call ids; prepare/commit split |
| Infinite or very long loops | Cost blow-up | Step, token, cost, and time budgets; progress checks |
| Goal drift / premature completion | Says "done" when it isn't | Verification step (tests, checks); explicit success criteria; evaluator pass |
| Context overflow | Errors or degraded quality late in runs | Compaction, note files, sub-agents |
| Model or version change | Silent behavior regressions | Pin model versions; eval suite gating upgrades |

### Durable execution and checkpointing

Persist the agent state (messages, plan, executed-call ledger) **after every step** so a run can
resume on another worker after a crash or deploy.

- **LangGraph checkpointers** save state at every super-step to a thread. Durability modes are
  `"exit"` (persist only at the end: fastest, no crash recovery), `"async"` (persist while the
  next step runs: small risk of loss), and `"sync"` (persist before the next step: most durable).
- **Durable workflow engines** (Temporal, DBOS, Restate, Inngest) record each activity's result
  in an event history. On replay, completed LLM and tool calls are *not* re-executed; their
  recorded results are returned. This is a natural fit: put each LLM call and each tool call in
  its own activity.
- **The idempotency rule:** anything that can be re-executed after a crash must be safe to
  re-execute. LangGraph's `interrupt()` re-runs the interrupted node *from its start* on resume,
  so code before the interrupt must be idempotent.

### Human-in-the-loop interrupts

```text
agent proposes send_email(to=..., body=...)
      │
      ▼
policy: requires approval ──► persist state, emit "approval needed" event ──► (minutes/days)
                                                                       │
     ┌───────────────── user approves / edits / rejects ◄──────────────┘
     ▼
resume from checkpoint: execute (approve), execute edited args (edit),
or return "rejected: <reason>" as the tool result (reject) so the agent adapts
```

Design points: make approvals **asynchronous** (don't hold a process open for hours), show the
**exact arguments** and a preview, put **expiry** on pending approvals, and record who approved
what.

### Budgets

Enforce limits at several levels: per step (`max_tokens`), per run (steps, tokens, dollars,
wall-clock), per user or tenant per day, and globally (circuit breaker on spend rate). Surface
budget exhaustion as a first-class outcome with partial results.

## Evaluating agents

Agents are harder to evaluate than single prompts. Runs are long and non-deterministic, and
many different paths can be correct.

### What to measure

- **Task success (outcome).** Did the final state satisfy the goal? Prefer **programmatic
  checks**: tests pass, DB state matches, the answer equals the reference. Use LLM-as-judge with a
  rubric only where there is no ground truth.
- **Trajectory quality (process).** Right tools, sensible order, no policy violations, no
  unnecessary calls. Compare against reference trajectories loosely (order-insensitive where
  appropriate), or judge with a rubric.
- **Efficiency.** Steps, tokens, cost, and latency per successful task.
- **Reliability across repeats.** Run each task *k* times. tau-bench's **pass^k** (the
  probability that *all k* trials succeed) exposes inconsistency that pass@1 hides. An agent
  with 60% pass@1 can have a far lower pass^8.
- **Safety.** Rate of policy violations, unsafe tool calls attempted, injection susceptibility
  (red-team suites).

### Benchmarks (described accurately)

| Benchmark | What it tests | Setup and metric |
| --- | --- | --- |
| **SWE-bench** (Jimenez et al., 2023) | Resolving real GitHub issues in Python repos | 2,294 issue/PR pairs from 12 popular repos. The agent produces a patch; success means the repo's tests that the fix should make pass (FAIL_TO_PASS) now pass, without breaking others (PASS_TO_PASS). **SWE-bench Verified** (OpenAI, 2024) is a 500-task human-validated subset; *Lite* has 300. Harder successors exist because top scores saturated and contamination is a concern |
| **τ-bench / tau-bench** (Sierra, 2024) | Tool-agent-*user* interaction under domain policies | Retail and airline domains with API tools and a policy document; an LLM simulates the user. Success means the final database state matches the goal state. Introduced pass^k. τ²-bench added a dual-control telecom domain where the user also acts |
| **GAIA** (Mialon et al., 2023) | General assistant ability: browsing, multimodality, tools, reasoning | 466 real-world questions at three difficulty levels with short, unambiguous answers (exact match). At release, humans scored about 92% vs about 15% for GPT-4 with plugins |
| **WebArena** (Zhou et al., 2023) | Autonomous web tasks on realistic sites | Self-hosted sites (e-commerce, forum, GitLab, CMS admin, maps) with 812 tasks, scored by functional correctness of the end state. At release: about 14% for the best GPT-4 agent vs about 78% for humans |
| **OSWorld** (Xie et al., 2024) | Computer use in real OS VMs | Desktop tasks across apps, scored by execution-based checkers |
| **BFCL** (Berkeley) | Function-calling accuracy | Single, parallel, and multi-turn calls; AST and executable checks; irrelevance detection |

Caveats: benchmark scores depend heavily on the **harness** (tools, prompts, retries), so they
are not pure model scores. Public test sets leak into training data. And your domain is not
SWE-bench. **Build an internal eval set** from real tasks, run it in CI on every prompt, tool,
model, or framework change, and read the failing transcripts.

## Safety and security

### Threats

- **Excessive agency** (OWASP LLM06): too much *functionality* (tools the task doesn't need),
  too many *permissions* (write access where read would do), or too much *autonomy* (no
  approval for high-impact actions). Most real incidents trace back to one of the three.
- **Prompt injection** (OWASP LLM01), especially *indirect*: instructions hidden in web pages,
  emails, issues, documents, tool results, or other agents' outputs.
- **The lethal trifecta** (Simon Willison): **private data + untrusted content + external
  communication**. An agent with all three can be tricked into exfiltrating data. Break the
  triangle per task or per sub-agent.
- **Memory poisoning**: injected content written into long-term memory fires later.
- **Tool and supply-chain risk**: malicious or compromised MCP servers, tool descriptions that
  carry injections ("tool poisoning"), and dependency confusion in coding agents.
- **Destructive mistakes without an attacker**: `rm -rf`, `DROP TABLE`, force-push, mass email.

### Controls

```text
 ┌──────────────────────────────────────────────────────────────────────┐
 │ identity: agent acts AS the user, scoped short-lived credentials     │
 ├──────────────────────────────────────────────────────────────────────┤
 │ permissions: per-tool allow/deny, read vs write scopes, path & domain│
 │ allow-lists, risk tiers (auto / confirm / forbidden)                 │
 ├──────────────────────────────────────────────────────────────────────┤
 │ sandbox: container/microVM, no secrets inside, egress allow-list,    │
 │ CPU/mem/time limits, ephemeral filesystem                            │
 ├──────────────────────────────────────────────────────────────────────┤
 │ guardrails: input/output classifiers, tool-arg validation, PII scrub │
 ├──────────────────────────────────────────────────────────────────────┤
 │ human-in-the-loop for irreversible / external actions                │
 ├──────────────────────────────────────────────────────────────────────┤
 │ audit: full traces, immutable logs, anomaly alerts, kill switch      │
 └──────────────────────────────────────────────────────────────────────┘
```

Design patterns:

- **Least privilege per task.** Grant only the tools and scopes a task needs, and drop
  capabilities after reading untrusted content.
- **Split privileged and quarantined agents.** The agent that reads the web cannot send email;
  the agent that sends email never sees raw web content, only structured fields.
- **Treat every agent output as untrusted input** to the next system (SQL, shell, HTML
  rendering, other agents).
- **Prefer reversible actions** (drafts, branches, soft deletes) and keep an undo path.
- **Rate-limit side effects** (at most N emails per run) as a backstop against loops and abuse.

## Cost and latency

Rough anatomy of an agent run's cost:

```text
cost ≈ Σ_steps [ (system + tools + history_i) × price_in  +  output_i × price_out ]
       + tool/infra costs
history_i grows each step → total input ≈ O(steps²) without caching or compaction
```

Levers:

| Lever | Effect |
| --- | --- |
| Prompt caching (stable prefix) | Large reduction in input cost and time to first token on every step |
| Compaction / eliding old results | Bends the O(steps²) growth back toward linear |
| Fewer, task-shaped tools | Fewer steps, shorter definitions |
| Model routing (small model for simple steps, sub-agents) | Lower price per token |
| Parallel tool calls and parallel sub-agents | Lower wall-clock time (not tokens) |
| Workflows instead of agents where the path is known | Removes planning calls entirely |
| Budgets and early stopping | Caps the tail |
| Thinking budget tuning | Trades accuracy against output tokens and latency |

Latency budget for a user-facing agent: most of the time goes to sequential model calls (time
to first token plus generation), not tools. Reduce the **number of sequential steps**, stream
progress ("Searching orders…"), and move long tasks to **async jobs** with notifications.

## Production deployment and observability

### Deployment shape

```text
client ─► API (auth, rate limit) ─► job queue ─► agent workers (stateless) ─► LLM APIs
                                         │              │      │
                                         │              │      └─► tool services / MCP
                                         ▼              ▼
                                   state store     sandboxes (per-run)
                                  (checkpoints,
                                   approvals)
          traces/metrics/logs ◄── all components (OpenTelemetry)
```

- **Stateless workers plus an external state store**, so any worker can resume any run.
- **Queue long runs.** Stream progress over SSE or WebSocket, and notify on completion or when
  approval is needed.
- **Version everything together**: prompt, tool set, model id, and framework version. Make the
  version part of the trace and use it to gate rollouts on eval results.
- **Kill switch** per agent or per tool, and per-tenant spend limits.

### Tracing with OpenTelemetry GenAI semantic conventions

OpenTelemetry defines **GenAI semantic conventions** (still marked *Development* status; they
now live in a dedicated `semantic-conventions-genai` repository). Relevant operations
(`gen_ai.operation.name`) include `invoke_agent`, `invoke_workflow`, `chat`, `execute_tool`,
`plan`, `retrieval`, `embeddings`, and memory operations. A typical trace:

```text
invoke_agent support_agent                       (INTERNAL; gen_ai.agent.name)
├── chat claude-opus-5-5                         (CLIENT; gen_ai.request.model,
│                                                 gen_ai.usage.input_tokens/output_tokens)
├── execute_tool orders_search                   (INTERNAL; gen_ai.tool.name,
│                                                 gen_ai.tool.call.id)
├── chat claude-opus-5-5
├── execute_tool send_email   [status=ERROR]
└── chat claude-opus-5-5      finish_reasons=["end_turn"]
```

```python
from opentelemetry import trace

tracer = trace.get_tracer("support-agent")

def traced_llm_call(messages: list[dict]):
    with tracer.start_as_current_span(f"chat {MODEL}", kind=trace.SpanKind.CLIENT) as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.provider.name", "anthropic")
        span.set_attribute("gen_ai.request.model", MODEL)
        resp = client.messages.create(model=MODEL, max_tokens=2048,
                                      tools=TOOL_DEFS, messages=messages)
        span.set_attribute("gen_ai.response.finish_reasons", [resp.stop_reason])
        span.set_attribute("gen_ai.usage.input_tokens", resp.usage.input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", resp.usage.output_tokens)
        return resp

def traced_tool(call):
    with tracer.start_as_current_span(f"execute_tool {call.name}") as span:
        span.set_attribute("gen_ai.operation.name", "execute_tool")
        span.set_attribute("gen_ai.tool.name", call.name)
        span.set_attribute("gen_ai.tool.call.id", call.id)
        # arguments/results are opt-in attributes: record only after redaction
        return execute(call)

def run_traced(task: str):
    with tracer.start_as_current_span("invoke_agent support_agent") as span:
        span.set_attribute("gen_ai.operation.name", "invoke_agent")
        span.set_attribute("gen_ai.agent.name", "support_agent")
        ...  # the agent loop, calling traced_llm_call / traced_tool
```

Many frameworks (OpenAI Agents SDK, Google ADK, Microsoft Agent Framework, Pydantic AI, CrewAI,
LangChain/LangGraph via instrumentation) emit these spans already. Message content is
**opt-in** because of privacy; redact before enabling it.

What to monitor:

- Success, abort, and escalation rates; steps, tokens, and cost per run (p50/p95/p99); tool
  error rates by tool; approval rates and latency; guardrail trips; cache hit rate.
- **Online evals**: sample production traces and score them with your eval rubric to catch
  drift.
- **Dashboards by version** (prompt, model, tools) so regressions are attributable.

## Common mistakes

- Building an agent where a workflow (or a single call) would do.
- No step, token, or time limits, and no repetition detection.
- Letting the model pass identity (`user_id`, `tenant_id`) as tool arguments.
- Treating multi-agent as the default architecture; ignoring the roughly 15x token cost.
- Sub-agents with vague task descriptions, which duplicate or miss work.
- Unbounded context growth: no compaction, no eliding of tool results.
- Non-idempotent side effects combined with retries or resume, giving duplicate actions.
- Using memory as a dumping ground: storing raw transcripts and untrusted content, never
  forgetting.
- Evaluating with a handful of manual chats; no repeated runs; no trajectory inspection.
- Frameworks that hide the actual prompts, so you can't debug.
- Giving one agent private data, untrusted content, and outbound communication (the lethal
  trifecta).
- Not pinning model versions; silent behavior changes on upgrade.

## Interview questions

**1. What distinguishes an agent from a workflow, and how do you decide which to build?**
In a workflow, code defines the control flow and LLMs fill steps. In an agent, the model
chooses the next step at runtime in a loop. Choose a workflow when the steps are known and
predictability, cost, and latency matter. Choose an agent when the path depends on what is
discovered along the way and the value justifies the variable cost and the extra risk. Start
simple and escalate only when evals show the need.

**2. Explain ReAct and why interleaving reasoning with actions helps.**
The model alternates thoughts, actions (tool calls), and observations. Reasoning lets it plan,
track progress, and handle exceptions; actions ground it in real information and so reduce
hallucination. Reasoning-only hallucinates facts; acting-only lacks planning and error
analysis. Modern tool-calling models implement ReAct natively through thinking plus tool calls.

**3. How would you stop an agent from running forever or burning money?**
Layered stopping conditions: natural completion or a `finish` tool, verified success checks,
max steps, token and cost budgets, wall-clock timeouts, and repetition detection. Add per-tenant
spend limits and a global circuit breaker. Return structured partial results when stopping
early, and alert on budget exhaustion.

**4. Describe the types of agent memory and how you'd implement each.**
Working memory is the context window, managed by compaction. Episodic memory is past runs and
experiences, as retrievable logs or summaries. Semantic memory is facts, in vector or
structured stores or a knowledge graph. Procedural memory is how-to knowledge, in system
prompts, skill files, and tool code. Implement retrieval-into-context. Use explicit write
policies (what, when, metadata, deduplication, TTL), scope memory per user, and treat memory
written from untrusted content as untrusted.

**5. Your agent degrades badly after ~40 steps. Why, and what do you do?**
Context growth: old tool results crowd the window (context rot), the goal drifts out of
attention, and costs grow quadratically. Fixes: elide or summarize old tool results, rolling
summaries at a threshold, a plan or notes file that is re-read regularly, just-in-time retrieval
instead of preloading, and delegating bulky sub-tasks to sub-agents that return condensed
results.

**6. Compare plan-and-execute, ReWOO, and Reflexion.**
Plan-and-execute writes a global plan, executes steps (often with cheaper models), and re-plans
on failure. ReWOO plans every tool call with variable placeholders in one pass and executes them
without further LLM calls. It is cheap, but it cannot adapt mid-run. Reflexion runs trials,
writes verbal reflections from a feedback signal into memory, and retries; it works best with
reliable external feedback such as tests.

**7. When does a multi-agent architecture help, and when does it hurt?**
It helps for breadth-first, parallelizable tasks such as research, for context isolation, for
permission separation, and for using different models per role. It hurts for tightly coupled
tasks like most coding, where sub-agents make conflicting implicit decisions. It also multiplies
tokens (around 15x chat in Anthropic's research system), and it complicates debugging and
evaluation. Default to a single agent.

**8. How would you design a deep-research agent?**
A lead agent analyzes the query, writes a plan to memory, and spawns N sub-agents with precise
objectives, output formats, and tool guidance, scaling N to complexity. Sub-agents search in
parallel with separate contexts and return condensed findings. The lead synthesizes the results,
decides whether more research is needed, and a citation pass attaches sources. Add durable
state, budgets, tracing, and an eval set with LLM-judge rubrics for factual accuracy, citation
accuracy, and completeness.

**9. How do coding agents verify their work, and what can go wrong?**
They run builds, linters, and tests in a sandbox, ideally writing a reproduction test first,
and they iterate until checks pass. Then humans review the diff and CI gates the merge. Failure
modes: reward hacking (weakening tests, special-casing inputs), destructive commands, leaking
secrets, and getting lost in large repos without just-in-time navigation.

**10. How do you make an agent survive crashes and wait days for a human approval?**
Durable execution: checkpoint state after every step to an external store (LangGraph
checkpointers in `sync` mode, or Temporal-style event histories where completed calls aren't
re-executed). Approvals become asynchronous interrupts: persist, notify, and resume on the same
thread id with the decision. All side-effecting steps must be idempotent, because resumption
can re-run code.

**11. What is the "lethal trifecta" and how do you design around it?**
Access to private data, exposure to untrusted content, and the ability to communicate
externally. With all three, a prompt injection can exfiltrate data. Remove a leg per task: no
outbound channels after reading untrusted content, quarantined readers that return only
structured fields, egress allow-lists, and confirmation for sends.

**12. How do you evaluate an agent?**
Measure outcome success with programmatic checks where possible, trajectory quality (tool
choice, order, policy compliance), efficiency (steps, tokens, latency), consistency over k
repeats (pass^k), and safety (injection red-teaming). Use public benchmarks (SWE-bench,
tau-bench, GAIA, WebArena) for model selection, and an internal task suite in CI for your
system. Read transcripts.

**13. MCP vs A2A: what is each for?**
MCP connects an agent to tools, resources, and prompts exposed by servers (current spec
revision 2026-07-28, now stateless). A2A (v1.0, Linux Foundation) lets opaque agents discover
each other via Agent Cards and collaborate on tasks with lifecycle, streaming, and artifacts.
MCP gives an agent hands; A2A lets agents talk across frameworks and organizations.

**14. What would you put in an agent's traces, and why OpenTelemetry?**
One trace per run: an `invoke_agent` root span with child `chat` spans (model, tokens, finish
reason) and `execute_tool` spans (tool name, call id, status, latency), with redacted
arguments and results opt-in, and version attributes. OpenTelemetry's GenAI conventions give
vendor-neutral names, so traces flow into any backend and correlate with the rest of your
system's telemetry.

## Summary

- **Agent = LLM + tools + loop + limits.** Workflows fix the path in code; agents let the
  model choose it. Use the simplest option that meets the bar.
- **The loop**: perceive, plan, act, observe. ReAct is the canonical form. Combine natural,
  verified, and safety stopping conditions.
- **Components**: model, instructions, tools, memory, planner, environment, guardrails, and an
  orchestrator that owns limits, state, and tracing.
- **Memory**: working (context), episodic, semantic, procedural. Retrieval brings memory into
  context; compaction and note files keep long runs alive; write policies prevent pollution
  and poisoning.
- **Context engineering** is the main quality lever: minimal high-signal context, just-in-time
  retrieval, stable cached prefixes, sub-agents for isolation.
- **Planning**: implicit ReAct by default; plan-and-execute for structure; ReWOO for cheap
  predictable chains; Reflexion and evaluator loops when a reliable feedback signal exists;
  tree search for hard, offline problems.
- **Workflow patterns**: chaining, routing, parallelization, orchestrator–workers,
  evaluator–optimizer.
- **Multi-agent** helps breadth-first parallel work and isolation but costs about 15x the
  tokens of chat and hurts tightly coupled tasks.
- **Reliability**: durable checkpoints, idempotent side effects, async human approvals,
  budgets, pinned versions.
- **Evaluation**: outcome plus trajectory plus efficiency plus pass^k plus safety. Public
  benchmarks for models, internal suites for your system.
- **Security**: least privilege, sandboxing, avoid the lethal trifecta, confirm irreversible
  actions, treat all tool results and memory as untrusted.
- **Operations**: stateless workers, an external state store, queues for long runs, and
  OpenTelemetry GenAI spans (`invoke_agent`, `chat`, `execute_tool`).

## Further reading

- Yao et al., *ReAct: Synergizing Reasoning and Acting in Language Models* (2022),
  arXiv:2210.03629.
- Shinn et al., *Reflexion: Language Agents with Verbal Reinforcement Learning* (2023),
  arXiv:2303.11366.
- Anthropic, *Building effective agents* (Schluntz and Zhang, December 2024):
  https://www.anthropic.com/engineering/building-effective-agents
- Lilian Weng, *LLM Powered Autonomous Agents* (June 2023):
  https://lilianweng.github.io/posts/2023-06-23-agent/
- Anthropic Engineering, *How we built our multi-agent research system* (2025).
- Anthropic Engineering, *Effective context engineering for AI agents* (2025).
- Cognition (Walden Yan), *Don't Build Multi-Agents* (2025).
- Sumers et al., *Cognitive Architectures for Language Agents* (CoALA, 2023), arXiv:2309.02427.
- Xu et al., *ReWOO: Decoupling Reasoning from Observations for Efficient Augmented Language
  Models* (2023), arXiv:2305.18323.
- Zhou et al., *Language Agent Tree Search Unifies Reasoning, Acting, and Planning in Language
  Models* (LATS, 2023), arXiv:2310.04406.
- Yao et al., *Tree of Thoughts* (2023), arXiv:2305.10601.
- Park et al., *Generative Agents: Interactive Simulacra of Human Behavior* (2023),
  arXiv:2304.03442.
- Packer et al., *MemGPT: Towards LLMs as Operating Systems* (2023), arXiv:2310.08560.
- Jimenez et al., *SWE-bench: Can Language Models Resolve Real-World GitHub Issues?* (2023),
  arXiv:2310.06770.
- Yao et al., *τ-bench* (2024), arXiv:2406.12045.
- Mialon et al., *GAIA: a benchmark for General AI Assistants* (2023), arXiv:2311.12983.
- Zhou et al., *WebArena: A Realistic Web Environment for Building Autonomous Agents* (2023),
  arXiv:2307.13854.
- Simon Willison, *The lethal trifecta for AI agents* (2025), simonwillison.net.
- OWASP, *Top 10 for LLM Applications 2025*.
- Model Context Protocol specification (2026-07-28): https://modelcontextprotocol.io/specification
- A2A Protocol specification (v1.0): https://a2a-protocol.org/latest/specification/
- OpenTelemetry GenAI semantic conventions: https://github.com/open-telemetry/semantic-conventions-genai
