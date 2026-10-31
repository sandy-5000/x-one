---
title: "System design basics"
order: 3
---
# System Design Basics

How large applications fit together: clients, DNS, load balancers, stateless services, caches, databases, queues, object storage. Goal: stay **fast, available, correct, affordable** as traffic and data grow.

**Rule:** every decision ties to a requirement or a number — “Redis because reads are ~20× writes, p99 < 100 ms, DB would see ~40k QPS without cache” beats “add a cache.”

---

## Key points

- Interviews are **structured conversations**: scope → estimate → API/data → boxes → 2–3 deep dives → failures and 10× evolution; have a diagram by ~minute 10.
- **Non-functional** requirements (latency p99, peak QPS, durability, consistency per operation, RPO/RTO) drive architecture more than feature lists.
- **SLI** measures behaviour; **SLO** is your internal target; **SLA** is contractual (looser). **Error budget** = 1 − SLO — spend it on features until burn forces reliability work.
- **Little’s law:** in-flight = λ × latency — sizes pools and explains how slow DBs exhaust every upstream pool.
- **Tail at scale:** parallel fan-out makes user p99 worse than backend p99 (e.g. 100 calls at p99 1 s → ~63% of requests hit a slow hop); reduce fan-out, hedge, partial results.
- Estimate for **decisions**: 86,400 s/day ≈ 10⁵; 1M req/day ≈ 12/s; peak often 2–3× (10×+ for spikes); remember replication (×3) and index overhead.
- **Scale out** stateless tiers first; DB: vertical → read replicas + cache → **shard last** (expensive forever).
- **L4** balances connections; **L7** balances requests — required for HTTP/2/gRPC multiplexing; use **power of two choices** when many LBs share stale views.
- **CDN** for bytes (especially images/video); **edge TLS** cuts handshake RTTs; content-hashed filenames beat purge-at-scale.
- **Availability math** assumes independence — correlated failures (deploy, config, AZ, shared DB) dominate; **stateful failover** dominates downtime.
- Reliability: **timeouts + propagated deadlines**, jittered retries at **one layer**, **idempotency keys** for POST-like ops, circuit breakers, bulkheads, shedding before queue explosion.
- Observe with metrics/logs/traces (OpenTelemetry); **alert on SLO burn**, not “CPU 80%.”

---

## Interview flow (45–60 min)

| Phase | Time | Output |
| --- | --- | --- |
| Clarify | 0–5 | Functional list, NFR targets, explicit out-of-scope |
| Estimate | 5–10 | Peak QPS, storage/year, bandwidth — only if they change design |
| API + data | 10–15 | Few endpoints; entities; **access patterns** pick the DB |
| High level | 15–25 | End-to-end path per use case; add boxes when justified |
| Deep dives | 25–40 | Partitioning, hot keys, fan-out, consistency, failure |
| Wrap | 40–45 | Bottlenecks, monitoring, 10× plan |

**Anti-patterns:** microservices/K8s “for scale” below stated load; cache box with no key/TTL/invalidation; database chosen before access patterns; happy path only; estimates never tied to a decision.

```text
Users → DNS/GSLB → CDN (static) → regional L7 LB/gateway → stateless services (3 AZ)
                              → Redis / SQL / queue / object storage → async workers
```

---

## Requirements and SLOs

| Type | Examples |
| --- | --- |
| Functional | Post photo, redirect short URL, search by prefix |
| Non-functional | p99 latency, peak QPS, 99.9% availability, RPO 5 min, GDPR residency |

| Term | Meaning |
| --- | --- |
| SLI | Measured signal (e.g. non-5xx within 300 ms) |
| SLO | Internal target over a window (e.g. 99.9% / 30 days) |
| SLA | Customer contract + penalties (looser than SLO) |

**Percentiles:** never average p99 across hosts — merge histograms. **Durability** ≠ **availability** (S3 can be down briefly without losing objects). **Consistency** is per operation — ledger vs like counter vs feed.

```text
Tail at scale (parallel fan-out):
  user request waits for N backends in parallel
  P(at least one slow) ≈ 1 − (1 − p_slow)^N
  100 backends each p99=1s → ~63% of user requests see ≥1s tail
Mitigate: fewer hops, hedged requests, partial results, fix backend tails
```

**Little’s law:** L = λ × W — at 2,000 req/s and 50 ms average time in system, ~100 requests in flight (sizes thread/connection pools).

---

## Back-of-the-envelope

| Fact | Use |
| --- | --- |
| 2³⁰ ≈ 1 GB | Memory sizing |
| 10⁵ s/day | QPS from daily volume |
| 1 Gbps = 125 MB/s | Egress math (watch bits vs bytes) |
| Peak 2–3× avg | Fleet sizing |

