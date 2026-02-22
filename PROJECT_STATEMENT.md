# [Projects for Distributed Data Systems Course](https://docs.google.com/document/d/1OAHOSXucovy6m1RG_4UgwMbVr7dED9Xng5Bqk5ftgVw/edit?tab=t.0#heading=h.is6brajmt6us)

The goal of this year’s project is to implement a set of microservices that need to coordinate in order to guarantee data consistency. 

This project reflects the best practices that are in use already in the development world. There we want to see the effect of different technologies and design patterns on data management aspects of microservices (SAGAs, 2PC, consistency, performance, scalability, fault tolerance).

We will judge all project implementations according to their difficulty, the quality of the solution, and the number of things that the students have learned in the process. Those will be assessed during a rigorous interview, at the end of the course. So, we can promise you that we will be fair and will take those into account. *The goal of a master lecture is to learn, not to count beans for grades.* 

**Evaluation Criteria:**

* **Consistency** (we do not lose money, nor item counts) 
* **Performance** (latency & throughput) 
* **Architecture Difficulty** (e.g., synchronous, asynchronous / event-driven)

## Microservice-architecture

Implementing Microservices with Python Flask and Redis. We provide a project template: [https://github.com/delftdata/wdm-project-template](https://github.com/delftdata/wdm-project-template). If you want to use another Python framework (e.g., async Flask with Quart, etc.) you have the right to do so. Just bear in mind that you cannot use any other language and you cannot change the following external-world-facing API described below (and already implemented in the template).

### Microservice Endpoints to Implement (already in the template)

Those have to adhere to the principles of microservices design. 
[https://martinfowler.com/articles/microservices.html](https://martinfowler.com/articles/microservices.html)
Pay special attention to the section “Decentralized Data Management”.

We have prepared a template for your project, where we have implemented the “API” of each microservice here: 
[https://github.com/delftdata/wdm-project-template](https://github.com/delftdata/wdm-project-template)

### API Reference (implemented in the Python template we provided)

#### Order Service

* **/orders/create/{user\_id}** 
  * POST \- creates an order for the given user, and returns an order\_id 
  * Output JSON fields: 
    * “order-iduser\_id” \- the orderuser’s id 
* **/orders/find/{order\_id}** 
  * GET \- retrieves the information of an order (id, payment status, items included and user id) 
  * Output JSON fields: 
    * “order\_id” \- the order’s id 
    * “paid” (true/false) 
    * “items” \- list of item ids that are included in the order 
    * “user\_id” \- the user’s id that made the order 
    * “total\_cost” \- the total cost of the items in the order 
* **/orders/addItem/{order\_id}/{item\_id}/{quantity}** 
  * POST \- adds a given item in the order given 
* **/orders/checkout/{order\_id}** 
  * POST \- makes the payment (via calling the payment service), subtracts the stock (via the stock service) and returns a status (success/failure).

#### Stock Service

* **/stock/find/{item\_id}** 
  * GET \- returns an item’s stock availability and price. 
  * Output JSON fields: 
    * “stock” \- the item’s stock 
    * “price” \- the item’s price 
* **/stock/subtract/{item\_id}/{amount}** 
  * POST \- subtracts an item from stock by the amount specified. 
* **/stock/add/{item\_id}/{amount}** 
  * POST \- adds the given number of stock items to the item count in the stock 
* **/stock/item/create/{price}** 
  * POST \- adds an item and its price, and returns its ID. 
  * Output JSON fields: 
    * “item\_id” \- the item’s id

#### Payment Service

* **/payment/pay/{user\_id}/{amount}** 
  * POST \- subtracts the amount of the order from the user’s credit (returns failure if credit is not enough) 
* **/payment/add\_funds/{user\_id}/{amount}** 
  * POST \- adds funds (amount) to the user’s (user\_id) account 
  * Output JSON fields: 
    * “done” (true/false) 
* **/payment/create\_user** 
  * POST \- creates a user with 0 credit 
   * Output JSON fields: 
     * “user\_id” \- the user’s id 
* **/payment/find\_user/{user\_id}** 
   * GET \- returns the user information 
  * Output JSON fields: 
    * “user\_id” \- the user’s id 
    * “credit” \- the user’s credit

For response status codes you can use the generic ones. 400 for failure and 200 for success on every request. For a more detailed list, you can check [https://developer.mozilla.org/en-US/docs/Web/HTTP/Status](https://developer.mozilla.org/en-US/docs/Web/HTTP/Status) but make sure you keep the failures to 4xx codes, and the successes to 2xx codes.

#### SAGAs vs Two-Phase Commit vs Managed

You will have to choose, according to the database backend that you are implementing with, whether you can perform two-phase commits using the Open XA standard, SAGAs \[4\] or a distributed database system offering transactions (the database needs to be scalable and to support multi-partition transactions).

A rough description of SAGAs can be found here: [https://microservices.io/patterns/data/saga.html](https://microservices.io/patterns/data/saga.html) please use the internet and youtube for more information on what that is.

## Further Notes on Evaluation

#### Scalability 

Your architecture needs to be scalable and elastic to accommodate the varying load. However, the way that it scales needs to be efficient.

#### Consistency 

Your transaction implementation needs to provide some kind of consistency guarantee (e.g., eventual consistency, serializability, snapshot isolation). 

#### Availability 

The system needs to be available during any load scenario.

#### Fault Tolerance 

Machine failures can happen at any time in a distributed system. Try to handle cases of failures that can happen in the middle of a transaction (checkout). For example, the payment microservice might die after receiving a rollback message from the order microservice and haven’t committed that rollback yet to its database. 

#### Transaction Performance

Try to reach as high throughput with as low latency as possible while trying to remain efficient. 

#### Event-Driven Design

Event-driven asynchronous architectures are far more performant and difficult to implement compared to a synchronous architecture with REST calls between the microservices. A solution like that will get extra points. (reactive microservices)

#### Difficulty 

Some systems might be easier on some aspects of the implementation compared to others. So the difficulty of implementation will play a role in the evaluation. 

## Provided Benchmark

To test microservices you can use postman ([https://www.getpostman.com/](https://www.getpostman.com/)) and to stress test service you can use [http://locust.io](http://locust.io). We provide some [basic stress and consistency tests](https://github.com/delftdata/wdm-project-benchmark) that will help you during the development of the system. For basic correctness tests, you could take a look at the test folder in [the template project](https://github.com/delftdata/wdm-project-template). You could also try to create a better benchmark for some bonus points. Your code will be checked against 20 CPUs max. We will kill one container at a time and give it some seconds to recover.

## **Deliverables (all dates refer to 11.59pm)**

* February 16th: group formation 
* Phase 1: 13th March: 
  * Implement two-phase commit and SAGAs protocols in Flask \+ Redis 
  * Fault-tolerance: we should be able to fail a container, and your system should be able to recover. This includes killing a database, or a service instance. 
  * We will evaluate your system by failing a single container, letting the system recover, then fail another, etc. 
  * Your system should remain consistent. 
  * Stretch-goal: high-performance (i.e., zero down-time) under failures 
  * Criteria: performance, consistency 
  * Deliverable: 
  Public Github repository link; {username}/dds26-{team\#} (team\# is your group number on brightspace) 
  * This benchmark should be able to work on a local machine without changes: 
  * [https://github.com/delftdata/wdm-project-benchmark](https://github.com/delftdata/wdm-project-benchmark) 
* Phase 2 (final deliverable, 1st April): 
  * Goal: abstract away the Two-phase commit protocol and SAGAs into a separate software artifact that we will call an Orchestrator. 
  * The old project should be rewritten in a way that it makes use of the orchestrator, instead of implementing two-phase commit-like behavior in application code. 
  * You will share, again, a github repository with the implementation of the orchestrator, and give us an implementation of the Shopping-cart project that uses that orchestrator. 
 

contributions.txt should be a file at the top-level directory of your repository where each member describes (in a sentence) what they have contributed to the project (e.g., code, architecture, documentation, experiments, psychological support, beer, cookies, etc.). 