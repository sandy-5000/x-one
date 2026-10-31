---
title: "Evals & guardrails"
order: 10
---
# Evals and guardrails

LLMs are **stochastic**: prompt, model, retrieval, and tool changes can silently degrade quality.
**Evaluations** turn “it feels worse” into metrics you can gate releases on. **Guardrails** are the
runtime control plane that blocks, redacts, or escalates when policy or safety boundaries are crossed.

You need both: evals ask “are we good enough to ship?”; guardrails ask “what happens when the model or
an attacker crosses a line we did not train for?”

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                         LLM application lifecycle                           │
├─────────────────────────────────────────────────────────────────────────────┤
│  OFFLINE: dataset ──► harness ──► compare baseline ──► CI gate ──► ship?    │
│  ONLINE:  traffic ──► input guardrails ──► app ──► output/action guardrails │
│           │                                    │                            │
│           └──────── sample traces & grade ◄────┴──── alerts, dashboards     │
└─────────────────────────────────────────────────────────────────────────────┘
```

| | Evals | Guardrails |
| --- | --- | --- |
| When | Before/during development; sampled in prod | Every request at runtime |
| Role | Measure quality, regressions, safety ASR | Enforce policy; limit blast radius |
| Failure mode | Misses novel bugs if not in suite | False positives block users |

---

## Key points

- **Offline evals** run on pinned datasets in CI; **online evals** sample production traces and grade
  on a schedule — offline lags reality; online catches drift but needs redaction.
- **Golden sets** are versioned cases with expected facts, forbidden phrases, tool rules, or
  programmatic success checks — grow the set on every production failure.
- **Regression suites** compare candidate vs baseline on the **same** cases; use paired stats, not
  vibes.
- **LLM-as-judge** scales rubrics; calibrate to humans, watch verbosity/position/self-preference bias.
- **Programmatic checks first** — schema, regex, tests, SQL result — judges only where needed.
- **Guardrails in layers:** input → model/agent → output → **action** (tools); policy in code/config,
  not prompt alone.
- **PII/toxicity filters** on input and output; **output validation** before HTML/SQL/shell sinks
  (OWASP LLM05).
- Evals do **not** prove security — pair with permissions, injection suites, and red teaming.

---

## Offline vs online evals

| Stage | Purpose | Typical signals |
| --- | --- | --- |
| **Dev / PR** | Fast feedback | Smoke set (20–50 cases) |
| **CI regression** | Block bad merges | 100–500 cases, pass thresholds |
| **Pre-launch** | Unbiased estimate | Held-out set never tuned against |
| **Canary / shadow** | Live model, no user impact | Diff scores vs production |
| **Production monitoring** | Drift, incidents, cost | Sampling, dashboards, alerts |

```text
                    narrow ◄──────────────────────────────────────► broad
  unit                 component              integration           end-to-end
  schema parse         retriever recall       full RAG chain        agent + DB + tools
```

**Offline strengths:** reproducible, cheap at scale, gates releases. **Limits:** stale gold answers,
distribution shift, no proof against novel attacks.

**Online strengths:** real queries, real retrieval, real tool latency. **Limits:** PII in logs,
expensive judges, delayed signal. Pattern: **log → sample → re-run offline graders on redacted
payloads → alert on slice regressions**.

### Continuous loop

```text
prod failure or thumbs-down
        │
        ▼
  add/adjust eval case ──► CI harness ──► fix ──► deploy ──► monitor sample
```

---

## Golden sets and dataset design

A golden set is a **versioned** list of cases: `input`, `expected` (or rubric), `metadata` tags.

**Sources (best first):**

1. Production failures and escalations (after PII redaction).
2. Support corrections — where humans fixed the bot.
3. Policy/regulatory scenarios — must-say / must-not-say.
4. Red-team payloads — every successful injection becomes a permanent case.
5. Synthetic paraphrases — fast; mark `metadata.source=synthetic` and spot-check.

**Stratify metadata** so aggregate pass rate does not hide regressions:

| Tag | Why slice |
| --- | --- |
| `category` | billing vs security |
| `risk` | high = PII, money, medical |
| `requires_tools` | none / read / write |
| `locale` | multilingual fairness |
| `difficulty` | easy / hard |

Example case shape:

```json
{
  "id": "refund-045d-001",
  "input": {"messages": [{"role": "user", "content": "Refund after 45 days?"}]},
  "expected": {
    "must_include_facts": ["30 day standard window"],
    "must_not_include": ["guaranteed refund"],
    "required_citations": ["policy-refunds-v3"]
  },
  "metadata": {"category": "billing", "risk": "medium"}
}
```

**Splits:**

| Split | Size (start) | Rule |
| --- | --- | --- |
| Dev | 20–50 | Engineers iterate |
| CI regression | 100–500 | Gate merges |
| Held-out | 50–200 | Never tune prompts against it |
| Safety / injection | 50+ | Grows weekly |

Pin **dataset hash**, model id, prompt version, and index snapshot in every eval report.

### Dataset pitfalls

- **Tuning the held-out set** until it passes — keep one frozen set.
- **Stale gold** after policy change — attach `valid_until` or review quarterly.
- **Synthetic-only** — models match synthetic phrasing; mix real logs.
- **Leaky eval index** — retrieval setup must match production permissions.

---

## Regression suites

Regression evals ask: **did we break what worked?** Same cases, same graders, compare to a **pinned
baseline** (previous release or main branch).

**CI gate examples:**

- `pass_rate >= baseline - 0.02` on regression set.
- Safety slice: injection ASR **must not** increase by > 1 point.
- p95 latency and cost per success within SLO.

**Statistical hygiene (small N):**

- Bootstrap **confidence intervals** on pass rate — overlapping CIs mean “maybe noise.”
- **McNemar** on paired pass/fail when A and B run on identical cases — do not compare different
  random subsets.
- **Pre-register** minimum effect and max regression before peeking at candidate scores.

```text
release decision
────────────────
run candidate on pinned dataset hash
        │
        ▼
