---
title: "Case study: URL shortener"
order: 11
---
# Case Study: URL Shortener

Like bit.ly: `https://x1.io/aZ3kP9q` → long URL redirect. Simple core; senior depth = **ID generation**, **302 vs 301**, **cache under skew**, **analytics at scale**, **abuse**, **multi-region**.

---

## Clarify (ask first)

| Question | Assumption here |
| --- | --- |
| Scale (creates, read:write)? | 100M links/month; **100:1** read:write |
| Link lifetime? | Forever default; optional expiry |
| Custom aliases? | Yes (logged-in) |
| Same long URL → same code? | New code per request (optional per-owner dedup) |
| Analytics freshness? | Yes; ~1 min lag |
| Anonymous create? | Yes, rate-limited |
| Codes guessable? | Non-sequential; links still "public if you have URL" |
| Edit destination? | **Immutable** (anti-phishing, cache-safe) |
| Global / latency? | Redirect p99 **<50 ms** server; **<20 ms** at edge cache |
| Code length? | **7** base62 chars target |

---

## Requirements

| Type | Item |
| --- | --- |
| **Functional** | Create short link (optional alias, expiry); `GET /{code}` redirect; owner list/disable/delete; stats (clicks, uniques, geo, referrer) |
| **Non-functional** | Redirects **99.99%**; create **99.9%**; durable mappings; new link visible in seconds (read-your-writes in region); **unique codes forever**; no open redirect; rate-limit create |
| **Out of scope** | Link-in-bio, QR render, A/B URLs, billing (mention as extensions) |

---

## Estimates

| Metric | Calculation |
| --- | --- |
| **Writes** | 100M/mo ≈ **40/s** avg, **~200/s** peak (×5) |
| **Reads** | ×100 ≈ **4k/s** avg, **~12k/s** daily peak, **~50k/s** viral (few hot codes) |
| **Link storage (5y)** | 6×10⁹ × ~500 B ≈ **3 TB** primary (~9 TB RF=3) — fits one Postgres; sharding later optional |
| **Click events** | ~3.5×10⁸/day × ~200 B ≈ **70 GB/day** raw → OLAP + compression |
| **Redirect egress** | ~12k/s × 400 B ≈ **40 Mbps** peak — trivial |
| **Redis cache** | 100M entries × ~200 B ≈ **20 GB** → ~**80–90%** hit (Zipf + recency) |
| **Code space** | 62⁷ ≈ **3.5×10¹²** (~580× headroom over 6B links) |
| **Redirect fleet** | ~50k peak / ~5k per instance ≈ **10–15** instances (+ AZ headroom) |

---

## API (sketch)

```text
POST /api/v1/links     → 201 { code, shortUrl, longUrl, ... }
                       → 409 alias taken; 422 blocked URL; 429 rate limit
                       Idempotency-Key for safe retries

GET  /{code}           → 302 Location (default); 404 unknown; 410 expired/deleted
GET  /api/v1/links/{code}/stats?from&to&granularity=day

GET/PATCH/DELETE /api/v1/links/{code}   owner metadata / disable / delete
```

**Validation:** `http`/`https` only; max ~2 KB URL; block loops to self/other shorteners; alias `[A-Za-z0-9_-]{4,32}`; reserve `api`, `admin`, …

**302 not 301:** every click hits you → analytics + takedowns work. Optional CDN `s-maxage` + purge on disable.

| Code | Cached by browser? | Shortener use |
| --- | --- | --- |
| 301/308 | Often yes (permanent) | Loses repeat analytics; disable ineffective for cached clients |
| 302/307 | Only with explicit cache headers | **Default** — control + accurate stats |

---

## High-level design

```text
  clients ──► CDN/WAF (optional redirect cache, short TTL)
                  │
                  ▼
           L7 gateway (auth /api, rate limits)
                  │
        ┌─────────┴─────────┐
        ▼                   ▼
  Redirect service      Link API service
  (hot, minimal deps)   (create, manage, stats queries)
        │                   │
        ├── Redis code→URL ─┤ writes store + warm Redis
        │                   │
        ▼                   ▼
   Link store (PK=code, RF=3, shard by code)
        │
        └── async ──► Kafka "clicks" ──► stream job ──► OLAP + Redis HLL (optional)
```

