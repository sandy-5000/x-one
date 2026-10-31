---
title: "CAP & consistency"
order: 6
---
# CAP Theorem, Consistency Models and Coordination

When data lives on more than one machine, answer two questions for any system: **what can a client
observe?** and **what happens when something fails?**

## Key points

- Networks fail **partially**; no reply means request lost, server dead, slow, or response lost—retries need **idempotency**.
- **CAP** (Gilbert–Lynch): during a **partition**, a replicated register cannot be both **linearizable** and **available** (every non-failing node responds).
- **P is not optional**; "CA" only means you have not decided partition behaviour yet.
- **PACELC**: without partition, trade **latency vs consistency** (local replica read vs quorum sync).
- **Linearizability** = single copy, real-time order; **serializability** = txn order exists, not necessarily real-time—**strict serializability** is both.
- **Quorums**: `W + R > N` gives overlap, not linearizability (sloppy quorums, LWW, ABD read repair aside).
- **Consensus** (Raft/Paxos): `2f+1` nodes tolerate `f` failures; commit current-term entries only (Raft Figure 8).
- **Locks**: leases expire under GC pauses; **fencing tokens** checked by the resource are mandatory for correctness.
- Cross-service atomicity: **sagas + outbox**, not naive 2PC across HTTP; **exactly-once delivery** is impossible—aim for idempotent **effects**.
- Classify **operations and config**, not product labels ("Cassandra is AP").

---

## Why distributed systems are hard

### Fallacies (selected)

| Fallacy | Design consequence |
| ------- | ------------------ |
| Network is reliable | Retries, idempotency, timeouts |
| Latency is zero | Batch, locality, avoid chatty RPC |
| Topology doesn't change | Service discovery, no hard-coded peers |

### Failure models

| Model | Faulty node | Consensus size |
| ----- | ------------- | -------------- |
| Crash-stop | Halts forever | 2f+1 for consensus |
| Crash-recovery | Restarts with durable state | Raft, most DBs |
| Byzantine | Arbitrary lies | 3f+1 (blockchains) |

**Timing**: fully async → cannot distinguish slow from dead (FLP). Raft/Paxos assume **partial synchrony** for liveness; **safety** holds even if async.

```text
 Client sends request, gets no reply:
 (1) request lost  (2) server crashed  (3) server slow  (4) response lost
 Case (4): work was done — blind retry may duplicate
```

Use **monotonic clocks** (`nanoTime`) for timeouts, not wall clock.

---

## CAP theorem

**Definitions (proof)**:

- **Consistency**: linearizable single register (total order matching real time).
- **Availability**: every non-failing node must respond (no time bound).
- **Partition tolerance**: arbitrary message loss between nodes.

**During partition**: either refuse some requests (**CP**) or answer with stale/wrong order (**AP**).

```text
 Partition between G1 and G2
 Client A writes v1 on G1 (ok) → Client B reads G2 → still v0 → not linearizable
 Unless G2 refuses read or G1 refused write
```

### Misconceptions

| Myth | Reality |
| ---- | ------- |
| Pick two of CAP | P happens; choice is during partition |
| CAP C = ACID C | CAP C is linearizability |
| AP always wrong | Fine when staleness bounded |
| Product is CP or AP | Per-operation tuning (Cassandra CL) |

Brewer (2012): detect partition, limit ops in partition mode, reconcile after.

---

## PACELC

> If **P**artition → **A** or **C**; **E**lse → **L**atency or **C**onsistency.

| System (typical) | Partition | Normal | Notes |
| ---------------- | --------- | ------ | ----- |
| Cassandra | PA | EL | `QUORUM` both sides → more PC/EC |
| DynamoDB | PA | EL | Strong reads opt-in |
| Redis async repl | PA | EL | Failover may lose writes |
| etcd | PC | EC | Linearizable reads default |
| Spanner | PC | EC | Commit-wait + Paxos |

Coordination costs latency; during partition coordination may be impossible.

---

## Consistency models

Stronger models = fewer allowed histories = more coordination.

```text
 Strict serializability
   /                    \
Linearizability    Serializability (txns)
        │
 Sequential → Causal → PRAM/FIFO → Session guarantees → SEC → Eventual
```

