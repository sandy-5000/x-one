---
title: "Design patterns"
order: 2
---
# Design Patterns

Design patterns are named, reusable solutions to problems that keep coming up in object-oriented code. The classic catalogue is the "Gang of Four" (GoF) book, which groups 23 patterns into three families.

| Family | Concern | Patterns covered here |
| --- | --- | --- |
| Creational | How objects are created | Singleton, Factory Method, Abstract Factory, Builder, Prototype |
| Structural | How objects are composed | Adapter, Decorator, Facade, Proxy, Composite |
| Behavioral | How objects communicate | Strategy, Observer, Command, Template Method, State, Chain of Responsibility, Iterator |

All examples are in Java.

---

# Creational patterns

## Singleton

**Intent:** exactly one instance of a class, with a global access point.

**Use for:** configuration, logging, connection pools, caches.

```java
public final class Config {
    private Config() {}

    private static class Holder {
        static final Config INSTANCE = new Config();   // lazy, thread-safe
    }

    public static Config getInstance() {
        return Holder.INSTANCE;
    }
}

// Simplest thread-safe form, also safe against reflection and serialization:
public enum AppConfig {
    INSTANCE;
    public String get(String key) { return System.getenv(key); }
}
```

**Watch out:** singletons are hidden global state and make testing harder. Prefer injecting one shared instance via a constructor.

## Factory Method

**Intent:** let a method decide which concrete class to instantiate, so callers depend only on an interface.

```java
interface Notification { void send(String msg); }

class EmailNotification implements Notification {
    public void send(String msg) { System.out.println("Email: " + msg); }
}

class SmsNotification implements Notification {
    public void send(String msg) { System.out.println("SMS: " + msg); }
}

class NotificationFactory {
    static Notification create(String channel) {
        return switch (channel) {
            case "email" -> new EmailNotification();
            case "sms"   -> new SmsNotification();
            default -> throw new IllegalArgumentException(channel);
        };
    }
}

NotificationFactory.create("sms").send("Your OTP is 4821");
```

JDK examples: `List.of(...)`, `Calendar.getInstance()`, `NumberFormat.getInstance()`.

## Abstract Factory

**Intent:** create *families* of related objects that must be used together.

```java
interface Button   { void render(); }
interface Checkbox { void render(); }

interface UiFactory {
    Button createButton();
    Checkbox createCheckbox();
}

class DarkFactory implements UiFactory {
    public Button createButton()     { return () -> System.out.println("dark button"); }
    public Checkbox createCheckbox() { return () -> System.out.println("dark checkbox"); }
}

class LightFactory implements UiFactory {
    public Button createButton()     { return () -> System.out.println("light button"); }
    public Checkbox createCheckbox() { return () -> System.out.println("light checkbox"); }
}

void drawForm(UiFactory ui) {
    ui.createButton().render();
    ui.createCheckbox().render();   // always matches the button's theme
}
```

## Builder

**Intent:** construct a complex object step by step; avoid constructors with many parameters.

```java
public class HttpRequest {
    private final String url;
    private final String method;
    private final Map<String, String> headers;
    private final int timeoutMs;

    private HttpRequest(Builder b) {
        url = b.url;
        method = b.method;
        headers = Map.copyOf(b.headers);
        timeoutMs = b.timeoutMs;
    }

    public static Builder builder(String url) { return new Builder(url); }

    public static class Builder {
        private final String url;
        private String method = "GET";
        private final Map<String, String> headers = new HashMap<>();
        private int timeoutMs = 5000;

        private Builder(String url) { this.url = url; }

        public Builder method(String m)              { method = m; return this; }
        public Builder header(String k, String v)    { headers.put(k, v); return this; }
        public Builder timeout(int ms)               { timeoutMs = ms; return this; }
        public HttpRequest build()                   { return new HttpRequest(this); }
    }
}

HttpRequest req = HttpRequest.builder("https://api.example.com/users")
        .method("POST")
        .header("Content-Type", "application/json")
        .timeout(2000)
        .build();
```

JDK examples: `StringBuilder`, `java.net.http.HttpRequest.newBuilder()`, `Stream.builder()`.

## Prototype

**Intent:** create new objects by copying an existing one instead of building from scratch.

```java
class Shape implements Cloneable {
    String color;
    int x, y;

    Shape(String color) { this.color = color; }

    Shape copy() {   // a copy constructor or method is clearer than Object.clone()
        Shape s = new Shape(color);
        s.x = x;
        s.y = y;
        return s;
    }
}

Shape template = new Shape("red");
Shape a = template.copy();
Shape b = template.copy();
```

Useful when construction is expensive (parsing, network calls) and you need many similar objects.

---

# Structural patterns

## Adapter

**Intent:** make an existing class work with an interface it was not written for.

