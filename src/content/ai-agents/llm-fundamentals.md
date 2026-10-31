---
title: "LLM fundamentals"
order: 1
---
# LLM Fundamentals

A large language model (LLM) is a neural network trained to predict the **next token** given the tokens before it. Everything else — chat, coding, tool use, agents — is built on top of that one operation, repeated in a loop.

## Tokens

Models don't see characters or words; they see **tokens** — sub-word pieces from a fixed vocabulary (typically 50k–200k entries), produced by algorithms such as Byte-Pair Encoding (BPE).

```text
"Unbelievable results!"  →  ["Un", "believ", "able", " results", "!"]  →  [1844, 33467, 481, 3135, 0]
```

Rules of thumb for English:

- 1 token ≈ 4 characters ≈ 0.75 words.
- 1,000 tokens ≈ 750 words ≈ 1.5 pages.
- Code, non-English text, and numbers usually take **more** tokens per character.

Tokens matter because **pricing, speed, and limits are all measured in tokens**.

## Context window

The context window is the maximum number of tokens the model can look at in one call — the prompt **plus** the generated output. Modern models range from tens of thousands to over a million tokens.

```text
system prompt + tool definitions + chat history + new message + output  ≤  context window
```

The model has **no memory between calls**. A "conversation" is the app re-sending the whole history each turn. Long contexts cost more, run slower, and recall from the middle of a very long prompt can be weaker ("lost in the middle").

## How generation works

```text
prompt tokens ──► model ──► probability for every token in the vocabulary
                                │
                       sample one token
                                │
                 append it to the input and repeat
                 until an end token or max_tokens
```

This is **autoregressive** generation: one token at a time, each depending on all previous ones. That's why output is slower than input processing, and why streaming responses arrive word by word.

## Sampling parameters

| Parameter | Effect | Typical use |
| --- | --- | --- |
| `temperature` | Flattens (high) or sharpens (low) the probability distribution | 0–0.3 for extraction and code, 0.7–1.0 for creative writing |
| `top_p` | Sample only from the smallest set of tokens whose probabilities sum to p | 0.9–1.0; tune either this or temperature, not both |
| `top_k` | Sample only from the k most likely tokens | Some APIs only |
| `max_tokens` | Hard cap on output length | Always set it |
| `stop` | Strings that end generation early | Structured formats |

Temperature 0 is close to deterministic but not guaranteed identical across runs.

## The transformer in one paragraph

Each token becomes a vector (an **embedding**). A stack of transformer layers repeatedly applies **self-attention** — every token looks at every earlier token and decides how much each matters — followed by a feed-forward network. The final vector for the last position is turned into a probability over the vocabulary. Attention cost grows with the square of the sequence length, which is why long contexts are expensive.

## How models are trained

1. **Pre-training** — next-token prediction on trillions of tokens of web text, books, and code. The model learns language, facts, and reasoning patterns. Extremely expensive.
2. **Supervised fine-tuning (SFT)** — train on curated instruction → response examples so it behaves like an assistant.
3. **Preference tuning** — RLHF, DPO, or similar: humans or models rank responses and the model is pushed toward preferred ones (helpful, honest, harmless).
4. **Reasoning training** — reinforcement learning on problems with checkable answers (math, code) teaches the model to "think" step by step before answering.

## Chat message format

APIs take a list of role-tagged messages:

```json
[
  { "role": "system",    "content": "You are a concise assistant for Java developers." },
  { "role": "user",      "content": "What does `final` mean on a method?" },
  { "role": "assistant", "content": "It can't be overridden by subclasses." },
  { "role": "user",      "content": "And on a class?" }
]
```

- **system** — standing instructions and persona.
- **user** — the human (or calling program).
- **assistant** — the model's earlier replies.
- **tool** — results of tool calls (see the Tool calling page).

## Strengths and failure modes

**Good at:** summarising, rewriting, translating, extracting structure from text, writing and explaining code, brainstorming, following a format.

**Watch out for:**

- **Hallucination** — fluent, confident, wrong. Ground answers in retrieved sources and ask for citations.
- **Knowledge cutoff** — no knowledge after training unless you supply it (RAG, web search tools).
- **Arithmetic and exact counting** — use a code-execution tool instead.
- **Prompt sensitivity** — small wording changes can change results; evaluate with test sets.
- **Non-determinism** — the same input can give different outputs.

## Choosing a model

- **Frontier models** — best reasoning and coding; highest cost and latency.
- **Small / fast models** — classification, extraction, routing, autocomplete; cheap and quick.
- **Open-weight models** (Llama, Qwen, Mistral, Gemma, DeepSeek) — run on your own hardware for privacy, cost control, or fine-tuning.

A common production pattern is **routing**: a small model handles easy requests and escalates hard ones to a larger model.
