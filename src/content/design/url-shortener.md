---
title: "Case study: URL shortener"
order: 11
---
# Case Study: URL Shortener

Design a service like bit.ly or TinyURL: turn a long URL into a short one such as
`https://x1.io/aZ3kP9q`, and redirect anyone who opens the short link. It is the most common
warm-up design question because the core is simple, but a senior answer goes much deeper:
how codes are generated without collisions or coordination bottlenecks, how to stop
enumeration, which redirect status to use, how caching behaves under a skewed workload,
how click analytics scale, how abuse is handled, and how the system survives a regional
outage.

This page follows the interview framework from *System design basics*: requirements,
estimates, API, data model, high-level design, deep dives, failure modes, evolution and
trade-offs.

---

## 1. Clarifying questions

Ask these before drawing anything. Each answer changes the design.

| Question | Why it matters | Assumption for this design |
| --- | --- | --- |
| What is the scale (new links per month, clicks)? | Drives storage, sharding, cache size | 100 M new links/month; 100:1 read:write |
| How long must links live? | Total storage, ID space, cleanup | Forever by default; optional expiry |
| Can users choose custom aliases? | Global uniqueness checks, squatting rules | Yes, for logged-in users |
| Same long URL twice: same code or new code? | Deduplication strategy, per-user analytics | New code per request (per-user dedup optional) |
| Are analytics needed? How fresh? | 301 vs 302, event pipeline | Yes: clicks, uniques, country, referrer; within about 1 minute |
| Anonymous creation allowed? | Abuse, rate limiting | Yes, heavily rate-limited; API keys for volume |
| Must codes be unguessable? | Enumeration of private links | Not sequential; not secret either (links are public) |
| Can links be edited or deleted? | Cache invalidation, CDN caching of redirects | Owners can delete or disable; destination is immutable |
| Global users? Latency target? | Multi-region, edge caching | Global; redirect p99 under 50 ms server side |
| Code length constraints? | ID space and encoding | As short as possible; 7 characters target |

---

## 2. Requirements

### Functional

1. Create a short link for a long URL, optionally with a custom alias and an expiry time.
2. Redirect `GET /{code}` to the long URL.
3. Owners can list, disable and delete their links.
4. Owners can view analytics: total clicks, unique visitors, clicks over time, and top
   countries and referrers.

### Non-functional

- **Availability:** redirects are the critical path. Broken short links break other
  people's emails, QR codes printed on posters, and tweets. Target 99.99% for redirects.
  Creation can be slightly lower (99.9%).
- **Latency:** redirect p99 under 50 ms at the server, and under 20 ms when served from an
  edge cache.
- **Durability:** a created link must never be lost. Losing a mapping breaks it forever.
- **Consistency:** a newly created link should work within seconds everywhere. Within the
  creating region it should work immediately (read-your-writes).
- **Uniqueness:** a code maps to exactly one URL, forever. Codes are never reused.
- **Security:** no open-redirect abuse vector for phishing or malware. Resist enumeration.
  Rate-limit creation.

### Out of scope (say so explicitly)

Link-in-bio pages, QR code rendering, A/B destination rotation, and billing. These are easy
to mention as extensions.

---

## 3. Estimates

### Traffic

```text
New links:   100M / month = 100 × 10^6 / (30 × 86,400) ≈ 40 writes/s average
             peak (bulk API clients, campaigns) ×5      ≈ 200 writes/s

Redirects:   100:1 read:write                        ≈ 4,000 reads/s average
             daily peak ×3                             ≈ 12,000 reads/s
             viral events (one link in a TV ad)        → design for 50,000 reads/s,
                                                         most of it on a few hot codes
```

### Storage

```text
Links over 5 years   = 100M × 12 × 5 = 6 × 10^9 links

Per record:  code 7 B + long URL (avg ~100 B, max 2 KB) + owner id 8 B
             + timestamps 16 B + flags/status + URL hash 32 B
             + row/index overhead               ≈ 300–500 B; use 500 B

Primary data      = 6 × 10^9 × 500 B = 3 TB
With 3 replicas   ≈ 9 TB
10-year horizon   ≈ 6 TB primary, 18 TB replicated
```

3 TB is *not* big data. A single well-provisioned Postgres primary could hold it, which
matters for the MVP discussion later.

### Click events

```text
Clicks/day       = 4,000 × 86,400 ≈ 3.5 × 10^8
Event size       ≈ 200 B (code, ts, ip-derived country, UA class, referrer, visitor hash)
Raw volume       ≈ 70 GB/day ≈ 25 TB/year raw
Columnar storage compresses this roughly 5–10x → a few TB/year in an OLAP store
```

Analytics data quickly outgrows the link data itself, which is why it lives in a separate
pipeline and store.

### Bandwidth

```text
Redirect response ≈ 400 B (status line, Location header, a few other headers)
Egress            = 12,000 req/s × 400 B ≈ 4.8 MB/s ≈ 40 Mbps at daily peak
```

Bandwidth is trivial. The constraints are request rate, tail latency and availability.

### Cache memory

```text
Cache entry  ≈ key 10 B + URL ~100 B + Redis per-key overhead ~50–80 B ≈ 200 B
100M entries × 200 B ≈ 20 GB
```

The next sections show this gives roughly an 80–90% hit rate.

### Code length

| Length | Base62 space | Enough for 6 B links? |
| --- | --- | --- |
| 6 | 62^6 ≈ 5.68 × 10^10 (56.8 billion) | Yes, but only about 9x headroom, and random codes collide often |
| 7 | 62^7 ≈ 3.52 × 10^12 (3.5 trillion) | Yes, with about 580x headroom |
| 8 | 62^8 ≈ 2.18 × 10^14 | Plenty; consider for long-lived or random codes |

Choose **7 characters**. Base62 uses `0-9 a-z A-Z`, all URL-safe without escaping. Some
services drop look-alike characters (`0/O`, `1/l/I`) for links that people type. That is
base57 or similar, and slightly longer codes.

### Servers

A redirect does very little work: parse, cache lookup, emit an event, respond. One modern
instance handles several thousand redirects per second.

```text
50,000 peak / ~5,000 per instance = 10 instances
+ AZ headroom (survive losing 1 of 3 AZs) → 15 instances, 5 per AZ
```

Creation traffic is tiny and can share the same fleet or use a separate deployment so a
spike in creates never affects redirects.

---

## 4. API design

### Create a short link

```text
POST /api/v1/links
Authorization: Bearer <token>          (or API key; anonymous allowed with low limits)
Idempotency-Key: 6f1c2a9e-...          (optional; makes client retries safe)
Content-Type: application/json
```

```json
{
  "longUrl": "https://example.com/articles/2026/10/some-very-long-path?ref=newsletter",
  "customAlias": "fall-sale",
  "expiresAt": "2026-12-31T23:59:59Z"
}
```

```json
{
  "code": "fall-sale",
  "shortUrl": "https://x1.io/fall-sale",
  "longUrl": "https://example.com/articles/2026/10/some-very-long-path?ref=newsletter",
  "createdAt": "2026-10-31T11:50:02Z",
  "expiresAt": "2026-12-31T23:59:59Z"
}
```

| Status | When |
| --- | --- |
| 201 Created | Link created (`Location: /api/v1/links/{code}`) |
| 200 OK | Same `Idempotency-Key` replayed; returns the original response |
| 400 Bad Request | Invalid URL, unsupported scheme, URL too long, alias has invalid characters |
| 409 Conflict | Custom alias already taken |
| 422 Unprocessable Content | URL blocked by malware or phishing checks |
| 429 Too Many Requests | Rate limit exceeded (`Retry-After` header) |

Validation rules: only `http` and `https` schemes, so `javascript:` and `data:` URLs are
rejected. Maximum length is about 2,048 characters. Reject destinations that point back at
the shortener itself (redirect loops) and, by policy, at other shorteners (chains hide the
real destination). Custom aliases must match `[A-Za-z0-9_-]{4,32}`. Reserve words like
`api`, `login`, `admin` and `help`, and offensive terms.

### Redirect

```text
GET /aZ3kP9q

HTTP/1.1 302 Found
Location: https://example.com/articles/2026/10/some-very-long-path?ref=newsletter
Cache-Control: private, max-age=0
Referrer-Policy: unsafe-url
```

- Unknown code: `404 Not Found` (a small HTML page for browsers).
- Expired or deleted: `410 Gone` tells clients and crawlers the link existed but is
  permanently dead.
- Disabled for abuse: an interstitial warning page (200) or 451/404, depending on policy.
- `HEAD` should behave like `GET` without a body. Link-preview bots use it.
- Set `Referrer-Policy` deliberately. Some customers want the original referrer passed
  through, while privacy-focused setups use `no-referrer`.

### Analytics

```text
GET /api/v1/links/{code}/stats?from=2026-10-01&to=2026-10-31&granularity=day
```

