---
title: "Databases at scale"
order: 5
---
# Databases at Scale

## SQL vs NoSQL

| | SQL (relational) | NoSQL |
| --- | --- | --- |
| Examples | PostgreSQL, MySQL | MongoDB, Cassandra, DynamoDB, Redis, Neo4j |
| Schema | Fixed, enforced | Flexible / schema-on-read |
| Relations | Joins, foreign keys | Usually denormalised, no joins |
| Transactions | Full ACID | Varies; often single-document/row only |
| Scaling | Vertical first; sharding is manual work | Built for horizontal scaling |
| Best for | Complex queries, money, strong consistency | Huge volume, simple access patterns, flexible data |

### NoSQL families

- **Key-value:** Redis, DynamoDB — lookup by key, extremely fast.
- **Document:** MongoDB — JSON-like documents, nested data.
- **Wide-column:** Cassandra, HBase — massive write throughput, time series.
- **Graph:** Neo4j — relationships are first-class (social graphs, recommendations).

Default to a relational database; move specific workloads to NoSQL when a concrete need appears.

---

## ACID vs BASE

**ACID** (relational transactions):

- **Atomicity** — all or nothing.
- **Consistency** — constraints always hold.
- **Isolation** — concurrent transactions don't see each other's partial work.
- **Durability** — committed data survives crashes.

**BASE** (many distributed NoSQL stores):

- **Basically Available** — always answers.
- **Soft state** — state may change without input as replicas sync.
- **Eventually consistent** — replicas converge given time.

### Isolation levels

| Level | Dirty read | Non-repeatable read | Phantom read |
| --- | --- | --- | --- |
| Read uncommitted | Possible | Possible | Possible |
| Read committed | No | Possible | Possible |
| Repeatable read | No | No | Possible |
| Serializable | No | No | No |

---

## Indexing

An index is a separate sorted structure that lets the database find rows without scanning the whole table.

```sql
-- Without index: full table scan, O(n)
SELECT * FROM users WHERE email = 'a@b.com';

CREATE INDEX idx_users_email ON users(email);
-- With B-tree index: O(log n)
```

- **B-tree / B+tree** — default; supports equality and range (`<`, `BETWEEN`, `ORDER BY`).
- **Hash index** — equality only, O(1).
- **Composite index** `(country, city)` — usable for `country` alone or `country + city`, but **not** `city` alone (leftmost-prefix rule).
- **Covering index** — contains every column the query needs, so the table itself is never read.

**Cost:** every index slows down `INSERT`/`UPDATE`/`DELETE` and uses disk. Index what you filter, join, and sort on — not everything.

---

## Replication

Copies of the same data on several machines — for read scaling and fault tolerance.

### Leader–follower (primary–replica)

```text
            writes
 app ─────────────────► ┌─────────┐
                        │ Leader  │
                        └────┬────┘
                 replication │ log
            ┌────────────────┼────────────────┐
            ▼                ▼                ▼
       ┌─────────┐      ┌─────────┐      ┌─────────┐
       │Follower │      │Follower │      │Follower │
       └─────────┘      └─────────┘      └─────────┘
            ▲                ▲                ▲
            └──────── reads ─┴────────────────┘
```

- **Synchronous:** leader waits for followers before confirming. No data loss, slower writes.
- **Asynchronous:** leader confirms immediately. Fast, but followers lag (**replication lag**) and a leader crash can lose recent writes.

Replication lag causes "I just updated my profile but still see the old one". Fix with **read-your-writes**: read from the leader for a short time after a user's own write.

### Multi-leader and leaderless

- **Multi-leader:** several nodes accept writes (multi-region). Needs conflict resolution (last-write-wins, merge, CRDTs).
- **Leaderless (Dynamo-style):** client writes to W replicas and reads from R replicas. If **W + R > N**, reads overlap the latest write. Used by Cassandra and DynamoDB.

---

## Partitioning (sharding)

Split one large dataset across several databases, each holding a subset.

### Strategies

- **Range-based:** users A–M on shard 1, N–Z on shard 2. Range queries are easy; hot spots are common.
- **Hash-based:** `shard = hash(user_id) % N`. Even distribution; range queries need every shard.
- **Directory-based:** a lookup service maps keys to shards. Flexible, but the directory is an extra dependency.

### Consistent hashing

With `hash % N`, changing N moves almost every key. Consistent hashing places servers and keys on a ring; a key belongs to the next server clockwise. Adding or removing a server only moves the keys between it and its neighbour (about 1/N of keys).

```text
                 S1
            ●────●────●
         k3 ●          ● k1      k1 → S2
           ●            ●        k2 → S3
       S3 ●              ● S2    k3 → S1
           ●            ●
            ●────●────●
                 k2
```

**Virtual nodes:** each physical server appears many times on the ring, smoothing out load.

### Sharding pain points

- Cross-shard joins and transactions are hard or impossible.
- Choosing the shard key is critical — pick one that spreads load and keeps related data together (e.g. `tenant_id`).
- Rebalancing when shards fill up.
- Celebrity / hot keys — split them further or cache them.

---

## Denormalisation

Store redundant copies of data to avoid joins at read time — e.g. store `author_name` on each post. Reads get faster; writes must update every copy. Common in read-heavy systems and NoSQL stores.

## Other storage types

- **Object storage** (S3, GCS) — files, images, backups; cheap and practically unlimited.
- **Search engines** (Elasticsearch, OpenSearch) — full-text search via inverted indexes.
- **Time-series DBs** (InfluxDB, TimescaleDB) — metrics and events by time.
- **Data warehouses** (BigQuery, Snowflake) — analytics over huge columnar datasets.
