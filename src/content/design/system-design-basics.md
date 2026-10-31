---
title: "System design basics"
order: 3
---
# System Design Basics

System design is deciding how the pieces of a large application — servers, databases, caches, queues — fit together so it stays **fast, available, and correct** as traffic grows.

## The usual interview / design flow

1. **Clarify requirements.** Functional (what it does) and non-functional (scale, latency, availability, consistency).
2. **Estimate load.** Users, requests per second, storage, bandwidth.
3. **Define the API.** Endpoints and their inputs and outputs.
4. **Model the data.** Entities, relationships, access patterns.
5. **High-level design.** Boxes and arrows: clients, load balancer, services, stores.
6. **Deep dive.** Bottlenecks, scaling, failure handling.
7. **Trade-offs.** What you gave up and why.

---

## Scalability

**Vertical scaling (scale up):** a bigger machine — more CPU, RAM, faster disk.

- Simple; no code changes.
- Hard ceiling, expensive at the top, single point of failure.

**Horizontal scaling (scale out):** more machines behind a load balancer.

- Practically unlimited, cheaper commodity hardware, survives machine loss.
- Requires stateless services, data partitioning, and dealing with network failures.

```text
Vertical                     Horizontal

 ┌──────────────┐              ┌────────────────┐
 │   BIG BOX    │              │ Load Balancer  │
 │ 64 CPU 512GB │              └───┬────┬────┬──┘
 └──────────────┘                  │    │    │
                                 ┌─▼┐ ┌─▼┐ ┌─▼┐
                                 │S1│ │S2│ │S3│
                                 └──┘ └──┘ └──┘
```

### Stateless services

To scale out, any server must be able to handle any request. Move session state **out** of the app server:

- Sessions into Redis or a database.
- Auth into signed tokens (JWT) that the client sends each time.
- Uploaded files into object storage (S3), not local disk.

---

## Load balancing

A load balancer spreads incoming requests across healthy servers.

| Algorithm | How it picks | Good for |
| --- | --- | --- |
| Round robin | Next server in order | Identical servers, similar requests |
| Weighted round robin | Proportional to weight | Mixed server sizes |
| Least connections | Fewest open connections | Long-lived or uneven requests |
| IP hash | Hash of client IP | Sticky sessions without shared state |
| Consistent hashing | Hash ring | Caches and shards (minimal reshuffle) |

**Layer 4 vs Layer 7**

- **L4 (transport):** routes by IP and port. Very fast, no knowledge of HTTP.
- **L7 (application):** reads HTTP — can route `/api/*` and `/static/*` differently, terminate TLS, add headers. Examples: Nginx, HAProxy, AWS ALB, Envoy.

**Health checks** remove dead servers automatically. Run at least two load balancers (active-passive with a floating IP, or DNS across several) so the balancer is not itself a single point of failure.

---

## Latency numbers to know

| Operation | Approx. time |
| --- | --- |
| L1 cache reference | 1 ns |
| Main memory reference | 100 ns |
| Compress 1 KB (fast codec) | 2 µs |
| Read 1 MB sequentially from memory | 3 µs |
| SSD random read | 16 µs |
| Read 1 MB sequentially from SSD | 50 µs |
| Round trip within same datacenter | 500 µs |
| Disk (HDD) seek | 2 ms |
| Read 1 MB sequentially from HDD | 1 ms |
| Round trip India ↔ US | 150–250 ms |

Takeaways: memory is ~100× faster than SSD; network hops inside a datacenter are cheap compared to cross-continent ones; avoid disk seeks on hot paths.

---

## Back-of-the-envelope estimation

Handy constants:

- 1 day ≈ 86,400 s ≈ **10^5 s**.
- 1 million requests/day ≈ **12 req/s**.
- Peak traffic ≈ 2–3× average.

### Example: a photo-sharing app

- 10 M daily active users, each uploads 2 photos/day, views 50/day.
- Writes: 20 M/day ÷ 10^5 ≈ **200 uploads/s** (peak ~600/s).
- Reads: 500 M/day ÷ 10^5 ≈ **5,000 views/s** (peak ~15,000/s) — read-heavy, so cache aggressively and use a CDN.
- Storage: 20 M × 500 KB = **10 TB/day**, about **3.6 PB/year** — object storage, not a database.

---

## Availability

Availability is the fraction of time a system works.

| Availability | Downtime per year | Downtime per month |
| --- | --- | --- |
| 99% ("two nines") | 3.65 days | 7.3 hours |
| 99.9% | 8.8 hours | 43.8 minutes |
| 99.99% | 52.6 minutes | 4.4 minutes |
| 99.999% | 5.3 minutes | 26 seconds |

**Components in series** multiply: two 99.9% services both required → 99.9% × 99.9% ≈ 99.8%.

**Components in parallel** (redundant) improve it: two 99.9% replicas, either one enough → 1 − (0.001 × 0.001) = 99.9999%.

Ways to raise availability:

- Remove single points of failure (redundant servers, LBs, DB replicas).
- Spread across availability zones and regions.
- Health checks plus automatic failover.
- Graceful degradation — serve stale cache or a reduced feature set instead of an error.

---

## CDN (Content Delivery Network)

A CDN caches static content (images, JS, CSS, video) on edge servers close to users.

- **Pull CDN:** edge fetches from your origin on the first miss, then caches. Easy to set up.
- **Push CDN:** you upload content to the CDN ahead of time. Good for large, rarely changing files.

Benefits: lower latency, less load on origin, DDoS absorption. Control freshness with `Cache-Control` headers and versioned file names such as `app.3f9a1c.js`.

---

## Proxies

- **Forward proxy** sits in front of *clients* (corporate proxy, VPN) — the server sees the proxy's IP.
- **Reverse proxy** sits in front of *servers* (Nginx) — the client sees one address; the proxy handles TLS, compression, caching, and routing to backends.

```text
Forward:  client ──► proxy ──► internet ──► server
Reverse:  client ──► internet ──► proxy ──► server pool
```
