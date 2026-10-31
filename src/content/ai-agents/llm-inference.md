---
title: "LLM inference & serving"
order: 9
---
# LLM Inference and Serving

This page is about what happens after a model is trained: how tokens are actually produced on
GPUs, why latency and cost behave the way they do, and how production systems (vLLM, SGLang,
TensorRT-LLM, hosted APIs) squeeze more tokens per second out of the same hardware. It assumes
the transformer basics from the LLM fundamentals page: causal attention, keys and values, GQA.

The single most useful idea on this page: **generating tokens is limited by how fast you can read
memory, not by how fast you can do math.** Almost every serving optimisation follows from that.

## Prefill vs decode

Serving a request has two phases with completely different performance profiles.

```text
 request: 2,000 prompt tokens, 400 output tokens

 PREFILL  one forward pass over all 2,000 prompt tokens in parallel
          ├─ computes and stores K and V for every prompt token (the KV cache)
          └─ produces the logits for output token 1
 DECODE   400 sequential forward passes, one new token each
          └─ each pass reads all weights + the whole KV cache, writes one K/V row

 time ──►
 │◄─ queue ─►│◄── prefill ──►│◄────────────── decode (400 steps) ──────────────►│
             │               ▲ first token (TTFT)                               ▲ done
```

| | Prefill | Decode |
| --- | --- | --- |
| Tokens per forward pass | Whole prompt (hundreds to 100k+) | 1 per sequence |
| Bottleneck | Compute (FLOPs) | Memory bandwidth |
| Determines | Time to first token | Time per output token |
| Scales with | Prompt length (attention part quadratically) | Output length × (weights + KV cache bytes) |
| Why API price differs | Cheap per token | Expensive per token (typically 3–8× input price) |

### Arithmetic intensity and the roofline

**Arithmetic intensity** is FLOPs performed per byte moved from memory. A GPU has a peak compute
rate and a peak memory bandwidth; their ratio is the **ridge point**. Below it you are
memory-bound, above it compute-bound.

```text
 NVIDIA H100 SXM (approximate datasheet figures)
   BF16 dense compute     ~989 TFLOP/s
   HBM3 bandwidth         ~3.35 TB/s
   ridge point            989e12 / 3.35e12  ≈ 295 FLOPs per byte
```

For a matrix-vector product with BF16 weights, each 2-byte weight is used for one multiply and one
add (2 FLOPs) **per token in the batch**:

```text
 intensity ≈ (2 FLOPs × tokens_in_pass) / 2 bytes  =  tokens_in_pass  FLOPs per byte

 decode, batch 1       ≈ 1 FLOP/byte      ──► ~300× below the ridge: memory-bound
 decode, batch 64      ≈ 64 FLOPs/byte    ──► still memory-bound
 prefill, 2,000 tokens ≈ 2,000 FLOPs/byte ──► compute-bound
```

A roofline sketch:

```text
 attainable
 TFLOP/s
   989 ┤                          ___________________________  compute roof
       │                       _/
       │                    _/
       │                 _/     memory roof = 3.35 TB/s × intensity
       │              _/
       │           _/
       │        _/
       │     _/
       └──┬──────┬─────────┬───────────────┬──────────────────►  FLOPs per byte
          1      64       295            2,000
       decode  decode    ridge          prefill
        B=1     B=64
```

### Worked example: decode speed of an 8B model

8B parameters in BF16 = 16 GB of weights on one H100.

```text
 batch 1:   every step reads 16 GB of weights
            16 GB / 3.35 TB/s ≈ 4.8 ms per step  ──►  ceiling ≈ 209 tokens/s
            (real kernels reach ~70-85% of peak bandwidth: ~150-175 tokens/s)

 batch 64, 2,000-token contexts each:
            weights read once per step, shared by all 64 sequences   16 GB
            KV cache: 64 × 2,000 tokens × 128 KiB/token              ~16.8 GB
            step time ≈ 32.8 GB / 3.35 TB/s ≈ 9.8 ms
            compute: 2 × 8e9 × 64 ≈ 1.0e12 FLOPs ≈ 1 ms at peak   (still memory-bound)
            aggregate ≈ 64 / 9.8 ms ≈ 6,500 tokens/s, ~100 tokens/s per user
```

Lessons:

- **Batching is nearly free throughput** for decode: the weights are read once and shared. This is
  why providers batch aggressively and why a single user's request is so expensive to serve alone.
- **The KV cache is not shared across sequences**, so its reads do not amortise. With long
  contexts, KV reads dominate and decode stays memory-bound however large the batch.
- Ways to go faster all reduce bytes per token: smaller models, quantised weights, quantised or
  compressed KV cache (GQA, MLA, FP8 KV), or producing several tokens per weight read (speculative
  decoding).

## Latency metrics

| Metric | Definition | Dominated by | Users feel it as |
| --- | --- | --- | --- |
| **TTFT** (time to first token) | Request sent → first output token received | Queueing + prefill (+ network) | "Is it doing anything?" |
| **TPOT** (time per output token) | `(E2E − TTFT) / (output_tokens − 1)`, averaged per request | Decode step time | Reading speed of the stream |
| **ITL** (inter-token latency) | Time between consecutive tokens; a distribution | Decode + interference from prefills | Stutter and pauses |
| **E2E latency** | Request sent → last token | TTFT + output_tokens × TPOT | Total wait for non-streaming calls |
| **Throughput** | Output (or total) tokens/s across all requests on the hardware | Batch size, efficiency | Cost per token |
| **Goodput** | Requests per second that **meet all SLOs** (for example TTFT p95 < 1 s and TPOT p95 < 50 ms) | Everything | The real capacity number |

Throughput and latency trade off: bigger batches raise throughput but slow every step. Goodput
(term popularised by the DistServe paper) captures the trade-off in one number: maximum load at
which your latency targets still hold.

Rough targets for interactive chat: TTFT under ~1 s, TPOT under ~50 ms (20+ tokens/s, faster
than people read). Agents and code completion often care more about E2E and TTFT respectively.
Reasoning models add a long thinking phase before visible output; measure time to first
**visible** token separately.

### Measuring from the client

```python
import time

from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="not-needed")


def measure(model: str, prompt: str, max_tokens: int = 400) -> dict:
    start = time.perf_counter()
    first = None
    arrivals = []
    usage = None
    stream = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=max_tokens,
        stream=True,
        stream_options={"include_usage": True},   # final chunk carries token counts
    )
    for chunk in stream:
        now = time.perf_counter()
        if chunk.usage is not None:
            usage = chunk.usage
        if chunk.choices and chunk.choices[0].delta.content:
            if first is None:
                first = now
            arrivals.append(now)
    end = time.perf_counter()

    out_tokens = usage.completion_tokens if usage else len(arrivals)
    gaps = [b - a for a, b in zip(arrivals, arrivals[1:])]
    return {
        "ttft_s": first - start if first else None,
        "e2e_s": end - start,
        "tpot_ms": 1000 * (end - first) / max(out_tokens - 1, 1) if first else None,
        "max_gap_ms": 1000 * max(gaps) if gaps else None,
        "output_tokens": out_tokens,
    }
```

One stream chunk can carry more than one token, so compute TPOT from the server's token count, not
the chunk count. Measure under **realistic concurrency and prompt-length distributions**; a single
request on an idle server tells you almost nothing. Report p50, p95 and p99, not averages.

For load tests, use the engine's benchmark tool (for example `vllm bench serve`) or NVIDIA
GenAI-Perf, which sweep request rate and report TTFT, ITL and throughput percentiles:

```bash
vllm bench serve \
  --model meta-llama/Llama-3.1-8B-Instruct \
  --dataset-name random --random-input-len 2000 --random-output-len 400 \
  --num-prompts 500 --request-rate 8
```

## KV cache in depth

### Why it exists

At decode step `t`, attention for the new token needs the keys and values of all previous tokens.
Because of the causal mask, those never change, so they are computed once (during prefill or when
the token was generated) and stored. Without the cache, each step would redo the whole prefix:
`O(T²)` work per token instead of `O(T)`.

