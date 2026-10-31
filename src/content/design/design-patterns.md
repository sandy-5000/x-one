---
title: "Design patterns"
order: 2
---

# Design Patterns

Named, reusable **shapes of collaboration** between classes (who creates whom, who calls whom)—not copy-paste libraries. The GoF catalogue (1994) lists **23 patterns** in three families. Underneath almost all of them: **program to an interface**, and **favor composition over inheritance**.

```text
  A ──► B     A uses B          B ▲  C implements/extends B
  «interface»  + op() public     - field private
```

| Family | Question | Patterns |
| --- | --- | --- |
| Creational | Who builds which concrete object? | Singleton, Factory Method, Abstract Factory, Builder, Prototype |
| Structural | How are parts composed? | Adapter, Bridge, Composite, Decorator, Facade, Flyweight, Proxy |
| Behavioral | How do objects divide work and talk? | Chain of Responsibility, Command, Interpreter, Iterator, Mediator, Memento, Observer, State, Strategy, Template Method, Visitor |

## Key points

**Why patterns still matter**

- Shared vocabulary in design reviews (“facade over that subsystem”, “strategy for pricing”).
- Frameworks are built from them (Spring: Proxy, Template Method, Factory, Strategy, Observer, Chain of Responsibility).
- Even when a pattern collapses into language features (Strategy → lambdas, Iterator → enhanced for), the name tells you *what problem* the feature solves.

**Composition over inheritance**

- Inheritance fixes behaviour at compile time and creates deep, brittle trees.
- Composition assembles behaviour from collaborators you can swap at runtime (Decorator, Strategy, State).
- Prefer small interfaces + delegation; use inheritance for true “is-a” subtype contracts, not for code reuse alone.

**When NOT to use patterns**

- The problem is not recurring yet—YAGNI; one concrete class is fine.
- A simpler mechanism exists: plain functions, records, DI container, library API.
- The pattern adds indirection without a second variant or extension point (fake “strategy” with one implementation).
- Team cannot maintain the extra types—clarity beats pattern completeness.
- You are forcing a pattern because it is “best practice” rather than because coupling or variation hurts today.

---

## Creational

Separate **what you need** from **how it is instantiated**—`new Concrete()` is the tightest coupling in Java.

### Singleton

**Problem:** Exactly one instance must exist in a process (config registry, metrics hub).

**Solution:** Hide constructors; expose one controlled access path (`getInstance()` or enum singleton).

**When:**
- Truly one resource per JVM and global lookup is acceptable
- Lazy init with thread-safe creation is required

**Skip when:**
- You need test doubles or multiple configs per process
- DI can provide a single scoped bean instead

**Structure:**
```text
Client ──► Singleton.getInstance() ──► lone instance
```

**Example:**
```java
enum AppConfig {
  INSTANCE;
  private final Properties props = load();
  Properties get() { return props; }
}
// or: private static class Holder { static final X I = new X(); }
```

**In the wild:**
- JDBC `DriverManager` style registries; logging facades—often replaced by DI `@Singleton` scope.

### Factory Method

**Problem:** Client code should not depend on which concrete product class is constructed.

**Solution:** Subclass (or nested type) overrides a creation hook; client uses the abstract creator interface.

**When:**
- Product type varies by deployment, config, or platform
- Construction logic is non-trivial and belongs in one place

**Skip when:**
- Only one product will ever exist—call `new` or a simple static factory
- DI framework already chooses implementations

**Structure:**
```text
Client ──► Creator.createProduct() ──► Product ◄── ConcreteProduct
              ▲
         ConcreteCreator
```

**Example:**
```java
interface Document { void open(); }
abstract class Application {
  abstract Document createDocument();
  void newDoc() { Document d = createDocument(); d.open(); }
}
class TextApp extends Application {
  Document createDocument() { return new TextDoc(); }
}
```

**In the wild:**
- `Collection.iterator()` and framework creation hooks (`TemplateMethod` + factory method).

### Abstract Factory

**Problem:** Client must create **families** of related products without naming concrete classes.

