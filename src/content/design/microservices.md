---
title: "Microservices"
order: 10
---
# Microservices

**Independent deployable services**, each owning a business capability and its data, talking over the network. The goal is **independent change** (deploy, scale, fail in isolation), not small binaries. You pay with partial failure, latency, eventual consistency, distributed debugging, and platform cost.

---

## Monolith vs modular monolith vs microservices

```text
 Monolith              Modular monolith           Microservices
 ┌─────────────┐       ┌─────────────┐            ┌──────┐  ┌──────┐
 │ one deploy  │       │ one deploy  │            │Orders│  │ Pay  │
 │ any→any     │       │ modules +   │            └──┬───┘  └──┬───┘
 └──────┬──────┘       │ enforced    │               │         │
        │              │ boundaries  │            ┌──▼──┐   ┌──▼──┐
   ┌────▼────┐         └──────┬──────┘            │ DB  │   │ DB  │
   │   DB    │                │                   └─────┘   └─────┘
   └─────────┘           one DB (schemas per module)
```

| | Monolith | Modular monolith | Microservices |
| --- | --- | --- | --- |
| Deploy | One unit | One unit | Per service |
| Boundaries | Conventions (erode) | Build/tooling enforced | Network enforced |
| Calls | In-process | In-process | Network (ms, can fail) |
| Transactions | ACID global | ACID (discourage cross-module) | Local only; sagas |
| Scale | Whole app | Whole app | Per service |
| Ops cost | Low | Low | High (platform, CI, observability) |
| Refactoring | Easy | Easy | Hard (contracts, migrations) |

**Buys:** independent deploys, per-service scaling/resources, fault isolation (if you build resilience), team autonomy.

**Costs:** every call can fail or slow; no cross-service ACID; platform team; wrong boundaries are expensive to move.

**Real-world:** Amazon/Netflix scaled with services; Shopify runs a **modular monolith**; Segment consolidated 100+ services back (2018); Prime Video cut cost ~90% by collapsing one pipeline (2023).

---

## When to split vs stay monolith

**Default:** well-structured **modular monolith** (packages/modules, ArchUnit, Spring Modulith). Extract when you have a **concrete** reason:

| Split when | Stay monolith when |
| --- | --- |
| Teams blocked on each other's releases | Small team, unclear domain |
| Very different scale / SLA / resources per part | Boundaries still moving |
| Fault or compliance isolation (PCI, risky integration) | "Modern architecture" / resume-driven |
| Boundary is **stable** after running as a module | Tangled modules → distributed tangle |

**Bad reasons:** fashion, team of five, hoping microservices fix spaghetti.

**Migration:** **Strangler fig** — facade routes slices to new services; data via API → ownership → CDC; dark launch / parallel run; anticorruption layer; delete old path when done.

```text
 Step 1:  clients → facade → monolith (everything)
 Step 2:  facade → /orders/* → orders-service; rest → monolith
 Step 3:  repeat until monolith empty or kept as small legacy
```

Pick first slice: decoupled, valuable, not the riskiest path (notifications, search). Data migration is hardest: read via monolith API first, then cut ownership, CDC during transition, delete old tables when safe.

---

## Decomposition (boundaries)

- **Business capabilities** (catalogue, pricing, orders, payments) — stable; avoid layer splits ("validation service").
- **DDD:** bounded context = consistent model/language; **aggregate** = largest atomic unit — never split across services; reference other aggregates by ID only.
- **Context mapping:** partnership, customer/supplier, **anticorruption layer** (legacy), open host, conformist, shared kernel (keep tiny).
- **Heuristics:** one service ≈ one bounded context; one team owns end-to-end; if two services always deploy together → merge; avoid **entity services** (CRUD tables, logic elsewhere).

**Aggregate rules (microservices-relevant):**

- Invariants enforced inside one aggregate per transaction.
- One transaction modifies one aggregate.
- Aggregates reference each other by **ID only** — never join across services in one TX.
- Domain events (`OrderPlaced`) replace direct calls to other services; outbox makes publish reliable.

---

## Communication

### Sync vs async

| | Synchronous (REST/gRPC) | Asynchronous (events/messages) |
| --- | --- | --- |
| Caller | Waits for response | Publishes and continues |
| Coupling | Temporal (chain availability multiplies) | Loose in time |
| Consistency | Immediate for the read | Eventual |
| Good for | User waiting; query now | Notify; decouple; absorb spikes |
| Risks | Latency stacks; fan-out tail latency | At-least-once; ordering per partition; schema evolution |

**Commands** → one consumer acts. **Events** → fact happened; many consumers may react.

