---
title: "Microservices"
order: 10
---
# Microservices

A microservice architecture structures one application as a set of **independently deployable
services**, each owning a business capability and its own data, communicating over the network.
The point is not small code size; it is **independent change**: one team can change, test,
deploy, and scale its service without coordinating with every other team.

That independence is bought with distributed-systems problems: partial failure, network latency,
eventual consistency, harder debugging, and a large operational platform. This page covers when
the trade is worth it, how to draw service boundaries, how services communicate and manage data,
resilience, observability, deployment on Kubernetes, service meshes, security, testing, team
structure, and the anti-patterns that make microservices worse than the monolith they replaced.

---

## Monolith vs modular monolith vs microservices

```text
 Monolith                  Modular monolith              Microservices

 ┌───────────────────┐     ┌───────────────────┐         ┌────────┐  ┌────────┐
 │ one codebase      │     │ one deployable    │         │ Orders │  │Payments│
 │ one deployable    │     │ modules with      │         └───┬────┘  └───┬────┘
 │ any code calls    │     │ public APIs and   │             │           │
 │ any code          │     │ enforced borders  │          ┌──▼───┐    ┌──▼───┐
 └─────────┬─────────┘     └─────────┬─────────┘          │  DB  │    │  DB  │
           │                         │                    └──────┘    └──────┘
      ┌────▼────┐          ┌─────────▼─────────┐        each deployed, scaled,
      │   DB    │          │ one DB, schema    │        and owned separately;
      └─────────┘          │ per module        │        talk over the network
                           └───────────────────┘
```

| | Monolith | Modular monolith | Microservices |
| --- | --- | --- | --- |
| Deployment | One unit | One unit | Each service independently |
| Module boundaries | Conventions, usually eroded | Enforced in the build (packages, modules, tooling) | Enforced by the network |
| Calls between parts | In-process, nanoseconds | In-process | Network: ms, can fail, can be slow |
| Transactions | ACID across everything | ACID (but discouraged across modules) | Local only; sagas and eventual consistency |
| Scaling | Whole app | Whole app | Per service |
| Failure isolation | One bug can take everything down | Same | A failing service can be isolated (if resilience is built) |
| Technology choice | One stack | One stack | Per service (use sparingly) |
| Refactoring boundaries | Easy (IDE refactor) | Easy | Hard (API contracts, data migration) |
| Debugging | Stack trace, one log | Same | Distributed tracing, correlated logs |
| Operational cost | Low | Low | High: platform, CI/CD per service, observability, on-call |
| Team scaling | Contention beyond a few teams | Better; still one release train | Many autonomous teams |

### Honest trade-offs

What microservices actually buy:

- **Independent deployability.** Teams release on their own schedule; a change to search does not
  wait for the payments release train.
- **Independent scaling and resource profiles.** The image-resizing service gets GPU nodes; the
  catalogue gets 50 replicas; the admin service gets 2.
- **Fault isolation**, if you invest in timeouts, bulkheads, and fallbacks; otherwise failures
  cascade just as well over the network.
- **Organisational scaling**: clear ownership, smaller codebases, local decision making.

What they cost:

- **Every call can fail or be slow.** Network calls are orders of magnitude slower than
  in-process calls and introduce partial failure.
- **No cross-service ACID transactions.** Consistency becomes a design problem (sagas, outbox,
  idempotency).
- **Operational overhead**: service discovery, config, secrets, CI/CD, monitoring, tracing, and
  on-call for dozens of deployables. You need a platform team.
- **Boundaries are expensive to move.** Getting a boundary wrong in a monolith is a refactor;
  in microservices it is a migration.

Public examples on both sides: Amazon and Netflix scaled organisations with services; Shopify
deliberately runs a large **modular monolith** with enforced component boundaries; Segment wrote
"Goodbye Microservices" (2018) after consolidating over a hundred destination services back into
one; Amazon Prime Video's team described moving one monitoring pipeline from distributed
components to a single process and cutting its cost by about 90% (2023).

### When to migrate

Start with a **well-structured modular monolith** unless you already know you need otherwise.
Split a part out when there is a concrete reason:

- Several teams are blocked on each other's releases (deploy queues, merge conflicts, long test
  suites).
- A component has a very different scaling or resource profile, or availability requirement.
- A component needs fault isolation (a risky integration that must not take down checkout).
- Regulatory or security isolation (card data in a PCI-scoped service).
- The boundary is **stable and well understood**; you have run it as a module for a while.

Bad reasons: "it is modern", resumes, a team of five, or hoping it will fix a codebase whose
modules are already tangled (you will get a **distributed** tangle).

A modular monolith in Java can enforce boundaries with Java modules (JPMS), Maven/Gradle modules,
ArchUnit tests, or Spring Modulith (which verifies that modules only use each other's public API
and supports in-process application events):

```java
import com.tngtech.archunit.core.importer.ImportOption;
import com.tngtech.archunit.junit.AnalyzeClasses;
import com.tngtech.archunit.junit.ArchTest;
import com.tngtech.archunit.lang.ArchRule;

import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noClasses;

@AnalyzeClasses(packages = "com.example.shop", importOptions = ImportOption.DoNotIncludeTests.class)
class ModuleBoundaryTest {

    // Other modules may use com.example.shop.billing.api only, never billing internals.
    @ArchTest
    static final ArchRule billingInternalsArePrivate = noClasses()
            .that().resideOutsideOfPackage("com.example.shop.billing..")
            .should().dependOnClassesThat().resideInAPackage("com.example.shop.billing.internal..");
}
```

---

## Decomposition: where to draw boundaries

The most important and least reversible decision. Good boundaries give **high cohesion inside,
loose coupling between**: most changes touch one service.

### By business capability

A business capability is something the business does to create value: *catalogue management*,
*pricing*, *order management*, *payments*, *fulfilment*, *customer support*. Capabilities are
stable over years even as processes and UIs change, which makes them good service candidates.
Decomposing by technical layer (a "database service", a "validation service") is the classic
mistake: every feature then touches every service.

### Domain-Driven Design

DDD (Eric Evans, 2003) supplies the vocabulary.

- **Ubiquitous language**: within a context, code and domain experts use the same terms with
  the same meaning.
- **Bounded context**: the boundary inside which a model and its language are consistent. The
  word "Product" means a marketing description in *Catalogue*, a SKU with dimensions and stock
  in *Warehouse*, and a price list entry in *Pricing*. Forcing one shared `Product` model on all
  three creates a god object every team must coordinate on. A bounded context is the most natural
  candidate for a service (or for a small group of services owned by one team).
- **Subdomains**: *core* (your competitive advantage: invest), *supporting* (necessary,
  specific), *generic* (buy or use off-the-shelf: auth, email).

### Aggregates

An **aggregate** is a cluster of objects treated as one unit for consistency, with one **root**
through which all changes go. Rules: invariants hold within an aggregate after every
transaction; one transaction modifies one aggregate; aggregates reference each other by ID only.
These rules matter for microservices because an aggregate is the largest thing you can update
atomically, and it must never be split across services.

```java
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

public final class Order {                         // aggregate root

    public enum Status { DRAFT, PLACED, CANCELLED }

    public record Line(String sku, int quantity, long unitPriceCents) {
        public Line {
            if (quantity <= 0) throw new IllegalArgumentException("quantity must be positive");
        }
    }

    private final String id;
    private final String customerId;               // reference to another aggregate by ID only
    private final List<Line> lines = new ArrayList<>();
    private Status status = Status.DRAFT;
    private long version;

    public Order(String id, String customerId) {
        this.id = id;
        this.customerId = customerId;
    }

    public void addLine(String sku, int quantity, long unitPriceCents) {
        requireStatus(Status.DRAFT);
        if (lines.size() >= 100) {
            throw new IllegalStateException("an order has at most 100 lines");   // invariant
        }
        lines.add(new Line(sku, quantity, unitPriceCents));
    }

    public OrderPlaced place() {
        requireStatus(Status.DRAFT);
        if (lines.isEmpty()) {
            throw new IllegalStateException("cannot place an empty order");      // invariant
        }
        status = Status.PLACED;
        return new OrderPlaced(id, customerId, totalCents(), List.copyOf(lines));
    }

    public long totalCents() {
        return lines.stream().mapToLong(l -> l.quantity() * l.unitPriceCents()).sum();
    }

    public List<Line> lines() {
        return Collections.unmodifiableList(lines);
    }

    private void requireStatus(Status expected) {
        if (status != expected) {
            throw new IllegalStateException("order " + id + " is " + status);
        }
    }

    public record OrderPlaced(String orderId, String customerId, long totalCents, List<Line> lines) {}
}
```

The aggregate returns a **domain event** (`OrderPlaced`) instead of calling payments directly;
publishing it reliably is the outbox pattern's job (below).

### Context mapping

How bounded contexts (and therefore teams and services) relate:

| Pattern | Relationship | Example / when |
| --- | --- | --- |
| Partnership | Two teams succeed or fail together, plan jointly | Checkout and payments during a launch |
| Shared kernel | Small shared model both teams change only by agreement | A shared `Money` type; keep tiny |
| Customer / supplier | Upstream team prioritises downstream needs | Catalogue (upstream) serves search (downstream) |
| Conformist | Downstream adopts upstream's model as is | Consuming a dominant vendor's API |
| Anticorruption layer (ACL) | Downstream translates upstream's model into its own | Wrapping a legacy ERP so its model does not leak |
| Open host service | Upstream offers a well-defined protocol for all | A public, versioned API |
| Published language | Shared, documented exchange format | Event schemas in a registry, industry standards |
| Separate ways | No integration; duplicate instead | When integration costs more than duplication |

An ACL is essential during migrations: the new service talks to the monolith through a
translation layer so the legacy model does not infect the new one.

### Sizing heuristics

- One service per bounded context is a good starting point; split further only for a concrete
  reason (scaling, team ownership, change cadence).
