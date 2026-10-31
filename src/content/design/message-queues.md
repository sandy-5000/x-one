---
title: "Message queues"
order: 7
---
# Message Queues, Logs and Event-Driven Systems

These notes cover asynchronous messaging from first principles to broker internals: what a broker
actually guarantees, how Kafka, RabbitMQ, and SQS work underneath, how to write producers and
consumers that do not lose or duplicate work, and the patterns (outbox, inbox, retry topics, event
sourcing, CQRS) built on top. Code is Java 17+; configs are for Apache Kafka 3.x/4.x unless noted.

---

## Why asynchronous messaging

In a synchronous call chain, the caller waits for the callee, and the callee's failures and
latency become the caller's.

```text
 Synchronous: availability multiplies, latency adds

 Client ─► Order ─► Payment ─► Inventory ─► Email
           99.9%    99.9%      99.9%        99.9%     overall ≈ 99.6%, latency = sum

 Asynchronous: the request path stops at the broker

 Client ─► Order ─► [ broker ] ─► Payment / Inventory / Email consume at their own pace
```

What a broker buys you:

| Benefit | What it means | Example |
| --- | --- | --- |
| **Temporal decoupling** | Producer and consumer do not need to be up at the same time | Email service deploys without failing checkouts |
| **Load levelling** | Bursts are buffered and drained at a sustainable rate | Black Friday: 50k orders/min queued, workers process 10k/min |
| **Lower latency for the user** | Respond "accepted" and do slow work later | Video upload returns immediately; transcoding is async |
| **Fan-out** | One event, many independent consumers | `OrderPlaced` → billing, inventory, analytics, search |
| **Retry and isolation** | Failed work is retried without the producer knowing | Payment provider down for 5 min; messages wait |
| **Scaling** | Add consumers to process faster | Autoscale workers on queue depth |

What it costs:

- **Eventual consistency**: the system is in an intermediate state while messages are in flight.
- **Operational complexity**: one more distributed system to run, monitor, and upgrade.
- **Harder debugging**: no single stack trace; you need correlation ids and tracing.
- **Duplicates and reordering** must be handled by consumers.
- No immediate answer: if the user needs the result now (authorization, price quote), a
  synchronous call is often the right choice.

---

## Queue vs log vs pub/sub

Three models are often mixed up because many products support more than one.

### Message queue (point-to-point)

Each message is delivered to **one** consumer among competing consumers. Once acknowledged, it is
**deleted**. Unacknowledged messages are redelivered.

```text
                       ┌──► worker 1   gets m1, m4
 [m6 m5 m4 m3 m2 m1] ──┼──► worker 2   gets m2, m5
                       └──► worker 3   gets m3, m6
```

Per-message acknowledgement allows fine-grained retry and parallelism, but ordering across
competing consumers is lost, and there is no replay once a message is acked. Examples: RabbitMQ
classic and quorum queues, SQS, ActiveMQ.

### Log (append-only, retained)

Messages are appended to an ordered, durable log and **kept** for a retention period regardless
of consumption. Each consumer group tracks its own **position (offset)**. Consumption does not
delete anything.

```text
 offset:  0    1    2    3    4    5    6    7
         [e0] [e1] [e2] [e3] [e4] [e5] [e6] [e7]  ◄── producers append here
                     ▲              ▲
          analytics group      billing group
          (offset 2, slow)     (offset 5)
```

Strengths: replay (reset an offset), many independent readers at no extra storage cost, strict
order within a partition, very high throughput through sequential I/O. Weakness: the unit of
parallelism is the partition, and a message that cannot be processed blocks its partition unless
you move it elsewhere. Examples: Kafka, Pulsar (also does queues), Kinesis, Redpanda, RabbitMQ
Streams.

### Publish/subscribe

A message published to a **topic** is delivered to **every subscription**. Inside each
subscription there may be competing consumers. Pub/sub describes **fan-out semantics**, and is
implemented on top of either queues (SNS → one SQS queue per subscriber, RabbitMQ fanout exchange
→ one queue per subscriber) or logs (Kafka: one consumer group per subscriber).

```text
                           ┌──► subscription "billing"   ──► 3 competing consumers
 publish OrderPlaced ──────┼──► subscription "inventory" ──► 2 competing consumers
                           └──► subscription "analytics" ──► 1 consumer
```

| | Queue | Log | Pub/sub |
| --- | --- | --- | --- |
| Delivered to | One consumer | Every group (one consumer per partition in each group) | Every subscription |
| After ack | Deleted | Kept until retention | Depends on implementation |
| Replay | No | Yes | Only if built on a log |
| Ordering | Approximate (per queue, single consumer) | Per partition | Per underlying queue/partition |
| Ack granularity | Per message | Per offset (cumulative) | Per message or offset |

---

## Delivery semantics

### The three guarantees

| Guarantee | Meaning | How it is achieved |
| --- | --- | --- |
| **At-most-once** | Each message is processed 0 or 1 times; loss possible | Ack/commit **before** processing; producer does not retry |
| **At-least-once** | Processed 1 or more times; duplicates possible | Ack/commit **after** processing; producer retries |
| **Exactly-once (effectively-once)** | The **effect** happens once | At-least-once + idempotency, or atomic commit of output and input position |

At-least-once with idempotent consumers is the default design for almost every system.

### Precise failure scenarios

Each guarantee is defined by what happens at each crash point. Producer side:

| Failure | Without retries | With retries, non-idempotent producer | With idempotent producer |
| --- | --- | --- | --- |
| Request lost before reaching broker | Message lost | Resent, written once | Written once |
| Broker wrote it, ack lost | Producer thinks it failed (caller may retry) | **Duplicate** written | Broker sees same producer id + sequence, discards duplicate |
| Broker leader crashed before followers copied it (acks=1) | Lost although acked | Lost although acked | Lost although acked; idempotence does not fix weak acks |
| Producer process crashes with records in its buffer | Lost | Lost | Lost; use an outbox if the source is a DB |

Consumer side:

```text
 Commit BEFORE processing (at-most-once)
   poll ─► commit offset 10 ─► process m10 ──✗ crash
   restart at 11: m10 never processed                          → LOSS

 Commit AFTER processing (at-least-once)
   poll ─► process m10 (DB updated) ──✗ crash before commit
   restart at 10: m10 processed again                          → DUPLICATE

 Atomic commit (exactly-once effect)
   poll ─► BEGIN; apply m10; store offset 11 in same DB / Kafka transaction; COMMIT
   crash anywhere: either both happened or neither             → ONCE
```

Two more classic traps:

- **Auto-commit with async processing**: Kafka's `enable.auto.commit=true` commits offsets
  returned by the previous `poll()` during later polls. If you hand records to a thread pool and
  poll again, offsets can be committed before processing finishes, and a crash loses messages.
- **Visibility/ack timeout shorter than processing time**: SQS or RabbitMQ redelivers the message
  to another consumer while the first is still working, so it is processed twice concurrently.

---

## Kafka internals

### Topics, partitions and records

A **topic** is a named stream split into **partitions**. Each partition is an ordered,
append-only log stored on one **leader** broker and replicated to followers. A **record** has a
key, value, timestamp, headers, and gets an **offset**, its position in the partition.

```text
 Topic "orders", 3 partitions, replication factor 3, 3 brokers

 ┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
 │ Broker 1         │   │ Broker 2         │   │ Broker 3         │
 │ orders-0  LEADER │   │ orders-0  follow │   │ orders-0  follow │
 │ orders-1  follow │   │ orders-1  LEADER │   │ orders-1  follow │
 │ orders-2  follow │   │ orders-2  follow │   │ orders-2  LEADER │
 └──────────────────┘   └──────────────────┘   └──────────────────┘
```

**Partitioning**: with a key, the default partitioner uses `murmur2(keyBytes) % numPartitions`,
so the same key always lands in the same partition (while the partition count is unchanged). With
a null key, the producer uses a **sticky** strategy: it fills a batch for one partition, then
switches, which gives larger batches than pure round-robin.

### Segments and indexes on disk

A partition is a directory of **segments**. Only the newest (**active**) segment receives writes.

```text
 /var/lib/kafka/data/orders-0/
   00000000000000000000.log        records, offsets 0 .. 1,048,575
   00000000000000000000.index      sparse map: offset → byte position in .log
   00000000000000000000.timeindex  sparse map: timestamp → offset
   00000000000001048576.log        next segment (file name = base offset)
   00000000000001048576.index
   00000000000001048576.timeindex
   00000000000002097152.log        ACTIVE segment
   ...
   leader-epoch-checkpoint
```

- A new segment is rolled when the active one reaches `segment.bytes` (default 1 GiB) or
  `segment.ms` (default 7 days).
- To read offset N, the broker binary-searches segment file names for the base offset, then the
  sparse `.index` (one entry per ~4 KiB by default) for a nearby position, then scans forward.
- Retention and compaction work **per segment**: deleting old data is deleting files, which is
  cheap.
- Kafka writes to the OS **page cache** and does not fsync each message by default; durability
  comes from **replication**, not from disk flushes. Consumers that keep up read from page cache,
  and the broker can use `sendfile` (zero-copy) when TLS is not in use.

Records are written in **batches** (record batch format v2): one header carrying producer id,
producer epoch, base sequence, compression codec, and transactional flag, followed by compressed
records. Batching and compression are why Kafka is fast.

### Offsets, high watermark and log end offset

```text
 Partition orders-0 on the leader

 offset:  0   1   2   3   4   5   6   7   8
        ┌───┬───┬───┬───┬───┬───┬───┬───┬───┐
        │ a │ b │ c │ d │ e │ f │ g │ h │ i │
        └───┴───┴───┴───┴───┴───┴───┴───┴───┘
                          ▲           ▲       ▲
                          │           │       └─ LEO = 9 (next offset to write)
                          │           └───────── HW = 7: 0..6 on all ISR members
                          └───────────────────── group "billing" committed offset = 4
```

