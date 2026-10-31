---
title: "Message queues"
order: 7
---
# Message queues, logs, and event-driven systems

## Key points

- Async messaging decouples producers and consumers in **time**, **load**, and **failure**; you pay with **eventual consistency**, ops complexity, and **at-least-once** delivery unless you design for effective-once.
- **Queue** = one consumer, delete on ack; **log** = retained, per-group offsets, replay; **pub/sub** = fan-out (implemented on queues or logs).
- **At-least-once + idempotent consumers** is the default production pattern; Kafka **EOS** covers Kafka→Kafka only.
- **Kafka durability**: RF=3, `acks=all`, `min.insync.replicas=2`, idempotent producer, manual offset commit after processing.
- **Reliability patterns**: transactional **outbox**, **inbox** / processed-message table, **retry topics**, **DLQ**, bounded retries.

---

## Why async messaging

```text
Sync:  Client → A → B → C     latency adds, availability multiplies
Async: Client → A → [broker] → B, C, D consume independently
```

| Benefit | Meaning |
| --- | --- |
| Temporal decoupling | Producer and consumer need not be up together |
| Load levelling | Bursts buffered; workers drain at sustainable rate |
| Fan-out | One event → many independent consumers |
| Retry isolation | Failed work retried without blocking the caller |

| Cost | Meaning |
| --- | --- |
| Eventual consistency | Intermediate states while messages are in flight |
| Duplicates / reordering | Consumers must handle both |
| Debugging | Correlation IDs and distributed tracing required |

Use sync when the caller needs an **immediate** authoritative answer (auth, price quote).

---

## Queue vs log vs pub/sub

### Message queue (point-to-point)

Each message goes to **one** competing consumer; **deleted** after ack. Fine-grained retry; **no replay** after ack.

```text
[m6…m1] ──┬── worker1 (m1, m4)
          ├── worker2 (m2, m5)
          └── worker3 (m3, m6)
```

Examples: RabbitMQ classic/quorum, SQS, ActiveMQ.

### Log (append-only, retained)

Records **appended** and **kept** until retention. Each **consumer group** tracks its own **offset**; reading does not delete.

```text
offset:  0  1  2  3  4  5  6  7
        [e0][e1][e2][e3][e4][e5][e6][e7]  ← append
              ▲           ▲
         group A       group B (independent offsets)
```

Strengths: replay, many readers, high throughput (sequential I/O). Weakness: parallelism = **partitions**; one stuck message blocks its partition unless moved aside.

Examples: Kafka, Pulsar, Kinesis, Redpanda.

### Publish/subscribe

One publish → **every subscription** (each may have competing consumers). Built on fanout exchanges + per-subscriber queues, or Kafka consumer groups.

| | Queue | Log | Pub/sub |
| --- | --- | --- | --- |
| Delivered to | One consumer | One consumer per partition per group | Every subscription |
| After consume | Deleted (usually) | Retained | Varies |
| Replay | No | Yes (log-based) | Only if log-backed |
| Ordering | Per queue (approx) | Per partition | Per underlying unit |

---

## Delivery semantics

| Guarantee | Meaning | Typical mechanism |
| --- | --- | --- |
| At-most-once | 0 or 1 processing; loss possible | Commit/ack **before** process |
| At-least-once | ≥1 processing; duplicates possible | Ack **after** process; producer retries |
| Exactly-once (effect) | Side effect once | At-least-once + **idempotency**, or atomic offset + output |

```text
Commit BEFORE process → crash after commit, before work → LOSS (at-most-once)
Commit AFTER process  → crash after work, before commit → DUPLICATE (at-least-once)
Same DB txn: apply + store offset → effective-once
```

**Traps**

- Kafka `enable.auto.commit=true` with async processing: offsets committed before work finishes → **loss**.
- SQS/Rabbit **visibility/ack timeout** shorter than processing → **two consumers** process the same message concurrently.

Producer retries without idempotence → duplicate records. **Idempotent producer** (PID + sequence per partition) dedupes broker-side retries for one producer session.

---

## Kafka semantics (core)

### Topics, partitions, records

- **Topic** → **partitions** (ordered append-only logs).
- **Key** → same key → same partition (`hash(key) % n`); **null key** → sticky batching, no per-key order.
- **Record**: key, value, headers, **offset** (position in partition).

```text
Topic orders, RF=3, 3 brokers — each partition has one leader + followers
```

**Partition count**: max consumer parallelism in a group = partition count. **Cannot shrink** partitions; **increasing** rehashes keys → breaks per-key ordering across the change.

### Offsets, HW, LEO

```text
offsets:  0 … 6  7  8
          [committed data][HW gap?][LEO]
```

- **LEO**: next offset to write on leader.
- **High watermark (HW)**: min LEO across **ISR**; consumers read only below HW → no uncommitted data on failover.
- **Committed offset** (per group, partition): **next** offset to read (`__consumer_offsets`).

