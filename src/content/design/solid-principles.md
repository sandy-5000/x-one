---
title: "SOLID principles"
order: 1
---
# SOLID Principles

SOLID is a set of five object-oriented design principles collected and popularised by Robert C.
Martin. The acronym itself was coined later by Michael Feathers. Together the principles target one
goal: **code whose behaviour can be changed cheaply and safely**. A change should touch few places,
it should not break unrelated features, and the parts you rely on should be swappable and testable.

| Letter | Principle | One-line idea | Origin |
| --- | --- | --- | --- |
| S | Single Responsibility | A module is responsible to one actor (one reason to change) | Martin, building on DeMarco / Page-Jones cohesion |
| O | Open/Closed | Add behaviour by adding code, not by editing stable code | Bertrand Meyer, 1988 |
| L | Liskov Substitution | Subtypes must honour the behavioural contract of their supertype | Barbara Liskov, 1987; Liskov & Wing, 1994 |
| I | Interface Segregation | Clients should not depend on methods they do not use | Martin, 1996 (Xerox consulting) |
| D | Dependency Inversion | Policy should not depend on detail; both depend on abstractions owned by policy | Martin, 1996 |

These notes cover each principle in depth: the precise definition, the usual misreadings, realistic
violations, step-by-step refactoring, how to spot violations in code review, when not to apply the
principle, and how it relates to design patterns. After that come a full end-to-end refactoring,
SOLID in a functional style, and related principles.

---

## 1. Why SOLID exists: history and motivation

### 1.1 Coupling and cohesion

SOLID descends from two ideas Larry Constantine and Ed Yourdon formalised in *Structured Design*
(1979), long before object orientation went mainstream:

- **Coupling** measures how strongly one module depends on another. High coupling means a change
  in module A forces changes, recompiles, or retests of module B.
- **Cohesion** measures how strongly the elements inside one module belong together. High cohesion
  means everything in the module serves one purpose and changes together.

The goal is **low coupling, high cohesion**. Each SOLID principle is a specific technique for that:

| Principle | Mainly improves | How |
| --- | --- | --- |
| SRP | Cohesion | Groups code that changes for the same reason; separates code that does not |
| OCP | Coupling | New variants plug into an abstraction instead of editing a central switch |
| LSP | Coupling (safety) | Clients depend on a contract and any implementation honours it |
| ISP | Coupling | Clients depend on narrow interfaces, so unrelated changes do not reach them |
| DIP | Coupling (direction) | Source-code dependencies point toward stable policy, not volatile detail |

Kinds of coupling, from worst to best (classic structured-design list): content coupling (reaching
into another module's internals), common coupling (shared global state), control coupling (passing
a flag that tells the callee what to do), stamp coupling (passing a whole structure when a part is
needed), and data coupling (passing only the data needed). A `boolean sendEmail` parameter is
control coupling. Passing a whole `Order` when the method needs only `orderId` is stamp coupling.

### 1.2 Symptoms of rotting design

In *Design Principles and Design Patterns* (2000) Martin described four symptoms of a design going
bad. *Agile Software Development: Principles, Patterns, and Practices* (2002) extended the list
with needless complexity, needless repetition, and opacity.

| Symptom | What you observe | Typical root cause |
| --- | --- | --- |
| **Rigidity** | A small change cascades into many modules; estimates explode | High coupling; dependencies on concrete classes |
| **Fragility** | A change breaks something in an apparently unrelated place | Low cohesion; one class serving several actors |
| **Immobility** | A useful part cannot be reused elsewhere without dragging half the system | Business logic tangled with framework / DB / UI |
| **Viscosity** | Doing the right thing is harder than hacking; the build or test cycle is slow | Missing seams; tests need real DB / network |
| Needless complexity | Abstractions with one implementation "just in case" | Over-applied OCP / DIP |
| Needless repetition | Copy-paste of rules across modules | Missing abstraction (DRY violation) |
| Opacity | Code is hard to read and understand | Mixed abstraction levels, god classes |

Note that **needless complexity is listed as a design smell in the same book that teaches SOLID**.
The principles are cures for specific symptoms, not goals to maximise.

### 1.3 Timeline

```text
1979  Yourdon & Constantine   "Structured Design": coupling and cohesion
1987  Barbara Liskov          OOPSLA keynote "Data Abstraction and Hierarchy"
1988  Bertrand Meyer          "Object-Oriented Software Construction": Open/Closed
1994  Liskov & Wing           "A Behavioral Notion of Subtyping" (ACM TOPLAS)
1996  Robert C. Martin        C++ Report columns on OCP, LSP, DIP and ISP
2000  Robert C. Martin        "Design Principles and Design Patterns" (paper)
2002  Robert C. Martin        "Agile Software Development: PPP" (book form)
~2004 Michael Feathers        Reorders the principles into the acronym "SOLID"
2017  Robert C. Martin        "Clean Architecture": SRP restated in terms of actors
```

### 1.4 What SOLID is really about

Strip away the acronym and SOLID is **dependency management**. In an object-oriented language the
key move is polymorphism: a call site can invoke code it has no source-code dependency on. That lets
you choose the direction of every arrow in your dependency graph. SOLID is the set of rules for
choosing those arrows well:

- Put things that change together in one place (SRP).
- Make the places that change often into plug-in points (OCP).
- Make sure plug-ins are safe to substitute (LSP).
- Keep the plug-in sockets small (ISP).
- Point the arrows from volatile details toward stable policy (DIP).

---

## 2. S — Single Responsibility Principle

### 2.1 Definition

The principle has been stated three ways over time. The later ones are more precise:

1. *"A class should have only one reason to change."* (Martin, early 2000s)
2. *"Gather together the things that change for the same reasons. Separate those things that
   change for different reasons."* (Martin, restating cohesion)
3. *"A module should be responsible to one, and only one, actor."* (*Clean Architecture*, 2017)

An **actor** is a group of people (a stakeholder or role) who request changes: finance, operations,
compliance, the DBA team, marketing. A "reason to change" is a change request from one actor. SRP is
therefore a statement about **people and organisations**, not about the number of methods.

### 2.2 Common misreadings