- **Log end offset (LEO)**: next offset the leader will assign.
- **High watermark (HW)**: offsets below it are replicated to all in-sync replicas. Consumers can
  only read below the HW, so they never see data that could be lost on leader failover.
- **Committed offset**: per consumer group and partition, the **next** offset to read. Stored in
  the internal compacted topic `__consumer_offsets`.

### Replication and the ISR

Followers replicate by **fetching** from the leader, exactly like consumers. The leader tracks
which followers are caught up.

- **ISR (in-sync replicas)**: the leader plus followers that have fetched up to the leader's LEO
  within `replica.lag.time.max.ms` (default 30 s). A follower that falls behind is removed from
  the ISR; when it catches up it is added back.
- **HW = the minimum LEO across the ISR.** A write is "committed" when every ISR member has it.

```text
                  Producer (acks=all)
                         │
                         ▼
 ┌──────────────────────────┐   fetch    ┌──────────────────────────┐
 │ Broker 1: leader  p0     │ ◄───────── │ Broker 2: follower p0    │
 │ LEO = 10   HW = 8        │            │ LEO = 9   (in ISR)       │
 └──────────────────────────┘            └──────────────────────────┘
              ▲
              │ fetch
 ┌──────────────────────────┐
 │ Broker 3: follower p0    │
 │ LEO = 8   (in ISR)       │
 └──────────────────────────┘

 HW = min(10, 9, 8) = 8. A producer waiting on offset 9 with acks=all is not acknowledged
 until broker 3 fetches it, or until broker 3 drops out of the ISR.
```

Kafka's data replication is **not** Raft or Paxos: the ISR can shrink to the leader alone, which
is why `min.insync.replicas` exists.

### Leader election

The **controller** (in KRaft mode, the active member of the controller quorum) detects a dead
broker and picks a new leader for each of its partitions **from the ISR**. Because every ISR
member has everything below the HW, no committed record is lost.

- **Leader epoch**: each leadership change increments an epoch. A returning old leader uses the
  epoch history (`OffsetsForLeaderEpoch`) to truncate records the new leader never had, instead of
  trusting its own HW, which avoids log divergence.
- **Unclean leader election** (`unclean.leader.election.enable`, default **false**): if all ISR
  members are dead, allowing an out-of-sync replica to become leader restores availability but
  **loses committed data**. This is Kafka's CAP switch: false = CP, true = AP.
- **Preferred leader**: the first replica in the assignment list; Kafka periodically moves
  leadership back to it to balance load (`auto.leader.rebalance.enable`).

### acks and min.insync.replicas

| `acks` | Producer is acknowledged when | Loss possible when |
| --- | --- | --- |
| `0` | Immediately, no broker response | Any failure; you also cannot get retries or offsets |
| `1` | The leader has written to its log | Leader dies before followers fetch |
| `all` (`-1`) | Every **current ISR member** has it | ISR has shrunk to 1 and that broker dies |

`min.insync.replicas` (topic or broker config, default 1) closes the last gap: with `acks=all`,
the leader **rejects** writes (`NotEnoughReplicasException`) if the ISR is smaller than this
value.

```text
 Replication factor 3, min.insync.replicas 2, acks=all (the standard durable setting)

 ISR size 3 → writes succeed
 ISR size 2 → writes succeed (tolerates 1 broker down)
 ISR size 1 → writes REJECTED (availability given up to protect durability)
```

Since Kafka 3.0 the producer defaults are `acks=all` and `enable.idempotence=true`, but
`min.insync.replicas` still defaults to 1 on the broker; set it to 2 for RF=3 topics explicitly.

```bash
kafka-topics.sh --bootstrap-server broker1:9092 --create --topic orders \
  --partitions 12 --replication-factor 3 \
  --config min.insync.replicas=2 \
  --config retention.ms=604800000
```

### Idempotent producer

With `enable.idempotence=true`, the broker assigns the producer a **producer id (PID)** and an
**epoch**. The producer numbers each batch per partition with a **sequence number**. The broker
remembers the last sequence numbers per (PID, partition) and:

- discards a batch whose sequence it has already written (duplicate from a retry) and returns
  success;
- rejects a gap (`OutOfOrderSequenceException`), which means data was lost.

Constraints: `acks=all`, `retries > 0`, and `max.in.flight.requests.per.connection ≤ 5`. Ordering
is preserved even with 5 in-flight batches. Scope: **one producer session, per partition**. A
restarted producer gets a new PID, so duplicates across restarts are not caught unless you use a
`transactional.id`.

### Transactions and exactly-once semantics (EOS)

Transactions let a producer write to **several partitions** and **commit consumer offsets**
atomically. This makes **consume → transform → produce** pipelines exactly-once inside Kafka.

How it works:

1. The producer has a stable `transactional.id`. `initTransactions()` registers it with a
   **transaction coordinator** (a broker owning that id's partition of `__transaction_state`),
   bumps the epoch, and **fences** any older producer with the same id (zombie fencing). It also
   completes or aborts any transaction the previous instance left open.
2. `beginTransaction()`; sends go to partitions, which are registered with the coordinator.
3. `sendOffsetsToTransaction(offsets, groupMetadata)` adds the consumer offsets to the transaction.
4. `commitTransaction()`: the coordinator writes PREPARE_COMMIT to its log (a two-phase commit
   whose coordinator log is itself a replicated Kafka partition), then writes **commit markers**
   (control records) into every involved partition, then COMPLETE_COMMIT.
5. Consumers with `isolation.level=read_committed` only read up to the **last stable offset (LSO)**,
   the first offset of any still-open transaction, and skip records from aborted transactions.

```text
 input-topic ──► [ consumer ─ transform ─ producer ] ──► output-topic
                          │                  │
                          └── offsets ───────┴──► committed in ONE transaction
```

Limits to remember:

- EOS covers **Kafka to Kafka**. A database write or HTTP call inside the loop is **not** covered;
  make those idempotent.
- Default consumers use `read_uncommitted` and will see aborted records; set `read_committed`.
- Transactions add latency (commit markers, coordinator round trips). Commit every few hundred ms
  or every batch, not per record.
- Kafka Streams wraps all of this with `processing.guarantee=exactly_once_v2`.

### Consumer groups

Consumers sharing a `group.id` split the partitions of the subscribed topics: **each partition is
assigned to exactly one consumer in the group**. Partitions are the maximum parallelism: 12
partitions means at most 12 active consumers in a group; extras sit idle.

```text
 Topic with 6 partitions, group "billing" with 3 consumers

 p0 p1 ──► consumer A
 p2 p3 ──► consumer B
 p4 p5 ──► consumer C

 Group "analytics" reads the same 6 partitions independently with its own offsets.
```

One broker acts as **group coordinator** for each group. Consumers send **heartbeats**; if a
consumer misses `session.timeout.ms` (default 45 s) or does not call `poll()` within
`max.poll.interval.ms` (default 5 min), it is removed and its partitions are reassigned.

### Rebalancing protocols

A **rebalance** reassigns partitions when members join, leave, crash, or subscriptions change.

| Protocol | How it works | Pause behaviour |
| --- | --- | --- |
| **Eager** (classic; `RangeAssignor`, `RoundRobinAssignor`) | Every member revokes **all** partitions, rejoins, the group leader computes a new assignment | Stop-the-world: whole group stops consuming |
| **Incremental cooperative** (classic; `CooperativeStickyAssignor`) | Members keep partitions that do not move; only moved partitions are revoked, in a second rebalance round | Only moved partitions pause |
| **Static membership** (`group.instance.id`) | A restarted member with the same instance id reclaims its partitions without a rebalance, if it returns within `session.timeout.ms` | Avoids rebalances on rolling restarts |
| **Next-gen consumer protocol** (KIP-848, `group.protocol=consumer`) | Broker computes assignments; members reconcile incrementally via heartbeats; no group-wide barrier | Minimal; GA in Kafka 4.0 |

```text
 Eager:        A revokes p0..p5 ──► everyone rejoins ──► new assignment ──► resume all
 Cooperative:  only p4, p5 move from C to new consumer D; A, B, C keep consuming the rest
```

With the KIP-848 protocol, client-side `partition.assignment.strategy` does not apply; the
broker-side assignor is chosen with `group.remote.assignor` (check your client and broker versions,
since this protocol is still evolving).

### Offset commit strategies

| Strategy | Code | Guarantee | Notes |
| --- | --- | --- | --- |
| Auto-commit | `enable.auto.commit=true` | At-least-once **if** processing finishes before the next `poll()`; otherwise may lose | Commits every `auto.commit.interval.ms` (5 s) during `poll()` |
| Sync commit after batch | `commitSync()` | At-least-once | Blocks; simple; duplicates up to one batch on crash |
| Async commit + sync on shutdown | `commitAsync()` in loop, `commitSync()` on close/revoke | At-least-once | Faster; do not retry async commits blindly (an older commit may overwrite a newer one) |
| Per-record or per-N commit | `commitSync(Map<TopicPartition, OffsetAndMetadata>)` | At-least-once with fewer duplicates | More commit traffic |
| Offsets stored in your DB | Save offset with results in one DB transaction; `seek()` on assignment | Exactly-once effect | Ignore Kafka's committed offsets |
| Kafka transactions | `sendOffsetsToTransaction` | Exactly-once Kafka-to-Kafka | Output must go to Kafka |

The committed value is always **last processed offset + 1**.

### Retention and log compaction

`cleanup.policy` decides what happens to old segments:

- **`delete`** (default): delete whole segments older than `retention.ms` (default 7 days) or when
  the partition exceeds `retention.bytes` (default unlimited).
- **`compact`**: keep at least the **latest record per key**. A record with a null value
  (**tombstone**) deletes the key; tombstones themselves are removed after `delete.retention.ms`
  (default 24 h).
- **`compact,delete`**: compact, and also drop old segments.

```text
 Before compaction (key:value)       After compaction
 off 0  user1:{v1}
 off 1  user2:{v1}
 off 2  user1:{v2}
 off 3  user3:{v1}                   off 3  user3:{v1}
 off 4  user2:null  (tombstone)      off 4  user2:null   (until delete.retention.ms)
 off 5  user1:{v3}                   off 5  user1:{v3}
```

Offsets are preserved (gaps appear). The active segment is never compacted, and the cleaner runs
when the "dirty" ratio exceeds `min.cleanable.dirty.ratio` (default 0.5), so compaction is not
immediate. Compacted topics act as a durable **table changelog**: Kafka uses them for
`__consumer_offsets`, Kafka Streams for state store changelogs, and they are ideal for
event-carried state transfer ("latest customer profile by id").

**Tiered storage** (KIP-405, production-ready in recent 3.x releases) moves old segments to object
storage such as S3, so retention can be long without large local disks. Check your distribution's
support status.

### KRaft

Kafka originally stored metadata (topics, partitions, ISR, configs) in ZooKeeper. **KRaft**
replaces it with a Raft-based **controller quorum** (usually 3 or 5 controller nodes) that stores
metadata in an internal log, `__cluster_metadata`. Brokers replicate that log and keep metadata in
memory.

- Faster controller failover (the standby controllers already have the metadata).
- Supports far more partitions per cluster than the ZooKeeper design.
- One system to secure and operate.
- **Kafka 4.0 (2025) removed ZooKeeper mode entirely**; ZooKeeper clusters must migrate through a
  3.x bridge release first.

### Ordering guarantees

- Order is guaranteed **within a partition** only. There is no order across partitions.
- Records with the same key go to the same partition, so **per-key order** is preserved, as long
  as the partition count does not change.
- With the idempotent producer (default), retries do not reorder. Without it, `retries > 0` and
  `max.in.flight.requests.per.connection > 1` can reorder batches.
- In a consumer, processing records from one partition on several threads breaks order unless you
  keep per-key ordering yourself (see "Ordering and parallelism").

### Partition count sizing

Partitions are the unit of parallelism, but each costs file handles, memory, replication traffic,
and leader-election work.

```text
 Inputs:
   target throughput          T  = 300 MB/s
   per-partition producer     Pp = 50 MB/s   (measure in your environment)
   per-partition consumer     Pc = 20 MB/s   (limited by your processing logic)

 partitions ≥ max(T / Pp, T / Pc) = max(6, 15) = 15
 add headroom for 2–3x growth                  → 32 or 48
 also ≥ max number of consumers you will ever want in one group
```

Rules of thumb:

- **You cannot reduce** a topic's partition count.
- **Increasing** it changes `hash(key) % n`, so existing keys move to new partitions and per-key
  ordering breaks across the change. For keyed topics, over-provision at creation or create a new
  topic and migrate.
- Very small topics do not need many partitions; thousands of tiny partitions waste resources.
- Under KRaft, clusters handle hundreds of thousands to millions of partitions, but per-broker
  limits (tens of thousands of partition replicas, depending on hardware) still apply.

### Key choice and hot partitions

The key defines ordering scope **and** load distribution.

| Key | Ordering scope | Risk |
| --- | --- | --- |
| `orderId` | Per order | Good spread; usually right for order events |
| `customerId` | Per customer | A large B2B customer can create a hot partition |
| `country` | Per country | Severe skew: few distinct values, one dominates |
| null | None | Even spread, no ordering |

Mitigations for hot keys:

- Choose a finer-grained key (order instead of customer) if ordering only matters per entity.
- **Salt** hot keys (`customerId + "#" + (n % 8)`): spreads load but gives up ordering for that key
  across the 8 sub-keys; consumers must tolerate that.
- A custom `Partitioner` that routes a known list of heavy keys to dedicated partitions.
- Monitor bytes-in per partition, not just per topic.

---

## Kafka producer and consumer in Java

### Producer

```java
import org.apache.kafka.clients.producer.*;
import org.apache.kafka.common.serialization.StringSerializer;

import java.nio.charset.StandardCharsets;
import java.util.Properties;
import java.util.UUID;

public final class OrderEventProducer implements AutoCloseable {
    private final KafkaProducer<String, String> producer;

    public OrderEventProducer(String bootstrapServers) {
        Properties p = new Properties();
        p.put(ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrapServers);
        p.put(ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG, StringSerializer.class.getName());
        p.put(ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG, StringSerializer.class.getName());
        p.put(ProducerConfig.CLIENT_ID_CONFIG, "order-service");
        // Durability and no duplicates on retry (defaults since 3.0, set explicitly anyway)
        p.put(ProducerConfig.ACKS_CONFIG, "all");
        p.put(ProducerConfig.ENABLE_IDEMPOTENCE_CONFIG, true);
        p.put(ProducerConfig.MAX_IN_FLIGHT_REQUESTS_PER_CONNECTION, 5);
        // Upper bound for send + retries; retries are effectively unlimited within it
        p.put(ProducerConfig.DELIVERY_TIMEOUT_MS_CONFIG, 120_000);
        p.put(ProducerConfig.REQUEST_TIMEOUT_MS_CONFIG, 30_000);
        // Throughput: small linger, bigger batches, compression
        p.put(ProducerConfig.LINGER_MS_CONFIG, 10);
        p.put(ProducerConfig.BATCH_SIZE_CONFIG, 64 * 1024);
        p.put(ProducerConfig.COMPRESSION_TYPE_CONFIG, "zstd");
        this.producer = new KafkaProducer<>(p);
    }

    public void publish(String orderId, String jsonPayload) {
        var record = new ProducerRecord<>("orders", orderId, jsonPayload);
        record.headers().add("message-id",
                UUID.randomUUID().toString().getBytes(StandardCharsets.UTF_8));
        producer.send(record, (metadata, exception) -> {
            if (exception != null) {
                // Retriable errors were already retried until delivery.timeout.ms.
                // Reaching here means the record was NOT written: alert, or fail the caller.
                System.err.printf("send failed for order %s: %s%n", orderId, exception);
            }
        });
    }

    @Override
    public void close() {
        producer.close();   // flushes buffered records
    }
}
```

Notes:

- `send()` is asynchronous; records sit in a buffer (`buffer.memory`, 32 MiB default) until sent.
  If the process dies, buffered records are lost. For "DB change + event" use the outbox.
- `linger.ms` defaults to 5 ms in Kafka 4.0 (0 before). A few ms of linger greatly improves
  batching at high volume.
- Do not call `send(...).get()` per record on a hot path; it serialises everything to one
  round trip per record.

### Consumer with manual commits

```java
import org.apache.kafka.clients.consumer.*;
import org.apache.kafka.common.TopicPartition;
import org.apache.kafka.common.errors.WakeupException;
import org.apache.kafka.common.serialization.StringDeserializer;

import java.time.Duration;
import java.util.*;
import java.util.concurrent.atomic.AtomicBoolean;

public final class OrderConsumer implements Runnable, AutoCloseable {
    private final KafkaConsumer<String, String> consumer;
    private final RecordHandler handler;
    private final Map<TopicPartition, OffsetAndMetadata> pending = new HashMap<>();
    private final AtomicBoolean running = new AtomicBoolean(true);

    public interface RecordHandler {
        void handle(ConsumerRecord<String, String> record) throws Exception;
    }

    public OrderConsumer(String bootstrapServers, String instanceId, RecordHandler handler) {
        Properties p = new Properties();
        p.put(ConsumerConfig.BOOTSTRAP_SERVERS_CONFIG, bootstrapServers);
        p.put(ConsumerConfig.GROUP_ID_CONFIG, "billing-service");
        p.put(ConsumerConfig.GROUP_INSTANCE_ID_CONFIG, instanceId);       // static membership
        p.put(ConsumerConfig.KEY_DESERIALIZER_CLASS_CONFIG, StringDeserializer.class.getName());
        p.put(ConsumerConfig.VALUE_DESERIALIZER_CLASS_CONFIG, StringDeserializer.class.getName());
        p.put(ConsumerConfig.ENABLE_AUTO_COMMIT_CONFIG, false);
        p.put(ConsumerConfig.AUTO_OFFSET_RESET_CONFIG, "earliest");      // new group starts at beginning
        p.put(ConsumerConfig.ISOLATION_LEVEL_CONFIG, "read_committed");
        p.put(ConsumerConfig.MAX_POLL_RECORDS_CONFIG, 200);
        p.put(ConsumerConfig.MAX_POLL_INTERVAL_MS_CONFIG, 300_000);
        p.put(ConsumerConfig.PARTITION_ASSIGNMENT_STRATEGY_CONFIG,
                CooperativeStickyAssignor.class.getName());
        this.consumer = new KafkaConsumer<>(p);
        this.handler = handler;
    }

    @Override
    public void run() {
        consumer.subscribe(List.of("orders"), new ConsumerRebalanceListener() {
            @Override
            public void onPartitionsRevoked(Collection<TopicPartition> partitions) {
                commitPending();             // do not lose progress on partitions moving away
            }

            @Override
            public void onPartitionsAssigned(Collection<TopicPartition> partitions) { }
        });
        try {
            while (running.get()) {
                ConsumerRecords<String, String> records = consumer.poll(Duration.ofMillis(500));
                for (ConsumerRecord<String, String> r : records) {
                    try {
                        handler.handle(r);   // must be idempotent
                    } catch (Exception e) {
                        // Real code: publish to a retry topic or DLQ, then continue.
                        throw new IllegalStateException("unhandled failure at " + r.offset(), e);
                    }
                    pending.put(new TopicPartition(r.topic(), r.partition()),
                            new OffsetAndMetadata(r.offset() + 1));
                }
                commitPending();
            }
        } catch (WakeupException e) {
            if (running.get()) throw e;      // unexpected wakeup
        } finally {
            try {
                commitPending();
            } finally {
                consumer.close();
            }
        }
    }

    private void commitPending() {
        if (!pending.isEmpty()) {
            consumer.commitSync(pending);
            pending.clear();
        }
    }

    /** Call from another thread (e.g. a shutdown hook). */
    @Override
    public void close() {
        running.set(false);
        consumer.wakeup();
    }
}
```

`KafkaConsumer` is **not thread-safe**: one consumer per thread, and only `wakeup()` may be called
from another thread. For more throughput run more consumer instances (up to the partition count).

### Consume-transform-produce with transactions

```java
producerProps.put(ProducerConfig.TRANSACTIONAL_ID_CONFIG, "enricher-" + instanceId);
consumerProps.put(ConsumerConfig.ISOLATION_LEVEL_CONFIG, "read_committed");
consumerProps.put(ConsumerConfig.ENABLE_AUTO_COMMIT_CONFIG, false);

producer.initTransactions();
consumer.subscribe(List.of("orders"));

while (running) {
    ConsumerRecords<String, String> records = consumer.poll(Duration.ofMillis(500));
    if (records.isEmpty()) continue;

    producer.beginTransaction();
    try {
        Map<TopicPartition, OffsetAndMetadata> offsets = new HashMap<>();
        for (ConsumerRecord<String, String> r : records) {
            producer.send(new ProducerRecord<>("orders-enriched", r.key(), enrich(r.value())));
            offsets.put(new TopicPartition(r.topic(), r.partition()),
                    new OffsetAndMetadata(r.offset() + 1));
        }
        producer.sendOffsetsToTransaction(offsets, consumer.groupMetadata());
        producer.commitTransaction();
    } catch (ProducerFencedException e) {
        producer.close();                  // another instance took over this transactional.id
        throw e;
    } catch (KafkaException e) {
        producer.abortTransaction();
        rewindToCommitted(consumer);       // seek each assigned partition to its committed offset
    }
}
```

`rewindToCommitted` uses `consumer.committed(consumer.assignment())` and `seek()` so the aborted
batch is re-read. Since KIP-447 (Kafka 2.5), one `transactional.id` per producer instance is
enough; fencing uses the consumer group metadata.

---

## RabbitMQ internals

RabbitMQ implements **AMQP 0-9-1** (and also AMQP 1.0, MQTT, STOMP). Producers never publish to
queues directly; they publish to an **exchange**, which routes copies to **queues** according to
**bindings**.

```text
 Producer ──► Exchange ──(binding rules)──► Queue ──► Consumer
                  │
                  └──(another binding)────► Queue ──► Consumer
```

### Exchange types

| Type | Routing rule | Use |
| --- | --- | --- |
| **direct** | Routing key equals binding key | Task queues by type (`resize`, `email`) |
| **topic** | Pattern match on dot-separated words: `*` = exactly one word, `#` = zero or more | `order.eu.created` matches `order.*.created` and `order.#` |
| **fanout** | Ignores routing key; copy to every bound queue | Broadcast, pub/sub |
| **headers** | Match on message headers; `x-match` = `all` or `any` | Routing on several attributes |

The **default exchange** (empty name) is a direct exchange where every queue is bound with its own
name, which is why "publishing to a queue" appears to work.

```text
 Topic exchange "orders"

 publish key "order.eu.created" ──► [orders] ──┬── "order.*.created" ──► billing.q     ✓
                                               ├── "order.us.*"      ──► us-ops.q      ✗
                                               └── "order.#"         ──► audit.q       ✓
```

If no binding matches, the message is dropped silently, unless the publisher sets the `mandatory`
flag (unroutable messages are returned via `basic.return`) or the exchange has an
**alternate exchange**.

### Queues and queue types

| Type | Replication | Notes |
| --- | --- | --- |
| **Classic** | None (single node) | Fast; lost if the node's disk is lost. Classic mirrored queues were removed in RabbitMQ 4.0 |
| **Quorum** | Raft across 3 or 5 nodes | The durable, replicated default for important data; built-in poison message handling |
| **Stream** | Replicated append-only log | Kafka-like: non-destructive reads, replay by offset, large fan-out |

Quorum queues are declared with `x-queue-type=quorum`. They track a delivery count per message and
dead-letter (or drop) it after `x-delivery-limit` deliveries; RabbitMQ 4.0 sets a default limit
of 20.

### Acknowledgements and prefetch

- **Consumer acks**: in manual mode the consumer sends `basic.ack` after processing. `basic.nack`
  or `basic.reject` with `requeue=true` puts it back; `requeue=false` dead-letters or drops it.
  If the channel or connection closes, unacked messages are requeued (and redelivered with the
  `redelivered` flag set).
- **Auto-ack mode** acks on delivery: at-most-once. Avoid for important work.
- **Prefetch (`basic.qos`)**: maximum unacked messages per consumer (per channel or per consumer).
  Without it, RabbitMQ pushes everything to the first consumer, which may run out of memory while
  others sit idle. Start around 10–100 for fast handlers, 1 for long jobs.
- A consumer that holds a message unacked longer than `consumer_timeout` (default 30 minutes) has
  its channel closed and the message is requeued.

### Publisher confirms

Publishing is fire-and-forget by default. With **publisher confirms** (`confirm.select`), the
broker acks each message once it is safely handled: routed to all target queues and, for durable
queues, persisted (for quorum queues, written by a majority). Without confirms, a broker crash can
silently lose published messages.

### Dead-letter exchanges (DLX) and TTL

A queue argument `x-dead-letter-exchange` (and optionally `x-dead-letter-routing-key`) republishes
a message to another exchange when:

1. it is rejected or nacked with `requeue=false`,
2. its TTL expires,
3. the queue's length limit (`x-max-length`) is exceeded with overflow `drop-head`,
4. a quorum queue's delivery limit is exceeded.

TTL options: per queue (`x-message-ttl`), per message (the `expiration` property, a string in ms),
and queue expiry when unused (`x-expires`). In classic queues, expired messages are only removed
when they reach the head of the queue, so a message with a long TTL in front blocks earlier expiry
of later ones.

Retry with delay using TTL + DLX:

```text
 work.q ── nack(requeue=false) ──► DLX "retry" ──► retry-30s.q (x-message-ttl=30000,
   ▲                                                  x-dead-letter-exchange="work")
   └────────────── after 30 s the message is dead-lettered back to work ──┘
```

### Java example

```java
import com.rabbitmq.client.*;

import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.UUID;

public final class RabbitExample {
    public static void main(String[] args) throws Exception {
        ConnectionFactory factory = new ConnectionFactory();
        factory.setHost("rabbit");
        try (Connection conn = factory.newConnection();
             Channel ch = conn.createChannel()) {

            ch.exchangeDeclare("orders", BuiltinExchangeType.TOPIC, true);
            ch.exchangeDeclare("orders.dlx", BuiltinExchangeType.FANOUT, true);
            ch.queueDeclare("orders.dead", true, false, false, Map.of("x-queue-type", "quorum"));
            ch.queueBind("orders.dead", "orders.dlx", "");

            Map<String, Object> args = Map.of(
                    "x-queue-type", "quorum",
                    "x-dead-letter-exchange", "orders.dlx",
                    "x-delivery-limit", 5);
            ch.queueDeclare("billing.orders", true, false, false, args);
            ch.queueBind("billing.orders", "orders", "order.*.created");

            // Publish with confirms
            ch.confirmSelect();
            AMQP.BasicProperties props = new AMQP.BasicProperties.Builder()
                    .contentType("application/json")
                    .deliveryMode(2)                       // persistent
                    .messageId(UUID.randomUUID().toString())
                    .build();
            ch.basicPublish("orders", "order.eu.created", true, props,
                    "{\"orderId\":\"42\"}".getBytes(StandardCharsets.UTF_8));
            ch.waitForConfirmsOrDie(5_000);                // throws if nacked or timed out
        }
    }

    static void consume(Channel ch) throws Exception {
        ch.basicQos(50);
        DeliverCallback onDeliver = (tag, delivery) -> {
            long deliveryTag = delivery.getEnvelope().getDeliveryTag();
            try {
                handle(new String(delivery.getBody(), StandardCharsets.UTF_8));
                ch.basicAck(deliveryTag, false);
            } catch (Exception e) {
                ch.basicNack(deliveryTag, false, false); // dead-letter, do not requeue forever
            }
        };
        ch.basicConsume("billing.orders", false, onDeliver, consumerTag -> { });
    }

    static void handle(String body) { /* idempotent business logic */ }
}
```

`waitForConfirmsOrDie` per message is simple but slow; for throughput, publish in batches and
wait once, or track confirms asynchronously with `addConfirmListener`.

---

## Amazon SQS and SNS

### SQS standard vs FIFO

| | Standard | FIFO (`.fifo` suffix) |
| --- | --- | --- |
| Delivery | At-least-once; occasional duplicates | Exactly-once **processing** within a 5-minute deduplication window |
| Ordering | Best effort | Strict within a **message group** (`MessageGroupId`) |
| Throughput | Nearly unlimited | 300 API calls/s per action (3,000 messages/s with batches of 10); high-throughput mode is much higher (check current quotas) |
| Deduplication | None | `MessageDeduplicationId` or content-based (SHA-256 of body) |
| Parallelism | Any consumer gets any message | One in-flight batch per message group at a time |

Message groups in FIFO are like Kafka keys: order and serial processing per group, parallelism
across groups.

### Visibility timeout

SQS does not push. A consumer calls `ReceiveMessage`; returned messages become **invisible** for
the **visibility timeout** (default 30 s, max 12 h). The consumer must `DeleteMessage` with the
**receipt handle** before it expires, or the message becomes visible again and is redelivered.

```text
 t=0    ReceiveMessage → m1 (invisible until t=30)
 t=10   processing...
 t=25   ChangeMessageVisibility(m1, 60)   extend for slow work (heartbeat)
 t=50   DeleteMessage(m1)                 done
        ── if the consumer had crashed at t=10, m1 reappears at t=30 for another consumer
```

Set the visibility timeout above your p99 processing time (AWS suggests about 6x the Lambda
function timeout for Lambda triggers), and extend it from a heartbeat for long jobs.

### Long polling

`WaitTimeSeconds` (1–20 s) makes `ReceiveMessage` wait until messages arrive instead of returning
empty immediately. It reduces empty responses (cost) and latency. Short polling also samples only
a subset of servers, so it can return empty even when messages exist. Always use long polling.

### Dead-letter queues and redrive

A **redrive policy** on the source queue moves a message to a DLQ after `maxReceiveCount` receives
without deletion.

```json
{
  "RedrivePolicy": "{\"deadLetterTargetArn\":\"arn:aws:sqs:eu-west-1:123456789012:orders-dlq\",\"maxReceiveCount\":\"5\"}"
}
```

- The DLQ must be the same type as the source (FIFO for FIFO).
- Set the DLQ's retention longer than the source's: the original enqueue timestamp is kept, so a
  message near its retention limit can expire soon after arriving in the DLQ.
- **DLQ redrive** moves messages back to the source (or another queue) after a fix, from the
  console or the `StartMessageMoveTask` API.

Other limits: retention 1 minute to 14 days (default 4 days); delay queues up to 15 minutes;
`ReceiveMessage` returns at most 10 messages. Maximum message size was 256 KiB for many years and
was raised in 2025; check current quotas, and use the claim-check pattern (store payload in S3,
send a pointer) for large payloads.

```java
import software.amazon.awssdk.services.sqs.SqsClient;
import software.amazon.awssdk.services.sqs.model.*;

public final class SqsWorker implements Runnable {
    private final SqsClient sqs = SqsClient.create();
    private final String queueUrl;
    private volatile boolean running = true;

    public SqsWorker(String queueUrl) { this.queueUrl = queueUrl; }

    @Override
    public void run() {
        while (running) {
            ReceiveMessageRequest req = ReceiveMessageRequest.builder()
                    .queueUrl(queueUrl)
                    .maxNumberOfMessages(10)
                    .waitTimeSeconds(20)          // long polling
                    .visibilityTimeout(120)
                    .build();
            for (Message m : sqs.receiveMessage(req).messages()) {
                try {
                    process(m.messageId(), m.body());          // idempotent by messageId
                    sqs.deleteMessage(DeleteMessageRequest.builder()
                            .queueUrl(queueUrl)
                            .receiptHandle(m.receiptHandle())
                            .build());
                } catch (Exception e) {
                    // Do nothing: the message reappears after the visibility timeout and
                    // moves to the DLQ after maxReceiveCount attempts.
                }
            }
        }
    }

    private void process(String id, String body) { /* business logic */ }

    public void stop() { running = false; }
}
```

### SNS fan-out

SNS is push-based pub/sub. A topic delivers to subscribers: SQS queues, Lambda, HTTP(S), email,
SMS, and more. The standard pattern is **SNS → SQS per consumer**, so each consumer gets its own
durable buffer, retries, and DLQ.

```text
                     ┌──► SQS billing-q   ──► billing workers
 OrderPlaced ─► SNS ─┼──► SQS search-q    ──► indexer
                     └──► SQS analytics-q ──► loader (filter policy: amount > 1000)
```

- **Subscription filter policies** (JSON on message attributes or body) let each subscriber
  receive only relevant messages.
- **SNS FIFO topics** deliver to SQS FIFO queues, preserving order per message group.
- Enable **raw message delivery** to SQS to avoid the SNS JSON envelope.
- **EventBridge** is the alternative when you need content-based routing rules across many
  sources, schema discovery, and archive/replay.

---

## Comparison: Kafka, RabbitMQ, SQS, Pulsar, NATS

| | Apache Kafka | RabbitMQ | Amazon SQS | Apache Pulsar | NATS (JetStream) |
| --- | --- | --- | --- | --- | --- |
| Core model | Partitioned log | Broker with exchanges and queues (plus streams) | Managed queue | Log with queue semantics; brokers separate from storage | Subject-based messaging; JetStream adds persistent streams |
| Storage | Broker local disks (+ tiered storage) | Node disks; quorum queues replicated via Raft | Managed by AWS | Apache BookKeeper (+ tiered storage) | File or memory, Raft-replicated streams |
| Retention after consume | Kept until retention | Deleted on ack (streams kept) | Deleted on delete | Kept per retention policy | Configurable (limits, interest, work-queue) |
| Replay | Yes, by offset or timestamp | Streams only | No | Yes | Yes |
| Ordering | Per partition | Per queue (single consumer) | FIFO queues per group | Per partition / per key (Key_Shared) | Per stream / subject |
| Consumer scaling | Up to partition count per group | Competing consumers, unlimited | Unlimited | Shared subscriptions, unlimited | Pull consumers, unlimited |
| Routing | By topic and key | Rich: direct, topic, fanout, headers | None (use SNS / EventBridge) | Topics, namespaces, tenants | Subject wildcards `*` and `>` |
| Delivery | At-least-once; EOS within Kafka | At-least-once with acks and confirms | At-least-once; FIFO dedup window | At-least-once; transactions available | Core: at-most-once; JetStream: at-least-once, dedup by `Nats-Msg-Id` |
| Per-message ack / DLQ | No (offsets); DLQ by convention | Yes, DLX built in | Yes, redrive built in | Yes; retry and dead-letter topics built in | Yes; max deliveries + advisories |
| Throughput | Very high (millions msg/s per cluster) | High (tens of thousands msg/s per queue) | Effectively unlimited (standard) | Very high | Very high, low latency |
| Ops burden | Medium–high (managed: MSK, Confluent) | Medium | None | High (brokers + bookies) | Low |
| Best for | Event streaming, CDC, analytics, event sourcing | Task queues, complex routing, RPC-style work | Simple decoupling on AWS | Multi-tenant, geo-replicated streaming + queues | Lightweight service messaging, edge/IoT |

Kafka 4.x also introduces **share groups** ("queues for Kafka", KIP-932), which give per-message
acknowledgement and more consumers than partitions; check its maturity in your version before
relying on it.

Choosing:

- Need replay, multiple independent consumers, high throughput, or CDC: **Kafka** (or Pulsar).
- Need per-message work distribution with complex routing and per-message retry: **RabbitMQ**.
- On AWS and want zero operations for a work queue: **SQS** (with SNS for fan-out).
- Need very low-latency request/reply and lightweight pub/sub: **NATS**.

---

## Idempotent consumers and deduplication

Because delivery is at-least-once, every consumer must make duplicates harmless.

### Strategies

| Strategy | How | Trade-off |
| --- | --- | --- |
| **Naturally idempotent operation** | `SET status = 'SHIPPED'`, upserts, `PUT` semantics | Best; not always possible |
| **Processed-message table** | Insert message id with a unique constraint **in the same transaction** as the effect | Needs a DB; table grows (expire old rows) |
| **Version / sequence check** | Apply only if `event.version = row.version + 1` | Also detects gaps and reordering |
| **Conditional write** | `UPDATE ... WHERE status = 'PENDING'` | Works for state machines |
| **Dedup cache (Redis `SET NX EX`)** | Remember ids for a window | Not atomic with the effect; a crash between can lose or duplicate |
| **Idempotency key downstream** | Pass the message id to the payment provider | Required when the effect is an external call |

### Processed-message table in the same transaction

```sql
CREATE TABLE processed_messages (
    consumer     TEXT        NOT NULL,
    message_id   TEXT        NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (consumer, message_id)
);
```

```java
public void handle(ConsumerRecord<String, String> rec) throws SQLException {
    Header header = rec.headers().lastHeader("message-id");
    String messageId = header != null
            ? new String(header.value(), StandardCharsets.UTF_8)
            : rec.topic() + "-" + rec.partition() + "-" + rec.offset();

    try (Connection c = dataSource.getConnection()) {
        c.setAutoCommit(false);
        try {
            try (PreparedStatement ins = c.prepareStatement("""
                    INSERT INTO processed_messages (consumer, message_id)
                    VALUES (?, ?) ON CONFLICT DO NOTHING
                    """)) {
                ins.setString(1, "billing-service");
                ins.setString(2, messageId);
                if (ins.executeUpdate() == 0) {
                    c.rollback();
                    return;                      // duplicate: already applied
                }
            }
            applyBusinessChange(c, rec.value()); // same connection, same transaction
            c.commit();
        } catch (SQLException | RuntimeException e) {
            c.rollback();
            throw e;
        }
    }
}
```

The fallback id (topic-partition-offset) only deduplicates redeliveries of the **same** record; a
producer-side duplicate gets a different offset, so a producer-assigned id is better.

---

## Retries, backoff, DLQs and poison messages

### Classify failures first

| Failure | Example | Action |
| --- | --- | --- |
| Transient | Timeout, 503, deadlock, rate limit | Retry with exponential backoff and jitter |
| Permanent (poison) | Unparseable JSON, unknown schema, validation failure | Do not retry; send to DLQ immediately |
| Dependency outage | Downstream down for minutes | Pause consumption or circuit-break; do not burn through retries into the DLQ |
| Bug | NullPointerException on a valid message | DLQ, fix, redrive |

Backoff with full jitter: `sleep = random(0, min(cap, base * 2^attempt))`. Jitter prevents all
consumers from retrying in lockstep against a recovering service.

### Poison messages

A poison message always fails. In a queue it is redelivered forever; in a Kafka partition it
**blocks everything behind it**. Always bound attempts (SQS `maxReceiveCount`, RabbitMQ quorum
`x-delivery-limit`, a counter header in Kafka) and move it aside with enough context (original
topic, partition, offset, exception, stack trace, attempt count) to debug and redrive.

### Retry topics pattern (Kafka)

Kafka has no per-message delay or ack, so non-blocking retries are built from extra topics:

```text
 orders ──► main consumer ── fail ──► orders-retry-1m ──► retry consumer (waits until
   │                                                       record timestamp + 1 min)
   │                                       fail ──► orders-retry-10m ──► retry consumer
   │                                                          fail ──► orders-dlq
   └── success: commit offset and move on; the main partition is never blocked
```

- Each retry consumer checks the record's timestamp and **pauses** the partition
  (`consumer.pause()`) until it is due, rather than sleeping inside `poll()` past
  `max.poll.interval.ms`.
- Copy headers like `original-topic`, `original-offset`, `attempt`, `exception-message`.
- Spring for Apache Kafka provides this as `@RetryableTopic` with configurable backoff and a DLT.
- Trade-off: a retried message is processed **after** later messages with the same key, so per-key
  ordering is broken. If order matters (account balance updates), use **blocking** retries in the
  main partition (retry in place with backoff, then pause) instead.

---

## Ordering and parallelism

Order and parallelism pull against each other. Choose the narrowest ordering scope your business
actually needs.

| Approach | Ordering | Parallelism |
| --- | --- | --- |
| One partition, one consumer | Total order | 1 |
| Keyed partitions, one consumer thread per partition | Per key | = partitions |
| Per-partition consumer + **key-based worker threads** inside | Per key | Many threads per partition |
| Unordered worker pool | None | Highest |

The middle-ground pattern: hash each record's key to one of N single-threaded executors, so records
with the same key run in order while different keys run in parallel. Track the **lowest
unfinished offset per partition** and only commit up to it. The open-source Confluent Parallel
Consumer library implements this (key or unordered modes) with correct offset tracking.

```java
/** Per-key ordering with parallelism inside one partition (offset tracking omitted). */
public final class KeyOrderedExecutor {
    private final ExecutorService[] lanes;

    public KeyOrderedExecutor(int laneCount) {
        lanes = new ExecutorService[laneCount];
        for (int i = 0; i < laneCount; i++) {
            lanes[i] = Executors.newSingleThreadExecutor();
        }
    }

    public CompletableFuture<Void> submit(String key, Runnable task) {
        int lane = Math.floorMod(Objects.hashCode(key), lanes.length);
        return CompletableFuture.runAsync(task, lanes[lane]);
    }
}
```

---

## The transactional outbox pattern with Debezium

### The dual-write problem

```text
 @Transactional
 void placeOrder(...) {
     orderRepository.save(order);        ① DB commit
     kafka.send("orders", event);        ② broker write
 }

 Crash or failure between ① and ② → order exists, event never sent.
 Send first, then DB fails       → event sent for an order that does not exist.
 Reordering the calls does not help: two systems, no shared commit.
```

### The pattern

Write the event to an **outbox** table in the **same local transaction** as the state change. A
separate relay reads the outbox and publishes. Either both are committed or neither.

```sql
CREATE TABLE outbox (
    id             UUID         PRIMARY KEY,
    aggregatetype  VARCHAR(255) NOT NULL,   -- routes to topic, e.g. "Order"
    aggregateid    VARCHAR(255) NOT NULL,   -- becomes the Kafka key → per-aggregate order
    type           VARCHAR(255) NOT NULL,   -- event type, e.g. "OrderPlaced"
    payload        JSONB        NOT NULL,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);
```

```java
@Transactional
public void placeOrder(PlaceOrder cmd) {
    Order order = Order.create(cmd);
    orderRepository.save(order);
    jdbc.update("""
            INSERT INTO outbox (id, aggregatetype, aggregateid, type, payload)
            VALUES (?, 'Order', ?, 'OrderPlaced', ?::jsonb)
            """, UUID.randomUUID(), order.id(), toJson(OrderPlaced.from(order)));
}
```

### Relay option 1: polling publisher

```sql
-- run in a loop by one or more relay workers
BEGIN;
SELECT id, aggregateid, type, payload
FROM   outbox
ORDER  BY created_at
LIMIT  100
FOR UPDATE SKIP LOCKED;
-- publish each row to Kafka and wait for acks, then:
DELETE FROM outbox WHERE id = ANY(:published_ids);
COMMIT;
```

Simple and portable, but adds polling load and latency, and ordering across concurrent relay
workers needs care (order by aggregate, or a single relay per partition of aggregates).

### Relay option 2: change data capture with Debezium

Debezium reads the database's **transaction log** (PostgreSQL WAL via logical decoding, MySQL
binlog) and turns committed row changes into Kafka records. It runs as a **Kafka Connect** source
connector. Its **outbox event router** SMT turns outbox rows into clean domain events.

