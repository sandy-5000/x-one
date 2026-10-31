---
title: "Message queues"
order: 7
---
# Message Queues and Async Processing

A message queue lets one service hand work to another **without waiting for it**. The producer writes a message; a consumer processes it later.

```text
┌──────────┐     ┌───────────────┐     ┌──────────┐
│ Producer │ ──► │ Queue / Topic │ ──► │ Consumer │
└──────────┘     └───────────────┘     └──────────┘
```

## Why use one

- **Decoupling** — producer and consumer don't need to know about each other or be up at the same time.
- **Load levelling** — a burst of 10,000 orders queues up; workers drain it at their own pace.
- **Faster responses** — return "accepted" now, send the email / resize the image in the background.
- **Reliability** — messages persist until processed; failed ones can be retried.
- **Fan-out** — one event, many independent consumers.

---

## Point-to-point vs publish/subscribe

**Queue (point-to-point):** each message goes to **one** consumer. Add consumers to process faster.

```text
           ┌──► worker 1   (msg 1, 3)
queue ─────┼──► worker 2   (msg 2, 5)
           └──► worker 3   (msg 4)
```

**Topic (pub/sub):** each message goes to **every** subscriber group.

```text
                 ┌──► email service
"OrderPlaced" ───┼──► inventory service
                 └──► analytics service
```

---

## Popular systems

| | RabbitMQ | Apache Kafka | AWS SQS |
| --- | --- | --- | --- |
| Model | Broker with queues and exchanges | Distributed append-only log | Managed queue |
| After consumption | Message deleted | Message kept (retention period) | Message deleted |
| Replay old messages | No | Yes (reset offset) | No |
| Ordering | Per queue | Per partition | FIFO queues only |
| Throughput | Tens of thousands/s | Millions/s | Effectively unlimited (managed) |
| Best for | Task queues, complex routing | Event streaming, logs, analytics | Simple cloud task queues |

### Kafka in brief

- A **topic** is split into **partitions**; each partition is an ordered log.
- Messages with the same **key** go to the same partition, so they stay in order (e.g. all events for `order-42`).
- **Consumer groups:** each partition is read by exactly one consumer in a group — partitions are the unit of parallelism.
- Consumers track their **offset**; replay means moving the offset back.

```text
Topic "orders"
 P0: [0][1][2][3][4]        ◄── consumer A (offset 3)
 P1: [0][1][2]              ◄── consumer B (offset 2)
 P2: [0][1][2][3]           ◄── consumer C (offset 4)
```

---

## Delivery guarantees

| Guarantee | Meaning | How |
| --- | --- | --- |
| At-most-once | May lose messages, never duplicates | Ack before processing |
| At-least-once | Never loses, may duplicate | Ack after processing (the usual default) |
| Exactly-once | Each message has effect once | At-least-once + idempotent consumer, or transactional systems |

Because at-least-once is the norm, **consumers must be idempotent**.

### Idempotent consumer

```java
void handle(PaymentMessage msg) {
    // processed_messages has a unique constraint on message_id
    try {
        jdbc.update("INSERT INTO processed_messages(message_id) VALUES (?)", msg.id());
    } catch (DuplicateKeyException e) {
        return;   // already handled, skip safely
    }
    chargeCustomer(msg);   // ideally in the same DB transaction as the insert
}
```

Other ways: natural idempotency (`SET status = 'PAID'` rather than `balance = balance - 100`), or version checks.

---

## Handling failures

- **Retries with exponential backoff** — wait 1 s, 2 s, 4 s, 8 s… plus random jitter.
- **Dead-letter queue (DLQ)** — after N failed attempts, move the message aside for inspection instead of blocking the queue.
- **Visibility timeout** (SQS) — a received message is hidden for a while; if not deleted in time it reappears for another consumer.
- **Poison messages** — malformed messages that always fail; the DLQ keeps them from looping forever.

---

## The outbox pattern

Problem: saving an order to the DB and publishing `OrderPlaced` are two separate systems. If the app crashes between them, they disagree.

Solution: write the event into an **outbox table in the same DB transaction**, and let a separate relay publish it.

```text
BEGIN
  INSERT INTO orders ...
  INSERT INTO outbox (event_type, payload) VALUES ('OrderPlaced', ...)
COMMIT

relay / CDC (e.g. Debezium) ──reads outbox──► Kafka ──► consumers
```

The DB transaction guarantees both happen or neither does; the relay gives at-least-once publishing.

---

## Backpressure

When consumers can't keep up, the queue grows without bound. Options:

- Scale consumers (autoscale on queue depth).
- Rate-limit producers or reject new work (HTTP 429 / 503).
- Drop or sample low-priority messages.
- Set max queue length and alert on lag.
