---
title: "Databases at scale"
order: 5
---
# Databases at Scale

Most backend scaling problems become database problems. These notes cover storage engines, durability,
isolation, indexing, replication, partitioning, and when to shard.

```text
┌────────────────────────────────────────────────────────────────────┐
│ Clients, connection pool                                           │
├────────────────────────────────────────────────────────────────────┤
│ Query layer: parser, planner, executor                             │
├────────────────────────────────────────────────────────────────────┤
│ Transaction layer: MVCC, locks, isolation                          │
├────────────────────────────────────────────────────────────────────┤
│ Storage engine: B+tree or LSM-tree, buffer pool, WAL               │
├────────────────────────────────────────────────────────────────────┤
│ Distribution: replication, partitioning, consensus                 │
└────────────────────────────────────────────────────────────────────┘
```

## Key points

- **B+trees**: in-place page updates; great point/range reads; random PKs (UUIDv4) cause splits and bloat.
- **LSM-trees**: append WAL + memtable + SSTables + compaction; high write throughput; read/compaction amplification.
- **WAL before data pages**: commit = durable log record (often one fsync); group commit improves throughput.
- **MVCC**: readers don't block writers; old versions need vacuum (PG) or purge (InnoDB)—long transactions hurt both.
- **Isolation**: PostgreSQL `REPEATABLE READ` is snapshot isolation; **write skew** needs `SERIALIZABLE` (SSI) or explicit locks.
- Fix lost updates with atomic SQL (`balance = balance + 10`), `FOR UPDATE`, or optimistic version columns.
- **Replication**: leader-follower is default; async replicas are stale—route RYW with LSN/GTID or sticky primary.
- **Quorums** (`W + R > N`): overlap, not linearizability; sloppy quorums and LWW break guarantees.
- **Sharding**: hash mod N is a trap; prefer fixed partitions, consistent hashing, or directory routing.
- Shard late: tune queries, cache, read replicas, vertical scale, archive cold data first.

---

## Storage engines

Pages: PostgreSQL 8 KB, InnoDB 16 KB default—unit of I/O and caching.

### B+tree vs LSM

| | B+tree | LSM-tree |
| --- | --- | --- |
| Writes | Random in-place pages | Sequential append + compaction |
| Point read | ~1 leaf (cached upper levels) | Memtable + Bloom filters + multiple SSTables |
| Range scan | Excellent (linked leaves) | Merge iterators across levels |
| Space | Split fragmentation | Tombstones until compaction |
| Examples | PostgreSQL, InnoDB | RocksDB, Cassandra, TiKV |

**Clustered (InnoDB)**: PK leaf **is** the row; secondary indexes store PK. **Heap (PostgreSQL)**: rows in heap; indexes store `ctid`.

Sequential keys (UUIDv7, serial) fill right-edge pages; random UUIDv4 scatters splits.

### LSM pipeline

```text
 write ──► WAL (fsync)
   │
   ▼
 memtable ──flush──► L0 SSTables (overlapping)
   │
   └── compaction ──► L1, L2... (non-overlapping in leveled)
```

| Compaction | Good for | Cost |
| ---------- | -------- | ---- |
| Size-tiered | Write-heavy | Up to ~2× space; read many files |
| Leveled | Read/update heavy | ~10–30× write amplification |
| Time-window | TTL time series | Bad for out-of-order updates |

**Amplification**: write = bytes to disk / app bytes; read = disk reads per logical read; space = disk / live data.

**RUM conjecture**: optimize two of read, update, space at expense of the third.

Bloom filters: ~10 bits/key, ~1% false positives—skip absent keys in SSTables.

---

## WAL and crash recovery

1. Append change to WAL (LSN).
2. Dirty data page may not hit disk until its WAL is durable.
3. **Commit** when commit record is durable in WAL—not when data pages flush.

Checkpoints advance the redo start point; **ARIES**-style: redo + undo for in-flight transactions.

Group commit batches many commits into one fsync.

---

## MVCC

Readers see a **snapshot**; writers create new versions.

| | PostgreSQL | InnoDB |
| --- | --- | --- |
| Old versions | Heap tuples (`xmin`/`xmax`) | Undo log chain |
| Update | New tuple (+ indexes unless HOT) | In-place row + undo |
| Cleanup | VACUUM / autovacuum | Purge |
| Long txn pain | Bloat, XID wraparound risk | Long undo chains |

