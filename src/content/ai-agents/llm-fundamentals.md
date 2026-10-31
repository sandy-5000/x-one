---
title: "LLM fundamentals"
order: 1
---
# LLM Fundamentals

A **large language model** assigns probabilities to the **next token** given everything before it. Chat, coding assistants, and agents are that one operation in a loop. This page covers tokens, the decoder-only transformer, training vs inference, sampling, context limits, hallucination, and how to think about model families — enough to reason about cost, latency, and failure modes without hand-waving.

```text
 prompt tokens ──► embed ──► L × (attention + MLP) ──► logits ──► sample 1 token
      ▲                                                      │
      └──────────────── append; repeat until stop ───────────┘
```

| Concept | One-line idea |
| --- | --- |
| Autoregressive LM | Models `P(x_{t+1} \| x_1..x_t)` at every step |
| Token | Integer id from a fixed vocabulary (32k–256k); not characters |
| Causal attention | Position `t` may only attend to positions `≤ t` |
| Training | Parallel over all positions; loss on next-token prediction |
| Inference | Sequential output; only last position's logits used each step |

---

## Key points

- **Generation is sequential in output** — token 50 cannot be chosen before token 49; the prompt can be processed in parallel (prefill), then decode is one token at a time.
- **The model is stateless across API calls** — "memory" in chat is your app re-sending history or retrieving external facts.
- **Tokens drive cost, limits, and quirks** — billing, context windows, weak letter-counting, and uneven cost across languages all follow from sub-word tokenization.
- **Decoder-only stack** — embedding → `L` blocks of pre-norm causal multi-head attention + SwiGLU MLP with residuals → LM head → `V` logits per position.
- **Attention** — content-based lookup: queries match keys, weights sum values; causal mask prevents peeking at the future and enables KV reuse at inference.
- **Training** — pre-train on next-token prediction, then post-train (SFT, preference tuning, sometimes RL on verifiable tasks); objective rewards plausible text, not truth.
- **Sampling** — temperature reshapes the distribution; top-p (nucleus) adapts how many candidates stay in play; greedy and high temperature both have failure modes.
- **Context window** — hard cap on input + output tokens; long prompts cost compute (`O(T²)` attention on prefill) and KV memory; effective retrieval often shorter than the advertised window.
- **Hallucination** — confident false output is structural; mitigate with grounding, tools, abstention, and evals, not prompts alone.
- **Model families** — closed API frontier vs open weights (customisation, on-prem); tiered sizes and MoE are the durable patterns, not any single model name.
- **Chat templates** — roles become one token stream via special tokens; wrong formatting silently hurts open-weight models.

---

## What the model computes

Language modelling factorises a sequence left to right:

```text
P(x1..xT) = P(x1) · P(x2|x1) · ... · P(xT|x1..xT-1)
```

Each step the network outputs **logits** `z` (one score per vocabulary entry), then **softmax** turns them into probabilities. Example after `"The capital of France is"`:

| Next token | Rough probability |
| --- | --- |
| `" Paris"` | high |
| other plausible continuations | small |
| long tail (~100k ids) | tiny each, non-zero in sum |

Predicting the next token well on diverse text forces internal structure (syntax, facts, code patterns) even though the loss never mentions "understanding."

---

## Tokenization

Text becomes a sequence of integer **token ids** from a fixed vocabulary. Modern models mostly use **byte-level BPE**: base symbols are 256 bytes, merges build frequent substrings — no unknown-token hole for any UTF-8 string.

```text
"Unbelievable!"  ──►  sub-word pieces  ──►  [2265, 9880, ...]
```

| Unit | Trade-off |
| --- | --- |
| Characters | Tiny vocab, very long sequences |
| Words | Open vocabulary, poor morphology |
| Sub-words (BPE, SentencePiece) | 32k–256k vocab; common compromise |

**Why tokens matter in products**

| Effect | Cause |
| --- | --- |
| Cost and rate limits | Priced per token, not per word |
| Context cap | Window counts tokens |
| Weak exact arithmetic / letter counts | Digits and letters may span multiple tokens |
| Multilingual unfairness | Non-English text often needs more tokens per meaning |

Rule of thumb for English: ~4 characters or ~0.75 words per token — **use the tokenizer of the model you call**; counts differ 10–30% across tokenizers.

```python
import tiktoken
enc = tiktoken.get_encoding("o200k_base")
text = "def add(a, b): return a + b"
print(len(enc.encode(text)), enc.encode(text))
```

---

## Embeddings and position

**Token embedding:** lookup table `E[V × d]` — id `i` → vector of dimension `d_model` (e.g. 4096).

**Position:** self-attention alone is permutation-invariant, so models inject **where** each token sits. Most modern LLMs use **RoPE** (rotary): rotate query/key pairs by an angle that depends on position so attention scores depend on **relative** distance.

