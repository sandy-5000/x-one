---
title: "API design"
order: 9
---
# API design

## Key points

- An API is a **long-lived contract** — design for retries, concurrency, pagination, evolution, and auth failures, not only happy paths.
- REST level 2: **resources + HTTP verbs + status codes**; errors use **RFC 9457 problem details**, not `200` + error JSON.
- **Safe** methods don't change state; **idempotent** methods (PUT, DELETE) safe to retry — **POST is not** → **idempotency keys** for payments and creates.
- **Keyset pagination** + signed opaque cursors for large lists; offset only for small/admin UIs.
- **Version rarely** — additive changes + tolerant readers; **Deprecation** / **Sunset** headers when removing.
- Security failures are mostly **authorization** (BOLA), not crypto.

---

## REST fundamentals

Resources at URIs; method = action.

| Level (Richardson) | Characteristic |
| --- | --- |
| 0 | Single RPC endpoint |
| 1 | Multiple resources |
| 2 | Verbs + status codes |
| 3 | HATEOAS links |

### URI design

- Nouns: `/orders/42`, not `/getOrder`.
- Plural collections; shallow nesting (`/customers/7/orders` OK; prefer `/orders?customer=7` for deep graphs).
- **Opaque IDs** (`ord_…`, UUID) vs sequential (enumeration).
- Non-CRUD actions: `POST /orders/42/cancellation` or `POST /orders/42:cancel`.
- Filters/sort/page in **query string**, not path identity.

### HTTP methods

| Method | Safe | Idempotent | Typical success |
| --- | --- | --- | --- |
| GET, HEAD | Yes | Yes | 200, 304 |
| PUT | No | Yes | 200, 201, 204 |
| DELETE | No | Yes | 204 |
| POST | No | **No** | 201, 200, 202 |
| PATCH | No | **Not always** | 200, 204 |

Proxies and clients **retry idempotent** requests after timeouts. JSON Patch `add` to array ≠ idempotent; merge patch `{"status":"shipped"}` often is.

### Status codes (essential)

| Code | Use |
| --- | --- |
| 200 / 201 / 204 | Success variants |
| 202 | Accepted — async work (see operations) |
| 304 | Conditional GET unchanged |
| 400 | Malformed |
| 401 | Unauthenticated (`WWW-Authenticate`) |
| 403 | Authenticated, forbidden (or 404 to hide existence) |
| 404 | Not found |
| 409 | Conflict (duplicate, idempotency in progress) |
| 412 | `If-Match` failed — optimistic concurrency |
| 422 | Semantic validation |
| 428 | Precondition required — force `If-Match` |
| 429 | Rate limited — `Retry-After` |
| 5xx | Server must fix |

**Rule**: 4xx = client can fix; 5xx = server; never **200 with error body**.

### Conditional requests and ETags

**ETag** = representation validator (often version number).

```text
GET /orders/42           → 200, ETag: "7"
GET … If-None-Match: "7" → 304 (cache revalidation)

PUT … If-Match: "7"      → 200 if version still 7
PUT … If-Match: "7"      → 412 if Alice already wrote version 8 (lost update prevented)
```

Server: `UPDATE … SET …, version = version + 1 WHERE id = ? AND version = ?`.

### Caching (sketch)

| Directive | Meaning |
| --- | --- |
| `private, no-cache` + ETag | Per-user; revalidate each use |
| `public, max-age, s-maxage` | CDN-friendly catalogue |
| `no-store` | Secrets, PII |
| `stale-while-revalidate` | Serve stale briefly while refreshing |

---

## Error model: RFC 9457 Problem Details

Media type: `application/problem+json`.

```json
{
  "type": "https://api.example.com/problems/validation-error",
  "title": "Your request is not valid.",
  "status": 422,
  "detail": "2 fields failed validation.",
  "instance": "/v1/orders/ord_42",
  "requestId": "req_7f3b9c",
  "errors": [
    { "pointer": "#/email", "detail": "must be a valid email address" }
  ]
}
```

| Field | Role |
| --- | --- |
| `type` | Stable URI — clients branch here, not on `detail` text |
| `title` | Short type summary |
| `status` | Copy of HTTP status |
| `detail` | This occurrence |
| `instance` | This request/resource |
| extensions | `errors[]`, `requestId`, domain fields |

Never expose stack traces, SQL, internal hostnames. Include **request ID** in header and body for support.

---

## Pagination

Never unbounded lists; default + **max** page size.

### Offset

