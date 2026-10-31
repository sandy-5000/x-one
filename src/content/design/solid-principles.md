---
title: "SOLID principles"
order: 1
---
# SOLID Principles

Five OO design principles (Robert C. Martin; acronym by Michael Feathers) for **cheap, safe change**: low coupling, high cohesion, swappable dependencies, testable policy.

| Letter | Principle | One-line idea |
| --- | --- | --- |
| S | Single Responsibility | One **actor** (stakeholder) per module — one reason to change |
| O | Open/Closed | Add behaviour by **new code**, not by editing stable code |
| L | Liskov Substitution | Subtypes honour the **behavioural contract** of the supertype |
| I | Interface Segregation | Clients depend only on methods they **use** |
| D | Dependency Inversion | **Policy** owns abstractions; **details** implement them |

```text
SRP → where to cut     ISP → shape of seams     DIP → direction of dependencies
  └────────────────── OCP → new plug-ins ──► LSP → plug-ins must be safe
```

---

## Key points

- Goal is **dependency management**, not more interfaces for their own sake — judge designs by how painful the last few changes were.
- **SRP** is about *actors* (who asks for changes), not “one method” or “one thing” — a large class serving one team can be fine.
- **OCP** protects *expected axes of variation* — introduce Strategy/registry on the **second** variant, not speculatively.
- **Sealed types + exhaustive `switch`** trade OCP on *types* for easy new *operations* (expression problem); use for small stable sets you own.
- **LSP** is semantics the compiler cannot see: preconditions cannot strengthen, postconditions cannot weaken, invariants and **history** must hold.
- Prefer **composition**, `final` classes, and **contract tests** over inheritance for reuse; fakes and test doubles must honour the same contract as production.
- **ISP** splits *client dependencies* (role interfaces), not necessarily the implementing class — one service can implement several narrow ports.
- **DIP** inverts **source** dependencies against control flow; ports live in the policy package and use **domain** types, not JDBC/HTTP DTOs.
- **DI** (constructor injection) is a technique; **containers** are optional — a hand-written composition root is enough.
- **DRY** is about duplicated *knowledge*; merging code that changes for different actors recreates the classic `Employee` fragility.
- Refactor in steps: characterisation tests → identify actors → extract → ports/adapters → contract tests on every implementation.

---

## S — Single Responsibility

**Definition (Clean Architecture):** a module is responsible to **one actor** — one group that requests changes (finance, ops, platform, marketing).

| Misreading | Reality |
| --- | --- |
| “One method / one thing” | That’s function-level advice; many methods for one actor is OK |
| “Split by layer only” | Two business rules for two departments in one service still violates SRP |

```text
   CFO                    COO                    DBA
     │                      │                      │
     ▼                      ▼                      ▼
 calculatePay()        reportHours()             save()
     └──────────► shared regularHours() ◄──────────┘
              one change breaks the other actor’s behaviour
```

**Smell:** `*Manager`, mixed imports (`sql` + `mail` + JSON), disjoint field use per method, many teams in `git log`.

**Fix:** extract class per actor + thin **coordinator** that only sequences work (Repository, CQRS, Facade).

```java
// After: finance owns pricing; coordinator only orchestrates
public interface PricingPolicy { BigDecimal total(List<OrderLine> lines, CustomerTier tier); }

public final class PlaceOrderService {
    private final PricingPolicy pricing;
    private final OrderRepository orders;
    public Order place(PlaceOrderCommand cmd) {
        var total = pricing.total(cmd.lines(), cmd.tier());
        orders.save(new Order(cmd.customerId(), total));
        return /* ... */;
    }
}
```

**When to wait:** early product with unknown actors — split when a **second** actor actually forces unrelated edits.

| Symptom (rotting design) | Often means |
| --- | --- |
| Rigidity — small change cascades | High coupling to concrete types |
| Fragility — unrelated tests break | Shared code across actors (SRP) |
| Immobility — cannot reuse a module | Policy tangled with DB/UI/framework |
| Viscosity — hacks easier than right design | No seams; tests need real infra |

**Refactor loop:** characterisation test → label actors on each block → extract method/class → coordinator wires collaborators → move unit tests down with each extraction.

---

## O — Open/Closed

**Definition:** open for **extension**, closed for **modification** along axes you expect to change (Martin’s polymorphic OCP: depend on abstractions, add implementations).

| Closed set (you own) | Open set (plugins, many providers) |
| --- | --- |
| Sealed hierarchy + exhaustive `switch` | Interface + registry / `ServiceLoader` |
| Compiler lists every edit site | New class only; stable registry |

**Smell:** same `switch` on payment/channel/type in many methods; new variant PR touches half the codebase.

**Fix:** Strategy + map built from injected list (duplicate keys fail at startup).

