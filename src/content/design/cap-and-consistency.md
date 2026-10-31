---
title: "CAP & consistency"
order: 6
---
# CAP Theorem and Consistency

## CAP theorem

In a distributed data store, when a **network partition** happens you can keep only one of:

- **Consistency (C)** — every read sees the most recent write.
- **Availability (A)** — every request gets a non-error response.

**Partition tolerance (P)** is not optional: networks do fail. So the real choice is **CP or AP during a partition**.

```text
          Partition!
 ┌──────┐    ╳    ┌──────┐
 │Node A│ ───╳─── │Node B│
 └──────┘    ╳    └──────┘
 x = 2             x = 1  (has not received the update)

 Client reads x from B:
   CP → B refuses (error / timeout) until it can sync
   AP → B answers x = 1 (stale but available)
```

| Choice | Behaviour during partition | Examples | Fits |
| --- | --- | --- | --- |
| CP | Rejects some requests to stay correct | ZooKeeper, etcd, HBase, MongoDB (default) | Bank balances, inventory, leader election |
| AP | Always answers, may be stale | Cassandra, DynamoDB, CouchDB | Social feeds, likes, shopping carts |

### PACELC

CAP only talks about partitions. PACELC adds the normal case:

> If **P**artition, choose **A** or **C**; **E**lse, choose **L**atency or **C**onsistency.

Even with a healthy network, waiting for replicas to agree costs latency. DynamoDB and Cassandra are PA/EL; traditional relational clusters are PC/EC.

---

## Consistency models

From strongest to weakest:

- **Linearizability (strong):** the system behaves like a single copy; once a write completes, every later read sees it.
- **Sequential consistency:** all nodes see operations in the same order, though not necessarily in real time.
- **Causal consistency:** operations that are causally related (reply after post) are seen in order; unrelated ones may differ.
- **Read-your-writes:** a user always sees their own updates.
- **Monotonic reads:** a user never sees data go "back in time".
- **Eventual consistency:** if writes stop, all replicas converge eventually. No ordering guarantees in the meantime.

Stronger models are easier to reason about but need more coordination: more latency, lower availability.

---

## Quorums

With N replicas, write to W and read from R:

- **W + R > N** — read set and write set overlap, so a read sees the latest write.
- Typical: N = 3, W = 2, R = 2.
- W = 1, R = 1 — fast and available, but only eventually consistent.
- W = N — every write needs every replica; one dead node blocks writes.

---

## Consensus

Consensus lets a group of nodes agree on a single value (who is leader, what the next log entry is) even if some nodes fail.

- **Raft** — used by etcd, Consul, CockroachDB. Designed to be understandable.
- **Paxos** — the original; used inside Google Chubby and Spanner.
- **ZAB** — ZooKeeper's protocol.

### Raft in one picture

```text
1. Leader election
   follower ──timeout──► candidate ──majority votes──► leader

2. Log replication
   client ──► leader: append entry
   leader ──► followers: AppendEntries
   majority acked ──► entry committed ──► apply to state machine
```

A cluster of **2f + 1** nodes tolerates **f** failures: 3 nodes survive 1 failure, 5 nodes survive 2. That's why clusters use odd sizes.

---

## Distributed transactions

### Two-phase commit (2PC)

```text
Coordinator                 Participants
   │ ── PREPARE ──────────────► │  lock resources, vote
   │ ◄─────────── YES / NO ──── │
   │ ── COMMIT / ABORT ───────► │  apply or roll back
```

Atomic across databases, but **blocking**: if the coordinator dies after PREPARE, participants hold locks until it comes back. Rarely used across microservices.

### Saga

A sequence of local transactions; each step has a **compensating action** that undoes it on failure.

```text
Create order ─► Reserve stock ─► Charge card ─► Ship
                                     ✗ fails
              ◄─ Release stock ◄─ Cancel order   (compensations)
```

- **Choreography:** services react to each other's events. No central brain; harder to follow.
- **Orchestration:** a saga orchestrator tells each service what to do next. Easier to trace.

Sagas give eventual consistency, not isolation — other requests can see intermediate states.

---

## Clocks and ordering

Machine clocks drift, so "last write wins" by wall clock can silently drop data.

- **Lamport clocks** — a counter incremented on each event and on every message received; gives a consistent ordering.
- **Vector clocks** — one counter per node; can tell whether two events are ordered or **concurrent** (a conflict).
- **Hybrid logical clocks / TrueTime** — combine physical time with bounds on uncertainty (Spanner, CockroachDB).