```java
interface PaymentProcessor {          // what our code expects
    void pay(double amountInRupees);
}

class LegacyStripeClient {            // third-party API we cannot change
    void makeCharge(long amountInPaise) { System.out.println("charged " + amountInPaise); }
}

class StripeAdapter implements PaymentProcessor {
    private final LegacyStripeClient client;

    StripeAdapter(LegacyStripeClient client) { this.client = client; }

    public void pay(double amountInRupees) {
        client.makeCharge(Math.round(amountInRupees * 100));
    }
}

PaymentProcessor p = new StripeAdapter(new LegacyStripeClient());
p.pay(499.99);
```

JDK examples: `Arrays.asList(array)`, `InputStreamReader` (byte stream to char reader).

## Decorator

**Intent:** add behaviour to an object at runtime by wrapping it, without subclassing.

```java
interface Coffee {
    double cost();
    String description();
}

class Espresso implements Coffee {
    public double cost() { return 100; }
    public String description() { return "Espresso"; }
}

abstract class CoffeeDecorator implements Coffee {
    protected final Coffee inner;
    CoffeeDecorator(Coffee inner) { this.inner = inner; }
}

class Milk extends CoffeeDecorator {
    Milk(Coffee c) { super(c); }
    public double cost() { return inner.cost() + 20; }
    public String description() { return inner.description() + " + milk"; }
}

class Caramel extends CoffeeDecorator {
    Caramel(Coffee c) { super(c); }
    public double cost() { return inner.cost() + 30; }
    public String description() { return inner.description() + " + caramel"; }
}

Coffee order = new Caramel(new Milk(new Espresso()));
System.out.println(order.description() + " = " + order.cost());   // Espresso + milk + caramel = 150.0
```

JDK example: `new BufferedReader(new InputStreamReader(new FileInputStream(f)))`.

## Facade

**Intent:** provide one simple interface in front of a complicated subsystem.

```java
class Inventory  { boolean reserve(String sku) { return true; } }
class Payments   { boolean charge(String user, double amt) { return true; } }
class Shipping   { void schedule(String user, String sku) {} }
class Emails     { void confirm(String user) {} }

class OrderFacade {
    private final Inventory inventory = new Inventory();
    private final Payments payments = new Payments();
    private final Shipping shipping = new Shipping();
    private final Emails emails = new Emails();

    boolean placeOrder(String user, String sku, double price) {
        if (!inventory.reserve(sku)) return false;
        if (!payments.charge(user, price)) return false;
        shipping.schedule(user, sku);
        emails.confirm(user);
        return true;
    }
}
```

The controller calls one method instead of orchestrating four services.

## Proxy

**Intent:** a stand-in object that controls access to the real one — for lazy loading, caching, access control, logging, or remote calls.

```java
interface Image { void display(); }

class RealImage implements Image {
    private final String file;
    RealImage(String file) {
        this.file = file;
        System.out.println("Loading " + file);   // expensive
    }
    public void display() { System.out.println("Showing " + file); }
}

class LazyImage implements Image {
    private final String file;
    private RealImage real;

    LazyImage(String file) { this.file = file; }

    public void display() {
        if (real == null) real = new RealImage(file);   // load on first use
        real.display();
    }
}
```

Real-world: Hibernate lazy entities, Spring `@Transactional` proxies, RMI stubs.

**Decorator vs Proxy:** both wrap the same interface. A decorator *adds* behaviour and is usually stacked by the client; a proxy *controls access* and usually manages the real object's lifecycle itself.

## Composite

**Intent:** treat single objects and groups of objects the same way (tree structures).

```java
interface FileNode { long size(); }

record File(String name, long bytes) implements FileNode {
    public long size() { return bytes; }
}

class Folder implements FileNode {
    private final List<FileNode> children = new ArrayList<>();
    Folder add(FileNode n) { children.add(n); return this; }

    public long size() {
        return children.stream().mapToLong(FileNode::size).sum();
    }
}

FileNode root = new Folder()
        .add(new File("a.txt", 100))
        .add(new Folder().add(new File("b.png", 2048)));
System.out.println(root.size());   // 2148
```

---

# Behavioral patterns

## Strategy

**Intent:** define a family of interchangeable algorithms and choose one at runtime.

```java
interface ShippingStrategy { double cost(double weightKg); }

class StandardShipping implements ShippingStrategy {
    public double cost(double w) { return 40 + w * 10; }
}

class ExpressShipping implements ShippingStrategy {
    public double cost(double w) { return 100 + w * 25; }
}

class Checkout {
    private ShippingStrategy shipping;
    void setShipping(ShippingStrategy s) { shipping = s; }
    double total(double itemPrice, double weight) {
        return itemPrice + shipping.cost(weight);
    }
}

// With lambdas the strategy can be a one-liner:
checkout.setShipping(w -> 0);   // free shipping promo
```

JDK example: `Comparator` passed to `Collections.sort`.

## Observer

**Intent:** when one object changes state, all its subscribers are notified automatically.