**Solution:** Interface of factory methods—one factory per family (Win/Mac, SQL/NoSQL UI kit).

**When:**
- Multiple products must stay consistent (theme: button + scrollbar + dialog)
- Swapping entire platform at runtime or via config

**Skip when:**
- Only one product type, not a family—use Factory Method
- Products are unrelated—separate factories or DI modules

**Structure:**
```text
Client ──► AbstractFactory ──► AbstractProductA / AbstractProductB
                ▲
         ConcreteFactory1 / ConcreteFactory2
```

**Example:**
```java
interface UIFactory { Button button(); Checkbox box(); }
class WinFactory implements UIFactory {
  public Button button() { return new WinButton(); }
  public Checkbox box() { return new WinCheckbox(); }
}
// Client: UIFactory f = config.pick(); f.button().render();
```

**In the wild:**
- JDBC driver families; cross-platform UI themes.

### Builder

**Problem:** Object has many optional parts or invalid intermediate states if built in one constructor.

**Solution:** Step-by-step assembly via a builder; director (optional) defines build order; `build()` validates.

**When:**
- Telescoping constructors or huge parameter lists
- Immutable objects with many optional fields

**Skip when:**
- Few fields—constructor or factory is enough
- Records + compact canonical constructor suffice

**Structure:**
```text
Director ──► Builder.setA().setB() ──► build() ──► Product
```

**Example:**
```java
class HttpRequest {
  final String url; final Map<String,String> headers;
  private HttpRequest(Builder b) { url = b.url; headers = Map.copyOf(b.headers); }
  static class Builder {
    String url; Map<String,String> headers = new HashMap<>();
    Builder url(String u) { url = u; return this; }
    Builder header(String k, String v) { headers.put(k,v); return this; }
    HttpRequest build() { requireNonNull(url); return new HttpRequest(this); }
  }
}
```

**In the wild:**
- `StringBuilder`, protobuf/gRPC builders, test data builders (`UserBuilder` in tests).

### Prototype

**Problem:** Creating an object from scratch is expensive or complex; you need a copy with tweaks.

**Solution:** Clone an existing instance (prototype); registry maps keys to prototypes.

**When:**
- Deep graph copy cheaper than rebuilding from DB or parsing
- Runtime types decide which clone to use

**Skip when:**
- Copy semantics are unclear (shared mutable internals)—fix model first
- Serialization or copy constructors are simpler in your language

**Structure:**
```text
Client ──► prototype.clone() ──► new object (copy of state)
Registry: key ──► Prototype instance
```

**Example:**
```java
interface Shape extends Cloneable { Shape copy(); }
record Circle(int r, Point c) implements Shape {
  public Shape copy() { return new Circle(r, c); } // deep-copy c if needed
}
Map<String, Shape> registry = Map.of("dot", new Circle(1, origin));
Shape s = registry.get("dot").copy();
```

**In the wild:**
- Document/game prefab cloning; explicit `copy()` preferred over raw `clone()`.

---

## Structural

How types are **wrapped, grouped, or shared** without exploding coupling.

### Adapter

**Problem:** Existing class does not match the interface your client expects.

**Solution:** Wrapper implements target interface and delegates to adaptee (object adapter) or subclass adaptee (class adapter).

**When:**
- Integrating third-party or legacy APIs
- Incremental migration behind a clean port

**Skip when:**
- You own both sides—change the API instead
- Adapter becomes a god object—consider anti-corruption layer module

**Structure:**
```text
Client ──► Target ◄── Adapter ──► Adaptee (legacy)
```

**Example:**
```java
interface JsonPort { String toJson(Object o); }
class GsonAdapter implements JsonPort {
  private final Gson gson = new Gson();
  public String toJson(Object o) { return gson.toJson(o); }
}
```

**In the wild:**
- Spring `HandlerAdapter`, JPA attribute converters, Android `ListAdapter`.

### Bridge

**Problem:** Abstraction and implementation would explode into a Cartesian product of subclasses.

**Solution:** Split into two hierarchies; abstraction holds a reference to implementation interface.

**When:**
- Dimensions vary independently (UI control × rendering backend)
- Runtime binding of implementation