### Size formula

```text
 KV bytes per token = 2 (K and V) × layers × kv_heads × head_dim × bytes_per_element
 KV bytes total     = per_token × tokens_in_sequence × concurrent_sequences
```

**Worked example: a Llama-3-70B-style model** (80 layers, 64 query heads, **8 KV heads** with GQA,
head_dim 128, BF16 cache):

```text
 per token = 2 × 80 × 8 × 128 × 2 bytes = 327,680 bytes = 320 KiB

 one 8K-token conversation      320 KiB × 8,192    =  2.5 GiB
 one 128K-token document        320 KiB × 131,072  = 40   GiB
 32 concurrent 8K conversations                    = 80   GiB   (a whole H100)

 same model without GQA (64 KV heads):  2.5 MiB per token  ->  320 GiB for 128K tokens
 with an FP8 KV cache:                  160 KiB per token  ->  half of all the above
```

For comparison, an 8B Llama-3-style model (32 layers, 8 KV heads, head_dim 128) needs 128 KiB per
token in BF16.

So for long contexts or many users, **the KV cache, not the weights, decides how many requests fit
on a GPU**, and therefore throughput and cost.

### PagedAttention

Early servers reserved a contiguous KV buffer of `max_seq_len` for every request. Most requests
end far sooner, so most of that memory was wasted: the vLLM paper (Kwon et al., 2023) measured
existing systems using only 20–40% of KV memory for actual tokens.

**PagedAttention** applies virtual memory ideas:

```text
 logical KV (per request)          block table            physical GPU blocks
 ┌────────────────────────┐      ┌─────────────┐        ┌─────┬─────┬─────┬─────┐
 │ tokens 0-15   block 0  │ ───► │ 0  ->  #7   │        │ #0  │ #1  │ #2  │ #3  │
 │ tokens 16-31  block 1  │ ───► │ 1  ->  #2   │        ├─────┼─────┼─────┼─────┤
 │ tokens 32-40  block 2  │ ───► │ 2  ->  #5   │        │ #4  │ #5  │ #6  │ #7  │
 └────────────────────────┘      └─────────────┘        └─────┴─────┴─────┴─────┘
```

