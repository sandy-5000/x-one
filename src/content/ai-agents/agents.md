---
title: "AI agents"
order: 5
---
# AI agents

An **agent** is an LLM in a **loop**: each step the model chooses the next action (usually tool calls),
your runtime executes and returns observations, and the cycle continues until the goal is met or a
limit stops the run. Workflows fix the path in code; agents let the model **discover** the path at
runtime. That flexibility costs predictability, tokens, and latency — use it only when the task
genuinely requires it.

These notes assume tool calling (see the Tool calling page) and focus on loops, planning, memory,
multi-agent design, orchestration vs workflow, and production economics.

```text
        ┌──────────────────────────────────────────────────────────┐
        │                                                          │
        ▼                                                          │
 ┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌───────────┴─┐
 │  PERCEIVE   │──►│    PLAN     │──►│     ACT     │──►│   OBSERVE   │
 │ goal, ctx,  │   │ next step   │   │ tools / ask │   │ results in  │
 │ memory      │   │ (implicit)  │   │ human       │   │ context     │
 └─────────────┘   └──────┬──────┘   └─────────────┘   └─────────────┘
                          │ done or limit
                          ▼
                     answer / handoff / abort
```

| | Chatbot | Workflow | Agent |
| --- | --- | --- | --- |
| Who picks steps | One shot | **Your code** (DAG) | **Model** at runtime |
| Tools | None or one | At fixed nodes | Dynamic, in a loop |
| LLM calls | 1 | Bounded | Unbounded (capped) |
| Predictability | High | High | Lower |
| Typical cost vs chat | 1× | 1–3× | **4–15×** (multi-agent higher) |
| Good for | Q&A, draft | Repeatable pipelines | Open-ended discovery |

---

## Key points

- **Simplest thing that works:** one LLM call + RAG → workflow when steps are known → agent only when
  the path cannot be fixed upfront.
- **ReAct** interleaves reasoning traces with tool calls and observations; modern tool models do this
  natively — ground facts in tool results, not memory.
- **Reactive (step-by-step) planning** is the default; explicit plan-and-execute helps long structured
  tasks but plans go stale — re-plan on failure.
- **Memory** splits into working (context), episodic (past runs), semantic (facts/RAG), procedural
  (prompts, skills, tool code); everything except the window must be **retrieved in**.
- **Multi-agent** helps parallel breadth-first work and permission isolation; it hurts tightly coupled
  tasks and multiplies tokens and debug surface.
- **Orchestration** (your runtime: limits, checkpoints, guardrails, tracing) wraps the loop;
  **workflow** means predefined control flow — many “agents” in production are workflows with LLM
  nodes.
- **Stopping conditions** stack: natural finish, verified success (tests), max steps, token/cost
  budget, timeout, repetition detection, policy tripwires.
- **Cost grows with steps** — history is resent every step; without caching/compaction, input tokens
  trend toward **O(steps²)**.

---

## Orchestration vs workflow

**Workflow:** code owns the graph — classify → retrieve → generate → validate. LLM calls sit at named
nodes; branches are `if` statements or a state machine. You know the upper bound on calls and cost.

**Agent / orchestrated loop:** code owns **tools, limits, and guardrails**; the model owns **which**
tool runs next. The **orchestrator** is your runtime: assemble context, call the model, validate tool
calls, execute, append results, checkpoint, trace, enforce budgets.

```text
WORKFLOW (control in code)              AGENT (control shared with model)
─────────────────────────              ───────────────────────────────────
input ─► [LLM: route] ─► switch        input ─► ┌ orchestrator loop ┐
              ├─ path A ─► tools                  │  model picks tools │
              └─ path B ─► tools                  └─────────┬──────────┘
                                                            ▼
                                                      environment
```

Anthropic’s *Building effective agents* line: **workflows orchestrate** predefined paths; **agents**
dynamically direct their process. In practice, hybrid systems are common — a workflow wraps an agent
for one messy sub-step, or an agent calls workflow-shaped tools (“run_refund_pipeline”).

