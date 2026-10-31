---
title: "Rate limiting"
order: 8
---
# Rate limiting

## Key points

- **Rate limiting** caps *caller* behaviour (429); **load shedding** protects *server* health (503). Use both.
- Enforce **early** (edge/gateway), **identity-based** limits after auth; **business-cost** limits in the service.
- **Token bucket / GCRA**: bursts up to capacity, O(1) — default for APIs.
- **Leaky bucket (queue)**: smooth outbound rate; **meter** ≡ token bucket inverted.
- **Fixed window**: cheapest; **2× boundary burst**. **Sliding window counter**: scale + good accuracy. **Sliding log**: exact, small limits.
- **Distributed**: atomic **Lua** in Redis, **`TIME` in script**, **hash tags** for Cluster; local-first + global hybrid at high RPS.
- Tell clients: **`Retry-After`**, RateLimit headers; clients use **jittered backoff**.

---

## Why rate limit

| Goal | Example |
| --- | --- |
| Protect availability | Runaway client at 5k req/s |
| Fairness | One tenant starves others |
| Security | Login / OTP brute force |
| Cost | LLM, SMS per call |
| Monetisation | Free 60/min, Pro 1000/min |
| Protect downstream | Partner API 50 req/s cap |

```text
Client → Edge (IP, bots) → Gateway (API key, route) → Service (business cost) → DB (pools)
         reject cheaply      reject after auth           expensive ops only
```

| Layer | Good for | Weak at |
| --- | --- | --- |
| Edge / CDN | Volumetric abuse, per-IP | User identity |
| Gateway | Per-key, per-route, plan tiers | Operation cost |
| Service | "3 exports/hour/account" | Must replicate logic |
| DB pool | Last-resort concurrency | Fairness between callers |

---

## Rate limit vs load shedding vs concurrency

| | Rate limit | Load shedding | Concurrency limit |
| --- | --- | --- | --- |
| Limits | Arrivals per time | Work accepted when overloaded | Requests **in flight** |
| HTTP | 429 | 503 | 503 / shed |
| Little's law | L = λ × W — slow deps → same λ, huge **L** without concurrency cap |

Stripe-style stack: request rate limiter + **concurrent** limiter + fleet/worker shedders.

---

## Algorithms

Use a monotonic clock (`nanoTime`), not wall clock (NTP jumps).

### Token bucket

Bucket capacity **C**, refill **r** tokens/s. Request costs **n** tokens; reject if insufficient. Allows **burst up to C** after idle.

```text
capacity=4, r=1/s
t=0: ●●●●● → A A A A R (burst then reject)
t=7 idle → refilled to 4
```

| Property | Value |
| --- | --- |
| State | tokens + last refill time |
| Bursts | Up to capacity |
| Distributed | Lua read-modify-write |

**Pitfalls**: oversized capacity = one client dumps C requests at once; **full bucket on new key** = deploy burst.

Implementation note: lock-free token bucket keeps `{tokens, lastNanos}` in an `AtomicReference`; on reject, often **no write** (read-only path under attack). `nanosUntilAvailable` drives accurate **`Retry-After`**.

Weighted requests: charge `n` tokens for expensive operations (export = 50, read = 1).

### Leaky bucket

**2a — Queue (shaper)**: bounded FIFO; drain at fixed rate; overflow rejected. Smooth **output**; adds **latency** — poor for inbound HTTP, good for **outbound** to strict partners (Nginx `limit_req` with burst, no `nodelay`).

**2b — Meter (policer)**: water level drains at constant rate; each request adds water; overflow reject. **Mathematically equivalent to token bucket** (level ↔ empty tokens).

```text
Bursty in → [queue cap N] → steady out at r
```

### Fixed window counter

One counter per window (e.g. calendar minute). **Boundary problem**:

```text
limit 100/min — 100 at 12:00:59 + 100 at 12:01:00 → 200 in ~1s
```

Cheap in Redis (`INCR` + `EXPIRE` in one script). Mitigate: pair coarse window with short token bucket; **per-key random window offset** to avoid synchronized retries at :00.

### Sliding window log

Store timestamp of each accepted request; evict older than window; count — **exact**.

| | Cost |
| --- | --- |
| Memory | O(limit) per key |
| Redis | Sorted set, ~50–100 B/entry |

Use for **small strict limits** (login, OTP). Decide: count **rejected** requests or not?

### Sliding window counter

