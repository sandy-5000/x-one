---
title: "Caching"
order: 4
---
# Caching

A cache stores copies of expensive work in faster storage so later requests skip that work. It is
often the largest performance win in backends—and one of the easiest ways to ship correctness bugs.

```text
               hit (fast path)
 client ──► ┌─────────┐ ─────────────────────────────► response
            │  cache  │
            └────┬────┘
                 │ miss (slow path)
                 ▼
            ┌──────────┐      ┌─────────────────┐
            │ database │ ───► │ populate cache  │ ───► response
            └──────────┘      └─────────────────┘
```

## Key points

- Skewed access (Zipf) means a tiny fraction of keys can absorb most traffic; doubling cache size has diminishing returns.
- Effective latency: `T_eff = T_c + (1 - h) * T_m` where `h` is hit ratio—reason about **miss ratio**, not hits alone.
- Layers stack from browser/CDN → reverse proxy → in-process → Redis → DB buffer pool; each is faster but harder to invalidate.
- **Cache-aside** (default): read through cache; on write, update DB then **delete** the key (not update-in-place).
- Always set a TTL even with explicit invalidation; it bounds staleness from races and missed invalidations.
- Stampede: coalesce loads (single-flight), distributed locks, soft TTL / stale-while-revalidate, XFetch, or never expire hot keys.
- Penetration: negative caching, Bloom filters (~9.6 bits/key at 1% FP), input validation.
- Eviction: LRU (recency), LFU (frequency + aging), W-TinyLFU (Caffeine)—Redis uses sampled approximations, not exact lists.
- Invalidation: app deletes, CDC (Debezium), pub/sub or Redis `CLIENT TRACKING` for L1 near caches.
- Treat cache as critical if the DB cannot survive full miss load; plan avalanche, circuit breakers, warm-up.

---

## Why caching works

**Locality**: temporal (recent keys stay hot), spatial (nearby data), and popularity skew (head of Zipf).

Zipf: rank `k` gets probability ∝ `1/k^s` (`s` ≈ 1). Share to top `K` of `N` items ≈ `H(K)/H(N)` (harmonic numbers).

| Cache holds top K (N = 1M) | Catalogue share | Approx. request share |
| -------------------------- | --------------- | --------------------- |
| 1,000                      | 0.1%            | ~52%                  |
| 10,000                     | 1%              | ~68%                  |
| 100,000                    | 10%             | ~84%                  |

Example: Redis `T_c = 0.5 ms`, DB `T_m = 10 ms`:

| Hit ratio | Effective latency | DB load at 10k req/s |
| --------- | ----------------- | -------------------- |
| 80%       | 2.5 ms            | 2,000/s              |
| 99%       | 0.6 ms            | 100/s                |
| 99.9%     | 0.51 ms           | 10/s                 |

**Good cache candidates**: read-heavy (10:1+), expensive to compute, tolerates bounded staleness, skewed keys, modest size.

| Operation              | Typical latency |
| ---------------------- | --------------- |
| L1 / in-process lookup | ~50–500 ns      |
| Same-DC Redis GET      | ~0.2–1 ms       |
| Indexed SQL query      | ~1–10 ms        |
| Cross-region RTT       | ~70–150 ms      |

---

## Cache layers

```text
 Browser / HTTP cache
        ▼
 CDN edge
        ▼
 Reverse proxy (Nginx, Varnish)
        ▼
 In-process (Caffeine)
        ▼
 Distributed (Redis, Memcached)
        ▼
 DB buffer pool + OS page cache
```

| Layer          | Scope        | Hit cost   | Invalidation        |
| -------------- | ------------ | ---------- | ------------------- |
| Browser        | One user     | No network | Very hard           |
| CDN            | Region       | ~10–30 ms  | Purge / TTL         |
| In-process     | One instance | ~100 ns    | Per-instance broadcast |
| Distributed    | Fleet        | ~0.5 ms    | Single `DEL`        |
| DB buffer pool | One DB node  | Automatic  | Always consistent   |

### HTTP caching (RFC 9111)

| Directive                  | Meaning                                      |
| -------------------------- | -------------------------------------------- |
| `max-age=N`                | Fresh N seconds                              |
| `no-cache`                 | Store but revalidate before use (not “no store”) |
| `no-store`                 | Do not store                                 |
| `stale-while-revalidate=N` | Serve stale while refreshing (RFC 5861)      |
| `immutable`                | Fingerprinted assets; skip revalidation      |

**ETag** + `If-None-Match` → `304 Not Modified`. Prefer version-column ETags over body hashing.

| Resource              | Typical headers                                      |
| --------------------- | ---------------------------------------------------- |
| Fingerprinted static  | `public, max-age=31536000, immutable`                |
| HTML shell            | `no-cache` + ETag                                    |
| Public API (small staleness OK) | `public, max-age=30, stale-while-revalidate=300` |
| Per-user API          | `private`, short TTL + ETag                          |
| Secrets               | `no-store`                                           |

**CDN**: normalize cache keys (strip `utm_*`); origin shield; purge by tag; request collapsing on miss.

