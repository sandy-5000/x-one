---
title: "Caching"
order: 4
---
# Caching

A cache keeps a copy of the result of expensive work (a database query, a remote call, a rendered
page, a computed aggregate) in faster storage so that later requests can skip that work. Caching is
the cheapest large performance win in most backends, and it is also one of the easiest ways to ship
a correctness bug. These notes cover why caches work, where they live, how to read and write
through them, how they evict, how they fail, and how to run them at scale.

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

## Why caching works

### Locality of reference

Caches exploit the fact that access patterns are not uniform:

- **Temporal locality**: something accessed now is likely to be accessed again soon (a user's
  session, the home page, a trending post).
- **Spatial locality**: things near something accessed now are likely to be accessed soon (the
  next rows on a disk page, the next 64-byte CPU cache line, the next page of a feed).
- **Popularity skew**: a small set of items receives most of the traffic.

If every key were requested with equal probability, a cache holding 1% of keys would serve about 1%
of requests. Real workloads are far more skewed than that.

### Zipf and power-law distributions

Many real workloads (web pages, videos, products, social posts, DNS names) follow roughly a **Zipf
distribution**: the item of popularity rank `k` is requested with probability proportional to
`1 / k^s`, with `s` often near 1.

For `s = 1` and `N` items, the share of requests that go to the top `K` items is `H(K) / H(N)`, where
`H(n) = 1 + 1/2 + ... + 1/n ≈ ln(n) + 0.577` is the harmonic number.

Worked example with `N = 1,000,000` items (`H(N) ≈ 14.39`):

| Cache holds top K items | Share of catalogue | H(K)   | Share of requests served |
| ----------------------- | ------------------ | ------ | ------------------------ |
| 1,000                   | 0.1%               | ≈ 7.49 | ≈ 52%                    |
| 10,000                  | 1%                 | ≈ 9.79 | ≈ 68%                    |
| 100,000                 | 10%                | ≈ 12.09| ≈ 84%                    |

So a cache holding 1% of the data can absorb about two thirds of the traffic. This is the
fundamental reason caching pays off, and also why the hit ratio grows only logarithmically once
you are past the hot head: doubling the cache size gives diminishing returns.

### Hit ratio and effective latency

Define:

- `h` = hit ratio (hits / total lookups)
- `T_c` = latency of a cache lookup
- `T_m` = latency of the backing store (the "miss penalty")

For a look-aside cache, a miss pays for the cache lookup and then the backing store:

```text
T_eff = h * T_c + (1 - h) * (T_c + T_m)
      = T_c + (1 - h) * T_m
```

Worked numbers, with Redis at `T_c = 0.5 ms` and a database query at `T_m = 10 ms`:

| Hit ratio | Effective latency          | DB load at 10,000 req/s |
| --------- | -------------------------- | ----------------------- |
| 0%        | 0.5 + 1.00 * 10 = 10.5 ms  | 10,000 req/s            |
| 80%       | 0.5 + 0.20 * 10 = 2.5 ms   | 2,000 req/s             |
| 90%       | 0.5 + 0.10 * 10 = 1.5 ms   | 1,000 req/s             |
| 99%       | 0.5 + 0.01 * 10 = 0.6 ms   | 100 req/s               |
| 99.9%     | 0.5 + 0.001 * 10 = 0.51 ms | 10 req/s                |

Two important lessons come from this table:

1. **Think in miss ratio, not hit ratio.** Going from 90% to 99% hits sounds like a 10% improvement
   but it cuts the miss ratio, and therefore the database load, by **10x**.
2. **The database is now sized for the miss rate.** If the cache is flushed or fails, load jumps
   from 100 req/s to 10,000 req/s. A system that "needs" its cache to survive must treat the cache
   as critical infrastructure (see cache avalanche below).

The same formula applies to a chain of caches. With L1 (in-process, 0.001 ms), L2 (Redis, 0.5 ms)
and a DB (10 ms), and hit ratios of 60% at L1 and 90% at L2 for the requests that reach it:

```text
T_eff = 0.001 + 0.4 * (0.5 + 0.1 * 10) = 0.001 + 0.4 * 1.5 = 0.601 ms
```

### Latency numbers worth remembering

Orders of magnitude only; measure your own environment.

| Operation                                     | Typical latency |
| --------------------------------------------- | --------------- |
| CPU L1 cache reference                        | ~1 ns           |
| Main memory reference                         | ~100 ns         |
| In-process hash map lookup (Caffeine, HashMap)| ~50–500 ns      |
| Round trip within a data centre               | ~0.1–0.5 ms     |
| Redis / Memcached GET (same DC)               | ~0.2–1 ms       |
| Random read from NVMe SSD                     | ~0.1 ms         |
| Simple indexed SQL query                      | ~1–10 ms        |
| Cross-continent round trip                    | ~70–150 ms      |

### What makes data cacheable

A value is a good cache candidate when:

- it is **read much more than written** (read/write ratio of 10:1 or higher),
- it is **expensive** to compute or fetch,
- it is **tolerant of some staleness**, or the staleness can be bounded by invalidation,
- the **key space is skewed** (a hot set exists),
- the value is **reasonably small** (large blobs belong in a CDN or object store).

Bad candidates: balances and inventory counts that must be exact at write time, highly personalised
data with no reuse, write-heavy counters (use a counter store instead), and data whose key space is
uniformly random.

---

## Cache layers

A request can pass through many caches before reaching the source of truth. Each layer is closer to
the user, cheaper per hit, and harder to invalidate than the layer behind it.

```text
┌──────────────────────────────────────────────────────────┐
│ Browser / mobile app cache   (HTTP cache, service worker)│
└──────────────────────────────┬───────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────┐
│ CDN edge PoP                 (CloudFront, Fastly, Akamai)│
└──────────────────────────────┬───────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────┐
│ Reverse proxy / gateway      (Nginx, Varnish, Envoy)     │
└──────────────────────────────┬───────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────┐
│ Application local cache      (Caffeine, Guava, HashMap)  │
└──────────────────────────────┬───────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────┐
│ Distributed cache            (Redis, Memcached)          │
└──────────────────────────────┬───────────────────────────┘
                               ▼
┌──────────────────────────────────────────────────────────┐
│ Database buffer pool + OS page cache                     │
└──────────────────────────────────────────────────────────┘
```

| Layer               | Scope               | Typical hit cost | Invalidation difficulty            |
| ------------------- | ------------------- | ---------------- | ---------------------------------- |
| Browser             | One user            | 0 network        | Very hard (you cannot reach it)    |
| CDN                 | All users in region | ~10–30 ms        | Purge API, seconds to propagate    |
| Reverse proxy       | One data centre     | sub-ms           | Purge / short TTL                  |
| Local in-process    | One instance        | ~100 ns          | Per-instance, needs a broadcast    |
| Distributed         | Whole fleet         | ~0.5 ms          | Single DEL                         |
| DB buffer pool      | One DB node         | automatic        | Automatic (always consistent)      |

### Browser and HTTP caching

HTTP caching is specified in **RFC 9111** (HTTP Caching, 2022, which replaced RFC 7234). Caches
decide two things: is a stored response **fresh** (can be served without contacting the origin),
and if it is stale, can it be **revalidated** cheaply.

#### Cache-Control directives

| Directive                    | Meaning                                                                 |
| ---------------------------- | ----------------------------------------------------------------------- |
| `max-age=N`                  | Fresh for N seconds after it was generated.                             |
| `s-maxage=N`                 | Like `max-age` but only for shared caches (CDN, proxy); overrides it.   |
| `public`                     | Any cache may store it, even if the request had `Authorization`.        |
| `private`                    | Only the browser may store it; CDNs and proxies must not.               |
| `no-cache`                   | May be stored, but must be revalidated with the origin before each use. |
| `no-store`                   | Must not be stored anywhere. Use for sensitive data.                    |
| `must-revalidate`            | Once stale, must not be served without successful revalidation.        |
| `immutable`                  | The body will never change while fresh; skip revalidation on reload.    |
| `stale-while-revalidate=N`   | For N seconds after going stale, serve stale and refresh in background. |
| `stale-if-error=N`           | For N seconds after going stale, serve stale if the origin errors.      |

`stale-while-revalidate` and `stale-if-error` come from **RFC 5861**. A common misconception is
that `no-cache` means "do not cache"; it means "always revalidate". `no-store` is the one that
forbids storage.

#### Validators: ETag and Last-Modified

When a response is stale, the client sends a **conditional request**. If nothing changed, the
origin replies `304 Not Modified` with no body, saving bandwidth and serialization work.

```text
Browser                                   Origin
  │  GET /api/products/42                   │
  │────────────────────────────────────────►│
  │  200 OK                                 │
  │  ETag: "v7-a1b2"                        │
  │  Cache-Control: max-age=60              │
  │◄────────────────────────────────────────│
  │                                         │
  │  ... 60 s later, entry is stale ...     │
  │                                         │
  │  GET /api/products/42                   │
  │  If-None-Match: "v7-a1b2"               │
  │────────────────────────────────────────►│
  │  304 Not Modified  (no body)            │
  │◄────────────────────────────────────────│
```

- **ETag** is an opaque version identifier. A **strong** ETag (`"abc"`) means byte-identical; a
  **weak** ETag (`W/"abc"`) means semantically equivalent. The client sends it back in
  `If-None-Match`.
- **Last-Modified** is a timestamp with one-second resolution. The client sends `If-Modified-Since`.
  It is weaker than ETag because two changes within one second are indistinguishable.
- If neither `max-age` nor `Expires` is present, caches may apply **heuristic freshness** (often
  10% of the time since `Last-Modified`). Always set explicit `Cache-Control` to avoid surprises.

#### Vary

`Vary` lists request headers that are part of the cache key. `Vary: Accept-Encoding` means a gzip
response and a brotli response are stored separately. `Vary: Cookie` or `Vary: Authorization`
effectively makes a response uncacheable in shared caches because every user has a different key.
Too many `Vary` headers fragment the cache and destroy the hit ratio.

#### Recommended header recipes

| Resource                                   | Header                                                          |
| ------------------------------------------ | --------------------------------------------------------------- |
| Fingerprinted static asset (`app.3f9a.js`) | `Cache-Control: public, max-age=31536000, immutable`             |
| HTML entry page                            | `Cache-Control: no-cache` plus an ETag                           |
| Public API list, small staleness OK        | `Cache-Control: public, max-age=30, stale-while-revalidate=300`  |
| Per-user API response                      | `Cache-Control: private, max-age=0, must-revalidate` plus ETag   |
| Secrets, banking pages                     | `Cache-Control: no-store`                                        |

The fingerprinted-asset pattern is the key trick: the file name changes whenever the content
changes, so the file can be cached forever and "invalidation" is just pointing HTML to a new name.

#### ETag support in Spring

