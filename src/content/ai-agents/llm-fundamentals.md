---
title: "LLM fundamentals"
order: 1
---
# LLM Fundamentals

A large language model (LLM) is a neural network that, given a sequence of tokens, outputs a
probability distribution over what the **next token** should be. Chat, coding assistants, tool use
and agents are all built on that one operation, run in a loop. This page covers how that works from
the bottom up: tokens, embeddings, the transformer, how models are trained, how text is sampled
from them, and what that means for the systems you build on top.

The target reader is a software engineer: enough math to reason about cost, latency and failure
modes, and enough intuition to explain it in an interview without hand-waving.

## What a language model is

### Language modelling as probability

A language model assigns a probability to a sequence of tokens. By the chain rule of probability,
any joint distribution over a sequence can be factored left to right:

```text
P(x1, x2, ..., xT) = P(x1) · P(x2 | x1) · P(x3 | x1, x2) · ... · P(xT | x1..xT-1)
```

An **autoregressive** language model learns exactly those conditional factors: a function
`f(x1..xt) -> P(x_{t+1} | x1..xt)` that returns one probability per vocabulary entry. Nothing in
the factorisation is an approximation; the approximation is only in how well the network learns
each conditional.

For example, after the prefix `"The capital of France is"`, a well-trained model might produce:

| Next token | Probability |
| --- | --- |
| `" Paris"` | 0.92 |
| `" a"` | 0.02 |
| `" the"` | 0.015 |
| `" located"` | 0.01 |
| (all ~100k others) | 0.035 combined |

### Why "just predicting the next token" is so powerful

Predicting the next token well across trillions of tokens of human text forces the model to
internalise whatever makes text predictable: grammar, facts, the structure of code, arithmetic
patterns, the plot of a story, the likely next step of a proof. To predict the next line of a
Python function, it helps to "understand" what the function is for. The objective is simple; what
it takes to minimise it is not.

### Generation is a loop

```text
 prompt tokens
      │
      ▼
 ┌─────────────────────┐     logits over the vocabulary (one number per token)
 │  model forward pass │ ──► softmax ──► probabilities ──► pick one token
 └─────────────────────┘                                        │
      ▲                                                         │
      └──────────── append the chosen token to the input ◄──────┘
            stop on an end-of-sequence token, a stop string, or max_tokens
```

Consequences you will meet everywhere else in this guide:

- **Output is sequential.** Token 50 cannot be produced before token 49. Prompt (input) tokens can
  be processed in parallel; output tokens cannot. That is why output is slower and pricier.
- **The model is stateless.** Each call sees only the tokens you send. "Memory" in a chat app is
  the application re-sending history (see the Context window section).
- **Every token is a sampling decision.** Randomness, formatting slips and hallucinations all
  enter through this loop.

## Tokenization

Models do not see characters or words. Text is converted into a sequence of integer **token ids**
from a fixed vocabulary, typically 32k–256k entries for modern models.

```text
"Unbelievable results!"  ──►  ["Un", "bel", "ievable", " results", "!"]
                         ──►  [2265, 9880, 45794, 4376, 0]
```

(That is the split from OpenAI's `o200k_base` encoding; other tokenizers split differently.
Note that the leading space belongs to `" results"`.)

### Why sub-words and not characters or words

| Unit | Vocabulary | Sequence length | Problem |
| --- | --- | --- | --- |
| Characters / bytes | Tiny (256 bytes) | Very long | Attention cost grows with length squared; model must learn spelling from scratch |
| Words | Huge, open-ended | Short | Unknown words, typos, morphology ("run", "running", "runner" unrelated) |
| Sub-words | 32k–256k | Medium | A good compromise: frequent words are one token, rare words split into pieces |

Sub-word tokenizers are learned from a corpus: frequent strings get their own token; rare strings
fall back to smaller pieces, down to individual bytes. Nothing is ever "out of vocabulary".

### Byte-Pair Encoding (BPE), step by step

BPE (Sennrich et al., 2016, adapted from a 1994 compression algorithm) builds a vocabulary by
repeatedly merging the most frequent adjacent pair of symbols.

**Training:**

1. Pre-tokenize the corpus into words (usually split on whitespace and punctuation, with the
   leading space attached to the word, e.g. `" results"`).
2. Start with a vocabulary of base symbols (characters, or the 256 bytes for byte-level BPE).
3. Count every adjacent pair of symbols across all words, weighted by word frequency.
4. Merge the most frequent pair into a new symbol, add it to the vocabulary, record the merge.
5. Repeat steps 3–4 until the vocabulary reaches the target size.

**Worked example.** Corpus with word frequencies (the classic Hugging Face course example):

```text
hug ×10   pug ×5   pun ×12   bun ×4   hugs ×5
base symbols: b g h n p s u
```

Iteration 1: split each word into characters and count pairs.

| Pair | Where it occurs | Count |
| --- | --- | --- |
| `u g` | hug(10), pug(5), hugs(5) | **20** |
| `p u` | pug(5), pun(12) | 17 |
| `u n` | pun(12), bun(4) | 16 |
| `h u` | hug(10), hugs(5) | 15 |
| `g s` | hugs(5) | 5 |
| `b u` | bun(4) | 4 |

Merge `u g -> ug`. Words become `h ug`, `p ug`, `p u n`, `b u n`, `h ug s`.

Iteration 2: recount. `u n` = 16, `h ug` = 15, `p u` = 12, `p ug` = 5, `ug s` = 5, `b u` = 4.
Merge `u n -> un`. Words become `h ug`, `p ug`, `p un`, `b un`, `h ug s`.

Iteration 3: `h ug` = 15, `p un` = 12, `p ug` = 5, `ug s` = 5, `b un` = 4. Merge `h ug -> hug`.

```text
vocabulary after 3 merges: b g h n p s u ug un hug
merge list (ordered):      1. u g -> ug   2. u n -> un   3. h ug -> hug
```

**Encoding new text** applies the learned merges in the same order:

```text
"bugs"  ->  b u g s  ->(merge 1)  b ug s  ->(no u n, no h ug)  ["b", "ug", "s"]
"hugs"  ->  h u g s  ->(merge 1)  h ug s  ->(merge 3)          ["hug", "s"]
"mug"   ->  m u g    ->  "m" is not a base symbol  ->  [UNK] with a character-level base
```

The `"mug"` case is why production tokenizers use **byte-level** BPE (below).

A runnable version of training and encoding:

```python
from collections import Counter


def pair_counts(words: dict[tuple[str, ...], int]) -> Counter:
    pairs: Counter = Counter()
    for symbols, freq in words.items():
        for a, b in zip(symbols, symbols[1:]):
            pairs[(a, b)] += freq
    return pairs


def apply_merge(symbols: tuple[str, ...], pair: tuple[str, str]) -> tuple[str, ...]:
    a, b = pair
    out, i = [], 0
    while i < len(symbols):
        if i < len(symbols) - 1 and symbols[i] == a and symbols[i + 1] == b:
            out.append(a + b)
            i += 2
        else:
            out.append(symbols[i])
            i += 1
    return tuple(out)


def train_bpe(corpus: dict[str, int], num_merges: int) -> list[tuple[str, str]]:
    words = {tuple(w): f for w, f in corpus.items()}
    merges = []
    for _ in range(num_merges):
        pairs = pair_counts(words)
        if not pairs:
            break
        best = max(pairs, key=pairs.get)
        merges.append(best)
        words = {apply_merge(s, best): f for s, f in words.items()}
    return merges


def encode(word: str, merges: list[tuple[str, str]]) -> list[str]:
    symbols = tuple(word)
    for pair in merges:
        symbols = apply_merge(symbols, pair)
    return list(symbols)


merges = train_bpe({"hug": 10, "pug": 5, "pun": 12, "bun": 4, "hugs": 5}, num_merges=3)
print(merges)                  # [('u', 'g'), ('u', 'n'), ('h', 'ug')]
print(encode("bugs", merges))  # ['b', 'ug', 's']
print(encode("hugs", merges))  # ['hug', 's']
```