programmatic gates ──fail──► stop
        │ pass
        ▼
primary metric + CI vs baseline ──► ship or reject
```

For agents, report **pass@k** (≥1 success in k runs) and **pass^k** (all k succeed) on high-risk
tasks — pass@1 alone flatters demos.

---

## LLM-as-judge

Use a strong model to score rubrics (correctness, groundedness, tone) or **pairwise** preferences
(A vs B).

### When it works

- Subjective quality, tone, clarity.
- Rubric with explicit criteria and JSON scores.
- Pairwise with **order swapped** and averaged.

### Caveats (systematic biases)

| Bias | Mitigation |
| --- | --- |
| Prefers longer answers | Score one criterion per call; penalize verbosity in rubric |
| Same model family as generator | Different-family or stronger judge |
| Position bias (A first wins) | Run twice with swapped order |
| Self-preference | Never judge with the same model that generated |
| Misses subtle domain errors | Human calibration on 100+ labels; structured `reference_facts` |

**G-Eval pattern:** judge reasons step-by-step, then emits JSON scores — improves human correlation.

**Calibrate:** Pearson/Spearman vs human labels; tune rubric where they disagree; **freeze** judge
model + prompt version.

**Cost cascade:** cheap programmatic → embedding similarity (calibrated threshold) → full judge only on
borderline or high-risk slices.

Treat judge scores as **noisy labels**, not ground truth — especially for legal/medical facts.

---

## Metrics (what to measure)

| Layer | Examples |
| --- | --- |
| **Classification** | Accuracy, F1, exact match on slots |
| **RAG retrieval** | recall@k, nDCG (with qrels), hit rate |
| **RAG generation** | faithfulness, relevance, citation match |
| **Agent outcome** | DB state, tests pass, API mock assertions |
| **Agent trajectory** | required/forbidden tools, step count |
| **Efficiency** | tokens, cost per success, latency p95 |
| **Safety** | injection ASR, forbidden tool rate |

RAG failure taxonomy — score separately:

```text
retrieval_miss → retrieval_noise → generation_ignore → hallucinate → citation_wrong
```

Fix **recall@k** before prompt-tuning answers when facts are missing from context.

---

## Guardrail layers

Defense in depth — no single classifier must be perfect.

```text
                         ┌──────────────────────────────────────────┐
  user ──► INPUT layer ──►│ topic, injection heuristics, PII scrub,  │
                         │ rate limits, max tokens                    │
                         └──────────────────┬───────────────────────┘
                                            ▼
                         ┌──────────────────────────────────────────┐
                         │ LLM / agent core                           │
                         └──────────────────┬───────────────────────┘
                                            ▼
                         ┌──────────────────────────────────────────┐
                         │ OUTPUT layer: schema, moderation, toxicity,│
                         │ secret leak scan, grounding checks         │
                         └──────────────────┬───────────────────────┘
                                            ▼
                         ┌──────────────────────────────────────────┐
                         │ ACTION layer: tool allow-list, arg schema, │
                         │ path/domain limits, HITL, budgets          │
                         └──────────────────┬───────────────────────┘
                                            ▼
                                         user / world