```json
{
  "code": "aZ3kP9q",
  "totalClicks": 182340,
  "uniqueVisitors": 120118,
  "series": [
    { "date": "2026-10-30", "clicks": 10231, "uniques": 7012 },
    { "date": "2026-10-31", "clicks": 15890, "uniques": 11005 }
  ],
  "topCountries": [ { "country": "IN", "clicks": 80211 }, { "country": "US", "clicks": 40110 } ],
  "topReferrers": [ { "referrer": "t.co", "clicks": 51200 } ]
}
```

Unique visitor counts are approximate (HyperLogLog, about 1% error). Document that in the
API.

### Manage

```text
GET    /api/v1/links?cursor=...&limit=50      list my links (cursor pagination)
GET    /api/v1/links/{code}                   metadata
PATCH  /api/v1/links/{code}                   { "expiresAt": ..., "disabled": true }
DELETE /api/v1/links/{code}                   204 No Content; idempotent
```

Making the destination **immutable** is a deliberate choice. Editable destinations allow
bait-and-switch phishing (create a link to a harmless page, get it shared, then change it).
They also make long-lived caching of redirects unsafe. If the business needs editable
links, offer them as a separate paid feature with short cache TTLs and re-scanning on every
edit.

Deletes and disables must invalidate the Redis entry and purge the CDN. This is one
reason a short CDN TTL on redirects matters.

---

## 5. High-level design

```text
                         ┌─────────────────────────────────────┐
  browsers, apps,  ────► │ Anycast edge / CDN                  │
  bots, API clients      │ TLS, WAF, DDoS, optional redirect   │
                         │ cache (short TTL), edge rate limits │
                         └──────────────────┬──────────────────┘
                                            │
                         ┌──────────────────▼──────────────────┐
                         │ Regional L7 load balancer / gateway │
                         │ auth for /api, rate limiting        │
                         └─────────┬─────────────────┬─────────┘
                    GET /{code}    │                 │   /api/v1/*
                         ┌─────────▼────────┐ ┌──────▼───────────┐
                         │ Redirect service │ │ Link API service │
                         │ stateless, hot   │ │ create, manage,  │
                         │ local LRU cache  │ │ stats queries    │
                         └──┬──────────┬────┘ └──┬────────┬──────┘
                            │          │         │        │
          ┌─────────────────▼────┐     │    ┌────▼─────┐ ┌▼──────────────┐
          │ Redis cluster        │     │    │ ID block │ │ URL safety    │
          │ code → URL, flags    │     │    │ allocator│ │ (blocklists,  │
          └─────────┬────────────┘     │    └──────────┘ │ Safe Browsing)│
                    │                  │                 └───────────────┘
                    │ miss             │    Link API also writes the
          ┌─────────▼───────────────┐  │    store and Redis (not drawn)
          │ Link store: sharded by  │  │
          │ code, RF=3, multi-AZ    │  │ click events
          └─────────────────────────┘  │ (async)
                                ┌──────▼─────┐   ┌────────────────┐   ┌─────────────────┐
                                │ Kafka      │──►│ Stream proc.   │──►│ OLAP store      │
                                │ "clicks"   │   │ (Flink)        │   │ + Redis HLL     │
                                └────────────┘   └────────────────┘   └─────────────────┘
```

Two services with very different profiles are deliberately split:

- **Redirect service:** extremely read-heavy and latency-critical. It has few
  dependencies (cache, store, Kafka producer) and must keep working when anything else is
  broken.
- **Link API service:** low volume, but it carries the complex logic: validation, safety
  checks, ID allocation, custom alias conflicts, auth and stats queries.

Splitting them gives independent scaling, deploys and failure isolation. A bad deploy of
the stats feature cannot break redirects.

---

## 6. Data model

### Access patterns

| Pattern | Frequency | Key |
| --- | --- | --- |
| Lookup URL by code | Very high (every redirect miss) | `code` |
| Insert new link if code is free | Low | `code` (conditional insert) |
| List links of an owner, newest first | Low | `owner_id, created_at` |
| Find links expiring before time T | Background | `expires_at` |
| Find existing code for (owner, URL) (optional dedup) | Low | `owner_id, url_hash` |
| Click stats by code and time range | Medium | `code, time` (OLAP store) |

The dominant pattern is a **point lookup by primary key**. That is the ideal shape for a
key-value store and also trivial for an RDBMS.

### SQL schema (PostgreSQL)

```sql
CREATE TABLE links (
    code        VARCHAR(32)  PRIMARY KEY,
    long_url    TEXT         NOT NULL CHECK (length(long_url) <= 2048),
    url_hash    BYTEA        NOT NULL,
    owner_id    BIGINT,
    is_custom   BOOLEAN      NOT NULL DEFAULT false,
    status      TEXT         NOT NULL DEFAULT 'active'
                             CHECK (status IN ('active', 'disabled', 'deleted')),
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ
);

CREATE INDEX links_owner_created ON links (owner_id, created_at DESC)
    WHERE owner_id IS NOT NULL;

CREATE INDEX links_expiry ON links (expires_at)
    WHERE expires_at IS NOT NULL AND status = 'active';

CREATE UNIQUE INDEX links_owner_dedup ON links (owner_id, url_hash)
    WHERE owner_id IS NOT NULL AND is_custom = false;

CREATE SEQUENCE link_id_blocks START 1 INCREMENT BY 10000;
```

```sql
-- Hot path: the redirect lookup.
SELECT long_url, status, expires_at FROM links WHERE code = $1;

-- Create: conditional insert; zero rows means the code (or alias) is taken.
INSERT INTO links (code, long_url, url_hash, owner_id, is_custom, expires_at)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (code) DO NOTHING
RETURNING code;
```

The partial unique index implements optional per-owner deduplication: the same owner
shortening the same URL gets the existing code back instead of a new one. Drop it if every
request should create a fresh link (for example, for per-campaign analytics).

When sharding SQL (for example with Vitess or Citus), shard `links` by `code`. The
owner listing then becomes a scatter-gather query. The usual fix is a second table,
`links_by_owner (owner_id, created_at, code)`, sharded by `owner_id` and written in the same
logical operation, or maintained asynchronously from a change stream.

### NoSQL alternative: DynamoDB

```json
{
  "TableName": "links",
  "KeySchema": [ { "AttributeName": "code", "KeyType": "HASH" } ],
  "AttributeDefinitions": [
    { "AttributeName": "code", "AttributeType": "S" },
    { "AttributeName": "owner_id", "AttributeType": "S" },
    { "AttributeName": "created_at", "AttributeType": "S" }
  ],
  "GlobalSecondaryIndexes": [
    {
      "IndexName": "by_owner",
      "KeySchema": [
        { "AttributeName": "owner_id", "KeyType": "HASH" },
        { "AttributeName": "created_at", "KeyType": "RANGE" }
      ],
      "Projection": { "ProjectionType": "KEYS_ONLY" }
    }
  ],
  "BillingMode": "PAY_PER_REQUEST"
}
```

- Create with `PutItem` and `ConditionExpression: attribute_not_exists(code)`. This is an
  atomic "insert if absent" for both generated codes and custom aliases.
- Use an `expires_at_epoch` attribute with **DynamoDB TTL** enabled for automatic deletion.
  TTL deletes happen in the background, typically within a few days of expiry, so the read
  path must still check expiry itself.
- Anonymous links have no `owner_id`, so they simply do not appear in the sparse GSI.
- Watch the partition throughput limits for a single viral key. Caching absorbs that.

### NoSQL alternative: Cassandra

```sql
CREATE TABLE links (
    code        text PRIMARY KEY,
    long_url    text,
    owner_id    bigint,
    status      text,
    created_at  timestamp,
    expires_at  timestamp
);

CREATE TABLE links_by_owner (
    owner_id    bigint,
    created_at  timestamp,
    code        text,
    PRIMARY KEY ((owner_id), created_at, code)
) WITH CLUSTERING ORDER BY (created_at DESC, code ASC);

INSERT INTO links (code, long_url, owner_id, status, created_at, expires_at)
VALUES ('fall-sale', 'https://example.com/...', 42, 'active', toTimestamp(now()), null)
IF NOT EXISTS;
```

`IF NOT EXISTS` is a lightweight transaction (Paxos-based). It is correct for uniqueness
but costs several round trips, which is fine at 200 writes/s. Generated codes that are
unique by construction (counter or Snowflake) can skip the LWT and use a plain insert.
Custom aliases must use it.

### Which store?

| Option | Pros | Cons | Verdict |
| --- | --- | --- | --- |
| Single Postgres primary + replicas | Simple, strong consistency, `ON CONFLICT`, rich queries | Vertical write ceiling, manual sharding later, failover takes seconds | Great MVP; fine up to a few TB |
| Sharded MySQL/Postgres (Vitess, Citus) | SQL, proven at huge scale | Operational complexity, cross-shard queries | Good if the org already runs it |
| DynamoDB | Managed, conditional writes, TTL, global tables, scales automatically | Vendor lock-in, cost at high read rates (cache helps), limited queries | Strong default on AWS |
| Cassandra / ScyllaDB | Linear scale, multi-DC replication, TTL | Operating it, LWT cost, eventual consistency by default | Good for self-hosted, multi-region |

