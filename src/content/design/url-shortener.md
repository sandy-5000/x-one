---
title: "Case study: URL shortener"
order: 11
---
# Case Study: URL Shortener

A worked example that pulls the other concepts together: design a service like bit.ly.

## 1. Requirements

**Functional**

- Given a long URL, return a short one: `https://x1.io/aZ3kP9q`.
- Visiting the short URL redirects to the long URL.
- Optional: custom aliases, expiry, click analytics.

**Non-functional**

- Very high availability — broken redirects break other people's sites.
- Redirect latency under ~50 ms.
- Short codes must not be guessable in sequence (nice to have).
- Read-heavy: roughly 100 redirects per URL created.

## 2. Estimates

- 100 M new URLs per month ≈ **40 writes/s** (peak ~100/s).
- 100:1 read ratio → **4,000 redirects/s** (peak ~10,000/s).
- Each record ~500 bytes; 5 years = 6 B URLs → **~3 TB** of storage.
- Hot set: 20% of URLs get 80% of traffic → caching a few hundred GB covers most reads.

**Code length:** base62 (`a–z A–Z 0–9`) with 7 characters gives 62^7 ≈ **3.5 trillion** codes — plenty.

## 3. API

```text
POST /api/v1/urls
{ "longUrl": "https://example.com/very/long/path", "alias": null, "expiresAt": null }
→ 201 { "shortUrl": "https://x1.io/aZ3kP9q" }

GET /aZ3kP9q
→ 302 Found
  Location: https://example.com/very/long/path
```

**301 vs 302:** 301 (permanent) is cached by browsers, so later clicks never reach us — less load but no analytics. 302 (temporary) sends every click through us. Choose 302 if analytics matter.

## 4. Data model

```sql
CREATE TABLE urls (
    code        VARCHAR(10) PRIMARY KEY,
    long_url    TEXT        NOT NULL,
    user_id     BIGINT,
    created_at  TIMESTAMP   NOT NULL,
    expires_at  TIMESTAMP
);
```

The only hot query is lookup by `code` — a key-value access pattern. Either a relational DB with the primary key, or a key-value store like DynamoDB/Cassandra for easy horizontal scaling.

## 5. Generating short codes

### Option A — hash the URL

`base62(md5(longUrl))[0..7]`. Same URL → same code (free de-duplication), but collisions need detection and retry.

### Option B — counter + base62 (preferred)

Take a unique 64-bit ID and encode it in base62.

```java
static final String ALPHABET =
        "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";

static String toBase62(long id) {
    StringBuilder sb = new StringBuilder();
    do {
        sb.append(ALPHABET.charAt((int) (id % 62)));
        id /= 62;
    } while (id > 0);
    return sb.reverse().toString();
}

toBase62(125);          // "21"
toBase62(3_500_000_000_000L);   // 7 characters
```

No collisions by construction. To get unique IDs across many servers without a single bottleneck:

- **Range allocation** — each app server grabs a block of 10,000 IDs from a coordinator (ZooKeeper / DB row) and hands them out locally.
- **Snowflake IDs** — timestamp + machine ID + sequence, generated locally.

To avoid sequential, guessable codes, shuffle the ID with a reversible bit permutation before encoding.

## 6. High-level design

```text
                     ┌──────────────┐
 client ──► CDN ───► │ Load Balancer│
                     └──────┬───────┘
                ┌───────────┼───────────┐
                ▼           ▼           ▼
           ┌────────┐  ┌────────┐  ┌────────┐
           │ App 1  │  │ App 2  │  │ App 3  │   stateless
           └───┬────┘  └───┬────┘  └───┬────┘
               │  ID range │ from coordinator
               ▼           ▼           ▼
           ┌──────────────────────────────┐
           │      Redis cache cluster     │   code → longUrl
           └──────────────┬───────────────┘
                          │ miss
           ┌──────────────▼───────────────┐
           │   Key-value DB (sharded by   │
           │   code, 3 replicas each)     │
           └──────────────────────────────┘

 click events ──► Kafka ──► analytics workers ──► OLAP store
```

**Redirect path:** LB → app → Redis (hit ~90%+) → DB on miss → fill cache → 302.

**Create path:** LB → app → next ID from local range → base62 → write DB → return.

## 7. Deep dives

- **Caching:** cache-aside with LRU, TTL of a day; cache "not found" briefly to blunt scans for random codes.
- **Sharding:** hash-based on `code` with consistent hashing — every lookup goes to exactly one shard.
- **Analytics:** don't write to the DB on each click; publish an event to Kafka and aggregate asynchronously so redirects stay fast.
- **Rate limiting:** limit `POST /urls` per user and per IP to stop spam generation.
- **Abuse:** check long URLs against malware/phishing lists before creating codes.
- **Expiry:** lazy delete (check `expires_at` on read) plus a background cleanup job.
- **Availability:** multi-AZ replicas, multiple app instances, and Redis replicas; if Redis dies, reads fall back to the DB.

## 8. Trade-offs recap

| Decision | Chosen | Gave up |
| --- | --- | --- |
| 302 redirect | Analytics on every click | Higher load than 301 |
| Counter + base62 | No collisions, short codes | Needs ID coordination |
| Key-value store | Easy horizontal scaling | No ad-hoc joins |
| Async analytics | Fast redirects | Stats are seconds behind |