**Example (read-heavy social):** 200M DAU × 10 feed reads/day ≈ 23k QPS avg → ~70k peak (×3); posts ~1.2k avg → cache + CDN for reads, shard metadata DB as TB/year × replicas grows.

**Servers (CPU bound):** 16 cores, 10 ms CPU/request → ~800 req/s at 50% util → 70k/800 ≈ 88; ×3/2 for surviving one AZ ≈ 132.

**Servers (Little’s law):** 70k × 80 ms in-flight = 5,600; if 200 concurrent/server → 28 — take **max** of CPU vs concurrency; latency regression flips the bottleneck.

**Cache (80/20):** 20% of 10⁸ posts × ~1 KB ≈ 22 GB — fits one Redis; precomputed timelines for all users can be **TB** — shard, active-user subset, rebuild on miss.

**Storage walkthrough:** 10⁸ posts/day × 1 KB metadata ≈ 100 GB/day → ~36 TB/year × 3 replicas → shard SQL. 10⁷ photos/day × 250 KB ≈ 2.5 TB/day media → object storage; DB holds keys only.

**Bandwidth walkthrough:** 23k feed QPS × 20 KB JSON ≈ 460 MB/s API (~3.7 Gbps avg); 5 thumbs × 40 KB ≈ 37 Gbps image egress → CDN mandatory at peak.

---

## Latency orders of magnitude

| Operation | ~Time |
| --- | --- |
| L1 cache | 1 ns |
| RAM | 100 ns |
| SSD random 4K | 16 µs |
| Same-DC RTT | 0.5 ms |
| Cross-region RTT | 60–250 ms |

**Implications:** batch Redis (`MGET`, pipelines); sequential I/O beats random; **count RTTs** on the critical path; geography needs CDN/edge/multi-region, not faster code alone.

---

## Scaling and statelessness

| | Vertical | Horizontal |
| --- | --- | --- |
| Pros | Simple, strong consistency on one node | Redundancy, linear capacity |
| Cons | Ceiling, SPOF | Partitioning, partial failures |

```text
Vertical: one BIG box          Horizontal: LB → S1, S2, S3 (no local session state)
```

| Session approach | Trade-off |
| --- | --- |
| Sticky LB | Uneven load; lost on instance death |
| Redis session store | Extra hop; HA store required |
| JWT access + refresh | Fast; revocation harder |

Externalise: uploads → object storage; jobs → queue/leader election; WebSockets → connection registry (e.g. Redis).

---

## Load balancing

```text
L4: IP:port, high PPS          L7: path/host, TLS terminate, retries, gRPC-aware
     often fronting L7 fleet
```

| Algorithm | When |
| --- | --- |
| Round robin | Uniform backends |
| Least connections | Long-lived requests |
| Power of two choices | Many LBs — avoids herding |
| Consistent hashing + vnodes | Cache/shard affinity — ~1/N keys move on node change |

`hash(key) % N` remaps almost all keys when N changes — bad for cache clusters. Ring + many **virtual nodes** per host spreads load when a node fails.

| L4 vs L7 | L4 | L7 |
| --- | --- | --- |
| Sees | IP, port, TCP stream | HTTP path, headers, method |
| TLS | Often pass-through | Usually terminates |
| Best for | Raw throughput, first tier | Routing, gRPC/HTTP2 request balance |

**Health checks:** shallow for traffic (process up); deep DB checks can **drain entire fleet** when DB blips — use passive outlier detection separately. **Readiness** ≠ liveness; **connection draining** on deploy (SIGTERM → fail readiness → finish in-flight). K8s: brief `preStop` sleep — endpoints lag after SIGTERM.

**Global traffic:** GeoDNS/latency routing (TTL limits failover) vs **anycast** (fast withdraw, no TTL pain) — often CDN edge + private backbone to region.

**LB HA:** floating VIP (keepalived), ECMP across L4 nodes, or managed cloud LB — backends still need multi-AZ.

```java
// Power of two choices: pick 2 random backends, send to lower in-flight count
Backend pick(List<Backend> b) {
    int i = rnd.nextInt(b.size()), j = rnd.nextInt(b.size() - 1);
    if (j >= i) j++;
    return b.get(i).inFlight() <= b.get(j).inFlight() ? b.get(i) : b.get(j);
}
```

---

## Edge: reverse proxy, gateway, CDN

```text
User ──► edge PoP ──hit──► cached asset (10–30 ms)
              │
              miss ──► shield/origin (200+ ms if cross-region)
```

**Reverse proxy:** TLS, compression, buffering, routing (Nginx, Envoy, HAProxy).

**API gateway:** authn, rate limits, routing, observability — avoid business logic monolith at the edge; **BFF** per client type when needed.