Split **redirect** vs **API** so stats deploys never break redirects.

**Store choice:** Postgres MVP; DynamoDB/Cassandra when multi-region or >~10 TB. Access pattern = **point lookup by code**.

**Data model (minimal):**

```sql
-- PK lookup dominates
CREATE TABLE links (
  code VARCHAR(32) PRIMARY KEY,
  long_url TEXT NOT NULL,
  owner_id BIGINT,
  status TEXT DEFAULT 'active',  -- active | disabled | deleted
  created_at TIMESTAMPTZ,
  expires_at TIMESTAMPTZ
);
CREATE INDEX links_owner ON links (owner_id, created_at DESC);
-- Create: INSERT ... ON CONFLICT (code) DO NOTHING RETURNING code;
```

Optional `links_by_owner` table if sharded by `code` and listings scatter-gather.

---

## Deep dive 1: ID generation

Must: **unique forever**, **7 chars**, no hot coordinator, **non-guessable**, available on coordinator blip.

| Approach | Collisions | Coord | Length @ scale | Guessable? |
| --- | --- | --- | --- | --- |
| Hash URL + retry | Yes | None | 7 | No |
| DB sequence | No | Every write | 6–7 | Yes |
| **Block allocation** | No | Once per ~10k IDs | 6–7 | Yes unless permuted |
| Snowflake | No | Worker ID | **10–11** | Partly time-ordered |
| Random + conditional insert | Rare | None | 7–8 | No |
| KGS pre-gen pool | No | Batch claim | 7 | No |

**Recommended:** **Block allocation** (`SEQUENCE INCREMENT BY 10000`) + **Feistel permutation** on ID before base62 → dense, collision-free, looks random. Prefetch next block; region-partitioned ranges for multi-region.

**Uniqueness under concurrency:** never SELECT-then-INSERT. Use `INSERT … ON CONFLICT DO NOTHING` / `attribute_not_exists` / `IF NOT EXISTS` and retry (hash/random) or structural disjoint IDs (blocks).

**Custom aliases:** same conditional insert; **global uniqueness** in multi-region → route alias create to **home region** or strongly consistent store (Spanner).

**Write path (create):** gateway auth + rate limit → normalize URL (scheme/host, strip trackers) → safety (blocklist + Safe Browsing, timeout → `pending_scan`) → allocate ID / alias → conditional insert → optional Redis write-through → 201. Idempotency-Key for safe client retries.

---

## Deep dive 2: Redirect hot path

```text
GET /code → validate charset
         → in-process LRU (Caffeine, ~100k, 30–60s)
         → Redis u:{code} (TTL min(24h, time-to-expiry))
         → store PK lookup → populate cache (+ negative cache 404 ~30–60s)
         → check status / expiresAt → 410 if dead
         → async click event to Kafka (drop if buffer full — redirects sacred)
         → 302 + Location (~1–2 ms on cache hit)
```

| Layer | Role |
| --- | --- |
| CDN | Absorb viral link; short TTL; purge on disable |
| In-process | **Hot key** shield (one Redis key ≠ one shard meltdown) |
| Redis | Shared cache; stampede: coalesce loads, TTL jitter |
| Store | Sized for **cold cache** (~10× miss rate if Redis fails) |

**Timeouts:** Redis ~20–50 ms; store ~100–200 ms; Redis down = miss, not fail. Circuit breaker on store when cache cold.

**301/302 summary:** 301/308 cached by browsers → lost analytics & broken takedowns for repeat visitors. Default **302** (+ controlled CDN cache).

**Hit-rate intuition (Zipf):** caching top C of N links ≈ `ln(C)/ln(N)` of traffic; 100M of 6B ≈ ~82% before recency skew → **90%+** realistic with LRU. Size store for **cold cache** (Redis failover → ~10× DB reads).

---

## Deep dive 3: Analytics (optional path)

Not on critical path.

```text
Redirect ──► Kafka (key=code, enough partitions) ──► Flink/Streams
              │ bot filter, geo from edge, referrer
              └──► OLAP (ClickHouse/Druid) aggregates
              └──► Redis PFADD per (code, day) for live uniques (~12 KB/sketch, ~0.8% error)
```