The data is a simple key to value mapping with modest size and write rate. **Any of these
works.** In the interview, pick one and justify it with the access patterns. A strong
answer is "Postgres for the MVP, migrate to DynamoDB or Cassandra when we go multi-region
or past about 10 TB". Saying that 3 TB does not need sharding on day one shows judgment.

---

## 7. Short code generation deep dive

This is the core of the problem. A code generation scheme must provide:

1. **Uniqueness:** one code, one URL, forever.
2. **Shortness:** 7 characters for at least 6 billion links.
3. **Scalability:** no single coordinator on the hot path, and no hot spots.
4. **Non-guessability:** consecutive links should not get consecutive codes.
5. **Availability:** creation keeps working when a node or region fails.

### 7.1 Base62 encoding

All approaches except random keys produce a number and then encode it in base62.

```java
public final class Base62 {
    private static final char[] ALPHABET =
            "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ".toCharArray();
    private static final int[] INDEX = new int[128];

    static {
        java.util.Arrays.fill(INDEX, -1);
        for (int i = 0; i < ALPHABET.length; i++) {
            INDEX[ALPHABET[i]] = i;
        }
    }

    private Base62() {}

    public static String encode(long value) {
        if (value < 0) {
            throw new IllegalArgumentException("negative id: " + value);
        }
        var sb = new StringBuilder(11);
        do {
            sb.append(ALPHABET[(int) (value % 62)]);
            value /= 62;
        } while (value > 0);
        return sb.reverse().toString();
    }

    public static String encodePadded(long value, int length) {
        String s = encode(value);
        if (s.length() > length) {
            throw new IllegalArgumentException("value too large for " + length + " chars");
        }
        return "0".repeat(length - s.length()) + s;
    }

    public static long decode(String code) {
        long value = 0;
        for (int i = 0; i < code.length(); i++) {
            char c = code.charAt(i);
            int digit = c < 128 ? INDEX[c] : -1;
            if (digit < 0) {
                throw new IllegalArgumentException("invalid character: " + c);
            }
            value = Math.addExact(Math.multiplyExact(value, 62L), digit);
        }
        return value;
    }
}
```

```java
Base62.encode(125);                          // "21"   (2 × 62 + 1)
Base62.encode(3_521_614_606_207L);           // "ZZZZZZZ" (62^7 − 1, largest 7-char code)
Base62.encodePadded(125, 7);                 // "0000021"
Base62.decode("aZ3kP9q");                    // back to the number
```

### 7.2 Option A: hash the URL, then handle collisions

Compute `hash(longUrl)`, take enough bits, and encode them as 7 base62 characters.

- **Pro:** stateless and needs no coordination. The same input gives the same code, so
  deduplication is free if desired.
- **Con:** truncating to about 41.7 bits (62^7) means **collisions are certain at scale**.
  By the birthday bound, the expected number of colliding pairs among n random codes in a
  space of size M is about n² / 2M. With n = 6 × 10^9 and M = 3.5 × 10^12 that is about
  5 million collisions. Each new insert collides with probability n / M, which reaches about
  0.17% when the table is full. So you **must** use a conditional insert and retry with a
  salt.
- **Con:** identical URLs from different users collide by design. Include the owner (or a
  random salt) in the hash input if links should be distinct.
- **Con:** each collision costs an extra round trip, and the uniqueness check must be atomic
  (`INSERT ... ON CONFLICT DO NOTHING` / `attribute_not_exists`). A "check, then insert" in
  two steps is a race condition.

```java
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;

public final class HashCodeGenerator {
    private static final long SPACE_7 = 3_521_614_606_208L;
    private static final int MAX_ATTEMPTS = 5;

    public interface LinkRepository {
        boolean insertIfAbsent(String code, String longUrl, Long ownerId);
    }

    private final LinkRepository repo;

    public HashCodeGenerator(LinkRepository repo) {
        this.repo = repo;
    }

    public String create(String normalizedUrl, Long ownerId) {
        for (int attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
            String input = normalizedUrl + '\u0000' + ownerId + '\u0000' + attempt;
            String code = Base62.encodePadded(hashToRange(input), 7);
            if (repo.insertIfAbsent(code, normalizedUrl, ownerId)) {
                return code;
            }
        }
        throw new IllegalStateException("could not find a free code after " + MAX_ATTEMPTS);
    }

    private static long hashToRange(String input) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(input.getBytes(StandardCharsets.UTF_8));
            long h = ByteBuffer.wrap(digest, 0, 8).getLong();
            return Long.remainderUnsigned(h, SPACE_7);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}
```

A conflict on the very first attempt for the *same* owner and URL means the link already
exists. A production version would read the existing row and return it, rather than
creating a near-duplicate with the next salt.

### 7.3 Option B: a counter encoded in base62

Give every link a unique 64-bit integer, then base62-encode it. Uniqueness comes from the
counter, so there are **no collisions by construction** and no retry loop. ID 1 billion
encodes to 6 characters, and 3.5 trillion IDs fit in 7.

The problem is the counter itself. A single `AUTO_INCREMENT` or sequence on one database is
a single point of failure and a throughput ceiling. At 200 writes/s that ceiling is not
actually reached, but availability and multi-region writes still matter. Raw sequential
codes are also trivially enumerable (see section 8).

### 7.4 Option C: range (block) allocation

Each app instance claims a **block** of IDs (say 10,000) from a durable coordinator and
hands them out from memory. The coordinator is touched once per 10,000 links, so it is
off the hot path. If an instance crashes, the rest of its block is lost, which leaves a
harmless gap in the ID space.

```text
 ┌──────────────────────────────┐
 │ Coordinator (DB sequence or  │   next block start: 4,210,000
 │ ZooKeeper / etcd counter)    │
 └──────┬──────────────┬────────┘
        │ claim        │ claim
        ▼              ▼
 ┌──────────────┐ ┌──────────────┐
 │ app-1        │ │ app-2        │
 │ 4,190,000 to │ │ 4,200,000 to │
 │ 4,199,999    │ │ 4,209,999    │
 └──────────────┘ └──────────────┘
```