```java
@RestController
@RequestMapping("/api/products")
class ProductController {

    private final ProductService products;

    ProductController(ProductService products) {
        this.products = products;
    }

    @GetMapping("/{id}")
    ResponseEntity<ProductDto> get(@PathVariable long id, WebRequest request) {
        Product p = products.find(id);
        String etag = "\"" + p.id() + "-" + p.version() + "\"";

        if (request.checkNotModified(etag)) {
            return null; // Spring has already prepared a 304 response
        }
        return ResponseEntity.ok()
                .eTag(etag)
                .cacheControl(CacheControl.maxAge(30, TimeUnit.SECONDS)
                        .cachePublic()
                        .staleWhileRevalidate(300, TimeUnit.SECONDS))
                .body(ProductDto.from(p));
    }
}
```

Deriving the ETag from a version column is much cheaper than hashing the serialized body, which
Spring's `ShallowEtagHeaderFilter` does (it still renders the whole response, so it saves bandwidth
but not server work).

### CDN

A **Content Delivery Network** places caches in points of presence (PoPs) close to users. It reduces
latency (shorter round trips, TLS terminated nearby) and offloads origin traffic.

Key concepts:

- **Cache key**: by default scheme + host + path + query string. Normalize or strip irrelevant
  query parameters (`utm_source`) or every campaign link becomes a separate cache entry.
- **Origin shield / tiered caching**: an extra mid-tier cache between edges and origin, so that a
  miss at 200 edges results in one origin request instead of 200.
- **Purging**: by URL, by prefix, or by tag. Fastly calls tags *surrogate keys*; Cloudflare and
  Akamai offer cache tags. Tag a product page with `product-42` and purge that tag on update.
- **Dynamic content**: CDNs can cache API responses with short TTLs (1–10 s "micro-caching"), which
  still absorbs huge spikes: 10,000 req/s on one URL becomes about one origin request per second
  per shield.
- **Request collapsing**: most CDNs merge concurrent misses for the same object into one origin
  fetch, which is CDN-level stampede protection.

### Reverse proxy cache

A reverse proxy such as Nginx or Varnish caches full HTTP responses inside your data centre. It is
useful for server-rendered pages and public APIs.

```nginx
proxy_cache_path /var/cache/nginx levels=1:2 keys_zone=api:100m
                 max_size=10g inactive=60m use_temp_path=off;

server {
    location /api/ {
        proxy_cache                 api;
        proxy_cache_key             $scheme$host$request_uri;
        proxy_cache_valid           200 30s;
        proxy_cache_lock            on;
        proxy_cache_use_stale       error timeout updating http_500 http_502 http_503;
        proxy_cache_background_update on;
        add_header                  X-Cache-Status $upstream_cache_status;
        proxy_pass                  http://backend;
    }
}
```

- `proxy_cache_lock on` lets only one request per key go to the upstream while others wait, which
  is request coalescing.
- `proxy_cache_use_stale updating` plus `proxy_cache_background_update` is the Nginx equivalent of
  stale-while-revalidate.
- Varnish has the same ideas under the names **grace mode** (serve stale while refreshing) and
  request coalescing (its "waiting list").

### Application local cache (in-process)

An in-process cache stores objects in the JVM heap. It is the fastest cache possible (no network,
no serialization) but each instance has its own copy, so instances can disagree and memory is
duplicated N times.

**Caffeine** is the standard high-performance Java cache. It uses the W-TinyLFU eviction policy
(explained below), supports size- and weight-based limits, time-based expiry, asynchronous loading,
and refresh.

```java
LoadingCache<Long, User> users = Caffeine.newBuilder()
        .maximumSize(100_000)
        .expireAfterWrite(Duration.ofMinutes(10))
        .refreshAfterWrite(Duration.ofMinutes(1))
        .recordStats()
        .build(id -> userRepository.findById(id).orElse(null));

User u = users.get(42L);          // loads on miss, at most one load per key at a time
CacheStats stats = users.stats(); // hitRate(), missCount(), evictionCount(), ...
```

Things to know:

- `get` on a `LoadingCache` performs the load **atomically per key**: concurrent callers for the same
  key wait for one load. This gives stampede protection inside one JVM for free.
- Returning `null` from the loader means "no mapping" and nothing is stored. To cache negative
  results, wrap the value in `Optional`.
- `refreshAfterWrite` refreshes asynchronously on the first access after the interval and keeps
  serving the old value until the new one arrives (refresh-ahead).
- `maximumWeight` plus a `Weigher` bounds memory by approximate size instead of entry count.
- Keep local caches small and TTLs short, because there is no cross-instance coherence unless you
  add it (see near-cache coherence).

### Distributed cache: Redis and Memcached

A distributed cache is a shared network service. Every app instance sees the same entries, so a
single `DEL` invalidates for everyone, and cache capacity scales independently of app servers.

|                       | Redis                                                    | Memcached                                  |
| --------------------- | -------------------------------------------------------- | ------------------------------------------ |
| Data model            | Strings, hashes, lists, sets, sorted sets, streams, more | Opaque byte strings                        |
| Max value size        | 512 MB per string                                        | 1 MB default (configurable with `-I`)      |
| Threading             | Single-threaded command execution, optional I/O threads  | Multi-threaded                             |
| Memory management     | jemalloc, per-key allocation                             | Slab allocator with size classes           |
| Eviction              | Approximated LRU / LFU, TTL-based, configurable          | Segmented LRU per slab class               |
| Persistence           | RDB snapshots, AOF log                                   | None                                       |
| Replication / cluster | Built in (replicas, Sentinel, Redis Cluster)             | None; client-side consistent hashing       |
| Scripting / atomics   | Lua scripts, functions, MULTI/EXEC, INCR, SET NX         | `cas`, `incr`, `add`                        |
| Extra uses            | Locks, rate limits, leaderboards, queues, pub/sub        | Pure cache                                 |

Choose Memcached when you need a very large, simple, multi-threaded string cache with predictable
memory behaviour. Choose Redis for everything else, which in practice is most teams.

### Database buffer pool and OS page cache

Databases cache disk pages in memory themselves:

- **InnoDB buffer pool** (`innodb_buffer_pool_size`, typically 50–75% of RAM on a dedicated host)
  uses an LRU with **midpoint insertion**: new pages enter the "old" sublist (about 3/8 of the list,
  `innodb_old_blocks_pct = 37`) and only move to the "young" sublist if they are accessed again after
  `innodb_old_blocks_time` (1000 ms). This stops one full-table scan from flushing the hot set,
  which is the same "scan resistance" idea behind SLRU and W-TinyLFU.
- **PostgreSQL** `shared_buffers` (commonly about 25% of RAM) uses a **clock-sweep** approximation of
  LRU, and relies heavily on the OS page cache for the rest (double buffering).
- MySQL's old **query cache** (whole result sets keyed by SQL text) was removed in MySQL 8.0 because
  its global mutex and coarse invalidation hurt more than it helped. Result caching belongs in the
  application.

A query that fits in the buffer pool is usually fast enough that adding Redis in front gives only a
modest gain. Caching pays off most for expensive queries, joins, aggregates, and remote calls.

---

## Caching strategies

### Cache-aside (lazy loading)

The application talks to both the cache and the database. On read it checks the cache, and on a
miss it loads from the database and populates the cache. On write it updates the database and then
**deletes** the cache entry.

```text
App                     Cache                   DB
 │  GET user:42           │                      │
 │───────────────────────►│                      │
 │  (nil)  miss           │                      │
 │◄───────────────────────│                      │
 │  SELECT ... WHERE id=42                       │
 │──────────────────────────────────────────────►│
 │  row                                          │
 │◄──────────────────────────────────────────────│
 │  SET user:42 EX 3600   │                      │
 │───────────────────────►│                      │
```

```java
public final class UserCache {
    private static final Duration TTL = Duration.ofHours(1);

    private final JedisPooled redis;
    private final UserRepository db;
    private final ObjectMapper json;

    public UserCache(JedisPooled redis, UserRepository db, ObjectMapper json) {
        this.redis = redis;
        this.db = db;
        this.json = json;
    }

    public Optional<User> get(long id) throws JsonProcessingException {
        String key = "user:v1:" + id;
        String cached = redis.get(key);
        if (cached != null) {
            return Optional.of(json.readValue(cached, User.class));
        }
        Optional<User> fromDb = db.findById(id);
        if (fromDb.isPresent()) {
            redis.setex(key, jitter(TTL).toSeconds(), json.writeValueAsString(fromDb.get()));
        }
        return fromDb;
    }

    public void update(User user) {
        db.save(user);
        redis.del("user:v1:" + user.id());
    }

    private static Duration jitter(Duration base) {
        long extra = ThreadLocalRandom.current().nextLong(base.toSeconds() / 10 + 1);
        return base.plusSeconds(extra);
    }
}
```

Properties:

- Only data that is actually requested is cached, so memory is spent on the hot set.
- The cache is **not on the critical path for correctness**: if Redis is down, reads fall back to the
  database (with a timeout and a circuit breaker so a slow Redis does not slow everything).
- The first read after a miss or invalidation pays the full cost.
- It has subtle race conditions, covered in detail in the next section.
- The key includes a schema version (`v1`) so that a deployment changing the serialized shape can
  switch to `v2` without reading incompatible old entries.

**Why delete instead of update on write?** Updating the cache on write has a write/write race: two
writers can update the DB in one order and the cache in the opposite order, leaving the cache
permanently wrong (until TTL). Deleting is idempotent and the next reader loads the current value.
Delete also avoids computing expensive cache values that nobody reads.

### Read-through

The cache sits in front of the database and owns the loading logic. The application asks only the
cache; on a miss the cache calls a loader. Caffeine's `LoadingCache`, Spring's `@Cacheable`, and
products like Hazelcast or NCache with `MapLoader` work this way.

```text
App                     Cache (with loader)     DB
 │  get(42)               │                      │
 │───────────────────────►│  miss: load(42)      │
 │                        │─────────────────────►│
 │                        │  row                 │
 │                        │◄─────────────────────│
 │  value                 │  store               │
 │◄───────────────────────│                      │
```

```java
@Service
class ProductService {

    private final ProductRepository repo;

    ProductService(ProductRepository repo) {
        this.repo = repo;
    }

    @Cacheable(cacheNames = "products", key = "#id", sync = true)
    public Product find(long id) {
        return repo.findById(id).orElseThrow();
    }

    @CacheEvict(cacheNames = "products", key = "#product.id()")
    public Product save(Product product) {
        return repo.save(product);
    }
}
```

`sync = true` asks the cache provider to synchronize concurrent loads for the same key (local to one
JVM for most providers). Spring cache annotations go through a proxy, so self-invocation from
inside the same class bypasses the cache, a frequent surprise.

Compared with cache-aside: identical data flow, but loading code is centralized and the cache can
coalesce loads. The drawback is coupling the cache to the data access layer.

### Write-through

Every write goes to the cache and to the database synchronously, as one logical operation. Reads
always hit a warm, fresh cache for anything written through it.

