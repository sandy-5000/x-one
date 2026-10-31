---
title: "Rate limiting"
order: 8
---
# Rate Limiting

A rate limiter caps how many requests a client can make in a time window — for example **100 requests per minute per API key**.

Why:

- Protect services from overload and abuse (scraping, brute-force login, DDoS).
- Fair sharing between tenants.
- Cost control on paid downstream APIs.
- Enforce pricing tiers (free: 60/min, pro: 1000/min).

When a client goes over the limit, respond with **HTTP 429 Too Many Requests** and headers such as:

```text
HTTP/1.1 429 Too Many Requests
Retry-After: 30
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1793448000
```

---

## Algorithms

### 1. Token bucket

A bucket holds up to `capacity` tokens and refills at `rate` tokens per second. Each request takes one token; no token means reject.

- Allows short **bursts** up to the capacity, then a steady rate.
- Used by AWS API Gateway and Stripe.

```java
class TokenBucket {
    private final long capacity;
    private final double refillPerMs;
    private double tokens;
    private long lastRefill = System.currentTimeMillis();

    TokenBucket(long capacity, double refillPerSecond) {
        this.capacity = capacity;
        this.refillPerMs = refillPerSecond / 1000.0;
        this.tokens = capacity;
    }

    synchronized boolean tryAcquire() {
        long now = System.currentTimeMillis();
        tokens = Math.min(capacity, tokens + (now - lastRefill) * refillPerMs);
        lastRefill = now;
        if (tokens >= 1) {
            tokens -= 1;
            return true;
        }
        return false;
    }
}
```

### 2. Leaky bucket

Requests enter a FIFO queue that drains at a constant rate; if the queue is full, new requests are dropped.

- Smooths traffic into a perfectly steady output.
- Bursts are delayed or dropped, not served instantly.

### 3. Fixed window counter

Count requests per fixed window (e.g. per calendar minute); reset at the boundary.

```text
key = "rl:" + userId + ":" + (now / 60)     # one counter per minute
INCR key ; EXPIRE key 60
if count > limit → reject
```

- Very cheap.
- **Boundary problem:** 100 requests at 12:00:59 and 100 more at 12:01:00 means 200 in two seconds.

### 4. Sliding window log

Store a timestamp per request (e.g. a Redis sorted set). On each request, remove timestamps older than the window and count the rest.

- Exact.
- Memory grows with the limit — one entry per request.

### 5. Sliding window counter

Blend the current and previous fixed windows:

```text
estimate = current_count + previous_count × (1 − elapsed_fraction_of_current_window)
```

Example: limit 100/min, 30 s into the current minute, previous minute had 80, current has 50 → 50 + 80 × 0.5 = 90, so allow.

- Nearly as accurate as the log, as cheap as fixed windows. A good default.

| Algorithm | Bursts | Memory | Accuracy |
| --- | --- | --- | --- |
| Token bucket | Allowed up to capacity | O(1) | Good |
| Leaky bucket | Smoothed out | O(queue) | Good |
| Fixed window | Up to 2× at edges | O(1) | Weak at edges |
| Sliding log | No | O(requests) | Exact |
| Sliding counter | Mostly no | O(1) | Very good |

---

## Distributed rate limiting

With many app servers, counters must be shared — usually in **Redis**.

```lua
-- Fixed-window limiter as an atomic Redis Lua script
local current = redis.call("INCR", KEYS[1])
if current == 1 then
  redis.call("EXPIRE", KEYS[1], ARGV[1])
end
if current > tonumber(ARGV[2]) then
  return 0
end
return 1
```

Running the check as one Lua script avoids the race where two servers both read "99" and both allow request 100.

Design notes:

- **Where:** at the API gateway or load balancer (Nginx `limit_req`, Envoy, Kong), or as middleware in the app.
- **Key:** user ID, API key, IP address, or a combination per endpoint.
- **Fail open vs fail closed:** if Redis is down, do you allow everything (availability) or block everything (protection)? Most choose fail open with a local fallback limiter.
- **Multiple tiers:** per-second burst limit plus per-day quota.
- **Clients** should retry with exponential backoff and honour `Retry-After`.