With Postgres, `CREATE SEQUENCE link_id_blocks INCREMENT BY 10000` and
`SELECT nextval('link_id_blocks')` returns the start of a fresh block atomically. With
ZooKeeper or etcd, keep a counter node and advance it with a compare-and-set on the node
version (Apache Curator's `DistributedAtomicLong` recipe does this). A single DB row
updated with `UPDATE ... RETURNING` also works.

```java
public final class BlockIdAllocator {

    @FunctionalInterface
    public interface BlockSource {
        long claimBlockStart();
    }

    private final BlockSource source;
    private final long blockSize;
    private long next;
    private long end;

    public BlockIdAllocator(BlockSource source, long blockSize) {
        this.source = source;
        this.blockSize = blockSize;
    }

    public synchronized long nextId() {
        if (next >= end) {
            long start = source.claimBlockStart();
            next = start;
            end = start + blockSize;
        }
        return next++;
    }
}
```

Refinements for production:

- **Prefetch** the next block in the background when the current one is 80% used, so no
  request ever waits on the coordinator.
- If the coordinator is down, instances keep serving from their current block plus one
  prefetched block. That covers 20,000 creates per instance, which is minutes to hours of
  normal traffic.
- **Multi-region:** give each region its own disjoint range space. For example, the top
  bits encode the region, or each region uses its own sequence with a different offset. No
  cross-region coordination is needed.
- Block size trades coordinator load against wasted IDs on restarts. 10,000 is a good
  default, because even a thousand restarts per day waste a negligible fraction of 3.5
  trillion.

### 7.5 Option D: Snowflake-style IDs

Twitter's Snowflake layout generates 64-bit, roughly time-ordered IDs **locally**, with no
coordination at all after a worker ID is assigned.

```text
 ┌───┬──────────────────────────────────┬────────────┬────────────┐
 │ 0 │ timestamp: ms since custom epoch │ worker ID  │ sequence   │
 │   │ 41 bits (about 69.7 years)       │ 10 bits    │ 12 bits    │
 └───┴──────────────────────────────────┴────────────┴────────────┘
  63  62                             22  21      12  11        0
```

- 1 sign bit is always 0, so IDs are positive Java `long` values.
- 41 bits of milliseconds give 2^41 ms ≈ 69.7 years from a custom epoch.
- 10 bits of worker ID allow 1,024 generators. Assign them via ZooKeeper or etcd leases, a
  Kubernetes StatefulSet ordinal, or configuration. Two workers with the same ID produce
  duplicates, so the assignment must be reliable.
- 12 bits of sequence allow 4,096 IDs per millisecond per worker, about 4 million per
  second per worker.

```java
public final class SnowflakeIdGenerator {
    private static final long EPOCH_MS = 1_735_689_600_000L;
    private static final int WORKER_BITS = 10;
    private static final int SEQUENCE_BITS = 12;
    private static final long MAX_WORKER_ID = (1L << WORKER_BITS) - 1;
    private static final long SEQUENCE_MASK = (1L << SEQUENCE_BITS) - 1;
    private static final long MAX_BACKWARD_MS = 10;

    private final long workerId;
    private long lastMs = -1L;
    private long sequence = 0L;

    public SnowflakeIdGenerator(long workerId) {
        if (workerId < 0 || workerId > MAX_WORKER_ID) {
            throw new IllegalArgumentException("workerId out of range: " + workerId);
        }
        this.workerId = workerId;
    }

    public synchronized long nextId() {
        long now = System.currentTimeMillis();
        if (now < lastMs) {
            if (lastMs - now > MAX_BACKWARD_MS) {
                throw new IllegalStateException("clock moved backwards by " + (lastMs - now) + " ms");
            }
            now = waitUntil(lastMs);
        }
        if (now == lastMs) {
            sequence = (sequence + 1) & SEQUENCE_MASK;
            if (sequence == 0) {
                now = waitUntil(lastMs + 1);
            }
        } else {
            sequence = 0;
        }
        lastMs = now;
        return ((now - EPOCH_MS) << (WORKER_BITS + SEQUENCE_BITS))
                | (workerId << SEQUENCE_BITS)
                | sequence;
    }

    private static long waitUntil(long targetMs) {
        long now = System.currentTimeMillis();
        while (now < targetMs) {
            Thread.onSpinWait();
            now = System.currentTimeMillis();
        }
        return now;
    }
}
```

The epoch above is 2025-01-01T00:00:00Z. Choose a recent epoch to keep the numbers, and
therefore the codes, short.

**The catch for a URL shortener: length.** After one year, the timestamp part is about
3.15 × 10^10 ms. Shifted left by 22 bits, that is about 1.3 × 10^17, which needs **10
base62 characters**, and 11 after about six years. Snowflake is excellent for database
primary keys and for sorting by time, but it wastes the code space here. Two ways out:

- Use a smaller custom layout, for example 32 bits of seconds, 6 bits of worker and 4 bits
  of sequence. Run the arithmetic carefully: low per-worker throughput, clock dependence,
  and you still get about 8 characters.
- Use block allocation (dense IDs give 6–7 characters) and keep Snowflake for internal
  event ids.

Clock issues are Snowflake's main operational risk. NTP can step the clock backwards.
Leap smearing helps, the generator above refuses large backward jumps, and many
implementations persist the last timestamp so a restart cannot reuse it.

### 7.6 Option E: key generation service (KGS) with pre-generated keys

Pre-generate random 7-character codes offline, store them as "unused", and have app
servers claim them in batches.

```text
 ┌────────────────────┐   batch of 1,000   ┌─────────────────────────┐
 │ Key generator job  │ ─────────────────► │ unused_keys table       │
 │ random 7-char,     │   (dedup by PK)    │ (billions of rows OK:   │
 │ runs ahead of use  │                    │ 7 B each, ~10s of GB)   │
 └────────────────────┘                    └────────────┬────────────┘
                                                        │ claim batch
                                                        │ (atomic delete)
                                           ┌────────────▼────────────┐
                                           │ Link API instance       │
                                           │ in-memory queue of keys │
                                           └─────────────────────────┘
```

```sql
CREATE TABLE unused_keys (code CHAR(7) PRIMARY KEY);

-- Claim a batch atomically; concurrent claimers skip each other's locked rows.
WITH batch AS (
    SELECT code FROM unused_keys
    LIMIT 1000
    FOR UPDATE SKIP LOCKED
)
DELETE FROM unused_keys k
USING batch
WHERE k.code = batch.code
RETURNING k.code;
```

- **Pros:** codes are random, so there is no enumeration and no visible ordering. There is
  no collision handling at request time, and creation is a fast pop from a local queue.
- **Cons:** another stateful service to run. Keys held in memory by a crashed instance are
  lost, which is fine because the space is huge. The generator must check uniqueness
  against both unused keys and keys already in `links`. A simple approach is to generate
  keys from a random permutation of the space or to check both tables.
- Custom aliases bypass the KGS. They are conditionally inserted into the same `links`
  keyspace, and the generator must also skip any key that a custom alias already holds.

An even simpler variant without the KGS: **generate a random 7-character code at request
time and conditionally insert it.** At 0.17% collision probability, retries are rare, and
the result is the same unguessable random code with no extra service. Increasing the length
to 8 makes collisions negligible.

### 7.7 Comparison

| Approach | Collisions | Coordination | Length (6 B links) | Guessable | Complexity |
| --- | --- | --- | --- | --- | --- |
| Hash + retry | Yes, must handle | None | 7 | No | Low |
| Single DB sequence | None | Every write | 6–7 | Yes (sequential) | Lowest |
| Block allocation | None | Once per block | 6–7 | Yes, unless permuted | Low–medium |
| Snowflake | None | Worker ID only | 10–11 | Partly (time-ordered) | Medium (clocks) |
| KGS pre-generated | None at request time | Batch claims | 7 | No | Medium–high |
| Random + conditional insert | Rare, retry | None | 7–8 | No | Low |

**Recommended answer:** block allocation for dense, collision-free IDs, passed through a
keyed bijective permutation so codes look random (section 8), with region-partitioned
ranges for multi-region. Mention random + conditional insert as an equally valid simpler
alternative when the database supports cheap atomic conditional inserts.

---

## 8. Avoiding enumeration

With sequential codes, anyone can walk `aZ3kP9q`, `aZ3kP9r`, ... and scrape every link.
That leaks private documents shared by link, reveals business volume (competitors can
measure your growth), and helps spammers. Defences in layers:

1. **Make codes non-sequential.** Pass the counter through a **keyed bijection** (a
   permutation of the ID space) before encoding. A small Feistel network over a fixed bit
   width is a permutation for *any* round function, so it can never produce duplicates. It
   is also reversible, which is handy for debugging.
2. **Or use random codes** (KGS or random + conditional insert).
3. **Rate-limit 404s** per IP and per subnet, and add negative caching (section 10) so
   scans are cheap to absorb. Alert on high 404 ratios.
4. **Longer codes for sensitive links.** For "anyone with the link" sharing of private
   content, use 10–22 character random tokens (about 60–128 bits) or require auth. Security
   should not depend on a 41-bit space.

A Feistel permutation over 40 bits (two 20-bit halves). 2^40 ≈ 1.1 × 10^12 is below 62^7,
so every output still fits in 7 base62 characters.

```java
public final class FeistelPermutation {
    private static final int HALF_BITS = 20;
    private static final long HALF_MASK = (1L << HALF_BITS) - 1;
    private static final long DOMAIN = 1L << (2 * HALF_BITS);

    private final long[] roundKeys;

    public FeistelPermutation(long[] roundKeys) {
        if (roundKeys.length < 4) {
            throw new IllegalArgumentException("use at least 4 rounds");
        }
        this.roundKeys = roundKeys.clone();
    }

    public long permute(long id) {
        checkDomain(id);
        long left = id >>> HALF_BITS;
        long right = id & HALF_MASK;
        for (long key : roundKeys) {
            long next = left ^ round(right, key);
            left = right;
            right = next;
        }
        return (left << HALF_BITS) | right;
    }

    public long invert(long value) {
        checkDomain(value);
        long left = value >>> HALF_BITS;
        long right = value & HALF_MASK;
        for (int i = roundKeys.length - 1; i >= 0; i--) {
            long previous = right ^ round(left, roundKeys[i]);
            right = left;
            left = previous;
        }
        return (left << HALF_BITS) | right;
    }

    private static long round(long half, long key) {
        long x = (half ^ key) * 0x9E3779B97F4A7C15L;
        x ^= x >>> 29;
        x *= 0xBF58476D1CE4E5B9L;
        x ^= x >>> 32;
        return x & HALF_MASK;
    }

    private static void checkDomain(long v) {
        if (v < 0 || v >= DOMAIN) {
            throw new IllegalArgumentException("out of 40-bit domain: " + v);
        }
    }
}
```

```java
var perm = new FeistelPermutation(secretRoundKeys);
long id = allocator.nextId();
String code = Base62.encodePadded(perm.permute(id), 7);
```

Two caveats are worth saying out loud. First, this is **obfuscation, not cryptography**. A
determined attacker with many samples could analyse a simple round function. It reliably
defeats casual "increment the code" scraping. For stronger guarantees, use a real
format-preserving encryption scheme (NIST FF1/FF3-1) or random codes. Second, 2^40 caps
this scheme at about 1.1 trillion links, far above the 6 billion needed. Keep the round
keys in a secrets manager. Rotating them is impossible without changing all codes, so in
practice they are fixed forever.

---

## 9. Redirect status codes: 301 vs 302 vs 307 vs 308

| Code | Meaning | Method on redirect | Cached by browser by default? | Use for a shortener |
| --- | --- | --- | --- | --- |
| 301 Moved Permanently | Permanent | Clients may change POST to GET | Yes (heuristically cacheable) | Fewer hits on the server; browsers may never ask again, so clicks are lost from analytics and later disables have no effect |
| 302 Found | Temporary | Clients may change POST to GET | Only with explicit `Cache-Control`/`Expires` | Default choice: every click reaches you, analytics are accurate, disables take effect immediately |
| 307 Temporary Redirect | Temporary | Method and body preserved | Only with explicit freshness | Same as 302 for GET; semantically precise |
| 308 Permanent Redirect | Permanent | Method and body preserved | Yes | Same caching downside as 301 |

Shortened links are clicked with GET, so the method distinction between 302 and 307 (or
301 and 308) does not matter in practice. The real decision is **permanent vs temporary**:

- **Permanent (301/308):** a browser that has seen the link once may redirect locally
  forever, without contacting you. That means less load, but you lose click analytics for
  repeat visitors, cannot disable a link that later turns malicious for those users, and
  cannot fix mistakes.
- **Temporary (302/307):** every click comes to you (or to your CDN). That means more load,
  which is cheap anyway, in exchange for accurate analytics and control.

A middle ground is a 301 or 302 with explicit `Cache-Control: private, max-age=N`, which
allows short browser caching. Alternatively, cache at **your CDN** (with `s-maxage`) and
derive analytics from CDN logs. The CDN absorbs viral load, you keep control through purge,
and the logs still record every click.

SEO note: search engines treat 301/308 as passing ranking signals to the target. Most
shortener links are not meant to be indexed, so this rarely matters.

---

## 10. Caching strategy

### What is cached where

```text
 request ──► CDN edge (optional, TTL 60 s, purge on disable)
               │ miss
               ▼
           in-process LRU in redirect service (Caffeine, ~100k entries, TTL 30–60 s)
               │ miss
               ▼
           Redis cluster  key "u:{code}" → {url, status, expiresAt}, TTL 24 h ± jitter
               │ miss
               ▼
           link store (primary key lookup)
```

- **Cache-aside** at each tier: read the cache, on a miss read the store, then populate.
  Links are immutable, so there is no invalidation problem *except* for disable, delete
  and expiry. Those paths explicitly delete the Redis key, purge the CDN URL, and rely on
  the short TTL of the in-process tier.
- Store `expiresAt` in the cached value and check it on every hit. Set the Redis TTL to
  `min(24 h, time until expiry)`.
- **Negative caching:** cache "not found" for 30–60 seconds. Without it, a scanner probing
  random codes sends 100% misses to the database. Use a short TTL, because a just-created
  link must not be hidden by a stale negative entry. Also skip negative caching for codes
  in the current ID range that might be created at any moment, or simply delete the
  negative entry on create.
- **Hot keys:** a viral link can get tens of thousands of requests/s on a *single* Redis key,
  which lives on one shard. The in-process LRU tier absorbs this, since every redirect
  instance holds its own copy. So does the CDN. Optionally replicate hot keys under
  suffixed names (`u:{code}#1..#8`) to spread them across shards.
- **Stampede protection:** when a hot key expires, thousands of concurrent misses hit the
  store. Use request coalescing (one loader per key per instance, which Caffeine's
  `get(key, loader)` provides), TTL jitter, and early probabilistic refresh.

### Hit-rate math

Link popularity is extremely skewed. Most links are clicked a handful of times, right after
creation, and a few are clicked millions of times. Model popularity as **Zipf with s ≈ 1**:
the k-th most popular link gets traffic proportional to 1/k. The fraction of requests that
hit the top C of N items is then about

```text
hit rate ≈ H(C) / H(N) ≈ (ln C + 0.577) / (ln N + 0.577)

N = 6 × 10^9 links,  C = 10^8 cached (≈ 20 GB)
  = (18.42 + 0.58) / (22.52 + 0.58) = 19.0 / 23.1 ≈ 0.82

C = 5 × 10^8 cached (≈ 100 GB)
  = (20.03 + 0.58) / 23.1 ≈ 0.89
```

So about 20 GB of cache serves more than 80% of redirects, and 5x more memory only buys a
few more points. Real traffic is also strongly **recency**-skewed: new links get most of
their lifetime clicks in the first days. That makes an LRU cache perform better than this
static model suggests, so 90%+ is realistic.

What the hit rate buys:

```text
Peak 50,000 req/s, hit rate 90%:
  store reads  = 50,000 × 0.10 = 5,000/s       (vs 50,000/s with no cache)
  mean latency ≈ 0.9 × 1 ms (Redis) + 0.1 × 5 ms (store) ≈ 1.4 ms server-side

Hit rate drops to 50% (cold cache after a Redis failover):
  store reads  = 25,000/s  → the store must be provisioned for this, or it falls over
```

**Plan for the cold-cache case.** The store must survive the miss rate during a cache
rebuild, or you need cache warm-up (pre-loading the hottest keys from analytics) and
admission control while warming.

---

## 11. Storage and sharding

- **Shard key:** `code`. Every redirect touches exactly one shard, and creates spread evenly
  because codes are uniformly distributed (permuted or random). Never shard by
  `created_at`, because all writes would go to the newest shard.
- **Partitioning scheme:** hash partitioning with consistent hashing or many fixed virtual
  shards (for example 4,096 logical shards mapped to physical nodes). Rebalancing then moves
  whole logical shards. DynamoDB and Cassandra do this internally.
- **Replication:** RF = 3 across 3 AZs. Write with quorum or synchronous replication to at
  least one other AZ before acknowledging. A lost mapping is a permanently broken link.
- **Read consistency:** redirects can read from any replica. To guarantee read-your-writes
  right after creation, the creating region writes through to Redis on create, so the
  first click hits the cache. A store miss on a code that "should" exist (ID within
  recently issued ranges) can retry against the primary or a quorum read.
- **Secondary data:** the owner listing lives in its own table or index, keyed by owner
  (see the data model).
- **Size check:** 6 TB primary over 10 years. At about 1–2 TB per node for comfortable
  compaction and recovery, that is about 6–12 shards (×3 replicas), which is a small
  cluster.

---

## 12. Read path (redirect)

```text
Browser        Edge/LB          App           Redis           DB            Kafka
   │              │              │              │              │              │
   │ GET /aZ3kP9q │              │              │              │              │
   │─────────────►│              │              │              │              │
   │              │ forward      │              │              │              │
   │              │─────────────►│              │              │              │
   │              │              │ check code   │              │              │
   │              │              │ GET u:<code> │              │              │
   │              │              │─────────────►│              │              │
   │              │              │ hit: longUrl │              │              │
   │              │              │◄─────────────│              │              │
   │              │              │ (on a miss)  │              │              │
   │              │              │ SELECT code  │              │              │
   │              │              │────────────────────────────►│              │
   │              │              │ row or none  │              │              │
   │              │              │◄────────────────────────────│              │
   │              │              │ SET (TTL)    │              │              │
   │              │              │─────────────►│              │              │
   │              │              │ click event  │              │              │
   │              │              │───────────────────────────────────────────►│
   │              │              │              │              │              │ async
   │              │ 302 Location │              │              │              │
   │              │◄─────────────│              │              │              │
   │ 302 Location │              │              │              │              │
   │◄─────────────│              │              │              │              │
   │              │              │              │              │              │
```

Step by step:

1. Validate the code syntactically (charset, length) before touching any cache. Garbage
   gets an immediate 404.
2. Check the in-process LRU, then Redis. On a miss, read the store and populate Redis,
   including a negative entry if the code was not found.
3. Check `status` and `expiresAt`: 410 for expired or deleted, a warning page for disabled.
4. Emit a click event to Kafka **asynchronously**. The producer batches in memory
   (`linger.ms`, `batch.size`). If Kafka is unavailable and the buffer fills, **drop events
   and count the drops** rather than slowing redirects. Analytics are best effort and
   redirects are sacred. Alternatively, spool to local disk.
5. Respond 302 with `Location`. Total server time is about 1–2 ms on a cache hit.

The redirect service should have **strict timeouts** on Redis (about 20–50 ms) and the
store (about 100–200 ms), and should treat a Redis failure as a cache miss. A circuit
breaker on the store protects it when Redis is down and the miss rate spikes.

---

## 13. Write path (create)

```text
Client         Gateway          App          Safety         IdAlloc          DB
   │              │              │              │              │              │
   │ POST /urls   │              │              │              │              │
   │─────────────►│              │              │              │              │
   │              │ auth, limit  │              │              │              │
   │              │ forward      │              │              │              │
   │              │─────────────►│              │              │              │
   │              │              │ normalize    │              │              │
   │              │              │ check URL    │              │              │
   │              │              │─────────────►│              │              │
   │              │              │ ok or block  │              │              │
   │              │              │◄─────────────│              │              │
   │              │              │ next id      │              │              │
   │              │              │────────────────────────────►│              │
   │              │              │ id (local)   │              │              │
   │              │              │◄────────────────────────────│              │
   │              │              │ permute+b62  │              │              │
   │              │              │ INSERT       │              │              │
   │              │              │───────────────────────────────────────────►│
   │              │              │ ok           │              │              │
   │              │              │◄───────────────────────────────────────────│
   │              │ 201 shortUrl │              │              │              │
   │              │◄─────────────│              │              │              │
   │ 201 shortUrl │              │              │              │              │
   │◄─────────────│              │              │              │              │
   │              │              │              │              │              │
```

1. The gateway authenticates (or classifies the request as anonymous) and applies rate
   limits.
2. If an `Idempotency-Key` is present, check or claim it. A replay returns the stored
   response.
3. **Normalise** the URL: lowercase the scheme and host, remove default ports, IDNA-encode
   the host, and optionally strip tracking parameters. Reject bad schemes and own-domain
   targets.
4. **Safety check:** first the local blocklist cache, then an external reputation service
   such as Google Safe Browsing. If the external check times out, accept the link but mark
   it `pending_scan` and re-check asynchronously. Do not block creation on a third party.
5. **Get a code:** for a custom alias, use the alias. Otherwise take the next ID from the
   local block, permute it, and base62-encode it.
6. **Conditional insert.** For generated codes, a conflict is impossible by construction
   (but still checked, since it indicates a bug). For aliases, a conflict returns 409.
7. Optionally write through to Redis so the very first click is a cache hit. Then return
   201.

---

## 14. Analytics pipeline

```text
 ┌────────────┐   ┌──────────────────┐   ┌───────────────────────┐   ┌──────────────┐
 │ Redirect   │──►│ Kafka "clicks"   │──►│ Stream processor      │──►│ ClickHouse / │
 │ service    │   │ keyed by code,   │   │ (Flink / Kafka        │   │ Druid (OLAP) │
 │ (producer) │   │ 7-day retention  │   │ Streams): bot filter, │   └──────────────┘
 └────────────┘   └────────┬─────────┘   │ geo enrich, 1-min     │   ┌──────────────┐
                           │             │ windows, HLL uniques  │──►│ Redis: live  │
                           │             └───────────────────────┘   │ counters, HLL│
                           │                                         └──────────────┘
                           ▼
                  ┌──────────────────┐
                  │ Object storage   │  raw events (Parquet) for backfills and audits
                  └──────────────────┘
```

### Click event

```json
{
  "eventId": "01JBQ6Z8M4W1T7S2K9X3C5V0NR",
  "code": "aZ3kP9q",
  "ts": "2026-10-31T11:52:07.412Z",
  "country": "IN",
  "referrerHost": "t.co",
  "uaClass": "mobile-browser",
  "isBot": false,
  "visitorHash": "b3f1c9d27a0e4f55"
}
```

- **Keyed by `code`**, so all events for a link land in one partition, in order, and
  per-link aggregation needs no shuffle. A viral link makes its partition hot. Use enough
  partitions, and if needed salt the key for known-hot codes and merge downstream.
- **Privacy:** do not store raw IPs in analytics. Derive the country at the edge (GeoIP),
  and compute `visitorHash = hash(ip + userAgent + dailySalt)`. A rotating salt prevents
  long-term tracking while still counting daily uniques. Check the applicable privacy law
  (GDPR and others) for your exact obligations.
- **Bots and previews:** Slack, X/Twitter, iMessage and email security scanners fetch links
  to build previews or scan them. Classify by user agent and known IP ranges, mark
  `isBot`, and exclude these from "clicks" (but keep them for debugging).
- **Delivery semantics:** the producer is at-least-once (idempotent producer enabled). The
  stream job uses checkpoints and can deduplicate by `eventId` within a window. Small
  inaccuracies are acceptable for click stats, so state that explicitly.
- **Aggregation:** the stream job emits per-minute `(code, minute, country, referrer) →
  count` rows into the OLAP store, which rolls them up to hours and days. The stats API
  queries the OLAP store, never the link store.

```sql
CREATE TABLE clicks_by_minute
(
    code          String,
    minute        DateTime,
    country       LowCardinality(String),
    referrer_host LowCardinality(String),
    clicks        UInt64
)
ENGINE = SummingMergeTree
PARTITION BY toYYYYMM(minute)
ORDER BY (code, minute, country, referrer_host);
```

(ClickHouse syntax. `SummingMergeTree` sums `clicks` for rows with the same sort key
during background merges, so queries should still use `sum(clicks)`.)

### Unique visitors with HyperLogLog

Counting exact unique visitors per link per day requires storing every distinct visitor
id. For millions of links that is far too much memory. **HyperLogLog (HLL)** estimates
cardinality in fixed memory. Elements are hashed. The hash picks one of m registers, and
each register keeps the maximum number of leading zeros seen. Long runs of zeros are
unlikely, so they indicate many distinct values. A harmonic mean across registers gives
the estimate.

- Standard error ≈ 1.04 / √m. Redis uses m = 16,384 registers, so the error is **0.81%**,
  and each key uses at most **12 KB** (small cardinalities use a compact sparse encoding).
- HLLs are **mergeable**: the union of two HLLs is the register-wise max. Daily sketches can
  therefore be combined into weekly or monthly uniques without recounting. Merging gives
  the *union*. Intersections are not directly supported.

```bash
# Record a visitor (returns 1 if the estimate changed)
PFADD hll:aZ3kP9q:2026-10-31 b3f1c9d27a0e4f55

# Uniques for one day
PFCOUNT hll:aZ3kP9q:2026-10-31

# Uniques across several days (union estimated on the fly)
PFCOUNT hll:aZ3kP9q:2026-10-29 hll:aZ3kP9q:2026-10-30 hll:aZ3kP9q:2026-10-31

# Materialise a monthly sketch
PFMERGE hll:aZ3kP9q:2026-10 hll:aZ3kP9q:2026-10-01 hll:aZ3kP9q:2026-10-02
EXPIRE hll:aZ3kP9q:2026-10-31 7776000
```

For long-term storage, OLAP stores have built-in approximate distinct functions (for
example ClickHouse `uniq`/`uniqCombined` and Druid's HLL and Theta sketches), so sketches
can be stored per day and merged at query time.

---

## 15. Rate limiting and abuse

URL shorteners are attractive to spammers and phishers, because a short link hides the
destination and borrows your domain's reputation. If your domain gets onto blocklists, *all*
your customers' links stop working in email and chat apps. Abuse handling is therefore an
availability concern, not just trust and safety.

### Rate limiting creation

Use tiered limits, for example anonymous: 10 creates/hour per IP; free account: 100/hour;
API key: by plan. Enforce them at the gateway with a token bucket in Redis. A Lua script
makes the read-modify-write atomic:

```lua
-- KEYS[1] = bucket key, e.g. "rl:create:apikey:123"
-- ARGV[1] = capacity, ARGV[2] = refill tokens per second,
-- ARGV[3] = now in milliseconds, ARGV[4] = cost
local capacity = tonumber(ARGV[1])
local rate     = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])
local cost     = tonumber(ARGV[4])

local state  = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1]) or capacity
local ts     = tonumber(state[2]) or now

local elapsed = math.max(0, now - ts) / 1000
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], math.ceil(capacity / rate * 1000))
return allowed
```

Passing `now` from the caller keeps the script deterministic, but it requires reasonably
synced clocks across gateway instances. In Redis Cluster, the single key keeps the script
on one shard. Also rate-limit **redirect 404s** per IP to slow enumeration, and apply
coarse per-IP redirect limits at the edge against floods.

### Malware and phishing checks

- **At creation:** check the normalised URL and its host against internal blocklists and an
  external reputation API (for example the Google Safe Browsing / Web Risk APIs; check the
  current API version and terms). Block or hold for review.
- **Continuously:** the destination's content can change after creation. Re-scan links
  periodically, and especially when click volume suddenly spikes, which is a common sign of
  a spam campaign.
- **Signals:** a new account creating many links to one new domain, links to newly
  registered domains, lookalike domains (homoglyphs, punycode), and destinations that
  themselves redirect elsewhere.
- **Takedown flow:** set `status = 'disabled'`, delete the Redis key, purge the CDN, and
  serve a warning interstitial. Keep an audit log. Provide an abuse report endpoint.
- **Scanning safely:** if you fetch destination pages to scan or preview them, do it from an
  isolated network that cannot reach internal addresses. Otherwise a user can submit
  `http://169.254.169.254/...` and turn your scanner into an SSRF tool.

---

## 16. Expiry and cleanup

Expiry has two halves: **correct behaviour at read time** and **eventual physical
deletion**.

1. **Lazy check on read:** every redirect compares `expiresAt` with now and returns 410 if
   expired. This is always correct, regardless of when deletion happens.
2. **Cache TTL bound:** the Redis TTL is set to `min(default TTL, expiresAt − now)`, so the
   cache never outlives the link.
3. **Background sweeper:** a periodic job deletes expired rows in small batches using the
   partial index on `expires_at`. Deleting in small batches avoids long transactions and
   replication lag.

```sql
DELETE FROM links
WHERE code IN (
    SELECT code FROM links
    WHERE expires_at < now() - interval '30 days'
      AND status = 'active'
    LIMIT 5000
);
```

   The 30-day grace period keeps "recently expired" links answering 410 instead of 404 for
   a while, and makes accidental-expiry support tickets recoverable. With DynamoDB or
   Cassandra, use the native TTL instead, but still keep the lazy check, because native
   TTL deletion is not immediate.
4. **Never reuse codes.** An expired code may still be printed on posters or saved in old
   emails. Reusing it would send those readers to someone else's destination, which is a
   phishing vector. With 3.5 trillion codes, reuse saves nothing. If you must reclaim
   aliases (custom vanity names), require a long quarantine period.

---

## 17. Multi-region and availability

```text
             ┌────────────────────────────────────────────────┐
  users ───► │ Anycast / GeoDNS → nearest healthy region      │
             └───────────┬────────────────────────┬───────────┘
                         ▼                        ▼
           ┌──────────────────────────┐ ┌──────────────────────────┐
           │ Region A (ap-south)      │ │ Region B (us-east)       │
           │ redirect + API services  │ │ redirect + API services  │
           │ Redis (regional)         │ │ Redis (regional)         │
           │ link store replica       │ │ link store replica       │
           │ ID ranges: region bit 0  │ │ ID ranges: region bit 1  │
           └────────────┬─────────────┘ └─────────────┬────────────┘
                        │   async multi-region        │
                        └────── replication ──────────┘
```

- **Redirects are active-active.** Every region serves reads from its local cache and store
  replica. A region failure only shifts users to another region via anycast or DNS health
  checks.
- **Generated-code creates are active-active too,** because ID ranges are partitioned by
  region (a region bit or disjoint blocks). Two regions can never produce the same code, so
  no cross-region coordination is needed on the write path.
- **Replication lag and read-your-writes across regions:** a link created in Region A is
  briefly absent in Region B. Fixes: (a) on a local miss for a code whose region bit points
  elsewhere, fall back to a remote read in the home region before returning 404; (b) skip
  negative caching for codes that are younger than the replication lag; (c) both. Option
  (a) is attractive because the code itself encodes where it was created.
- **Custom aliases need global uniqueness.** Two users in different regions could claim
  `fall-sale` at the same moment. Many multi-region stores reconcile concurrent writes with
  last-writer-wins. DynamoDB global tables historically do this, though newer
  strong-consistency modes exist, so check the current docs. A per-region conditional
  write is therefore *not* globally safe. Options: route alias creation to a single home
  region (simple, and alias creation is rare), or use a globally consistent store (Spanner,
  CockroachDB, or a strongly consistent global table) just for the alias namespace.
- **Edge as a safety net:** with redirect responses cached at the CDN for 60 s and
  `stale-if-error`, popular links keep working even during a full origin outage.
- **Durability:** store data replicated across AZs synchronously and across regions
  asynchronously, so the RPO for a full region loss is seconds. Take daily snapshots plus
  continuous backups for logical corruption. Test restores.

---

## 18. Failure scenarios

| Failure | Impact | Mitigation |
| --- | --- | --- |
| Redis cluster down | Every redirect misses; store load jumps up to 10x | Treat cache errors as misses with tight timeouts; in-process LRU keeps hot links served; store provisioned for degraded hit rate; circuit breaker and load shedding protect the store |
| Store primary or shard leader fails | Creates fail for that shard; reads continue from replicas | Automatic failover (seconds); redirects read replicas; creates retry with idempotency keys |
| Kafka unavailable | Click events cannot be published | Bounded in-memory buffer, then drop with a metric (or spool to disk); redirects unaffected |
| Stream processor lagging | Stats delayed | Kafka retains 7 days; scale out consumers; stats API shows a "data as of" timestamp |
| ID coordinator down | New blocks cannot be claimed | Instances keep serving from the current and prefetched blocks; the coordinator is replicated (DB HA, ZooKeeper quorum) |
| Safe-browsing API down or slow | Creation latency or failures | Short timeout; accept as `pending_scan`, re-check asynchronously |
| Viral hot key | One Redis shard and one Kafka partition overloaded | In-process cache, CDN caching, key replication, salting hot partitions |
| Cache stampede after expiry or restart | Store overload | Request coalescing, TTL jitter, warm-up from analytics top-N, admission control |
| Enumeration scan | Load and data scraping | Permuted or random codes, negative caching, 404 rate limits, WAF rules |
| DDoS on redirect endpoint | Saturation | Anycast CDN absorption, edge rate limits, cached redirects, bot challenges for suspicious traffic |
| Snowflake clock moves backwards | Duplicate or failed ID generation | Refuse large backward jumps, wait out small ones, persist the last timestamp; or use block allocation |
| Region outage | Users in that region affected | Anycast or DNS failover to healthy regions; codes created there replicate asynchronously (small RPO) |
| Bad deploy of redirect service | Broken redirects everywhere | Canary per region and AZ, automatic rollback on SLO burn, keep the redirect service small and rarely changed |
| Domain placed on a blocklist | All links blocked by mail and chat providers | Proactive abuse detection, fast takedowns, separate domains for untrusted (anonymous) links |

---

## 19. Scaling bottlenecks and evolution

Design for today with a clear path to tomorrow. Interviewers like to hear the stages.

| Stage | Scale | Architecture | First bottleneck to watch |
| --- | --- | --- | --- |
| 0. MVP | Up to about 100 redirects/s | One app (2 instances for HA), Postgres primary + standby, sequence + base62, 302 redirects, synchronous click counter | Synchronous counter `UPDATE` causes row contention on popular links |
| 1. Early growth | Up to about 5k redirects/s | Redis cache-aside, read replicas, clicks sent to Kafka instead of `UPDATE`, rate limiting, Safe Browsing checks, multi-AZ | DB failover time; cache cold start |
| 2. Scale | Up to about 50k redirects/s | Separate redirect and API services, in-process hot cache, CDN in front of redirects, block allocation + permutation, OLAP analytics, HLL uniques | Single-primary write capacity and storage growth; hot keys |
| 3. Global | 100k+ redirects/s, global users | Sharded KV store (DynamoDB / Cassandra) or sharded SQL, multi-region active-active redirects, region-partitioned ID ranges, home-region aliases | Cross-region replication lag, alias uniqueness, cost |
| 4. Edge-native | Massive, latency-obsessed | Redirect logic and a replicated KV at the CDN edge (edge workers + edge KV), origin only for creates and misses | Edge KV propagation delay; consistency of disables |

Specific bottlenecks and their fixes:

- **Click counting in the main DB** is the classic first failure: `UPDATE links SET clicks =
  clicks + 1` on a viral link causes lock contention and write amplification. Move clicks
  to an async pipeline early.
- **Single-sequence ID generation** becomes an availability risk before it becomes a
  throughput limit. Switch to block allocation.
- **The cache hit rate** decides the store size. Watch it and the miss latency together.
- **The owner listing** gets slow with scatter-gather on a code-sharded store. Add the
  owner-keyed table or index.
- **Cost:** at high scale the main costs are CDN egress, KV read units on misses, and the
  analytics store. Caching and columnar compression are the levers.

---

## 20. Trade-offs

| Decision | Chosen | Alternative | Why |
| --- | --- | --- | --- |
| Redirect status | 302 (+ short CDN cache) | 301 | Accurate analytics and the ability to disable malicious links outweigh extra load |
| Code generation | Block allocation + Feistel permutation | Hash + retry, Snowflake, KGS, random | Dense and short (7 chars), no collisions, no hot-path coordination, non-sequential |
| Code length | 7 base62 chars | 6 or 8 | 3.5 T space, about 580x headroom over 6 B links |
| Destination mutability | Immutable | Editable | Prevents bait-and-switch phishing; makes caching safe |
| Store | KV (DynamoDB/Cassandra) at scale; Postgres for the MVP | Sharded SQL from day one | Access pattern is a primary-key lookup; avoid premature sharding |
| Caching | CDN + in-process LRU + Redis, cache-aside, negative caching | Redis only | Hot keys and scans need tiers in front of Redis |
| Analytics | Async via Kafka, approximate uniques (HLL) | Synchronous DB counters, exact uniques | Redirect latency and availability are independent of analytics |
| Multi-region | Active-active reads and generated creates; home-region aliases | Single-region writes for everything | Local creates without coordination; only aliases need global uniqueness |
| Expiry | Lazy check + background sweep, no code reuse | Eager deletion, code recycling | Correct at read time; reuse is a security risk |
| Safety checks | Synchronous with timeout, then asynchronous re-scan | Block creation until scanned | Third-party outages must not stop creation |

---

## Common mistakes

- Generating codes with `hash(url)` truncated to 7 characters and **no collision check**,
  or checking with a separate SELECT before the INSERT (a race condition).
- Using raw sequential IDs and calling it done, which makes codes enumerable.
- Picking 301 without discussing that analytics and takedowns stop working for cached
  clients.
- Updating a click counter in the links table on every redirect.
- Letting analytics failures (Kafka down) slow or break redirects.
- A cache without negative caching, which lets scanners pound the database.
- Forgetting hot keys. Average QPS is fine, but one viral link melts one shard.
- Sharding 3 TB on day one "for scale" without justification.
- Ignoring abuse. In practice it is the biggest operational problem for public shorteners.
- Reusing expired codes.
- Assuming per-region conditional writes give global uniqueness for custom aliases.

---

## Interview questions

**1. How do you guarantee that two concurrent creates never get the same code?**
Make uniqueness structural or atomic. With block allocation, each instance owns disjoint ID
ranges, so duplicates are impossible. With hash or random codes, use an atomic conditional
insert (`ON CONFLICT DO NOTHING`, `attribute_not_exists`, `IF NOT EXISTS`) and retry on
conflict. Never use a separate check followed by an insert.

**2. Why not just use MD5 of the URL and take 7 characters?**
Seven base62 characters is about 41.7 bits. With billions of links, the birthday bound
guarantees millions of collisions, so you still need collision detection and retries. It
also maps the same URL from different users to the same code, which breaks per-user
analytics and expiry. It works if combined with a conditional insert and a salted retry,
but a counter avoids the issue entirely.

**3. 301 or 302, and why?**
302 (or 307) by default: every click reaches us, so analytics are accurate and disabling a
malicious link takes effect immediately. 301 is cached by browsers, which saves load but
loses repeat clicks and control. If load is a concern, cache the 302 at our own CDN with a
short TTL and purge on disable, which gives most of the savings while keeping control.

**4. A link goes viral at 100k requests/s. What breaks and how do you handle it?**
The single Redis key and its shard, and the Kafka partition for that code. Mitigations:
CDN caching of the redirect (short TTL), an in-process LRU in every redirect instance (so
Redis sees almost nothing), replicating the hot key across shards, and salting the Kafka key
for hot codes with a downstream merge. Also load-test the edge path, since that is where
most of the traffic ends.

**5. How do you make codes non-guessable without giving up short, dense IDs?**
Pass the sequential ID through a keyed bijection over a fixed bit width (for example a
4-round Feistel network on 40 bits) before base62 encoding. It is a permutation, so codes
stay unique and short but look random. For security-sensitive links, use long random
tokens instead, since obfuscation is not encryption.

**6. Your Redis cluster just failed over and the cache is cold. What happens?**
The hit rate drops from about 90% to near zero, so store reads rise roughly 10x. If the
store was sized only for cached traffic, it overloads and redirects fail. Prepare: provision
the store for degraded hit rates, add request coalescing and the in-process cache tier,
warm Redis with the top-N codes from analytics, and shed load (prioritise redirects over
API calls) while warming.

**7. How do you count unique visitors per link per day for millions of links?**
Use HyperLogLog: about 12 KB per sketch with 0.81% standard error in Redis. Sketches merge by
register-wise max, so daily sketches combine into weekly or monthly uniques. Visitor
identity is a privacy-preserving hash of IP and user agent with a rotating daily salt. In
the OLAP store, use the built-in approximate distinct functions or stored sketches.

**8. How would you support custom aliases in a multi-region active-active deployment?**
Generated codes are safe because ranges are region-partitioned. Aliases need global
uniqueness, and most multi-region replication resolves concurrent writes with
last-writer-wins, which would silently let two users claim the same alias. Route alias
claims to a single home region (or a globally consistent store such as Spanner or
CockroachDB) and replicate the result outward. Alias creation is rare, so the extra
cross-region latency is acceptable.

**9. A user deletes a link but people still reach the destination. Why, and how do you fix
it?**
Something cached the redirect: browsers (if 301 or a long `max-age`), the CDN, Redis, or the
in-process LRU. The fix is to use 302 with no or short browser caching. On delete, delete
the Redis key and issue a CDN purge, and keep the in-process TTL short (30–60 s). Document
that a delete takes up to about a minute to propagate everywhere.

**10. Snowflake IDs: good choice here?**
They are excellent for coordination-free, time-ordered 64-bit IDs, but they encode to 10–11
base62 characters, much longer than the 7 a dense counter gives. Their time ordering also
leaks creation time and makes neighbours guessable. Use them for internal ids (click
events), not for short codes. Clock skew handling is their main operational risk.

**11. How do you prevent the shortener from being used for phishing?**
Validate schemes and block loops. Check URLs at creation against blocklists and reputation
APIs, re-scan periodically and on traffic spikes, keep destinations immutable to prevent
bait-and-switch, rate-limit anonymous creation, and use a separate domain for untrusted
links. Have a fast takedown path (disable, cache delete, CDN purge, interstitial) and an
abuse report channel.

**12. How would you implement expiry?**
Check expiry lazily on every read (410 Gone), and bound the cache TTL by the remaining
lifetime. Delete physically with a batched background sweeper over an `expires_at` index,
or with native TTL in DynamoDB or Cassandra, which is not immediate, so the lazy check
still matters. Keep a grace period, and never reuse codes.

**13. What would you build first if you had two weeks?**
One stateless service on two instances, Postgres with a standby, a sequence passed
through a permutation and base62, 302 redirects, Redis cache-aside, click events written
to a queue (or even a log file) rather than a counter column, and basic rate limiting and
URL validation. This handles thousands of redirects per second and evolves cleanly into
the full design.

**14. How do you estimate the cache size needed?**
Model popularity as Zipf. The hit rate for caching the top C of N links is about
ln C / ln N (plus a small constant). With 6 billion links, caching 100 million entries of
about 200 B (20 GB) gives about 82%, and recency skew pushes it higher. Then verify against
real traffic: measure the hit rate against cache size by simulating an LRU on sampled logs.

---

## Summary

- **Core:** map a 7-character base62 code to a long URL. The workload is about 100:1
  read-heavy, so redirects must be fast, highly available and independent of everything
  else.
- **Numbers:** about 40 creates/s, 4k–50k redirects/s, 6 B links (3 TB) over 5 years, and a
  20 GB cache for an 80–90% hit rate. Bandwidth is trivial.
- **Codes:** block-allocated counters (no collisions, no hot-path coordination) plus a keyed
  Feistel permutation (non-sequential) plus base62. Alternatives: hash + conditional insert,
  random + conditional insert, KGS, Snowflake (too long for codes).
- **Redirects:** 302 by default for analytics and control. 301/308 only if you accept losing
  both. Use 410 for expired links.
- **Caching:** CDN, then in-process LRU, then Redis, then the store. Use negative caching,
  hot-key handling and stampede control, and size the store for a cold cache.
- **Storage:** primary-key lookups. Postgres for the MVP, a sharded KV store at scale,
  sharded by code, RF = 3 across AZs. Add an owner-keyed index for listings.
- **Analytics:** async Kafka to stream aggregation to OLAP, HLL for uniques, bot filtering,
  privacy-preserving visitor hashes. Never let analytics affect redirects.
- **Safety:** URL validation, rate limits, reputation checks plus re-scans, immutable
  destinations, a fast takedown path, and no code reuse.
- **Global:** active-active redirects, region-partitioned ID ranges, home-region aliases, and
  remote fallback reads for replication lag.

---

## Further reading

- Alex Xu, *System Design Interview – An Insider's Guide*, Volume 1, chapter "Design a URL
  Shortener".
- Martin Kleppmann, *Designing Data-Intensive Applications* (O'Reilly): partitioning,
  replication, and consistency chapters.
- Twitter Engineering, "Announcing Snowflake" (2010), and the original `twitter-archive/snowflake`
  repository on GitHub.
- Instagram Engineering, "Sharding & IDs at Instagram" (2012), an alternative ID scheme
  using Postgres-generated time-ordered IDs.
- Philippe Flajolet, Éric Fusy, Olivier Gandouet, Frédéric Meunier, "HyperLogLog: the
  analysis of a near-optimal cardinality estimation algorithm" (2007), and Stefan Heule,
  Marc Nunkesser, Alexander Hall, "HyperLogLog in Practice" (EDBT 2013).
- Redis documentation: HyperLogLog (`PFADD`, `PFCOUNT`, `PFMERGE`) and scripting with Lua.
- RFC 9110, *HTTP Semantics*, section 15.4 (Redirection 3xx), and RFC 9111, *HTTP
  Caching*.
- Michael Luby and Charles Rackoff, "How to Construct Pseudorandom Permutations from
  Pseudorandom Functions" (SIAM Journal on Computing, 1988), the theory behind Feistel
  permutations. Also NIST SP 800-38G for format-preserving encryption (FF1/FF3-1).
- Google Safe Browsing and Web Risk API documentation (check the current API version).
- Amazon DynamoDB Developer Guide: conditional writes, Time to Live, and global tables.