### Replication and ISR

Followers **fetch** like consumers. **ISR** = leader + followers caught up within `replica.lag.time.max.ms`.

- Write durable when all **current ISR** have it (`acks=all`).
- **`min.insync.replicas`**: with `acks=all`, reject writes if ISR size < min → choose CP over AP when ISR shrinks to leader alone.
- **Unclean leader election** (default off): out-of-sync replica as leader → **data loss** but availability.

| `acks` | Ack when | Risk |
| --- | --- | --- |
| `0` | Immediately | Total loss possible |
| `1` | Leader log | Leader dies before replication |
| `all` | All ISR members | Safe if min.insync.replicas aligned with RF |

Standard durable setting: **RF=3, min.insync.replicas=2, acks=all, enable.idempotence=true**.

### Idempotent producer

Broker assigns **PID + epoch**; batches carry **sequence numbers** per partition. Duplicate sequence → success without duplicate write; gap → `OutOfOrderSequenceException`. Scope: **one session, per partition**; restart → new PID → use **transactional.id** or app-level dedup.

### Transactions (EOS within Kafka)

1. `initTransactions()` — coordinator, epoch, **fences** zombies.
2. `beginTransaction()` → produce → `sendOffsetsToTransaction()` → `commitTransaction()`.
3. Consumers: **`isolation.level=read_committed`** — read up to **LSO**, skip aborted batches.

```text
input topic → [ consume → transform → produce ] → output topic
                    └── consumer offsets in SAME transaction
```

**Limits**: DB/HTTP side effects **not** in the transaction — make them idempotent. Latency cost → commit on batch interval, not every record.

### Consumer groups and rebalancing

- Same `group.id` → each partition assigned to **exactly one** consumer in the group.
- Rebalance on join/leave/crash/subscription change.
- **Eager**: revoke all → stop-the-world.
- **Cooperative sticky**: only moved partitions pause.
- **Static membership** (`group.instance.id`): rolling restart without rebalance if return within session timeout.
- **`max.poll.interval.ms`**: processing too long without `poll()` → member removed → rebalance storm.

### Offset commit strategies

| Strategy | Guarantee | Notes |
| --- | --- | --- |
| Auto-commit | Risky at-most-once | Commits on poll, not on finish |
| Sync after batch | At-least-once | Simple; duplicates up to one batch |
| Offsets in DB + same txn as effect | Effective-once | `seek()` on assignment |
| `sendOffsetsToTransaction` | Kafka EOS | Output must be Kafka |

Committed value = **last processed offset + 1**. Commit on **`onPartitionsRevoked`** before reassignment.

### Retention and compaction

- **`delete`**: time/size retention (default 7d).
- **`compact`**: latest record per key; **tombstone** (null value) deletes key.
- Compacted topics: changelog for `__consumer_offsets`, Streams state, **event-carried state transfer**.

### Ordering

- Order **within a partition** only; **per-key order** if key stable and partition count unchanged.
- Parallel threads on one partition break order unless per-key routing.

### Hot partitions

| Key | Risk |
| --- | --- |
| `orderId` | Usually good spread |
| `customerId` | Whale customer → hot partition |
| `country` | Low cardinality → severe skew |

Mitigations: finer key, **salt** (sacrifices order), dedicated partitioner, monitor **bytes-in per partition**.

---

## RabbitMQ and SQS (essentials)

### RabbitMQ

Publish → **exchange** → **bindings** → **queues** → consumers.

| Exchange | Routing |
| --- | --- |
| direct | Routing key = binding key |
| topic | Patterns (`*`, `#`) |
| fanout | All bound queues |

Use **quorum queues** for durability (Raft). **Manual ack** after process; **prefetch** caps unacked per consumer. **Publisher confirms** for durability. **DLX** + TTL for delayed retry; **`x-delivery-limit`** on quorum queues.

### Amazon SQS

| | Standard | FIFO |
| --- | --- | --- |
| Delivery | At-least-once | Dedup window (~5 min) |
| Order | Best effort | Per **MessageGroupId** |
| Throughput | Very high | Lower (groups serialize) |

**Visibility timeout**: message hidden until deleted or timeout → redelivered. Set **> p99 processing**; extend with heartbeat. **Long polling** (`WaitTimeSeconds` 1–20). **Redrive policy** → DLQ after `maxReceiveCount`. **SNS → SQS** per subscriber for fan-out.

---

## Broker comparison (when to choose)

| Need | Choice |
| --- | --- |
| Replay, many consumers, CDC, streaming | Kafka (or Pulsar) |
| Task queue, routing, per-message retry | RabbitMQ quorum / SQS |
| AWS zero-ops work queue | SQS (+ SNS fan-out) |
| DB + event atomically | Outbox (+ CDC relay) |

