---
title: "LLM inference & serving"
order: 9
---
# LLM Inference & Serving

After training, **serving** turns weights into streamed tokens on GPUs (or CPUs). Almost every optimisation follows one fact: **decode is usually memory-bandwidth-bound**, not FLOP-bound — each step re-reads all weights (and the growing KV cache) for roughly one new token per sequence. This page covers prefill vs decode, KV cache, batching, quantisation, engines (vLLM, TGI), latency vs throughput, speculative decoding, and hardware basics. Assumes causal attention and GQA from LLM fundamentals.

```text
 request ──► queue ──► PREFILL (parallel prompt) ──► DECODE (1 token/step) ──► stream
                              │                              │
                              └── writes KV cache ───────────┘ reads KV each step
```

| Phase | Bottleneck | Sets |
| --- | --- | --- |
| Prefill | Compute (many tokens per weight read) | TTFT |
| Decode | HBM bandwidth (batch ≈ intensity) | TPOT / tokens/s |

---

## Key points

- **Prefill** processes the whole prompt in parallel and fills the **KV cache**; **decode** runs one token at a time and re-reads weights + full KV each step.
- **Arithmetic intensity** on decode ≈ batch size (FLOPs per byte read); on an H100-class GPU the ridge is ~300 FLOPs/byte — batch-1 decode sits far below it.
- **KV cache size** often limits concurrency and long context more than weight memory: `2 × layers × kv_heads × head_dim × bytes × sequence_length`.
- **PagedAttention** (vLLM) stores KV in fixed blocks with a block table — less waste than reserving `max_seq_len` per request; **prefix caching** skips prefill for identical prompt prefixes.
- **Continuous batching** re-forms the batch every decode step so finished sequences leave and new ones join; mandatory for good GPU utilisation.
- **Quantisation** shrinks bytes per weight (and optionally KV): FP8 near-lossless on Hopper+; INT4/AWQ/GPTQ for single-GPU; always eval on your tasks.
- **Latency vs throughput** trade off — bigger batches raise aggregate tokens/s but slow each step; **goodput** = load where TTFT/TPOT SLOs still hold.
- **Speculative decoding** drafts several tokens, verifies in one target forward pass — exact target distribution; best at low batch when GPU has spare compute.
- **vLLM** is the default open GPU server (PagedAttention, OpenAI-compatible API); **TGI** is in maintenance — migrate to vLLM or SGLang.
- **Hardware:** capacity (GB) decides what fits; **bandwidth** (TB/s) caps decode tokens/s; NVLink matters for tensor parallel within a node.

---

## Prefill vs decode

```text
 2,000 prompt + 400 output tokens

 PREFILL   one pass over 2,000 tokens → KV for all prompt positions → logits for token 1
 DECODE    400 passes, each: read weights + all KV, append one K/V row, sample
```

| | Prefill | Decode |
| --- | --- | --- |
| Tokens per step | Entire prompt (or chunk) | 1 per sequence |
| Bottleneck | Compute | Memory bandwidth |
| API pricing | Lower $/token | Often 3–8× input $/token |

**Roofline sketch (H100-class, order of magnitude):**

```text
 BF16 matvec-ish decode, batch 1     ~1 FLOP/byte     → memory-bound (~150–200 tok/s for 8B)
 prefill, 2k tokens in pass           ~2k FLOPs/byte   → compute-bound
```

**8B BF16 weights ≈ 16 GB:** one decode step reads ~16 GB; at ~3.3 TB/s → ~5 ms/step → ~200 tok/s ceiling (real kernels ~70–85% of peak). **Batching** shares the weight read across sequences — throughput scales until KV reads dominate.

**Batch 64, 2k context each (same 8B):** weights 16 GB once + KV `64 × 2000 × 128 KiB ≈ 16 GB` per step → ~32 GB / 3.3 TB/s ≈ 10 ms → ~6,400 aggregate tok/s (~100 tok/s per user). Long contexts push KV reads ahead of weight reads — decode stays bandwidth-bound at any batch.

---

## Latency vs throughput

