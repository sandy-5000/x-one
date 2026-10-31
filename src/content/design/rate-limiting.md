---
title: "Rate limiting"
order: 8
---
# Rate Limiting

A rate limiter decides, for each incoming unit of work, whether it may proceed **now**, must
**wait**, or must be **rejected**, based on how much work the same caller (or the whole system)
has already done recently. "100 requests per minute per API key" is the classic example, but the
same machinery also limits login attempts, outbound calls to a paid API, messages per chat room,
or concurrent database queries.

This page covers the goals, where limits belong in an architecture, every common algorithm with
a thread-safe Java implementation, distributed limiting with Redis, how to design a rate limiter
service end to end, adaptive limiting and load shedding, the real implementations you will meet
(Nginx, Envoy, Stripe, Guava, Resilience4j, Bucket4j), and how to test and operate one.

---

## Why rate limit

| Goal | Example | What it implies for the design |
| --- | --- | --- |
| Protect availability | A buggy client loops and sends 5k req/s | Must be cheap and fast; enforced early |
| Fairness between tenants | One tenant's batch job starves everyone else | Keyed per tenant, not global |
| Security / abuse | Credential stuffing on `/login`, scraping | Keyed per IP, per account, per device |
| Cost control | Each call to an LLM or SMS provider costs money | Often a quota (per day/month) not a rate |
| Monetisation | Free tier 60/min, Pro 1,000/min | Rules come from the billing plan |
| Protect downstreams | Your service calls a partner API limited to 50 req/s | Limit is on **your outbound** traffic |

Two distinct questions get mixed up:

- **Rate limiting** is about *a caller's* behaviour: "this client may not exceed X per period."
  It returns **429 Too Many Requests** and is mostly a policy decision.
- **Load shedding** is about *the server's* health: "I am overloaded, I will drop some work
  regardless of who sent it." It returns **503 Service Unavailable** and is a survival decision.

A well-protected system has both. A client can be well within its rate limit while the service
is still overloaded (a thousand well-behaved clients at once), and a single client can be over its
limit while the service is idle.

---

## Where to enforce

```text
 ┌────────┐   ┌─────────────┐   ┌─────────────┐   ┌─────────────┐   ┌──────────┐
 │ Client │──►│ Edge / CDN  │──►│ API gateway │──►│  Service    │──►│ Database │
 └────────┘   └─────────────┘   └─────────────┘   └─────────────┘   └──────────┘
  self-limit    per-IP, WAF,      per API key,      per business      connection
  + backoff     DDoS, bots        per route,        operation,        pools, query
                                  per tenant        concurrency       timeouts
```

| Layer | Knows about | Good for | Weak at |
| --- | --- | --- | --- |
| Client | Its own traffic | Avoiding 429s, smoothing batch jobs | Cannot be trusted |
| Edge / CDN (Cloudflare, Akamai, AWS WAF) | IP, path, headers, geo | Volumetric attacks, bots, cheap rejection far from origin | User identity, business rules |
| Load balancer / gateway (Nginx, Envoy, Kong, AWS API Gateway) | Authenticated principal, API key, route | Per-key and per-route limits, plan tiers | Cost of individual operations |
| Service (in-process middleware) | Full business context | "3 password resets per hour per account", weighted cost | Must be replicated in every service |
| Database / shared resource | Its own capacity | Connection pool caps, statement timeouts, admission control | Fairness between callers |

Rules of thumb:

- **Reject as early as possible.** Rejecting at the edge costs microseconds; rejecting after
  authentication, deserialisation, and a DB read wastes most of the work you were trying to save.
- **Enforce identity-based limits after authentication.** An unauthenticated per-IP limit at the
  edge plus an authenticated per-key limit at the gateway is the standard combination.
- **Business limits live in the service.** Only the service knows that a `POST /exports` costs
  1,000x more than a `GET /me`.
- **Defence in depth.** Each layer catches what the previous one could not see. The database
  connection pool is itself a concurrency limiter of last resort.

---

## Algorithms

All implementations below share one small interface and take an injectable clock, which matters
later for deterministic tests.

```java
import java.util.function.LongSupplier;

public interface RateLimiter {
    /** Try to take {@code permits} units of capacity right now; never blocks. */
    boolean tryAcquire(int permits);

    default boolean tryAcquire() {
        return tryAcquire(1);
    }

    /** Monotonic nanosecond clock; System::nanoTime in production, a fake in tests. */
    LongSupplier SYSTEM_NANOS = System::nanoTime;
}
```

`System.nanoTime()` is used instead of `System.currentTimeMillis()` because it is monotonic:
wall-clock time can jump backwards on NTP corrections, which would make a bucket refill negatively
or a window reset early.

### 1. Token bucket

**Model.** A bucket holds at most `capacity` tokens. Tokens are added continuously at `rate`
tokens per second. A request costing `n` takes `n` tokens; if there are not enough, it is
rejected (or waits). The bucket starts full.

**Properties.**

- Long-run average rate is bounded by `rate`.
- Bursts up to `capacity` are allowed after a quiet period. Capacity is the knob for "how bursty
  may a client be."
- O(1) memory: just `tokens` and `lastRefillTime`. No timer is needed; refill is computed lazily
  on each request from the elapsed time.
- Weighted requests are natural: an expensive call costs 10 tokens.

Used by AWS API Gateway (rate + burst settings), Stripe, Envoy's local rate limiter, Bucket4j,
Guava (a variant), and most cloud provider APIs.

```text
capacity = 4 tokens, refill = 1 token/s, cost = 1 token per request
A = allowed, R = rejected

t (s)      0.0           1.0      1.5      2.0                 7.0
           │             │        │        │                   │
requests   ● ● ● ● ●     ●        ●        ●                   ● ● ● ●
result     A A A A R     A        R        A                   A A A A
tokens     4 → 0         1 → 0    0.5      1 → 0               4 → 0
                                                 (idle 5 s: refills to 4, capped)
```

**Thread-safe, lock-free implementation.** State is an immutable record swapped with
compare-and-set, so concurrent callers never block each other. On contention a caller simply
recomputes and retries.

```java
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.LongSupplier;

public final class TokenBucket implements RateLimiter {

    private record State(double tokens, long lastNanos) {}

    private final double capacity;
    private final double tokensPerNano;
    private final LongSupplier clock;
    private final AtomicReference<State> state;

    public TokenBucket(long capacity, double tokensPerSecond, LongSupplier clock) {
        if (capacity <= 0 || tokensPerSecond <= 0) {
            throw new IllegalArgumentException("capacity and rate must be positive");
        }
        this.capacity = capacity;
        this.tokensPerNano = tokensPerSecond / 1_000_000_000.0;
        this.clock = clock;
        this.state = new AtomicReference<>(new State(capacity, clock.getAsLong()));
    }

    public TokenBucket(long capacity, double tokensPerSecond) {
        this(capacity, tokensPerSecond, SYSTEM_NANOS);
    }

    @Override
    public boolean tryAcquire(int permits) {
        if (permits > capacity) {
            return false; // can never succeed; fail fast instead of starving
        }
        while (true) {
            State current = state.get();
            long now = clock.getAsLong();
            long elapsed = Math.max(0, now - current.lastNanos());
            double available = Math.min(capacity, current.tokens() + elapsed * tokensPerNano);
            if (available < permits) {
                return false; // nothing to write: rejection is read-only
            }
            State next = new State(available - permits, Math.max(now, current.lastNanos()));
            if (state.compareAndSet(current, next)) {
                return true;
            }
        }
    }

    /** Nanoseconds until {@code permits} tokens are available; 0 if available now. */
    public long nanosUntilAvailable(int permits) {
        State current = state.get();
        long elapsed = Math.max(0, clock.getAsLong() - current.lastNanos());
        double available = Math.min(capacity, current.tokens() + elapsed * tokensPerNano);
        return available >= permits ? 0 : (long) Math.ceil((permits - available) / tokensPerNano);
    }
}
```

Details worth noticing:

- **Rejections do not write.** Under attack most calls are rejections; keeping them read-only
  avoids CAS contention exactly when load is highest.
- **`Math.max(now, lastNanos)`** guards against a thread that read the clock earlier but wins the
  CAS later, which would otherwise move `lastNanos` backwards and double-count refill.
- **Doubles** keep fractional tokens; with integer tokens, a rate of 0.5/s would never refill.
- `nanosUntilAvailable` gives you the value for a `Retry-After` header.

**Pitfalls.** A large capacity means a client can dump `capacity` requests on you at once after
being idle; size capacity for what the backend can absorb, not for client convenience. Starting
the bucket full means every deploy or new key gets a full burst.

### 2. Leaky bucket

"Leaky bucket" names two different things, and interviewers sometimes mean either.

#### 2a. Leaky bucket as a queue (traffic shaper)

Requests enter a bounded FIFO queue; a worker removes them at a constant rate. If the queue is
full, new arrivals are dropped. The output is perfectly smooth regardless of input burstiness.

```text
arrivals (bursty)       queue, capacity 5              output (smooth)

●●●● ●●  ●●●●●●  ────►  ┌───┬───┬───┬───┬───┐  ────►  ●   ●   ●   ●   ●
                        │ ● │ ● │ ● │ ● │ ● │         one every 100 ms
queue full: reject      └───┴───┴───┴───┴───┘
```