**Skip when:**
- Only one implementation forever—merge hierarchies
- A single dimension of variation—Strategy inside one class may suffice

**Structure:**
```text
Abstraction ──► Implementor ◄── ConcreteImplementorA/B
     ▲
RefinedAbstraction
```

**Example:**
```java
interface Renderer { void drawCircle(int x, int y, int r); }
class VectorRenderer implements Renderer { /* ... */ }
class Shape {
  protected Renderer r;
  Shape(Renderer r) { this.r = r; }
}
class Circle extends Shape {
  void draw() { r.drawCircle(cx, cy, radius); }
}
```

**In the wild:**
- JDBC `Connection` vs driver; remote vs local resource APIs.

### Composite

**Problem:** Client should treat individual objects and groups uniformly (tree structures).

**Solution:** Component interface; leaf and composite both implement it; composite holds children.

**When:**
- File systems, org charts, UI widget trees, scene graphs
- Recursive operations (size, render, validate) over whole subtree

**Skip when:**
- Flat lists only—no need for composite node type
- Type safety requires distinguishing leaf vs container in API

**Structure:**
```text
Client ──► Component ◄── Leaf
                ▲
            Composite (children: Component*)
```

**Example:**
```java
interface Node { int size(); }
record File(int bytes) implements Node { public int size() { return bytes; } }
class Folder implements Node {
  List<Node> kids = new ArrayList<>();
  public int size() { return kids.stream().mapToInt(Node::size).sum(); }
}
```

**In the wild:**
- Java AWT/Swing `Component`, XML DOM, filesystem APIs.

### Decorator

**Problem:** Add responsibilities to an object dynamically without subclassing every combination.

**Solution:** Wrapper implements same interface as wrapped object; forwards calls and adds behaviour before/after.

**When:**
- Cross-cutting enhancements: buffering, compression, logging, caching
- Stackable features (streams)

**Skip when:**
- One fixed wrapper—single delegating class is enough
- Order of wrappers matters but is undocumented—fragile

**Structure:**
```text
Client ──► Component ◄── ConcreteComponent
                ▲
         Decorator ──► wraps Component
```

**Example:**
```java
interface DataSource { void write(String s); String read(); }
class EncryptingSource implements DataSource {
  DataSource inner;
  public void write(String s) { inner.write(enc(s)); }
  public String read() { return dec(inner.read()); }
}
```

**In the wild:**
- `java.io` stream stacks; HTTP middleware.

### Facade

**Problem:** Subsystem has many classes and awkward entry points; clients couple to internals.

**Solution:** Thin facade class exposes a small, task-oriented API and coordinates subsystem objects.

**When:**
- Onboarding layer over legacy modules
- Simplifying framework bootstrapping for app code

**Skip when:**
- Facade hides operations callers need—leaky or incomplete API
- You are only renaming one class—not a real subsystem boundary

**Structure:**
```text
Client ──► Facade ──► SubsystemA, SubsystemB, SubsystemC
```

**Example:**
```java
class OrderFacade {
  private final Inventory inv; private final Payment pay; private final Shipping ship;
  void placeOrder(Order o) {
    inv.reserve(o); pay.charge(o); ship.dispatch(o);
  }
}
```

**In the wild:**
- SLF4J over logging backends; high-level REST clients over HTTP details.

### Flyweight

**Problem:** Huge numbers of similar objects waste memory on repeated intrinsic state.

**Solution:** Share immutable intrinsic state in a factory/cache; extrinsic state passed in by client.

**When:**
- Text glyphs, game tiles, map coordinates with shared texture data
- Object count is orders of magnitude larger than unique shared data

**Skip when:**
- Few instances—premature optimization
- Intrinsic state is not truly immutable or shareable

**Structure:**
```text
Client ──► FlyweightFactory ──► shared Flyweight
         passes extrinsic ctx at use time
```

**Example:**
```java
record GlyphKey(char c, String font) {}
class GlyphFactory {
  Map<GlyphKey, Glyph> cache = new ConcurrentHashMap<>();
  Glyph get(char c, String font) {
    return cache.computeIfAbsent(new GlyphKey(c, font), Glyph::new);
  }
}
// draw(glyph, x, y) — position is extrinsic
```

