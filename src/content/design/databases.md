---
title: "Databases at scale"
order: 5
---
# Databases at Scale

Most backend scaling problems end up as database problems. To design well you need to know what the
database does internally (storage engines, logs, versions, locks), what guarantees it really gives
(isolation levels and their anomalies), how to make queries fast (indexes, plans, pooling), and how
to spread data across machines (replication, partitioning, distributed SQL). These notes go through
each layer from the disk page up to multi-region clusters.

```text
┌────────────────────────────────────────────────────────────────────┐
│ Clients, connection pool                                           │
├────────────────────────────────────────────────────────────────────┤
│ Query layer: parser, planner / optimizer, executor                 │
├────────────────────────────────────────────────────────────────────┤
│ Transaction layer: MVCC, locks, isolation                          │
├────────────────────────────────────────────────────────────────────┤
│ Storage engine: B+tree or LSM-tree, buffer pool, WAL               │
├────────────────────────────────────────────────────────────────────┤
│ Distribution: replication, partitioning, consensus                 │
└────────────────────────────────────────────────────────────────────┘
```

---

## Storage engines

A storage engine decides how rows are laid out on disk and how they are found again. The two
dominant families are **B+trees** (update in place; PostgreSQL, MySQL InnoDB, SQL Server, Oracle)
and **LSM-trees** (append and merge; RocksDB, Cassandra, ScyllaDB, HBase, LevelDB, and the storage
layer of CockroachDB, TiDB/TiKV and YugabyteDB).

### Pages

Databases read and write disk in fixed-size **pages** (blocks): 8 KB in PostgreSQL, 16 KB by default
in InnoDB. A page is the unit of I/O, caching, and often locking. Most pages use a **slotted page**
layout so variable-length rows can move inside the page without changing their external address:

```text
┌────────────────────────────────────────────────────────────┐
│ page header: LSN, checksum, free space start / end         │
├────────────────────────────────────────────────────────────┤
│ slot array: [1][2][3][4]   grows forward ──►               │
│                                                            │
│                        free space                          │
│                                                            │
│ ◄── row data grows backward: [row 4][row 3][row 2][row 1]  │
└────────────────────────────────────────────────────────────┘
```

A row address is (page number, slot number). In PostgreSQL this is the **ctid** (tuple identifier).
Compaction inside a page only updates slot offsets.

### B+tree

A B+tree is a balanced search tree whose nodes are pages:

- **Internal pages** store separator keys and child page pointers only.
- **Leaf pages** store the keys and either the rows themselves or pointers to them, and are linked
  to their neighbours so range scans walk sideways without going back up.
- All leaves are at the same depth. Lookups cost `O(log_f N)` page reads where `f` is the fan-out.

```text
                         ┌─────────────────┐
                         │  [ 40 |  80 ]   │  root
                         └──┬─────┬─────┬──┘
           ┌────────────────┘     │     └─────────────────┐
           ▼                      ▼                       ▼
   ┌───────────────┐      ┌───────────────┐       ┌───────────────┐
   │ [ 10 |  25 ]  │      │ [ 50 |  65 ]  │       │ [ 90 | 120 ]  │  internal
   └───────────────┘      └───────────────┘       └───────────────┘
           │                      │                       │
           ▼                      ▼                       ▼
   leaves: [5 8] ◄──► [10 17 22] ◄──► [25 31] ◄──► ... ◄──► [120 133]
           sorted, doubly linked, hold the row (clustered) or a pointer
```

Why it is shallow: with 16 KB pages and roughly 100-byte entries (key plus pointer), an internal
page has a fan-out of over 100, often several hundred. Three levels address
`500 * 500 * (leaf rows)` which is hundreds of millions of rows; four levels reach billions. The
root and upper levels are always in the buffer pool, so a point lookup usually costs one or two
real disk reads.

**Inserts and page splits**: inserting into a full leaf splits it into two half-full pages and
pushes a separator key to the parent, which may split recursively. Splits cost I/O, leave pages
half empty (fragmentation), and contend on locks.

- Sequential keys (auto-increment, time-ordered UUIDv7) always insert at the right edge, so pages
  fill completely and stay hot in memory.
- Random keys (UUIDv4) insert all over the tree, causing splits, half-empty pages, and a working set
  as large as the whole index. This is why random UUID primary keys hurt InnoDB in particular.

**Clustered vs heap organization**:

| | InnoDB (MySQL) | PostgreSQL |
| --- | --- | --- |
| Table storage | Clustered index: the primary key B+tree's leaves **are** the rows | Heap: rows in insertion order in unordered pages |
| Secondary index leaf | Stores the **primary key** value | Stores the **ctid** (physical row address) |
| Secondary lookup | Index ► PK ► clustered index (two tree walks) | Index ► heap page (one hop) |
| Update moving a row | Secondary indexes unaffected | New row version needs new index entries (unless HOT) |
| Implication | Keep PK small; PK range scans are very fast | Clustering is not maintained; `CLUSTER` is one-off |

### LSM-tree (Log-Structured Merge tree)

An LSM-tree turns random writes into sequential writes:

```text
 write ──► WAL (append + fsync)
   │
   ▼
┌───────────────────┐  full   ┌────────────────────┐  flush
│ memtable          │ ──────► │ immutable memtable │ ───────► L0 SSTable files
│ (sorted skiplist) │         └────────────────────┘
└───────────────────┘

 L0:  [sst] [sst] [sst] [sst]          key ranges overlap, newest first
 L1:  [a-c] [d-h] [i-m] [n-s] [t-z]    non-overlapping, ~10x L0
 L2:  [....................... ]       non-overlapping, ~10x L1
                ▲
                └── background compaction merges levels, drops overwritten
                    values and tombstones
```

1. **Write**: append to the WAL for durability, insert into the in-memory **memtable** (a sorted
   structure, typically a skip list). Writes never read or modify disk pages in place.
2. **Flush**: when the memtable is full (for example 64 MB), it becomes immutable and is written to
   disk as a sorted, immutable **SSTable** (Sorted String Table) with a block index and a Bloom
   filter.
3. **Read**: check the memtable, then immutable memtables, then SSTables from newest to oldest. The
   first version found wins. Deletes write a **tombstone**, which shadows older values.
4. **Compaction**: background merge-sorts SSTables, keeps only the newest version of each key, and
   eventually drops tombstones.

#### Compaction strategies

| Strategy | How it works | Good for | Costs |
| --- | --- | --- | --- |
| **Size-tiered** (Cassandra STCS default, RocksDB "universal") | Merge several similar-sized SSTables into one bigger one | Write-heavy workloads | Up to ~2x temporary space; a key may exist in many tables, so reads touch more files |
| **Leveled** (LevelDB, RocksDB default, Cassandra LCS) | Each level ~10x the previous; non-overlapping files per level; compaction merges one file into the overlapping files of the next level | Read-heavy and update-heavy workloads | Higher write amplification (a key is rewritten roughly once per level, ~10x each time in the worst case) |
| **Time-window** (Cassandra TWCS) | SSTables bucketed by write time; only compacted within a window; whole windows expire | Time series with TTL | Bad if data is updated out of order |

#### Write, read and space amplification

- **Write amplification**: bytes written to disk divided by bytes written by the application. A
  B+tree rewrites a whole 8–16 KB page (plus a WAL record, plus possibly a full-page image) to change a
  100-byte row. An LSM-tree writes the WAL plus each compaction rewrite; leveled compaction often
  totals 10–30x.
- **Read amplification**: disk reads per logical read. A B+tree needs about one leaf read once upper
  levels are cached. An LSM-tree may check one file per level (and every L0 file), mitigated by Bloom
  filters and block caches.
- **Space amplification**: disk bytes divided by live data bytes. B+trees waste space in half-full
  pages after splits. LSM-trees hold obsolete versions and tombstones until compaction; leveled
  compaction keeps it around 1.1x, size-tiered can reach 2x.

The **RUM conjecture** (Athanassoulis et al., 2016) states that you can optimize for two of read,
update and memory (space) overheads at the expense of the third. B+trees favour reads; LSM-trees
favour writes and space.

| | B+tree | LSM-tree |
| --- | --- | --- |
| Writes | Random in-place page writes | Sequential appends; higher sustained write throughput |
| Point reads | Predictable, ~1 disk read | Memtable + Bloom filters + possibly several files |
| Range scans | Excellent (sorted linked leaves) | Good, but must merge iterators from many files |
| Latency predictability | Stable | Compaction can cause latency spikes and write stalls |
| Space | Fragmentation from splits | Better compression (immutable, sorted, dense blocks) |
| Typical systems | PostgreSQL, InnoDB, SQL Server | RocksDB, Cassandra, ScyllaDB, HBase, TiKV |

#### Bloom filters in LSM-trees

Each SSTable carries a Bloom filter over its keys. A point read for a key that is not in a file can
skip that file without any disk read when the filter says "definitely absent". With about 10 bits
per key and 7 hash functions, the false positive rate is about 1%, so a read that would have touched
5 files touches about 1 plus 0.04 on average. Bloom filters do not help range scans (RocksDB adds
prefix Bloom filters for prefix scans). The math is in the caching notes: `m/n ≈ 9.6` bits per key
for 1% false positives, `k = (m/n) ln 2`.

---

## Write-ahead logging and crash recovery

### The WAL rule

The buffer pool modifies pages in memory and writes them to disk later. To survive a crash:

1. Every change is first described in a **write-ahead log** (WAL; InnoDB calls it the **redo log**),
   an append-only sequential file. Each record has a **Log Sequence Number (LSN)**.
