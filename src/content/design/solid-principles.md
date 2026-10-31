---
title: "SOLID principles"
order: 1
---
# SOLID Principles

SOLID is five object-oriented design rules collected by Robert C. Martin. They all aim at the same thing: **code that is easy to change without breaking unrelated parts**.

| Letter | Principle | One-line idea |
| --- | --- | --- |
| S | Single Responsibility | A class has one reason to change |
| O | Open/Closed | Extend behaviour without editing existing code |
| L | Liskov Substitution | Subtypes must be usable wherever the base type is |
| I | Interface Segregation | Many small interfaces beat one fat interface |
| D | Dependency Inversion | Depend on abstractions, not concrete classes |

---

## S — Single Responsibility Principle

> A class should have one, and only one, reason to change.

"Reason to change" means one *actor* or concern: business rules, persistence, formatting, and so on. When one class mixes them, a change for one concern risks breaking the others.

### Violation

```java
class Invoice {
    double amount;

    double calculateTotal() { return amount * 1.18; }   // business rule

    void saveToDatabase() { /* JDBC code */ }           // persistence

    String toPdf() { /* PDF rendering */ return ""; }   // presentation
}
```

A tax change, a database migration, and a new PDF layout all edit the same class.

### Fix

```java
class Invoice {
    double amount;
    double calculateTotal() { return amount * 1.18; }
}

class InvoiceRepository {
    void save(Invoice invoice) { /* JDBC code */ }
}

class InvoicePdfPrinter {
    String print(Invoice invoice) { /* PDF rendering */ return ""; }
}
```

Each class now changes for exactly one reason, and each can be tested on its own.

---

## O — Open/Closed Principle

> Software entities should be open for extension, but closed for modification.

Adding a new feature should mean **adding new code**, not editing working, tested code.

### Violation

```java
class DiscountService {
    double discount(String customerType, double price) {
        if (customerType.equals("REGULAR")) return price * 0.05;
        if (customerType.equals("PREMIUM")) return price * 0.10;
        if (customerType.equals("VIP"))     return price * 0.20;   // edit every time
        return 0;
    }
}
```

### Fix — polymorphism

```java
interface DiscountPolicy {
    double discount(double price);
}

class RegularDiscount implements DiscountPolicy {
    public double discount(double price) { return price * 0.05; }
}

class PremiumDiscount implements DiscountPolicy {
    public double discount(double price) { return price * 0.10; }
}

// New type = new class. DiscountService never changes.
class VipDiscount implements DiscountPolicy {
    public double discount(double price) { return price * 0.20; }
}

class DiscountService {
    double discount(DiscountPolicy policy, double price) {
        return policy.discount(price);
    }
}
```

The `if`/`switch` on a type code is the classic smell. Strategy, Decorator, and plugin systems are common ways to follow OCP.

---

## L — Liskov Substitution Principle

> If `S` is a subtype of `T`, objects of type `T` may be replaced with objects of type `S` without breaking the program.

A subclass must honour the **contract** of its parent: same preconditions (or weaker), same postconditions (or stronger), no surprising exceptions.

### Violation — the Rectangle/Square problem

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

void resize(Rectangle r) {
    r.setWidth(5);
    r.setHeight(4);
    assert r.area() == 20;   // fails for Square: area is 16
}
```

Mathematically a square *is a* rectangle, but behaviourally it is not: it breaks the expectation that width and height change independently.

### Fix

```java
interface Shape {
    int area();
}

record Rectangle(int width, int height) implements Shape {
    public int area() { return width * height; }
}

record Square(int side) implements Shape {
    public int area() { return side * side; }
}
```

Other common LSP smells:

- A subclass method that throws `UnsupportedOperationException`.
- A subclass that ignores a call the parent would have honoured.
- Code that checks `instanceof` before calling a method.

---

## I — Interface Segregation Principle

> Clients should not be forced to depend on methods they do not use.

### Violation

```java
interface Worker {
    void work();
    void eat();
    void attendMeeting();
}

class Robot implements Worker {
    public void work() { /* ok */ }
    public void eat() { throw new UnsupportedOperationException(); }   // forced
    public void attendMeeting() { throw new UnsupportedOperationException(); }
}
```

### Fix

```java
interface Workable  { void work(); }
interface Eatable   { void eat(); }
interface Meetable  { void attendMeeting(); }

class Human implements Workable, Eatable, Meetable {
    public void work() {}
    public void eat() {}
    public void attendMeeting() {}
}

class Robot implements Workable {
    public void work() {}
}
```

Small, role-based interfaces also keep recompilation and mocking cheap: a test for `Robot` never has to stub `eat()`.

---

## D — Dependency Inversion Principle

> High-level modules should not depend on low-level modules. Both should depend on abstractions.

### Violation

```java
class MySqlDatabase {
    void insert(String data) { /* ... */ }
}

class UserService {
    private final MySqlDatabase db = new MySqlDatabase();   // hard-wired

    void register(String name) { db.insert(name); }
}
```

`UserService` (business logic) cannot be tested without MySQL and cannot switch to another store.

### Fix — depend on an interface, inject the implementation

```java
interface UserStore {
    void save(String name);
}

class MySqlUserStore implements UserStore {
    public void save(String name) { /* JDBC */ }
}

class InMemoryUserStore implements UserStore {
    final List<String> users = new ArrayList<>();
    public void save(String name) { users.add(name); }
}

class UserService {
    private final UserStore store;

    UserService(UserStore store) { this.store = store; }   // constructor injection

    void register(String name) { store.save(name); }
}

// production
new UserService(new MySqlUserStore());
// test
new UserService(new InMemoryUserStore());
```

```text
Before:  UserService ──────────► MySqlDatabase

After:   UserService ──► UserStore ◄── MySqlUserStore
                         (interface)◄── InMemoryUserStore
```

The arrow from the low-level module now points *up* to the abstraction — that is the "inversion". Frameworks like Spring automate the wiring (dependency injection), but the principle is just constructor parameters typed as interfaces.

---

## How they fit together

- **SRP** keeps classes small and focused.
- **OCP** lets you add behaviour by adding classes.
- **LSP** makes sure those new classes can be plugged in safely.
- **ISP** keeps the plug-in points (interfaces) narrow.
- **DIP** lets high-level code receive those plug-ins from outside.

Do not apply them dogmatically: an interface with a single implementation that will never change is just extra indirection. Reach for them when a part of the code **actually changes often** or needs to be tested in isolation.