- The cache is split into fixed-size **blocks** (vLLM's default is 16 tokens).
- Each request has a **block table** mapping its logical blocks to any free physical blocks; the
  attention kernel follows the table.
- Blocks are allocated on demand as tokens are generated, so waste is under one block per request.
- Blocks can be **shared** with reference counts: parallel samples (`n > 1`) and beams share the
  prompt's blocks, with copy-on-write when they diverge.
- When memory runs out, the scheduler **preempts** a request: frees its blocks and later recomputes
  them (or swaps to CPU memory).

The paper reported 2–4× higher throughput than prior systems at the same latency. Paged KV is now
standard in every serious engine.

### Prefix caching and RadixAttention

Many requests share a prefix: the same system prompt and tool definitions, the same document in a
RAG app, earlier turns of the same conversation, or the same few-shot examples. If the KV blocks
for that prefix are still in memory, a new request can **skip prefill** for it.

- **vLLM automatic prefix caching**: each full block is identified by a hash of its tokens plus the
  hash of the preceding block (so a hash identifies the entire prefix up to that point). On a new
  request, the scheduler looks up the block hashes from the start; every hit is reused. Freed
  blocks stay cached until evicted (LRU).
- **SGLang RadixAttention** (Zheng et al., 2023): keeps all cached prefixes in a **radix tree**
  keyed by token sequences, which matches the longest shared prefix at any token granularity and
  makes branching workloads (agents, tree search, many samples from one prompt) cheap.

```text
                      [system prompt + tools]          (shared by every request)
                       /                    \
          [doc A + history 1]           [doc B]
            /            \                  \
     [question 1]    [question 2]      [question 3]
```

Effect: TTFT for a hit drops from "prefill the whole prompt" to "prefill only the new suffix", and
GPU compute is freed for other work. Prefix caching only works if the prefix is **byte-identical**
up to the reuse point, and it is only as good as your **routing** (the request must land on the
replica that holds the cache; see Load balancing).

### KV cache quantization and offloading

- **FP8 KV cache** (for example `--kv-cache-dtype fp8` in vLLM): halves KV memory and bandwidth with
  small quality loss for most models. Lower-precision KV formats (FP4 variants such as NVFP4 on
  Blackwell) are arriving in engines as of 2026; validate quality on long-context evals.
- **Architectural compression**: GQA (8× vs MHA in the example), MLA (a compressed latent per
  token), sliding-window layers (cache only the last `w` tokens for those layers).
- **Offloading tiers**: GPU HBM → CPU DRAM → local NVMe → remote storage. Hot prefixes stay on GPU;
  colder ones move down and are reloaded instead of recomputed. Systems like LMCache, SGLang's
  hierarchical cache and NVIDIA Dynamo's KV block manager implement this. Reloading 1 GiB over
  PCIe 5 (~50 GB/s real) takes ~20 ms, often far cheaper than re-running prefill for a long
  prefix.

## Batching

GPUs are efficient only with many sequences in flight (previous section). How requests are
grouped matters enormously.

### Static, dynamic and continuous batching

- **Static batching**: collect a fixed batch, run it to completion. Short requests wait for the
  longest; new requests wait for the whole batch.
- **Dynamic batching**: form a batch when either it is full or a timeout passes. Fine for
  fixed-size workloads like embeddings or classification; still request-level for generation.
- **Continuous (in-flight, iteration-level) batching** (Orca, Yu et al., OSDI 2022): the scheduler
  decides the batch **every decode step**. Finished sequences leave immediately; waiting requests
  join as soon as there is KV memory.

```text
 static batching: 4 slots, the batch waits for its longest member
 slot 1  AAAAAAAA........        "." = idle slot, wasted GPU time
 slot 2  BBB.............
 slot 3  CCCCCCCCCCCCCCCC
 slot 4  DDDDD...........
         ───────────────► decode steps

 continuous batching: a slot is refilled the step after a sequence finishes
 slot 1  AAAAAAAAEEEEEEEE
 slot 2  BBBFFFFFFFGGGGGG
 slot 3  CCCCCCCCCCCCCCCC
 slot 4  DDDDDHHHHHHHHIII
```

Continuous batching combined with paged KV is the core of vLLM, SGLang, TensorRT-LLM and every
hosted API. Typical gains over static batching are several-fold in throughput at equal latency.

### Chunked prefill

A new request's prefill can be large (say 30k tokens). If the scheduler runs it as one step, every
ongoing decode stalls for that whole step: users see the stream freeze. **Chunked prefill**
(Sarathi-Serve, Agrawal et al., 2024) splits prefills into chunks and fills each step up to a
**token budget** with all pending decode tokens plus a slice of prefill:

```text
 step budget = 2,048 tokens
 step 1:  [ 60 decode tokens ][ prefill chunk 1 of request X: 1,988 tokens ]
 step 2:  [ 60 decode tokens ][ prefill chunk 2 of request X: 1,988 tokens ]
 ...
```

Decode tokens ride along "for free" in compute-bound prefill steps, so ITL stays smooth and GPU
utilisation goes up; the cost is a somewhat longer TTFT for the long request. In vLLM the budget is
`--max-num-batched-tokens`; smaller values favour ITL, larger favour TTFT and throughput.

### Prefill/decode disaggregation

Prefill and decode want different things: prefill wants compute and benefits from large tensor
parallelism; decode wants bandwidth, memory for KV and big batches. Running both on the same GPUs
means they interfere. **Disaggregated serving** (DistServe, Splitwise, Mooncake) runs them on
separate pools and transfers the KV cache between them:

```text
 ┌────────┐     ┌─────────────────┐   KV cache over   ┌─────────────────┐
 │ router │ ──► │ prefill workers │ ────────────────► │ decode workers  │ ──► stream
 └────────┘     │ compute-bound   │   NVLink / RDMA   │ bandwidth-bound │
                └─────────────────┘                   └─────────────────┘
```

- Each pool can be sized, parallelised and even placed on different GPU types independently.
- TTFT and ITL SLOs can be tuned separately; goodput improves at scale.
- Cost: moving the KV cache (gigabytes for long prompts) needs fast interconnect, and the system is
  much more complex. Worth it at large scale, especially for big MoE models and long prompts; not
  for a two-GPU deployment.

As of 2026, vLLM and SGLang support PD disaggregation, and NVIDIA Dynamo and llm-d orchestrate it
across nodes.

## Attention kernels: FlashAttention

Naive attention materialises the `T × T` score matrix in GPU HBM, reads it back for softmax, writes
the weights, reads them again for the multiplication with V. For `T = 32k`, one head's score
matrix in FP16 is 2 GiB. The work is memory-bound on HBM traffic, not on FLOPs.

**FlashAttention** (Dao et al., 2022) is an exact, IO-aware attention algorithm:

- **Tiling**: load blocks of Q, K and V into on-chip SRAM (shared memory, ~hundreds of KB per SM),
  compute scores for the block, and accumulate the output without ever writing the full score
  matrix to HBM.
- **Online softmax**: softmax needs the row max and sum over all keys. FlashAttention keeps a running
  max `m` and running sum `l` and rescales the partial output whenever a larger max appears.
- **Recomputation in backward**: during training, recompute scores from Q and K instead of storing
  them.

Result: memory `O(T)` instead of `O(T²)`, and 2–4× faster attention. FlashAttention-2 improved
parallelism and work partitioning; FlashAttention-3 targets Hopper (asynchronous Tensor Core and
TMA usage, FP8). For decode, **FlashDecoding** splits the long KV sequence across thread blocks so
one query token can still use the whole GPU. FlashInfer is a widely used kernel library providing
paged, prefix-shared and decode-optimised attention for serving engines.

The online softmax trick in a few lines:

```python
import numpy as np


def blocked_attention_row(scores: np.ndarray, values: np.ndarray, block: int = 4) -> np.ndarray:
    """softmax(scores) @ values, computed one block at a time without the full softmax."""
    m, l = -np.inf, 0.0
    acc = np.zeros(values.shape[1])
    for s in range(0, len(scores), block):
        sb, vb = scores[s:s + block], values[s:s + block]
        m_new = max(m, sb.max())
        rescale = np.exp(m - m_new)          # shrink earlier partial sums to the new max
        p = np.exp(sb - m_new)
        l = l * rescale + p.sum()
        acc = acc * rescale + p @ vb
        m = m_new
    return acc / l


rng = np.random.default_rng(0)
s, v = rng.normal(size=10), rng.normal(size=(10, 3))
w = np.exp(s - s.max()); w /= w.sum()
print(np.allclose(blocked_attention_row(s, v), w @ v))   # True
```

## Parallelism

When a model does not fit on one GPU, or one GPU is too slow, the work is split.

| Strategy | What is split | Communication | Use when |
| --- | --- | --- | --- |
| **Data parallel (replicas)** | Requests; each replica has the full model | None between replicas (just a load balancer) | Model fits on one GPU or one node; scale out throughput |
| **Tensor parallel (TP)** | Each weight matrix across GPUs | Two all-reduces per layer per step | Model does not fit on one GPU; needs NVLink; usually within one node (TP ≤ 8) |
| **Pipeline parallel (PP)** | Layers into stages on different GPUs | Activations passed stage to stage | Model does not fit in one node; slower interconnect between nodes |
| **Expert parallel (EP)** | MoE experts across GPUs | All-to-all to dispatch tokens to experts and gather results | Large MoE models |
| **Sequence / context parallel (CP)** | The sequence (tokens) across GPUs | Pass K/V blocks around a ring (ring attention) | Very long prompts, mainly to speed up prefill |

### Tensor parallelism in one picture

Megatron-style TP splits the first MLP matrix by columns and the second by rows, so each GPU
computes a slice independently and a single all-reduce sums the partial results:

```text
 X is replicated on both GPUs. A is split by columns, A = [A0 | A1];
 B is split by rows, B = [B0 ; B1]. f is the activation function.

 ┌────────────────────────────┐      ┌────────────────────────────┐
 │ GPU 0                      │      │ GPU 1                      │
 │ Y0 = f(X · A0)             │      │ Y1 = f(X · A1)             │
 │ Z0 = Y0 · B0               │      │ Z1 = Y1 · B1               │
 └──────────────┬─────────────┘      └──────────────┬─────────────┘
                └──────────► all-reduce ◄───────────┘
                                  │
                                  ▼
                     Z = Z0 + Z1 = f(X · A) · B
```

Because the activation is applied element-wise to column slices, no communication is needed
between the two matmuls: one all-reduce per MLP (and one per attention block) suffices.

Attention is split the same way by heads (each GPU owns some query heads and their KV heads, which
also splits the KV cache). The all-reduces happen every layer, every step, so TP over slow links
(PCIe or Ethernet between nodes) kills latency. Rule of thumb: **TP inside a node, PP across nodes,
DP for more throughput.** For a 70B model on 8 GPUs, two replicas of TP=4 usually give more total
throughput than one replica of TP=8, while TP=8 gives lower per-request latency.

PP at inference has no backward pass, but at small batch only one stage works at a time ("pipeline
bubbles"), so it helps capacity more than latency.

## Quantization in depth

Quantization stores weights (and sometimes activations and the KV cache) in fewer bits. Because
decode is memory-bound, fewer bytes per weight means proportionally faster decode and more room
for KV cache.

### Formats

| Format | Bits | Notes |
| --- | --- | --- |
| BF16 / FP16 | 16 | Baseline for serving. BF16 has FP32's exponent range |
| FP8 (E4M3 / E5M2) | 8 | Hardware support on Hopper, Ada, Blackwell, MI300. E4M3 is typical for inference. Often near-lossless with per-channel or per-block scales |
| INT8 | 8 | Integer with scales. W8A8 needs outlier handling (SmoothQuant) |
| INT4 | 4 | Usually weight-only with group-wise scales (for example one scale per 128 weights) |
| FP4 (NVFP4, MXFP4) | 4 | Blackwell-era hardware formats. MXFP4 (OCP spec): blocks of 32 values sharing a power-of-two scale. NVFP4: blocks of 16 with an FP8 scale plus a per-tensor scale. gpt-oss shipped its MoE weights in MXFP4 |
| GGUF k-quants | ~2–8 | llama.cpp's family of block formats, for CPU, Apple and consumer GPUs |

### Weight-only vs weight + activation

- **Weight-only** (W4A16, W8A16): weights are stored quantised and dequantised to 16-bit inside the
  matmul kernel (for example Marlin kernels). Speeds up **decode** (fewer bytes read) but not
  compute-bound prefill. The workhorse for single-GPU and edge serving.
- **Weight + activation** (W8A8 INT8 or FP8, W4A4/FP4): matmuls run on low-precision Tensor Cores,
  which also speeds up **prefill** and high-batch decode. Activations have large **outlier**
  channels that make naive INT8 activation quantisation lossy; FP8's wider dynamic range handles
  this better, which is why FP8 is the default production choice on Hopper.

### Algorithms

- **Round-to-nearest (RTN)** with per-group scales: simplest baseline; fine at 8-bit, noticeably
  worse at 4-bit.
- **GPTQ** (Frantar et al., 2022): quantises one layer at a time, column by column, and uses
  second-order information (an approximate Hessian from calibration activations) to adjust the
  remaining weights to compensate for each rounding error. Good 4-bit and 3-bit quality; needs a
  calibration set (a few hundred samples).
- **AWQ** (Lin et al., 2023): observes that a small fraction of weight channels (~1%) matter most,
  identified by **activation** magnitude. Instead of keeping them in high precision, it scales those
  channels up before quantisation (and the inputs down to compensate), protecting them. Simple,
  fast, robust 4-bit; widely supported.
- **SmoothQuant** (Xiao et al., 2022): for W8A8, migrates activation outliers into the weights with a
  per-channel scale `s_j = max|X_j|^α / max|W_j|^(1-α)`, dividing activations by `s` and multiplying
  weights by `s`. The product is mathematically unchanged but both become easy to quantise.
- **GGUF k-quants** (llama.cpp): super-blocks of 256 weights split into sub-blocks with their own
  quantised scales and minimums. Names encode bits and mix: `Q8_0` (~8.5 bits per weight),
  `Q6_K` (~6.6), `Q5_K_M` (~5.7), `Q4_K_M` (~4.8), `Q3_K_M` (~3.9), `Q2_K`. The `_M` / `_S` / `_L`
  variants keep some sensitive tensors at higher precision. An **importance matrix** (imatrix)
  computed from calibration text improves low-bit quality.

### Quality trade-offs

General patterns (always verify on your own evals):

| Precision | Typical quality vs BF16 | Typical use |
| --- | --- | --- |
| FP8 W8A8 | Near-lossless for most models | Default for datacenter serving on Hopper/Blackwell |
| INT8 weight-only | Near-lossless | Older GPUs |
| 4-bit weight-only (AWQ, GPTQ, Q4_K_M) | Small loss; more on math, code, long context, and on small models | Single-GPU and local serving |
| FP4 (NVFP4/MXFP4) | Good with quantisation-aware training or careful calibration | Blackwell serving, large MoE |
| 3-bit and below | Clear degradation | Fitting a huge model on limited hardware |

Rules of thumb:

- A bigger model at 4-bit usually beats a smaller model at 16-bit with the same memory.
- Quantisation hurts reasoning chains, rare languages and long contexts before it hurts short
  chat; generic benchmarks can hide this.
- Calibration data should resemble your traffic (code for code models, your languages).
- Quantise the KV cache separately and evaluate long-context tasks when you do.

## Speculative decoding

Decode reads all weights to produce one token. If we could check several tokens per weight read,
we would get more tokens for nearly the same cost, because verifying `k` tokens in one forward pass
costs about the same as generating one (the pass is memory-bound anyway).

### Draft and verify

1. A cheap **draft** proposes `γ` tokens (for example 4).
2. The **target** model runs one forward pass over all `γ` proposed positions in parallel.
3. Accept proposed tokens left to right using a rejection rule; at the first rejection, sample a
   correction token; if all are accepted, the target's pass also yields one bonus token.

```text
 draft proposes:     " the"   " cat"   " sat"   " down"
 target verifies:      ok       ok       no
 output this step:   " the"   " cat"   " lay"          (" lay" sampled by the target)
```

The acceptance rule (Leviathan et al., 2023; Chen et al., 2023) makes the output distribution
**exactly** the target model's distribution: accept draft token `x` with probability
`min(1, p(x) / q(x))`, where `p` is the target's probability and `q` the draft's; on rejection,
sample from the normalised residual `max(0, p − q)`. With greedy decoding it reduces to "accept
while the draft's token equals the target's argmax".

### Acceptance math

If each draft token is accepted independently with probability `α`, the expected number of tokens
produced per target forward pass with `γ` draft tokens is:

```text
 E[tokens per step] = (1 − α^(γ+1)) / (1 − α)

 α = 0.8, γ = 4:  (1 − 0.8^5) / 0.2 = (1 − 0.328) / 0.2 ≈ 3.36 tokens per target pass
 α = 0.6, γ = 4:  (1 − 0.6^5) / 0.4 = (1 − 0.078) / 0.4 ≈ 2.31
```

Including the draft's cost `c` (time of one draft step relative to one target step), the speedup is
roughly:

```text
 speedup ≈ (1 − α^(γ+1)) / ((1 − α) · (γ·c + 1))

 α = 0.8, γ = 4, c = 0.05:  0.672 / (0.2 · 1.2) ≈ 2.8×
```

Acceptance is high for predictable text (code, structured output, repeated context, low
temperature) and low for creative high-temperature text.

### Draft methods

| Method | Draft source | Notes |
| --- | --- | --- |
| Separate draft model | A small model of the same family and tokenizer | Simple; extra memory; must share the tokenizer |
| n-gram / prompt lookup | Copy spans from the prompt that match the recent context | Free; great for RAG, editing and summarisation where output repeats input |
| Medusa | Extra decoding heads on the target predict tokens t+2, t+3...; tree attention verifies several candidate paths | Needs head training; no separate model |
| EAGLE (1, 2, 3) | A small autoregressive head that drafts at the **feature** level using the target's hidden states | Among the highest acceptance rates; widely supported in vLLM, SGLang, TensorRT-LLM |
| MTP (multi-token prediction) | Prediction modules trained with the model (for example DeepSeek-V3 onward) | Built in; reused as the draft at inference |

Caveats: speculative decoding helps most at **low batch sizes**, where the GPU has idle compute. At
high load, the GPU is already busy and verification of rejected tokens wastes capacity, so gains
shrink or turn negative. Engines expose it through configuration (for example vLLM's
`--speculative-config`); check the current docs for supported methods.

## MoE inference considerations

Mixture-of-Experts models replace each MLP with many experts and a router that picks a few per
token (for example 8 of 256 plus a shared expert). A model with 671B total parameters might have
37B active per token (DeepSeek-V3); 2026-era open MoE models range from ~100B to several trillion
total parameters.

- **Memory follows total parameters; compute follows active parameters.** A 671B MoE in FP8 needs
  ~671 GB just for weights, for example 8× H200 (141 GB each), even though it computes like a ~37B
  dense model.
- **Decode bandwidth depends on batch.** At batch 1, only the chosen experts' weights are read, so
  decode is fast for the size. At large batches, different tokens pick different experts and
  nearly all experts are read every step, so bandwidth cost approaches that of the full model, now
  shared across many tokens.
- **Expert parallelism and all-to-all.** Experts spread across GPUs; each layer dispatches tokens to
  the GPUs holding their experts and gathers results. All-to-all latency and network bandwidth
  become first-order. Large deployments use wide EP across many nodes with data-parallel attention.
- **Load imbalance.** Popular experts become hot spots; engines replicate hot experts and rebalance
  (expert parallel load balancers).
- **Quantisation** of experts (FP8, FP4/MXFP4) is common because experts are most of the bytes.
- Small-batch, single-node serving of a large MoE is memory-hungry for its quality; MoE shines at
  high throughput on large clusters.

## Memory sizing and GPU selection

### What uses GPU memory

```text
 total ≈ weights + KV cache + activations/workspace + runtime overhead

 weights     = parameters × bytes per parameter      (MoE: total parameters)
 KV cache    = per-token KV × tokens across all concurrent sequences
 activations = temporary buffers for the largest batch step (prefill chunks, logits:
               batch × vocab × 4 bytes, e.g. 256 × 128k × 4 B ≈ 128 MB)
 overhead    = CUDA context, CUDA graphs, NCCL buffers, fragmentation: a few GB per GPU
```

Engines like vLLM pre-allocate a fraction of GPU memory (`--gpu-memory-utilization`, default 0.9),
load the weights, profile activation memory, and give **everything left** to the KV cache. The log
line reporting KV cache capacity in tokens is the most important number at startup.

### Worked examples

**A. 8B model, BF16, one L40S (48 GB):**

```text
 budget      48 GB × 0.9                      = 43.2 GB
 weights     8B × 2 bytes                     = 16 GB
 overhead + activations (estimate)            ≈  4 GB
 KV cache    43.2 − 16 − 4                    ≈ 23 GB
 capacity    23 GB / 128 KiB per token        ≈ 175,000 tokens
             ≈ 21 concurrent 8K-token conversations, or 1 at 128K with room to spare
```

**B. 70B model, FP8 weights, 2× H100 80 GB, TP=2:**

```text
 budget      160 GB × 0.9                     = 144 GB
 weights     70B × 1 byte                     =  70 GB   (35 GB per GPU)
 overhead + activations                       ≈   8 GB
 KV cache    144 − 70 − 8                     ≈  66 GB
 capacity    66 GB / 320 KiB (BF16 KV)        ≈ 200,000 tokens
             with FP8 KV (160 KiB/token)      ≈ 400,000 tokens
```

**C. 70B model, BF16, 1× H100 80 GB:** 140 GB of weights does not fit. Options: quantise (FP8
weights at 70 GB still leave no room for KV within a 72 GB budget; AWQ 4-bit at ~38 GB leaves
~30 GB, about 90,000 tokens), use 2–4 GPUs with TP, or a larger GPU (an H200's 141 GB fits FP8
weights with ~50 GB of KV, but not BF16 weights).

**D. Is it enough for the traffic?** Use Little's law: concurrent sequences ≈ arrival rate ×
time in system. At a peak of 3 requests/s with 12 s average end-to-end time, ~36 sequences are in
flight; at ~3,400 tokens each (prompt + output) that is ~122k tokens of KV, about 40 GB for the
70B model. Configuration B holds it in BF16 KV with headroom; at twice the peak it would need FP8
KV or another replica.

### GPU reference (approximate datasheet figures)

| GPU | Memory | Bandwidth | Notes |
| --- | --- | --- | --- |
| NVIDIA A100 | 40 / 80 GB HBM2e | ~1.6–2.0 TB/s | No FP8 Tensor Cores |
| NVIDIA L40S | 48 GB GDDR6 | ~0.86 TB/s | Cheap per GB; PCIe only; fine for ≤ 8–30B models |
| NVIDIA H100 SXM | 80 GB HBM3 | ~3.35 TB/s | FP8; NVLink; the 2023–2025 workhorse |
| NVIDIA H200 | 141 GB HBM3e | ~4.8 TB/s | Same compute as H100, much more memory and bandwidth |
| NVIDIA B200 | ~180 GB HBM3e | ~8 TB/s | FP4; NVLink 5 |
| AMD MI300X | 192 GB HBM3 | ~5.3 TB/s | Large memory per GPU; ROCm support in vLLM and SGLang |
| RTX 4090 / 5090 | 24 / 32 GB | ~1.0 / ~1.8 TB/s | Local and hobby; no NVLink |
| Apple M-series Max/Ultra | Up to 128–512 GB unified | ~0.4–0.8 TB/s | Huge memory, modest bandwidth; MLX and llama.cpp |

Selection heuristics:

- **Memory capacity** decides whether it fits and how much KV (concurrency, context) you get.
- **Memory bandwidth** decides decode speed. Compare `bandwidth / bytes per token` across options.
- **Compute and low-precision support** (FP8, FP4) decide prefill speed and high-batch throughput.
- **Interconnect** (NVLink vs PCIe) decides whether TP across GPUs is efficient.
- Price per hour and availability usually decide in the end; measure tokens per dollar on your
  workload rather than comparing peak specs.

## Serving engines

State as of late October 2026; these projects release every few weeks, so check release notes.

| Engine | Status (Oct 2026) | Strengths | Typical use |
| --- | --- | --- | --- |
| **vLLM** | Very active; v0.32 released mid-October 2026 | Broadest model and hardware support (NVIDIA, AMD, TPU, others), PagedAttention, prefix caching, many quantisation formats, speculative decoding, PD disaggregation, OpenAI-compatible server | Default choice for GPU serving |
| **SGLang** | Very active; v0.5.22 released mid-October 2026 | RadixAttention prefix reuse, fast structured output, strong large-MoE and PD-disaggregated deployments, cache-aware router | Agentic and prefix-heavy workloads, large MoE at scale |
| **TensorRT-LLM** | Active; frequent release candidates; PyTorch-based backend now the main path | Peak per-GPU performance on NVIDIA (especially Blackwell, FP4), in-flight batching | Maximum efficiency on NVIDIA; often paired with NVIDIA Dynamo for multi-node orchestration |
| **TGI** (Hugging Face) | **Maintenance mode** since December 2025, repository archived in 2026; Hugging Face recommends vLLM or SGLang | Was the HF ecosystem default | Migrate existing deployments |
| **llama.cpp** | Very active; GGUF format; daily builds and semantic-version releases | CPU, Apple Silicon (Metal), CUDA, Vulkan; partial GPU offload; k-quants; `llama-server` with an OpenAI-compatible API | Local, edge, consumer GPUs |
| **Ollama** | Active (0.40.x in October 2026) | One-command local models; wraps llama.cpp and, on Apple Silicon, MLX; simple model registry and API | Developer laptops, prototypes |

Orchestration layers sit above engines: **NVIDIA Dynamo** (disaggregation, KV-aware routing, KV
offload over vLLM, SGLang or TensorRT-LLM), **llm-d** (Kubernetes-native distributed inference on
vLLM), and the Kubernetes **Gateway API Inference Extension** for model-aware routing.

Starting points:

```bash
# vLLM: a 70B model across 4 GPUs, FP8 KV cache, prefix caching
vllm serve meta-llama/Llama-3.3-70B-Instruct \
  --tensor-parallel-size 4 \
  --max-model-len 32768 \
  --gpu-memory-utilization 0.90 \
  --kv-cache-dtype fp8 \
  --enable-prefix-caching

# SGLang
python -m sglang.launch_server --model-path Qwen/Qwen2.5-7B-Instruct --tp 1 --port 30000

# llama.cpp: GGUF model, all layers on GPU, 8K context
llama-server -m ./model-Q4_K_M.gguf -c 8192 -ngl 99 --port 8080

# Ollama
ollama run gemma4
```

Flags change between versions (for example, prefix caching is on by default in recent vLLM
releases). Treat these as shapes, not copy-paste recipes; check `--help` for your version.

## OpenAI-compatible APIs

Almost every engine and many hosted providers expose the OpenAI **Chat Completions** wire format
(`POST /v1/chat/completions`), so client code can switch between backends by changing `base_url`
and `model`. OpenAI's own newer **Responses API** (`/v1/responses`) is also implemented by some
engines; Chat Completions remains the lowest common denominator.

### Request and streaming response on the wire

```json
{
  "model": "meta-llama/Llama-3.3-70B-Instruct",
  "messages": [
    { "role": "system", "content": "You are terse." },
    { "role": "user", "content": "Explain the KV cache in one sentence." }
  ],
  "max_tokens": 200,
  "temperature": 0.2,
  "stream": true,
  "stream_options": { "include_usage": true }
}
```

The streamed response is **Server-Sent Events** (`Content-Type: text/event-stream`): each event is a
`data:` line followed by a blank line, ending with a sentinel. Every chunk also carries `id`,
`object: "chat.completion.chunk"`, `created` and `model`; they are omitted below for width.

```text
data: {"choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}

data: {"choices":[{"index":0,"delta":{"content":"It stores"}}]}

data: {"choices":[{"index":0,"delta":{"content":" past keys"}}]}

data: {"choices":[],"usage":{"prompt_tokens":28,"completion_tokens":21,"total_tokens":49}}

data: [DONE]
```

Anthropic's Messages API uses named SSE events instead (`message_start`, `content_block_delta`,
`message_stop`, and so on); SDKs hide the difference.

### Python client with streaming

```python
import httpx
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",   # vLLM, SGLang, llama-server, or a hosted provider
    api_key="not-needed-locally",
    timeout=httpx.Timeout(60.0, connect=5.0),
    max_retries=2,
)

stream = client.chat.completions.create(
    model="meta-llama/Llama-3.3-70B-Instruct",
    messages=[
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "Explain the KV cache in one sentence."},
    ],
    max_tokens=200,
    temperature=0.2,
    stream=True,
    stream_options={"include_usage": True},
)

parts = []
for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        parts.append(chunk.choices[0].delta.content)
        print(chunk.choices[0].delta.content, end="", flush=True)
    if chunk.usage:
        print(f"\n[{chunk.usage.prompt_tokens} in / {chunk.usage.completion_tokens} out]")
```

### TypeScript client with streaming

```ts
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://localhost:8000/v1",
  apiKey: "not-needed-locally",
  timeout: 60_000,
  maxRetries: 2,
});

const stream = await client.chat.completions.create({
  model: "meta-llama/Llama-3.3-70B-Instruct",
  messages: [{ role: "user", content: "Explain the KV cache in one sentence." }],
  max_tokens: 200,
  stream: true,
});

for await (const chunk of stream) {
  process.stdout.write(chunk.choices[0]?.delta?.content ?? "");
}
```

Compatibility is good for the basics but not perfect: tool-calling formats, structured-output
parameters, reasoning content fields and logprobs differ between engines and models. Test the
features you depend on against each backend.

## Prompt caching at providers

Hosted APIs implement the same prefix caching as the engines above and pass the savings on.

### How prefix matching works

- The provider hashes the prompt in fixed-size chunks from the **start**. A request hits the cache
  for the longest prefix that is byte-identical to a recently processed prompt, typically above a
  minimum length (1,024 tokens is a common minimum).
- Any change at token `i` invalidates the cache for everything after `i`. A timestamp in the first
  line of the system prompt means **zero** cache hits.
- Caches live on specific machines and expire after minutes of inactivity (longer retention options
  exist at some providers). Routing hints such as OpenAI's `prompt_cache_key` help land related
  requests on the same machine.

### Provider behaviour (October 2026, check current docs)

| Provider | How to enable | Typical economics |
| --- | --- | --- |
| OpenAI | Automatic for prompts ≥ 1,024 tokens; optional cache key and retention settings that vary by model generation | Cached input billed at a large discount; `usage.prompt_tokens_details.cached_tokens` reports hits |
| Anthropic | `cache_control` breakpoints on content blocks (up to 4), or automatic mode; 5-minute default TTL, 1-hour option | Cache writes cost more than base input (1.25× for 5 min, 2× for 1 h); reads around 0.1× or less; `cache_creation_input_tokens` / `cache_read_input_tokens` in usage |
| Google Gemini | Implicit caching on recent models; explicit `cachedContents` with a TTL on some models | Discounted cached tokens; explicit caches also bill storage per hour |
| DeepSeek, Qwen, Mistral, xAI | Mostly automatic | Discounts vary; see each provider's pricing page |

### Structuring prompts for cache hits

```text
 most stable ───────────────────────────────────────────────────────► most variable
 [tool definitions][system prompt][static docs / few-shot][history][latest user turn]
  └──────────────── cacheable prefix ─────────────────────┘
```

- Put stable content first and variable content last. Keep tool definitions in a fixed order and
  serialise JSON deterministically.
- Never interpolate per-request values (timestamps, user names, request ids) into the prefix; put
  them in the final user message.
- In multi-turn chats and agents, **append** to history instead of rewriting it; summarising or
  truncating old turns invalidates the cache from that point.
- Do not toggle tools or change settings that the provider includes in the cached prefix mid-
  conversation.
- Monitor the cached-token fields; a sudden drop to zero usually means someone changed the prefix.

Anthropic-style explicit caching in Python:

```python
import anthropic

client = anthropic.Anthropic()
MODEL = "claude-..."   # use a current model id from the provider docs

LONG_STABLE_INSTRUCTIONS = "..."   # policies, persona, output format
PRODUCT_MANUAL = "..."             # tens of thousands of tokens, identical on every call


def ask(question: str) -> str:
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=[
            {"type": "text", "text": LONG_STABLE_INSTRUCTIONS},
            {"type": "text", "text": PRODUCT_MANUAL, "cache_control": {"type": "ephemeral"}},
        ],
        messages=[{"role": "user", "content": question}],
    )
    u = resp.usage
    print("write:", u.cache_creation_input_tokens, "read:", u.cache_read_input_tokens,
          "uncached:", u.input_tokens)
    return resp.content[0].text
```

## Cost modelling

### Worked example: tokens per month to dollars

Assume a support assistant on a hosted API with illustrative prices of **$3 per million input
tokens, $15 per million output tokens, cached reads at $0.30 per million** and a 1.25× write premium.

```text
 traffic        2,000,000 requests / month
 per request    3,000 input tokens (2,000 of them a stable system prompt + tools)
                  400 output tokens

 input tokens   2M × 3,000 = 6.0B      output tokens   2M × 400 = 0.8B

 1) no caching
    input   6,000M × $3   = $18,000
    output    800M × $15  = $12,000
    total                 = $30,000 / month

 2) prefix caching, 95% hit rate on the 2,000-token prefix
    cached reads   4.0B × 0.95 = 3.8B × $0.30/M          =  $1,140
    uncached input 6.0B − 3.8B = 2.2B × $3/M             =  $6,600
    write premium  0.2B (cache misses) × $3/M × 0.25     =    $150
    output                                               = $12,000
    total                                                = $19,890 / month  (−34%)

 3) also cap answers at ~250 tokens with a concise format
    output   2M × 250 = 0.5B × $15/M                     =  $7,500
    total                                                = $15,390 / month  (−49%)
```

Output tokens are often the bigger lever, and reasoning models add hidden thinking tokens billed as
output, so measure actual usage per request rather than estimating. Other levers: route easy
requests to a small model (often 10–30× cheaper per token), use **batch APIs** for offline work
(commonly ~50% off with hours of latency), and trim retrieved context.

### Self-hosting vs API break-even

Self-hosting cost is mostly **fixed** (GPUs by the hour, whether busy or not, plus engineering
time); API cost is **variable** (per token).

```text
 break-even tokens/month = (GPU cost + ops cost) / blended API price per token

 example (assumptions, not quotes):
   8× H100 node at $2.50 per GPU-hour     8 × 2.50 × 730 h      = $14,600 / month
   share of an engineer to run it                               ≈ $10,000 / month
   fixed total                                                  ≈ $24,600 / month
   blended API price for a comparable model                     = $2 per 1M tokens
   break-even  =  $24,600 / $2 per 1M  ≈  12.3B tokens / month
```

Then check the node can actually serve that volume at your latency SLO. Suppose benchmarking shows
4 replicas of a 70B FP8 model (TP=2 each) sustain ~8,000 output tokens/s per node at acceptable
TPOT. At 100% utilisation that is ~21B output tokens/month; real traffic has peaks and troughs, so
average utilisation of 30–50% is typical, giving ~6–10B output tokens/month per node.

Conclusions that usually hold:

- **Low or spiky volume: use an API.** Idle GPUs are the most expensive way to serve tokens.
- **Hosted open models** from inference providers are often cheaper than self-hosting the same model
  unless you run at high, steady utilisation.
- **Self-host** when you have sustained high volume, strict data-residency or air-gap requirements,
  a fine-tuned model, or latency needs that require co-location.
- Compare at **equal quality** on your evals; the cheapest token is useless if a frontier model
  solves the task in one call and the open model needs three.

## Scaling, queueing and routing

### Queueing and headroom

LLM servers behave like queues: as utilisation approaches 100%, waiting time grows sharply (for an
M/M/1 queue, waiting time scales with `ρ / (1 − ρ)`). For latency SLOs, plan for peak traffic at
~60–75% of measured maximum throughput. Admission control (reject or queue with a timeout when
queue depth or KV usage is too high) is better than letting every request become slow.

Preemption is a warning sign: when KV memory runs out, engines evict running requests and
recompute them later, wasting work and spiking latency.

### Autoscaling

- **Scale on the right signals**: queue depth (`num_requests_waiting`), KV cache utilisation, TTFT
  p95, and tokens/s. CPU utilisation is meaningless; `nvidia-smi` "GPU utilisation" only says a
  kernel was running, not how busy the GPU was.
- **Cold starts are slow**: pulling a multi-GB container, loading 140 GB of weights (over a minute at
  2 GB/s), compiling CUDA graphs and warming up can take several minutes. Keep a minimum number of
  warm replicas, cache weights on local NVMe, use fast weight streaming loaders, and scale up
  ahead of predictable peaks.
- **Scale-to-zero** suits internal or bursty batch workloads, not interactive products.

### Rate limits

Providers limit requests per minute (RPM) and tokens per minute (TPM), sometimes separately for
input and output, per model and per organisation tier. Some providers count `max_tokens` against
TPM at admission, so setting it far above what you need wastes quota. On your own gateway, apply
per-tenant token-bucket limits on **tokens**, not just requests, since one 100k-token request costs
as much as hundreds of small ones.

### Load balancing and KV-aware routing

Round-robin across replicas wastes prefix caches: the second turn of a conversation lands on a
replica that never saw the first. Better policies:

| Policy | Idea | Trade-off |
| --- | --- | --- |
| Least outstanding tokens | Route to the replica with the fewest queued + running tokens | Better than least-requests, since requests vary 100× in size |
| Session affinity / consistent hashing | Hash a conversation or tenant id to a replica | Simple; uneven load for heavy sessions |
| Prefix-aware (KV-aware) routing | Router tracks which replicas hold which prefix blocks and scores replicas by expected cache hit minus load | Best TTFT and throughput for chat and agents; more complex |

Implementations include SGLang's cache-aware router, NVIDIA Dynamo's KV router, the llm-d
inference scheduler, and the Gateway API Inference Extension for Kubernetes.

## Reliability

### Timeouts

- **Connect timeout**: short (a few seconds).
- **Idle / read timeout between stream events**: catches stalled streams; for example 30–60 s, longer
  for reasoning models that think silently before the first visible token.
- **Overall deadline**: per use case; an autocomplete is worthless after 1 s, a report generator can
  take minutes.
- Propagate deadlines downstream and **cancel** generation when the client goes away; engines like
  vLLM abort a request when its connection closes, freeing KV memory.

### Retries and fallbacks

- Retry on 429, 5xx, connection errors and timeouts, with **exponential backoff and jitter**,
  honouring `Retry-After`. Do not retry 400-class validation errors.
- Retries of LLM calls are not free: a retry after a partial stream re-generates (and re-bills) the
  whole answer, and may produce a different answer. Decide whether to resume, restart or surface
  the partial output.
- **Fallbacks**: a second region, provider or model. Keep prompts portable and evaluate the fallback
  model; a silent fallback to a weaker model is a quality incident.
- **Circuit breakers** stop hammering a failing provider and fail over quickly.
- **Hedged requests** (send a second request if the first has not produced a token within, say, the
  p95 TTFT) cut tail latency but cost extra tokens; use sparingly.

```python
import random
import time

import openai
from openai import OpenAI

RETRYABLE = (
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
)

backends = [
    (OpenAI(base_url="https://primary.example/v1", api_key="...", max_retries=0), "primary-model"),
    (OpenAI(base_url="https://fallback.example/v1", api_key="...", max_retries=0), "fallback-model"),
]


def retry_after_seconds(err: Exception) -> float | None:
    response = getattr(err, "response", None)
    value = response.headers.get("retry-after") if response is not None else None
    try:
        return float(value) if value else None
    except ValueError:
        return None


def complete(messages: list[dict], attempts: int = 4, base: float = 0.5, cap: float = 8.0):
    last_error = None
    for client, model in backends:
        for attempt in range(attempts):
            try:
                return client.chat.completions.create(
                    model=model, messages=messages, max_tokens=512, timeout=60.0
                )
            except RETRYABLE as err:
                last_error = err
                delay = retry_after_seconds(err) or random.uniform(0, min(cap, base * 2**attempt))
                time.sleep(delay)
    raise RuntimeError("all backends failed") from last_error
```

### Streaming over SSE in production

- Disable response buffering in proxies (for nginx, `proxy_buffering off;` or the
  `X-Accel-Buffering: no` response header), otherwise users get the whole answer at once.
- Load balancers and proxies have idle timeouts (often 60 s by default); send periodic SSE comment
  lines (`: ping`) during long thinking phases, or raise the timeout.
- Flush per event; set `Cache-Control: no-cache`.
- Handle client disconnects: stop generation and record a partial-usage event for billing.
- For the browser, `EventSource` only supports GET; for POST-based chat APIs use `fetch` with a
  streamed response body reader, or WebSockets if you need bidirectional messages.

## Observability for LLM serving

### Server metrics

| Metric | Why |
| --- | --- |
| TTFT, ITL / TPOT, E2E latency (p50/p95/p99) | SLOs; ITL spikes reveal prefill interference |
| Requests running and waiting | Saturation; the main autoscaling signal |
| KV cache utilisation, preemptions | Memory pressure; preemptions mean wasted work |
| Prefix cache hit rate | Whether routing and prompt structure are working |
| Prompt and generation tokens/s | Throughput and cost |
| Speculative decoding acceptance rate | Whether speculation is paying off |
| GPU SM activity, HBM bandwidth, memory used, temperature, XID errors | Hardware health (DCGM exporter) |
| Errors by type: 4xx, 5xx, timeouts, aborted streams | Reliability |

vLLM and SGLang expose Prometheus metrics at `/metrics` (in vLLM, names like
`vllm:num_requests_waiting`, `vllm:time_to_first_token_seconds` and a KV cache usage gauge; names
change between versions, so check your version's output).

### Request-level telemetry

Log or trace, for every call: model and version, prompt template version, input / cached / output /
reasoning token counts, TTFT, E2E, finish reason (`stop`, `length`, `tool_calls`, content filter),
cost, tenant, and an outcome signal (user feedback, task success). The OpenTelemetry **GenAI
semantic conventions** define standard attributes such as `gen_ai.request.model`,
`gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens`. Be deliberate about logging prompt and
completion text: it is often personal data; redact, sample and set retention.

Watch `finish_reason == "length"` rates: rising truncation usually means `max_tokens` is too low or a
model change made answers longer.

## Edge and local inference

- **Decode speed rule of thumb**: tokens/s ≤ memory bandwidth / model bytes. An 8B model at
  `Q4_K_M` is about 4.9 GB; on a laptop with ~500 GB/s unified memory the ceiling is ~100 tokens/s,
  on a desktop CPU with dual-channel DDR5 (~80 GB/s) about 16 tokens/s. Prefill on CPUs is slow
  because it is compute-bound.
- **Tools**: llama.cpp (GGUF, CPU/Metal/CUDA/Vulkan, partial GPU offload with `-ngl`), Ollama (wraps
  llama.cpp, and MLX on Apple Silicon), MLX and `mlx-lm` for Apple Silicon, LM Studio (desktop UI),
  ONNX Runtime GenAI, WebLLM for in-browser WebGPU inference.
- **On-device platform models**: operating systems now ship small built-in models (for example
  Apple's on-device foundation model APIs and Gemini Nano on Android); capabilities and APIs vary
  by OS version.
- **Typical models**: 1–30B dense or small-active MoE models at 4–8-bit; they handle autocomplete,
  summarisation, classification, local RAG and privacy-sensitive tasks well, and lag frontier
  models on hard reasoning.
- **Trade-offs**: no per-token cost and full privacy, but limited context (KV memory), battery and
  thermal limits, and you own model updates. A hybrid of local first with cloud escalation is common.

## Common mistakes

- **Sizing only for weights.** Forgetting the KV cache, then discovering the server fits four
  concurrent users.
- **Benchmarking one request on an idle server.** Latency and throughput under realistic concurrency
  are completely different.
- **Optimising averages.** Users feel p95/p99 TTFT and ITL stalls.
- **Breaking the prompt cache** with timestamps or reordered tools at the top of the prompt.
- **Round-robin load balancing** for multi-turn chat, wasting prefix caches.
- **TP across nodes over slow links.** Use PP or more replicas instead.
- **Assuming quantisation is free.** Evaluate on your tasks, especially reasoning and long context.
- **Turning on speculative decoding at high load** without measuring; it can reduce throughput.
- **Autoscaling on CPU or nvidia-smi utilisation** instead of queue depth and KV usage.
- **Proxies buffering SSE**, or idle timeouts killing long reasoning streams.
- **Retrying non-idempotent tool-using calls blindly**, causing duplicate side effects.
- **Self-hosting at low utilisation** and comparing GPU list price to API price without ops cost.

## Interview questions

**1. Why is decode memory-bandwidth-bound while prefill is compute-bound?**
In decode, each step processes one token per sequence, so every weight byte read from HBM is used
for only about 2 FLOPs per sequence: arithmetic intensity is about the batch size, far below the
GPU's ridge point (~300 FLOPs/byte on an H100). Prefill processes thousands of tokens per weight
read, so it is limited by compute.

**2. Estimate the decode speed of an 8B BF16 model on one H100 at batch 1.**
16 GB of weights read per token at ~3.35 TB/s gives ~4.8 ms per token, a ceiling of ~200 tokens/s;
real kernels achieve perhaps 150–175.

**3. Compute the KV cache for a 70B GQA model at 128k context.**
`2 × 80 layers × 8 KV heads × 128 head_dim × 2 bytes = 320 KiB per token`; × 131,072 tokens =
40 GiB for one sequence. FP8 KV halves it; without GQA it would be 8× larger.

**4. What problem does PagedAttention solve?**
Contiguous per-request KV reservations waste most memory through over-allocation and
fragmentation. Paging into fixed-size blocks with a block table allocates on demand, wastes under
one block per request, and allows sharing prefix blocks with copy-on-write, so far more requests fit
in memory.

**5. Explain continuous batching and chunked prefill.**
Continuous batching re-forms the batch every decode step, so finished requests leave and new ones
join immediately instead of waiting for a whole batch. Chunked prefill splits long prompt
processing into chunks mixed with decode tokens under a per-step token budget, preventing long
prefills from stalling ongoing streams.

**6. When would you use prefill/decode disaggregation?**
At large scale with long prompts or big MoE models, where prefill interference hurts ITL and the two
phases benefit from different parallelism and hardware. It requires fast KV transfer between pools
and adds complexity, so it is not worthwhile for small deployments.

**7. Tensor vs pipeline vs expert parallelism?**
TP splits each matrix across GPUs with all-reduces every layer; it reduces latency but needs NVLink,
so keep it within a node. PP splits layers into stages across nodes with only activation transfers,
adding capacity but with pipeline bubbles. EP places MoE experts on different GPUs with all-to-all
token routing. Data parallel replicas scale throughput.

**8. Compare GPTQ, AWQ and SmoothQuant.**
GPTQ quantises weights layer by layer using approximate second-order information to compensate
rounding errors. AWQ protects the small set of salient weight channels, identified by activation
magnitude, by scaling them before weight-only quantisation. SmoothQuant moves activation outliers
into the weights with per-channel scales so both weights and activations can be quantised to INT8.

**9. How does speculative decoding preserve output quality, and when does it not help?**
The target verifies draft tokens in one forward pass and accepts each with probability
`min(1, p/q)`, resampling from the residual on rejection, which provably yields the target's
distribution. Gains depend on acceptance rate; at high batch sizes the GPU is no longer idle, so
rejected work costs real throughput.

**10. How do provider prompt caches work and how do you structure prompts for them?**
They reuse KV for the longest byte-identical prefix of a recent prompt above a minimum length,
expiring after minutes of inactivity. Put tools, system prompt and static documents first, variable
content last, never put timestamps or per-user data in the prefix, and append to history rather than
rewriting it.

**11. How would you load balance a fleet of vLLM replicas for a chat product?**
Use prefix-aware or session-affine routing so multi-turn requests hit the replica holding their KV
cache, combined with a load signal such as outstanding tokens; scale on queue depth and KV usage;
enforce per-tenant token rate limits; keep warm capacity because cold starts take minutes.

**12. A team wants to self-host to save money. How do you evaluate it?**
Estimate monthly token volume and peak concurrency; benchmark candidate open models for quality on
their evals and for goodput on target hardware; compute fixed monthly GPU and ops cost at realistic
utilisation; compare with API and hosted-open-model prices at equal quality. Self-hosting usually
wins only at high steady utilisation or when data control requires it.

**13. Users report the stream freezes for a second mid-answer. What do you check?**
ITL p99 and its correlation with long prefills (enable or tune chunked prefill), preemptions due to
KV pressure, proxy buffering or network issues, and garbage collection or CPU contention in the API
server process.

**14. How do you size GPUs for a 70B model serving 30 concurrent 8k-token chats?**
Weights: 70 GB at FP8. KV: 30 × 8,192 × 320 KiB ≈ 75 GiB at BF16 (about half with FP8 KV). Add
~10 GB of overhead: roughly 155 GB, so 2× H100 80 GB with FP8 KV, or 2× H200, with TP=2. Then
benchmark to confirm TPOT meets the SLO.

**15. What metrics do you put on an LLM serving dashboard?**
TTFT, ITL and E2E percentiles; running and waiting requests; KV cache utilisation and preemptions;
prefix cache hit rate; tokens/s in and out; speculative acceptance rate; error and timeout rates;
GPU health from DCGM; cost per request and per tenant.

## Summary

- Prefill is compute-bound and sets TTFT; decode is memory-bandwidth-bound and sets TPOT.
  Arithmetic intensity of decode ≈ batch size, far below a GPU's ridge point.
- Decode ceiling ≈ bandwidth / bytes read per step (weights + KV cache).
- KV per token = `2 × layers × kv_heads × head_dim × bytes`; for a 70B GQA model, 320 KiB. KV
  capacity decides concurrency.
- PagedAttention eliminates KV waste; prefix caching / RadixAttention skip repeated prefill; FP8 KV
  and offloading stretch capacity.
- Continuous batching is mandatory; chunked prefill smooths ITL; PD disaggregation helps at scale.
- FlashAttention: tiled, exact, IO-aware attention with online softmax.
- Parallelism: TP within a node, PP across nodes, EP for MoE, CP for long prompts, DP for
  throughput.
- Quantisation: FP8 near-lossless; 4-bit weight-only (AWQ, GPTQ, GGUF) for single-GPU and local;
  evaluate on your tasks.
- Speculative decoding gives 2–3× at low batch with exact outputs.
- MoE: memory for total parameters, compute for active ones; all-to-all and load balance matter.
- vLLM and SGLang are the default open engines; TensorRT-LLM for peak NVIDIA performance; TGI is
  in maintenance mode; llama.cpp and Ollama for local.
- Cost: structure prompts for caching, cap output, route by difficulty, batch offline work;
  self-host only at high steady utilisation or for data control.
- Operate on queue depth, KV usage and latency percentiles; route by prefix; stream over SSE
  with buffering disabled; retry with backoff and fall back deliberately.

## Further reading

- Kwon et al., "Efficient Memory Management for Large Language Model Serving with PagedAttention"
  (SOSP 2023). https://arxiv.org/abs/2309.06180
- Dao et al., "FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness" (2022).
  https://arxiv.org/abs/2205.14135
- Dao, "FlashAttention-2: Faster Attention with Better Parallelism and Work Partitioning" (2023).
  https://arxiv.org/abs/2307.08691
- Leviathan, Kalman and Matias, "Fast Inference from Transformers via Speculative Decoding" (2023).
  https://arxiv.org/abs/2211.17192
- Chen et al., "Accelerating Large Language Model Decoding with Speculative Sampling" (DeepMind,
  2023). https://arxiv.org/abs/2302.01318
- Yu et al., "Orca: A Distributed Serving System for Transformer-Based Generative Models"
  (OSDI 2022).
- Zheng et al., "SGLang: Efficient Execution of Structured Language Model Programs" (2023).
  https://arxiv.org/abs/2312.07104
- Agrawal et al., "Taming Throughput-Latency Tradeoff in LLM Inference with Sarathi-Serve" (2024).
- Zhong et al., "DistServe: Disaggregating Prefill and Decoding for Goodput-optimized Large Language
  Model Serving" (OSDI 2024).
- Frantar et al., "GPTQ" (2022); Lin et al., "AWQ" (2023); Xiao et al., "SmoothQuant" (2022).
- Shoeybi et al., "Megatron-LM: Training Multi-Billion Parameter Language Models Using Model
  Parallelism" (2019).
- vLLM documentation: https://docs.vllm.ai
- SGLang documentation: https://docs.sglang.ai