```text
GET /posts?limit=20&offset=40
→ LIMIT 20 OFFSET 40
```

| Pros | Cons |
| --- | --- |
| Simple, jump to page N | O(offset) deep pages |
| | Unstable under inserts (skip/duplicate) |
| | Expensive `COUNT(*)` |

### Keyset (cursor)

Sort on **unique total order** — tiebreak with primary key:

```sql
-- after (created_at, id) = (:c, :i)
WHERE (created_at, id) < (:c, :i)
ORDER BY created_at DESC, id DESC
LIMIT :n + 1   -- extra row → hasMore
```

Index: `(author_id, created_at DESC, id DESC)`.

**Opaque signed cursor** — don't expose raw ids clients can tamper with; bind cursor to filter/sort version (`v1|…` prefix for format migration).

Response:

```json
{
  "data": [ … ],
  "pagination": { "nextCursor": "…", "hasMore": true }
}
```

Alternative: `Link: <…?cursor=…>; rel="next"` (RFC 8288).

| | Offset | Keyset |
| --- | --- | --- |
| Deep page cost | O(offset) | O(limit) |
| Jump to page N | Yes | No |
| Stable under writes | No | Yes (for sort key) |
| Public feeds / sync | Poor | Preferred |

For exports: **changes feed** (`updated_since` + cursor), not paging a mutating list forever.

---

## Idempotency keys

Problem: `POST /payments` succeeds, response lost → client retries → **double charge**.

```http
POST /v1/payments
Idempotency-Key: 5f2b1e6c-3c41-4c3b-9b2e-7a0d1f4e8c11
```

| Situation | Response |
| --- | --- |
| New key | Execute, store response, return |
| Same key, same body, completed | **Replay** stored status + body |
| Same key, in progress | **409** Conflict |
| Same key, **different** body | **422** key misuse |
| Required endpoint, missing key | **400** |

### Implementation essentials

- Scope keys per **tenant / API key**.
- **Request hash** (method + path + canonical body) stored with key.
- **Claim**: `INSERT … ON CONFLICT DO NOTHING` — one winner runs business logic.
- **Complete** in **same transaction** as side effects + store `response_code`, `response_body`.
- **Lease** on `IN_PROGRESS` — crashed owner expires; new owner takes over with compare-and-set on lock token.
- Store **4xx** deterministic errors; **don't** store 5xx — delete claim so retry can re-execute.
- **Retention** ~24 h; pass key to **payment provider** idempotency.
- Alternative: `PUT` with client-chosen resource id.

```text
A: INSERT key IN_PROGRESS → runs → COMPLETED + response
B: INSERT fails → SELECT → IN_PROGRESS → 409
B: retry later → SELECT COMPLETED → replay 201
```

External card network calls can't join DB txn — use provider idempotency + **recovery points** for multi-step flows.

---

## Versioning and evolution

### Strategies

| Strategy | Example | Pros | Cons |
| --- | --- | --- | --- |
| URI path | `/v1/orders` | Obvious, cache-friendly | Coarse bump |
| Header | `X-Api-Version: 2022-11-28` | Clean URLs | Less visible |
| Media type | `Accept: application/vnd…+json` | Per-resource | Awkward caching |
| Date pinned | `Stripe-Version: 2024-06-20` | Account pinned; transform chain | Ops complexity |

Major version = **years** of parallel operation. Prefer needing new versions **rarely**.

### Safe vs breaking changes

**Safe (additive)**

- New optional request/response fields.
- New endpoints; new optional query params with old default.
- New enum values **if** clients tolerate unknown values (document from day one).
- Relax validation.

**Breaking**

- Remove/rename fields or endpoints; change types or meaning.
- New **required** request fields; tighter validation.
- Change defaults, sort order, error codes clients branch on.

**Tolerant reader** (Postel): ignore unknown fields; don't fail on new enum values.

**Expand–contract**: add new field → migrate clients → remove old in next major.

### Deprecation

```http
Deprecation: @1798761600
Sunset: Sat, 01 May 2027 00:00:00 GMT
Link: <https://developer.example.com/migrations/v2>; rel="deprecation"
```

Track usage per client; brownouts (short 410 windows); final **410 Gone** with migration link.

---

## Filtering, sorting, bulk, long-running work

```text
GET /orders?status=paid&sort=-created_at,id&fields=id,status,total
```