| Metric | Meaning | User feels |
| --- | --- | --- |
| **TTFT** | Request → first output token | "Did it start?" |
| **TPOT** | Average time per output token after first | Reading speed |
| **ITL** | Gap between consecutive tokens (distribution) | Stutter |
| **E2E** | Request → last token | Total wait |
| **Throughput** | Total output tokens/s on hardware | Cost efficiency |
| **Goodput** | Requests/s meeting latency SLOs | Real capacity |

Interactive chat targets (rule of thumb): TTFT ≲ 1 s, TPOT ≲ 50 ms (~20+ tok/s). **Throughput and latency oppose** — raising batch size helps tokens/s but increases per-step time.

Measure under **realistic concurrency** and prompt lengths; report p50/p95/p99, not means only.

```bash
# Shape of a load test (check your engine's CLI)
vllm bench serve --model meta-llama/Llama-3.1-8B-Instruct \
  --random-input-len 2000 --random-output-len 400 --request-rate 8
```

---

## KV cache

Without cache, each decode step would recompute attention over the full prefix — **O(T²)** per token instead of **O(T)**.

```text
 KV bytes/token = 2 × L × n_kv_heads × head_dim × bytes_per_elt

 Example (70B-class GQA): 2 × 80 × 8 × 128 × 2 (BF16) ≈ 320 KiB/token
 128K context × one seq          ≈ 40 GiB KV alone
 32 × 8K concurrent chats        ≈ 80 GiB KV
```

| Technique | Effect |
| --- | --- |
| GQA / MQA | Fewer KV heads → smaller cache |
| FP8 KV | ~half memory and bandwidth |
| MLA (some models) | Compressed latent per token |
| PagedAttention | On-demand blocks; less reserved waste |
| Prefix cache | Reuse blocks for identical prompt prefix |

**PagedAttention (concept):**

```text
 logical blocks per request  ──block table──►  physical GPU block pool
 finished seq frees blocks; new seq allocates; prompt blocks shareable (refcount)
```

When GPU KV is full, engines **preempt** — evict and recompute later (latency spike).

**Prefix caching:** hash fixed-size KV **blocks** from the start of the prompt; on a cache hit, skip prefill for that prefix (TTFT drops to suffix-only). Requires **byte-identical** prefix and often **routing** the same conversation to a replica that still holds the blocks.

```text
 stable prefix ──────────────────────► variable suffix
 [tools][system][static RAG doc]        [history][latest user]
 └──────── cacheable on repeat ────────┘
```

---

## Batching

| Mode | Behaviour |
| --- | --- |
| Static | Fixed batch until all finish — short requests wait |
| Dynamic | Batch when full or timeout — OK for embeddings |
| **Continuous** | Every decode step: drop finished, admit new if KV available |

```text
 static:     AAAAAAAA........  (idle slots waste GPU)
 continuous: AAAAAAAAEEEEEEEE  (slot E refilled immediately)
```

**Chunked prefill:** split long prompts into slices mixed with decode tokens under a **token budget** so one huge prefill does not freeze ITL for everyone.

**Prefill/decode disaggregation (scale):** separate GPU pools for compute-heavy prefill vs bandwidth-heavy decode; pay KV transfer cost — worth it at large scale, not on two GPUs.

---

## Quantisation

Decode speed ≈ proportional to **bytes read per token** from weights (and KV).

| Format | Typical use |
| --- | --- |
| BF16 / FP16 | Baseline serving |
| FP8 (W8A8) | Hopper/Blackwell datacenter; near-lossless with scales |
| INT8 | Weight-only or W8A8 (SmoothQuant for activations) |
| INT4 | Weight-only; group scales (128 weights) |
| GGUF (Q4_K_M, etc.) | llama.cpp local/CPU |

| Algorithm | Idea |
| --- | --- |
| RTN + group scales | Simple baseline |
| **GPTQ** | Layer-wise 4-bit; Hessian-aware error compensation; needs calibration set |
| **AWQ** | Scale salient channels (from activations) before 4-bit; fast, robust |
| **SmoothQuant** | Move activation outliers into weights for INT8 W8A8 |

**Rules of thumb:** bigger model at 4-bit often beats smaller at 16-bit for same VRAM; quant hurts **math, code, long context** before short chat — run **your** evals. FP8 KV is separate from weight quant — test long-context tasks.

---

## Attention kernels (brief)

