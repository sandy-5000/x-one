---
title: "Design patterns"
order: 2
---
# Design Patterns

A design pattern is a **named, reusable solution to a problem that keeps recurring in object-oriented
design**. A pattern is not a library or a snippet to paste. It is a shape of collaboration between
classes (who knows about whom, who creates whom, who calls whom) plus the trade-offs that come with it.

The classic catalogue is *Design Patterns: Elements of Reusable Object-Oriented Software* (1994) by
Erich Gamma, Richard Helm, Ralph Johnson and John Vlissides, the "Gang of Four" (GoF). It describes 23
patterns in three families. Two principles from that book run underneath almost all of them:

1. **Program to an interface, not an implementation.** Clients depend on abstractions, so the concrete
   class can change without touching them.
2. **Favor object composition over class inheritance.** Behaviour assembled from collaborating objects
   can be changed at runtime and does not lock you into a rigid class tree.

Why still study them in 2026, with lambdas, records, sealed interfaces and DI frameworks?

- They are a **shared vocabulary**. "Wrap it in a decorator", "this is a strategy", "put a facade in
  front of it" compresses a paragraph of design into a word, in code review and in interviews.
- Frameworks are built from them. Spring is a tour of Proxy, Template Method, Factory, Strategy,
  Observer and Chain of Responsibility. Reading framework code is much easier when you can name the
  shapes.
- Some patterns have **collapsed into language features** (Strategy and Command into lambdas, Iterator
  into `for-each`, Visitor partly into sealed types plus pattern matching). Knowing the pattern tells you
  what the feature is really for, and when the full class-based version is still worth it.

| Family | Question it answers | Patterns |
| --- | --- | --- |
| Creational | How are objects created, and who decides the concrete class? | Singleton, Factory Method, Abstract Factory, Builder, Prototype |
| Structural | How are classes and objects composed into larger structures? | Adapter, Bridge, Composite, Decorator, Facade, Flyweight, Proxy |
| Behavioral | How do objects communicate and divide responsibility? | Chain of Responsibility, Command, Interpreter, Iterator, Mediator, Memento, Observer, State, Strategy, Template Method, Visitor |

## How to read these notes

Each pattern follows the same template: intent, a realistic problem, a structure diagram, a complete
runnable Java 21 example (each compiles as a single file named after its public class), real-world
uses, variations, pros and cons, pitfalls, and how it relates to neighbouring patterns.

Diagram notation used throughout:

```text
  A ──────► B        A holds or uses a reference to B (association / dependency)

  B
  ▲
  │                  C implements the interface B, or extends the class B
  C

  «interface» X      X is an interface or abstract class
  + op()             public operation
  - field            private field
```

---

# Creational patterns

Creational patterns separate **"what object do I need"** from **"how exactly is it built and which
class is it"**. They matter because `new ConcreteClass(...)` is the strongest coupling in Java: the
caller is welded to one class and to its constructor signature.

## Singleton

**Intent.** Ensure a class has exactly one instance and provide a global point of access to it.

**Problem.** Some resources must be unique within a process: a JVM-wide metrics registry, a cache of
parsed configuration, a hardware handle. If two parts of the code each create their own registry,
metrics are split across two objects and dashboards show half the numbers. You want one instance,
created lazily and safely, reachable from anywhere.

### Structure

```text
┌──────────┐   getInstance()   ┌─────────────────────────────┐
│ Client A │──────────────────►│ Singleton                   │
└──────────┘                   ├─────────────────────────────┤
┌──────────┐   getInstance()   │ - INSTANCE : Singleton      │
│ Client B │──────────────────►│ - Singleton()   (private)   │
└──────────┘                   ├─────────────────────────────┤
                               │ + getInstance() : Singleton │
                               │ + operation()               │
                               └─────────────────────────────┘
```

Participants: the **Singleton** class owns its only instance, hides its constructor, and exposes a
static accessor. Clients never call `new`.

### Example

```java
import java.io.*;
import java.lang.reflect.Constructor;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.LongAdder;

public class SingletonDemo {

    // 1. Eager: created when the class is initialized. Simple and thread-safe.
    static final class EagerClock {
        private static final EagerClock INSTANCE = new EagerClock();
        private EagerClock() {}
        static EagerClock getInstance() { return INSTANCE; }
        long now() { return System.currentTimeMillis(); }
    }

    // 2. Double-checked locking: lazy, thread-safe ONLY because the field is volatile.
    static final class DclRegistry {
        private static volatile DclRegistry instance;
        private DclRegistry() {}
        static DclRegistry getInstance() {
            DclRegistry local = instance;          // one volatile read on the fast path
            if (local == null) {
                synchronized (DclRegistry.class) {
                    local = instance;
                    if (local == null) {
                        instance = local = new DclRegistry();
                    }
                }
            }
            return local;
        }
    }

    // 3. Initialization-on-demand holder: lazy, thread-safe, no locking on access.
    static final class MetricsRegistry implements Serializable {
        private static boolean created;            // guards against reflective construction
        private final transient ConcurrentHashMap<String, LongAdder> counters =
                new ConcurrentHashMap<>();

        private MetricsRegistry() {
            synchronized (MetricsRegistry.class) {
                if (created) throw new IllegalStateException("singleton already created");
                created = true;
            }
        }

        private static final class Holder {
            static final MetricsRegistry INSTANCE = new MetricsRegistry();
        }

        static MetricsRegistry getInstance() { return Holder.INSTANCE; }

        void increment(String name) { counters.computeIfAbsent(name, k -> new LongAdder()).increment(); }
        long get(String name) { LongAdder a = counters.get(name); return a == null ? 0 : a.sum(); }

        @Serial
        private Object readResolve() { return getInstance(); }   // keep uniqueness on deserialization
    }

    // 4. Enum: Effective Java's recommended form. Serialization and reflection safe for free.
    enum IdGenerator {
        INSTANCE;
        private long next = 1;
        synchronized long nextId() { return next++; }
    }

    public static void main(String[] args) throws Exception {
        System.out.println(EagerClock.getInstance() == EagerClock.getInstance());       // true
        System.out.println(DclRegistry.getInstance() == DclRegistry.getInstance());     // true

        MetricsRegistry.getInstance().increment("orders");
        MetricsRegistry.getInstance().increment("orders");
        System.out.println("orders=" + MetricsRegistry.getInstance().get("orders"));    // 2

        // Serialization round trip returns the same instance thanks to readResolve.
        var bytes = new ByteArrayOutputStream();
        try (var out = new ObjectOutputStream(bytes)) { out.writeObject(MetricsRegistry.getInstance()); }
        try (var in = new ObjectInputStream(new ByteArrayInputStream(bytes.toByteArray()))) {
            System.out.println("same after deserialize: " + (in.readObject() == MetricsRegistry.getInstance()));
        }

        // Reflection attack is rejected by the constructor guard.
        Constructor<MetricsRegistry> ctor = MetricsRegistry.class.getDeclaredConstructor();
        ctor.setAccessible(true);
        try {
            ctor.newInstance();
        } catch (java.lang.reflect.InvocationTargetException e) {
            System.out.println("reflection blocked: " + e.getCause().getMessage());
        }

        System.out.println(IdGenerator.INSTANCE.nextId() + ", " + IdGenerator.INSTANCE.nextId()); // 1, 2
    }
}
```

### Singleton thread-safety deep dive

**Naive lazy initialization is a race.** `if (instance == null) instance = new X();` lets two threads
both see `null` and both construct. Worse, without a happens-before edge a thread can see a non-null
reference to an object whose fields are not yet visible.

**`synchronized` accessor** (`static synchronized X getInstance()`) is correct. Uncontended locks are
cheap on modern JVMs, but every call still takes the monitor, and under contention threads queue.
For most applications this is honestly fine; the fancier forms are about hot paths.

**Double-checked locking (DCL) and why `volatile` is mandatory.** `instance = new X()` is not atomic. It
is roughly: (1) allocate memory, (2) run the constructor, (3) publish the reference. The compiler and
CPU may reorder (2) and (3). Without `volatile`, thread B can pass the unsynchronized first check, see a
non-null `instance`, and use an object whose constructor has not finished. Since Java 5 (JSR-133
memory model), a `volatile` write happens-before every subsequent read of that field, which forbids
the reordering and guarantees safe publication. The local variable `local` in the example is a small
optimisation: it reads the volatile field once on the fast path instead of twice. DCL before Java 5
was broken even with `volatile`.

**Holder idiom (initialization-on-demand holder).** The JVM initializes a class lazily, the first time
it is actively used, and the Java Language Specification (section 12.4.2) requires class
initialization to be performed under a lock with happens-before guarantees. `Holder` is not touched
until `getInstance()` runs, so the instance is lazy; initialization is thread-safe by specification;
and after that, access is a plain static field read with no synchronization. This is the best choice
for a lazy singleton that must be a regular class.

**Enum singleton.** *Effective Java* (Item 3) calls a single-element enum "often the best way to
implement a singleton". The JVM guarantees one instance per constant, enum serialization writes only
the name and resolves it back to the existing constant, and `Constructor.newInstance` refuses to create
enum instances (it throws `IllegalArgumentException`). Limitations: an enum cannot extend another class,
and it reads oddly if the singleton has heavy lifecycle (open/close).

**Serialization attack.** Deserialization creates a new object without calling the constructor. A
`Serializable` singleton therefore needs `readResolve()` returning the canonical instance, and its
instance fields should be `transient` (otherwise a crafted stream can smuggle in references, as
described in *Effective Java* Item 89).

**Reflection attack.** `setAccessible(true)` on the private constructor lets anyone call it. Defences:
a guard flag in the constructor (shown above; note that checking `Holder.INSTANCE != null` does not
work because the field is still `null` while the holder is being initialized), or simply use an enum.
Strong encapsulation in the module system also blocks deep reflection into packages that a module
does not `open`.

**"One instance" is narrower than it sounds.** A static field is one per **class loader**, not per JVM:
an application server or plugin system with several class loaders can hold several "singletons". It is
also one per JVM, not per cluster: ten pods mean ten instances. Anything that must be unique across a
cluster (a scheduler, a leader) needs distributed coordination such as a lock or leader election.

**Why singletons hurt testing.**

- **Hidden dependency.** `MetricsRegistry.getInstance()` buried in a method does not appear in the
  constructor or signature; a reader cannot see what the class needs.
- **Cannot substitute.** A static call cannot be replaced with a fake without bytecode tricks (static
  mocking), so tests hit the real thing.
- **State leaks between tests.** The instance outlives a test, so a counter incremented in test A is
  visible in test B. Tests become order-dependent and flaky in parallel runs.
- **Global mutable state** has the same problems as global variables: action at a distance and
  concurrency hazards.

The fix is to keep the **"one instance" policy** but move it out of the class: create one instance at the
composition root and **inject** it. Spring's default singleton scope does exactly this: one bean per
application context, with no static accessor.

```java
interface Metrics { void increment(String name); }

final class OrderService {
    private final Metrics metrics;                 // dependency is explicit
    OrderService(Metrics metrics) { this.metrics = metrics; }
    void place() { metrics.increment("orders"); }
}
// In a test: new OrderService(name -> recorded.add(name));
```

### Real-world uses

- `java.lang.Runtime.getRuntime()` returns the single `Runtime` of the JVM.
- `java.awt.Desktop.getDesktop()` and `Toolkit.getDefaultToolkit()`.
- Spring beans default to **singleton scope**, which means one instance per container, not the GoF
  class-enforced singleton.
- Enum-based singletons appear widely, for example stateless comparators and strategies.

### Variations

Eager field; `synchronized` accessor; double-checked locking; holder idiom; enum; container-managed
singleton (DI scope); per-thread "singleton" via `ThreadLocal`; multiton (one instance per key, a
`Map<Key, Instance>`).

### Pros and cons

| Pros | Cons |
| --- | --- |
| Controlled single instance, lazy creation possible | Global mutable state, hidden coupling |
| Saves resources for expensive shared objects | Hard to test and to substitute |
| Simple access from anywhere | Violates single responsibility (manages own lifecycle) |
| | Uniqueness only per class loader and per JVM |

### Pitfalls

