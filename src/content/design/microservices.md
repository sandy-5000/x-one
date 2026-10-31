---
title: "Microservices"
order: 10
---
# Microservices

## Monolith vs microservices

**Monolith:** one deployable application containing every feature, usually one database.

**Microservices:** many small services, each owning one business capability and its own data, communicating over the network.

```text
Monolith                         Microservices

┌──────────────────────┐         ┌───────┐  ┌────────┐  ┌──────────┐
│ users | orders |     │         │ Users │  │ Orders │  │ Payments │
│ payments | search    │         └───┬───┘  └───┬────┘  └────┬─────┘
└──────────┬───────────┘             │          │            │
           │                       ┌─▼─┐      ┌─▼─┐        ┌─▼─┐
      ┌────▼────┐                  │DB │      │DB │        │DB │
      │   DB    │                  └───┘      └───┘        └───┘
      └─────────┘
```

| | Monolith | Microservices |
| --- | --- | --- |
| Deployment | One unit | Each service independently |
| Scaling | Whole app | Per service |
| Tech stack | One | Can differ per service |
| Local calls | In-process, fast | Network: latency, failures |
| Transactions | Simple ACID | Sagas, eventual consistency |
| Debugging | One log, one stack trace | Distributed tracing needed |
| Team fit | Small team | Many teams owning services |

**Start with a well-structured (modular) monolith.** Split out services when a team, scaling, or deployment need actually demands it.

---

## Core building blocks

### API gateway

One entry point for clients. Handles routing, authentication, rate limiting, TLS, request aggregation. Examples: Kong, AWS API Gateway, Spring Cloud Gateway, Envoy.

```text
mobile / web ──► API Gateway ──┬──► user-service
                               ├──► order-service
                               └──► search-service
```

**Backend for Frontend (BFF):** a separate gateway per client type (mobile, web) shaped to its needs.

### Service discovery

Instances come and go, so services find each other through a registry instead of hard-coded IPs.

- **Client-side:** the caller asks the registry (Eureka, Consul) and picks an instance.
- **Server-side:** the caller hits a load balancer / DNS name; the platform routes. Kubernetes Services work this way.

### Communication

- **Synchronous** — REST or gRPC. Simple, but the caller is blocked and coupled to the callee's availability.
- **Asynchronous** — events through Kafka/RabbitMQ. Loosely coupled and resilient, but eventually consistent and harder to trace.

### Database per service

Each service owns its data; others access it only through the service's API or events. This keeps services independently deployable — but cross-service queries need API composition or a read model built from events (CQRS).

---

## Resilience patterns

Every network call can be slow or fail. Without protection, one slow service exhausts threads in all its callers — a **cascading failure**.

### Timeouts

Never call another service without a timeout. A missing timeout means waiting forever.

### Retries with backoff and jitter

Retry only idempotent operations, a limited number of times, with growing delays: 100 ms, 200 ms, 400 ms… plus randomness so clients don't retry in lockstep.

### Circuit breaker

Stop calling a service that keeps failing; fail fast instead.

```text
          failures > threshold
 CLOSED ──────────────────────► OPEN
   ▲                              │
   │ trial call succeeds          │ after cool-down
   │                              ▼
   └──────────────────────── HALF-OPEN
          trial call fails ──► back to OPEN
```

```java
// Resilience4j
CircuitBreaker cb = CircuitBreaker.of("payments", CircuitBreakerConfig.custom()
        .failureRateThreshold(50)                       // open at 50% failures
        .waitDurationInOpenState(Duration.ofSeconds(30))
        .slidingWindowSize(20)
        .build());

Supplier<Receipt> call = CircuitBreaker.decorateSupplier(cb, () -> paymentClient.charge(order));

Receipt receipt = Try.ofSupplier(call)
        .recover(ex -> Receipt.pending(order))          // fallback
        .get();
```

### Bulkhead

Isolate resources (separate thread pools or connection pools per downstream) so one failing dependency can't sink the whole service — like watertight compartments in a ship.

### Fallbacks

Return cached data, a default, or a degraded response ("recommendations unavailable") instead of an error page.

---

## Data patterns

- **Saga** — a chain of local transactions with compensating actions (see the CAP and Consistency page).
- **Outbox** — write the event to an outbox table in the same transaction as the data change (see Message Queues).
- **CQRS** — separate the write model (commands) from read models (queries) optimised for each screen.
- **Event sourcing** — store every change as an event; current state is a replay of events. Full audit history, but more complex.

---

## Observability

You can't attach a debugger to 40 services, so build in visibility.

- **Logs** — structured (JSON), centralised (ELK, Loki), always including a `traceId`.
- **Metrics** — request rate, error rate, latency percentiles (p50, p95, p99), saturation. Prometheus + Grafana.
- **Traces** — follow one request across services. OpenTelemetry, Jaeger, Zipkin.

```text
trace 9f2c ─┬─ gateway          12 ms
            ├─ order-service    85 ms
            │   ├─ db query     20 ms
            │   └─ payment-svc  60 ms   ◄── slow span
            └─ notify (async)
```

**Health endpoints** (`/health/live`, `/health/ready`) let orchestrators restart dead instances and stop routing to ones that aren't ready.

---

## Deployment

- **Containers** (Docker) package each service with its dependencies.
- **Orchestration** (Kubernetes, Docker Swarm) handles scheduling, scaling, restarts, and service discovery.
- **Blue-green deployment** — run the new version beside the old one, switch traffic at once, switch back if broken.
- **Canary release** — send 1% → 10% → 100% of traffic to the new version while watching metrics.
- **Feature flags** — ship code dark and enable it at runtime.
- **Service mesh** (Istio, Linkerd) — a sidecar proxy next to each service handles mTLS, retries, and telemetry outside the application code.