Naive attention materialises `T×T` scores in HBM — brutal at 32k+ length. **FlashAttention** tiles Q/K/V in SRAM, uses **online softmax**, avoids full score matrix — same math, less IO. Serving stacks use FlashAttention / FlashInfer / paged variants for prefix-shared and decode paths.

Online softmax (one row, blocked keys — schematic):

```python
m, l, acc = -inf, 0.0, zeros(d)
for block in key_blocks:
    m_new = max(m, block_scores.max())
    p = exp(block_scores - m_new)
    acc = acc * exp(m - m_new) + p @ V_block
    l = l * exp(m - m_new) + p.sum(); m = m_new
return acc / l
```

---

## Speculative decoding

**Idea:** draft model (or n-gram / EAGLE head) proposes `γ` tokens; **target** verifies all positions in **one** forward pass; accept left-to-right with rule that preserves the target distribution (`min(1, p/q)` per token).

```text
 draft:  " the" " cat" " sat"
 verify: ok     ok     reject → resample from target
 output: " the" " cat" + corrected token
```

Expected tokens per target step grows with acceptance rate `α`; speedup shrinks at **high batch** (GPU already saturated). Best for predictable text (code, low temperature, RAG echo).

---

## Parallelism (when one GPU is not enough)

| Strategy | Split | When |
| --- | --- | --- |
| Data parallel | Replicas, same weights | Throughput scale-out |
| Tensor parallel (TP) | Layers/matrices across GPUs | Model too big; **NVLink inside node** |
| Pipeline parallel (PP) | Layer stages | Very deep models across nodes |
| Expert parallel (EP) | MoE experts | Large MoE; all-to-all cost |

```text
 TP (one node, NVLink):  each GPU holds slice of every layer; all-reduce per layer
 PP (across nodes):      stage0 layers → stage1 → ... ; pipeline bubbles at low batch
 EP (MoE):                token → router → all-to-all to expert GPUs → gather
```

Rule of thumb: **TP inside a node**, **PP across nodes**, **replicas (DP)** for throughput. TP over PCIe/Ethernet between nodes hurts latency.

**MoE inference:** memory ≈ **total** parameters; compute ≈ **active** experts per token. Small batch reads fewer expert weights; large batch may touch most experts — bandwidth rises.

---

## Memory sizing (sketch)

```text
 GPU budget ≈ weights + KV + activations + overhead (CUDA, graphs, fragmentation)

 vLLM: --gpu-memory-utilization 0.9 → remainder to KV pool after load
```

**8B BF16 on 48 GB:** ~16 GB weights → often ~100k+ tokens KV capacity — until concurrency fills it.

**70B on one 80 GB H100:** BF16 weights (~140 GB) do not fit; need TP, H200, or 4-bit (~35–40 GB weights) plus KV headroom.

Little's law: concurrent sequences ≈ arrival rate × average time in system — size KV for **peak** concurrent tokens, not average QPS alone.

---

## Hardware basics

| GPU (approx.) | Memory | Bandwidth | Notes |
| --- | --- | --- | --- |
| A100 | 40/80 GB | ~2 TB/s | No FP8 TC |
| L40S | 48 GB | ~0.9 TB/s | Cost/GB; PCIe |
| H100 SXM | 80 GB | ~3.35 TB/s | FP8; NVLink |
| H200 | 141 GB | ~4.8 TB/s | More KV room |
| B200 | ~180 GB | ~8 TB/s | FP4 era |
| MI300X | 192 GB | ~5.3 TB/s | Large memory |
| RTX 4090/5090 | 24/32 GB | ~1–1.8 TB/s | Local dev |

**Selection:** (1) Do weights + expected KV fit? (2) `bandwidth / bytes_per_decode_step` for target TPOT. (3) FP8/FP4 support for prefill. (4) Interconnect for TP.

Local rule: decode tok/s ceiling ≈ `bandwidth / model_bytes` (weight-only quant helps directly).

---

## Serving engines (conceptual)

| Engine | Role (late 2026 landscape) |
| --- | --- |
| **vLLM** | Default OSS GPU server: PagedAttention, continuous batching, prefix cache, quant formats, speculative decoding, OpenAI-compatible `/v1/chat/completions` |
| **SGLang** | Strong prefix-heavy / agent workloads; RadixAttention; structured output |
| **TensorRT-LLM** | Peak NVIDIA efficiency; often with Dynamo at scale |
| **TGI** | Hugging Face Text Generation Inference — **maintenance / migrate away** |
| **llama.cpp / Ollama** | GGUF, CPU/Metal/consumer GPU; laptops and edge |