| Choose workflow | Choose agent |
| --- | --- |
| Steps known and stable | Path depends on what is discovered |
| Hard latency/cost SLO | Value justifies variable spend |
| Audit needs fixed procedure | Flexibility beats reproducibility of path |
| Side effects are sensitive | Strong sandbox + approvals still required |

---

## The ReAct loop

**ReAct** (reason + act) alternates free-text reasoning with actions and environment feedback.
Reasoning-only chain-of-thought can **hallucinate facts**; acting-only skips planning and error
analysis. Interleaving fixes both: each action should follow from an observation.

Compact trace pattern:

```text
[thought]  Need opening years for both structures.
[action]   web_search("Eiffel Tower completed year")
[obs]      1889
[action]   web_search("Golden Gate Bridge opened")
[obs]      1937
[action]   calculator("1937 - 1889")
[obs]      48
[answer]   48 years (with sources from obs)
```

Design rules:

- **Observations are truth** — tests pass/fail, API status codes, not the model’s belief.
- **Errors are observations** — actionable messages enable retry/rephrase, not silent loops.
- **External stopping** — the model decides “done”; you still enforce limits and verification.

### Stopping conditions

| Condition | Role |
| --- | --- |
| No tool calls / `finish` tool | Normal exit |
| Programmatic success (tests, state check) | Best when available |
| Max steps (typical 10–50 assist, higher coding) | Safety |
| Token / cost / wall-clock budget | Safety + SLO |
| Same tool+args repeated N times | Loop breaker |
| Guardrail / approval rejected | Control |

On abort, return **structured partial progress** (what completed, what blocked), not a silent failure.

---

## Planning vs reactive

| Style | Mechanism | Strength | Weakness |
| --- | --- | --- | --- |
| **Reactive (implicit)** | ReAct: one step at a time in the loop | Adaptive, simple default | Myopic on very long tasks |
| **Plan-and-execute** | Plan all steps, execute each (often sub-loops), re-plan on fail | Global coherence | Stale plans; re-plan logic needed |
| **ReWOO** | One LLM pass writes full tool plan with placeholders; workers execute without LLM | Few calls, cheap | No mid-run adaptation |
| **Reflection / Reflexion** | Trial → external feedback → verbal lesson in memory → retry | Learns from tests | Extra calls; weak if feedback is self-judge |
| **Tree search (ToT, LATS)** | Explore branches, score, backtrack | Hard search problems | Many rollouts — usually offline |

**Default:** reactive ReAct with good tools and context engineering.

**Add explicit planning** when tasks have clear phases (research → draft → review) and failures are
localized — store the plan in a **file or object** re-read each step so compaction does not erase it.

**Prefer workflows over ReWOO** when the tool chain is fixed; ReWOO is for cost-sensitive, predictable
chains with no surprises in the middle.

```text
reactive                         plan-and-execute
────────                         ────────────────
think → act → obs → think …      plan ──► step1 ──► step2 ──► …
                                 fail at step2 ──► replan remaining
```

---

## Memory types

CoALA-style taxonomy (maps cleanly to implementations):

| Type | Holds | Implementation |
| --- | --- | --- |
| **Working** | Current task state | Context window: messages, tool results, scratchpad |
| **Episodic** | Past runs, trajectories | Session logs, summaries, retrievable traces |
| **Semantic** | Facts about world/user | Vector store, KB, RAG, key-value profile |
| **Procedural** | How to do things | System prompt, `AGENTS.md`, skills, tool code |

Only **working memory** is directly visible to the model; episodic/semantic must be **retrieved into**
the window (RAG, `remember` tool, re-read `NOTES.md`).

### Working memory and compaction

- Tool results dominate token growth — **elide** old results (keep call, stub “re-run if needed”).
- **Rolling summary** at ~70–80% window: decisions, open errors, goal verbatim.
- **Files as memory** (`PLAN.md`, `NOTES.md`): survive compaction, diffable, human-inspectable.