```

**Principles:**

1. **Fail closed** on high risk when checks error — block or escalate, not silent allow.
2. **Policy in code/config** — business rules not only in system prompt.
3. **Observable** — stable `reason_code` on every block/redact (`PII_REDACTED`, `TOOL_DENIED`).
4. **Test guardrails in CI** — adversarial strings expect block/escalate.

---

## Input guardrails: PII, toxicity, abuse

Run **before** the model and before logging raw text where possible.

| Control | Purpose |
| --- | --- |
| **PII detection** | PAN (Luhn), gov IDs, API-key patterns → redact or tokenize |
| **Secrets in user paste** | Block or strip before context |
| **Toxicity / policy classifiers** | Hate, sexual content, self-harm — threshold per product |
| **Topic routing** | Out-of-scope → fixed refusal template (not model-improvised debate) |
| **Injection heuristics** | Patterns + classifiers — weak alone; pair with permissions |
| **Rate limits** | Per user/IP/key; max attachment size and input tokens |
| **Normalization** | NFC unicode, strip nulls, cap repetition |

**Structural isolation:** untrusted documents in delimited fields the orchestrator treats as **data** —
delimiters are **not** a security boundary; they help honest models only.

Return **fixed refusal templates** for policy blocks — reduces jailbreak debate loops.

---

## Output validation

Model text is **untrusted** at integration boundaries (OWASP **LLM05** improper output handling).

| Check | When |
| --- | --- |
| **JSON / schema** | Pydantic, Zod, `json.loads` — repair prompt at most 1–2×, then fail closed |
| **Business invariants** | Enums, date ranges, numeric bounds in code |
| **Moderation API** | Hate, violence, sexual content on user-visible text |
| **Secret leak scan** | JWTs, API keys, internal hostnames in output |
| **PII in output** | Other users’ data, accidental context bleed |
| **Grounding / citations** | RAG: cited chunk supports claim; empty retrieval → approved refusal only |
| **System prompt leakage** | Probes for “repeat above” — log rate |

Never: `eval(model_output)`, render HTML unsanitized, concatenate into SQL/shell.

```text
answer ──► parse schema ──fail──► repair (≤2) ──fail──► safe user message
                │ pass
                ▼
         moderation + leak scan ──► deliver
```

---

## Action guardrails (agents)

When tools **write**, output text checks are insufficient.

| Control | Examples |
| --- | --- |
| **Least privilege** | Read-only default; short-lived scoped tokens |
| **Allow-lists** | Paths, commands, HTTP methods, domains |
| **Deny lists** | `rm -rf`, arbitrary outbound email |
| **HITL** | Money, legal send, delete, first-time external recipient |
| **Budgets** | Max steps, tokens, wall time, spend per session |
| **Sandbox** | Container, no prod creds, egress proxy |
| **Idempotency** | Retries/resume must not double-charge or duplicate tickets |

**Lethal trifecta** (Willison): **private data + untrusted content + external comms** together →
exfil via indirect injection. Break a leg: quarantined reader with no send tool; or no browsing before
send; HITL on outbound.

Measure **attack success rate (ASR)** on a separate injection eval set — regression when “helpful” prompt
changes widen holes.

---

## Evaluating agents (outcome + trajectory)

| Dimension | Prefer |
| --- | --- |
| **Outcome** | Programmatic: tests, DB state, mock API |
| **Trajectory** | Required/forbidden tools; no dangerous sequence |
| **Efficiency** | Steps, tokens, cost per success |
| **Safety** | ASR on injection cases attempting forbidden tools |

Never run write evals against production. Use mocks, ephemeral DBs, recorded tool fixtures. Log full
traces; human-review sampled failures — judges miss “technically passed but hostile.”

**Harness shape:** JSON cases → `run_app` → programmatic checks → optional judge → summarize with
version pins; CI fails if `pass_rate` drops below baseline − δ.

**Online logs (redacted):** request/session ids, prompt/model/index versions, retrieval and tool
traces, tokens/cost, guardrail `reason_code`, user feedback — alert on latency, empty retrieval,
refusal spikes, and block-rate storms (false positives).

---

## Pitfalls

1. **Single-turn eval for multi-turn agents** — context poisoning and mid-conversation inject missed.
2. **BLEU/ROUGE on prose** — use facts, programmatic checks, faithfulness judges.
3. **No retrieval-only metrics** — weeks tuning prompts while recall@k is 55%.
4. **One global pass rate** — security slice regresses while chitchat improves.
5. **LLM judge without human calibration** — false confidence.
6. **Guardrails only in system prompt** — not enforcement.
7. **Logging full prompts with PII** — creates disclosure while fixing quality.
8. **Prod write tools in evals** — real emails and charges.
9. **Ignoring cost in release notes** — quality up 2 points at 10× spend.
10. **Delimiter “untrusted” blocks** — `# DOCUMENT` does not stop injection.
11. **Skipping indirect injection** — only testing direct user jailbreaks.
12. **No version pins in reports** — “passed last week” is irreproducible.

---

## Quick check

**Offline vs online evals — when each?**  
Offline on pinned golden sets gates every prompt/model/index change in CI. Online samples live traffic
(with redaction) to catch drift, new query clusters, and retrieval corruption that offline sets never
included — use online to **prioritize new cases**, not as the only gate.

**What belongs in a golden case beyond a reference answer string?**  
Structured `must_include_facts`, `must_not_include`, citation ids, regex or schema for agents,
metadata tags for slicing, and for agents `allowed_tools` / success predicates — prose-only gold goes
stale and misses paraphrased lies.

**Three LLM-as-judge biases and mitigations?**  
Verbosity → one criterion per call; position → swap A/B order; self-preference → different judge
model; always calibrate to human labels on a sample before trusting pass thresholds.

**Draw the guardrail layers in order.**  
Input (PII, limits, injection heuristics) → model/agent → output (schema, moderation, leak scan,
grounding) → action (tool allow-list, HITL, budgets). Policy and allow-lists live closest to side
effects.

**How do regression suites differ from red teaming?**  
Regression ensures **known** cases stay fixed over time; red team searches for **novel** failures.
Red team findings should become regression cases; regression tracks pass rate/ASR trends, red team
explores the unknown.