```java
public interface PaymentProcessor {
    PaymentMethod method();
    PaymentResult charge(Order order);
}

@Service
class CheckoutService {
    private final Map<PaymentMethod, PaymentProcessor> byMethod;
    CheckoutService(List<PaymentProcessor> all) {
        this.byMethod = all.stream()
            .collect(Collectors.toUnmodifiableMap(PaymentProcessor::method, p -> p));
    }
    public PaymentResult pay(Order o, PaymentMethod m) {
        return Optional.ofNullable(byMethod.get(m))
            .orElseThrow(() -> new IllegalArgumentException("no processor"))
            .charge(o);
    }
}
```

Cross-cutting retry/metrics: **Decorator** (delegate must be **idempotent** — LSP).

**Expression problem** — pick the shape that matches how the system will change:

| Design | Add new type | Add new operation |
| --- | --- | --- |
| Interface + classes | New class (OCP) | Edit every class |
| Sealed + exhaustive `switch` | Edit every switch (compiler helps) | New function |

Do not use `default` on a sealed switch — you lose exhaustiveness.

---

## L — Liskov Substitution

**Definition:** any client that works with type `T` must work with any subtype `S` — same observable behaviour under `T`’s contract.

| Rule | Subtype may | Must not |
| --- | --- | --- |
| Preconditions | Weaken (require less) | Strengthen |
| Postconditions | Strengthen (promise more) | Weaken |
| Invariants / history | Preserve | Break (e.g. mutators on “immutable” base) |

**Classic trap:** `Square extends Rectangle` — `setWidth` implied height unchanged; `stretch(Rectangle)` fails.

**Fixes:** immutable records; or **composition** / shared `Shape` interface without shared mutators.

**Realistic traps:** gateway that only allows full refunds while interface promises partial; subclass that breaks `balance >= 0` invariant; fake repo that skips DB constraints — tests pass, prod fails.

**JDK lessons:** `Stack extends Vector`, `Timestamp extends Date`, optional `List.add` → design for **narrow types** or document optional ops explicitly.

**Java vs contract:** covariant returns and checked-exception narrowing are enforced; pre/postconditions and history are not — arrays are covariant (`Object[] a = new String[1]; a[0]=1` → `ArrayStoreException`).

**Practice:** Javadoc contracts + **contract tests** (abstract test class run against JDBC repo, in-memory fake, etc.).

```java
abstract class OrderRepositoryContract {
    protected abstract OrderRepository repo();
    @Test void rejectsDuplicateExternalId() {
        repo().save(sample("x"));
        assertThrows(DuplicateOrderException.class, () -> repo().save(sample("x")));
    }
}
```

---

## I — Interface Segregation

**Definition:** no client should depend on methods it does not use (Martin’s Xerox story: `StapleJob`, `PrintJob` in front of one `Job`).

| Header interface | Role interface |
| --- | --- |
| Mirrors entire `FooServiceImpl` | Named by client need: `PlaceOrder`, `OrderLookup` |

**Smell:** mocks stubbing nine unused methods; read-only client injected with `deleteAll()`.

**Fix:** split by **client**; one `@Service` can implement `PlaceOrder`, `CancelOrder`, `OrderLookup`.

```java
public interface PlaceOrder { Order place(PlaceOrderCommand cmd); }

@RestController
class CheckoutController {
    private final PlaceOrder placeOrder;
    CheckoutController(PlaceOrder placeOrder) { this.placeOrder = placeOrder; }
}
```

Framework note: prefer Spring Data `Repository<T,ID>` with only needed methods over fat `JpaRepository`.

---

## D — Dependency Inversion

**Definition:** (A) high-level modules do not depend on low-level modules — both depend on abstractions; (B) abstractions are owned by **policy**, details implement them.

```text
Without DIP:  OrderService ──imports──► JdbcOrderDao

With DIP:     OrderService ──► OrderRepository ◄──implements── JdbcOrderRepository
              (policy owns port)                    (adapter in outer layer)
```

| Term | What it is |
| --- | --- |
| DIP | Dependency **direction** and port **ownership** |
| DI | Pass dependencies in (prefer **constructor** + `final`) |
| IoC container | Automates wiring; not required for DIP |

**Smell:** `new StripeClient()`, `LocalDate.now()` in domain; domain imports Spring JDBC.

**Fix:** ports with domain types; adapters translate vendor vocabulary; inject `Clock`, `Supplier<String>` for IDs.

```java
public interface Payments {
    ChargeOutcome charge(CustomerId customer, Money amount, String idempotencyKey);
}

public final class SubscriptionRenewalService {
    private final Subscriptions subs;
    private final Payments payments;
    private final Clock clock;
    // constructor injection; renewDue() uses LocalDate.now(clock)
}
```

**Hexagonal / Clean Architecture:** same rule — all source arrows point **inward**; composition root in `config`.

