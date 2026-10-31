---
title: "Embeddings & RAG"
order: 3
---
# Embeddings and RAG

## Embeddings

An embedding model turns text (or images, audio) into a fixed-length vector — for example 768 or 1536 numbers — so that **similar meanings are close together** in vector space.

```text
"How do I reset my password?"     → [0.12, -0.48, 0.91, ...]   ┐ close
"Forgot login credentials, help"  → [0.10, -0.45, 0.88, ...]   ┘
"Best pizza in Bangalore"         → [-0.73, 0.22, 0.05, ...]     far
```

Similarity is usually **cosine similarity**:

```text
cos(a, b) = (a · b) / (|a| × |b|)      1 = same direction, 0 = unrelated
```

Uses: semantic search, RAG retrieval, clustering, deduplication, recommendations, classification.

## Vector databases

A vector database stores embeddings and finds the nearest neighbours of a query vector quickly.

- **Exact search** compares against every vector — O(n), fine up to ~100k vectors.
- **Approximate nearest neighbour (ANN)** indexes such as **HNSW** (graph-based) or **IVF** (clustered) trade a little recall for huge speed-ups.

Options: pgvector (PostgreSQL extension), Qdrant, Weaviate, Milvus, Pinecone, Chroma, Elasticsearch/OpenSearch k-NN, Redis.

If you already run PostgreSQL, **pgvector** is usually the simplest start:

```sql
CREATE EXTENSION vector;

CREATE TABLE chunks (
    id        BIGSERIAL PRIMARY KEY,
    doc_id    TEXT,
    content   TEXT,
    embedding VECTOR(1536)
);

CREATE INDEX ON chunks USING hnsw (embedding vector_cosine_ops);

-- 5 nearest chunks to the query embedding
SELECT content
FROM chunks
ORDER BY embedding <=> $1
LIMIT 5;
```

---

## RAG (Retrieval-Augmented Generation)

RAG answers questions using **your** documents: retrieve relevant passages, put them in the prompt, and ask the model to answer from them.

Why: models don't know your private or recent data, and grounding in sources cuts hallucination and enables citations — without retraining the model.

```text
INDEXING (offline)
 documents ──► clean ──► chunk ──► embed ──► vector store

QUERYING (per request)
 question ──► embed ──► search top-k ──► (re-rank) ──► prompt with chunks ──► LLM ──► answer + citations
```

### Chunking

Documents are split into chunks small enough to be specific but large enough to carry meaning.

- Typical size: 200–800 tokens with 10–20% overlap.
- Split on structure first (headings, paragraphs, functions in code), then by size.
- Store metadata with each chunk: source, title, section, date, permissions.
- **Contextual chunking:** prepend a one-line summary of the parent document to each chunk before embedding, so "It increased by 3%" still knows what "it" is.

### Retrieval quality

- **Hybrid search** — combine vector similarity with keyword search (BM25). Keywords catch exact names, IDs, and error codes that embeddings blur.
- **Re-ranking** — fetch top 50 cheaply, then a cross-encoder re-ranker picks the best 5.
- **Query rewriting** — have the LLM turn a chatty follow-up ("what about the second one?") into a standalone search query.
- **Metadata filters** — restrict by tenant, product, date, or user permissions **before** ranking.

### The generation prompt

```text
Answer the question using only the sources below.
If the sources don't contain the answer, say "I don't know based on the docs."
Cite sources as [1], [2].

<sources>
[1] (billing-faq.md) Refunds are processed within 5–7 business days...
[2] (payments-api.md) A refund can be issued via POST /v1/refunds...
</sources>

<question>How long does a refund take?</question>
```

### Minimal RAG in Python

```python
def answer(question: str) -> str:
    q_vec = embed(question)
    rows = db.execute(
        "SELECT source, content FROM chunks ORDER BY embedding <=> %s LIMIT 5",
        (q_vec,),
    ).fetchall()
    sources = "\n".join(f"[{i + 1}] ({src}) {text}" for i, (src, text) in enumerate(rows))
    prompt = f"""Answer only from the sources. Cite as [n]. Say "I don't know" if unsure.

<sources>
{sources}
</sources>

<question>{question}</question>"""
    return llm(prompt)
```

### Evaluating RAG

Measure the two halves separately:

- **Retrieval:** recall@k — is the right chunk in the top k? Build a set of question → expected-source pairs.
- **Generation:** faithfulness (answer supported by the sources?), relevance (does it answer the question?), and citation correctness.

### RAG vs long context vs fine-tuning

| Approach | Best for | Weakness |
| --- | --- | --- |
| RAG | Large or changing knowledge bases, citations, permissions | Retrieval misses = wrong answers |
| Long context (paste it all) | A few documents that fit in the window | Cost and latency per call |
| Fine-tuning | Teaching style, format, or a narrow skill | Poor at injecting facts; must retrain to update |

### Agentic RAG

Instead of one fixed retrieval step, give an agent a `search` tool and let it decide what to search, read results, and search again until it has enough. Better for multi-hop questions ("compare our 2024 and 2025 refund policies"), at the cost of more calls.
