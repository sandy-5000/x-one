---
title: "System design basics"
order: 3
---
# System Design Basics

System design is the work of deciding how the pieces of a large application fit together:
clients, DNS, load balancers, stateless services, caches, databases, queues and object
storage. The goal is a system that stays **fast, available, correct and affordable** while
traffic, data and the team all grow. This page covers the foundations that every later
design page builds on: how to run a design interview, how to state requirements precisely,
how to estimate, and the core building blocks (load balancing, proxies, CDNs, DNS, HTTP,
redundancy and reliability patterns).

Throughout, keep one idea in mind: **every design decision is a trade-off that should be
justified by a requirement or a number.** "We add a cache" is weak. "Reads are 50x writes,
p99 must be under 100 ms, and the DB would need 40k QPS without a cache, so we put Redis in
front of it" is a senior answer.

---

## The interview framework

A system design interview is a structured conversation. It is not a quiz with one correct
answer. The interviewer wants to see how you handle an ambiguous problem: whether you scope
it, quantify it, design something that works, and then reason about where it breaks.

### Timing for a 45–60 minute round

| Phase | Time (45 min) | What you produce |
| --- | --- | --- |
| 1. Clarify requirements | 0–5 min | Functional list, non-functional targets, explicit out-of-scope items |
| 2. Estimate | 5–10 min | QPS (avg and peak), storage growth, bandwidth, cache size; only numbers that drive decisions |
| 3. API and data model | 10–15 min | 3–6 endpoints, main entities, the hot access patterns |
| 4. High-level design | 15–25 min | Boxes and arrows covering every functional requirement end to end |
| 5. Deep dives | 25–40 min | 2–3 hard parts in depth: data partitioning, consistency, hot keys, fan-out |
| 6. Wrap-up | 40–45 min | Bottlenecks, failure modes, monitoring, how the design evolves at 10x |

For a 60-minute round, stretch the deep dives. Never spend more than about 10 minutes before
something is on the whiteboard; an interviewer who has seen no design by minute 20 has very
little signal.

### Step by step

1. **Clarify requirements.** Ask who the users are, what the core use cases are, and what
   scale is expected. Write the functional requirements as a short numbered list. Then ask
   about non-functional targets: latency, availability, consistency, durability, and
   regional scope. State assumptions out loud when the interviewer says "you decide".
2. **Estimate.** Turn users into requests per second, bytes per day, and memory. Round
   aggressively. The purpose is to discover the *shape* of the problem: read-heavy or
   write-heavy, fits on one machine or needs sharding, CDN-worthy or not.
3. **Define the API.** A few REST or RPC signatures with inputs, outputs and error cases.
   This pins down exactly what the system must do and exposes hidden requirements
   (pagination, idempotency, auth).
4. **Model the data.** Entities, keys, relationships, and above all the *access patterns*:
   "get feed by user id ordered by time", "lookup by short code". The access patterns choose
   the database, not the other way around.
5. **High-level design.** Draw the request path for each use case. Start simple: client, load
   balancer, stateless service, database. Add components only when a requirement or estimate
   demands them.
6. **Deep dives.** Pick the hardest parts, or let the interviewer pick. Typical topics: how
   data is partitioned, how a counter stays correct under concurrency, how a hot key is
   handled, how a fan-out is bounded, what happens when a dependency fails.
7. **Trade-offs and evolution.** Say what you gave up, what fails first at 10x load, and what
   you would monitor.

### What interviewers look for at senior level (IC4/IC5)

| Signal | Mid-level answer | Senior answer |
| --- | --- | --- |
| Scoping | Accepts the prompt as given | Negotiates scope, names out-of-scope items, prioritises |
| Numbers | Skips estimates or computes but never uses them | Uses estimates to justify sharding, caching, CDN, machine counts |
| Building blocks | Names technologies ("use Kafka") | Explains why: ordering per key, replay, consumer groups, retention |
| Trade-offs | One option presented as the answer | Two or three options compared on explicit criteria |
| Failure | Happy path only | Timeouts, retries, partial failure, data loss windows, failover |
| Operations | Not mentioned | Metrics, SLOs, alerts, deploys, migrations, cost |
| Driving | Waits for prompts | Leads the conversation, checks in, manages time |
| Depth | Shallow everywhere | At least one area in real depth (internals, concurrency, consistency) |

Common anti-patterns: jumping straight to microservices and Kubernetes, naming a database
without discussing access patterns, drawing a box labelled "cache" without saying what is
cached, with which key and TTL, and how it is invalidated.

---

## Requirements

### Functional vs non-functional

**Functional requirements** describe *what* the system does: "users can post a photo",
"followers see new posts in their feed", "a short link redirects to the original URL".

**Non-functional requirements** (quality attributes) describe *how well* it does it:
latency, throughput, availability, durability, consistency, security, cost, and
operability. Non-functional requirements drive almost all architecture. Two systems with
identical features can look completely different at 100 QPS and at 1 million QPS.

### SLI, SLO, SLA

These terms come from Google's SRE practice and are worth using precisely.

| Term | Meaning | Example |
| --- | --- | --- |
| SLI (indicator) | A measured quantity | Fraction of HTTP requests that return non-5xx within 300 ms |
| SLO (objective) | Internal target for an SLI over a window | 99.9% of requests good over 30 days |
| SLA (agreement) | A contract with customers, with penalties | 99.5% monthly uptime or service credits |
| Error budget | 1 − SLO | 0.1% of requests in 30 days may fail |

The SLA is always looser than the SLO, so the team gets warning before contractual
penalties apply. The **error budget** turns reliability into an engineering decision: while
budget remains, ship features; when it is burned, prioritise reliability work.

A good SLI is measured **as close to the user as possible** (load balancer logs or client
telemetry rather than a server's own view, which misses requests that never arrived).

### Latency and percentiles

Averages hide the experience of the slowest users. Use percentiles:

- **p50 (median):** half of requests are faster than this.
- **p99:** 99% are faster; 1 in 100 is slower.
- **p99.9:** 1 in 1,000 is slower. For a user who makes 100 requests per page session, the
  tail is not rare at all.

Why the tail matters more at scale: if one user request fans out to 100 backend calls in
parallel and waits for all of them, and each backend has a p99 of 1 s, then the probability
that at least one call is slow is

```text
1 - 0.99^100 = 1 - 0.366 = 0.634
```

So **63% of user requests** see the 1-second tail. This is the central point of Dean and
Barroso's "The Tail at Scale". Mitigations: hedged requests (send a second request after
the p95 delay, take the first response), tied requests, reducing fan-out, and partial
results.

Percentiles cannot be averaged across servers. The mean of each host's p99 is not the
fleet p99. Aggregate raw histograms (HdrHistogram, Prometheus histograms, t-digest) and
compute the percentile from the merged distribution.

### Throughput

Throughput is work per unit time: requests per second (QPS/RPS), messages per second,
bytes per second. Latency and throughput are linked by **Little's law**:

```text
L = λ × W
in-flight requests = arrival rate × time in system
```

At 2,000 requests/s and 50 ms each, 2,000 × 0.05 = **100 requests are in flight** on
average. That number sizes thread pools, connection pools and concurrency limits. If
latency doubles because a dependency slowed down, in-flight work doubles too. That is how
a slow database exhausts the connection pools of every service in front of it.

### Durability, consistency, availability

- **Durability:** once acknowledged, data is not lost. Achieved with replication (synchronous
  to at least one other node or AZ), write-ahead logs and fsync, backups, and checksums.
  Object stores advertise very high durability. For example, Amazon S3 is designed for
  99.999999999% (11 nines) durability. Durability is not the same as availability: S3 can
  be briefly unreachable without losing data.
- **Consistency:** what a reader is guaranteed to see after a write. The spectrum runs from
  linearizable (behaves like a single copy) through causal and read-your-writes to eventual
  consistency (replicas converge "eventually"). Stronger guarantees cost latency and
  availability during partitions (CAP, PACELC; see the consistency pages).
- **Availability:** the fraction of time (or of requests) the system serves successfully.
  Request-based availability ("good requests / total requests") is usually more meaningful
  than time-based uptime.

Ask the interviewer which matters most per operation. A payment ledger needs strong
consistency and durability. A like counter tolerates eventual consistency and even small
loss. A news feed values availability and low latency over freshness.

---

## Back-of-the-envelope estimation

Estimates exist to make decisions: one database or a sharded cluster, whether a cache fits
in memory, whether you need a CDN, roughly how many servers. Round freely. Being within a
factor of 2–3 is fine. Being off by 1000x is not.