- Event: `{ code, ts, country, referrerHost, uaClass, isBot, visitorHash }` — **no raw IP**; `visitorHash = hash(ip+UA+dailySalt)`.
- Exclude preview bots (Slack, iMessage scanners) from "clicks".
- **At-least-once** OK; dedupe by `eventId` in window. Stats API reads OLAP, never link store.
- **Never** `UPDATE clicks` on link row — row lock on viral links.

---

## Evolution stages

| Stage | Scale | Architecture |
| --- | --- | --- |
| MVP | ~100 r/s | Monolith + Postgres sequence + 302; clicks async not `UPDATE` |
| Growth | ~5k r/s | Redis cache-aside, Kafka clicks, rate limits, multi-AZ |
| Scale | ~50k r/s | Split redirect/API, CDN, block IDs + Feistel, OLAP + HLL |
| Global | 100k+ r/s | Sharded KV, region ID ranges, home-region aliases |

---

## Failure modes (selected)

| Failure | Mitigation |
| --- | --- |
| Redis down | Treat as miss; in-process LRU; store sized for degraded hit rate |
| Kafka down | Drop/buffer clicks; never block redirect |
| Viral hot key | CDN + local cache; replicate Redis key suffixes |
| ID coordinator down | Serve from current + prefetched blocks |
| Domain blocklisted | Proactive abuse + fast takedown |

---

## Other essentials (one-liners)

- **Sharding:** by `code` only — never by `created_at`.
- **Expiry:** lazy 410 on read; Redis TTL bound; background batch delete; **never reuse codes** (phishing if poster still has old URL).
- **Abuse:** rate-limit create + 404 scans; Safe Browsing at create (timeout → pending_scan); re-scan on spikes; disable → purge Redis + CDN; SSRF-safe fetch for scanners.
- **Multi-region:** active-active redirects; **region bit** in ID ranges; replication lag → warm Redis on create, optional home-region read on miss, careful negative cache.

---

## Trade-offs (interview)

| Decision | Choice | Why |
| --- | --- | --- |
| Redirect | 302 + short CDN cache | Stats + takedown vs 301 browser cache |
| IDs | Block + Feistel + base62 | Short, unique, non-sequential |
| Destination | Immutable | Phishing + safe caching |
| Analytics | Kafka + OLAP + HLL | Never on redirect critical path |
| Aliases (global) | Home region write | True global uniqueness |

---

## Pitfalls

- Hash without atomic insert / check-then-insert race
- Sequential IDs without permutation
- 301 without discussing cache
- Sync click counter in DB
- Kafka failure blocking redirect
- No negative caching → scan hammers DB
- Ignoring hot keys / one viral code
- Sharding 3 TB on day one
- Reusing expired codes
- Per-region alias writes assuming global uniqueness

---

## Interview quick check

| Q | A |
| --- | --- |
| Same code concurrently? | Disjoint ID blocks or atomic conditional insert + retry |
| MD5 first 7 chars? | ~41 bits → birthday collisions; still need atomic insert; same URL collision across users |
| 301 vs 302? | 302 for analytics/control; CDN cache if load matters |
| 100k/s on one link? | CDN + in-process cache; hot Redis replication; Kafka partition salt |
| Non-guessable short IDs? | Feistel on counter or random codes; long tokens for secrets |
| Redis cold? | Store sized for ~10× reads; coalesce; warm top-N; shed non-redirect |
| Uniques at scale? | HyperLogLog; rotating salt |
| Multi-region aliases? | Home region or global consistent store |
| Delete but still redirects? | Purge CDN + Redis; short in-process TTL; avoid 301 |
| Two-week MVP? | Postgres + sequence+permute+base62, 302, Redis, async clicks, rate limits |

---

## Summary

**100:1 reads** → protect redirect path (302, tiered cache, async analytics). **7-char codes** via block allocation + permutation (or random conditional insert). **Postgres until** multi-region/TB forces KV. **Abuse** is operational reality — rate limits, scanning, immutable destinations, no code reuse.

---

## Further reading

Alex Xu URL shortener chapter; Kleppmann *DDIA* (partitioning); Twitter Snowflake post; Redis HLL docs; RFC 9110 (3xx); Google Safe Browsing / Web Risk.