2. **A dirty page may not be written to disk before the log records describing its changes are
   durable** (the page's LSN must be at most the flushed log LSN).
3. **A transaction is committed when its commit record is durable** in the log (fsync), not when its
   data pages are written.

Sequential log appends are much cheaper than random page writes, so commit latency is one fsync.
**Group commit** batches the commit records of many concurrent transactions into one fsync, which
is why throughput scales with concurrency.

### Checkpoints

A **checkpoint** flushes dirty pages and records an LSN from which recovery must start. Without
checkpoints, recovery would replay the log from the beginning of time, and the log could never be
truncated. Frequent checkpoints mean faster recovery but more write I/O. PostgreSQL tunes this with
`checkpoint_timeout` and `max_wal_size`.

### Crash recovery (ARIES)

ARIES (Mohan et al., 1992) is the classic algorithm behind most recovery implementations:

```text
 log:  ... [checkpoint] .... T1 upd .. T2 upd .. T1 COMMIT .. T2 upd ..  CRASH
              │
 1. Analysis  │──► scan forward: which transactions were active, which pages were dirty
 2. Redo      │──► replay history from the oldest dirty page LSN, for ALL transactions
 3. Undo      ◄─── roll back transactions that never committed (T2), newest first
```

- **Redo** brings pages to their exact state at the crash ("repeating history").
- **Undo** removes the effects of uncommitted transactions, writing compensation log records so
  undo itself is crash-safe.
- PostgreSQL needs only redo: because of its MVCC design (old row versions stay in the heap),
  uncommitted changes are simply invisible once the transaction is marked aborted in the commit log.
  InnoDB uses redo logs plus **undo logs**.

### Torn pages

A 16 KB page write can be interrupted halfway (the OS and disk write in smaller units), leaving a
page that is neither old nor new. Redo records describing a small change cannot fix a corrupted
page.

- **InnoDB doublewrite buffer**: pages are first written to a doublewrite area, then to their real
  location. After a crash, a torn page is restored from the doublewrite copy.
- **PostgreSQL full_page_writes**: the first modification of a page after each checkpoint writes the
  whole page image into the WAL. This is one reason WAL volume spikes right after checkpoints.

### Durability knobs

| Setting | Effect | Risk |
| --- | --- | --- |
| InnoDB `innodb_flush_log_at_trx_commit = 1` | fsync redo log at every commit (default) | None; full durability |
| `= 2` | Write to OS at commit, fsync about once per second | Lose ~1 s on OS crash or power loss |
| `= 0` | Write and fsync about once per second | Lose ~1 s even on a mysqld crash |
| PostgreSQL `synchronous_commit = off` | Commit returns before WAL fsync | Lose recent commits on crash, no corruption |
| `fsync = off` (PostgreSQL) | Never fsync | Data corruption on crash; never in production |

---

## MVCC (multi-version concurrency control)

MVCC keeps multiple versions of a row so that **readers never block writers and writers never block
readers**. Each transaction reads from a **snapshot**: the set of transactions committed at the time
the snapshot was taken.

### PostgreSQL: versions in the heap

Every row version (tuple) carries hidden system columns:

- `xmin`: the transaction ID that created this version.
- `xmax`: the transaction ID that deleted or replaced it (0 if alive).
- `ctid`: the tuple's own location; for an outdated version it points to the newer version.

An `UPDATE` never overwrites in place. It marks the old tuple's `xmax` and inserts a new tuple:

```text
 UPDATE accounts SET balance = 70 WHERE id = 1;     -- by transaction 105

 heap page
 ┌──────────────────────────────────────────────────────────────────┐
 │ (0,1)  xmin=100  xmax=105  id=1 balance=100   ◄── old version    │
 │ (0,2)  xmin=105  xmax=0    id=1 balance=70    ◄── new version    │
 └──────────────────────────────────────────────────────────────────┘
 A snapshot taken before 105 committed sees (0,1); one taken after sees (0,2).
```

Simplified visibility rule: a tuple is visible to a snapshot if its `xmin` committed before the
snapshot, and its `xmax` is empty, aborted, or not yet committed as of the snapshot.

Consequences:

- **Dead tuples** accumulate. **VACUUM** (and autovacuum) reclaims them and updates the visibility
  map and free space map. A long-running transaction holds back the oldest visible snapshot, so
  vacuum cannot remove anything newer: one forgotten open transaction can bloat every table.
- **Transaction ID wraparound**: XIDs are 32-bit. Vacuum must "freeze" old tuples before about 2
  billion transactions pass, or PostgreSQL stops accepting writes to protect data. Monitor
  `age(datfrozenxid)`.
- Every update creates new index entries for the new tuple, unless it is a **HOT update** (Heap-Only
  Tuple): if no indexed column changed and the new version fits in the same page, indexes keep
  pointing at the old tuple, which redirects. Leaving free space with `fillfactor` (for example 90)
  makes HOT updates more likely.

### InnoDB: in-place update plus undo log

InnoDB updates the row in the clustered index **in place** and writes the previous version to the
**undo log**. Each row has hidden fields:

- `DB_TRX_ID`: the last transaction that modified the row.
- `DB_ROLL_PTR`: a pointer into the undo log to reconstruct the previous version.

```text
 clustered index leaf                   undo log
 ┌─────────────────────────────┐       ┌─────────────────────────────┐
 │ id=1 balance=70  trx=105    │       │ id=1 balance=100  trx=100   │
 │ roll_ptr ───────────────────┼─────► │ roll_ptr ──► older versions │
 └─────────────────────────────┘       └─────────────────────────────┘
```

A consistent read creates a **read view** (the list of transactions active at that moment). If the
latest row version is not visible to the read view, InnoDB follows `DB_ROLL_PTR` and rebuilds older
versions from undo records until it finds a visible one. A **purge** thread deletes undo records no
read view needs. Long transactions make the undo chains (and `History list length` in `SHOW ENGINE
INNODB STATUS`) grow, slowing every read that must walk them.

| | PostgreSQL | InnoDB |
| --- | --- | --- |
| Where old versions live | In the table heap | In the undo log (rollback segments) |
| Update cost | New tuple, new index entries unless HOT | In-place; secondary index entries marked, not rewritten for unchanged columns |
| Cleanup | VACUUM | Purge threads |
| Rollback | Cheap (mark aborted) | Expensive (apply undo) |
| Long transaction pain | Table and index bloat | Long undo chains, slow reads |

---

## Transactions and isolation

### ACID

- **Atomicity**: all of a transaction's writes happen or none do (implemented with undo / MVCC and
  the commit record).
- **Consistency**: the application's invariants hold. The database helps with constraints (foreign
  keys, unique, check), but mostly this is the application's job.
- **Isolation**: concurrent transactions do not interfere, to the degree defined by the isolation
  level.
- **Durability**: once committed, data survives crashes (WAL plus fsync, and replication for
  machine loss).

Many distributed NoSQL systems advertise **BASE** instead: Basically Available, Soft state,
Eventually consistent. In practice the line is blurry: DynamoDB, MongoDB and Cassandra (lightweight
transactions) all offer some transactional features now.

### Anomalies

| Anomaly | What happens |
| --- | --- |
| **Dirty read** | T1 reads data written by T2 that has not committed (and may roll back). |
| **Dirty write** | T1 overwrites data written by uncommitted T2. Prevented at every level by row locks. |
| **Non-repeatable read** | T1 reads a row twice and gets different values because T2 committed an update in between. |
| **Phantom read** | T1 runs the same predicate query twice and gets a different set of rows because T2 inserted or deleted matching rows. |
| **Lost update** | T1 and T2 both read a value, both compute a new value from it, both write; one update is lost. |
| **Write skew** | T1 and T2 read an overlapping set, each makes a decision based on it, and each writes a **different** row; the combined result violates an invariant neither write violated alone. |

### Isolation levels: standard vs reality

The SQL standard defines levels by the first three anomalies only, which is why real behaviour
differs by database.

| Level | Dirty read | Non-repeatable | Phantom | Lost update | Write skew |
| --- | --- | --- | --- | --- | --- |
| Read uncommitted (standard) | Possible | Possible | Possible | Possible | Possible |
| Read committed (PG default) | No | Possible | Possible | Possible | Possible |
| Repeatable read, PostgreSQL (snapshot isolation) | No | No | No | No (aborts with serialization error) | **Possible** |
| Repeatable read, MySQL InnoDB (default) | No | No for plain reads | No for plain reads; locking reads see new rows | **Possible** | Possible |
| Serializable, PostgreSQL (SSI) | No | No | No | No | No |
| Serializable, MySQL (2PL via shared locks) | No | No | No | No | No |

Notes:

- PostgreSQL treats `READ UNCOMMITTED` as `READ COMMITTED`; it never shows dirty data.
- In **Read Committed**, every statement takes a new snapshot. In **Repeatable Read / snapshot
  isolation**, the whole transaction uses the snapshot taken at its first statement.
- MySQL's Repeatable Read mixes two modes: plain `SELECT`s read the snapshot, while `UPDATE`,
  `DELETE` and `SELECT ... FOR UPDATE` read and lock the **latest committed** rows. That is why a
  lost update can slip through.
- PostgreSQL's Serializable is **Serializable Snapshot Isolation** (Cahill et al., 2008): it runs
  like snapshot isolation, tracks read/write dependencies, and aborts a transaction (SQLSTATE
  `40001`) when a dangerous cycle could make the outcome non-serializable. Applications **must
  retry** on `40001`.

### Demonstrations

Each example uses two concurrent sessions, A and B, against:

```sql
CREATE TABLE accounts (id int PRIMARY KEY, owner text, balance int NOT NULL);
INSERT INTO accounts VALUES (1, 'ada', 100), (2, 'bob', 50);
```

#### Non-repeatable read (Read Committed)

```sql
-- A
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT balance FROM accounts WHERE id = 1;          -- 100

-- B
UPDATE accounts SET balance = 80 WHERE id = 1;      -- autocommit

-- A
SELECT balance FROM accounts WHERE id = 1;          -- 80: same query, new value
COMMIT;
```

Under `REPEATABLE READ`, A's second read returns 100 again because A keeps its first snapshot.

#### Phantom read (Read Committed)

```sql
-- A
BEGIN ISOLATION LEVEL READ COMMITTED;
SELECT count(*) FROM accounts WHERE balance >= 50;  -- 2

-- B
INSERT INTO accounts VALUES (3, 'cy', 75);

-- A
SELECT count(*) FROM accounts WHERE balance >= 50;  -- 3: a phantom row appeared
COMMIT;
```

#### Lost update (read-modify-write)

```sql
-- A                                              -- B
BEGIN;                                            BEGIN;
SELECT balance FROM accounts WHERE id = 1;        SELECT balance FROM accounts WHERE id = 1;
-- app computes 100 + 10 = 110                    -- app computes 100 - 30 = 70
UPDATE accounts SET balance = 110 WHERE id = 1;
COMMIT;
                                                  UPDATE accounts SET balance = 70 WHERE id = 1;
                                                  COMMIT;   -- final 70: A's +10 is lost
```

What each database does:

- PostgreSQL Read Committed and MySQL Repeatable Read: final balance 70, the update is lost silently.
- PostgreSQL Repeatable Read: B's `UPDATE` fails with `could not serialize access due to concurrent
  update`, and B must retry.

Fixes, best first:

```sql
-- 1. Atomic update: no read-modify-write in the application
UPDATE accounts SET balance = balance + 10 WHERE id = 1;

-- 2. Pessimistic: lock the row while computing
BEGIN;
SELECT balance FROM accounts WHERE id = 1 FOR UPDATE;
UPDATE accounts SET balance = 110 WHERE id = 1;
COMMIT;

-- 3. Optimistic: compare-and-set on a version column (see "Optimistic locking")
UPDATE accounts SET balance = 110, version = version + 1 WHERE id = 1 AND version = 7;
```

#### Write skew (the on-call doctors problem)

Invariant: at least one doctor must stay on call.

```sql
CREATE TABLE doctors (name text PRIMARY KEY, on_call boolean NOT NULL);
INSERT INTO doctors VALUES ('alice', true), ('bob', true);

-- A (alice)                                       -- B (bob)
BEGIN ISOLATION LEVEL REPEATABLE READ;             BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT count(*) FROM doctors WHERE on_call;        SELECT count(*) FROM doctors WHERE on_call;
-- 2, so it is safe for me to leave                -- 2, so it is safe for me to leave
UPDATE doctors SET on_call = false                 UPDATE doctors SET on_call = false
  WHERE name = 'alice';                              WHERE name = 'bob';
COMMIT;                                            COMMIT;
-- Both commit. Nobody is on call.
```

The two transactions update **different rows**, so row locks and snapshot isolation do not detect a
conflict. Fixes:

- Run at `SERIALIZABLE` in PostgreSQL: one transaction fails with `40001` and the retry sees count 1.
- Lock the rows the decision depends on: `SELECT ... FROM doctors WHERE on_call FOR UPDATE`. This
  works only if the rows exist. When the check is about the **absence** of rows (double-booking a
  meeting room), there is nothing to lock; use serializable isolation, a unique or exclusion
  constraint, or **materialize the conflict** (pre-create one row per room and time slot and lock
  it).

PostgreSQL exclusion constraints solve the booking case declaratively:

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE bookings (
    room_id int NOT NULL,
    during  tstzrange NOT NULL,
    EXCLUDE USING gist (room_id WITH =, during WITH &&)
);
```

---

## Locking

### Lock types

- **Shared (S)** locks allow other readers; **exclusive (X)** locks block everyone else.
- **Row-level locks** are taken by `UPDATE`, `DELETE`, and locking reads.
- **Table-level locks** are taken by DDL. In PostgreSQL, `ALTER TABLE` often takes an `ACCESS
  EXCLUSIVE` lock; a migration waiting for it queues **all** subsequent queries behind it. Use
  `lock_timeout` in migrations.
- **Intention locks** (InnoDB `IS`/`IX`) at table level signal that row locks exist below, so a table
  lock request can be checked cheaply.

### InnoDB record, gap and next-key locks

To prevent phantoms for locking reads under Repeatable Read, InnoDB locks **index ranges**, not just
rows:

```text
 index on age:     10        20        30
                   │         │         │
 record lock:      ■ locks the index record 20 itself
 gap lock:         (10 ─────── 20)  locks the gap; no inserts in between
 next-key lock:    (10 ─────── 20]  gap plus the record = default for range scans
```

```sql
-- Session A (InnoDB, REPEATABLE READ)
START TRANSACTION;
SELECT * FROM people WHERE age BETWEEN 15 AND 25 FOR UPDATE;

-- Session B
INSERT INTO people (name, age) VALUES ('zed', 18);   -- blocks until A ends: gap is locked
```