### Powers of two and units

| Power | Exact value | Approximation | Common use |
| --- | --- | --- | --- |
| 2^10 | 1,024 | 1 thousand | 1 KB |
| 2^16 | 65,536 | 65 thousand | TCP ports, `uint16` |
| 2^20 | 1,048,576 | 1 million | 1 MB |
| 2^30 | 1,073,741,824 | 1 billion | 1 GB |
| 2^32 | 4,294,967,296 | 4.3 billion | IPv4 addresses, `uint32` |
| 2^40 | about 1.1 × 10^12 | 1 trillion | 1 TB |
| 2^50 | about 1.13 × 10^15 | 1 quadrillion | 1 PB |
| 2^63 | about 9.2 × 10^18 | 9 quintillion | Java `long` max + 1 |

### Handy constants

| Fact | Value |
| --- | --- |
| Seconds per day | 86,400 ≈ 10^5 (use 10^5 for mental math; it overestimates by about 15%) |
| Seconds per month | about 2.6 × 10^6 |
| Seconds per year | about 3.15 × 10^7 |
| 1 million requests/day | about 12 requests/s |
| 1 billion requests/day | about 12,000 requests/s |
| 100 million per month | about 40 per second |
| Peak-to-average ratio | 2–3x for most consumer apps; 10x+ for spiky events (sales, live sports) |
| 1 Gbps | 125 MB/s |
| Typical record sizes | `long` / timestamp 8 B, UUID 16 B, short text post 200–500 B, JSON API response 1–10 KB, image thumbnail 20–50 KB, photo 200 KB–2 MB, 1 min of 1080p video 50–100 MB |

### Worked example 1: QPS for a social app

Assumptions: 200 million daily active users (DAU). Each user opens the feed 10 times a day
and posts 0.5 times a day.

```text
Feed reads  = 200M × 10   = 2 × 10^9 per day
            = 2 × 10^9 / 86,400 ≈ 23,000 QPS average
            peak (×3)        ≈ 70,000 QPS

Post writes = 200M × 0.5  = 10^8 per day
            = 10^8 / 86,400 ≈ 1,150 QPS average
            peak (×3)        ≈ 3,500 QPS

Read:write ≈ 20:1  →  read-heavy; invest in caching and precomputed feeds.
```

### Worked example 2: storage growth

Each post stores text and metadata of about 1 KB. 10% of posts include one photo of about
200 KB (stored once in object storage, plus a few resized variants of about 50 KB total).

```text
Post metadata: 10^8 posts/day × 1 KB         = 100 GB/day
               × 365                          ≈ 36.5 TB/year
               × 3 replicas                   ≈ 110 TB/year   → must shard

Media:         10^7 photos/day × 250 KB       = 2.5 TB/day
               × 365                          ≈ 900 TB/year   → object storage
```

Decision: metadata goes to a sharded database (tens of nodes at a few TB each). Media goes
to object storage such as S3 or GCS, with the database storing only the object key.

### Worked example 3: bandwidth

A feed response contains 20 posts at about 1 KB of JSON each, so about 20 KB per response.
The client then loads 5 thumbnails at 40 KB each.

```text
API egress:   23,000 QPS × 20 KB   = 460 MB/s  ≈ 3.7 Gbps average, ~11 Gbps peak
Image egress: 23,000 × 5 × 40 KB   = 4.6 GB/s  ≈ 37 Gbps average, ~110 Gbps peak
```

Decision: about 110 Gbps of image traffic at peak should never touch your origin. Serve
images from a CDN with long cache lifetimes. API traffic can be served from a few regions.

### Worked example 4: memory for a cache

Cache the hot posts. By the usual 80/20 rule, about 20% of a day's posts receive most reads.

```text
Posts per day       = 10^8
Hot fraction        = 20%   → 2 × 10^7 posts
Size per entry      = 1 KB payload + ~100 B key/overhead ≈ 1.1 KB
Cache size          = 2 × 10^7 × 1.1 KB ≈ 22 GB
```

22 GB fits in one large Redis node. You would still run a small cluster (for example 3
primaries with 1 replica each) for throughput and availability. For precomputed timelines
the numbers look very different:

```text
Timelines: 200M users × 800 post ids × 8 B = 1.28 TB
```

1.28 TB is far too much for one node, so cache timelines only for active users (for
example the last 7 days of DAU), shard by user id, and rebuild a timeline on a miss.

### Worked example 5: number of servers

There are two ways to size a fleet. Use both and take the larger result.

**CPU-bound approach.** Load tests show one request costs about 10 ms of CPU. A 16-core
server therefore does at most 16 / 0.010 = 1,600 requests/s at 100% CPU. Plan for about 50%
utilisation to leave headroom for spikes, GC pauses and a lost AZ, which gives 800 requests/s
per server.

```text
Peak QPS            = 70,000
Per-server capacity = 800
Servers             = 70,000 / 800 ≈ 88
With N+1 AZ headroom (lose 1 of 3 AZs → remaining 2 must carry everything):
                      88 × 3/2 ≈ 132 servers, spread 44 per AZ
```