| CDN concern | Practice |
| --- | --- |
| Cache key | Strip tracking query params; beware `Vary: Cookie` killing hit rate |
| Static JS/CSS | Content hash in filename + `immutable` — no purge on deploy |
| Freshness | `s-maxage` for shared cache; `stale-while-revalidate` for resilience |
| Purge | Expensive at scale — not primary freshness strategy |
| Origin shield | Mid-tier cache collapses many edge misses to one origin fetch |

**CDN pull model:** edge miss → origin (slow); images/video at scale **must not** hit origin at peak Gbps. **Push CDN** for large rarely changing blobs (installers, video libraries).

---

## DNS (design impact)

```text
Browser → OS cache → recursive resolver → root → TLD → authoritative NS → A/AAAA + TTL
Each layer caches; most lookups = one RTT to resolver if warm
```

Resolver chain: browser → OS → recursive resolver → root → TLD → authoritative. **TTL** trades failover speed vs query load; some clients cache longer than TTL — not instant failover. CNAME at apex limitations (ALIAS/ANAME). Plan lower TTL **before** migrations. Run authoritative DNS on anycast; consider second provider — DNS is a dependency.

---

## HTTP and TLS cost

| | HTTP/1.1 | HTTP/2 | HTTP/3 (QUIC) |
| --- | --- | --- | --- |
| Multiplexing | ~6 conn/host | One TCP, many streams | Streams without TCP HOL blocking |
| Typical new conn | 4 RTT (TLS1.2) | 3 RTT (TLS1.3) | ~2 RTT (1 with 0-RTT resumption*) |

\*0-RTT only for **idempotent** requests (replay risk). **Pool connections** inside the datacenter — avoid per-call handshakes and TIME_WAIT exhaustion.

| Handshake path (new connection) | RTTs before first byte |
| --- | --- |
| TCP + TLS 1.2 + HTTP | ~4 |
| TCP + TLS 1.3 + HTTP | ~3 |
| QUIC (HTTP/3) | ~2 (~1 with resumption) |

At 200 ms cross-region RTT, handshake alone is 600–800 ms — **terminate TLS at edge**, reuse connections (keep-alive, HTTP/2), prefer TLS 1.3 session resumption.

---

## Availability and DR

| 99.9% | ~8.8 h downtime/year |
| 99.99% | ~52 min/year — needs **automated** failover |

**Series:** A = A₁ × A₂ × … (chain weaker than weakest link). **Parallel:** A = 1 − (1−a)ⁿ if independent — **usually isn’t** (same deploy/AZ/bug).

```text
Example stack (rough):
  LB 99.99% × app tier (3×99.5% parallel) × DB failover 99.95% ≈ 99.94% (~5 h/year)
  Stateless redundancy barely moves the number — DB + failover dominate
```

**Failover risks:** split brain (two primaries) — use quorum + **fencing**; async replication → RPO > 0 on promote; test failover in game days.

```text
Active-passive DB: standby promotion + VIP/DNS     Active-active stateless: all nodes serve; size < 1/n load each
```

**Multi-AZ:** sync replication practical (~1–2 ms). **Multi-region:** async replication, conflict/residency cost — don’t default to it.

| Strategy | RPO (typical) | RTO (typical) |
| --- | --- | --- |
| Backup/restore | Hours | Hours–day |
| Pilot light / warm standby | Minutes | Minutes |
| Active-active multi-site | Near zero | Near zero |

**RPO** = acceptable data loss window; **RTO** = time to restore service. Test **restore**, not just backup; replication copies bad deletes.

---

## Reliability patterns

| Pattern | Purpose |
| --- | --- |
| Timeouts + deadlines | Stop pool exhaustion; pass remaining budget downstream |
| Retry + full jitter | Transient errors only; 2–3 attempts; **one layer**; retry budget |
| Idempotency key | Safe retry of charge/create — store result by `(client, key)` |
| Circuit breaker | Fail fast while dependency recovers |
| Bulkhead | Separate pools per dependency |
| Backpressure | Bounded queues; 429/503 vs unbounded latency |
| Degradation / shedding | Optional features off; reject when in-flight > limit |

```text
Circuit breaker: CLOSED ──failures──► OPEN (fail fast) ──timeout──► HALF-OPEN (trial calls)
Bulkhead: separate thread/connection pool per dependency so one slow service cannot starve checkout
```

```java
// Full jitter between retries (cap attempts, idempotent ops only)
long sleepMs = ThreadLocalRandom.current().nextLong(
    Math.min(capMs, baseMs * (1L << attempt)) + 1);
```

POST payments: client sends `Idempotency-Key`; server `INSERT ... ON CONFLICT` claim then execute once; duplicate returns stored response; mismatched body → 422.