```text
 ┌──────────────┐ commit ┌──────────────┐ WAL ┌──────────────┐     ┌──────────────┐
 │ Order svc    │ ─────► │  PostgreSQL  │ ──► │  Debezium    │ ──► │ Kafka topic  │
 │ order+outbox │        │ orders,outbox│     │ (Connect +   │     │ outbox.event │
 │ in one tx    │        │ repl. slot   │     │ EventRouter) │     │ .Order       │
 └──────────────┘        └──────────────┘     └──────────────┘     └──────────────┘
```

Connector configuration (Debezium 2.x/3.x property names):

```json
{
  "name": "orders-outbox",
  "config": {
    "connector.class": "io.debezium.connector.postgresql.PostgresConnector",
    "plugin.name": "pgoutput",
    "database.hostname": "orders-db",
    "database.port": "5432",
    "database.user": "debezium",
    "database.password": "${file:/secrets/db.properties:password}",
    "database.dbname": "orders",
    "topic.prefix": "orders-db",
    "slot.name": "orders_outbox_slot",
    "table.include.list": "public.outbox",
    "tombstones.on.delete": "false",
    "heartbeat.interval.ms": "10000",
    "transforms": "outbox",
    "transforms.outbox.type": "io.debezium.transforms.outbox.EventRouter",
    "transforms.outbox.table.field.event.id": "id",
    "transforms.outbox.table.field.event.key": "aggregateid",
    "transforms.outbox.table.field.event.payload": "payload",
    "transforms.outbox.route.by.field": "aggregatetype",
    "transforms.outbox.route.topic.replacement": "outbox.event.${routedByValue}",
    "transforms.outbox.table.fields.additional.placement": "type:header:eventType"
  }
}
```