**Concurrency-bound approach (Little's law).** Each request spends 80 ms in the service,
mostly waiting on I/O. Each server can hold 200 concurrent requests, limited by its thread
pool or connection pool.

```text
In-flight at peak   = 70,000 × 0.080 = 5,600
Servers             = 5,600 / 200 = 28
```

Here CPU is the binding constraint (88 vs 28), so plan for about 130 servers. If latency
regresses to 400 ms, the concurrency estimate becomes 140 servers, and concurrency becomes
the bottleneck. This is why concurrency limits matter.

### Estimation pitfalls

- Forgetting **peak** vs average, or forgetting replication (×3) and indexes (+20–50%).
- Estimating storage but not **growth rate**: "36 TB/year" drives the sharding plan, not
  today's size.
- Mixing bits and bytes in bandwidth (network links are in bits per second).
- Computing numbers and then never using them. Every estimate should end with a decision.

---

## Latency numbers to know

These are approximate modern figures, popularised by Jeff Dean and updated by others such
as Colin Scott's interactive version. Learn the **orders of magnitude**, not the digits.

| Operation | Approximate time | Relative |
| --- | --- | --- |
| L1 cache reference | 1 ns | 1x |
| Branch mispredict | 3 ns | |
| L2 cache reference | 4 ns | |
| Mutex lock/unlock (uncontended) | 17 ns | |
| Main memory reference | 100 ns | 100x L1 |
| Compress 1 KB with a fast codec (Snappy/LZ4) | 2 µs | |
| Read 1 MB sequentially from memory | 3 µs | |
| SSD random read (4 KB) | 16 µs | 160x memory reference |
| Read 1 MB sequentially from SSD | 50 µs | |
| Round trip within one datacenter / AZ | 500 µs | |
| Round trip between AZs in a region | 1–2 ms | |
| HDD seek | 2–10 ms | |
| Read 1 MB sequentially from HDD | about 1 ms | |
| Round trip US East to US West | about 60–70 ms | |
| Round trip California to Netherlands | about 150 ms | |
| Round trip India to US East | about 200–250 ms | |

How to interpret them:

- **Memory is cheap, network is not, cross-region is expensive.** A Redis GET is dominated
  by the roughly 0.5 ms network round trip, not by Redis's microsecond lookup. Batch calls
  (`MGET`, pipelines) to amortise round trips.
- **Sequential beats random.** This is why log-structured storage (LSM trees, Kafka) gets
  high write throughput: it turns random writes into sequential appends.
- **The speed of light is a hard floor.** Light in fibre travels about 200 km per ms, so a
  10,000 km path costs at least 100 ms per round trip. No software optimisation fixes
  geography. Only moving data or compute closer to users does (CDN, edge, multi-region).
- **Count round trips on the critical path.** A page that makes 6 sequential cross-region
  calls has spent over a second before doing any work.
- **Serialization is not free.** Parsing a 1 MB JSON payload takes milliseconds of CPU.
  Measure it before assuming the network is the bottleneck.

---

## Scaling: vertical vs horizontal

**Vertical scaling (scale up):** move to a bigger machine with more cores, RAM and faster
disks. Cloud instances reach hundreds of vCPUs and several TB of RAM.

- No code changes, no distributed-systems complexity, strong consistency for free.
- There is a hard ceiling, cost grows faster than linearly near the top, and the machine is
  a single point of failure. Upgrades often need a restart or failover.

**Horizontal scaling (scale out):** add more machines and spread work across them.

- Near-linear capacity growth, commodity hardware, survives machine loss, and you can scale
  down as well as up.
- Requires stateless services or partitioned data, plus handling of network failures,
  rebalancing and distributed consistency.

```text
      Vertical                               Horizontal

 ┌──────────────────┐                   ┌───────────────────┐
 │     BIG BOX      │                   │   Load balancer   │
 │ 192 vCPU, 2 TB   │                   └──┬──────┬──────┬──┘
 └──────────────────┘                      │      │      │
                                         ┌─▼──┐ ┌─▼──┐ ┌─▼──┐
                                         │ S1 │ │ S2 │ │ S3 │
                                         └────┘ └────┘ └────┘
```

In practice you combine both. Stateless tiers scale horizontally. Databases are often
scaled vertically for as long as possible, because one well-tuned Postgres primary on a
large machine handles a surprising amount of load. Read replicas come next, and sharding
is the last resort. Premature sharding is one of the most expensive mistakes in system
design.

### Stateless services and session handling

To scale horizontally, **any instance must be able to serve any request.** That means no
request-relevant state may live only in one instance's memory or on its local disk.

| Approach | How it works | Pros | Cons |
| --- | --- | --- | --- |
| Sticky sessions | LB pins a client to one server (cookie or IP hash) | No shared store | Uneven load; sessions lost on instance death; hard to drain |
| Central session store | Session id cookie maps to data in Redis or a DB | Any instance serves any request; easy revocation | Extra round trip; the store must be highly available |
| Client-side token (JWT) | Signed claims travel with each request | No lookup; good across services | Hard to revoke before expiry; token size; key rotation |
| Hybrid | Short-lived JWT access token plus a refresh token checked against a store | Fast path without lookup, revocation within minutes | More moving parts |

Other state to externalise:

- **Uploaded files:** object storage, never local disk.
- **Caches:** a local in-process cache is fine as an optimisation only if a miss is
  harmless.
- **Scheduled jobs:** a distributed scheduler or a leader-elected worker, not "every
  instance runs cron".
- **WebSocket connections** are inherently stateful. Keep a registry (connection id to gateway
  node) in Redis and route messages through it.

---

## Load balancing deep dive

A load balancer (LB) spreads requests across a pool of healthy backends. It provides
horizontal scale, hides failures, and gives one stable address to clients.

### Layer 4 vs Layer 7

```text
 L4 (transport)                           L7 (application)

 client ──TCP/UDP──► ┌──────────┐         client ──HTTPS──► ┌────────────────┐
                     │ L4 LB    │                           │ L7 LB / proxy  │
                     │ IP:port  │                           │ TLS terminated │
                     └────┬─────┘                           │ reads HTTP     │
                          │ same TCP stream                 └───┬────────┬───┘
                          ▼ (NAT or DSR)                        │        │
                     ┌──────────┐                  API pool ◄───┘        └───► asset pool
                     │ backend  │                  (/api/*)                    (/static/*)
                     └──────────┘
```

| Aspect | L4 | L7 |
| --- | --- | --- |
| Sees | IP addresses, ports, TCP/UDP | Full HTTP: path, headers, cookies, method |
| Routing | Per connection | Per request (one connection can carry many requests) |
| TLS | Passes through (or terminates in some products) | Usually terminates TLS |
| Performance | Very high; millions of packets/s, low latency | Lower; parses every request |
| Features | Simple hashing, health checks | Path/host routing, retries, header rewrite, auth, compression, rate limiting, gRPC awareness |
| Examples | AWS NLB, Google Maglev, IPVS, HAProxy in TCP mode | AWS ALB, Envoy, Nginx, HAProxy in HTTP mode, Traefik |

A common layout uses both: an L4 tier spreads TCP connections across a fleet of L7 proxies,
and the L7 proxies make per-request decisions. Note that **HTTP/2 and gRPC multiplex many
requests over one long-lived connection**, so an L4 balancer balances connections, not
requests, and can leave load badly skewed. gRPC traffic needs L7 (request-level) balancing
or client-side balancing.

### Algorithms

| Algorithm | How it picks | Good for | Watch out for |
| --- | --- | --- | --- |
| Round robin | Next backend in order | Identical backends and similar requests | Ignores slow or overloaded backends |
| Weighted round robin | Proportional to configured weight | Mixed instance sizes, canaries (5% weight) | Static weights drift from reality |
| Least connections | Backend with fewest active connections | Long or variable request durations | Needs global view; herding when many LBs see the same "least" backend |
| Least outstanding requests / least latency (EWMA) | Fewest in-flight requests or lowest smoothed latency | Heterogeneous latency | Feedback loops; a fast-failing backend looks "fast" |
| Power of two choices | Pick 2 at random, choose the less loaded | Many independent LBs or clients | Needs a load signal (in-flight count) |
| IP hash / header hash | Hash of client IP or a header | Affinity without shared state | Pool changes remap most clients |
| Consistent hashing (ring, Maglev, rendezvous) | Hash key onto a ring | Caches and shards where affinity matters | Hot keys still overload one node |

#### Power of two choices

Picking the globally least-loaded backend requires a consistent global view. With many LB
instances, each one's view is stale, so all of them send traffic to the same "best" backend
at once (herding). Picking **two backends at random and sending to the less loaded one**
avoids this and still gives a dramatic improvement over pure random. With n backends and n
requests, the maximum load drops from about log n / log log n (random) to about
log log n / log 2 (two choices), as shown by Mitzenmacher. Envoy's least-request balancer
and many client-side balancers use this idea.

```java
import java.util.List;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Function;

public final class PowerOfTwoChoicesBalancer {

    public record Backend(String address, AtomicInteger inFlight) {
        public Backend(String address) {
            this(address, new AtomicInteger());
        }
    }

    private final List<Backend> backends;

    public PowerOfTwoChoicesBalancer(List<Backend> backends) {
        if (backends.size() < 2) {
            throw new IllegalArgumentException("need at least two backends");
        }
        this.backends = List.copyOf(backends);
    }

    public Backend pick() {
        var rnd = ThreadLocalRandom.current();
        int i = rnd.nextInt(backends.size());
        int j = rnd.nextInt(backends.size() - 1);
        if (j >= i) {
            j++;
        }
        Backend a = backends.get(i);
        Backend b = backends.get(j);
        return a.inFlight().get() <= b.inFlight().get() ? a : b;
    }

    public <T> T call(Function<Backend, T> request) {
        Backend backend = pick();
        backend.inFlight().incrementAndGet();
        try {
            return request.apply(backend);
        } finally {
            backend.inFlight().decrementAndGet();
        }
    }
}
```

#### Consistent hashing

With `hash(key) % N`, changing N from 10 to 11 remaps about 10/11 of all keys, which is a
disaster for a cache cluster because almost everything misses at once. Consistent hashing
places nodes and keys on the same hash ring. A key belongs to the first node clockwise from
its hash, so adding or removing a node moves only about 1/N of the keys. **Virtual nodes**
(each physical node placed at 100–200 points on the ring) smooth out the distribution and
spread a failed node's load across all survivors instead of one neighbour.

```text
                 hash ring (0 .. 2^64-1)

                        A#1
                   ●─────────●
               C#2/             \ B#1
                 ●               ●
                 │   key k ──►   │   k maps to the first vnode
                 ●               ●   clockwise from hash(k)
               B#2 \             / A#2
                   ●─────────●
                        C#1
```

```java
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Map;
import java.util.TreeMap;

public final class ConsistentHashRing {
    private final TreeMap<Long, String> ring = new TreeMap<>();
    private final int virtualNodes;

    public ConsistentHashRing(int virtualNodes) {
        this.virtualNodes = virtualNodes;
    }

    public synchronized void addNode(String node) {
        for (int i = 0; i < virtualNodes; i++) {
            ring.put(hash(node + "#" + i), node);
        }
    }

    public synchronized void removeNode(String node) {
        for (int i = 0; i < virtualNodes; i++) {
            ring.remove(hash(node + "#" + i), node);
        }
    }

    public synchronized String nodeFor(String key) {
        if (ring.isEmpty()) {
            throw new IllegalStateException("ring is empty");
        }
        Map.Entry<Long, String> e = ring.ceilingEntry(hash(key));
        return (e != null ? e : ring.firstEntry()).getValue();
    }

    private static long hash(String s) {
        try {
            byte[] d = MessageDigest.getInstance("MD5")
                    .digest(s.getBytes(StandardCharsets.UTF_8));
            long h = 0;
            for (int i = 0; i < 8; i++) {
                h = (h << 8) | (d[i] & 0xFF);
            }
            return h;
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}
```

MD5 is used here only for its spread, not for security. Production systems use faster
non-cryptographic hashes such as MurmurHash3 or xxHash. Alternatives to the ring include
**rendezvous (highest random weight) hashing**, which needs no ring state, and **Maglev
hashing** (Google), which builds a lookup table with near-perfect balance and minimal
disruption, designed for L4 balancers.

### Health checks

- **Active checks:** the LB probes `GET /healthz` every few seconds. A backend is marked
  unhealthy after N consecutive failures (for example 3 failures at 5 s intervals) and
  healthy again after M successes. This hysteresis prevents flapping.
- **Passive checks (outlier detection):** the LB watches real traffic and ejects a backend
  that returns too many 5xx responses or times out. This is faster than active probing and
  catches "healthy but broken" instances. Envoy calls this outlier detection.
- **Liveness vs readiness:** liveness asks "should this process be restarted?" and readiness
  asks "should it receive traffic right now?". During startup or warm-up an instance is live
  but not ready.
- **Shallow vs deep checks:** a deep check that also queries the database looks thorough, but
  if the database blips, *every* instance fails its check at once and the LB removes the
  whole fleet. Keep the check that controls traffic shallow (process healthy, can serve) and
  report dependency health separately. Some LBs, such as AWS ALB, "fail open" and route to
  all targets if every target is unhealthy, precisely because of this failure mode.

### Connection draining

When an instance is removed during a deploy or scale-in, abruptly closing it kills
in-flight requests. **Connection draining** (AWS calls it *deregistration delay*) stops
sending new requests to the instance but lets existing ones finish, up to a timeout. The
application side of a graceful shutdown:

1. Receive SIGTERM, start failing readiness so the LB stops routing new requests.
2. Keep serving in-flight requests. For HTTP/1.1 keep-alive, send `Connection: close`. For
   HTTP/2, send GOAWAY.
3. Wait for in-flight work to finish, up to a deadline, then exit.

Kubernetes adds a subtlety: endpoint removal propagates asynchronously, so a pod may still
receive traffic for a few seconds after SIGTERM. A short `preStop` sleep covers that gap.

### High availability for the load balancer itself

The LB must not become a single point of failure.

- **Active-passive with a floating IP:** two LB nodes share a virtual IP using VRRP (for
  example `keepalived`). If the active node stops sending heartbeats, the passive node takes
  over the IP within seconds.
- **Active-active with ECMP:** routers spread traffic across several L4 LB nodes with
  equal-cost multi-path routing. Consistent hashing in each LB (Maglev) keeps existing
  connections on the same backend even when the set of LB nodes changes.
- **Managed cloud LBs** (ALB, NLB, Google Cloud Load Balancing) are already distributed
  across zones. You still have to deploy backends in multiple AZs.

### Global load balancing: DNS, GSLB and anycast

Before a request reaches a regional LB it must pick a region.

| Technique | How it works | Strengths | Weaknesses |
| --- | --- | --- | --- |
| DNS round robin | Several A records; clients pick one | Trivial | No health awareness; clients cache records |
| GeoDNS / latency-based DNS (GSLB) | Authoritative DNS answers with the closest or healthiest region (for example Route 53 latency or geolocation routing) | Region steering, health-checked failover | Failover limited by TTLs and resolvers that ignore them; locates the resolver, not the user (EDNS Client Subnet helps) |
| Anycast | The same IP is announced via BGP from many locations; routing delivers packets to the nearest | Fast failover (withdraw the route), DDoS absorption, no DNS TTL issues | Needs your own network or a provider (Cloudflare, Google Front End, AWS Global Accelerator); route changes can break long TCP flows |

Typical large setup: anycast to an edge network that terminates TLS close to the user, then
private backbone transport to the best healthy region.

---

## Reverse proxy, API gateway and CDN

### Forward vs reverse proxy

```text
Forward proxy:  clients ──► proxy ──► internet ──► server
                (acts for clients: corporate egress, filtering, anonymity)

Reverse proxy:  client ──► internet ──► proxy ──► server pool
                (acts for servers: one address, TLS, caching, routing)
```

A **reverse proxy** (Nginx, Envoy, HAProxy, Caddy) sits in front of servers. It terminates
TLS, compresses responses, buffers slow clients so app threads are not tied up, caches
responses, enforces timeouts and request size limits, and routes to backends. Most L7 load
balancers are reverse proxies.

### API gateway

An API gateway is a reverse proxy with API-management features, sitting at the edge of a
service-oriented backend:

- Authentication (API keys, OAuth 2.0 / OIDC token validation) and coarse authorisation.
- Rate limiting and quotas per client or plan.
- Routing `/v1/orders/*` to the orders service, versioning, canary splits.
- Request and response transformation, protocol translation (REST to gRPC).
- Centralised access logs, metrics and tracing headers.

Examples include Kong, AWS API Gateway, Apigee, and Envoy-based gateways. Pitfalls: putting
business logic in the gateway (it becomes a distributed monolith), and the gateway being a
shared bottleneck and single point of failure. A **Backend for Frontend (BFF)** is a variant
with one gateway per client type (web, iOS, Android) that aggregates calls for that client.

### CDN (content delivery network)

A CDN is a globally distributed set of caching reverse proxies (points of presence, PoPs).
Users connect to a nearby edge, which serves cached content or fetches from your origin.

```text
 user (Mumbai) ──► edge PoP Mumbai ──hit──► response in ~10–30 ms
                         │
                         │ miss
                         ▼
                   regional / shield cache ──miss──► origin (us-east-1)
                                                      ~200+ ms round trip
```

**Pull vs push**

| | Pull CDN | Push CDN |
| --- | --- | --- |
| How content arrives | Edge fetches from origin on first miss | You upload to the CDN ahead of time |
| Setup | Point the CDN at your origin; control via headers | Publishing pipeline must upload |
| Best for | Websites, APIs, frequently changing assets | Large, rarely changing files (software downloads, video libraries) |
| Downside | First request per edge is slow; origin load spikes after purges | Storage cost; you manage freshness |

**Cache keys.** By default the key is roughly scheme + host + path + query string. Including
unneeded parts (tracking parameters like `utm_source`, random cookies) fragments the cache
and destroys the hit rate. Normalise or strip them. Headers listed in `Vary` (for example
`Accept-Encoding`) add variants. `Vary: Cookie` or `Vary: User-Agent` effectively disables
caching.

**Freshness and invalidation.**

- `Cache-Control: public, max-age=31536000, immutable` for **content-hashed file names**
  such as `app.3f9a1c.js`. A new deploy produces a new name, so no invalidation is needed.
  This is the best strategy.
- `s-maxage` sets the shared (CDN) TTL separately from the browser TTL.
- `stale-while-revalidate` and `stale-if-error` (RFC 5861) let the edge serve stale content
  while refreshing in the background, or while the origin is down.
- **Purge / invalidation APIs** remove objects by URL, prefix or tag (surrogate keys).
  Purges propagate in seconds to minutes and cost money at scale. Treat them as a tool,
  not the primary freshness mechanism.
- An **origin shield** (a mid-tier cache) collapses misses from many edges into one origin
  request, which protects the origin after a purge.

**Edge compute.** CDNs run code at the edge: Cloudflare Workers, Fastly Compute, AWS
CloudFront Functions and Lambda@Edge. Typical uses are auth token checks, A/B routing,
header rewrites, redirects, personalisation of cached pages, and bot filtering. Limits on
CPU time, memory and available APIs vary by product, so check the current docs.

**Dynamic content.** Even uncacheable API calls benefit from a CDN. TLS terminates close to
the user (saving round trips), and the edge reuses warm, long-lived connections to the
origin over the provider's backbone.

---

## DNS resolution path

DNS maps names to addresses through a hierarchy of servers, with caching at every step.

```text
Browser        OS stub       Resolver         Root         TLD .com        Auth NS
   │              │              │              │              │              │
   │ lookup       │              │              │              │              │
   │─────────────►│              │              │              │              │
   │              │ cache?       │              │              │              │
   │              │ A? ex.com    │              │              │              │
   │              │─────────────►│              │              │              │
   │              │              │ cache miss   │              │              │
   │              │              │ query        │              │              │
   │              │              │─────────────►│              │              │
   │              │              │ .com NS      │              │              │
   │              │              │◄─────────────│              │              │
   │              │              │ query        │              │              │
   │              │              │────────────────────────────►│              │
   │              │              │ ex.com NS    │              │              │
   │              │              │◄────────────────────────────│              │
   │              │              │ query        │              │              │
   │              │              │───────────────────────────────────────────►│
   │              │              │ A + TTL      │              │              │
   │              │              │◄───────────────────────────────────────────│
   │              │ A + TTL      │              │              │              │
   │              │◄─────────────│              │              │              │
   │ IP           │              │              │              │              │
   │◄─────────────│              │              │              │              │
   │              │              │              │              │              │
```

1. The browser checks its own cache, then asks the OS stub resolver (which checks
   `/etc/hosts` and its cache).
2. The stub sends a recursive query to a configured **recursive resolver** (ISP, corporate,
   or public such as 1.1.1.1 or 8.8.8.8).
3. On a miss, the resolver walks the hierarchy iteratively: a root server refers it to the
   `.com` TLD servers, which refer it to the domain's **authoritative** name servers, which
   return the record and its TTL.
4. Every layer caches the answer for the TTL. Referrals for root and TLD are cached for a
   long time, so in practice most lookups cost one round trip to a nearby resolver.

Design implications:

- **TTL is a trade-off.** Short TTLs (30–60 s) allow fast failover and traffic shifting but
  increase query load and lookup latency. Some resolvers and clients (notably older JVM
  settings and some mobile stacks) cache longer than the TTL, so DNS failover is never
  instant.
- Lower the TTL **before** a planned migration, not during it.
- **CNAME chains** add lookups. A CNAME is not allowed at the zone apex; providers offer
  ALIAS/ANAME-style records to work around this.
- DNS is a dependency. Use a provider with anycast authoritative servers, or two providers.
- Modern transports (DNS over HTTPS and DNS over TLS) encrypt queries but do not change the
  hierarchy.

---

## HTTP/1.1, HTTP/2, HTTP/3 and TLS cost

| Feature | HTTP/1.1 | HTTP/2 | HTTP/3 |
| --- | --- | --- | --- |
| Transport | TCP | TCP | QUIC over UDP |
| Format | Text | Binary frames | Binary frames |
| Concurrency | One request at a time per connection; browsers open about 6 connections per host | Many streams multiplexed on one connection | Many independent streams on one connection |
| Head-of-line blocking | At HTTP level (a slow response blocks the connection) | Solved at HTTP level, but one lost TCP packet stalls **all** streams | Solved: loss on one stream does not block others |
| Header compression | None | HPACK | QPACK |
| TLS | Optional | Effectively required by browsers | Built in (TLS 1.3 inside QUIC) |
| Connection migration | No | No | Yes (connection IDs survive Wi-Fi to cellular switch) |
| Current spec | RFC 9112 | RFC 9113 | RFC 9114 (QUIC: RFC 9000) |

HTTP/2 server push was specified but saw little real use, and major browsers have removed
support. Use `103 Early Hints` and preload links instead.

### Keep-alive and connection reuse

Opening a connection costs round trips (TCP handshake, then TLS). **Persistent connections**
(keep-alive, the default since HTTP/1.1) and **connection pools** let many requests reuse one
connection. Inside a datacenter, a service calling another without pooling pays a handshake
on every call and can run out of ephemeral ports (TIME_WAIT accumulation). Always pool
connections to databases, caches and downstream services, and size the pools with Little's
law.

### Handshake round trips

```text
Round trips before the first response byte (new connection, no resumption)

TCP + TLS 1.2 + HTTP:  TCP SYN/ACK (1) + TLS (2) + request (1)   = 4 RTT
TCP + TLS 1.3 + HTTP:  TCP SYN/ACK (1) + TLS (1) + request (1)   = 3 RTT
QUIC (HTTP/3):         QUIC+TLS combined (1)     + request (1)   = 2 RTT
QUIC with 0-RTT resumption: request sent with first flight       = 1 RTT
```

At a 200 ms India to US round trip, that is 800 ms vs 400 ms before any server work
happens. Terminating TLS at a nearby edge (10 ms away) reduces the handshake cost to tens
of milliseconds. The edge then uses an already-warm connection to the origin.

**0-RTT caveat:** 0-RTT early data can be replayed by an attacker, so servers should accept
it only for idempotent requests such as GET.

TLS CPU cost is small on modern hardware (AES-NI, ECDHE), so the latency cost is in round
trips, not cryptography. Session resumption and TLS 1.3 are the main levers.

---

## Availability, redundancy and disaster recovery

### The nines

| Availability | Downtime per year | Per month (30 days) | Per week |
| --- | --- | --- | --- |
| 99% | 3.65 days | 7.2 hours | 1.68 hours |
| 99.9% | 8.77 hours | 43.2 minutes | 10.1 minutes |
| 99.95% | 4.38 hours | 21.6 minutes | 5.04 minutes |
| 99.99% | 52.6 minutes | 4.32 minutes | 1.01 minutes |
| 99.999% | 5.26 minutes | 25.9 seconds | 6.05 seconds |

At four nines you have about 4 minutes per month. A human cannot even be paged and log in
within that budget, so four nines and beyond require **automated** detection and failover.

### Series and parallel composition

**Series:** every component must work, so availabilities multiply.

```text
A_series = A1 × A2 × ... × An
LB 99.99% → app 99.95% → DB 99.95%  =  0.9999 × 0.9995 × 0.9995 ≈ 99.89%
```

A chain is always less available than its weakest link. Ten microservices at 99.9% called
in series give 0.999^10 ≈ 99.0%.

**Parallel:** the system works if any one replica works (assuming independent failures).

```text
A_parallel = 1 − (1 − A)^n
two replicas at 99.5%:   1 − 0.005^2 = 99.9975%
three replicas at 99.5%: 1 − 0.005^3 ≈ 99.99999%
```

Worked example of a full stack:

```text
LB (managed)           99.99%
app tier: 3 × 99.5%    1 − 0.005^3        ≈ 99.99999%
DB primary + failover  99.95%   (includes failover time)
total ≈ 0.9999 × 0.9999999 × 0.9995       ≈ 99.94%  → about 5.3 hours/year
```

The math makes the lesson visible: the redundant stateless tier contributes almost
nothing to downtime. **The stateful components and their failover dominate.**

**The independence assumption is the trap.** Replicas in the same rack, AZ, deploy
pipeline, configuration push or certificate expiry fail *together*. Most large outages are
correlated failures: bad config pushed everywhere, a shared dependency down, a region-wide
control plane problem. Redundancy must be paired with isolation: different AZs, staggered
deploys, cell-based architecture, and limited blast radius.

### Redundancy and failover

```text
 Active-passive                          Active-active

 ┌────────────┐  replication  ┌────────────┐     ┌────────────┐     ┌────────────┐
 │  Primary   │──────────────►│  Standby   │     │  Node A    │◄───►│  Node B    │
 │ (traffic)  │               │ (idle/warm)│     │ (traffic)  │sync │ (traffic)  │
 └────────────┘               └────────────┘     └────────────┘     └────────────┘
       ▲ on failure: promote standby,               both serve; on failure the
         move VIP/DNS, fence old primary            survivor absorbs the load
```

| | Active-passive | Active-active |
| --- | --- | --- |
| Capacity use | Standby idle (paying for 2x) | All nodes serve traffic |
| Failover time | Detection + promotion + reroute: seconds to minutes | Near zero for stateless; reroute only |
| Data consistency | Simple: single writer | Hard for stateful data: conflicts, needs partitioning or consensus |
| Headroom rule | Standby must equal the primary | Each node must run below 1/n of max load, so survivors can absorb a failure |
| Typical use | Relational DB primary/replica, LB pair | Stateless services, multi-leader or partitioned data stores |

**Failover mechanics and dangers:**

- **Detection:** heartbeats with timeouts. Too short causes false failovers during GC pauses
  or network blips. Too long extends downtime.
- **Split brain:** both nodes believe they are primary and accept writes. Prevent it with a
  quorum-based leader election (etcd, ZooKeeper, Raft) and **fencing**: the old primary's
  writes are rejected using a fencing token or epoch, or it is cut off from storage.
- **Data loss window:** with asynchronous replication, writes acknowledged by the old primary
  but not yet replicated are lost on failover. Use semi-synchronous or synchronous
  replication to at least one standby when that is unacceptable.
- **Test it.** Untested failover usually fails. Run game days and chaos experiments.

### Multi-AZ and multi-region

- An **availability zone** is one or more datacenters with independent power, cooling and
  networking, a few to tens of km from other AZs in the region, with about 1–2 ms latency
  between them. Synchronous replication across AZs is practical. **Multi-AZ is the default
  for anything in production.**
- A **region** is a geographic area hundreds or thousands of km away. Cross-region latency
  (tens to hundreds of ms) makes synchronous replication expensive, so cross-region
  replication is usually asynchronous.
- Multi-region is chosen for (a) latency to global users, (b) surviving a region-wide
  outage, and (c) data residency laws. It costs a lot: data replication, conflict handling,
  duplicated infrastructure, and much harder testing. Common patterns are a read-local /
  write-global single home region, users partitioned by home region, or full multi-leader
  with conflict resolution.

### RPO and RTO

- **RPO (recovery point objective):** how much data, measured in time, you can afford to
  lose. RPO = 5 minutes means you may lose the last 5 minutes of writes.
- **RTO (recovery time objective):** how long until service is restored after a disaster.

| DR strategy | Description | RPO | RTO | Cost |
| --- | --- | --- | --- | --- |
| Backup and restore | Periodic backups shipped to another region | Hours | Hours to a day | Low |
| Pilot light | Data replicated continuously; minimal core infra running, rest provisioned on demand | Minutes | Tens of minutes to hours | Medium |
| Warm standby | Scaled-down but fully functional copy running in another region | Seconds to minutes | Minutes | Higher |
| Multi-site active-active | Full capacity in several regions serving traffic | Near zero | Near zero | Highest |

These four names follow AWS's disaster recovery guidance and are widely used. Other
essentials:

- Backups are worthless until a **restore** has been tested. Automate restore drills.
- Use point-in-time recovery (WAL archiving) as protection against *logical* corruption.
  Replication faithfully copies a bad `DELETE` to every replica.
- Keep immutable or offline backups in a separate account as protection against ransomware
  and operator error.

---

## Reliability patterns

Distributed systems fail partially: one dependency is slow, one shard is down, one AZ is
lossy. These patterns stop a partial failure from turning into a total outage.

### Timeouts and deadlines

Every network call needs a timeout. Without one, a hung dependency holds threads and
connections until the caller's pools are exhausted and it fails too (cascading failure).

- Set timeouts from the dependency's measured latency, for example a little above its p99.9,
  not from a round number like 30 s.
- **Propagate deadlines.** If the user-facing request has 500 ms left, downstream calls must
  not wait 2 s. gRPC propagates deadlines natively. For HTTP, pass a remaining-budget
  header.
- Distinguish connect timeouts (short, about 100 ms–1 s) from read/request timeouts.

### Retries with exponential backoff and jitter

Retries handle transient failures, but naive retries cause **retry storms**. If a struggling
service gets three times its normal traffic because every client retries immediately, it
never recovers.

Rules:

1. Retry only errors that are **transient** (timeouts, 503, connection reset) and only
   operations that are **idempotent**.
2. Use **exponential backoff with jitter.** The "full jitter" variant from Marc Brooker's
   AWS article sleeps a random time in `[0, min(cap, base × 2^attempt)]`, which
   de-synchronises clients.
3. Cap attempts (2–3 total) and respect the overall deadline.
4. **Retry at one layer only.** If 3 layers each retry 3 times, a failing leaf receives
   3 × 3 × 3 = 27 times the load.
5. Use a **retry budget**: allow retries only up to, say, 10% of normal request volume per
   client, so retries cannot multiply load during an outage.
6. Honour `Retry-After` headers.

```java
import java.time.Duration;
import java.util.concurrent.Callable;
import java.util.concurrent.ThreadLocalRandom;
import java.util.function.Predicate;

public final class Retry {

    private Retry() {}

    public static <T> T withFullJitter(Callable<T> call,
                                       int maxAttempts,
                                       Duration base,
                                       Duration cap,
                                       Predicate<Exception> retryable) throws Exception {
        for (int attempt = 0; ; attempt++) {
            try {
                return call.call();
            } catch (Exception e) {
                boolean last = attempt + 1 >= maxAttempts;
                if (last || !retryable.test(e)) {
                    throw e;
                }
                long exp = base.toMillis() * (1L << Math.min(attempt, 20));
                long ceiling = Math.min(cap.toMillis(), exp);
                Thread.sleep(ThreadLocalRandom.current().nextLong(ceiling + 1));
            }
        }
    }
}
```

### Idempotency

An operation is idempotent if performing it twice has the same effect as performing it
once. GET, PUT and DELETE are defined as idempotent in HTTP semantics. POST is not. Because
the client cannot tell "the request was lost" from "the response was lost", any retried
non-idempotent operation (charging a card, creating an order) needs an **idempotency key**:

1. The client generates a unique key (UUID) per logical operation and sends it, for example
   in an `Idempotency-Key` header. Stripe popularised this. An IETF draft standardises the
   header.
2. The server atomically records the key before doing the work. A duplicate request with
   the same key returns the stored response instead of repeating the work.
3. If the same key arrives with a *different* request body, reject it, because this is a
   client bug.

```sql
CREATE TABLE idempotency_keys (
    client_id      BIGINT       NOT NULL,
    idem_key       VARCHAR(64)  NOT NULL,
    request_hash   CHAR(64)     NOT NULL,
    status         VARCHAR(16)  NOT NULL,
    response_code  INT,
    response_body  TEXT,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, idem_key)
);

-- Claim the key; zero rows inserted means it was seen before.
INSERT INTO idempotency_keys (client_id, idem_key, request_hash, status)
VALUES ($1, $2, $3, 'IN_PROGRESS')
ON CONFLICT (client_id, idem_key) DO NOTHING;
```

```java
import java.util.Optional;

public sealed interface IdempotencyDecision {
    record Proceed() implements IdempotencyDecision {}
    record Replay(int status, String body) implements IdempotencyDecision {}
    record StillRunning() implements IdempotencyDecision {}
    record KeyReused() implements IdempotencyDecision {}
}

record StoredRequest(String requestHash, String status, Integer responseCode, String responseBody) {}

interface IdempotencyStore {
    boolean tryClaim(long clientId, String key, String requestHash);
    Optional<StoredRequest> find(long clientId, String key);
    void complete(long clientId, String key, int status, String body);
}

final class IdempotencyGuard {
    private final IdempotencyStore store;

    IdempotencyGuard(IdempotencyStore store) {
        this.store = store;
    }

    IdempotencyDecision begin(long clientId, String key, String requestHash) {
        if (store.tryClaim(clientId, key, requestHash)) {
            return new IdempotencyDecision.Proceed();
        }
        StoredRequest seen = store.find(clientId, key).orElseThrow();
        if (!seen.requestHash().equals(requestHash)) {
            return new IdempotencyDecision.KeyReused();
        }
        return switch (seen.status()) {
            case "DONE" -> new IdempotencyDecision.Replay(seen.responseCode(), seen.responseBody());
            default -> new IdempotencyDecision.StillRunning();
        };
    }
}
```

Map `KeyReused` to HTTP 422 and `StillRunning` to 409 so the client retries later. Expire
keys after a window (24 hours is common). Ideally, do the business write and the
`complete` update in the same database transaction. Message consumers need the same idea:
with at-least-once delivery, deduplicate by message id or make the handler naturally
idempotent (upserts, conditional writes).

### Circuit breakers and bulkheads

A **circuit breaker** stops calling a dependency that is clearly failing, so the caller
fails fast instead of waiting on timeouts, and the dependency gets room to recover.

```text
           failure rate > threshold
   ┌────────┐ ───────────────────────► ┌────────┐
   │ CLOSED │                          │  OPEN  │  fail fast for a cool-down period
   └────────┘ ◄──────┐                 └────────┘
        ▲            │ trial calls          │ cool-down elapsed
        │            │ succeed              ▼
        │            │               ┌───────────┐
        │            └────────────── │ HALF-OPEN │  allow a few trial calls
        │                            └───────────┘
        └──── (trial calls fail → back to OPEN)
```

A **bulkhead** isolates resources per dependency (separate thread pools, connection pools
or semaphores), so a slow recommendation service cannot consume the threads needed for
checkout. Libraries such as Resilience4j implement both patterns. Service meshes (Envoy,
Istio) provide similar controls at the proxy level.

### Backpressure

When a consumer is slower than a producer, the excess must go somewhere. Unbounded queues
just turn overload into memory exhaustion and huge latency. Instead:

- Use **bounded queues** and reject or block when they are full.
- Use protocols with flow control (TCP windows, HTTP/2 flow control, gRPC streaming,
  Reactive Streams `request(n)`, Kafka consumers pulling at their own pace).
- Propagate the signal upstream: return 429 or 503 with `Retry-After` rather than silently
  queueing.

### Graceful degradation

Serve a reduced experience instead of an error: show cached or stale data, hide the
"recommended for you" panel, disable expensive features, or switch search to a simpler
ranking. Design this deliberately by marking each dependency as critical or optional, and
make every call to an optional dependency able to fail without failing the page. Feature
flags make degradation switchable at runtime.

### Load shedding

When load exceeds capacity, it is better to serve 80% of requests well than 100% badly.
Shedding means rejecting excess work early and cheaply:

- **Concurrency limits** per instance: reject when in-flight requests exceed a limit (from
  Little's law). Adaptive limiters such as Netflix's concurrency-limits library or Envoy's
  adaptive concurrency filter adjust the limit from observed latency.
- **Priority:** shed background, batch and prefetch traffic before user-facing traffic, and
  anonymous before logged-in users.
- **Deadline-aware:** drop requests that have waited in a queue longer than the client will
  wait. Work done for a client that has already timed out is pure waste.

```java
import java.util.concurrent.Callable;
import java.util.concurrent.Semaphore;
import java.util.function.Supplier;

public final class ConcurrencyLimiter {
    private final Semaphore permits;

    public ConcurrencyLimiter(int maxInFlight) {
        this.permits = new Semaphore(maxInFlight);
    }

    public <T> T execute(Callable<T> work, Supplier<T> onRejected) throws Exception {
        if (!permits.tryAcquire()) {
            return onRejected.get();
        }
        try {
            return work.call();
        } finally {
            permits.release();
        }
    }
}
```

`onRejected` typically returns a 503 with `Retry-After`. Rejection costs microseconds,
while queueing would cost seconds and memory.

---

## Observability basics

You cannot operate what you cannot see. Observability is the ability to answer new
questions about system behaviour from its outputs.

| Signal | What it is | Good for | Cost and caveats |
| --- | --- | --- | --- |
| Metrics | Numeric time series (counters, gauges, histograms) with labels | Dashboards, alerting, trends, SLOs | Cheap per data point; high-cardinality labels (user id) explode cost |
| Logs | Timestamped events, ideally structured JSON with request id | Debugging specific requests, audit | Expensive at volume; sample debug logs |
| Traces | A request's path across services as a tree of spans | Finding which hop is slow, fan-out analysis | Needs context propagation (W3C `traceparent`); usually sampled |
| Profiles | Continuous CPU and memory profiling | Finding hot code, cost optimisation | Low overhead with sampling profilers |

**OpenTelemetry** is the vendor-neutral standard for instrumenting all three main signals.

What to measure:

- **Four golden signals** (Google SRE): latency, traffic, errors, saturation.
- **RED** for request-driven services: rate, errors, duration.
- **USE** for resources: utilisation, saturation, errors (CPU, disks, pools, queues).
- Business metrics: orders per minute, sign-ups. These often detect outages that technical
  metrics miss.

Alerting:

- Page on **symptoms that users feel** (SLO burn), not on causes ("CPU 80%").
- Use **burn-rate alerts**: for a 30-day 99.9% SLO, burning budget at 14.4x the sustainable
  rate for 1 hour consumes 2% of the monthly budget (14.4 × 1 h / 720 h = 0.02). That is a
  reasonable page threshold. Slower burns open tickets instead. The SRE Workbook describes
  multi-window, multi-burn-rate alerting.
- Every alert should be actionable and link to a runbook.

Always propagate a **request id / trace id** from the edge through every service, log line
and message, so one user complaint can be traced end to end.

---

## Security basics

| Area | Practice |
| --- | --- |
| Transport | TLS everywhere, including internal traffic. Use mTLS between services (often via a service mesh). HSTS on public sites. |
| Authentication (who are you) | Use OIDC / OAuth 2.0 with a proven identity provider; never roll your own password crypto. Hash passwords with Argon2id, scrypt or bcrypt. Offer MFA and passkeys. |
| Sessions and tokens | Short-lived access tokens; refresh tokens rotated and revocable; cookies `HttpOnly`, `Secure`, `SameSite`. Validate JWT signature, `exp`, `aud` and `iss`, and pin the expected algorithm. |
| Authorisation (what may you do) | Enforce in the service that owns the data, on every request; RBAC or ABAC; check object ownership to prevent IDOR ("can user 42 read order 9001?"). |
| Service identity | Workload identity (SPIFFE, cloud IAM roles) instead of shared static credentials. |
| Secrets | Store in a secrets manager (HashiCorp Vault, AWS Secrets Manager, GCP Secret Manager); never in git, images or logs; rotate automatically; least-privilege access. |
| Encryption at rest | Disk/DB encryption with keys in a KMS; envelope encryption (data keys encrypted by a master key) for application-level encryption of sensitive fields. |
| Input handling | Parameterised queries (SQL injection), output encoding (XSS), size limits, schema validation, SSRF protection when fetching user-supplied URLs. |
| Edge protection | WAF, DDoS protection at CDN/anycast edge, rate limiting per IP/user/API key, bot detection. |
| Audit and privacy | Immutable audit logs for sensitive actions; data minimisation; PII classification; deletion workflows; data residency. |

The OWASP Top 10 is the standard checklist for web application risks. Defence in depth
means assuming any single layer can fail: the gateway checks the token, and the service
still checks authorisation.

---

## Reference architecture for a typical web application

```text
   ┌─────────────────┐            ┌──────────────────────┐
   │ Clients         │───────────►│ DNS / GSLB (anycast) │
   │ web, mobile     │            └──────────────────────┘
   └────────┬────────┘
            │ HTTPS
            ▼
   ┌──────────────────────────────────────────────────────┐
   │ CDN edge: static assets, TLS termination, WAF, DDoS  │
   └──────────────────────────┬───────────────────────────┘
                              │ cache miss / dynamic requests
                              ▼
   ┌──────────────────────────────────────────────────────┐
   │ Regional L7 load balancer / API gateway              │
   │ authn, rate limits, routing, request ids             │
   └────────┬─────────────────┬─────────────────┬─────────┘
            ▼                 ▼                 ▼
   ┌────────────────┐ ┌────────────────┐ ┌────────────────┐
   │ Service A      │ │ Service B      │ │ Service C      │  stateless,
   │ N instances    │ │ N instances    │ │ N instances    │  autoscaled,
   └───────┬────────┘ └───────┬────────┘ └───────┬────────┘  across 3 AZs
           └──────────────────┼──────────────────┘
        ┌──────────────┬──────┴───────┬──────────────┐
        ▼              ▼              ▼              ▼
   ┌──────────┐  ┌───────────┐  ┌───────────┐  ┌───────────┐
   │ Redis    │  │ SQL       │  │ Kafka /   │  │ Object    │
   │ cache    │  │ primary + │  │ queue     │  │ storage   │
   │ cluster  │  │ replicas  │  │           │  │ (S3/GCS)  │
   └──────────┘  └───────────┘  └─────┬─────┘  └───────────┘
                                      ▼
                               ┌─────────────┐
                               │ Async       │──► search index, email,
                               │ workers     │    analytics warehouse
                               └─────────────┘

   Cross-cutting: metrics, logs, traces (OpenTelemetry) · secrets manager · CI/CD
```

Walking a request through it:

1. DNS (or anycast) sends the user to the nearest edge PoP. TLS terminates there.
2. Static assets are served from the edge cache. Dynamic requests travel over the CDN
   backbone to the nearest healthy region.
3. The regional L7 LB / gateway authenticates, rate-limits, attaches a request id and routes
   by path to a service.
4. Stateless services read through Redis (cache-aside) and fall back to the SQL primary or
   replicas. Writes go to the primary.
5. Slow or non-critical work (emails, search indexing, analytics) is published to Kafka or
   a queue and processed asynchronously by workers.
6. Large blobs go directly to object storage, often via pre-signed upload URLs so they never
   pass through the services.

How it evolves: start with one region, multi-AZ, a single SQL primary with replicas, and a
cache. Then add read replicas and caching for read scale, queues to decouple slow work,
sharding when a single primary's write throughput or storage runs out, and multi-region
when latency or disaster-recovery requirements demand it.

---

## Common mistakes

- Designing before clarifying requirements, or never stating non-functional targets.
- Producing estimates that never influence a decision.
- Adding microservices, Kafka and Kubernetes "because scale" when one service and one
  database would serve the stated load.
- Drawing a cache without specifying the key, TTL, invalidation, and what happens on a miss
  storm.
- Treating averages as latency targets. Use p99, and remember fan-out amplifies the tail.
- Calls without timeouts, retries without backoff or jitter, retries at every layer, and
  retrying non-idempotent operations.
- Deep health checks that take the whole fleet out when a shared dependency blips.
- Assuming replicas fail independently. Correlated failures (config, deploys, AZ, region)
  dominate real outages.
- Untested failover and never-restored backups.
- Sticky sessions or local disk state that block scaling and draining.
- Ignoring the cost dimension. Cross-AZ and cross-region data transfer, and CDN egress,
  are often major line items.

---

## Interview questions

**1. How do you decide between vertical and horizontal scaling?**
Scale vertically while it is cheap and sufficient, because it is simpler and keeps strong
consistency. Move stateless tiers to horizontal scaling early, since it costs little and
also gives redundancy. For databases, exhaust vertical scale, read replicas and caching
before sharding, because sharding adds permanent complexity (cross-shard queries,
rebalancing, hot shards).

**2. A request fans out to 50 backends, each with p99 = 200 ms. What is the user-facing tail
and how do you improve it?**
The chance that at least one backend is slower than its p99 is 1 − 0.99^50 ≈ 39%, so
roughly 39% of user requests take at least 200 ms. To improve it: hedged requests after
the p95 delay, reduce fan-out (precompute, coarser partitions), return partial results
after a deadline, and fix the backend tail itself (GC, noisy neighbours, compaction).

**3. L4 vs L7 load balancing: when would you use each?**
L4 for raw throughput, non-HTTP protocols, TLS passthrough, and as the first tier in front
of proxies. L7 when you need path or host routing, TLS termination, per-request balancing
(essential for HTTP/2 and gRPC, where one connection carries many requests), retries, auth
and observability. Large systems use L4 in front of an L7 fleet.

**4. Why is "least connections" sometimes worse than "power of two choices"?**
With many independent balancers, each one has a stale view and they all pick the same
least-loaded backend at once (herding), overloading it. Power of two choices adds
randomness, so balancers spread out, while still avoiding the most loaded backends. It
achieves near-optimal balance with only local information.

**5. Estimate the servers needed for 50,000 peak QPS where each request uses 20 ms of CPU on
8-core machines.**
One machine gives 8 / 0.02 = 400 requests/s at 100% CPU, or about 200 at a 50% target.
50,000 / 200 = 250 servers. Across 3 AZs, survive the loss of one by sizing for 250 × 1.5 =
375, about 125 per AZ. Validate with a load test, and check concurrency with Little's law.

**6. What are RPO and RTO, and how do they shape the architecture?**
RPO is the maximum acceptable data loss (in time), and RTO is the maximum acceptable
downtime. An RPO near zero requires synchronous replication to another failure domain.
Minutes allow asynchronous replication, and hours allow backups. An RTO of minutes requires
a warm standby or active-active with automated failover, while hours allow restore from
backup. Cost rises steeply as both approach zero.

**7. Your health check queries the database. What can go wrong?**
A brief database outage makes every instance fail its check at the same time, so the LB
removes the whole fleet and turns a partial degradation into a full outage. Keep traffic
health checks shallow, use passive outlier detection for real errors, and report deep
dependency health separately for alerting.

**8. How do you make retries safe?**
Retry only transient errors and idempotent operations. Use exponential backoff with full
jitter, cap the attempts, respect the overall deadline, retry at a single layer, enforce a
retry budget, and use idempotency keys for operations like payments so a duplicate is
detected and the original response is replayed.

**9. Explain the TLS and connection setup cost for a user far from your servers, and how to
reduce it.**
A new HTTPS connection over TCP with TLS 1.3 needs about 3 round trips before the first
response byte (4 with TLS 1.2). At 200 ms RTT that is 600–800 ms. Reduce it with edge TLS
termination close to users (CDN or anycast), TLS 1.3, session resumption, HTTP/3 (2 RTT,
or 1 with 0-RTT for idempotent requests), and connection reuse via keep-alive and HTTP/2.

**10. How would you design DNS-based failover between two regions, and what are its limits?**
Use health-checked latency or failover routing at the authoritative DNS with a short TTL
(30–60 s). Limits: resolvers and clients may cache beyond the TTL, failover takes at least
detection time plus TTL, and DNS locates the resolver, not the user. For faster and more
deterministic failover, use anycast or a global L7 load balancer in front of the regions.

**11. When does adding a cache make things worse?**
When the hit rate is low (random access over a huge key space), when consistency
requirements make invalidation complex and bugs serve stale data, when a cold cache or
mass expiry causes a stampede that overloads the database, or when the system silently
depends on the cache for capacity and cannot survive its loss. Mitigations: request
coalescing, TTL jitter, warm-up, and capacity planning for cache failure.

**12. Two replicas at 99.9% each. What is the combined availability, and why might reality
be worse?**
If failures are independent, 1 − 0.001² = 99.9999%. In reality, replicas share failure
modes: the same deploy, config, AZ, dependency or bug. Failover itself takes time and can
fail. True availability is bounded by correlated failures and failover quality, not by
the formula.

**13. What is load shedding and how is it different from rate limiting?**
Rate limiting enforces a per-client policy ("100 requests/min per API key") regardless of
server health. Load shedding protects the server itself: when in-flight work exceeds
capacity, it rejects excess requests early and cheaply, ideally lowest priority first, so
the requests it does accept complete within their deadlines.

---

## Summary

- Run the interview as a structured conversation: requirements, estimates, API, data model,
  high-level design, deep dives, then trade-offs and evolution. Drive it, and keep to time.
- State non-functional targets precisely: SLIs and SLOs, p99 latency, peak QPS, durability,
  consistency per operation, availability, RPO and RTO.
- Estimate to make decisions: 1M/day ≈ 12/s, 86,400 s ≈ 10^5, 1 Gbps = 125 MB/s. Remember
  peak, replication and growth.
- Latency orders of magnitude: memory ~100 ns, SSD ~16 µs, same-DC RTT ~0.5 ms, cross-region
  ~60–250 ms. Count round trips on the critical path.
- Keep services stateless. Externalise sessions, files and jobs.
- Load balancing: L4 for speed, L7 for per-request smarts. Power of two choices for
  distributed balancers, consistent hashing for affinity. Shallow health checks, connection
  draining, and redundant balancers.
- Edge: anycast or GeoDNS, CDN with good cache keys, content-hashed assets,
  `stale-while-revalidate`, and edge TLS termination to cut handshake RTTs.
- Availability: series multiplies down, parallel multiplies up, and correlated failures
  break the math. Stateful failover dominates downtime.
- Reliability toolkit: timeouts and deadlines, jittered and budgeted retries, idempotency
  keys, circuit breakers, bulkheads, backpressure, graceful degradation, load shedding.
- Observe with metrics, logs and traces via OpenTelemetry. Alert on SLO burn, not on causes.
- Secure by default: TLS and mTLS, OIDC, authorisation in the owning service, a secrets
  manager, and least privilege.

---

## Further reading

- Martin Kleppmann, *Designing Data-Intensive Applications* (O'Reilly). The best single
  book on the data side of system design. A second edition co-authored with Chris Riccomini
  is in progress; check its status.
- Betsy Beyer, Chris Jones, Jennifer Petoff, Niall Richard Murphy (eds.), *Site Reliability
  Engineering: How Google Runs Production Systems* (O'Reilly, 2016), and the companion *The
  Site Reliability Workbook*. Both are free to read at https://sre.google/books/.
- Alex Xu, *System Design Interview – An Insider's Guide*, Volume 1; and Alex Xu and Sahn
  Lam, Volume 2.
- Jeffrey Dean and Luiz André Barroso, "The Tail at Scale", *Communications of the ACM*,
  2013.
- Michael Mitzenmacher, "The Power of Two Choices in Randomized Load Balancing", *IEEE
  Transactions on Parallel and Distributed Systems*, 2001.
- David Karger et al., "Consistent Hashing and Random Trees", STOC 1997.
- Daniel E. Eisenbud et al., "Maglev: A Fast and Reliable Software Network Load Balancer",
  NSDI 2016.
- Marc Brooker, "Exponential Backoff And Jitter", AWS Architecture Blog, 2015.
- RFC 9110 (HTTP Semantics), RFC 9111 (HTTP Caching), RFC 9112 (HTTP/1.1), RFC 9113
  (HTTP/2), RFC 9114 (HTTP/3), RFC 9000 (QUIC), RFC 8446 (TLS 1.3), RFC 5861 (Cache-Control
  extensions for stale content).
- AWS whitepaper, *Disaster Recovery of Workloads on AWS: Recovery in the Cloud*, and the
  Reliability Pillar of the AWS Well-Architected Framework.
- OWASP Top 10 (owasp.org).