| Scheme | Idea |
| --- | --- |
| Learned absolute | One vector per position 0..max_len |
| RoPE | Rotation on Q/K; relative distance in dot products |
| ALiBi | Distance penalty on attention scores |

Extending context beyond training length uses RoPE scaling + short fine-tune on long documents (PI, NTK-aware, YaRN).

---

## Transformer stack (decoder-only)

Original Transformers had encoder + decoder; **LLMs are decoder-only**: causal self-attention + MLP, repeated `L` times.

```text
 ids ──► embed ──►  ┌─ RMSNorm ──► causal MHA (+ RoPE) ──► residual ──┐
                    │  RMSNorm ──► SwiGLU MLP ──► residual            │ × L
                    └───────────────────────────────────────────────────┘
                              ──► final norm ──► LM head (d × V) ──► logits
```

Typical 8B-ish shape: `L ≈ 32`, `d ≈ 4096`. The **residual stream** (`T × d`) is shared working memory; attention **mixes across positions**, MLP **transforms within** each position.

**Parameter scale (dense, rough):** `N ≈ 12 · L · d²` plus embeddings; 70B-class models use **GQA** (grouped-query attention) — fewer KV heads than query heads — to shrink inference KV cache with little quality loss.

---

## Attention intuition

For each position, learned projections produce **query** Q, **key** K, **value** V:

```text
scores[i,j] = (Q_i · K_j) / sqrt(d_k)     (+ mask: j > i → -∞)
weights_i   = softmax(scores[i,:])
output_i    = Σ_j weights[i,j] · V_j
```

| Piece | Intuition |
| --- | --- |
| Q | "What am I looking for?" |
| K | "What do I advertise?" |
| V | "What I contribute if selected" |
| Causal mask | No future tokens; training can predict all positions in parallel |
| `1/sqrt(d_k)` | Keeps dot-product variance stable so softmax doesn't saturate |

**Multi-head:** several parallel attentions on slices of `d`, then concat + mix — different heads specialize (syntax, copy-from-earlier, etc.).

Mini causal pass (3 tokens, schematic):

```text
token 1 sees only itself
token 2 blends 1 and 2
token 3 blends 1, 2, 3 — strongest match often includes its own key
```

At **inference**, keys and values for past tokens need not be recomputed — stored in the **KV cache** (see the inference page).

---

## Training vs inference

| | Training | Inference (generation) |
| --- | --- | --- |
| Direction | All positions in one forward pass | One new token per step (after prefill) |
| Loss / output | Cross-entropy at every position | Softmax at **last** position only |
| Attention | Causal mask, full sequence parallel | Append one token; reuse cached K/V |
| Dominant cost | Compute (FLOPs), huge data | Memory bandwidth on decode; KV grows with length |
| Randomness | Usually none in forward | Sampling chooses each token |

**Pipeline (high level):**

```text
pre-train (next token on web/code/books)
    → SFT (instruction/chat pairs; loss on assistant tokens only)
    → preference tuning (RLHF/DPO/RLAIF — helpful, safe style)
    → optional RL on verifiable rewards (math, code tests — long "thinking")
```

**Chinchilla-style rule of thumb:** ~20 training tokens per parameter for compute-optimal pre-training; many production models are **over-trained** (far more tokens per parameter) because **inference** cost scales with size and dominates at scale.

---

## Decoding: temperature, top-p, and friends

Logits `z` → adjust → softmax → pick one token.

**Temperature** `T`: use `z_i / T` before softmax.

| T | Effect |
| --- | --- |
| → 0 | Near greedy (argmax) |
| 1 | Model's native distribution |
| > 1 | Flatter; more random, more tail junk |

**Top-p (nucleus):** keep the smallest set of tokens whose cumulative probability ≥ `p`; sample within that set. Adapts to confidence — unlike fixed top-k.

**Practical guidance:** tune temperature **or** top-p with evals, not both blindly; factual extraction → lower temperature; creative writing → moderate temperature + top-p ~0.9.

| Method | Rule |
| --- | --- |
| Greedy | Always argmax; repetitive loops |
| top-k | Fixed k candidates |
| top-p | Dynamic nucleus to mass `p` |
| min-p | Drop tokens below `min_p × p_max` |

Penalties (frequency, presence, repetition) nudge away from loops — over-penalize and structured output (JSON braces) breaks.

**Temperature 0** is not always bit-identical across runs (GPU reduction order, near ties).

---

## Context windows and limits

```text
system + tools + history + RAG + user message + output  ≤  max context
```

| Pressure | Why |
| --- | --- |
| Prefill compute | Attention over prompt length (quadratic in full attention) |
| KV memory | Grows linearly with total tokens × layers × KV heads |
| Billing | Every input token charged every call |
| "Lost in the middle" | Facts in the middle of long prompts retrieved worse than start/end |