Monitor PG `age(datfrozenxid)`; one open snapshot blocks vacuum for all tables.

---

## Transactions and isolation

### ACID vs BASE

ACID: atomicity, consistency (app invariants + constraints), isolation, durability (WAL + replication).

Many distributed stores are **eventually consistent** with optional stronger ops.

### Anomalies

| Anomaly | Summary |
| ------- | ------- |
| Dirty read | Uncommitted data from another txn |
| Non-repeatable read | Same row, two values in one txn |
| Phantom | Predicate query returns different row set |
| Lost update | Two read-modify-writes; one overwrites |
| Write skew | Concurrent updates to different rows break global invariant |

### Isolation levels (practical)

| Level | PG behaviour highlights |
| ----- | ------------------------ |
| Read committed (default) | New snapshot per statement |
| Repeatable read | Snapshot for whole txn; concurrent update → error |
| Serializable (SSI) | Detects dangerous cycles; **retry `40001`** |

MySQL InnoDB `REPEATABLE READ`: plain SELECT uses snapshot; locking reads see latest—**lost update** still possible without `FOR UPDATE` or atomic UPDATE.

**Write skew fix**: `SERIALIZABLE`, lock all rows the decision depends on (`FOR UPDATE`), or constraints (e.g. PostgreSQL `EXCLUDE` for booking ranges).

---

## Locking (essentials)

Row locks from `UPDATE`/`DELETE`/`SELECT FOR UPDATE`. DDL can block everything (`ACCESS EXCLUSIVE` on PG—use `lock_timeout` in migrations).

InnoDB **next-key locks** (gap + record) prevent phantoms on range `FOR UPDATE` under RR.

**Deadlock**: cycle of waits; engine picks victim—apps should retry.

---

## Indexing

| Index type | Use |
| ---------- | --- |
| B-tree (default) | Equality, range, sort |
| Hash | Equality only (PG) |
| GiST/GIN | Full text, JSON, ranges |
| Covering | Include columns → index-only scan |

**Selectivity**: low-cardinality indexes (boolean, country alone) often useless.

Composite index: leading column must match query filters; `(a, b)` helps `WHERE a = ?` and `WHERE a = ? AND b = ?`.

**N+1**: one query per row in a loop—fix with JOIN, `IN (...)`, or batch loader.

---

## Query optimization

Planner uses statistics (`ANALYZE`); wrong stats → bad plans.

| Symptom | Check |
| ------- | ----- |
| Seq scan on large table | Missing index, stale stats, selective predicate |
| Slow after migration | New index missing, lock contention |
| Spiky latency | Buffer pool cold, compaction (LSM), vacuum |

Explain (analyze, buffers) on production-like data sizes.

### Optimistic locking

Add `version` (or `updated_at`) column; update with `WHERE id = ? AND version = ?`. Zero rows updated → conflict, retry. Cheaper than `FOR UPDATE` when contention is low.

---

## Connection pooling

Each DB connection costs memory and file descriptors. Pool size ≈ `(cores * 2) + spindle_count` is a starting heuristic—measure wait time and DB `max_connections`.

**Prepared statements**: poolers like PgBouncer in transaction mode may break named prepared statements—use simple query mode or session pooling for heavy prep use.

---

## Normalization vs denormalization

Normalize for write consistency and smaller rows; denormalize (materialized counters, JSON blobs) for read latency—accept update complexity and staleness.

Common pattern: transactional normalized core + read-optimized projections (CQRS-lite).

---

## SQL vs NoSQL (decision lens)

| Need | Lean toward |
| ---- | ----------- |
| Ad hoc joins, strong txn on one node | PostgreSQL, MySQL |
| Massive partition-scale writes, tunable consistency | Cassandra, Scylla |
| Document model, flexible schema | MongoDB |
| Key-value cache/session | Redis |
| Global SQL + serializable | Spanner, CockroachDB, Yugabyte |

---

## Replication

```text
 app ──writes──► Leader ──log──► Follower(s)
                    ▲
                    └── reads (may lag)
```

| Mode | Commit when | Loss on leader death |
| ---- | ----------- | -------------------- |
| Async | Local WAL durable | Recent commits possible |
| Sync / quorum | Named replicas ack | Smaller window |
| Semi-sync | At least one replica received | Very small window |

PG: `synchronous_standby_names`, `synchronous_commit` levels (`remote_apply` for readable replica).