Consequences:

- A locking query **without a usable index** scans and locks every row (and gap) it examines, which
  effectively locks the table. Index the columns in your `WHERE` for locking statements.
- Gap locks cause surprising deadlocks between inserts. Under `READ COMMITTED`, InnoDB uses gap locks
  only for foreign-key and duplicate-key checking, which is one reason many MySQL shops run at RC.
- PostgreSQL does not use gap locks; it prevents phantoms through snapshots and, at Serializable,
  predicate (SIREAD) locks that only detect conflicts and never block.

### SELECT FOR UPDATE and friends

```sql
SELECT * FROM jobs WHERE id = 7 FOR UPDATE;          -- X lock; others wait
SELECT * FROM jobs WHERE id = 7 FOR SHARE;           -- S lock; blocks writers
SELECT * FROM jobs WHERE id = 7 FOR UPDATE NOWAIT;   -- error immediately if locked

-- Work queue: each worker grabs different rows without blocking on others
SELECT id, payload
FROM jobs
WHERE status = 'pending'
ORDER BY created_at
LIMIT 10
FOR UPDATE SKIP LOCKED;
```

`SKIP LOCKED` (PostgreSQL 9.5+, MySQL 8.0+) turns a table into a reasonable job queue for moderate
volumes. Keep transactions that hold row locks **short**, and never hold a lock across a network
call to another service.

### Optimistic locking

Optimistic concurrency control assumes conflicts are rare: read without locks, then write only if
nothing changed since, detected by a **version column**. On conflict, retry or report to the user.
It suits web applications where a "transaction" spans a user think-time (read form, edit, submit)
that is far too long to hold a database lock.

```sql
ALTER TABLE accounts ADD COLUMN version bigint NOT NULL DEFAULT 0;
```

```java
public record Account(long id, BigDecimal balance, long version) {}

public final class OptimisticConflictException extends RuntimeException {
    public OptimisticConflictException(long id) {
        super("concurrent modification of account " + id);
    }
}

public final class AccountDao {
    private static final int MAX_ATTEMPTS = 3;
    private final DataSource dataSource;

    public AccountDao(DataSource dataSource) {
        this.dataSource = dataSource;
    }

    public void withdraw(long id, BigDecimal amount) throws SQLException {
        for (int attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
            try (Connection c = dataSource.getConnection()) {
                Account a = load(c, id);
                if (a.balance().compareTo(amount) < 0) {
                    throw new IllegalStateException("insufficient funds");
                }
                if (tryUpdate(c, a, a.balance().subtract(amount))) {
                    return;
                }
            }
        }
        throw new OptimisticConflictException(id);
    }

    private Account load(Connection c, long id) throws SQLException {
        try (PreparedStatement ps = c.prepareStatement(
                "SELECT id, balance, version FROM accounts WHERE id = ?")) {
            ps.setLong(1, id);
            try (ResultSet rs = ps.executeQuery()) {
                if (!rs.next()) throw new NoSuchElementException("account " + id);
                return new Account(rs.getLong(1), rs.getBigDecimal(2), rs.getLong(3));
            }
        }
    }

    private boolean tryUpdate(Connection c, Account a, BigDecimal newBalance) throws SQLException {
        try (PreparedStatement ps = c.prepareStatement("""
                UPDATE accounts
                SET balance = ?, version = version + 1
                WHERE id = ? AND version = ?
                """)) {
            ps.setBigDecimal(1, newBalance);
            ps.setLong(2, a.id());
            ps.setLong(3, a.version());
            return ps.executeUpdate() == 1;
        }
    }
}
```

With JPA, annotate a field with `@Version`. Hibernate adds `AND version = ?` to updates and throws
`OptimisticLockException` (wrapped by Spring as `ObjectOptimisticLockingFailureException`) when no
row matched:

```java
@Entity
public class Product {
    @Id
    private Long id;

    private int stock;

    @Version
    private long version;

    public void reserve(int qty) {
        if (stock < qty) throw new IllegalStateException("out of stock");
        stock -= qty;
    }
}
```

| | Pessimistic (`FOR UPDATE`) | Optimistic (version column) |
| --- | --- | --- |
| Conflict handling | Waits for the lock | Detects at write, caller retries |
| Best when | Contention is high, retries are expensive | Contention is low, think-time is long |
| Risks | Lock waits, deadlocks, long-held locks | Retry storms under high contention |
| Works across HTTP requests | No | Yes (version sent to client, back in the form) |

### Deadlocks

A deadlock is a cycle of transactions each waiting for a lock the next one holds.

```sql
-- A                                             -- B
BEGIN;                                           BEGIN;
UPDATE accounts SET balance = balance - 10       UPDATE accounts SET balance = balance - 5
  WHERE id = 1;           -- locks row 1           WHERE id = 2;           -- locks row 2
UPDATE accounts SET balance = balance + 10       UPDATE accounts SET balance = balance + 5
  WHERE id = 2;           -- waits for B           WHERE id = 1;           -- waits for A: cycle
```

- **Detection**: InnoDB detects cycles in its wait-for graph immediately (`innodb_deadlock_detect`)
  and rolls back the transaction with less undo; error 1213 / SQLSTATE `40001`. PostgreSQL checks for
  cycles after waiting `deadlock_timeout` (default 1 s) and aborts one with SQLSTATE `40P01`.
- **Prevention**: acquire locks in a **consistent global order** (for a transfer, always lock the
  lower account ID first), keep transactions short, touch fewer rows, and index the columns used in
  locking statements so fewer rows are locked.
- **Handling**: deadlocks cannot be fully prevented in a busy system. Retry the whole transaction
  with backoff.

```java
public <T> T inTransactionWithRetry(TransactionTemplate tx, Supplier<T> work) {
    int attempt = 0;
    while (true) {
        try {
            return tx.execute(status -> work.get());
        } catch (ConcurrencyFailureException e) {   // Spring maps 40001 / 40P01 / 1213 here
            if (++attempt >= 5) throw e;
            long backoffMs = (long) (Math.pow(2, attempt) * 10
                    + ThreadLocalRandom.current().nextInt(10));
            try {
                Thread.sleep(backoffMs);
            } catch (InterruptedException ie) {
                Thread.currentThread().interrupt();
                throw e;
            }
        }
    }
}
```

---

## Indexing deep dive

An index is a separate data structure that maps column values to rows, trading write cost and
space for faster reads. Every index must be updated on every insert, on deletes, and on updates of
the indexed columns.

### B-tree indexes

The default index type. Supports equality, ranges (`<`, `>`, `BETWEEN`), prefix `LIKE 'abc%'` (with
the right collation or operator class), `ORDER BY` without a sort, and `MIN`/`MAX`. Complexity
`O(log N)` to find the first entry, then sequential leaf reads.

### Hash indexes

Equality only, no ordering, no ranges. PostgreSQL hash indexes are crash-safe only since version 10
and rarely beat B-trees in practice. InnoDB does not support user-defined hash indexes (only the
`MEMORY` engine does); it has an internal **adaptive hash index** that it builds automatically over
hot B-tree pages.

### Composite indexes and the leftmost-prefix rule

A composite index on `(a, b, c)` is sorted by `a`, then by `b` within equal `a`, then by `c`. It is
like a phone book sorted by last name, then first name.

| Query predicate | Can use index `(a, b, c)`? |
| --- | --- |
| `a = 1` | Yes |
| `a = 1 AND b = 2` | Yes |
| `a = 1 AND b = 2 AND c = 3` | Yes, fully |
| `b = 2` | No (no leading column); at best a full index scan or skip scan |
| `a = 1 AND c = 3` | Uses `a` for the seek; `c` filtered within that range |
| `a = 1 AND b > 5 AND c = 3` | Seeks on `a` and the range on `b`; `c` cannot narrow the seek |
| `a = 1 ORDER BY b` | Yes, no sort needed |
| `a > 1 ORDER BY b` | Seek on `a`, but must sort by `b` |

Column order rules of thumb:

1. **Equality columns first**, then **one range or sort column** last. A range on a column stops
   later columns from being used for seeking.
2. Among equality columns, order by what other queries also need as a prefix (so one index serves
   several queries), not strictly by selectivity.
3. Match the `ORDER BY` direction, or use a backward scan; mixed directions need an index declared
   with matching `ASC`/`DESC`.

MySQL 8.0.13+ can sometimes use a **skip scan** when the leading column has very few distinct
values; do not design around it.

### Covering indexes and index-only scans

If an index contains every column a query needs, the database can answer from the index alone,
skipping the table lookups (which are often random I/O).

```sql
-- Query
SELECT created_at, total FROM orders WHERE customer_id = ? ORDER BY created_at DESC LIMIT 20;

-- PostgreSQL 11+: key columns plus non-key payload columns
CREATE INDEX idx_orders_cust_created ON orders (customer_id, created_at DESC) INCLUDE (total);

-- MySQL: just put the columns in the index (InnoDB secondaries already contain the PK)
CREATE INDEX idx_orders_cust_created ON orders (customer_id, created_at, total);
```

In PostgreSQL an index-only scan still checks the **visibility map**; pages modified since the last
vacuum need a heap visit. `EXPLAIN` shows this as `Heap Fetches`.

### Partial indexes

Index only the rows a query cares about (PostgreSQL, SQLite; not MySQL):

```sql
CREATE INDEX idx_orders_pending ON orders (created_at) WHERE status = 'pending';

-- Unique among active rows only: soft-deleted rows may repeat the email
CREATE UNIQUE INDEX uq_users_email_active ON users (email) WHERE deleted_at IS NULL;
```

Smaller index, cheaper writes for rows outside the predicate, and it encodes business rules.

### Expression (functional) indexes

```sql
-- WHERE lower(email) = ? cannot use a plain index on email
CREATE INDEX idx_users_email_lower ON users (lower(email));             -- PostgreSQL
CREATE INDEX idx_users_email_lower ON users ((lower(email)));           -- MySQL 8.0.13+
```

The query must use the same expression. A common alternative is a generated column with a normal
index.

### GIN, GiST, BRIN (PostgreSQL)

| Type | Structure | Use for |
| --- | --- | --- |
| **GIN** (Generalized Inverted Index) | Maps each element (word, array item, JSON key) to the rows containing it | `jsonb` containment (`@>`), arrays (`&&`, `@>`), full-text (`tsvector`), trigram search (`pg_trgm`) |
| **GiST** (Generalized Search Tree) | Balanced tree of bounding predicates | Geometric and range types, nearest-neighbour, exclusion constraints, PostGIS |
| **SP-GiST** | Space-partitioned trees (quad-trees, tries) | Points, IP ranges, non-balanced data |
| **BRIN** (Block Range Index) | Min/max per range of heap pages; tiny | Huge append-only tables where values correlate with physical order (time series) |

```sql
CREATE INDEX idx_events_payload ON events USING gin (payload jsonb_path_ops);
SELECT * FROM events WHERE payload @> '{"type": "signup", "country": "IN"}';

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX idx_products_name_trgm ON products USING gin (name gin_trgm_ops);
SELECT * FROM products WHERE name ILIKE '%kettle%';    -- now indexable

CREATE INDEX idx_metrics_ts_brin ON metrics USING brin (recorded_at);
```

GIN indexes are slower to update (PostgreSQL buffers new entries in a pending list via `fastupdate`)
but fast to search.

### Full-text search

```sql
-- PostgreSQL
ALTER TABLE articles ADD COLUMN search tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(title, '') || ' ' || body)) STORED;
CREATE INDEX idx_articles_search ON articles USING gin (search);

SELECT id, title, ts_rank(search, q) AS rank
FROM articles, websearch_to_tsquery('english', 'database index tuning') AS q
WHERE search @@ q
ORDER BY rank DESC
LIMIT 10;

-- MySQL InnoDB
CREATE FULLTEXT INDEX ft_articles ON articles (title, body);
SELECT id, title FROM articles
WHERE MATCH(title, body) AGAINST ('database index tuning' IN NATURAL LANGUAGE MODE);
```

Built-in full-text search is good enough for many products. Move to Elasticsearch or OpenSearch when
you need relevance tuning, faceting, fuzzy matching, many languages, or search load isolated from
the OLTP database.

