---
title: "LLM inference & serving"
order: 9
---
# LLM Inference and Serving

What happens when you run a model in production, and how to make it fast and cheap.

## Two phases

```text
PREFILL   process all prompt tokens in parallel   → compute-bound, fast per token
DECODE    generate output one token at a time     → memory-bandwidth-bound, slow per token
```

Key metrics:

| Metric | Meaning |
| --- | --- |
| TTFT (time to first token) | Latency until streaming starts — dominated by prefill and queueing |
| TPOT / ITL (time per output token) | Speed of streaming — dominated by decode |
| Throughput | Total tokens/s across all users on the hardware |
| Cost per 1M tokens | Input and output are priced separately; output is typically 3–5× pricier |

## KV cache

During decoding, attention needs the keys and values of every previous token. Recomputing them each step would be wasteful, so they are cached in GPU memory — the **KV cache**.

- Memory grows with `layers × heads × head_dim × sequence_length × batch_size`.
- Long contexts and many concurrent users run out of KV-cache memory long before compute.
- **PagedAttention** (vLLM) stores the cache in fixed-size pages like virtual memory, cutting fragmentation and allowing more concurrent requests.

## Prompt caching

If many requests share the same prefix — a long system prompt, tool definitions, a big document — providers can reuse its computed KV cache.

- Typically **50–90% cheaper** and much lower TTFT for the cached part.
- Put **stable content first** (system prompt, tools, documents) and variable content (the user's question) last.
- Caches expire after minutes of inactivity.

## Batching

GPUs are efficient only with many requests in flight.

- **Static batching** waits for a batch to fill and finish together — slow requests hold up fast ones.
- **Continuous (in-flight) batching** adds and removes requests every decoding step — the standard in modern servers.

## Making models smaller and faster

- **Quantization** — store weights in 8-bit, 4-bit, or lower instead of 16-bit. Formats: GPTQ, AWQ, GGUF (llama.cpp), FP8. Roughly halves memory per halving of bits, with small quality loss at 8-bit and modest loss at 4-bit.
- **Distillation** — train a small "student" model to imitate a large "teacher".
- **Speculative decoding** — a small draft model proposes several tokens; the big model verifies them in one pass. Same output quality, 2–3× faster decoding.
- **Mixture of Experts (MoE)** — only a few expert sub-networks run per token, so a huge model costs like a small one per token (but still needs memory for all weights).

### Memory estimate

```text
weights ≈ parameters × bytes per parameter

70B model in FP16 (2 bytes)  ≈ 140 GB  → multiple GPUs
70B model in 4-bit (0.5 B)   ≈  35 GB  → one 48 GB GPU
8B model in 4-bit            ≈   4–5 GB → laptop
```

Add headroom for the KV cache and activations.

## Serving stacks

| Tool | Best for |
| --- | --- |
| vLLM | High-throughput GPU serving, OpenAI-compatible API |
| SGLang | Fast serving with strong prefix caching, structured output |
| TensorRT-LLM / Triton | Maximum performance on NVIDIA hardware |
| Text Generation Inference (TGI) | Hugging Face ecosystem |
| llama.cpp / Ollama / LM Studio | Local and CPU / Apple Silicon inference with GGUF models |

Most expose an **OpenAI-compatible HTTP API**, so client code doesn't change when you swap backends.

```bash
# Serve an open-weight model with vLLM
vllm serve Qwen/Qwen2.5-7B-Instruct --max-model-len 32768

# Local with Ollama
ollama run llama3.1:8b
```

## Cost and latency playbook

1. **Pick the smallest model that passes your evals.** Route only hard requests to big models.
2. **Cache:** prompt caching for shared prefixes; response caching for repeated identical questions; semantic caching (embedding similarity) with care.
3. **Trim context:** retrieve fewer, better chunks; summarise history; drop unused tool definitions.
4. **Limit output:** set `max_tokens`, ask for concise formats.
5. **Stream** responses so users see progress immediately.
6. **Batch APIs** for offline jobs — often ~50% cheaper with hours of latency allowed.
7. **Parallelise** independent calls instead of chaining them.

## Production concerns

- **Rate limits** are per tokens-per-minute and requests-per-minute — retry with exponential backoff on 429s.
- **Fallbacks** to a second provider or model when one is down.
- **Timeouts** sized for long generations; prefer streaming over a single long-held request.
- **Observability:** log model, prompt version, token counts, latency, cost, and outcome for every call.
- **Data handling:** know each provider's retention and training policy; redact PII before sending if required.