**Event payload styles:**

| Style | Example | Trade-off |
| --- | --- | --- |
| Notification | `{ "orderId": "o1" }` | Small; consumer calls back (couples to producer uptime) |
| Event-carried state | Full line items in event | Autonomous consumers; larger payloads; schema evolution |
| Event sourcing | Events are the log of record | Audit/rebuild; high complexity |

Design for **at-least-once** (idempotent handlers), ordering **within a partition** (key by aggregate ID), schema registry + compatibility rules, DLQ, consumer lag alerts.

**Sync availability math:** if A→B→C each 99.9% available, serial chain ≈ 99.7%. Fan-out to 100 services with 1% slow each → ~63% of requests hit at least one slow dependency. Prefer parallel calls with deadlines, caching, and async where possible.

**Request/reply over messaging:** correlation ID + reply-to queue — decouples slow responders; must handle missing replies (timeout) and late replies.

| Need | Prefer |
| --- | --- |
| User waits for another service's data | Sync + timeout/fallback, or local replica from events |
| Something happened; others react | Async events |
| Multi-step business process with undo | Saga (orchestrated or choreographed) |
| High-volume propagation | Event streaming (Kafka) |
| Spike smoothing | Queue |

**Choreography:** each service reacts to events — implicit flow. **Orchestration:** coordinator drives steps — explicit, testable; use for real business processes with compensation.

**Gateway / BFF:** single entry (TLS, auth, rate limit, routing). **BFF per client** aggregates calls into screen-shaped responses; keep business logic out of the gateway.

```text
 mobile ──► Mobile BFF ──┐
 web    ──► Web BFF    ──┼──► orders, catalog, payments (internal)
 partners ─► API gateway ┘
```

Pitfalls: gateway as shared monolith; one BFF for all clients; single point of failure without HA replicas.

**Idempotency:** POST with `Idempotency-Key` at edge; saga steps keyed by saga ID; message consumers dedupe by event ID — all required for at-least-once messaging.

**Service discovery (K8s sketch):** Service + EndpointSlices + DNS (`name.namespace.svc.cluster.local`); kube-proxy balances; headless Service for gRPC client-side LB.

| | Client-side discovery | Server-side discovery |
| --- | --- | --- |
| How | Client queries registry, picks instance | Client calls stable name; LB/proxy routes |
| Examples | Eureka, Consul, gRPC + DNS | K8s Service, cloud ALB, mesh |
| Trade-off | Smarter balancing, no extra hop | Simpler clients, extra hop |

Registration: self-register with heartbeats vs platform registers (K8s from pod readiness).

---

## Data

**Database per service** — no other service reads/writes your store; access via API or events. Shared DB = worst of both worlds (coordinated deploys, no owner of invariants).

### Saga (brief)

Sequence of **local transactions**, each with a **compensating** undo if a later step fails. Replaces 2PC (blocks, couples availability).

Example order flow:

| Step | Service | Action | Compensation |
| --- | --- | --- | --- |
| 1 | Orders | Create `PENDING` | Mark `REJECTED` |
| 2 | Inventory | Reserve stock | Release reservation |
| 3 | Payments | Authorize (**pivot**) | Void authorization |
| 4 | Shipping | Create shipment | Retriable until success |
| 5 | Orders | Mark `CONFIRMED` | — |

Fail after pivot → compensations (release stock, void auth), not full rollback.

- **No isolation** — others see intermediate state; use semantic locks (`PENDING`), order risky step as pivot.
- Compensations are **business** (refund, email), not DB rollback; some steps irreversible → put last.
- Orchestrator **persists progress**; steps **idempotent** (saga ID as key). Workflow engines (Temporal, Camunda) for many sagas.

### Transactional outbox (brief)

**Dual-write problem:** commit DB + publish event — crash between = lost or phantom events.

**Fix:** insert event row in **same transaction** as state change; relay (poller + `SKIP LOCKED`, or CDC/Debezium) publishes to broker.

```text
  BEGIN;
    UPDATE orders SET status = 'PLACED' WHERE id = ?;
    INSERT INTO outbox (id, aggregate_id, event_type, payload) VALUES (...);
  COMMIT;
  relay → Kafka (key = aggregate_id) → consumers (dedupe by event id)
```

- **At-least-once** delivery → consumers **dedupe** by event ID (inbox table).
- Key messages by aggregate ID for ordering; include sequence if strict order needed.

**CQRS (one line):** separate write model from read models updated by events — answers cross-service queries without chatty sync calls; eventual consistency on reads.

**API composition:** BFF calls N services in parallel and joins — fine for small joins; slowest call + product of availability limits you.