Design: retrieve fewer, better chunks; put instructions and the question **after** long documents; summarize old agent turns instead of infinite history.

---

## Hallucination

**Hallucination** = fluent output that is false or unsupported by provided sources.

| Driver | Mechanism |
| --- | --- |
| Training objective | Rewards plausible continuations, not verified truth |
| Knowledge gaps | Rare or post-cutoff facts absent or weak |
| Evaluation incentives | Guessing beats "I don't know" on many benchmarks |
| Sampling | One wrong token, then confident completion |
| Sycophancy | Preference tuning agrees with user premises |

| Mitigation | Role |
| --- | --- |
| RAG + cite sources | Grounding; verify quotes in retrieval |
| Tools (search, code, DB) | External truth for facts and math |
| Abstention allowed | Reduces forced guessing |
| Lower temperature + structured output | Fewer wild tail draws; validate fields |
| Domain evals | Measure rate on **your** data |

You cannot prompt hallucination to zero — design for **detectable** failure (sources, validation, human review for high stakes).

---

## Chat format and roles

APIs send `{role, content}` lists; the model sees **one** byte sequence from a **chat template** (special tokens per family).

```text
<|im_start|>system
You are terse.<|im_end|>
<|im_start|>user
Question?<|im_end|>
<|im_start|>assistant
```

Open models: always `apply_chat_template` from the tokenizer — manual string concat is a common "model seems dumb" bug.

| Role | Typical use |
| --- | --- |
| system / developer | Standing policy, format |
| user | Human or caller input (often untrusted) |
| assistant | Prior model turns |
| tool | Tool results (untrusted — injection risk) |

---

## Model families (high level)

| | Closed (API) | Open weights |
| --- | --- | --- |
| Access | Hosted frontier | Download / self-host / many hosts |
| Ops | None | GPUs, quantisation, templates |
| Data | Leaves boundary (check retention) | Can run air-gapped |
| Licence | Terms of service | Apache, MIT, or custom restrictions |

**Durable trends:** tiered small/medium/large per vendor; **MoE** at scale (many total params, fewer active per token); long-context marketing vs **effective** context on hard benchmarks; reasoning as a **budget** parameter on some APIs.

Choosing: define task + eval set → prove feasibility with a strong model → step down for cost/latency → route easy traffic to small models → pin versions in production.

---

## Training vs inference — product consequences

```text
                    TRAINING                    INFERENCE
 compute           massive, once               per request, forever
 parallelism       all prompt positions        output: serial steps
 state             weights + optimizer         weights + growing KV cache
 quality knobs     data, stages, RL            temperature, top-p, tools, RAG
```

Output tokens are typically **several×** input price on APIs because decode re-reads all weights (and KV) per token while prefill amortizes over the prompt.

---

## Pitfalls

1. **Treating the model as a database** — facts are lossy; ground important claims.
2. **Assuming cross-call memory** — every request is fresh unless you send state.
3. **Wrong chat template** on open weights — silent quality collapse.
4. **Maxing context** — more tokens often **lower** accuracy and always raise cost.
5. **High temperature for extraction** — use low T and validate outputs.
6. **Tuning temperature and top-p together** without evals.
7. **Trusting JSON mode alone** — schema-constrained decoding still needs semantic validation.
8. **Ignoring tokenization** for non-English billing and latency estimates.
9. **Expecting determinism at temperature 0** — design tests for small variation.
10. **Reasoning models for everything** — hidden thinking tokens explode cost and latency.

---

## Quick check

**What does one forward pass produce during chat generation?**  
After embeddings and `L` decoder blocks, the **LM head** yields logits over the vocabulary at each position; during autoregressive generation you only **sample from the last position** after each append. Prefill computes logits for the whole prompt in parallel; decode adds one token at a time.

**Why does causal masking matter for training and serving?**  
It ensures position `t` cannot attend to future tokens, so parallel training predicts `x_{t+1}` without cheating. Because past K/V never change when new tokens arrive, those tensors can be **cached** during decode instead of recomputing the full prefix.

**How do temperature and top-p differ?**  
Temperature scales all logits before softmax — lower sharpens toward the favorite, higher flattens toward randomness. Top-p keeps the smallest set of tokens whose cumulative probability reaches `p`, then samples within that nucleus, adapting width to how peaked the distribution is.

**Why do LLMs hallucinate even when prompted to be accurate?**  
Pre-training and post-training optimize **plausible** continuations; knowledge is incomplete and sampling can commit to a wrong token early. Mitigations are architectural (RAG, tools, verification, abstention) and operational (evals), not a single magic system prompt.

**Open weights vs closed API — what should drive the decision?**  
Run **your** evals on task quality, then compare **total** cost (tokens × price or GPU + ops), latency, context needs, compliance, and licence. APIs win at low or spiky volume; open weights win for air-gap, heavy customisation, or sustained high utilisation you can benchmark on real hardware.