### Reading EXPLAIN output

Always use `EXPLAIN (ANALYZE, BUFFERS)` in PostgreSQL (it executes the query, so wrap writes in a
transaction you roll back), or `EXPLAIN ANALYZE` in MySQL 8.0.18+.

A bad plan:

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT id, total FROM orders
WHERE customer_id = 42 AND status = 'paid'
ORDER BY created_at DESC LIMIT 20;
```

```text
Limit  (cost=48211.10..48211.15 rows=20 width=16)
       (actual time=412.3..412.3 rows=20 loops=1)
  Buffers: shared hit=1204 read=30113
  ->  Sort  (cost=48211.10..48212.40 rows=520 width=16)
            (actual time=412.3..412.3 rows=20 loops=1)
        Sort Key: created_at DESC
        Sort Method: top-N heapsort  Memory: 26kB
        ->  Seq Scan on orders  (cost=0.00..48197.00 rows=520 width=16)
                                (actual time=0.9..411.8 rows=498 loops=1)
              Filter: ((customer_id = 42) AND (status = 'paid'::text))
              Rows Removed by Filter: 1999502
Planning Time: 0.2 ms
Execution Time: 412.4 ms
```

After `CREATE INDEX ON orders (customer_id, status, created_at DESC);`:

```text
Limit  (cost=0.56..22.10 rows=20 width=16) (actual time=0.03..0.09 rows=20 loops=1)
  Buffers: shared hit=24
  ->  Index Scan using orders_customer_id_status_created_at_idx on orders
        (cost=0.56..560.80 rows=520 width=16) (actual time=0.03..0.08 rows=20 loops=1)
        Index Cond: ((customer_id = 42) AND (status = 'paid'::text))
Planning Time: 0.3 ms
Execution Time: 0.1 ms
```

How to read it:

- The plan is a tree; read it **inside out** (deepest node runs first). Each node shows the
  planner's estimate (`cost=startup..total`, `rows`, `width`) and, with `ANALYZE`, the actual values
  (`actual time` in ms, per loop, `rows`, `loops`).
- **Estimated vs actual rows**: a large mismatch (estimated 10, actual 1,000,000) means bad
  statistics or correlated columns, and is the root cause of most bad plans. Run `ANALYZE`, raise the
  statistics target, or create extended statistics (`CREATE STATISTICS`).
- `Rows Removed by Filter` large compared to rows returned means a missing or unusable index.
- `Buffers: shared read` is disk (or OS cache) reads; `hit` is buffer pool hits.
- Scan types: `Seq Scan` (whole table), `Index Scan` (index then heap), `Index Only Scan`, `Bitmap
  Index Scan` plus `Bitmap Heap Scan` (collect matching row locations, then read pages in physical
  order; good for medium selectivity and combining indexes with `BitmapAnd` / `BitmapOr`).
- Join types: `Nested Loop` (good when the outer side is small and the inner side is indexed), `Hash
  Join` (build a hash table on the smaller input; good for large unsorted inputs), `Merge Join` (both
  inputs sorted on the join key).
- `Sort Method: external merge  Disk: ...` means the sort spilled to disk; raise `work_mem` for that
  query or add an index that provides the order.

MySQL `EXPLAIN` columns to know:

| Column | Watch for |
| --- | --- |
| `type` | From best to worst: `const`, `eq_ref`, `ref`, `range`, `index` (full index scan), `ALL` (full table scan) |
| `key` | Which index was chosen (NULL means none) |
| `rows` | Estimated rows examined |
| `Extra` | `Using index` (covering, good), `Using index condition` (pushdown), `Using filesort`, `Using temporary` (often bad) |

### When indexes are not used

| Cause | Example | Fix |
| --- | --- | --- |
| Function or arithmetic on the column | `WHERE date(created_at) = '2026-10-31'` | Rewrite as a range: `created_at >= '2026-10-31' AND created_at < '2026-11-01'`, or an expression index |
| Leading wildcard | `WHERE name LIKE '%son'` | Trigram index, full-text search, or reversed-string column |
| Implicit type conversion | `WHERE phone = 5551234` on a `varchar` column (MySQL casts every row) | Compare with the right type |
| Not the leftmost prefix | Index `(a, b)`, query `WHERE b = ?` | Add an index starting with `b` |
| Low selectivity | `WHERE is_active = true` matching 90% of rows | A seq scan is genuinely cheaper; consider a partial index on the rare value |
| `OR` across different columns | `WHERE a = ? OR b = ?` | Separate indexes (bitmap OR / index merge) or rewrite as `UNION ALL` |
| `!=`, `NOT IN`, `IS NOT NULL` on common values | Matches most rows | Reformulate or accept the scan |
| Stale statistics | Table grew 100x since last `ANALYZE` | Run `ANALYZE`; tune autovacuum / auto-analyze |
| Collation mismatch | `LIKE 'abc%'` in PostgreSQL with a non-C collation | Index with `text_pattern_ops` |
| Small table | 200 rows | Seq scan is faster; nothing to fix |
| Parameter-sensitive plans | Generic plan chosen for a skewed value | PG `plan_cache_mode`, or query-specific handling |

### Index costs and hygiene

- Each index adds write amplification and buffer pool pressure. Five indexes on a table can make
  inserts several times slower than with only a primary key.
- Remove unused indexes: PostgreSQL `pg_stat_user_indexes.idx_scan = 0`; MySQL
  `sys.schema_unused_indexes`.
- Remove redundant indexes: `(a)` is redundant when `(a, b)` exists (except for size or uniqueness).
- Build indexes on live tables with `CREATE INDEX CONCURRENTLY` (PostgreSQL) or online DDL
  (`ALGORITHM=INPLACE, LOCK=NONE` in MySQL) to avoid blocking writes.

---

## Query optimization and the N+1 problem

### The N+1 problem

An ORM loads a list (1 query), then lazily loads a relation for each element (N queries):

```java
@Entity
class Order {
    @Id Long id;

    @OneToMany(mappedBy = "order", fetch = FetchType.LAZY)
    List<OrderItem> items;
}

List<Order> orders = orderRepository.findByCustomerId(42L);  // 1 query
for (Order o : orders) {
    total += o.getItems().size();                            // N more queries
}
```

With 100 orders that is 101 round trips; at 1 ms each, 100 ms of pure latency, and it grows with
data. Detect it with SQL logging, Hibernate statistics, or tests that assert query counts.

Fixes:

```java
// 1. Fetch join: one query with a JOIN
@Query("select distinct o from Order o left join fetch o.items where o.customerId = :id")
List<Order> findWithItems(@Param("id") long customerId);

// 2. Entity graph: same idea, declarative
@EntityGraph(attributePaths = "items")
List<Order> findByCustomerId(long customerId);
```

```yaml
# 3. Batch fetching: load lazy collections for many parents with IN (...) queries
spring:
  jpa:
    properties:
      hibernate.default_batch_fetch_size: 50
```

Fetch-joining a collection while paginating makes Hibernate paginate in memory (it warns with
`HHH90003004` in recent versions, `HHH000104` in older ones); paginate parent IDs first, then
fetch children with `WHERE order_id IN (...)`. For read-heavy endpoints, a DTO projection with a
single hand-written query is often simplest.

### Pagination: OFFSET vs keyset

```sql
-- OFFSET: the database still reads and discards the first 100,000 rows
SELECT id, created_at, title FROM posts ORDER BY created_at DESC, id DESC
LIMIT 20 OFFSET 100000;

-- Keyset (seek) pagination: constant cost per page, uses the index directly
SELECT id, created_at, title FROM posts
WHERE (created_at, id) < (:last_created_at, :last_id)
ORDER BY created_at DESC, id DESC
LIMIT 20;
```

Keyset pagination needs an index on `(created_at, id)`, a unique tie-breaker column, and gives up
"jump to page 500". Row-value comparison `(a, b) < (x, y)` works in PostgreSQL and MySQL, though
MySQL may not always use the index for it; expand to `a < x OR (a = x AND b < y)` if needed.

### Other optimization habits

- Select only the columns you need (enables covering indexes, less network and memory).
- Batch writes (`INSERT ... VALUES (...), (...)`, JDBC batching with `reWriteBatchedInserts=true` for
  the PostgreSQL driver or `rewriteBatchedStatements=true` for MySQL Connector/J).
- Avoid chatty transactions; push set-based logic into SQL rather than loops.
- Use `EXISTS` instead of `COUNT(*) > 0`; avoid `SELECT DISTINCT` hiding a bad join.
- Watch for slow queries continuously: PostgreSQL `pg_stat_statements`, MySQL slow query log and
  Performance Schema.

---

## Connection pooling

Opening a database connection costs a TCP handshake, TLS, authentication, and backend setup
(PostgreSQL forks a process per connection, using several MB each). A pool keeps connections open
and lends them out per transaction.

### Sizing: smaller than you think

A database can only execute as many queries in parallel as it has CPU cores (plus some overlap for
I/O waits). Beyond that, extra connections add context switching, lock contention and memory, and
throughput **drops**. The PostgreSQL wiki formula, popularized by HikariCP's "About Pool Sizing"
page, is a starting point:

```text
pool size ≈ (core_count * 2) + effective_spindle_count

8-core DB server on SSD  ≈ 8 * 2 + 1 = 17 connections, total, across all clients
```

Little's law gives the demand side: busy connections = throughput times time per query.

```text
L = λ * W
2,000 queries/s * 0.005 s per query = 10 connections busy on average
```

So a pool of 10–20 can serve thousands of queries per second if queries are short. The real
constraint is usually the **sum** over all app instances: 40 pods with `maximumPoolSize = 50` is 2,000
connections against a database that is healthy at a few hundred. Solutions: smaller per-pod pools,
or a server-side pooler.

### HikariCP configuration

```yaml
spring:
  datasource:
    url: jdbc:postgresql://db:5432/app
    hikari:
      maximum-pool-size: 10          # per instance; multiply by instance count
      minimum-idle: 10               # Hikari recommends a fixed-size pool
      connection-timeout: 3000       # ms to wait for a free connection (default 30000)
      max-lifetime: 1800000          # ms; keep below DB / proxy / firewall idle limits
      idle-timeout: 600000           # ms; only applies when minimum-idle < maximum-pool-size
      leak-detection-threshold: 20000
