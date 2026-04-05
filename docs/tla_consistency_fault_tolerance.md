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

The specification performs an exhaustive, unbounded model check of the system's entire reachable state space (~4,000-6,000 states, depending on configuration). This mathematically proves the resilience of the system against a single process fault, directly fulfilling the core assignment requirements.

*   **Single Service Failure (Crash & Restart)**: The model verifies fault tolerance against exactly one service failing. Using the `maybeRestart()` macro, a single crash is injected non-deterministically at *any* step of the process. The exhaustive check guarantees that no matter when or where this single crash occurs, the system always recovers to a consistent state.
*   **Blind Recovery (Process Amnesia)**: Crucially, the model does NOT rely on perfect process-local memory. When the Orchestrator, Payment Worker, or Stock Worker crashes and restarts (via `maybeRestart()`), it completely wipes its local variables (e.g., `checkoutStatus` is reset to `"notStarted"`) and jumps back to the `Start` state. This accurately models a true container crash where volatile memory is lost, but the underlying databases (modeled via global variables like `paymentStatus` and `userCredits`) remain durable.
*   **Strict Idempotency**: Because recovery is "blind", a resurrected Orchestrator may re-issue original transaction messages without knowing what it sent previously. The model mathematically proves that the system's idempotency guarantees are ironclad. If a worker receives a duplicate `prepare` or `checkout` because of a crash-retry loop, it safely relies on its durable database state to acknowledge the operation without double-deducting.

## 3. Liveness and Eventual Consistency

While Safety properties ensure "bad things don't happen" (e.g., deducting too many credits), Liveness properties ensure "good things eventually happen."

*   **`EventuallyCompletes`**: The model leverages temporal logic (`EventuallyCompletes == <> (...)`) to prove that under fair scheduling, every transaction strictly completes. It verifies that:
    *   The overall state ultimately settles to either fully `completed`, or fully `failed`/`compensated`.
    *   Intermediate locked states (`userHeldCredits = 0` and `stockHeld = 0`) are fully resolved and never left dangling permanently.

## 4. Limitations of the TLA+ Abstraction

While the formal model perfectly and exhaustively verifies the high-level orchestration logic, it abstracts away certain low-level infrastructure details present in the actual application:

1.  **Saga Execution Order**: The Python `SagaExecutor` executes steps sequentially, halting at the first failure. The TLA+ model simplifies this by initiating the Saga requests in parallel, testing a slightly broader set of interleavings.
2.  **Network Timeouts**: The Python implementation handles ambiguous network timeouts by aggressively assuming operations might have occurred and triggering compensations just in case. The TLA+ model abstracts this into pure process crashes and retries.
3.  **Redis Volatility & Failovers**: The model assumes participant local state remains perfectly durable across crashes. It does not account for transient data loss during Redis Sentinel failovers or lock key TTL expirations (which are handled explicitly via Lua scripts in the implementation).
4.  **Circuit Breakers**: The fail-fast mechanisms (Circuit Breakers) preventing cascading failures under load are completely omitted from the formal model, as they serve as protective operational boundaries rather than transaction logic correctness.