| | Kafka | RabbitMQ | SQS |
| --- | --- | --- | --- |
| Model | Partitioned log | Exchange + queue | Managed queue |
| Replay | Yes | Streams only | No |
| Parallelism | ≤ partitions per group | Competing consumers | Unlimited |
| Per-message ack | Offsets (by convention DLQ) | Built-in + DLX | Built-in + redrive |

---

## Idempotent consumers

At-least-once ⇒ duplicates are normal.

| Strategy | How |
| --- | --- |
| Natural idempotency | `SET status = X`, upsert, conditional `UPDATE … WHERE status = pending` |
| Processed-message table | `INSERT message_id` **same txn** as effect; PK dedupes |
| Version check | Apply only if `event.version = row.version + 1` |
| Dedup cache (Redis) | Not atomic with DB — weaker |

Prefer **producer-assigned message id** in headers; fallback `topic-partition-offset` only dedupes **redelivery of same record**, not producer retries (new offset).

```sql
-- Same transaction as business write
INSERT INTO processed_messages (consumer, message_id) VALUES (?, ?)
ON CONFLICT DO NOTHING;
-- if 0 rows affected → duplicate, skip
```

---

## Retries, DLQ, poison messages

| Failure | Action |
| --- | --- |
| Transient (503, timeout) | Backoff + jitter, retry |
| Permanent (bad JSON, invalid) | DLQ immediately, no retry loop |
| Dependency down | Pause consumption / circuit break |

**Poison message**: always fails — in a queue, infinite redelivery; in Kafka, **blocks partition**. Bound attempts; move to DLQ with context (topic, partition, offset, error).

### Kafka retry topics

```text
orders → main consumer ─fail→ orders-retry-1m → … → orders-dlq
         (main partition never blocked; per-key order may break on retry)
```

Pause partition until retry time (`consumer.pause()`), don't sleep inside `poll()` past `max.poll.interval.ms`.

---

## Transactional outbox

**Dual-write problem**: DB commit and broker send are two systems — crash between → inconsistency.

**Pattern**: insert event into **outbox table** in the **same DB transaction** as state change; separate **relay** publishes.

```text
@Transactional: save order + INSERT outbox
Relay: polling (FOR UPDATE SKIP LOCKED) or CDC (Debezium from WAL)
```

CDC guarantees: **at-least-once** from relay — consumers dedupe on event id. Watch **replication slot lag** (WAL retention).

---

## Inbox pattern

On receive: `INSERT inbox (message_id) ON CONFLICT DO NOTHING` → fast ack → local worker processes. Combines dedup + durable local queue.

---

## Event-driven patterns (short)

| Pattern | Payload | Coupling |
| --- | --- | --- |
| Event notification | "something happened" + id | Consumers call back |
| Event-carried state transfer | Full state needed | Local copy, no callback |
| Event sourcing | Events = source of truth | Replay, schema evolution forever |
| CQRS | Write model + read projections | Read lag; read-your-writes UX |

---

## Schema evolution (Kafka)

| Format | Wire | Evolution |
| --- | --- | --- |
| JSON | Verbose | Convention only |
| Avro | Compact | Schema Registry; defaults |
| Protobuf | Compact | Field numbers never reused |

Registry **compatibility**: `BACKWARD` (add fields with defaults → upgrade consumers first), `FORWARD`, `FULL`, transitive modes for long retention replay.

---

## Backpressure and lag

- Kafka: pull-based; backlog on disk; `pause()`/`resume()` when downstream saturated; producer blocked by `buffer.memory`.
- **Lag** = LEO − committed offset; also track **time lag** (age of oldest unprocessed).
- Single hot partition lag → hot key, poison message, or slow instance — not always "add consumers" (cap = partition count).

---

## Pitfalls

- `acks=1` or `min.insync.replicas=1` on important topics.
- Auto-commit + async processing (silent loss).
- Non-idempotent consumers under at-least-once.
- DB then Kafka without outbox.
- Repartitioning keyed topics (breaks per-key order).
- Hot keys / low-cardinality keys.
- Processing exceeds `max.poll.interval.ms`.
- Unbounded retries on poison messages.
- SQS visibility timeout too short (concurrent duplicates).
- Assuming Kafka EOS covers DB or HTTP.
- Unmonitored Debezium replication slot (disk full).

---

## Quick check

1. **Queue vs log** — When do you pick each, and what does replay imply for consumers?
2. **ISR and HW** — Why can consumers only read below the high watermark?
3. **At-least-once vs effective-once** — What must the consumer do? What does Kafka EOS *not* cover?
4. **Outbox** — Why not "save order then `kafka.send`" in one `@Transactional` method?
5. **Poison message in Kafka** — Why does it block a partition, and what pattern avoids blocking the main consumer?