What this does:

- Captures only the `outbox` table. Each inserted row becomes one Kafka record on topic
  `outbox.event.Order` (from `aggregatetype`), keyed by `aggregateid` so all events of one order
  stay in order, with the payload as the value and the event id and type in headers.
- The router ignores row deletions, so the application can **delete outbox rows** right after
  inserting them (even in the same transaction) or with a periodic cleanup job; the insert is
  already in the WAL. This keeps the table small.
- `heartbeat.interval.ms` makes Debezium confirm progress periodically, so the replication slot can
  advance even when the captured table is quiet.

Guarantees and pitfalls:

- **At-least-once**: after a connector restart, Debezium may re-emit events since its last stored
  offset. Consumers must deduplicate on the event id header.
- **Replication slot retention**: PostgreSQL keeps WAL until the slot's consumer confirms it. If
  Debezium is down for hours, WAL piles up and can **fill the disk** of the primary. Monitor slot
  lag (`pg_replication_slots`) and set `max_slot_wal_keep_size` (PostgreSQL 13+) as a safety limit.
- Failover: logical replication slots historically were not carried over to a promoted replica;
  newer PostgreSQL versions add failover slots. Check your version and managed-service support.
- Schema the `payload` as a versioned contract; it is a public API.
- Requires `wal_level=logical` and a user with replication privileges.