**Redis vs Memcached**: Redis—rich types, persistence, cluster, Lua; Memcached—simple multi-threaded string cache at huge scale.

**InnoDB buffer pool**: often 50–75% RAM; midpoint LRU (`innodb_old_blocks_pct` ≈ 37). PostgreSQL `shared_buffers` ~25% + OS cache.

---

## Caching strategies

### Cache-aside (lazy loading)

```text
App          Cache        DB
 │ GET key     │            │
 │───────────►│ miss       │
 │◄───────────│            │
 │ SELECT ─────────────────►│
 │ SET key EX ttl ─────────►│
```

On write: **DB commit first**, then `DEL` key. Include schema version in key (`user:v1:42`).

| Strategy      | Read              | Write                    | Main risk                    |
| ------------- | ----------------- | ------------------------ | ---------------------------- |
| Cache-aside   | App checks/fills  | DB then delete           | Read/write races             |
| Read-through  | Cache loads miss  | Usually evict on write   | Coupling to data layer       |
| Write-through | Warm cache        | Sync cache + DB          | Slow writes; cold-key bloat  |
| Write-behind  | Warm cache        | Cache now, DB async      | Loss before flush            |
| Write-around  | Miss after write  | DB only                  | Pollutes cache if misapplied |
| Refresh-ahead | Hot keys no miss  | Background refresh       | Wasted work on cold keys     |

**Write-through order**: DB first so failed DB leaves cache untouched. **Write-behind**: counters, view aggregation—needs durability if you cannot lose writes.

**TTL jitter**: `base ± spread%` avoids synchronized expiry waves (e.g. 3600 s ± 10%).

---

## Cache-aside races and fixes

**Stale set after delete**: reader loads v1, writer commits v2 and deletes key, reader sets v1 → stale until TTL. Worse with lagging read replicas refilling after delete.

**Write/write race if updating cache**: two writers can order DB and cache differently—always delete, not update.

**Delete before DB commit**: readers refill old value that survives the commit—delete **after** commit, outside a rolled-back transaction.

| Mitigation            | Idea                                              |
| --------------------- | ------------------------------------------------- |
| TTL                   | Upper bound on any race                           |
| Delayed double delete | Delete → write → delete after ~500 ms             |
| Versioned set         | Lua: set only if version newer than cached         |
| Leases (Facebook)     | Miss returns token; delete invalidates stale set; token rate limit tames herd |

Facebook leases: hot-key DB load dropped ~17k/s → ~1.3k/s with 10 s token spacing.

---

## Eviction

When full, policy maximizes hit ratio at low metadata cost.

| Algorithm   | Captures        | Scan resistant | Typical use        |
| ----------- | --------------- | -------------- | ------------------ |
| FIFO/Random | Insertion order | No             | Simple             |
| LRU         | Recency         | No             | Linked list + map  |
| SLRU / CLOCK| Approx LRU      | Somewhat       | PG buffers, OS     |
| LFU         | Frequency       | Yes (with decay)| Stable hot set     |
| ARC         | Adaptive T1/T2  | Yes            | ZFS                |
| W-TinyLFU   | Window + sketch | Yes            | Caffeine           |

**W-TinyLFU**: ~1% window LRU for new keys → TinyLFU admission (Count-Min sketch) vs main SLRU victim; counters halve periodically.

**Redis** (`maxmemory` set): samples `maxmemory-samples` (default 5), eviction pool of 16—not a full LRU list.

| Policy         | Evicts from                          |
| -------------- | ------------------------------------ |
| `allkeys-lru/lfu` | All keys                          |
| `volatile-*`   | Keys with TTL only                   |
| `noeviction`   | Writes fail (default without maxmemory) |

Pure cache: separate Redis for cache vs durable state; use `allkeys-lfu` or `allkeys-lru`.

---

## TTL design

| Data type           | Typical TTL        |
| ------------------- | ------------------ |
| Reference data      | Hours–days + purge on change |
| User profile        | 5–60 min + delete on write   |
| Feed fragments      | Seconds–minutes    |
| Negative (“not found”) | 30–300 s        |
| Session             | Sliding on access  |

**Absolute** (`expireAfterWrite`): data age bounded. **Sliding** (`expireAfterAccess`): hot entries never refresh from source—dangerous for mutable data.

Redis expiry: lazy on access + active sampling cycle—expired keys reclaim gradually.

---

## Failure modes

### Stampede

```text
 key expires at t0 → N concurrent misses → N identical DB queries
```

| Fix              | Mechanism                                      |
| ---------------- | ---------------------------------------------- |
| Single-flight    | One loader per key per process                 |
| Redis lock       | `SET lock NX PX`; token-checked release        |
| Soft TTL         | Logical expiry inside payload; serve stale     |
| XFetch           | Early recompute prob. rises near expiry        |
| Scheduled refresh| No short TTL on known hot keys                 |

**XFetch**: recompute if `now - delta * beta * ln(rand()) >= expiry` (`beta` ≈ 1).

### Penetration