---

## Resilience

Dominant failure: **cascade** — slow dependency ties threads → callers slow → everyone down.

| Pattern | What | Key rules |
| --- | --- | --- |
| **Timeouts** | Cap every network call + pool acquire | From callee p99; propagate **deadline** (gRPC `grpc-timeout`, HTTP header); drop expired work |
| **Retries** | Transient failures only | Idempotent or idempotency key; 503/429/connect; **one layer**; exp backoff + jitter; max 2–3; **retry budget**; never retry 4xx business errors |
| **Circuit breaker** | Fail fast when dependency unhealthy | Closed → open (threshold on failures/slow calls) → half-open trials; per dependency; ignore business errors |
| **Bulkhead** | Limit concurrency per dependency | Semaphore / dedicated pool; separate pools critical vs batch; with virtual threads, limit concurrency not thread count |
| **Rate limiting** | Protect self and partners | Gateway + client-side for external APIs |
| **Fallback** | Degrade gracefully | Stale cache, hide widget, queue charge — never "fallback" to approve payment |
| **Load shedding** | Reject early when overloaded | 503 + `Retry-After`; prioritize health/critical paths |
| **Chaos** | Prove the above works | Steady-state metrics; small blast radius; game days |

**Retry storm:** 3 retries × 4 hops ≈ 81× load at bottom — retry at edge only.

```text
 client deadline 1000 ms
   gateway 20 ms → orders 80 ms → inventory cap 300 ms (parallel with payments)
   server drops work if deadline already passed
```

**Circuit breaker states:** CLOSED (normal) → OPEN (fail fast) → HALF-OPEN (trial calls) → CLOSED or OPEN. Tune `minimumNumberOfCalls` so low traffic does not flap open.

Compose resilience **outside-in** at the call site: bulkhead → timeout → breaker → retry (retry outermost records each attempt in breaker — avoid double retry with mesh/gateway).

---

## Observability

You cannot attach a debugger across dozens of services.

| Method | For | Signals |
| --- | --- | --- |
| **RED** | Request-driven services | **R**ate, **E**rrors, **D**uration (histograms) |
| **USE** | Resources (CPU, disk, pools) | **U**tilization, **S**aturation, **E**rrors |
| Four golden signals | User-facing | Latency, traffic, errors, saturation |

- **Logs:** structured JSON; `trace_id` / `span_id` in MDC; no secrets; centralise; sample at high volume.
- **Tracing:** OpenTelemetry spans; W3C `traceparent`; propagate on Kafka headers; tail sampling keeps errors/slow traces.
- **SLOs:** SLI (measured ratio) → SLO (target) → **error budget**; alert on **burn rate**; dependency SLO bounds yours (serial 99.9% × 3 ≠ 99.9% without fallbacks).

| SLO (30 days) | Error budget (time) |
| --- | --- |
| 99% | ~7.2 hours |
| 99.9% | ~43 minutes |
| 99.99% | ~4.3 minutes |

While budget remains → ship features; exhausted → reliability freeze. Multi-window burn alerts reduce noise (Google SRE Workbook).