```bash
# Illustrative vLLM serve shape — flags vary by version
vllm serve meta-llama/Llama-3.3-70B-Instruct \
  --tensor-parallel-size 2 --kv-cache-dtype fp8 --enable-prefix-caching
```

Hosted APIs reuse the same ideas (batching, prefix caching on stable prompt heads) and bill TTFT-heavy prefill vs decode differently.

**TGI vs vLLM:** Text Generation Inference was the Hugging Face default for years (Rust core, continuous batching). As of 2025–2026 the project is **maintenance-only**; new GPU deployments should standardise on **vLLM** (widest ecosystem) or **SGLang** (prefix/agent heavy). Concepts — paged KV, in-flight batching, OpenAI-compatible HTTP — are the same; migration is mostly ops and flag names.

---

## Operations snapshot

| Topic | Practice |
| --- | --- |
| Load balancing | Avoid blind round-robin for chat — **session affinity** or prefix-aware routing |
| Prompt cache (API) | Stable prefix first; no timestamps in system prompt; append history |
| Queueing | Plan ~60–75% of max throughput for SLO headroom |
| Streaming | Disable proxy buffering on SSE; cancel server work on client disconnect |
| Observability | TTFT, ITL p95, waiting requests, KV utilisation, prefix hit rate |

**Dashboard starters (Prometheus on vLLM/SGLang):** `num_requests_waiting`, time-to-first-token histogram, KV cache usage %, generation tokens/s, preemptions, prefix cache hits, speculative acceptance if enabled.

**Cost levers (API or self-host):** structure prompts for prefix hits; cap `max_tokens`; route easy tasks to smaller models; batch offline jobs; quantise only after eval pass.

---

## Pitfalls

1. **Sizing GPU for weights only** — KV for concurrency kills you on long chats.
2. **Single-request benchmarks** — idle server numbers mislead.
3. **Optimising mean latency** — users feel p95 TTFT and ITL spikes.
4. **TP across slow links** — prefer PP or more replicas.
5. **Assuming 4-bit is free** — eval reasoning and long context.
6. **Speculative decoding at full load** — can reduce throughput.
7. **Breaking prefix cache** — dynamic content at top of prompt.
8. **Ignoring prefill interference** — tune chunked prefill when streams freeze.
9. **TGI for new deployments** — use vLLM/SGLang.
10. **Self-host vs API** — compare at equal quality and **utilisation**, include ops.

---

## Quick check

**Why is decode memory-bound at batch 1?**  
Each step reads essentially the full weight tensor from HBM while doing only ~2 FLOPs per weight byte per sequence — arithmetic intensity ≈ 1, far below the GPU ridge (~300 FLOPs/byte). Prefill amortises one weight read over thousands of tokens, so compute limits TTFT instead.

**What does the KV cache store and how big is it?**  
For each layer, **keys and values** for every past token so the new query can attend without recomputing the prefix. Size per token is `2 × layers × kv_heads × head_dim × element_size`; at 70B-class GQA and 128k tokens, one sequence can be **tens of GB** — often more than weights on a single GPU.

**What problem does continuous batching solve?**  
Static batches hold GPU slots until the slowest sequence finishes, wasting capacity. Re-scheduling **every decode step** lets completed requests exit immediately and waiting requests enter when KV blocks are free, raising throughput at the same latency target when paired with paged KV.

**Compare AWQ and GPTQ for 4-bit serving.**  
Both target weight-only INT4 with group scales; **GPTQ** quantises column-wise using calibration activations and approximate second-order error feedback; **AWQ** identifies salient weight channels via activation magnitudes and scales them before quantisation. AWQ is often faster to run and robust; both need validation on your evals, especially code and math.

**When does speculative decoding help, and when not?**  
It increases tokens per target forward pass when draft acceptance is high (low temperature, repetitive or structured text). At **high batch**, the GPU is already memory-busy verifying rejected drafts adds waste — measure acceptance rate and goodput before leaving it on in production.