| | Polling publisher | Debezium CDC |
| --- | --- | --- |
| Latency | Poll interval | Sub-second |
| DB load | Repeated queries | Reads the WAL stream |
| Infrastructure | None extra | Kafka Connect + Debezium |
| Ordering | Needs care with multiple relays | Commit order from the log |
| Failure risk | Table growth | WAL retention filling disk |

---

## The inbox pattern

The mirror image on the consumer side. On receipt, the consumer writes the message into an
**inbox** table (primary key = message id) and acks; a local worker processes inbox rows.

```text
 broker ──► consumer: INSERT INTO inbox (message_id, payload) ON CONFLICT DO NOTHING; ack
                         │
                         ▼
            inbox worker: BEGIN; process row; mark processed (or delete); COMMIT
```

Benefits: deduplication (unique key), fast acks so the broker is not held up by slow processing,
and a durable local record for retries. It is essentially the processed-message table combined with
a local work queue.

---

## Event-driven architecture patterns

Martin Fowler's article "What do you mean by Event-Driven?" separates four patterns that are often
conflated.

### Event notification

The event says **something happened** with minimal data; consumers call back for details.

```json
{ "type": "OrderPlaced", "orderId": "42", "occurredAt": "2026-10-31T11:50:00Z" }
```

Low coupling of payload, but consumers make synchronous calls back to the source, which
re-introduces runtime coupling and load on the source.

### Event-carried state transfer

The event carries **the state consumers need**, so they keep a local copy and never call back.