**In the wild:**
- String interning, integer caching (`Integer.valueOf`), browser DOM node pooling patterns.

### Proxy

**Problem:** Need to control access, lazy load, or remote call without changing the real subject’s interface.

**Solution:** Proxy implements same interface; adds logic then delegates to real subject (or creates it lazily).

**When:**
- Lazy initialization, access control, logging, remote stub
- AOP around a service interface

**Skip when:**
- Behaviour belongs in the real object—don’t split for show
- Dynamic proxies / framework AOP already cover the cross-cut

**Structure:**
```text
Client ──► Subject ◄── RealSubject
                ▲
              Proxy ──► RealSubject (optional lazy ref)
```

**Example:**
```java
interface Image { void display(); }
class ProxyImage implements Image {
  RealImage real;
  public void display() {
    if (real == null) real = new RealImage("big.png");
    real.display();
  }
}
```

**In the wild:**
- Spring AOP proxies; RMI/Hibernate lazy loading.

---

## Behavioral

How objects **assign responsibilities** and **communicate** without turning everything into a ball of mud.

### Chain of Responsibility

**Problem:** Request should be handled by one of several handlers; exact handler not known upfront.

**Solution:** Linked handlers; each tries to handle or forwards to `next`.

**When:**
- Middleware pipelines, servlet filters, approval workflows
- Multiple optional processing stages

**Skip when:**
- Fixed pipeline order with no early exit—plain list of functions
- Debugging the chain becomes opaque without good tracing

**Structure:**
```text
Client ──► Handler1 ──► Handler2 ──► Handler3 ──► ...
              │ handle or forward next
```

**Example:**
```java
abstract class AuthHandler {
  AuthHandler next;
  boolean handle(Request r) {
    if (canHandle(r)) return doHandle(r);
    return next != null && next.handle(r);
  }
}
```

**In the wild:**
- Servlet filter chains, Netty pipeline handlers, logging appenders.

### Command

**Problem:** Encapsulate a request as an object so you can queue, undo, log, or parameterize actions.

**Solution:** Command interface with `execute()` (and optional `undo()`); invoker holds command; receiver does work.

**When:**
- Undo/redo, job queues, macro recording, GUI actions
- Decouple UI from business operations

**Skip when:**
- Simple synchronous call—method reference is enough
- Undo semantics are not defined—don’t fake Command

**Structure:**
```text
Invoker ──► Command.execute() ──► Receiver
```

**Example:**
```java
interface Command { void execute(); }
record Transfer(Account from, Account to, int amt) implements Command {
  public void execute() { from.debit(amt); to.credit(amt); }
}
List<Command> queue = new ArrayList<>();
queue.add(new Transfer(a, b, 100));
queue.forEach(Command::execute);
```

**In the wild:**
- Event sourcing commands; `Runnable` as minimal command; transactional outbox jobs.

### Interpreter

**Problem:** Grammar is small and repeated; embed language evaluation in the app.

**Solution:** Expression hierarchy with `interpret(context)`; composite expressions build tree from grammar.

**When:**
- DSLs, rule engines, query filters with stable grammar
- Performance of interpretation is acceptable

**Skip when:**
- Grammar is large—use parser generator (ANTLR) not hand-rolled Interpreter
- Security: user-supplied language needs sandbox, not naive eval

**Structure:**
```text
Client ──► AbstractExpression.interpret(ctx)
                ▲
    Terminal / Nonterminal expressions
```

**Example:**
```java
interface Expr { boolean eval(Map<String,Boolean> vars); }
record Var(String name) implements Expr {
  public boolean eval(Map<String,Boolean> v) { return v.get(name); }
}
record And(Expr l, Expr r) implements Expr {
  public boolean eval(Map<String,Boolean> v) { return l.eval(v) && r.eval(v); }
}
```

**In the wild:**
- Regular expression engines (conceptually); SpEL/MEL in config tools; SQL WHERE clause ASTs.

