---
title: "API design"
order: 9
---
# API Design

## REST basics

Model the API around **resources** (nouns) and use HTTP methods as verbs.

| Method | Path | Meaning | Idempotent |
| --- | --- | --- | --- |
| GET | `/users` | List users | Yes |
| GET | `/users/42` | Get one user | Yes |
| POST | `/users` | Create a user | No |
| PUT | `/users/42` | Replace user 42 entirely | Yes |
| PATCH | `/users/42` | Update some fields | Not necessarily |
| DELETE | `/users/42` | Delete user 42 | Yes |
| GET | `/users/42/orders` | Orders of user 42 (sub-resource) | Yes |

Guidelines:

- Plural nouns, lowercase, hyphens: `/order-items`, not `/getOrderItems`.
- Nest only one level deep; beyond that use query filters: `/orders?userId=42&status=paid`.
- Use JSON with consistent casing (`camelCase` or `snake_case`, pick one).
- Return the created resource and a `Location` header from `POST`.

## Status codes

| Code | When |
| --- | --- |
| 200 OK | Successful read or update |
| 201 Created | Resource created |
| 204 No Content | Success with empty body (DELETE) |
| 400 Bad Request | Malformed input |
| 401 Unauthorized | Not authenticated |
| 403 Forbidden | Authenticated but not allowed |
| 404 Not Found | Resource doesn't exist |
| 409 Conflict | Version conflict, duplicate |
| 422 Unprocessable Entity | Valid JSON, failed validation |
| 429 Too Many Requests | Rate limited |
| 500 Internal Server Error | Bug on the server |
| 503 Service Unavailable | Overloaded or down for maintenance |

### Consistent error body

```json
{
  "error": {
    "code": "VALIDATION_FAILED",
    "message": "email is not valid",
    "field": "email",
    "requestId": "c0a8012e-7f3b"
  }
}
```

---

## Pagination

### Offset pagination

```text
GET /posts?limit=20&offset=40
```

Simple and supports "jump to page 5", but slow for large offsets (the DB still scans skipped rows) and items shift if new rows are inserted.

### Cursor (keyset) pagination

```text
GET /posts?limit=20
→ { "data": [...], "nextCursor": "eyJpZCI6MTIzNH0" }

GET /posts?limit=20&cursor=eyJpZCI6MTIzNH0
```

```sql
SELECT * FROM posts
WHERE id < :lastSeenId
ORDER BY id DESC
LIMIT 20;
```

Fast at any depth and stable under inserts. Used by most feeds (Twitter, Slack, Stripe).

---

## Idempotency keys

Networks retry. If a client retries `POST /payments` after a timeout, the customer must not be charged twice.

```text
POST /payments
Idempotency-Key: 7b1e2c9a-5d4f-4e1a-9f0c-2d6a8b3e1f00
{ "amount": 4999, "currency": "INR" }
```

Server side:

1. Look up the key in a store.
2. If found, return the saved response — don't execute again.
3. If not, process the request, then save `key → response` with a TTL (e.g. 24 h).

GET, PUT, and DELETE are idempotent by definition; POST and PATCH need keys when duplicates matter.

---

## Versioning

| Style | Example | Notes |
| --- | --- | --- |
| URL path | `/v1/users` | Most common, visible, easy to route |
| Header | `Accept: application/vnd.app.v2+json` | Clean URLs, harder to test in a browser |
| Query param | `/users?version=2` | Easy, but easy to forget |

Avoid breaking changes in the first place: add fields instead of renaming, never change a field's type, and keep old versions alive with a published deprecation date.

---

## Authentication

- **API keys** — simple, for server-to-server; send in a header, never in the URL.
- **Session cookies** — classic web apps; server stores the session.
- **JWT (JSON Web Token)** — signed token the client sends as `Authorization: Bearer <token>`; servers verify the signature without a DB lookup. Keep them short-lived and use refresh tokens.
- **OAuth 2.0 / OpenID Connect** — delegated login ("Sign in with Google") and third-party access.

Always use HTTPS.

---

## REST vs GraphQL vs gRPC

| | REST | GraphQL | gRPC |
| --- | --- | --- | --- |
| Transport | HTTP/1.1+, JSON | HTTP, JSON | HTTP/2, Protocol Buffers (binary) |
| Shape of response | Fixed per endpoint | Client asks for exact fields | Fixed by `.proto` contract |
| Over/under-fetching | Common | Solved | N/A |
| Caching | Easy (HTTP caching) | Harder (single POST endpoint) | Custom |
| Streaming | Limited (SSE, WebSockets) | Subscriptions | Native bi-directional |
| Best for | Public APIs, CRUD | Rich front ends, many clients | Internal service-to-service, low latency |

### gRPC example

```protobuf
syntax = "proto3";

service UserService {
  rpc GetUser (GetUserRequest) returns (User);
  rpc StreamUpdates (GetUserRequest) returns (stream User);
}

message GetUserRequest { int64 id = 1; }

message User {
  int64 id = 1;
  string name = 2;
  string email = 3;
}
```

---

## Real-time options

- **Short polling** — client asks every N seconds. Simple, wasteful.
- **Long polling** — server holds the request open until there is data.
- **Server-Sent Events (SSE)** — one-way server → client stream over HTTP. Great for notifications and live feeds.
- **WebSockets** — full-duplex connection. Chat, multiplayer, collaborative editing.
- **Webhooks** — server calls *your* URL when something happens (payment succeeded). Verify signatures and make handlers idempotent.