```text
App                     Cache                   DB
 │  put(42, v)            │                      │
 │───────────────────────►│  write(42, v)        │
 │                        │─────────────────────►│
 │                        │  ok                  │
 │                        │◄─────────────────────│
 │  ok                    │  store v             │
 │◄───────────────────────│                      │
```

```java
public final class WriteThroughRepository<K, V> {
    private final Cache<K, V> cache;
    private final Repository<K, V> db;

    public WriteThroughRepository(Cache<K, V> cache, Repository<K, V> db) {
        this.cache = cache;
        this.db = db;
    }

    public void put(K key, V value) {
        db.save(key, value);      // DB first: if it fails, the cache is untouched
        cache.put(key, value);
    }

    public V get(K key) {
        return cache.get(key, db::load);
    }
}
```

- Writes are slower (two writes on the critical path).
- The cache fills with data that may never be read; combine with a TTL.
- It is still not atomic across two systems: a crash between the DB write and the cache write leaves
  the cache stale, and concurrent writers still race. In practice write-through is often paired
  with a TTL and a delete-on-failure fallback.

### Write-behind (write-back)

Writes go to the cache only, and the cache persists them to the database asynchronously, often in
batches. The cache becomes the temporary source of truth.

```text
App                     Cache                   DB
 │  put(42, v)            │                      │
 │───────────────────────►│                      │
 │  ok (immediately)      │                      │
 │◄───────────────────────│                      │
 │                        │  ... later, batch ...│
 │                        │  write(42, v), ...   │
 │                        │─────────────────────►│
```

```java
public final class WriteBehindCache<K, V> implements AutoCloseable {
    private final ConcurrentHashMap<K, V> cache = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<K, V> dirty = new ConcurrentHashMap<>();
    private final ScheduledExecutorService flusher =
            Executors.newSingleThreadScheduledExecutor();
    private final Repository<K, V> db;

    public WriteBehindCache(Repository<K, V> db, Duration interval) {
        this.db = db;
        flusher.scheduleWithFixedDelay(this::flush,
                interval.toMillis(), interval.toMillis(), TimeUnit.MILLISECONDS);
    }

    public void put(K key, V value) {
        cache.put(key, value);
        dirty.put(key, value);            // later writes to the same key coalesce
    }

    public V get(K key) {
        return cache.computeIfAbsent(key, db::load);
    }

    private void flush() {
        Map<K, V> batch = new HashMap<>();
        for (K key : dirty.keySet()) {
            V v = dirty.remove(key);
            if (v != null) batch.put(key, v);
        }
        if (batch.isEmpty()) return;
        try {
            db.saveAll(batch);
        } catch (RuntimeException e) {
            batch.forEach(dirty::putIfAbsent); // retry next round, newer values win
        }
    }

    @Override
    public void close() {
        flusher.shutdown();
        flush();
    }
}
```

- Lowest write latency, absorbs write bursts, and coalesces repeated writes to the same key (a
  counter incremented 1,000 times becomes one DB write).
- **Data loss** if the cache node dies before flushing, unless the cache itself is durable and
  replicated.
- Ordering and consistency are harder: other systems reading the database see old data.
- Real-world use: CPU caches, OS page cache (dirty pages), databases' own buffer pools (backed by a
  WAL for durability), view counters, and "likes" aggregation.

### Write-around

Writes go directly to the database and do not touch the cache (other than an optional delete). The
cache is populated only by reads. This is effectively cache-aside's write path, and it is the right
choice when recently written data is unlikely to be read soon (logs, bulk imports, archival
records), because it avoids polluting the cache.

### Refresh-ahead

Popular entries are refreshed **before** they expire, so readers never see a miss for hot keys.
Caffeine's `refreshAfterWrite` does this lazily: after the refresh interval, the next read triggers
an asynchronous reload while still returning the current value.

```java
AsyncLoadingCache<String, ExchangeRate> rates = Caffeine.newBuilder()
        .maximumSize(1_000)
        .refreshAfterWrite(Duration.ofSeconds(30))
        .expireAfterWrite(Duration.ofMinutes(5))
        .buildAsync((pair, executor) -> rateClient.fetchAsync(pair));
```

The `expireAfterWrite` value bounds how stale data can get if refreshes keep failing. Refresh-ahead
wastes work on entries that would not have been read again, so apply it to known-hot data.

### Strategy comparison

| Strategy      | Read path             | Write path                | Freshness                  | Main risk                       |
| ------------- | --------------------- | ------------------------- | -------------------------- | ------------------------------- |
| Cache-aside   | App checks, fills     | DB, then delete key       | Stale until delete / TTL   | Races between readers, writers  |
| Read-through  | Cache loads on miss   | Usually paired w/ evict   | Same as cache-aside        | Coupling cache to data layer    |
| Write-through | Always warm           | Cache and DB synchronously| Fresh if all writes pass   | Slower writes, cold-data bloat  |
| Write-behind  | Always warm           | Cache now, DB later       | DB lags behind cache       | Data loss on cache failure      |
| Write-around  | Miss after each write | DB only                   | Fresh in DB                | Misses on recently written data |
| Refresh-ahead | Hot keys never miss   | Independent               | Bounded by refresh period  | Wasted refreshes                |

---

## Race conditions in cache-aside

Cache-aside is the most common pattern, and it is not linearizable. Know these races cold; they are
favourite senior interview questions.

### Race 1: stale set after delete (read/write race)

```text
time   Reader A                          Writer B                     Cache    DB
 t1    GET user:42 -> miss                                            -        v1
 t2    SELECT -> v1                                                   -        v1
 t3                                      UPDATE -> v2                 -        v2
 t4                                      DEL user:42                  -        v2
 t5    SET user:42 = v1   (stale!)                                    v1       v2
```