| Misreading | Why it is wrong |
| --- | --- |
| "A class should do one thing." | That describes *functions* (Clean Code's "do one thing"). A class like `OrderPricing` does many things, all for one actor. |
| "One public method per class." | Produces anaemic fragments and scatters one concept across ten files, lowering cohesion. |
| "Small classes satisfy SRP." | A 40-line class can serve three actors; a 400-line pricing engine can serve one. |
| "SRP means splitting by technical layer only." | Layering helps, but two business rules for two departments in the same service still violate SRP. |
| "Every responsibility needs its own interface." | SRP is about separating code, not about adding abstractions. |

### 2.3 The canonical example: one class, three actors

From *Clean Architecture*: an `Employee` class has `calculatePay()` (specified by the CFO's
accounting team), `reportHours()` (specified by the COO's operations team), and `save()` (owned by
the DBAs / CTO).

```text
   CFO (accounting)        COO (operations)        CTO (DBAs)
         │                        │                     │
         ▼                        ▼                     ▼
   calculatePay()           reportHours()            save()
         │                        │                     │
         └───────────┬────────────┘                     │
                     ▼                                  │
              regularHours()   (shared helper)          │
                     │                                  │
                     └────────────► Employee ◄──────────┘
```

Both `calculatePay` and `reportHours` reuse a private `regularHours()` helper. Accounting asks to
change how overtime is counted; a developer edits `regularHours()`; payroll is now correct but the
operations report is silently wrong. Nobody in operations asked for a change. That is the
**fragility** SRP prevents: code that belongs to different actors must not share a hidden coupling.

### 2.4 Realistic violation 1: the order service that does everything

```java
@Service
public class OrderService {
    private final JdbcTemplate jdbc;
    private final JavaMailSender mail;

    public OrderService(JdbcTemplate jdbc, JavaMailSender mail) {
        this.jdbc = jdbc;
        this.mail = mail;
    }

    public long placeOrder(PlaceOrderRequest req) {
        // 1. validation rules (product team)
        if (req.lines().isEmpty()) throw new IllegalArgumentException("empty order");

        // 2. pricing and tax rules (finance)
        BigDecimal subtotal = req.lines().stream()
                .map(l -> l.unitPrice().multiply(BigDecimal.valueOf(l.quantity())))
                .reduce(BigDecimal.ZERO, BigDecimal::add);
        BigDecimal tax = subtotal.multiply(new BigDecimal("0.18"));
        BigDecimal total = subtotal.add(tax).setScale(2, RoundingMode.HALF_EVEN);

        // 3. persistence (DBAs / platform)
        jdbc.update("INSERT INTO orders(customer_id, total) VALUES (?, ?)",
                req.customerId(), total);
        long id = jdbc.queryForObject("SELECT lastval()", Long.class);

        // 4. customer communication wording (marketing)
        var msg = new SimpleMailMessage();
        msg.setTo(req.email());
        msg.setSubject("Your order #" + id);
        msg.setText("Thanks! You paid " + total + " incl. GST.");
        mail.send(msg);
        return id;
    }
}
```

Four actors can force an edit here: product (validation), finance (tax), platform (schema), and
marketing (email copy). A marketing copy change triggers a redeploy of pricing code and a re-run of
pricing tests. Merge conflicts appear between unrelated teams.

### 2.5 Realistic violation 2: report generation

```java
public class SalesReportJob {
    public void run(LocalDate day) throws Exception {
        List<Object[]> rows = /* SQL aggregation over orders */ List.of();   // data access
        BigDecimal revenue = BigDecimal.ZERO;                                 // business metric
        for (Object[] r : rows) revenue = revenue.add((BigDecimal) r[2]);
        StringBuilder csv = new StringBuilder("region,orders,revenue\n");     // formatting
        for (Object[] r : rows) csv.append(r[0]).append(',').append(r[1])
                                   .append(',').append(r[2]).append('\n');
        Files.writeString(Path.of("/reports/sales-" + day + ".csv"), csv);   // storage
        // ... SFTP upload to the finance partner                            // delivery
    }
}
```

Data access, metric definition ("does revenue include refunds?"), output format (CSV today, XLSX
tomorrow), storage, and delivery are five independently changing concerns.

### 2.6 Step-by-step refactoring

Refactor in small, behaviour-preserving steps with tests green after each:

1. **Characterise.** Write a test around the current behaviour (golden-file test for the CSV, or a
   test with an in-memory DB). Without it you cannot refactor safely.
2. **Identify actors.** Annotate each block with who would request a change to it.
3. **Extract method** per block, so each concern is a named function.
4. **Extract class** for each concern, passing data as records rather than shared fields.
5. **Introduce a coordinator** that only sequences the collaborators and contains no rules.
6. **Move tests** down: each extracted class gets focused unit tests; the coordinator gets one
   wiring test.

Result for the report:

```java
public record RegionSales(String region, long orders, BigDecimal revenue) {}

public interface SalesQuery {                        // platform team
    List<RegionSales> salesFor(LocalDate day);
}

public final class SalesMetrics {                    // finance: definition of the numbers
    public BigDecimal totalRevenue(List<RegionSales> rows) {
        return rows.stream().map(RegionSales::revenue).reduce(BigDecimal.ZERO, BigDecimal::add);
    }
}

public interface ReportRenderer {                    // whoever consumes the format
    byte[] render(List<RegionSales> rows);
}

public final class CsvReportRenderer implements ReportRenderer {
    @Override
    public byte[] render(List<RegionSales> rows) {
        var sb = new StringBuilder("region,orders,revenue\n");
        rows.forEach(r -> sb.append(r.region()).append(',')
                            .append(r.orders()).append(',')
                            .append(r.revenue().toPlainString()).append('\n'));
        return sb.toString().getBytes(StandardCharsets.UTF_8);
    }
}

public interface ReportDelivery {                    // integration / partner contract
    void deliver(String name, byte[] content);
}

public final class SalesReportJob {                  // coordinator: sequencing only
    private final SalesQuery query;
    private final ReportRenderer renderer;
    private final ReportDelivery delivery;

    public SalesReportJob(SalesQuery query, ReportRenderer renderer, ReportDelivery delivery) {
        this.query = query;
        this.renderer = renderer;
        this.delivery = delivery;
    }

    public void run(LocalDate day) {
        List<RegionSales> rows = query.salesFor(day);
        delivery.deliver("sales-" + day + ".csv", renderer.render(rows));
    }
}
```

Notice that the split also produced natural OCP and DIP seams (`ReportRenderer`,
`ReportDelivery`). This is common: SRP tells you *where* to cut; the other principles tell you
*how* to connect the pieces.

### 2.7 Detecting SRP violations in code review

- The class name contains `Manager`, `Processor`, `Handler`, `Util`, `Helper`, or `And`.
- The constructor takes many unrelated dependencies (more than about five, from different layers).
- Imports mix layers: `java.sql`, `jakarta.mail`, `com.fasterxml.jackson`, and domain types together.
- Different methods use disjoint subsets of fields (low LCOM, "lack of cohesion of methods").
- `git log` shows the file changed by many teams for unrelated tickets ("change frequency by
  author" or hotspot analysis).
- Tests need large fixtures because exercising one rule requires setting up all the others.
- Private helpers shared between public methods that belong to different features.

### 2.8 Trade-offs and when not to apply

- **Over-splitting** creates "ravioli code": dozens of tiny classes and indirection that hide the
  flow. If two pieces always change together, keeping them together *is* SRP.
- **Early in a product**, actors are unknown. A cohesive 300-line service is fine until a second
  actor appears. Split on evidence (actual change requests), not speculation.
- **Transaction boundaries** sometimes argue for keeping coordination in one place; split rules
  out but keep one coordinator that owns the transaction.
- Splitting a class across services (microservices) for SRP reasons adds network failure modes.
  SRP is a module-level principle first.

### 2.9 Relation to patterns

| Pattern / technique | Connection to SRP |
| --- | --- |
| Extract Class, Move Method (Fowler) | The mechanical refactorings that implement SRP |
| Facade | Keeps a simple entry point after splitting a god class into collaborators |
| Repository | Separates persistence concerns from domain logic |
| CQRS | SRP applied to read vs write models, which change for different reasons |
| Builder / Renderer / Formatter classes | Separate presentation from the data being presented |
| Common Closure Principle | SRP at the package/component level |

---

## 3. O — Open/Closed Principle

### 3.1 Definition: Meyer's version vs Martin's version

> Software entities (classes, modules, functions) should be open for extension, but closed for
> modification.

- **Meyer (1988)** meant *implementation inheritance*: once a class is published and in use
  (closed), you extend it by subclassing (open) instead of editing it, so existing clients keep
  working.
- **Martin (1996)**, the "polymorphic OCP", meant *abstract interfaces*: clients depend on an
  abstraction; new behaviour is a new implementation. The abstraction is closed, the set of
  implementations is open. This is the version used today.

The practical reading: **for the axes of change you expect, adding a variant should mean adding a
class (or a configuration entry), not editing a central piece of tested code.**

### 3.2 Common misreadings

- "Never modify existing code." Bug fixes and refactoring are modifications and are fine. OCP is
  about *adding features along a known axis of variation*.
- "Make everything extensible." No code is closed against every change. You choose which axes to
  protect against (Martin calls this *strategic closure*). Guessing wrong yields needless
  complexity.
- "OCP means inheritance." Composition (Strategy, Decorator) is usually the better mechanism.
- "A `switch` is always a violation." One switch in a factory that maps config to implementations
  is acceptable. The smell is the *same* switch on the same type code repeated in many places.

### 3.3 Realistic violation: payment methods

```java
public class CheckoutService {
    public PaymentResult pay(Order order, String method) {
        switch (method) {
            case "CARD" -> { /* Stripe API call, 3DS handling, card error mapping */ }
            case "PAYPAL" -> { /* redirect flow, webhook correlation */ }
            case "UPI" -> { /* collect request, polling for status */ }
            default -> throw new IllegalArgumentException("unknown method " + method);
        }
        return PaymentResult.ok();
    }

    public void refund(Order order, String method) {
        switch (method) {               // the same switch, duplicated
            case "CARD" -> { /* ... */ }
            case "PAYPAL" -> { /* ... */ }
            case "UPI" -> { /* ... */ }
            default -> throw new IllegalArgumentException("unknown method " + method);
        }
    }
}
```

Adding "wallet" means editing every switch, re-testing card and PayPal flows, and coordinating
deploys across teams. Missing one switch is a runtime failure.

### 3.4 Realistic violation: notifications

```java
public void notify(User user, String event, Set<String> channels) {
    if (channels.contains("EMAIL")) { /* SMTP */ }
    if (channels.contains("SMS"))   { /* Twilio, with DND-hours rule for India */ }
    if (channels.contains("PUSH"))  { /* FCM token lookup */ }
    // marketing wants WhatsApp next quarter...
}
```

### 3.5 Refactoring with Strategy and a registry

```java
public enum PaymentMethod { CARD, PAYPAL, UPI }

public interface PaymentProcessor {
    PaymentMethod method();
    PaymentResult charge(Order order);
    RefundResult refund(Order order, BigDecimal amount);
}

@Component
final class CardProcessor implements PaymentProcessor {
    public PaymentMethod method() { return PaymentMethod.CARD; }
    public PaymentResult charge(Order order) { /* Stripe */ return PaymentResult.ok(); }
    public RefundResult refund(Order order, BigDecimal amount) { return RefundResult.ok(); }
}

@Service
public class CheckoutService {
    private final Map<PaymentMethod, PaymentProcessor> processors;

    public CheckoutService(List<PaymentProcessor> all) {          // Spring injects every bean
        this.processors = all.stream()
                .collect(Collectors.toUnmodifiableMap(PaymentProcessor::method, p -> p));
    }

    public PaymentResult pay(Order order, PaymentMethod method) {
        var p = processors.get(method);
        if (p == null) throw new IllegalArgumentException("no processor for " + method);
        return p.charge(order);
    }
}
```

Adding a wallet: add an enum constant and a `WalletProcessor` bean. `CheckoutService` and the
existing processors are untouched. `toUnmodifiableMap` throws on duplicate keys, which catches two
beans claiming the same method at startup instead of in production traffic.

### 3.6 Extension by Decorator

Cross-cutting behaviour (retry, metrics, idempotency) is a second axis. Decorators extend behaviour
without modifying either the client or the wrapped processor:

```java
public final class RetryingProcessor implements PaymentProcessor {
    private final PaymentProcessor delegate;
    private final int maxAttempts;

    public RetryingProcessor(PaymentProcessor delegate, int maxAttempts) {
        this.delegate = delegate;
        this.maxAttempts = maxAttempts;
    }

    public PaymentMethod method() { return delegate.method(); }

    public PaymentResult charge(Order order) {
        TransientPaymentException last = null;
        for (int i = 1; i <= maxAttempts; i++) {
            try {
                return delegate.charge(order);           // must be idempotent (idempotency key)
            } catch (TransientPaymentException e) {
                last = e;
            }
        }
        throw last;
    }

    public RefundResult refund(Order order, BigDecimal amount) {
        return delegate.refund(order, amount);
    }
}
```

`new RetryingProcessor(new MeteredProcessor(new CardProcessor()), 3)` composes behaviours in any
order. Note the comment: a retrying decorator is only correct if the delegate is idempotent, which
is part of the `PaymentProcessor` contract (an LSP concern).

### 3.7 Extension by plugins: `ServiceLoader`

For extension by *separately compiled* modules, the JDK has `java.util.ServiceLoader`:

```java
// in module-info.java of the plugin module:
//   provides com.shop.payments.PaymentProcessor with com.acme.WalletProcessor;
// or a classpath file META-INF/services/com.shop.payments.PaymentProcessor

List<PaymentProcessor> processors = ServiceLoader.load(PaymentProcessor.class)
        .stream()
        .map(ServiceLoader.Provider::get)
        .toList();
```

The core module never mentions the plugin. JDBC drivers, `java.nio.file.spi.FileSystemProvider`,
and SLF4J bindings use the same mechanism.

### 3.8 Counterpoint: sealed types and pattern matching

Java 17 (sealed classes, JEP 409) and Java 21 (pattern matching for `switch`, JEP 441; record
patterns, JEP 440) make a *closed* set of types with exhaustive switches idiomatic:

```java
public sealed interface PaymentMethod permits Card, PayPal, Upi {}
public record Card(String token, String last4) implements PaymentMethod {}
public record PayPal(String payerEmail) implements PaymentMethod {}
public record Upi(String vpa) implements PaymentMethod {}

static BigDecimal processingFee(PaymentMethod m, BigDecimal amount) {
    return switch (m) {                                   // no default: exhaustive
        case Card c      -> amount.multiply(new BigDecimal("0.020"));
        case PayPal p    -> amount.multiply(new BigDecimal("0.034"));
        case Upi(var vpa) -> BigDecimal.ZERO;
    };
}
```

This **deliberately violates** the polymorphic OCP for *types*: adding `Wallet` means editing every
switch. But the compiler lists every switch that needs editing (a missing case is a compile error),
which removes the main risk OCP guards against. In exchange, adding a new *operation* (fees,
display name, risk score) is a new function and touches no type. This is the **expression
problem**:

| Design | Add a new type | Add a new operation |
| --- | --- | --- |
| Interface + polymorphic classes (OOP OCP) | Easy: one new class | Hard: edit every class |
| Sealed hierarchy + exhaustive switch | Hard but compiler-checked | Easy: one new function |

Guideline:

- The set of variants is **owned by you, small, and stable** (domain states, AST nodes, result
  types, events): prefer sealed types and switches.
- The set of variants is **open, grows often, or is contributed by other teams/plugins**
  (payment providers, notification channels, export formats): prefer interfaces.
- Do not use a `default` branch on a sealed switch; it silently swallows new types and throws away
  the compiler check.

### 3.9 Detecting OCP violations in code review

- The same `switch`/`if-else` on a type code or enum appears in several methods or classes.
- A pull request for a "new variant" modifies many existing files instead of adding one.
- `instanceof` chains over an open (non-sealed) hierarchy.
- Feature flags implemented as `if (flag)` deep inside core logic instead of selecting an
  implementation at wiring time.
- Boolean parameters that change behaviour (`export(data, true, false)`).

### 3.10 Trade-offs and when not to apply

- Every abstraction is a guess about future change. Martin's advice ("take the first bullet"):
  write the simple version, and introduce the abstraction when the first change along that axis
  actually arrives.
- Indirection costs readability: following a call into `processors.get(method)` requires knowing
  the registry.
- Strategy objects tend to need the same data; designing a stable interface for them is hard. A
  wrong interface is worse than a switch.
- For performance-critical inner loops, megamorphic call sites can inhibit JIT inlining. Rarely
  relevant in business code, but real in hot paths.

### 3.11 Relation to patterns

Strategy, Decorator, Template Method, Observer, Chain of Responsibility, Visitor (the OOP answer to
the "new operation" side of the expression problem), Abstract Factory, and plugin architectures
are all mechanisms for OCP. Visitor and sealed-switch are alternatives to each other.

---

## 4. L — Liskov Substitution Principle

### 4.1 Definition

Liskov's 1987 keynote phrased it informally:

> If for each object o1 of type S there is an object o2 of type T such that for all programs P
> defined in terms of T, the behavior of P is unchanged when o1 is substituted for o2, then S is a
> subtype of T.

Liskov and Wing (1994) gave the property form:

> Let φ(x) be a property provable about objects x of type T. Then φ(y) should be true for objects y
> of type S where S is a subtype of T.

In practice: **anything a client may rely on about `T`, as stated by `T`'s contract, must also hold
for every subtype.** LSP is about *behaviour*, not about whether the code compiles. The compiler
checks signatures; LSP is about semantics the compiler cannot see.

### 4.2 The formal rules

Liskov and Wing split the requirements into signature rules, method rules, and property rules.

```text
                       Subtype S may ...                     must not ...
Signature rules
  argument types       accept wider types (contravariance)   demand narrower types
  return types         return narrower types (covariance)    return wider types
  exceptions           throw fewer / narrower exceptions     throw new, unexpected ones
Method rules
  preconditions        weaken them (require less)            strengthen them
  postconditions       strengthen them (promise more)        weaken them
Property rules
  invariants           keep every supertype invariant        break any
  history constraint   allow only histories T allows         add state changes T forbids
```

**History constraint** is the rule people forget. If `T` is immutable, clients can rely on an
object never changing after construction. A subtype that adds a setter violates this, even though
every individual method obeys its pre- and postconditions.

### 4.3 What Java enforces and what it does not

| Rule | Enforced by `javac`? | Notes |
| --- | --- | --- |
| Covariant return types | Yes (since Java 5) | `Object clone()` may be overridden as `Foo clone()` |
| Contravariant parameters | Not supported | A method with a wider parameter type is an *overload*, not an override |
| Checked exceptions | Yes | An override cannot declare broader checked exceptions |
| Unchecked exceptions | No | `UnsupportedOperationException` can be thrown anywhere |
| Pre/postconditions | No | Javadoc, assertions, and tests only |
| Invariants, history | No | Design and tests only |
| Generic variance | Use-site | `List<? extends T>` covariant (producer), `List<? super T>` contravariant (consumer): PECS |

Java **arrays are covariant**, which is a known type-system hole that produces runtime LSP
failures:

```java
Object[] objects = new String[1];  // compiles: String[] is a subtype of Object[]
objects[0] = 42;                   // ArrayStoreException at runtime
```

Generics were made invariant precisely to avoid this: `List<Object> l = new ArrayList<String>()`
does not compile.

### 4.4 Design by Contract

Bertrand Meyer's **Design by Contract** (Eiffel) makes contracts explicit: `require`
(preconditions), `ensure` (postconditions), and class `invariant`. Eiffel enforces the LSP rule for
redefinitions syntactically: a redefined routine can only use `require else` (weaken: OR with the
parent precondition) and `ensure then` (strengthen: AND with the parent postcondition).

Java has no built-in contracts. Approximate them with:

- Javadoc that states pre/postconditions and thrown exceptions precisely (the JDK collection
  interfaces are a good model).
- Guard clauses: `Objects.requireNonNull`, `Objects.checkIndex`, explicit
  `IllegalArgumentException`.
- Compact constructors in records to enforce invariants at construction.
- `assert` for internal invariants (disabled by default; enable with `-ea` in tests).
- **Contract tests**: one abstract test suite run against every implementation (see 4.8).

```java
public record Money(BigDecimal amount, Currency currency) {
    public Money {                                         // invariant enforced once
        Objects.requireNonNull(amount);
        Objects.requireNonNull(currency);
        if (amount.scale() > currency.getDefaultFractionDigits())
            throw new IllegalArgumentException("too many decimals for " + currency);
    }
}
```

### 4.5 Classic example: Rectangle and Square

```java
class Rectangle {
    protected int width, height;
    void setWidth(int w)  { width = w; }
    void setHeight(int h) { height = h; }
    int area() { return width * height; }
}

class Square extends Rectangle {
    @Override void setWidth(int w)  { width = w; height = w; }
    @Override void setHeight(int h) { width = h; height = h; }
}

static void stretch(Rectangle r) {
    r.setWidth(5);
    r.setHeight(4);
    assert r.area() == 20;   // fails for Square: 16
}
```

The implicit postcondition of `Rectangle.setWidth` is "width changes, height does not". `Square`
weakens it. The geometric "is-a" is true; the *behavioural* "is-a" is false. Two lessons:

1. LSP depends on the **contract**, and the contract depends on the mutators. If `Rectangle` and
   `Square` are immutable records, `Square` could validly be a `Rectangle` with equal sides,
   because there is no `setWidth` whose promise it could break.
2. Inheritance models *substitutable behaviour*, not real-world taxonomy.

### 4.6 Realistic violations

**Strengthened precondition: a gateway that refuses partial refunds.**

```java
/**
 * Refunds {@code amount} of a previous charge.
 * Pre:  0 < amount <= remaining refundable amount of the charge.
 * Post: returns a RefundResult; repeated calls with the same refundId have no extra effect.
 * Throws PaymentDeclinedException if the provider rejects the refund.
 */
public interface PaymentGateway {
    RefundResult refund(String chargeId, String refundId, BigDecimal amount);
}

public final class GiftCardGateway implements PaymentGateway {
    public RefundResult refund(String chargeId, String refundId, BigDecimal amount) {
        if (amount.compareTo(originalAmount(chargeId)) != 0)
            throw new UnsupportedOperationException("gift cards support full refunds only");
        return RefundResult.ok();
    }
    private BigDecimal originalAmount(String chargeId) { return BigDecimal.TEN; }
}
```

A returns workflow that issues partial refunds works for every gateway except gift cards, and it
fails at runtime. Fixes, in order of preference: change the contract so callers can ask
(`RefundCapabilities capabilities()`), split the interface (`FullRefunds`, `PartialRefunds`, which
is ISP), or implement partial refunds as a store-credit issuance inside the gateway so the
contract holds.

**Broken invariant: overdraft account.**

```java
public class Account {
    protected BigDecimal balance = BigDecimal.ZERO;
    /** Invariant: balance >= 0. Pre: amount > 0 and amount <= balance. */
    public void withdraw(BigDecimal amount) {
        if (amount.compareTo(balance) > 0) throw new InsufficientFundsException();
        balance = balance.subtract(amount);
    }
}

public class OverdraftAccount extends Account {
    private final BigDecimal limit = new BigDecimal("500");
    @Override public void withdraw(BigDecimal amount) {      // weakens the precondition (ok)
        if (amount.compareTo(balance.add(limit)) > 0) throw new InsufficientFundsException();
        balance = balance.subtract(amount);                  // but breaks the invariant (not ok)
    }
}
```

Weakening the precondition is allowed, but here it breaks the base invariant `balance >= 0`, which
interest calculation and statement code rely on. The fix is to put the invariant where it belongs:
the base type promises `balance >= -availableCredit()`, with `availableCredit()` being zero for
plain accounts.

**Weakened postcondition: unsorted "recent" query.** An interface promises
`findRecent(limit)` returns at most `limit` orders, newest first. A cached implementation returns
them in hash order. Every caller that shows "latest order" is now wrong, intermittently.

**Test doubles that lie.** An `InMemoryOrderRepository` used in tests does not enforce the unique
constraint on `externalId` that the real PostgreSQL table does. Tests pass; production throws
`DuplicateKeyException`. Fakes are subtypes too, and LSP applies to them. Contract tests (4.8)
catch this.

**Equality.** Overriding `equals` in a subclass that adds a field typically breaks symmetry or
transitivity (*Effective Java*, Item 10). There is no way to extend an instantiable class with a
value component and preserve the `equals` contract, short of giving up on substitutability or
using composition.

### 4.7 LSP violations in the JDK

**`Collections.unmodifiableList`, `List.of`, `Arrays.asList`.** `List` declares `add`, `remove`,
and `set` as **optional operations** that may throw `UnsupportedOperationException`. Strictly, this
is not an LSP violation because the contract says so. But it moves the problem to the caller: code
typed `List<T>` cannot know whether `add` will work. `Arrays.asList` supports `set` but not `add`,
so there are three different capability levels behind one type. This was a deliberate trade-off by
the collections designers to avoid an explosion of interfaces (mutable vs immutable vs
fixed-size). It is more accurately an **ISP** compromise that creates LSP-style surprises.

```java
List<String> names = Collections.unmodifiableList(new ArrayList<>(List.of("a")));
names.add("b");   // UnsupportedOperationException, although List "has" add
```

**`Stack extends Vector`.** `java.util.Stack` (JDK 1.0) inherits every `Vector` method, so a
"stack" supports `add(int, E)`, `remove(int)`, `insertElementAt`, and `get(i)`, which let callers
violate LIFO order. It is the textbook case of using inheritance for implementation reuse. The
`Stack` Javadoc itself recommends `Deque`, for example `ArrayDeque`.

**`java.sql.Timestamp extends java.util.Date`.** `Timestamp` adds nanoseconds. Its Javadoc states
that `Timestamp.equals(Object)` is **not symmetric** with `Date.equals(Object)`, that the
`hashCode` uses only the integral seconds of the underlying `Date`, and that the inheritance
"really denotes implementation inheritance, and not type inheritance", recommending that code not
treat a `Timestamp` as a `Date`.

**`java.sql.Date extends java.util.Date`.** `java.sql.Date` represents a date with no time of day.
Its time-related methods inherited from `java.util.Date` (`getHours`, `setMinutes`, and so on)
throw `IllegalArgumentException`, and `toInstant()` throws `UnsupportedOperationException`.
Passing a `java.sql.Date` to code written against `java.util.Date` can therefore fail. The modern
answer is `java.time.LocalDate` / `Instant`, which do not share a hierarchy.

**`Properties extends Hashtable<Object, Object>`.** `Properties` is meant to hold string keys and
values, but inherited `put` accepts any object. After `props.put("port", 8080)`,
`props.getProperty("port")` returns `null` because the value is not a `String`.

The common lesson: **all of these come from implementation inheritance used for reuse.**
Composition would have exposed only the intended operations.

### 4.8 Contract tests: making LSP checkable

```java
abstract class OrderRepositoryContract {
    protected abstract OrderRepository repository();

    @Test
    void savedOrderCanBeFoundById() {
        var order = Orders.sample("ext-1");
        repository().save(order);
        assertThat(repository().findById(order.id())).contains(order);
    }

    @Test
    void rejectsDuplicateExternalId() {
        repository().save(Orders.sample("ext-2"));
        assertThatThrownBy(() -> repository().save(Orders.sample("ext-2")))
                .isInstanceOf(DuplicateOrderException.class);
    }

    @Test
    void findRecentReturnsNewestFirstAndRespectsLimit() {
        /* ... */
    }
}

class InMemoryOrderRepositoryTest extends OrderRepositoryContract {
    private final InMemoryOrderRepository repo = new InMemoryOrderRepository();
    protected OrderRepository repository() { return repo; }
}

@Testcontainers
class JdbcOrderRepositoryTest extends OrderRepositoryContract {
    /* PostgreSQL container, real schema */
    protected OrderRepository repository() { return jdbcRepo; }
}
```

The contract is written once, as executable tests, and every implementation, including the fake
used by service tests, must pass it. This turns LSP from a review opinion into a CI gate.

### 4.9 Detecting LSP violations in code review

- An override throws `UnsupportedOperationException` or is an empty method body.
- Callers check `instanceof` or `getClass()` before calling a method on the base type.
- An override adds a guard (`if (x > limit) throw`) the base did not have: stronger precondition.
- An override catches and swallows an error the base would have propagated: weaker postcondition.
- A subclass that adds mutators to an immutable base, or caches results the base promised fresh.
- Javadoc on the override says "unlike the parent, this method...".
- A class extends a concrete class from another library just to reuse a few methods.

### 4.10 Trade-offs and when not to apply

- LSP is not optional for public subtyping, but you can **avoid the question** by not
  subclassing: prefer composition and small interfaces, and make classes `final` unless designed
  for extension (*Effective Java*, Item 19: "design and document for inheritance or else prohibit
  it").
- Some "optional operation" designs (JDK collections) are pragmatic trade-offs. When you choose
  one, document it in the contract and offer a capability query if callers need to branch.
- Perfectly substitutable implementations can still differ in **non-functional** behaviour
  (latency, memory, consistency). Whether that matters depends on what the contract promises: an
  eventually consistent repository behind an interface that implies read-your-writes is a
  violation.

### 4.11 Relation to patterns

Every pattern built on polymorphism (Strategy, Decorator, Proxy, Composite, Null Object, Template
Method) is only correct if the substitutes obey LSP. Null Object is a nice example of doing it
right: it satisfies the contract with neutral behaviour instead of throwing. Proxy and Decorator
must preserve the wrapped object's contract, including exceptions and idempotency.

---

## 5. I — Interface Segregation Principle

### 5.1 Definition and origin

> Clients should not be forced to depend upon interfaces that they do not use.

Martin describes the origin at Xerox: a printer system had one large `Job` class used by stapling,
printing, faxing, and other tasks. Every task depended on all of `Job`, so any change to it forced a
rebuild and redeploy of everything, and builds took about an hour. The fix was to put an interface
per client in front of `Job` (`StapleJob`, `PrintJob`, ...). Each task depended only on its own
interface; `Job` still implemented them all.

The key words are **"clients"** and **"depend"**. ISP is defined from the caller's side: what does
this particular client need? It is not a rule about how big an implementation may be.

### 5.2 Common misreadings

- "Every interface should have one method." Single-method interfaces are great for functional
  composition, but a `Deque` used as a deque genuinely needs many methods. The test is whether
  *each client* uses most of what it depends on.
- "ISP means splitting classes." The implementation can stay one class implementing several role
  interfaces (as at Xerox). ISP splits *dependencies*, not necessarily code.
- "ISP only matters in statically compiled languages." The recompilation argument is weaker in
  Java with incremental builds, but the design arguments remain: smaller blast radius, easier
  fakes, clearer intent, safer permissions.

### 5.3 Role interfaces vs header interfaces

Martin Fowler distinguishes:

- **Header interface**: an interface that mirrors all public methods of one class
  (`OrderService` / `OrderServiceImpl`). It adds an abstraction but no segregation.
- **Role interface**: an interface defined by what a specific client needs to do with a
  collaborator (`OrderPlacer`, `OrderLookup`). Names often end in `-er`, `-able`, or describe a
  capability.

Role interfaces are the ISP-friendly default.

### 5.4 Realistic violation: one fat service interface

```java
public interface OrderService {
    Order place(PlaceOrderCommand cmd);
    void cancel(OrderId id, String reason);
    Optional<Order> findById(OrderId id);
    List<Order> search(OrderSearchCriteria criteria);
    void refund(OrderId id, BigDecimal amount);
    void recalculateTaxes(LocalDate from, LocalDate to);    // finance batch
    void anonymise(CustomerId id);                          // GDPR job
    byte[] exportCsv(LocalDate day);                        // reporting
    void reassignWarehouse(OrderId id, WarehouseId to);     // ops tooling
}
```

Clients and what they use:

| Client | Uses | Forced to depend on |
| --- | --- | --- |
| `CheckoutController` | `place` | 8 other methods |
| `CustomerOrdersController` | `findById`, `cancel` | 7 other methods |
| `GdprErasureJob` | `anonymise` | 8 other methods |
| `FinanceTaxBatch` | `recalculateTaxes` | 8 other methods |

Consequences: every test for `CheckoutController` needs a mock with nine methods (or a mocking
framework that hides the problem); a signature change to `exportCsv` touches every client; a
read-only client technically has access to `anonymise`, which is a permissions smell.

### 5.5 Refactoring to role interfaces

```java
public interface PlaceOrder      { Order place(PlaceOrderCommand cmd); }
public interface CancelOrder     { void cancel(OrderId id, String reason); }
public interface OrderLookup     {
    Optional<Order> findById(OrderId id);
    List<Order> search(OrderSearchCriteria criteria);
}
public interface CustomerErasure { void anonymise(CustomerId id); }

// One implementation may still implement several roles.
@Service
class OrderApplicationService implements PlaceOrder, CancelOrder, OrderLookup {
    /* ... */
}

@RestController
class CheckoutController {
    private final PlaceOrder placeOrder;                  // depends only on what it uses
    CheckoutController(PlaceOrder placeOrder) { this.placeOrder = placeOrder; }
}
```

The controller test now needs a one-line lambda as a fake:

```java
var controller = new CheckoutController(cmd -> Orders.sample("ext-1"));
```

That works because `PlaceOrder` has a single abstract method, so it is a functional interface even
without the annotation.

### 5.6 ISP in the JDK: good examples

| Interface | Methods | Why it is a good role interface |
| --- | --- | --- |
| `Runnable`, `Callable<V>` | 1 | Executors need only "something to run" |
| `Comparable<T>` | 1 | Sorting needs only ordering |
| `AutoCloseable` / `Closeable` | 1 | try-with-resources needs only `close()` |
| `Iterable<T>` | 1 abstract | The enhanced `for` loop needs only `iterator()` |
| `CharSequence` | 4 abstract | String-like reading without committing to `String` |
| `Appendable`, `Readable`, `Flushable` | 1 to 3 | Separate write / read / flush capabilities |
| `java.util.function.*` | 1 | `Supplier`, `Function`, `Predicate`: maximal segregation |

The `Collection` hierarchy also segregates by capability: `Iterable` < `Collection` < `List` /
`Set` / `Queue` < `Deque`, and `SequencedCollection` (Java 21) was added as a role for "has a
defined encounter order with first/last access".

### 5.7 Fat interfaces in the JDK and frameworks

- **`java.sql.ResultSet`** has on the order of 190 methods (getters for every type by index and by
  label, updaters, cursor movement, metadata). Most code uses a handful. This is why libraries
  such as Spring's `JdbcTemplate` expose a narrow `RowMapper<T>` callback instead.
- **`java.util.List`** with optional mutators, discussed under LSP: a pragmatic refusal to
  segregate read-only and mutable roles.
- **`HttpServletRequest`** carries headers, cookies, sessions, auth, multipart parts, async
  support, and more. Frameworks wrap it so handlers receive only the parameters they declare.
- **Spring Data `JpaRepository`** inherits from `ListCrudRepository`,
  `ListPagingAndSortingRepository`, and `QueryByExampleExecutor`, so every repository gets
  `deleteAll()`, `flush()`, and batch operations whether or not the domain should allow them. The
  ISP-friendly alternative is to extend the marker `Repository<T, ID>` and declare only the methods
  you need:

```java
public interface CustomerRepository extends Repository<Customer, Long> {
    Optional<Customer> findById(Long id);
    Customer save(Customer customer);
    // no deleteAll(), no findAll() over millions of rows
}
```

- **Old Spring MVC `WebMvcConfigurerAdapter`** existed only so users did not have to implement every
  method of `WebMvcConfigurer`. Once Java 8 allowed default methods, the interface gained defaults
  and the adapter was deprecated (Spring 5). Default methods reduce the *implementation* burden of
  fat interfaces but not the *dependency* burden on clients, so they mitigate rather than solve ISP.

### 5.8 Detecting ISP violations in code review

- Implementations with methods that throw `UnsupportedOperationException` or do nothing.
- Mocks in tests stubbing methods unrelated to the test, or `mock()` used because writing a fake
  would require implementing twenty methods.
- An interface that mirrors exactly one class (`FooService` / `FooServiceImpl`) and is injected
  into many clients that each use a different subset.
- Clients receiving a powerful object (`EntityManager`, `JdbcTemplate`, a full repository) to do
  one small thing.
- Abstract "adapter" base classes whose only purpose is empty default implementations.

### 5.9 Trade-offs and when not to apply

- Too many tiny interfaces fragment the domain vocabulary and make discovery hard ("which of these
  twelve interfaces does `OrderApplicationService` implement?").
- Interfaces that are always used together should stay together. Splitting `OrderLookup` into
  `FindOrderById` and `SearchOrders` buys nothing if every client uses both.
- For internal code with one client, a concrete class is fine; segregation pays off when there are
  multiple clients with different needs, or at module / team boundaries.

### 5.10 Relation to patterns

Adapter (adapt a fat third-party API to a narrow role interface you own), Facade (one narrow
entry point over a complex subsystem), Proxy (expose a restricted view), and Command / Query
separation all apply ISP. Role interfaces are also what makes Strategy injection cheap.

---

## 6. D — Dependency Inversion Principle

### 6.1 Definition

> A. High-level modules should not depend on low-level modules. Both should depend on
> abstractions.
>
> B. Abstractions should not depend on details. Details should depend on abstractions.

- **High-level module**: policy. The business rules and use cases ("place an order, charge the
  customer, notify them").
- **Low-level module**: detail. Mechanisms like JDBC, Stripe's SDK, SMTP, Kafka, the file system,
  the HTTP framework.

### 6.2 What exactly is "inverted"

In naive layered design, source-code dependencies follow the flow of control: the service calls
the DAO, so the service imports the DAO. DIP inverts the **source-code dependency** so that it
points *against* the flow of control:

```text
Without DIP:  flow of control and source dependency point the same way

    OrderService ───────────────────────► JdbcOrderDao
    (policy)        imports / calls          (detail)

With DIP:     control still flows right, but the source dependency points left

    OrderService ──calls──► OrderRepository ◄──implements── JdbcOrderRepository
    (policy)                (abstraction,                    (detail)
                             owned by policy)
```

The second, subtler inversion is **ownership**. The interface belongs to the high-level module and
is shaped by its needs (`OrderRepository.save(Order)`), not by the database (`executeUpdate(sql)`).
If the interface lives in the persistence package and mirrors JDBC concepts, you have an
abstraction but not inversion: the policy still depends on the detail's package and vocabulary.

### 6.3 DIP vs dependency injection vs IoC vs containers

These terms are often conflated. They are related but different:

| Term | Kind | What it means | Example |
| --- | --- | --- | --- |
| Dependency Inversion (DIP) | Design principle | Direction and ownership of source dependencies | `OrderService` depends on `OrderRepository` it owns |
| Dependency Injection (DI) | Technique | A component receives its dependencies from outside instead of creating them | Constructor parameter `OrderService(OrderRepository repo)` |
| Inversion of Control (IoC) | General principle | A framework calls your code rather than you calling it ("Hollywood principle") | Servlet container calls `doGet`; JUnit calls `@Test` methods |
| IoC / DI container | Tool | A library that builds the object graph and injects dependencies | Spring, Guice, Dagger, Micronaut, CDI |

Key relationships:

- You can do **DI without DIP**: injecting a concrete `JdbcOrderDao` into `OrderService` is
  injection, but the policy still depends on the detail.
- You can do **DIP without a container**: a `main` method that calls constructors (a "composition
  root", sometimes called pure DI) is enough.
- A container is a convenience for wiring large graphs; it is not the principle. Martin Fowler's
  2004 article *Inversion of Control Containers and the Dependency Injection pattern* introduced the
  term "dependency injection" to be more specific than "IoC".

Prefer **constructor injection**: dependencies are explicit, fields can be `final`, objects are
fully initialised, and tests can construct the class without a container. Field injection
(`@Autowired` on a private field) hides dependencies and requires reflection or a container in
tests.

### 6.4 Realistic violation

```java
public class SubscriptionRenewalService {
    private final StripeClient stripe = new StripeClient(System.getenv("STRIPE_KEY"));
    private final SubscriptionDao dao = new SubscriptionDao(DataSourceHolder.get());

    public void renewDue() {
        for (var sub : dao.findDue(LocalDate.now())) {          // hidden clock dependency
            var charge = stripe.charges().create(sub.customerRef(), sub.priceCents(), "inr");
            if (charge.status().equals("succeeded")) {
                dao.markRenewed(sub.id(), LocalDate.now().plusMonths(1));
            }
        }
    }
}
```

Problems: the renewal rule cannot be tested without Stripe and a database; `LocalDate.now()` makes
date logic untestable; Stripe's vocabulary (`"succeeded"`, cents, currency strings) leaks into
business code; switching to Razorpay touches the policy. (`StripeClient` here is a stand-in for any
vendor SDK; check the real SDK's API.)

### 6.5 Refactoring step by step

1. **Name the policy's needs** in its own words: "find subscriptions due on a date", "charge a
   customer an amount", "record a renewal", "know today's date".
2. **Define ports** (interfaces) in the policy's package with domain types.
3. **Move vendor code into adapters** that implement those ports and translate vocabularies.
4. **Inject** the ports through the constructor; use `java.time.Clock` for time.
5. **Wire** in a composition root or Spring configuration.
6. **Test** the policy with fakes.

```java
// --- domain / application (policy) ---
public record Subscription(SubscriptionId id, CustomerId customer, Money price, LocalDate dueOn) {}

public sealed interface ChargeOutcome permits ChargeOutcome.Paid, ChargeOutcome.Declined {
    record Paid(String reference) implements ChargeOutcome {}
    record Declined(String reason) implements ChargeOutcome {}
}

public interface Subscriptions {                       // port, owned by policy
    List<Subscription> dueOn(LocalDate day);
    void renewed(SubscriptionId id, LocalDate nextDue);
}

public interface Payments {                            // port, owned by policy
    ChargeOutcome charge(CustomerId customer, Money amount, String idempotencyKey);
}

public final class SubscriptionRenewalService {
    private final Subscriptions subscriptions;
    private final Payments payments;
    private final Clock clock;

    public SubscriptionRenewalService(Subscriptions subscriptions, Payments payments, Clock clock) {
        this.subscriptions = subscriptions;
        this.payments = payments;
        this.clock = clock;
    }

    public void renewDue() {
        LocalDate today = LocalDate.now(clock);
        for (var sub : subscriptions.dueOn(today)) {
            var key = sub.id() + ":" + sub.dueOn();          // stable across retries
            switch (payments.charge(sub.customer(), sub.price(), key)) {
                case ChargeOutcome.Paid paid -> subscriptions.renewed(sub.id(), sub.dueOn().plusMonths(1));
                case ChargeOutcome.Declined declined -> { /* dunning flow */ }
            }
        }
    }
}

// --- adapter (detail) ---
public final class StripePayments implements Payments {
    private final StripeClient stripe;                       // vendor SDK stays here
    public StripePayments(StripeClient stripe) { this.stripe = stripe; }

    @Override
    public ChargeOutcome charge(CustomerId customer, Money amount, String idempotencyKey) {
        var charge = stripe.charges().create(customer.value(),
                amount.amount().movePointRight(2).longValueExact(),
                amount.currency().getCurrencyCode().toLowerCase(Locale.ROOT));
        return "succeeded".equals(charge.status())
                ? new ChargeOutcome.Paid(charge.id())
                : new ChargeOutcome.Declined(charge.failureMessage());
    }
}
```

Note `movePointRight(2)` assumes a two-decimal currency; real code should use the currency's
fraction digits. That translation logic is exactly the kind of detail that belongs in the adapter.

### 6.6 Ports and adapters (hexagonal architecture)

Alistair Cockburn's **hexagonal architecture** (2005), also called ports and adapters, is DIP
applied to a whole application. The core contains domain and use cases and defines **ports**:

- **Driving (primary, inbound) ports**: interfaces the outside world calls, such as
  `PlaceOrderUseCase`. Adapters: REST controllers, message consumers, CLI, tests.
- **Driven (secondary, outbound) ports**: interfaces the core needs, such as `OrderRepository`.
  Adapters: JDBC, HTTP clients, SMTP, Kafka producers.

```text
 ┌────────────────────┐     ┌──────────────────────────────┐     ┌────────────────────┐
 │  Driving adapters  │     │       Application core       │     │  Driven adapters   │
 │                    │     │                              │     │                    │
 │  REST controller   │────►│  PlaceOrderUseCase (in-port) │     │  JdbcOrderRepo     │
 │  Kafka consumer    │────►│  PlaceOrderService           │     │  StripeGateway     │
 │  CLI / tests       │────►│                              │     │  SmtpNotifier      │
 │                    │     │  OrderRepository  (out-port) │◄────│                    │
 │                    │     │  PaymentGateway   (out-port) │◄────│  (implement ports) │
 │                    │     │  OrderNotifier    (out-port) │◄────│                    │
 └────────────────────┘     └──────────────────────────────┘     └────────────────────┘

 Every source-code arrow points INTO the core. The core imports nothing from the adapters.
```

A typical package layout that makes the rule visible (and enforceable with ArchUnit or JPMS
modules):

```text
com.shop.order
├── domain          Order, OrderLine, Money, pricing rules         (no framework imports)
├── application     PlaceOrderService, ports: in/ and out/         (no framework imports)
├── adapter
│   ├── in.web      OrderController (Spring MVC)
│   ├── out.jdbc    JdbcOrderRepository
│   └── out.stripe  StripePaymentGateway
└── config          Spring @Configuration: the composition root
```

Clean Architecture's concentric circles and Jeffrey Palermo's onion architecture express the same
dependency rule: **source dependencies point inward, toward policy.**

### 6.7 Testing with fakes

Once dependencies are ports, tests can replace them. Test-double vocabulary (Gerard Meszaros,
*xUnit Test Patterns*):

| Double | What it does | Good for |
| --- | --- | --- |
| Dummy | Passed but never used | Filling parameters |
| Stub | Returns canned answers | Driving a code path |
| Spy | Records calls for later assertion | Checking outbound notifications |
| Mock | Pre-programmed with expectations, verifies interactions | Protocol-heavy collaborations |
| Fake | Working lightweight implementation (in-memory repo) | Most service-level tests |

Fakes plus state-based assertions are usually more robust than mocks: tests assert *what
happened* (the order is saved, the customer was charged once) rather than *how* (which method was
called in what order), so refactoring internals does not break them.

```java
final class InMemorySubscriptions implements Subscriptions {
    final Map<SubscriptionId, Subscription> store = new HashMap<>();
    final Map<SubscriptionId, LocalDate> renewals = new HashMap<>();
    public List<Subscription> dueOn(LocalDate day) {
        return store.values().stream().filter(s -> s.dueOn().equals(day)).toList();
    }
    public void renewed(SubscriptionId id, LocalDate nextDue) { renewals.put(id, nextDue); }
}

final class FakePayments implements Payments {
    final Set<CustomerId> declining = new HashSet<>();
    final List<String> keys = new ArrayList<>();
    public ChargeOutcome charge(CustomerId c, Money amount, String key) {
        keys.add(key);
        return declining.contains(c) ? new ChargeOutcome.Declined("card_declined")
                                     : new ChargeOutcome.Paid("ch_" + keys.size());
    }
}

@Test
void renewsPaidSubscriptionsForOneMonth() {
    var subs = new InMemorySubscriptions();
    var payments = new FakePayments();
    var today = LocalDate.of(2026, 10, 31);
    var clock = Clock.fixed(today.atStartOfDay(ZoneOffset.UTC).toInstant(), ZoneOffset.UTC);
    var sub = new Subscription(new SubscriptionId("s1"), new CustomerId("c1"),
            new Money(new BigDecimal("499.00"), Currency.getInstance("INR")), today);
    subs.store.put(sub.id(), sub);

    new SubscriptionRenewalService(subs, payments, clock).renewDue();

    assertThat(subs.renewals).containsEntry(sub.id(), LocalDate.of(2026, 11, 30));
}
```

`LocalDate.of(2026, 10, 31).plusMonths(1)` is 30 November 2026, because `plusMonths` clamps to the
last valid day of the month. That is the kind of edge case these fast, deterministic tests let you
pin down. Keep fakes honest with the contract tests from 4.8, and keep a small number of
integration tests (Testcontainers, a provider's sandbox) for the real adapters.

### 6.8 Detecting DIP violations in code review

- `new` of infrastructure classes inside business logic (`new RestTemplate()`, `new
  StripeClient(...)`, `DriverManager.getConnection`).
- Static calls to globals: `LocalDate.now()`, `UUID.randomUUID()`, `System.getenv`, singletons
  (`Config.getInstance()`), in code that should be deterministic.
- Domain or application packages importing `org.springframework.jdbc`, `jakarta.persistence`,
  vendor SDKs, or HTTP types.
- Port interfaces that expose infrastructure types (`ResultSet`, `HttpResponse`, `SQLException`,
  vendor DTOs).
- Tests for business rules that need Spring context, a database, or network to run.

### 6.9 Trade-offs and when not to apply

- **Stable dependencies do not need inversion.** Depending directly on `String`, `java.time`,
  `BigDecimal`, or a stable in-house library is fine. DIP is for *volatile* details.
- **Interface per class is not DIP.** `FooService` + `FooServiceImpl` with one implementation that
  is never faked adds files and indirection without decoupling anything. Since Mockito can mock
  classes, testability alone is not a reason for the interface either.
- **Small CRUD services** where the "business logic" is mapping requests to tables may not
  benefit from a full hexagon. Layered architecture with Spring Data repositories can be the right
  call; introduce ports when real policy appears.
- **Leaky abstractions.** A `Repository` port can hide transactions, locking, and consistency only
  so far. Do not pretend a remote call is a local one; model failures (timeouts, declined, unknown
  outcome) in the port's types.

### 6.10 Relation to patterns

Abstract Factory and Factory Method (create details without naming them), Adapter (translate a
vendor API to your port), Template Method and Observer (framework-style IoC), Service Locator (an
alternative to DI that hides dependencies; generally discouraged), Plugin, and Repository. The
Stable Dependencies Principle and Stable Abstractions Principle are DIP at the component level.

---

## 7. End-to-end case study: refactoring one messy class

### 7.1 The starting point

A real-looking checkout class from a young codebase. `StripeClient`, `PayPalClient`, and
`SmtpMailer` stand in for vendor SDK wrappers.

```java
public class OrderManager {
    private final Connection conn;

    public OrderManager() throws SQLException {
        this.conn = DriverManager.getConnection("jdbc:postgresql://db/shop", "app", "secret");
    }

    public String placeOrder(String customerId, String customerType, List<String[]> lines,
                             String paymentType, String cardToken, String email) throws Exception {
        if (lines.isEmpty()) throw new IllegalArgumentException("empty order");

        BigDecimal total = BigDecimal.ZERO;
        for (String[] l : lines) {                                    // [sku, qty, unitPrice]
            total = total.add(new BigDecimal(l[2]).multiply(new BigDecimal(l[1])));
        }
        switch (customerType) {
            case "PREMIUM" -> total = total.multiply(new BigDecimal("0.90"));
            case "VIP"     -> total = total.multiply(new BigDecimal("0.80"));
            default        -> { }
        }
        total = total.setScale(2, RoundingMode.HALF_EVEN);

        String chargeId;
        if (paymentType.equals("CARD")) {
            chargeId = new StripeClient("sk_live_xxx").charge(cardToken, total);
        } else if (paymentType.equals("PAYPAL")) {
            chargeId = new PayPalClient().pay(email, total);
        } else {
            throw new IllegalArgumentException("unsupported payment " + paymentType);
        }

        String orderId = UUID.randomUUID().toString();
        try (var ps = conn.prepareStatement(
                "INSERT INTO orders(id, customer_id, total, charge_id, created_at) VALUES (?,?,?,?,?)")) {
            ps.setString(1, orderId);
            ps.setString(2, customerId);
            ps.setBigDecimal(3, total);
            ps.setString(4, chargeId);
            ps.setTimestamp(5, Timestamp.from(Instant.now()));
            ps.executeUpdate();
        }

        new SmtpMailer("smtp.shop.com").send(email, "Order confirmed",
                "Thanks! Order " + orderId + " total " + total);
        return orderId;
    }
}
```

### 7.2 Diagnosis, principle by principle

| Problem | Principle | Evidence |
| --- | --- | --- |
| Validation, pricing, payment, persistence, and email in one method | SRP | Product, finance, payments, platform, and marketing can all force edits |
| `switch (customerType)` and `if (paymentType...)` | OCP | Every new tier or payment provider edits this method |
| `PayPalClient.pay` ignores `cardToken`; Stripe ignores `email` | ISP / LSP | One parameter list serves incompatible payment methods |
| `new StripeClient`, `DriverManager`, `new SmtpMailer`, `Instant.now()`, `UUID` | DIP | Cannot test without network, DB, SMTP; secrets in code |
| `String[]` lines, `String` types for enums | General (primitive obsession) | Invalid states representable; no compiler help |

Also note the hidden correctness bug: the customer is charged **before** the order is saved. If
the insert fails, money is taken with no order. The refactoring makes this ordering explicit and
testable.

### 7.3 Step 1: characterise and introduce domain types

Write one test against the current behaviour (with a disposable database), then replace stringly
typed inputs with records and sealed types. Invalid states become unrepresentable:

```java
public record OrderLine(String sku, int quantity, BigDecimal unitPrice) {
    public OrderLine {
        if (quantity <= 0) throw new IllegalArgumentException("quantity must be positive");
        if (unitPrice.signum() < 0) throw new IllegalArgumentException("negative price");
    }
    BigDecimal subtotal() { return unitPrice.multiply(BigDecimal.valueOf(quantity)); }
}

public enum CustomerTier { REGULAR, PREMIUM, VIP }

public sealed interface PaymentMethod permits PaymentMethod.Card, PaymentMethod.PayPal {
    record Card(String token) implements PaymentMethod {}
    record PayPal(String payerEmail) implements PaymentMethod {}
}

public record PlaceOrderCommand(String customerId, CustomerTier tier, List<OrderLine> lines,
                                PaymentMethod payment, String email) {
    public PlaceOrderCommand {
        lines = List.copyOf(lines);
        if (lines.isEmpty()) throw new IllegalArgumentException("empty order");
    }
}

public record Order(String id, String customerId, BigDecimal total, String chargeId, Instant createdAt) {}
```

Each payment method now carries only its own data, which already fixes the ISP/LSP problem of the
shared parameter list.

### 7.4 Step 2 (SRP + OCP): extract pricing

```java
public interface PricingPolicy {
    BigDecimal price(CustomerTier tier, List<OrderLine> lines);
}

public final class TieredPricing implements PricingPolicy {
    private final Map<CustomerTier, BigDecimal> multipliers;

    public TieredPricing(Map<CustomerTier, BigDecimal> multipliers) {
        this.multipliers = Map.copyOf(multipliers);       // tiers come from configuration
    }

    @Override
    public BigDecimal price(CustomerTier tier, List<OrderLine> lines) {
        BigDecimal subtotal = lines.stream().map(OrderLine::subtotal)
                .reduce(BigDecimal.ZERO, BigDecimal::add);
        return subtotal.multiply(multipliers.getOrDefault(tier, BigDecimal.ONE))
                .setScale(2, RoundingMode.HALF_EVEN);
    }
}
```

Finance's rules live in one class with fast unit tests. A new tier is a configuration change. A
completely different pricing model (coupons, volume discounts) is a new `PricingPolicy`
implementation or a decorator around this one.

### 7.5 Step 3 (DIP + ISP): define ports owned by the use case

```java
public interface PaymentGateway {
    boolean supports(PaymentMethod method);
    /** Charges exactly once per idempotencyKey. Returns the provider reference. */
    String charge(PaymentMethod method, BigDecimal amount, String idempotencyKey);
}

public interface OrderRepository { void save(Order order); }

public interface OrderNotifier { void orderPlaced(Order order, String email); }
```

Each port is a role interface with the minimum the use case needs. The `charge` contract states
idempotency, so retrying decorators are safe and every adapter must honour it (LSP).

### 7.6 Step 4 (OCP + LSP): payment routing

```java
public final class PaymentRouter {
    private final List<PaymentGateway> gateways;

    public PaymentRouter(List<PaymentGateway> gateways) { this.gateways = List.copyOf(gateways); }

    public String charge(PaymentMethod method, BigDecimal amount, String idempotencyKey) {
        return gateways.stream()
                .filter(g -> g.supports(method))
                .findFirst()
                .orElseThrow(() -> new IllegalStateException("no gateway for " + method))
                .charge(method, amount, idempotencyKey);
    }
}

public final class StripeCardGateway implements PaymentGateway {
    private final StripeClient stripe;
    public StripeCardGateway(StripeClient stripe) { this.stripe = stripe; }

    public boolean supports(PaymentMethod m) { return m instanceof PaymentMethod.Card; }

    public String charge(PaymentMethod m, BigDecimal amount, String key) {
        var card = (PaymentMethod.Card) m;                 // guarded by supports()
        return stripe.charge(card.token(), amount, key);
    }
}
```

Adding UPI: add a `Upi` record to the sealed `PaymentMethod` and a `UpiGateway` class. The router
and use case do not change. Exhaustive switches elsewhere (for example fee display) are flagged by
the compiler, which is the sealed-type counterpoint working alongside OCP.

### 7.7 Step 5: the use case, adapters, and wiring

```java
public final class PlaceOrderService {
    private final PricingPolicy pricing;
    private final PaymentRouter payments;
    private final OrderRepository orders;
    private final OrderNotifier notifier;
    private final Clock clock;
    private final Supplier<String> ids;

    public PlaceOrderService(PricingPolicy pricing, PaymentRouter payments, OrderRepository orders,
                             OrderNotifier notifier, Clock clock, Supplier<String> ids) {
        this.pricing = pricing;
        this.payments = payments;
        this.orders = orders;
        this.notifier = notifier;
        this.clock = clock;
        this.ids = ids;
    }

    public Order place(PlaceOrderCommand cmd) {
        String orderId = ids.get();
        BigDecimal total = pricing.price(cmd.tier(), cmd.lines());
        String chargeId = payments.charge(cmd.payment(), total, "order-" + orderId);
        var order = new Order(orderId, cmd.customerId(), total, chargeId, clock.instant());
        orders.save(order);
        notifier.orderPlaced(order, cmd.email());
        return order;
    }
}
```

The charge-then-save ordering is still a distributed-consistency problem (payment succeeded, save
failed). Now it is visible in a twenty-line method and can be addressed explicitly: save a
`PENDING` order first, charge with an idempotency key derived from the order id, then mark it
`PAID`; or use an outbox for the notification. SOLID did not solve that problem, but it made it
obvious and testable.

Adapters (`JdbcOrderRepository`, `EmailOrderNotifier`, `StripeCardGateway`, `PayPalGateway`) live
in adapter packages. Wiring, as a Spring configuration (the composition root):

```java
@Configuration
class OrderConfig {
    @Bean PricingPolicy pricing() {
        return new TieredPricing(Map.of(
                CustomerTier.PREMIUM, new BigDecimal("0.90"),
                CustomerTier.VIP, new BigDecimal("0.80")));
    }

    @Bean PaymentRouter paymentRouter(List<PaymentGateway> gateways) {
        return new PaymentRouter(gateways);
    }

    @Bean PlaceOrderService placeOrderService(PricingPolicy pricing, PaymentRouter router,
                                              OrderRepository orders, OrderNotifier notifier) {
        return new PlaceOrderService(pricing, router, orders, notifier,
                Clock.systemUTC(), () -> UUID.randomUUID().toString());
    }
}
```

### 7.8 Step 6: the payoff in tests

```java
@Test
void vipCustomerIsChargedDiscountedTotalAndOrderIsSaved() {
    var saved = new ArrayList<Order>();
    var notified = new ArrayList<String>();
    var gateway = new PaymentGateway() {
        BigDecimal charged;
        public boolean supports(PaymentMethod m) { return true; }
        public String charge(PaymentMethod m, BigDecimal amount, String key) {
            charged = amount;
            return "ch_1";
        }
    };
    var service = new PlaceOrderService(
            new TieredPricing(Map.of(CustomerTier.VIP, new BigDecimal("0.80"))),
            new PaymentRouter(List.of(gateway)),
            saved::add,                                         // OrderRepository is a SAM
            (order, email) -> notified.add(email),              // so is OrderNotifier
            Clock.fixed(Instant.parse("2026-10-31T10:00:00Z"), ZoneOffset.UTC),
            () -> "o-1");

    var order = service.place(new PlaceOrderCommand("c-1", CustomerTier.VIP,
            List.of(new OrderLine("sku-1", 2, new BigDecimal("50.00"))),
            new PaymentMethod.Card("tok"), "a@b.com"));

    assertThat(gateway.charged).isEqualByComparingTo("80.00");
    assertThat(saved).containsExactly(order);
    assertThat(notified).containsExactly("a@b.com");
}
```

No database, no network, no Spring context, runs in milliseconds. ISP (single-method ports) makes
the fakes one-liners.

### 7.9 Summary of the transformation

```text
Before                                     After
──────────────────────────────────────     ──────────────────────────────────────────
1 class, 1 method, ~50 lines               ~12 small types, each with one actor
switch on tier, if-chain on payment        TieredPricing config + PaymentRouter list
String[] lines, String enums               records, enum, sealed PaymentMethod
new StripeClient / DriverManager / SMTP    ports owned by use case, adapters outside
Instant.now(), UUID.randomUUID()           Clock and Supplier<String> injected
Tests need DB + Stripe + SMTP              Unit tests with lambdas; contract tests
Charge-before-save bug hidden              Ordering visible in a 20-line method
```

---

## 8. SOLID in a functional style

The principles are about dependencies and change, so they carry over to functional code with
different mechanics:

| Principle | OOP mechanism | Functional mechanism |
| --- | --- | --- |
| SRP | Small cohesive classes | Small pure functions; separate pure core from effectful shell |
| OCP | Strategy / Decorator objects | Higher-order functions; function composition (`andThen`, `compose`) |
| LSP | Subtypes honour contracts | Any function of type `A -> B` must honour the same laws (totality, purity, invariants on outputs) |
| ISP | Role interfaces | Pass exactly the functions needed (`Function<OrderId, Order>`) instead of a whole repository |
| DIP | Interfaces + injection | Pass functions as parameters; partial application; "functional core, imperative shell" |

Example: pricing as composable functions. Each rule is a `UnaryOperator<BigDecimal>`; adding a
rule is composition, not modification:

```java
UnaryOperator<BigDecimal> tierDiscount(CustomerTier tier) {
    return switch (tier) {
        case REGULAR -> p -> p;
        case PREMIUM -> p -> p.multiply(new BigDecimal("0.90"));
        case VIP     -> p -> p.multiply(new BigDecimal("0.80"));
    };
}

Function<BigDecimal, BigDecimal> coupon(BigDecimal off) {
    return p -> p.subtract(off).max(BigDecimal.ZERO);
}

Function<BigDecimal, BigDecimal> pipeline = tierDiscount(CustomerTier.VIP)
        .andThen(coupon(new BigDecimal("10")))
        .andThen(p -> p.setScale(2, RoundingMode.HALF_EVEN));
```

DIP as function parameters: the use case receives only the capabilities it needs.

```java
static Order place(PlaceOrderCommand cmd,
                   Function<PlaceOrderCommand, BigDecimal> price,
                   BiFunction<PaymentMethod, BigDecimal, String> charge,
                   Consumer<Order> save,
                   Supplier<Instant> now,
                   Supplier<String> newId) {
    var total = price.apply(cmd);
    var order = new Order(newId.get(), cmd.customerId(), total,
            charge.apply(cmd.payment(), total), now.get());
    save.accept(order);
    return order;
}
```

Trade-off: past three or four function parameters, named interfaces (or a record of functions)
read better than anonymous `BiFunction`s, because the names document the contract. Java's
functional interfaces are themselves the ultimate ISP: one abstract method each.

The "**functional core, imperative shell**" pattern (Gary Bernhardt) is DIP by construction: the
core is pure functions over data and has no dependencies at all; the shell does I/O and calls the
core. It is also the easiest code to test.

---

## 9. How the five fit together

```text
   SRP ── decides WHERE to cut: one actor per module
    │
    ▼
   ISP ── decides the SHAPE of each seam: small, client-owned role interfaces
    │
    ▼
   DIP ── decides the DIRECTION of each seam: details depend on policy's abstractions
    │
    ▼
   OCP ── is the PAYOFF: new behaviour = new implementation plugged into a seam
    │
    ▼
   LSP ── is the SAFETY CONDITION: every implementation honours the seam's contract
```

Martin's component-level principles extend the same ideas to packages and modules: Reuse/Release
Equivalence, Common Closure (SRP for components), Common Reuse (ISP for components), Acyclic
Dependencies, Stable Dependencies, and Stable Abstractions (DIP for components).

---

## 10. Related principles

| Principle | Statement | Source | Relation to SOLID |
| --- | --- | --- | --- |
| DRY | "Every piece of knowledge must have a single, unambiguous, authoritative representation within a system." | Hunt and Thomas, *The Pragmatic Programmer* (1999) | About *knowledge*, not text. Merging two similar-looking rules owned by different actors violates SRP. |
| KISS | Keep it simple | Attributed to Kelly Johnson (Lockheed) | Counterweight to over-applied OCP / DIP |
| YAGNI | Do not build it until you need it | Extreme Programming (Beck, Jeffries) | Introduce abstractions when the second variant arrives |
| Law of Demeter | A method should only talk to its object, its parameters, objects it creates, and its direct fields ("only talk to your friends") | Holland and Lieberherr, Northeastern, 1987 | Reduces coupling; `order.getCustomer().getAddress().getCity()` chains are the smell |
| Composition over inheritance | "Favor object composition over class inheritance." | GoF, *Design Patterns* (1994); *Effective Java* Item 18 | Avoids most LSP problems; enables Strategy / Decorator |
| Tell, don't ask | Ask objects to do work rather than pulling their data and deciding outside | Pragmatic Programmers | Keeps behaviour with data (Information Expert) |
| Separation of concerns | Divide a program into distinct sections, each addressing one concern | Dijkstra, 1974 | The general idea behind SRP |

**DRY vs SRP tension.** Two functions computing "hours worked" for payroll and for operations may
look identical today. If they belong to different actors, they are *accidental* duplication, and
merging them recreates the `Employee` problem from 2.3. Duplicate knowledge must be merged;
duplicate *text* that changes for different reasons should stay separate.

**Law of Demeter caveat.** Fluent builders and stream pipelines (`list.stream().map(...).toList()`)
are not violations: they operate on the same conceptual object or return new values. The rule
targets navigating through other objects' internals.

### GRASP, briefly

Craig Larman's GRASP (*Applying UML and Patterns*) is nine principles for assigning
responsibilities to classes, and overlaps heavily with SOLID:

| GRASP principle | Idea | SOLID overlap |
| --- | --- | --- |
| Information Expert | Give a responsibility to the class that has the data | Cohesion, Tell-don't-ask |
| Creator | The class that contains / aggregates / closely uses B should create B | Factories, DIP for wiring |
| Controller | A non-UI object handles system events (use case controller) | Application services |
| Low Coupling | Assign responsibilities to keep coupling low | All five |
| High Cohesion | Keep responsibilities focused | SRP |
| Polymorphism | Handle type-based variation with polymorphic operations | OCP, LSP |
| Pure Fabrication | Invent a class not in the domain to achieve cohesion (e.g. a repository) | SRP, DIP |
| Indirection | Introduce an intermediate to decouple | DIP, Adapter |
| Protected Variations | Wrap points of predicted variation behind a stable interface | OCP (Larman calls OCP essentially equivalent) |

---

## Common mistakes

1. **Interface for every class.** `XService` / `XServiceImpl` pairs with one implementation are
   ceremony, not DIP. Abstract where there is variation, a team/module boundary, or volatile
   infrastructure.
2. **Reading SRP as "one method" or "do one thing".** It is about actors and reasons to change.
3. **Speculative OCP.** Strategy hierarchies for variations that never arrive. Wait for the second
   case.
4. **Inheritance for reuse.** `Stack extends Vector` in your own code. Prefer composition and
   `final` classes.
5. **Throwing `UnsupportedOperationException` from an implementation** instead of fixing the
   interface (ISP) or the contract (LSP).
6. **Ports that leak infrastructure**: `SQLException`, `ResultSet`, vendor DTOs, or HTTP status
   codes in domain interfaces.
7. **Mock-heavy tests** that verify interaction order. They freeze the implementation and make
   refactoring (the whole point of SOLID) expensive. Prefer fakes and contract tests.
8. **Field injection and hidden statics** (`LocalDate.now()`, singletons) that defeat DIP while
   appearing to use a DI container.
9. **`default` branches on sealed switches**, which discard the compiler's exhaustiveness check.
10. **Merging accidental duplication** in the name of DRY, coupling two actors together.
11. **Treating SOLID as a goal.** The goal is cheap, safe change. Measure by how painful the last
    few feature changes were, not by how many principles a design ticks.

---

## Interview questions

**1. What does SRP actually mean? Is "a class should do one thing" correct?**
No. SRP says a module should be responsible to one actor, that is, have one source of change
requests. "Do one thing" is advice for functions. A class may have many methods if they all
change for the same stakeholder. The test is: which people or teams could ask for this code to
change? If more than one, consider splitting along that line.

**2. Explain the Open/Closed Principle and how you would apply it in a payment system.**
Code should be open for extension and closed for modification along expected axes of change.
Define a `PaymentProcessor` interface, implement one class per provider, and select via a registry
built from injected beans. Adding a provider adds a class and touches nothing else. Cross-cutting
concerns (retry, metrics) are decorators. I would only introduce this once there are at least two
providers or one clearly on the roadmap.

**3. Do sealed interfaces and pattern matching violate OCP?**
For types, yes, deliberately: adding a subtype requires editing every switch. But exhaustive
switches without `default` make the compiler list every place to change, and adding a new
operation becomes trivial. It is the expression problem: choose sealed types for small, stable,
self-owned variant sets (domain states, results, events); choose interfaces for open sets
contributed by other teams or plugins.

**4. Why is Square extending Rectangle an LSP violation, and how do you fix it?**
`Rectangle.setWidth` implicitly promises the height does not change. `Square` breaks that
postcondition, so code written for rectangles fails with squares. Fixes: make shapes immutable
(then a square can be a rectangle with equal sides), or do not relate them by inheritance; both
implement a `Shape` interface with `area()`.

**5. List the formal LSP rules.**
Preconditions cannot be strengthened; postconditions cannot be weakened; invariants must be
preserved; the history constraint (a subtype must not allow state changes the supertype forbids,
e.g. adding mutators to an immutable type); argument types contravariant, return types covariant;
no new exceptions outside those the supertype allows. Java enforces covariant returns and checked
exception narrowing; the rest is contracts and tests.

**6. Give LSP violations from the JDK.**
`Stack extends Vector` exposes index-based insertion that breaks LIFO. `java.sql.Timestamp extends
java.util.Date` has an asymmetric `equals`, and its Javadoc says the inheritance is implementation
inheritance only. `java.sql.Date` throws from time-of-day methods and `toInstant()`. `Properties
extends Hashtable` accepts non-String values that `getProperty` then ignores. Unmodifiable lists
throw from `add`, which the `List` contract permits as an optional operation but which surprises
callers. Also covariant arrays and `ArrayStoreException`.

**7. How do you check LSP in practice, given Java cannot enforce contracts?**
Document pre/postconditions in Javadoc, enforce invariants in constructors (record compact
constructors), and write contract tests: an abstract test class run against every implementation,
including in-memory fakes used by other tests. In review, look for `UnsupportedOperationException`,
`instanceof` checks on the base type, and overrides adding guards.

**8. What is ISP, and how is it different from SRP?**
ISP is about clients: no client should depend on methods it does not use. SRP is about
implementations and actors. One class can satisfy SRP and still be exposed through a fat interface
to many clients; ISP says give each client a role interface. Conversely, a class can implement many
role interfaces (as in Martin's Xerox example) and the clients are still segregated.

**9. Give a framework example of a fat interface and how to avoid it.**
Spring Data's `JpaRepository` gives every repository `deleteAll`, `findAll`, `flush` and so on.
Extend the `Repository<T, ID>` marker and declare only the needed methods. Another is
`java.sql.ResultSet`; `JdbcTemplate` hides it behind a `RowMapper` callback.

**10. Distinguish DIP, dependency injection, IoC, and a DI container.**
DIP is a design principle about the direction and ownership of source dependencies: policy owns
the abstraction, details implement it. DI is the technique of passing dependencies in. IoC is the
broader idea that a framework calls your code. A container (Spring, Guice, Dagger) automates DI.
You can inject concrete classes (DI without DIP), and you can follow DIP with a hand-written
composition root (DIP without a container).

**11. What is "inverted" in the Dependency Inversion Principle?**
The source-code dependency relative to the flow of control. Control flows from the service to the
database adapter, but the adapter's code depends on (implements) an interface defined in the
service's package. Ownership is also inverted: the interface is shaped by the policy's needs, not
by the infrastructure API.

**12. How does hexagonal architecture relate to SOLID?**
It is DIP applied at the application level. The core defines inbound ports (use cases) and
outbound ports (repositories, gateways); adapters on both sides depend inward. ISP keeps ports
small; LSP (via contract tests) ensures adapters and fakes are interchangeable; OCP lets you add
adapters (a new consumer, a new provider) without touching the core.

**13. When would you not apply SOLID?**
Prototypes, small scripts, CRUD services with little policy, and code with one implementation and
no realistic variation. Abstractions have a cost in indirection and navigation. I introduce them
when a second variant appears, when a volatile dependency blocks testing, or at team and module
boundaries. Needless complexity is itself one of Martin's design smells.

**14. Walk me through refactoring a god class.**
Write characterisation tests first. Identify actors per block. Extract methods, then classes per
actor. Replace primitive types with records and sealed types. Define ports owned by the use case
for each external dependency, inject them through the constructor with `Clock` and ID suppliers,
move vendor code into adapters, wire in a composition root, and add contract tests for adapters
and fakes. Keep tests green after every step.

**15. How does DRY interact with SRP?**
DRY is about duplicated knowledge. Two pieces of code that look identical but belong to different
actors are accidental duplication; merging them couples two reasons to change, violating SRP.
Merge only when they truly represent the same rule that should always change together.

---

## Summary

```text
S  Single Responsibility   one actor per module; split by who asks for changes
O  Open/Closed             new variant = new class/config; protect expected axes only
L  Liskov Substitution     subtypes honour the contract: pre <=, post >=, invariants,
                           history; no surprise exceptions
I  Interface Segregation   clients depend on small role interfaces they fully use
D  Dependency Inversion    policy owns abstractions; details implement them; inject
```

| Principle | Smell in review | Usual fix | Watch out for |
| --- | --- | --- | --- |
| SRP | `Manager` classes, mixed imports, many teams editing one file | Extract Class per actor + coordinator | Ravioli code, splitting things that change together |
| OCP | Same switch on a type code in many places | Strategy registry, Decorator, `ServiceLoader` | Speculative abstraction; use sealed + switch for closed sets |
| LSP | `UnsupportedOperationException`, `instanceof` on base type | Composition, split interfaces, fix contracts, contract tests | Inheritance for reuse; dishonest fakes |
| ISP | Mocks stubbing unused methods, fat `XService` injected everywhere | Role interfaces, narrow `Repository` interfaces, Adapter | Fragmenting cohesive interfaces |
| DIP | `new` of infra or `now()` in business logic; framework imports in domain | Ports owned by policy, adapters, constructor injection, `Clock` | Interface-per-class ceremony; leaky ports |

Rules of thumb:

- Low coupling and high cohesion are the goal; SOLID is the toolbox.
- Prefer composition, `final` classes, records, and sealed types; use inheritance only for true
  behavioural subtyping.
- Introduce an abstraction on the second variant, at a boundary, or around volatile infrastructure.
- Make contracts executable with contract tests; test policy with fakes, adapters with real
  infrastructure.
- Judge a design by the cost of the last few changes, not by the number of interfaces.

---

## Further reading

- Robert C. Martin, *Clean Architecture: A Craftsman's Guide to Software Structure and Design*
  (Prentice Hall, 2017). Part III covers SOLID, including the actor-based SRP; Part IV covers
  component principles.
- Robert C. Martin, *Agile Software Development: Principles, Patterns, and Practices* (Prentice
  Hall, 2002). The original book-length treatment with the design smells and case studies.
- Robert C. Martin, *Design Principles and Design Patterns* (paper, 2000).
- Barbara H. Liskov and Jeannette M. Wing, "A Behavioral Notion of Subtyping", *ACM Transactions on
  Programming Languages and Systems*, 16(6), 1994.
- Barbara Liskov, "Data Abstraction and Hierarchy", OOPSLA '87 keynote (published in SIGPLAN
  Notices, 1988).
- Bertrand Meyer, *Object-Oriented Software Construction*, 2nd edition (Prentice Hall, 1997).
  Open/Closed and Design by Contract.
- Joshua Bloch, *Effective Java*, 3rd edition (Addison-Wesley, 2018). Items 10 (equals contract),
  18 (composition over inheritance), 19 (design for inheritance or prohibit it).
- Erich Gamma, Richard Helm, Ralph Johnson, John Vlissides, *Design Patterns: Elements of Reusable
  Object-Oriented Software* (Addison-Wesley, 1994).
- Martin Fowler, *Refactoring*, 2nd edition (Addison-Wesley, 2018), and the article *Inversion of
  Control Containers and the Dependency Injection pattern* (2004).
- Alistair Cockburn, "Hexagonal Architecture" (ports and adapters), 2005.
- Craig Larman, *Applying UML and Patterns*, 3rd edition (Prentice Hall, 2004). GRASP.
- Andrew Hunt and David Thomas, *The Pragmatic Programmer* (Addison-Wesley, 1999; 20th
  anniversary edition 2019). DRY, orthogonality.
- Gerard Meszaros, *xUnit Test Patterns* (Addison-Wesley, 2007). Test-double vocabulary.
- JDK Javadoc for `java.util.Stack`, `java.sql.Timestamp`, `java.sql.Date`, `java.util.List`, and
  `java.util.ServiceLoader` (check the version matching your JDK).