**Failover risks**: lost writes (async), split brain (need quorum + fencing), cross-region failover amplifies loss.

Patroni / Orchestrator / managed HA automate leader election.

### Multi-leader

Write conflicts inevitable—LWW loses data; CRDTs, merge, or home-region routing.

### Leaderless (Dynamo)

```text
 N=3, W=2, R=2  →  W+R=6>5 overlap (if strict quorums)
```

Read repair, Merkle anti-entropy (`nodetool repair` within `gc_grace_seconds`), hinted handoff when home node down.

---

## Partitioning (sharding)

Scales writes and storage; each partition usually has its own replication.

| Strategy | Range queries | Rebalance | Examples |
| -------- | ------------- | --------- | -------- |
| Range | Good | Split/merge | HBase, Cockroach ranges |
| Hash mod N | Scatter | ~90% keys move | Avoid |
| Consistent hash + vnodes | Scatter | ~1/N | Cassandra, DynamoDB |
| Fixed partitions | Scatter | Move partitions | ES shards, Redis 16384 slots |
| Directory | Flexible | Per-tenant moves | Vitess vindexes |

```text
 Consistent hash ring: key walks clockwise to first node
 Virtual nodes spread load when physical nodes fail
```

**Secondary indexes**: local (scatter-gather every partition) vs global (async or 2PC).

**Shard key rules**: high cardinality, even access, matches hot queries, stable value.

| Workload | Good key | Bad key |
| -------- | -------- | ------- |
| Multi-tenant | `tenant_id` | `created_at` alone |
| Chat | `conversation_id` | `sender_id` |
| IoT | `(device_id, day)` | timestamp alone |

**Costs**: cross-shard joins in app, distributed txns (2PC/sagas), global uniqueness (UUIDv7/Snowflake), ops multiply.

---

## Distributed SQL (NewSQL)

Automatic range sharding + **consensus per range** (Raft/Paxos).

| System | Notes |
| ------ | ----- |
| Spanner | Paxos + TrueTime commit-wait → external consistency |
| CockroachDB | Raft ranges ~512 MiB; serializable default; HLC + max offset |
| TiDB/TiKV | MySQL front; Percolator-style txns on RocksDB |
| Vitess | MySQL sharding layer; vindexes; not full cross-shard ACID |

Every write pays consensus RTT—design shard keys to keep transactions single-shard when possible.

---

## CQRS and read replicas

Separate write model from read-optimized projections; replicas serve reads with lag—combine with session routing (LSN/GTID) for RYW.

---

## Backups and PITR

Full backup + continuous WAL/binlog archiving → point-in-time recovery. Test restores. Replication is not a backup (logical corruption replicates).

---

## Choosing a database

| Signal | Direction |
| ------ | --------- |
| Complex queries, FKs, one-region strong ACID | PostgreSQL |
| Write-heavy, multi-region AP | Cassandra + tunable CL |
| Existing MySQL at huge scale | Vitess or migrate plan |
| Global serializable SQL | Spanner/Cockroach with eyes open on latency |

---

## Pitfalls

- Random UUID primary keys on InnoDB without understanding insert pattern.
- `REPEATABLE READ` assumed to prevent all concurrency bugs—write skew and MySQL lost updates still happen.
- Reading replicas immediately after user write without LSN/GTID routing.
- `hash(key) % num_nodes` when node count changes.
- Cassandra repair slower than `gc_grace_seconds` → deleted rows resurrect.
- Sharding before exhausting indexes, caching, and read replicas.
- Connection pool sized to “number of app threads” without measuring DB capacity.
- Long-running transactions blocking vacuum/purge.

---

## Quick check

**1. State the WAL rule in one sentence.**  
Log records for a change must be durable before the corresponding dirty data page is written; commit is durable when the commit record is in the log.

**2. When does PostgreSQL repeatable read abort?**  
On concurrent update to a row the transaction already read (`could not serialize access due to concurrent update`—retry).

**3. For N=3, W=2, R=2, what does W+R>N give you?**  
Read and write quorums overlap in at least one replica so reads usually see latest acknowledged writes—not linearizability.

**4. Why is consistent hashing preferred over hash mod N?**  
Adding a node moves about 1/N of keys instead of nearly all keys, avoiding cache/DB avalanches on resharding.

**5. Name three things you lose or complicate when sharding.**  
Cross-shard joins, global unique constraints without coordination, and simple multi-row transactions (need 2PC/sagas or redesign).