| Model | Real-time order | Same order all clients | Available under partition |
| ----- | --------------- | ------------------------ | ------------------------- |
| Linearizable | Yes | Yes | No |
| Sequential | No | Yes | No |
| Causal | For causal deps | Partial | Yes |
| Session (RYW, MR, …) | No | Per session | Yes with routing |
| Strong eventual (CRDT) | No | Convergent merge | Yes |
| Eventual | No | No | Yes |

### Session guarantees

| Guarantee | Promise |
| --------- | ------- |
| Read-your-writes | After write, you see it |
| Monotonic reads | Never go backwards in time |
| Monotonic writes | Your writes apply in order everywhere |
| Writes-follow-reads | Write ordered after read version |

**RYW on replicas**: store commit LSN/GTID; route to caught-up replica or primary briefly.

### Eventual vs SEC

Eventual: if writes stop, replicas converge—**no bound on when or to what value**.

**SEC** (CRDTs): same update set → same state immediately without app merge.

---

## Linearizability vs serializability

| | Scope | Real time? |
| --- | --- | --- |
| Linearizability | Single ops | Yes |
| Serializability | Multi-object txns | No (any serial order) |
| Strict serializability | Txns | Yes |

Snapshot isolation ≠ serializable (write skew). PostgreSQL SSI and single-node serializable DBs aim higher.

---

## Quorums

`W + R > N` → read and write sets overlap.

| N | W | R | Notes |
| --- | --- | --- | --- |
| 3 | 2 | 2 | Balanced default |
| 3 | 1 | 1 | No overlap guarantee |
| 5 | 3 | 3 | Tolerates 2 down each side |

**W > N/2**: two majorities intersect—conflicting write majors can't both succeed.

**Not linearizable because**:

1. Reader sees new then old during in-flight write.
2. Failed write may leave value on one replica.
3. **Sloppy quorum** + hinted handoff breaks overlap.
4. **LWW** may discard newer write (clock skew).

**Sloppy quorum**: write goes to substitute node with hint for home replica—availability over strict overlap.

**Read repair**: fix stale replicas on read. **Merkle trees**: O(log n) compare for anti-entropy.

---

## Conflict resolution

| Strategy | Risk |
| -------- | ---- |
| LWW (timestamp) | Silent loss if clocks skew |
| Version vectors | Detect concurrency → siblings |
| CRDTs | Deterministic merge; limited types |

**Version vector rule**: V ≤ W if all entries ≤; else concurrent.

**CRDT examples**: G-Counter (per-replica max merge), PN-Counter (P−N), OR-Set (add wins over observed remove).

---

## Clocks and ordering

| Clock | Use | Don't use for |
| ----- | --- | ------------- |
| Wall (`Instant`) | Human timestamps | Cross-node ordering |
| Monotonic | Timeouts, duration | Cross-machine compare |

**Lamport**: local event `L++`; on receive `L = max(L, Lm)+1`. If a→b then L(a)<L(b); converse false.

**Vector clocks**: one counter per process—exact causality; O(n) size.

**HLC** `(wall, logical)`: causal like Lamport, stays near physical time; Cockroach/Yugabyte/Mongo cluster time. Reject messages beyond max skew (e.g. 500 ms).

**Spanner TrueTime**: interval `[earliest, latest]`; **commit-wait** until `earliest > commit_ts` → external consistency (~2ε latency).

---

## Consensus

Properties: agreement, validity, integrity, termination (under partial sync).

**FLP**: no deterministic consensus in fully async with one crash.

### Raft (sketch)

- **Terms** as logical clock; one leader per term.
- Election: randomized timeout → `RequestVote`; vote if log **at least as up-to-date**; majority wins.
- Log replication: `AppendEntries` with `prevLogIndex/Term`; conflicts truncated.
- **Commit**: current-term entry on majority; never commit prior-term entry by count alone (Figure 8)—no-op entry common.
- **Reads**: ReadIndex / lease read after confirming leadership.
- **Membership**: one-server-at-a-time changes (etcd) or joint consensus.
- Size: **2f+1** tolerates f; prefer odd counts (4 nodes ≠ more tolerance than 3).

```text
 Leader log:  [1:x][1:y][2:z][3:a]  commitIndex on majority of current term
 Follower mismatch → delete suffix, append leader entries
```

### Paxos / ZAB

