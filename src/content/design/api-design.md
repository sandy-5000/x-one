---
title: "API design"
order: 9
---
# API Design

An API is a contract that outlives the code behind it. Clients you do not control will depend on
every field, status code, and quirk you ship, so the hard parts of API design are not the happy
path but **evolution, failure, concurrency, and security**: what happens on retry, on a
concurrent update, on page 10,000, on a stolen token, and two years later when a field must
change.

This page covers REST in depth (HTTP semantics, status codes, conditional requests, caching),
error formats, pagination, bulk and long-running operations, idempotency keys, versioning and
deprecation, authentication and authorization (API keys, sessions, JWT, OAuth 2.0, OIDC), API
security, GraphQL, gRPC, real-time APIs, OpenAPI, gateways, and developer experience.

---

## REST fundamentals

REST (Roy Fielding's 2000 dissertation) is an architectural style: **resources** identified by
URIs, manipulated through a **uniform interface** (HTTP methods), with **stateless** requests,
**cacheable** responses, and representations (JSON) that may link to other resources.

In practice "REST API" usually means "resource-oriented HTTP + JSON API". The Richardson Maturity
Model is a useful vocabulary:

| Level | Description | Example |
| --- | --- | --- |
| 0 | One endpoint, RPC over HTTP | `POST /api` with `{"action": "getUser"}` |
| 1 | Resources | `/users/42`, but everything is `POST` |
| 2 | Resources + HTTP verbs + status codes | `GET /users/42`, `DELETE /users/42` → 204 |
| 3 | Hypermedia (HATEOAS) | Responses contain links to next actions |

Most good public APIs (Stripe, GitHub) are level 2 with a few links.

### Resources and URI design

- **Nouns, not verbs**: `/orders/42`, not `/getOrder?id=42`. The verb is the HTTP method.
- **Plural collections**: `/orders` (collection) and `/orders/42` (member).
- **Lowercase, hyphens**: `/order-items`. Pick one JSON casing (`camelCase` or `snake_case`)
  and never mix.
- **Shallow nesting** for ownership: `/customers/7/orders` is fine; `/customers/7/orders/42/items/3/notes`
  is not. Once a resource has a globally unique ID, address it directly: `/order-items/3`.
- **Opaque, stable IDs**: prefer `ord_8f3k2...` or UUIDs over sequential integers, which leak
  volume and invite enumeration (see BOLA below). Type prefixes (Stripe's `cus_`, `pi_`) make
  logs and support tickets much easier.
- **Actions that are not CRUD**: model them as sub-resources or custom methods rather than
  forcing them into `PATCH`:
  - `POST /orders/42/cancellation` (create a cancellation), or
  - `POST /orders/42:cancel` (Google AIP custom method style), or
  - `POST /orders/42/cancel` (common and acceptable).
- **Query strings for filtering, sorting, pagination**, never for identity.

### HTTP methods: safe and idempotent

RFC 9110 defines two properties:

- **Safe**: the client does not request any state change (read-only from the client's
  perspective; the server may still log or count).
- **Idempotent**: sending the request N times has the same **effect on server state** as sending
  it once. The responses may differ (the second `DELETE` may return 404).

| Method | Safe | Idempotent | Request body | Typical success | Notes |
| --- | --- | --- | --- | --- | --- |
| GET | Yes | Yes | No (no defined semantics) | 200, 304 | Cacheable |
| HEAD | Yes | Yes | No | 200 | Headers only; cacheable |
| OPTIONS | Yes | Yes | Rare | 204 / 200 | CORS preflight |
| PUT | No | Yes | Full representation | 200, 201, 204 | Create-or-replace at a known URI |
| DELETE | No | Yes | Avoid | 204, 202 | Second call: 404 or 204, both fine |
| POST | No | **No** | Yes | 201, 200, 202 | Create in a collection, or an action |
| PATCH | No | **Not necessarily** | Patch document | 200, 204 | JSON Merge Patch (RFC 7396) or JSON Patch (RFC 6902) |

Why it matters: clients, proxies, and SDKs **automatically retry idempotent requests** after
timeouts. A `PUT` that sets `balance = 100` is safe to retry; a `POST` that adds 100 is not (hence
idempotency keys, below). A `PATCH` with JSON Patch `{"op": "add", "path": "/tags/-"}` appends
each time, so it is not idempotent; a merge patch `{"status": "shipped"}` is.

### Status code guide

| Code | Use when | Notes |
| --- | --- | --- |
| 200 OK | Successful read, or update returning a body | |
| 201 Created | Resource created | Include `Location: /orders/42` and usually the body |
| 202 Accepted | Work queued, not done | Return a status resource (see long-running operations) |
| 204 No Content | Success, no body | DELETE, or PUT/PATCH when you do not echo the resource |
| 301 / 308 | Permanent redirect | 308 preserves the method and body; 301 may turn POST into GET |
| 302 / 307 | Temporary redirect | 307 preserves the method |
| 304 Not Modified | Conditional GET, cached copy still valid | No body |
| 400 Bad Request | Malformed syntax, invalid JSON, wrong types | |
| 401 Unauthorized | Missing or invalid credentials | Really "unauthenticated"; send `WWW-Authenticate` |
| 403 Forbidden | Authenticated but not allowed | Or 404 to hide existence |
| 404 Not Found | No such resource, or caller may not know it exists | |
| 405 Method Not Allowed | Wrong method for this resource | Send `Allow` header |
| 406 Not Acceptable | Cannot produce any type in `Accept` | |
| 409 Conflict | Conflicts with current state | Duplicate unique field, invalid state transition, concurrent idempotent request |
| 410 Gone | Existed, permanently removed | Useful after sunsetting an endpoint |
| 412 Precondition Failed | `If-Match` / `If-Unmodified-Since` failed | Optimistic concurrency conflict |
| 413 Content Too Large | Body too big | |
| 415 Unsupported Media Type | Wrong `Content-Type` | |
| 422 Unprocessable Content | Well-formed but semantically invalid | Validation errors (named "Unprocessable Entity" before RFC 9110) |
| 428 Precondition Required | Server requires a conditional request | Force clients to send `If-Match` (RFC 6585) |
| 429 Too Many Requests | Rate limited | `Retry-After` |
| 500 Internal Server Error | Unhandled server bug | Never leak stack traces |
| 502 Bad Gateway | Upstream returned garbage | From proxies/gateways |
| 503 Service Unavailable | Overloaded or in maintenance | `Retry-After` |
| 504 Gateway Timeout | Upstream did not answer in time | |

Two rules: **4xx means the client can fix it, 5xx means the server must**; and **do not return
200 with `{"error": ...}`** in a REST API, because every proxy, monitor, and retry policy reads
the status code.

### Conditional requests and ETags

An **ETag** is an opaque validator for a specific representation of a resource. Strong ETags
(`"v7"`) mean byte-identical; weak ETags (`W/"v7"`) mean semantically equivalent. A version
column or a hash of the body both work.

Two uses:

**1. Cache revalidation (GET).** The client sends what it has; the server answers 304 if it is
still current, saving the body transfer.

```text
GET /orders/42                      →  200 OK, ETag: "7", body
GET /orders/42  If-None-Match: "7"  →  304 Not Modified (no body)
```

**2. Optimistic concurrency (writes).** Prevents the **lost update**: two clients read version 7,
both modify, the second write silently overwrites the first.

```text
Alice: GET /orders/42           → ETag "7"
Bob:   GET /orders/42           → ETag "7"
Alice: PUT /orders/42  If-Match: "7"   → 200, ETag "8"
Bob:   PUT /orders/42  If-Match: "7"   → 412 Precondition Failed  (re-read, merge, retry)
```

Implementation with a version column, Spring MVC and JDBC:

```java
import java.time.Duration;
import org.springframework.http.CacheControl;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.context.request.WebRequest;

@RestController
@RequestMapping("/v1/orders")
class OrderController {

    record OrderDto(String id, String status, String shippingAddress, long version) {}
    record OrderUpdate(String status, String shippingAddress) {}

    private final OrderRepository orders;
    private final JdbcTemplate jdbc;

    OrderController(OrderRepository orders, JdbcTemplate jdbc) {
        this.orders = orders;
        this.jdbc = jdbc;
    }

    @GetMapping("/{id}")
    ResponseEntity<OrderDto> get(@PathVariable String id, WebRequest request) {
        OrderDto order = orders.find(id).orElseThrow(() -> new NotFoundException(id));
        String etag = "\"" + order.version() + "\"";
        if (request.checkNotModified(etag)) {
            return null; // Spring has already written 304 Not Modified
        }
        return ResponseEntity.ok()
                .eTag(etag)
                .cacheControl(CacheControl.maxAge(Duration.ofSeconds(30)).cachePrivate())
                .body(order);
    }

    @PutMapping("/{id}")
    ResponseEntity<OrderDto> replace(@PathVariable String id,
                                     @RequestHeader(value = "If-Match", required = false) String ifMatch,
                                     @RequestBody OrderUpdate update) {
        if (ifMatch == null) {
            return ResponseEntity.status(HttpStatus.PRECONDITION_REQUIRED).build();   // 428
        }
        long expected = Long.parseLong(ifMatch.replace("W/", "").replace("\"", ""));
        int rows = jdbc.update("""
                UPDATE orders
                   SET status = ?, shipping_address = ?, version = version + 1
                 WHERE id = ? AND version = ?
                """, update.status(), update.shippingAddress(), id, expected);
        if (rows == 0) {
            return orders.find(id).isPresent()
                    ? ResponseEntity.status(HttpStatus.PRECONDITION_FAILED).build()   // 412
                    : ResponseEntity.notFound().build();
        }
        OrderDto saved = orders.find(id).orElseThrow();
        return ResponseEntity.ok().eTag("\"" + saved.version() + "\"").body(saved);
    }
}
```

The `WHERE version = ?` clause is the actual concurrency control; the ETag is just how the version
travels over HTTP. For multiple representations of the same resource (JSON vs CSV), include the
media type in the ETag so they do not collide.

### Caching headers

HTTP caching (RFC 9111) is the cheapest scalability tool REST gives you.

| Directive / header | Meaning |
| --- | --- |
| `Cache-Control: max-age=60` | Fresh for 60 s in any cache |
| `s-maxage=300` | Override for shared caches (CDN) only |
| `private` | Only the end client may cache (per-user data) |
| `public` | Shared caches may store, even if the request had `Authorization` |
| `no-cache` | May store, but **must revalidate** before each use (not "don't cache") |
| `no-store` | Never store anywhere (secrets, PII) |
| `must-revalidate` | Once stale, do not serve without revalidating |
| `stale-while-revalidate=30` | Serve stale up to 30 s while refreshing in background (RFC 5861) |
| `immutable` | Content at this URL never changes (fingerprinted assets) |
| `ETag` / `Last-Modified` | Validators for conditional requests |
| `Vary: Accept, Accept-Encoding` | Cache key includes these request headers |

Typical choices: per-user API responses `private, no-cache` with an ETag (always revalidate,
cheap 304s); public catalogue data `public, max-age=60, s-maxage=300, stale-while-revalidate=60`;
tokens and account data `no-store`.

### Content negotiation

- The client says what it sends (`Content-Type`) and what it wants (`Accept`,
  `Accept-Language`, `Accept-Encoding`).
- The server picks a representation and responds with `Content-Type` and `Vary`.
- Unsupported request type: 415. Cannot satisfy `Accept`: 406 (or ignore and send the default,
  which many APIs do).
- Compression: `Accept-Encoding: gzip, br` → `Content-Encoding: br`. Big win for JSON.
- Vendor media types (`application/vnd.example.v2+json`) are one versioning strategy (below).

### HATEOAS, briefly

Hypermedia as the engine of application state: responses include links describing what the
client can do next, so clients follow links instead of constructing URLs.

```json
{
  "id": "ord_42",
  "status": "pending_payment",
  "_links": {
    "self":    { "href": "/v1/orders/ord_42" },
    "pay":     { "href": "/v1/orders/ord_42/payments", "method": "POST" },
    "cancel":  { "href": "/v1/orders/ord_42/cancellation", "method": "POST" }
  }
}
```

Formats: HAL, JSON:API, Siren. In practice full HATEOAS is rare in public APIs because clients are
written against documentation anyway; the parts that pay off are **pagination links** and
**state-dependent action links** (show "cancel" only when cancellable).

---

## Error format: RFC 9457 Problem Details

RFC 9457 (which obsoletes RFC 7807) standardises a JSON error body with media type
`application/problem+json`.

```json
{
  "type": "https://api.example.com/problems/validation-error",
  "title": "Your request is not valid.",
  "status": 422,
  "detail": "2 fields failed validation.",
  "instance": "/v1/orders/ord_42",
  "requestId": "req_7f3b9c",
  "errors": [
    { "pointer": "#/email", "detail": "must be a valid email address" },
    { "pointer": "#/items/0/quantity", "detail": "must be greater than 0" }
  ]
}
```

| Member | Meaning |
| --- | --- |
| `type` | URI identifying the problem type; stable, documented, machine-readable. Default `about:blank` |
| `title` | Short human summary of the type; should not change between occurrences |
| `status` | HTTP status code (advisory copy) |
| `detail` | Human explanation of this occurrence |
| `instance` | URI identifying this occurrence |
| extensions | Any additional members (`errors`, `requestId`, `balance`) |

Guidelines: clients branch on `type` (or your own stable `code`), never on `detail` text; include
a request/trace ID; never include stack traces, SQL, or internal hostnames. Spring Framework 6
has built-in support:

```java
import java.net.URI;
import org.springframework.http.HttpStatus;
import org.springframework.http.ProblemDetail;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
class ApiErrorHandler {

    @ExceptionHandler(InsufficientFundsException.class)
    ProblemDetail insufficientFunds(InsufficientFundsException e) {
        ProblemDetail pd = ProblemDetail.forStatusAndDetail(
                HttpStatus.UNPROCESSABLE_ENTITY, "Balance is lower than the transfer amount.");
        pd.setType(URI.create("https://api.example.com/problems/insufficient-funds"));
        pd.setTitle("Insufficient funds");
        pd.setProperty("balance", e.balance());
        pd.setProperty("currency", e.currency());
        return pd;
    }
}
```

---

## Pagination

Never return an unbounded collection. Every list endpoint needs a default and maximum page size.

### Offset pagination

```text
GET /v1/posts?limit=20&offset=40
```

```sql
SELECT id, title, created_at FROM posts
ORDER BY created_at DESC, id DESC
LIMIT 20 OFFSET 40;
```

- Pros: trivial, supports "jump to page 7", total counts are natural.
- **Deep pages are slow**: the database must produce and discard `offset` rows. `OFFSET 1000000`
  reads a million index entries.
- **Unstable under writes**: an insert at the top shifts everything down, so the next page
  repeats an item; a delete skips one.
- `COUNT(*)` for "page 3 of 1,204" is itself expensive on large tables.

### Keyset (cursor) pagination

Remember the sort key of the last row and ask for rows strictly after it. The sort must be on a
**unique, total order**, so add the primary key as a tiebreaker.

```sql
-- First page
SELECT id, title, created_at FROM posts
WHERE author_id = :author
ORDER BY created_at DESC, id DESC
LIMIT :page_size + 1;           -- one extra row tells us whether there is a next page

-- Next page, after the last row (created_at = :c, id = :i)
SELECT id, title, created_at FROM posts
WHERE author_id = :author
  AND (created_at, id) < (:c, :i)       -- row-value comparison (PostgreSQL, MySQL 8)
ORDER BY created_at DESC, id DESC
LIMIT :page_size + 1;

CREATE INDEX posts_author_created_id ON posts (author_id, created_at DESC, id DESC);
```

Where row-value comparison is unsupported or not index-friendly, expand it:
`created_at < :c OR (created_at = :c AND id < :i)`.

- O(page size) at any depth, using the index range scan.
- Stable under inserts and deletes: no duplicates, no skips relative to the sort key.
- Cannot jump to an arbitrary page; total counts are a separate (often approximate) query.
- Sorting by a user-chosen column requires a cursor containing that column plus the tiebreaker,
  and an index for each supported sort.

**Opaque cursors.** Do not expose `?after_id=123&after_ts=...`: clients will construct them,
and you cannot change the encoding later. Encode the position into an opaque token, and sign it
so clients cannot tamper with it (for example to change an embedded tenant or filter).

```java
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.Arrays;
import java.util.Base64;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

public final class CursorCodec {

    public record Cursor(Instant createdAt, String id) {}

    private static final Base64.Encoder ENC = Base64.getUrlEncoder().withoutPadding();
    private static final Base64.Decoder DEC = Base64.getUrlDecoder();
    private final SecretKeySpec key;

    public CursorCodec(byte[] secret) {
        this.key = new SecretKeySpec(secret, "HmacSHA256");
    }

    public String encode(Cursor c) {
        byte[] payload = ("v1|" + c.createdAt() + "|" + c.id()).getBytes(StandardCharsets.UTF_8);
        return ENC.encodeToString(payload) + "." + ENC.encodeToString(sign(payload));
    }

    public Cursor decode(String token) {
        String[] parts = token.split("\\.", 2);
        if (parts.length != 2) {
            throw new InvalidCursorException();
        }
        byte[] payload;
        byte[] mac;
        try {
            payload = DEC.decode(parts[0]);
            mac = DEC.decode(parts[1]);
        } catch (IllegalArgumentException e) {
            throw new InvalidCursorException();
        }
        if (!MessageDigest.isEqual(sign(payload), mac)) {   // constant-time comparison
            throw new InvalidCursorException();
        }
        String[] fields = new String(payload, StandardCharsets.UTF_8).split("\\|", 3);
        if (fields.length != 3 || !fields[0].equals("v1")) {
            throw new InvalidCursorException();
        }
        return new Cursor(Instant.parse(fields[1]), fields[2]);
    }

    private byte[] sign(byte[] payload) {
        try {
            Mac hmac = Mac.getInstance("HmacSHA256");
            hmac.init(key);
            return Arrays.copyOf(hmac.doFinal(payload), 16);   // 128-bit tag is plenty
        } catch (GeneralSecurityException e) {
            throw new IllegalStateException(e);
        }
    }

    public static final class InvalidCursorException extends RuntimeException {}
}
```

The `v1|` prefix lets you change the cursor format later. Cursors should also be bound to the
query's filters and sort (either embedded in the cursor or rejected when they differ), otherwise a
client can reuse a cursor from a different query and get nonsense.

Response envelope:

```json
{
  "data": [ { "id": "post_901", "title": "..." } ],
  "pagination": {
    "nextCursor": "djF8MjAyNi0xMC0zMVQxMDowMDowMFp8cG9zdF85MDE.q3Xr...",
    "hasMore": true
  }
}
```

(Some APIs use a `Link: <...?cursor=...>; rel="next"` header, RFC 8288, as GitHub does.)

### Consistency issues

- Keyset pagination gives a consistent view **relative to the sort key**, not a snapshot. A row
  updated so that its sort key moves (e.g. sorting by `updated_at`) can appear twice or never.
  Sort by an immutable key when completeness matters.
- For exports or sync, offer a **changes feed** (`?updated_since=` plus a cursor on
  `(updated_at, id)`, or an event stream) instead of paging a mutable list.
- Snapshot pagination (holding a DB snapshot or materialising results) is expensive; reserve it
  for reports.

| | Offset | Keyset / cursor |
| --- | --- | --- |
| Deep page cost | O(offset + limit) | O(limit) |
| Jump to page N | Yes | No |
| Stable under inserts | No | Yes |
| Total count | Natural (but costly) | Separate query |
| Typical use | Admin tables, small data | Feeds, public APIs, infinite scroll, sync |

---

## Filtering, sorting, sparse fieldsets

```text
GET /v1/orders?status=paid&created_after=2026-10-01T00:00:00Z&customer=cus_9
GET /v1/orders?sort=-created_at,id
GET /v1/orders?fields=id,status,total
GET /v1/orders/ord_42?expand=customer,items.product
```

- **Filtering**: simple equality and range parameters cover most needs. For rich filtering,
  define a small grammar (Google AIP-160 filter syntax, OData `$filter`, or RSQL) rather than
  inventing ad-hoc parameters per endpoint.
- **Sorting**: `-` prefix for descending is a common convention. **Allow-list** sortable fields:
  unvalidated sort parameters lead to SQL injection (`ORDER BY` cannot be parameterised) and to
  full-table sorts on unindexed columns.
- **Sparse fieldsets** (`fields=`, or JSON:API's `fields[orders]=status,total`) reduce payload
  size for mobile clients.
- **Expansion** (`expand=customer`, Stripe style) inlines related resources to avoid N extra
  round trips. Limit depth, and enforce authorization on expanded objects too.

---

## Bulk and batch operations

When clients need to create 10,000 contacts, 10,000 HTTP requests is slow and hits rate limits.

```http
POST /v1/contacts:batchCreate
Content-Type: application/json

{ "requests": [ { "email": "a@example.com" }, { "email": "not-an-email" } ] }
```

```json
{
  "results": [
    { "index": 0, "status": 201, "data": { "id": "con_1", "email": "a@example.com" } },
    { "index": 1, "status": 422,
      "error": { "type": "https://api.example.com/problems/validation-error",
                 "detail": "email is not valid" } }
  ]
}
```

Decide and document:

- **Atomic vs partial success.** All-or-nothing is easier to reason about; partial success needs
  per-item results. Return 200 with per-item statuses (or 207 Multi-Status, borrowed from WebDAV);
  do not return 201 when half failed.
- **Limits**: max items per batch and max body size.
- **Idempotency**: an idempotency key for the whole batch, or per-item client IDs.
- **Large batches become async**: accept, return 202, process in background (next section).

---

## Long-running operations

If work takes more than a few seconds (video transcoding, exports, provisioning), do not hold the
HTTP connection open.

```text
POST /v1/exports                     →  202 Accepted
                                        Location: /v1/operations/op_77
                                        Retry-After: 5
                                        { "id": "op_77", "status": "running" }

GET /v1/operations/op_77             →  200 { "id": "op_77", "status": "running",
                                              "progress": 0.4 }

GET /v1/operations/op_77             →  200 { "id": "op_77", "status": "succeeded",
                                              "result": { "href": "/v1/exports/exp_5" } }
```

- The operation resource has a stable state machine: `queued → running → succeeded | failed |
  cancelled`, with an error object on failure. Google AIP-151 describes this pattern.
- Offer **webhooks** (or SSE) on completion so clients need not poll; keep polling as the fallback.
- Support `POST /v1/operations/op_77:cancel` if the work can be stopped.
- Make the initiating `POST` idempotent; a retried "start export" must not start two exports.
- Expire operation records after a documented retention period.

---

## Idempotency keys in depth

A client sends `POST /payments`, the server charges the card, and the response is lost in a
network failure. The client cannot know whether the charge happened. Retrying without protection
double-charges. **Idempotency keys** make unsafe methods safely retryable.

```http
POST /v1/payments
Idempotency-Key: 5f2b1e6c-3c41-4c3b-9b2e-7a0d1f4e8c11
Content-Type: application/json

{ "amount": 4999, "currency": "INR", "source": "card_1" }
```

The IETF draft "The Idempotency-Key HTTP Header Field" (draft-ietf-httpapi-idempotency-key-header)
standardises the header; Stripe popularised the pattern. Semantics:

| Situation | Response |
| --- | --- |
| New key | Execute, store the response, return it |
| Same key, same request, completed | Replay the stored status and body, do not execute |
| Same key, same request, still in progress | 409 Conflict (client retries later) |
| Same key, **different** request body | 422 Unprocessable Content (key misuse) |
| Key missing on an endpoint that requires it | 400 Bad Request |

### Schema

```sql
CREATE TABLE idempotency_keys (
    tenant_id      TEXT        NOT NULL,       -- keys are scoped per caller
    idem_key       TEXT        NOT NULL,
    request_hash   TEXT        NOT NULL,       -- SHA-256 of method + path + canonical body
    status         TEXT        NOT NULL CHECK (status IN ('IN_PROGRESS', 'COMPLETED')),
    lock_token     UUID,                       -- identifies the current owner
    locked_until   TIMESTAMPTZ,                -- lease; a crashed owner's lock expires
    response_code  INT,
    response_body  JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, idem_key)
);

CREATE INDEX idempotency_keys_created ON idempotency_keys (created_at);  -- for TTL cleanup
-- Cleanup job: DELETE FROM idempotency_keys WHERE created_at < now() - interval '24 hours';
```

### Race handling

The tricky part is two identical requests arriving concurrently (client retry while the first is
still running, or a double-click). The primary key makes **insert the claim**: exactly one request
wins the `INSERT`, the other sees a conflict.

```text
request A                         idempotency_keys                     request B
INSERT (k, IN_PROGRESS) ───────►  row k created (A owns it)
                                  conflict             ◄─────────────── INSERT (k) → 0 rows
                                                                        SELECT k → IN_PROGRESS
                                                                        → 409 Conflict
run business logic + mark
COMPLETED in one transaction ──►  row k COMPLETED, response stored
                                                       ◄─────────────── retry: SELECT k
                                                                        → replay stored 201
```

Implementation with Spring `JdbcTemplate` and PostgreSQL:

```java
import java.util.Map;
import java.util.UUID;
import java.util.function.Supplier;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.transaction.support.TransactionTemplate;

public final class IdempotencyService {

    public record StoredResponse(int status, String jsonBody) {}

    public static final class KeyReusedException extends RuntimeException {}     // → 422
    public static final class InProgressException extends RuntimeException {}    // → 409
    public static final class LostOwnershipException extends RuntimeException {} // → 409

    private static final long LEASE_SECONDS = 60;

    private final JdbcTemplate jdbc;
    private final TransactionTemplate tx;

    public IdempotencyService(JdbcTemplate jdbc, TransactionTemplate tx) {
        this.jdbc = jdbc;
        this.tx = tx;
    }

    /**
     * Runs {@code businessLogic} at most once per (tenant, key). The business logic must do its
     * database writes through the same DataSource so they commit atomically with the result.
     */
    public StoredResponse execute(String tenantId, String key, String requestHash,
                                  Supplier<StoredResponse> businessLogic) {
        UUID token = UUID.randomUUID();

        int inserted = jdbc.update("""
                INSERT INTO idempotency_keys
                       (tenant_id, idem_key, request_hash, status, lock_token, locked_until)
                VALUES (?, ?, ?, 'IN_PROGRESS', ?, now() + (? * interval '1 second'))
                ON CONFLICT (tenant_id, idem_key) DO NOTHING
                """, tenantId, key, requestHash, token, LEASE_SECONDS);

        if (inserted == 0) {
            Map<String, Object> row = jdbc.queryForMap("""
                    SELECT request_hash, status, response_code, response_body::text AS body,
                           (locked_until < now()) AS lease_expired
                      FROM idempotency_keys
                     WHERE tenant_id = ? AND idem_key = ?
                    """, tenantId, key);

            if (!requestHash.equals(row.get("request_hash"))) {
                throw new KeyReusedException();
            }
            if ("COMPLETED".equals(row.get("status"))) {
                return new StoredResponse((Integer) row.get("response_code"), (String) row.get("body"));
            }
            if (!Boolean.TRUE.equals(row.get("lease_expired"))) {
                throw new InProgressException();
            }
            // The previous owner crashed: take over the lease atomically (compare-and-set).
            int taken = jdbc.update("""
                    UPDATE idempotency_keys
                       SET lock_token = ?, locked_until = now() + (? * interval '1 second')
                     WHERE tenant_id = ? AND idem_key = ?
                       AND status = 'IN_PROGRESS' AND locked_until < now()
                    """, token, LEASE_SECONDS, tenantId, key);
            if (taken == 0) {
                throw new InProgressException();
            }
        }

        try {
            return tx.execute(status -> {
                StoredResponse response = businessLogic.get();
                int completed = jdbc.update("""
                        UPDATE idempotency_keys
                           SET status = 'COMPLETED', response_code = ?,
                               response_body = ?::jsonb, lock_token = NULL, locked_until = NULL
                         WHERE tenant_id = ? AND idem_key = ? AND lock_token = ?
                        """, response.status(), response.jsonBody(), tenantId, key, token);
                if (completed == 0) {
                    throw new LostOwnershipException(); // rolls back the business writes too
                }
                return response;
            });
        } catch (RuntimeException e) {
            // Release the claim so the client can retry (only if we still own it).
            jdbc.update("""
                    DELETE FROM idempotency_keys
                     WHERE tenant_id = ? AND idem_key = ? AND lock_token = ? AND status = 'IN_PROGRESS'
                    """, tenantId, key, token);
            throw e;
        }
    }
}
```

Design points that interviewers probe:

- **Scope keys per tenant/API key**, otherwise one customer's key collides with another's (and
  could replay their response).
- **Fingerprint the request** so a reused key with a different body is rejected rather than
  silently replaying an unrelated response.
- **Commit the result atomically with the side effects.** Because the business writes and the
  `COMPLETED` update share a transaction, there is no window where the payment row exists but the
  key says "in progress".
- **Lease + lock token** handles crashed owners without letting a slow original owner and a new
  owner both commit: only the current token can complete.
- **What to store**: successful responses and deterministic client errors (4xx validation). Do
  not store 5xx or transient failures; release the key so a retry re-executes.
- **External side effects** (calling a card network) cannot join your DB transaction. Pass your
  idempotency key downstream (most payment providers accept one), and for multi-step flows record
  **recovery points** so a retry resumes from the last completed step (described in Brandur
  Leach's "Implementing Stripe-like Idempotency Keys in Postgres").
- **Retention**: 24 hours is common (Stripe documents 24 h). Longer than any client retry window.
- `PUT` with client-chosen IDs (`PUT /payments/pay_client_123`) is an alternative that makes
  creation naturally idempotent.

---

## Versioning and evolution

### Strategies

| Strategy | Example | Pros | Cons |
| --- | --- | --- | --- |
| URI path | `/v1/orders` | Obvious, easy routing, cache-friendly | Coarse: whole API bumps at once |
| Header (custom) | `X-GitHub-Api-Version: 2022-11-28` | Clean URLs, fine-grained | Less visible, needs tooling |
| Media type | `Accept: application/vnd.example.v2+json` | Per-resource versions, "pure" REST | Awkward for clients and caches (`Vary`) |
| Query param | `/orders?api-version=2024-05-01` | Easy to test (Azure uses this) | Easy to forget, pollutes caching |
| Date-based, pinned per account | `Stripe-Version: 2024-06-20` | Clients upgrade explicitly; many small changes | Server needs a transformation layer per version |

Stripe's model is worth knowing: each account is pinned to the API version current when it first
made a request; requests can override with a header; internally the latest version is
implemented once, and a chain of **version change modules** transforms responses backward for
older versions.

Whatever the scheme, **a major version is a new product**: you will run v1 and v2 side by side for
years. The best strategy is to need new versions rarely.

### Backward-compatible evolution rules

Safe (additive) changes:

- Adding a new endpoint, a new optional request field, or a new response field.
- Adding a new optional query parameter with a default that preserves old behaviour.
- Adding a new enum value **only if** clients were told to handle unknown values (document this
  from day one).
- Relaxing validation (accepting a longer string).

Breaking changes:

- Removing or renaming a field, endpoint, or enum value.
- Changing a field's type or format (`"amount": "49.99"` to `4999`), or its meaning (units!).
- Making an optional request field required; tightening validation.
- Changing default behaviour, sort order, pagination defaults, or error codes clients branch on.
- Changing authentication or the response for an existing status code.

Client-side rule (the **tolerant reader**, Postel's law): ignore unknown fields, do not fail on new
enum values, do not depend on field order. Put this in your SDKs.

Evolution techniques: expand-and-contract (add the new field, migrate clients, remove the old one
in the next major version); feature flags per client; response field aliases during transitions.

### Deprecation and sunset

Announce, measure, then remove.

```http
HTTP/1.1 200 OK
Deprecation: @1798761600
Sunset: Sat, 01 May 2027 00:00:00 GMT
Link: <https://developer.example.com/migrations/v2>; rel="deprecation"; type="text/html"
```

- **`Sunset`** (RFC 8594): the HTTP-date after which the resource will stop responding.
- **`Deprecation`** (RFC 9745, 2025): when the resource was or will be deprecated, as a
  structured-field date (`@` + Unix seconds), plus a `deprecation` link relation for docs.
- Track usage of deprecated endpoints **per client** and contact the top users directly.
- Brownouts (scheduled short periods where the old endpoint returns 410) surface forgotten
  integrations before the final shutdown.
- After the sunset date, return **410 Gone** with a problem body that links to the migration
  guide.

---

## Authentication and authorization

Authentication answers "who is calling?"; authorization answers "may they do this to this
object?". Most API breaches are authorization failures, not cryptography failures.

### API keys

- Long random secrets (at least 128 bits of entropy), with a recognisable prefix
  (`sk_live_...`) so secret scanners (GitHub secret scanning) can detect leaks.
- Sent in a header (`Authorization: Bearer sk_live_...` or `X-API-Key`), **never in the URL**
  (URLs end up in logs, proxies, browser history).
- **Store only a hash.** Because keys are high-entropy random strings, a fast hash (SHA-256) is
  sufficient; slow password hashes are for low-entropy human passwords. Show the key once at
  creation.
- Support multiple active keys per account for **zero-downtime rotation**, per-key scopes,
  expiry, last-used timestamps, and instant revocation.
- API keys identify an application, not a user. For user-delegated access use OAuth.

### Sessions and cookies

Classic for first-party web apps: the server stores session state, the browser holds a random
session ID in a cookie.

```http
Set-Cookie: __Host-session=9f2c...; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=3600
```

| Attribute | Purpose |
| --- | --- |
| `HttpOnly` | JavaScript cannot read it, limiting XSS token theft |
| `Secure` | Only sent over HTTPS |
| `SameSite=Lax` / `Strict` | Not sent on most cross-site requests; main CSRF defence |
| `__Host-` prefix | Browser enforces `Secure`, `Path=/`, no `Domain` (no subdomain override) |

**CSRF**: because browsers attach cookies automatically, a malicious site can make the user's
browser send a state-changing request to your API. Defences, layered:

- `SameSite=Lax` or `Strict` cookies (Lax still allows top-level GET navigations, so GETs must be
  safe).
- **Synchronizer token**: a per-session random token rendered into the page and required in a
  header (`X-CSRF-Token`) or form field on unsafe methods.
- **Double-submit cookie**: token in a cookie and in a header; the server checks they match
  (sign it to the session to prevent cookie injection).
- Verify `Origin` (or `Sec-Fetch-Site`) on unsafe requests.
- APIs authenticated only with `Authorization` headers are not vulnerable to CSRF, because
  browsers do not attach those automatically.

Sessions are easy to revoke (delete the row) and keep tokens out of JavaScript. Their cost is a
session store lookup per request (Redis), which is rarely a real problem.

### JWT

A JSON Web Token (RFC 7519) is three base64url parts: `header.payload.signature`.

```text
eyJhbGciOiJSUzI1NiIsImtpZCI6IjIwMjYtMTAifQ . eyJpc3MiOiJodHRwczovL2F1dGguZXhhbXBsZS5j... . kQ3v...

header   { "alg": "RS256", "kid": "2026-10", "typ": "JWT" }
payload  { "iss": "https://auth.example.com",
           "sub": "user_42",
           "aud": "orders-api",
           "exp": 1793448900, "iat": 1793448000, "nbf": 1793448000,
           "jti": "8c1d...", "scope": "orders:read orders:write" }
signature = RSASSA-PKCS1-v1_5-SHA256( base64url(header) + "." + base64url(payload), private key )
```

- **Signed, not encrypted** (JWS). Anyone can read the payload; never put secrets or sensitive PII
  in it. JWE exists for encryption but is rarely needed.
- **Algorithms**: HS256 (shared secret; every verifier can also mint tokens), RS256/PS256 (RSA),
  ES256 (ECDSA P-256), EdDSA (Ed25519). Prefer asymmetric algorithms so resource servers only
  hold public keys.
- **Key distribution**: the issuer publishes public keys as a JWKS (`/.well-known/jwks.json`);
  the `kid` header selects the key, enabling rotation (publish the new key before signing with it).

**Validation checklist** (RFC 8725, JWT Best Current Practices):

1. Verify the signature with a key from **your trusted JWKS**, using an **allow-listed algorithm**
   fixed by configuration, never taken from the token's `alg` header.
2. Reject `alg: none`.
3. Check `iss` equals the expected issuer and `aud` contains this API.
4. Check `exp` and `nbf`, with small clock skew tolerance (30 to 60 s).
5. Check `typ` where token types could be confused (access token vs ID token).
6. Then apply authorization: scopes and object-level checks.

Classic pitfalls:

- **Algorithm confusion**: a server configured for RS256 that trusts the header can be sent an
  HS256 token signed with the **public key** as the HMAC secret, and it verifies. Pin algorithms.
- **`kid` / `jku` injection**: never fetch keys from URLs inside the token, and treat `kid` as a
  lookup key, not a file path or SQL fragment.
- **Using ID tokens as access tokens** for APIs (wrong audience).
- **Storing tokens in `localStorage`**, where any XSS can exfiltrate them. For browser apps a
  BFF holding tokens server-side with an `HttpOnly` session cookie is the current recommendation.

**Revocation** is the fundamental trade-off: a JWT is valid until `exp` without asking anyone.

| Approach | How | Cost |
| --- | --- | --- |
| Short-lived access tokens | 5 to 15 min `exp` + refresh tokens | Revocation delay = token lifetime |
| Refresh token rotation | Each refresh returns a new refresh token; reuse of an old one revokes the family | Detects theft of refresh tokens |
| Denylist by `jti` | Cache of revoked IDs until their `exp` | A lookup per request (but only for a small set) |
| Token version per user | `ver` claim compared with a per-user counter | A lookup per request (cacheable) |
| Introspection (RFC 7662) | Resource server asks the auth server; or use opaque tokens | Network call per request (cache briefly) |

Validating JWTs in a Spring Boot resource server:

```java
import java.util.List;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.http.HttpMethod;
import org.springframework.security.config.Customizer;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.oauth2.core.DelegatingOAuth2TokenValidator;
import org.springframework.security.oauth2.jwt.*;
import org.springframework.security.web.SecurityFilterChain;

@Configuration
class SecurityConfig {

    private static final String ISSUER = "https://auth.example.com";

    @Bean
    SecurityFilterChain api(HttpSecurity http) throws Exception {
        http.authorizeHttpRequests(auth -> auth
                        .requestMatchers(HttpMethod.GET, "/v1/orders/**").hasAuthority("SCOPE_orders:read")
                        .requestMatchers(HttpMethod.POST, "/v1/orders/**").hasAuthority("SCOPE_orders:write")
                        .anyRequest().authenticated())
                .oauth2ResourceServer(oauth -> oauth.jwt(Customizer.withDefaults()));
        return http.build();
    }

    @Bean
    JwtDecoder jwtDecoder() {
        // Discovers jwks_uri from the issuer's OpenID configuration; keys are cached and rotated.
        NimbusJwtDecoder decoder = (NimbusJwtDecoder) JwtDecoders.fromIssuerLocation(ISSUER);
        OAuth2TokenValidator<Jwt> audience = new JwtClaimValidator<List<String>>(
                JwtClaimNames.AUD, aud -> aud != null && aud.contains("orders-api"));
        decoder.setJwtValidator(new DelegatingOAuth2TokenValidator<>(
                JwtValidators.createDefaultWithIssuer(ISSUER), audience));   // exp, nbf, iss, aud
        return decoder;
    }
}
```

Scopes say what the **client** may do on the user's behalf; they do not replace object-level
checks ("is order 42 owned by this user's tenant?"), which belong in the service.

### OAuth 2.0

OAuth 2.0 (RFC 6749) is a **delegated authorization** framework: it lets a client obtain a limited
access token to call an API on behalf of a resource owner, without seeing their password.

Roles: **resource owner** (user), **client** (the app), **authorization server** (issues tokens),
**resource server** (your API).

**Authorization code flow with PKCE** (RFC 7636): the default for web, mobile, and SPA clients.
OAuth 2.1 (in draft) makes PKCE mandatory for all clients and removes the implicit and password
grants.

```text
 Browser / app              Client                Authorization server        API
      │                       │                            │                    │
      │ 1. "Log in"           │                            │                    │
      │──────────────────────►│ generate code_verifier     │                    │
      │                       │ challenge = BASE64URL(SHA256(verifier))         │
      │ 2. redirect to /authorize?response_type=code&client_id=..&redirect_uri=..
      │    &scope=orders:read&state=xyz&code_challenge=..&code_challenge_method=S256
      │◄──────────────────────│                            │                    │
      │ 3. user logs in and consents ─────────────────────►│                    │
      │ 4. redirect to redirect_uri?code=AUTH_CODE&state=xyz                    │
      │◄───────────────────────────────────────────────────│                    │
      │──────────────────────►│ 5. check state             │                    │
      │                       │ POST /token (code, code_verifier)               │
      │                       │───────────────────────────►│ verify             │
      │                       │                            │ SHA256(verifier)   │
      │                       │◄───────────────────────────│ == challenge       │
      │                       │ 6. access_token (+ refresh_token, id_token)     │
      │                       │ 7. GET /orders  Authorization: Bearer <token> ─►│
      │                       │◄────────────────────────────────────────────────│
```

- **`state`** binds the callback to the session that started it (CSRF protection for the
  redirect).
- **PKCE** stops an attacker who intercepts the authorization code (a malicious app registered for
  the same custom URI scheme, logs, referrers) from redeeming it, because they lack the verifier.
- **Exact redirect URI matching** at the authorization server; open redirects here leak codes.

```java
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.util.Base64;

public record Pkce(String verifier, String challenge) {

    private static final SecureRandom RANDOM = new SecureRandom();

    public static Pkce generate() {
        byte[] bytes = new byte[32];                        // 43 chars once base64url-encoded
        RANDOM.nextBytes(bytes);
        String verifier = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
        try {
            byte[] hash = MessageDigest.getInstance("SHA-256")
                    .digest(verifier.getBytes(StandardCharsets.US_ASCII));
            String challenge = Base64.getUrlEncoder().withoutPadding().encodeToString(hash);
            return new Pkce(verifier, challenge);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}
```

**Client credentials flow**: machine-to-machine, no user. The client authenticates itself
(secret, or better `private_key_jwt` or mTLS) and receives a token for its own identity.

```http
POST /oauth2/token HTTP/1.1
Host: auth.example.com
Authorization: Basic b3JkZXJzLXN2YzpzM2NyM3Q=
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&scope=inventory:read
```

```json
{ "access_token": "eyJhbGciOi...", "token_type": "Bearer", "expires_in": 600,
  "scope": "inventory:read" }
```

Other grants: **refresh token** (get a new access token; rotate refresh tokens for public
clients), **device authorization** (RFC 8628, for TVs and CLIs: "go to example.com/device and enter
ABCD-EFGH"), **token exchange** (RFC 8693, for service-to-service delegation). The **implicit** and
**resource owner password** grants are deprecated; do not use them for new systems.

Sender-constrained tokens bind a token to a key so a stolen bearer token is useless: **mTLS**
(RFC 8705) or **DPoP** (RFC 9449). The OAuth 2.0 Security Best Current Practice (RFC 9700)
collects current guidance.

### OpenID Connect

OAuth answers "may this client access the API?"; it says nothing standard about **who the user
is**. OpenID Connect (OIDC) adds an identity layer on top:

- `scope=openid profile email` in the authorization request.
- The token response includes an **ID token**: a JWT for the **client** (audience = client ID)
  with `sub`, `iss`, `aud`, `exp`, `nonce`, `auth_time`, and profile claims.
- A **UserInfo** endpoint returns claims for an access token.
- **Discovery** at `/.well-known/openid-configuration` lists endpoints, `jwks_uri`, supported
  algorithms.
- `nonce` binds the ID token to the authentication request (replay protection).

Rule: the **ID token** is for the client to log the user in; the **access token** is for calling
APIs. APIs should accept access tokens with their own audience only.

### Scopes and permissions

- **Scopes** are coarse, consented capabilities granted to a client (`orders:read`,
  `payments:write`). Name them `resource:action`. Keep the list small and stable.
- **Permissions/roles** are what the user may do in your domain (admin of tenant 7).
- The effective permission is the **intersection**: the client has `orders:write` **and** the user
  may write **this** order.
- Fine-grained authorization (per object, per relationship) belongs in a policy layer: RBAC, ABAC,
  or relationship-based (Google Zanzibar style: OpenFGA, SpiceDB), or policy engines like OPA.

---

## API security: OWASP API Security Top 10 (2023)

| ID | Risk | What it looks like | Mitigation |
| --- | --- | --- | --- |
| API1 | Broken Object Level Authorization (BOLA) | `GET /orders/1002` returns another customer's order | Check ownership on every object access; opaque IDs help but are not a fix |
| API2 | Broken Authentication | Weak token validation, no brute-force protection, long-lived tokens | Standard libraries, validate all claims, rate limit login, MFA |
| API3 | Broken Object Property Level Authorization | Mass assignment (`"role": "admin"` in a PATCH), over-exposed fields | Explicit request/response DTOs; allow-list writable fields |
| API4 | Unrestricted Resource Consumption | No limits on page size, upload size, query cost, SMS sends | Rate limits, quotas, max sizes, timeouts, cost-based limits |
| API5 | Broken Function Level Authorization | Regular user calls `DELETE /admin/users/7` | Deny by default; role checks on every function |
| API6 | Unrestricted Access to Sensitive Business Flows | Bots buying all concert tickets, mass account creation | Business-flow limits, device fingerprinting, CAPTCHA, anomaly detection |
| API7 | Server Side Request Forgery (SSRF) | `"webhookUrl": "http://169.254.169.254/..."` | Allow-list destinations, block private ranges, resolve-then-check, egress proxy |
| API8 | Security Misconfiguration | Verbose errors, permissive CORS, missing TLS, default creds | Hardened defaults, automated config scanning |
| API9 | Improper Inventory Management | Forgotten `/v1-beta` or staging APIs exposed | API inventory, gateway-only exposure, retire old versions |
| API10 | Unsafe Consumption of APIs | Trusting third-party API data blindly | Validate and sanitise upstream responses; timeouts; TLS |

Additional basics: TLS everywhere (HSTS for browsers), input validation at the edge of the
service, output encoding, strict CORS (`Access-Control-Allow-Origin` allow-list, never reflect
arbitrary origins with credentials), request size limits, audit logs for sensitive actions, and
never logging secrets or full tokens.

**Rate limiting headers** belong in the API contract too: document limits per plan, return
`429` with `Retry-After`, and expose remaining budget (`RateLimit-Policy` / `RateLimit` per the
IETF draft, or the widely used `X-RateLimit-*`). See the Rate limiting page for algorithms.

---

## GraphQL in depth

GraphQL is a query language and runtime where the **client specifies the shape** of the response
against a typed schema, usually via a single `POST /graphql` endpoint.

### Schema

```graphql
type Query {
  book(id: ID!): Book
  books(first: Int = 20, after: String): BookConnection!
}

type Mutation {
  addReview(input: AddReviewInput!): AddReviewPayload!
}

type Book {
  id: ID!
  title: String!
  author: Author!
  reviews(first: Int = 10): [Review!]!
}

type Author {
  id: ID!
  name: String!
}

type Review {
  id: ID!
  rating: Int!
  text: String
}

type BookConnection {
  edges: [BookEdge!]!
  pageInfo: PageInfo!
}

type BookEdge {
  node: Book!
  cursor: String!
}

type PageInfo {
  hasNextPage: Boolean!
  endCursor: String
}

input AddReviewInput {
  bookId: ID!
  rating: Int!
  text: String
}

type AddReviewPayload {
  review: Review
  errors: [UserError!]!
}

type UserError {
  field: [String!]
  message: String!
}
```

```graphql
query BookPage($id: ID!) {
  book(id: $id) {
    title
    author { name }
    reviews(first: 3) { rating text }
  }
}
```

Key ideas:

- `!` means non-null. Nullability is part of the contract: a resolver error on a non-null field
  nulls the nearest nullable parent.
- **Connections** (Relay cursor spec) are the standard pagination shape.
- **Mutations return payload types** with user-facing errors as data; transport/system errors go
  in the top-level `errors` array.
- Responses are usually **HTTP 200 even when there are errors**, with `data` and `errors` side by
  side. Monitoring must look inside the body.
- **Evolution without versions**: add fields freely, mark old ones `@deprecated(reason: "...")`,
  and track field usage to know when to remove them.

### Resolvers and the N+1 problem

Each field has a resolver. A naive `Book.author` resolver runs once **per book**:

```text
query { books(first: 50) { title author { name } } }

SELECT * FROM books LIMIT 50;                     -- 1 query
SELECT * FROM authors WHERE id = 7;               -- 50 queries, one per book
SELECT * FROM authors WHERE id = 3;
...
```

**DataLoader** fixes this by **batching** and **caching per request**: resolvers call
`loader.load(authorId)`, which returns a future; at the end of the current execution level the
loader dispatches one batched call with all collected IDs.

```text
resolve Book.author × 50 ──► loader.load(7), load(3), load(7), ...   (futures)
end of level             ──► batch fn([7, 3, 12, ...])               (deduplicated)
                         ──► SELECT * FROM authors WHERE id IN (7, 3, 12, ...)   1 query
                         ──► complete each future with its author
```

Spring for GraphQL (on graphql-java) exposes this through `@BatchMapping`, which registers a
DataLoader for you:

```java
import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.stream.Collectors;
import org.springframework.graphql.data.method.annotation.Argument;
import org.springframework.graphql.data.method.annotation.BatchMapping;
import org.springframework.graphql.data.method.annotation.QueryMapping;
import org.springframework.stereotype.Controller;

@Controller
class BookGraphqlController {

    record Book(String id, String title, String authorId) {}
    record Author(String id, String name) {}

    private final BookRepository books;
    private final AuthorRepository authors;

    BookGraphqlController(BookRepository books, AuthorRepository authors) {
        this.books = books;
        this.authors = authors;
    }

    @QueryMapping
    Book book(@Argument String id) {
        return books.findById(id).orElse(null);
    }

    /** Called once per request level with all parent books, not once per book. */
    @BatchMapping
    Map<Book, Author> author(List<Book> bookList) {
        List<String> ids = bookList.stream().map(Book::authorId).distinct().toList();
        Map<String, Author> byId = authors.findAllById(ids).stream()
                .collect(Collectors.toMap(Author::id, Function.identity()));
        return bookList.stream()
                .collect(Collectors.toMap(Function.identity(), b -> byId.get(b.authorId())));
    }
}
```

The DataLoader cache is **per request**; never share it across users (authorization and
staleness).

### Query complexity limits

A client can write an expensive query; the server must bound it.

```graphql
query Evil {
  books(first: 100) {
    reviews(first: 100) {
      book { reviews(first: 100) { book { title } } }
    }
  }
}
```

Defences:

- **Max depth** (e.g. 10) and **max complexity/cost**: assign each field a cost, multiply by list
  arguments (`first`), reject before execution. graphql-java provides
  `MaxQueryDepthInstrumentation` and `MaxQueryComplexityInstrumentation`.
- **Mandatory pagination arguments** with maximums on every list field.
- **Timeouts** and per-client **cost-based rate limits** (GitHub's GraphQL API limits by computed
  points per hour).
- **Disable introspection** in production for private APIs, or at least do not rely on hiding it.
- Batching attacks: many operations in one HTTP request, or aliasing the same field 1,000 times
  (`a1: login(...) a2: login(...)`), bypass per-request rate limits; count operations and aliases.

### Persisted queries

Instead of sending query text, the client sends a hash of a query the server already knows.

- **Automatic persisted queries (APQ, Apollo)**: the client sends
  `{"extensions": {"persistedQuery": {"version": 1, "sha256Hash": "..."}}}`; on a miss the server
  answers `PersistedQueryNotFound` and the client retries with the full text once. Saves
  bandwidth and allows `GET` requests that CDNs can cache.
- **Trusted documents / allow-listed persisted queries**: queries are registered at build time and
  the server **rejects anything else**. This is the strongest protection for first-party APIs:
  arbitrary expensive queries become impossible.

### Other GraphQL considerations

- **Caching**: HTTP caching mostly does not apply to `POST /graphql`; clients use normalised
  caches keyed by `__typename` + `id`. Persisted queries over GET restore some CDN caching.
- **Authorization** must be per field/object in resolvers or the domain layer, not per endpoint.
- **Federation** (Apollo Federation, GraphQL Fusion) composes a supergraph from subgraphs owned
  by different teams, with a router in front.
- **Subscriptions** typically run over WebSockets (`graphql-ws` protocol) or SSE.

---

## gRPC in depth

gRPC is an RPC framework using **Protocol Buffers** for the schema and binary encoding, over
**HTTP/2**, with generated clients and servers in many languages.

### Protobuf contract

```protobuf
syntax = "proto3";

package inventory.v1;

option java_multiple_files = true;
option java_package = "com.example.inventory.v1";

import "google/protobuf/timestamp.proto";

service InventoryService {
  rpc GetStock(GetStockRequest) returns (Stock);                              // unary
  rpc WatchStock(WatchStockRequest) returns (stream StockEvent);              // server streaming
  rpc UploadCounts(stream StockCount) returns (UploadSummary);                // client streaming
  rpc Reconcile(stream ReconcileMessage) returns (stream ReconcileMessage);   // bidirectional
}

message GetStockRequest {
  string sku = 1;
  string warehouse_id = 2;
}

message Stock {
  reserved 5;                    // removed field: number may never be reused
  reserved "location";

  string sku = 1;
  int32 available = 2;
  int32 reserved_units = 3;
  google.protobuf.Timestamp updated_at = 4;
  StockStatus status = 6;
}

enum StockStatus {
  STOCK_STATUS_UNSPECIFIED = 0;  // proto3 enums must start at 0; use it as "unknown"
  STOCK_STATUS_IN_STOCK = 1;
  STOCK_STATUS_BACKORDERED = 2;
}

message WatchStockRequest { repeated string skus = 1; }
message StockEvent { Stock stock = 1; }
message StockCount { string sku = 1; int32 counted = 2; }
message UploadSummary { int32 accepted = 1; int32 rejected = 2; }
message ReconcileMessage { string sku = 1; int32 delta = 2; }
```

Why binary protobuf is fast: fields are encoded as `(field number, wire type)` tags followed by
values; integers are varints; unknown fields are skipped by tag. Field numbers 1 to 15 encode in
one byte, so give them to the most frequent fields. There is no field name on the wire, which is
why **field numbers are the contract**.

### Streaming types

| Type | Shape | Example |
| --- | --- | --- |
| Unary | 1 request, 1 response | `GetStock` |
| Server streaming | 1 request, many responses | Watch for changes, download large result |
| Client streaming | many requests, 1 response | Upload telemetry, bulk import |
| Bidirectional | independent streams both ways | Chat, sync protocols, interactive sessions |

All run over a single HTTP/2 stream with flow control, so backpressure is built in.

### Server and client in Java

```java
import io.grpc.Status;
import io.grpc.stub.StreamObserver;
import com.example.inventory.v1.*;

public final class InventoryServiceImpl extends InventoryServiceGrpc.InventoryServiceImplBase {

    private final StockRepository repo;

    public InventoryServiceImpl(StockRepository repo) {
        this.repo = repo;
    }

    @Override
    public void getStock(GetStockRequest request, StreamObserver<Stock> responseObserver) {
        if (request.getSku().isBlank()) {
            responseObserver.onError(Status.INVALID_ARGUMENT
                    .withDescription("sku is required").asRuntimeException());
            return;
        }
        repo.find(request.getSku(), request.getWarehouseId()).ifPresentOrElse(
                stock -> {
                    responseObserver.onNext(stock);
                    responseObserver.onCompleted();
                },
                () -> responseObserver.onError(Status.NOT_FOUND
                        .withDescription("unknown sku " + request.getSku()).asRuntimeException()));
    }
}
```

```java
import io.grpc.ManagedChannel;
import io.grpc.ManagedChannelBuilder;
import io.grpc.Status;
import io.grpc.StatusRuntimeException;
import java.util.concurrent.TimeUnit;
import com.example.inventory.v1.*;

ManagedChannel channel = ManagedChannelBuilder
        .forTarget("dns:///inventory.internal:9090")
        .defaultLoadBalancingPolicy("round_robin")   // per-RPC balancing across resolved IPs
        .useTransportSecurity()
        .build();

InventoryServiceGrpc.InventoryServiceBlockingStub stub = InventoryServiceGrpc.newBlockingStub(channel);

try {
    Stock stock = stub.withDeadlineAfter(200, TimeUnit.MILLISECONDS)
            .getStock(GetStockRequest.newBuilder().setSku("SKU-42").build());
} catch (StatusRuntimeException e) {
    if (e.getStatus().getCode() == Status.Code.DEADLINE_EXCEEDED) {
        // fall back, or fail the parent request
    }
}
```

### Deadlines

gRPC has **deadlines**, not just timeouts: an absolute point in time sent to the server (as the
`grpc-timeout` header). The server can check whether the client is still waiting and stop work
when it is not. In gRPC Java, the deadline lives in the `io.grpc.Context`, so outgoing calls made
from a server handler inherit the remaining time, which gives **deadline propagation** across
service hops for free. Always set one; the default is no deadline.

### Status codes

| Code | Meaning | Retry? |
| --- | --- | --- |
| `OK` (0) | Success | |
| `CANCELLED` (1) | Caller cancelled | No |
| `UNKNOWN` (2) | Unknown error (e.g. exception without status) | Maybe |
| `INVALID_ARGUMENT` (3) | Bad request regardless of state | No |
| `DEADLINE_EXCEEDED` (4) | Deadline passed | Only if idempotent |
| `NOT_FOUND` (5) | Entity not found | No |
| `ALREADY_EXISTS` (6) | Create conflict | No |
| `PERMISSION_DENIED` (7) | Authenticated, not allowed | No |
| `RESOURCE_EXHAUSTED` (8) | Quota/rate limit, out of resources | With backoff |
| `FAILED_PRECONDITION` (9) | System not in required state | No (fix state first) |
| `ABORTED` (10) | Concurrency conflict (transaction aborted) | Yes, at a higher level |
| `OUT_OF_RANGE` (11) | Past valid range (e.g. reading past EOF) | No |
| `UNIMPLEMENTED` (12) | Method not supported | No |
| `INTERNAL` (13) | Server invariant broken | No |
| `UNAVAILABLE` (14) | Transient, try again | Yes, with backoff |
| `DATA_LOSS` (15) | Unrecoverable data loss | No |
| `UNAUTHENTICATED` (16) | No valid credentials | No (refresh credentials) |

Rich error details go in `google.rpc.Status` details (`BadRequest`, `ErrorInfo`, `RetryInfo`)
per Google's error model.

### Compatibility rules

- **Never change or reuse a field number.** Mark removed fields `reserved` (numbers and names).
- Adding fields is safe; old readers skip unknown fields, old writers omit new ones (defaults).
- Renaming a field is wire-compatible (names are not on the wire) but breaks JSON mapping and
  generated code.
- Changing types is mostly breaking; a few are wire-compatible (`int32`/`int64`/`uint32`/`bool`)
  but can truncate. Do not rely on it.
- proto3 scalars have no presence by default (0 vs unset are indistinguishable); use `optional`
  or wrapper types when "not set" matters.
- Enums: always have `*_UNSPECIFIED = 0`; clients must handle unknown values.
- Version packages (`inventory.v1`, `inventory.v2`) for breaking changes. Tools like `buf
  breaking` catch violations in CI.

### gRPC in practice

- **Load balancing**: HTTP/2 multiplexes all calls over one long-lived connection, so an L4 load
  balancer pins a client to one backend. Use client-side balancing (`round_robin` with DNS
  returning all IPs; a headless Kubernetes Service), an L7 proxy (Envoy), or a service mesh.
- **gRPC-Web**: browsers cannot speak raw gRPC (no access to HTTP/2 framing and trailers), so
  gRPC-Web uses a modified protocol translated by a proxy (Envoy) or a server-side handler. It
  supports unary and server streaming only, not client or bidirectional streaming. The Connect
  protocol (from Buf) is another browser-friendly option compatible with gRPC.
- **gRPC-JSON transcoding** (`google.api.http` annotations, Envoy or grpc-gateway) exposes the
  same service as REST for external clients.
- Debugging needs tools (`grpcurl`, server reflection) because payloads are binary.

---

## REST vs GraphQL vs gRPC

| | REST | GraphQL | gRPC |
| --- | --- | --- | --- |
| Contract | OpenAPI (optional) | Schema (required, introspectable) | `.proto` (required) |
| Transport / encoding | HTTP/1.1 or 2, JSON | HTTP, JSON | HTTP/2, protobuf binary |
| Response shape | Fixed per endpoint | Chosen by client | Fixed per method |
| Over/under-fetching | Common (mitigated by fields/expand) | Solved by design | Designed per method |
| HTTP caching / CDN | Excellent | Poor (persisted GET helps) | None (custom) |
| Streaming | SSE, WebSockets bolted on | Subscriptions | Native, 4 types |
| Browser support | Native | Native | Needs gRPC-Web or Connect proxy |
| Errors | HTTP status + problem details | 200 + `errors` array | Status codes + details |
| Versioning | URI/header versions | Deprecate fields, evolve continuously | Field numbers, package versions |
| Tooling to debug | curl, browser | GraphiQL | grpcurl, reflection |
| Performance | Good | Good; resolver cost hard to predict | Best (binary, multiplexed) |
| Best fit | Public APIs, CRUD, cacheable resources | Rich UIs aggregating many entities, many client types | Internal service-to-service, low latency, streaming, polyglot |

A common architecture uses all three: gRPC between internal services, a GraphQL or REST BFF for
the apps, and REST for public partners.

---

## Real-time APIs

| Technique | Direction | Transport | Good for | Watch out for |
| --- | --- | --- | --- | --- |
| Short polling | Client pull | Plain HTTP | Rare updates, simplicity | Wasted requests, latency = interval |
| Long polling | Client pull, server holds | HTTP | Fallback when nothing else works | Connection churn, timeouts |
| Server-Sent Events | Server to client | HTTP, `text/event-stream` | Notifications, feeds, LLM token streaming | One-way; per-domain connection limit on HTTP/1.1 |
| WebSockets | Full duplex | Upgraded TCP connection | Chat, collaboration, games | Stateful scaling, proxies, auth |
| Webhooks | Server to server push | HTTP POST to client's URL | Integrations (payment succeeded) | Delivery guarantees, security |

### Long polling

The client sends `GET /events?after=cursor`; the server holds the request until there is data or
about 30 s pass, then responds; the client immediately reconnects. Always return a cursor so
nothing is missed between connections.

### Server-Sent Events

```http
GET /v1/orders/stream HTTP/1.1
Accept: text/event-stream
Last-Event-ID: 1041
```

```text
HTTP/1.1 200 OK
Content-Type: text/event-stream
Cache-Control: no-cache

retry: 5000
id: 1042
event: order.updated
data: {"orderId":"ord_42","status":"shipped"}

: heartbeat comment, keeps proxies from closing an idle connection

id: 1043
event: order.updated
data: {"orderId":"ord_43","status":"paid"}

```

- Browsers' `EventSource` reconnects automatically and sends `Last-Event-ID`; the server resumes
  from there, so event IDs should be cursors into a durable log.
- Plain HTTP: works through most proxies, HTTP/2 multiplexing removes the 6-connections-per-origin
  HTTP/1.1 limit. Disable proxy buffering (e.g. Nginx `X-Accel-Buffering: no`).
- `EventSource` cannot set custom headers, so auth is via cookies (or use `fetch` streaming).

In Spring MVC:

```java
import java.io.IOException;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

@RestController
class OrderStreamController {

    private final OrderEvents events;   // your pub/sub abstraction

    OrderStreamController(OrderEvents events) {
        this.events = events;
    }

    @GetMapping(path = "/v1/orders/stream", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    SseEmitter stream() {
        SseEmitter emitter = new SseEmitter(0L);   // no server-side timeout
        var subscription = events.subscribe(event -> {
            try {
                emitter.send(SseEmitter.event()
                        .id(Long.toString(event.sequence()))
                        .name("order.updated")
                        .data(event.payload()));
            } catch (IOException e) {
                emitter.completeWithError(e);       // client went away
            }
        });
        emitter.onCompletion(subscription::close);
        emitter.onTimeout(subscription::close);
        return emitter;
    }
}
```

### WebSockets

The client upgrades an HTTP/1.1 connection (`Upgrade: websocket`, `Sec-WebSocket-Key`; server
answers `101 Switching Protocols`), then both sides exchange frames (RFC 6455). HTTP/2 and HTTP/3
have their own bootstrapping (RFC 8441, RFC 9220).

Scaling notes:

- **Connections are stateful and long-lived.** A node holding 100k sockets is limited by memory
  and file descriptors, not CPU. Use an event-loop server (Netty, Node, Go).
- **Fan-out across nodes**: user A on node 1 sends to user B on node 7, so nodes need a pub/sub
  backplane (Redis Pub/Sub, NATS, Kafka) or a connection registry mapping users to nodes.
- **Load balancers**: idle timeouts (send ping/pong heartbeats every ~30 s), and least-connections
  balancing since connection counts, not request counts, matter.
- **Deploys** drop every connection on a node; clients must reconnect with jittered backoff and
  resume from a cursor, otherwise a deploy becomes a self-inflicted thundering herd.
- **Auth**: authenticate during the handshake (cookie or a short-lived ticket in the URL query,
  since browsers cannot set headers on `WebSocket`), and re-check authorization on each
  subscription. Check `Origin` to prevent cross-site WebSocket hijacking.
- **Backpressure**: slow consumers grow server-side buffers; cap them and disconnect.

### Webhooks

Your server POSTs events to a URL the customer registered.

```http
POST /hooks/payments HTTP/1.1
Host: customer.example.org
Content-Type: application/json
webhook-id: msg_2Kf9
webhook-timestamp: 1793448000
webhook-signature: v1,K5oZfzN95Z9UVu1EsfQmfVNQhnkZ2pj9o9NDN/H/pI4=

{"type":"payment.succeeded","data":{"id":"pay_77","amount":4999}}
```

This header layout follows the Standard Webhooks specification; Stripe uses a single
`Stripe-Signature: t=...,v1=...` header with the same idea.

Sender responsibilities:

- **Sign** each delivery: HMAC-SHA256 over `id.timestamp.body` with a per-endpoint secret.
- **Retry** with exponential backoff over hours to days (deliveries are **at least once**), then
  disable the endpoint and notify the owner.
- Short timeouts (5 to 10 s); treat any 2xx as success.
- Protect yourself from SSRF: validate URLs, block private IP ranges, send through an egress proxy.
- Provide an event log and **replay** in the dashboard, plus an API to list events so receivers
  can reconcile missed ones.

Receiver responsibilities:

- Verify the signature over the **raw body bytes** (before JSON parsing), with a constant-time
  comparison.
- Reject timestamps older than ~5 minutes (replay protection).
- **Deduplicate by event ID** (idempotent handling) because retries and duplicates will happen.
- Respond 2xx quickly and process asynchronously (queue), so slow processing does not trigger
  retries.
- Do not trust ordering; fetch the current state from the API if order matters.

```java
import java.nio.charset.StandardCharsets;
import java.security.GeneralSecurityException;
import java.security.MessageDigest;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

public final class WebhookVerifier {

    private static final Duration TOLERANCE = Duration.ofMinutes(5);
    private final byte[] secret;

    public WebhookVerifier(byte[] secret) {
        this.secret = secret.clone();
    }

    public boolean verify(String id, String timestamp, String signatureHeader, byte[] rawBody) {
        long ts;
        try {
            ts = Long.parseLong(timestamp);
        } catch (NumberFormatException e) {
            return false;
        }
        if (Duration.between(Instant.ofEpochSecond(ts), Instant.now()).abs().compareTo(TOLERANCE) > 0) {
            return false;                                   // stale or from the future: replay?
        }
        byte[] expected = hmac((id + "." + timestamp + ".").getBytes(StandardCharsets.UTF_8), rawBody);
        // The header may carry several space-separated signatures during secret rotation.
        for (String candidate : signatureHeader.split(" ")) {
            String[] parts = candidate.split(",", 2);
            if (parts.length == 2 && parts[0].equals("v1")) {
                byte[] given;
                try {
                    given = Base64.getDecoder().decode(parts[1]);
                } catch (IllegalArgumentException e) {
                    continue;
                }
                if (MessageDigest.isEqual(expected, given)) {
                    return true;
                }
            }
        }
        return false;
    }

    private byte[] hmac(byte[] prefix, byte[] body) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret, "HmacSHA256"));
            mac.update(prefix);
            return mac.doFinal(body);
        } catch (GeneralSecurityException e) {
            throw new IllegalStateException(e);
        }
    }
}
```

---

## OpenAPI: spec-first design

OpenAPI describes HTTP APIs in YAML or JSON. **Spec-first** (design the contract, review it, then
generate server stubs, clients, docs, and mocks) catches design mistakes before code exists and
lets frontend and backend work in parallel. OpenAPI 3.1 aligned its schemas with JSON Schema
2020-12 (e.g. `type: [string, "null"]` instead of `nullable: true`). OpenAPI 3.2 was published in
2025; check that your tooling supports it before adopting.

```yaml
openapi: 3.1.0
info:
  title: Orders API
  version: 1.4.0
servers:
  - url: https://api.example.com/v1
security:
  - oauth: [orders:read]

paths:
  /orders:
    get:
      operationId: listOrders
      parameters:
        - name: status
          in: query
          schema: { $ref: '#/components/schemas/OrderStatus' }
        - name: limit
          in: query
          schema: { type: integer, minimum: 1, maximum: 100, default: 20 }
        - name: cursor
          in: query
          schema: { type: string }
      responses:
        '200':
          description: A page of orders
          content:
            application/json:
              schema: { $ref: '#/components/schemas/OrderPage' }
        '400': { $ref: '#/components/responses/Problem' }
    post:
      operationId: createOrder
      security:
        - oauth: [orders:write]
      parameters:
        - name: Idempotency-Key
          in: header
          required: true
          schema: { type: string, maxLength: 255 }
      requestBody:
        required: true
        content:
          application/json:
            schema: { $ref: '#/components/schemas/CreateOrder' }
      responses:
        '201':
          description: Created
          headers:
            Location:
              schema: { type: string }
          content:
            application/json:
              schema: { $ref: '#/components/schemas/Order' }
        '409': { $ref: '#/components/responses/Problem' }
        '422': { $ref: '#/components/responses/Problem' }
        '429': { $ref: '#/components/responses/Problem' }

components:
  schemas:
    OrderStatus:
      type: string
      enum: [pending, paid, shipped, cancelled]
      description: New values may be added; clients must tolerate unknown values.
    CreateOrder:
      type: object
      required: [customerId, items]
      properties:
        customerId: { type: string }
        items:
          type: array
          minItems: 1
          maxItems: 100
          items:
            type: object
            required: [sku, quantity]
            properties:
              sku: { type: string }
              quantity: { type: integer, minimum: 1 }
    Order:
      type: object
      required: [id, status, createdAt]
      properties:
        id: { type: string, examples: [ord_8f3k2] }
        status: { $ref: '#/components/schemas/OrderStatus' }
        createdAt: { type: string, format: date-time }
        note: { type: [string, 'null'] }
    OrderPage:
      type: object
      required: [data, pagination]
      properties:
        data:
          type: array
          items: { $ref: '#/components/schemas/Order' }
        pagination:
          type: object
          properties:
            nextCursor: { type: [string, 'null'] }
            hasMore: { type: boolean }
    Problem:
      type: object
      properties:
        type: { type: string, format: uri }
        title: { type: string }
        status: { type: integer }
        detail: { type: string }
        instance: { type: string }

  responses:
    Problem:
      description: Error (RFC 9457 problem details)
      content:
        application/problem+json:
          schema: { $ref: '#/components/schemas/Problem' }

  securitySchemes:
    oauth:
      type: oauth2
      flows:
        clientCredentials:
          tokenUrl: https://auth.example.com/oauth2/token
          scopes:
            orders:read: Read orders
            orders:write: Create and modify orders
```

Tooling around the spec: linting with style rules (Spectral), breaking-change detection in CI
(oasdiff, openapi-diff), code generation (OpenAPI Generator), mock servers (Prism), contract
testing against the running service, and rendered docs (Redoc, Swagger UI).

---

## API gateways

An API gateway is the single entry point that applies cross-cutting policy before traffic reaches
services.

| Responsibility | Examples |
| --- | --- |
| TLS termination, HTTP/2, compression | Every gateway |
| Authentication | API key lookup, JWT validation, OAuth token introspection |
| Rate limiting and quotas | Per key, per plan |
| Routing and versioning | `/v1/orders` → orders-v1, canary weights |
| Request/response transformation | Header injection, REST-to-gRPC transcoding |
| Observability | Access logs, metrics, trace header injection |
| Developer portal and key management | Apigee, Kong, AWS API Gateway, Azure API Management |

Products: Kong, Envoy-based gateways (Envoy Gateway, Gloo, Emissary), AWS API Gateway, Google
Apigee, Azure API Management, Spring Cloud Gateway, Tyk, Traefik.

Guidelines: keep **business logic out** of the gateway (it becomes a shared, hard-to-change
monolith); treat its configuration as code; avoid making it a single point of failure (run it
horizontally scaled across zones); and do not rely on it alone for authorization, since services
must still check object-level permissions. A **Backend for Frontend** (BFF) is a client-specific
API layer (one for mobile, one for web) that aggregates and shapes data; it differs from a gateway
in that it owns presentation-oriented logic.

---

## Documentation and developer experience

The API's quality is judged by the first 30 minutes of a developer's experience.

- **Reference docs generated from the spec** plus hand-written guides: quick start, auth,
  pagination, errors, idempotency, webhooks, rate limits.
- **Copy-pasteable examples** in curl and the main languages; examples that actually run against a
  sandbox.
- **Test mode / sandbox** with test keys and test card numbers (Stripe's `sk_test_` model).
- **Consistency** beats cleverness: same pagination, errors, naming, timestamps (RFC 3339 UTC),
  money (integer minor units + ISO 4217 currency) everywhere.
- **Request IDs** in every response (`Request-Id` header) and in error bodies, searchable by
  support.
- **Changelog**, versioning policy, deprecation policy, status page.
- **SDKs** that handle retries with backoff, idempotency keys, pagination iterators, and typed
  errors, so clients do the right thing by default.
- **Design reviews** against a written style guide (Google AIPs, Microsoft REST API Guidelines,
  Zalando RESTful API Guidelines are good starting points), enforced by linting.

---

## Common mistakes

- Verbs in URLs and inconsistent naming, casing, and pagination across endpoints.
- 200 OK with an error body; 500 for validation errors; leaking stack traces.
- Unbounded list endpoints; deep offset pagination on large tables.
- Non-idempotent `POST` for payments or orders without idempotency keys.
- Lost updates because writes ignore versions (no `If-Match`, no `WHERE version = ?`).
- Exposing database entities directly, causing mass assignment and over-exposure (API3).
- Checking authentication but not object-level authorization (BOLA, API1).
- JWT verification that trusts the `alg` header, skips `aud`, or uses ID tokens as access
  tokens.
- API keys in query strings; storing keys in plaintext.
- Breaking changes without a version bump (renaming a field, adding a required parameter).
- Enums without an "unknown" strategy, so adding a value breaks clients.
- Webhooks without signatures, retries, or deduplication.
- gRPC without deadlines; GraphQL without depth/complexity limits or DataLoader.
- Floats for money; local times without zones.

---

## Interview questions

1. **What is the difference between safe and idempotent methods? Is PATCH idempotent?**
   Safe methods do not request state changes (GET, HEAD, OPTIONS). Idempotent methods have the
   same effect when repeated (all safe methods plus PUT and DELETE). PATCH is not guaranteed
   idempotent: a merge patch setting fields is, a JSON Patch appending to an array is not.

2. **How do you prevent duplicate payments when a client retries a POST?**
   Idempotency keys: the client sends a unique key; the server atomically claims it (unique
   constraint), executes, and stores the response in the same transaction as the side effects;
   retries replay the stored response; concurrent duplicates get 409; a different body with the
   same key gets 422. Scope keys per client, expire after ~24 h, pass keys to downstream providers.

3. **How do you prevent lost updates in a REST API?**
   Optimistic concurrency: return an ETag (version) on GET, require `If-Match` on PUT/PATCH,
   update with `WHERE version = ?`, return 412 on mismatch and 428 if the header is missing.

4. **Offset vs cursor pagination?**
   Offset is simple and supports page jumps but is O(offset) and unstable under inserts. Keyset
   uses `WHERE (sort_key, id) < (last values)` with a matching index: O(page) at any depth and
   stable, but no random access. Encode cursors opaquely and sign them.

5. **How would you version a public API?**
   Prefer additive, backward-compatible changes and tolerant readers so versions are rare. When
   needed, URI major versions or date-based versions pinned per account (Stripe) with
   transformation layers. Announce deprecation with `Deprecation`/`Sunset` headers, track usage,
   brown out, then 410.

6. **401 vs 403? When return 404 instead of 403?**
   401: not authenticated (missing/invalid credentials). 403: authenticated but not permitted.
   Return 404 when revealing the resource's existence would leak information.

7. **Walk through the OAuth authorization code flow with PKCE. Why PKCE?**
   Client generates a verifier, sends its SHA-256 challenge with the authorize request; the user
   authenticates and consents; the client receives a code at its redirect URI, checks `state`,
   and exchanges code + verifier for tokens. PKCE ensures an intercepted code is useless without
   the verifier, which matters for public clients without secrets.

8. **What are the main JWT pitfalls?**
   Trusting the `alg` header (none, RS/HS confusion), not checking `iss`/`aud`/`exp`, putting
   secrets in the readable payload, long lifetimes without revocation, storing tokens in
   `localStorage`, using ID tokens for API calls. Mitigate with pinned algorithms, JWKS with `kid`,
   short-lived tokens plus rotating refresh tokens.

9. **What is BOLA and how do you prevent it?**
   Broken Object Level Authorization: the API trusts an object ID from the client without checking
   the caller may access that object. Check ownership/tenancy on every access, ideally in a central
   data-access layer; opaque IDs reduce enumeration but are not a fix.

10. **What is the N+1 problem in GraphQL and how does DataLoader solve it?**
    Per-item resolvers issue one query per parent. DataLoader collects keys requested during one
    execution level, then issues one batched query, deduplicates, and caches per request.

11. **How do you protect a GraphQL API from expensive queries?**
    Depth and cost limits computed before execution, mandatory pagination maxima, timeouts,
    cost-based rate limits, alias/batch limits, and for first-party clients trusted persisted
    queries that reject anything else.

12. **When would you choose gRPC over REST? What breaks with gRPC on Kubernetes?**
    Internal, latency-sensitive, polyglot service-to-service calls and streaming. HTTP/2
    connections are long-lived, so L4 load balancing pins clients to one pod; use client-side
    round-robin with a headless Service, an L7 proxy, or a mesh. Always set deadlines.

13. **SSE vs WebSockets?**
    SSE is one-way server push over plain HTTP with automatic reconnection and `Last-Event-ID`
    resume, ideal for notifications and streaming responses. WebSockets are full duplex for
    interactive use (chat, collaboration) but need a pub/sub backplane, heartbeats, and careful
    auth and reconnect handling.

14. **How do you design reliable webhooks?**
    HMAC signatures with timestamps, at-least-once delivery with exponential backoff, per-event IDs
    for deduplication, fast 2xx acknowledgements with async processing on the receiver, replay
    tooling, SSRF protection on the sender.

15. **How do you design a long-running operation?**
    Return 202 with a `Location` to an operation resource that has a state machine and result or
    error; support polling with `Retry-After`, webhooks on completion, cancellation, and an
    idempotent initiating request.

---

## Summary

- Model **resources** with nouns, use HTTP methods by their semantics (safe, idempotent), and
  return precise status codes; 4xx is the client's problem, 5xx is yours.
- **ETags** give you cheap revalidation (`If-None-Match` → 304) and optimistic concurrency
  (`If-Match` → 412). Cache-Control directives are part of the contract.
- **RFC 9457 problem details** for every error, with stable `type` URIs and a request ID.
- **Keyset pagination** with signed opaque cursors for anything large or changing.
- **Idempotency keys** for unsafe operations: claim atomically, commit result with side effects,
  replay, 409 for concurrent, 422 for mismatched reuse.
- Evolve **additively**; version rarely; deprecate with `Deprecation` and `Sunset`; tolerate
  unknown fields and enum values.
- **Auth**: hashed, prefixed API keys for apps; `HttpOnly`/`SameSite` session cookies with CSRF
  defences for first-party web; short-lived JWT access tokens with strict validation; OAuth 2.0
  auth code + PKCE for users, client credentials for machines; OIDC for identity.
- Security is mostly **authorization**: check object and property level access on every request
  (OWASP API Top 10).
- **GraphQL**: flexible reads; needs DataLoader, cost limits, persisted queries. **gRPC**: fast
  typed internal RPC; needs deadlines, L7 balancing, field-number discipline.
- Real-time: SSE for server push, WebSockets for duplex, webhooks for integrations (signed,
  retried, deduplicated).
- Spec-first OpenAPI, lint and diff in CI, and invest in docs, sandboxes, and SDKs.

---

## Further reading

- Roy T. Fielding, "Architectural Styles and the Design of Network-based Software Architectures"
  (PhD dissertation, 2000), chapter 5.
- RFC 9110 "HTTP Semantics", RFC 9111 "HTTP Caching", RFC 5861 (stale-while-revalidate),
  RFC 8288 "Web Linking".
- RFC 9457 "Problem Details for HTTP APIs".
- RFC 8594 "The Sunset HTTP Header Field" and RFC 9745 "The Deprecation HTTP Response Header
  Field".
- IETF draft "The Idempotency-Key HTTP Header Field" (draft-ietf-httpapi-idempotency-key-header).
- Brandur Leach, "Implementing Stripe-like Idempotency Keys in Postgres".
- RFC 6749 (OAuth 2.0), RFC 6750 (Bearer tokens), RFC 7636 (PKCE), RFC 8628 (device grant),
  RFC 7662 (introspection), RFC 9449 (DPoP), RFC 9700 (OAuth 2.0 Security BCP), the OAuth 2.1
  draft.
- RFC 7519 (JWT), RFC 7515 (JWS), RFC 7517 (JWK), RFC 8725 (JWT Best Current Practices).
- OpenID Connect Core 1.0 and OpenID Connect Discovery 1.0.
- OWASP API Security Top 10 (2023 edition).
- GraphQL specification (spec.graphql.org), GraphQL Foundation documentation, DataLoader
  repository, Relay Cursor Connections specification.
- gRPC documentation (grpc.io), Protocol Buffers language guide (proto3), Google API error model.
- OpenAPI Specification 3.1 (and 3.2) from the OpenAPI Initiative.
- Google API Improvement Proposals (aip.dev), Microsoft REST API Guidelines, Zalando RESTful API
  Guidelines.
- RFC 6455 "The WebSocket Protocol"; WHATWG HTML Standard, "Server-sent events"; Standard Webhooks
  specification.
- JJ Geewax, *API Design Patterns* (Manning).
- Brenda Jin, Saurabh Sahni, Amir Shevat, *Designing Web APIs* (O'Reilly).
- Arnaud Lauret, *The Design of Web APIs* (Manning).
