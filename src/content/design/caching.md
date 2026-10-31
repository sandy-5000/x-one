---
title: "Caching"
order: 4
---
# Caching

A cache stores the result of an expensive operation (a DB query, an API call, a rendered page) in fast storage so the next request can skip the work.

```text
            hit
client ──► cache ─────────────► response
             │ miss
             ▼
          database ──► fill cache ──► response
```

## Where caches live

| Layer | Example | Caches |
| --- | --- | --- |
| Browser | HTTP cache, `localStorage` | Static assets, API responses |
| CDN | Cloudflare, CloudFront | Images, JS, CSS, whole pages |
| Reverse proxy | Nginx, Varnish | Rendered HTTP responses |
| Application (local) | Caffeine, Guava, a `HashMap` | Hot objects inside one process |
| Distributed | Redis, Memcached | Shared data across all app servers |
| Database | Buffer pool, query cache | Pages and query results |

---

## Caching strategies

### Cache-aside (lazy loading)

The application manages the cache itself. The most common pattern.

```java
User getUser(long id) {
    String key = "user:" + id;
    User cached = redis.get(key);
    if (cached != null) return cached;          // hit

    User user = db.findUser(id);                // miss
    redis.setex(key, 3600, user);               // populate with TTL
    return user;
}

void updateUser(User u) {
    db.save(u);
    redis.del("user:" + u.id());                // invalidate, don't update
}
```

- Only requested data is cached.
- A cache failure is not fatal — reads fall back to the DB.
- First request after a miss is slow; data can be stale until TTL or invalidation.

### Read-through

The cache library itself loads from the DB on a miss. Same behaviour as cache-aside but the loading logic lives in the cache layer.

### Write-through

Every write goes to the cache **and** the DB synchronously.

- Cache is always fresh.
- Writes are slower; caches data that may never be read.

### Write-behind (write-back)

Writes go to the cache; the cache flushes to the DB asynchronously in batches.

- Very fast writes, absorbs bursts.
- Risk of data loss if the cache dies before flushing.

### Write-around

Writes go straight to the DB; the cache fills only on reads. Good when written data is rarely read soon after.

| Strategy | Read latency | Write latency | Freshness | Risk |
| --- | --- | --- | --- | --- |
| Cache-aside | Miss is slow | Fast | Stale until invalidated | Low |
| Write-through | Fast | Slower | Fresh | Low |
| Write-behind | Fast | Fastest | Fresh in cache | Data loss |
| Write-around | Miss is slow | Fast | Fresh in DB | Low |

---

## Eviction policies

When the cache is full, something must go.

- **LRU (Least Recently Used)** — evict what was touched longest ago. The default choice.
- **LFU (Least Frequently Used)** — evict what is used least often. Good for stable popularity.
- **FIFO** — evict the oldest insert. Simple but ignores usage.
- **TTL** — entries expire after a fixed time regardless of space.

### LRU cache in Java

```java
class LruCache<K, V> extends LinkedHashMap<K, V> {
    private final int capacity;

    LruCache(int capacity) {
        super(16, 0.75f, true);    // accessOrder = true
        this.capacity = capacity;
    }

    @Override
    protected boolean removeEldestEntry(Map.Entry<K, V> eldest) {
        return size() > capacity;
    }
}
```

Under the hood: a hash map for O(1) lookup plus a doubly linked list for O(1) move-to-front and eviction.

---

## Common problems

### Cache stampede (thundering herd)

A hot key expires; thousands of requests miss at once and all hit the DB.

Fixes:

- **Locking / request coalescing** — only one request rebuilds the value; others wait.
- **Early refresh** — refresh slightly before expiry.
- **Jittered TTLs** — `ttl = 3600 + random(0, 300)` so keys don't expire together.

### Cache penetration

Requests for keys that don't exist (e.g. `user:-1`) always miss and always hit the DB — often an attack.

Fixes: cache the "not found" result with a short TTL, or put a **Bloom filter** in front to reject keys that certainly don't exist.

### Cache avalanche

Many keys expire at the same moment, or the cache cluster goes down. Fixes: jittered TTLs, replicated cache cluster, circuit breakers on the DB.

### Stale data and invalidation

> There are only two hard things in Computer Science: cache invalidation and naming things. — Phil Karlton

- Prefer **delete on write** over update on write (avoids races between two writers).
- Keep TTLs as a safety net even when you invalidate explicitly.
- For strict freshness, use change-data-capture (CDC) from the DB to invalidate.

---

## Redis vs Memcached

| | Redis | Memcached |
| --- | --- | --- |
| Data types | Strings, lists, sets, sorted sets, hashes, streams | Strings only |
| Persistence | Optional (RDB snapshots, AOF log) | None |
| Replication / clustering | Built in | Client-side sharding |
| Threads | Mostly single-threaded command execution | Multi-threaded |
| Extra uses | Rate limiting, leaderboards, queues, pub/sub, locks | Pure cache |

Pick Redis unless you need only a simple, very large, multi-threaded key/value cache.