**Retry rules:** transient only (503, timeout); **one layer** retries (3 layers × 3 attempts = 27× load at leaf); **retry budget** (~10% of normal traffic); honour `Retry-After`.

---

## Observability and security (basics)

| Signal | Use |
| --- | --- |
| Metrics | SLOs, saturation — avoid high-cardinality labels (user id) |
| Logs | Structured + **request/trace id** |
| Traces | Fan-out latency — W3C `traceparent` |

| Framework | Measures |
| --- | --- |
| Four golden signals | Latency, traffic, errors, saturation |
| RED (services) | Rate, errors, duration |
| USE (resources) | Utilisation, saturation, errors |

Alert on **SLO burn rate** (e.g. burning monthly budget at 14× for 1 h → page), link runbooks — not “CPU 80%” alone.

| Security area | Baseline |
| --- | --- |
| Transport | TLS everywhere; mTLS service-to-service where feasible |
| AuthN | OIDC/OAuth; Argon2id/bcrypt for passwords; MFA |
| AuthZ | Enforce in **owning service** every request; prevent IDOR |
| Secrets | Vault/cloud SM; never in git/images; rotate |
| Input | Parameterised SQL; size limits; SSRF care on URL fetch |
| Edge | WAF, DDoS at CDN, rate limits per key/IP |

Defence in depth: gateway validates token; service still checks authorisation.

---

## Reference path (one request)

```text
Clients ──► DNS/GSLB/anycast ──► CDN (static, TLS, WAF)
                    │
                    ▼ dynamic
         Regional L7 LB / API gateway (auth, rate limit, trace id)
                    │
         ┌──────────┼──────────┐
         ▼          ▼          ▼
    Service A   Service B   Service C   (stateless, 3 AZ, autoscale)
         └──────────┼──────────┘
    Redis cache   SQL primary+replicas   Kafka/queue   S3/GCS
                              │
                         async workers → search, email, analytics

Cross-cutting: OpenTelemetry · secrets manager · CI/CD
```

1. Anycast/CDN — TLS near user; static from edge.
2. Dynamic → regional L7 — auth, rate limit, route.
3. Service — cache-aside Redis; writes to SQL primary; reads replica OK when stale OK.
4. Async via queue for email/search/analytics; blobs via pre-signed object storage URLs.

Evolve: one region multi-AZ → replicas + cache → queue decouple → shard writes → multi-region when latency/DR law requires.

---

## Pitfalls

- Estimates that never change the design; **average** latency targets; ignoring **peak** and **growth/year**.
- Microservices/Kafka/K8s before one service + DB fits stated load.
- Cache with no key, TTL, invalidation, or plan for **cache failure** / stampede.
- No timeouts; retries everywhere; retry non-idempotent POST without idempotency keys.
- Deep LB health check on shared DB removes **all** backends at once.
- Sticky sessions + local disk blocking scale and drain.
- Assuming independent replica failures; **untested** failover and backups.
- Forgetting egress cost (CDN, cross-AZ/region transfer).
- HTTP/2/gRPC on L4-only balance — one fat connection to one backend; need L7 or client-side balance.
- Shedding vs rate limiting confused — rate limit is per-client policy; shedding protects total server capacity under overload.

---

## Quick check

**Vertical vs horizontal for a growing web app?**  
Scale stateless app tier horizontally early for capacity and AZ redundancy. Push the database vertically and add read replicas and caching until write throughput or disk forces sharding — sharding is the last resort because operability cost is permanent.

**50 parallel backends each p99 = 200 ms — user-facing impact?**  
Probability at least one exceeds p99 is 1 − 0.99⁵⁰ ≈ 39%, so many user requests wait ≥200 ms even if each service looks fine. Cut fan-out, hedge after p95, return partial results under deadline, and fix slow backends.

**L4 vs L7 — when each?**  
L4 for maximum throughput and non-HTTP or TLS passthrough, often in front of L7 proxies. L7 for path routing, TLS termination, per-request balancing (critical for HTTP/2/gRPC), retries, and auth — most user-facing HTTP uses L7 (sometimes stacked on L4).

**RPO vs RTO — how they constrain design?**  
RPO is how much data you may lose (time window); near-zero needs sync replication to another domain. RTO is how fast service returns; minutes need warm standby and automation, hours allow backup restore — cost rises sharply as both approach zero.

**Why can a cache make things worse?**  
Low hit rate, complex invalidation serving stale wrong data, **thundering herd** on expiry/cold start overloads the DB, or hidden capacity dependence so losing Redis causes collapse. Mitigate with TTL jitter, single-flight/coalescing, warm-up, and sizing origin for cache miss load.