- A team should own its services end to end; a service should not be owned by multiple teams.
- If two services always change and deploy together, they are one service.
- If a "service" is a thin CRUD wrapper over one table (an **entity service**), the business logic
  lives elsewhere and every feature needs several services; merge it into its capability.

---

## Strangler fig migration

Named by Martin Fowler after the fig that grows around a host tree until it replaces it:
incrementally route functionality from the legacy system to new services, never a big-bang
rewrite.

```text
 Step 1: put a routing facade in front; everything still goes to the monolith

   clients ──► facade ──────────────────────────────► monolith (orders, users, billing)

 Step 2: build the new service, route one slice to it, keep the rest on the monolith

   clients ──► facade ──┬── /orders/** ─────────────► orders-service ──► orders DB
                        └── everything else ────────► monolith (users, billing)

 Step 3: repeat per capability until the monolith is empty (or small enough to keep)

   clients ──► facade ──┬── /orders/** ─────────────► orders-service
                        ├── /users/** ──────────────► users-service
                        └── /billing/** ────────────► billing-service
```

Practical steps:

1. **Pick the first slice carefully**: valuable, relatively decoupled, not the most critical path.
   Edge capabilities (notifications, search) are common first candidates.
2. **Facade**: an API gateway or reverse proxy that routes by path, header, or user cohort.
3. **Data is the hard part.** Options: the new service reads the monolith's data through an API
   at first; then takes ownership of its tables, with the monolith calling the new service; use
   change data capture to keep a copy in sync during the transition; finally remove the old
   tables.
4. **Dark launch and compare**: send traffic to both, return the monolith's answer, diff the
   results ("parallel run"), then switch.
5. **Branch by abstraction** inside the monolith: introduce an interface over the capability,
   implement it by calling the new service, switch with a feature flag, delete the old code.
6. **Anticorruption layer** between the new service and the monolith's model.
7. **Delete** the old code path when traffic has moved. Unfinished strangler migrations, where
   both paths live forever, are common and expensive.

---

## Inter-service communication

### Synchronous: REST and gRPC

The caller waits for a response.

- Simple mental model, immediate consistency, easy request/response semantics.
- **Temporal coupling**: the caller is only as available as the callee. If A calls B calls C,
  each 99.9% available, the chain is at most about 99.7% available.
- Latency adds up along call chains; tail latencies amplify with fan-out (if a request fans out
  to 100 services and each has a 1% chance of being slow, about 63% of requests hit at least one
  slow call).
- REST/JSON for broad compatibility; gRPC for internal low-latency typed contracts and streaming.

Use for queries that need an answer now, and for commands whose result the user waits for.

### Asynchronous: events and messages

The producer publishes and moves on; consumers process later via a broker (Kafka, RabbitMQ, SQS/
SNS, Pulsar, NATS).

- **Commands** ("ReserveStock") are addressed to one consumer and expect it to act.
- **Events** ("OrderPlaced") state a fact that happened; any number of consumers may react; the
  producer does not know them.

Event styles:

| Style | Payload | Pros | Cons |
| --- | --- | --- | --- |
| Event notification | `{"orderId": "o1"}`; consumers call back for details | Small events, no data duplication | Callbacks couple consumers to the producer's availability |
| Event-carried state transfer | Full relevant state in the event | Consumers are autonomous, keep local copies | Larger events, schema evolution matters, eventual consistency |
| Event sourcing | Events are the system of record | Full history, rebuildable views | Complexity (see below) |

Properties to design for: **at-least-once delivery** (consumers must be idempotent), ordering
only within a partition (key by aggregate ID), schema evolution (a schema registry with Avro,
Protobuf, or JSON Schema and compatibility rules), dead-letter queues, and consumer lag
monitoring.

### Request/reply over messaging

Asynchronous request/response: the requester sends a message with a **correlation ID** and a
**reply-to** address, then continues; the responder publishes the reply to that address.

```text
orders-service                          broker                         pricing-service
  publish to "pricing.requests"   ─────►  ─────────────────────────────►  consume
  { correlationId: c-91,                                                  compute price
    replyTo: "orders.replies" }
  ...continues other work...
  consume "orders.replies"        ◄─────  ◄─────────────────────────────  publish reply
  match correlationId c-91                                                { correlationId: c-91 }
```

Useful when the responder may be slow or temporarily down (the request waits in the queue), for
long work, and for sagas. Costs: you must store pending requests, handle replies that never come
(timeouts), and replies that arrive after you gave up.

### Choosing

| Need | Prefer |
| --- | --- |
| User is waiting for data from another service | Sync (gRPC/REST) with timeout and fallback, or a local replica built from events |
| Notify others that something happened | Async events |
| Long-running, multi-step business process | Saga (orchestrated via messages or a workflow engine) |
| High-volume data propagation | Event streaming (Kafka) |
| Smoothing load spikes | Queue between producer and consumer |

Choreography vs orchestration: in **choreography** each service reacts to events and emits new
ones, with no central coordinator; it is loosely coupled but the overall flow is implicit and
hard to follow beyond a few steps. In **orchestration** a coordinator tells each participant what
to do; the flow is explicit, testable, and observable, at the cost of a central component. Most
teams use choreography for simple reactions and orchestration for business processes with
compensation.

---

## API gateway and Backend for Frontend

```text
  ┌────────────┐   ┌────────────┐   ┌────────────────┐
  │ iOS/Android│   │  Web SPA   │   │ Partner (REST) │
  └─────┬──────┘   └─────┬──────┘   └───────┬────────┘
        │                │                  │
  ┌─────▼──────┐   ┌─────▼──────┐   ┌───────▼────────┐
  │ Mobile BFF │   │  Web BFF   │   │ Public gateway │
  └─────┬──────┘   └─────┬──────┘   └───────┬────────┘
        └────────────────┼──────────────────┘
                         │
        ┌──────────┬─────┴─────┬──────────┐
        ▼          ▼           ▼          ▼
     orders     catalog     payments    users      (internal REST / gRPC)
```

The **API gateway** is the single entry point: TLS termination, authentication, rate limiting,
routing, request IDs, CORS, response caching. Products: Kong, Envoy Gateway, AWS API Gateway,
Apigee, Spring Cloud Gateway.