Real implementations (tiktoken, Hugging Face `tokenizers`) use priority queues and merge ranks
instead of replaying every merge, but the result is the same.

### Byte-level BPE

GPT-2 introduced BPE over **UTF-8 bytes** instead of Unicode characters. The base vocabulary is the
256 possible byte values, so any string, in any language, including emoji and binary garbage, can
be encoded with no `[UNK]` token. Merges then build up common byte sequences. GPT-2/3/4,
the Llama 3 family and most modern models use byte-level BPE (Llama 3 uses a tiktoken-based
tokenizer with a ~128k vocabulary).

A side effect: a single non-ASCII character can be several bytes, and if those bytes are not a
learned merge, it costs several tokens. Streaming decoders must buffer partial UTF-8 sequences
before displaying them.

### WordPiece, Unigram and SentencePiece

| Algorithm | How it builds the vocabulary | Used by |
| --- | --- | --- |
| BPE | Greedy merges of the most frequent pair | GPT family, Llama 3, Mistral, Qwen, DeepSeek |
| WordPiece | Merges the pair that most increases corpus likelihood: score = `count(ab) / (count(a)·count(b))`. Continuation pieces are marked with `##` | BERT, DistilBERT |
| Unigram LM | Starts with a large candidate vocabulary and **prunes** pieces whose removal least hurts a unigram language-model likelihood. Encoding picks the most probable segmentation (Viterbi) and can sample alternatives | T5, ALBERT, many multilingual models |

**SentencePiece** is a library (Kudo and Richardson, 2018), not an algorithm. It implements BPE and
Unigram, treats the input as a raw stream (no language-specific pre-tokenizer) and encodes spaces
as a visible symbol `▁`, which makes it work for languages without spaces such as Japanese and
Chinese. Llama 1 and 2 used SentencePiece BPE with a 32k vocabulary.

### Why tokens matter in practice

**Cost and limits.** Prices, rate limits and context windows are all in tokens. Rules of thumb for
English with modern tokenizers: 1 token ≈ 4 characters ≈ 0.75 words; 1,000 tokens ≈ 750 words.

**Arithmetic and character tasks.** Numbers are split into chunks the model did not choose:
`"1234567"` may become `["123", "456", "7"]` with one tokenizer and single digits with another.
The model never sees the digits aligned by place value, so carrying in long addition is harder
than it looks. Counting letters ("how many r's in strawberry") is hard because `"strawberry"` may be
two or three tokens; the model never saw individual letters. Some tokenizers (Llama 3, for
example) split numbers into groups of at most three digits; some split every digit, which helps
arithmetic.

**Multilingual fairness.** A tokenizer trained mostly on English text gives English the most
merges. The same sentence in Hindi, Thai or Amharic can take 2–10× more tokens, which means
higher cost, more latency and less effective context for those users. Larger vocabularies (for
example the ~200k `o200k_base` encoding) were partly motivated by better non-English compression.

**Code.** Indentation, brackets and identifiers matter. Good code tokenizers have tokens for runs of
spaces and common keywords; poor ones spend a token per space. `camelCase` and `snake_case`
identifiers fragment differently.

**Glitch tokens and boundaries.** Tokens that were in the vocabulary but rare in training data
(the famous `" SolidGoldMagikarp"` in GPT-2/3) can produce bizarre behaviour. Token boundaries
also explain why a prompt ending in a trailing space can degrade output: the model usually emits
the space as part of the next token (`" Paris"`), so a prompt ending in `" "` forces an unusual
split.

### Counting tokens with tiktoken

```python
import tiktoken

enc = tiktoken.get_encoding("o200k_base")   # encoding used by GPT-4o-era OpenAI models

text = "Unbelievable results! 1234567 + 89 = ?"
ids = enc.encode(text)
print(len(ids), ids)
print([enc.decode_single_token_bytes(t) for t in ids])   # see the actual pieces
assert enc.decode(ids) == text                            # lossless round trip

for sample in ["hello world", "नमस्ते दुनिया", "def add(a, b):\n    return a + b"]:
    print(repr(sample), len(sample), "chars ->", len(enc.encode(sample)), "tokens")
```

Use the tokenizer of the model you actually call. For other providers, use their token-counting
endpoint or the Hugging Face tokenizer shipped with an open model; counts differ by 10–30%
between tokenizers. Remember that chat formatting and tool definitions also cost tokens.

## Embeddings and positional information

### Token embeddings

The first layer is a lookup table `E` of shape `(V, d_model)`. Token id `i` becomes row `E[i]`, a
vector of `d_model` numbers (for example 4,096 or 8,192). These vectors are learned. Tokens used in
similar contexts end up with similar vectors, which is the old word2vec intuition: meaning as
position in a vector space.

These input embeddings are not the same thing as the "embedding models" used for retrieval (see
the RAG pages). Retrieval embeddings are a single vector for a whole text, produced by a model
trained specifically for similarity.

### Why position must be added

Self-attention (below) is **permutation-invariant**: if you shuffle the input tokens, each output is
the same weighted sum, just shuffled. "Dog bites man" and "man bites dog" would look identical. So
the model must be told where each token is.

| Scheme | Idea | Used by |
| --- | --- | --- |
| Sinusoidal absolute | Add fixed sin/cos waves of different frequencies to each embedding | Original Transformer (2017) |
| Learned absolute | Learn one vector per position 0..max_len, add it | GPT-2, BERT |
| Relative bias (ALiBi) | Add a penalty to attention scores proportional to distance | BLOOM, MPT |
| **RoPE** (rotary) | Rotate query and key vectors by an angle proportional to position | Llama, Qwen, Mistral, DeepSeek, most modern LLMs |

Learned absolute positions cannot extrapolate: there is simply no vector for position 2,049 if
training stopped at 2,048.

### RoPE intuition

Rotary Position Embedding (Su et al., 2021) splits each query and key vector into pairs of
dimensions and treats each pair as a 2-D point. At position `m`, pair `i` is rotated by angle
`m · θ_i`, where `θ_i = base^(-2i/d)` and `base` is typically 10,000 (larger for long-context
models).

```text
 pair (q0, q1) at position m:

   [q0']   [cos mθ   -sin mθ] [q0]
   [q1'] = [sin mθ    cos mθ] [q1]
```

The key property: the dot product of a query rotated by `mθ` and a key rotated by `nθ` depends only
on the **difference** `m - n`, because rotating both by the same extra angle leaves their dot
product unchanged. So attention scores become a function of relative distance, without adding
anything to the embeddings. Low-index pairs rotate fast (sensitive to nearby order); high-index
pairs rotate slowly (capture long-range position).

Context-length extension techniques (Position Interpolation, NTK-aware scaling, YaRN) work by
rescaling these angles so that a model trained on, say, 8k positions sees angles it recognises at
128k positions, followed by a short fine-tune on long documents.

## The transformer architecture

The transformer (Vaswani et al., 2017, "Attention Is All You Need") originally had an encoder and
a decoder for translation. Modern LLMs are **decoder-only**: a stack of identical blocks, each
using causal (masked) self-attention so a position can only see itself and earlier positions.

### Decoder-only architecture

```text
              token ids  [t1, t2, ..., tT]
                           │
                           ▼
              ┌─────────────────────────┐
              │ token embedding (V × d) │
              └────────────┬────────────┘
                           ▼   x : T × d  (the "residual stream")
   ┌──────────────────────────────────────────────────┐
   │  decoder block, repeated L times                 │
   │                                                  │
   │   h = RMSNorm(x)                                 │
   │   x = x + MultiHeadCausalAttention(h)   (RoPE)   │
   │   h = RMSNorm(x)                                 │
   │   x = x + MLP(h)                       (SwiGLU)  │
   └────────────────────────┬─────────────────────────┘
                            ▼
              ┌─────────────────────────┐
              │ final RMSNorm           │
              │ LM head: d × V matrix   │
              └────────────┬────────────┘
                           ▼
              logits : T × V   ──►  softmax over V at each position
```

