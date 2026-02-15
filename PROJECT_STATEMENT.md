# Distributed Data Systems — Course Project

## Overview

The goal of this year's project is to implement a set of **microservices** that need to coordinate in order to guarantee **data consistency**.

This project reflects best practices already in use in the development world. We want to see the effect of different technologies and design patterns on data management aspects of microservices — including **SAGAs**, **2PC**, **consistency**, **performance**, **scalability**, and **fault tolerance**.

> We will judge all project implementations according to their **difficulty**, the **quality of the solution**, and the **number of things that the students have learned** in the process. Those will be assessed during a rigorous interview at the end of the course.
>
> The goal of a master lecture is to learn, not to count beans for grades.

---

## Evaluation Criteria

| Criterion | Description |
|-----------|-------------|
| **Consistency** | We do not lose money, nor item counts |
| **Performance** | Latency & throughput |
| **Architecture Difficulty** | e.g., synchronous vs. asynchronous / event-driven |

---

## Microservice Architecture

Implement microservices with **Python Flask** and **Redis**.

- **Project template:** <https://github.com/delftdata/wdm-project-template>
- You may use another Python framework (e.g., async Flask with **Quart**), but:
  - You **cannot** use any other language.
  - You **cannot** change the external-world-facing API described below.

Microservice design must adhere to: <https://martinfowler.com/articles/microservices.html>
> Pay special attention to the section **"Decentralized Data Management"**.

---

## API Reference

### Order Service

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/orders/create/{user_id}` | `POST` | Creates an order for the given user, returns an `order_id` |
| `/orders/find/{order_id}` | `GET` | Retrieves order information |
| `/orders/addItem/{order_id}/{item_id}/{quantity}` | `POST` | Adds a given item to the order |
| `/orders/checkout/{order_id}` | `POST` | Makes the payment, subtracts stock, returns success/failure |

**`/orders/create/{user_id}`** response:
```json
{ "user_id": "<user's id>" }
```

**`/orders/find/{order_id}`** response:
```json
{
  "order_id": "<order's id>",
  "paid": true,
  "items": ["<item_id>", "..."],
  "user_id": "<user's id>",
  "total_cost": 0.00
}
```

### Stock Service

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/stock/find/{item_id}` | `GET` | Returns item stock availability and price |
| `/stock/subtract/{item_id}/{amount}` | `POST` | Subtracts amount from item stock |
| `/stock/add/{item_id}/{amount}` | `POST` | Adds amount to item stock |
| `/stock/item/create/{price}` | `POST` | Creates an item with given price, returns its ID |

**`/stock/find/{item_id}`** response:
```json
{ "stock": 0, "price": 0.00 }
```

**`/stock/item/create/{price}`** response:
```json
{ "item_id": "<item's id>" }
```

### Payment Service

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/payment/pay/{user_id}/{amount}` | `POST` | Subtracts amount from user's credit (fails if insufficient) |
| `/payment/add_funds/{user_id}/{amount}` | `POST` | Adds funds to user's account |
| `/payment/create_user` | `POST` | Creates a user with 0 credit |
| `/payment/find_user/{user_id}` | `GET` | Returns user information |

**`/payment/add_funds/{user_id}/{amount}`** response:
```json
{ "done": true }
```

**`/payment/create_user`** response:
```json
{ "user_id": "<user's id>" }
```

**`/payment/find_user/{user_id}`** response:
```json
{ "user_id": "<user's id>", "credit": 0.00 }
```

### HTTP Status Codes

- **2xx** — Success
- **4xx** — Failure (use `400` as a generic failure code)
- Reference: <https://developer.mozilla.org/en-US/docs/Web/HTTP/Status>

---

## Transaction Strategy: SAGAs vs 2PC vs Managed

Choose your approach based on the database backend:

- **Two-Phase Commit (2PC)** using the Open XA standard
- **SAGAs** — <https://microservices.io/patterns/data/saga.html>
- **Managed** — a distributed database offering multi-partition transactions

---

## Detailed Evaluation Dimensions

### Scalability
Architecture must be **scalable and elastic** to accommodate varying load — but scaling needs to be **efficient**.

### Consistency
Transaction implementation must provide a consistency guarantee (e.g., eventual consistency, serializability, snapshot isolation).

### Availability
The system must remain **available under any load scenario**.

### Fault Tolerance
Handle failures that occur **mid-transaction** (e.g., during checkout). Example: the payment microservice dies after receiving a rollback message but before committing the rollback to its database.

### Transaction Performance
Maximize **throughput** with minimal **latency** while remaining efficient.

### Event-Driven Design
Asynchronous event-driven architectures are more performant (and harder to implement) than synchronous REST-based communication. **Extra points** for reactive microservices.

### Difficulty
Implementation difficulty is factored into evaluation — some approaches are harder on certain aspects.

### Benchmarking
- Manual testing: [Postman](https://www.getpostman.com/)
- Stress testing: [Locust](http://locust.io)
- Provided tests: see the `test` folder in the template project
- **Max resources:** 20 CPUs
- **Failure testing:** one container killed at a time, with recovery time allowed
- **Bonus:** create a better benchmark

---

## Deliverables

> All deadlines refer to **11:59 PM**.

### Group Formation
**February 18th**

### Phase 1 — System Design (March 3rd)
- Transaction protocol and planned architecture
- Message-flow style description (e.g., [2PC message flow](https://en.wikipedia.org/wiki/Two-phase_commit_protocol))
- **Format:** PDF document, max 2 pages (mostly diagrams, few explanations)
- **Submit on:** Brightspace

### Phase 2 — Implementation (March 21st)
- Implement transactional protocol in **Flask + Redis**
- **Criteria:** performance, consistency
- **Deliverable:** Private GitHub repository `{username}/dds25-{team#}`
  - Add collaborators: `kpsarakis`, `asteriosk`, `GiorgosChristodoulou`
- Must work with the benchmark without changes: <https://github.com/delftdata/wdm-project-benchmark>

### Phase 3 — Final Deliverable (April 11th)
- **Fault tolerance:** system must recover from individual container failures (databases or service instances)
- Testing approach: fail one container → let system recover → fail another → repeat
- System **must remain consistent** throughout
- **Stretch goal:** high-performance with **zero downtime** under failures

### `contributions.txt`
A top-level file where each team member describes their contribution in one sentence (e.g., code, architecture, documentation, experiments, psychological support, beer, cookies, etc.).