A **Backend for Frontend** (Sam Newman's term) is an API layer per client type, owned by the team
building that client. The mobile BFF aggregates five service calls into one screen-shaped
response with small payloads; the web BFF does the same for the web's different screens. A BFF is
also the recommended place to hold OAuth tokens for browser apps (tokens stay server-side; the
browser gets an `HttpOnly` cookie).

Pitfalls: putting business logic in the gateway (it becomes a shared monolith); one "general
purpose" BFF for all clients (it becomes a gateway with everyone's logic); the gateway as a single
point of failure (run it replicated across zones).

---

## Service discovery

Instances come and go (autoscaling, deploys, failures), so callers must find current addresses
dynamically.

| | Client-side discovery | Server-side discovery |
| --- | --- | --- |
| How | Client queries a registry and load balances itself | Client calls a stable name; a load balancer/proxy routes |
| Examples | Netflix Eureka + Spring Cloud LoadBalancer, Consul, gRPC with DNS and `round_robin` | Kubernetes Services, AWS ALB/NLB, service meshes |
| Pros | No extra hop; smart balancing (least requests, zone-aware) | Language-agnostic; clients stay simple |
| Cons | Discovery logic in every client language | Extra hop (or kernel rules); less smart balancing |

Registration is either **self-registration** (the instance registers and sends heartbeats) or
**third-party** (the platform registers it; Kubernetes does this from pod readiness).

### Kubernetes Services and DNS

- A **Service** selects pods by label and gets a stable virtual IP (ClusterIP) and DNS name:
  `orders.shop.svc.cluster.local` (`<service>.<namespace>.svc.<cluster-domain>`); within the same
  namespace, plain `orders` resolves.
- **EndpointSlices** list the IPs of the Service's **ready** pods; a pod failing its readiness
  probe is removed automatically.
- **kube-proxy** programs the node (iptables, IPVS, or nftables mode) so connections to the
  ClusterIP are spread across endpoints at **connection** level.
- A **headless Service** (`clusterIP: None`) returns pod IPs directly in DNS, enabling client-side
  load balancing (needed for gRPC, whose long-lived HTTP/2 connections otherwise stick to one pod)
  and stable per-pod names for StatefulSets.
- Service types: `ClusterIP` (internal), `NodePort`, `LoadBalancer` (cloud LB), `ExternalName`
  (DNS alias).

---

## Configuration management

- **Externalise configuration** (Twelve-Factor App): the same immutable image runs in every
  environment; config comes from the environment.
- **Kubernetes ConfigMaps** for non-secret config (as env vars or mounted files; mounted files
  update in place, env vars need a restart). **Secrets** for credentials, noting that by default
  they are only base64-encoded; enable encryption at rest and restrict RBAC, or use an external
  secret store.
- **Central config services** (Spring Cloud Config, Consul KV, AWS AppConfig) for dynamic,
  versioned config with audit.
- **Validate at startup** and fail fast on missing or invalid config (Spring
  `@ConfigurationProperties` with `@Validated`).
- **Config is code**: versioned in git, reviewed, rolled out progressively. A bad config push is a
  deploy and has caused many large outages.
- **Feature flags** are runtime config for behaviour (see deployment). OpenFeature is a CNCF
  vendor-neutral API for flag evaluation.

---

## Data management

### Database per service

Each service owns its data store; **no other service reads or writes it directly**. Others go
through the service's API or consume its events. This is what makes services independently
deployable: a service can change its schema, or its database technology, without coordinating.

"Per service" means logically private: separate databases, or separate schemas with separate
credentials on a shared server, are both acceptable. What matters is that no other service has
access.

### The shared database anti-pattern

Services sharing tables get the worst of both worlds: network calls **and** coupling.

- A column rename requires coordinated deploys of every service that touches the table.
- No service owns the invariants; any service can write invalid state.
- Contention and noisy neighbours: one service's heavy query slows everyone.
- You cannot tell who depends on what.

It is acceptable as a temporary migration step, or for read-only reporting replicas fed by CDC.

### Sagas

A **saga** is a sequence of local transactions, one per service, where each step has a
**compensating transaction** that semantically undoes it if a later step fails. It replaces a
distributed ACID transaction (two-phase commit is rarely used between microservices: it blocks on
coordinator failure, needs every participant to support XA, and couples availability).

Order placement as a saga:

| Step | Service | Action | Compensation |
| --- | --- | --- | --- |
| 1 | Orders | Create order `PENDING` | Mark order `REJECTED` |
| 2 | Inventory | Reserve stock | Release reservation |
| 3 | Payments | Authorise payment (**pivot**) | Void authorisation |
| 4 | Shipping | Create shipment (retriable) | (none needed: retried until it succeeds) |
| 5 | Orders | Mark order `CONFIRMED` | |

Steps before the **pivot transaction** are compensatable; after the pivot, steps must be
**retriable** until they succeed, because the saga can no longer be rolled back.

```text
 Orchestrator        Inventory          Payments          Shipping
      │ ReserveStock     │                  │                 │
      │─────────────────►│                  │                 │
      │◄──── reserved ───│                  │                 │
      │ AuthorizePayment                    │                 │
      │────────────────────────────────────►│                 │
      │◄──────────────────────── declined ──│                 │
      │ ReleaseStock (compensation)         │                 │
      │─────────────────►│                  │                 │
      │◄──── released ───│                  │                 │
      │ mark order REJECTED                                   │
```

**Orchestration example in Java.** The orchestrator persists the saga's progress after every step,
so a crash resumes from the log instead of losing track. Participants are called through clients
that pass the saga ID as an idempotency key, so repeated calls during recovery are harmless.

```java
import java.util.List;
import java.util.UUID;

public final class OrderSaga {

    public enum Status { RUNNING, COMPENSATING, COMPLETED, FAILED }

    public record Data(UUID sagaId, String orderId, String customerId, long amountCents,
                       List<Order.Line> lines) {}

    public record Instance(Data data, Status status, int completedSteps, String failureReason) {}

    public sealed interface Outcome permits Completed, Rejected {}
    public record Completed(UUID sagaId) implements Outcome {}
    public record Rejected(UUID sagaId, String reason) implements Outcome {}

    /** A business "no" (card declined, out of stock): compensate, do not retry. */
    public static final class StepRejected extends RuntimeException {
        public StepRejected(String reason) { super(reason); }
    }

    /** Each step must be idempotent, keyed by sagaId; compensation likewise. */
    public interface Step {
        String name();
        void execute(Data data);
        void compensate(Data data);
    }

    public interface SagaLog {
        Instance stepCompleted(Instance saga, int stepIndex);
        Instance compensating(Instance saga, String reason);
        Instance stepCompensated(Instance saga, int stepIndex);
        Instance finish(Instance saga, Status status);
    }

    private final List<Step> steps;
    private final SagaLog log;

    public OrderSaga(List<Step> steps, SagaLog log) {
        this.steps = List.copyOf(steps);
        this.log = log;
    }

    /** Runs a new saga or resumes one loaded from the log after a crash. */
    public Outcome run(Instance saga) {
        if (saga.status() == Status.COMPENSATING) {
            return compensate(saga, saga.completedSteps() - 1, saga.failureReason());
        }
        for (int i = saga.completedSteps(); i < steps.size(); i++) {
            try {
                steps.get(i).execute(saga.data());
            } catch (StepRejected rejected) {
                saga = log.compensating(saga, steps.get(i).name() + ": " + rejected.getMessage());
                return compensate(saga, i - 1, saga.failureReason());
            }
            // Transient failures (timeouts, 503) propagate: a scheduler retries run() later,
            // and because steps are idempotent, re-executing step i is safe.
            saga = log.stepCompleted(saga, i);
        }
        log.finish(saga, Status.COMPLETED);
        return new Completed(saga.data().sagaId());
    }

    private Outcome compensate(Instance saga, int lastCompleted, String reason) {
        for (int i = lastCompleted; i >= 0; i--) {
            steps.get(i).compensate(saga.data());   // retried until success; alert if stuck
            saga = log.stepCompensated(saga, i);
        }
        log.finish(saga, Status.FAILED);
        return new Rejected(saga.data().sagaId(), reason);
    }
}
```

A step implementation, calling the inventory service:

```java
final class ReserveStock implements OrderSaga.Step {

    private final InventoryClient inventory;

    ReserveStock(InventoryClient inventory) {
        this.inventory = inventory;
    }

    @Override
    public String name() {
        return "reserve-stock";
    }

    @Override
    public void execute(OrderSaga.Data d) {
        var result = inventory.reserve(d.sagaId().toString(), d.lines());   // idempotency key
        if (!result.reserved()) {
            throw new OrderSaga.StepRejected("insufficient stock for " + result.missingSkus());
        }
    }

    @Override
    public void compensate(OrderSaga.Data d) {
        inventory.release(d.sagaId().toString());   // releasing twice, or an unknown id, is a no-op
    }
}
```

Saga realities:

- **No isolation.** Other transactions see intermediate states (stock reserved, payment not yet
  taken). Countermeasures: semantic locks (`PENDING` status that others respect), commutative
  updates, re-reading values before acting, ordering steps so the riskiest is the pivot.
- **Compensations are business operations**, not rollbacks: a refund, a cancellation email, a
  release of a reservation. Some actions cannot be undone (an email was sent), so put them last.
- **Workflow engines** handle persistence, retries, timers, and visibility for you: Temporal,
  Camunda, AWS Step Functions, Azure Durable Functions; Java frameworks such as Axon and Eventuate
  Tram offer saga support. Hand-rolled orchestrators are fine for a few sagas, but you will end up
  rebuilding parts of these engines.

### Transactional outbox

The **dual-write problem**: a service must update its database **and** publish an event. If it
commits and then crashes before publishing, the event is lost; if it publishes first and the
commit fails, consumers act on something that never happened. Two-phase commit between a database
and Kafka is not a practical option.

The outbox writes the event into the **same database, in the same transaction**, then a separate
relay publishes it.

```text
  ┌──────────────── one local DB transaction ────────────────┐
  │  INSERT INTO orders (...)                                │
  │  INSERT INTO outbox (event_type='OrderPlaced', ...)      │
  └──────────────────────────────────────────────────────────┘
               │
               ▼
  relay (poller, or CDC such as Debezium reading the WAL / binlog)
               │  publish, then mark published (at least once)
               ▼
  Kafka topic "orders.events" ──► consumers (idempotent: dedupe by event id)
```

```sql
CREATE TABLE outbox (
    id             UUID PRIMARY KEY,
    aggregate_type TEXT        NOT NULL,     -- "order"
    aggregate_id   TEXT        NOT NULL,     -- used as the Kafka message key (ordering)
    event_type     TEXT        NOT NULL,     -- "OrderPlaced"
    payload        JSONB       NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at   TIMESTAMPTZ
);
CREATE INDEX outbox_unpublished ON outbox (created_at) WHERE published_at IS NULL;
```

```java
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.UUID;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.annotation.Transactional;

public class PlaceOrderService {

    private final OrderRepository orders;
    private final JdbcTemplate jdbc;
    private final ObjectMapper json;

    public PlaceOrderService(OrderRepository orders, JdbcTemplate jdbc, ObjectMapper json) {
        this.orders = orders;
        this.jdbc = jdbc;
        this.json = json;
    }

    @Transactional
    public void place(Order order) throws Exception {
        Order.OrderPlaced event = order.place();
        orders.save(order);
        jdbc.update("""
                INSERT INTO outbox (id, aggregate_type, aggregate_id, event_type, payload)
                VALUES (?, 'order', ?, 'OrderPlaced', ?::jsonb)
                """, UUID.randomUUID(), event.orderId(), json.writeValueAsString(event));
        // Both rows commit or neither does. No broker call inside the transaction.
    }
}
```

The polling relay, safe to run on several instances at once thanks to `SKIP LOCKED`:

```sql
-- In a transaction, repeatedly:
SELECT id, aggregate_id, event_type, payload
  FROM outbox
 WHERE published_at IS NULL
 ORDER BY created_at
 LIMIT 100
 FOR UPDATE SKIP LOCKED;
-- publish each to Kafka (key = aggregate_id), wait for acks, then:
UPDATE outbox SET published_at = now() WHERE id = ANY(:published_ids);
```

- **At least once**: the relay can crash after publishing and before marking, so events may be
  published twice. Consumers deduplicate by event ID (an **inbox** table of processed IDs, written
  in the same transaction as the consumer's own changes).
- **Ordering**: polling by `created_at` across concurrent transactions is not strictly commit
  order; key messages by aggregate ID and include a per-aggregate sequence number if consumers
  need ordering.
- **CDC** (Debezium reading the PostgreSQL WAL or MySQL binlog, with its outbox event router)
  avoids polling load and publishes in commit order. Debezium can also delete outbox rows
  immediately after insert, since it reads the log, not the table.
- Clean up published rows periodically.

### CQRS

**Command Query Responsibility Segregation**: separate the model that handles writes (commands,
invariants, normalised) from one or more models optimised for reads (denormalised, per screen),
kept in sync by events.

```text
            commands                                   queries
               │                                          │
      ┌────────▼────────┐                       ┌─────────▼────────┐
      │  Write model    │   events (Kafka)      │  Read model(s)   │
      │  (normalised,   │ ────────────────────► │  (denormalised,  │
      │   invariants)   │   projector updates   │   per screen)    │
      └────────┬────────┘                       └─────────┬────────┘
               │                                          │
         ┌─────▼─────┐                            ┌───────▼───────┐
         │ Postgres  │                            │ Elasticsearch │
         └───────────┘                            │ / Redis / ... │
                                                  └───────────────┘
```

In microservices, CQRS is the standard answer to "how do I query data owned by five services?":
build a **read model** (a "view service") that subscribes to their events and maintains the joined
view. Costs: eventual consistency (the read side lags; design UIs for it, e.g. return the written
data directly after a command), more moving parts, and projections that must be rebuildable from
events.

### Event sourcing

Store the **sequence of events** as the source of truth; current state is a fold over them.

```java
import java.util.List;

public sealed interface AccountEvent permits AccountEvent.Opened, AccountEvent.Deposited,
        AccountEvent.Withdrawn {
    record Opened(String accountId, String owner) implements AccountEvent {}
    record Deposited(long amountCents) implements AccountEvent {}
    record Withdrawn(long amountCents) implements AccountEvent {}
}

public record Account(String id, String owner, long balanceCents, long version) {

    public static Account replay(List<AccountEvent> history) {
        Account state = null;
        for (AccountEvent e : history) {
            state = apply(state, e);
        }
        return state;
    }

    static Account apply(Account a, AccountEvent e) {
        return switch (e) {
            case AccountEvent.Opened o -> new Account(o.accountId(), o.owner(), 0, 1);
            case AccountEvent.Deposited d ->
                    new Account(a.id(), a.owner(), a.balanceCents() + d.amountCents(), a.version() + 1);
            case AccountEvent.Withdrawn w ->
                    new Account(a.id(), a.owner(), a.balanceCents() - w.amountCents(), a.version() + 1);
        };
    }

    /** Command handler: validate against current state, return new events (not yet stored). */
    public List<AccountEvent> withdraw(long amountCents) {
        if (amountCents <= 0) throw new IllegalArgumentException("amount must be positive");
        if (amountCents > balanceCents) throw new IllegalStateException("insufficient funds");
        return List.of(new AccountEvent.Withdrawn(amountCents));
    }
}
```

The event store appends with an **expected version** for optimistic concurrency:

```sql
CREATE TABLE events (
    stream_id   TEXT        NOT NULL,
    version     BIGINT      NOT NULL,
    event_type  TEXT        NOT NULL,
    payload     JSONB       NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (stream_id, version)      -- a concurrent append at the same version fails
);
```

| Benefits | Costs |
| --- | --- |
| Complete audit history; temporal queries ("balance on 1 March") | Harder to learn and query; needs CQRS for reads |
| Events are naturally published (no outbox needed if the store is the log) | Event schema evolution forever (upcasting old events) |
| Rebuild or add read models by replaying | Long streams need snapshots |
| Debugging by replaying production history | GDPR deletion is awkward (crypto-shredding) |

Use it where history is the domain (ledgers, bookings, workflows), not as a default.

### API composition

For queries spanning services, a composer (BFF, gateway, or dedicated service) calls each owner
and joins in memory:

```text
GET /order-details/o1
  composer ──► orders-service     GET /orders/o1            (customerId, lines)
           ──► customers-service  GET /customers/c7         } in parallel
           ──► shipping-service   GET /shipments?order=o1   }
  join, return
```

Simple and consistent with sources of truth, but latency is bounded by the slowest call,
availability is the product of all callees, and large joins (filtering and sorting across
services) are impractical, which is when you move to a CQRS read model.

---

## Resilience

Networks fail, services slow down, and in a large system something is always degraded. The
dominant failure mode is the **cascading failure**: one slow dependency ties up threads in its
callers, which become slow, which ties up their callers, until the whole system is down.

### Timeouts, budgets, and deadline propagation

- **Every network call needs a timeout**, including connection-pool acquisition. Default
  timeouts in many HTTP clients are infinite or very long.
- Set timeouts from the callee's **observed latency** (e.g. a bit above its p99.9), not from
  hope. Separate connect timeouts (short, ~100 ms to 1 s inside a data centre) from request
  timeouts.
- **Budgets**: the end-to-end request has a deadline; each hop gets what remains. An inner timeout
  longer than the outer one is pointless: the caller has already given up, so the work is wasted.

```text
 client deadline: 1,000 ms
 gateway     spends  20 ms  → forwards deadline (980 ms left)
 orders      spends  80 ms  → calls inventory and payments in parallel with 900 ms left
 inventory   caps its own downstream calls at 300 ms, well inside the 900 ms remaining
 any service that receives a request whose deadline already passed drops it immediately
```

- **Deadline propagation**: send the absolute deadline (or remaining time) with each call. gRPC
  does this natively (`grpc-timeout`, and gRPC Java carries it in `Context`); for HTTP use a
  header such as `X-Request-Deadline` and apply it to outgoing clients.
- Servers should also **check the deadline before expensive work** and abandon requests nobody is
  waiting for.

### Retries and retry storms

Retries fix transient failures (a pod restarting, a dropped connection) and amplify persistent
ones.

```text
 each layer: 1 try + 2 retries = up to 3 attempts

 client ──3x──► gateway ──3x──► orders ──3x──► inventory ──3x──► database
 1 request      3               9              27                81 attempts at the bottom
```

Rules:

- **Retry only idempotent operations** (or ones with idempotency keys) and only on retryable
  errors (connection failures, 503, 429 with `Retry-After`, gRPC `UNAVAILABLE`). Never retry 400s.
- **Exponential backoff with jitter**, small max attempts (2 to 3), within the deadline.
- **Retry at one layer only**, typically the one closest to the user or the edge proxy, not at
  every hop.
- **Retry budgets**: cap retries as a fraction of traffic (e.g. at most 10% extra). Envoy and
  gRPC support retry budgets/throttling; Google's SRE book describes per-client retry ratios.
- **Hedged requests** (send a second request after the p95 latency elapses, take the first
  answer) cut tail latency for idempotent reads but add load; use with a budget.

### Circuit breaker

Stop calling a dependency that is failing; fail fast and give it room to recover (Michael Nygard,
*Release It!*).

```text
   ┌──────────────┐  failure rate ≥ threshold   ┌──────────────┐
   │    CLOSED    │ ──────────────────────────► │     OPEN     │
   │ calls pass,  │                             │ fail fast,   │
   │ outcomes     │                             │ no remote    │
   │ recorded     │                             │ calls        │
   └──────────────┘                             └──────────────┘
          ▲                                        │       ▲
          │ trial calls                 wait       │       │ any trial
          │ succeed                     elapsed    ▼       │ call fails
          │                             ┌──────────────────┴─┐
          └─────────────────────────────│     HALF-OPEN      │
                                        │ N trial calls      │
                                        └────────────────────┘
```

- **Closed**: calls go through; outcomes are recorded in a sliding window (count-based: last N
  calls; or time-based: last N seconds).
- **Open**: when the failure rate (or slow-call rate) in the window exceeds the threshold, and at
  least `minimumNumberOfCalls` were recorded, calls are rejected immediately
  (`CallNotPermittedException` in Resilience4j) for `waitDurationInOpenState`.
- **Half-open**: a limited number of trial calls are permitted; if they succeed the breaker
  closes, otherwise it opens again.

Resilience4j configuration (Spring Boot `application.yml`):

```yaml
resilience4j:
  circuitbreaker:
    instances:
      payments:
        slidingWindowType: COUNT_BASED
        slidingWindowSize: 50
        minimumNumberOfCalls: 20
        failureRateThreshold: 50              # percent
        slowCallDurationThreshold: 2s
        slowCallRateThreshold: 80             # percent of calls slower than 2s
        waitDurationInOpenState: 30s
        permittedNumberOfCallsInHalfOpenState: 5
        automaticTransitionFromOpenToHalfOpenEnabled: true
        recordExceptions:
          - java.io.IOException
          - java.util.concurrent.TimeoutException
        ignoreExceptions:
          - com.example.payments.CardDeclinedException   # business errors are not failures
  retry:
    instances:
      payments:
        maxAttempts: 3
        waitDuration: 200ms
        enableExponentialBackoff: true
        exponentialBackoffMultiplier: 2
        enableRandomizedWait: true
        retryExceptions:
          - java.io.IOException
  bulkhead:
    instances:
      payments:
        maxConcurrentCalls: 25
        maxWaitDuration: 0ms
  timelimiter:
    instances:
      payments:
        timeoutDuration: 2s
```

The same in code, composing decorators explicitly:

```java
import io.github.resilience4j.bulkhead.Bulkhead;
import io.github.resilience4j.bulkhead.BulkheadConfig;
import io.github.resilience4j.circuitbreaker.CallNotPermittedException;
import io.github.resilience4j.circuitbreaker.CircuitBreaker;
import io.github.resilience4j.circuitbreaker.CircuitBreakerConfig;
import io.github.resilience4j.circuitbreaker.CircuitBreakerConfig.SlidingWindowType;
import io.github.resilience4j.core.IntervalFunction;
import io.github.resilience4j.decorators.Decorators;
import io.github.resilience4j.retry.Retry;
import io.github.resilience4j.retry.RetryConfig;
import java.io.IOException;
import java.time.Duration;
import java.util.List;
import java.util.function.Supplier;

CircuitBreaker breaker = CircuitBreaker.of("payments", CircuitBreakerConfig.custom()
        .slidingWindowType(SlidingWindowType.COUNT_BASED)
        .slidingWindowSize(50)
        .minimumNumberOfCalls(20)
        .failureRateThreshold(50)
        .slowCallDurationThreshold(Duration.ofSeconds(2))
        .slowCallRateThreshold(80)
        .waitDurationInOpenState(Duration.ofSeconds(30))
        .permittedNumberOfCallsInHalfOpenState(5)
        .ignoreExceptions(CardDeclinedException.class)
        .build());

Retry retry = Retry.of("payments", RetryConfig.custom()
        .maxAttempts(3)
        .intervalFunction(IntervalFunction.ofExponentialRandomBackoff(Duration.ofMillis(200), 2.0, 0.5))
        .retryExceptions(IOException.class)
        .build());

Bulkhead bulkhead = Bulkhead.of("payments", BulkheadConfig.custom()
        .maxConcurrentCalls(25)
        .maxWaitDuration(Duration.ZERO)
        .build());

Supplier<Receipt> call = Decorators.ofSupplier(() -> paymentsClient.charge(order))
        .withBulkhead(bulkhead)
        .withCircuitBreaker(breaker)
        .withRetry(retry)
        .withFallback(List.of(CallNotPermittedException.class, IOException.class),
                e -> Receipt.pending(order.id()))      // degrade: charge later, tell the user
        .decorate();

Receipt receipt = call.get();
```

Decoration order matters. The decorator added last is the outermost: here retry wraps the circuit
breaker, so each retry attempt is recorded by the breaker and an open breaker stops retries
quickly. Resilience4j's Spring Boot annotations apply a default order of
`Retry(CircuitBreaker(RateLimiter(TimeLimiter(Bulkhead(call)))))`; check the documentation for
your version if you rely on it.

Tuning notes: the threshold must be computed over enough calls (`minimumNumberOfCalls`) or a
handful of errors at low traffic opens it; business errors (card declined, 404) must be ignored;
one breaker per dependency (and sometimes per host); expose breaker state as a metric and alert on
transitions.

### Bulkheads

Partition resources so one failing dependency cannot consume all of them, like watertight
compartments in a ship.

- **Semaphore bulkhead**: cap concurrent calls per dependency (Resilience4j `Bulkhead`).
- **Thread-pool bulkhead**: a dedicated, bounded pool and queue per dependency
  (`ThreadPoolBulkhead`), so a hung dependency exhausts only its own threads.
- **Connection pools per downstream**, separate pools for critical vs batch traffic.
- **Infrastructure bulkheads**: separate deployments for critical and non-critical traffic
  (a checkout cluster vs a reporting cluster), cell-based architecture (each cell serves a subset
  of customers with its own stack, limiting blast radius).

With virtual threads (Java 21), threads are no longer the scarce resource, so bulkheads should
limit **concurrency to the dependency** (semaphores) rather than rely on thread pool sizes.

### Rate limiting

Protect services from callers (per-client limits at the gateway and per-caller limits between
services) and protect fragile dependencies from yourself (client-side limiters for partner APIs).
See the Rate limiting page for algorithms and distributed implementations.

### Fallbacks and graceful degradation

When a call fails or the breaker is open, return something useful:

- **Cached / stale data**: last known prices, a cached product page.
- **Defaults**: generic recommendations instead of personalised ones; hide the widget.
- **Deferred work**: accept the order, charge later (queue it), tell the user "processing".
- **Fail closed for correctness-critical paths**: do not "fall back" to approving a payment.

Fallbacks are code paths that rarely run, so they rot: test them (chaos experiments, unit tests
with failing stubs).

### Load shedding

When a service is beyond capacity, rejecting some requests quickly keeps the rest fast; serving
everyone slowly ends with timeouts for everyone. Shed early (before expensive work), by priority
(health checks and critical user paths last), return 503 with `Retry-After`, and consider
adaptive concurrency limits (Netflix concurrency-limits) that find capacity from latency.

### Chaos engineering

Deliberately inject failures in a controlled way to verify that the resilience mechanisms work.

1. Define **steady state** with metrics (orders per minute, p99 latency).
2. Hypothesise it holds under a failure (a payments instance dies, 300 ms latency to Redis).
3. Inject the failure with a small **blast radius** (one instance, a fraction of traffic), with
   an abort switch.
4. Compare to steady state; fix what broke; automate the experiment.

Tools and history: Netflix's Chaos Monkey (terminating instances) and the wider Simian Army;
Gremlin; Chaos Mesh and LitmusChaos on Kubernetes; AWS Fault Injection Service; service-mesh
fault injection (delays, aborts). **Game days** practise incident response as well as the
system.

---

## Observability

With dozens of services you cannot attach a debugger. Observability means being able to answer
new questions about production from the telemetry you emit.

### Structured logs

```json
{"ts":"2026-10-31T11:52:03.412Z","level":"WARN","service":"orders","version":"1.14.2",
 "trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","span_id":"00f067aa0ba902b7",
 "msg":"payment authorisation declined","order_id":"ord_42","reason":"insufficient_funds"}
```

- JSON, one event per line, consistent field names across services.
- Always include `trace_id` and `span_id` (inject via the logging MDC; OpenTelemetry
  instrumentation can do it automatically).
- Log events with context, not prose; never log secrets, tokens, or full card numbers.
- Centralise (Loki, Elasticsearch/OpenSearch, Splunk, cloud logging) and control volume with
  levels and sampling; logs are usually the most expensive telemetry.

### Metrics: RED and USE

| Method | For | Signals |
| --- | --- | --- |
| **RED** (Tom Wilkie) | Request-driven services | **R**ate (req/s), **E**rrors (failed req/s), **D**uration (latency histogram) |
| **USE** (Brendan Gregg) | Resources: CPU, memory, disks, pools, queues | **U**tilisation, **S**aturation (queue length, waiting), **E**rrors |
| Four golden signals (Google SRE) | User-facing systems | Latency, traffic, errors, saturation |

- Record latency as **histograms**, not averages; percentiles cannot be averaged across
  instances, histograms can be aggregated.
- Separate latency of successful and failed requests (fast failures distort the picture).
- Watch label cardinality: never use user IDs or order IDs as metric labels.
- Prometheus + Grafana, or OpenTelemetry metrics to any backend; Micrometer in Spring Boot.

### Distributed tracing with OpenTelemetry

A **trace** is the tree of **spans** for one request across services. Each span has a trace ID,
its own span ID, its parent's ID, timing, attributes, and status.

```text
trace 4bf92f35...                          0 ms        100 ms       200 ms
├─ gateway   GET /checkout                 ████████████████████████████  210 ms
│  └─ orders POST /orders                    ██████████████████████████  190 ms
│     ├─ postgres INSERT orders               ██                          12 ms
│     ├─ inventory  Reserve (gRPC)              ████                      35 ms
│     └─ payments   POST /authorisations            ████████████████     120 ms  ◄ slow
│        └─ card-network call                        ██████████████      105 ms
└─ (async) notifications  consume OrderPlaced                       ███   after response
```

**Context propagation** is what links spans across processes. The W3C Trace Context standard
defines the `traceparent` header (plus `tracestate` for vendor data):

```text
traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
             │  │                                │                └─ flags (01 = sampled)
             │  │                                └─ parent span id (16 hex)
             │  └─ trace id (32 hex)
             └─ version
```

For messaging, the same context goes into message headers (Kafka record headers), so the consumer
span links to the producer. `baggage` carries application key-values (tenant, experiment) across
hops; keep it small and non-sensitive.

OpenTelemetry (CNCF) provides vendor-neutral APIs, SDKs, auto-instrumentation (the Java agent
instruments common HTTP servers and clients, JDBC, Kafka, gRPC without code changes), and the
OTLP protocol; the **Collector** receives, processes (sampling, redaction), and exports to
backends (Jaeger, Tempo, Zipkin, commercial APMs). Manual spans for business operations:

```java
import io.opentelemetry.api.OpenTelemetry;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.StatusCode;
import io.opentelemetry.api.trace.Tracer;
import io.opentelemetry.context.Scope;

public final class PricingService {

    private final Tracer tracer;

    public PricingService(OpenTelemetry otel) {
        this.tracer = otel.getTracer("com.example.pricing");
    }

    public Price quote(String sku, int quantity) {
        Span span = tracer.spanBuilder("pricing.quote")
                .setAttribute("pricing.sku", sku)
                .setAttribute("pricing.quantity", quantity)
                .startSpan();
        try (Scope ignored = span.makeCurrent()) {        // child spans and logs see this context
            Price price = computeWithPromotions(sku, quantity);
            span.setAttribute("pricing.promotions_applied", price.promotions().size());
            return price;
        } catch (RuntimeException e) {
            span.recordException(e);
            span.setStatus(StatusCode.ERROR);
            throw e;
        } finally {
            span.end();
        }
    }
}
```

Sampling: **head sampling** (decide at the root, e.g. 10%) is cheap but misses rare errors; **tail
sampling** in the Collector (keep all errors and slow traces, sample the rest) is more useful and
needs all spans of a trace to reach the same Collector instance.

### SLOs and error budgets

- **SLI** (indicator): a measured ratio, e.g. "fraction of `POST /orders` requests that succeed
  within 500 ms", measured at the load balancer.
- **SLO** (objective): the target for the SLI over a window, e.g. 99.9% over 30 days.
- **SLA** (agreement): a contractual promise with penalties, set looser than the SLO.
- **Error budget** = 1 - SLO: the amount of unreliability you may spend.

| SLO (30 days) | Error budget (time-equivalent) |
| --- | --- |
| 99% | 7.2 hours |
| 99.9% | 43.2 minutes |
| 99.95% | 21.6 minutes |
| 99.99% | 4.32 minutes |

How teams use it: while budget remains, ship features and take risks; when it is exhausted,
prioritise reliability (freeze risky launches). Alert on **burn rate** (how fast the budget is
being spent) rather than raw error rates. The Google SRE Workbook's multiwindow approach pages
when, for example, the 1-hour burn rate exceeds 14.4 (that rate consumes 2% of a 30-day budget in
one hour) confirmed by a 5-minute window, and opens tickets for slower burns over days.

A dependency's SLO bounds yours: a service calling three dependencies serially, each at 99.9%,
cannot promise 99.9% itself without fallbacks or redundancy.

### Health endpoints

- **Liveness**: "the process is alive and not deadlocked". Failure restarts the container. Must
  **not** check dependencies, or a database blip restarts every pod at once.
- **Readiness**: "send me traffic". Failure removes the pod from Service endpoints. May reflect
  warm-up, overload, or critical dependency state (with care).
- **Startup**: holds off liveness checks until a slow-starting app is up.

Spring Boot Actuator exposes `/actuator/health/liveness` and `/actuator/health/readiness` and
enables these probe groups automatically when it detects Kubernetes.

---

## Deployment

### Containers

Each service ships as an immutable image: the same artifact goes through every environment.

```dockerfile
FROM eclipse-temurin:21-jdk AS build
WORKDIR /src
COPY . .
RUN ./mvnw -q -DskipTests package

FROM eclipse-temurin:21-jre
RUN useradd --system --uid 10001 app
WORKDIR /app
COPY --from=build /src/target/orders.jar /app/orders.jar
USER 10001
EXPOSE 8080
ENTRYPOINT ["java", "-XX:MaxRAMPercentage=75", "-jar", "/app/orders.jar"]
```

Multi-stage builds keep compilers out of the runtime image; run as non-root; pin base images and
scan them; `MaxRAMPercentage` sizes the heap from the container memory limit (JVMs have been
container-aware since Java 10). Tag images immutably (version or git SHA), never deploy `latest`.

### Kubernetes basics

| Object | Purpose |
| --- | --- |
| Pod | One or more containers sharing network namespace and volumes; the scheduling unit; ephemeral |
| Deployment | Desired replica count and pod template; performs rolling updates via ReplicaSets |
| Service | Stable virtual IP and DNS name in front of ready pods |
| Ingress / Gateway API | HTTP routing from outside the cluster to Services |
| HorizontalPodAutoscaler | Adjusts replicas from metrics (CPU, memory, custom/external) |
| ConfigMap / Secret | Configuration and credentials |
| PodDisruptionBudget | Limits voluntary evictions (node drains) so enough replicas stay up |
| Namespace | Scope for names, RBAC, quotas |

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: orders
  namespace: shop
  labels: { app: orders }
spec:
  replicas: 3
  selector:
    matchLabels: { app: orders }
  strategy:
    type: RollingUpdate
    rollingUpdate: { maxSurge: 1, maxUnavailable: 0 }    # never drop below desired capacity
  template:
    metadata:
      labels: { app: orders, version: v1 }
    spec:
      serviceAccountName: orders
      terminationGracePeriodSeconds: 45
      containers:
        - name: orders
          image: registry.example.com/shop/orders:1.14.2
          ports:
            - containerPort: 8080
          resources:
            requests: { cpu: 500m, memory: 768Mi }
            limits: { memory: 768Mi }
          envFrom:
            - configMapRef: { name: orders-config }
          startupProbe:
            httpGet: { path: /actuator/health/liveness, port: 8080 }
            periodSeconds: 5
            failureThreshold: 30          # up to 150 s to start
          livenessProbe:
            httpGet: { path: /actuator/health/liveness, port: 8080 }
            periodSeconds: 10
            failureThreshold: 3
          readinessProbe:
            httpGet: { path: /actuator/health/readiness, port: 8080 }
            periodSeconds: 5
            failureThreshold: 2
          lifecycle:
            preStop:
              exec: { command: ["sh", "-c", "sleep 10"] }   # let endpoints update before SIGTERM
---
apiVersion: v1
kind: Service
metadata:
  name: orders
  namespace: shop
spec:
  selector: { app: orders }
  ports:
    - port: 80
      targetPort: 8080
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: shop-api
  namespace: shop
spec:
  ingressClassName: nginx
  rules:
    - host: api.example.com
      http:
        paths:
          - path: /orders
            pathType: Prefix
            backend:
              service:
                name: orders
                port: { number: 80 }
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: orders
  namespace: shop
spec:
  scaleTargetRef: { apiVersion: apps/v1, kind: Deployment, name: orders }
  minReplicas: 3
  maxReplicas: 30
  metrics:
    - type: Resource
      resource:
        name: cpu
        target: { type: Utilization, averageUtilization: 65 }
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300     # avoid flapping
```

Notes that come up in practice:

- **Requests vs limits**: requests drive scheduling and HPA utilisation; a memory limit kills the
  container (OOMKilled) when exceeded; CPU limits throttle and often hurt latency, so many teams set
  CPU requests but no CPU limit.
- **Graceful shutdown**: on termination Kubernetes sends SIGTERM and removes the pod from endpoints
  concurrently; the `preStop` sleep avoids sending new requests to a stopping pod. Enable Spring
  Boot's `server.shutdown=graceful` to drain in-flight requests.
- The **Gateway API** (`Gateway`, `HTTPRoute`) is the more expressive successor to Ingress
  (traffic splitting, header matching, role separation) and is supported by most controllers and
  meshes. The community ingress-nginx controller has been announced for retirement; check its
  status before choosing it for new clusters.
- HPA on CPU suits CPU-bound services; queue consumers scale better on lag (KEDA).

### Release strategies

| Strategy | How | Rollback | Cost / risk |
| --- | --- | --- | --- |
| Rolling update | Replace pods gradually | Roll forward/back gradually | Mixed versions run together; must be compatible |
| Blue-green | Full new environment (green) beside old (blue); switch traffic at once | Switch back instantly | Double capacity during the switch; DB schema must suit both |
| Canary | Send 1% → 5% → 25% → 100% to the new version, comparing metrics | Shift traffic back | Needs good metrics and traffic splitting |
| Shadow / dark launch | Mirror traffic to the new version, discard its responses | Nothing to roll back | Side effects must be suppressed |
| Feature flags | Deploy code dark, enable per user/cohort at runtime | Flip the flag | Flag debt; test both paths |

- **Automated canary analysis** (Argo Rollouts, Flagger, Spinnaker's Kayenta) compares error rate
  and latency of canary vs baseline and aborts automatically.
- **Database changes** must be backward compatible across versions that run simultaneously:
  **expand and contract** (add column → write both → backfill → read new → stop writing old →
  drop old), each step a separate deploy.
- **Feature flags** separate deploy from release: trunk-based development, kill switches for risky
  features, gradual rollout by cohort. Remove flags once rolled out.

### GitOps

The desired state of every environment lives in git (manifests, Helm charts, Kustomize overlays);
an in-cluster controller (Argo CD, Flux) **pulls** and continuously reconciles the cluster to it.
OpenGitOps summarises the principles: declarative, versioned and immutable, pulled automatically,
continuously reconciled.

Benefits: every change is a reviewed commit (audit trail), rollback is `git revert`, drift is
detected and corrected, and CI no longer needs cluster credentials. A typical flow: CI builds and
pushes the image, then opens a PR bumping the image tag in the environment repo; merge deploys.

---

## Service mesh

A service mesh moves cross-cutting network concerns (mTLS, retries, timeouts, traffic splitting,
telemetry) out of application code into infrastructure.

```text
 ┌──────── pod: orders ────────┐            ┌─────── pod: payments ───────┐
 │ ┌─────────┐    ┌──────────┐ │   mTLS     │ ┌──────────┐    ┌─────────┐ │
 │ │  app    │───►│  proxy   │─┼───────────►┼─│  proxy   │───►│  app    │ │
 │ └─────────┘    └────▲─────┘ │            │ └────▲─────┘    └─────────┘ │
 └─────────────────────┼───────┘            └──────┼──────────────────────┘
                       │ config, certs, telemetry  │
                 ┌─────┴───────────────────────────┴─────┐
                 │ control plane (istiod / Linkerd)      │
                 └───────────────────────────────────────┘
```

- **Data plane**: proxies that intercept all traffic. Istio uses Envoy; Linkerd uses its own
  lightweight Rust proxy (linkerd2-proxy).
- **Control plane**: distributes routing config, issues and rotates workload certificates, and
  collects telemetry.

What you get without code changes: **mTLS** between all workloads with automatic certificate
rotation and workload identity; **L7 metrics** (RED per service pair) and trace span propagation
(the app must still forward trace headers); **traffic management** (weighted routing, retries,
timeouts, outlier detection, fault injection); **authorization policies** by workload identity.

Istio traffic policy example: a 90/10 canary with timeouts, a retry policy, and outlier detection:

```yaml
apiVersion: networking.istio.io/v1
kind: VirtualService
metadata:
  name: payments
  namespace: shop
spec:
  hosts: [payments]
  http:
    - route:
        - destination: { host: payments, subset: v1 }
          weight: 90
        - destination: { host: payments, subset: v2 }
          weight: 10
      timeout: 2s
      retries:
        attempts: 2
        perTryTimeout: 600ms
        retryOn: connect-failure,refused-stream,unavailable,503
---
apiVersion: networking.istio.io/v1
kind: DestinationRule
metadata:
  name: payments
  namespace: shop
spec:
  host: payments
  trafficPolicy:
    connectionPool:
      http: { http1MaxPendingRequests: 100 }
    outlierDetection:                    # eject misbehaving pods (a per-host circuit breaker)
      consecutive5xxErrors: 5
      interval: 10s
      baseEjectionTime: 30s
      maxEjectionPercent: 50
  subsets:
    - name: v1
      labels: { version: v1 }
    - name: v2
      labels: { version: v2 }
---
apiVersion: security.istio.io/v1
kind: PeerAuthentication
metadata:
  name: default
  namespace: istio-system                # mesh-wide
spec:
  mtls: { mode: STRICT }                 # reject plaintext between workloads
---
apiVersion: security.istio.io/v1
kind: AuthorizationPolicy
metadata:
  name: payments-allow-orders
  namespace: shop
spec:
  selector:
    matchLabels: { app: payments }
  action: ALLOW
  rules:
    - from:
        - source: { principals: ["cluster.local/ns/shop/sa/orders"] }
      to:
        - operation: { methods: ["POST"], paths: ["/v1/authorisations"] }
```

Coordinate mesh retries with application retries; retrying in both multiplies attempts.

**Sidecar costs**: a proxy per pod adds memory and CPU across thousands of pods, a little latency
per hop, and operational complexity (sidecar injection, upgrades requiring pod restarts, startup
ordering).

**Istio ambient mode** (generally available since Istio 1.24, late 2024) removes sidecars: a
per-node **ztunnel** handles L4 (mTLS, identity, L4 authorization, telemetry), and optional
per-namespace or per-service-account **waypoint** proxies (Envoy) handle L7 features only where
needed. It lowers resource overhead and decouples proxy upgrades from application pods.

| | Istio (sidecar) | Istio (ambient) | Linkerd |
| --- | --- | --- | --- |
| Data plane | Envoy per pod | ztunnel per node + optional waypoints | linkerd2-proxy per pod |
| Feature breadth | Very broad | Broad (L7 via waypoints) | Focused: mTLS, metrics, retries, traffic split |
| Overhead | Highest | Lower | Low |
| Operational complexity | High | Medium | Lower |

When a mesh is worth it: many services in several languages, a zero-trust/mTLS requirement, the
need for uniform telemetry and traffic control. For a handful of services in one language, a good
client library (Resilience4j, OpenTelemetry) and Kubernetes NetworkPolicies are often enough.

---

## Security

### Zero trust

Do not trust the network: "inside the cluster" is not a credential (NIST SP 800-207). Every call
is authenticated and authorised based on identity, not IP address; least privilege everywhere;
assume breach and limit blast radius.

### mTLS and workload identity

- Every workload gets a cryptographic identity, typically a **SPIFFE ID** such as
  `spiffe://cluster.local/ns/shop/sa/orders` in a short-lived X.509 certificate (SVID), issued by
  the mesh or SPIRE and rotated automatically (hours, not years).
- Mutual TLS authenticates both sides and encrypts traffic; authorization policies then say
  "only `orders` may call `POST /v1/authorisations` on `payments`".
- Kubernetes **NetworkPolicies** add L3/L4 segmentation (default deny, allow by label).

### Propagating end-user identity

Service identity says which **service** is calling; requests also carry which **user** they are
for.

| Approach | How | Trade-offs |
| --- | --- | --- |
| Forward the user's access token | Each service validates the same JWT | Simple; but the token's audience is the edge API, every service sees a token that could be replayed elsewhere |
| Token exchange (RFC 8693) | Each hop exchanges for a token scoped to the next service's audience | Least privilege; extra calls to the auth server (cache) |
| Internal signed context token | The edge validates the external token and mints a short-lived internal token with user, tenant, scopes | Decouples internal services from external token formats; Netflix described this as "Passport" |
| Trusted headers (`X-User-Id`) | Gateway sets headers after authenticating | Only safe with mTLS and strict policies ensuring headers come from the gateway; easy to get wrong |

Whichever you pick: services still perform **object-level authorization** ("does this user's tenant
own order 42?"), and tokens never go into logs.

### Secrets

- Never in images, git, or plain environment files. Inject at runtime.
- **HashiCorp Vault** (including **dynamic secrets**: per-instance database credentials that
  expire), cloud secret managers (AWS Secrets Manager, GCP Secret Manager, Azure Key Vault),
  delivered via the External Secrets Operator or the Secrets Store CSI driver.
- Rotate automatically; prefer short-lived credentials and workload identity federation (pods
  assume cloud IAM roles) over long-lived keys.
- Encrypt Kubernetes Secrets at rest (KMS provider) and restrict who can read them.

### Supply chain

Signed images (Sigstore cosign), SBOMs, vulnerability scanning in CI, admission policies that only
admit signed images from your registry (Kyverno, OPA Gatekeeper), minimal base images.

---

## Testing

### The test pyramid for microservices

```text
            /\         end-to-end tests: few, slow, flaky; critical user journeys only
           /  \
          /────\       contract tests: every consumer/provider pair, fast, run in CI
         /      \
        /────────\     component tests: one service with real infra (Testcontainers),
       /          \    dependencies stubbed
      /────────────\   unit tests: domain logic, many and fast
```

Spotify's "testing honeycomb" argues that for small services **integration/component tests**
should dominate, since most bugs live in the interactions with databases, brokers, and other
services. Either way, the goal is high confidence **without** a shared, fragile end-to-end
environment that blocks every team's deploys.

### Contract testing with Pact

End-to-end tests are a poor way to check that services agree on an API. **Consumer-driven
contract testing**: the consumer writes tests against a mock provider; those interactions are
recorded as a **pact** (contract); the provider verifies it can satisfy every consumer's pact in
its own CI. A **Pact Broker** stores contracts and verification results, and `can-i-deploy` tells
you whether a given version is compatible with what is deployed in an environment.

Consumer side (order-service, JUnit 5, Pact JVM):

```java
import au.com.dius.pact.consumer.MockServer;
import au.com.dius.pact.consumer.dsl.PactDslJsonBody;
import au.com.dius.pact.consumer.dsl.PactDslWithProvider;
import au.com.dius.pact.consumer.junit5.PactConsumerTestExt;
import au.com.dius.pact.consumer.junit5.PactTestFor;
import au.com.dius.pact.core.model.V4Pact;
import au.com.dius.pact.core.model.annotations.Pact;
import java.util.Map;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;

import static org.junit.jupiter.api.Assertions.assertEquals;

@ExtendWith(PactConsumerTestExt.class)
@PactTestFor(providerName = "inventory-service")
class InventoryClientPactTest {

    @Pact(consumer = "order-service")
    V4Pact stockForSku(PactDslWithProvider builder) {
        return builder
                .given("SKU-42 has 7 units available")
                .uponReceiving("a request for the stock of SKU-42")
                    .path("/v1/stock/SKU-42")
                    .method("GET")
                .willRespondWith()
                    .status(200)
                    .headers(Map.of("Content-Type", "application/json"))
                    .body(new PactDslJsonBody()
                            .stringValue("sku", "SKU-42")
                            .integerType("available", 7))     // any integer matches
                .toPact(V4Pact.class);
    }

    @Test
    @PactTestFor(pactMethod = "stockForSku")
    void readsAvailableStock(MockServer mockServer) {
        InventoryClient client = new InventoryClient(mockServer.getUrl());
        assertEquals(7, client.available("SKU-42"));
    }
}
```

Provider side (inventory-service) replays the pact against the real service, with state handlers
setting up data:

```java
import au.com.dius.pact.provider.junit5.HttpTestTarget;
import au.com.dius.pact.provider.junit5.PactVerificationContext;
import au.com.dius.pact.provider.junit5.PactVerificationInvocationContextProvider;
import au.com.dius.pact.provider.junitsupport.Provider;
import au.com.dius.pact.provider.junitsupport.State;
import au.com.dius.pact.provider.junitsupport.loader.PactBroker;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.TestTemplate;
import org.junit.jupiter.api.extension.ExtendWith;

@Provider("inventory-service")
@PactBroker   // broker URL and credentials from system properties / environment
class InventoryProviderPactTest {

    @BeforeEach
    void target(PactVerificationContext context) {
        context.setTarget(new HttpTestTarget("localhost", 8080));   // the running service
    }

    @TestTemplate
    @ExtendWith(PactVerificationInvocationContextProvider.class)
    void verify(PactVerificationContext context) {
        context.verifyInteraction();
    }

    @State("SKU-42 has 7 units available")
    void sku42InStock() {
        TestData.stock("SKU-42", 7);
    }
}
```

Pact JVM's APIs have changed across major versions (V3 vs V4 pacts); check the docs for your
version. Alternatives: Spring Cloud Contract (provider-driven contracts generating stubs and
tests), schema compatibility checks for events (schema registry rules), and `buf breaking` for
protobuf.

### Testcontainers

Run real dependencies (PostgreSQL, Kafka, Redis) in throwaway Docker containers from tests,
instead of in-memory fakes that behave differently.

```java
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.testcontainers.service.connection.ServiceConnection;
import org.testcontainers.containers.PostgreSQLContainer;
import org.testcontainers.junit.jupiter.Container;
import org.testcontainers.junit.jupiter.Testcontainers;

import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest
@Testcontainers
class PlaceOrderServiceIT {

    @Container
    @ServiceConnection                       // Spring Boot 3.1+: wires the datasource automatically
    static PostgreSQLContainer<?> postgres = new PostgreSQLContainer<>("postgres:16-alpine");

    @Autowired PlaceOrderService service;
    @Autowired OutboxRepository outbox;

    @Test
    void placingAnOrderWritesAnOutboxEventInTheSameTransaction() throws Exception {
        Order order = new Order("ord_1", "cus_7");
        order.addLine("SKU-42", 2, 1_999);

        service.place(order);

        assertThat(outbox.unpublished())
                .singleElement()
                .satisfies(e -> assertThat(e.eventType()).isEqualTo("OrderPlaced"));
    }
}
```

Other useful practices: consumer tests for message handlers (feed a recorded event, assert on
effects), testing in production with canaries and synthetic monitoring, and chaos experiments for
resilience paths.

---

## Team topology and Conway's law

> "Any organization that designs a system (defined broadly) will produce a design whose structure
> is a copy of the organization's communication structure." — Melvin Conway (1968)

Architecture and organisation are the same decision. If three teams build a compiler, you get a
three-pass compiler. If one team owns five services, they will drift into a distributed monolith
that deploys together; if five teams share one service, it becomes a coordination bottleneck.

- **Inverse Conway maneuver**: shape the teams to match the architecture you want; the system will
  follow.
- **"You build it, you run it"** (Werner Vogels, Amazon): the owning team is on call for its
  services, which is what makes ownership real.
- **Team size**: Amazon's "two-pizza team"; a team should be able to hold its services in its
  head (cognitive load).

*Team Topologies* (Matthew Skelton and Manuel Pais) gives four team types and three interaction
modes:

| Team type | Role |
| --- | --- |
| Stream-aligned | Owns a flow of business change end to end (e.g. checkout); most teams |
| Platform | Provides self-service internal products (CI/CD, Kubernetes, observability) so stream teams move fast |
| Enabling | Specialists who help other teams adopt capabilities (e.g. performance, security), then step back |
| Complicated-subsystem | Owns a component needing deep specialist knowledge (pricing engine, video codec) |

Interaction modes: **collaboration** (working closely for a period, e.g. discovering a new
boundary), **X-as-a-Service** (consuming a platform with minimal coordination), **facilitating**
(an enabling team coaching). Microservices pay off when most interactions between stream-aligned
teams are X-as-a-Service through stable APIs.

---

## Common anti-patterns

| Anti-pattern | Symptoms | Fix |
| --- | --- | --- |
| **Distributed monolith** | Services must deploy together; one change touches five repos; shared DB; synchronous chains everywhere | Re-draw boundaries around capabilities; merge services that change together; async where possible |
| **Shared database** | Several services read/write the same tables | Database per service; APIs or events for access; CDC-fed replicas for reporting |
| **Chatty services** | One user request makes 50 sequential calls; latency adds up | Coarser APIs, batch endpoints, local read models, move the boundary |
| **Nano-services** | A service per function or per table; more deployment overhead than logic | Merge into capability-sized services |
| **Entity services** | `CustomerService`, `OrderService` as CRUD over tables, logic in an "orchestrator" | Put behaviour with data; services own capabilities, not tables |
| **Shared domain library** | A common JAR with domain models every service must upgrade in lockstep | Share only technical utilities; duplicate small models per context |
| **Synchronous everything** | Availability is the product of a long chain | Async events, local caches/replicas, fallbacks |
| **Smart pipes** (ESB) | Business logic in the integration layer | "Smart endpoints, dumb pipes": brokers only transport |
| **Missing platform** | Each team reinvents CI/CD, logging, and alerting | Platform team, paved road, templates |
| **No observability** | Incidents take hours to localise | Tracing, RED metrics, correlated logs before splitting further |
| **Big-bang rewrite** | Years without delivering; old and new diverge | Strangler fig, incremental slices |
| **Microservices first** | Small team, unclear domain, boundaries keep moving | Modular monolith until the domain and team structure stabilise |

---

## Interview questions

1. **When would you choose microservices over a monolith?**
   When multiple teams need to deploy independently, parts have very different scaling or
   reliability needs, or fault/compliance isolation is required, and the domain boundaries are
   understood. Otherwise start with a modular monolith with enforced boundaries; extracting
   services later is easier from clean modules.

2. **How do you decide service boundaries?**
   Around business capabilities and DDD bounded contexts, where a model and language are
   consistent; keep aggregates whole; aim for changes that touch one service; align with team
   ownership; avoid entity services and layer-based splits; merge services that always change
   together.

3. **How do you keep data consistent across services without distributed transactions?**
   Sagas: local transactions with compensations, orchestrated (explicit coordinator, persisted
   state) or choreographed (events). The transactional outbox publishes events reliably, consumers
   are idempotent, and semantic locks handle the lack of isolation.

4. **Explain the outbox pattern and why it is needed.**
   It solves the dual write: the event row is inserted in the same DB transaction as the state
   change; a relay (polling with `SKIP LOCKED` or CDC like Debezium) publishes it. Delivery is at
   least once, so consumers deduplicate by event ID.

5. **Walk through a circuit breaker.**
   Closed: calls pass and outcomes are recorded in a sliding window. When failure or slow-call
   rate exceeds the threshold over a minimum number of calls, it opens and fails fast for a wait
   duration. Then half-open permits a few trial calls; success closes it, failure re-opens it.
   Ignore business errors; one breaker per dependency; combine with timeouts and fallbacks.

6. **What causes retry storms and how do you prevent them?**
   Retries at every layer multiply (3 attempts across 4 layers is 81x at the bottom), and
   synchronised retries hit a recovering service together. Retry at one layer, only idempotent
   requests on retryable errors, exponential backoff with jitter, retry budgets, respect
   deadlines and `Retry-After`, and circuit breakers.

7. **How do timeouts work across a call chain?**
   Each request has an end-to-end deadline propagated to downstream calls (gRPC does it natively);
   each hop uses the remaining budget, inner timeouts must be shorter than outer ones, and servers
   drop work whose deadline has passed.

8. **How do you query data that spans services?**
   API composition for simple, small joins; otherwise a CQRS read model built from events. Avoid
   reading other services' databases.

9. **How does service discovery work in Kubernetes?**
   A Service selects ready pods by label; EndpointSlices track their IPs; DNS resolves
   `name.namespace.svc.cluster.local` to a ClusterIP; kube-proxy balances connections. Headless
   Services return pod IPs for client-side balancing (needed for gRPC).

10. **Liveness vs readiness probes?**
    Liveness failing restarts the container and must not depend on downstreams; readiness failing
    removes the pod from load balancing (warm-up, overload). Startup probes protect slow starters.

11. **What does a service mesh give you, and what does it cost?**
    mTLS with workload identity, uniform L7 telemetry, traffic splitting, retries and timeouts,
    and authorization policies without code changes. Costs: per-pod proxy resources, latency,
    operational complexity, and double retries if apps retry too. Ambient mode reduces overhead by
    moving L4 to per-node ztunnels.

12. **How do you trace a request across services?**
    OpenTelemetry instrumentation creates spans; W3C `traceparent` headers (and message headers
    for Kafka) propagate context; a Collector exports to a backend; logs include trace IDs; tail
    sampling keeps errors and slow traces.

13. **What is contract testing and why prefer it over end-to-end tests?**
    Consumers record expectations as contracts; providers verify them in CI; a broker answers
    "can I deploy this version?". It catches API incompatibilities quickly and independently per
    team, without a shared, flaky end-to-end environment.

14. **What is a distributed monolith?**
    Services that cannot be deployed independently: shared databases, lockstep releases, shared
    domain libraries, deep synchronous chains. All the costs of distribution with none of the
    benefits.

15. **What is Conway's law and how does it affect microservices?**
    Systems mirror the communication structure of the organisation. Align service ownership with
    stream-aligned teams, use the inverse Conway maneuver to shape teams for the target
    architecture, and give teams end-to-end ownership including on-call.

---

## Summary

- Microservices buy **independent deployability, scaling, and team autonomy**, and cost
  distributed-systems complexity and platform investment. Default to a **modular monolith**;
  extract services for concrete reasons, via the **strangler fig**.
- Draw boundaries around **business capabilities and bounded contexts**; keep aggregates whole;
  use context-mapping patterns (especially anticorruption layers) between contexts.
- Communicate **synchronously** (REST/gRPC) when someone waits for an answer, **asynchronously**
  (events) to decouple; design for at-least-once delivery and idempotent consumers.
- **Database per service**; consistency via **sagas** (prefer orchestration for real business
  processes), **outbox** for reliable events, **CQRS** read models or **API composition** for
  cross-service queries, **event sourcing** only where history is the domain.
- Resilience is mandatory: **timeouts with deadline propagation**, **careful retries** with jitter
  and budgets, **circuit breakers**, **bulkheads**, **fallbacks**, **load shedding**, verified by
  **chaos experiments**.
- Observability: structured logs with trace IDs, **RED/USE** metrics, **OpenTelemetry** tracing,
  **SLOs and error budgets** with burn-rate alerts.
- Deploy immutable containers on Kubernetes with proper probes, graceful shutdown, HPA; release by
  **canary** or blue-green with feature flags; manage environments with **GitOps**.
- A **service mesh** provides mTLS, telemetry, and traffic policy uniformly; weigh its cost.
- **Zero trust**: workload identity and mTLS, scoped user-token propagation, managed secrets.
- Test with unit and component tests (**Testcontainers**) and **consumer-driven contracts**
  (Pact) rather than large end-to-end suites.
- Architecture follows organisation (**Conway's law**): stream-aligned teams owning services end
  to end, supported by a platform team.

---

## Further reading

- Sam Newman, *Building Microservices* (2nd edition, O'Reilly, 2021).
- Sam Newman, *Monolith to Microservices* (O'Reilly, 2019).
- Chris Richardson, *Microservices Patterns* (Manning, 2018), and microservices.io.
- Michael T. Nygard, *Release It! Design and Deploy Production-Ready Software* (2nd edition,
  Pragmatic Bookshelf, 2018).
- Eric Evans, *Domain-Driven Design: Tackling Complexity in the Heart of Software* (2003).
- Vaughn Vernon, *Implementing Domain-Driven Design* (2013).
- Martin Fowler and James Lewis, "Microservices" (martinfowler.com, 2014); Martin Fowler,
  "StranglerFigApplication" and "MonolithFirst".
- Matthew Skelton and Manuel Pais, *Team Topologies* (2019).
- Melvin E. Conway, "How Do Committees Invent?" (Datamation, 1968).
- Betsy Beyer et al., *Site Reliability Engineering* and *The Site Reliability Workbook* (Google,
  O'Reilly), especially the chapters on SLOs, alerting on SLOs, and handling overload.
- Martin Kleppmann, *Designing Data-Intensive Applications* (O'Reilly), for messaging, consistency,
  and event logs.
- Hector Garcia-Molina and Kenneth Salem, "Sagas" (SIGMOD 1987).
- OpenTelemetry documentation and the W3C Trace Context recommendation.
- Kubernetes documentation (Services, probes, Deployments, HPA, Gateway API).
- Istio and Linkerd documentation, including Istio ambient mode.
- Resilience4j documentation; Pact documentation (docs.pact.io); Testcontainers documentation.
- NIST SP 800-207, *Zero Trust Architecture*; SPIFFE specification.
- Segment Engineering, "Goodbye Microservices: From 100s of problem children to 1 superstar"
  (2018); Prime Video Tech, "Scaling up the Prime Video audio/video monitoring service and reducing
  costs by 90%" (2023).