### Write policies

Store stable preferences and **verified** facts; not raw transcripts or secrets by default. Tag source,
timestamp, scope (user/team), TTL. Treat memory written from **untrusted** content (web, email) as
untrusted — **memory poisoning** is indirect injection that fires in a later session.

---

## Architecture components

```text
┌──────────────────────────────────────────────────────────────────────────┐
│ ORCHESTRATOR: loop, limits, checkpoints, retries, tracing                 │
│  instructions ──► model ──► tool calls ──► guardrails ──► tools ──► env  │
│       ▲              │                         │                         │
│       └──── memory ◄─┴─────────────────────────┘                         │
└──────────────────────────────────────────────────────────────────────────┘
```

| Component | You control |
| --- | --- |
| Instructions | Role, policies, output shape; static prefix for caching |
| Tools | Schemas, permissions, error messages |
| Memory | What to retrieve, compact, persist |
| Guardrails | Validate args, block policy, approvals |
| Environment | Sandbox, reversibility, observability |

**Context engineering** beats prompt trivia: minimal high-signal tokens, just-in-time retrieval,
non-overlapping tools, stable cached prefix, sub-agents for bulky sub-tasks that return **summaries**.

---

## Multi-agent patterns

Several LLM loops with **separate context windows** cooperating on one task.

```text
 SUPERVISOR                         HANDOFF (swarm)
 ┌────────────┐                    [triage] ──► [billing] ──► [tech]
 │ supervisor │──► workers              control follows transfer_to_*
 └────────────┘                    BLACKBOARD: shared store, agents read/write
```

| Pattern | Works when | Watch out |
| --- | --- | --- |
| **Supervisor** | Delegate parallel subtasks; one integrator | Bottleneck; summary loss |
| **Handoffs** | Phase changes (sales → support) | Ping-pong; history passed wrong |
| **Agents-as-tools** | Lead keeps control; specialists are tools | Lead must ask good sub-questions |
| **Pipeline** | Fixed stages (plan → code → review) | Really a workflow; errors propagate |
| **Debate / judge** | Red-team, reasoning checks | Cost; confident wrong consensus |

Reported production pattern (research-style): strong **lead** writes plan to durable memory, spawns N
**sub-agents** with explicit objectives and output format, parallel search, lead synthesizes. Token use
across separate windows often explains much of quality gains on breadth-first queries — not magic
coordination.

---

## When NOT to use agents

| Situation | Use instead |
| --- | --- |
| Steps are known | Workflow or prompt chain |
| Single retrieval + answer | RAG + one LLM call |
| Tight latency/cost SLO | Fixed pipeline; small model |
| Most coding changes | One agent + repo tools often enough; not N sub-agents without isolation need |
| Task needs full shared context | Multi-agent **summaries** hide details → conflicting decisions |
| No sandbox or approvals for writes | Fix permissions first — agent amplifies risk |
| No evals or stopping limits | Agent will loop and spend |

**Rule of thumb:** one capable agent with **good tools and memory** before splitting. Split for
**parallelizable breadth**, **permission boundaries**, or **context isolation** — not because
“multi-agent” is fashionable.

Autonomy scale (informal): chat (0) → single tool (1) → workflow (2) → supervised agent (3) →
bounded autonomous (4). Most production sits at 2–4 **per action** (auto-edit files, confirm `git push`).

---

## Cost and latency

```text
cost ≈ Σ_steps [ (system + tools + history_i) × price_in + output_i × price_out ] + infra
history_i grows each step → without cache/compaction, total input ~ O(steps²)
latency ≈ Σ sequential (TTFT + generation per step)   // tools often smaller than LLM waits
```