```json
{
  "type": "CustomerUpdated",
  "customerId": "c-7",
  "version": 12,
  "name": "Ada Lovelace",
  "tier": "GOLD",
  "shippingAddress": { "city": "London", "postcode": "N1 9GU" }
}
```

Consumers are autonomous and resilient to the source being down; data is duplicated and eventually
consistent. Pairs naturally with **compacted topics** keyed by entity id.

### Event sourcing

The **events are the source of truth**. State is derived by replaying them; you never update in
place.

```java
public sealed interface AccountEvent permits Opened, Deposited, Withdrawn {
    long version();
}
public record Opened(String accountId, long version) implements AccountEvent {}
public record Deposited(long amountCents, long version) implements AccountEvent {}
public record Withdrawn(long amountCents, long version) implements AccountEvent {}

public record Account(String id, long balanceCents, long version) {
    public static Account replay(List<AccountEvent> events) {
        Account acc = null;
        for (AccountEvent e : events) {
            acc = switch (e) {
                case Opened o -> new Account(o.accountId(), 0, o.version());
                case Deposited d -> new Account(acc.id(), acc.balanceCents() + d.amountCents(), d.version());
                case Withdrawn w -> new Account(acc.id(), acc.balanceCents() - w.amountCents(), w.version());
            };
        }
        return acc;
    }
}
```

- Appends use **optimistic concurrency**: "append event version 13 only if the stream is at 12".
- **Snapshots** avoid replaying long histories.
- Benefits: full audit trail, temporal queries, rebuild new read models from history.
- Costs: event schema evolution is forever, GDPR deletion is hard (crypto-shredding helps), and
  queries need projections.
- Kafka alone is a poor primary event store (no per-entity optimistic concurrency, per-entity
  reads are inefficient); use a database or a dedicated store (EventStoreDB, Axon Server) and
  publish to Kafka.

### CQRS

**Command Query Responsibility Segregation**: separate write model (commands, invariants) from
read models (denormalised views optimised per query), synchronised by events.

```text
 Commands ──► Write model (DB, invariants) ──events──► Projector ──► Read model(s)
                                                                    (Elasticsearch,
 Queries  ◄──────────────────────────────────────────────────────── Redis, SQL views)
```

Read models lag the write model; handle read-your-writes in the UI (return the new state from the
command, or wait for the projection version). Use CQRS where read and write shapes truly differ,
not by default.

---

## Schema evolution

Producers and consumers are deployed independently, so message formats must evolve without breaking
either side.

### Formats

| | JSON (no schema) | JSON Schema | Avro | Protobuf |
| --- | --- | --- | --- | --- |
| Size | Large | Large | Compact (no field names on wire) | Compact (field numbers on wire) |
| Schema needed to read | No | Optional | **Yes**, writer's schema | No for parsing; yes for meaning |
| Evolution rules | Convention only | Validator-based | Defaults + resolution rules | Field numbers, never reuse |
| Typical home | Webhooks, small systems | REST-like events | Kafka + Schema Registry, Hadoop | gRPC, Kafka |

Avro example: adding an optional field with a default is safe in both directions.

```json
{
  "type": "record",
  "name": "OrderPlaced",
  "namespace": "com.example.orders",
  "fields": [
    { "name": "orderId",     "type": "string" },
    { "name": "amountCents", "type": "long" },
    { "name": "currency",    "type": "string", "default": "EUR" },
    { "name": "couponCode",  "type": ["null", "string"], "default": null }
  ]
}
```

Protobuf rules: never change a field's number or type, never reuse a deleted number (mark it
`reserved`), and new fields are optional by nature.

```protobuf
syntax = "proto3";
package orders.v1;

message OrderPlaced {
  string order_id = 1;
  int64 amount_cents = 2;
  string currency = 3;
  reserved 4;                 // was "legacy_status"; never reuse
  reserved "legacy_status";
  optional string coupon_code = 5;
}
```

### Schema registry

A schema registry (Confluent Schema Registry, Apicurio, AWS Glue Schema Registry) stores versioned
schemas per **subject** (by default `<topic>-value` and `<topic>-key`). Serializers register or look
up the schema and prepend its id to each message. In Confluent's wire format that is one magic byte
(0) plus a 4-byte schema id, then the encoded payload. Deserializers fetch the writer's schema by id
and resolve it against the reader's schema.

### Compatibility modes

| Mode | Guarantee | Allowed changes (Avro) | Upgrade order |
| --- | --- | --- | --- |
| `BACKWARD` (default) | New schema can read data written with the **previous** schema | Delete fields; add fields **with defaults** | Consumers first |
| `BACKWARD_TRANSITIVE` | New schema can read **all** previous versions | Same, checked against all | Consumers first |
| `FORWARD` | **Previous** schema can read data written with the new schema | Add fields; delete fields **with defaults** | Producers first |
| `FORWARD_TRANSITIVE` | All previous schemas can read new data | Same, checked against all | Producers first |
| `FULL` | Both backward and forward with the previous version | Add or delete only fields with defaults | Any order |
| `FULL_TRANSITIVE` | Both, against all versions | Same | Any order |
| `NONE` | No checks | Anything | Coordinated big-bang |

For long-retention topics that consumers may replay from the beginning, use a **transitive** mode:
a new consumer must read every version still in the log.

---

## Backpressure and consumer lag

### Backpressure mechanisms by system

- **Kafka** is pull-based, so consumers are never overwhelmed; the backlog simply grows on disk.
  Inside a consumer, use `pause(partitions)` / `resume(partitions)` when a downstream dependency is
  saturated, while still calling `poll()` to stay in the group. Producers are slowed by broker
  **quotas** and block in `send()` when `buffer.memory` is full (up to `max.block.ms`).
- **RabbitMQ** pushes, bounded by **prefetch**. When memory or disk crosses a watermark, the broker
  **blocks publishing connections** (flow control). Queue length limits with `reject-publish`
  overflow push errors back to publishers.
- **SQS** consumers pull; limit in-flight work with the number of workers and batch sizes. The
  in-flight message quota (about 120,000 for standard queues) bounds unacked messages.
- At the edge, when the backlog is beyond what you can process in an acceptable time, **shed load**:
  return 429/503, drop or sample low-priority work.

### Consumer lag

Lag for a partition = **log end offset − committed offset**. Offsets are not time, so also track
**time lag** (age of the oldest unprocessed record); 10,000 records of lag may be 1 second or 1 hour.

```bash
kafka-consumer-groups.sh --bootstrap-server broker1:9092 --describe --group billing-service

# GROUP            TOPIC   PARTITION  CURRENT-OFFSET  LOG-END-OFFSET  LAG    CONSUMER-ID ...
# billing-service  orders  0          1048200         1048230         30     consumer-1-...
# billing-service  orders  1          990120          1032000         41880  consumer-2-...
```

Partition 1 lagging alone suggests a hot key, a slow consumer instance, or a poison record, not a
capacity problem. Lag that grows on every partition means you need more consumers (up to the
partition count) or faster processing.

| System | Metrics to alert on |
| --- | --- |
| Kafka | Consumer lag (records and time), under-replicated partitions, ISR shrink rate, offline partitions, request latency, rebalance rate |
| RabbitMQ | Queue depth, unacked count, publish vs deliver rate, memory/disk alarms, consumer count |
| SQS | `ApproximateNumberOfMessagesVisible`, `ApproximateAgeOfOldestMessage`, `NumberOfMessagesSent` to the DLQ |

Autoscale consumers on lag (KEDA has Kafka, RabbitMQ, and SQS scalers), with the partition count as
a ceiling for Kafka.

Resetting offsets (consumers in the group must be stopped):

```bash
kafka-consumer-groups.sh --bootstrap-server broker1:9092 --group billing-service \
  --topic orders --reset-offsets --to-datetime 2026-10-31T00:00:00.000 --execute
```

---

## Stream processing intro

Stream processing runs continuous computations over unbounded event streams: aggregations, joins,
enrichment, pattern detection.

### Core concepts

- **Event time vs processing time**: when the event happened versus when it is processed. Use event
  time for correctness; events arrive late and out of order.
- **Watermarks** (Flink): an assertion that no events older than time T are expected, which lets a
  window close. Late events beyond the allowed lateness are dropped or sent to a side output.
- **Windows**:

```text
 Tumbling (5m):        [00:00-00:05) [00:05-00:10) [00:10-00:15)  fixed, no overlap
 Hopping (5m, adv 1m): [00:00-00:05) [00:01-00:06) [00:02-00:07)  overlapping
 Sliding:              defined by max time difference between two events (joins)
 Session (gap 30m):    [ e e  e ]          [ e e ]                ends after 30m idle
```

- **Stateful processing**: counts, joins, and deduplication need state. Kafka Streams keeps it in
  local **RocksDB** stores backed by compacted **changelog topics**; Flink keeps it in state
  backends and takes periodic **checkpoints** (asynchronous barrier snapshots) to durable storage.
- **Exactly-once**: Kafka Streams uses Kafka transactions (`exactly_once_v2`); Flink restores state
  and source offsets from a checkpoint and uses two-phase-commit sinks for external outputs.

### Kafka Streams example

```java
Properties props = new Properties();
props.put(StreamsConfig.APPLICATION_ID_CONFIG, "page-view-counter");
props.put(StreamsConfig.BOOTSTRAP_SERVERS_CONFIG, "broker1:9092");
props.put(StreamsConfig.PROCESSING_GUARANTEE_CONFIG, StreamsConfig.EXACTLY_ONCE_V2);
props.put(StreamsConfig.DEFAULT_KEY_SERDE_CLASS_CONFIG, Serdes.StringSerde.class);
props.put(StreamsConfig.DEFAULT_VALUE_SERDE_CLASS_CONFIG, Serdes.StringSerde.class);

StreamsBuilder builder = new StreamsBuilder();
builder.stream("page-views", Consumed.with(Serdes.String(), Serdes.String()))   // key = pageId
       .groupByKey()
       .windowedBy(TimeWindows.ofSizeAndGrace(Duration.ofMinutes(5), Duration.ofMinutes(1)))
       .count(Materialized.as("views-per-5m"))
       .toStream()
       .map((windowedKey, count) -> KeyValue.pair(
               windowedKey.key() + "@" + windowedKey.window().startTime(), count.toString()))
       .to("page-view-counts", Produced.with(Serdes.String(), Serdes.String()));

KafkaStreams streams = new KafkaStreams(builder.build(), props);
Runtime.getRuntime().addShutdownHook(new Thread(streams::close));
streams.start();
```