Non-existent keys always miss. Fixes: short-TTL null sentinel, Bloom filter (`m ≈ -n ln(p)/(ln 2)²`, optimal `k = (m/n) ln 2`), reject bad IDs.

Example: 10M keys, 1% FP → ~12 MB, ~7 hashes.

### Avalanche

| Cause              | Fix                                    |
| ------------------ | -------------------------------------- |
| Same TTL batch     | Jitter                                 |
| Cluster down       | HA, warm-up, circuit breaker on miss path |
| `FLUSHALL`         | ACLs, rename-command                   |

### Hot keys

One shard/node saturated. L1 with 1–5 s TTL; replicate key as `key#0..N-1` on different shards; read replicas; shard write counters.

---

## Consistency and invalidation

| Approach           | Staleness bound        | Complexity |
| ------------------ | ---------------------- | ---------- |
| TTL only           | TTL                    | Low        |
| Delete on write      | Race windows           | Low        |
| Version / leases     | Near zero in cache     | Medium     |
| CDC (Debezium)       | Replication lag        | Medium     |
| Read primary         | Zero                   | Simple     |

```text
 DB ──binlog/WAL──► Debezium ──► Kafka ──► invalidator ──► DEL Redis keys
```

L1 + Redis: `DEL` on L2 does not clear L1—pub/sub, short L1 TTL, or Redis 6+ `CLIENT TRACKING`.

---

## Distributed cache architecture

**Consistent hashing**: add/remove node moves ~`1/N` keys vs ~`N/(N+1)` for `hash mod N`. **Virtual nodes** (100–200 per host) balance load.

**Redis Cluster**: `slot = CRC16(key) mod 16384`; hash tags `{user:42}` co-locate keys for multi-key ops.

Replication is **async**—failover can lose acknowledged writes; acceptable for cache-only roles.

| RDB snapshot     | AOF                                      |
| ---------------- | ---------------------------------------- |
| Point-in-time    | Log of writes                            |
| Fast load        | `everysec` default (~1 s loss)           |
| Fork memory spike| `aof-use-rdb-preamble` for hybrid load   |

---

## Multi-level cache

```text
 App L1 (seconds) ──► Redis L2 (minutes) ──► DB
```

L1 TTL must be short; staleness ≈ L1 TTL + invalidation delay. Size L1 by heap, not entry count alone.

---

## Sizing and monitoring

Working set ≈ hot keys × (value + key + Redis overhead ~ tens of bytes). Add 1.3× fragmentation, replication, fork headroom.

| Metric              | Why                          |
| ------------------- | ---------------------------- |
| Hit ratio by prefix | Global 95% can hide 20% prefix |
| `evicted_keys`      | Undersized or TTL too long   |
| Miss-driven DB load | Real cost of cache loss      |

Never `KEYS *` in prod; use `SCAN`. Timeouts + circuit breaker on cache path.

### Redis structures (quick map)

| Type | Typical use |
| ---- | ----------- |
| String + TTL | Serialized objects, locks (`SET NX PX`) |
| Hash | Field-level updates without rewriting blob |
| Sorted set | Leaderboards, sliding-window rate limits |
| Stream | Durable log with consumer groups |
| HyperLogLog | ~12 KB approximate distinct counts (~0.81% error) |

Rate-limit pattern: run `INCR` + `EXPIRE` in one Lua script so a crash cannot leave a counter without TTL.

---

## Pitfalls

- Caching without TTL; treating Redis as sole source of truth without durability design.
- Update cache on write, or delete cache before DB commit.
- `no-cache` confused with `no-store`; `Vary: Cookie` destroying CDN hit ratio.
- No stampede protection on expensive hot keys; no fallback when cache is slow (worse than down).
- Huge values / `HGETALL` on million-field hashes blocking single-threaded Redis.
- Assuming hits mean healthy DB—load-test with cold cache.
- Redis lock for **correctness** without fencing tokens on the protected resource.

---

## Quick check

**1. Why delete the cache key on write instead of updating it?**  
Concurrent writers can commit DB in one order and update cache in another, leaving wrong data until TTL. Delete is idempotent; next reader loads current row.

**2. Write the effective latency formula and what happens when hit ratio goes from 99% to 95%.**  
`T_eff = T_c + (1 - h) * T_m`. Miss ratio rises from 1% to 5%—DB load **5×** and latency grows by `0.04 * T_m`.

**3. Name three stampede mitigations.**  
Single-flight, Redis rebuild lock, soft TTL / stale-while-revalidate, XFetch probabilistic early refresh, or background refresh without short TTL on hot keys.

**4. How does W + R > N relate to linearizability?**  
It does not guarantee it. Overlapping quorums usually see latest writes, but concurrent reads during partial writes, failed unrolled writes, sloppy quorums, and LWW can still violate linearizability.

**5. Which HTTP header recipe for a fingerprinted JS bundle vs per-user API?**  
Bundle: `Cache-Control: public, max-age=31536000, immutable`. Per-user API: `private`, short or zero max-age, often with ETag for 304—not cacheable on shared CDN without `private`.
