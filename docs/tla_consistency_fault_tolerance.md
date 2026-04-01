# Consistency and Fault Tolerance in the TLA+ Model

This document outlines the consistency and fault tolerance guarantees verified by the TLA+ specification (`ServicesConsistencyPlusCal.tla`) for our distributed transaction orchestration, as well as the bounds of this mathematical model compared to the actual Python and Lua implementation.

## 1. Consistency Guarantees

The TLA+ model verifies the safety of both the **Two-Phase Commit (2PC)** and **Saga** protocols by continuously checking strict invariants across all possible state transitions.

*   **Resource Conservation**:
    *   `UserCreditsConsistent`: Ensures that the sum of `userCredits`, `userHeldCredits` (locked during 2PC), and `userPaidCredits` ALWAYS equals `InitialCredits`. No credits are ever minted out of thin air or lost during a transaction.
    *   `StockConsistent`: Ensures that the sum of `stockAvailable`, `stockHeld`, and `stockSold` ALWAYS equals `InitialStock`.
*   **Protocol Adherence**:
    *   In 2PC mode, resources are explicitly locked (`prepared`) before being consumed (`completed`) or released (`failed`).
    *   In Saga mode, resources are consumed immediately (`completed`) and reversed via exact inverse operations (`compensated`) if a subsequent step fails. 

## 2. Fault Tolerance Mechanisms

The specification explicitly tests the resilience of the system against process crashes and network retries using non-deterministic modeling.

*   **Process Crashes & Restarts**: The model injects crashes at virtually any step of the process using the `maybeRestart()` macro. This macro non-deterministically halts the process and jumps back to the `Start` label, simulating a node crashing and coming back online.
*   **Idempotency Over Unreliable Networks**: Messages can be duplicated or re-sent after a restart. The `PaymentWorker` and `StockWorker` processes rigorously check for idempotency. For example, if a `prepare` message is received but the state is already `prepared` or `completed`, they safely acknowledge it with `prepareOk` without double-deducting.
*   **Orchestrator Retries**: If the orchestrator crashes and restarts, it evaluates the `checkoutStatus` and gracefully resumes the transaction (e.g., re-sending `abort` or `commit` messages if it crashed during the final phase of a 2PC loop).

## 3. Liveness and Eventual Consistency

While Safety properties ensure "bad things don't happen" (e.g., deducting too many credits), Liveness properties ensure "good things eventually happen."

*   **`EventuallyCompletes`**: The model leverages temporal logic (`EventuallyCompletes == <> (...)`) to prove that under fair scheduling, every transaction strictly completes. It verifies that:
    *   The overall state ultimately settles to either fully `completed`, or fully `failed`/`compensated`.
    *   Intermediate locked states (`userHeldCredits = 0` and `stockHeld = 0`) are fully resolved and never left dangling permanently.

## 4. Limitations of the TLA+ Abstraction

To avoid state-space explosion, the TLA+ model abstracts away several low-level infrastructure concerns present in the actual application:

1.  **Saga Execution Order**: The Python `SagaExecutor` executes steps sequentially, halting at the first failure. The TLA+ model simplifies this by initiating the Saga requests in parallel, testing a slightly broader set of interleavings.
2.  **Network Timeouts**: The Python implementation handles ambiguous network timeouts by aggressively assuming operations might have occurred and triggering compensations just in case. The TLA+ model abstracts this into pure process crashes and retries.
3.  **Redis Volatility & Failovers**: The model assumes participant local state remains perfectly durable across crashes. It does not account for transient data loss during Redis Sentinel failovers or lock key TTL expirations (which are handled explicitly via Lua scripts in the implementation).
4.  **Circuit Breakers**: The fail-fast mechanisms (Circuit Breakers) preventing cascading failures under load are completely omitted from the formal model, as they serve as protective operational boundaries rather than transaction logic correctness.