```text
com.app.order
  domain/           entities, value objects — no framework imports
  application/      use cases + inbound/outbound port interfaces
  adapter/in.web    controllers
  adapter/out.jdbc  repositories, gateways
  config/           Spring @Configuration — only place that knows concrete adapters
```

**Testing:** fakes for policy (fast); contract tests for every adapter + fake; a few integration tests (Testcontainers, vendor sandbox) for wiring.

**Stable deps:** `String`, `java.time`, well-owned libs — no port needed.

---

## Mini case study: god class → seams

```text
Before: OrderManager — validate, price switch, payment if-chain, JDBC, SMTP, UUID.now()
After:  PlaceOrderCommand (records/sealed types)
        PricingPolicy (SRP/OCP)  PaymentRouter + gateways (OCP/LSP)
        OrderRepository, OrderNotifier ports (DIP/ISP)
        PlaceOrderService — charge then save (ordering visible for consistency fixes)
```

| Violation in one method | Principle |
| --- | --- |
| Validation + tax + SQL + email copy | SRP (four actors) |
| `switch` tier + payment type | OCP |
| `new StripeClient`, `DriverManager` | DIP |
| One fat parameter list for all pay types | ISP / LSP |

---

## How the five work together

| Principle | Review smell | Usual fix |
| --- | --- | --- |
| SRP | God class, mixed layers | Extract per actor + coordinator |
| OCP | Repeated switches | Strategy registry, Decorator |
| LSP | `UnsupportedOperationException`, `instanceof` on base | Composition, contract tests |
| ISP | Fat `XService` everywhere | Role interfaces |
| DIP | `new` infra in policy | Ports, adapters, fakes |

**Functional style:** SRP ≈ small pure functions; OCP ≈ HOF composition; ISP ≈ pass only needed functions; DIP ≈ “functional core, imperative shell”.

**Related (one line each):** **DRY** — one authoritative representation of each rule; **YAGNI** — abstract on second variant; **composition over inheritance** — avoids most LSP pain; **Law of Demeter** — don’t chain through strangers’ internals (`order.customer().address().city()` in caller code).

| GRASP (Larman) | SOLID echo |
| --- | --- |
| High cohesion / Information Expert | SRP, tell-don’t-ask |
| Protected Variations | OCP |
| Polymorphism | OCP + LSP |
| Pure Fabrication (e.g. Repository) | SRP + DIP |
| Indirection | DIP, Adapter |

**Code review checklist (quick scan):**

| If you see… | Check principle |
| --- | --- |
| Class edited by unrelated tickets / teams | SRP |
| Copy-pasted `switch` on same enum | OCP |
| `instanceof` before calling base-typed method | LSP |
| Test mock with 15 stubbed methods | ISP |
| `new` JDBC/HTTP client inside `@Service` | DIP |
| `FooService` + `FooServiceImpl` only | Ceremony, not SOLID |

---

## Pitfalls

1. **Interface per class** with one implementation — ceremony, not DIP.
2. **Speculative OCP** — abstractions before a second variant or volatile boundary.
3. **Inheritance for reuse** — most LSP failures; use composition and `final`.
4. **`UnsupportedOperationException`** instead of fixing the interface (ISP) or contract (LSP).
5. **Leaky ports** — `ResultSet`, vendor DTOs, HTTP codes in domain interfaces.
6. **Mock-heavy tests** — freeze implementation; prefer fakes + state assertions.
7. **`default` on sealed switches** — loses exhaustiveness when adding types.
8. **DRY merge** across actors — couples unrelated change reasons.
9. **Treating SOLID as the goal** — needless complexity is itself a design smell in Martin’s work.

---

## Quick check

**What does SRP really mean?**  
One **actor** (stakeholder) per module — one source of change requests. “Do one thing” describes functions, not classes. Split when different teams or roles would force unrelated edits to the same type.

**OCP in a payment system — one sentence plus mechanism?**  
New provider = new class implementing `PaymentProcessor`, registered in a map; `CheckoutService` stays unchanged. Add the abstraction when a second provider or a clear roadmap appears, not before.

**Why is Square ⊂ Rectangle often wrong, and a fix?**  
Mutating `setWidth` on `Rectangle` promises height unchanged; `Square` breaks that postcondition. Prefer immutable shapes, or separate types / composition instead of inheritance.

**DIP vs dependency injection vs a Spring container?**  
DIP is **where** dependencies point (policy owns ports). DI is **how** dependencies arrive (constructor args). A container **wires** the graph; you can follow DIP with a plain `main()` composition root and no framework.

**How does DRY conflict with SRP?**  
DRY removes duplicated **knowledge**. Two identical-looking functions that must change for different actors should stay separate; merging them ties two reasons to change into one module and recreates hidden coupling.