```

- A short `connection-timeout` makes pool exhaustion fail fast instead of piling up request threads.
- Pool exhaustion is usually a symptom: slow queries, long transactions, or holding a connection
  while calling another service. Fix the cause, not the pool size.
- Watch `hikaricp_connections_active`, `_pending`, and `_acquire` time (Micrometer metrics).

### Server-side poolers

**PgBouncer** (or RDS Proxy, pgcat, Odyssey; ProxySQL for MySQL) multiplexes many client connections
over few server connections.

| PgBouncer mode | Server connection returned | Caveat |
| --- | --- | --- |
| Session | When the client disconnects | Little gain over no pooler |
| Transaction | At the end of each transaction | Session state (`SET`, advisory locks, `LISTEN`, temp tables) does not carry over; protocol-level prepared statements need PgBouncer 1.21+ with `max_prepared_statements` |
| Statement | After each statement | Multi-statement transactions not allowed |

Typical setup: each app pod has a small Hikari pool to PgBouncer, which keeps 50–100 real
connections to PostgreSQL.

---

## Normalization and denormalization

### Normal forms

Normalization removes redundancy so that each fact is stored once and update anomalies cannot
happen.

Start with a denormalized orders sheet:

| order_id | customer_id | customer_name | customer_city | product_id | product_name | qty |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 7 | Ada | London | 100 | Kettle | 1 |
| 1 | 7 | Ada | London | 101 | Mug | 4 |
| 2 | 8 | Bob | Pune | 100 | Kettle | 2 |

- **1NF**: atomic values, no repeating groups (no `products = "Kettle, Mug"` column), each row
  identifiable by a key. Key here: `(order_id, product_id)`.
- **2NF**: 1NF and no non-key column depends on only **part** of a composite key. `product_name`
  depends only on `product_id`; `customer_id` depends only on `order_id`. Split into `orders`,
  `order_items`, `products`.
- **3NF**: 2NF and no non-key column depends on another non-key column (no **transitive**
  dependency). `customer_name` depends on `customer_id`, not on `order_id`. Move it to `customers`.
- **BCNF**: every determinant is a candidate key. Differs from 3NF only in unusual cases with
  overlapping candidate keys.
- **4NF / 5NF** deal with multi-valued and join dependencies; rarely discussed outside academia.

```sql
CREATE TABLE customers   (id bigint PRIMARY KEY, name text NOT NULL, city text);
CREATE TABLE products    (id bigint PRIMARY KEY, name text NOT NULL, price_cents int NOT NULL);
CREATE TABLE orders      (id bigint PRIMARY KEY,
                          customer_id bigint NOT NULL REFERENCES customers(id),
                          created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE order_items (order_id bigint REFERENCES orders(id),
                          product_id bigint REFERENCES products(id),
                          qty int NOT NULL CHECK (qty > 0),
                          unit_price_cents int NOT NULL,
                          PRIMARY KEY (order_id, product_id));
```

Note `unit_price_cents` in `order_items`: it looks like duplication of `products.price_cents`, but
it is a **different fact** (the price at the time of the order). Historical facts belong on the
event, not looked up from current state.

Anomalies prevented by normalization:

- **Update anomaly**: Ada moves city; with the sheet you must update many rows and may miss some.
- **Insert anomaly**: cannot record a new product until someone orders it.
- **Delete anomaly**: deleting the only order for Bob deletes the fact that Bob exists.

### Denormalization

Denormalization deliberately stores redundant or precomputed data to make reads cheaper:

- **Duplicated columns**: `author_name` on each post to avoid a join.
- **Counters**: `posts.comment_count` instead of `COUNT(*)` on every page view.
- **Materialized views** / summary tables for dashboards.
- **Embedded documents** in document stores (an order with its line items).
- **Read models** built from events (CQRS).

Costs: every write must update every copy (or accept staleness), the copies can drift, and storage
grows. Keep the normalized data as the source of truth and maintain denormalized copies in the same
transaction (triggers, or the application) or asynchronously (CDC, events), and have a way to
rebuild them.

Rule: **normalize for writes and correctness, denormalize measured read hot paths.**

---

## SQL vs NoSQL

### Deep comparison

| Dimension | Relational (PostgreSQL, MySQL) | NoSQL (varies by family) |
| --- | --- | --- |
| Data model | Tables, rows, typed columns, relations | Key-value, documents, wide rows, graphs |
| Schema | Enforced on write; migrations | Usually schema-on-read; the application enforces structure |
| Query flexibility | Ad hoc SQL, joins, aggregates; new questions need no remodel | Optimized for known access patterns; new patterns often mean new tables or indexes |
| Transactions | Multi-row, multi-table ACID | Often single-key or single-partition; some offer multi-item (DynamoDB `TransactWriteItems`, MongoDB 4.0+) |
| Consistency | Strong on a single primary | Often tunable or eventual; some strong per key |
| Scaling writes | Vertical, then manual sharding or distributed SQL | Horizontal partitioning built in |
| Operational model | Mature tooling, well understood | Varies; managed services hide much |
| Typical failure | Single-node limits, sharding pain | Data modelling mistakes, hot partitions, consistency surprises |

"NoSQL scales, SQL does not" is a misconception. A single modern PostgreSQL or MySQL server handles
tens of thousands of transactions per second and multiple terabytes, and sharded MySQL runs some of
the largest sites in the world. The real difference is **where the work of scaling is done**: NoSQL
systems force you to model around partitions up front; relational systems let you defer that
decision at the cost of harder sharding later.

### Key-value stores

Examples: Redis, DynamoDB (also document-like), Riak, etcd (for coordination), RocksDB (embedded).

Model: `get(key)`, `put(key, value)`, sometimes range scans over sorted keys. Everything is designed
around the **key**.

DynamoDB-style **single-table design** uses a partition key (PK) and sort key (SK) to place related
items together, so one query returns a whole aggregate:

| PK | SK | Attributes |
| --- | --- | --- |
| `CUSTOMER#7` | `PROFILE` | name, email |
| `CUSTOMER#7` | `ORDER#2026-10-30#1001` | total, status |
| `CUSTOMER#7` | `ORDER#2026-10-31#1002` | total, status |
| `ORDER#1002` | `ITEM#100` | qty, price |

"Latest orders for customer 7" is a `Query` on `PK = CUSTOMER#7 AND begins_with(SK, 'ORDER#')` in
descending order. Other access paths need a **global secondary index** (eventually consistent,
separately provisioned). Design starts from a list of access patterns, not from entities.

### Document stores

Examples: MongoDB, Couchbase, Firestore, PostgreSQL `jsonb`.

Model: JSON-like documents with nesting, grouped in collections, with secondary indexes on any path.

```json
{
  "_id": "order_1002",
  "customerId": 7,
  "status": "paid",
  "createdAt": "2026-10-31T09:12:00Z",
  "shipping": { "city": "London", "postcode": "N1 9GU" },
  "items": [
    { "productId": 100, "name": "Kettle", "qty": 1, "priceCents": 3999 },
    { "productId": 101, "name": "Mug", "qty": 4, "priceCents": 899 }
  ],
  "totalCents": 7595
}
```

**Embed vs reference**:

| Embed when | Reference when |
| --- | --- |
| Data is read together (order and its items) | Data is shared across many parents (products) |
| Child has no life outside the parent | Child is large or grows without bound (comments on a viral post) |
| Bounded size (MongoDB documents max 16 MB) | Child is updated independently and often |

Pitfalls: unbounded arrays, duplicated data drifting, and multi-document invariants requiring
transactions (available, but slower, and a hint the data may be relational).

### Wide-column stores

Examples: Apache Cassandra, ScyllaDB, HBase, Google Bigtable.

Model: tables whose primary key has a **partition key** (which node stores the data) and
**clustering columns** (sort order within the partition). A partition is a sorted mini-table that
lives together on the same replicas. Queries must specify the partition key; there are no joins.
You model **one table per query**, duplicating data across tables.

```sql
-- CQL (Cassandra Query Language)
CREATE TABLE messages_by_conversation (
    conversation_id uuid,
    month           text,          -- bucket, e.g. '2026-10', to bound partition size
    sent_at         timeuuid,
    sender_id       uuid,
    body            text,
    PRIMARY KEY ((conversation_id, month), sent_at)
) WITH CLUSTERING ORDER BY (sent_at DESC);

-- Latest 50 messages of a conversation this month: one partition, sequential read
SELECT sender_id, body, sent_at
FROM messages_by_conversation
WHERE conversation_id = 5b6962dd-3f90-4c93-8f61-eabfa4a803e2 AND month = '2026-10'
LIMIT 50;
```

- `(conversation_id, month)` is the composite **partition key**; `sent_at` is the **clustering key**.
- Time **bucketing** keeps partitions bounded; a common guideline is to keep partitions well under
  about 100 MB and around 100,000 rows. Huge partitions cause slow reads, compaction and repair pain,
  and hot nodes.
- Writes are cheap (LSM), reads by partition are fast, and anything else (filter by `sender_id`)
  needs another table such as `messages_by_sender`. `ALLOW FILTERING` is a smell.
- Deletes create tombstones; many tombstones in a partition slow reads dramatically (queue-like
  workloads are a classic Cassandra anti-pattern).
- Consistency is tunable per query (`ONE`, `QUORUM`, `LOCAL_QUORUM`, `ALL`); see leaderless
  replication below.

### Graph databases

Examples: Neo4j, Amazon Neptune, JanusGraph, Memgraph.

Model: nodes and relationships with properties. Traversals follow direct pointers ("index-free
adjacency"), so the cost depends on the neighbourhood size, not the total graph size. Multi-hop
queries that need many self-joins in SQL become short and fast.

```cypher
// Cypher: friends-of-friends that user 42 does not follow yet, ranked by mutual friends
MATCH (u:User {id: 42})-[:FOLLOWS]->(:User)-[:FOLLOWS]->(fof:User)
WHERE fof <> u AND NOT (u)-[:FOLLOWS]->(fof)
RETURN fof.id, count(*) AS mutual
ORDER BY mutual DESC
LIMIT 10;
```

Use graphs for recommendations, fraud rings, access-control graphs, knowledge graphs, and network
topology. They are rarely the system of record for everything; they are usually fed from a primary
store.

### Other specialized stores

- **Search engines** (Elasticsearch, OpenSearch): inverted indexes, relevance, aggregations.
- **Time-series** (TimescaleDB, InfluxDB, Prometheus): time-partitioned, compressed, retention policies.
- **Columnar warehouses** (BigQuery, Snowflake, ClickHouse, Redshift): analytics over billions of
  rows by scanning compressed columns.
- **Object storage** (S3, GCS): blobs, backups, data lakes; cheap and practically unlimited.
- **Vector databases / extensions** (pgvector, dedicated vector stores): nearest-neighbour search on
  embeddings.

---

## Replication

Replication keeps copies of the same data on several machines to tolerate failures, scale reads,
and place data near users.

### Replication log formats

| Method | How | Trade-offs |
| --- | --- | --- |
| Statement-based | Ship SQL statements | Non-deterministic functions (`NOW()`, `RAND()`, auto-increment races) diverge; mostly abandoned |
| Physical / WAL shipping | Ship the storage engine's log (PostgreSQL streaming replication) | Exact byte-level copy; same major version required; whole cluster only |
| Logical / row-based | Ship row changes (MySQL `binlog_format=ROW`, PostgreSQL logical replication) | Version-independent, can filter tables, feeds CDC (Debezium) |

### Synchronous, asynchronous, semi-synchronous

```text
 client      leader                     follower
   │  write    │                            │
   │──────────►│  write WAL                 │
   │           │──────── replicate ────────►│
   │           │                            │ apply / persist
   │           │◄─────────── ack ───────────│
   │  ok       │                            │
   │◄──────────│   (synchronous: ok sent only after the ack)
   │           │   (asynchronous: ok sent right after the local WAL write)
```

| Mode | Commit returns when | Data loss on leader failure | Latency / availability |
| --- | --- | --- | --- |
| Asynchronous | Leader has it durably | Recent commits can be lost | Fastest; follower outage does not affect writes |
| Synchronous | Leader and all named sync followers have it | None for those followers | Slowest; a slow or dead sync follower stalls writes |
| Semi-synchronous | Leader plus at least one follower acknowledged receipt | Very small window | Moderate |

- PostgreSQL: `synchronous_standby_names = 'ANY 1 (replica_a, replica_b)'` (quorum commit: any one of
  them), and `synchronous_commit` levels `remote_write`, `on`, `remote_apply` (the last means the
  replica has applied it, so reads there will see it).
- MySQL semi-sync with `rpl_semi_sync_source_wait_point = AFTER_SYNC` ("lossless" semi-sync): the
  source waits for a replica to receive the transaction before committing in the engine, so no
  client sees data a replica does not have.
- Practical setup: one synchronous (or semi-sync) replica in another availability zone for
  durability, more asynchronous replicas for reads.

### Leader-follower (single leader)

```text
                 writes
 app ─────────────────────────► ┌──────────┐
                                │  Leader  │
                                └────┬─────┘
                       replication   │   log
            ┌────────────────────────┼────────────────────────┐
            ▼                        ▼                        ▼
      ┌──────────┐             ┌──────────┐             ┌──────────┐
      │ Follower │             │ Follower │             │ Follower │
      └──────────┘             └──────────┘             └──────────┘
            ▲                        ▲                        ▲
            └────────────── reads (possibly stale) ───────────┘
```

Simple, no write conflicts, the default for PostgreSQL, MySQL, MongoDB replica sets, and Redis.
Write throughput is limited to one node; reads scale with followers but are **eventually
consistent**.

### Replication lag and session guarantees

Asynchronous followers lag behind by milliseconds normally, and by seconds or minutes under heavy
writes, long-running queries on the replica, or network issues. Monitor it:

```sql
-- PostgreSQL, on the primary
SELECT application_name, state, write_lag, flush_lag, replay_lag FROM pg_stat_replication;

-- PostgreSQL, on a replica
SELECT now() - pg_last_xact_replay_timestamp() AS approx_lag;

-- MySQL 8.0.22+
SHOW REPLICA STATUS;   -- Seconds_Behind_Source, and GTID sets
```

Anomalies and the guarantees that fix them:

| Problem | Guarantee | Implementations |
| --- | --- | --- |
| User updates profile, refreshes, sees the old one | **Read-your-writes** | Read from leader for N seconds after the user's write; or track the write's LSN/GTID and only read from replicas that have applied it |
| User refreshes twice and sees newer then older data (different replicas) | **Monotonic reads** | Pin each user to one replica (hash of user ID); or track the highest position seen and require replicas at least that far |
| Reader sees an answer before the question | **Consistent prefix reads** | Keep causally related writes in the same partition; causal tokens |

Routing reads with Spring's `AbstractRoutingDataSource`:

```java
public final class ReplicaRoutingDataSource extends AbstractRoutingDataSource {
    public enum Target { PRIMARY, REPLICA }

    private static final ThreadLocal<Target> CURRENT = ThreadLocal.withInitial(() -> Target.REPLICA);

    public static void use(Target target) {
        CURRENT.set(target);
    }

    public static void clear() {
        CURRENT.remove();
    }

    @Override
    protected Object determineCurrentLookupKey() {
        return CURRENT.get();
    }
}
```

```java
public final class ReadYourWritesPolicy {
    private static final Duration STICKY = Duration.ofSeconds(5);
    private final Cache<Long, Instant> lastWriteByUser = Caffeine.newBuilder()
            .expireAfterWrite(STICKY)
            .maximumSize(1_000_000)
            .build();

    public void recordWrite(long userId) {
        lastWriteByUser.put(userId, Instant.now());
    }

    public ReplicaRoutingDataSource.Target targetFor(long userId) {
        return lastWriteByUser.getIfPresent(userId) != null
                ? ReplicaRoutingDataSource.Target.PRIMARY
                : ReplicaRoutingDataSource.Target.REPLICA;
    }
}
```

This in-memory version only works if the user's requests hit the same instance; with many
instances, keep the "last write" timestamp in a cookie or session, or in Redis. The precise version
stores the commit position (PostgreSQL `pg_current_wal_lsn()` after the write) and checks
`pg_last_wal_replay_lsn()` on the replica; MySQL offers `WAIT_FOR_EXECUTED_GTID_SET(gtid_set,
timeout)` to wait until a replica catches up.

### Failover and split brain

When the leader dies, a follower must be promoted:

1. **Detect** failure (missed heartbeats over a timeout). Too short a timeout causes needless
   failovers under load; too long extends outages.
2. **Choose** the most up-to-date follower (to minimize lost writes).
3. **Reconfigure** clients and other followers to use the new leader.
4. **Fence** the old leader so it cannot accept writes if it comes back.

What goes wrong:

- **Lost writes**: with async replication, commits the old leader acknowledged but did not ship are
  gone. If the old leader rejoins, its divergent writes must be discarded. If other systems (caches,
  other databases) already saw them, you get inconsistency; GitHub's 2012 incident with reused
  auto-increment IDs leaking data between Redis and MySQL is a famous example from DDIA.
- **Split brain**: a network partition makes two nodes believe they are leader, both accept writes,
  and data diverges. Prevent with:
  - a **consensus-based** coordinator (etcd, ZooKeeper, Consul) holding a leader lease; only the
    lease holder may write (Patroni for PostgreSQL works this way);
  - **quorum**: a node may only act as leader with a majority of votes, so at most one side of a
    partition can have a leader;
  - **fencing**: STONITH ("shoot the other node in the head": power off or cut network), or
    **fencing tokens** (a monotonically increasing epoch that storage checks and rejects older ones).
- **Cross-region failover** can make latency and data loss worse than the outage. GitHub's October
  2018 incident started with a 43-second network partition that triggered a cross-coast MySQL
  failover, leaving writes on both coasts and about 24 hours of degraded service while they were
  reconciled.

Tools: Patroni (PostgreSQL), Orchestrator (MySQL), MySQL Group Replication / InnoDB Cluster (Paxos
based), managed services (RDS Multi-AZ, Cloud SQL HA, Aurora) which handle most of this for you.

### Multi-leader replication

Several nodes accept writes and replicate to each other. Used for multi-region writes (each region
has a leader), offline-first clients (each device is a "leader"), and collaborative editing.

```text
   region EU                         region US
 ┌────────────┐    async, both    ┌────────────┐
 │  Leader A  │ ◄───────────────► │  Leader B  │
 └─────┬──────┘     directions    └─────┬──────┘
       ▼                                ▼
   followers                        followers
```

The core problem is **write conflicts**: the same record changed in two places concurrently.

| Strategy | How | Trade-off |
| --- | --- | --- |
| Avoid conflicts | Route all writes for a record to its "home" region | Simplest; fails when the home region is down |
| Last write wins (LWW) | Highest timestamp wins | Silently loses data; clock skew picks the "wrong" winner |
| Higher replica ID wins | Deterministic tie-break | Also loses data |
| Merge values | Union sets, concatenate, application-specific merge | Needs domain logic |
| Keep siblings | Store both versions, let the reader or user resolve (Riak, CouchDB) | Pushes complexity to the app |
| CRDTs | Data types whose merge is commutative, associative, idempotent (counters, OR-sets, sequence CRDTs) | Limited data types, metadata overhead |
| Operational transformation | Transform concurrent edits (Google Docs style) | Complex, specialized |

Multi-leader also breaks things that are easy on one node: auto-increment keys (use UUIDs or
interleaved ranges), uniqueness constraints, and foreign keys across regions.

### Leaderless replication (Dynamo style)

Clients (or a coordinator node) send each write to all `N` replicas and wait for `W`
acknowledgements; reads query replicas and wait for `R` responses, taking the newest version.
Amazon's Dynamo paper (2007) introduced the design; Cassandra, ScyllaDB and Riak follow it.

```text
 N = 3, W = 2, R = 2          write v2                       read
                    ┌──────► replica 1: v2  ✓        replica 1: v2 ◄──┐
 client ────────────┼──────► replica 2: v2  ✓        replica 2: v2    ├── client
                    └──────► replica 3: v1  (down)   replica 3: v1 ◄──┘
                                                     newest wins: v2
```

- **Quorum condition `W + R > N`**: every read set overlaps every write set in at least one replica,
  so a read sees the latest acknowledged write (in the absence of edge cases).
- Typical: `N = 3, W = 2, R = 2`. Write-heavy: `W = 1, R = 3`. Read-heavy: `W = 3, R = 1`.
- **Read repair**: when a read sees a stale replica, it writes the newest value back.
- **Anti-entropy**: background comparison of replicas using **Merkle trees** (Cassandra `nodetool
  repair`) to fix data no one has read.
- **Hinted handoff / sloppy quorum**: if a home replica is down, another node accepts the write with
  a hint and forwards it later. This keeps writes available but breaks the overlap guarantee.
- **Conflict detection**: concurrent writes are detected with version vectors (Riak) or resolved
  with LWW timestamps (Cassandra, per cell).

Quorums are not linearizable on their own: with sloppy quorums, LWW clock skew, a write that
succeeded on fewer than `W` replicas (reported as failed but not rolled back), or concurrent reads
during a write, a reader can see new then old values. Cassandra offers linearizable
"lightweight transactions" (`IF NOT EXISTS`, `IF col = ?`) using Paxos for cases that need it.

---

## Partitioning (sharding)

Partitioning splits a dataset so each node holds a subset. It scales **writes and storage** beyond
one machine (replication only scales reads). Usually combined with replication: each partition has
its own leader and followers.

### Range partitioning

Each partition owns a contiguous key range: `[A, F)`, `[F, M)`, ...

- Efficient range scans (all keys in a range live together).
- Ranges can split automatically when they grow (HBase regions, Bigtable tablets, MongoDB chunks,
  CockroachDB ranges, TiKV regions).
- **Hot spots** with monotonic keys: if the key is a timestamp, every write goes to the last
  partition. Fix by prefixing with something else (`sensor_id, timestamp`) at the cost of range scans
  across sensors.

### Hash partitioning

`partition = hash(key) mod P`. Spreads keys evenly regardless of their distribution, but destroys
ordering, so range queries must hit every partition. Use a stable hash (MurmurHash, xxHash, MD5),
never a language's in-process `hashCode`, which can vary between runs and versions.

Naive `hash mod N` (where N is the number of **nodes**) is a trap: going from N to N+1 nodes moves
about `N / (N + 1)` of all keys (90% when going from 9 to 10 nodes).

### Consistent hashing

Place both nodes and keys on a ring of hash values; each key belongs to the first node clockwise.
Adding or removing a node only moves the keys between it and its predecessor, about `1/N` of keys.

```text
                    0           ring of hash values, walk clockwise ──►
           C  ●─────●─────●  k1
              │           │                 k1 ──► A
         k3   ●           ●  A              k2 ──► B   (passes k4, then B)
              │           │                 k4 ──► B
           B  ●─────●─────●  k2             k3 ──► C
                   k4
```

**Virtual nodes**: each physical node owns many points on the ring (for example 256 tokens per node
in older Cassandra defaults; Cassandra 4.0 defaults to 16 with a smarter allocation algorithm).
Benefits:

- Even load: with one point per node, arc sizes vary wildly; with many, they average out.
- When a node fails, its load spreads across **all** remaining nodes rather than one neighbour.
- Heterogeneous hardware: give bigger machines more virtual nodes.

```java
public final class ConsistentHashRing<N> {
    private final NavigableMap<Long, N> ring = new TreeMap<>();
    private final int virtualNodes;

    public ConsistentHashRing(Collection<N> nodes, int virtualNodes) {
        this.virtualNodes = virtualNodes;
        nodes.forEach(this::add);
    }

    public void add(N node) {
        for (int i = 0; i < virtualNodes; i++) {
            ring.put(hash(node + "#" + i), node);
        }
    }

    public void remove(N node) {
        for (int i = 0; i < virtualNodes; i++) {
            ring.remove(hash(node + "#" + i));
        }
    }

    public N nodeFor(String key) {
        if (ring.isEmpty()) throw new IllegalStateException("empty ring");
        Map.Entry<Long, N> e = ring.ceilingEntry(hash(key));
        return (e != null ? e : ring.firstEntry()).getValue();
    }

    private static long hash(String s) {
        try {
            byte[] d = MessageDigest.getInstance("MD5").digest(s.getBytes(StandardCharsets.UTF_8));
            return ByteBuffer.wrap(d).getLong();
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}
```

Alternatives: **rendezvous (highest random weight) hashing** (score every node per key, pick the
highest; simple, minimal movement, O(N) per lookup) and **jump consistent hash** (Lamping and Veach,
2014; tiny and fast, but nodes can only be added or removed at the end).

### Fixed number of partitions

Create many more partitions than nodes up front (for example 1,024 partitions on 10 nodes) and
assign whole partitions to nodes. Adding a node steals some partitions from each existing node; keys
never change partition, only partitions move. Used by Elasticsearch (shard count fixed at index
creation), Couchbase (1,024 vBuckets), Redis Cluster (16,384 slots), and Riak. Choosing the count is
the hard part: too few limits growth, too many adds overhead.

### Directory-based partitioning

A lookup service maps each key (or tenant) to a partition. Maximum flexibility: move a big tenant to
its own shard, place EU tenants in the EU. Costs: the directory is a critical dependency (cache it
aggressively, replicate it), and it is extra state to keep consistent. Vitess's lookup vindexes and
many multi-tenant SaaS systems use this.

| Strategy | Range queries | Even distribution | Rebalancing | Example systems |
| --- | --- | --- | --- | --- |
| Range | Efficient | Risk of hot spots | Split / merge ranges | HBase, Bigtable, CockroachDB, MongoDB ranged |
| Hash mod N | Scatter-gather | Good | Moves almost everything | Naive app sharding (avoid) |
| Consistent hashing | Scatter-gather | Good with virtual nodes | Moves ~1/N | Cassandra, DynamoDB, Memcached clients |
| Fixed partitions | Scatter-gather | Good | Move whole partitions | Elasticsearch, Couchbase, Redis Cluster |
| Directory | Depends | Controllable | Flexible, per key | Vitess lookup, multi-tenant SaaS |

### Secondary indexes in a partitioned database

```text
 Local (document-partitioned)              Global (term-partitioned)
 ┌───────────────┐ ┌───────────────┐       ┌───────────────┐ ┌───────────────┐
 │ partition 0   │ │ partition 1   │       │ index part A-M│ │ index part N-Z│
 │ rows 0-499    │ │ rows 500-999  │       │ color=black:  │ │ color=red:    │
 │ idx: red->3,9 │ │ idx: red->512 │       │  [3, 701]     │ │  [3, 9, 512]  │
 └───────────────┘ └───────────────┘       └───────────────┘ └───────────────┘
 query color=red: ask EVERY partition      query color=red: ask ONE index partition
```

- **Local index**: each partition indexes only its own rows. Writes are cheap and synchronous, but a
  query on the indexed column must **scatter-gather** to all partitions; tail latency is the slowest
  partition. Used by MongoDB, Cassandra secondary indexes, Elasticsearch.
- **Global index**: the index itself is partitioned by the indexed value. Reads go to one place, but
  a single row write may update index partitions on other nodes, so global indexes are usually
  **updated asynchronously** (DynamoDB global secondary indexes are eventually consistent) or need a
  distributed transaction (Spanner, CockroachDB).

### Rebalancing

Requirements: load is fair afterwards, the database keeps serving during the move, and no more data
moves than necessary.

- Moving data is expensive (network, disk, cache coldness). Throttle it.
- **Automatic vs manual**: fully automatic rebalancing combined with automatic failure detection can
  cascade (an overloaded node is declared dead, its load moves to others, which become overloaded).
  Many operators prefer the system to propose moves and a human (or a slow controller) approve them.
- **Dynamic splitting**: range-partitioned stores split partitions that exceed a size threshold
  (CockroachDB's default range size is 512 MiB) and can also split on load.

### Hot spots and celebrity keys

Even with perfect hashing, one key can be hot (a celebrity's posts, a viral product, a huge tenant).

- **Split the key**: append a random suffix `0..9` to the hot key for writes (`post:123#7`), and read
  all 10 sub-keys and combine. Only worth it for the few known-hot keys; track them separately.
- **Cache** hot reads in front of the database.
- **Isolate** big tenants onto dedicated shards (directory-based).
- **Write aggregation**: buffer and batch increments in memory or a stream processor.

### Choosing a shard key

Criteria:

1. **High cardinality** (many distinct values) so data can spread across many shards.
2. **Even access** (no single value dominating traffic).
3. **Query locality**: the most common and most important queries include the shard key, so they hit
   one shard. Related data that is read or written together (and needs transactions) shares a key.
4. **Stability**: the key value of a row never changes (changing it means moving the row).

| System | Good shard key | Why | Bad choice |
| --- | --- | --- | --- |
| Multi-tenant SaaS | `tenant_id` | All tenant queries and transactions on one shard | `created_at` (hot last shard) |
| Chat | `conversation_id` | Messages of a conversation read together, ordered by time | `sender_id` (a conversation spreads across shards) |
| E-commerce orders | `customer_id` | "My orders" is the hot query; orders and items co-located | `order_id` hashed (customer history becomes scatter-gather); `country` (low cardinality, skewed) |
| IoT time series | `(device_id, day)` | Spreads devices, bounds partition size, time scans per device | `timestamp` alone (all writes on one shard) |
| Social feed | `user_id` for the feed table | Each user's feed is one partition | `post_id` for feed reads |
| URL shortener | hash of `short_code` | Point lookups only, uniform | Sequential `id` range partitioning |

Multi-tenant caveat: one enormous tenant breaks `tenant_id` sharding. Plan an escape hatch: a
directory that can move that tenant to its own shard, or a compound key `(tenant_id, entity_id)`
hashed for large tenants.

### What sharding costs you

- **Cross-shard queries** become scatter-gather; joins across shards must happen in the application
  or a query router.
- **Cross-shard transactions** need two-phase commit or sagas, or redesign so they are rare.
- **Unique constraints and auto-increment** only work per shard; use globally unique IDs (UUIDv7,
  Snowflake-style IDs).
- **Operations** multiply: schema migrations, backups, monitoring, failover per shard.
- **Resharding** is a project. Vitess and distributed SQL databases exist largely to automate it.

Shard as late as you reasonably can: first tune queries and indexes, add caching, add read replicas,
scale vertically, and archive cold data. Then shard, or adopt a database that shards for you.

---

## Distributed SQL (NewSQL)

Distributed SQL databases offer SQL, ACID transactions, and secondary indexes on top of automatic
range partitioning, with each range replicated by a consensus protocol.

- **Google Spanner**: data split into splits replicated by Paxos groups; transactions use two-phase
  locking plus two-phase commit across groups. **TrueTime** exposes clock uncertainty as an interval
  `[earliest, latest]` (GPS and atomic clocks keep it to a few milliseconds); a commit waits out the
  uncertainty ("commit wait") so that timestamp order matches real-time order, giving **external
  consistency** (strict serializability). Paper: Corbett et al., OSDI 2012.
- **CockroachDB**: PostgreSQL wire protocol; data in ranges (default 512 MiB) each replicated with
  **Raft**; serializable isolation by default (read committed added later as an option); uses
  **hybrid logical clocks** without special hardware, so it handles clock uncertainty with
  uncertainty intervals and occasional transaction restarts, and requires bounded clock skew
  (`--max-offset`, default 500 ms).
- **YugabyteDB** (PostgreSQL-compatible, Raft) and **TiDB** (MySQL-compatible; TiKV storage on
  RocksDB with Raft; Percolator-style transactions) are in the same family.
- **Vitess**: not a new database but a sharding layer for MySQL. **VTGate** routes queries using
  **vindexes** (sharding functions such as hash or lookup tables), **VTTablet** sidecars manage each
  MySQL instance, and a topology service (etcd, ZooKeeper, Consul) stores the shard map. It supports
  online resharding and was built at YouTube; Slack and GitHub run large deployments. Cross-shard
  transactions are limited (best effort or two-phase commit with caveats), so you still design a good
  sharding key.

Trade-offs: every write needs a consensus round trip (a few ms in one region, tens of ms across
regions), cross-range transactions are slower than on one node, and operational and licensing
models vary. They shine when you need horizontal write scaling **and** relational transactions,
especially multi-region with data locality (pin rows to regions).

---

## CQRS and read replicas

**Command Query Responsibility Segregation** separates the model that handles writes (commands)
from the models that serve reads (queries).

```text
              commands                                  queries
 client ──────────────────┐                ┌──────────────────────────────► client
                          ▼                │
               ┌──────────────────┐   ┌────┴──────────────────────┐
               │ Write model      │   │ Read models               │
               │ normalized SQL,  │   │ denormalized views,       │
               │ invariants, ACID │   │ search index, cache,      │
               └────────┬─────────┘   │ per-screen documents      │
                        │             └─────────────▲─────────────┘
                        │ events / CDC (async)      │
                        └───────────────────────────┘
```

Levels of CQRS, from cheap to heavy:

1. **Read replicas**: same schema, reads routed to followers. Easiest; inherits replication lag.
2. **Separate read tables or materialized views** in the same database, refreshed by triggers or
   jobs (`REFRESH MATERIALIZED VIEW CONCURRENTLY`).
3. **Separate read stores** fed by events or CDC: Elasticsearch for search, Redis for counters, a
   denormalized document per screen.
4. **Event sourcing**: the write model stores events as the source of truth; all state is a
   projection. Powerful audit and replay, high complexity.

Use it when read and write shapes or scales differ greatly (search, feeds, dashboards). Costs:
eventual consistency between models (design the UI for it), more moving parts, and the need to
rebuild projections from scratch when they change. Use the **transactional outbox** pattern (write
the event to an outbox table in the same transaction as the state change, then relay it) to avoid
dual-write inconsistency between the database and the message broker.

---

## Backups and point-in-time recovery

Replication is **not** a backup: a `DROP TABLE` or a bad migration replicates instantly. Backups
protect against human error, bugs, ransomware, and region loss.

Two numbers drive the design:

- **RPO** (Recovery Point Objective): how much data you may lose, for example 5 minutes.
- **RTO** (Recovery Time Objective): how long recovery may take, for example 1 hour.

| Type | Tools | Pros | Cons |
| --- | --- | --- | --- |
| Logical | `pg_dump`, `mysqldump`, `mydumper` | Portable across versions, per-table restore | Slow for large databases, point-in-time only |
| Physical | `pg_basebackup`, pgBackRest, WAL-G, Barman; Percona XtraBackup, MySQL Enterprise Backup | Fast for big data, base for PITR | Same major version and platform |
| Snapshots | EBS / disk snapshots, managed service snapshots | Fast, cheap incremental | Need crash-consistent or coordinated snapshots |

### Point-in-time recovery

PITR = a physical base backup + every WAL (or binlog) segment since. To restore to 14:31:59, restore
the base backup and replay the log up to that moment.

```text
 base backup (Sunday 02:00)
     │
     ▼
 ────●──────────── WAL segments archived continuously ───────────────► now
                                              ▲         ▲
                                   restore to 14:31:59  14:32 bad DELETE
```

PostgreSQL:

```ini
# postgresql.conf on the primary
wal_level = replica
archive_mode = on
archive_command = 'pgbackrest --stanza=main archive-push %p'
```

```ini
# On the restored server (PostgreSQL 12+): settings in postgresql.conf plus an empty recovery.signal
restore_command = 'pgbackrest --stanza=main archive-get %f "%p"'
recovery_target_time = '2026-10-31 14:31:59+05:30'
recovery_target_action = 'promote'
```

MySQL: restore an XtraBackup, then replay binary logs with `mysqlbinlog --stop-datetime="2026-10-31
14:31:59" binlog.000123 | mysql`. Managed services (RDS, Cloud SQL, Aurora) automate both parts and
let you restore to any second within the retention window.

Practices:

- **Test restores regularly** (automated, with a timer). An untested backup is a hope, not a backup.
- 3-2-1: three copies, two media types, one off-site (another region or account), with immutability
  (object lock) against ransomware.
- Encrypt backups and control access as tightly as production.
- A **delayed replica** (PostgreSQL `recovery_min_apply_delay`, MySQL `SOURCE_DELAY`) gives a fast way
  to recover from a mistake noticed within the delay window.
- Monitor backup age and WAL archive lag; alert when archiving fails (PostgreSQL keeps WAL until it is
  archived, so a broken `archive_command` can fill the disk).

---

## Choosing a database

| Need | Good default | Notes |
| --- | --- | --- |
| General OLTP, relations, transactions | PostgreSQL or MySQL | Start here unless a requirement rules it out |
| Very high write throughput, simple access by key, multi-DC | Cassandra / ScyllaDB, DynamoDB | Model per query; eventual consistency by default |
| Serverless, predictable latency at any scale, key access | DynamoDB | Design around access patterns and partition keys |
| Flexible documents, nested aggregates | MongoDB, or PostgreSQL `jsonb` | Watch unbounded arrays and cross-document invariants |
| Horizontal SQL with strong consistency, multi-region | Spanner, CockroachDB, YugabyteDB, TiDB | Higher write latency; cost |
| Scale out existing MySQL | Vitess, or managed equivalents | Keeps MySQL semantics, needs a sharding key |
| Cache, counters, leaderboards, rate limits | Redis | Not a system of record without care |
| Full-text and faceted search | Elasticsearch / OpenSearch | Fed from the primary DB via CDC |
| Relationship traversals | Neo4j, Neptune | Usually alongside a primary store |
| Metrics and time series | TimescaleDB, InfluxDB, Prometheus | Retention, downsampling |
| Analytics over large data | BigQuery, Snowflake, ClickHouse, Redshift | Columnar, not for OLTP |
| Files and blobs | S3 / GCS / Azure Blob | Store the reference in the DB |

Questions to ask before choosing:

1. What are the access patterns (queries per second by type, read/write ratio, point vs range vs
   analytical)?
2. What consistency does each operation need (money vs likes)?
3. How large will the data and the hot working set be in two to three years?
4. What are the latency targets and from which regions?
5. What does the team already know how to run, and what managed options exist?
6. What is the failure story: RPO, RTO, multi-AZ, multi-region?

Polyglot persistence (several stores, each for what it is good at) is normal at scale, but each
extra database is another thing to operate, back up, secure, and keep consistent (usually through
CDC from the primary).

---

## Common mistakes

- Using random UUIDv4 primary keys in InnoDB at high insert rates (use UUIDv7, ULID, or bigint).
- Adding indexes for every column, or none beyond the primary key; not checking `EXPLAIN`.
- Wrapping indexed columns in functions or comparing with the wrong type.
- Long-running transactions: bloat in PostgreSQL, long undo chains in InnoDB, held locks.
- Read-modify-write without atomic updates, locks, or versions (lost updates).
- Assuming Repeatable Read prevents all anomalies (write skew) or that the default level is
  Serializable.
- Not retrying serialization failures and deadlocks.
- Huge connection pools multiplied by many instances.
- Reading from replicas right after a write without read-your-writes handling.
- Treating replication as backup; never testing restores.
- Sharding too early, or choosing a shard key that does not appear in the main queries.
- Hash mod N for shard assignment.
- Running DDL that takes exclusive locks on big tables during peak traffic without `lock_timeout`.
- `OFFSET` pagination on large tables.
- Picking NoSQL "for scale" without listing access patterns, then needing joins and transactions.

---

## Interview questions

**1. Compare B+trees and LSM-trees. When would you choose each?**
B+trees update pages in place, give predictable point reads (~1 disk read) and excellent range scans,
at the cost of random writes and split fragmentation. LSM-trees buffer writes in a memtable and flush
sorted immutable SSTables, giving high write throughput and compression, at the cost of read
amplification (mitigated by Bloom filters) and compaction overhead and latency spikes. Read-heavy
OLTP: B+tree; write-heavy, time series, large key-value: LSM.

**2. How does a database guarantee durability without writing every data page at commit?**
Write-ahead logging: changes are recorded in a sequential log with LSNs; commit means the commit
record is fsynced (group commit batches many). Dirty pages are flushed later, never before their log
records. After a crash, recovery redoes logged changes from the last checkpoint and undoes
uncommitted transactions (ARIES). Torn pages are handled by doublewrite (InnoDB) or full-page images
(PostgreSQL).

**3. Explain MVCC in PostgreSQL vs InnoDB.**
PostgreSQL writes a new tuple per update with `xmin`/`xmax` in the heap; snapshots decide visibility;
VACUUM removes dead tuples; index entries are added unless HOT. InnoDB updates rows in place and keeps
previous versions in the undo log via `DB_ROLL_PTR`; read views reconstruct old versions; purge
cleans up. Long transactions hurt both: bloat vs long undo chains.

**4. What is write skew? Give an example and fixes.**
Two transactions read overlapping data, each updates a different row based on what it read, and
together they break an invariant (two on-call doctors both leave). Snapshot isolation allows it. Fix
with Serializable (SSI in PostgreSQL, retry on 40001), `SELECT ... FOR UPDATE` on the rows the
decision depends on, constraints (exclusion constraints), or materializing the conflict.

**5. How do you prevent lost updates?**
Atomic SQL (`SET x = x + 1`), pessimistic locking (`SELECT ... FOR UPDATE`), optimistic locking with a
version column (`UPDATE ... WHERE version = ?`, retry on 0 rows), or an isolation level that detects
it (PostgreSQL Repeatable Read aborts). MySQL Repeatable Read does not detect it.

**6. A query is slow. Walk through how you would diagnose it.**
Find it via `pg_stat_statements` or the slow log. Run `EXPLAIN (ANALYZE, BUFFERS)`. Look for seq
scans with many rows removed by filter, estimated vs actual row mismatches (stale stats, correlated
columns), sorts spilling to disk, nested loops over large inputs, and heap fetches. Fix with the right
composite or covering index (equality columns first, then range/sort), rewriting non-sargable
predicates, `ANALYZE`, or restructuring (keyset pagination, removing N+1).

**7. Index `(country, city, created_at)`: which queries use it?**
`country = ?`, `country = ? AND city = ?`, and those with a range or `ORDER BY` on `created_at` use it
fully. `city = ?` alone does not (not a leftmost prefix). `country = ? AND created_at > ?` seeks on
`country` only and filters `created_at`.

**8. How would you size a connection pool?**
Start near `cores * 2 + spindles` for the database as a whole, check with Little's law (throughput
times query time), and divide across instances. More connections than the DB can run in parallel
reduces throughput. Use PgBouncer or a proxy when many instances share the DB. Pool exhaustion
usually indicates slow queries or long transactions.

**9. Sync vs async replication: what are the trade-offs? What happens during failover?**
Sync guarantees a follower has every commit but adds latency and a stalled follower blocks writes;
async is fast but can lose acknowledged commits on failover. Semi-sync or quorum commit to one
follower in another AZ is the usual compromise. Failover must detect failure, promote the most
up-to-date follower, reroute clients, and fence the old leader to avoid split brain (consensus-held
lease, quorum, STONITH, fencing tokens).

**10. A user edits their profile and sees the old version on refresh. Why, and how do you fix it?**
Their read went to an asynchronous replica that had not applied the write yet. Implement
read-your-writes: route the user's reads to the primary for a few seconds after a write (timestamp in
session), or record the commit LSN/GTID and only read from replicas that have caught up, or wait for
it (`WAIT_FOR_EXECUTED_GTID_SET`). Monotonic reads: pin users to one replica.

**11. Explain leaderless quorums. Does `W + R > N` give strong consistency?**
Writes go to all N replicas and succeed with W acks; reads query R replicas and take the newest
version; overlap ensures a read sees the latest acknowledged write. Read repair and Merkle-tree
anti-entropy converge replicas. It is not linearizable in general: sloppy quorums and hinted handoff,
LWW with clock skew, partially failed writes, and concurrent read/write races can all return stale
data. Use Paxos-based lightweight transactions for compare-and-set.

**12. How do you choose a shard key for a multi-tenant SaaS? What if one tenant is huge?**
`tenant_id`: high cardinality, in almost every query, keeps tenant transactions single-shard. Use a
directory or fixed-partition scheme so tenants can be moved; isolate a huge tenant on its own shard,
or sub-shard it by `(tenant_id, entity_id)`. Avoid time-based keys that create hot shards.

**13. Why is `hash(key) mod N` a poor sharding scheme, and what are the alternatives?**
Changing N remaps about `N/(N+1)` of keys, forcing massive data movement. Alternatives: consistent
hashing with virtual nodes (moves ~1/N), a fixed number of logical partitions mapped to nodes (Redis
slots, Elasticsearch shards, Couchbase vBuckets), range partitioning with splits, or a directory.

**14. Local vs global secondary indexes in a sharded database?**
Local: each shard indexes its own rows; writes are local and synchronous, but index queries
scatter-gather to all shards. Global: the index is partitioned by indexed value; reads hit one index
partition, but writes touch remote partitions, so updates are asynchronous (DynamoDB GSIs) or require
distributed transactions (Spanner, CockroachDB).

**15. How does Spanner provide external consistency, and how does CockroachDB differ?**
Spanner assigns commit timestamps from TrueTime, whose bounded uncertainty is a few ms, and waits out
that uncertainty before making a commit visible, so timestamp order equals real-time order. Data is
replicated by Paxos per split, with 2PC across splits. CockroachDB has no atomic clocks; it uses
hybrid logical clocks plus a maximum clock offset, Raft per range, and restarts transactions that
read values within their uncertainty window. It provides serializability, but not Spanner's full
external consistency in all cases.

---

## Summary

- **Storage**: pages (8 KB PG, 16 KB InnoDB), B+trees (shallow, update in place, splits; clustered in
  InnoDB, heap in PG) vs LSM-trees (memtable, SSTables, compaction; size-tiered vs leveled vs
  time-window). Amplification trade-offs (RUM). Bloom filters at ~10 bits per key give ~1% false
  positives.
- **WAL**: log before data; commit = durable commit record; checkpoints bound recovery; ARIES does
  analysis, redo, undo; doublewrite or full-page writes stop torn pages.
- **MVCC**: PG keeps versions in the heap (VACUUM, wraparound, HOT); InnoDB keeps them in undo (purge).
  Readers and writers do not block each other.
- **Isolation**: know dirty read, non-repeatable read, phantom, lost update, write skew; PG RR is
  snapshot isolation (no lost update, has write skew), PG Serializable is SSI (retry 40001); MySQL
  RR has lost updates and uses next-key locks for locking reads.
- **Locking**: `FOR UPDATE`, `SKIP LOCKED` queues, gap and next-key locks, optimistic version columns,
  consistent lock order and retries for deadlocks.
- **Indexes**: equality columns first, then one range or sort column; covering with `INCLUDE`;
  partial, expression, GIN, GiST, BRIN; read `EXPLAIN ANALYZE` inside out and compare estimated vs
  actual rows; know why indexes get skipped.
- **App side**: fix N+1 with fetch joins, entity graphs or batching; keyset pagination; small
  connection pools (`cores * 2 + spindles`), PgBouncer at scale.
- **Modelling**: normalize to 3NF for writes, denormalize measured hot reads. NoSQL modelling starts
  from access patterns: key-value (PK/SK), documents (embed vs reference), wide-column (partition key
  plus clustering key, bucketed), graph (traversals).
- **Replication**: sync / async / semi-sync; leader-follower is the default; lag needs
  read-your-writes and monotonic reads; failover needs fencing against split brain; multi-leader needs
  conflict resolution; leaderless uses `W + R > N`, read repair, anti-entropy.
- **Partitioning**: range vs hash vs consistent hashing (virtual nodes) vs fixed partitions vs
  directory; local vs global secondary indexes; shard keys with high cardinality, even access, and
  query locality; shard as late as reasonable.
- **Beyond**: distributed SQL (Spanner TrueTime, CockroachDB Raft + HLC, Vitess for MySQL), CQRS with
  outbox and CDC, backups with tested PITR and clear RPO/RTO.

---

## Further reading

- Martin Kleppmann, *Designing Data-Intensive Applications*, O'Reilly, 2017 (chapters 3, 5, 6, 7 and 9
  map directly onto these notes).
- Alex Petrov, *Database Internals*, O'Reilly, 2019 (storage engines, B-trees, LSM-trees, distributed
  systems).