- DCL without `volatile`.
- Forgetting `readResolve` on a serializable singleton.
- Putting mutable request-scoped data in a singleton bean (thread-safety bugs in Spring services).
- Using a singleton as a convenient global for things that are not really unique (the "Singleton
  abuse" anti-pattern below).

### Related patterns

Abstract Factory, Builder and Facade objects are often singletons. Flyweight looks similar (shared
instances) but shares **many** immutable instances keyed by state, while Singleton has exactly one.
Monostate (all fields static, normal constructor) is an alternative with the same testing drawbacks.

## Factory Method

**Intent.** Define an interface for creating an object, but let subclasses (or a separate method)
decide which class to instantiate.

**Problem.** A reporting job runs the same workflow (load data, render, publish) for many formats:
CSV for finance, JSON for an API, PDF for management. If the workflow says `new CsvExporter()`, adding
a format means editing the workflow. You want the workflow to depend on an `Exporter` interface and
defer "which exporter" to a hook.

### Structure

```text
┌───────────────────────────┐            ┌──────────────────────┐
│ «abstract» Creator        │  creates   │ «interface» Product  │
├───────────────────────────┤ ─────────► ├──────────────────────┤
│ + run()  (uses product)   │            │ + export(report)     │
│ # createProduct() : Prod  │            └──────────────────────┘
└─────────────▲─────────────┘                       ▲
              │                                     │
┌─────────────┴─────────────┐            ┌──────────┴───────────┐
│ ConcreteCreator           │  creates   │ ConcreteProduct      │
│ # createProduct()         │ ─────────► │ + export(report)     │
└───────────────────────────┘            └──────────────────────┘
```

Participants: **Product** (interface of created objects), **ConcreteProduct**, **Creator** (declares
the factory method and usually contains logic that uses the product), **ConcreteCreator** (overrides
the factory method).

### Example

```java
import java.util.*;
import java.util.function.Supplier;

public class FactoryMethodDemo {

    record Report(String title, List<String> rows) {}

    interface Exporter {
        String contentType();
        String export(Report report);
    }

    static final class CsvExporter implements Exporter {
        public String contentType() { return "text/csv"; }
        public String export(Report r) { return r.title() + "\n" + String.join("\n", r.rows()); }
    }

    static final class JsonExporter implements Exporter {
        public String contentType() { return "application/json"; }
        public String export(Report r) {
            var rows = r.rows().stream().map(s -> "\"" + s + "\"").toList();
            return "{\"title\":\"" + r.title() + "\",\"rows\":[" + String.join(",", rows) + "]}";
        }
    }

    // Classic GoF form: the creator's workflow calls an overridable factory method.
    abstract static class ReportJob {
        protected abstract Exporter createExporter();          // the factory method

        public final String run(Report report) {
            Exporter exporter = createExporter();
            return "[" + exporter.contentType() + "] " + exporter.export(report);
        }
    }

    static final class FinanceJob extends ReportJob {
        protected Exporter createExporter() { return new CsvExporter(); }
    }

    static final class ApiJob extends ReportJob {
        protected Exporter createExporter() { return new JsonExporter(); }
    }

    // Common modern form: a static factory backed by a registry of constructors.
    private static final Map<String, Supplier<Exporter>> REGISTRY = Map.of(
            "csv", CsvExporter::new,
            "json", JsonExporter::new);

    static Exporter exporterFor(String format) {
        Supplier<Exporter> supplier = REGISTRY.get(format.toLowerCase(Locale.ROOT));
        if (supplier == null) throw new IllegalArgumentException("unknown format: " + format);
        return supplier.get();
    }

    public static void main(String[] args) {
        var report = new Report("Q3 revenue", List.of("north,120", "south,95"));
        System.out.println(new FinanceJob().run(report));
        System.out.println(new ApiJob().run(report));
        System.out.println(exporterFor("JSON").contentType());
    }
}
```

### Real-world uses

- `Collection.iterator()` is a textbook factory method: each collection subclass returns its own
  `Iterator` implementation.
- Static factories: `List.of`, `Calendar.getInstance()`, `NumberFormat.getInstance()`,
  `Charset.forName()`, `Path.of()`, `EnumSet.of` (returns `RegularEnumSet` or `JumboEnumSet` depending
  on size, which callers never see).
- Spring's `FactoryBean<T>` interface lets a bean definition delegate creation of the real object to a
  `getObject()` method; `@Bean` methods are factory methods too.

### Variations

- **Subclass-based** (GoF): the creator is abstract and subclasses override the method.
- **Parameterized factory**: one method takes a key and returns different products (`exporterFor`).
- **Static factory method** (*Effective Java* Item 1): named constructors such as `valueOf`, `of`,
  `from`, `newInstance`. Not the GoF pattern strictly, but the same idea of hiding the concrete class.
- **Registry of `Supplier`s**: open for extension without a growing `switch`.

### Pros and cons

| Pros | Cons |
| --- | --- |
| Callers depend only on the product interface | Subclass-based form adds a creator class per product |
| New products without editing client workflow | Indirection makes "what is actually created" harder to trace |
| Factory can cache, pool or return subtypes | Parameterized form with a `switch` drifts toward a god factory |

### Pitfalls

- A giant `switch` factory that every team edits; prefer a registry or DI.
- Returning `null` for unknown keys instead of failing fast.
- Creating a factory for a class that has exactly one implementation and always will.

### Related patterns

Factory Method is often called inside a **Template Method** (as in `run()` above). **Abstract Factory**
is usually a set of factory methods for a product family. **Prototype** creates by copying instead of
subclassing the creator. Static factories often return **Flyweights** (`Integer.valueOf`).

## Abstract Factory

**Intent.** Provide an interface for creating **families of related objects** without specifying their
concrete classes.

**Problem.** An upload service stores a blob and then enqueues a processing message. In production it
uses S3 and SQS; in local development, an in-memory store and an in-memory queue. Mixing families
(S3 with the in-memory queue) is a bug: the worker would never see the message. You want to choose the
whole family in one place and guarantee consistency.

### Structure

```text
              ┌──────────────────────────────┐
  Client ────►│ «interface» CloudFactory     │
    │         ├──────────────────────────────┤
    │         │ + blobStore() : BlobStore    │
    │         │ + queue()     : MessageQueue │
    │         └──────────────▲───────────────┘
    │              ┌─────────┴──────────┐
    │      ┌───────┴───────┐    ┌───────┴───────┐
    │      │ AwsFactory    │    │ LocalFactory  │
    │      └───────────────┘    └───────────────┘
    │
    │      «interface» BlobStore ◄── S3BlobStore, InMemoryBlobStore
    └────► «interface» MessageQueue ◄── SqsQueue, InMemoryQueue
```

Participants: **AbstractFactory** (one creation method per product type), **ConcreteFactory** (one
per family), **AbstractProduct** interfaces, **ConcreteProducts**, and the **Client**, which only sees
interfaces.

### Example

```java
import java.util.*;

public class AbstractFactoryDemo {

    interface BlobStore { String put(String key, byte[] data); }
    interface MessageQueue { void send(String message); List<String> drain(); }

    interface CloudFactory {
        BlobStore blobStore();
        MessageQueue queue();
    }

    // --- AWS family (simulated) ---
    static final class S3BlobStore implements BlobStore {
        public String put(String key, byte[] data) { return "s3://uploads/" + key; }
    }
    static final class SqsQueue implements MessageQueue {
        private final List<String> sent = new ArrayList<>();
        public void send(String m) { sent.add(m); System.out.println("SQS <- " + m); }
        public List<String> drain() { var copy = List.copyOf(sent); sent.clear(); return copy; }
    }
    static final class AwsFactory implements CloudFactory {
        public BlobStore blobStore() { return new S3BlobStore(); }
        public MessageQueue queue() { return new SqsQueue(); }
    }

    // --- Local family ---
    static final class InMemoryBlobStore implements BlobStore {
        private final Map<String, byte[]> blobs = new HashMap<>();
        public String put(String key, byte[] data) { blobs.put(key, data); return "mem://" + key; }
    }
    static final class InMemoryQueue implements MessageQueue {
        private final Deque<String> q = new ArrayDeque<>();
        public void send(String m) { q.add(m); }
        public List<String> drain() { var copy = List.copyOf(q); q.clear(); return copy; }
    }
    static final class LocalFactory implements CloudFactory {
        public BlobStore blobStore() { return new InMemoryBlobStore(); }
        public MessageQueue queue() { return new InMemoryQueue(); }
    }

    // Client: knows only the abstract factory and abstract products.
    static final class UploadService {
        private final BlobStore store;
        private final MessageQueue queue;
        UploadService(CloudFactory factory) {
            this.store = factory.blobStore();
            this.queue = factory.queue();
        }
        String upload(String name, byte[] data) {
            String location = store.put(name, data);
            queue.send("process " + location);
            return location;
        }
        List<String> pending() { return queue.drain(); }
    }

    public static void main(String[] args) {
        String env = System.getProperty("env", "local");
        CloudFactory factory = switch (env) {
            case "aws" -> new AwsFactory();
            case "local" -> new LocalFactory();
            default -> throw new IllegalArgumentException(env);
        };
        var service = new UploadService(factory);
        System.out.println(service.upload("cat.png", new byte[] {1, 2, 3}));
        System.out.println(service.pending());
    }
}
```

### Real-world uses

- `javax.xml.parsers.DocumentBuilderFactory` and `javax.xml.transform.TransformerFactory`:
  `newInstance()` picks an implementation (via the service lookup mechanism) whose products all come
  from the same XML library.
- `java.sql.Connection` acts as a factory for a family of driver-specific objects
  (`createStatement`, `prepareStatement`, `createBlob`) that must all come from the same driver.
- UI toolkits with look-and-feel families (Swing's pluggable look and feel is the classic example).

### Variations

- Each factory method can itself be a **Factory Method** or use **Prototype**s.
- Factories are frequently **Singletons** or DI beans selected by profile (Spring `@Profile("aws")`).
- A reflective or `ServiceLoader`-based factory discovers families at runtime.

### Pros and cons

| Pros | Cons |
| --- | --- |
| Guarantees products from one family are used together | Adding a new **product type** changes every factory |
| Swapping the whole family is a one-line change | Many interfaces and classes for small problems |
| Isolates concrete classes from clients | Can be overkill when DI profiles already do the job |

### Pitfalls

The asymmetry is the key point: adding a new **family** is easy (one new factory), adding a new
**kind of product** is hard (every factory changes). If product types change often, the pattern
works against you. Also avoid factories that leak concrete types through casts.

### Related patterns

Factory Method creates one product through inheritance; Abstract Factory creates a family through
composition (the client holds a factory object). **Builder** focuses on assembling one complex object
step by step; Abstract Factory returns products immediately. **Bridge** often uses an Abstract Factory
to create the right implementor.

## Builder

**Intent.** Separate the construction of a complex object from its representation, so the same
construction process can create different representations. In everyday Java, it mostly means:
**build an object with many optional parameters readably and validate it once.**

**Problem.** An HTTP call has a method, URI, headers, timeout, retries and an optional body. A
telescoping constructor `new HttpCall("POST", uri, headers, 2000, 3, body)` is unreadable and easy to
get wrong (two adjacent `int`s swap silently). JavaBeans setters leave the object mutable and
temporarily invalid. A builder gives named steps and an immutable, validated result.

### Structure

```text
┌──────────┐  uses   ┌──────────────────────────┐ build() ┌─────────────────────┐
│ Client   │────────►│ Builder                  │────────►│ Product (immutable) │
└──────────┘         ├──────────────────────────┤         ├─────────────────────┤
                     │ + method(m) : Builder    │         │ - method            │
                     │ + header(k,v) : Builder  │         │ - uri               │
                     │ + timeout(d) : Builder   │         │ - headers           │
                     │ + build() : Product      │         │ - timeout           │
                     └──────────────────────────┘         └─────────────────────┘
```

GoF also describes a **Director** that drives a builder through a fixed sequence of steps (for example
a document converter that calls `addHeading`, `addParagraph` on an HTML or a Markdown builder). The
fluent builder above is the variant Java developers use daily (*Effective Java* Item 2).

### Example

```java
import java.net.URI;
import java.time.Duration;
import java.util.*;

public class BuilderDemo {

    record HttpCall(String method, URI uri, Map<String, String> headers,
                    Duration timeout, int maxRetries, Optional<String> body) {

        HttpCall {                                         // compact constructor: final validation
            headers = Map.copyOf(headers);
            if (maxRetries < 0) throw new IllegalArgumentException("maxRetries < 0");
            if (body.isPresent() && method.equals("GET"))
                throw new IllegalStateException("GET must not have a body");
        }

        static Builder builder(String uri) { return new Builder(URI.create(uri)); }

        static final class Builder {
            private final URI uri;                         // required: constructor argument
            private String method = "GET";                 // optional: defaults
            private final Map<String, String> headers = new LinkedHashMap<>();
            private Duration timeout = Duration.ofSeconds(5);
            private int maxRetries = 0;
            private String body;

            private Builder(URI uri) { this.uri = Objects.requireNonNull(uri); }

            Builder method(String m)           { this.method = m; return this; }
            Builder header(String k, String v) { headers.put(k, v); return this; }
            Builder timeout(Duration d)        { this.timeout = d; return this; }
            Builder retries(int n)             { this.maxRetries = n; return this; }
            Builder jsonBody(String json) {
                this.body = json;
                return header("Content-Type", "application/json");
            }

            HttpCall build() {
                return new HttpCall(method, uri, headers, timeout, maxRetries, Optional.ofNullable(body));
            }
        }
    }

    public static void main(String[] args) {
        HttpCall call = HttpCall.builder("https://api.example.com/orders")
                .method("POST")
                .jsonBody("{\"sku\":\"A1\",\"qty\":2}")
                .timeout(Duration.ofSeconds(2))
                .retries(3)
                .build();
        System.out.println(call);

        try {
            HttpCall.builder("https://api.example.com/orders").jsonBody("{}").build();
        } catch (IllegalStateException e) {
            System.out.println("rejected: " + e.getMessage());
        }
    }
}
```

### Real-world uses

- `java.net.http.HttpClient.newBuilder()` and `HttpRequest.newBuilder()`.
- `Locale.Builder`, `Stream.builder()`, `ProcessBuilder`, `StringBuilder` (a loose builder of a
  `String`).
- Spring: `UriComponentsBuilder`, `WebClient.builder()`, `RestClient.builder()`, `MockMvcRequestBuilders`.
- Lombok's `@Builder` generates the boilerplate; Protocol Buffers generated classes use
  `newBuilder()...build()`.

### Variations

- **Fluent builder** with required parameters in the builder constructor.
- **Step builder**: each step returns a different interface, so the compiler enforces order and
  required fields (`uri()` must be called before `build()` is even visible).
- **Director + builder** (GoF): one algorithm, many output representations.
- **`toBuilder()`**: copy an existing immutable object into a builder, change one field, rebuild.
- **Hierarchical builders** with a recursive generic `Builder<T extends Builder<T>>` (*Effective Java*
  Item 2) for class hierarchies.

### Pros and cons

| Pros | Cons |
| --- | --- |
| Readable named parameters, sensible defaults | Extra class and duplicated field list |
| Produces immutable objects; validation in one place | Overkill for 2-3 parameters (use a record) |
| Can build different representations (Director form) | Builders are mutable and not thread-safe |

### Pitfalls

- Validating in setters but not in `build()` (or in the product's constructor), so invalid
  combinations slip through.
- Sharing a builder across threads.
- Forgetting to copy mutable collections (`Map.copyOf`), so later builder calls mutate the "immutable"
  product.

### Related patterns

**Abstract Factory** returns a product immediately; Builder assembles step by step and returns at the
end. Builders often build **Composites** (a tree of UI or document nodes). With records and named
static factories, many simple builders become unnecessary.

## Prototype

**Intent.** Specify the kind of object to create using a prototypical instance, and create new
objects by **copying** it.

**Problem.** A document service creates contracts from templates. Building a template is expensive:
load clauses from a database, apply branding, compute default sections. Users create hundreds of
contracts per hour from a dozen templates. Copying a pre-built template is cheaper and keeps the code
independent of how each template was assembled.

### Structure

```text
┌──────────┐  copy()  ┌──────────────────────────┐
│ Client   │─────────►│ «interface» Prototype    │
└────┬─────┘          ├──────────────────────────┤
     │                │ + copy() : Prototype     │
     │                └────────────▲─────────────┘
     │                  ┌──────────┴──────────┐
     │          ┌───────┴───────┐     ┌───────┴───────┐
     │          │ ConcreteA     │     │ ConcreteB     │
     │          │ + copy()      │     │ + copy()      │
     │          └───────────────┘     └───────────────┘
     │
     └──► PrototypeRegistry: Map<String, Prototype>  (lookup by name, then copy)
```

### Example

```java
import java.util.*;

public class PrototypeDemo {

    record Branding(String logo, String font) {}            // immutable: safe to share

    static final class DocumentTemplate {
        private final String name;
        private final Branding branding;
        private final List<String> sections;                 // mutable: must be deep-copied

        DocumentTemplate(String name, Branding branding, List<String> sections) {
            this.name = name;
            this.branding = branding;
            this.sections = new ArrayList<>(sections);
        }

        // Copy constructor: clearer and safer than Object.clone().
        DocumentTemplate(DocumentTemplate other) {
            this(other.name, other.branding, other.sections); // list is copied in the main constructor
        }

        DocumentTemplate copy() { return new DocumentTemplate(this); }

        void addSection(String s) { sections.add(s); }
        @Override public String toString() { return name + " " + branding.font() + " " + sections; }
    }

    static final class TemplateRegistry {
        private final Map<String, DocumentTemplate> prototypes = new HashMap<>();
        void register(String key, DocumentTemplate t) { prototypes.put(key, t); }
        DocumentTemplate create(String key) {
            DocumentTemplate p = prototypes.get(key);
            if (p == null) throw new NoSuchElementException(key);
            return p.copy();
        }
    }

    public static void main(String[] args) {
        var registry = new TemplateRegistry();
        // imagine this was expensive: DB lookups, rendering, legal review
        registry.register("nda", new DocumentTemplate("NDA", new Branding("acme.svg", "Inter"),
                List.of("Parties", "Confidential information", "Term")));

        DocumentTemplate a = registry.create("nda");
        a.addSection("Governing law: India");
        DocumentTemplate b = registry.create("nda");

        System.out.println(a);   // has the extra section
        System.out.println(b);   // unaffected: the list was deep-copied
    }
}
```

### Shallow vs deep copy

A **shallow** copy duplicates the top-level object but shares referenced objects. That is fine for
immutable parts (`Branding`, `String`) and a bug for mutable ones: if `sections` were shared, adding a
section to one contract would add it to every contract created from the template. A **deep** copy
recursively copies mutable state. Most real implementations are "deep where mutable, shared where
immutable".

`Object.clone()` and `Cloneable` are widely considered broken (*Effective Java* Item 13): `Cloneable`
has no `clone` method, `clone()` bypasses constructors, returns a shallow copy, conflicts with `final`
fields, and throws a checked exception. Prefer a **copy constructor** or a **copy factory**.

### Real-world uses

- `ArrayList`, `HashMap` and arrays implement `clone()` (shallow); copy constructors such as
  `new ArrayList<>(other)` and `List.copyOf` are the idiomatic alternatives.
- JavaScript's object model is prototype-based (`Object.create(proto)`), a language-level version of
  the idea.
- Spring's **prototype scope** means "a new instance per lookup". Despite the name it creates a fresh
  bean rather than cloning one, so it is not the GoF pattern.

### Variations

Copy constructor; copy factory (`static T copyOf(T)`); prototype registry; `toBuilder()` on immutable
objects (copy-and-modify); serialization-based deep copy (slow, avoid on hot paths).

### Pros and cons

| Pros | Cons |
| --- | --- |
| Avoids repeating expensive initialization | Deep copying object graphs with cycles is tricky |
| Creates objects without knowing concrete classes | Every subclass must implement copying correctly |
| Runtime-configurable set of "kinds" (registry) | Shallow-copy bugs are subtle |

### Pitfalls

Shallow copies of mutable collections; using `clone()` on classes with `final` mutable fields;
copying objects that hold resources (connections, file handles) which must not be duplicated.

### Related patterns

An alternative to **Factory Method** (no parallel creator hierarchy) and to **Abstract Factory**
(a factory can be configured with prototypes). **Memento** snapshots state for restoring the same
object; Prototype copies state to make a new one. Immutable objects make Prototype mostly unnecessary,
since sharing is safe.

---

# Structural patterns

Structural patterns are about **wrapping and composing**. Several of them (Adapter, Decorator, Proxy,
Facade) look alike in code because they all put one object in front of another. The difference is
**intent**, compared in a table after the section.

## Adapter

**Intent.** Convert the interface of a class into another interface clients expect, so classes with
incompatible interfaces can work together.

**Problem.** Your checkout code depends on a clean `PaymentGateway` interface with a `Money` record in
minor units. The bank's legacy SDK exposes `int doTxn(String acct, double amount, String ccy)` that
returns numeric status codes. You cannot change the SDK, and you do not want doubles and magic codes
leaking into the domain.

### Structure

```text
┌──────────┐    ┌───────────────────────────┐
│ Client   │───►│ «interface» Target        │
└──────────┘    ├───────────────────────────┤
                │ + charge(money, token)    │
                └─────────────▲─────────────┘
                              │ implements
                ┌─────────────┴─────────────┐    ┌───────────────────────────┐
                │ Adapter                   │───►│ Adaptee (legacy SDK)      │
                ├───────────────────────────┤    ├───────────────────────────┤
                │ + charge(money, token)    │    │ + doTxn(acct, amt, ccy)   │
                └───────────────────────────┘    └───────────────────────────┘
```

### Example

```java
import java.math.BigDecimal;

public class AdapterDemo {

    // --- our domain (Target) ---
    record Money(long minorUnits, String currency) {}
    sealed interface PaymentResult permits Approved, Declined {}
    record Approved(String reference) implements PaymentResult {}
    record Declined(String reason) implements PaymentResult {}

    interface PaymentGateway {
        PaymentResult charge(Money amount, String cardToken);
    }

    // --- third-party code we cannot change (Adaptee) ---
    static final class LegacyBankApi {
        /** @return 0 = ok, 51 = insufficient funds, 54 = expired card */
        int doTxn(String account, double amount, String currency) {
            System.out.printf("legacy doTxn(%s, %.2f, %s)%n", account, amount, currency);
            return amount > 10_000 ? 51 : 0;
        }
        String lastReference() { return "TXN-" + System.nanoTime() % 100_000; }
    }

    // --- Adapter: translates types, units and error codes ---
    static final class LegacyBankAdapter implements PaymentGateway {
        private final LegacyBankApi bank;
        LegacyBankAdapter(LegacyBankApi bank) { this.bank = bank; }

        @Override
        public PaymentResult charge(Money amount, String cardToken) {
            double major = BigDecimal.valueOf(amount.minorUnits()).movePointLeft(2).doubleValue();
            int code = bank.doTxn(cardToken, major, amount.currency());
            return switch (code) {
                case 0 -> new Approved(bank.lastReference());
                case 51 -> new Declined("insufficient funds");
                case 54 -> new Declined("card expired");
                default -> new Declined("bank error " + code);
            };
        }
    }

    public static void main(String[] args) {
        PaymentGateway gateway = new LegacyBankAdapter(new LegacyBankApi());
        System.out.println(gateway.charge(new Money(49_999, "INR"), "tok_123"));
        System.out.println(gateway.charge(new Money(5_000_000, "INR"), "tok_123"));
    }
}
```

### Real-world uses

- `Arrays.asList(array)` adapts an array to the `List` interface.
- `InputStreamReader` / `OutputStreamWriter` adapt byte streams to character streams.
- `Collections.list(Enumeration)` and `Collections.enumeration(Collection)` bridge old and new
  iteration APIs.
- Spring MVC's `HandlerAdapter` lets `DispatcherServlet` invoke very different handler types
  (annotated controllers, `HttpRequestHandler`) through one interface.
- SLF4J bridge modules such as `jcl-over-slf4j` and `log4j-over-slf4j` adapt other logging APIs onto
  SLF4J.

### Variations

- **Object adapter** (composition, shown above): works with any subclass of the adaptee. Preferred.
- **Class adapter** (inherit from the adaptee and implement the target): only possible when the adaptee
  is not final; Java lacks multiple class inheritance, so it is rare.
- **Two-way adapter**: implements both interfaces.
- **Lambda adapter**: if the target is a functional interface, a method reference is an adapter:
  `Supplier<Instant> now = clock::instant;`.

### Pros and cons

| Pros | Cons |
| --- | --- |
| Reuse existing code without modifying it | One more layer; can hide expensive or lossy conversions |
| Keeps third-party types out of your domain (anti-corruption layer) | Many adapters suggest a deeper modelling mismatch |
| Single place to translate units, errors and naming | Behavioural mismatches (sync vs async) cannot always be adapted cleanly |

### Pitfalls

Lossy conversions (the `double` above would be wrong for some currencies with three decimal places;
real code must use the currency's minor-unit exponent). Swallowing adaptee exceptions instead of
translating them. Letting adaptee types leak through the target interface.

### Related patterns

**Facade** defines a *new, simpler* interface over many classes; Adapter makes *one* existing class fit
an *existing* interface. **Decorator** keeps the interface and adds behaviour; **Proxy** keeps the
interface and controls access. **Bridge** is designed up front; Adapter is applied after the fact.

## Bridge

**Intent.** Decouple an abstraction from its implementation so that the two can vary independently.

**Problem.** An alerting system has alert kinds (simple, escalating with retries) and delivery
channels (email, Slack, SMS). With inheritance you get a class per combination: `EmailSimpleAlert`,
`SlackEscalatingAlert`... that is kinds × channels classes, and adding PagerDuty multiplies again. You
want two independent hierarchies joined by a reference.

### Structure

```text
┌─────────────────────────────┐ has-a  ┌─────────────────────────────┐
│ «abstract» Alert            │◆──────►│ «interface» Channel         │
├─────────────────────────────┤        ├─────────────────────────────┤
│ # channel : Channel         │        │ + deliver(to, text)         │
│ + raise(event)              │        └──────────────▲──────────────┘
└──────────────▲──────────────┘              ┌────────┴────────┐
       ┌───────┴────────┐               ┌────┴─────┐      ┌────┴─────┐
┌──────┴──────┐  ┌──────┴───────┐       │ Email    │      │ Slack    │
│ SimpleAlert │  │ Escalating   │       └──────────┘      └──────────┘
└─────────────┘  └──────────────┘
   Abstraction hierarchy                    Implementor hierarchy
```

Participants: **Abstraction** (high-level control logic, holds an implementor), **RefinedAbstraction**,
**Implementor** (low-level primitive operations), **ConcreteImplementor**.

### Example

```java
import java.util.List;

public class BridgeDemo {

    // Implementor: primitive delivery operations.
    interface Channel { boolean deliver(String recipient, String text); }

    static final class EmailChannel implements Channel {
        public boolean deliver(String to, String text) { System.out.println("EMAIL " + to + ": " + text); return true; }
    }
    static final class SlackChannel implements Channel {
        private int calls;
        public boolean deliver(String to, String text) {
            System.out.println("SLACK #" + to + ": " + text);
            return ++calls > 1;                       // first call "fails" to show escalation
        }
    }

    record Event(String service, String message, int severity) {}

    // Abstraction: high-level policy built on top of the implementor.
    abstract static class Alert {
        protected final Channel channel;
        protected Alert(Channel channel) { this.channel = channel; }
        abstract void raise(Event e);
    }

    static final class SimpleAlert extends Alert {
        private final String recipient;
        SimpleAlert(Channel c, String recipient) { super(c); this.recipient = recipient; }
        void raise(Event e) { channel.deliver(recipient, e.service() + ": " + e.message()); }
    }

    static final class EscalatingAlert extends Alert {
        private final List<String> escalationChain;
        EscalatingAlert(Channel c, List<String> chain) { super(c); this.escalationChain = chain; }
        void raise(Event e) {
            for (String who : escalationChain) {
                if (channel.deliver(who, "[SEV" + e.severity() + "] " + e.message())) return;
                System.out.println("  no ack from " + who + ", escalating");
            }
        }
    }

    public static void main(String[] args) {
        var event = new Event("payments", "error rate 7%", 1);
        new SimpleAlert(new EmailChannel(), "oncall@example.com").raise(event);
        new EscalatingAlert(new SlackChannel(), List.of("payments-oncall", "eng-manager")).raise(event);
    }
}
```

### Real-world uses

- **JDBC**: application code is written against `Connection`, `Statement`, `ResultSet`; vendor
  drivers implement them. The API (abstraction) and the drivers (implementors) evolve separately.
- **SLF4J**: the logging API is separate from the binding (Logback, Log4j 2) chosen at deploy time.
- Early AWT components delegated to platform-specific "peer" objects.

### Variations

Implementor chosen at construction (DI) or swapped at runtime; implementor created by an **Abstract
Factory**; a single refined abstraction when only the implementation side varies (still useful as a
seam for testing).

### Pros and cons

| Pros | Cons |
| --- | --- |
| Avoids class explosion (kinds + channels instead of kinds × channels) | Harder to understand than plain inheritance |
| Abstraction and implementation evolve and ship independently | Must design the implementor interface well up front |
| Implementation can be switched at runtime | Overkill when only one dimension varies |

### Pitfalls

An implementor interface that is too high-level (it then duplicates the abstraction) or too
leaky (it exposes one vendor's quirks so other implementors cannot fit).

### Related patterns

**Adapter** retrofits compatibility after the fact; Bridge separates dimensions by design. **Strategy**
has the same object shape (context holds an interface), but Strategy swaps an *algorithm* for one
operation, while Bridge splits a whole *abstraction* from its *platform*. **Abstract Factory** can
create and configure a bridge.

## Composite

**Intent.** Compose objects into tree structures to represent part-whole hierarchies, and let clients
treat individual objects and compositions uniformly.

**Problem.** A storage dashboard shows the size of files and folders. Folders contain files and other
folders, to any depth. Client code that asks "is this a file or a folder?" at every level becomes a
mess of `instanceof` checks and recursion. You want `node.size()` to work on both.

### Structure

```text
              ┌──────────────────────────┐
  Client ────►│ «interface» Component    │◄─────────────┐
              ├──────────────────────────┤              │ children
              │ + size()                 │              │ (0..n)
              │ + render(indent)         │              │
              └────────────▲─────────────┘              │
                  ┌────────┴──────────┐                 │
          ┌───────┴───────┐   ┌───────┴───────────┐     │
          │ Leaf (File)   │   │ Composite (Dir)   │◆────┘
          │ + size()      │   │ + size() = sum    │
          └───────────────┘   └───────────────────┘
```

### Example

```java
import java.util.*;

public class CompositeDemo {

    sealed interface Node permits FileNode, Dir {
        String name();
        long size();
        default void render(StringBuilder out, int depth) {
            out.append("  ".repeat(depth)).append(name()).append(" (").append(size()).append(" B)\n");
        }
    }

    record FileNode(String name, long size) implements Node {}

    record Dir(String name, List<Node> children) implements Node {
        Dir { children = List.copyOf(children); }
        Dir(String name, Node... children) { this(name, List.of(children)); }

        public long size() { return children.stream().mapToLong(Node::size).sum(); }

        @Override public void render(StringBuilder out, int depth) {
            Node.super.render(out, depth);
            for (Node child : children) child.render(out, depth + 1);
        }
    }

    public static void main(String[] args) {
        Node root = new Dir("project",
                new FileNode("README.md", 1_200),
                new Dir("src",
                        new FileNode("Main.java", 4_096),
                        new Dir("util", new FileNode("Strings.java", 2_048))),
                new Dir("empty"));

        var out = new StringBuilder();
        root.render(out, 0);
        System.out.print(out);
        System.out.println("total = " + root.size());   // 7344
    }
}
```

### Real-world uses

- AWT/Swing: `java.awt.Container` extends `Component` and contains `Component`s; layout and painting
  recurse through the tree.
- DOM (`org.w3c.dom.Node`), JSON trees (Jackson's `JsonNode`), compiler ASTs.
- Spring: `CompositeCacheManager`, `CompositePropertySource`, `CompositeMeterRegistry` in Micrometer;
  each looks like one component but delegates to several.

### Variations

- **Transparent composite**: `add/remove` declared on the component interface, so leaves must throw.
  Uniform but less type-safe.
- **Safe composite**: child management only on the composite (shown above, with immutable children).
- Parent references for upward navigation; caching aggregated values (invalidate on change).

### Pros and cons

| Pros | Cons |
| --- | --- |
| Uniform treatment of leaves and trees | Hard to restrict which children a composite may contain |
| Easy to add new node types | Transparent form puts meaningless methods on leaves |
| Recursive operations are natural | Deep trees risk stack overflow and O(n) aggregates per call |

### Pitfalls

Cycles (a folder containing itself) cause infinite recursion. Recomputing aggregates repeatedly on
large trees. Mutable child lists exposed to callers.

### Related patterns

**Iterator** and **Visitor** traverse composites. **Decorator** has a similar recursive structure but
exactly one wrapped child and adds behaviour; Composite has many children and aggregates. **Builder**
often constructs composites. **Flyweight** can share leaf nodes. **Interpreter** is Composite applied
to a grammar.

## Decorator

**Intent.** Attach additional responsibilities to an object dynamically, by wrapping it in an object
with the same interface. A flexible alternative to subclassing.

**Problem.** A `PriceService` calls a slow, flaky pricing API. You need caching, retries and logging,
in different combinations per environment (no caching in tests, extra logging in staging). Subclasses
like `CachingRetryingLoggingPriceService` explode combinatorially and cannot be reordered at runtime.

### Structure

```text
              ┌──────────────────────────┐
  Client ────►│ «interface» Component    │◄────────────────┐
              ├──────────────────────────┤                 │ wraps
              │ + price(sku)             │                 │ exactly one
              └────────────▲─────────────┘                 │
                 ┌─────────┴───────────┐                   │
      ┌──────────┴──────────┐   ┌──────┴────────────────┐  │
      │ ConcreteComponent   │   │ «abstract» Decorator  │◆─┘
      │ (RemotePriceService)│   │ # inner : Component   │
      └─────────────────────┘   └──────▲────────────────┘
                                   ┌───┴──────────┬─────────────┐
                                 Caching       Retrying      Logging
```

### Example

```java
import java.math.BigDecimal;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public class DecoratorDemo {

    interface PriceService { BigDecimal price(String sku); }

    // Concrete component: slow and flaky.
    static final class RemotePriceService implements PriceService {
        private int calls;
        public BigDecimal price(String sku) {
            calls++;
            if (calls % 2 == 1) throw new IllegalStateException("upstream timeout");
            return new BigDecimal("499.00");
        }
    }

    // Base decorator: forwards everything; subclasses override what they enhance.
    abstract static class PriceServiceDecorator implements PriceService {
        protected final PriceService inner;
        protected PriceServiceDecorator(PriceService inner) { this.inner = inner; }
        public BigDecimal price(String sku) { return inner.price(sku); }
    }

    static final class Retrying extends PriceServiceDecorator {
        private final int attempts;
        Retrying(PriceService inner, int attempts) { super(inner); this.attempts = attempts; }
        @Override public BigDecimal price(String sku) {
            RuntimeException last = null;
            for (int i = 1; i <= attempts; i++) {
                try { return inner.price(sku); }
                catch (RuntimeException e) { last = e; System.out.println("  retry " + i + ": " + e.getMessage()); }
            }
            throw last;
        }
    }

    static final class Caching extends PriceServiceDecorator {
        private final Map<String, BigDecimal> cache = new ConcurrentHashMap<>();
        Caching(PriceService inner) { super(inner); }
        @Override public BigDecimal price(String sku) { return cache.computeIfAbsent(sku, inner::price); }
    }

    static final class Logging extends PriceServiceDecorator {
        Logging(PriceService inner) { super(inner); }
        @Override public BigDecimal price(String sku) {
            long start = System.nanoTime();
            BigDecimal p = inner.price(sku);
            System.out.printf("price(%s)=%s in %d us%n", sku, p, (System.nanoTime() - start) / 1_000);
            return p;
        }
    }

    public static void main(String[] args) {
        // Order matters: cache outside retry means cached hits skip retries entirely.
        PriceService service = new Logging(new Caching(new Retrying(new RemotePriceService(), 3)));
        service.price("SKU-1");   // miss: retry once, then succeed
        service.price("SKU-1");   // hit: served from cache
    }
}
```

### Real-world uses

- `java.io`: `new BufferedInputStream(new GZIPInputStream(new FileInputStream(f)))`; every
  `FilterInputStream` subclass is a decorator.
- `Collections.unmodifiableList`, `synchronizedList`, `checkedList` wrap a list with the same
  interface and extra behaviour.
- Servlet API: `HttpServletRequestWrapper` / `HttpServletResponseWrapper` exist so filters can decorate
  requests and responses.
- Spring: `TransactionAwareDataSourceProxy` and `LazyConnectionDataSourceProxy` wrap a `DataSource`
  (named "proxy", structurally decorators); Spring Cache's `TransactionAwareCacheDecorator`.
- Resilience4j decorates `Supplier`s and `Function`s with retry, circuit breaker and rate limiter.

### Variations

- Abstract base decorator that forwards by default (shown above).
- **Functional decorators**: `Function<String, BigDecimal> withLogging(Function<...> f)`, or
  `Function.andThen` / `compose` for pipelines.
- Decorators applied by a framework through proxies and annotations (`@Cacheable`, `@Retryable`).

### Pros and cons

| Pros | Cons |
| --- | --- |
| Combine behaviours freely at runtime | Many small objects; stack traces get deep |
| Each decorator has a single responsibility | Order dependence is subtle (cache vs retry vs metrics) |
| Open/closed: add behaviour without editing the component | Identity breaks: `decorated != original`, `equals` can surprise |

### Pitfalls

- Wrong ordering, for example metrics outside the cache (counts hits) versus inside (counts only
  misses). Decide deliberately.
- Forgetting to forward a method in a large interface, silently skipping the inner object.
- Decorating a class whose methods call each other internally: the inner object's self-calls bypass
  the decorator.

### Related patterns

**Proxy** has the same structure but controls access and usually manages the subject itself.
**Composite** has many children; Decorator exactly one. **Strategy** changes the guts of an object;
Decorator changes its skin. **Chain of Responsibility** is a chain of decorators where any link may
stop the request.

## Facade

**Intent.** Provide a unified, simpler interface to a set of interfaces in a subsystem.

**Problem.** Placing an order touches inventory reservation, payment capture, shipment scheduling and
confirmation email, in the right order and with compensation on failure. Every controller, batch job
and message consumer that places orders duplicates this choreography and gets the failure handling
slightly different.

### Structure

```text
┌──────────────┐     ┌─────────────────────────┐
│ Controller   │────►│ CheckoutFacade          │
└──────────────┘     ├─────────────────────────┤
┌──────────────┐     │ + placeOrder(cart)      │
│ Batch job    │────►│                         │
└──────────────┘     └────────────┬────────────┘
                 ┌────────────────┼────────────────┬────────────────┐
                 ▼                ▼                ▼                ▼
          ┌────────────┐   ┌────────────┐   ┌────────────┐   ┌────────────┐
          │ Inventory  │   │ Payments   │   │ Shipping   │   │ Email      │
          └────────────┘   └────────────┘   └────────────┘   └────────────┘
                               subsystem (still usable directly)
```

### Example

```java
import java.math.BigDecimal;
import java.util.UUID;

public class FacadeDemo {

    record Cart(String userId, String sku, int qty, BigDecimal total) {}
    record OrderConfirmation(String orderId, String trackingId) {}

    static final class Inventory {
        boolean reserve(String sku, int qty) { System.out.println("reserve " + qty + " x " + sku); return true; }
        void release(String sku, int qty) { System.out.println("release " + qty + " x " + sku); }
    }
    static final class Payments {
        String capture(String user, BigDecimal amount) {
            if (amount.compareTo(new BigDecimal("100000")) > 0) throw new IllegalStateException("limit exceeded");
            return "pay_" + UUID.randomUUID().toString().substring(0, 8);
        }
    }
    static final class Shipping { String schedule(String user, String sku) { return "TRK" + (Math.abs(sku.hashCode()) % 10_000); } }
    static final class Email { void confirm(String user, String orderId) { System.out.println("email " + user + ": " + orderId); } }

    static final class CheckoutFacade {
        private final Inventory inventory;
        private final Payments payments;
        private final Shipping shipping;
        private final Email email;

        CheckoutFacade(Inventory i, Payments p, Shipping s, Email e) {
            inventory = i; payments = p; shipping = s; email = e;
        }

        OrderConfirmation placeOrder(Cart cart) {
            if (!inventory.reserve(cart.sku(), cart.qty()))
                throw new IllegalStateException("out of stock");
            try {
                String paymentId = payments.capture(cart.userId(), cart.total());
                String tracking = shipping.schedule(cart.userId(), cart.sku());
                String orderId = "ord_" + paymentId.substring(4);
                email.confirm(cart.userId(), orderId);
                return new OrderConfirmation(orderId, tracking);
            } catch (RuntimeException e) {
                inventory.release(cart.sku(), cart.qty());       // compensation lives in one place
                throw e;
            }
        }
    }

    public static void main(String[] args) {
        var facade = new CheckoutFacade(new Inventory(), new Payments(), new Shipping(), new Email());
        System.out.println(facade.placeOrder(new Cart("u42", "SKU-9", 2, new BigDecimal("1998"))));
        try {
            facade.placeOrder(new Cart("u42", "SKU-9", 1, new BigDecimal("250000")));
        } catch (IllegalStateException e) {
            System.out.println("failed: " + e.getMessage());
        }
    }
}
```

### Real-world uses

- **SLF4J** is literally the "Simple Logging Facade for Java".
- Spring's `JdbcTemplate`, `RestTemplate` / `RestClient` and `JmsTemplate` hide connection handling,
  resource cleanup and exception translation behind a small API.
- `javax.faces.context.FacesContext` and `java.net.URL.openStream()` (hides protocol handlers and
  connections) are often cited.
- In microservices, an API gateway or a backend-for-frontend is a facade at system scale.

### Variations

Multiple facades over one subsystem for different clients; a facade that is also the subsystem's only
public entry point (package-private internals); facade as a service-layer class in layered
architectures.

### Pros and cons

| Pros | Cons |
| --- | --- |
| Simple entry point; hides ordering and error handling | Can become a god object that knows everything |
| Decouples clients from subsystem churn | May hide capabilities power users need |
| Natural seam for testing and transactions | Thin pass-through facades add a layer for nothing |

### Pitfalls

Letting the facade grow business logic that belongs in the subsystems; forcing everyone through the
facade when some clients legitimately need finer control.

### Related patterns

**Adapter** changes an interface to match an expected one; Facade invents a simpler one. **Mediator**
coordinates peers that talk to the mediator; with a Facade, subsystem classes do not know the facade
exists. Facades are often **Singletons** or single beans.

## Flyweight

**Intent.** Use sharing to support large numbers of fine-grained objects efficiently, by separating
**intrinsic** state (shared, immutable) from **extrinsic** state (supplied by the client per use).

**Problem.** A map renders two million trees. Each tree has a position (unique) and a species with
name, colour and a 50 KB texture (shared by thousands). Storing the texture per tree costs roughly
100 GB; sharing one object per species costs a few MB.

### Structure

```text
┌──────────┐      ┌─────────────────────────────┐        ┌─────────────────────────────┐
│ Client   │─────►│ FlyweightFactory            │───────►│ Flyweight (TreeType)        │
│ (Forest) │      ├─────────────────────────────┤ cache  ├─────────────────────────────┤
└────┬─────┘      │ - pool : Map<Key,Flyweight> │        │ intrinsic: name, colour,    │
     │            │ + get(key) : Flyweight      │        │            texture          │
     │            └─────────────────────────────┘        │ + draw(x, y)  (extrinsic)   │
     │                                                   └─────────────────────────────┘
     └─► stores extrinsic state per object: Tree(x, y, typeRef)
```

### Example

```java
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

public class FlyweightDemo {

    // Flyweight: intrinsic, immutable, shared.
    record TreeType(String species, String colour, byte[] texture) {
        void draw(int x, int y) { /* render texture at (x, y) */ }
    }

    // Factory guarantees sharing.
    static final class TreeTypes {
        private static final Map<String, TreeType> POOL = new ConcurrentHashMap<>();
        static TreeType of(String species, String colour) {
            return POOL.computeIfAbsent(species + "|" + colour,
                    k -> new TreeType(species, colour, new byte[50_000]));   // expensive texture
        }
        static int size() { return POOL.size(); }
    }

    // Context object: small, holds extrinsic state plus a reference to the flyweight.
    record Tree(int x, int y, TreeType type) {
        void draw() { type.draw(x, y); }
    }

    public static void main(String[] args) {
        var random = new Random(42);
        String[] species = {"oak", "pine", "birch"};
        List<Tree> forest = new ArrayList<>();
        for (int i = 0; i < 1_000_000; i++) {
            String s = species[random.nextInt(species.length)];
            forest.add(new Tree(random.nextInt(10_000), random.nextInt(10_000), TreeTypes.of(s, "green")));
        }
        forest.forEach(Tree::draw);
        System.out.println("trees=" + forest.size() + ", shared types=" + TreeTypes.size());

        // The JDK does the same for small boxed integers:
        System.out.println(Integer.valueOf(127) == Integer.valueOf(127));   // true (cached)
        System.out.println(Integer.valueOf(128) == Integer.valueOf(128));   // false by default
    }
}
```

### Real-world uses

- `Integer.valueOf`, `Short.valueOf`, `Long.valueOf`, `Byte.valueOf` cache values from -128 to 127;
  `Character.valueOf` caches 0 to 127; `Boolean.valueOf` returns two shared constants. The upper bound
  of the `Integer` cache can be raised with `-XX:AutoBoxCacheMax`.
- String literals and `String.intern()` share one instance per distinct string in the string pool.
- Enum constants are shared, immutable flyweights.
- Text editors and browsers share glyph and font objects across characters.

### Variations

Unshared flyweights (some objects in the structure are not shared); flyweight factories with weak
references so unused flyweights can be collected; flyweights combined with **Composite** leaves.

### Pros and cons

| Pros | Cons |
| --- | --- |
| Large memory savings when intrinsic state dominates | Extrinsic state must be passed around, complicating APIs |
| Fewer allocations, better cache locality of hot data | Factory lookup costs CPU; unbounded pools leak |
| Shared immutable objects are thread-safe | Only pays off with huge numbers of similar objects |

### Pitfalls

- Comparing boxed numbers with `==`: it works up to 127 due to the cache and silently fails above.
- Mutable flyweights: one client's change corrupts everyone.
- Unbounded pools keyed by user input become a memory leak.

### Related patterns

**Singleton** is one shared instance; Flyweight is a pool of shared instances keyed by intrinsic state.
**Composite** leaves are often flyweights. **Object Pool** reuses *mutable* objects exclusively, one
client at a time; Flyweight shares *immutable* objects concurrently.

## Proxy

**Intent.** Provide a surrogate or placeholder for another object to **control access** to it.

**Problem.** An `AccountService` must enforce authorization, record timings and, in some deployments,
live in another process. Putting all of this into the service mixes concerns. You want a stand-in with
the same interface that intercepts calls.

Common kinds of proxy:

| Kind | Purpose | Example |
| --- | --- | --- |
| Virtual | Create the expensive real object lazily | Hibernate lazy-loaded entity |
| Protection | Check permissions before forwarding | Spring Security method security |
| Remote | Represent an object in another address space | RMI stub, gRPC client stub |
| Smart reference | Add bookkeeping: counting, locking, caching, logging | Spring `@Transactional`, `@Cacheable` |

### Structure

```text
┌──────────┐     ┌──────────────────────────┐
│ Client   │────►│ «interface» Subject      │
└──────────┘     ├──────────────────────────┤
                 │ + balance(id)            │
                 │ + withdraw(id, amount)   │
                 └────────────▲─────────────┘
                     ┌────────┴──────────────┐
          ┌──────────┴──────────┐  ┌─────────┴───────────┐
          │ Proxy               │─►│ RealSubject         │
          │ check / time / lazy │  │ (AccountServiceImpl)│
          └─────────────────────┘  └─────────────────────┘
```

### Example

```java
import java.lang.reflect.*;
import java.math.BigDecimal;
import java.util.*;

public class ProxyDemo {

    interface AccountService {
        BigDecimal balance(String accountId);
        void withdraw(String accountId, BigDecimal amount);
    }

    static final class AccountServiceImpl implements AccountService {
        private final Map<String, BigDecimal> balances = new HashMap<>(Map.of("acc-1", new BigDecimal("1000")));
        public BigDecimal balance(String id) { return balances.getOrDefault(id, BigDecimal.ZERO); }
        public void withdraw(String id, BigDecimal amt) { balances.merge(id, amt.negate(), BigDecimal::add); }
    }

    // Protection proxy, written by hand.
    static final class AuthorizingAccountService implements AccountService {
        private final AccountService target;
        private final Set<String> roles;
        AuthorizingAccountService(AccountService target, Set<String> roles) { this.target = target; this.roles = roles; }
        public BigDecimal balance(String id) { return target.balance(id); }
        public void withdraw(String id, BigDecimal amt) {
            if (!roles.contains("TELLER")) throw new SecurityException("withdraw requires TELLER");
            target.withdraw(id, amt);
        }
    }

    // Smart-reference proxy generated at runtime with java.lang.reflect.Proxy.
    @SuppressWarnings("unchecked")
    static <T> T timed(T target, Class<T> iface) {
        InvocationHandler handler = (proxy, method, args) -> {
            long start = System.nanoTime();
            try {
                return method.invoke(target, args);
            } catch (InvocationTargetException e) {
                throw e.getCause();                         // rethrow the real exception
            } finally {
                System.out.printf("%s took %d us%n", method.getName(), (System.nanoTime() - start) / 1_000);
            }
        };
        return (T) Proxy.newProxyInstance(iface.getClassLoader(), new Class<?>[] {iface}, handler);
    }

    public static void main(String[] args) {
        AccountService real = new AccountServiceImpl();
        AccountService service = timed(new AuthorizingAccountService(real, Set.of("VIEWER")), AccountService.class);

        System.out.println(service.balance("acc-1"));
        try {
            service.withdraw("acc-1", new BigDecimal("100"));
        } catch (SecurityException e) {
            System.out.println("denied: " + e.getMessage());
        }
    }
}
```

### Real-world uses

- `java.lang.reflect.Proxy` creates interface-based proxies at runtime.
- **Spring AOP**: `@Transactional`, `@Cacheable`, `@Async` and method security work through JDK dynamic
  proxies (when the bean implements interfaces and proxying by interface is configured) or CGLIB
  subclass proxies. Spring Boot defaults to class-based (CGLIB) proxies.
- **Hibernate / JPA** lazy associations return proxy objects that load from the database on first
  access (hence `LazyInitializationException` outside a session).
- RMI stubs and gRPC / Feign clients are remote proxies.

### Variations

Static (hand-written) proxies; dynamic proxies (`java.lang.reflect.Proxy`, interface only); subclass
proxies via bytecode generation (CGLIB, ByteBuddy), which cannot proxy `final` classes or methods.

### Pros and cons

| Pros | Cons |
| --- | --- |
| Cross-cutting concerns without touching the real class | Hidden behaviour: a method call may hit the network or DB |
| Lazy loading, access control, remoting transparently | Extra latency and indirection; harder debugging |
| Lifecycle of the real object can be managed by the proxy | Identity and `equals`/`hashCode` subtleties |

### Pitfalls

- **Self-invocation in Spring**: a method calling another `@Transactional` method on `this` bypasses the
  proxy, so the annotation has no effect.
- Proxying `final` classes or methods with subclass-based proxies silently does nothing.
- Remote proxies that look local encourage chatty calls (the "fallacies of distributed computing").

### Related patterns

Structurally identical to **Decorator**; the difference is intent and who controls the real object.
**Adapter** changes the interface; Proxy keeps it identical. **Facade** simplifies; Proxy does not.

## Decorator vs Proxy vs Adapter vs Facade

All four put an object in front of other objects. Ask: does the interface change, and why does the
wrapper exist?

| | Adapter | Decorator | Proxy | Facade |
| --- | --- | --- | --- | --- |
| Interface seen by client | Different from the wrapped object (matches what client expects) | Same as wrapped | Same as wrapped | New, simplified |
| Number of wrapped objects | One | One | One | Many (a subsystem) |
| Main purpose | Make incompatible things fit | Add responsibilities | Control access (lazy, security, remote, bookkeeping) | Simplify usage |
| Who creates the wrapped object | Usually given to it | Client, which stacks decorators | Often the proxy itself or a framework | The facade or DI |
| Stacking | Rare | Designed for it | Possible, usually one | No |
| When introduced | After the fact, to integrate | Anytime, to compose behaviour | Usually transparently by infrastructure | To tame a complex subsystem |
| JDK / framework example | `InputStreamReader` | `BufferedInputStream` | `java.lang.reflect.Proxy`, Spring AOP | SLF4J, `JdbcTemplate` |

A useful one-liner: **Adapter changes the interface, Decorator enhances it, Proxy guards it, Facade
simplifies it.**

---

# Behavioral patterns

Behavioral patterns distribute responsibility: who decides, who reacts, who knows the algorithm.

## Chain of Responsibility

**Intent.** Avoid coupling the sender of a request to its receiver by giving more than one object a
chance to handle it. Chain the receivers and pass the request along until handled.

**Problem.** Every HTTP request must pass logging, authentication, rate limiting and then reach the
endpoint. Each concern may short-circuit (401, 429). Hard-coding the sequence in each endpoint
duplicates it, and reordering means editing many files.

### Structure

```text
┌────────┐ handle ┌─────────┐ next ┌─────────┐ next ┌───────────┐
│ Client │───────►│ Logging │─────►│ Auth    │─────►│ RateLimit │──► endpoint
└────────┘        └─────────┘      └────┬────┘      └─────┬─────┘
                                        │ no token        │ too many
                                        ▼                 ▼
                                       401               429

  «interface» Middleware: handle(request, chain) ; «interface» Chain: proceed(request)
```

### Example

```java
import java.util.*;
import java.util.function.Function;

public class ChainDemo {

    record Request(String path, Map<String, String> headers) {}
    record Response(int status, String body) {}

    interface Chain { Response proceed(Request request); }

    @FunctionalInterface
    interface Middleware { Response handle(Request request, Chain next); }

    // Index-based chain, the same design as servlet FilterChain and OkHttp interceptors.
    record Pipeline(List<Middleware> middlewares, int index, Function<Request, Response> endpoint)
            implements Chain {
        public Response proceed(Request request) {
            if (index == middlewares.size()) return endpoint.apply(request);
            return middlewares.get(index).handle(request, new Pipeline(middlewares, index + 1, endpoint));
        }
    }

    static Middleware logging() {
        return (req, next) -> {
            Response r = next.proceed(req);                  // around-advice: before and after
            System.out.println(req.path() + " -> " + r.status());
            return r;
        };
    }

    static Middleware auth() {
        return (req, next) -> req.headers().containsKey("Authorization")
                ? next.proceed(req)
                : new Response(401, "missing token");
    }

    static Middleware rateLimit(int maxRequests) {
        var counts = new HashMap<String, Integer>();
        return (req, next) -> {
            String who = req.headers().getOrDefault("Authorization", "anon");
            int n = counts.merge(who, 1, Integer::sum);
            return n > maxRequests ? new Response(429, "slow down") : next.proceed(req);
        };
    }

    public static void main(String[] args) {
        Chain app = new Pipeline(List.of(logging(), auth(), rateLimit(2)), 0,
                req -> new Response(200, "orders for " + req.headers().get("Authorization")));

        var authed = new Request("/orders", Map.of("Authorization", "alice"));
        app.proceed(new Request("/orders", Map.of()));   // 401
        app.proceed(authed);                             // 200
        app.proceed(authed);                             // 200
        app.proceed(authed);                             // 429
    }
}
```

### Real-world uses

- Servlet `Filter` / `FilterChain.doFilter`, and Spring Security's `SecurityFilterChain` (a list of
  filters such as authentication, CSRF and authorization).
- Spring MVC `HandlerInterceptor`s; OkHttp `Interceptor` chain; Netty's `ChannelPipeline` of handlers.
- `java.util.logging.Logger` forwards records to parent loggers' handlers.
- Exception handling itself: a thrown exception travels up the call stack until a matching `catch`.

### Variations

- **Pure GoF**: exactly one handler handles the request; others pass it on (approval workflow: team
  lead, manager, director by amount).
- **Pipeline / middleware**: every handler does some work and may stop the chain (shown above).
- Linked handlers (`setNext`) versus a list plus index (easier to reorder and configure).

### Pros and cons

| Pros | Cons |
| --- | --- |
| Sender does not know which handler acts | A request may fall off the end unhandled |
| Add, remove, reorder handlers by configuration | Debugging requires knowing the chain order |
| Each handler has a single concern | Long chains add latency to every request |

### Pitfalls

Order bugs (rate limiting before authentication lets anonymous floods consume per-user quota, or
logging placed after auth misses rejected requests); handlers that forget to call `next`; cycles in
linked chains.

### Related patterns

Often built from **Decorators** (a chain of wrappers). **Command** objects are frequently what travels
down the chain. **Composite** parents can act as the next handler (GoF's help-system example).

## Command

**Intent.** Encapsulate a request as an object, letting you parameterize clients with requests, queue
or log them, and support undoable operations.

**Problem.** A collaborative editor needs undo/redo, a macro recorder and an operation log for crash
recovery. If the toolbar calls `document.insert(...)` directly, there is nothing to store, replay or
reverse. Turning each action into an object gives you all three.

### Structure

```text
┌───────────────┐ executes  ┌──────────────────────────┐        ┌────────────────┐
│ Invoker       │──────────►│ «interface» Command      │        │ Receiver       │
│ (History,     │           ├──────────────────────────┤        │ (Document)     │
│  button,      │           │ + execute()              │        ├────────────────┤
│  queue)       │           │ + undo()                 │        │ insert(),      │
└───────────────┘           └────────────▲─────────────┘        │ delete()       │
                                         │                      └───────▲────────┘
                            ┌────────────┴─────────────┐                │
                            │ ConcreteCommand          │────────────────┘
                            │ (Insert, Delete, Macro)  │   calls receiver
                            └──────────────────────────┘
  Client creates ConcreteCommand(receiver, args) and hands it to the Invoker.
```

### Example

```java
import java.util.*;

public class CommandDemo {

    // Receiver
    static final class Document {
        private final StringBuilder text = new StringBuilder();
        void insert(int pos, String s) { text.insert(pos, s); }
        String delete(int pos, int len) {
            String removed = text.substring(pos, pos + len);
            text.delete(pos, pos + len);
            return removed;
        }
        @Override public String toString() { return text.toString(); }
    }

    interface Command { void execute(); void undo(); }

    record Insert(Document doc, int pos, String text) implements Command {
        public void execute() { doc.insert(pos, text); }
        public void undo() { doc.delete(pos, text.length()); }
    }

    static final class Delete implements Command {
        private final Document doc; private final int pos, len;
        private String removed;                               // captured for undo
        Delete(Document doc, int pos, int len) { this.doc = doc; this.pos = pos; this.len = len; }
        public void execute() { removed = doc.delete(pos, len); }
        public void undo() { doc.insert(pos, removed); }
    }

    record Macro(List<Command> steps) implements Command {
        public void execute() { steps.forEach(Command::execute); }
        public void undo() {
            for (int i = steps.size() - 1; i >= 0; i--) steps.get(i).undo();
        }
    }

    // Invoker with undo/redo stacks
    static final class History {
        private final Deque<Command> undo = new ArrayDeque<>();
        private final Deque<Command> redo = new ArrayDeque<>();
        void run(Command c) { c.execute(); undo.push(c); redo.clear(); }
        void undo() { if (!undo.isEmpty()) { Command c = undo.pop(); c.undo(); redo.push(c); } }
        void redo() { if (!redo.isEmpty()) { Command c = redo.pop(); c.execute(); undo.push(c); } }
    }

    public static void main(String[] args) {
        var doc = new Document();
        var history = new History();
        history.run(new Insert(doc, 0, "Hello world"));
        history.run(new Delete(doc, 5, 6));                  // "Hello"
        history.run(new Macro(List.of(new Insert(doc, 5, ","), new Insert(doc, 6, " team"))));
        System.out.println(doc);                             // Hello, team
        history.undo();
        System.out.println(doc);                             // Hello
        history.undo();
        System.out.println(doc);                             // Hello world
        history.redo();
        System.out.println(doc);                             // Hello
    }
}
```

### Real-world uses

- `Runnable` and `Callable` submitted to an `ExecutorService`: the executor (invoker) runs commands
  without knowing what they do.
- Swing's `javax.swing.Action` and the `javax.swing.undo` package (`UndoableEdit`, `UndoManager`).
- Job queues and message-driven systems: a serialized command in Kafka or SQS is a Command crossing
  process boundaries; CQRS "commands" are the same idea at architecture level.
- Database transaction logs and event-sourcing replays.

### Variations

- **Simple command** = a lambda (`Runnable`), when undo and metadata are not needed.
- **Undoable command** storing the state needed to reverse (or a **Memento**).
- **Macro command** (a Composite of commands).
- **Serializable command** for queues, retries and audit logs.
- Compensating commands in sagas (undo across services).

### Pros and cons

| Pros | Cons |
| --- | --- |
| Decouples invoker from receiver | Many small classes if every action is a class |
| Undo/redo, queuing, logging, retries, scheduling | Undo is hard when operations are not reversible |
| Macros by composition | Commands holding receiver references are hard to serialize |

### Pitfalls

Undo that depends on state captured at construction rather than at execution (capture in `execute()`,
as `Delete` does). Forgetting to clear the redo stack after a new command. Non-idempotent commands in
at-least-once queues.

### Related patterns

**Memento** stores state for undo; **Composite** builds macros; **Chain of Responsibility** can pass
commands along. **Strategy** is also "behaviour as an object", but a strategy is *how* to do something
for a context, while a command is *what* to do, as a self-contained request.

## Interpreter

**Intent.** Given a language, define a representation for its grammar along with an interpreter that
uses the representation to interpret sentences in the language.

**Problem.** Product managers want to target features with rules like
`country == "IN" AND (age > 18 OR plan == "pro")`, editable without a deployment. You need to
represent such rules as objects and evaluate them against a user context.

### Structure

```text
┌──────────┐      ┌──────────────────────────────┐
│ Client   │─────►│ «interface» Expression       │◄───────────────┐
└──────────┘      ├──────────────────────────────┤                │
     │            │ + interpret(context) : bool  │                │ operands
     │            └───────────────▲──────────────┘                │
     │               ┌────────────┴─────────────┐                 │
     │     ┌─────────┴──────────┐   ┌───────────┴──────────┐      │
     │     │ TerminalExpression │   │ NonterminalExpression│◆─────┘
     │     │ Eq, Gt             │   │ And, Or, Not         │
     │     └────────────────────┘   └──────────────────────┘
     └──► Context: Map<String, Object> (the user's attributes)
```

### Example

```java
import java.util.*;

public class InterpreterDemo {

    sealed interface Rule permits Eq, Gt, And, Or, Not {
        boolean interpret(Map<String, Object> ctx);
    }

    // Terminal expressions
    record Eq(String field, Object value) implements Rule {
        public boolean interpret(Map<String, Object> ctx) { return Objects.equals(ctx.get(field), value); }
    }
    record Gt(String field, double threshold) implements Rule {
        public boolean interpret(Map<String, Object> ctx) {
            return ctx.get(field) instanceof Number n && n.doubleValue() > threshold;
        }
    }

    // Nonterminal expressions
    record And(Rule left, Rule right) implements Rule {
        public boolean interpret(Map<String, Object> ctx) { return left.interpret(ctx) && right.interpret(ctx); }
    }
    record Or(Rule left, Rule right) implements Rule {
        public boolean interpret(Map<String, Object> ctx) { return left.interpret(ctx) || right.interpret(ctx); }
    }
    record Not(Rule inner) implements Rule {
        public boolean interpret(Map<String, Object> ctx) { return !inner.interpret(ctx); }
    }

    public static void main(String[] args) {
        // country == "IN" AND (age > 18 OR plan == "pro") AND NOT banned == true
        Rule rule = new And(
                new And(new Eq("country", "IN"), new Or(new Gt("age", 18), new Eq("plan", "pro"))),
                new Not(new Eq("banned", true)));

        System.out.println(rule.interpret(Map.of("country", "IN", "age", 25, "plan", "free", "banned", false))); // true
        System.out.println(rule.interpret(Map.of("country", "IN", "age", 16, "plan", "pro", "banned", false)));  // true
        System.out.println(rule.interpret(Map.of("country", "US", "age", 30, "plan", "pro", "banned", false)));  // false
    }
}
```

The pattern covers only the **representation and evaluation**. Turning the text
`country == "IN" AND ...` into this tree is a separate parsing step: a small recursive-descent parser
or a parser generator such as ANTLR.

### Real-world uses

- `java.util.regex.Pattern` compiles a regular expression into an internal tree of node objects that
  match input.
- Spring Expression Language (SpEL): expressions are parsed into an AST (`org.springframework.expression.spel.ast`)
  whose nodes evaluate themselves.
- JPA Criteria API builds query expression trees; rule engines and template languages use the same
  idea.

### Variations

Interpretation inside the nodes (GoF) versus a separate evaluator using **Visitor** or pattern
matching; compiling the tree to bytecode or closures for speed; caching parsed trees.

### Pros and cons

| Pros | Cons |
| --- | --- |
| Grammar maps directly to classes; easy to extend | Complex grammars produce many classes; use a parser generator |
| Rules become data that can be stored and edited | Tree-walking is slow compared with compiled code |
| Easy to add evaluation contexts | Adding a new *operation* (pretty print, validate) touches every class |

### Pitfalls

Building a general-purpose language by accident; unbounded recursion on user-supplied deep
expressions; letting untrusted users write expressions in powerful languages (SpEL injection is a
real vulnerability class, so restrict the evaluation context).

### Related patterns

The syntax tree is a **Composite**. **Visitor** adds new operations over the tree without changing node
classes. **Flyweight** can share terminal symbols. **Iterator** traverses the tree.

## Iterator

**Intent.** Provide a way to access the elements of an aggregate sequentially without exposing its
underlying representation.

**Problem.** A REST API returns users 100 per page with a `page` parameter. Callers want to loop over
"all users" without knowing about pages, without loading 2 million users into memory, and with the same
code whether data comes from a list, a database cursor or an API.

### Structure

```text
┌──────────┐     ┌──────────────────────────┐ creates ┌──────────────────────────┐
│ Client   │────►│ «interface» Iterable<T>  │────────►│ «interface» Iterator<T>  │
└──────────┘     ├──────────────────────────┤         ├──────────────────────────┤
                 │ + iterator()             │         │ + hasNext()              │
                 └────────────▲─────────────┘         │ + next()                 │
                              │                       └────────────▲─────────────┘
                 ┌────────────┴─────────────┐         ┌────────────┴─────────────┐
                 │ UserDirectory            │────────►│ PagedIterator            │
                 │ (concrete aggregate)     │         │ (tracks page, position)  │
                 └──────────────────────────┘         └──────────────────────────┘
```

### Example

```java
import java.util.*;
import java.util.function.IntFunction;
import java.util.stream.*;

public class IteratorDemo {

    record User(int id, String name) {}

    // Lazily fetches one page at a time; the client never sees pages.
    static final class PagedIterator<T> implements Iterator<T> {
        private final IntFunction<List<T>> fetchPage;
        private int nextPage = 0;
        private Iterator<T> current = Collections.emptyIterator();
        private boolean exhausted;

        PagedIterator(IntFunction<List<T>> fetchPage) { this.fetchPage = fetchPage; }

        @Override public boolean hasNext() {
            while (!current.hasNext() && !exhausted) {
                List<T> page = fetchPage.apply(nextPage++);
                if (page.isEmpty()) exhausted = true;
                else current = page.iterator();
            }
            return current.hasNext();
        }

        @Override public T next() {
            if (!hasNext()) throw new NoSuchElementException();
            return current.next();
        }
    }

    static final class UserDirectory implements Iterable<User> {
        private static final int PAGE_SIZE = 3, TOTAL = 8;

        private List<User> fetch(int page) {                 // simulates GET /users?page=N
            System.out.println("  fetching page " + page);
            return IntStream.range(page * PAGE_SIZE, Math.min(TOTAL, (page + 1) * PAGE_SIZE))
                    .mapToObj(i -> new User(i, "user" + i)).toList();
        }

        @Override public Iterator<User> iterator() { return new PagedIterator<>(this::fetch); }

        Stream<User> stream() {
            return StreamSupport.stream(
                    Spliterators.spliteratorUnknownSize(iterator(), Spliterator.ORDERED | Spliterator.NONNULL),
                    false);
        }
    }

    public static void main(String[] args) {
        var directory = new UserDirectory();
        for (User u : directory) System.out.println(u);              // for-each uses iterator()

        // Laziness: only the pages needed for the first 4 users are fetched.
        System.out.println(directory.stream().limit(4).map(User::name).toList());
    }
}
```

### Real-world uses

- `java.util.Iterator`, `ListIterator`, legacy `Enumeration`; the enhanced `for` loop works on any
  `Iterable`.
- `Spliterator` (Java 8) adds splitting for parallel streams.
- `Scanner` implements `Iterator<String>`; `DirectoryStream<Path>` is `Iterable`; JDBC `ResultSet` is a
  cursor-style iterator (`next()` plus getters).
- Spring Data's `Slice` / `Page` and streaming query methods returning `Stream<T>`.

### Variations

- **External iterator** (client calls `next()`, as above) versus **internal iterator** (the collection
  drives, client passes a function: `forEach`, streams).
- **Fail-fast** iterators (`ArrayList`, `HashMap`) throw `ConcurrentModificationException` when the
  collection is structurally modified during iteration (best effort, not guaranteed).
- **Weakly consistent** iterators (`ConcurrentHashMap`, `CopyOnWriteArrayList`) never throw and may or
  may not reflect concurrent updates.
- Generators (Python `yield`) are language-level iterators.

### Pros and cons

| Pros | Cons |
| --- | --- |
| Hides storage; one traversal API for many sources | Stateful; not thread-safe to share one iterator |
| Lazy, constant-memory traversal of huge or remote data | External iterators are verbose compared with streams |
| Multiple simultaneous traversals | Resource-backed iterators must be closed (`Stream` from DB) |

### Pitfalls

Calling `next()` without `hasNext()`; modifying a collection inside for-each (use `Iterator.remove()`
or `removeIf`); `hasNext()` with side effects that are not idempotent; leaking database cursors from
unclosed streams.

### Related patterns

Iterators traverse **Composites**. Collections use **Factory Method** (`iterator()`) to create them.
**Memento** can capture iteration state. **Visitor** also traverses but adds operations per element type.

## Mediator

**Intent.** Define an object that encapsulates how a set of objects interact, promoting loose coupling
by keeping objects from referring to each other explicitly.

**Problem.** A signup form has an email field, a "company account" checkbox, a company-name field and
a submit button. The company field is enabled only when the checkbox is ticked; submit is enabled only
when the email is valid, terms are accepted and, if company is ticked, the company name is filled. If
each widget references the others, you get an n² web of dependencies that nobody can change safely.

### Structure

```text
                 ┌─────────────────────────────┐
                 │ «interface» Mediator        │
                 │ + changed(component)        │
                 └──────────────▲──────────────┘
                                │
                 ┌──────────────┴──────────────┐
          ┌─────►│ SignupDialog (concrete)     │◄─────┐
          │      │ knows every component       │      │
          │      └──┬───────────┬───────────┬──┘      │
 notifies │         │ updates   │           │         │ notifies
          │         ▼           ▼           ▼         │
     ┌────┴─────┐ ┌──────────┐ ┌──────────┐ ┌─────────┴┐
     │ Email    │ │ Company  │ │ Company  │ │ Terms    │
     │ field    │ │ checkbox │ │ name     │ │ checkbox │
     └──────────┘ └──────────┘ └──────────┘ └──────────┘
   Components know only the mediator, never each other.
```

### Example

```java
public class MediatorDemo {

    interface FormMediator { void changed(Component source); }

    abstract static class Component {
        protected final FormMediator mediator;
        final String name;
        boolean enabled = true;
        Component(FormMediator mediator, String name) { this.mediator = mediator; this.name = name; }
    }

    static final class TextField extends Component {
        String value = "";
        TextField(FormMediator m, String name) { super(m, name); }
        void type(String v) { value = v; mediator.changed(this); }
    }

    static final class CheckBox extends Component {
        boolean checked;
        CheckBox(FormMediator m, String name) { super(m, name); }
        void toggle() { checked = !checked; mediator.changed(this); }
    }

    static final class Button extends Component {
        Button(FormMediator m, String name) { super(m, name); }
    }

    // Concrete mediator: all interaction rules live here.
    static final class SignupDialog implements FormMediator {
        final TextField email = new TextField(this, "email");
        final CheckBox company = new CheckBox(this, "company");
        final TextField companyName = new TextField(this, "companyName");
        final CheckBox terms = new CheckBox(this, "terms");
        final Button submit = new Button(this, "submit");

        SignupDialog() { recompute(); }

        @Override public void changed(Component source) {
            if (source == company && !company.checked) companyName.value = "";
            recompute();
        }

        private void recompute() {
            companyName.enabled = company.checked;
            boolean companyOk = !company.checked || !companyName.value.isBlank();
            submit.enabled = email.value.contains("@") && terms.checked && companyOk;
        }

        String state() {
            return "companyName.enabled=" + companyName.enabled + " submit.enabled=" + submit.enabled;
        }
    }

    public static void main(String[] args) {
        var form = new SignupDialog();
        System.out.println(form.state());
        form.email.type("dev@example.com");
        form.terms.toggle();
        System.out.println(form.state());          // submit enabled
        form.company.toggle();
        System.out.println(form.state());          // company name required -> submit disabled
        form.companyName.type("Acme");
        System.out.println(form.state());          // submit enabled again
    }
}
```

### Real-world uses

- GUI dialogs and controllers (the GoF example): a dialog mediates between its widgets.
- Spring MVC's `DispatcherServlet` coordinates handler mappings, handler adapters, view resolvers and
  exception resolvers, which do not reference one another.
- Air-traffic control, chat rooms, and message brokers at system scale (participants talk to the hub,
  not to each other).
- The MediatR library in .NET popularized in-process request/handler mediation; Java codebases build
  the same with a small dispatcher or Spring events.

### Variations

Mediator with explicit method per event versus a generic `changed(source)`; event-based mediators
(components publish events, the mediator subscribes), which is where Mediator meets Observer.

### Pros and cons

| Pros | Cons |
| --- | --- |
| Replaces many-to-many links with one-to-many | The mediator can become a god object |
| Interaction logic is centralized and testable | Changes in any component may require mediator changes |
| Components become reusable in other contexts | Centralized control can become a bottleneck |

### Pitfalls

Mediators that accumulate business logic unrelated to coordination; infinite notification loops (the
mediator updates a component, which notifies the mediator again).

### Related patterns

**Facade** simplifies a subsystem for outside clients and subsystem classes do not know it exists;
Mediator coordinates peers, which do know the mediator. **Observer** is one way to implement the
notification path. A pub/sub broker is a distributed mediator.

## Memento

**Intent.** Without violating encapsulation, capture and externalize an object's internal state so
the object can be restored to this state later.

**Problem.** An editor needs "undo to last checkpoint" and autosave snapshots. The undo manager must
store states, but it should not be able to read or modify the editor's private fields (cursor, text,
selection), or every change to the editor's internals breaks the undo manager.

### Structure

```text
┌───────────────────────────┐ creates  ┌───────────────────────────┐
│ Originator (Editor)       │─────────►│ Memento (Snapshot)        │
├───────────────────────────┤          ├───────────────────────────┤
│ - text, cursor            │          │ narrow interface:         │
│ + save() : Memento        │          │   takenAt()               │
│ + restore(Memento)        │          │ wide (private) contents:  │
└───────────────────────────┘          │   text, cursor            │
                                       └─────────────▲─────────────┘
┌───────────────────────────┐  stores                │
│ Caretaker (History)       │────────────────────────┘
│ never looks inside        │
└───────────────────────────┘
```

### Example

```java
import java.time.Instant;
import java.util.*;

public class MementoDemo {

    static final class Editor {
        private String text = "";
        private int cursor = 0;

        // Narrow interface visible to everyone.
        interface Snapshot { Instant takenAt(); }

        // Wide interface: only Editor can see the contents.
        private record State(String text, int cursor, Instant takenAt) implements Snapshot {}

        void type(String s) {
            text = text.substring(0, cursor) + s + text.substring(cursor);
            cursor += s.length();
        }
        void moveCursor(int pos) { cursor = Math.max(0, Math.min(pos, text.length())); }

        Snapshot save() { return new State(text, cursor, Instant.now()); }

        void restore(Snapshot snapshot) {
            if (!(snapshot instanceof State s)) throw new IllegalArgumentException("foreign snapshot");
            text = s.text();
            cursor = s.cursor();
        }

        @Override public String toString() { return "\"" + text + "\" cursor=" + cursor; }
    }

    // Caretaker: stores snapshots, bounded, never inspects them.
    static final class History {
        private final Deque<Editor.Snapshot> stack = new ArrayDeque<>();
        private final int limit;
        History(int limit) { this.limit = limit; }
        void push(Editor.Snapshot s) {
            stack.push(s);
            if (stack.size() > limit) stack.removeLast();
        }
        Optional<Editor.Snapshot> pop() { return Optional.ofNullable(stack.poll()); }
    }

    public static void main(String[] args) {
        var editor = new Editor();
        var history = new History(50);

        editor.type("Design patterns");
        history.push(editor.save());
        editor.moveCursor(6);
        editor.type(" and refactoring");
        System.out.println(editor);

        history.pop().ifPresent(editor::restore);
        System.out.println(editor);                      // back to the checkpoint
    }
}
```

### Real-world uses

- `java.sql.Savepoint`: `Connection.setSavepoint()` returns an opaque token and
  `rollback(savepoint)` restores; the caller cannot inspect the database state inside it.
- Undo in editors and IDEs; game save states; snapshotting in event-sourced systems (store a snapshot
  every N events to avoid replaying from the beginning).
- Serialization (`Serializable`) is sometimes used to externalize a memento.

### Variations

Full snapshots versus incremental (diff) mementos; persistent mementos written to disk; immutable
originators, where the "memento" is simply the previous immutable value (persistent data structures
make this cheap).

### Pros and cons

| Pros | Cons |
| --- | --- |
| Preserves encapsulation; caretaker sees an opaque token | Memory cost of many large snapshots |
| Simple undo and checkpointing | Snapshot creation can be slow for big state |
| Originator stays in control of what is saved | Caretaker cannot judge memento size or relevance |

### Pitfalls

Shallow snapshots of mutable fields (later edits leak into "saved" state); unbounded histories; storing
references to external resources that cannot be restored.

### Related patterns

**Command** often uses Memento to implement undo (snapshot before execute). **Prototype** copies to
create a new object; Memento captures state to restore the same object. **Iterator** can use a memento
for its position.

## Observer

**Intent.** Define a one-to-many dependency so that when one object changes state, all its dependents
are notified and updated automatically.

**Problem.** When a stock price changes, the chart redraws, an alert service checks thresholds and an
audit log records the change. The `Stock` class should not know about charts, alerts or logs, and new
reactions should be added without editing it.

### Structure

```text
┌─────────────────────────────┐ notifies 0..n ┌───────────────────────────┐
│ Subject (Stock)             │──────────────►│ «interface» Observer      │
├─────────────────────────────┤               ├───────────────────────────┤
│ - observers : List<Obs>     │               │ + onEvent(event)          │
│ + subscribe(o) : Subscr.    │               └─────────────▲─────────────┘
│ + setPrice(p)  (notifies)   │                  ┌──────────┼──────────┐
└─────────────────────────────┘               ┌──┴────┐ ┌───┴───┐ ┌────┴────┐
                                              │ Chart │ │ Alert │ │ Audit   │
                                              └───────┘ └───────┘ └─────────┘
```

### Example

```java
import java.math.BigDecimal;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.function.Consumer;

public class ObserverDemo {

    record PriceChanged(String symbol, BigDecimal oldPrice, BigDecimal newPrice) {}

    interface Subscription extends AutoCloseable { @Override void close(); }

    static final class Stock {
        private final String symbol;
        private BigDecimal price;
        // Copy-on-write: safe if listeners subscribe/unsubscribe during notification.
        private final List<Consumer<PriceChanged>> listeners = new CopyOnWriteArrayList<>();

        Stock(String symbol, BigDecimal price) { this.symbol = symbol; this.price = price; }

        Subscription subscribe(Consumer<PriceChanged> listener) {
            listeners.add(listener);
            return () -> listeners.remove(listener);
        }

        void setPrice(BigDecimal newPrice) {
            var event = new PriceChanged(symbol, price, newPrice);
            price = newPrice;
            for (var l : listeners) {
                try { l.accept(event); }
                catch (RuntimeException e) { System.out.println("listener failed: " + e.getMessage()); }
            }
        }
    }

    public static void main(String[] args) {
        var infy = new Stock("INFY", new BigDecimal("1500"));

        Subscription chart = infy.subscribe(e -> System.out.println("chart: " + e.symbol() + " " + e.newPrice()));
        infy.subscribe(e -> {
            if (e.newPrice().compareTo(new BigDecimal("1600")) > 0) System.out.println("ALERT: " + e.symbol() + " above 1600");
        });
        infy.subscribe(e -> { throw new IllegalStateException("audit DB down"); });   // isolated

        infy.setPrice(new BigDecimal("1620"));
        chart.close();                                       // unsubscribe
        infy.setPrice(new BigDecimal("1580"));
    }
}
```

### Real-world uses

- `java.beans.PropertyChangeSupport` / `PropertyChangeListener`; Swing and AWT event listeners.
- `java.util.Observer` / `Observable`, deprecated since Java 9 (not serializable, not thread-safe,
  `Observable` is a class you must extend, and the event model is too thin).
- `java.util.concurrent.Flow` (Java 9) defines Reactive Streams interfaces with back-pressure;
  `SubmissionPublisher` implements `Flow.Publisher`. Project Reactor and RxJava build on the same ideas.
- Spring's `ApplicationEventPublisher` with `@EventListener` / `ApplicationListener`, including
  `@TransactionalEventListener` to fire after commit.

### Variations

- **Push** (event carries data, shown above) versus **pull** (observer is told "something changed" and
  queries the subject).
- Synchronous versus asynchronous delivery (executor per listener).
- Typed event objects (records) instead of one generic `update()`.
- Weak listeners to avoid leaks; reactive streams with back-pressure.

### Pros and cons

| Pros | Cons |
| --- | --- |
| Subject knows only an interface; observers added freely | Notification order is unspecified; cascades are hard to follow |
| Supports broadcast and runtime subscription | Memory leaks from forgotten subscriptions (lapsed listener) |
| Foundation of event-driven and reactive designs | A slow or failing observer can stall or break the subject |

### Pitfalls

- **Lapsed listener**: a long-lived subject keeps short-lived observers reachable forever. Always
  return a way to unsubscribe.
- `ConcurrentModificationException` when a listener unsubscribes during notification with a plain
  `ArrayList`.
- Re-entrancy: an observer changes the subject during notification, triggering nested notifications.
- One throwing listener aborting the loop for all others.

### Observer vs pub/sub

| | Observer (GoF) | Publish/subscribe |
| --- | --- | --- |
| Coupling | Observers register directly with the subject; subject holds references | Publishers and subscribers know only a broker/topic, not each other |
| Location | Same process, usually same thread | Often across processes and machines (Kafka, RabbitMQ, Redis Pub/Sub, SNS) |
| Delivery | Usually synchronous method calls | Usually asynchronous, buffered, possibly persistent |
| Filtering | Subject decides what to send | Broker routes by topic, key or content |
| Failure model | Exception propagates to the subject unless caught | Retries, dead-letter queues, at-least-once semantics |
| Ordering and replay | In-memory, no replay | Broker-dependent (Kafka: ordered per partition, replayable by offset) |

In-process event buses (Spring events, Guava `EventBus`) sit between the two: a mediator-like bus in
one JVM. Pub/sub is Observer plus a **Mediator** (the broker) plus durability.

### Related patterns

**Mediator** can be implemented with Observer; an event bus is both. **Command** objects can be the
events. **Singleton** subjects are common (application-wide event bus).

## State

**Intent.** Allow an object to alter its behaviour when its internal state changes. The object will
appear to change its class.

**Problem.** An order moves through Created, Paid, Shipped, Delivered and Cancelled. Each operation
(pay, ship, deliver, cancel) is legal only in some states. With a `status` field, every method becomes a
`switch`, the rules for one state are scattered across all methods, and adding a "Returned" state means
editing every switch.

### Structure

```text
┌─────────────────────────────┐ delegates  ┌─────────────────────────────┐
│ Context (Order)             │───────────►│ «interface» OrderState      │
├─────────────────────────────┤            ├─────────────────────────────┤
│ - state : OrderState        │            │ + pay()      : OrderState   │
│ + pay()  { state =          │            │ + ship()     : OrderState   │
│           state.pay() }     │            │ + cancel()   : OrderState   │
└─────────────────────────────┘            └──────────────▲──────────────┘
                                       ┌─────────┬────────┴┬──────────┬──────────┐
                                    Created     Paid    Shipped   Delivered  Cancelled

  Created ──pay──► Paid ──ship──► Shipped ──deliver──► Delivered
     │               │
     └────cancel─────┴──────────► Cancelled
```

### Example

```java
public class StateDemo {

    interface OrderState {
        String name();
        default OrderState pay()     { throw illegal("pay"); }
        default OrderState ship()    { throw illegal("ship"); }
        default OrderState deliver() { throw illegal("deliver"); }
        default OrderState cancel()  { throw illegal("cancel"); }
        private IllegalStateException illegal(String action) {
            return new IllegalStateException("cannot " + action + " an order that is " + name());
        }
    }

    // Each state lists only the transitions it allows.
    record Created() implements OrderState {
        public String name() { return "CREATED"; }
        public OrderState pay() { return new Paid(); }
        public OrderState cancel() { return new Cancelled("customer request"); }
    }
    record Paid() implements OrderState {
        public String name() { return "PAID"; }
        public OrderState ship() { return new Shipped(); }
        public OrderState cancel() { return new Cancelled("refund issued"); }
    }
    record Shipped() implements OrderState {
        public String name() { return "SHIPPED"; }
        public OrderState deliver() { return new Delivered(); }
    }
    record Delivered() implements OrderState { public String name() { return "DELIVERED"; } }
    record Cancelled(String reason) implements OrderState { public String name() { return "CANCELLED"; } }

    // Context: delegates and swaps its state object.
    static final class Order {
        private OrderState state = new Created();
        void pay()     { transition(state.pay()); }
        void ship()    { transition(state.ship()); }
        void deliver() { transition(state.deliver()); }
        void cancel()  { transition(state.cancel()); }
        private void transition(OrderState next) {
            System.out.println(state.name() + " -> " + next.name());
            state = next;
        }
    }

    public static void main(String[] args) {
        var order = new Order();
        order.pay();
        order.ship();
        try {
            order.cancel();
        } catch (IllegalStateException e) {
            System.out.println("rejected: " + e.getMessage());
        }
        order.deliver();
    }
}
```

A compact alternative for small machines is an **enum with constant-specific methods**:

```java
enum Light {
    RED    { Light next() { return GREEN; } },
    GREEN  { Light next() { return YELLOW; } },
    YELLOW { Light next() { return RED; } };
    abstract Light next();
}
```

### Real-world uses

- GoF's original example is a TCP connection (`TCPEstablished`, `TCPListen`, `TCPClosed`).
- Spring Statemachine provides a framework for state machines with guards, actions and persistence.
- Workflow engines, parsers and protocol handlers (HTTP/2 stream states) are state machines; many
  implementations use the State pattern or a transition table.

### Variations

Stateless state objects shared as singletons or enum constants; states holding data (the `Cancelled`
reason); transitions decided by the context versus by the states (shown: states return the next
state); table-driven machines (`Map<State, Map<Event, State>>`) when transitions are data.

### Pros and cons

| Pros | Cons |
| --- | --- |
| Each state's rules live in one place | More classes than a `switch` for small machines |
| Illegal transitions are impossible or explicit | Transitions spread over state classes can be hard to see as a whole |
| Adding a state does not touch other states much | Persisting state requires mapping objects to a column |

### Pitfalls

Leaving the context in an inconsistent state if a transition fails midway; persisting the machine
without optimistic locking (two workers both move `PAID` to `SHIPPED`); duplicating guard logic in the
context and the states.

### Related patterns

Same shape as **Strategy** (see the comparison below). State objects are often **Flyweights** or
**Singletons**. Sealed interfaces plus pattern matching offer a data-oriented alternative.

## Strategy

**Intent.** Define a family of algorithms, encapsulate each one, and make them interchangeable. The
algorithm varies independently from clients that use it.

**Problem.** Checkout applies different discount rules: none for regular customers, 10% for gold,
30% for employees, and seasonal promotions like "flat 200 off above 2000". Business adds new rules
monthly. An `if/else` chain inside `Checkout` grows forever and every change risks breaking the others.

### Structure

```text
┌─────────────────────────────┐   uses   ┌─────────────────────────────┐
│ Context (Checkout)          │─────────►│ «interface» DiscountStrategy│
├─────────────────────────────┤          ├─────────────────────────────┤
│ - strategy                  │          │ + apply(subtotal)           │
│ + total(cart)               │          └──────────────▲──────────────┘
└─────────────────────────────┘             ┌───────────┼─────────────┐
                                     ┌──────┴─────┐ ┌───┴────────┐ ┌──┴──────────┐
                                     │ NoDiscount │ │ PercentOff │ │ FlatAbove   │
                                     └────────────┘ └────────────┘ └─────────────┘
```

### Example

```java
import java.math.BigDecimal;
import java.math.RoundingMode;
import java.util.Map;

public class StrategyDemo {

    @FunctionalInterface
    interface DiscountStrategy {
        BigDecimal apply(BigDecimal subtotal);

        static DiscountStrategy none() { return s -> s; }
    }

    record PercentOff(int percent) implements DiscountStrategy {
        public BigDecimal apply(BigDecimal s) {
            return s.multiply(BigDecimal.valueOf(100 - percent)).divide(BigDecimal.valueOf(100), 2, RoundingMode.HALF_UP);
        }
    }

    record FlatAbove(BigDecimal threshold, BigDecimal off) implements DiscountStrategy {
        public BigDecimal apply(BigDecimal s) { return s.compareTo(threshold) >= 0 ? s.subtract(off) : s; }
    }

    enum Tier { REGULAR, GOLD, EMPLOYEE }

    // Strategy selection is data, not an if/else chain.
    static final Map<Tier, DiscountStrategy> BY_TIER = Map.of(
            Tier.REGULAR, DiscountStrategy.none(),
            Tier.GOLD, new PercentOff(10),
            Tier.EMPLOYEE, new PercentOff(30));

    static final class Checkout {
        private final DiscountStrategy discount;
        Checkout(DiscountStrategy discount) { this.discount = discount; }
        BigDecimal total(BigDecimal subtotal) { return discount.apply(subtotal); }
    }

    public static void main(String[] args) {
        var subtotal = new BigDecimal("2500.00");
        for (Tier t : Tier.values())
            System.out.println(t + ": " + new Checkout(BY_TIER.get(t)).total(subtotal));

        // Promotions compose: flat 200 off above 2000, then gold 10%.
        DiscountStrategy promo = new FlatAbove(new BigDecimal("2000"), new BigDecimal("200"));
        DiscountStrategy gold = BY_TIER.get(Tier.GOLD);
        System.out.println("promo+gold: " + new Checkout(s -> gold.apply(promo.apply(s))).total(subtotal));
    }
}
```

### Real-world uses

- `Comparator` passed to `List.sort`, `TreeMap`, `Collections.max`.
- `ThreadPoolExecutor`'s `RejectedExecutionHandler` with `AbortPolicy`, `CallerRunsPolicy`,
  `DiscardPolicy`, `DiscardOldestPolicy`.
- Spring Security's `PasswordEncoder` (`BCryptPasswordEncoder`, `Argon2PasswordEncoder`); Spring's
  `ResourceLoader` and `TaskExecutor` implementations; AWT `LayoutManager`.
- Jackson's `PropertyNamingStrategy` (snake case versus camel case).

### Variations

Interface plus classes; lambdas and method references for stateless strategies; strategy registry
(`Map<Key, Strategy>`) or DI collection injection (`List<DiscountStrategy>` in Spring, each with a
`supports()` method); default strategies as static factory methods on the interface.

### Pros and cons

| Pros | Cons |
| --- | --- |
| Open/closed: add algorithms without touching the context | Clients must know which strategy to choose |
| Removes conditionals; each algorithm testable alone | More objects for trivially different behaviour |
| Swap at runtime (A/B tests, config, per tenant) | Strategy interface must fit all algorithms' data needs |

### Pitfalls

Strategies that need wildly different inputs (the interface turns into a grab bag of parameters);
selecting strategies with an `if/else` chain anyway; stateful strategies shared across threads.

### Related patterns

**State** has the same shape but different intent (compared below). **Template Method** varies steps
via inheritance; Strategy varies the whole algorithm via composition. **Decorator** changes behaviour
by wrapping; Strategy by replacing the internals. **Bridge** has the same structure applied to a whole
abstraction.

## Template Method

**Intent.** Define the skeleton of an algorithm in an operation, deferring some steps to subclasses.
Subclasses redefine certain steps without changing the algorithm's structure.

**Problem.** The company imports customers, products and invoices from CSV files. Every importer does
the same thing: skip comments and blanks, parse each line, validate, save, count successes and
failures, report. Only parsing, validation and saving differ. Copy-pasting the loop three times means
three subtly different error-handling policies.

### Structure

```text
┌──────────────────────────────────────┐
│ «abstract» DataImporter<T>           │
├──────────────────────────────────────┤
│ + importFrom(lines)   final template │──► calls skip, parse, validate, save
│ # parse(line) : T     abstract       │
│ # save(record)        abstract       │
│ # validate(record)    hook (no-op)   │
│ # skip(line)          hook (default) │
└──────────────────▲───────────────────┘
          ┌────────┴──────────────┐
┌─────────┴──────────┐  ┌─────────┴──────────┐
│ CustomerImporter   │  │ ProductImporter    │
│ parse, save,       │  │ parse, save        │
│ validate           │  │                    │
└────────────────────┘  └────────────────────┘
```

### Example

```java
import java.util.*;

public class TemplateMethodDemo {

    record ImportResult(int imported, int rejected) {}

    abstract static class DataImporter<T> {

        // The template method: final so subclasses cannot change the algorithm.
        public final ImportResult importFrom(List<String> lines) {
            int ok = 0, bad = 0;
            for (String line : lines) {
                if (skip(line)) continue;
                try {
                    T record = parse(line);
                    validate(record);
                    save(record);
                    ok++;
                } catch (IllegalArgumentException e) {
                    bad++;
                    onReject(line, e);
                }
            }
            return new ImportResult(ok, bad);
        }

        protected abstract T parse(String line);            // required steps
        protected abstract void save(T record);

        protected void validate(T record) {}                // hooks with defaults
        protected boolean skip(String line) { return line.isBlank() || line.startsWith("#"); }
        protected void onReject(String line, Exception e) { System.out.println("rejected '" + line + "': " + e.getMessage()); }
    }

    record Customer(String id, String email) {}

    static final class CustomerImporter extends DataImporter<Customer> {
        final List<Customer> saved = new ArrayList<>();

        protected Customer parse(String line) {
            String[] parts = line.split(",");
            if (parts.length != 2) throw new IllegalArgumentException("expected 2 columns");
            return new Customer(parts[0].trim(), parts[1].trim());
        }
        @Override protected void validate(Customer c) {
            if (!c.email().contains("@")) throw new IllegalArgumentException("bad email");
        }
        protected void save(Customer c) { saved.add(c); }
    }

    public static void main(String[] args) {
        var importer = new CustomerImporter();
        ImportResult result = importer.importFrom(List.of(
                "# id,email",
                "c1, asha@example.com",
                "c2, not-an-email",
                "",
                "c3, ravi@example.com, extra"));
        System.out.println(result + " saved=" + importer.saved);
    }
}
```

### Real-world uses

- `java.util.AbstractList`: implement `get` and `size`, inherit `indexOf`, `iterator`, `equals` and
  more. Same for `AbstractMap`, `AbstractSet`.
- `java.io.InputStream.read(byte[], int, int)` is implemented in terms of the abstract single-byte
  `read()`.
- `HttpServlet.service()` dispatches to `doGet`, `doPost` and friends.
- `AbstractQueuedSynchronizer`: subclasses implement `tryAcquire` / `tryRelease`; AQS supplies queuing
  and blocking (`ReentrantLock`, `CountDownLatch`, `Semaphore` are built this way).
- Spring's `JdbcTemplate`, `TransactionTemplate` and `RestTemplate` use the **template-callback**
  variant: the fixed algorithm lives in the template, and the varying step is passed in as a callback
  (`RowMapper`, `TransactionCallback`) instead of being overridden.

### Variations

Classic inheritance-based template; **template-callback** (composition with lambdas); hooks (optional
overrides with defaults) versus abstract steps (mandatory).

### Pros and cons

| Pros | Cons |
| --- | --- |
| Removes duplication of the invariant algorithm | Inheritance couples subclasses tightly to the base |
| Base class controls the order and policy (Hollywood principle) | Single inheritance: one template per class |
| Hooks give controlled extension points | Fragile base class: base changes ripple into subclasses |

### Pitfalls

Non-final template methods (subclasses override and break the invariant); calling overridable
methods from a constructor; too many hooks, so the flow becomes impossible to follow.

### Related patterns

**Strategy** replaces the whole algorithm by composition; Template Method replaces parts by
inheritance. **Factory Method** is often a step of a template method.

## Visitor

**Intent.** Represent an operation to be performed on the elements of an object structure. Visitor
lets you define a new operation without changing the classes of the elements on which it operates.

**Problem.** A drawing tool has a stable set of shapes (circle, rectangle, triangle). Features keep
being added: area for pricing, SVG export, JSON export, bounding boxes. Adding a method to every
shape class for every feature scatters each feature over many files and bloats the shapes with
unrelated responsibilities.

### Structure

```text
┌──────────────────────────────┐        ┌──────────────────────────────┐
│ «interface» Shape            │        │ «interface» ShapeVisitor<R>  │
├──────────────────────────────┤        ├──────────────────────────────┤
│ + accept(visitor) : R        │───────►│ + visitCircle(c) : R         │
└──────────────▲───────────────┘        │ + visitRect(r)   : R         │
       ┌───────┴───────┐                │ + visitTriangle(t) : R       │
   ┌───┴────┐     ┌────┴───┐            └──────────────▲───────────────┘
   │ Circle │     │ Rect   │ ...                ┌──────┴──────┐
   └────────┘     └────────┘                ┌───┴────┐   ┌────┴───┐
  accept(v) { return v.visitCircle(this); } │ Area   │   │ Svg    │
                                            └────────┘   └────────┘
  Double dispatch: the call depends on BOTH the shape's type and the visitor's type.
```

### Example

```java
import java.util.List;

public class VisitorDemo {

    interface ShapeVisitor<R> {
        R visitCircle(Circle c);
        R visitRect(Rect r);
        R visitTriangle(Triangle t);
    }

    interface Shape { <R> R accept(ShapeVisitor<R> visitor); }

    record Circle(double radius) implements Shape {
        public <R> R accept(ShapeVisitor<R> v) { return v.visitCircle(this); }
    }
    record Rect(double width, double height) implements Shape {
        public <R> R accept(ShapeVisitor<R> v) { return v.visitRect(this); }
    }
    record Triangle(double base, double height) implements Shape {
        public <R> R accept(ShapeVisitor<R> v) { return v.visitTriangle(this); }
    }

    // New operation #1
    static final class AreaVisitor implements ShapeVisitor<Double> {
        public Double visitCircle(Circle c) { return Math.PI * c.radius() * c.radius(); }
        public Double visitRect(Rect r) { return r.width() * r.height(); }
        public Double visitTriangle(Triangle t) { return 0.5 * t.base() * t.height(); }
    }

    // New operation #2, without touching any shape class
    static final class SvgVisitor implements ShapeVisitor<String> {
        public String visitCircle(Circle c) { return "<circle r=\"" + c.radius() + "\"/>"; }
        public String visitRect(Rect r) { return "<rect width=\"" + r.width() + "\" height=\"" + r.height() + "\"/>"; }
        public String visitTriangle(Triangle t) {
            return "<polygon points=\"0,0 " + t.base() + ",0 " + t.base() / 2 + "," + t.height() + "\"/>";
        }
    }

    public static void main(String[] args) {
        List<Shape> shapes = List.of(new Circle(1), new Rect(2, 3), new Triangle(4, 5));
        var area = new AreaVisitor();
        var svg = new SvgVisitor();
        double total = 0;
        for (Shape s : shapes) {
            total += s.accept(area);
            System.out.println(s.accept(svg));
        }
        System.out.printf("total area = %.2f%n", total);
    }
}
```

### Real-world uses

- `javax.lang.model.element.ElementVisitor` and `javax.lang.model.type.TypeVisitor` in annotation
  processing.
- `java.nio.file.FileVisitor` / `SimpleFileVisitor` with `Files.walkFileTree` (callbacks per file and
  directory; a looser variant without double dispatch over many element types).
- ASM's `ClassVisitor` / `MethodVisitor` for bytecode, and compiler ASTs in javac and Eclipse JDT.

### Variations

Visitors returning values (generic `R`, shown) versus accumulating state; acyclic visitor (avoids the
dependency cycle between visitor and element hierarchies); reflective visitors (slow, loses type
safety); and in modern Java, **pattern matching over sealed types** (next section).

### Pros and cons

| Pros | Cons |
| --- | --- |
| New operations without changing element classes | Adding a new element type breaks every visitor |
| Related behaviour grouped in one class | Elements must expose enough state for visitors |
| Visitors can accumulate state across a traversal | Boilerplate `accept` methods; double dispatch confuses newcomers |

### Pitfalls

Using Visitor when element types change frequently (every new type touches all visitors); forgetting
that `accept` must be implemented in every concrete class (copy-paste errors call the wrong `visitX`).

### Visitor vs sealed interfaces and pattern matching (Java 21)

Visitor exists because older Java could only dispatch on the receiver's runtime type (single dispatch).
To pick code based on the element's type without `instanceof` chains, it bounces through `accept`.
Java 21 (JEP 441 pattern matching for `switch`, JEP 440 record patterns, and sealed classes from Java
17) gives the same safety directly:

```java
public class SealedShapesDemo {

    sealed interface Shape permits Circle, Rect, Triangle {}
    record Circle(double radius) implements Shape {}
    record Rect(double width, double height) implements Shape {}
    record Triangle(double base, double height) implements Shape {}

    static double area(Shape s) {
        return switch (s) {                                 // exhaustive: no default needed
            case Circle c -> Math.PI * c.radius() * c.radius();
            case Rect(double w, double h) -> w * h;           // record pattern deconstructs
            case Triangle(double b, double h) -> 0.5 * b * h;
        };
    }

    static String describe(Shape s) {
        return switch (s) {
            case Circle c when c.radius() == 0 -> "degenerate circle";   // guard
            case Circle c -> "circle r=" + c.radius();
            case Rect(double w, double h) when w == h -> "square " + w;
            case Rect r -> "rect " + r.width() + "x" + r.height();
            case Triangle t -> "triangle";
        };
    }

    public static void main(String[] args) {
        for (Shape s : new Shape[] {new Circle(1), new Rect(2, 2), new Triangle(3, 4)})
            System.out.printf("%s area=%.2f%n", describe(s), area(s));
    }
}
```

| | Classic Visitor | Sealed types + pattern matching |
| --- | --- | --- |
| Add a new operation | New visitor class | New method with a `switch` |
| Add a new element type | Edit every visitor interface and implementation | Add to `permits`; compiler flags every non-exhaustive `switch` |
| Boilerplate | `accept` in every class, visitor interface | None beyond the sealed hierarchy |
| Exhaustiveness | Enforced by the visitor interface | Enforced by the compiler for sealed types (if no `default`) |
| Works across modules / open hierarchies | Yes | Sealed subtypes must be in the same module (or package if unnamed module) |
| Traversal state | Natural in visitor fields | Pass accumulators explicitly or use recursion |

Both solve the **expression problem** in the same direction: easy to add operations, harder to add
types. For closed, data-like hierarchies (ASTs, events, results, commands) in Java 21+, prefer sealed
types and `switch`. Keep classic Visitor for open hierarchies, for libraries that must support older
Java, or when traversal logic (visiting children in a specific order) is the real value, as in ASM and
the `javax.lang.model` API.

### Related patterns

Visitors traverse **Composites** and **Interpreter** ASTs. **Iterator** visits elements uniformly;
Visitor dispatches on element type. **Strategy** varies one algorithm for a context; Visitor varies an
operation across a type hierarchy.

---

# Comparisons and modern Java

## Strategy vs State vs Template Method

Strategy and State have identical class diagrams (a context delegating to an interface). Template
Method solves a similar "vary part of the behaviour" problem with inheritance instead of composition.

| | Strategy | State | Template Method |
| --- | --- | --- | --- |
| Mechanism | Composition: context holds an algorithm object | Composition: context holds a state object | Inheritance: subclass overrides steps |
| Who chooses the variant | The client or configuration, usually once | The states themselves, as transitions happen | The programmer, at compile time, by choosing the subclass |
| Do variants know each other | No, strategies are independent | Yes, states return or set the next state | No, subclasses only know the base class |
| Changes during object lifetime | Rarely | Constantly; that is the point | Never (fixed by class) |
| Granularity | Whole algorithm | All state-dependent behaviour | Individual steps of a fixed algorithm |
| Typical example | `Comparator`, discount rules | Order lifecycle, TCP connection | `AbstractList`, `HttpServlet` |
| Lambda-friendly | Very (functional interface) | Somewhat (enum or records) | Via template-callback |

Rule of thumb: if the client picks it, it is a **Strategy**; if the object moves itself between
variants based on events, it is **State**; if you are filling in blanks of an algorithm owned by a base
class, it is **Template Method**.

## How lambdas simplify Strategy, Command, Template Method and Observer

Since Java 8, any single-method interface can be implemented by a lambda or method reference. Four
patterns shrink dramatically.

**Strategy** becomes a function value:

```java
Comparator<Employee> bySalaryThenName =
        Comparator.comparing(Employee::salary).reversed().thenComparing(Employee::name);
employees.sort(bySalaryThenName);
```

**Command** becomes a `Runnable` / `Callable` or a list of them:

```java
Deque<Runnable> undoStack = new ArrayDeque<>();
document.insert(0, "Hi");
undoStack.push(() -> document.delete(0, 2));   // the undo command captures what it needs
undoStack.pop().run();
```

**Template Method** becomes template-callback: the fixed algorithm is a method, the varying step a
parameter, so no subclass is required:

```java
static <T> List<T> query(DataSource ds, String sql, RowMapper<T> mapper) throws SQLException {
    try (Connection c = ds.getConnection();                 // invariant: acquire
         PreparedStatement ps = c.prepareStatement(sql);
         ResultSet rs = ps.executeQuery()) {
        List<T> out = new ArrayList<>();
        while (rs.next()) out.add(mapper.map(rs));          // variant step supplied by caller
        return out;
    }                                                       // invariant: always release
}

@FunctionalInterface interface RowMapper<T> { T map(ResultSet rs) throws SQLException; }

List<String> names = query(ds, "select name from users", rs -> rs.getString("name"));
```

This is exactly how Spring's `JdbcTemplate.query(sql, RowMapper)` works.

**Observer** becomes a list of `Consumer<Event>`:

```java
List<Consumer<OrderPlaced>> listeners = new CopyOnWriteArrayList<>();
listeners.add(e -> email.sendReceipt(e.orderId()));
listeners.add(metrics::recordOrder);
listeners.forEach(l -> l.accept(new OrderPlaced("ord_1")));
```

When do you still want full classes? When the behaviour has **state or several methods** (a command
with `execute` and `undo`; a strategy with `supports()` and `apply()`), when it needs a **name** for
logging and configuration, when it must be **serializable** (lambdas should not be serialized), or when
it is large enough that an anonymous function hurts readability and stack traces.

---

# Important non-GoF patterns

## Dependency Injection

Objects receive their collaborators from outside (constructor, setter or field) instead of creating
or looking them up. It is Inversion of Control applied to dependencies. Constructor injection makes
dependencies explicit, enables `final` fields and makes testing trivial.

```java
final class InvoiceService {
    private final InvoiceRepository repo;
    private final Clock clock;
    InvoiceService(InvoiceRepository repo, Clock clock) { this.repo = repo; this.clock = clock; }
}
// Production: Spring creates and wires beans. Test: new InvoiceService(fakeRepo, Clock.fixed(...)).
```

Spring, Jakarta CDI, Google Guice and Dagger are DI containers. DI replaces most uses of Singleton
and Service Locator, and the container itself acts as a giant Abstract Factory.

## Repository

A Repository mediates between the domain and data mapping, presenting a collection-like interface
for aggregates (Fowler, *Patterns of Enterprise Application Architecture*; Evans, *Domain-Driven
Design*). The domain asks for `findById`, `save`, not SQL.

```java
interface OrderRepository {
    Optional<Order> findById(OrderId id);
    List<Order> findByCustomer(CustomerId customer);
    void save(Order order);
}
```

Spring Data generates implementations from interfaces such as
`interface OrderRepository extends JpaRepository<Order, Long>` and derived query methods
(`findByStatusAndCreatedAtAfter`). Pitfall: generic repositories that expose every query to everyone,
turning the repository into a leaky DAO.

## Null Object

Instead of returning `null` and forcing callers to check, return an object that implements the
interface with neutral behaviour.

```java
interface DiscountPolicy { BigDecimal apply(BigDecimal amount); }
enum NoDiscount implements DiscountPolicy {
    INSTANCE;
    public BigDecimal apply(BigDecimal amount) { return amount; }
}
```

JDK examples: `Collections.emptyList()`, `Map.of()`, `InputStream.nullInputStream()` and
`OutputStream.nullOutputStream()` (Java 11). Return empty collections, never `null` (*Effective
Java* Item 54). `Optional` is a related but different tool: it makes absence explicit rather than
hiding it. Pitfall: a Null Object that hides real errors (a silent no-op payment gateway in production).

## Object Pool

Keep a set of initialized, expensive, **mutable** objects and lend them out exclusively, one borrower at
a time, returning them to the pool afterwards. Worth it when creation is genuinely expensive
(connections, threads, large native buffers).

- **HikariCP** pools JDBC connections (TCP plus TLS plus authentication is milliseconds each).
- `ThreadPoolExecutor` pools threads.
- Apache Commons Pool is a general-purpose implementation.

Pitfalls: leaking borrowed objects (always return in `finally` or try-with-resources), returning
objects in a dirty state (open transactions, thread-locals), and pooling cheap objects; modern GCs
make pooling small plain objects counterproductive. Pools should be sized from measurements, not
guesses.

## Specification

Encapsulate a business rule as a combinable predicate object (Evans and Fowler). Rules can be
reused for validation, selection and query building.

```java
@FunctionalInterface
interface Spec<T> {
    boolean isSatisfiedBy(T t);
    default Spec<T> and(Spec<T> o) { return t -> isSatisfiedBy(t) && o.isSatisfiedBy(t); }
    default Spec<T> or(Spec<T> o)  { return t -> isSatisfiedBy(t) || o.isSatisfiedBy(t); }
    default Spec<T> not()          { return t -> !isSatisfiedBy(t); }
}

Spec<Customer> premium = c -> c.lifetimeSpend().compareTo(new BigDecimal("100000")) > 0;
Spec<Customer> active  = c -> c.lastOrder().isAfter(LocalDate.now().minusDays(90));
Spec<Customer> target  = premium.and(active);
```

Spring Data JPA's `Specification<T>` (with `toPredicate(root, query, criteriaBuilder)`) translates
specs into SQL `WHERE` clauses; `java.util.function.Predicate` is the in-memory equivalent. A
Specification is a small **Interpreter** over business rules.

---

# Anti-patterns

**God object.** One class (often `OrderManager`, `Utils`, `ApplicationService`) that knows and does
everything: thousands of lines, dozens of dependencies, every change touches it. Facades and
Mediators decay into god objects when business logic accumulates in them. Fix: split by
responsibility and by domain concept, push behaviour into the objects that own the data.

**Overuse of patterns ("patternitis").** `AbstractSingletonProxyFactoryBean`-style layering for a
problem that needed one class. Symptoms: interfaces with exactly one implementation and no test
double, factories that create one class, strategies with one strategy, three layers of indirection to
read a config value. Patterns cost indirection; they should be introduced when the variation they
handle actually exists (or is very likely). YAGNI applies.

**Singleton abuse.** Using Singleton as a socially acceptable global variable: `UserContext.getInstance()`,
`Database.getInstance()` called from deep inside business logic. Leads to hidden coupling, test
pollution, concurrency bugs and impossible multi-tenancy. Fix: inject dependencies; let the container
manage single instances.

**Service Locator.** A registry from which code pulls dependencies (`ServiceLocator.get(PaymentService.class)`).
It decouples from concrete classes, but dependencies become invisible in the API, failures move from
startup to runtime ("no service registered" in production), and tests must configure a global
registry. Mark Seemann's *Dependency Injection Principles, Practices, and Patterns* describes it as an
anti-pattern. Prefer constructor injection; if you must look up dynamically, inject a narrow factory
(`Map<String, PaymentService>` or `ObjectProvider<T>` in Spring).

Other common ones: **anemic domain model** (entities with only getters and setters, all logic in
services), **golden hammer** (one favourite pattern applied everywhere), **lava flow** (dead
abstractions no one dares remove), and **poltergeist** classes that exist only to pass calls along.

---

# Which pattern for which problem

| Problem you are facing | Consider | Why |
| --- | --- | --- |
| Constructor with many optional parameters | Builder (or a record if few) | Named steps, defaults, validation, immutability |
| Caller should not know which concrete class to create | Factory Method / static factory | Hides the class, can cache or return subtypes |
| Need a consistent family of objects (per cloud, per theme) | Abstract Factory | Swap the whole family at once |
| Creating objects is expensive; many similar copies needed | Prototype | Copy a pre-built instance |
| Exactly one instance needed | DI singleton scope (or enum / holder) | Uniqueness without hidden globals |
| Third-party API has the wrong shape | Adapter | Translate at the boundary |
| Two dimensions of variation causing class explosion | Bridge | Two hierarchies joined by composition |
| Tree of items treated uniformly | Composite | Same interface for leaf and group |
| Add optional behaviours in combinations (cache, retry, log) | Decorator | Stackable wrappers |
| Complex subsystem, simple common use case | Facade | One entry point |
| Millions of similar objects exhausting memory | Flyweight | Share intrinsic state |
| Lazy loading, access control, remote calls, AOP | Proxy | Intercept with same interface |
| Pipeline of checks that may short-circuit | Chain of Responsibility | Configurable ordered handlers |
| Undo/redo, queue, retry, audit actions | Command | Requests as objects |
| User-editable rules or a small DSL | Interpreter (or Specification) | Grammar as classes |
| Traverse data without exposing storage, lazily | Iterator | Uniform, lazy traversal |
| Many components with tangled interactions | Mediator | Centralize coordination |
| Snapshot and restore without exposing internals | Memento | Opaque saved state |
| Many parts react to one change | Observer (pub/sub across services) | Decoupled notification |
| Big `switch` on a status field in many methods | State | Behaviour per state object |
| Interchangeable algorithms chosen at runtime | Strategy | Composition, open/closed |
| Same algorithm skeleton, different steps | Template Method (or template-callback) | Invariant flow in one place |
| New operations over a stable type hierarchy | Sealed types + `switch`, or Visitor | Add operations without editing types |
| `null` checks everywhere for "no behaviour" | Null Object | Neutral default implementation |
| Expensive mutable resources (connections, threads) | Object Pool | Reuse instead of recreate |

---

## Common mistakes

- **Starting from patterns instead of problems.** Write the simple version first; refactor toward a
  pattern when duplication or rigidity actually appears.
- **Confusing structurally identical patterns** (Decorator vs Proxy, Strategy vs State vs Bridge). In
  interviews, explain the *intent*, not just the class diagram.
- **Ignoring modern Java.** Writing a full Strategy class hierarchy where a `Function` suffices, or a
  Visitor where a sealed interface plus `switch` is clearer.
- **Broken Singletons.** DCL without `volatile`, mutable state in singletons, singletons that make tests
  order-dependent.
- **Shallow copies** in Prototype and Memento.
- **Leaky Observers.** No unsubscribe path, unhandled listener exceptions, re-entrant notifications.
- **Proxy surprises.** Spring self-invocation bypassing `@Transactional`; final methods not proxied.
- **Factories and facades becoming god objects.**
- **Inheritance-heavy Template Methods** where composition (template-callback) would be more flexible.

## Interview questions

**1. What problem does double-checked locking solve, and why does it need `volatile`?**
It makes lazy singleton creation thread-safe while avoiding a lock on every call after
initialization. Without `volatile`, the write of the reference can be reordered before the
constructor's field writes, so another thread can see a non-null but partially constructed object.
`volatile` (Java 5+ memory model) establishes happens-before between the write and later reads and
prevents that reordering.

**2. Which Singleton implementation would you choose, and why?**
In application code, none: let the DI container hold one instance and inject it. If a real singleton
is required, an enum (serialization and reflection safe, per *Effective Java* Item 3), or the holder
idiom when it must be a class (lazy, thread-safe by class-initialization rules, no synchronization
cost).

**3. Decorator vs Proxy: same structure, what is the difference?**
Intent and control. Decorators add responsibilities and are stacked by the client in chosen
combinations. Proxies control access to the subject (lazy loading, security, remoting, transactions),
usually manage or hide the real object, and are typically inserted transparently by infrastructure
(Spring AOP, Hibernate).

**4. Strategy vs State?**
Same shape, different intent. A strategy is chosen by the client and strategies are independent; a
state object represents the context's current condition, and states trigger transitions to each other
as events happen. If behaviour changes because the object's lifecycle advances, it is State.

**5. Factory Method vs Abstract Factory?**
Factory Method is one method, often overridden by subclasses, creating one product. Abstract Factory
is an object with several creation methods producing a family of related products that must be used
together. Abstract Factory makes adding families easy and adding product types hard.

**6. How do lambdas change classic patterns?**
Strategy, Command, Observer and the varying step of Template Method become functional interfaces
implemented by lambdas or method references, removing most boilerplate classes. Full classes remain
useful when behaviour has state, multiple methods (`execute` plus `undo`), needs a name, or must be
serialized.

**7. Is Visitor still relevant with sealed interfaces and pattern matching?**
For closed hierarchies in Java 21+, a `switch` over a sealed interface with record patterns gives
exhaustiveness checking with far less boilerplate, so it usually replaces Visitor. Visitor remains
useful for open hierarchies, pre-21 codebases and libraries, and when the visitor encapsulates
traversal order and state (ASM, annotation-processing APIs).

**8. Observer vs publish/subscribe?**
Observer is in-process: observers register directly with a subject, which calls them synchronously.
Pub/sub inserts a broker, so publishers and subscribers do not know each other; delivery is usually
asynchronous, may cross machines, and comes with durability, retries and ordering guarantees defined
by the broker.

**9. Where does Spring use design patterns?**
Proxy (AOP: `@Transactional`, `@Cacheable`), Template Method and template-callback (`JdbcTemplate`,
`TransactionTemplate`), Factory (`BeanFactory`, `FactoryBean`), Singleton scope, Observer
(`ApplicationEvent` and `@EventListener`), Chain of Responsibility (servlet filters, Spring Security
filter chain), Adapter (`HandlerAdapter`), Strategy (`PasswordEncoder`, `ResourceLoader`).

**10. Why is `@Transactional` on a method called from the same class ignored?**
Spring applies it through a proxy around the bean. A call on `this` goes directly to the target
object, not through the proxy, so no transaction interceptor runs. Fixes: move the method to another
bean, inject the bean's own proxy, or use `TransactionTemplate`.

**11. When would you use Flyweight, and what JDK feature shows it?**
When you have a very large number of objects whose state is mostly shared and immutable. Separate the
shared intrinsic part (one instance per distinct value, from a factory) from per-object extrinsic
state. `Integer.valueOf` caches -128 to 127, which is also why `==` on boxed integers is a classic bug.

**12. Why is Service Locator considered an anti-pattern?**
It hides dependencies inside method bodies instead of declaring them in the constructor, so you
cannot tell what a class needs by looking at its API, missing registrations fail at runtime instead
of at startup, and tests must manipulate a global registry. Constructor injection solves the same
decoupling problem explicitly.

**13. How would you implement undo in an editor?**
Command pattern: each user action is a command object with `execute` and `undo`, pushed onto an
undo stack by an invoker; undo pops and reverses, pushing to a redo stack, which is cleared on a new
command. For operations that are hard to reverse, capture a Memento before executing. Group related
commands into a macro command.

**14. Adapter vs Facade?**
An Adapter makes one existing class conform to an interface that clients already expect; the
interface is dictated by the client. A Facade designs a new, simpler interface over many classes; the
interface is dictated by the use case. Adapter is about compatibility, Facade about simplicity.

**15. What is the downside of Template Method compared with Strategy?**
It relies on inheritance: subclasses are tightly coupled to the base class, a class can extend only one
template, the variant is fixed at compile time, and changes to the base can break subclasses (fragile
base class). Strategy (or template-callback) uses composition, so variants can be swapped at runtime
and combined.

## Summary

| Pattern | One-line essence | Remember it by |
| --- | --- | --- |
| Singleton | One instance, global access | Prefer DI scope; enum or holder if needed |
| Factory Method | Subclass or method decides the concrete class | `Collection.iterator()` |
| Abstract Factory | Create consistent families | `DocumentBuilderFactory`, per-cloud factories |
| Builder | Step-by-step, readable, validated construction | `HttpRequest.newBuilder()` |
| Prototype | Create by copying | Copy constructors, not `clone()` |
| Adapter | Make one interface fit another | `InputStreamReader` |
| Bridge | Split abstraction from implementation | JDBC, SLF4J API vs binding |
| Composite | Treat leaf and tree uniformly | AWT `Container` |
| Decorator | Stack behaviours with the same interface | `java.io` streams |
| Facade | Simple front for a complex subsystem | SLF4J, `JdbcTemplate` |
| Flyweight | Share immutable intrinsic state | `Integer.valueOf` cache |
| Proxy | Control access with a stand-in | Spring AOP, Hibernate lazy loading |
| Chain of Responsibility | Pass along handlers until done | Servlet filters |
| Command | Request as an object: queue, log, undo | `Runnable`, undo stacks |
| Interpreter | Grammar as a class tree | Regex, SpEL |
| Iterator | Traverse without exposing storage | `Iterator`, `Spliterator` |
| Mediator | Centralize interactions between peers | Dialogs, `DispatcherServlet` |
| Memento | Opaque snapshot for restore | `Savepoint` |
| Observer | Notify dependents of changes | Listeners, Spring events |
| State | Behaviour changes with internal state | Order lifecycle |
| Strategy | Swappable algorithm | `Comparator` |
| Template Method | Fixed skeleton, variable steps | `AbstractList`, `HttpServlet` |
| Visitor | New operations over a type hierarchy | Sealed types + `switch` in Java 21 |

Key takeaways:

- Learn patterns by **intent**; many share a structure.
- Composition over inheritance: most patterns are ways of plugging behaviour objects together.
- Modern Java absorbs several patterns: lambdas (Strategy, Command, Observer, template-callback),
  records (Builder for small objects, Memento, Command), sealed types plus `switch` (Visitor,
  Interpreter evaluation), enums (Singleton, State, Strategy).
- Frameworks are pattern catalogues; recognizing Proxy, Template Method and Chain of Responsibility
  in Spring explains most of its "magic".
- Introduce a pattern when the problem appears, not before.

## Further reading

- Erich Gamma, Richard Helm, Ralph Johnson, John Vlissides: *Design Patterns: Elements of Reusable
  Object-Oriented Software* (Addison-Wesley, 1994). The original catalogue.
- Eric Freeman and Elisabeth Robson: *Head First Design Patterns* (O'Reilly, 2nd edition 2020). The most
  approachable introduction, Java examples.
- Joshua Bloch: *Effective Java* (3rd edition, 2018). Especially Item 1 (static factory methods),
  Item 2 (builders), Item 3 (singleton with private constructor or enum), Item 5 (prefer dependency
  injection to hardwiring resources), Item 13 (override `clone` judiciously), Item 18 (favor
  composition over inheritance), Item 42-44 (lambdas and standard functional interfaces), Item 54
  (return empty collections, not null), Item 89 (`readResolve` for instance control).
- Martin Fowler: *Patterns of Enterprise Application Architecture* (2002). Repository, Unit of Work,
  Data Mapper, Service Layer.
- Eric Evans: *Domain-Driven Design* (2003). Repository, Specification, Aggregates.
- Mark Seemann and Steven van Deursen: *Dependency Injection Principles, Practices, and Patterns*
  (Manning, 2019). DI and why Service Locator is an anti-pattern.
- Brian Goetz et al.: *Java Concurrency in Practice* (2006). Safe publication, the holder idiom and
  why double-checked locking needs `volatile`.
- OpenJDK JEPs 409 (sealed classes), 440 (record patterns) and 441 (pattern matching for `switch`).
- Spring Framework reference documentation: AOP proxies, transaction management, application events.