Paxos: prepare/promise, accept—Multi-Paxos ≈ Raft with different packaging. ZAB: ZooKeeper epochs + broadcast; reads may be stale unless `sync()`.

| Use | Algorithm |
| --- | --------- |
| etcd, Consul | Raft |
| ZooKeeper | ZAB |
| KRaft (Kafka metadata) | Raft variant |
| Cockroach/TiKV ranges | Raft |

Kafka **data** ISR ≠ Raft.

### Raft linearizable reads (options)

| Method | Cost | Idea |
| ------ | ---- | ---- |
| Log read | Highest | Append read to replicated log |
| ReadIndex | Medium | Confirm leadership + apply through commitIndex |
| Lease read | Lower | Leader assumes exclusive window < election timeout |

Never serve writes/reads from a leader that might be deposed without one of these checks.

---

## Leases, fencing, locks

**Problem**: holder paused (GC); lease expires; new holder writes; old holder writes too.

**Fencing token**: monotonic with each grant; storage rejects `token < highest_seen`.

```sql
UPDATE jobs SET owner = ?, fencing_token = 34
WHERE id = 7 AND fencing_token <= 34;
```

| Lock | Correctness? |
| ---- | ------------ |
| Redis `SET NX PX` | Efficiency only; async repl loses lock |
| Redlock | Timing assumptions; no standard fencing |
| ZooKeeper / etcd | Consensus + session; still fence writes |
| DB advisory lock | Tied to connection/txn |

---

## Distributed transactions

### 2PC

Prepare (vote, hold locks) → coordinator logs commit → commit all.

**Blocking**: coordinator dies after YES votes → participants wait (**in doubt**).

Needs XA-capable participants; rare across microservices.

### Sagas

Local txns T1…Tn with compensations C1…Cn. **No isolation**—visible intermediate state; compensations idempotent.

| | Choreography | Orchestration |
| --- | --- | --- |
| Flow | Events between services | Central orchestrator + durable log |
| Visibility | Hard to trace | Explicit |

**Outbox**: write event in same DB txn as business row; relay publishes—at-least-once without 2PC.

### TCC

Try (reserve) → Confirm / Cancel; better isolation than saga, more participant API surface.

| | 2PC | Saga | TCC |
| --- | --- | --- | --- |
| Blocking | Yes | No | No |
| Isolation | Locks | None | Reservations |

---

## Idempotency and exactly-once

Network: at-most-once or at-least-once delivery only.

**Effectively-once**: at-least-once + idempotent ops or dedup table (same txn as effect) or atomic offset+output (Kafka transactions)—**boundary stops at broker**.

API **Idempotency-Key**: store key + request hash + response; reject hash mismatch.

---

## Jepsen (lesson)

Real partitions expose lost writes, stale leader reads, and docs overstating guarantees—check analyses for your version and settings.

---

## Pitfalls

- "CA database" or "pick two of CAP."
- `W+R>N` ⇒ linearizable.
- LWW across regions without accepting data loss.
- Redis/Redlock for correctness without fencing on storage.
- 2-node or 4-node Raft "for HA" (2 nodes tolerate 0 failures; 4 like 3 for quorum).
- Raft across 2 DCs (lose DC with 2 of 3 → no quorum)—use 3 sites.
- Saga compensation treated as rollback; non-idempotent retries on timeout.
- "Exactly-once" assumed for email/HTTP side effects.

---

## Quick check

**1. During a partition, what are the only CAP choices for a write?**  
Refuse the write (sacrifice availability) or accept it without all replicas updated (sacrifice linearizability).

**2. PACELC: classify Cassandra default vs Spanner.**  
Cassandra: PA / EL (tunable toward PC/EC with quorums). Spanner: PC / EC (consensus + commit-wait).

**3. Difference between linearizability and serializability?**  
Linearizability: each op atomic at a point in real time on one object. Serializability: transactions equivalent to some serial order—not necessarily real-time.

**4. Why can't sloppy quorums preserve strong consistency?**  
Writes may land on non-home nodes; read quorums on home replicas can miss those writes until hinted handoff completes.

**5. What makes a lock safe for correctness under pauses?**  
Consensus-backed lease **plus** fencing token enforced by the resource (DB conditional on token/revision), not by the client alone.