```java
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;

public final class LeakyBucketQueue<T> implements AutoCloseable {

    private final BlockingQueue<T> queue;
    private final ScheduledExecutorService drainer;

    public LeakyBucketQueue(int capacity, double leaksPerSecond, Consumer<T> handler) {
        this.queue = new ArrayBlockingQueue<>(capacity);
        this.drainer = Executors.newSingleThreadScheduledExecutor(r -> {
            Thread t = new Thread(r, "leaky-bucket-drainer");
            t.setDaemon(true);
            return t;
        });
        long periodNanos = (long) (1_000_000_000L / leaksPerSecond);
        drainer.scheduleAtFixedRate(() -> {
            T item = queue.poll();
            if (item == null) {
                return;
            }
            try {
                handler.accept(item);
            } catch (RuntimeException e) {
                // An exception escaping a scheduled task cancels all future runs.
                System.err.println("leaky bucket handler failed: " + e);
            }
        }, periodNanos, periodNanos, TimeUnit.NANOSECONDS);
    }

    /** Thread-safe; returns false when the bucket overflows. */
    public boolean offer(T item) {
        return queue.offer(item);
    }

    @Override
    public void close() {
        drainer.shutdownNow();
    }
}
```

Use it for **outbound** traffic where the remote side demands a steady rate (a partner API that
bans you above 50 req/s, an SMTP relay). It is a poor fit for inbound HTTP: queued requests add
latency, and the client may have timed out by the time its request is processed. Nginx
`limit_req` with `burst` (without `nodelay`) behaves like this.

#### 2b. Leaky bucket as a meter (policer)

No real queue: keep a "water level" that drains at a constant rate. Each request adds water;
if it would overflow, reject. This is exactly a **token bucket turned upside down** (water level
= capacity - tokens), and it is what the ATM networking literature (and GCRA below) formalised.

```java
import java.util.function.LongSupplier;

public final class LeakyBucketMeter implements RateLimiter {

    private final double capacity;
    private final double leakPerNano;
    private final LongSupplier clock;
    private double level;      // guarded by this
    private long lastNanos;    // guarded by this

    public LeakyBucketMeter(long capacity, double leaksPerSecond, LongSupplier clock) {
        this.capacity = capacity;
        this.leakPerNano = leaksPerSecond / 1_000_000_000.0;
        this.clock = clock;
        this.lastNanos = clock.getAsLong();
    }

    @Override
    public synchronized boolean tryAcquire(int permits) {
        long now = clock.getAsLong();
        level = Math.max(0.0, level - Math.max(0, now - lastNanos) * leakPerNano);
        lastNanos = Math.max(now, lastNanos);
        if (level + permits > capacity) {
            return false;
        }
        level += permits;
        return true;
    }
}
```

A plain `synchronized` method is fine here: the critical section is a few arithmetic operations,
and an uncontended monitor in modern HotSpot costs tens of nanoseconds. Prefer the simple version
unless profiling shows contention.

### 3. Fixed window counter

Split time into fixed windows (each calendar minute) and keep one counter per window. Increment
on each request; reject when the counter exceeds the limit; the counter resets when the window
changes.

```java
import java.util.function.LongSupplier;

public final class FixedWindow implements RateLimiter {

    private final long limit;
    private final long windowNanos;
    private final LongSupplier clock;
    private long currentWindow = Long.MIN_VALUE; // guarded by this
    private long count;                          // guarded by this

    public FixedWindow(long limit, long windowNanos, LongSupplier clock) {
        this.limit = limit;
        this.windowNanos = windowNanos;
        this.clock = clock;
    }

    @Override
    public synchronized boolean tryAcquire(int permits) {
        long window = Math.floorDiv(clock.getAsLong(), windowNanos);
        if (window != currentWindow) {
            currentWindow = window;
            count = 0;
        }
        if (count + permits > limit) {
            return false;
        }
        count += permits;
        return true;
    }
}
```

With `nanoTime` the windows are not aligned to wall-clock minutes (its origin is arbitrary).
For headers like "resets at 12:01:00" use epoch milliseconds for the window index instead.

**The boundary problem.** Each window is individually within the limit, but a client can send
the full limit at the end of one window and again at the start of the next:

```text
limit = 100 per minute

           window 12:00                         window 12:01
 ├──────────────────────────────────┼──────────────────────────────────┤
                         100 requests ▲ 100 requests
                           12:00:59.5 │ 12:01:00.2
                                ◄──── 200 requests in 0.7 s ────►
```

So the worst case is **2x the limit in a short interval**. It is still widely used because it is
the cheapest possible distributed limiter (one `INCR` per request) and its counters map directly
onto "per minute / per day" quotas that humans understand. Mitigate the spike by pairing a coarse
fixed window ("10,000/hour") with a short token bucket ("20/second").

A second, subtler issue: all clients reset at the same instant, so if many are throttled they all
retry at 12:01:00 and create a synchronized spike. Per-key random window offsets avoid this.

### 4. Sliding window log

Keep the timestamp of every accepted request. On each request, drop timestamps older than the
window and count what remains. This is **exact**: at every instant, the last `window` seconds
contain at most `limit` accepted requests.

```text
limit = 3 per 10 s

t (s):   0    2    4         10   11   12        14
         ●    ●    ●         ●    ●    ●         ●
         A    A    A         A    R    A         A

  t=10  log [0,2,4]   0 is exactly 10 s old, evicted → [2,4]     → allow, log [2,4,10]
  t=11  log [2,4,10]  nothing old enough to evict, 3 in window   → reject
  t=12  log [2,4,10]  2 is now 10 s old, evicted → [4,10]        → allow, log [4,10,12]
  t=14  log [4,10,12] 4 evicted → [10,12]                        → allow
```

A neat thread-safe implementation for single-permit requests uses a ring buffer of `limit`
timestamps: the oldest slot tells you whether there is room, with no allocation per request.

```java
import java.util.Arrays;
import java.util.function.LongSupplier;

public final class SlidingWindowLog implements RateLimiter {

    private final long windowNanos;
    private final LongSupplier clock;
    private final long[] stamps;   // ring buffer of the last `limit` accepted timestamps
    private int oldest;            // index of the oldest timestamp; guarded by this

    public SlidingWindowLog(int limit, long windowNanos, LongSupplier clock) {
        this.windowNanos = windowNanos;
        this.clock = clock;
        this.stamps = new long[limit];
        // Pretend all slots were used exactly one window ago, i.e. all are free.
        Arrays.fill(stamps, clock.getAsLong() - windowNanos);
    }

    @Override
    public synchronized boolean tryAcquire(int permits) {
        if (permits != 1) {
            throw new UnsupportedOperationException("ring buffer log supports 1 permit");
        }
        long now = clock.getAsLong();
        if (now - stamps[oldest] < windowNanos) {
            return false;               // `limit` requests already inside the window
        }
        stamps[oldest] = now;           // overwrite the oldest with the newest
        oldest = (oldest + 1) % stamps.length;
        return true;
    }
}
```

**Cost.** Memory is O(limit) per key. For "100/min" that is 800 bytes per key, fine. For
"10,000/hour" across a million keys it is 80 GB, not fine. In Redis the log is a sorted set and
each entry costs far more than 8 bytes. Use the log when the limit is small and exactness matters
(login attempts, OTP sends, expensive operations).

A design choice: should **rejected** requests be logged? If yes, a client that keeps hammering
stays blocked until it backs off for a full window (a punitive limiter). If no (as above), the
client gets exactly `limit` per sliding window. Pick deliberately.

### 5. Sliding window counter

Keep only two fixed-window counters (current and previous) and **estimate** the count over the
sliding window by assuming the previous window's requests were spread evenly.

```text
                previous window                current window
        ├──────────────────────────────┼──────────────────────────────┤
                         ◄──────────── sliding window (60 s) ─────────►
                         │   overlap    │  elapsed  │
                         │  (1 - f)·W   │    f·W    │ now
```

Let `W` be the window length, `f` the fraction of the current window elapsed (0 to 1),
`p` the previous window's count and `c` the current window's count:

```text
estimate = p · (1 − f) + c
allow if   estimate + cost ≤ limit
```

**Worked example.** Limit 100/min. The previous minute had `p = 80`; the current minute has
`c = 50` so far; we are 15 s in, so `f = 0.25`.

```text
estimate = 80 · (1 − 0.25) + 50 = 60 + 50 = 110  > 100  → reject
```

At 30 s in, with no more requests: `80 · 0.5 + 50 = 90` → allow (10 more fit).

**How wrong can it be?** The estimate is exact only if the previous window's traffic was
uniform. Two extremes, limit 100/min:

- Previous window: all 100 requests at 12:00:59. At 12:01:01 (`f = 1/60`), estimate
  = 100 * 59/60 = 98.3, so 1 request is allowed although the true trailing-60 s count is 100.
  The estimator **over-admits** slightly.
- Previous window: all 100 at 12:00:00. At 12:01:30 the true trailing count is 0, but the
  estimate is 50, so only 50 are allowed. It **under-admits**.

In practice traffic is not this adversarial. Cloudflare reported in its write-up of this
approach that across 400 million requests only 0.003% were wrongly allowed or limited. It is the
best default for distributed HTTP limiting: two counters per key, O(1), no 2x boundary burst.