### Iterator

**Problem:** Traverse a collection without exposing its internal representation.

**Solution:** Iterator object with `hasNext`/`next`; aggregate provides `iterator()`.

**When:**
- Multiple traversal strategies over same structure
- Hide index vs linked vs tree storage

**Skip when:**
- Language already gives enhanced for-each over your type
- Streams cover read-only traversal with lambdas

**Structure:**
```text
Client ──► Iterator ◄── ConcreteIterator ──► Aggregate
```

**Example:**
```java
interface IntIterator { boolean hasNext(); int next(); }
class Range implements IntIterator {
  int i, end;
  public boolean hasNext() { return i < end; }
  public int next() { return i++; }
}
```

**In the wild:**
- Java `Iterable`/`Iterator`; database cursor iteration.

### Mediator

**Problem:** Many objects talk pairwise—network of dependencies hard to change.

**Solution:** Mediator centralizes routing; colleagues only know mediator.

**When:**
- UI dialog coordination, chat rooms, air-traffic style hubs
- Reducing N² links between components

**Skip when:**
- Mediator becomes god object—split domains
- Simple event bus already decouples enough (Observer at scale)

**Structure:**
```text
ColleagueA ──┐
ColleagueB ──┼──► Mediator ◄── routes events
ColleagueC ──┘
```

**Example:**
```java
interface Mediator { void notify(Object sender, String event); }
class Dialog implements Mediator {
  Button ok; TextField name;
  public void notify(Object sender, String event) {
    if (sender == name) ok.setEnabled(!name.text().isBlank());
  }
}
```

**In the wild:**
- Message brokers as external mediators; JavaFX `Platform.runLater` coordination patterns.

### Memento

**Problem:** Capture object state for restore later without breaking encapsulation.

**Solution:** Originator creates memento snapshot; caretaker stores it; originator restores from memento.

**When:**
- Undo stacks, checkpoints, transactional draft state
- Snapshots are cheaper than replaying events

**Skip when:**
- State is huge or contains non-serializable resources
- Event sourcing already models history

**Structure:**
```text
Caretaker stores Memento ◄── Originator.createMemento()
Originator.restore(Memento)
```

**Example:**
```java
class Editor {
  String text;
  Memento save() { return new Memento(text); }
  void restore(Memento m) { text = m.text; }
  record Memento(String text) {}
}
Stack<Memento> history = new Stack<>();
```

**In the wild:**
- IDE local history; game save checkpoints; form “restore draft”.

### Observer

**Problem:** One-to-many dependency: when subject changes, dependents must update without tight coupling.

**Solution:** Subject notifies registered observers (push or pull model).

**When:**
- UI binding, domain events, pub/sub within a process
- Model changes fan out to multiple views

**Skip when:**
- One listener—direct callback
- Reactive streams (RxJava, Reactor) replace hand-rolled observer lists

**Structure:**
```text
Subject ──notify──► Observer1, Observer2, ...
  ▲ register/unregister
```

**Example:**
```java
interface Listener { void onPriceChanged(String symbol, double p); }
class Stock {
  List<Listener> ls = new CopyOnWriteArrayList<>();
  void add(Listener l) { ls.add(l); }
  void setPrice(double p) { ls.forEach(l -> l.onPriceChanged(ticker, p)); }
}
```

**In the wild:**
- `PropertyChangeListener`, JavaFX properties, Spring `ApplicationEvent`.

### State

**Problem:** Object behaviour changes with internal mode; large conditionals sprawl.

**Solution:** Encapsulate each mode in a State class; context delegates to current state object.

**When:**
- Explicit lifecycle: TCP connection, order status, workflow steps
- Transitions are table-driven and testable

**Skip when:**
- Two states—enum + switch may be clearer
- Strategy fits better if “state” is really swappable algorithm

**Structure:**
```text
Context ──► State.handle() ──► may setContext(nextState)
     ▲
ConcreteStateA / B
```

