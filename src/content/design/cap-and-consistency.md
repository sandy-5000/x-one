---
title: "CAP & consistency"
order: 6
---
# CAP Theorem, Consistency Models and Coordination

These notes cover what goes wrong when data lives on more than one machine, what guarantees a
system can and cannot give you, and the protocols (quorums, clocks, consensus, locks, sagas) that
real systems use to get those guarantees. The goal is to be able to answer two questions about
any data system: **"what can a client observe?"** and **"what happens when something fails?"**

---

## Why distributed systems are hard

A single machine either works or crashes. A distributed system can be **partly broken**: one node
is slow, one link drops packets, one clock runs fast. Almost every hard problem in this page comes
from that.

### The eight fallacies of distributed computing

Peter Deutsch and colleagues at Sun Microsystems listed seven assumptions that newcomers make
(1994); James Gosling added the eighth. Each one is false, and each one has caused outages.

| Fallacy | Reality | Design consequence |
| --- | --- | --- |
| The network is reliable | Packets are dropped, duplicated, reordered; links flap; switches reboot | Retries, idempotency, timeouts, acknowledgements |
| Latency is zero | Same-DC round trip ~0.5 ms; cross-region 50–150 ms; tail latency far worse | Batch calls, avoid chatty protocols, put data near users |
| Bandwidth is infinite | NICs, cross-AZ links and egress are finite and cost money | Compression, pagination, do not ship whole tables |
| The network is secure | Anything on the wire can be read or forged | mTLS, authentication between services, zero trust |
| Topology doesn't change | Pods move, IPs change, nodes are replaced | Service discovery, no hard-coded addresses |
| There is one administrator | Different teams, clouds, and policies own each hop | Explicit contracts, versioned APIs, defensive configs |
| Transport cost is zero | Serialization CPU, egress fees, TLS handshakes | Measure; choose efficient encodings (Protobuf, Avro) |
| The network is homogeneous | Mixed hardware, MTUs, protocol versions, languages | Standard protocols, tolerant readers |

### Failure models

A failure model states what a faulty node is allowed to do. Algorithms are only correct under the
model they were designed for.

| Model | What a faulty node does | Typical assumption in | Tolerating f faults needs |
| --- | --- | --- | --- |
| **Crash-stop** (fail-stop) | Halts forever; never comes back | Textbook algorithms | f + 1 replicas for durability; 2f + 1 for consensus |
| **Crash-recovery** | Halts, may restart later with its **durable** state (in-memory state lost) | Raft, Paxos, ZAB, most databases | 2f + 1 for consensus; state must be fsynced before acknowledging |
| **Omission** | Drops some messages it should send or receive | Network models | Retries plus acknowledgements |
| **Byzantine** | Arbitrary behaviour: lies, sends conflicting messages, corrupts data | Blockchains, aerospace, PBFT | 3f + 1 replicas |

Almost every system you will design for work assumes **crash-recovery** with non-Byzantine nodes.
Byzantine tolerance is expensive and only worth it when nodes are run by mutually distrusting
parties. Checksums catch the most common "Byzantine-ish" fault in practice: corrupted bytes.

There is also a **timing model**:

- **Synchronous**: message delay and processing time have known upper bounds. Easy to reason
  about, but real networks do not behave like this.
- **Asynchronous**: no bounds at all. A slow node cannot be told apart from a dead one.
- **Partially synchronous**: the system is asynchronous for unknown periods but eventually behaves
  synchronously for long enough. This is the model Raft and Paxos assume for **liveness**; their
  **safety** holds even under full asynchrony.

### Partial failures

When you send a request and get no reply, you cannot tell which of these happened:

```text
  Client                         Server
    │ ── request ──X                        (1) request lost
    │ ── request ─────────────────► │       (2) server crashed before processing
    │ ── request ─────────────────► │ ...   (3) server is slow / GC pause / queueing
    │ ── request ─────────────────► │ done  (4) server processed it,
    │                 X── response ─┤           but the response was lost
```

In case (4) the work **was done**. Retrying blindly may do it twice (charge the card twice). This
is why every retried operation must be **idempotent** or carry an idempotency key.

### Timeouts and failure detection

A timeout is the only failure detector available in an asynchronous network, and it is always a
guess.

- **Too short**: a slow node is declared dead, its work is moved elsewhere, and load on the rest
  rises. This can cascade into more false positives. If the "dead" node was a leader, you may get
  two leaders.
- **Too long**: users wait, and recovery is slow.

A sensible starting point: timeout ≈ observed p99.9 latency × a safety factor, with exponential
backoff and jitter on retries. Better approaches adapt to measured behaviour:

- **Heartbeats**: each node pings peers every *h* ms; after *k* missed beats it is suspected.
- **Phi accrual detector** (Hayashibara et al., used by Cassandra and Akka): keeps the distribution
  of heartbeat inter-arrival times and outputs a suspicion level φ rather than a yes/no. The
  application picks a threshold (Cassandra's `phi_convict_threshold` defaults to 8).
- **Leases** (covered later) turn "is it alive?" into "it has the right to act until time T".

```java
/** Heartbeat failure detector with a fixed timeout. Uses a monotonic clock. */
public final class HeartbeatDetector {
    private final long timeoutNanos;
    private final Map<String, Long> lastSeen = new ConcurrentHashMap<>();

    public HeartbeatDetector(Duration timeout) {
        this.timeoutNanos = timeout.toNanos();
    }

    public void onHeartbeat(String nodeId) {
        lastSeen.put(nodeId, System.nanoTime());
    }

    public boolean isSuspected(String nodeId) {
        Long seen = lastSeen.get(nodeId);
        return seen == null || System.nanoTime() - seen > timeoutNanos;
    }
}
```

Note the use of `System.nanoTime()` (monotonic) and not `System.currentTimeMillis()` (wall clock,
which can jump backwards when NTP corrects it).

---

## The CAP theorem, precisely

### Origin and the formal definitions

Eric Brewer stated CAP as a conjecture in 2000. Seth Gilbert and Nancy Lynch proved it in 2002
("Brewer's conjecture and the feasibility of consistent, available, partition-tolerant web
services"). The proof uses specific, narrow definitions:

- **Consistency** means **atomic / linearizable** consistency for a single read/write register:
  there is a total order of all operations, consistent with real time, such that every read
  returns the value of the latest preceding write.
- **Availability**: *every request received by a non-failing node must result in a response*.
  No time bound is given, but the node may not refuse or wait forever.
- **Partition tolerance**: the network is allowed to lose arbitrarily many messages sent from one
  node to another.

**Theorem**: in an asynchronous network, no read/write register can guarantee both availability
and linearizability in every execution that includes a partition.

### Proof sketch

```text
  Partition: no messages get through
 ┌────────────┐    ╳    ┌────────────┐
 │  Node G1   │ ───╳─── │  Node G2   │
 │  v = v0    │    ╳    │  v = v0    │
 └────────────┘    ╳    └────────────┘
       ▲                       ▲
       │ write(v1)             │ read()
    Client A                Client B
```

1. Client A writes `v1` to G1. Availability says G1 must eventually acknowledge, even though it
   cannot reach G2.
2. Afterwards, client B reads from G2. Availability says G2 must answer.
3. G2 never heard about `v1`, so it returns `v0`. The read started after the write completed, so
   linearizability is violated. Contradiction.

The only escapes are for G1 to refuse the write or G2 to refuse the read. That is giving up
availability (a **CP** behaviour). Returning `v0` is giving up consistency (an **AP** behaviour).

### Common misconceptions

1. **"Pick any two of three."** P is not something you pick. Partitions happen whether or not you
   want them. CAP says what you must give up **while a partition is happening**.
2. **"My system is CA."** See the next subsection.
3. **"The C in CAP is the C in ACID."** No. ACID consistency means application invariants hold
   (balance ≥ 0). CAP consistency means linearizability, which is about recency and ordering of
   reads and writes.
4. **"An AP system is always inconsistent / a CP system is always unavailable."** With no
   partition, both can serve every request correctly. The difference only shows during a
   partition, or (see PACELC) in latency.
5. **"CAP availability = high availability (99.99%)."** CAP availability is all-or-nothing: every
   non-failing node must answer. A CP system that answers on the majority side of a partition is
   "unavailable" under CAP but can still have excellent measured uptime.
6. **"A database is CP or AP."** Most systems are tunable per operation. Cassandra at
   `CONSISTENCY ONE` behaves AP; with `QUORUM` reads and writes it refuses requests from the
   minority side. MongoDB depends on read and write concern. Classify **operations and
   configurations**, not products.
7. **"CAP covers all failures."** It only models network partitions. Node crashes, disk loss,
   latency spikes, and bugs are outside it.

### Why "CA" is not a real choice

A "CA" system would be consistent and available as long as there is no partition. But in any
system that communicates over a network, partitions **will** occur (switch failure, misconfigured
firewall, GC pause long enough to look like one). When one happens, the system must do
*something*: either answer (and risk being inconsistent) or not answer (and be unavailable). There
is no third option, so it was always really CP or AP, just without a plan.

A single-node database is "CA" only in the trivial sense that there is no network between replicas.
As soon as it has a replica it faces the same choice.

### "CAP twelve years later"

In 2012 Brewer wrote a follow-up ("CAP Twelve Years Later: How the 'Rules' Have Changed") making
these points:

- Partitions are rare, so a system should not give up C or A when there is no partition.
- The choice is made **per operation** and can be finer-grained than the whole system.
- Systems should **detect** partitions, enter an explicit **partition mode** that limits some
  operations, and run **partition recovery** afterwards: merge state, compensate for mistakes
  (for example an ATM allowing limited withdrawals while offline, then reconciling).

---

## PACELC

Daniel Abadi (2010, published 2012) pointed out that CAP says nothing about the normal case, where
the real trade-off is **latency versus consistency**:

> If there is a **P**artition, choose **A**vailability or **C**onsistency;
> **E**lse (normal operation), choose **L**atency or **C**onsistency.

Synchronous replication to a quorum before answering costs at least one round trip to other
replicas, possibly in other regions. Answering from the local replica is fast but may be stale.

| System (typical configuration) | During partition | Normal operation | Notes |
| --- | --- | --- | --- |
| Cassandra, ScyllaDB | PA | EL | Tunable per query; `QUORUM` on both sides pushes it towards PC/EC |
| Amazon DynamoDB | PA | EL | Eventually consistent reads by default; strongly consistent reads are opt-in and cost more |
| Riak (Dynamo-style) | PA | EL | Sloppy quorums, sibling values |
| Redis with async replicas / Redis Cluster | PA | EL | Can lose acknowledged writes on failover |
| MongoDB (replica set) | PA | EC | Abadi's classification; depends on read/write concern |
| Yahoo PNUTS | PC | EL | Per-record master; reads from local replica can be stale |
| ZooKeeper | PC | EL for reads, EC for writes | Reads served by any server may be stale; `sync()` first for fresh reads |
| etcd | PC | EC | Linearizable reads by default; `serializable` reads trade freshness for latency |
| HBase, Bigtable | PC | EC | One server owns each region / tablet |
| Google Spanner, CockroachDB | PC | EC | Consensus per range; Spanner adds commit-wait |
| VoltDB / H-Store | PC | EC | Synchronous replication |
| PostgreSQL primary + async replicas | PC on primary | EL if you read replicas | Synchronous replication gives EC at a latency cost |

The useful mental model: **consistency costs coordination, coordination costs latency, and during
a partition coordination may be impossible**.

---

## Consistency models

A consistency model is a contract: it says which histories of reads and writes a client is
allowed to observe. Stronger models permit fewer histories, which makes programs easier to
reason about but require more coordination.

```text
                    Strict serializability
                     /                  \
           Linearizability          Serializability
                 │                        │
        Sequential consistency     (transactional models:
                 │                  snapshot isolation,
        Causal consistency          repeatable read, ...)
            /          \
       PRAM / FIFO    Session guarantees (RYW, MR, MW, WFR)
            \          /
      Strong eventual consistency
                 │
        Eventual consistency
```

The left branch is about single operations on single objects. The right branch is about
multi-object transactions. Strict serializability combines both.

In the timelines below, `├──── op ────┤` shows when an operation was invoked (left) and when it
returned (right). Time flows left to right.

### Linearizability (atomic consistency)

**Definition** (Herlihy and Wing, 1990): every operation appears to take effect **instantaneously
at some point between its invocation and its response**, and the order of those points is
consistent with real time. The system behaves as if there is a single copy of the data.

```text
 x starts at 0

 A:  ├─────── write(x = 1) ───────┤
 B:        ├── read(x) → 1 ──┤                       OK: overlaps the write
 C:              ├── read(x) → 0 ──┤                 OK: also overlaps the write
 D:                                  ├── read → 0 ─┤ VIOLATION
```

D starts after A's write returned, so it must see 1. A subtler rule: once **any** read has returned
the new value, every read that starts afterwards must also see it. If B's read (returning 1)
completed before C's read began, C returning 0 would be illegal. That "no going back" rule is what
breaks naive quorum systems.

What needs linearizability:

- Leader election and locks (exactly one winner).
- Uniqueness constraints (one account per username).
- Cross-channel timing: a user uploads an image, then a message on a queue tells a resizer to
  fetch it. If the storage read is not linearizable, the resizer may see the old image.

How it is implemented: single-leader replication with reads from the leader (and a way to be sure
the leader is still the leader), or consensus (Raft, Paxos). Multi-leader and leaderless
replication generally are **not** linearizable.

### Sequential consistency

**Definition** (Lamport, 1979): the result is as if all operations ran in **some single total
order**, and each process's operations appear in that order in program order. Unlike
linearizability, the total order does **not** have to respect real time across processes.

```text
 P1:  ├─ write(x = 1) ─┤
 P2:                       ├─ read(x) → 0 ─┤   ├─ read(x) → 1 ─┤

 Linearizable?  No. P2's first read starts after the write returned.
 Sequential?    Yes. P2.read→0, P1.write(1), P2.read→1 respects each process's order.
```

Every process sees the same order of events, but that order may "lag" real time. CPU memory models
and some replicated logs offer roughly this.

### Causal consistency

**Definition**: writes that are **causally related** must be seen by every process in the same
order. Concurrent (unrelated) writes may be seen in different orders by different processes.

Operation *a* causally precedes *b* (written a → b) if: they are in the same process and *a* came
first; or *b* reads a value written by *a*; or there is a chain of such steps.

```text
 Alice:  post("I lost my ring")      post("Found it!")
                                           │ Bob reads "Found it!"
 Bob:                                      └──► comment("Glad to hear!")

 Carol must never see Bob's "Glad to hear!" before Alice's "Found it!".
 Carol MAY see Dave's unrelated post before or after either of them.
```

Causal consistency is the **strongest model that can stay available during a partition**
(Mahajan, Alvisi, Dahlin 2011 showed real-time causal is the strongest achievable for
always-available systems). It is implemented by tracking dependencies with vector clocks or
explicit dependency lists (COPS, MongoDB causal sessions).

### PRAM / FIFO consistency

**Pipelined RAM**: writes **from one process** are seen by every other process in the order that
process issued them. Writes from different processes may be interleaved differently on each
observer, and there is no tracking of "I read your value, then wrote mine".

```text
 P1: write(x=1), write(x=2)
 P2: write(y=9)

 Observer O1 sees: x=1, y=9, x=2   ✓
 Observer O2 sees: y=9, x=1, x=2   ✓   (different interleaving is fine)
 Observer O3 sees: x=2, x=1        ✗   (P1's own order violated)
```

A Kafka partition with a single producer gives you this for that producer's messages.

### Session guarantees

Terry et al. defined four guarantees for the Bayou system (1994). They are scoped to **one client
session** and are cheap to provide, so they are the practical middle ground for user-facing apps.
Together they are equivalent to causal consistency within a session.

| Guarantee | Promise | Violation example | Typical implementation |
| --- | --- | --- | --- |
| **Read-your-writes (RYW)** | After you write, your reads see that write or a newer value | You edit your profile, refresh, and see the old bio | Read from leader for a while after writing; or client sends its last write timestamp/LSN and replica waits until caught up |
| **Monotonic reads (MR)** | Once you saw a value, you never see an older one | Refresh shows a comment, refresh again and it is gone | Pin session to one replica (hash user id); or track highest version seen |
| **Monotonic writes (MW)** | Your writes are applied in the order you issued them, everywhere | "Set password" then "disable old sessions" applied in reverse on a replica | Per-session sequence numbers; single leader per key |
| **Writes-follow-reads (WFR)** | A write made after reading value v is ordered after the write that produced v | Your reply to a post is visible on a replica that does not yet have the post | Attach the read's version as a dependency of the write |

Read-your-writes with a version token, a common pattern with PostgreSQL replicas:

```java
public final class SessionAwareRouter {
    private final DataSource primary;
    private final List<ReplicaHandle> replicas;

    /** After a write, the caller stores the commit LSN in the user's session. */
    public String writeAndGetLsn(Connection c) throws SQLException {
        try (var st = c.createStatement();
             var rs = st.executeQuery("SELECT pg_current_wal_lsn()::text")) {
            rs.next();
            return rs.getString(1);
        }
    }

    /** Use a replica only if it has replayed at least the session's last write. */
    public DataSource forRead(String sessionMinLsn) {
        if (sessionMinLsn == null) {
            return replicas.get(ThreadLocalRandom.current().nextInt(replicas.size())).dataSource();
        }
        return replicas.stream()
                .filter(r -> r.replayedAtLeast(sessionMinLsn))
                .findFirst()
                .map(ReplicaHandle::dataSource)
                .orElse(primary);
    }

    public interface ReplicaHandle {
        DataSource dataSource();
        boolean replayedAtLeast(String lsn); // compares with pg_last_wal_replay_lsn()
    }

    public SessionAwareRouter(DataSource primary, List<ReplicaHandle> replicas) {
        this.primary = primary;
        this.replicas = List.copyOf(replicas);
    }
}
```

### Eventual consistency

**Definition**: if no new writes are made to an object, eventually all reads return the same
value. That is all. It says nothing about **when**, nor about what you see before then, nor what
value they converge **to** (it could converge by discarding writes).

It is a liveness property, not a safety property: at any finite moment, any read result is
technically allowed. In practice, the replication lag of systems like DynamoDB or Cassandra is
usually milliseconds, but under failure it can be minutes or hours. "Usually fast" is not a
guarantee your code may rely on.

### Strong eventual consistency (SEC)

Shapiro et al. (2011) defined SEC: replicas that have received **the same set of updates** (in any
order) are in **the same state**, immediately, without coordination or conflict resolution by the
application. This is what **CRDTs** provide (see below). SEC adds a safety property to eventual
consistency: convergence is deterministic and no update is silently lost.

### Summary of the models

| Model | Real-time order | Same order for all | Available under partition | Cost |
| --- | --- | --- | --- | --- |
| Linearizable | Yes | Yes | No | Consensus or a leader round trip per op |
| Sequential | No | Yes | No | Total-order broadcast |
| Causal | No | Only for causally related ops | Yes | Dependency metadata |
| PRAM / FIFO | No | Only per writer | Yes | Per-writer sequence numbers |
| Session guarantees | No | Only within a session | Yes (with sticky routing) | Session tokens |
| Strong eventual | No | No, but same final state | Yes | CRDT data types |
| Eventual | No | No | Yes | Nothing |

---

## Linearizability vs serializability vs strict serializability

These three are confused constantly, including in vendor docs.

| Property | Scope | Guarantee | Respects real time? |
| --- | --- | --- | --- |
| **Linearizability** | Single operations on single objects | Each op takes effect atomically at a point between call and return | Yes |
| **Serializability** | Transactions over many objects | Result equals **some** serial execution of the transactions | No |
| **Strict serializability** | Transactions over many objects | Serializable **and** the serial order respects real time | Yes |

Serializable but not linearizable, a legal history:

```text
 T1: ├── UPDATE balance = 50 WHERE id = 1; COMMIT ──┤
 T2:                                                    ├── SELECT balance → 100 ──┤
```

T2 started after T1 committed but saw the old value. That is serializable (the order "T2 then T1"
is a valid serial order), but not strictly serializable. This can happen with a serializable
database serving T2 from a lagging snapshot or a stale replica.

- **Snapshot isolation** (PostgreSQL `REPEATABLE READ`, Oracle `SERIALIZABLE`) is weaker than
  serializable: it permits write skew.
- **PostgreSQL `SERIALIZABLE`** (SSI) and a single-node database give strict serializability in
  practice, because on one node commits happen in real-time order.
- **Spanner** and **FaunaDB** advertise strict serializability (Spanner calls it "external
  consistency"). **CockroachDB** gives serializable transactions and linearizable single keys, but
  not strict serializability across all keys in every case (see its docs on clock uncertainty).

---

## Quorums

### The math

With **N** replicas, a write is acknowledged once **W** replicas accept it, and a read queries
**R** replicas and takes the newest version.

If **W + R > N**, any read set overlaps any write set in at least one replica, so a read will
contact at least one replica that has the latest acknowledged write.

```text
 N = 5, W = 3, R = 3     (3 + 3 = 6 > 5)

 Replicas:   r1   r2   r3   r4   r5
 Write v2:   ✓    ✓    ✓    ·    ·        acknowledged by W = 3
 Read:                 ✓    ✓    ✓      queries R = 3, overlap is r3 → sees v2
```

| N | W | R | Tolerates (writes / reads) | Behaviour |
| --- | --- | --- | --- | --- |
| 3 | 2 | 2 | 1 down / 1 down | Balanced; the standard choice |
| 3 | 3 | 1 | 0 down / 2 down | Fast reads, any node loss blocks writes |
| 3 | 1 | 3 | 2 down / 0 down | Fast writes, any node loss blocks reads |
| 3 | 1 | 1 | 2 down / 2 down | No overlap: eventual consistency only |
| 5 | 3 | 3 | 2 down / 2 down | More tolerance, more replicas to contact |

Rules of thumb:

- **W > N / 2** prevents two conflicting writes from both reaching a majority.
- Latency of a quorum op = latency of the **W-th** (or R-th) fastest replica, so quorums hide one
  slow node.
- In Cassandra, the consistency level is chosen per query; `LOCAL_QUORUM` is a quorum within the
  local datacenter, which avoids cross-region latency.

```sql
-- cqlsh: per-session consistency level, then a quorum write and read
CONSISTENCY LOCAL_QUORUM;
INSERT INTO shop.carts (user_id, item_id, qty) VALUES (42, 'sku-1', 2);
SELECT qty FROM shop.carts WHERE user_id = 42 AND item_id = 'sku-1';
```

### Why W + R > N is not linearizability

Overlapping quorums give "you will usually see the latest write", not linearizability:

1. **Concurrent read during a write**: the write has reached r1 only. Reader A queries {r1, r2}
   and sees the new value; reader B, starting after A finished, queries {r2, r3} and sees the
   old value. That is the "going back in time" violation shown earlier. The fix (the ABD
   algorithm) requires readers to **write back** the newest value to a quorum before returning,
   which adds a round trip. Cassandra does not do this for normal reads.
2. **Failed writes are not rolled back**: a write that reached 1 of 3 replicas returns an error to
   the client, but the value stays on that replica and may later spread via read repair.
3. **Sloppy quorums** (below) break the overlap guarantee completely.
4. **Clock-based conflict resolution** (LWW) may discard the newer write.

### Sloppy quorums and hinted handoff

In Dynamo-style systems, each key has a **preference list** of N "home" nodes on the hash ring.
If some home nodes are unreachable, a **sloppy quorum** accepts the write on other healthy nodes
further along the ring instead of failing.

```text
 Hash ring, key K, N = 3, home nodes = A, B, C

    A ── B ── C ── D ── E ── (back to A)
              ✗ (C unreachable)

 Write K with W = 2:  stored on A and B ... and on D with a hint "this belongs to C"
 When C recovers:     D hands the value to C (hinted handoff), then deletes its copy
```

This keeps writes available during partitions, but now a read quorum of {B, C} after C comes back
(before handoff) can miss the write. Sloppy quorums are a durability mechanism, not a consistency
one. Cassandra uses hinted handoff (hints stored for up to `max_hint_window`, 3 hours by default)
but counts only real replicas towards the consistency level.

### Read repair

When a read contacts several replicas and sees different versions, the coordinator sends the
newest version to the stale replicas. It only fixes keys that are actually read, so rarely read
data can stay divergent for a long time.

### Anti-entropy with Merkle trees

A background process compares replicas and copies missing data. Comparing every key would mean
shipping the whole dataset, so replicas build a **Merkle tree** (hash tree) over their key ranges
and compare top-down.

```text
                       root = H(H12 ‖ H34)
                      /                   \
             H12 = H(H1 ‖ H2)        H34 = H(H3 ‖ H4)
              /          \             /          \
            H1            H2         H3            H4
       keys 0-25%    25-50%     50-75%       75-100%

 1. Exchange roots. Equal → replicas identical, done.
 2. Different → exchange children. H12 equal, H34 different → ignore left half.
 3. Recurse into H34 until the differing leaf ranges are found; stream only those keys.
```

Cost to find differences is O(log n) hash exchanges per differing range. Cassandra builds these
trees during `nodetool repair`; operators must run repair regularly (more often than
`gc_grace_seconds`, 10 days by default) or deleted data can **resurrect** when a replica that
missed a tombstone streams the old value back.

---

## Conflict resolution

Whenever two replicas accept writes to the same key without coordinating (multi-leader,
leaderless, or offline clients), you will get **concurrent writes** and must decide the outcome.

### Last-writer-wins (LWW) and its data loss

Each write carries a timestamp; the highest timestamp wins and the others are discarded.
Cassandra does this per cell, using the client or coordinator timestamp.

```text
 Real order of events:
   t=100 ms  node X (clock 40 ms fast) writes  cart = [book]       stamped 140
   t=120 ms  node Y (clock accurate)   writes  cart = [book, pen]  stamped 120

 Merge by LWW: 140 > 120 → cart = [book]
 The later write [book, pen] is silently lost, and nobody gets an error.
```

LWW is fine when writes are immutable and keys are unique (for example a UUID key written once),
or when losing one of two concurrent updates is acceptable (a "last seen at" field). It is wrong
for counters, carts, balances, or anything a user expects to accumulate.

### Version vectors (vector clocks for replicas)

A **version vector** has one counter per replica that has coordinated a write to this key. It lets
the system tell "this write replaced that one" apart from "these two writes were concurrent".

Comparison rule for vectors V and W:

- V ≤ W if every entry of V is ≤ the matching entry of W. Then W **supersedes** V.
- If neither V ≤ W nor W ≤ V, the versions are **concurrent** and both must be kept as
  **siblings** until someone resolves them.

#### Worked example (the Dynamo paper's shopping cart)

Three servers Sx, Sy, Sz. The client always sends back the version it last read.

```text
 Step  Action                                  Stored versions
 ────  ──────────────────────────────────────  ──────────────────────────────────────
  1    Client writes D1 via Sx                 D1 [Sx:1]
  2    Client reads D1, writes D2 via Sx       D2 [Sx:2]                 (D2 > D1)
  3    Client A reads D2, writes D3 via Sy     D3 [Sx:2, Sy:1]
  4    Client B reads D2, writes D4 via Sz     D4 [Sx:2, Sz:1]
       Compare D3 and D4:
         Sy: 1 vs 0  → D3 bigger
         Sz: 0 vs 1  → D4 bigger            → CONCURRENT, keep both as siblings
  5    Client reads, gets {D3, D4},
       merges (union of cart items),
       writes D5 via Sx                        D5 [Sx:3, Sy:1, Sz:1]     (D5 > D3, D4)
```

The application decides how to merge (Amazon's cart took the union of items, which is why deleted
items could reappear). The data structures:

```java
public record VersionVector(Map<String, Long> entries) {

    public enum Order { BEFORE, AFTER, EQUAL, CONCURRENT }

    public VersionVector {
        entries = Map.copyOf(entries);
    }

    public static VersionVector empty() {
        return new VersionVector(Map.of());
    }

    public VersionVector increment(String replicaId) {
        var copy = new HashMap<>(entries);
        copy.merge(replicaId, 1L, Long::sum);
        return new VersionVector(copy);
    }

    public VersionVector merge(VersionVector other) {
        var copy = new HashMap<>(entries);
        other.entries.forEach((k, v) -> copy.merge(k, v, Math::max));
        return new VersionVector(copy);
    }

    public Order compareTo(VersionVector other) {
        boolean less = false;
        boolean greater = false;
        var keys = new HashSet<>(entries.keySet());
        keys.addAll(other.entries.keySet());
        for (String k : keys) {
            long a = entries.getOrDefault(k, 0L);
            long b = other.entries.getOrDefault(k, 0L);
            if (a < b) less = true;
            if (a > b) greater = true;
        }
        if (less && greater) return Order.CONCURRENT;
        if (less) return Order.BEFORE;
        if (greater) return Order.AFTER;
        return Order.EQUAL;
    }
}
```

Version vectors grow with the number of writers. Dynamo truncated old entries (risking false
"concurrent" results); Riak moved to **dotted version vectors** to avoid sibling explosion.

### CRDTs (conflict-free replicated data types)

A CRDT is a data type whose merge function is **commutative, associative, and idempotent**, so
replicas can apply updates in any order, any number of times, and still converge (strong eventual
consistency). Used in Riak data types, Redis Enterprise active-active, Azure Cosmos DB, and
collaborative editors (Automerge, Yjs).

Two flavours: **state-based** (ship the whole state, merge with a join) and **operation-based**
(ship operations; needs reliable causal delivery). The examples below are state-based.

#### G-Counter (grow-only counter)

Each replica increments only its own slot. Value = sum. Merge = element-wise max.

```java
public final class GCounter {
    private final String replicaId;
    private final Map<String, Long> counts = new HashMap<>();

    public GCounter(String replicaId) {
        this.replicaId = replicaId;
    }

    public void increment(long delta) {
        if (delta < 0) throw new IllegalArgumentException("G-Counter can only grow");
        counts.merge(replicaId, delta, Long::sum);
    }

    public long value() {
        return counts.values().stream().mapToLong(Long::longValue).sum();
    }

    /** Max per slot: commutative, associative, idempotent. */
    public void merge(GCounter other) {
        other.counts.forEach((id, c) -> counts.merge(id, c, Math::max));
    }
}
```

Why max and not sum: merging the same state twice must not double count (idempotence).

#### PN-Counter (increment and decrement)

Two G-Counters: one for increments (P), one for decrements (N). Value = P − N.

```java
public final class PNCounter {
    private final GCounter increments;
    private final GCounter decrements;

    public PNCounter(String replicaId) {
        this.increments = new GCounter(replicaId);
        this.decrements = new GCounter(replicaId);
    }

    public void increment() { increments.increment(1); }

    public void decrement() { decrements.increment(1); }

    public long value() { return increments.value() - decrements.value(); }

    public void merge(PNCounter other) {
        increments.merge(other.increments);
        decrements.merge(other.decrements);
    }
}
```

A PN-Counter cannot enforce an invariant like "stock ≥ 0": two replicas can each sell the last
item. Invariants that span replicas need coordination (or escrow: pre-split the stock between
replicas).

#### OR-Set (observed-remove set)

Each `add` creates a unique tag. `remove` deletes only the tags it has **observed**. A concurrent
add creates a new tag the remove did not see, so **add wins**.

```java
public final class ORSet<E> {
    private final String replicaId;
    private long counter;
    private final Map<E, Set<String>> addTags = new HashMap<>();
    private final Map<E, Set<String>> removedTags = new HashMap<>();

    public ORSet(String replicaId) {
        this.replicaId = replicaId;
    }

    public void add(E element) {
        String tag = replicaId + ":" + (++counter);
        addTags.computeIfAbsent(element, k -> new HashSet<>()).add(tag);
    }

    public void remove(E element) {
        Set<String> observed = addTags.getOrDefault(element, Set.of());
        removedTags.computeIfAbsent(element, k -> new HashSet<>()).addAll(observed);
    }

    public boolean contains(E element) {
        Set<String> live = new HashSet<>(addTags.getOrDefault(element, Set.of()));
        live.removeAll(removedTags.getOrDefault(element, Set.of()));
        return !live.isEmpty();
    }

    public Set<E> elements() {
        Set<E> result = new HashSet<>();
        for (E e : addTags.keySet()) {
            if (contains(e)) result.add(e);
        }
        return result;
    }

    public void merge(ORSet<E> other) {
        other.addTags.forEach((e, tags) ->
                addTags.computeIfAbsent(e, k -> new HashSet<>()).addAll(tags));
        other.removedTags.forEach((e, tags) ->
                removedTags.computeIfAbsent(e, k -> new HashSet<>()).addAll(tags));
    }
}
```

```text
 Replica 1: add("milk")  → tag r1:1          Replica 2 (after sync): remove("milk")
                                                removes observed tag r1:1
 Replica 1 (concurrently): add("milk") → tag r1:2
 Merge: tags {r1:1, r1:2} minus removed {r1:1} = {r1:2} → "milk" is present (add wins)
```

Pitfall: removed tags (tombstones) grow forever. Production CRDTs garbage-collect them using
causal stability (once every replica has seen a removal) or use optimised designs (for example
the delta-state OR-Set with dots).

---

## Clocks and ordering

### Physical clocks

- Quartz oscillators drift; Google assumes up to 200 ppm, which is about 17 seconds per day if
  never corrected.
- **NTP** synchronises clocks over the network. Typical accuracy: around a millisecond in a good
  LAN, tens of milliseconds over the internet, and much worse under congestion or misconfiguration.
- NTP can **step** the clock backwards or forwards. Leap seconds have caused real outages (2012).
  Cloud providers now smear leap seconds over many hours.
- Cloud time services (AWS Time Sync, Google's NTP) plus PTP hardware timestamping can reach
  microsecond accuracy, but you still need to know your error bound.

Two clocks every program should distinguish:

| Clock | Java API | Use for | Never use for |
| --- | --- | --- | --- |
| Wall clock (time of day) | `Instant.now()`, `System.currentTimeMillis()` | Timestamps shown to humans, log correlation | Measuring durations, ordering events across nodes |
| Monotonic clock | `System.nanoTime()` | Timeouts, durations, rate limits on one machine | Comparing across machines (the origin is arbitrary) |

### Lamport clocks

Lamport (1978) defined the **happens-before** relation (→) and a logical clock consistent with it.

**Algorithm** (each process keeps an integer `L`):

1. Before every local event, `L = L + 1`.
2. When sending a message, increment as above and attach `L`.
3. On receiving a message with timestamp `Lm`, set `L = max(L, Lm) + 1`.

Property: if a → b then L(a) < L(b). The converse does **not** hold: L(a) < L(b) does not mean a
happened before b; they may be concurrent. A **total order** is obtained by breaking ties with
process id: compare (L, processId).

```text
 P1:  a(1) ──────────── send m1 (2) ─────────────────────── e(3)
                               ╲
 P2:  b(1) ── c(2) ───────────── recv m1: max(2, 2) + 1 = 3 ── send m2 (4)
                                                                  ╲
 P3:  d(1) ──────────────────────────────────────────────────── recv m2: max(1, 4) + 1 = 5
```

```java
public final class LamportClock {
    private long time;

    /** Local event or send: returns the timestamp to attach. */
    public synchronized long tick() {
        return ++time;
    }

    /** Receive: merge the remote timestamp. */
    public synchronized long onReceive(long remoteTime) {
        time = Math.max(time, remoteTime) + 1;
        return time;
    }

    public record Stamp(long time, String nodeId) implements Comparable<Stamp> {
        @Override
        public int compareTo(Stamp o) {
            int c = Long.compare(time, o.time);
            return c != 0 ? c : nodeId.compareTo(o.nodeId);
        }
    }
}
```

Limitation: a Lamport total order is fixed only after the fact. To enforce a uniqueness constraint
("first to claim the username wins") a node would need to hear from every other node to know no
smaller timestamp is in flight, which is why you need consensus / total order broadcast instead.

### Vector clocks

A vector clock keeps one counter per process (the version vector code above works unchanged). It
captures causality **exactly**: V(a) < V(b) if and only if a → b, and incomparable vectors mean the
events are concurrent. The cost is O(number of processes) metadata per message or version.

- **Vector clocks** track causality between events in a set of processes.
- **Version vectors** track causality between versions of one data item, with one entry per
  replica. Same math, different purpose; the names are often used interchangeably.

### Hybrid logical clocks (HLC)

Kulkarni et al. (2014). An HLC timestamp is a pair `(l, c)`: `l` is the largest physical time seen
so far and `c` is a logical counter used when `l` does not advance. HLC is:

- causally consistent like a Lamport clock (a → b implies HLC(a) < HLC(b)),
- always within the clock-skew bound of physical time, so it is meaningful to humans,
- fixed size (64 bits is common: 48 bits time, 16 bits counter).

CockroachDB, YugabyteDB, and MongoDB (cluster time) use HLC variants.

```java
public final class HybridLogicalClock {

    public record Timestamp(long wallMillis, int logical) implements Comparable<Timestamp> {
        @Override
        public int compareTo(Timestamp o) {
            int c = Long.compare(wallMillis, o.wallMillis);
            return c != 0 ? c : Integer.compare(logical, o.logical);
        }
    }

    private final LongSupplier physicalClock;
    private long l;
    private int c;

    public HybridLogicalClock(LongSupplier physicalClock) {
        this.physicalClock = physicalClock;
    }

    /** Local event or send. */
    public synchronized Timestamp now() {
        long pt = physicalClock.getAsLong();
        long previous = l;
        l = Math.max(previous, pt);
        c = (l == previous) ? c + 1 : 0;
        return new Timestamp(l, c);
    }

    /** Receive a message stamped with m. */
    public synchronized Timestamp update(Timestamp m) {
        long pt = physicalClock.getAsLong();
        long previous = l;
        l = Math.max(Math.max(previous, m.wallMillis()), pt);
        if (l == previous && l == m.wallMillis()) {
            c = Math.max(c, m.logical()) + 1;
        } else if (l == previous) {
            c = c + 1;
        } else if (l == m.wallMillis()) {
            c = m.logical() + 1;
        } else {
            c = 0;
        }
        return new Timestamp(l, c);
    }
}
```

Construct it with `new HybridLogicalClock(System::currentTimeMillis)`. Production implementations
also reject messages whose timestamp is further ahead than the maximum allowed skew (CockroachDB's
`--max-offset`, 500 ms by default) because that indicates a broken clock.

### TrueTime and commit-wait (Spanner)

Google Spanner's TrueTime API does not return a single time. It returns an interval:

```text
 TT.now() = [earliest, latest]      the true time is guaranteed to be inside
            width = 2ε              ε is typically a few ms (GPS + atomic clocks per DC)
```

**Commit-wait** gives strict serializability (external consistency) using that interval:

```text
 Transaction T1 acquires locks, then:
   s1 = TT.now().latest                 choose commit timestamp
   wait until TT.now().earliest > s1    "commit wait", about 2ε
   release locks, reply to client

 Real time ──────────────────────────────────────────────────────────►
            │◄──── uncertainty ────►│
            s1 chosen               s1 is definitely in the past → commit visible

 Any transaction T2 that starts after T1's reply gets s2 = TT.now().latest > s1.
```

Because T1 waits until its timestamp is certainly in the past, any later transaction (anywhere in
the world) gets a larger timestamp, so timestamp order matches real-time order. The price is a few
ms of latency on every read-write transaction, and the hardware investment to keep ε small.

CockroachDB, without atomic clocks, uses HLC plus an **uncertainty interval**: if a read sees a
value with a timestamp slightly in its future (within max offset), it cannot know which came first
and restarts the read at a higher timestamp.

---

## Consensus

Consensus is the problem of getting several nodes to agree on a value such that:

- **Agreement**: no two correct nodes decide differently.
- **Validity**: the decided value was proposed by some node.
- **Integrity**: a node decides at most once.
- **Termination**: every correct node eventually decides.

Repeated consensus gives a **replicated log** (total order broadcast). Feed the same log into
deterministic state machines on each node and they end in the same state: **state machine
replication**. Consensus is equivalent to linearizable compare-and-set, total order broadcast,
leader election with a single winner, and atomic commit (with some care), so one good consensus
implementation solves many problems.

### FLP impossibility

Fischer, Lynch, and Paterson (1985): in a fully **asynchronous** system where even **one** process
may crash, no deterministic algorithm can guarantee consensus terminates. The intuition: you can
never tell a crashed process from a slow one, so the adversary can always delay the "deciding"
message.

FLP does not say consensus is useless. Practical algorithms keep **safety always** and get
**liveness** when the network is stable for long enough (partial synchrony), using timeouts and
randomization (Raft's randomized election timeouts). They may stall during bad periods but never
decide two different values.

### Raft deep dive

Raft (Ongaro and Ousterhout, 2014) was designed for understandability. It decomposes consensus into
**leader election**, **log replication**, and **safety**, and is used by etcd, Consul, CockroachDB,
TiKV, and Kafka's KRaft.

#### Roles and terms

Every server is a **follower**, **candidate**, or **leader**. Time is divided into **terms**
numbered by consecutive integers. Each term starts with an election; at most one leader is elected
per term. Terms act as a logical clock: any message carrying a higher term makes the receiver
update its term and step down to follower; messages with a lower term are rejected.

```text
 ┌──────────┐ election timeout  ┌───────────┐ majority of votes ┌──────────┐
 │ Follower │ ────────────────► │ Candidate │ ────────────────► │  Leader  │
 └──────────┘                   └───────────┘                   └──────────┘
      ▲                               │                               │
      ├─ sees leader or higher term ──┘                               │
      │                                                               │
      └───────────────────── sees a higher term ──────────────────────┘

 A candidate whose election times out (split vote) increments its term and retries.
```

```text
 Terms:   │ term 1           │ term 2 │ term 3              │ term 4 ...
          │ election │normal │ elect. │ election │ normal   │
          │          │ ops   │ (split │          │ ops      │
          │          │       │ vote,  │          │          │
          │          │       │ no     │          │          │
          │          │       │ leader)│          │          │
```

Persistent state on every server (fsynced before responding): `currentTerm`, `votedFor`, and the
`log[]`. Volatile state: `commitIndex`, `lastApplied`, and on the leader `nextIndex[]` and
`matchIndex[]` per follower.

#### Leader election

1. A follower that hears nothing from a leader within its **election timeout** (randomized, the
   paper suggests 150–300 ms) becomes a candidate.
2. It increments `currentTerm`, votes for itself, and sends `RequestVote(term, candidateId,
   lastLogIndex, lastLogTerm)` to all others.
3. Each server grants **at most one vote per term**, first come first served, and only if the
   candidate's log is **at least as up-to-date** as its own (the election restriction):
   the candidate's last log term is higher, or the terms are equal and its last index is ≥.
4. A candidate with votes from a **majority** becomes leader and immediately sends heartbeats
   (empty `AppendEntries`) to stop other elections.
5. If it receives `AppendEntries` from a leader with term ≥ its own, it steps down.
6. On a split vote, nobody wins; randomized timeouts make a repeat unlikely.

```java
/** RequestVote handler, the core of Raft's election safety. */
public synchronized VoteResponse onRequestVote(VoteRequest req) {
    if (req.term() < currentTerm) {
        return new VoteResponse(currentTerm, false);
    }
    if (req.term() > currentTerm) {
        currentTerm = req.term();
        votedFor = null;
        becomeFollower();
    }
    boolean logOk = req.lastLogTerm() > lastLogTerm()
            || (req.lastLogTerm() == lastLogTerm() && req.lastLogIndex() >= lastLogIndex());
    boolean canVote = votedFor == null || votedFor.equals(req.candidateId());
    if (logOk && canVote) {
        votedFor = req.candidateId();
        persistTermAndVote();          // fsync BEFORE replying
        resetElectionTimer();
        return new VoteResponse(currentTerm, true);
    }
    return new VoteResponse(currentTerm, false);
}

record VoteRequest(long term, String candidateId, long lastLogIndex, long lastLogTerm) {}
record VoteResponse(long term, boolean voteGranted) {}
```

#### Log replication

The leader appends each client command to its log as `(term, index, command)` and sends
`AppendEntries(term, leaderId, prevLogIndex, prevLogTerm, entries[], leaderCommit)`.

- The follower **rejects** unless its log has an entry at `prevLogIndex` with term `prevLogTerm`.
  This consistency check gives the **Log Matching Property**: if two logs have an entry with the
  same index and term, the logs are identical up to that index.
- On rejection the leader decrements `nextIndex` for that follower and retries, walking back until
  the logs match. The follower then **deletes conflicting entries** and appends the leader's.
  (Real implementations send a conflict term/index hint to skip back faster.)

```text
 index:        1     2     3     4     5     6
 Leader (t3) [1:x] [1:y] [2:z] [3:a] [3:b] [3:c]   commitIndex = 5
 Follower 1  [1:x] [1:y] [2:z] [3:a] [3:b]
 Follower 2  [1:x] [1:y] [2:z] [3:a] [3:b] [3:c]
 Follower 3  [1:x] [1:y]                           lagging, will be caught up
 Follower 4  [1:x] [1:y] [2:z] [2:q]               index 4 conflicts: overwritten

 [term:command]. Entry 5 is on 3 of 5 servers (L, F1, F2) → committed.
 Entry 6 is on 2 of 5 → not yet committed.
```

#### Commit rules

- An entry from the **current term** is committed once the leader has stored it on a majority.
  The leader advances `commitIndex`, applies the entry to its state machine, and replies to the
  client. Followers learn `leaderCommit` in later `AppendEntries` and apply too.
- Committing an entry also commits **all preceding entries**.
- A leader **never** commits an entry from a **previous term** just by counting replicas. It only
  commits it indirectly, by committing a current-term entry after it. Without this rule (the
  paper's Figure 8 scenario), an old-term entry stored on a majority could still be overwritten
  by a new leader whose last entry has a higher term. New leaders therefore append a **no-op**
  entry at the start of their term to commit everything before it quickly.

```java
/** Leader: advance commitIndex after matchIndex changes. */
private void maybeAdvanceCommitIndex() {
    for (long n = lastLogIndex(); n > commitIndex; n--) {
        if (termAt(n) != currentTerm) {
            break;                              // Figure 8 rule: only count current-term entries
        }
        int replicas = 1;                       // the leader itself
        for (long match : matchIndex.values()) {
            if (match >= n) replicas++;
        }
        if (replicas > clusterSize / 2) {
            commitIndex = n;
            applyCommittedEntries();
            return;
        }
    }
}
```

#### Safety

Raft guarantees:

| Property | Meaning |
| --- | --- |
| Election safety | At most one leader per term |
| Leader append-only | A leader never overwrites or deletes its own entries |
| Log matching | Same (index, term) means identical prefix |
| Leader completeness | A committed entry is present in the logs of all later leaders |
| State machine safety | No two servers apply different commands at the same index |

Leader completeness follows from the election restriction: a committed entry is on a majority, a
winning candidate needs votes from a majority, the two majorities overlap, and the overlapping
voter refuses to vote for a candidate whose log is less up-to-date.

#### Reads

A leader cannot simply answer reads from its state: it might have been deposed without knowing.
Options for linearizable reads:

- **Log read**: put the read through the log. Correct, slow.
- **ReadIndex**: leader records `commitIndex`, confirms it is still leader with a heartbeat round to
  a majority, waits until applied up to that index, then answers. etcd's default.
- **Lease read**: leader assumes no new leader can exist until its lease (shorter than the
  election timeout) expires. One round trip less, but depends on bounded clock drift.

#### Membership changes

Switching directly from configuration C_old to C_new is unsafe: at the moment of switching, a
majority of C_old and a majority of C_new might be disjoint and elect two leaders.

```text
 Unsafe direct change from 3 to 5 servers:
   S1 S2 S3          (old majority = 2 of 3: {S1, S2})
   S1 S2 S3 S4 S5    (new majority = 3 of 5: {S3, S4, S5})
   {S1, S2} and {S3, S4, S5} do not overlap → two leaders possible in the same term
```

Raft offers two solutions:

1. **Joint consensus** (the paper): first commit a transitional configuration C_old,new in which
   every decision needs a majority of **both** C_old and C_new; then commit C_new.
2. **Single-server changes** (Ongaro's dissertation, used by etcd and most implementations): add or
   remove **one server at a time**. Any majority of the old and new configurations then overlaps,
   so it is safe without the joint phase. New servers usually join as **learners** (non-voting)
   until they catch up, so they do not hurt availability.

#### Practical extensions

- **Pre-vote**: a node first asks "would you vote for me?" without bumping its term. Stops a
  partitioned node from disrupting the cluster with ever-higher terms when it rejoins.
- **CheckQuorum**: a leader that cannot reach a majority steps down.
- **Snapshots / log compaction**: state machine snapshots replace the log prefix;
  `InstallSnapshot` sends them to far-behind followers.
- **Sizing**: 2f + 1 servers tolerate f failures. 3 tolerates 1, 5 tolerates 2. Even sizes add
  cost without adding tolerance (4 tolerates 1, like 3). More voters means slower writes.

### Paxos overview

Lamport's Paxos ("The Part-Time Parliament", 1998; "Paxos Made Simple", 2001). Roles:
**proposers**, **acceptors**, **learners** (one process often plays all three).

**Single-decree Paxos** (decide one value):

```text
 1a  Proposer → acceptors:  Prepare(n)             n = unique, increasing ballot
 1b  Acceptor → proposer:   Promise(n, accepted?)  ignore ballots < n from now on;
                                                   report highest value accepted so far
 2a  Proposer → acceptors:  Accept(n, v)           v = value of highest-numbered
                                                   reported proposal, else its own
 2b  Acceptor → learners:   Accepted(n, v)         unless it promised a higher n
     Value chosen once a majority of acceptors accept the same (n, v).
```

The key rule (in 2a, "use the highest previously accepted value") is what makes an already-chosen
value survive. **Multi-Paxos** runs phase 1 once for a stable leader and then only phase 2 per log
slot, which is essentially what Raft does, with Raft adding stricter rules about log contiguity and
elections. Paxos is used in Google Chubby and Spanner (per-shard Paxos groups), and its variants
in Cassandra lightweight transactions (Paxos per partition for `IF` conditions).

### ZAB (ZooKeeper Atomic Broadcast)

ZooKeeper's protocol, similar in spirit to Raft:

- A leader orders all writes and assigns each a **zxid** = (epoch, counter). The epoch is like a
  Raft term.
- Phases: **discovery** (elect a prospective leader, agree on a new epoch), **synchronization**
  (bring followers' histories in line with the leader), **broadcast** (two-phase propose/ack/commit
  for each write; commit once a quorum acks).
- Writes are linearizable. Reads go to the local server and can be stale, but each client sees a
  consistent order (FIFO client order, sequential consistency). Call `sync()` before a read to
  get a fresh view.

### Where consensus is used

| System | Algorithm | Used for |
| --- | --- | --- |
| etcd | Raft | Kubernetes cluster state, config, leader election, locks |
| Apache ZooKeeper | ZAB | Coordination for HBase, older Kafka, Hadoop, Solr |
| HashiCorp Consul | Raft (servers) + gossip (Serf) for membership | Service catalog, KV, sessions/locks |
| Apache Kafka KRaft | Raft variant | Cluster metadata (`__cluster_metadata` log) in controller quorum; ZooKeeper removed in Kafka 4.0 |
| CockroachDB, TiKV, YugabyteDB | Raft per range / shard | Replicating each data range |
| Google Spanner, Chubby | Paxos | Per-split replication, lock service |

Note that Kafka's **data** replication (ISR) is not Raft; only its metadata uses KRaft.

---

## Leader election, leases and fencing tokens

### Leader election with leases

A **lease** is a lock with a timeout: "node A is leader until time T". The holder must renew it
before T; if it crashes, the lease expires and someone else can take over. Leases avoid waiting
forever for a dead holder.

Kubernetes controllers use `Lease` objects in the API server (backed by etcd); etcd and Consul
provide lease/session primitives directly.

### The problem: a holder that does not know it lost the lease

```text
 Client 1             Lock service             Client 2             Storage
    │                      │                       │                    │
    │ ── acquire ────────► │                       │                    │
    │ ◄── ok, token 33 ─── │                       │                    │
    │                      │                       │                    │
    │  stop-the-world GC   │                       │                    │
    │  pause (15 s)        │ lease expires         │                    │
    │                      │ ◄──── acquire ─────── │                    │
    │                      │ ──── ok, token 34 ──► │                    │
    │                      │                       │ ── write(t=34) ──► │ ok, max = 34
    │ wakes up, thinks it  │                       │                    │
    │ still holds the lock │                       │                    │
    │ ─────────────────── write(t=33) ────────────────────────────────► │ REJECT 33 < 34
```

Checking "do I still hold the lease?" before writing does not help: the pause can happen right
after the check. GC pauses, VM migration, swapping, and `SIGSTOP` all cause this.

### Fencing tokens

The lock service returns a **monotonically increasing token** with each grant. The protected
resource remembers the highest token it has seen and **rejects** any request with a lower one.
The safety check must be on the resource side; the client cannot be trusted to know.

```java
public final class FencedStorage {
    private final AtomicLong highestToken = new AtomicLong(-1);
    private final Map<String, byte[]> data = new ConcurrentHashMap<>();

    public void write(String key, byte[] value, long fencingToken) {
        long current;
        do {
            current = highestToken.get();
            if (fencingToken < current) {
                throw new StaleTokenException(fencingToken, current);
            }
        } while (!highestToken.compareAndSet(current, fencingToken));
        data.put(key, value);
    }

    public static final class StaleTokenException extends RuntimeException {
        public StaleTokenException(long got, long highest) {
            super("fencing token " + got + " is older than " + highest);
        }
    }
}
```

In a database the same check is a conditional update:

```sql
UPDATE jobs
SET    owner = 'worker-2', fencing_token = 34, payload = '...'
WHERE  id = 7
  AND  fencing_token <= 34;   -- 0 rows updated means a newer holder exists
```

Sources of tokens: ZooKeeper `zxid` or znode `czxid`/version, etcd key **revision**, Raft term
number, or a database sequence.

---

## Distributed locks

First ask **why** you need the lock:

- **Efficiency** (avoid doing the same work twice; occasional double execution is harmless):
  a single Redis instance is fine.
- **Correctness** (double execution corrupts data): you need a consensus-backed lock **and**
  fencing tokens checked by the resource.

### Single-instance Redis lock

```bash
# Acquire: only if not exists, with a 30 s expiry; value is a random owner id
SET lock:invoice:42 "c1f3-9a7e" NX PX 30000
```

Release must check ownership atomically, otherwise you may delete someone else's lock after
yours expired:

```lua
-- KEYS[1] = lock key, ARGV[1] = owner id
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
else
  return 0
end
```

Weakness: Redis replication is asynchronous. If the primary fails after granting the lock but
before replicating it, the promoted replica does not know about it and grants it again.

### Redlock and the controversy

**Redlock** (proposed by Salvatore Sanfilippo) uses N independent Redis masters (typically 5):

1. Record start time; try `SET key value NX PX ttl` on all N with a short per-node timeout.
2. The lock is acquired if a **majority** (≥ 3) succeeded and the elapsed time is less than the
   TTL. Validity = TTL − elapsed − clock drift allowance.
3. Otherwise release on all nodes.

Martin Kleppmann's critique ("How to do distributed locking", 2016):

- Redlock produces **no fencing token**, so it cannot protect against the GC pause scenario above.
- Its safety depends on **timing assumptions**: bounded network delay, bounded process pauses,
  and clocks that do not jump (a Redis node whose clock jumps forward expires the key early, and a
  second client gets a majority).
- So it is too heavy for efficiency locks (one Redis suffices) and not safe enough for correctness
  locks.

Sanfilippo's reply ("Is Redlock safe?") argued that the clock assumptions are reasonable with
monotonic time and that a unique random value can act like a token in some designs. The broadly
accepted takeaway: **for correctness, use a consensus system and fencing tokens**.

### ZooKeeper: ephemeral sequential nodes

```text
 /locks/invoice-42/
     lock-0000000017   (client A)  ← lowest number: A holds the lock
     lock-0000000018   (client B)  watches 017
     lock-0000000019   (client C)  watches 018
```

1. Create an **ephemeral sequential** znode under the lock path. Ephemeral means it is deleted
   automatically when the client's session ends (crash or session timeout).
2. `getChildren`. If yours has the lowest sequence number, you hold the lock.
3. Otherwise set a **watch on the node just before yours** (not on the parent). Watching only the
   predecessor avoids the **herd effect** where every waiter wakes up on each release.
4. When the watch fires, go back to step 2 (the predecessor may have died rather than released).

The znode's sequence number or `czxid` is a natural fencing token. Apache Curator's
`InterProcessMutex` implements this recipe correctly; use it rather than writing your own.

```java
CuratorFramework client = CuratorFrameworkFactory.newClient(
        "zk1:2181,zk2:2181,zk3:2181", new ExponentialBackoffRetry(1000, 3));
client.start();

InterProcessMutex lock = new InterProcessMutex(client, "/locks/invoice-42");
if (lock.acquire(10, TimeUnit.SECONDS)) {
    try {
        processInvoice(42);
    } finally {
        lock.release();
    }
}
```

Caveat: a session expiry (long GC pause again) deletes the ephemeral node while the client still
thinks it holds the lock. Fencing is still needed for correctness.

### etcd: leases and revisions

- A **lease** has a TTL; the client keeps it alive with `LeaseKeepAlive`. Keys attached to a lease
  are deleted when it expires.
- Every write gets a cluster-wide, monotonically increasing **revision**, a ready-made fencing token.
- The `concurrency` package (Go client) and `etcdctl lock` implement a mutex: each waiter creates a
  key under a prefix with its lease, and the one with the **lowest create revision** holds the lock;
  others watch the key just before theirs.

```bash
etcdctl lease grant 15                     # lease 694d77aa9e38260f granted with TTL(15s)
etcdctl put --lease=694d77aa9e38260f /leaders/billing node-a
etcdctl lease keep-alive 694d77aa9e38260f  # run in background while leader
etcdctl lock /locks/invoice-42 ./process-invoice.sh   # holds the lock while command runs
```

### Comparison

| Option | Safety basis | Fencing token | Failure behaviour | Use when |
| --- | --- | --- | --- | --- |
| Single Redis `SET NX PX` | One node, async replication | No (add your own) | Lock lost or duplicated on failover | Efficiency only |
| Redlock | Majority of independent Redis + timing assumptions | No | Unsafe under clock jumps / long pauses | Rarely the right answer |
| ZooKeeper (Curator) | ZAB consensus, sessions | czxid / sequence | Session expiry releases lock | Correctness, JVM ecosystems |
| etcd lease + lock | Raft, leases | Revision | Lease expiry releases lock | Correctness, Kubernetes ecosystems |
| Database row lock / advisory lock | Single primary DB | Row version | Tied to connection / transaction | Data already lives in that DB |

---

## Distributed transactions

### Two-phase commit (2PC)

A **coordinator** makes several **participants** (databases, queues) commit atomically.

```text
 Coordinator                         Participant A          Participant B
     │ write "begin T" to log              │                      │
     │ ──── PREPARE ─────────────────────► │                      │
     │ ──── PREPARE ────────────────────────────────────────────► │
     │                       write redo/undo, hold locks, fsync, vote
     │ ◄─────────────────────────── YES ── │                      │
     │ ◄─────────────────────────────────────────────────── YES ──│
     │ write "COMMIT T" to log, fsync   ◄── the commit point
     │ ──── COMMIT ──────────────────────► │                      │
     │ ──── COMMIT ─────────────────────────────────────────────► │
     │ ◄──────────────────────────── ack ──│ release locks        │
     │ ◄──────────────────────────────────────────────────── ack ─│ release locks
```

Two promises make it work: a participant that votes YES **must** be able to commit later, even
after a crash (so it persists the prepared state); and once the coordinator logs COMMIT, the
decision is final and it retries until every participant has applied it.

#### Failure analysis

| Failure | What happens |
| --- | --- |
| Participant crashes or votes NO before voting YES | Coordinator times out or sees NO, logs ABORT, tells everyone to roll back |
| Participant crashes **after** voting YES | On restart it finds a prepared transaction and must ask the coordinator for the outcome; it may not decide alone |
| Coordinator crashes before logging a decision | Prepared participants are **in doubt**: they hold locks and wait. They cannot abort (others may have been told COMMIT) or commit (others may have voted NO) |
| Coordinator crashes after logging COMMIT | On recovery it re-sends COMMIT; participants must handle duplicates idempotently |
| Network partition in phase 2 | Some participants commit, others wait in doubt until connectivity returns |
| Coordinator's log is lost | In-doubt transactions need manual resolution (heuristic commit/abort, which can break atomicity) |

The core weakness: **2PC is a blocking protocol**. The coordinator is a single point of failure
during the in-doubt window, and locks are held throughout, which hurts throughput. Also, all
participants must support it: XA in Java (JTA), PostgreSQL `PREPARE TRANSACTION`, MySQL XA. Most
message brokers and HTTP APIs do not.

Making the coordinator itself fault tolerant (replicating its log with consensus) removes the
blocking problem; this is what Spanner and CockroachDB do internally.

### Three-phase commit (3PC)

3PC adds a **pre-commit** phase between voting and committing, so participants that time out can
infer the outcome: if anyone is in pre-commit, everyone voted YES. It is non-blocking **only**
assuming bounded network delays and no partitions. Under a partition, two sides can decide
differently. It is studied, not used; real systems use consensus-backed commit instead.

### Sagas

A saga (Garcia-Molina and Salem, 1987) is a sequence of **local transactions** T1, T2, ... Tn.
Each Ti has a **compensating transaction** Ci that semantically undoes it. If Tk fails, run
Ck−1, ..., C1 in reverse.

```text
 Happy path:   T1 create order ─► T2 reserve stock ─► T3 charge card ─► T4 ship
 T3 fails:     T1 ─► T2 ─► T3 ✗
               C2 release stock ─► C1 cancel order
```

Properties and rules:

- **No isolation**. Other transactions see intermediate states (stock reserved, order pending).
  Countermeasures: semantic locks (a `PENDING` status others respect), commutative updates,
  re-reading values before acting, ordering steps so the riskiest is last.
- **Compensation is not rollback**. You cannot unsend an email; you send a correction. A refund is
  a new transaction, not a deletion.
- Compensations must be **idempotent** and **retryable until they succeed**; they cannot fail
  for business reasons.
- Order steps as: **compensatable** steps first, then the **pivot** (point of no return, for
  example charging the card), then **retryable** steps that are guaranteed to eventually succeed.

#### Choreography

No central coordinator. Each service listens for events and emits the next one.

```text
 Order svc ──OrderCreated──► Inventory svc ──StockReserved──► Payment svc
     ▲                            ▲                               │
     │                            └──── PaymentFailed ────────────┤
     └──────────── OrderCancelled ◄── StockReleased ◄─────────────┘
```

```java
public sealed interface OrderEvent permits OrderCreated, StockReserved, StockReleased,
        PaymentCompleted, PaymentFailed, OrderCancelled {
    String orderId();
}
public record OrderCreated(String orderId, String sku, int qty, long amountCents) implements OrderEvent {}
public record StockReserved(String orderId, long amountCents) implements OrderEvent {}
public record StockReleased(String orderId) implements OrderEvent {}
public record PaymentCompleted(String orderId) implements OrderEvent {}
public record PaymentFailed(String orderId, String reason) implements OrderEvent {}
public record OrderCancelled(String orderId) implements OrderEvent {}

/** Inventory service: reacts to OrderCreated and PaymentFailed. */
public final class InventoryHandler {
    private final InventoryRepository repo;
    private final EventPublisher publisher;   // writes to an outbox in the same transaction

    public InventoryHandler(InventoryRepository repo, EventPublisher publisher) {
        this.repo = repo;
        this.publisher = publisher;
    }

    public void on(OrderEvent event) {
        switch (event) {
            case OrderCreated e -> {
                if (repo.reserve(e.orderId(), e.sku(), e.qty())) {   // idempotent by orderId
                    publisher.publish(new StockReserved(e.orderId(), e.amountCents()));
                } else {
                    publisher.publish(new OrderCancelled(e.orderId()));
                }
            }
            case PaymentFailed e -> {
                repo.release(e.orderId());                           // compensation
                publisher.publish(new StockReleased(e.orderId()));
            }
            default -> { }                                           // not interested
        }
    }
}
```

Good for a few steps owned by different teams. Hard to see the whole flow, easy to create cyclic
dependencies, and hard to answer "what state is order 42 in?".

#### Orchestration

A **saga orchestrator** owns the workflow, sends commands, and persists the saga's state so it can
resume after a crash. Workflow engines like Temporal, Camunda, and AWS Step Functions provide this.

```java
public interface SagaStep<C> {
    String name();
    void execute(C ctx) throws Exception;
    void compensate(C ctx);                  // must be idempotent
}

public sealed interface SagaResult {
    record Completed() implements SagaResult {}
    record Compensated(String failedStep, String reason) implements SagaResult {}
}

public final class SagaOrchestrator<C> {
    private final List<SagaStep<C>> steps;
    private final SagaLog log;               // durable: saga id, step, status

    public SagaOrchestrator(List<SagaStep<C>> steps, SagaLog log) {
        this.steps = List.copyOf(steps);
        this.log = log;
    }

    public SagaResult run(String sagaId, C ctx) {
        Deque<SagaStep<C>> done = new ArrayDeque<>();
        for (SagaStep<C> step : steps) {
            try {
                log.record(sagaId, step.name(), "STARTED");
                step.execute(ctx);
                log.record(sagaId, step.name(), "DONE");
                done.push(step);
            } catch (Exception e) {
                log.record(sagaId, step.name(), "FAILED");
                while (!done.isEmpty()) {
                    SagaStep<C> s = done.pop();
                    compensateUntilSuccess(sagaId, s, ctx);
                }
                return new SagaResult.Compensated(step.name(), e.getMessage());
            }
        }
        return new SagaResult.Completed();
    }

    private void compensateUntilSuccess(String sagaId, SagaStep<C> step, C ctx) {
        long backoffMs = 100;
        while (true) {
            try {
                step.compensate(ctx);
                log.record(sagaId, step.name(), "COMPENSATED");
                return;
            } catch (RuntimeException e) {
                sleepQuietly(backoffMs);
                backoffMs = Math.min(backoffMs * 2, 30_000);
            }
        }
    }

    private static void sleepQuietly(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("interrupted during compensation", ie);
        }
    }

    public interface SagaLog {
        void record(String sagaId, String step, String status);
    }
}
```

This in-process loop shows the logic; in production, each step is an async command with a reply,
and the orchestrator's state lives in a database so a restarted orchestrator resumes from the log.

| | Choreography | Orchestration |
| --- | --- | --- |
| Coupling | Services know each other's events | Services know only the orchestrator's commands |
| Visibility | Flow is implicit, spread over services | Flow is explicit in one place |
| Single point of failure | No | Orchestrator (made durable and replicated) |
| Best for | 2–4 steps, simple flows | Many steps, branching, timeouts, human tasks |

### Transactional outbox

Any step that "updates my database **and** publishes an event" needs both or neither. Write the
event into an `outbox` table in the **same local transaction** as the business change, and have a
relay (poller or CDC such as Debezium) publish it. This gives at-least-once publishing without 2PC.
Details and Debezium configuration are in the message queues notes.

### TCC (Try-Confirm-Cancel)

A reservation-based variant popular in payment and travel systems:

- **Try**: reserve resources and check business rules (hold $100, hold a seat) with a timeout.
- **Confirm**: make the reservation permanent. Must be idempotent and should not fail if Try
  succeeded.
- **Cancel**: release the reservation. Must be idempotent and must handle "Cancel arrived before
  Try" (record an empty cancel so a late Try is refused).

Compared with sagas, TCC gives better isolation (reserved resources are not visible as consumed)
at the cost of every participant implementing three operations and reservation expiry.

| | 2PC | Saga | TCC |
| --- | --- | --- | --- |
| Atomicity | Yes | Eventually, via compensation | Eventually, via confirm/cancel |
| Isolation | Yes (locks) | No | Partial (reservations) |
| Blocking | Yes, on coordinator failure | No | No |
| Participants must support | XA / prepare | Local transactions + compensations | Try / Confirm / Cancel APIs |
| Typical use | Within one DB vendor, legacy enterprise | Microservices workflows | Payments, bookings |

---

## Idempotency and the exactly-once myth

### Exactly-once delivery is impossible; exactly-once effect is not

Over an unreliable network a sender that gets no acknowledgement must choose: resend (risking a
duplicate) or not (risking loss). So **delivery** is at-most-once or at-least-once. What systems
call "exactly-once" is really **effectively-once processing**: at-least-once delivery plus a
mechanism that makes duplicates harmless:

1. **Idempotent operations**: `SET status = 'PAID'` instead of `balance = balance - 100`.
2. **Deduplication by id**: record processed message ids, in the same transaction as the effect.
3. **Atomic commit of input position and output**: Kafka transactions commit the consumer offset
   and the produced records together; Flink checkpoints state and source offsets together.

The guarantee only holds **inside the boundary** of the system that provides it. Kafka's
exactly-once does not cover the HTTP call or email your consumer makes as a side effect.

### Idempotency keys for APIs

The client generates a unique key per logical operation and sends it with every retry (Stripe's
`Idempotency-Key` header is the well-known example). The server stores the key with the result.

```sql
CREATE TABLE idempotency_keys (
    key           TEXT PRIMARY KEY,
    request_hash  TEXT        NOT NULL,
    status        TEXT        NOT NULL,          -- IN_PROGRESS | DONE
    response_code INT,
    response_body JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

```java
public Response charge(String idempotencyKey, ChargeRequest req) {
    String hash = sha256(req);
    return tx.execute(status -> {
        int inserted = jdbc.update("""
                INSERT INTO idempotency_keys (key, request_hash, status)
                VALUES (?, ?, 'IN_PROGRESS')
                ON CONFLICT (key) DO NOTHING
                """, idempotencyKey, hash);
        if (inserted == 0) {
            var existing = jdbc.queryForMap(
                    "SELECT request_hash, status, response_code, response_body "
                    + "FROM idempotency_keys WHERE key = ? FOR UPDATE", idempotencyKey);
            if (!hash.equals(existing.get("request_hash"))) {
                return Response.status(422).entity("key reused with a different request").build();
            }
            if ("IN_PROGRESS".equals(existing.get("status"))) {
                return Response.status(409).entity("request in progress, retry later").build();
            }
            return Response.status((Integer) existing.get("response_code"))
                    .entity(existing.get("response_body").toString()).build();
        }
        ChargeResult result = ledger.charge(req);   // local DB writes, same transaction
        jdbc.update("""
                UPDATE idempotency_keys
                SET status = 'DONE', response_code = 201, response_body = ?::jsonb
                WHERE key = ?
                """, toJson(result), idempotencyKey);
        return Response.status(201).entity(result).build();
    });
}
```

Points that matter: the key insert and the business effect share one transaction; the request
hash detects a reused key with a different body; keys expire after a retention window (24 h is
common). If the effect is an external call that cannot join the transaction, pass the same key
downstream so the external system deduplicates too.

---

## Jepsen: what testing found

Jepsen (Kyle Kingsbury) runs real databases under injected partitions, clock skew, and crashes, and
checks histories against consistency models (with checkers such as Knossos and Elle). Recurring
lessons from its published analyses:

- **Documentation often overstates guarantees.** Many systems claimed "strong consistency" or
  "serializable" and lost acknowledged writes or showed anomalies under partitions.
- **Leader failover loses acknowledged writes** when replication is asynchronous or acknowledged
  before a majority has the data (seen in early analyses of Redis Sentinel, MongoDB with weak
  write concerns, and Elasticsearch).
- **Stale reads from deposed leaders**: early etcd and Consul analyses (2014) found that reads
  served by a leader without confirming its leadership could be stale; both added quorum /
  consistent read modes.
- **Defaults matter**: MongoDB 4.2.6 (2020) did not provide snapshot isolation for transactions
  under its default read and write concerns; stronger settings were needed.
- **Even mature databases have bugs**: PostgreSQL 12.3 (2020) showed a serializability anomaly
  (G2-item) under SSI that was fixed in a minor release.
- **Clock assumptions break things**: systems relying on synchronized clocks for LWW or leases
  misbehave under injected skew.

Read the analysis for any database before trusting its marketing; Jepsen reports state exact
versions and configurations.

---

## Common mistakes

- Calling a system "CA", or treating CAP as "pick two".
- Assuming `W + R > N` gives linearizability, especially with sloppy quorums or LWW.
- Using wall-clock timestamps to order events across machines (LWW silently drops writes).
- Measuring timeouts with `currentTimeMillis()` instead of a monotonic clock.
- Using Redis or Redlock locks for correctness without fencing tokens.
- Running a 4-node or 2-node consensus cluster (no extra fault tolerance; 2 nodes tolerate zero
  failures).
- Spreading a 3-node Raft cluster over 2 datacenters (losing the DC with 2 nodes loses quorum).
  Use 3 sites.
- Reading from followers or replicas and expecting read-your-writes.
- Treating saga compensations as rollbacks and forgetting the isolation gap.
- Believing "exactly-once" extends to side effects outside the broker or framework.
- Retrying non-idempotent operations on timeout.
- Not running Cassandra repair within `gc_grace_seconds`, causing deleted data to reappear.

---

## Interview questions

1. **Explain CAP precisely. Why is CA not an option?**
   CAP (Gilbert and Lynch) says that during a network partition, a replicated register cannot be
   both linearizable and available (every non-failing node answers). Partitions are not optional
   in a networked system, so the real choice is what to sacrifice during one. A "CA" system would
   have to pick anyway when a partition happens; it just has not decided how.

2. **What does PACELC add, and how would you classify Cassandra and Spanner?**
   It adds the normal-case trade-off between latency and consistency. Cassandra is PA/EL by default
   (tunable per query); Spanner is PC/EC, paying commit-wait and consensus latency for strict
   serializability.

3. **Difference between linearizability and serializability?**
   Linearizability is a recency guarantee for single operations on single objects, respecting real
   time. Serializability is an isolation guarantee for multi-object transactions: equivalent to
   some serial order, not necessarily real-time order. Both together are strict serializability.

4. **With N = 3, W = 2, R = 2, is the system linearizable?**
   Not necessarily. Concurrent reads during a partially applied write can see new then old values,
   failed writes are not rolled back, sloppy quorums break overlap, and LWW may discard the newer
   write. Linearizability needs synchronous read repair (ABD) or consensus.

5. **How would you give a user read-your-writes with read replicas?**
   After a write, store the commit LSN/version in the session. Route reads to a replica that has
   replayed at least that LSN, otherwise to the primary. Simpler: read from the primary for a short
   window after a write, or for data the user owns.

6. **Why can LWW lose data, and what are the alternatives?**
   Clock skew can give an older write a larger timestamp, and concurrent writes are resolved by
   discarding one. Alternatives: version vectors with siblings resolved by the application, CRDTs
   for mergeable types, or a single leader per key.

7. **Explain Lamport clocks versus vector clocks.**
   Lamport clocks are a single counter; a → b implies L(a) < L(b), but not the converse, so they
   cannot detect concurrency. Vector clocks keep one counter per process and characterize causality
   exactly, including detecting concurrent events, at O(n) size.

8. **Walk through Raft leader election and why a committed entry cannot be lost.**
   Followers time out, become candidates, increment the term, and request votes. Voters grant one
   vote per term, only to candidates whose log is at least as up-to-date. A committed entry is on a
   majority; any winning candidate needs a majority of votes; the majorities overlap, and the
   overlapping voter rejects candidates missing the entry. Plus: leaders only commit current-term
   entries by counting replicas.

9. **Why do consensus clusters have an odd number of nodes?**
   Tolerating f failures needs 2f + 1 nodes. An even count (2f + 2) still only tolerates f
   failures but adds a node to every quorum, increasing latency and cost.

10. **Your service uses a Redis lock to ensure a job runs on one worker. What can go wrong?**
    Lock expiry during a GC pause or slow I/O lets two workers run; Redis failover can lose the lock
    because replication is async. If double execution is merely wasteful, accept it. If it corrupts
    data, use etcd/ZooKeeper and fencing tokens checked by the storage layer, and make the job
    idempotent.

11. **What is a fencing token and where is it checked?**
    A monotonically increasing number issued with each lock grant. The protected resource stores the
    highest token seen and rejects lower ones. It must be checked by the resource, because the old
    lock holder cannot know it has been paused.

12. **Why is 2PC called blocking? When would you still use it?**
    Participants that voted YES cannot decide on their own if the coordinator fails, so they hold
    locks until it recovers. It is acceptable within one database system (or when the coordinator
    is replicated via consensus, as in Spanner) and for XA between a DB and a broker in legacy
    stacks, but rarely across microservices.

13. **Design an order flow across Order, Inventory, and Payment services.**
    Use a saga, usually orchestrated: create order (PENDING), reserve stock, charge card (pivot),
    confirm order. On failure, compensate in reverse (release stock, cancel order). Publish events
    via the outbox, make each step and compensation idempotent keyed by order id, and use
    PENDING status as a semantic lock.

14. **Is exactly-once delivery possible?**
    Not over an unreliable network. Exactly-once **processing** is achieved with at-least-once
    delivery plus idempotent handling or atomic commit of offsets and results (Kafka transactions,
    Flink checkpoints), and only inside the system that implements it.

15. **How does Spanner achieve external consistency?**
    TrueTime returns an uncertainty interval. A transaction takes timestamp s = latest bound, then
    waits until the earliest bound has passed s before releasing locks and replying. Any transaction
    starting afterwards gets a larger timestamp, so timestamp order matches real-time order.

---

## Summary

- **Networks fail partially**; timeouts are guesses; retries require idempotency.
- **CAP**: during a partition choose linearizability or availability. P is not optional. CA does
  not exist for replicated systems. Classify per operation.
- **PACELC**: without partitions, the trade-off is latency versus consistency.
- **Models, strongest to weakest**: strict serializable, linearizable, sequential, causal (strongest
  available under partitions), PRAM, session guarantees, strong eventual, eventual.
- **Quorums**: W + R > N gives overlap, not linearizability. Sloppy quorums, hinted handoff, read
  repair, and Merkle-tree anti-entropy keep leaderless replicas converging.
- **Conflicts**: LWW loses data; version vectors detect concurrency; CRDTs merge deterministically.
- **Clocks**: wall clocks drift and jump. Lamport for total order, vector clocks for causality, HLC
  for causality near physical time, TrueTime for bounded uncertainty with commit-wait.
- **Consensus**: Raft = terms + elections with up-to-date log restriction + majority-replicated log
  + current-term commit rule. 2f + 1 nodes tolerate f failures. Used by etcd, Consul, KRaft.
- **Locks**: leases expire under pauses; fencing tokens checked by the resource are mandatory for
  correctness. Prefer etcd/ZooKeeper over Redlock.
- **Transactions across services**: 2PC blocks; sagas with compensations plus the outbox are the
  norm; TCC when reservations are natural.
- **Exactly-once** means at-least-once plus idempotency or atomic offset/result commit.

| If you need | Use |
| --- | --- |
| Unique leader, lock, config | etcd / ZooKeeper / Consul with fencing |
| Highly available writes, mergeable data | Leaderless + CRDTs or version vectors |
| Strict serializable global SQL | Spanner, or CockroachDB with its caveats |
| User sees own writes on replicas | Session LSN token routing |
| Multi-service workflow | Saga (orchestrated) + outbox + idempotent steps |

---

## Further reading

- Martin Kleppmann, *Designing Data-Intensive Applications* (O'Reilly), chapters 5
  (Replication), 7 (Transactions), 8 (The Trouble with Distributed Systems), 9 (Consistency and
  Consensus).
- Diego Ongaro and John Ousterhout, "In Search of an Understandable Consensus Algorithm" (USENIX
  ATC 2014), and Ongaro's PhD dissertation "Consensus: Bridging Theory and Practice" (2014).
- Leslie Lamport, "Time, Clocks, and the Ordering of Events in a Distributed System"
  (Communications of the ACM, 1978).
- Eric Brewer, "CAP Twelve Years Later: How the 'Rules' Have Changed" (IEEE Computer, 2012).
- Seth Gilbert and Nancy Lynch, "Brewer's Conjecture and the Feasibility of Consistent, Available,
  Partition-Tolerant Web Services" (SIGACT News, 2002).
- Daniel Abadi, "Consistency Tradeoffs in Modern Distributed Database System Design" (IEEE
  Computer, 2012), the PACELC paper.
- Leslie Lamport, "Paxos Made Simple" (2001).
- Fischer, Lynch, Paterson, "Impossibility of Distributed Consensus with One Faulty Process"
  (JACM, 1985).
- DeCandia et al., "Dynamo: Amazon's Highly Available Key-value Store" (SOSP 2007).
- Corbett et al., "Spanner: Google's Globally-Distributed Database" (OSDI 2012).
- Shapiro, Preguiça, Baquero, Zawirski, "Conflict-free Replicated Data Types" (SSS 2011).
- Terry et al., "Session Guarantees for Weakly Consistent Replicated Data" (PDIS 1994).
- Kulkarni et al., "Logical Physical Clocks and Consistent Snapshots in Globally Distributed
  Databases" (2014), the HLC paper.
- Martin Kleppmann, "How to do distributed locking" (2016), and Salvatore Sanfilippo, "Is Redlock
  safe?" (2016).
- Jepsen analyses at jepsen.io.