Typical sizes: an 8B model has `L = 32`, `d = 4096`; a 70B model has `L = 80`, `d = 8192`.

Think of the **residual stream** `x` as a shared working memory of shape `T × d`. Each block reads
from it, computes something, and adds its result back. Attention moves information **between**
positions; the MLP transforms information **within** each position.

### Self-attention: queries, keys and values

For each position, three vectors are computed from its current representation `h` (shape `T × d`)
with learned matrices:

```text
Q = h · W_Q      "what am I looking for?"
K = h · W_K      "what do I contain / advertise?"
V = h · W_V      "what do I hand over if selected?"
```

Attention then computes, for every pair of positions, how well query `i` matches key `j`, turns
those scores into weights, and takes a weighted average of the values:

```text
Attention(Q, K, V) = softmax( (Q · Kᵀ) / sqrt(d_k)  +  M ) · V
```

where `M` is the causal mask (0 where `j ≤ i`, minus infinity where `j > i`).

**Softmax** turns a vector of arbitrary scores into positive weights that sum to 1:
`softmax(z)_j = exp(z_j) / Σ_k exp(z_k)`. Larger scores get exponentially more weight.

**Why divide by sqrt(d_k).** If the components of `q` and `k` are roughly independent with mean 0
and variance 1, their dot product is a sum of `d_k` such products and has variance `d_k`. With
`d_k = 128` the typical score magnitude is about `sqrt(128) ≈ 11`. Softmax of numbers that far
apart is almost one-hot, so gradients through it vanish and training stalls. Dividing by
`sqrt(d_k)` brings the variance back to about 1.

**Why the causal mask.** During training, the model predicts every position in parallel: position
`t` must predict token `t+1` using only tokens `1..t`. Without the mask, position `t` could simply
look at token `t+1` and copy it. Setting masked scores to minus infinity makes `exp(-inf) = 0`, so
future tokens get exactly zero weight. The same mask is what allows the KV cache at inference time:
earlier positions never change when later tokens are appended.

### A tiny numeric example

Three tokens, `d_k = 2`. Assume the projections produced:

```text
      Q            K            V
t1  [1, 0]       [1, 0]       [1, 0]
t2  [0, 1]       [0, 1]       [0, 1]
t3  [1, 1]       [1, 1]       [2, 2]
```

Step 1, raw scores `Q · Kᵀ` (row = query, column = key):

```text
          k1   k2   k3
   q1  [  1    0    1 ]
   q2  [  0    1    1 ]
   q3  [  1    1    2 ]
```

Step 2, scale by `1/sqrt(2) ≈ 0.707` and apply the causal mask:

```text
          k1      k2      k3
   q1  [ 0.707   -inf    -inf  ]
   q2  [ 0.000   0.707   -inf  ]
   q3  [ 0.707   0.707   1.414 ]
```

Step 3, softmax each row (`e^0.707 ≈ 2.028`, `e^1.414 ≈ 4.113`):

```text
   q1: [1, 0, 0]
   q2: [1, 2.028] / 3.028               = [0.330, 0.670, 0    ]
   q3: [2.028, 2.028, 4.113] / 8.169    = [0.248, 0.248, 0.503]
```

Step 4, weighted sum of values:

```text
   out1 = 1.0·[1,0]                                 = [1.000, 0.000]
   out2 = 0.330·[1,0] + 0.670·[0,1]                 = [0.330, 0.670]
   out3 = 0.248·[1,0] + 0.248·[0,1] + 0.503·[2,2]   = [1.255, 1.255]
```

Token 3's query matches its own key best, so it pulls half its output from `v3`. Token 1 can only
see itself. That is all attention is: content-based, differentiable lookup.

### Minimal NumPy implementation

```python
import numpy as np


def softmax(x: np.ndarray, axis: int = -1) -> np.ndarray:
    x = x - x.max(axis=axis, keepdims=True)   # numerical stability; does not change the result
    e = np.exp(x)
    return e / e.sum(axis=axis, keepdims=True)


def causal_attention(Q: np.ndarray, K: np.ndarray, V: np.ndarray):
    """Scaled dot-product attention with a causal mask. Q, K, V: (T, d_k)."""
    T, d_k = Q.shape
    scores = Q @ K.T / np.sqrt(d_k)                       # (T, T)
    future = np.triu(np.ones((T, T), dtype=bool), k=1)    # True above the diagonal
    scores = np.where(future, -np.inf, scores)
    weights = softmax(scores, axis=-1)                    # each row sums to 1
    return weights @ V, weights


Q = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
K = Q.copy()
V = np.array([[1.0, 0.0], [0.0, 1.0], [2.0, 2.0]])
out, w = causal_attention(Q, K, V)
print(np.round(w, 3))     # matches the worked example
print(np.round(out, 3))   # [[1. 0.] [0.33 0.67] [1.255 1.255]]
```

Subtracting the row maximum before `exp` avoids overflow. It is safe with the mask because the
diagonal is never masked, so every row has a finite maximum.

### Multi-head attention

One attention operation can only produce one weighting pattern per position. **Multi-head**
attention runs `H` attentions in parallel on smaller slices (`head_dim = d / H`, typically 128),
concatenates the results and mixes them with an output matrix `W_O`. Different heads learn
different jobs: one tracks the previous token, one matches brackets, one finds the subject of the
verb, one copies a name seen earlier (the "induction heads" behind in-context learning).

### MQA and GQA: sharing keys and values

At inference time, the keys and values of every past token are cached (the **KV cache**, covered in
depth on the inference page). With standard multi-head attention, each of the `H` heads has its own
K and V, so the cache is large. Two variants shrink it:

```text
 MHA (multi-head)        GQA (grouped-query)       MQA (multi-query)
 Q: 8 heads              Q: 8 heads                Q: 8 heads
 K/V: 8 heads            K/V: 2 heads              K/V: 1 head
 q1 q2 ... q8            q1 q2 q3 q4  q5 q6 q7 q8  q1 q2 ... q8
 │  │      │             └──┴──┼──┘   └──┴──┼──┘   └──┴──┬──┘
 k1 k2 ... k8                  kv1          kv2          kv1
 cache: 1x               cache: 1/4x               cache: 1/8x
```

- **MQA** (Shazeer, 2019): all query heads share one K/V head. Smallest cache, some quality loss.
- **GQA** (Ainslie et al., 2023): query heads are grouped; each group shares a K/V head. Llama 2
  70B and Llama 3 70B use 64 query heads with 8 KV heads, an 8× smaller cache with quality close to
  MHA. GQA is now the default for open models.
- **MLA** (multi-head latent attention, DeepSeek-V2 and later) goes further: it caches a compressed
  low-rank latent vector per token and reconstructs K and V from it.

A compact NumPy multi-head attention with GQA:

```python
def gqa_attention(x, Wq, Wk, Wv, Wo, n_heads: int, n_kv_heads: int):
    """x: (T, d). Wq: (d, d). Wk, Wv: (d, n_kv_heads * head_dim). Wo: (d, d)."""
    T, d = x.shape
    hd = d // n_heads
    q = (x @ Wq).reshape(T, n_heads, hd).transpose(1, 0, 2)       # (H, T, hd)
    k = (x @ Wk).reshape(T, n_kv_heads, hd).transpose(1, 0, 2)    # (KVH, T, hd)
    v = (x @ Wv).reshape(T, n_kv_heads, hd).transpose(1, 0, 2)
    group = n_heads // n_kv_heads
    k = np.repeat(k, group, axis=0)                               # share each KV head
    v = np.repeat(v, group, axis=0)
    scores = q @ k.transpose(0, 2, 1) / np.sqrt(hd)               # (H, T, T)
    future = np.triu(np.ones((T, T), dtype=bool), k=1)
    scores = np.where(future, -np.inf, scores)
    out = softmax(scores, axis=-1) @ v                            # (H, T, hd)
    return out.transpose(1, 0, 2).reshape(T, d) @ Wo


rng = np.random.default_rng(0)
T, d, H, KVH = 5, 64, 8, 2
x = rng.normal(size=(T, d))
Wq, Wo = rng.normal(size=(d, d)) / 8, rng.normal(size=(d, d)) / 8
Wk, Wv = rng.normal(size=(d, KVH * d // H)) / 8, rng.normal(size=(d, KVH * d // H)) / 8
print(gqa_attention(x, Wq, Wk, Wv, Wo, H, KVH).shape)   # (5, 64)
```