Two counters: **previous** window **p**, **current** **c**; fraction elapsed **f** in current window:

```text
estimate = p × (1 − f) + c
allow if estimate + cost ≤ limit
```

O(1), two counters — **Cloudflare-scale default** for "N per minute". Small error if traffic bunched at window edges; empirically tiny.

### GCRA (Generic Cell Rate Algorithm)

ATM **leaky bucket meter** with one **TAT** (theoretical arrival time):

```text
emission interval T = 1/r, burst tolerance = B×T
newTAT = max(TAT, now) + n×T
allow if newTAT − now ≤ B×T
```

One integer state, natural **retry-after** — used by `redis-cell`, many CDNs. Behaviour ≈ token bucket capacity **B**, rate **r**.

### Comparison

| Algorithm | State | Bursts | Distributed | Typical use |
| --- | --- | --- | --- | --- |
| Token bucket | 2 nums | Up to C | Lua | API limits, AWS burst |
| Leaky queue | Queue | Smoothed | Hard | Outbound shaping |
| Leaky meter / GCRA | 1–2 nums | Up to B | Lua | CDNs, redis-cell |
| Fixed window | 1 counter | 2× at edge | INCR | Daily/monthly quotas |
| Sliding log | O(limit) | None | ZSET | Logins, OTP |
| Sliding counter | 2 counters | Small approx | 2 keys + script | HTTP per-minute at scale |

**Interview pick**: token/GCRA for burst-tolerant APIs; sliding counter for literal "N/min" at scale; sliding log when exact + small N; fixed window for quotas; leaky queue for outbound shaping.

---

## Distributed rate limiting (Redis)

### Race to avoid

```text
A: GET count=99 → allow → SET 100
B: GET count=99 → allow → SET 100   → two allowed, counter wrong
```

Fix: **`INCR` atomic**, or **Lua script** (single atomic read-modify-write). Never separate `INCR` and `EXPIRE` without atomicity — key lives forever.

### Fixed window in Lua (sketch)

```lua
-- KEYS[1]=counter, ARGV: limit, window_sec, cost
local count = redis.call('INCRBY', KEYS[1], cost)
if count == cost then redis.call('EXPIRE', KEYS[1], window) end
return count <= limit and 1 or 0
```

### Token bucket Lua (sketch)

- `TIME` inside script → single clock per primary.
- Refill: `tokens = min(capacity, tokens + elapsed × rate)`.
- Rejections can avoid writes (optional optimization).
- `PEXPIRE` when bucket would be full again → idle keys disappear.

### Sliding log Lua (sketch)

`ZREMRANGEBYSCORE` old → `ZCARD` → `ZADD` with **unique member** (UUID); score = timestamp.

### Clock and Cluster

- Do **not** pass client `now` for refill — skew refills early/late.
- **Hash tags**: `rl:{tenant:42}:minute:…` — only `{tenant:42}` hashed → same slot for multi-key scripts.
- **Hot global key**: shard into `k` sub-keys each limit `L/k`, or local pre-aggregation.

### Hybrid local + global

| Strategy | Trade-off |
| --- | --- |
| Pure Redis every request | Exact, +RTT |
| Local at L/N per node | Bad if uneven load |
| Token **lease** (batch from Redis) | Over-admit up to N×batch |
| Local 2× fair share + Redis real limit | Shed attacks cheaply; exact globally |

Approximate over-admit (1003 vs 1000) usually OK for abuse; **not** for billing quotas (durable ledger).

---

## Designing a limiter service

**Requirements sketch**: keys (IP, API key, tenant, route); multiple rules per request (all must pass); hot-reload rules; p99 added latency ≪ 2 ms; **fail open** default with local fallback.

```yaml
# Rules as data (conceptual)
- name: login_attempts
  match: { path: /login, method: POST }
  key: [remote_ip]
  algorithm: sliding_window_log
  limit: 5
  window: 15m

- name: plan_burst
  key: [api_key]
  algorithm: token_bucket
  by_plan: { free: { capacity: 20, r: 1/s }, pro: { … } }
```

Flow: auth → extract descriptors → **local** generous bucket → **global** Lua per hash-tagged principal → 429 or forward. Evaluate all rules in **one script** when possible so one rule doesn't consume tokens before another rejects.

---

## Response headers and status codes

```text
429 Too Many Requests
Retry-After: 30
RateLimit-Policy: "burst";q=100;w=60
RateLimit: "burst";r=0;t=30
```