```java
import java.util.function.LongSupplier;

public final class SlidingWindowCounter implements RateLimiter {

    private final long limit;
    private final long windowNanos;
    private final LongSupplier clock;
    private long currentWindow = Long.MIN_VALUE; // guarded by this
    private long currentCount;                   // guarded by this
    private long previousCount;                  // guarded by this

    public SlidingWindowCounter(long limit, long windowNanos, LongSupplier clock) {
        this.limit = limit;
        this.windowNanos = windowNanos;
        this.clock = clock;
    }

    @Override
    public synchronized boolean tryAcquire(int permits) {
        long now = clock.getAsLong();
        long window = Math.floorDiv(now, windowNanos);
        if (window != currentWindow) {
            // If more than one full window passed, the previous window was empty.
            previousCount = (window == currentWindow + 1) ? currentCount : 0;
            currentCount = 0;
            currentWindow = window;
        }
        double elapsedFraction = (double) Math.floorMod(now, windowNanos) / windowNanos;
        double estimate = previousCount * (1.0 - elapsedFraction) + currentCount;
        if (estimate + permits > limit) {
            return false;
        }
        currentCount += permits;
        return true;
    }
}
```

### 6. GCRA (Generic Cell Rate Algorithm)

GCRA comes from ATM networks (ITU-T I.371) and is a leaky bucket meter expressed with **a single
timestamp** instead of a level plus a timestamp. Used by `redis-cell`, the `throttled` Go library,
and several CDNs.

Definitions for a rate of `r` requests per second with burst `B`:

- **Emission interval** `T = 1 / r`: ideal spacing between requests.
- **Burst capacity** `B · T`: how far ahead of schedule a client may run.
- **TAT** (theoretical arrival time): when the next request "should" arrive if the client had
  been perfectly paced. Initially "now".

On a request costing `n` at time `now`:

```text
newTAT = max(TAT, now) + n · T
allow  if newTAT − now ≤ B · T      → store TAT = newTAT
reject otherwise                    → retry after (newTAT − B·T) − now
```

```text
r = 1/s (T = 1 s), B = 3

t     TAT before   newTAT   newTAT − t   ≤ 3 ?   result   TAT after
0.0      0.0         1.0       1.0        yes      A        1.0
0.0      1.0         2.0       2.0        yes      A        2.0
0.0      2.0         3.0       3.0        yes      A        3.0
0.0      3.0         4.0       4.0        no       R        3.0   (retry in 1.0 s)
1.0      3.0         4.0       3.0        yes      A        4.0
5.0      4.0         6.0       1.0        yes      A        6.0   (idle → max(TAT, now))
```

Why people like it: **one integer of state**, no floating-point refill math, the retry time falls
out directly, and the update is a single CAS. Behaviourally it equals a token bucket of capacity
`B` refilled at `r`.

```java
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.LongSupplier;

public final class Gcra implements RateLimiter {

    private final long emissionNanos;   // T
    private final long burstNanos;      // B * T
    private final LongSupplier clock;
    private final AtomicLong tat = new AtomicLong(Long.MIN_VALUE);

    public Gcra(double ratePerSecond, int burst, LongSupplier clock) {
        this.emissionNanos = (long) (1_000_000_000L / ratePerSecond);
        this.burstNanos = emissionNanos * burst;
        this.clock = clock;
    }

    @Override
    public boolean tryAcquire(int permits) {
        while (true) {
            long now = clock.getAsLong();
            long current = tat.get();
            long newTat = Math.max(current, now) + emissionNanos * permits;
            if (newTat - now > burstNanos) {
                return false;
            }
            if (tat.compareAndSet(current, newTat)) {
                return true;
            }
        }
    }

    /** How long a rejected caller should wait before {@code permits} would be allowed. */
    public long retryAfterNanos(int permits) {
        long now = clock.getAsLong();
        long newTat = Math.max(tat.get(), now) + emissionNanos * permits;
        return Math.max(0, newTat - burstNanos - now);
    }
}
```

### Comparison

| Algorithm | State per key | Bursts | Accuracy | Distributed cost | Typical use |
| --- | --- | --- | --- | --- | --- |
| Token bucket | 2 numbers | Up to capacity, configurable | Exact for its model | Lua script (read-modify-write) | API limits with bursts; default choice |
| Leaky bucket (queue) | Queue of requests | Absorbed, output smooth | Exact | Hard (needs a queue) | Outbound shaping to strict partners |
| Leaky bucket (meter) | 2 numbers | Up to capacity | Exact | Lua script | Same as token bucket |
| Fixed window | 1 counter | Up to 2x at boundaries | Weak at edges | One `INCR` | Quotas (per day/month), cheap coarse limits |
| Sliding window log | 1 timestamp per request | None | Exact | Sorted set, O(log n) | Small, strict limits (logins, OTP) |
| Sliding window counter | 2 counters | Small, approximate | Very good | Two `GET`s + `INCR` | Distributed per-minute limits at scale |
| GCRA | 1 timestamp | Up to B | Exact (= token bucket) | Lua script, tiny state | High-scale limiters, CDNs, `redis-cell` |

How to choose in an interview: start with **token bucket** (or GCRA) when bursts are acceptable
and you want a `Retry-After`; **sliding window counter** when the requirement is literally
"N per minute" at scale; **sliding log** when the limit is small and must be exact; **fixed
window** for quotas; **leaky bucket queue** when you must shape outbound traffic.

---

## Distributed rate limiting with Redis

With one process, an in-memory limiter is exact. With 50 gateway instances behind a load
balancer, a client's requests are spread across all of them; per-instance limits of `L` would let
the client do `50 · L`. The counters must be shared, or the limit must be split.

### The race you must avoid

Naive "read, decide, write" from application code:

```text
gateway A                    Redis (count = 99, limit 100)        gateway B
GET rl:42  ───────────────►  99
                             99  ◄─────────────────────────────── GET rl:42
99 < 100, allow
SET rl:42 100 ────────────►  100
                             100 ◄─────────────────────────────── SET rl:42 100
                                                                  99 < 100, allow
                     two requests allowed, counter says 100: one was lost
```

Fixes, in order of preference:

1. **Atomic primitive.** `INCR` returns the new value atomically. For fixed windows,
   `INCR` then compare is race-free.
2. **Lua script** (`EVAL` / `EVALSHA`, or Redis 7 Functions). Redis runs a script atomically:
   no other command interleaves. All read-modify-write algorithms (token bucket, sliding log,
   GCRA) should be a single script.
3. **`WATCH`/`MULTI`/`EXEC`** optimistic transactions work but retry under contention, which is
   exactly when limiters are busiest. Avoid.

A second classic bug: `INCR` followed by a separate `EXPIRE` from the client. If the process dies
in between, the key never expires and the user is limited forever. Put both in one script, or use
`SET key 0 EX 60 NX` before `INCR`, or Redis 7's `EXPIRE key 60 NX` (only set the TTL if none
exists).

### Fixed window in Lua

```lua
-- KEYS[1] = counter key, e.g. "rl:{api_key:abc}:1793448000" (window start in the key)
-- ARGV[1] = limit, ARGV[2] = window length in seconds, ARGV[3] = cost
local limit  = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local cost   = tonumber(ARGV[3])

local count = redis.call('INCRBY', KEYS[1], cost)
if count == cost then
  redis.call('EXPIRE', KEYS[1], window)
end
if count > limit then
  return {0, 0, redis.call('TTL', KEYS[1])}
end
return {1, limit - count, redis.call('TTL', KEYS[1])}
```

Note that rejected requests are still counted here; a client that keeps hammering burns its own
window. If you do not want that, `DECRBY` on rejection or read first and increment only on allow.

### Token bucket in Lua

```lua
-- KEYS[1] = bucket hash
-- ARGV[1] = capacity, ARGV[2] = refill tokens per second, ARGV[3] = cost
-- Returns {allowed (0/1), remaining tokens (floor), retry_after_ms}
local capacity = tonumber(ARGV[1])
local rate     = tonumber(ARGV[2])
local cost     = tonumber(ARGV[3])

local t   = redis.call('TIME')                     -- {seconds, microseconds}
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)   -- ms

local data   = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts     = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  ts = now
end

local elapsed = math.max(0, now - ts)
tokens = math.min(capacity, tokens + elapsed * rate / 1000)

local allowed = 0
local retry_ms = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
else
  retry_ms = math.ceil((cost - tokens) * 1000 / rate)
end

redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
-- Expire once the bucket would be full again; an absent key means "full".
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity * 1000 / rate) + 1000)

return {allowed, math.floor(tokens), retry_ms}
```

Notes on correctness:

- **`redis.call('TIME')` inside a script** is allowed since Redis 5 because scripts are
  replicated by their effects (the resulting `HSET`), not re-executed on replicas. Redis 7
  removed the old verbatim replication mode entirely. On older versions you had to call
  `redis.replicate_commands()` first.
- **Numbers**: Redis converts a Lua number in a *reply* to an integer (truncating), so return
  `math.floor(tokens)` or a string; pass fractional values to `HSET` with `tostring`.
- **TTL** lets idle buckets disappear, which keeps memory proportional to active keys.

Calling it from Java with Lettuce:

```java
import io.lettuce.core.ScriptOutputType;
import io.lettuce.core.api.sync.RedisCommands;
import java.util.List;

public final class RedisTokenBucket {

    public record Decision(boolean allowed, long remaining, long retryAfterMillis) {}

    private final RedisCommands<String, String> redis;
    private final String sha;

    public RedisTokenBucket(RedisCommands<String, String> redis, String luaSource) {
        this.redis = redis;
        this.sha = redis.scriptLoad(luaSource); // cache script server-side, call by SHA
    }

    public Decision tryAcquire(String key, long capacity, double ratePerSecond, int cost) {
        List<Long> r = redis.evalsha(sha, ScriptOutputType.MULTI, new String[] {key},
                Long.toString(capacity), Double.toString(ratePerSecond), Integer.toString(cost));
        return new Decision(r.get(0) == 1L, r.get(1), r.get(2));
    }
}
```

If Redis restarts and loses its script cache, `EVALSHA` fails with `NOSCRIPT`; production
clients catch that and fall back to `EVAL` (many client libraries do this for you).

### Sliding window log with a sorted set

Each accepted request is a member scored by its timestamp. Trim, count, maybe add, all in one
script.

```lua
-- KEYS[1] = sorted set key
-- ARGV[1] = window in microseconds, ARGV[2] = limit, ARGV[3] = unique request id
local window = tonumber(ARGV[1])
local limit  = tonumber(ARGV[2])

local t   = redis.call('TIME')
local now = tonumber(t[1]) * 1000000 + tonumber(t[2])          -- microseconds

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)   -- drop old entries
local count = redis.call('ZCARD', KEYS[1])

if count < limit then
  redis.call('ZADD', KEYS[1], now, ARGV[3])
  redis.call('PEXPIRE', KEYS[1], math.ceil(window / 1000))
  return {1, limit - count - 1, 0}
end

local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
local retry_us = tonumber(oldest[2]) + window - now
return {0, 0, math.ceil(retry_us / 1000)}                       -- retry in ms
```

- Members must be **unique** (a UUID or `timestamp-counter`); two requests in the same
  microsecond with the same member would collapse into one entry.
- Microsecond timestamps around 1.8e15 fit exactly in a double (< 2^53), which is what sorted
  set scores are.
- Cost: `ZREMRANGEBYSCORE` is O(log n + m), `ZADD` O(log n). Memory is roughly 50 to 100 bytes
  per entry, so this is for small limits only.

### Sliding window counter in Redis

Two plain counters, named by window index, read and incremented in one script:

```lua
-- KEYS[1] = current window key, KEYS[2] = previous window key
-- ARGV[1] = limit, ARGV[2] = window seconds, ARGV[3] = elapsed fraction (0..1), ARGV[4] = cost
local limit   = tonumber(ARGV[1])
local window  = tonumber(ARGV[2])
local f       = tonumber(ARGV[3])
local cost    = tonumber(ARGV[4])

local cur  = tonumber(redis.call('GET', KEYS[1]) or '0')
local prev = tonumber(redis.call('GET', KEYS[2]) or '0')
local estimate = prev * (1 - f) + cur

if estimate + cost > limit then
  return 0
end
redis.call('INCRBY', KEYS[1], cost)
redis.call('EXPIRE', KEYS[1], window * 2)   -- must survive as "previous" for one more window
return 1
```

Here the client computes the window index and `f` from its own clock, which brings up clock skew.

### Clock issues

- **Client-supplied time.** If each gateway passes `now`, gateways with skewed clocks disagree
  about which window is current or how many tokens have refilled. A gateway 2 s ahead refills
  buckets early for everyone it serves.
- **Use one clock.** Calling `TIME` inside the script makes the Redis primary for that key the
  single source of time. In Redis Cluster each key lives on exactly one primary, so per-key
  decisions are consistent. After a failover, the new primary's clock may differ slightly; a
  small refill jump or window misalignment is the usual consequence and is acceptable.
- **Monotonic vs wall clock.** Redis `TIME` is wall-clock. A backwards NTP step makes `elapsed`
  negative, which is why the scripts clamp with `math.max(0, ...)`.
- **Window keys** derived from wall time (`floor(epoch / 60)`) are fine with modest skew; at
  boundaries, a skewed node may count into the neighbouring window for a moment.

### Redis Cluster: key hashing and hot keys

Redis Cluster shards keys across 16,384 hash slots by `CRC16(key) mod 16384`. A script may only
touch keys in **one slot**, and all keys must be passed in `KEYS` (not built inside the script),
otherwise you get a `CROSSSLOT` error or undefined behaviour.

Use **hash tags**: only the part between the first `{` and `}` is hashed.

```text
rl:{tenant:42}:minute:29890800       ─┐
rl:{tenant:42}:minute:29890799        ├─ all hash "tenant:42" → same slot → one script
rl:{tenant:42}:day:20757             ─┘
rl:{tenant:43}:minute:29890800       ── different slot, possibly a different node
```

Hash tags enable multi-key scripts per principal (current + previous window, per-second +
per-day limits) while still spreading different principals across the cluster.