- Markus Winand, *SQL Performance Explained* and the free site *Use The Index, Luke!*:
  [use-the-index-luke.com](https://use-the-index-luke.com/).
- PostgreSQL documentation: "Concurrency Control" (MVCC and isolation), "Indexes", "Using EXPLAIN",
  "Write-Ahead Logging", "Continuous Archiving and Point-in-Time Recovery":
  [postgresql.org/docs](https://www.postgresql.org/docs/).
- MySQL Reference Manual, "The InnoDB Storage Engine" (locking, transaction model, multi-versioning):
  [dev.mysql.com/doc](https://dev.mysql.com/doc/).
- C. Mohan et al., *ARIES: A Transaction Recovery Method Supporting Fine-Granularity Locking and
  Partial Rollbacks Using Write-Ahead Logging*, ACM TODS, 1992.
- Patrick O'Neil et al., *The Log-Structured Merge-Tree (LSM-Tree)*, Acta Informatica, 1996.
- Hal Berenson et al., *A Critique of ANSI SQL Isolation Levels*, SIGMOD 1995.
- Michael Cahill, Uwe Röhm, Alan Fekete, *Serializable Isolation for Snapshot Databases*, SIGMOD 2008.
- Giuseppe DeCandia et al., *Dynamo: Amazon's Highly Available Key-value Store*, SOSP 2007.
- James C. Corbett et al., *Spanner: Google's Globally-Distributed Database*, OSDI 2012.
- HikariCP wiki, "About Pool Sizing": [github.com/brettwooldridge/HikariCP/wiki](https://github.com/brettwooldridge/HikariCP/wiki).