RoPE would be applied to `q` and `k` after the reshape; it is omitted here for clarity.

### The feed-forward network (MLP)

After attention, each position independently passes through a two-layer MLP that expands the
dimension (typically 3.5–4×) and projects back. Modern models use **SwiGLU**, a gated variant with
three matrices:

```text
MLP(h) = W_down · ( SiLU(h · W_gate)  ⊙  (h · W_up) )
```

The MLP holds about two thirds of a dense model's parameters. Interpretability work suggests much
factual knowledge is stored here, as key-value-like associations. In **Mixture-of-Experts** models
the single MLP is replaced by many expert MLPs and a router that sends each token to a few of them
(for example 8 of 256), so total parameters grow without per-token compute growing.

### Residual connections and normalization

**Residual connections** (`x = x + f(x)`) give gradients a direct path from the loss back to early
layers, which is what makes 80- or 120-layer networks trainable. They also mean each block learns
a small edit to the stream rather than a whole new representation.

**Normalization** keeps activations in a stable range.

- **LayerNorm**: subtract the mean, divide by the standard deviation, then scale and shift with
  learned parameters.
- **RMSNorm** (Zhang and Sennrich, 2019): skip the mean subtraction; divide by the root mean square
  and scale. Cheaper and empirically as good. Used by Llama, Qwen, Mistral and most others.
- **Pre-norm vs post-norm**: the original transformer normalised after the residual add
  (post-norm). Modern models normalise the input of each sub-layer (pre-norm, as in the diagram),
  which trains more stably at depth.

```python
def rms_norm(x: np.ndarray, weight: np.ndarray, eps: float = 1e-6) -> np.ndarray:
    rms = np.sqrt(np.mean(x * x, axis=-1, keepdims=True) + eps)
    return x / rms * weight
```

### The LM head and the loss

After the final norm, the LM head multiplies each position's `d`-vector by a `d × V` matrix to get
`V` logits. Many models **tie** this matrix to the input embedding table to save parameters.
During training, the loss at each position is cross-entropy against the actual next token:

```text
loss = -(1/T) · Σ_t  log P(x_{t+1} | x_1..x_t)        perplexity = exp(loss)
```

A perplexity of 8 means the model is, on average, as uncertain as choosing uniformly among 8
tokens. During generation only the last position's logits are needed.

### Where the compute goes

| Component | Cost per token | Notes |
| --- | --- | --- |
| Matmuls (QKV, output, MLP) | about `2 × N` FLOPs, N = parameters | Dominates for short contexts |
| Attention scores and weighted sum | about `4 × L × d × T` FLOPs | Grows linearly per token, quadratically over a whole sequence |
| KV cache read (inference) | `2 × L × n_kv × head_dim × T × bytes` | Dominates memory traffic for long contexts |

## Parameters and scaling laws

### Counting parameters

Per decoder block with standard MHA and a 4× MLP: attention has four `d × d` matrices (`4d²`), the
MLP has two `d × 4d` matrices (`8d²`). Total per layer ≈ `12d²`.

```text
N ≈ 12 · L · d²  +  V · d          (embeddings, and the LM head if untied)

GPT-3:  L = 96, d = 12288   ->  12 · 96 · 12288² ≈ 174B   (published: 175B)
```

GQA shrinks the K and V matrices and SwiGLU uses three smaller matrices, but the `12 L d²`
estimate stays within ~10–20%.

Memory rule: **parameters × bytes per parameter**. 70B in BF16 (2 bytes) = 140 GB of weights; in
4-bit about 35–40 GB. See the inference page for full sizing.

### Kaplan scaling laws (2020)

Kaplan et al. ("Scaling Laws for Neural Language Models", OpenAI) found that test loss falls as a
smooth **power law** in model size `N`, dataset size `D` and compute `C`, over many orders of
magnitude, as long as the other factors are not the bottleneck. Their fits suggested that, for a
fixed compute budget, you should grow model size much faster than data. That shaped the era of
GPT-3 (175B parameters trained on only ~300B tokens).

### Chinchilla: compute-optimal training (2022)

Hoffmann et al. ("Training Compute-Optimal Large Language Models", DeepMind) re-ran the experiment
with learning-rate schedules matched to each run's length and found that parameters and tokens
should scale **equally** with compute. The compute-optimal ratio is roughly:

```text
D ≈ 20 · N           (about 20 training tokens per parameter)
C ≈ 6 · N · D        (training FLOPs: 2 forward + 4 backward per parameter per token)

Chinchilla: N = 70B, D = 1.4T  ->  C ≈ 6 · 70e9 · 1.4e12 ≈ 5.9e23 FLOPs
```

Chinchilla (70B, 1.4T tokens) beat Gopher (280B, 300B tokens) with the same compute budget. The
lesson: most 2020–2021 models were badly under-trained.

### Beyond compute-optimal: over-training for inference

Chinchilla optimises **training** compute only. But a model is trained once and served billions of
times, and inference cost scales with `N`. So modern labs deliberately train small models far past
20 tokens per parameter: Llama 3 8B was trained on ~15T tokens, about 1,900 tokens per parameter.
Loss keeps improving, just more slowly. The economic optimum shifts toward smaller, longer-trained
models whenever serving volume is large.

Other things scaling laws do not tell you: data quality matters as much as quantity, high-quality
data is running out (synthetic data and multi-epoch training are active areas), and downstream
task performance does not always track loss smoothly.

## How models are trained

```text
 ┌───────────────┐   ┌───────────────┐   ┌───────────────┐   ┌───────────────┐
 │ pre-training  │ ► │ SFT           │ ► │ preference    │ ► │ reasoning RL  │
 │ next token on │   │ instruction / │   │ RLHF, DPO,    │   │ verifiable    │
 │ ~10-40T toks  │   │ chat examples │   │ RLAIF         │   │ rewards       │
 └───────────────┘   └───────────────┘   └───────────────┘   └───────────────┘
   base model          follows format      helpful, safe       thinks longer,
   (autocomplete)      and instructions    preferred style     checks its work
```

Stages 2–4 are together called **post-training**. The order and mix vary by lab; reasoning RL and
preference tuning are often interleaved.

### Pre-training

- **Data**: web crawls (Common Crawl and proprietary crawls), heavily filtered for quality,
  deduplicated (near-duplicates hurt and cause memorisation), with code, books, papers, math and
  multilingual text mixed in at chosen ratios. Increasingly also synthetic data generated by earlier
  models. Late in training, labs often "anneal" on the highest-quality data and then extend context
  length with long documents.
- **Objective**: next-token cross-entropy on every position. No labels needed: the text is its own
  supervision (self-supervised learning).
- **Scale**: thousands to tens of thousands of GPUs for weeks to months, using data, tensor,
  pipeline and expert parallelism.
- **Result**: a **base model**. It continues text. Ask it a question and it may continue with three
  more questions, because that is what follows a question on a forum page.

### Supervised fine-tuning (SFT)

Train on curated `(prompt, ideal response)` pairs formatted with the chat template: instructions,
multi-turn chats, tool calls, refusals. The loss is computed only on the assistant tokens (prompt
tokens are masked) so the model learns to produce responses, not to imitate users. Tens of
thousands to millions of examples; quality beats quantity (the LIMA paper got far with 1,000
carefully chosen examples).

### RLHF: reward model plus PPO

InstructGPT (Ouyang et al., 2022) made the recipe famous:

1. **Collect comparisons.** For a prompt, sample several responses; humans rank them.
2. **Train a reward model** `r(x, y)` (usually the SFT model with a scalar head) so that preferred
   responses score higher. The Bradley–Terry loss for a pair where `y_w` beat `y_l`:

   ```text
   loss_RM = -log σ( r(x, y_w) - r(x, y_l) )
   ```

3. **Optimise the policy with PPO** to maximise reward while staying close to the SFT model:

   ```text
   maximise  E[ r(x, y) ]  -  β · KL( π_θ(y|x)  ||  π_SFT(y|x) )
   ```

The KL penalty prevents **reward hacking**: without it, the policy drifts into outputs that exploit
quirks of the reward model (very long answers, flattering language) rather than being better.
InstructGPT's headline result: labellers preferred outputs of the 1.3B RLHF model over the 175B
GPT-3 base model.

Downsides: PPO needs four models in memory (policy, reference, reward, value), is finicky to tune,
and human labels are slow and expensive.

### DPO: preference tuning without RL

Direct Preference Optimization (Rafailov et al., 2023) shows that the KL-regularised RLHF objective
has a closed-form optimal policy, and rearranges it into a simple classification loss on preference
pairs. No reward model, no sampling during training:

```text
loss_DPO = -log σ( β · [ log π_θ(y_w|x)/π_ref(y_w|x)  -  log π_θ(y_l|x)/π_ref(y_l|x) ] )
```

Intuition: raise the probability of the preferred answer relative to the reference model, lower it
for the rejected one. DPO and its variants (IPO, KTO, ORPO, SimPO) are widely used for open models
because they are cheap and stable. Online RL still tends to win at the frontier because it learns
from the model's own fresh samples.

### RLAIF and Constitutional AI

Constitutional AI (Bai et al., 2022, Anthropic) replaces much of the human harmlessness labelling
with AI feedback guided by a written list of principles (a "constitution"):

1. **Supervised phase**: the model drafts a response, critiques it against a principle, revises it;
   fine-tune on the revisions.
2. **RL phase**: an AI model compares pairs of responses according to the principles; those AI
   preferences train the reward model (RL from AI Feedback, **RLAIF**).

The general idea of using strong models as judges or label generators is now standard across labs.

### Reasoning RL with verifiable rewards

From 2024 (OpenAI o1) and openly with DeepSeek-R1 (January 2025), labs train models with
reinforcement learning on problems whose answers can be **checked automatically**: math with known
answers, code with unit tests, puzzles, formal proofs. This is often called RLVR (RL with
verifiable rewards).

- The model generates a long chain of thought, then an answer. The reward is 1 if the answer
  verifies, 0 otherwise (plus small format rewards).
- No learned reward model is needed for these domains, so there is less reward hacking.
- DeepSeek used **GRPO** (Group Relative Policy Optimization): sample a group of, say, 16 answers
  per prompt; each answer's advantage is its reward minus the group mean, divided by the group
  standard deviation. This removes PPO's separate value network.
- Behaviours such as backtracking ("wait, that's wrong"), checking work and trying alternative
  approaches emerge from the reward alone. DeepSeek-R1-Zero showed this starting from a base model
  with no SFT.

### Emergent abilities and in-context learning

**In-context learning (ICL)**: GPT-3 ("Language Models are Few-Shot Learners", Brown et al., 2020)
showed that a large model can perform a new task from a few examples in the prompt, with no weight
updates:

```text
English: cheese -> French: fromage
English: house  -> French: maison
English: tree   -> French:            (model outputs " arbre")
```

Mechanistic work (Olsson et al., 2022) links this to **induction heads**: attention circuits that
find an earlier occurrence of the current token and copy what followed it.