- **Allow-list** sort fields — `ORDER BY` isn't parameterisable (injection + unindexed sorts).
- **Batch**: partial success → 200 with per-item status (or 207); document max batch size; idempotency on whole batch or per item.
- **Long-running**: `POST` → **202** + `Location: /operations/op_77` + `Retry-After`; poll state machine `queued → running → succeeded | failed`; webhooks optional; initiating POST **idempotent**.

---

## Authentication and authorization (summary)

| Mechanism | Use |
| --- | --- |
| API keys | Machine/app identity; header only; store **hash**; prefix `sk_live_`; rotation |
| Session cookie | First-party web; `HttpOnly`, `Secure`, `SameSite`; CSRF defences |
| JWT access token | Stateless API; validate iss, aud, exp, **pinned alg**; short TTL |
| OAuth 2.0 auth code + **PKCE** | User delegation to client |
| Client credentials | Service-to-service |
| OIDC | ID token for login; **access token** for API |

**JWT pitfalls**: `alg:none`, RS/HS confusion, ID token as API token, secrets in payload, `localStorage` storage, no revocation → short TTL + refresh rotation.

**Scopes** = what client may do; **object checks** = may this user touch **this** order? (BOLA).

---

## OWASP API Security Top 10 (2023) — highlights

| ID | Risk | Mitigation |
| --- | --- | --- |
| API1 BOLA | `GET /orders/1002` another tenant's order | Check ownership every access |
| API2 Broken auth | Weak token validation | Standard libs, MFA, rate limit login |
| API3 Property auth | Mass assignment in PATCH | Explicit DTOs, allow-list writes |
| API4 Resource consumption | Huge page size, GraphQL cost | Limits, quotas, timeouts |
| API5 Function auth | User hits admin route | Deny by default, role on every function |
| API7 SSRF | Webhook to `169.254.169.254` | Allow-list, block private ranges |

Also: TLS, strict CORS, no secrets in logs, rate limit headers documented.

---

## GraphQL, gRPC, real-time (when to use)

| | REST | GraphQL | gRPC |
| --- | --- | --- | --- |
| Contract | OpenAPI | Schema required | `.proto` required |
| Shape | Fixed per route | Client-selected | Fixed per RPC |
| Caching/CDN | Strong | Weak (POST) | Custom |
| Fit | Public CRUD, cacheable | Aggregating UIs | Internal low-latency RPC |

**GraphQL**: N+1 → **DataLoader** batch per request level; **depth/cost limits**; persisted/trusted queries for first-party; HTTP **200 + errors[]** — monitor body.

**gRPC**: HTTP/2 protobuf; **deadlines** propagate; **never reuse field numbers**; L4 LB pins connections — client-side LB or mesh; **RESOURCE_EXHAUSTED** ≈ rate limit.

**Real-time**: SSE for server push + `Last-Event-ID`; WebSockets for duplex (pub/sub backplane, heartbeats); webhooks for integrations — **HMAC**, retries, dedupe by event id.

---

## OpenAPI and gateways

**Spec-first**: lint (Spectral), breaking diff in CI, codegen, mocks, contract tests.

Gateway: TLS, auth, rate limits, routing — **not** business rules or sole authorization. BFF aggregates for specific clients.

DX: sandbox keys, consistent pagination/errors, RFC 3339 UTC, money as **integer minor units + currency**, request IDs, changelog, SDKs with retries + idempotency helpers.

---

## Pitfalls

- Verbs in URLs; inconsistent pagination/errors across endpoints.
- 200 + error JSON; 500 for validation.
- Unbounded lists; deep offset on large tables.
- Non-idempotent POST for money/orders without idempotency keys.
- Writes without ETag / version column (lost updates).
- Entity exposure → mass assignment (API3).
- Auth without object-level checks (BOLA).
- JWT: trust `alg`, skip `aud`, ID token as access token.
- API keys in query strings; plaintext storage at rest.
- Breaking rename without version bump.
- Enums without unknown-value strategy.
- Webhooks without signature or deduplication.
- GraphQL without cost limits / DataLoader; gRPC without deadlines.
- Floats for money.

---

## Quick check

1. **Safe vs idempotent** — Is PATCH always idempotent? Why does POST need idempotency keys?
2. **412 vs 409 vs 422** — Optimistic concurrency, in-progress idempotency, validation — which status when?
3. **Offset vs keyset** — Name two failures of offset pagination at scale.
4. **Idempotency key reuse** — Same key, different body — what should happen and why fingerprint the request?
5. **Versioning** — Name three additive changes and three breaking changes; what headers announce retirement?