```java
interface Listener { void onPrice(String symbol, double price); }

class StockTicker {
    private final List<Listener> listeners = new ArrayList<>();

    void subscribe(Listener l)   { listeners.add(l); }
    void unsubscribe(Listener l) { listeners.remove(l); }

    void publish(String symbol, double price) {
        for (Listener l : listeners) l.onPrice(symbol, price);
    }
}

StockTicker ticker = new StockTicker();
ticker.subscribe((s, p) -> System.out.println("Chart: " + s + " " + p));
ticker.subscribe((s, p) -> { if (p > 1000) System.out.println("Alert: " + s); });
ticker.publish("INFY", 1520.5);
```

The same idea at system scale is **pub/sub messaging** (Kafka, Redis Pub/Sub).

## Command

**Intent:** wrap a request as an object, so it can be queued, logged, retried, or undone.

```java
interface Command {
    void execute();
    void undo();
}

class TextEditor { StringBuilder text = new StringBuilder(); }

class AppendCommand implements Command {
    private final TextEditor editor;
    private final String added;

    AppendCommand(TextEditor e, String s) { editor = e; added = s; }

    public void execute() { editor.text.append(added); }
    public void undo() {
        int len = editor.text.length();
        editor.text.delete(len - added.length(), len);
    }
}

class History {
    private final Deque<Command> stack = new ArrayDeque<>();
    void run(Command c) { c.execute(); stack.push(c); }
    void undo()         { if (!stack.isEmpty()) stack.pop().undo(); }
}
```

JDK example: `Runnable` submitted to an `ExecutorService`.

## Template Method

**Intent:** fix the skeleton of an algorithm in a base class and let subclasses fill in specific steps.

```java
abstract class DataExporter {
    // the template: final so subclasses cannot reorder steps
    public final void export() {
        List<String> rows = fetch();
        String out = format(rows);
        write(out);
    }

    protected abstract List<String> fetch();
    protected abstract String format(List<String> rows);

    protected void write(String out) { System.out.println(out); }   // default hook
}

class CsvExporter extends DataExporter {
    protected List<String> fetch() { return List.of("a", "b"); }
    protected String format(List<String> rows) { return String.join(",", rows); }
}
```

Strategy uses composition (pass the algorithm in); Template Method uses inheritance (override steps).

## State

**Intent:** an object changes its behaviour when its internal state changes, without big `switch` statements.

```java
interface OrderState {
    OrderState next();
    String name();
}

class Placed implements OrderState {
    public OrderState next() { return new Shipped(); }
    public String name() { return "PLACED"; }
}

class Shipped implements OrderState {
    public OrderState next() { return new Delivered(); }
    public String name() { return "SHIPPED"; }
}

class Delivered implements OrderState {
    public OrderState next() { throw new IllegalStateException("already delivered"); }
    public String name() { return "DELIVERED"; }
}

class Order {
    private OrderState state = new Placed();
    void advance() { state = state.next(); }
    String status() { return state.name(); }
}
```

```text
PLACED ──next()──► SHIPPED ──next()──► DELIVERED
```

## Chain of Responsibility

**Intent:** pass a request along a chain of handlers until one handles it (or each adds its part).

```java
abstract class Handler {
    private Handler next;

    Handler linkWith(Handler next) { this.next = next; return next; }

    boolean handle(Request r) {
        return next == null || next.handle(r);
    }
}

class AuthHandler extends Handler {
    boolean handle(Request r) {
        if (r.token() == null) { System.out.println("401"); return false; }
        return super.handle(r);
    }
}

class RateLimitHandler extends Handler {
    boolean handle(Request r) {
        if (tooManyRequests(r)) { System.out.println("429"); return false; }
        return super.handle(r);
    }
}

Handler chain = new AuthHandler();
chain.linkWith(new RateLimitHandler()).linkWith(new LoggingHandler());
chain.handle(request);
```

Real-world: servlet filters, Express/Spring middleware, logging levels.

## Iterator

**Intent:** traverse a collection without exposing how it is stored.

```java
class Range implements Iterable<Integer> {
    private final int start, end;
    Range(int start, int end) { this.start = start; this.end = end; }

    public Iterator<Integer> iterator() {
        return new Iterator<>() {
            int cur = start;
            public boolean hasNext() { return cur < end; }
            public Integer next() { return cur++; }
        };
    }
}

for (int i : new Range(0, 5)) System.out.print(i);   // 01234
```

---

## Choosing a pattern

| Problem | Pattern |
| --- | --- |
| Too many constructor parameters | Builder |
| Need to pick an implementation at runtime | Factory Method / Strategy |
| Third-party API has the wrong shape | Adapter |
| Add features in combinations (logging, caching, compression) | Decorator |
| Complex subsystem, simple use case | Facade |
| Expensive object, load only when needed | Proxy |
| Tree of items treated uniformly | Composite |
| Many parts react to one event | Observer |
| Undo / queue / retry operations | Command |
| Big `switch` on a status field | State |
| Pipeline of checks (auth, validation, logging) | Chain of Responsibility |

Patterns are a vocabulary, not a goal. Start with the simplest code, and introduce a pattern when the problem it solves actually appears.