**Emergent abilities** (Wei et al., 2022) are capabilities that appear to jump from near-zero to
strong as models scale, such as multi-step arithmetic. A counterpoint ("Are Emergent Abilities of
Large Language Models a Mirage?", Schaeffer et al., 2023) showed many such jumps disappear when you
use continuous metrics (per-token log-likelihood) instead of all-or-nothing exact match. A fair
summary: underlying competence improves smoothly; whether a task looks "emergent" depends partly
on how you score it.

## Decoding and sampling

The model outputs logits `z` (one per vocabulary entry). A **decoding strategy** turns them into a
chosen token. This is where most user-facing parameters live.

### Greedy and beam search

- **Greedy**: always pick `argmax(z)`. Deterministic in principle, but tends toward repetitive,
  bland text and loops, and a locally best token can lead to a globally worse sequence.
- **Beam search**: keep the `b` highest-probability partial sequences at each step, expand each,
  keep the best `b`. Good for translation and short-form tasks with one right answer. Poor for
  open-ended generation: high-likelihood text is generic and repetitive (Holtzman et al., "The
  Curious Case of Neural Text Degeneration", 2019). Chat APIs generally do not offer it.

### Temperature

Divide logits by a temperature `T` before softmax:

```text
p_i = exp(z_i / T) / Σ_j exp(z_j / T)
```

Worked example with logits `[2, 1, 0]`:

| Temperature | Scaled logits | Probabilities |
| --- | --- | --- |
| 0.5 | [4, 2, 0] | [0.867, 0.117, 0.016] |
| 1.0 | [2, 1, 0] | [0.665, 0.245, 0.090] |
| 2.0 | [1, 0.5, 0] | [0.506, 0.307, 0.186] |

`T → 0` approaches greedy; `T = 1` is the model's own distribution; `T > 1` flattens it toward
uniform and increases the chance of picking garbage from the long tail.

### Truncation: top-k, top-p, min-p

The long tail of 100k unlikely tokens collectively holds real probability mass. Sampling from it
occasionally produces nonsense, so samplers cut it off.

| Method | Rule | Weakness it addresses / has |
| --- | --- | --- |
| **top-k** | Keep the `k` most likely tokens | Fixed `k` is too many when the model is confident and too few when it is not |
| **top-p** (nucleus) | Keep the smallest set whose probabilities sum to at least `p` | Adapts to confidence; at high temperature the nucleus can still include junk |
| **min-p** | Keep tokens with `p_i ≥ min_p × p_max` | Scales the cutoff by the top token's confidence; robust at higher temperatures (Nguyen et al., 2024) |

Common guidance: tune temperature **or** top-p, not both; keep top-p around 0.9–1.0.

### Repetition, frequency and presence penalties

- **Repetition penalty** (from the CTRL paper, common in open-source servers): for tokens already
  generated, divide positive logits by `r` (for example 1.1) and multiply negative logits by `r`.
- **Frequency penalty** (OpenAI style): `z_i -= α_freq × count_i`, which grows with each repeat.
- **Presence penalty**: `z_i -= α_pres × [count_i > 0]`, a flat penalty for having appeared at all,
  which nudges toward new topics.

Over-penalising breaks code and structured output, where repeating tokens like `"` or `}` is
required.

### A sampler in Python

```python
import numpy as np

rng = np.random.default_rng(0)


def sample_next(
    logits,
    *,
    temperature: float = 1.0,
    top_k: int = 0,
    top_p: float = 1.0,
    min_p: float = 0.0,
    prev_tokens=(),
    repetition_penalty: float = 1.0,
    frequency_penalty: float = 0.0,
    presence_penalty: float = 0.0,
) -> int:
    z = np.asarray(logits, dtype=np.float64).copy()

    if len(prev_tokens):
        ids, counts = np.unique(np.asarray(prev_tokens), return_counts=True)
        if repetition_penalty != 1.0:
            seen = z[ids]
            z[ids] = np.where(seen > 0, seen / repetition_penalty, seen * repetition_penalty)
        z[ids] -= frequency_penalty * counts + presence_penalty

    if temperature == 0.0:
        return int(np.argmax(z))                      # greedy
    z = z / temperature

    p = np.exp(z - z.max())
    p /= p.sum()

    if top_k > 0:
        kth_largest = np.sort(p)[-top_k]
        p[p < kth_largest] = 0.0
    if min_p > 0.0:
        p[p < min_p * p.max()] = 0.0
    if top_p < 1.0:
        order = np.argsort(p)[::-1]
        cumulative = np.cumsum(p[order])
        keep = np.searchsorted(cumulative, top_p * cumulative[-1]) + 1
        p[order[keep:]] = 0.0

    p /= p.sum()
    return int(rng.choice(len(p), p=p))


logits = np.array([2.0, 1.0, 0.0, -1.0, -3.0])
print([sample_next(logits, temperature=0.7, top_p=0.9) for _ in range(10)])
print(sample_next(logits, temperature=0.0))   # 0
```

Real servers do this on the GPU for a whole batch at once, and the order of operations (penalties,
temperature, top-k, top-p) differs slightly between libraries, so the same parameters can behave
differently across vLLM, llama.cpp and hosted APIs.

### Why temperature 0 is not fully deterministic

Even greedy decoding can vary between runs: floating-point addition is not associative, and the
order of reductions in GPU kernels depends on batch size and which other requests share the batch.
A near-tie between two tokens can flip, and the outputs diverge from there. Some providers offer a
`seed` parameter for best-effort reproducibility; none guarantee bit-identical results at scale.

### Logprobs

Many APIs return the log-probability of each chosen token and the top alternatives
(`logprobs=True, top_logprobs=5` in the OpenAI Chat Completions API). Uses:

- **Confidence for classification**: ask for a one-token label and read `exp(logprob)` for each
  class instead of trusting a self-reported confidence.
- **Detecting uncertain spans**: low-probability tokens in a factual answer are candidates for
  verification.
- **Evaluation**: compute the likelihood of a reference answer.

Logprobs from RLHF-tuned models are less well calibrated than from base models; treat them as a
ranking signal, not a true probability.

### Structured and constrained decoding

To guarantee output matches a JSON Schema, a regex or a grammar, the server **masks** logits at
each step so only tokens that keep the output valid can be chosen:

```text
schema: {"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]}

generated so far: {"ok":
allowed next tokens: " true", " false", "true", "false"   (all others get -inf)
```

Libraries such as Outlines, XGrammar and llguidance compile the schema into a finite-state machine
or pushdown automaton and precompute token masks, so the overhead per token is small. Hosted APIs
expose this as "structured outputs" or "strict" JSON schema mode; plain "JSON mode" only guarantees
syntactically valid JSON, not your schema. Caveats: constraints guarantee **form**, not
correctness; overly tight constraints can force the model down low-probability paths and reduce
quality, which is why letting the model reason first (in a separate field or before the
structured part) often helps.

## Context windows

The context window is the maximum number of tokens in one call: input **plus** output.

```text
system prompt + tools + history + retrieved docs + new message + output  ≤  window
```

As of late 2026, frontier models commonly advertise 200k to 1M+ token windows; small open models
are typically 32k–256k. Maximum **output** length is a separate, smaller limit (often 8k–128k).

### Why long context is expensive

- **Compute**: attention over `T` tokens costs `O(T²)` for the prompt and `O(T)` per generated
  token.
- **Memory**: the KV cache grows linearly with `T` (hundreds of KB per token for a 70B model; see
  the inference page). Long contexts limit how many requests a GPU can serve at once.
- **Price and latency**: you pay for every input token on every call, and time to first token grows
  with prompt length. Some providers charge a higher rate above a threshold (for example 200k).

### How models reach long contexts

| Technique | Idea |
| --- | --- |
| RoPE scaling (PI, NTK-aware, YaRN) | Rescale rotation angles, then fine-tune on long documents |
| Sliding-window attention | Each layer attends only to the last `w` tokens (Mistral 7B); stacking layers widens the effective view |
| Interleaved local/global layers | Most layers use a short window, a few use full attention (Gemma family and others) |
| Sparse attention | Attend only to selected blocks or top-scoring tokens (for example DeepSeek's sparse attention) |
| Compressed KV (MLA) | Cache a small latent per token instead of full K and V |
| Linear attention / state-space hybrids | Replace some attention layers with recurrent layers (Mamba-style) whose state does not grow with length |
| Ring / context parallelism | Split a long sequence across GPUs during training and prefill |

### Lost in the middle and effective context

Liu et al. (2023, "Lost in the Middle") showed that models retrieve information best when it is at
the **beginning or end** of a long prompt and worst in the middle, a U-shaped curve. Newer models
are much better, but two lessons hold:

- "Needle in a haystack" tests (find one planted fact) are easy; benchmarks like RULER that require
  multi-hop reasoning, aggregation or tracking many items show **effective** context is often much
  shorter than the advertised window.
- More context is not free accuracy. Irrelevant retrieved text distracts the model. Retrieve fewer,
  better chunks; put instructions and the question at the end, after long documents; summarise or
  compact history in agents.

The model has no memory between calls. Chat products and agents create the illusion of memory by
re-sending history, summarising old turns, or storing facts externally and retrieving them.

## Chat templates, special tokens and roles

### From messages to tokens

APIs accept a list of role-tagged messages:

```json
[
  { "role": "system", "content": "You are a concise assistant for Java developers." },
  { "role": "user", "content": "What does `final` mean on a method?" },
  { "role": "assistant", "content": "It cannot be overridden by subclasses." },
  { "role": "user", "content": "And on a class?" }
]
```

But the model only sees one token sequence. A **chat template** serialises the messages using
**special tokens** that were added to the vocabulary during post-training. For example, the ChatML
format (used by Qwen and others):

```text
<|im_start|>system
You are a concise assistant for Java developers.<|im_end|>
<|im_start|>user
What does `final` mean on a method?<|im_end|>
<|im_start|>assistant
It cannot be overridden by subclasses.<|im_end|>
<|im_start|>user
And on a class?<|im_end|>
<|im_start|>assistant
```

Llama 3 uses `<|begin_of_text|>`, `<|start_header_id|>role<|end_header_id|>` and `<|eot_id|>`. The
trailing `assistant` header is the **generation prompt**: it tells the model it is its turn.
Generation stops when the model emits the end-of-turn token.

With open models, always format through the tokenizer's own template:

```python
from transformers import AutoTokenizer

tok = AutoTokenizer.from_pretrained("Qwen/Qwen2.5-7B-Instruct")
messages = [
    {"role": "system", "content": "You are terse."},
    {"role": "user", "content": "Name a prime number."},
]
prompt = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
print(prompt)
```

A wrong or missing template is one of the most common reasons a self-hosted model "seems dumb":
it was trained on one format and is being shown another.

### Roles

| Role | Purpose | Notes |
| --- | --- | --- |
| `system` (or `developer`) | Standing instructions, persona, policies, output format | Models are trained to give it priority over user text; OpenAI's newer APIs call the app-level role `developer` |
| `user` | The human or calling program | Untrusted input in most apps |
| `assistant` | The model's previous turns, including its tool-call requests | You can pre-fill the start of an assistant turn with some providers to steer format |
| `tool` | Results of tool calls, linked to the call id | Untrusted: a web page returned by a tool can contain prompt injection |

Models trained with an **instruction hierarchy** learn to prefer system over user over tool content
when they conflict, but this is a learned tendency, not a security boundary.

### Special tokens and injection

If user text could produce the literal string `<|im_start|>system` and the tokenizer turned it into
the real special token, users could forge a system message. Tokenizers therefore treat special
tokens in ordinary text as plain characters by default; tiktoken's `encode` raises an error if text
contains a special token unless you explicitly allow it. When you build prompts by hand, never
concatenate untrusted text into a template string that is then parsed for special tokens.

## Reasoning models and test-time compute

### Thinking before answering

Chain-of-thought prompting (Wei et al., 2022) showed that asking a model to "think step by step"
improves multi-step reasoning: each generated token is extra computation the model can condition
on. **Reasoning models** (OpenAI's o-series and successors, DeepSeek-R1, Claude and Gemini thinking
modes, Qwen thinking variants) are trained with reasoning RL to do this natively: they produce a
long hidden or summarised "thinking" section before the final answer.

### Test-time compute as a new scaling axis

Accuracy on hard problems rises with the number of thinking tokens, roughly log-linearly, giving a
second scaling knob besides model size: spend more compute **per query**. Ways to spend it:

- **Longer single chains** (controlled by a reasoning effort or thinking budget parameter).
- **Parallel sampling with voting**: self-consistency (Wang et al., 2022) samples several chains and
  takes the majority answer.
- **Best-of-N with a verifier**: generate N candidates, score with a reward model or unit tests.
- **Search**: tree search over reasoning steps, guided by a process reward model.

### Practical implications

- Thinking tokens are billed as **output** tokens and add latency, often seconds to minutes.
- Use low or no reasoning for extraction, classification and simple chat; higher effort for
  planning, math, debugging and agentic coding.
- Do not add "think step by step" scaffolding to a reasoning model; it already does, and
  prescriptive step lists can hurt.
- Some APIs require you to pass back the (encrypted or summarised) reasoning items in multi-turn
  tool-use conversations so the model keeps its chain of thought; check the provider's docs.
- Shown reasoning is not guaranteed to be a faithful account of how the answer was produced.

## Multimodal models

Most frontier models accept images (and often audio, video and PDFs) as input.

- **Vision**: an image encoder (usually a Vision Transformer) cuts the image into patches (for
  example 14×14 or 16×16 pixels), encodes them, and a projector maps them into the LLM's embedding
  space. The LLM then sees "image tokens" interleaved with text tokens. Some models are trained
  natively multimodal from the start (early fusion) rather than bolting an encoder onto a text model.
- **Cost**: an image typically costs hundreds to a few thousand tokens depending on resolution;
  providers document the formula. Downscale images when fine detail is not needed.
- **Audio**: either a speech encoder feeding the LLM, or native speech-to-speech models for voice
  agents with low latency.
- **Output**: image generation is usually a separate diffusion or autoregressive image model,
  though some systems integrate it.
- Failure modes: counting objects, reading small text, precise spatial reasoning and charts remain
  weaker than text reasoning.

## Hallucination

A hallucination is fluent, confident output that is false or unsupported by the provided sources.

### Why it happens

- **The objective rewards plausibility.** Pre-training teaches "what text would come next", not
  "what is true". A plausible-sounding citation is a good continuation whether or not it exists.
- **Knowledge gaps.** Rare facts (seen once or twice in training) are stored unreliably; facts after
  the training cutoff are absent.
- **Training and evaluation reward guessing.** If benchmarks give credit for right answers and none
  for "I don't know", guessing scores better than abstaining. OpenAI's 2025 paper "Why Language
  Models Hallucinate" (Kalai et al.) argues this is a key driver.
- **Sampling.** Each token is a random draw; one unlucky low-probability token (a wrong digit in a
  year) and the rest of the answer confidently follows from it.
- **Sycophancy.** Preference tuning can teach models to agree with the user's premise.
- **Long or noisy context.** Relevant facts buried in the middle, or contradicted by distracting
  retrieved text.

### Mitigations

| Mitigation | How it helps |
| --- | --- |
| Retrieval-augmented generation | Supply the facts in context; instruct the model to answer only from them |
| Citations with verification | Require quoting sources; programmatically check that quotes exist in the sources |
| Allow abstention | Explicitly permit "I don't know" or "not in the documents" |
| Tools | Calculators, code execution, search and databases for facts and arithmetic |
| Lower temperature for factual tasks | Fewer long-tail draws |
| Structured output | Constrain fields; validate values against known enums or databases |
| Self-consistency / verification passes | Sample multiple answers or ask a second call to check claims |
| Evals | Measure hallucination rate on your own data; it varies by domain |

You cannot prompt hallucination to zero. Design the system so wrong answers are detectable and
cheap: show sources, validate outputs, keep a human in the loop for high-stakes actions.

## Model families and open vs closed weights

### Closed vs open weights

| | Closed (API-only) | Open weights |
| --- | --- | --- |
| Access | Hosted API, provider's cloud | Download weights; run anywhere; also hosted by many providers |
| Quality | Usually the frontier | Typically within months of the frontier; strongest in coding and math |
| Data control | Data leaves your boundary (subject to the provider's retention terms) | Can run fully on-prem or air-gapped |
| Customisation | Prompting, sometimes hosted fine-tuning | Full fine-tuning, LoRA, quantisation, custom decoding |
| Cost model | Per token | GPU hours, ops staff, utilisation risk |

"Open weights" is not the same as open source: training data and code are usually not released,
and licences vary from permissive (Apache 2.0, MIT) to custom licences with use restrictions or
attribution and revenue thresholds. Read the licence before shipping.

### The landscape as of October 2026

This changes monthly. The table lists families and current flagships as reported by vendor pages
and model trackers at the end of October 2026; verify before relying on any detail.

| Provider | Closed flagship line | Open-weight models |
| --- | --- | --- |
| OpenAI | GPT-6 line (for example GPT-6 Astra, GPT-6.1 Sol / Terra) | gpt-oss-120b and gpt-oss-20b (Apache 2.0, August 2025) |
| Anthropic | Claude Fable 5.1, Opus 5.5, Sonnet 5.5, Haiku 5.5 | None |
| Google | Gemini 4 (Argon, Carbon) and Gemini 3.x | Gemma 4 (Apache 2.0) |
| Meta | Muse Wonder / Muse Spark (closed) | Muse Spark Open (custom licence, MoE, 816B total / 42B active), Llama 4 Scout and Maverick |
| xAI | Grok 4.x | Older Grok releases only |
| DeepSeek | (API serves the open models) | DeepSeek V4 family (V4-Pro, V4.1-Flash), MIT licence, MoE |
| Alibaba | Qwen3.8-Max (hosted) | Qwen3.8 (27B dense under Apache 2.0; a very large MoE under a custom licence) |
| Mistral | Mistral Large 4 (preview) | Mistral Small 4, Mistral Large 3 (Apache 2.0) |
| Others | | Moonshot Kimi K3, Zhipu GLM-5.x, NVIDIA Nemotron 3.5, MiniMax |

Structural trends that are more durable than any name:

- **MoE dominates large open models**: hundreds of billions to trillions of total parameters with
  tens of billions active per token.
- **1M-token windows are common at the frontier**, with effective long-context quality varying.
- **Reasoning effort is a request parameter**, not a separate model, in most current families.
- **Tiered families** (large / medium / small) share a tokenizer and API so you can route between
  them.

## Choosing a model

A decision process that works:

1. **Define the task and the bar.** Write 50–200 representative test cases with expected outcomes
   before choosing anything (see the Evals page).
2. **Start with a strong model** to establish that the task is feasible at all.
3. **Step down** to cheaper or smaller models until quality drops below the bar.
4. **Consider constraints**: latency (TTFT and tokens per second), cost per task (not per token:
   reasoning models use more tokens), context length, modalities, tool-calling reliability,
   structured-output support, data residency, licence.
5. **Route**: a small model handles easy or high-volume requests (classification, extraction,
   autocomplete); hard requests escalate to a larger or reasoning model.
6. **Re-evaluate** when new models ship; pin model versions in production so behaviour does not
   change under you.

| Need | Typical choice |
| --- | --- |
| Agentic coding, complex planning | Frontier model with high reasoning effort |
| High-volume extraction, classification | Small fast model, possibly fine-tuned |
| Strict data control, on-prem | Open-weight model on your GPUs (see the inference page) |
| Very long documents | Model with strong long-context results on your own evals, plus retrieval |
| Real-time voice | Native speech model or a small fast text model with streaming |

## Common mistakes

- **Treating the model as a database.** It stores facts lossily; ground anything important.
- **Assuming the model remembers.** Every call is independent; state lives in your application.
- **Ignoring tokenization.** Underestimating cost for non-English or code; asking for exact character
  counts; prompts that end in a trailing space.
- **Wrong chat template for open models.** Silent quality collapse.
- **Over-stuffing context.** More retrieved text often lowers accuracy and always raises cost.
- **Tuning temperature and top-p together** without evals, or using high temperature for extraction.
- **Expecting determinism at temperature 0.** Design tests to tolerate variation.
- **Trusting JSON mode to enforce a schema.** Use strict structured outputs and still validate.
- **Using a reasoning model for everything.** Latency and cost explode for tasks that do not need it.
- **Comparing models on public benchmarks only.** Contamination and task mismatch; build your own
  eval set.

## Interview questions

**1. Explain what an LLM computes in one forward pass.**
Given token ids, it embeds them, passes them through `L` transformer blocks of causal self-attention
and MLP with residual connections, and outputs, at each position, logits over the vocabulary for
the next token. At inference only the last position's distribution is used to choose the next
token; generation repeats this loop.

**2. Why do we scale attention scores by sqrt(d_k)?**
Dot products of random `d_k`-dimensional vectors have variance proportional to `d_k`. Large scores
push softmax into saturation (nearly one-hot), where gradients vanish. Dividing by `sqrt(d_k)`
keeps score variance near 1.

**3. What is the causal mask for, and how does it relate to the KV cache?**
It prevents position `t` from attending to later positions, so training can predict all positions
in parallel without cheating. Because earlier positions never depend on later ones, their keys and
values never change as tokens are appended, so they can be cached and reused at each decode step.

**4. Walk through BPE training. Why byte-level?**
Start from base symbols, count adjacent pairs weighted by word frequency, merge the most frequent
pair, repeat until the target vocabulary size. Encoding replays merges in order. Byte-level BPE
uses the 256 bytes as the base, so every possible string is encodable and there is no unknown
token.

**5. Why are LLMs bad at counting letters and long arithmetic?**
They see tokens, not characters, and numbers are chunked inconsistently, so digits are not aligned
by place value. The model must have memorised the spelling of each token. Use tools (code
execution) for exact computation.

**6. What are MQA and GQA and why do they matter?**
They share key/value heads across multiple query heads (all of them in MQA, groups in GQA). This
shrinks the KV cache by the ratio of query heads to KV heads, raising the batch size and context
length a GPU can serve, with little quality loss for GQA.

**7. What did Chinchilla change, and why do labs now train past it?**
It showed compute-optimal training uses roughly 20 tokens per parameter, so earlier models were
under-trained. Labs now over-train smaller models (thousands of tokens per parameter) because
inference cost scales with parameter count and dominates total cost at scale.

**8. Compare RLHF with PPO and DPO.**
RLHF trains a reward model on human preference pairs, then optimises the policy with PPO to
maximise reward minus a KL penalty to the reference model. DPO derives a closed-form loss directly
on preference pairs, skipping the reward model and RL sampling; it is simpler and stable but
offline. Online RL generally scales better at the frontier.

**9. How are reasoning models trained and what is test-time compute?**
RL on tasks with automatically verifiable answers (math, code tests), rewarding correct final
answers, which teaches long chains of thought with self-checking. Test-time compute means improving
accuracy by spending more tokens per query (longer thinking, multiple samples with voting or
verification) rather than a bigger model.

**10. Explain temperature, top-p and min-p.**
Temperature divides logits before softmax: below 1 sharpens, above 1 flattens. Top-p keeps the
smallest set of tokens whose cumulative probability reaches `p`. Min-p keeps tokens with probability
at least `min_p` times the top token's probability, adapting the cutoff to the model's confidence.

**11. Why might temperature 0 still give different outputs?**
Floating-point non-associativity and batch-dependent kernel reduction orders change logits slightly;
near-ties flip and the sequences diverge. Some providers also route to different hardware or model
snapshots.

**12. How does constrained decoding guarantee valid JSON?**
The schema is compiled into an automaton; at each step, tokens that cannot lead to a valid
continuation are masked to minus infinity before sampling. It guarantees syntax and schema shape,
not semantic correctness.

**13. What is "lost in the middle" and how do you design around it?**
Retrieval accuracy is highest for information at the start or end of a long context and worse in
the middle. Retrieve fewer, more relevant chunks; put key instructions and the question after the
documents; summarise long histories; evaluate on your own long-context tasks.

**14. What causes hallucination and how do you reduce it in a product?**
The training objective rewards plausible text, knowledge is stored lossily, evaluation rewards
guessing, and sampling can commit to a wrong token. Reduce it with retrieval grounding, citations
that are verified, permission to abstain, tools for facts and math, constrained outputs, and
domain-specific evals.

**15. Open-weight vs closed model: how do you decide?**
Closed models usually lead on capability and need no ops. Open weights give data control,
customisation and potentially lower cost at high, steady utilisation, at the price of running GPU
infrastructure and checking licences. Decide on evals of your task plus total cost and compliance
requirements.

## Summary

- An LLM models `P(next token | previous tokens)`; generation is a sampling loop, sequential in
  output and stateless across calls.
- Tokenizers (byte-level BPE in most modern models) turn text into sub-word ids; tokens drive cost,
  context limits, arithmetic weakness and multilingual unfairness.
- Decoder-only transformer: embeddings, then `L` blocks of pre-norm causal multi-head attention and
  SwiGLU MLP with residual connections, then an LM head. RoPE encodes relative position.
- Attention = `softmax(QKᵀ/sqrt(d_k) + mask) V`; GQA/MQA/MLA shrink the KV cache.
- Parameters ≈ `12 L d²`; training FLOPs ≈ `6ND`; Chinchilla ≈ 20 tokens per parameter;
  production models are over-trained for cheaper inference.
- Training: pre-training, SFT, preference tuning (RLHF/PPO, DPO, RLAIF), reasoning RL with
  verifiable rewards.
- Decoding: temperature, top-k, top-p, min-p, penalties, logprobs; constrained decoding guarantees
  format.
- Long context is expensive and its effective use is shorter than advertised.
- Chat templates and special tokens turn roles into one token stream; use the model's own template.
- Hallucination is structural; mitigate with grounding, tools, validation and evals.
- Pick models by evals and total cost per task; route between sizes.

## Further reading

- Vaswani et al., "Attention Is All You Need" (2017). https://arxiv.org/abs/1706.03762
- Jay Alammar, "The Illustrated Transformer". https://jalammar.github.io/illustrated-transformer/
- Andrej Karpathy, "Let's build GPT: from scratch, in code, spelled out" (YouTube, 2023), and his
  "Let's build the GPT Tokenizer" video.
- Hoffmann et al., "Training Compute-Optimal Large Language Models" (Chinchilla, 2022).
  https://arxiv.org/abs/2203.15556
- Ouyang et al., "Training language models to follow instructions with human feedback"
  (InstructGPT, 2022). https://arxiv.org/abs/2203.02155
- Kaplan et al., "Scaling Laws for Neural Language Models" (2020). https://arxiv.org/abs/2001.08361
- Sennrich, Haddow and Birch, "Neural Machine Translation of Rare Words with Subword Units" (BPE,
  2016). https://arxiv.org/abs/1508.07909
- Su et al., "RoFormer: Enhanced Transformer with Rotary Position Embedding" (2021).
  https://arxiv.org/abs/2104.09864
- Rafailov et al., "Direct Preference Optimization" (2023). https://arxiv.org/abs/2305.18290
- Bai et al., "Constitutional AI: Harmlessness from AI Feedback" (2022).
  https://arxiv.org/abs/2212.08073
- DeepSeek-AI, "DeepSeek-R1: Incentivizing Reasoning Capability in LLMs via Reinforcement Learning"
  (2025). https://arxiv.org/abs/2501.12948
- Liu et al., "Lost in the Middle: How Language Models Use Long Contexts" (2023).
  https://arxiv.org/abs/2307.03172
- Hugging Face LLM Course, tokenizers chapter (BPE, WordPiece, Unigram).