| Lever | Effect |
| --- | --- |
| Prompt caching (stable prefix) | Cuts repeated input cost/time per step |
| Elide / summarize old tool results | Bends quadratic growth toward linear |
| Fewer, task-shaped tools | Fewer steps, shorter definitions |
| Model routing (cheap sub-steps) | Lower $/token on easy steps |
| Workflow where path is known | Removes planning calls entirely |
| Parallel tool calls / parallel sub-agents | Wall-clock ↓, not token ↓ |
| Step/token/cost budgets | Caps tail risk |

User-facing agents: stream progress (“Searching…”), reduce **sequential** model rounds, push long
work to **async jobs** with notification. Anthropic-style rule of thumb: agents ~**4×** chat tokens;
multi-agent research setups can reach ~**15×** — economical only for high-value tasks.

---

## Reliability (brief)

Agents are distributed systems with a non-deterministic planner.

| Failure | Mitigation |
| --- | --- |
| Transient API errors | Retries, backoff, fallback model |
| Tool loops | Actionable errors, repetition guard, circuit breaker |
| Crash mid-run | Checkpoint messages + plan after each step; idempotent side effects |
| Premature “done” | Verified success (tests), explicit criteria |
| Context rot | Compaction, notes files, sub-agents |
| Model upgrade drift | Pin versions; regression eval suite |

**Human-in-the-loop:** persist state, emit approval event, resume from checkpoint — do not block a
worker for hours waiting on a human.

---

## Coding and computer-use agents (sketch)

**Coding agents** win when the environment gives **cheap verification** (build, lint, test). Loop:
explore → plan (todo) → edit → verify → iterate. Stopping on **tests green** beats “model says done.”
Watch **reward hacking** (weakening tests). Sandbox: container, no prod creds, tiered permissions.

**Computer-use** (screenshots → click/type): slow, brittle, token-heavy — prefer APIs; untrusted pixels
are injection surface; disposable VM and confirm on sends/purchases.

---

## Pitfalls

1. **Agent where a workflow suffices** — unpredictable cost and path for no gain.
2. **No step/token/time limits** or repetition detection — runaway loops.
3. **Unbounded context** — no elision of tool results; quadratic spend and context rot.
4. **Multi-agent by default** — duplicated work, conflicting implicit decisions, ~15× tokens.
5. **Vague sub-agent tasks** — gaps and overlap in parallel research.
6. **Trusting model “done”** without programmatic verification when tests exist.
7. **Identity in tool args** — `user_id` from model instead of session context.
8. **Non-idempotent tools + retries/resume** — duplicate tickets, double charges.
9. **Memory dump of untrusted web** — future-session poisoning.
10. **Hidden framework prompts** — cannot debug what the model actually sees.

---

## Quick check

**Agent vs workflow — how do you decide?**  
If your team can draw a stable flowchart and the steps do not depend on unknown discovery, implement
that chart (workflow). If the next action depends on tool results you cannot predict (debug this repo,
research N sources), use an agent with strict limits and verification — and prove with evals that the
agent beats a simpler baseline on **your** tasks.

**What does ReAct buy you over tool-only or chain-of-thought-only?**  
Tool-only skips planning and recovery; CoT-only invents facts. ReAct grounds actions in observations
and lets the model replan after errors — at the cost of extra tokens per step.

**Name four memory types and where they live.**  
Working = context window; episodic = past run logs/summaries; semantic = KB/vector/facts; procedural =
prompts and skills. Only working is native; retrieve the rest into context or files the agent re-reads.

**When does multi-agent help vs hurt?**  
Helps parallel independent subtasks, isolation of permissions, and keeping the lead context clean.
Hurts tightly coupled work (most multi-file coding) where sub-agents lack full state and summaries
conflict; also hurts budgets when 4×–15× token use is not justified.

**Why does agent cost often grow faster than linearly in steps?**  
Each step resends system prompt, tools, and **full history**; history includes prior tool outputs, so
input tokens accumulate across steps — mitigate with caching, compaction, and fewer steps via workflows
where possible.