Reader A loaded `v1` before the write but stored it after the invalidation. The cache now holds
`v1` until the TTL expires. The window is small (A must be slower than B's whole write) but at high
traffic it happens. The DB read taking a long time (a GC pause, a slow replica) makes it likely.

If the reader reads from an **asynchronous replica**, the race becomes much more likely: the
writer deletes the key after committing to the primary, and a reader immediately refills from a
replica that has not yet applied the write.

### Race 2: update-then-update (write/write race)

If writers update the cache instead of deleting it:

```text
time   Writer A                Writer B                Cache    DB
 t1    UPDATE DB -> a                                  -        a
 t2                            UPDATE DB -> b          -        b
 t3                            SET cache = b           b        b
 t4    SET cache = a                                   a        b    (wrong)
```

This is why the default advice is **update the DB, then delete the cache**.

### Race 3: delete-then-update ordering

"Delete the cache, then update the DB" is worse: between the delete and the commit, a reader can
miss, read the old row, and repopulate the old value, which then survives indefinitely after the
commit. Always delete **after** the DB commit (and not inside the transaction, where the commit may
still fail or a reader may see uncommitted state elsewhere).

### Mitigation: short TTL as a safety net

Every cache entry should have a TTL even if you invalidate explicitly. The TTL is the upper bound on
how long any of these races can leave stale data.

### Mitigation: delayed double delete

Delete the key, write the DB, then delete again after a delay longer than a typical read-and-fill
(or longer than replica lag):

```java
public void update(User user) {
    String key = "user:v1:" + user.id();
    redis.del(key);
    db.save(user);
    scheduler.schedule(() -> redis.del(key), 500, TimeUnit.MILLISECONDS);
}
```

It shrinks the race window but does not close it (a reader slower than the delay still wins), and
the delayed task can be lost if the process dies. Treat it as a probabilistic improvement.

### Mitigation: versioned values

Store a monotonically increasing version (a DB `version` column, an update timestamp from the DB,
or the replication log position) with each cached value, and only allow a set if it is newer than
what is in the cache. Writers do not delete the key; they write a tombstone or the new value
with the new version, so a slow reader carrying an old version is rejected.

```lua
-- KEYS[1] = cache key
-- ARGV[1] = version, ARGV[2] = payload, ARGV[3] = ttl seconds
local current = redis.call('HGET', KEYS[1], 'v')
if current and tonumber(current) >= tonumber(ARGV[1]) then
  return 0
end
redis.call('HSET', KEYS[1], 'v', ARGV[1], 'd', ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
```

```java
private static final String SET_IF_NEWER = """
        local current = redis.call('HGET', KEYS[1], 'v')
        if current and tonumber(current) >= tonumber(ARGV[1]) then return 0 end
        redis.call('HSET', KEYS[1], 'v', ARGV[1], 'd', ARGV[2])
        redis.call('EXPIRE', KEYS[1], ARGV[3])
        return 1
        """;

boolean setIfNewer(String key, long version, String payload, long ttlSeconds) {
    Object r = redis.eval(SET_IF_NEWER, List.of(key),
            List.of(Long.toString(version), payload, Long.toString(ttlSeconds)));
    return Long.valueOf(1L).equals(r);
}
```

The Lua script runs atomically on the Redis server, so the compare-and-set cannot interleave with
another command. On update, the writer calls `setIfNewer(key, newVersion, "", ttl)` with an empty
payload as a tombstone (readers treat an empty payload as a miss), or with the new value.

### Mitigation: leases (Facebook memcache)

The paper *Scaling Memcache at Facebook* (Nishtala et al., NSDI 2013) introduced **leases** to fix
both the stale-set race and the thundering herd:

1. On a miss, memcached returns a **lease token** (a 64-bit value) bound to that key.
2. The client loads from the DB and sets the value **with the token**.
3. If the key was deleted in the meantime, the delete **invalidates the token**, and the set is
   rejected. A stale filler therefore cannot overwrite a newer invalidation.
4. To tame herds, the server hands out a token for a given key at most once every 10 seconds.
   Other clients that miss during that window are told to wait briefly and retry, by which time the
   lease holder has usually filled the value. Facebook reported this reduced peak DB query rate for
   hot keys from about 17,000 to 1,300 per second.
5. Clients that can tolerate slightly stale data may receive the recently deleted value (kept briefly
   after deletion) instead of waiting.

```text
Client A               memcache                 Client B (writer)        DB
  │  get k               │                          │                    │
  │─────────────────────►│                          │                    │
  │  miss + lease L1     │                          │                    │
  │◄─────────────────────│                          │                    │
  │  read DB (old v1)    │                          │  UPDATE v2         │
  │─────────────────────────────────────────────────────────────────────►│
  │                      │  delete k (kills L1)     │                    │
  │                      │◄─────────────────────────│                    │
  │  set k=v1 lease L1   │                          │                    │
  │─────────────────────►│                          │                    │
  │  REJECTED            │                          │                    │
  │◄─────────────────────│                          │                    │
```

Stock open-source memcached does not implement Facebook's lease protocol; you can emulate it in
Redis with a token key and a Lua compare-and-set, similar to the versioning script above.

### Other ideas from the same paper

- **mcsqueal**: daemons tail the MySQL commit log and broadcast deletes to memcache, so invalidation
  is driven by committed data (an early form of CDC-driven invalidation).
- **Gutter pool**: when a memcache server fails, clients send its traffic to a small pool of spare
  servers with short TTLs instead of rehashing onto healthy servers (which could cascade).
- **Cold cluster warmup**: a new cluster reads from a warm cluster on misses instead of the DB.
- **Regional replication**: one region holds the master DB; remote markers prevent reading stale
  data from a replica immediately after a user's own write.

---

## Eviction algorithms

When the cache is full, the eviction policy decides what to drop. The goal is to maximize the hit
ratio for the access pattern, at low CPU and memory cost.

### FIFO and Random

- **FIFO** evicts the oldest inserted entry, regardless of use. Trivial to implement (a queue), but a
  hot key inserted long ago gets evicted. Recent research (S3-FIFO, SOSP 2023) shows that FIFO
  queues plus a small probationary queue and lazy promotion can match or beat LRU while being
  cheaper under concurrency.
- **Random** evicts a random entry. Surprisingly decent for some workloads and needs no metadata.

### LRU (Least Recently Used)

Evict the entry that was accessed longest ago. LRU captures temporal locality well and is the default
mental model. Its weakness is **scan pollution**: a one-time scan of many keys pushes the whole hot
set out.

Implementation: a hash map from key to node, plus a doubly linked list ordered by recency. Both
`get` and `put` are O(1).

```text
  HashMap: key ──► node
                                most recent                 least recent
 ┌──────┐   ┌──────┐   ┌──────┐   ┌──────┐   ┌──────┐
 │ head │◄─►│  k7  │◄─►│  k3  │◄─►│  k9  │◄─►│ tail │      evict tail.prev
 └──────┘   └──────┘   └──────┘   └──────┘   └──────┘
  get(k9): unlink k9, insert after head      put(new): insert after head
```

```java
public final class LruCache<K, V> {

    private final class Node {
        final K key;
        V value;
        Node prev;
        Node next;

        Node(K key, V value) {
            this.key = key;
            this.value = value;
        }
    }

    private final int capacity;
    private final Map<K, Node> index;
    private final Node head = new Node(null, null);
    private final Node tail = new Node(null, null);

    public LruCache(int capacity) {
        if (capacity <= 0) throw new IllegalArgumentException("capacity must be > 0");
        this.capacity = capacity;
        this.index = new HashMap<>(capacity * 2);
        head.next = tail;
        tail.prev = head;
    }

    public synchronized V get(K key) {
        Node n = index.get(key);
        if (n == null) return null;
        unlink(n);
        addFirst(n);
        return n.value;
    }

    public synchronized void put(K key, V value) {
        Node n = index.get(key);
        if (n != null) {
            n.value = value;
            unlink(n);
            addFirst(n);
            return;
        }
        if (index.size() == capacity) {
            Node lru = tail.prev;
            unlink(lru);
            index.remove(lru.key);
        }
        Node fresh = new Node(key, value);
        index.put(key, fresh);
        addFirst(fresh);
    }

    private void unlink(Node n) {
        n.prev.next = n.next;
        n.next.prev = n.prev;
    }

    private void addFirst(Node n) {
        n.next = head.next;
        n.prev = head;
        head.next.prev = n;
        head.next = n;
    }
}
```

The JDK shortcut is `LinkedHashMap` with `accessOrder = true` and an overridden `removeEldestEntry`:

```java
final class SimpleLru<K, V> extends LinkedHashMap<K, V> {
    private final int capacity;

    SimpleLru(int capacity) {
        super(16, 0.75f, true);
        this.capacity = capacity;
    }

    @Override
    protected boolean removeEldestEntry(Map.Entry<K, V> eldest) {
        return size() > capacity;
    }
}
```

Note that **`get` mutates** an access-ordered map, so even reads need a lock. A single global lock
is the reason naive LRU scales poorly on many cores; Caffeine avoids it by recording reads in
striped ring buffers and replaying them onto the policy in batches.

**Variants**:

- **SLRU (segmented LRU)**: a probationary segment for new entries and a protected segment for
  entries hit at least twice. Scans only churn probation.
- **2Q / LRU-K**: require multiple accesses before treating an entry as hot.
- **CLOCK**: entries in a circular buffer with a reference bit; the "hand" clears bits and evicts the
  first entry whose bit is already clear. Approximates LRU with no list manipulation on hits. Used
  by PostgreSQL buffers and OS page replacement.

### LFU (Least Frequently Used) with O(1) operations

LFU evicts the entry with the fewest accesses. It is excellent for stable popularity (the same items
stay hot for a long time) but has two classic problems:

1. **No aging**: an item that was hugely popular yesterday keeps its high count forever. Fix with
   decay (periodically halve counters) or windows.
2. **New items are fragile**: a fresh entry has count 1 and is evicted first, even if it is about to
   become hot.

The naive implementation uses a min-heap (O(log n)). The O(1) design (Shah, Mitra and Matani, 2010)
keeps a list of entries per frequency and tracks the minimum frequency:

```text
 minFreq = 1
 freq 1: [ k5, k8 ]          evict from here (oldest first: k5)
 freq 2: [ k2 ]
 freq 7: [ k1, k4 ]
 on get(k2): move k2 from freq 2 list to freq 3 list; if freq 2 list empties and
             minFreq was 2, minFreq becomes 3
```

```java
public final class LfuCache<K, V> {

    private static final class Entry<V> {
        V value;
        int freq = 1;

        Entry(V value) {
            this.value = value;
        }
    }

    private final int capacity;
    private final Map<K, Entry<V>> entries = new HashMap<>();
    private final Map<Integer, LinkedHashSet<K>> buckets = new HashMap<>();
    private int minFreq;

    public LfuCache(int capacity) {
        this.capacity = capacity;
    }

    public synchronized V get(K key) {
        Entry<V> e = entries.get(key);
        if (e == null) return null;
        touch(key, e);
        return e.value;
    }

    public synchronized void put(K key, V value) {
        if (capacity == 0) return;
        Entry<V> e = entries.get(key);
        if (e != null) {
            e.value = value;
            touch(key, e);
            return;
        }
        if (entries.size() == capacity) evict();
        entries.put(key, new Entry<>(value));
        buckets.computeIfAbsent(1, f -> new LinkedHashSet<>()).add(key);
        minFreq = 1;
    }

    private void touch(K key, Entry<V> e) {
        LinkedHashSet<K> bucket = buckets.get(e.freq);
        bucket.remove(key);
        if (bucket.isEmpty()) {
            buckets.remove(e.freq);
            if (minFreq == e.freq) minFreq++;
        }
        e.freq++;
        buckets.computeIfAbsent(e.freq, f -> new LinkedHashSet<>()).add(key);
    }

    private void evict() {
        LinkedHashSet<K> bucket = buckets.get(minFreq);
        K victim = bucket.iterator().next();
        bucket.remove(victim);
        if (bucket.isEmpty()) buckets.remove(minFreq);
        entries.remove(victim);
    }
}
```

`LinkedHashSet` keeps insertion order within one frequency, so ties are broken by LRU. `minFreq`
only ever needs to go up by one in `touch`, and resets to 1 on insert, which is why it stays O(1).

### ARC (Adaptive Replacement Cache)

ARC (Megiddo and Modha, IBM, FAST 2003) balances recency and frequency automatically:

- **T1**: entries seen once recently (recency).
- **T2**: entries seen at least twice (frequency).
- **B1, B2**: "ghost" lists holding only the keys recently evicted from T1 and T2.
- A target size `p` for T1 adapts: a hit in B1 means "T1 was too small", so `p` grows; a hit in B2
  shrinks `p`.

```text
        ◄──── recency side ─────┬───── frequency side ────►
  ┌─────────────┬───────────────┼───────────────┬─────────────┐
  │ B1 (ghosts) │      T1       │      T2       │ B2 (ghosts) │
  └─────────────┴───────────────┼───────────────┴─────────────┘
                     target p ◄─┴─► c - p        (c = capacity)
```

ARC is scan-resistant and self-tuning. It is used in ZFS (as the ARC/L2ARC) and was briefly used in
PostgreSQL 8.0 before being replaced partly because of IBM's patent on it.

### W-TinyLFU (Caffeine)

W-TinyLFU (Einziger, Friedman and Manes, "TinyLFU: A Highly Efficient Cache Admission Policy",
ACM Transactions on Storage, 2017) is what Caffeine uses. Its insight: separate **admission** (should
this new item enter the cache at all?) from **eviction** (which item leaves?), and make admission
decisions using a compact, approximate frequency history.

```text
 new entry
     │
     ▼
 ┌──────────────┐  evicted   ┌───────────────────┐       ┌──────────────────────────┐
 │ Window LRU   │ ─────────► │ TinyLFU admission │ ────► │ Main cache (SLRU)        │
 │ (~1% of size)│ candidate  │ freq(candidate) > │ admit │  probation (20%)         │
 └──────────────┘            │ freq(victim) ?    │       │  protected (80%)         │
                             └─────────┬─────────┘       └────────────┬─────────────┘
                                       │ reject                       │ victim from
                                       ▼                              │ probation
                                   discarded ◄────────────────────────┘ (if admitted)
```

How it works:

1. New entries go into a small **window LRU** (about 1% of capacity by default). This gives brand
   new items a chance to build up frequency, which fixes LFU's "new items are fragile" problem and
   handles bursty recency.
2. When the window overflows, its LRU victim becomes a **candidate** for the main cache. The main
   cache's own victim (from the probation segment of the SLRU) is compared with it.
3. A **frequency sketch** estimates how often each key was seen. Caffeine uses a Count-Min Sketch
   with 4-bit counters, so it tracks frequencies for many more keys than the cache holds, in a few
   bytes per cache entry.
4. The candidate is admitted only if its estimated frequency is higher than the victim's. One-hit
   wonders and scans are rejected at the door and never displace the hot set.
5. **Aging**: after a sample of accesses (about 10x the maximum size), all counters are halved, so
   old popularity decays.
6. **Adaptivity**: Caffeine uses hill climbing to resize the window versus main region at runtime,
   giving more room to recency for bursty workloads and to frequency for stable ones.
7. To prevent an attacker from exploiting predictable admission (hash flooding), Caffeine adds a
   little randomness to admission for candidates with moderate frequency.

In published simulations W-TinyLFU matches or beats ARC and LIRS on most traces, with lower
metadata overhead and good concurrency, which is why Caffeine replaced Guava's LRU cache as the
recommended Java cache.

### Redis eviction: approximated LRU and LFU

Redis evicts only when `maxmemory` is set and reached. It does **not** keep an exact LRU list (that
would cost two pointers per key). Instead:

- Each object stores a 24-bit field. Under LRU policies it holds a coarse **access clock**; under
  LFU policies it holds an 8-bit **logarithmic access counter** plus a 16-bit **last decrement time**.
- On eviction, Redis **samples** `maxmemory-samples` keys (default 5) and keeps the best candidates
  in an **eviction pool** of 16 entries across rounds, evicting the best one. Raising samples to 10
  gets very close to true LRU at more CPU cost.
- The LFU counter is a Morris-style probabilistic counter: the chance of incrementing gets smaller
  as the counter grows, controlled by `lfu-log-factor` (default 10), so 8 bits can represent
  millions of hits. The counter decays by one for every `lfu-decay-time` minutes (default 1) of
  inactivity. New keys start at 5 so they are not evicted instantly.

`maxmemory-policy` options:

| Policy            | Candidates                 | Rule                                      |
| ----------------- | -------------------------- | ----------------------------------------- |
| `noeviction`      | none                       | Writes fail with an OOM error (default)   |
| `allkeys-lru`     | all keys                   | Approximated LRU                          |
| `allkeys-lfu`     | all keys                   | Approximated LFU                          |
| `allkeys-random`  | all keys                   | Random                                    |
| `volatile-lru`    | keys with a TTL            | Approximated LRU                          |
| `volatile-lfu`    | keys with a TTL            | Approximated LFU                          |
| `volatile-random` | keys with a TTL            | Random                                    |
| `volatile-ttl`    | keys with a TTL            | Shortest remaining TTL first              |

```bash
redis-cli CONFIG SET maxmemory 8gb
redis-cli CONFIG SET maxmemory-policy allkeys-lfu
redis-cli OBJECT FREQ user:v1:42      # only valid under an LFU policy
redis-cli OBJECT IDLETIME user:v1:42  # seconds since last access (LRU clock)
```

Rules of thumb: a pure cache uses `allkeys-lru` or `allkeys-lfu`. A Redis that mixes cache keys
(with TTL) and persistent keys (no TTL, such as queues or locks) uses a `volatile-*` policy so that
only cache keys are evicted, but then if no key has a TTL, writes fail as if `noeviction`. Better
still, run separate Redis instances for cache data and for state you cannot lose. Newer Redis
versions may add policies; check the docs for your version.

### Comparison

| Algorithm   | Captures                 | Scan resistant | Metadata / CPU              | Used by                         |
| ----------- | ------------------------ | -------------- | --------------------------- | ------------------------------- |
| FIFO        | Insertion order          | No             | Minimal                     | Simple caches, S3-FIFO variants |
| LRU         | Recency                  | No             | 2 pointers per entry, lock  | Most libraries, Memcached (SLRU)|
| CLOCK       | Approximate recency      | Somewhat       | 1 bit per entry             | PostgreSQL, OS page cache       |
| LFU         | Frequency                | Yes            | Counter + buckets           | Redis LFU (approximated)        |
| ARC         | Recency + frequency      | Yes            | 2 lists + 2 ghost lists     | ZFS                             |
| W-TinyLFU   | Recency + frequency      | Yes            | Count-Min Sketch, SLRU      | Caffeine, Ristretto (Go)        |

---

## Expiration and TTL design

### How expiry works in Redis

Redis removes expired keys two ways:

- **Lazily**: when a key is accessed and found expired, it is deleted and treated as missing.
- **Actively**: a background cycle periodically samples keys that have a TTL and deletes expired
  ones, repeating while a large fraction of the sample is expired.

So memory used by expired-but-untouched keys is reclaimed gradually, not instantly. Expiry is
replicated to replicas as explicit `DEL` commands from the primary.

### Choosing TTLs

| Data                                 | Typical TTL            | Reasoning                                 |
| ------------------------------------ | ---------------------- | ----------------------------------------- |
| Static reference data (countries)    | Hours to days          | Rarely changes; explicit purge on change  |
| Product catalogue                    | Minutes, plus eviction | Changes occasionally; invalidate on write |
| User profile                         | 5–60 minutes           | Invalidate on write; TTL as safety net    |
| Feed / timeline fragments            | Seconds to minutes     | Freshness matters, very high read volume  |
| Negative results ("not found")       | 30–300 seconds         | Prevent penetration, allow late creation  |
| Rate-limit windows                   | Equal to the window    | Semantics, not caching                    |
| Session                              | Sliding, e.g. 30 min   | Refresh TTL on access                     |

The TTL is a contract: "this data may be up to TTL seconds stale". Pick it by asking the product
owner how stale each piece of data may be, not by guessing.

### TTL jitter

If a batch job warms 1 million keys with `EX 3600`, they all expire at the same moment an hour
later, and the database sees a synchronized wave of misses. Add randomness:

```java
static long jitteredTtlSeconds(long baseSeconds, double spread) {
    long delta = (long) (baseSeconds * spread);
    return baseSeconds + ThreadLocalRandom.current().nextLong(-delta, delta + 1);
}

// 3600 s +/- 10%: anywhere between 3240 and 3960 seconds
long ttl = jitteredTtlSeconds(3600, 0.10);
```

### Sliding vs absolute expiration

- **Absolute** (`expireAfterWrite`, `SET ... EX`): data is never older than the TTL. Use for data
  that changes in the source.
- **Sliding** (`expireAfterAccess`, `EXPIRE` on each read): keeps hot entries alive indefinitely.
  Good for sessions, dangerous for data that changes, because a hot stale entry never refreshes.

---

## Failure modes

### Cache stampede (thundering herd, dog-piling)

A hot key expires or is deleted. Thousands of concurrent requests miss at the same instant, all
query the database for the same value, and all write it back. The database spikes, latency rises,
more requests pile up, and it can cascade into an outage.

```text
             key "home:feed" expires at t0
 t0 + 1ms:  req1 miss ─┐
            req2 miss ─┤
            req3 miss ─┼──► 5,000 identical queries ──► database overload
            ...       ─┤
            reqN miss ─┘
```

#### Fix 1: request coalescing (single flight) within a process

Only one caller per key performs the load; the others wait for its result. Go has
`golang.org/x/sync/singleflight`; in Java you can build it on `ConcurrentHashMap` and
`CompletableFuture`:

```java
public final class SingleFlight<K, V> {

    private final ConcurrentHashMap<K, CompletableFuture<V>> inFlight = new ConcurrentHashMap<>();

    public V load(K key, Function<K, V> loader) {
        CompletableFuture<V> mine = new CompletableFuture<>();
        CompletableFuture<V> existing = inFlight.putIfAbsent(key, mine);
        if (existing != null) {
            return existing.join();
        }
        try {
            V value = loader.apply(key);
            mine.complete(value);
            return value;
        } catch (RuntimeException e) {
            mine.completeExceptionally(e);
            throw e;
        } finally {
            inFlight.remove(key, mine);
        }
    }
}
```

```java
private final SingleFlight<String, String> flights = new SingleFlight<>();

String getFeed(String key) {
    String cached = redis.get(key);
    if (cached != null) return cached;
    return flights.load(key, k -> {
        String fresh = feedService.build(k);
        redis.setex(k, jitteredTtlSeconds(60, 0.1), fresh);
        return fresh;
    });
}
```

Waiters get a `CompletionException` wrapping the loader's failure. With 50 app instances this
reduces 5,000 queries to at most 50, one per instance. Caffeine's `LoadingCache.get` and
`AsyncLoadingCache` give the same guarantee automatically.

#### Fix 2: distributed lock (mutex) across instances

Only the holder of a short Redis lock rebuilds; others briefly wait and re-read the cache, or serve
a stale copy.

```java
private static final String RELEASE = """
        if redis.call('GET', KEYS[1]) == ARGV[1] then
          return redis.call('DEL', KEYS[1])
        end
        return 0
        """;

String getWithLock(String key) throws InterruptedException {
    for (int attempt = 0; attempt < 50; attempt++) {
        String cached = redis.get(key);
        if (cached != null) return cached;

        String lockKey = "lock:" + key;
        String token = UUID.randomUUID().toString();
        String ok = redis.set(lockKey, token, SetParams.setParams().nx().px(10_000));
        if ("OK".equals(ok)) {
            try {
                String fresh = loadFromDb(key);
                redis.setex(key, jitteredTtlSeconds(300, 0.1), fresh);
                return fresh;
            } finally {
                redis.eval(RELEASE, List.of(lockKey), List.of(token));
            }
        }
        Thread.sleep(20);
    }
    return loadFromDb(key); // give up waiting rather than fail the request
}
```

- The lock has an expiry (`PX 10000`) so a crashed holder does not block everyone forever.
- Release checks the token so a holder whose lock already expired cannot delete someone else's lock.
- This lock is an **efficiency** lock (at worst two processes rebuild the same value), not a
  correctness lock. Do not use a single-Redis lock to protect correctness-critical work; for that
  you need fencing tokens and a consensus-backed store.

#### Fix 3: stale-while-revalidate (soft TTL)

Store the value with a **logical expiry** inside the payload and a longer physical TTL. When the
logical expiry passes, one caller (guarded by a lock) refreshes in the background, and everyone
keeps receiving the stale value meanwhile. Readers never block on the database for hot keys.

```java
record Envelope<V>(V value, long softExpiryEpochMs) {
    boolean isSoftExpired() {
        return System.currentTimeMillis() >= softExpiryEpochMs;
    }
}
```

#### Fix 4: probabilistic early expiration (XFetch)

From *Optimal Probabilistic Cache Stampede Prevention* (Vattani, Chierichetti and Lowenstein,
VLDB 2015). Each reader independently decides to recompute **before** expiry with a probability
that rises as expiry approaches. No coordination is needed.

Store with each value: `delta` (how long the last recomputation took) and `expiry`. On every read:

```text
recompute if:   now - delta * beta * ln(rand())  >=  expiry

rand()  uniform in (0, 1], so ln(rand()) <= 0 and the subtraction adds a positive gap
beta    tuning knob, default 1.0; > 1 favours earlier recomputation
```

Intuition: `-ln(rand())` is exponentially distributed with mean 1. A reader at time `now` acts as
if it were looking `delta * beta * Exp(1)` into the future. Far from expiry this almost never
triggers; within a few `delta` of expiry it becomes likely; expensive values (large `delta`) start
refreshing earlier. Typically exactly one request refreshes, slightly before expiry.

```java
record CachedValue<V>(V value, long deltaMs, long expiryEpochMs) {}

static boolean shouldRecompute(CachedValue<?> cv, double beta) {
    double r = ThreadLocalRandom.current().nextDouble();
    double gapMs = cv.deltaMs() * beta * -Math.log(r);
    return System.currentTimeMillis() + gapMs >= cv.expiryEpochMs();
}

<V> V fetch(String key, double beta, Supplier<V> compute, Duration ttl) {
    CachedValue<V> cv = readFromCache(key);
    if (cv == null || shouldRecompute(cv, beta)) {
        long start = System.currentTimeMillis();
        V value = compute.get();
        long delta = System.currentTimeMillis() - start;
        cv = new CachedValue<>(value, delta, System.currentTimeMillis() + ttl.toMillis());
        writeToCache(key, cv, ttl);
    }
    return cv.value();
}
```

#### Fix 5: never expire hot keys

For a handful of known-hot keys (home page, config), refresh them from a scheduled job and do not
give them a TTL shorter than the refresh interval.

### Cache penetration

Requests for keys that do not exist in the database always miss the cache (there is nothing to
cache) and always hit the database. Often caused by bugs or by attackers enumerating IDs
(`/users/-1`, random UUIDs).

Fixes:

1. **Negative caching**: cache a sentinel ("not found") with a short TTL.

   ```java
   private static final String NULL_SENTINEL = "\u0000NULL";

   Optional<String> get(String key) {
       String v = redis.get(key);
       if (NULL_SENTINEL.equals(v)) return Optional.empty();
       if (v != null) return Optional.of(v);
       Optional<String> fromDb = loadFromDb(key);
       redis.setex(key, fromDb.isPresent() ? 3600 : 60, fromDb.orElse(NULL_SENTINEL));
       return fromDb;
   }
   ```

   Downsides: random keys fill memory with sentinels (use a short TTL and LRU/LFU eviction), and a
   key created right after its sentinel was cached is invisible until the sentinel expires (delete
   the sentinel on create).

2. **Input validation**: reject malformed IDs before touching any storage.
3. **Bloom filter** in front of the cache containing every key that exists.
4. **Rate limiting** per client for repeated misses.

#### Bloom filter math

A Bloom filter is a bit array of `m` bits with `k` hash functions. Insert sets `k` bits; lookup
checks `k` bits. If any bit is 0, the key is **definitely absent**; if all are 1, it is **probably
present**. No false negatives; false positives with probability:

```text
p ≈ (1 - e^(-k * n / m))^k            n = number of inserted keys

optimal k = (m / n) * ln 2
bits needed for a target p:  m = -n * ln(p) / (ln 2)^2  ≈ 9.585 * n bits for p = 1%
```

Worked example: 10 million existing user IDs, target false positive rate 1%:

```text
m = 10,000,000 * 9.585 ≈ 95.9 million bits ≈ 12 MB
k = 9.585 * 0.693 ≈ 6.6, round to 7 hash functions
```

Each 10x improvement in `p` costs about 4.8 more bits per key (0.1% needs about 14.4 bits per key).
Twelve megabytes to protect a table of 10 million rows is a great deal.

Caveats:

- Standard Bloom filters do not support deletion. Use a **counting Bloom filter** or a **cuckoo
  filter** if keys are deleted, or rebuild periodically.
- The filter must be updated on every insert, or new keys get rejected (false negatives caused by
  your own staleness).
- Redis offers Bloom and cuckoo filters through the RedisBloom module (bundled in Redis Stack and in
  Redis 8 distributions):

```bash
BF.RESERVE users:exists 0.01 10000000
BF.ADD users:exists 42
BF.EXISTS users:exists 42        # 1 = maybe present
BF.EXISTS users:exists 999999999 # 0 = definitely absent
```

In Java, Guava provides `BloomFilter.create(Funnels.longFunnel(), 10_000_000, 0.01)` for an
in-process filter.

### Cache avalanche

A large part of the cache becomes unavailable at once, and the database receives the full
uncached load. Causes and fixes:

| Cause                                   | Fix                                                         |
| --------------------------------------- | ----------------------------------------------------------- |
| Many keys share the same TTL            | TTL jitter                                                  |
| Cache cluster crash or network split    | Replicas, multi-AZ, Redis Cluster, fast failover            |
| Cold cache after restart or deploy      | Warm up from a snapshot or a warm cluster before taking traffic |
| Flushing the whole cache (`FLUSHALL`)   | Disable dangerous commands (`rename-command`, ACLs)         |
| Database cannot survive the miss rate   | Circuit breakers, load shedding, rate limits on the DB path |

Also design for **graceful degradation**: when the cache is unavailable, serve a reduced experience
(defaults, stale data from a local cache, a "try again" response for non-critical features) rather
than sending everything to the database. Capacity-plan the database for some realistic fraction of
cache-off traffic.

### Hot keys

A single key receives so much traffic that the one cache node holding it saturates (CPU or network)
even though the cluster as a whole is idle. Examples: a celebrity's profile, a flash-sale product,
a global configuration key, the leaderboard top 10. In Redis Cluster all reads of a key go to one
shard's primary by default.

Detection:

- `redis-cli --hotkeys` (requires an LFU `maxmemory-policy`), `--bigkeys`, `MONITOR` sampling
  (expensive; use briefly), client-side metrics per key prefix, or proxy-level statistics.

Fixes:

1. **Local cache in front** (L1 Caffeine with a 1–5 second TTL). At 100 app instances, a key read
   500,000 times per second becomes at most 100 Redis reads per TTL period.
2. **Key replicas / key splitting**: store `N` copies under different keys that hash to different
   shards, read a random copy, write all copies.

   ```java
   private static final int REPLICAS = 8;

   String readHot(String key) {
       int r = ThreadLocalRandom.current().nextInt(REPLICAS);
       return redis.get(key + "#" + r);
   }

   void writeHot(String key, String value, long ttl) {
       for (int i = 0; i < REPLICAS; i++) {
           redis.setex(key + "#" + i, ttl, value);
       }
   }
   ```

3. **Read from replicas**: route reads for hot keys to Redis replicas (accepting replication lag).
4. **Split big values**: a 5 MB hash read in full on every request is a "big key"; break it into
   smaller keys or hash fields and fetch only what is needed.
5. For **hot write keys** (counters), shard the counter (`likes:42:0` to `likes:42:15`, increment a
   random shard, sum on read), or aggregate in memory and flush periodically (write-behind).

---

## Consistency and invalidation

> There are only two hard things in Computer Science: cache invalidation and naming things.
> -- Phil Karlton

### Levels of freshness

| Approach                          | Staleness bound                   | Complexity |
| --------------------------------- | --------------------------------- | ---------- |
| TTL only                          | TTL                               | Lowest     |
| Delete on write in app code       | Race windows, missed code paths   | Low        |
| Versioned sets / leases           | Close to zero for the cache       | Medium     |
| CDC-driven invalidation           | Replication delay (sub-second)    | Medium     |
| Write-through with a single owner | Near zero if every write goes via | Medium     |
| No cache, read from primary       | Zero                              | Trivial    |

Invalidation in application code has a structural weakness: any write that bypasses that code path
(a migration script, an admin tool, another service, a manual SQL fix) leaves the cache stale.

### Change Data Capture (CDC) invalidation

Make the database's own commit log the source of invalidation. **Debezium** reads the MySQL binlog
or PostgreSQL logical replication stream (`pgoutput`), publishes a change event per row to Kafka,
and an invalidator service deletes the affected keys.

```text
┌──────────┐ binlog / WAL  ┌──────────┐ events ┌───────┐  consume  ┌─────────────┐  DEL
│ Database │ ────────────► │ Debezium │ ─────► │ Kafka │ ────────► │ Invalidator │ ─────►
└──────────┘               └──────────┘        └───────┘           └─────────────┘  Redis
```

A Debezium change event value (simplified) looks like:

```json
{
  "before": { "id": 42, "name": "Ada", "version": 6 },
  "after":  { "id": 42, "name": "Ada Lovelace", "version": 7 },
  "source": { "connector": "postgresql", "table": "users", "lsn": 24023128 },
  "op": "u",
  "ts_ms": 1793450000000
}
```

```java
public final class CacheInvalidator implements Runnable {
    private final KafkaConsumer<String, String> consumer;
    private final JedisPooled redis;
    private final ObjectMapper json = new ObjectMapper();

    public CacheInvalidator(KafkaConsumer<String, String> consumer, JedisPooled redis) {
        this.consumer = consumer;
        this.redis = redis;
    }

    @Override
    public void run() {
        consumer.subscribe(List.of("dbserver.public.users"));
        while (!Thread.currentThread().isInterrupted()) {
            ConsumerRecords<String, String> records = consumer.poll(Duration.ofMillis(500));
            for (ConsumerRecord<String, String> rec : records) {
                if (rec.value() == null) continue; // tombstone after a delete event
                try {
                    JsonNode event = json.readTree(rec.value());
                    JsonNode row = event.path("after").isNull()
                            ? event.path("before") : event.path("after");
                    redis.del("user:v1:" + row.path("id").asLong());
                } catch (JsonProcessingException e) {
                    throw new IllegalStateException("bad CDC event at " + rec.offset(), e);
                }
            }
            consumer.commitSync();
        }
    }
}
```

Benefits: catches every write path, ordered per row (Kafka partition by primary key), and retries
are safe because `DEL` is idempotent. Costs: extra infrastructure, and invalidation is asynchronous
(typically tens to hundreds of milliseconds), so a user may not see their own write in the cache
immediately. Combine with delete-in-app for the writer's own request if needed.

### Pub/sub invalidation for local caches

A local L1 cache on each instance is invisible to a Redis `DEL`. Broadcast invalidations:

```java
// on write, after the DB commit
redis.del("product:" + id);
redis.publish("invalidate:product", Long.toString(id));

// on every instance, a subscriber thread
redis.subscribe(new JedisPubSub() {
    @Override
    public void onMessage(String channel, String message) {
        localProducts.invalidate(Long.parseLong(message));
    }
}, "invalidate:product");
```

Redis pub/sub is **fire-and-forget**: an instance that is disconnected while the message is sent
misses it. Mitigate with a short local TTL (seconds), by clearing the whole L1 on reconnect, or by
using a durable channel (Redis Streams, Kafka) with per-instance consumers.

### Near cache coherence and Redis client-side caching

A **near cache** is a local copy of a remote cache's entries kept coherent by server-pushed
invalidations. Hazelcast and Apache Ignite have built-in near caches. Redis 6 added server-assisted
**client-side caching** via `CLIENT TRACKING`:

- **Default mode**: the server remembers which keys each client has read and sends an invalidation
  message when any of them changes. Costs server memory per tracked key.
- **Broadcasting mode** (`CLIENT TRACKING ON BCAST PREFIX user:`): the server remembers nothing per
  key and broadcasts invalidations for every change to keys matching the registered prefixes.
- Invalidation messages arrive over RESP3 push messages, or via a redirect connection for RESP2.
- If the connection drops, the client must flush its local cache, because it may have missed
  invalidations.

Recent versions of the Lettuce and Jedis clients expose client-side caching APIs; check the current
client docs, since these APIs have changed across versions.

---

## Distributed cache architecture

### Sharding with consistent hashing

With `node = hash(key) % N`, adding one node remaps about `N/(N+1)` of all keys, which is a cache
avalanche. **Consistent hashing** places nodes and keys on a hash ring; each key belongs to the next
node clockwise, so adding or removing a node moves only about `1/N` of keys. **Virtual nodes**
(each physical node appears at many ring positions, often 100–200) even out the distribution and
spread a failed node's load across many survivors. Memcached clients use the **Ketama** consistent
hashing scheme. The databases page covers the ring in more detail.

### Redis Cluster

Redis Cluster does not use a ring. It divides the key space into **16,384 hash slots**:

```text
slot = CRC16(key) mod 16384

┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
│ Shard A primary      │  │ Shard B primary      │  │ Shard C primary      │
│ slots 0 - 5460       │  │ slots 5461 - 10922   │  │ slots 10923 - 16383  │
└──────────┬───────────┘  └──────────┬───────────┘  └──────────┬───────────┘
           │ async replication       │                         │
┌──────────▼───────────┐  ┌──────────▼───────────┐  ┌──────────▼───────────┐
│ Shard A replica      │  │ Shard B replica      │  │ Shard C replica      │
└──────────────────────┘  └──────────────────────┘  └──────────────────────┘
```

- Clients cache the slot-to-node map. A request to the wrong node gets `-MOVED <slot> <host:port>`
  (permanent, refresh your map) or `-ASK` (during a slot migration, retry once on the target).
- **Hash tags**: only the part inside `{...}` is hashed, so `{user:42}:profile` and
  `{user:42}:cart` share a slot. Multi-key commands, transactions, and Lua scripts only work when all
  keys are in one slot. Over-using one tag creates a hot slot.
- Nodes exchange state with a **gossip** protocol over the cluster bus. If a majority of primaries
  agree a primary is unreachable, one of its replicas is promoted.
- Why 16,384 slots: the slot bitmap in heartbeat messages is 2 KB, and the designers expected
  clusters of at most around 1,000 primaries, so more slots were unnecessary.
- Resharding moves slots between nodes online (`redis-cli --cluster reshard` / `rebalance`), key by
  key with `MIGRATE`.

### Replication and Sentinel

- Replication is **asynchronous**: the primary acknowledges a write before replicas receive it.
- `WAIT numreplicas timeout` blocks until that many replicas acknowledged the preceding writes. It
  reduces but does not eliminate the chance of losing a write on failover (it is not a consensus
  protocol).
- `min-replicas-to-write` and `min-replicas-max-lag` make a primary refuse writes when it cannot see
  enough healthy replicas, which limits the damage when it is partitioned away.
- **Redis Sentinel** provides high availability for non-cluster deployments: several Sentinel
  processes monitor the primary, agree (quorum) that it is down, elect a leader Sentinel, promote a
  replica, and tell clients the new address. Run at least three Sentinels in separate failure
  domains.

### Persistence: RDB and AOF

| | RDB snapshot | AOF (append-only file) |
| --- | --- | --- |
| What | Point-in-time binary dump via `fork()` (`BGSAVE`) | Log of every write command |
| Durability | Lose everything since last snapshot | `appendfsync always` (each write), `everysec` (up to about 1 s lost, default), `no` (OS decides) |
| Restart speed | Fast to load | Slower (replay), mitigated by rewrites |
| Cost | Fork can pause with large heaps; copy-on-write memory spike | Disk I/O; periodic `BGREWRITEAOF` compaction |

Modern Redis can write an RDB preamble inside the AOF (`aof-use-rdb-preamble yes`) to combine fast
loading with fine-grained durability, and Redis 7 split the AOF into a base file plus incremental
files (multi-part AOF). For a **pure cache**, persistence mainly speeds up warm restarts; many teams
disable it. Be aware that `fork()` on a 50 GB instance can stall for hundreds of milliseconds and
temporarily needs extra memory for copy-on-write pages, so leave headroom (often 25% or more).

### Failover and data loss

Because replication is asynchronous, this sequence loses acknowledged writes:

```text
t1  client writes X to primary P, P replies OK
t2  P crashes before replicating X
t3  replica R is promoted; R never saw X
t4  old P restarts as a replica of R and discards X
```

During a network partition, a primary on the minority side can keep accepting writes until
`cluster-node-timeout` (or Sentinel's `down-after-milliseconds`) passes, and those writes are lost
when the partition heals. This is acceptable for a cache (the database is the source of truth),
which is exactly why Redis should not be the only copy of data you cannot lose without careful
configuration or a consensus-based product.

---

## Redis data structures and use cases

| Structure     | Key commands                                    | Typical use                                   |
| ------------- | ----------------------------------------------- | --------------------------------------------- |
| String        | `GET`, `SET k v EX 60 NX`, `INCR`, `MGET`       | Cached objects, counters, locks, flags        |
| Hash          | `HSET`, `HGET`, `HGETALL`, `HINCRBY`            | Objects with fields, partial updates          |
| List          | `LPUSH`, `RPOP`, `LRANGE`, `LTRIM`, `BLMOVE`    | Recent items, simple queues                   |
| Set           | `SADD`, `SISMEMBER`, `SINTER`, `SCARD`          | Tags, unique visitors (small), relationships  |
| Sorted set    | `ZADD`, `ZINCRBY`, `ZRANGE ... REV`, `ZRANGEBYSCORE` | Leaderboards, priority queues, sliding windows |
| Stream        | `XADD`, `XREADGROUP`, `XACK`, `XPENDING`        | Durable event log with consumer groups        |
| HyperLogLog   | `PFADD`, `PFCOUNT`, `PFMERGE`                   | Approximate distinct counts, 12 KB, ~0.81% error |
| Bitmap        | `SETBIT`, `GETBIT`, `BITCOUNT`                  | Daily active flags per user ID                |
| Geospatial    | `GEOADD`, `GEOSEARCH`                           | Nearby drivers, stores                        |
| Pub/Sub       | `PUBLISH`, `SUBSCRIBE`                          | Fire-and-forget notifications, invalidation   |

Examples:

```bash
# Cached object with TTL; only set if absent
SET product:v1:42 '{"id":42,"name":"Kettle"}' EX 300 NX

# Object as a hash: update one field without rewriting the blob
HSET user:42 name "Ada" plan "pro"
HINCRBY user:42 logins 1

# Leaderboard: top 10 with scores
ZINCRBY game:leaderboard 50 player:7
ZRANGE game:leaderboard 0 9 REV WITHSCORES

# Recent activity capped at 100 entries
LPUSH user:42:recent "viewed:product:9"
LTRIM user:42:recent 0 99

# Unique daily visitors, approximately
PFADD visitors:2026-10-31 user:42 user:43
PFCOUNT visitors:2026-10-31
```

### Rate limiting (fixed window) with Lua

```lua
-- KEYS[1] = rate key, ARGV[1] = limit, ARGV[2] = window seconds
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
if current > tonumber(ARGV[1]) then
  return 0
end
return 1
```

Running `INCR` and `EXPIRE` inside one script avoids the bug where a crash between the two leaves a
counter with no TTL that blocks a user forever. A sliding-window variant stores request timestamps
in a sorted set, removes old ones with `ZREMRANGEBYSCORE`, and counts with `ZCARD`.

### Distributed lock caveats

`SET lock:k <token> NX PX 30000` plus a token-checked Lua release is a reasonable lock for
efficiency. It is not safe for correctness under GC pauses, clock jumps, or failover (the lock can
be held by two clients at once). Martin Kleppmann's essay *How to do distributed locking* (2016)
explains why, and why the multi-node Redlock algorithm does not fix it; the remedy is a **fencing
token** checked by the protected resource, typically issued by ZooKeeper or etcd.

---

## Cache sizing and monitoring

### Sizing

1. **Estimate the working set**: number of hot keys times average serialized size (key + value)
   plus per-key overhead. Redis adds tens of bytes of metadata per key, more for small hashes and
   sets; measure real numbers with `MEMORY USAGE <key>` on sample data.
2. **Add headroom** for fragmentation (`mem_fragmentation_ratio` is often 1.1–1.5), replication
   buffers, fork copy-on-write during persistence, and growth.
3. **Use the miss-ratio curve**: replay a production trace (or sample keys) against simulated cache
   sizes and plot the hit ratio. Pick the knee of the curve rather than "as big as possible".
4. **Shrink values**: compact serialization (Protobuf, Kryo, Smile) instead of verbose JSON,
   compression for large values, and only cache the fields you read.

Worked example: 20 million user profiles, 15% hot at peak, 1 KB serialized, about 80 bytes
overhead:

```text
3,000,000 keys * (1,024 + 80) bytes ≈ 3.3 GB
x 1.3 fragmentation and buffers      ≈ 4.3 GB
x 2 for one replica per shard        ≈ 8.6 GB total, e.g. 3 shards of ~1.5 GB data each
```

### Metrics to monitor

| Metric                                   | Source                                  | Why it matters                              |
| ---------------------------------------- | --------------------------------------- | ------------------------------------------- |
| Hit ratio (per key prefix ideally)       | `keyspace_hits / (hits + misses)`, app  | Primary effectiveness measure               |
| Evictions                                | `evicted_keys`                          | Cache too small or TTLs too long            |
| Expirations                              | `expired_keys`                          | Validates TTL design                        |
| Memory used vs maxmemory                 | `used_memory`, `maxmemory`              | Capacity                                    |
| Fragmentation                            | `mem_fragmentation_ratio`               | Above ~1.5 wastes RAM                       |
| Latency p50 / p99                        | Client metrics, `LATENCY` commands      | Slow commands block single-threaded Redis   |
| Slow commands                            | `SLOWLOG GET`                           | `KEYS *`, big `HGETALL`, large `LRANGE`     |
| Connected / blocked clients              | `connected_clients`, `blocked_clients`  | Connection leaks, blocking calls            |
| Replication lag                          | `INFO replication` offsets              | Staleness of replica reads, failover risk   |
| Network bandwidth per node               | Host metrics                            | Hot keys and big values saturate NICs       |
| DB load from misses                      | DB metrics                              | The real cost of a dropping hit ratio       |

Instrument the application, not just Redis: record hits, misses, load time, and errors per cache
name or key prefix (Micrometer integrates with Caffeine's `recordStats()`). A global Redis hit
ratio of 95% can hide one important prefix at 20%.

Operational rules: never run `KEYS *` in production (use `SCAN`), avoid huge values and huge
collections, set client timeouts (tens of milliseconds) so a sick cache fails fast, and protect the
database with a circuit breaker on the miss path.

---

## Multi-level caching

Combine a tiny, very fast per-instance L1 with a large shared L2:

```text
 ┌──────────────────────────────┐   ┌──────────────────────────────┐
 │ App instance 1               │   │ App instance 2               │
 │  ┌────────────────────────┐  │   │  ┌────────────────────────┐  │
 │  │ L1 Caffeine (TTL 5 s)  │  │   │  │ L1 Caffeine (TTL 5 s)  │  │
 │  └───────────┬────────────┘  │   │  └───────────┬────────────┘  │
 └──────────────┼───────────────┘   └──────────────┼───────────────┘
                │      invalidation pub/sub        │
                ▼                                  ▼
 ┌─────────────────────────────────────────────────────────────────┐
 │ L2 Redis Cluster (TTL 10 min)                                   │
 └────────────────────────────────┬────────────────────────────────┘
                                  ▼
 ┌─────────────────────────────────────────────────────────────────┐
 │ Database (source of truth)                                      │
 └─────────────────────────────────────────────────────────────────┘
```

```java
public final class TwoLevelCache {
    private final Cache<String, String> l1 = Caffeine.newBuilder()
            .maximumSize(50_000)
            .expireAfterWrite(Duration.ofSeconds(5))
            .build();
    private final JedisPooled l2;
    private final Function<String, String> loader;

    public TwoLevelCache(JedisPooled l2, Function<String, String> loader) {
        this.l2 = l2;
        this.loader = loader;
    }

    public String get(String key) {
        return l1.get(key, k -> {
            String v = l2.get(k);
            if (v != null) return v;
            String fresh = loader.apply(k);
            l2.setex(k, jitteredTtlSeconds(600, 0.1), fresh);
            return fresh;
        });
    }

    public void invalidate(String key) {
        l2.del(key);
        l2.publish("invalidate", key);
        l1.invalidate(key);
    }

    static long jitteredTtlSeconds(long base, double spread) {
        long delta = (long) (base * spread);
        return base + ThreadLocalRandom.current().nextLong(-delta, delta + 1);
    }
}
```

Design rules:

- `l1.get(key, loader)` runs the loader at most once per key at a time in each instance, so the L1
  also acts as single flight for L2 misses.
- L1 TTL must be **short** because coherence is best-effort. Its staleness bound is roughly "L1 TTL
  plus invalidation delay".
- Only put data in L1 that is hot and tolerates seconds of staleness (config, feature flags, product
  details, hot keys).
- Size L1 by heap budget, not by key count alone; large L1 caches create GC pressure.
- Each level multiplies staleness and debugging complexity. Add a level only when metrics show the
  benefit.

---

## Common mistakes and anti-patterns

- **Caching without a TTL.** Any missed invalidation becomes a permanent bug.
- **Treating the cache as the source of truth** (only writing to Redis) without durable persistence
  and replication designed for it.
- **Update-on-write instead of delete-on-write**, or deleting before the DB commit.
- **Deleting inside the DB transaction**: the transaction can roll back, or a reader can refill from
  the pre-commit state before commit completes.
- **Unbounded local caches** (`static Map` that only grows) causing memory leaks and GC storms.
- **Caching per-user data in shared HTTP caches**: missing `private` or `Vary` leaks one user's data
  to another through a CDN. This has caused real incidents.
- **Huge values and huge collections**: a 10 MB value saturates the network; `HGETALL` on a million
  fields blocks single-threaded Redis.
- **Chatty access**: 50 sequential `GET`s per request. Use `MGET`, pipelining, or a hash.
- **Caching everything**: low-reuse data evicts useful data and adds a network hop to every miss.
- **No stampede protection** on expensive hot keys.
- **No timeouts or circuit breaker** on cache calls, so a slow cache makes every request slow, which
  is worse than a dead cache.
- **Serialization coupling**: changing a class breaks deserialization of old entries. Version the key
  prefix or the schema.
- **Inconsistent key naming** making bulk invalidation and per-prefix metrics impossible. Use a
  scheme like `service:entity:version:id`.
- **Using `KEYS` or `FLUSHALL` in production.**
- **Assuming cache hits mean correctness**: the cache masks slow queries until the day it is cold.
  Load-test with a cold cache.

---

## Interview questions

**1. Walk through cache-aside. Why delete the key on write instead of updating it?**
Read: check cache, on miss read DB and set with TTL. Write: commit to DB, then delete the key.
Updating on write races between concurrent writers (DB order and cache order can differ), and it
computes values nobody may read. Delete is idempotent; the next reader loads current data.

**2. Even with delete-after-write, how can cache-aside serve stale data?**
A reader misses, reads the old row, stalls; a writer commits and deletes; the reader then sets the
old value. Also, reading from a lagging replica after the delete refills old data. Mitigations: a
TTL as a bound, delayed double delete, versioned compare-and-set, or leases (Facebook memcache),
plus CDC-driven invalidation.

**3. Your hit ratio drops from 99% to 95%. What happens to the database?**
The miss ratio goes from 1% to 5%, so DB read load rises 5x. Effective latency also rises
(`T_c + 0.05 * T_m` instead of `T_c + 0.01 * T_m`). Investigate evictions (too small), TTL changes,
new key patterns (fragmented keys, a deploy changing key prefixes), or a cold node after failover.

**4. How do you prevent a cache stampede on a hot key?**
In-process single flight (or Caffeine's atomic load), a short distributed lock so only one instance
rebuilds, serve stale while one caller refreshes (soft TTL), probabilistic early recomputation
(XFetch), TTL jitter, or background refresh for known hot keys. CDNs and Nginx have request
collapsing / `proxy_cache_lock`.

**5. Explain the XFetch formula.**
Recompute if `now - delta * beta * ln(rand()) >= expiry`. `-ln(rand())` is exponential with mean 1,
so each reader looks a random distance ahead proportional to the recompute cost `delta`. Far from
expiry it almost never fires; near expiry one reader will likely refresh early, without locks.

**6. What is cache penetration and how does a Bloom filter help? Size one.**
Requests for non-existent keys always miss and hit the DB. A Bloom filter of all existing keys
answers "definitely absent" without touching storage. For n = 10M at 1% false positives: about 9.6
bits per key, about 12 MB, k = 7 hashes. It cannot delete and must be updated on insert.

**7. Compare LRU, LFU and W-TinyLFU.**
LRU captures recency, is O(1) with a hash map plus linked list, but scans flush it. LFU captures
frequency but needs aging and penalizes new items. W-TinyLFU has a small LRU window for new items, an
SLRU main region, and admits window victims only if a Count-Min Sketch says they are more frequent
than the main victim; it is scan-resistant, adapts, and has small metadata. Caffeine uses it.

**8. How does Redis implement LRU without a linked list?**
Each object stores a 24-bit access clock. On eviction Redis samples `maxmemory-samples` keys
(default 5), keeps the best candidates in a 16-entry pool and evicts the idlest. LFU mode reuses the
field for an 8-bit logarithmic counter plus decay time.

**9. Redis Cluster: how are keys distributed and how do multi-key operations work?**
`CRC16(key) mod 16384` selects a slot; slots are assigned to primaries. Clients follow `MOVED` and
`ASK` redirects. Multi-key commands require all keys in one slot, achieved with hash tags like
`{user:42}:cart`. Replication is async, so failover can lose recent writes.

**10. How would you keep a local in-process cache consistent across 100 instances?**
Short TTLs (seconds) as the bound, plus invalidation broadcast (Redis pub/sub, a Kafka topic, or
Redis client-side caching with `CLIENT TRACKING`). Flush on reconnect since pub/sub is lossy. Only
store data that tolerates that staleness.

**11. A celebrity post melts one Redis shard. What do you do?**
Detect with `--hotkeys`/per-key metrics. Add an L1 local cache with a 1–5 s TTL, replicate the key
under N suffixes that land on different shards and read a random one, read from replicas, and split
big values. For write-hot counters, shard the counter or aggregate locally and flush.

**12. Write-through vs write-behind: when would you use each?**
Write-through when reads must be fast and fresh and writes can afford two synchronous writes.
Write-behind when write latency and throughput dominate and losing a few seconds of writes is
acceptable or the cache is durable (counters, analytics, likes). Write-behind coalesces writes but
risks data loss and delays visibility in the DB.

**13. Which HTTP headers would you set for a JS bundle, the HTML page, and a per-user API?**
Fingerprinted bundle: `public, max-age=31536000, immutable`. HTML: `no-cache` with an ETag so it
revalidates each time. Per-user API: `private` (never in a CDN), short or zero `max-age`, with an ETag
for 304s. Sensitive data: `no-store`.

**14. How do you size a Redis cache?**
Working set (hot keys times key+value size plus per-key overhead, measured with `MEMORY USAGE`), plus
fragmentation, replication and fork headroom, times replicas. Validate with a miss-ratio curve from a
production trace, then monitor evictions and hit ratio per prefix.

---

## Summary

- Caching works because access is skewed (Zipf): a small cache absorbs most traffic.
  `T_eff = T_c + (1 - h) * T_m`; reason about the **miss ratio**, which is the DB load.
- Layers: browser (Cache-Control, ETag, Vary), CDN (cache keys, shields, purge by tag), reverse
  proxy (Nginx `proxy_cache_lock`, stale-while-updating), in-process (Caffeine), distributed (Redis,
  Memcached), DB buffer pool (midpoint LRU, clock sweep).
- Strategies: cache-aside (default), read-through, write-through, write-behind (fast, risky),
  write-around, refresh-ahead.
- Cache-aside rules: commit to the DB, then **delete** the key; always set a TTL; know the stale-set
  race and its fixes (double delete, versioned CAS, leases, CDC).
- Eviction: LRU (hash map + list), LFU (O(1) frequency buckets), ARC (adaptive with ghosts),
  W-TinyLFU (window + SLRU + Count-Min admission, Caffeine), Redis sampled LRU/LFU with
  `maxmemory-policy`.
- Failure modes: stampede (single flight, locks, soft TTL, XFetch, jitter), penetration (negative
  caching, Bloom filter: ~9.6 bits/key at 1%), avalanche (jitter, HA, warm-up, circuit breakers),
  hot keys (L1, key replicas, replica reads).
- Invalidation: TTL as safety net, app deletes, CDC via Debezium for completeness, pub/sub or
  `CLIENT TRACKING` for local caches.
- Redis at scale: 16,384 slots, hash tags, async replication, Sentinel or Cluster failover can lose
  acknowledged writes, RDB/AOF trade-offs, never the only copy of critical data.
- Monitor hit ratio per prefix, evictions, memory, latency, slow log, and DB load from misses.

---

## Further reading

- Rajesh Nishtala et al., *Scaling Memcache at Facebook*, USENIX NSDI 2013.
- Gil Einziger, Roy Friedman, Ben Manes, *TinyLFU: A Highly Efficient Cache Admission Policy*, ACM
  Transactions on Storage, 2017.
- Caffeine wiki and design documentation (Ben Manes), especially the "Efficiency" and "Design" pages:
  [github.com/ben-manes/caffeine/wiki](https://github.com/ben-manes/caffeine/wiki).
- Nimrod Megiddo, Dharmendra Modha, *ARC: A Self-Tuning, Low Overhead Replacement Cache*, USENIX
  FAST 2003.
- Andrea Vattani, Flavio Chierichetti, Keegan Lowenstein, *Optimal Probabilistic Cache Stampede
  Prevention*, VLDB 2015.
- Redis documentation: key eviction, client-side caching, Redis Cluster specification, persistence:
  [redis.io/docs](https://redis.io/docs/).
- RFC 9111, *HTTP Caching*, and RFC 5861, *HTTP Cache-Control Extensions for Stale Content*.
- Martin Kleppmann, *How to do distributed locking*, 2016 (blog essay).
- Martin Kleppmann, *Designing Data-Intensive Applications*, O'Reilly, 2017.