| | 429 | 503 |
| --- | --- | --- |
| Meaning | **You** exceeded limit | **Server** overloaded / shedding |
| Client | Slow down | Backoff, maybe another region |
| Monitoring | Expected spikes per key | Availability SLO |

Prefer **delta seconds** in reset fields (not client clock). **`Retry-After`** is widely understood. Nginx `limit_req` defaults to 503 — set `limit_req_status 429`.

Legacy: `X-RateLimit-Limit/Remaining/Reset` still common.

---

## Multi-dimensional limits

| Dimension | Example |
| --- | --- |
| Per IP | Anonymous abuse (weak: NAT, CGNAT) |
| Per API key | Integration misbehaviour |
| Per tenant | Noisy neighbour |
| Per endpoint | Expensive POST |
| Global downstream | SMS gateway total cap |

- Trust **client IP** from **your** proxies only (`X-Forwarded-For` right-most untrusted hop).
- **Weighted cost**: GraphQL complexity, export rows, LLM tokens.
- **Quota vs rate**: quota = day/month, durable ledger, exact; rate = seconds–minutes, approximate OK.

---

## Adaptive limiting and shedding

**AIMD**: increase limit slowly on success; multiply decrease on timeout/latency spike (TCP-like).

Netflix **concurrency-limits**: gradient/Vegas algorithms on **in-flight** work — shed with 503 when limit exceeded; `onSuccess` / `onDropped` / `onIgnore` (don't count app bugs as overload).

Shedding tactics: priority tiers, reject before body parse, bounded queues with deadlines, **503 + Retry-After**, LIFO under overload (controversial).

---

## Client retries

Without backoff → **retry storm** at window reset.

```text
exponential (no jitter):  all clients retry at t+1s, t+2s, t+4s together
full jitter:            sleep = random(0, min(cap, base·2^attempt))   ← default
```

- Retry 429, 503, idempotent timeouts only.
- Honour **`Retry-After`**.
- **Full jitter**: `sleep = random(0, min(cap, base×2^attempt))`.
- **Retry budget**: retries ≤ 10% extra load.

Outbound: local token bucket **below** partner limit to avoid their 429.

---

## Real implementations (cheat sheet)

| System | Model | Notes |
| --- | --- | --- |
| Nginx `limit_req` | Leaky + burst | Shared zone per host; `burst nodelay` for API bursts |
| Envoy local RL | Token bucket | Per instance, no RTT |
| Envoy global RL | Fixed window in Redis | gRPC to ratelimit service; `failure_mode_deny` default false |
| AWS API Gateway | Token bucket | rate + burst |
| Guava / Resilience4j | In-process | Outbound pacing; R4j ≈ fixed window cycles |
| Bucket4j | Token bucket | Redis/JCache distributed |

---

## Fail-open vs fail-closed

| | Fail open | Fail closed |
| --- | --- | --- |
| API stays up | Yes | No if store down |
| Protection | Lost briefly | Kept |
| Typical | General API limits | Login, OTP, paid quota |

Always: **short timeout**, **circuit breaker**, **local fallback** at `limit/N`, **alert** on fallback — not silent unlimited.

---

## Testing and monitoring

- **Fake clock** in unit tests; multithreaded "never over-admit" test.
- Redis: Testcontainers + concurrent `EVALSHA`.
- Shadow / dry-run new rules before enforce.
- Metrics: allowed/limited per rule, limiter p99, fail-open count, top limited keys (sampled), Redis hot keys.

---

## Pitfalls

- GET-decide-SET across instances; `INCR` + separate `EXPIRE`.
- Per-instance limit × N gateways = N× effective limit.
- Wall clock in-process (NTP jump).
- Client-forged `X-Forwarded-For`.
- Rate limit only, no concurrency limit (slow dependency → thread exhaustion).
- Redis hard dependency, no timeout.
- Synchronized window reset + no jitter → thundering herd.
- 503 for rate limits (client can't distinguish).
- Counting rejects and locking out legitimate retries.
- Unbounded key cardinality (key = raw header value).

---

## Quick check

1. **Token bucket vs leaky bucket (queue)** — burst behaviour and when each fits inbound vs outbound.
2. **Fixed window boundary** — worst-case multiplier and two mitigations.
3. **Why Lua in Redis** — what race does it prevent?
4. **Hash tags in Cluster** — why `{tenant:42}` in the key?
5. **429 vs 503 vs concurrency limit** — what failure mode does each address?