**Hot keys.** A single huge tenant or a global limit ("total 50k req/s to the payments
provider") puts every request on one shard. Options:

- **Split the key** into `k` sub-keys (`{global:0}` ... `{global:15}`), each with limit `L / k`,
  and pick one randomly per request. Accuracy drops slightly when traffic is uneven.
- **Local pre-aggregation**: each gateway batches its increments (see hybrid below).
- **Replicas for reads**: the sliding window counter's previous-window `GET` can be served from a
  replica if you accept staleness.

GitHub described sharding and replicating its rate limiter Redis to remove exactly these hot
spots ("How we scaled the GitHub API with a sharded, replicated rate limiter in Redis").

### Local + global hybrid

A round trip to Redis adds roughly 0.2 to 1 ms inside a data centre, plus a dependency. At very
high request rates that is too much, so systems mix local and global state.

| Strategy | How it works | Accuracy | Latency | When |
| --- | --- | --- | --- | --- |
| Pure global | Every request runs the Redis script | Exact (within one key's shard) | +1 RTT | Low-to-medium RPS, strict limits |
| Static split | Each of `N` nodes enforces `L / N` locally | Bad if load is uneven or `N` changes | None | Gateways with sticky or even traffic |
| Token leasing | Node takes a batch of `b` tokens from Redis, spends locally, refills when empty | Over-reserves up to `N · b` | Amortised | High RPS per key |
| Async sync | Node counts locally, pushes deltas every 100 ms, pulls the global total | Overshoot ≈ rate x sync interval x N | None | Very high RPS, soft limits |
| Local first, global second | A local limiter at e.g. 2x the fair share rejects floods before Redis; Redis enforces the real limit | Exact globally, cheap under attack | +1 RTT for allowed traffic | Most production gateways |

Envoy follows the last pattern: its local rate limit filter (token bucket per instance) sheds
floods cheaply, and the global rate limit filter consults a central service for the real limit.

The async approach's error is easy to estimate: with 20 nodes syncing every 100 ms and a client
doing 1,000 req/s, the system can be up to about 100 requests (one interval's worth across all
nodes) behind reality. For "1,000 per minute" that is a 10% overshoot in the worst case; for
abuse protection that is fine, for billing quotas it is not.

### Consistency vs latency

A rate limiter is a rare place where **approximate is usually correct**. The point is to protect
the system and be roughly fair, not to count perfectly.

- Allowing 1,003 instead of 1,000 requests is harmless. Adding 1 ms to every request and making
  Redis a hard dependency of every API call is not.
- Billing quotas and anything with money attached (free trial credits, paid API calls) need
  exactness: use a durable store with transactions, and reconcile asynchronously.
- Security limits (login attempts) need **low false negatives**: an attacker must not get 10x the
  attempts by spreading over nodes, so use the global store.

---

## Designing a rate limiter service end to end

This is a common system design interview question. A strong answer walks through requirements,
API, rules, algorithm, storage, architecture, failure handling, and observability.

### Requirements

Functional:

- Limit requests by configurable keys: API key, user, IP, tenant, endpoint, or combinations.
- Multiple rules per request (per second and per day; per user and per tenant).
- Rules change without redeploying (minutes, not hours).
- Tell clients their remaining budget and when to retry.
- Dry-run ("shadow") mode for new rules.

Non-functional:

- **Low latency**: p99 added latency under ~2 ms; the limiter must not become the bottleneck.
- **High availability**: limiter failure must not take the API down (usually fail open).
- **Scale**: e.g. 1M req/s peak across 100 gateway nodes, 10M active keys.
- **Accuracy**: approximate is fine for abuse limits (within a few percent).

Back-of-envelope: 10M active keys at ~100 bytes each (key name, hash with two fields, TTL
overhead) is about 1 GB, comfortably sharded over a small Redis Cluster. 1M req/s of Lua scripts
at roughly 100k simple ops/s per Redis primary means on the order of 10 to 20 primaries before
any local pre-filtering, which is a strong argument for the local-first hybrid.

### Rules configuration

Rules are data, not code. Store them in a config repo or a rules service, push to gateways, and
hot-reload. An Envoy-ratelimit-inspired format:

```yaml
domain: public_api
rules:
  - name: anonymous_per_ip
    match:
      authenticated: false
    key: [remote_ip]
    algorithm: sliding_window_counter
    limit: 60
    window: 1m

  - name: login_attempts
    match:
      path: /v1/login
      method: POST
    key: [remote_ip, body.username]
    algorithm: sliding_window_log
    limit: 5
    window: 15m
    shadow: false

  - name: plan_burst
    match:
      authenticated: true
    key: [api_key]
    algorithm: token_bucket
    by_plan:
      free:       { capacity: 20,  refill_per_second: 1 }
      pro:        { capacity: 200, refill_per_second: 20 }
      enterprise: { capacity: 2000, refill_per_second: 200 }

  - name: exports_cost
    match:
      path: /v1/exports
      method: POST
    key: [tenant_id]
    algorithm: token_bucket
    cost: 50
    capacity: 500
    refill_per_second: 1

  - name: tenant_daily_quota
    match:
      authenticated: true
    key: [tenant_id]
    algorithm: fixed_window
    limit: 1000000
    window: 1d
    shadow: true          # log would-be rejections, do not enforce yet
```

Design points: rules are matched most specific first; every matching rule must pass; a rule has
a stable `name` used in metrics and in the `RateLimit-Policy` header; `shadow: true` lets you
measure impact before enforcing.

### Architecture

```text
                     ┌──────────────────────┐
  clients ─────────► │   Load balancer /    │
                     │   CDN edge (L3-L7)   │
                     └──────────┬───────────┘
                                │
                     ┌──────────▼───────────┐       ┌──────────────────┐
                     │     API gateway      │──────►│ Rate limit svc   │
                     │  (local limiter +    │◄──────│ (stateless, gRPC)│
                     │   RL client)         │       └────────┬─────────┘
                     └──────────┬───────────┘                │
                                │                   ┌────────▼─────────┐
                     ┌──────────▼───────────┐       │  Redis Cluster   │
                     │  Backend services    │       │  (counters)      │
                     └──────────────────────┘       └──────────────────┘

  rules repo ──► config pipeline ──► pushed to gateways and RL service (hot reload)
  metrics    ◄── every decision: rule, key class, allowed/limited/shadow, latency
```

Request flow:

1. The gateway authenticates the request and extracts descriptors (API key, tenant, route, IP).
2. The local limiter (generous token bucket per key) rejects obvious floods without a network
   call.
3. The gateway calls the rate limit service (or runs the Redis script directly; a separate
   service centralises rules and connection pooling, a library avoids a hop) with all matching
   rules. One call covers all rules for the request.
4. The service runs one Lua script per hash-tagged principal, so all rules for a user are
   evaluated atomically on one shard.
5. On allow, the gateway forwards with headers; on limit, it returns 429 with headers and does not
   touch the backend.
6. Timeouts on the limiter call are short (e.g. 5 to 20 ms). On timeout or error, apply the
   rule's failure policy (fail open by default, local-only fallback).

**Multiple rules and wasted tokens.** If rule A (per-second) consumes a token and then rule B
(per-day) rejects, A's token was spent on a rejected request. Either evaluate all rules in one
script that checks everything first and only then consumes, or accept the small over-count.
Checking first is easy when all keys share a hash tag; across shards you accept the imprecision.

### Response headers

Clients behave better when told their budget. There are three generations of headers in the
wild:

```text
# 1. De facto legacy (GitHub, Twitter/X, many others): X- prefixed, reset as epoch seconds
X-RateLimit-Limit: 5000
X-RateLimit-Remaining: 4987
X-RateLimit-Reset: 1793448000

# 2. Earlier IETF drafts (draft-ietf-httpapi-ratelimit-headers up to about -07)
RateLimit-Limit: 100
RateLimit-Remaining: 42
RateLimit-Reset: 30                 # delta seconds, not a timestamp
RateLimit-Policy: 100;w=60

# 3. Later drafts (-08 onward): structured fields, named policies
RateLimit-Policy: "burst";q=100;w=60, "daily";q=10000;w=86400
RateLimit: "burst";r=42;t=30
```

In the later drafts `q` is the quota, `w` the window in seconds, `r` the remaining quota and `t`
the seconds until it resets. The draft was still not an RFC at the time of writing and its syntax
has changed between versions, so check the current draft before implementing, and keep sending
whatever your existing clients already parse.

A rejected request:

```text
HTTP/1.1 429 Too Many Requests
Content-Type: application/problem+json
Retry-After: 30
RateLimit-Policy: "burst";q=100;w=60
RateLimit: "burst";r=0;t=30

{
  "type": "https://api.example.com/problems/rate-limited",
  "title": "Too many requests",
  "status": 429,
  "detail": "Rule 'plan_burst' allows 100 requests per 60 seconds for the free plan.",
  "retryAfterSeconds": 30
}
```

- **Reset as delta seconds** avoids depending on the client's clock; epoch timestamps break when
  the client's clock is wrong.
- **`Retry-After`** (RFC 9110) is the one header every HTTP client library understands. It is
  either delta seconds or an HTTP-date.
- Do not leak other tenants' information in limit headers for shared/global rules.

### 429 vs 503

| | 429 Too Many Requests | 503 Service Unavailable |
| --- | --- | --- |
| Defined in | RFC 6585 | RFC 9110 |
| Meaning | **You** sent too much | **I** cannot serve right now |
| Cause | Caller exceeded its rate limit or quota | Overload, load shedding, maintenance, dependency down |
| Client action | Slow down; respect `Retry-After` | Retry later with backoff, possibly another region |
| Retry safety | Retrying immediately makes it worse | Retrying immediately makes it worse too |
| Monitoring | Expected and normal; alert on spikes for a single key | Error; counts against your availability SLO |

Nginx `limit_req` returns **503** by default; set `limit_req_status 429;` to be explicit. Some
APIs return 403 for abuse blocks (permanent bans) to distinguish them from temporary limits.

---

## Multi-dimensional limits

Real APIs layer several limits; a request must pass all of them.

| Dimension | Protects against | Example |
| --- | --- | --- |
| Per IP | Anonymous abuse, scraping, credential stuffing | 60/min unauthenticated |
| Per user / account | One account abusing an endpoint | 5 password resets/hour |
| Per API key | A single integration misbehaving | 100 req/s per key |
| Per tenant / organisation | One customer starving others | 10k req/s per org across all keys |
| Per endpoint / operation | Expensive operations | 2 exports/min, search 10/s |
| Per tier / plan | Monetisation | Free 60/min, Pro 1,000/min |
| Global per downstream | Protecting a partner or shared resource | 500 req/s to the SMS gateway total |

Points that come up in practice:

- **IP is a weak identity.** Carrier-grade NAT and corporate proxies put thousands of users behind
  one IPv4 address; IPv6 gives one user billions of addresses. Limit IPv6 by prefix (/64 or
  /56), and treat IP limits as coarse abuse protection only.
- **Trust the right IP.** Behind a load balancer, use the client IP from `X-Forwarded-For` only
  as set by your own trusted proxies (take the right-most untrusted entry), never the left-most
  value a client can forge.
- **Weighted cost.** Charge tokens proportional to work: a GraphQL query's computed complexity,
  bytes uploaded, rows exported, LLM tokens.
- **Hierarchical limits.** A key's limit nests inside its tenant's limit, which nests inside the
  global limit. Check from the most specific to the most general.

### Quotas vs rate limits

| | Rate limit | Quota |
| --- | --- | --- |
| Period | Seconds to minutes | Day, month, billing cycle |
| Purpose | Protect the system, smooth traffic | Business entitlement, billing |
| Accuracy needed | Approximate is fine | Must be exact and auditable |
| Storage | Redis, in-memory, TTL'd | Durable DB, often with event log |
| Reset | Continuous or rolling | Calendar-aligned (1st of month, UTC) |
| On exceed | 429, retry soon | 429 or 402/403, upgrade plan; may be a soft overage |

Implement quotas as a usage ledger (append usage events, aggregate), with a fast cached
"remaining" counter in front that is periodically reconciled.

### Concurrency limits

A rate limit bounds **arrivals per second**; a concurrency limit bounds **requests in flight**.
They protect different things. A request rate of 100/s is harmless at 10 ms latency (1 in
flight on average) and deadly at 10 s latency (1,000 in flight) because of **Little's law**:

```text
in-flight requests (L) = arrival rate (λ) × time in system (W)
100 req/s × 0.010 s  =   1 concurrent
100 req/s × 10 s     = 1,000 concurrent  → thread pools and memory exhausted
```

When a dependency slows down, a pure rate limiter keeps admitting the same rate while in-flight
work explodes. A concurrency limit adapts automatically: slow requests hold permits longer, so
fewer are admitted.

```java
import java.util.concurrent.Semaphore;
import java.util.function.Supplier;

public final class ConcurrencyLimiter {

    private final Semaphore permits;

    public ConcurrencyLimiter(int maxInFlight) {
        this.permits = new Semaphore(maxInFlight);
    }

    public <T> T call(Supplier<T> work, Supplier<T> onRejected) {
        if (!permits.tryAcquire()) {
            return onRejected.get();          // shed immediately; do not queue
        }
        try {
            return work.get();
        } finally {
            permits.release();
        }
    }
}
```

Stripe runs a **concurrent requests limiter** alongside its request rate limiter for exactly this
reason (see below). Nginx has `limit_conn`; Resilience4j has `Bulkhead`.

---

## Adaptive limiting and load shedding

Static limits require you to know capacity in advance, and capacity changes with deploys, cache
hit rates, noisy neighbours, and downstream health. Adaptive limiters discover the limit from
observed behaviour.

### AIMD

**Additive increase, multiplicative decrease**, the same idea as TCP congestion control:

- On success (and latency below a target), increase the limit slowly: `limit += 1` (or
  `+= 1/limit` per request, which is roughly +1 per round trip).
- On a drop, timeout, or latency above target, cut sharply: `limit *= 0.9` (or 0.5).

This converges toward the largest concurrency the system sustains and backs off fast when it
degrades. A small, thread-safe version:

```java
public final class AimdConcurrencyLimiter {

    private final int minLimit;
    private final int maxLimit;
    private final double backoffRatio;
    private final long latencyTargetNanos;
    private double limit;     // guarded by this
    private int inFlight;     // guarded by this

    public AimdConcurrencyLimiter(int initial, int min, int max,
                                  double backoffRatio, long latencyTargetNanos) {
        this.limit = initial;
        this.minLimit = min;
        this.maxLimit = max;
        this.backoffRatio = backoffRatio;
        this.latencyTargetNanos = latencyTargetNanos;
    }

    /** Returns false if the caller should be shed. */
    public synchronized boolean tryAcquire() {
        if (inFlight >= (int) limit) {
            return false;
        }
        inFlight++;
        return true;
    }

    public synchronized void onComplete(long latencyNanos, boolean failedOrTimedOut) {
        inFlight--;
        if (failedOrTimedOut || latencyNanos > latencyTargetNanos) {
            limit = Math.max(minLimit, limit * backoffRatio);
        } else if (inFlight >= limit / 2) {   // only grow when the limit is actually in use
            limit = Math.min(maxLimit, limit + 1.0 / limit);
        }
    }

    public synchronized int currentLimit() {
        return (int) limit;
    }
}
```

The "only grow when in use" guard prevents the limit from drifting to the maximum during quiet
periods, where it would then be wildly wrong when traffic returns.

### Netflix concurrency-limits

Netflix's open-source `concurrency-limits` library (Java) implements this idea with several
algorithms: `AIMDLimit`, `VegasLimit` (based on TCP Vegas: compares measured RTT with the minimum
observed RTT to estimate queueing), and `GradientLimit` / `Gradient2Limit` (adjusts the limit by
the ratio of long-term to short-term latency). It ships servlet filters and gRPC interceptors.
The core usage pattern is: acquire a listener, then report the outcome.

```java
import com.netflix.concurrency.limits.Limiter;
import com.netflix.concurrency.limits.limit.Gradient2Limit;
import com.netflix.concurrency.limits.limiter.SimpleLimiter;
import java.util.Optional;

Limiter<Void> limiter = SimpleLimiter.newBuilder()
        .limit(Gradient2Limit.newBuilder().build())
        .build();

Optional<Limiter.Listener> listener = limiter.acquire(null);
if (listener.isEmpty()) {
    // shed: return 503 / RESOURCE_EXHAUSTED immediately
} else {
    try {
        handle(request);
        listener.get().onSuccess();
    } catch (TimeoutException e) {
        listener.get().onDropped();     // treated as an overload signal
    } catch (Exception e) {
        listener.get().onIgnore();      // not a capacity signal; do not affect the limit
    }
}
```

Builder options and class names have moved between releases; check the project's README for the
version you use.

### Load shedding

When the server is overloaded, shedding some requests quickly is better than serving all of them
slowly (which, through timeouts and retries, ends with serving none). Techniques:

- **Prioritise.** Shed by criticality: background jobs and prefetches before user-facing reads,
  reads before checkouts. Stripe and Google both describe tiered criticality for this.
- **Shed early and cheaply.** Reject before parsing the body or touching the DB.
- **Bounded queues with deadlines.** A request that has waited longer than the client's timeout
  is already lost; drop it instead of processing it (CoDel-style queue management).
- **LIFO under overload.** Serving the newest requests first gives some users a fast response
  instead of giving everyone a timeout.
- **Return 503 with `Retry-After`**, and make sure your own clients back off on it.

Stripe's 2017 post "Scaling your API with rate limiters" describes four layers in production:
a request rate limiter (token bucket per user), a concurrent requests limiter, a fleet usage load
shedder that reserves capacity for critical requests, and a worker utilisation load shedder that
sheds lower-priority traffic first when workers are saturated.

---

## Client-side retries and backoff

A limiter only works if clients slow down. Naive clients that retry immediately turn a brief
limit into a **retry storm**.

- Retry only on retryable conditions: 429, 503, 502/504, connection resets, timeouts on
  idempotent requests.
- **Honour `Retry-After`** when present; it beats any guess.
- Otherwise use **exponential backoff with jitter** and a cap on both delay and attempts.
- Use a **retry budget** (e.g. retries may add at most 10% extra load) so a fleet of clients cannot
  multiply traffic during an incident.

Jitter variants from Marc Brooker's AWS Architecture Blog post "Exponential Backoff And Jitter":

```text
exponential:   sleep = min(cap, base · 2^attempt)                    (synchronised herds)
full jitter:   sleep = random(0, min(cap, base · 2^attempt))         (recommended default)
equal jitter:  t = min(cap, base · 2^attempt); sleep = t/2 + random(0, t/2)
decorrelated:  sleep = min(cap, random(base, previous_sleep · 3))
```

Without jitter, every client throttled at the same moment retries at the same moment.

```java
import java.io.IOException;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.time.ZonedDateTime;
import java.time.format.DateTimeFormatter;
import java.time.format.DateTimeParseException;
import java.util.Optional;
import java.util.concurrent.ThreadLocalRandom;

public final class RetryingClient {

    private static final Duration BASE = Duration.ofMillis(200);
    private static final Duration CAP = Duration.ofSeconds(20);
    private static final int MAX_ATTEMPTS = 5;

    private final HttpClient http = HttpClient.newHttpClient();

    public HttpResponse<String> send(HttpRequest request) throws IOException, InterruptedException {
        for (int attempt = 0; ; attempt++) {
            HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
            int status = response.statusCode();
            boolean retryable = status == 429 || status == 503;
            if (!retryable || attempt == MAX_ATTEMPTS - 1) {
                return response;
            }
            Duration delay = retryAfter(response).orElseGet(() -> fullJitter(attempt));
            Thread.sleep(Math.min(delay.toMillis(), CAP.toMillis()));
        }
    }

    private static Duration fullJitter(int attempt) {
        long ceiling = Math.min(CAP.toMillis(), BASE.toMillis() << Math.min(attempt, 20));
        return Duration.ofMillis(ThreadLocalRandom.current().nextLong(ceiling + 1));
    }

    /** Retry-After is either delta-seconds or an HTTP-date (RFC 9110). */
    private static Optional<Duration> retryAfter(HttpResponse<?> response) {
        return response.headers().firstValue("Retry-After").map(String::trim).flatMap(v -> {
            try {
                return Optional.of(Duration.ofSeconds(Long.parseLong(v)));
            } catch (NumberFormatException notSeconds) {
                try {
                    ZonedDateTime at = ZonedDateTime.parse(v, DateTimeFormatter.RFC_1123_DATE_TIME);
                    Duration d = Duration.between(ZonedDateTime.now(at.getZone()), at);
                    return Optional.of(d.isNegative() ? Duration.ZERO : d);
                } catch (DateTimeParseException unparseable) {
                    return Optional.empty();
                }
            }
        });
    }
}
```

For outbound calls to a rate-limited partner, the best client-side tool is a **local limiter in
front of the client** (a token bucket or leaky bucket queue tuned slightly below the partner's
published limit), so you rarely see a 429 at all.

---

## Real implementations

### Nginx `limit_req` and `limit_conn`

Nginx implements a leaky bucket with millisecond granularity. State lives in a shared memory zone
across worker processes on one host (not across hosts).

```nginx
http {
    # 10 MB zone keyed by the binary client IP, 10 requests per second.
    limit_req_zone $binary_remote_addr zone=per_ip:10m rate=10r/s;
    limit_req_zone $http_x_api_key     zone=per_key:20m rate=100r/s;
    limit_conn_zone $binary_remote_addr zone=conn_ip:10m;

    limit_req_status  429;   # default is 503
    limit_conn_status 429;

    server {
        location /api/ {
            limit_req  zone=per_ip  burst=20 nodelay;
            limit_req  zone=per_key burst=200 delay=50;
            limit_conn conn_ip 20;
            proxy_pass http://backend;
        }

        location = /login {
            limit_req zone=per_ip burst=5;     # queued and delayed, never instant bursts
            proxy_pass http://backend;
        }
    }
}
```

How `rate`, `burst`, `nodelay`, and `delay` interact (rate 10r/s means one request per 100 ms):

| Configuration | Behaviour when 25 requests arrive at once |
| --- | --- |
| No `burst` | 1 forwarded; the other 24 rejected (each needs its own 100 ms slot) |
| `burst=20` | 1 forwarded now, 20 queued and released one per 100 ms (last waits ~2 s), 4 rejected |
| `burst=20 nodelay` | 21 forwarded immediately, 4 rejected; the 20 burst slots free up at 10/s |
| `burst=20 delay=8` | 9 forwarded immediately, 12 queued and delayed, 4 rejected |

- `nodelay` is what most APIs want: allow a burst, but do not add latency.
- One megabyte of zone holds about 16,000 states for `$binary_remote_addr` (per the Nginx docs),
  so `10m` handles about 160,000 IPs; when the zone is full, Nginx evicts old entries and, if it
  still cannot allocate, rejects.
- `limit_req_dry_run on;` (Nginx 1.17.1+) logs would-be rejections without enforcing them.
- An empty key value (e.g. missing `X-API-Key`) is not limited by that zone; combine with an IP
  zone.

### Envoy local and global rate limiting

**Local** (`envoy.filters.http.local_ratelimit`): a token bucket per Envoy instance (optionally per
descriptor). No network call; used to shed floods.

```yaml
http_filters:
  - name: envoy.filters.http.local_ratelimit
    typed_config:
      "@type": type.googleapis.com/envoy.extensions.filters.http.local_ratelimit.v3.LocalRateLimit
      stat_prefix: http_local_rate_limiter
      token_bucket:
        max_tokens: 1000
        tokens_per_fill: 500
        fill_interval: 1s
      filter_enabled:
        runtime_key: local_rate_limit_enabled
        default_value: { numerator: 100, denominator: HUNDRED }
      filter_enforced:
        runtime_key: local_rate_limit_enforced
        default_value: { numerator: 100, denominator: HUNDRED }
```

`filter_enabled` controls whether the filter evaluates; `filter_enforced` controls whether it
actually rejects, which gives you a percentage-based shadow mode.

**Global** (`envoy.filters.http.ratelimit`): for each request Envoy builds **descriptors** from
route actions (remote address, a header, a generic key) and calls an external gRPC rate limit
service (`envoy.service.ratelimit.v3.RateLimitService`). The reference implementation,
`envoyproxy/ratelimit` (from Lyft), keeps counters in Redis using fixed windows.

```yaml
# Route-level: which descriptors to send
rate_limits:
  - actions:
      - request_headers:
          header_name: x-api-key
          descriptor_key: api_key
  - actions:
      - remote_address: {}
```

```yaml
# envoyproxy/ratelimit service config
domain: public_api
descriptors:
  - key: api_key
    rate_limit:
      unit: minute
      requests_per_unit: 1000
  - key: remote_address
    rate_limit:
      unit: second
      requests_per_unit: 20
```

The filter has a `failure_mode_deny` flag: `false` (the default) fails open when the rate limit
service is unreachable.

### Stripe's approach

From Stripe's engineering blog ("Scaling your API with rate limiters", 2017):

- Token buckets in Redis, per user, as the main **request rate limiter**.
- A **concurrent requests limiter** for expensive endpoints, which caught more real incidents
  than the rate limiter because overloads usually show up as long requests.
- A **fleet usage load shedder** that keeps a fraction of capacity for critical requests
  (e.g. creating charges) and sheds non-critical ones.
- A **worker utilisation load shedder** that sheds by priority when workers are busy.
- Practical advice: launch new limiters in dark (logging) mode first, make it easy to disable a
  limiter, and give users clear 429s with guidance to retry with backoff.

### Guava `RateLimiter`

In-process only. `SmoothBursty` stores up to one second of unused permits;
`SmoothWarmingUp` ramps up slowly after idle (useful for cold caches).

```java
import com.google.common.util.concurrent.RateLimiter;
import java.time.Duration;

RateLimiter limiter = RateLimiter.create(50.0);                     // 50 permits/second
RateLimiter warm = RateLimiter.create(50.0, Duration.ofSeconds(10)); // warm-up period

limiter.acquire();                         // blocks until a permit is available
boolean ok = limiter.tryAcquire();         // non-blocking
boolean okWithin = limiter.tryAcquire(Duration.ofMillis(100));
```

A distinctive behaviour: Guava lets a single large `acquire(n)` proceed immediately and makes the
**next** caller pay the wait ("the cost of a request is paid by the next request"). Ideal for
pacing your own outbound calls, not for multi-tenant server-side limiting (no per-key state, no
distribution). The class was long marked `@Beta`; check its status in your Guava version.

### Resilience4j `RateLimiter`

```java
import io.github.resilience4j.ratelimiter.RateLimiter;
import io.github.resilience4j.ratelimiter.RateLimiterConfig;
import io.github.resilience4j.ratelimiter.RequestNotPermitted;
import java.time.Duration;
import java.util.function.Supplier;

RateLimiterConfig config = RateLimiterConfig.custom()
        .limitForPeriod(100)                          // permits per cycle
        .limitRefreshPeriod(Duration.ofSeconds(1))    // cycle length
        .timeoutDuration(Duration.ofMillis(25))       // how long a caller may wait
        .build();

RateLimiter limiter = RateLimiter.of("partner-api", config);
Supplier<Quote> guarded = RateLimiter.decorateSupplier(limiter, partnerClient::fetchQuote);

try {
    Quote q = guarded.get();
} catch (RequestNotPermitted e) {
    // limit hit and could not get a permit within timeoutDuration
}
```

The default `AtomicRateLimiter` divides time into cycles of `limitRefreshPeriod` and resets
permissions at each cycle start, which is effectively a **fixed window** (with the boundary burst).
It composes with Resilience4j's retry, circuit breaker, and bulkhead, and has Spring Boot
configuration support. In-process only.

### Bucket4j

A Java token bucket library that supports **distributed** buckets through `ProxyManager`
backends (JCache providers like Hazelcast and Infinispan, Redis via Lettuce/Redisson/Jedis,
and relational databases).

```java
import io.github.bucket4j.Bucket;
import io.github.bucket4j.ConsumptionProbe;
import java.time.Duration;

Bucket bucket = Bucket.builder()
        .addLimit(limit -> limit.capacity(100).refillGreedy(100, Duration.ofMinutes(1)))
        .addLimit(limit -> limit.capacity(10).refillIntervally(10, Duration.ofSeconds(1)))
        .build();

ConsumptionProbe probe = bucket.tryConsumeAndReturnRemaining(1);
if (probe.isConsumed()) {
    response.setHeader("X-RateLimit-Remaining", Long.toString(probe.getRemainingTokens()));
} else {
    long waitSeconds = Math.max(1, Duration.ofNanos(probe.getNanosToWaitForRefill()).toSeconds());
    response.setHeader("Retry-After", Long.toString(waitSeconds));
    response.setStatus(429);
}
```

`refillGreedy` adds tokens continuously; `refillIntervally` adds the whole batch at the end of
each period (fixed-window-like). Multiple limits on one bucket must all pass. The builder API
shown is the 8.x style; older releases used `Bandwidth.classic(...)` with `Refill.greedy(...)`.

| Library | Algorithm | Distributed | Best for |
| --- | --- | --- | --- |
| Guava `RateLimiter` | Smooth token bucket variant | No | Pacing your own outbound calls |
| Resilience4j `RateLimiter` | Fixed cycles | No | Protecting a dependency, with other resilience patterns |
| Bucket4j | Token bucket | Yes (JCache, Redis, DB) | Per-key server-side limits in Java |
| Netflix concurrency-limits | Adaptive concurrency | No (per instance) | Load shedding without hand-tuned limits |

---

## Fail-open vs fail-closed

When the limiter's store is unavailable or slow, you must decide what happens.

| | Fail open (allow) | Fail closed (reject) |
| --- | --- | --- |
| Availability | API stays up | API goes down with the limiter |
| Protection | Lost until recovery | Kept |
| Typical rules | General API rate limits | Login/OTP attempts, costly or fraud-prone operations, paid quotas |
| Mitigation | Fall back to a local in-memory limiter at the fair share | Fall back to a strict local limiter so some traffic still flows |

In practice:

- Set a **tight timeout** on the limiter call (single-digit milliseconds) and a **circuit
  breaker** so a dead Redis does not add 20 ms to every request.
- **Fail open per rule, not globally.** Mark security-sensitive rules `failure: closed`.
- **Fall back to local limits**, not to "unlimited". Each node enforcing `limit / N` (with `N`
  from service discovery) gives approximate protection while Redis is down.
- **Alert** on fail-open events; a silently failing limiter is discovered during the next attack.

---

## Testing and monitoring

### Testing

- **Inject the clock.** Every implementation above takes a `LongSupplier`, so tests advance time
  explicitly instead of sleeping:

```java
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import org.junit.jupiter.api.Test;

class TokenBucketTest {

    private final AtomicLong nanos = new AtomicLong();

    @Test
    void refillsContinuouslyAndCapsAtCapacity() {
        TokenBucket bucket = new TokenBucket(4, 1.0, nanos::get);
        for (int i = 0; i < 4; i++) {
            assertTrue(bucket.tryAcquire());
        }
        assertFalse(bucket.tryAcquire());

        nanos.addAndGet(1_000_000_000L);           // +1 s
        assertTrue(bucket.tryAcquire());
        assertFalse(bucket.tryAcquire());

        nanos.addAndGet(60_000_000_000L);          // idle 60 s, still capped at 4
        for (int i = 0; i < 4; i++) {
            assertTrue(bucket.tryAcquire());
        }
        assertFalse(bucket.tryAcquire());
    }

    @Test
    void neverOverAdmitsUnderContention() throws Exception {
        TokenBucket bucket = new TokenBucket(1_000, 0.000_001, nanos::get); // ~no refill
        AtomicInteger allowed = new AtomicInteger();
        CountDownLatch start = new CountDownLatch(1);
        try (ExecutorService pool = Executors.newFixedThreadPool(32)) {
            for (int t = 0; t < 32; t++) {
                pool.submit(() -> {
                    start.await();
                    for (int i = 0; i < 10_000; i++) {
                        if (bucket.tryAcquire()) {
                            allowed.incrementAndGet();
                        }
                    }
                    return null;
                });
            }
            start.countDown();
        }                                          // close() waits for all tasks (Java 19+)
        assertEquals(1_000, allowed.get());
    }
}
```

- **Property tests**: for any request sequence, a sliding log never admits more than `limit`
  within any window; a token bucket never admits more than `capacity + rate * t` over `t`.
- **Redis scripts**: run them against a real Redis in tests (Testcontainers), including
  concurrency from many connections and TTL expiry.
- **Boundary tests**: requests exactly at window edges, clock going backwards, cost larger than
  capacity, a key's first request.
- **Load tests** (k6, Gatling, JMeter) against the whole gateway: confirm added latency at p99
  and that limited traffic never reaches the backend.
- **Shadow mode in production**: log would-be rejections for a week before enforcing a new rule.

### Monitoring

| Metric | Why |
| --- | --- |
| Decisions per rule: allowed / limited / shadow-limited | Is a rule firing more than expected? |
| Top limited keys (sampled) | Distinguish one abuser from a broad misconfiguration |
| Limiter latency p50/p99 and timeouts | The limiter must not slow the API |
| Fail-open / fallback count | Protection silently disabled |
| Redis CPU, memory, ops/s per shard, hot keys | Capacity and sharding problems |
| 429 rate as seen by clients (per plan) | Customer-facing impact; limits too tight? |
| Backend load vs admitted rate | Are the limits actually protecting the backend? |

Alert on: fail-open events, limiter p99 above budget, a sudden jump in 429s across many keys
(misconfigured rule or bad deploy), and 429s for internal/critical callers.

---

## Common mistakes

- **Non-atomic read-modify-write** across instances (GET, decide, SET), or `INCR` with a separate
  `EXPIRE` that can be skipped.
- **Per-instance limits** behind a load balancer, silently multiplying the real limit by `N`.
- **Using wall-clock time** in-process, so NTP jumps reset or freeze limits.
- **Trusting `X-Forwarded-For`** as sent by the client, letting attackers pick their own key.
- **Only a rate limit, no concurrency limit**, so a slow dependency still exhausts threads.
- **Limiter as a hard dependency**: no timeout, no fallback, and Redis becomes a single point of
  failure for the whole API.
- **Synchronised resets and retries**: every client resets at :00 and retries without jitter.
- **Returning 500 or 503 for rate limits**, so clients cannot distinguish "slow down" from
  "broken", and the limit pollutes your error-rate SLO.
- **Counting rejected requests** without intending to, permanently locking out a retrying client.
- **Unbounded key cardinality**: keying on a raw header value an attacker controls creates
  millions of keys; normalise and cap.
- **Enforcing new rules without shadow mode** and discovering that your biggest customer's normal
  traffic exceeds them.

---

## Interview questions

1. **Design a rate limiter for a public API with 100 gateway nodes.**
   Identify keys and rules; local token bucket at each gateway to absorb floods; a global check
   in Redis Cluster via Lua (token bucket or sliding window counter) with hash-tagged keys per
   principal; rules in hot-reloaded config; 429 with `Retry-After` and RateLimit headers; tight
   timeout and fail-open to local limits; metrics per rule; shadow mode for new rules.

2. **Token bucket vs leaky bucket?**
   Token bucket allows bursts up to capacity and serves them immediately; the leaky bucket queue
   smooths output to a constant rate by delaying requests. The leaky bucket meter is
   mathematically equivalent to a token bucket. Use token bucket for APIs, leaky bucket queue for
   shaping outbound traffic to a strict partner.

3. **What is the fixed window boundary problem and how do you fix it?**
   A client can send the full limit at the end of one window and again at the start of the next,
   up to 2x in a short span. Use a sliding window counter or log, or add a short-term token bucket
   alongside the fixed window.

4. **Derive the sliding window counter estimate and its error.**
   `estimate = prev * (1 - f) + current`, assuming previous-window requests were uniform. It
   over-admits when the previous window's traffic was concentrated at its end and under-admits
   when concentrated at its start. Empirically the error is tiny (Cloudflare measured 0.003% of
   requests misjudged).

5. **Why use a Lua script in Redis instead of GET and SET?**
   Scripts execute atomically, so no other client interleaves between read and write; it also
   saves round trips. GET/SET from the application races and over-admits.

6. **How do you rate limit across a Redis Cluster?**
   Each key maps to one slot; scripts may only touch keys in one slot, so use hash tags like
   `rl:{user:42}:...` to co-locate all of a principal's keys. For hot keys, split into sub-keys or
   pre-aggregate locally.

7. **Redis is down. What happens?**
   Depends on rule policy. Default fail open with a local fallback limiter at `limit / N`; fail
   closed for security-sensitive rules such as login attempts. Tight timeouts and a circuit
   breaker keep the limiter from adding latency; alert on fallback.

8. **What is GCRA?**
   A leaky bucket meter using a single "theoretical arrival time". Each request advances TAT by
   the emission interval; it is allowed if TAT minus now stays within the burst tolerance.
   Equivalent to a token bucket with one number of state and a natural retry-after.

9. **Rate limit vs concurrency limit?**
   Rate limits cap arrivals per time; concurrency limits cap in-flight work. By Little's law,
   in-flight = rate x latency, so when latency spikes a rate limit admits the same traffic while
   concurrency explodes. Concurrency limits self-adjust to latency.

10. **When return 429 vs 503?**
    429 when the specific caller exceeded its limit; 503 when the server is overloaded or shedding
    load independent of who is calling. Both may carry `Retry-After`.

11. **How should clients retry?**
    Only on retryable errors and idempotent requests; honour `Retry-After`; otherwise exponential
    backoff with full jitter, capped delay and attempts, and a retry budget.

12. **How would you limit by IP correctly?**
    Derive the client IP from trusted proxies only; limit IPv6 by prefix; treat IP as coarse
    abuse protection because of NAT; combine with authenticated per-key limits.

13. **Quota vs rate limit?**
    Quotas are long-period business entitlements requiring exact, durable accounting (usage
    ledger, reconciliation). Rate limits are short-period protections where approximation is fine.

14. **How do you test a rate limiter?**
    Fake clock for deterministic unit tests; multithreaded tests asserting no over-admission;
    property tests; real Redis via Testcontainers for scripts; load tests for latency; shadow mode
    in production.

15. **What is adaptive concurrency limiting?**
    Discovering the concurrency limit from latency and errors (AIMD, Vegas, gradient), as in
    Netflix's concurrency-limits library, instead of configuring it statically; excess requests
    are shed early.

---

## Summary

- Rate limiting protects availability, fairness, cost, and security; load shedding protects the
  server itself. Use **429** for the first, **503** for the second.
- Enforce in layers: edge (IP, bots), gateway (API key, plan), service (business cost),
  resources (pools, timeouts). Reject as early as possible.
- **Token bucket / GCRA**: bursts up to capacity, O(1), the default. **Sliding window counter**:
  cheap, accurate "N per minute" at scale. **Sliding log**: exact, small limits. **Fixed window**:
  cheapest, 2x boundary burst, good for quotas. **Leaky bucket queue**: smooth outbound shaping.
- Distributed: atomic Lua scripts in Redis, `TIME` from Redis as the single clock, hash tags for
  Redis Cluster, local-first + global hybrid for latency, approximate is usually fine.
- Tell clients the budget (`RateLimit-*` per the IETF draft, `Retry-After`), and make clients back
  off with jitter.
- Add concurrency limits and adaptive load shedding; static rate limits do not protect against slow
  dependencies.
- Decide fail-open vs fail-closed per rule, always with a local fallback and alerts.
- Ship rules in shadow mode, measure per-rule decisions, limiter latency, and fallbacks.

---

## Further reading

- Stripe Engineering Blog, "Scaling your API with rate limiters" (Paul Tarjan, 2017).
- Cloudflare Blog, "How we built rate limiting capable of scaling to millions of domains" (2017).
- GitHub Engineering Blog, "How we scaled the GitHub API with a sharded, replicated rate limiter in
  Redis" (2021).
- Marc Brooker, "Exponential Backoff And Jitter", AWS Architecture Blog (2015).
- IETF HTTPAPI working group, "RateLimit header fields for HTTP"
  (draft-ietf-httpapi-ratelimit-headers), current draft.
- RFC 6585, "Additional HTTP Status Codes" (defines 429); RFC 9110, "HTTP Semantics"
  (`Retry-After`, 503).
- Nginx documentation: `ngx_http_limit_req_module` and `ngx_http_limit_conn_module`.
- Envoy documentation: local rate limit filter, global rate limiting, and the `envoyproxy/ratelimit`
  reference service.
- Redis documentation: scripting with Lua, `EVAL`/`EVALSHA`, Redis Cluster specification (hash
  slots and hash tags).
- Netflix Technology Blog, "Performance Under Load" (adaptive concurrency limits), and the
  `Netflix/concurrency-limits` repository.
- Documentation for Guava `RateLimiter`, Resilience4j, and Bucket4j.
- Google SRE book, chapters "Handling Overload" and "Addressing Cascading Failures".
- Alex Xu, *System Design Interview: An Insider's Guide*, chapter "Design a Rate Limiter".