**Example:**
```java
interface OrderState { void pay(Order ctx); void ship(Order ctx); }
class Paid implements OrderState {
  public void ship(Order ctx) { /* ... */ ctx.state = new Shipped(); }
  public void pay(Order ctx) { throw new IllegalStateException(); }
}
class Order { OrderState state = new Draft(); void ship() { state.ship(this); } }
```

**In the wild:**
- Workflow engines; media players (play/pause/stop); connection pools (idle/busy).

### Strategy

**Problem:** Family of algorithms; client should swap them without changing code that uses them.

**Solution:** Strategy interface; context holds strategy reference; delegates algorithm calls.

**When:**
- Pricing rules, sorting, compression, validation policies
- Runtime selection via config or user choice

**Skip when:**
- One algorithm—inline it
- Java functional interface + lambda replaces class per strategy

**Structure:**
```text
Context ──► Strategy.algorithm()
                ▲
         ConcreteStrategyA / B
```

**Example:**
```java
interface Discount { double apply(double total); }
class Checkout {
  Discount discount;
  double total(double sub) { return discount.apply(sub); }
}
// discount = (t) -> t * 0.9;  or new MemberDiscount()
```

**In the wild:**
- `Comparator` in sorts; `ThreadPoolExecutor` rejection policies; payment provider plugins.

### Template Method

**Problem:** Algorithm skeleton is fixed but some steps vary by subtype.

**Solution:** Abstract base class defines template method calling abstract/primitive hooks; subclasses override hooks.

**When:**
- Framework lifecycle: `Servlet.service`, JUnit test flow, ETL pipelines
- Invariant order of steps with variant details

**Skip when:**
- Composition + Strategy gives clearer testing than inheritance
- Hooks multiply—consider pipeline of functions

**Structure:**
```text
AbstractClass.templateMethod()
    step1(); abstract step2(); step3();  // hooks in subclasses
```

**Example:**
```java
abstract class DataImporter {
  final void importAll() {
    open(); while (hasNext()) { parse(); validate(); persist(); }
    close();
  }
  abstract void parse();
  void validate() { /* default */ }
}
```

**In the wild:**
- Spring `JdbcTemplate`, servlet `doGet`/`doPost`, many test framework `@BeforeEach` flows.

### Visitor

**Problem:** Add operations over an object structure without changing element classes.

**Solution:** Double dispatch: elements accept `Visitor`; visitor implements `visitConcreteElement` per type.

**When:**
- Stable element hierarchy, many new operations (export, validate, compile)
- Compiler AST passes

**Skip when:**
- Hierarchy changes often—every new element breaks all visitors
- Sealed types + pattern matching replace classic Visitor in modern Java

**Structure:**
```text
Client ──► Element.accept(Visitor)
Visitor ──► visitConcreteElementA / B
```

**Example:**
```java
interface Node { <T> T accept(Visitor<T> v); }
record Add(Node l, Node r) implements Node {
  public <T> T accept(Visitor<T> v) { return v.visitAdd(this); }
}
interface Visitor<T> { T visitAdd(Add n); T visitNum(Num n); }
class Eval implements Visitor<Integer> { /* ... */ }
```

**In the wild:**
- AST compilers (checkstyle, spotbugs); document object model traversals.

---

## Quick check

1. **Creational:** You need Win and Mac variants of button, checkbox, and menu created together—Factory Method, Abstract Factory, or Builder?
2. **Structural:** You wrap an object to add gzip on the fly without changing its interface—Adapter, Decorator, or Proxy?
3. **Behavioral:** Order object behaves differently when paid vs shipped; you want to avoid giant `switch`—State or Strategy?
4. **Trade-off:** When is Singleton an anti-pattern compared to DI-scoped single instance?
5. **Modern Java:** Name one GoF pattern often replaced by lambdas and one where the full class-based form still pays off.

**Answers (study):** (1) Abstract Factory for consistent families. (2) Decorator (same interface, stacked behaviour). (3) State when transitions define behaviour; Strategy if “mode” is really pluggable algorithm. (4) When tests need fresh instances, config varies, or global mutable state hides dependencies. (5) Strategy/Command → lambdas; Visitor or complex Template Method frameworks still use classes.