| | Kafka Streams | Apache Flink |
| --- | --- | --- |
| Deployment | A library inside your Java app | A cluster (JobManager + TaskManagers), or managed |
| Sources/sinks | Kafka only | Kafka, files, databases, many connectors |
| State | RocksDB + changelog topics | Pluggable backends + checkpoints/savepoints |
| Scaling | Up to the input partition count | Operator parallelism, independent of partitions |
| Best for | Kafka-centric microservices | Large, complex pipelines, event-time heavy analytics, SQL |

---

## Common mistakes

- Using `acks=1` or leaving `min.insync.replicas=1` for important topics.
- Auto-commit plus asynchronous processing (silent message loss).
- Non-idempotent consumers in an at-least-once system.
- Dual writes (DB then broker) instead of an outbox.
- Increasing partitions on a keyed topic and breaking per-key ordering.
- Low-cardinality or skewed keys causing hot partitions.
- Slow processing exceeding `max.poll.interval.ms`, causing rebalance storms.
- Unbounded retries on poison messages (blocked partition or infinite redelivery).
- RabbitMQ without publisher confirms or without prefetch.
- SQS visibility timeout shorter than processing time (concurrent duplicates).
- A DLQ nobody monitors or redrives.
- Breaking schema changes with no registry or compatibility checks.
- Assuming Kafka EOS covers database writes or HTTP calls.
- Leaving a Debezium replication slot unmonitored until the database disk fills.

---

## Interview questions

1. **Queue vs log: when would you pick each?**
   A queue deletes messages on ack and gives per-message retry and competing consumers, which suits
   task distribution. A log retains messages, supports replay and many independent consumer groups
   with per-partition ordering, which suits event streaming, CDC, and analytics.

2. **How does Kafka avoid losing an acknowledged write when a broker dies?**
   With `acks=all` the leader acks only after all ISR members have the record, and
   `min.insync.replicas=2` (RF=3) refuses writes if fewer than 2 replicas are in sync. On failure
   the controller elects a new leader from the ISR, which has every committed record. Unclean
   leader election must be disabled.

3. **What is the ISR and the high watermark?**
   The ISR is the leader plus followers caught up within `replica.lag.time.max.ms`. The high
   watermark is the minimum log end offset across the ISR; consumers only read below it, so they
   never see records that could vanish on failover.

4. **Explain the idempotent producer.**
   The broker assigns a producer id and epoch; the producer numbers batches per partition. The
   broker drops batches with already-seen sequence numbers, so retries do not create duplicates and
   ordering is preserved with up to 5 in-flight requests. It covers one producer session only.

5. **How do Kafka transactions give exactly-once, and what are the limits?**
   A transactional producer writes output records and the consumer's offsets in one atomic
   transaction coordinated via `__transaction_state` and commit markers; `read_committed` consumers
   skip aborted data; zombie producers are fenced by epoch. It only covers Kafka-to-Kafka; external
   side effects need idempotency.

6. **What happens during a consumer group rebalance and how do you minimise its impact?**
   Partitions are reassigned when members join or leave. Eager rebalancing stops the whole group.
   Use the cooperative sticky assignor or the KIP-848 protocol, static membership for rolling
   restarts, keep processing per poll well under `max.poll.interval.ms`, and commit offsets in
   `onPartitionsRevoked`.

7. **You need per-customer ordering but one customer produces 40% of the traffic. What do you do?**
   Check whether ordering is really needed per customer or per order/account; key by the finer
   entity if possible. Otherwise isolate the hot key to dedicated partitions, salt it if partial
   ordering is acceptable, and use key-ordered parallelism inside the consumer.

8. **Design reliable "save order and publish OrderPlaced".**
   Transactional outbox: insert the order and an outbox row in one DB transaction. Debezium reads
   the WAL and publishes to a topic keyed by order id via the EventRouter. Consumers dedupe on the
   event id. Monitor replication slot lag.

9. **How do you handle a message that always fails?**
   Bound retries, classify errors (permanent errors go straight to the DLQ), publish to a DLQ with
   original coordinates and error details, alert on DLQ depth, fix, and redrive. In Kafka, use retry
   topics for non-blocking retries if ordering allows, otherwise blocking retries with pause.

10. **Compare RabbitMQ and Kafka for a background job system that sends emails.**
    RabbitMQ (or SQS) fits: per-message acks, competing consumers, built-in DLX and TTL-based delayed
    retries, no need for replay. Kafka works but needs retry topics and partition-bound parallelism.

11. **What is the SQS visibility timeout and what goes wrong if it is too short?**
    A received message is hidden for that period; if not deleted it reappears. Too short and another
    consumer gets it while the first is still processing, causing concurrent duplicates. Set it above
    p99 processing time and extend it with `ChangeMessageVisibility` for long jobs.

12. **How do you size Kafka partitions?**
    Divide target throughput by measured per-partition producer and consumer throughput, take the
    maximum, ensure it covers the desired consumer parallelism, and add headroom since keyed topics
    should not be repartitioned later. Avoid excessive partitions due to per-partition overhead.

13. **Backward vs forward compatibility in a schema registry?**
    Backward: the new schema can read old data, so upgrade consumers first (add fields with
    defaults). Forward: old schemas can read new data, so upgrade producers first. Full means both.
    Use transitive modes for topics consumers replay from the start.

14. **Event notification vs event-carried state transfer vs event sourcing?**
    Notification signals a change and consumers call back for data. State transfer includes the data
    so consumers keep local copies. Event sourcing makes the event log the system of record and
    derives state by replay.

15. **Consumer lag is growing on one partition only. What do you check?**
    A hot key in that partition, a poison message being retried, a slow or stuck consumer instance
    (GC, a slow dependency), or uneven assignment. Look at per-partition bytes-in, the consumer's
    logs and processing time, and the record at the stuck offset.

---

## Summary

- Async messaging trades immediacy for decoupling, buffering, and fan-out; consumers must handle
  duplicates, reordering, and eventual consistency.
- **Queue** = delete on ack, per-message retry; **log** = retained, replayable, ordered per
  partition; **pub/sub** = fan-out semantics on either.
- Delivery is at-most-once or at-least-once; "exactly-once" is at-least-once plus idempotency or
  atomic commit of offsets and outputs.
- **Kafka durability**: RF=3, `acks=all`, `min.insync.replicas=2`, unclean election off, idempotent
  producer on. Order per partition; key = ordering scope and load distribution.
- **Kafka consumers**: manual commit after processing, cooperative or KIP-848 rebalancing, static
  membership, lag in records and time.
- **RabbitMQ**: exchanges route to queues; use quorum queues, publisher confirms, manual acks,
  prefetch, DLX with delivery limits.
- **SQS**: visibility timeout, long polling, redrive policy and DLQ redrive; FIFO for per-group
  order and dedup; SNS → SQS for fan-out.
- **Reliability patterns**: outbox (Debezium CDC), inbox, idempotent consumers, retry topics, DLQs.
- **Evolution**: schema registry with explicit compatibility mode; Avro defaults, Protobuf field
  numbers.

| Need | Choice |
| --- | --- |
| Event streaming, replay, CDC | Kafka (or Pulsar) |
| Task queue with routing and per-message retry | RabbitMQ quorum queues |
| Zero-ops queue on AWS | SQS (+ SNS fan-out) |
| DB change + event atomically | Outbox + Debezium |
| Duplicate-safe processing | Processed-message table in the same transaction |
| Kafka-to-Kafka exactly-once | Transactions / Kafka Streams `exactly_once_v2` |

---

## Further reading

- Gwen Shapira, Todd Palino, Rajini Sivaram, Krit Petty, *Kafka: The Definitive Guide*, 2nd
  edition (O'Reilly, 2021).
- Gregor Hohpe and Bobby Woolf, *Enterprise Integration Patterns* (Addison-Wesley, 2003).
- Martin Kleppmann, *Designing Data-Intensive Applications* (O'Reilly), chapter 11 "Stream
  Processing" (and chapter 4 on encoding and evolution).
- Apache Kafka documentation: "Design" and "Operations" sections, and the KIPs referenced here
  (KIP-98 exactly-once, KIP-429 cooperative rebalancing, KIP-345 static membership, KIP-500 and
  KRaft, KIP-848 consumer protocol, KIP-405 tiered storage, KIP-932 share groups).
- RabbitMQ documentation: "Reliability Guide", "Quorum Queues", "Consumer Acknowledgements and
  Publisher Confirms", "Dead Letter Exchanges".
- Amazon SQS Developer Guide: visibility timeout, FIFO queues, dead-letter queues.
- Debezium documentation: PostgreSQL connector and "Outbox Event Router".
- Chris Richardson, *Microservices Patterns* (Manning, 2018): transactional outbox, sagas,
  idempotent consumer.
- Martin Fowler, "What do you mean by 'Event-Driven'?" (2017).
- Jay Kreps, "The Log: What every software engineer should know about real-time data's unifying
  abstraction" (2013).
- Tyler Akidau, Slava Chernyak, Reuven Lax, *Streaming Systems* (O'Reilly, 2018).