**Health:** **Liveness** = process alive (don't check DB); **Readiness** = accept traffic; **Startup** for slow boot.

---

## Deployment & platform (sketch)

- Immutable container images; non-root; pin tags (not `latest`); multi-stage builds; scan bases.

| Release | Idea | Rollback | Note |
| --- | --- | --- | --- |
| Rolling | Replace pods gradually | Gradual | Mixed versions → backward-compatible schemas |
| Blue-green | Switch traffic to new stack | Instant switch back | 2× capacity during cutover |
| Canary | 1% → 5% → 100% with metrics | Shift traffic back | Needs SLO comparison (Argo Rollouts, Flagger) |
| Feature flags | Deploy dark, enable by cohort | Flip flag | Remove flags after rollout |

- K8s: Pod, Deployment, Service, Ingress/Gateway API, HPA, PDB, ConfigMap/Secret; **requests vs limits** (CPU limit often hurts latency); liveness/readiness/startup probes; `preStop` + graceful HTTP drain; HPA on CPU or custom (KEDA for queue lag).
- **Expand-contract** DB changes across rolling deploys: add column → dual-write → backfill → switch reads → drop old.
- **GitOps:** desired state in git; Argo CD/Flux reconciles.
- **Service mesh (optional):** mTLS, L7 metrics, traffic split — cost per proxy; ambient mode (ztunnel + waypoints) reduces sidecars; don't double-retry with app retries.

| When mesh helps | When libraries enough |
| --- | --- |
| Many services, several languages | Handful of services, one stack |
| Mandatory mTLS / zero trust | NetworkPolicies + app TLS |
| Uniform canary/retry policy without redeploying apps | Resilience4j + OTel in code |

- **Zero trust:** workload identity (SPIFFE); mTLS; propagate user context safely (token exchange or internal JWT); secrets from vault/cloud SM, not git.
- **Testing:** pyramid/honeycomb — unit, **Testcontainers** component tests, **consumer-driven contracts** (Pact) per pair; few E2E journeys. Avoid giant shared E2E env blocking all deploys.

**Contract testing (brief):** consumer defines expected interactions → pact file → provider verifies in its CI → broker answers "can I deploy?". Catches breaking API changes without a shared E2E environment. Complement with schema compatibility for events (registry) and `buf breaking` for protobuf.

**Event sourcing (when not default):** store event stream as truth; state = fold; pairs naturally with CQRS; use when history is the domain (ledger, bookings) — cost: schema evolution forever, GDPR harder.

---

## Team topology

**Conway's law:** system structure mirrors org communication. **Inverse Conway:** shape teams to match target architecture. **You build it, you run it.** *Team Topologies:* stream-aligned (most), platform (paved road), enabling, complicated-subsystem; prefer **X-as-a-Service** between stream teams.

| Team type | Role |
| --- | --- |
| Stream-aligned | End-to-end business flow (checkout) |
| Platform | Internal product: CI, K8s, observability ("paved road") |
| Enabling | Coach on security/performance, then leave |
| Complicated-subsystem | Deep specialist domain (pricing engine) |

**Security snapshot:** zero trust (no "inside network = safe"); mesh or SPIRE for workload mTLS; never forward raw `X-User-Id` without gateway trust; object-level auth in each service; secrets from Vault/cloud SM with rotation.

**Configuration:** externalise (12-factor); validate at startup; version config like code — bad config push = outage; feature flags via OpenFeature.

---

## Pitfalls (anti-patterns)

| Anti-pattern | Fix |
| --- | --- |
| **Distributed monolith** (lockstep deploys, shared DB) | Redraw boundaries; merge what changes together |
| **Shared database** | DB per service; events/API; CDC for reporting |
| **Chatty sync chains** | Coarser APIs, read models, async |
| **Nano / entity services** | Capability-sized services with behavior |
| **Shared domain JAR** | Duplicate small models per context |
| **Smart ESB** | Smart endpoints, dumb pipes |
| **No platform** | Platform team, templates |
| **No observability before split** | Tracing + RED first |
| **Big-bang rewrite** | Strangler |
| **Microservices day one** | Modular monolith until stable |
| **Missing idempotency** | Duplicate charges/events under retries | Keys on commands; inbox dedupe on consume |

---

## Interview quick check

1. **When microservices?** Independent deploy/scale/isolation + understood boundaries; else modular monolith.
2. **Boundaries?** Business capabilities / bounded contexts; whole aggregates; team ownership.
3. **Consistency without 2PC?** Sagas + outbox + idempotent consumers.
4. **Outbox?** Event in same TX as state; relay publishes; at-least-once + dedupe.
5. **Circuit breaker?** Closed/open/half-open; fail fast; per dependency.
6. **Retry storms?** One layer; backoff; budgets; breakers.
7. **Cross-service query?** API composition or CQRS read model — not foreign DB reads.
8. **Liveness vs readiness?** Restart vs remove from LB.
9. **Service mesh worth it?** Many polyglot services + mTLS + uniform traffic policy vs proxy cost; small fleet → libraries (Resilience4j, OTel) + NetworkPolicies may suffice.
10. **Distributed monolith?** Shared DB, lockstep releases, sync chains — all cost, no autonomy.
11. **Trace across services?** W3C `traceparent`; OTel auto-instrumentation; logs include trace_id.

---

## Summary

Default **modular monolith**; extract with **strangler** when boundaries and teams are ready. **DB per service**; **sagas + outbox** for cross-service consistency; **sync** when the user waits, **async** to decouple. Resilience and observability are not optional: timeouts, single-layer retries, breakers, RED/USE, traces, SLO burn alerts. Align teams with services (**Conway**); platform team pays down ops toil.

---

## Further reading

Sam Newman, *Building Microservices*; Chris Richardson, *Microservices Patterns*; Michael Nygard, *Release It!*; Eric Evans, *DDD*; Fowler "MonolithFirst" / Strangler; Skelton & Pais, *Team Topologies*; Google SRE books (SLOs, overload); Kleppmann, *DDIA*; OpenTelemetry docs.
