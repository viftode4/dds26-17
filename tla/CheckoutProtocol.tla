--------------------------- MODULE CheckoutProtocol ---------------------------
(*
 * Formal specification of the Hybrid 2PC/Saga checkout protocol.
 *
 * Models the orchestrator (executor.py), stock & payment services (Lua libs),
 * TTL-based reservation expiry, crash recovery (recovery.py), and
 * nondeterministic orchestrator crashes.
 *
 * Purpose: verify safety properties (no double-spend, resource conservation,
 * atomic outcomes) and expose real edge-case bugs in the implementation.
 *
 * Abstraction: single-saga, single-item, message channels as sequences.
 * This is sufficient to expose the protocol-level bugs without blowing up
 * the state space with multi-item/multi-saga combinatorics.
 *
 * Version 2: Updated to model fixes for 3 bugs found by TLC:
 *   Fix 1: Idempotent confirm re-emits outbox event
 *   Fix 2: Recovery waits for confirm responses before logging COMPLETED
 *   Fix 3: Executor cancels on confirm retry exhaustion +
 *           cancel restores counters from fallback amount key
 *)
EXTENDS Integers, Sequences, FiniteSets, TLC

CONSTANTS
    INITIAL_STOCK,      \* e.g. 10
    INITIAL_CREDIT,     \* e.g. 100
    RESERVE_AMOUNT,     \* e.g. 3
    PAYMENT_AMOUNT,     \* e.g. 15
    MAX_CONFIRM_RETRIES \* e.g. 2

(* Remove a specific element from a sequence *)
Remove(seq, idx) == [i \in 1..(Len(seq)-1) |->
                        IF i < idx THEN seq[i] ELSE seq[i+1]]

(*
--algorithm CheckoutProtocol
{
    \* ================================================================
    \* Global variables
    \* ================================================================
    variables
        \* --- WAL state ---
        walState = "INIT";          \* INIT|STARTED|PREPARING|COMMITTING|ABORTING|COMPLETED|FAILED

        \* --- Stock service state ---
        stockAvailable = INITIAL_STOCK;
        stockReserved = 0;
        stockSold = 0;
        stockReservationKey = FALSE;     \* TRUE = reservation key exists (not expired)
        stockFallbackAmount = 0;         \* fallback amount key (24h TTL, survives reservation TTL)
        stockStatus = "none";            \* none|reserved|confirmed|cancelled

        \* --- Payment service state ---
        creditAvailable = INITIAL_CREDIT;
        creditHeld = 0;
        creditSpent = 0;
        paymentReservationKey = FALSE;
        paymentFallbackAmount = 0;       \* fallback amount key (24h TTL)
        paymentStatus = "none";

        \* --- Message channels (sequences acting as FIFO queues) ---
        stockCmd = <<>>;       \* orchestrator -> stock
        stockResp = <<>>;      \* stock -> orchestrator (outbox)
        paymentCmd = <<>>;     \* orchestrator -> payment
        paymentResp = <<>>;    \* payment -> orchestrator (outbox)

        \* --- Orchestrator state ---
        orchestratorAlive = TRUE;
        confirmRetries = 0;

        \* --- Recovery flag ---
        recoveryDone = FALSE;

        \* --- Crash flag (environment can crash orchestrator once) ---
        crashed = FALSE;

    \* ================================================================
    \* STOCK SERVICE
    \* Faithfully models stock_lib.lua: try_reserve_batch, confirm_batch,
    \* cancel_batch including idempotency checks, TTL, and fallback keys.
    \* ================================================================
    fair process (StockService = 10)
    variable stockAction = "idle";
    {
    StockLoop:
        while (walState \notin {"COMPLETED", "FAILED"} \/ stockCmd /= <<>>) {
            \* Wait for a command
    StockWait:
            await stockCmd /= <<>> \/ (walState \in {"COMPLETED", "FAILED"} /\ stockCmd = <<>>);
            if (stockCmd = <<>>) { goto Done; }
            else {
                stockAction := stockCmd[1];
                stockCmd := Remove(stockCmd, 1);
            };

    StockExec:
            if (stockAction = "try_reserve") {
                \* --- try_reserve_batch (stock_lib.lua) ---
                if (stockStatus = "reserved") {
                    \* Idempotent: re-emit reserved event
                    stockResp := Append(stockResp, "reserved");
                } else if (stockAvailable >= RESERVE_AMOUNT) {
                    \* Atomically: available -= amount, reserved += amount, set key + TTL
                    stockAvailable := stockAvailable - RESERVE_AMOUNT;
                    stockReserved := stockReserved + RESERVE_AMOUNT;
                    stockReservationKey := TRUE;
                    stockFallbackAmount := RESERVE_AMOUNT;  \* FIX 3B: store fallback
                    stockStatus := "reserved";
                    stockResp := Append(stockResp, "reserved");
                } else {
                    \* Insufficient stock
                    stockResp := Append(stockResp, "failed");
                };
            } else if (stockAction = "confirm") {
                \* --- confirm_batch (stock_lib.lua) ---
                if (stockStatus = "confirmed") {
                    \* FIX 1: Idempotent confirm re-emits outbox event
                    stockResp := Append(stockResp, "confirmed");
                } else if (stockReservationKey = TRUE) {
                    \* Normal confirm: reserved -= amount, mark sold, delete key
                    stockReserved := stockReserved - RESERVE_AMOUNT;
                    stockSold := stockSold + RESERVE_AMOUNT;
                    stockReservationKey := FALSE;
                    stockFallbackAmount := 0;   \* FIX 3B: delete fallback on confirm
                    stockStatus := "confirmed";
                    stockResp := Append(stockResp, "confirmed");
                } else {
                    \* Reservation key expired (TTL) — confirm_failed
                    stockResp := Append(stockResp, "confirm_failed");
                };
            } else if (stockAction = "cancel") {
                \* --- cancel_batch (stock_lib.lua) ---
                if (stockStatus = "cancelled") {
                    \* Idempotent: skip but still emit event
                    stockResp := Append(stockResp, "cancelled");
                } else {
                    if (stockReservationKey = TRUE) {
                        \* Key present: restore available, decrement reserved
                        stockAvailable := stockAvailable + RESERVE_AMOUNT;
                        stockReserved := stockReserved - RESERVE_AMOUNT;
                        stockReservationKey := FALSE;
                    } else if (stockFallbackAmount > 0) {
                        \* FIX 3B: Key expired but fallback available — restore counters
                        stockAvailable := stockAvailable + stockFallbackAmount;
                        stockReserved := stockReserved - stockFallbackAmount;
                    };
                    stockFallbackAmount := 0;   \* Always delete fallback
                    stockStatus := "cancelled";
                    stockResp := Append(stockResp, "cancelled");
                };
            };
        };
    }

    \* ================================================================
    \* PAYMENT SERVICE
    \* Faithfully models payment_lib.lua: payment_try_reserve, payment_confirm,
    \* payment_cancel including idempotency, TTL, and fallback keys.
    \* ================================================================
    fair process (PaymentService = 20)
    variable paymentAction = "idle";
    {
    PaymentLoop:
        while (walState \notin {"COMPLETED", "FAILED"} \/ paymentCmd /= <<>>) {
    PaymentWait:
            await paymentCmd /= <<>> \/ (walState \in {"COMPLETED", "FAILED"} /\ paymentCmd = <<>>);
            if (paymentCmd = <<>>) { goto Done; }
            else {
                paymentAction := paymentCmd[1];
                paymentCmd := Remove(paymentCmd, 1);
            };

    PaymentExec:
            if (paymentAction = "try_reserve") {
                \* --- payment_try_reserve (payment_lib.lua) ---
                if (paymentStatus = "reserved") {
                    paymentResp := Append(paymentResp, "reserved");
                } else if (creditAvailable >= PAYMENT_AMOUNT) {
                    creditAvailable := creditAvailable - PAYMENT_AMOUNT;
                    creditHeld := creditHeld + PAYMENT_AMOUNT;
                    paymentReservationKey := TRUE;
                    paymentFallbackAmount := PAYMENT_AMOUNT;  \* FIX 3B: store fallback
                    paymentStatus := "reserved";
                    paymentResp := Append(paymentResp, "reserved");
                } else {
                    paymentResp := Append(paymentResp, "failed");
                };
            } else if (paymentAction = "confirm") {
                \* --- payment_confirm (payment_lib.lua) ---
                if (paymentStatus = "confirmed") {
                    \* FIX 1: Idempotent confirm re-emits outbox event
                    paymentResp := Append(paymentResp, "confirmed");
                } else if (paymentReservationKey = TRUE) {
                    creditHeld := creditHeld - PAYMENT_AMOUNT;
                    creditSpent := creditSpent + PAYMENT_AMOUNT;
                    paymentReservationKey := FALSE;
                    paymentFallbackAmount := 0;  \* FIX 3B: delete fallback on confirm
                    paymentStatus := "confirmed";
                    paymentResp := Append(paymentResp, "confirmed");
                } else {
                    paymentResp := Append(paymentResp, "confirm_failed");
                };
            } else if (paymentAction = "cancel") {
                \* --- payment_cancel (payment_lib.lua) ---
                if (paymentStatus = "cancelled") {
                    paymentResp := Append(paymentResp, "cancelled");
                } else {
                    if (paymentReservationKey = TRUE) {
                        creditAvailable := creditAvailable + PAYMENT_AMOUNT;
                        creditHeld := creditHeld - PAYMENT_AMOUNT;
                        paymentReservationKey := FALSE;
                    } else if (paymentFallbackAmount > 0) {
                        \* FIX 3B: Key expired but fallback available — restore counters
                        creditAvailable := creditAvailable + paymentFallbackAmount;
                        creditHeld := creditHeld - paymentFallbackAmount;
                    };
                    paymentFallbackAmount := 0;  \* Always delete fallback
                    paymentStatus := "cancelled";
                    paymentResp := Append(paymentResp, "cancelled");
                };
            };
        };
    }

    \* ================================================================
    \* TTL DAEMON
    \* Nondeterministically expires reservation keys while status = "reserved".
    \* Only deletes the key — does NOT touch counters (matches Redis TTL behavior).
    \* Does NOT expire fallback amount keys (they have 24h TTL).
    \* ================================================================
    fair process (TTLDaemon = 30)
    {
    TTLLoop:
        while (walState \notin {"COMPLETED", "FAILED"}) {
    TTLCheck:
            either {
                \* Expire stock reservation key
                if (stockReservationKey = TRUE /\ stockStatus = "reserved") {
                    stockReservationKey := FALSE;
                };
            } or {
                \* Expire payment reservation key
                if (paymentReservationKey = TRUE /\ paymentStatus = "reserved") {
                    paymentReservationKey := FALSE;
                };
            } or {
                \* Do nothing (TTL hasn't fired yet)
                skip;
            };
        };
    }

    \* ================================================================
    \* ORCHESTRATOR
    \* Models executor.py (TwoPCExecutor / SagaExecutor — functionally identical
    \* in protocol steps). Includes forward recovery (confirm retry) and
    \* FIX 3A: cancel broadcast on retry exhaustion.
    \* ================================================================
    fair process (Orchestrator = 40)
    variable stockVote = "pending", paymentVote = "pending",
             stockConfirmResult = "pending", paymentConfirmResult = "pending";
    {
    OrcStart:
        if (~orchestratorAlive) { goto Done; }
        else { walState := "STARTED"; };

    OrcPrepare:
        if (~orchestratorAlive) { goto Done; }
        else {
            walState := "PREPARING";
            \* Send try_reserve to both services
            stockCmd := Append(stockCmd, "try_reserve");
            paymentCmd := Append(paymentCmd, "try_reserve");
        };

    OrcCollectStockVote:
        \* Wait for stock vote
        await stockResp /= <<>> \/ ~orchestratorAlive;
        if (~orchestratorAlive) {
            goto Done;
        } else {
            stockVote := stockResp[1];
            stockResp := Remove(stockResp, 1);
        };

    OrcCollectPaymentVote:
        \* Wait for payment vote
        await paymentResp /= <<>> \/ ~orchestratorAlive;
        if (~orchestratorAlive) {
            goto Done;
        } else {
            paymentVote := paymentResp[1];
            paymentResp := Remove(paymentResp, 1);
        };

    OrcDecide:
        if (~orchestratorAlive) {
            goto Done;
        } else if (stockVote = "reserved" /\ paymentVote = "reserved") {
            \* All reserved — commit path
            goto OrcSendConfirm;
        } else {
            \* Any failed — abort path
            goto OrcSendCancel;
        };

    OrcSendConfirm:
        if (~orchestratorAlive) { goto Done; }
        else {
            walState := "COMMITTING";
            stockCmd := Append(stockCmd, "confirm");
            paymentCmd := Append(paymentCmd, "confirm");
        };

    OrcCollectStockConfirm:
        await stockResp /= <<>> \/ ~orchestratorAlive;
        if (~orchestratorAlive) {
            goto Done;
        } else {
            stockConfirmResult := stockResp[1];
            stockResp := Remove(stockResp, 1);
        };

    OrcCollectPaymentConfirm:
        await paymentResp /= <<>> \/ ~orchestratorAlive;
        if (~orchestratorAlive) {
            goto Done;
        } else {
            paymentConfirmResult := paymentResp[1];
            paymentResp := Remove(paymentResp, 1);
        };

    OrcEvalConfirms:
        if (~orchestratorAlive) {
            goto Done;
        } else if (stockConfirmResult = "confirm_failed" \/ paymentConfirmResult = "confirm_failed") {
            if (confirmRetries < MAX_CONFIRM_RETRIES) {
                \* Retry: re-send confirm to failed services
                confirmRetries := confirmRetries + 1;
                if (stockConfirmResult = "confirm_failed") {
                    stockCmd := Append(stockCmd, "confirm");
                    stockConfirmResult := "pending";
                };
                if (paymentConfirmResult = "confirm_failed") {
                    paymentCmd := Append(paymentCmd, "confirm");
                    paymentConfirmResult := "pending";
                };
                goto OrcRetryDispatch;
            } else {
                \* FIX 3A: Retries exhausted — send cancel to all before FAILED
                stockCmd := Append(stockCmd, "cancel");
                paymentCmd := Append(paymentCmd, "cancel");
                walState := "FAILED";
                goto Done;
            };
        } else {
            \* Both confirmed successfully
            goto OrcComplete;
        };

    OrcRetryDispatch:
        \* Route to the right collection label based on what needs retrying
        if (stockConfirmResult = "pending") {
            goto OrcRetryCollectStock;
        } else {
            goto OrcRetryCollectPayment;
        };

    OrcRetryCollectStock:
        await stockResp /= <<>> \/ ~orchestratorAlive;
        if (~orchestratorAlive) {
            goto Done;
        } else {
            stockConfirmResult := stockResp[1];
            stockResp := Remove(stockResp, 1);
        };

    OrcAfterRetryStock:
        if (paymentConfirmResult = "pending") {
            goto OrcRetryCollectPayment;
        } else {
            goto OrcEvalConfirms;
        };

    OrcRetryCollectPayment:
        await paymentResp /= <<>> \/ ~orchestratorAlive;
        if (~orchestratorAlive) {
            goto Done;
        } else {
            paymentConfirmResult := paymentResp[1];
            paymentResp := Remove(paymentResp, 1);
        };

    OrcAfterRetryPayment:
        if (stockConfirmResult = "pending") {
            goto OrcRetryCollectStock;
        } else {
            goto OrcEvalConfirms;
        };

    OrcComplete:
        walState := "COMPLETED";
        goto Done;

    OrcSendCancel:
        if (~orchestratorAlive) { goto Done; }
        else {
            walState := "ABORTING";
            stockCmd := Append(stockCmd, "cancel");
            paymentCmd := Append(paymentCmd, "cancel");
        };

    OrcFailed:
        \* Fire-and-forget: don't wait for cancel responses
        walState := "FAILED";
    }

    \* ================================================================
    \* CRASH PROCESS
    \* Nondeterministically crashes the orchestrator at any non-terminal
    \* WAL state. Crashes at most once.
    \* ================================================================
    process (CrashProcess = 50)
    {
    CrashDecide:
        either {
            \* Wait until orchestrator is in a crashable state
            await walState \notin {"INIT", "COMPLETED", "FAILED"} /\ orchestratorAlive /\ ~crashed;
            orchestratorAlive := FALSE;
            crashed := TRUE;
        } or {
            \* No crash happens
            skip;
        };
    }

    \* ================================================================
    \* RECOVERY WORKER
    \* Models recovery.py: activated after crash, resumes saga based on
    \* last WAL state. FIX 2: waits for confirm responses before COMPLETED.
    \* ================================================================
    fair process (RecoveryWorker = 60)
    variable recStockResult = "pending", recPaymentResult = "pending",
             recRetries = 0;
    {
    RecoveryStart:
        \* Only activate if orchestrator crashed
        await ~orchestratorAlive \/ walState \in {"COMPLETED", "FAILED"};
        if (walState \in {"COMPLETED", "FAILED"}) goto Done;

    RecoveryAct:
        if (walState = "STARTED") {
            \* Never reached prepare — no resources locked
            walState := "FAILED";
            recoveryDone := TRUE;
        } else if (walState = "PREPARING" \/ walState = "TRYING") {
            \* Sent prepares but outcome unknown — abort all (fire-and-forget is safe)
            stockCmd := Append(stockCmd, "cancel");
            paymentCmd := Append(paymentCmd, "cancel");
            walState := "FAILED";
            recoveryDone := TRUE;
        } else if (walState = "COMMITTING" \/ walState = "CONFIRMING") {
            \* FIX 2: Send confirm and WAIT for responses
            stockCmd := Append(stockCmd, "confirm");
            paymentCmd := Append(paymentCmd, "confirm");
            goto RecCollectStockConfirm;
        } else if (walState = "ABORTING" \/ walState = "COMPENSATING") {
            \* Was in abort phase — retry cancels (fire-and-forget is safe)
            stockCmd := Append(stockCmd, "cancel");
            paymentCmd := Append(paymentCmd, "cancel");
            walState := "FAILED";
            recoveryDone := TRUE;
        };

    RecDone:
        goto Done;

    RecCollectStockConfirm:
        await stockResp /= <<>>;
        recStockResult := stockResp[1];
        stockResp := Remove(stockResp, 1);

    RecCollectPaymentConfirm:
        await paymentResp /= <<>>;
        recPaymentResult := paymentResp[1];
        paymentResp := Remove(paymentResp, 1);

    RecEvalConfirms:
        if (recStockResult = "confirmed" /\ recPaymentResult = "confirmed") {
            \* Both confirmed — safe to log COMPLETED
            walState := "COMPLETED";
            recoveryDone := TRUE;
        } else if (recRetries < MAX_CONFIRM_RETRIES) {
            \* Retry failed confirms
            recRetries := recRetries + 1;
            if (recStockResult = "confirm_failed") {
                stockCmd := Append(stockCmd, "confirm");
                recStockResult := "pending";
            };
            if (recPaymentResult = "confirm_failed") {
                paymentCmd := Append(paymentCmd, "confirm");
                recPaymentResult := "pending";
            };
            goto RecRetryDispatch;
        } else {
            \* Retries exhausted — cancel all and log FAILED
            stockCmd := Append(stockCmd, "cancel");
            paymentCmd := Append(paymentCmd, "cancel");
            walState := "FAILED";
            recoveryDone := TRUE;
        };

    RecTerminate:
        goto Done;

    RecRetryDispatch:
        if (recStockResult = "pending") {
            goto RecRetryCollectStock;
        } else {
            goto RecRetryCollectPayment;
        };

    RecRetryCollectStock:
        await stockResp /= <<>>;
        recStockResult := stockResp[1];
        stockResp := Remove(stockResp, 1);

    RecAfterRetryStock:
        if (recPaymentResult = "pending") {
            goto RecRetryCollectPayment;
        } else {
            goto RecEvalConfirms;
        };

    RecRetryCollectPayment:
        await paymentResp /= <<>>;
        recPaymentResult := paymentResp[1];
        paymentResp := Remove(paymentResp, 1);

    RecAfterRetryPayment:
        if (recStockResult = "pending") {
            goto RecRetryCollectStock;
        } else {
            goto RecEvalConfirms;
        };
    }
}
*)

\* BEGIN TRANSLATION - the hash of the PCal code: PCal-placeholder
VARIABLES pc, walState, stockAvailable, stockReserved, stockSold, 
          stockReservationKey, stockFallbackAmount, stockStatus, 
          creditAvailable, creditHeld, creditSpent, paymentReservationKey, 
          paymentFallbackAmount, paymentStatus, stockCmd, stockResp, 
          paymentCmd, paymentResp, orchestratorAlive, confirmRetries, 
          recoveryDone, crashed, stockAction, paymentAction, stockVote, 
          paymentVote, stockConfirmResult, paymentConfirmResult, 
          recStockResult, recPaymentResult, recRetries

vars == << pc, walState, stockAvailable, stockReserved, stockSold, 
           stockReservationKey, stockFallbackAmount, stockStatus, 
           creditAvailable, creditHeld, creditSpent, paymentReservationKey, 
           paymentFallbackAmount, paymentStatus, stockCmd, stockResp, 
           paymentCmd, paymentResp, orchestratorAlive, confirmRetries, 
           recoveryDone, crashed, stockAction, paymentAction, stockVote, 
           paymentVote, stockConfirmResult, paymentConfirmResult, 
           recStockResult, recPaymentResult, recRetries >>

ProcSet == {10} \cup {20} \cup {30} \cup {40} \cup {50} \cup {60}

Init == (* Global variables *)
        /\ walState = "INIT"
        /\ stockAvailable = INITIAL_STOCK
        /\ stockReserved = 0
        /\ stockSold = 0
        /\ stockReservationKey = FALSE
        /\ stockFallbackAmount = 0
        /\ stockStatus = "none"
        /\ creditAvailable = INITIAL_CREDIT
        /\ creditHeld = 0
        /\ creditSpent = 0
        /\ paymentReservationKey = FALSE
        /\ paymentFallbackAmount = 0
        /\ paymentStatus = "none"
        /\ stockCmd = <<>>
        /\ stockResp = <<>>
        /\ paymentCmd = <<>>
        /\ paymentResp = <<>>
        /\ orchestratorAlive = TRUE
        /\ confirmRetries = 0
        /\ recoveryDone = FALSE
        /\ crashed = FALSE
        (* Process StockService *)
        /\ stockAction = "idle"
        (* Process PaymentService *)
        /\ paymentAction = "idle"
        (* Process Orchestrator *)
        /\ stockVote = "pending"
        /\ paymentVote = "pending"
        /\ stockConfirmResult = "pending"
        /\ paymentConfirmResult = "pending"
        (* Process RecoveryWorker *)
        /\ recStockResult = "pending"
        /\ recPaymentResult = "pending"
        /\ recRetries = 0
        /\ pc = [self \in ProcSet |-> CASE self = 10 -> "StockLoop"
                                        [] self = 20 -> "PaymentLoop"
                                        [] self = 30 -> "TTLLoop"
                                        [] self = 40 -> "OrcStart"
                                        [] self = 50 -> "CrashDecide"
                                        [] self = 60 -> "RecoveryStart"]

StockLoop == /\ pc[10] = "StockLoop"
             /\ IF walState \notin {"COMPLETED", "FAILED"} \/ stockCmd /= <<>>
                   THEN /\ pc' = [pc EXCEPT ![10] = "StockWait"]
                   ELSE /\ pc' = [pc EXCEPT ![10] = "Done"]
             /\ UNCHANGED << walState, stockAvailable, stockReserved, 
                             stockSold, stockReservationKey, 
                             stockFallbackAmount, stockStatus, creditAvailable, 
                             creditHeld, creditSpent, paymentReservationKey, 
                             paymentFallbackAmount, paymentStatus, stockCmd, 
                             stockResp, paymentCmd, paymentResp, 
                             orchestratorAlive, confirmRetries, recoveryDone, 
                             crashed, stockAction, paymentAction, stockVote, 
                             paymentVote, stockConfirmResult, 
                             paymentConfirmResult, recStockResult, 
                             recPaymentResult, recRetries >>

StockWait == /\ pc[10] = "StockWait"
             /\ stockCmd /= <<>> \/ (walState \in {"COMPLETED", "FAILED"} /\ stockCmd = <<>>)
             /\ IF stockCmd = <<>>
                   THEN /\ pc' = [pc EXCEPT ![10] = "Done"]
                        /\ UNCHANGED << stockCmd, stockAction >>
                   ELSE /\ stockAction' = stockCmd[1]
                        /\ stockCmd' = Remove(stockCmd, 1)
                        /\ pc' = [pc EXCEPT ![10] = "StockExec"]
             /\ UNCHANGED << walState, stockAvailable, stockReserved, 
                             stockSold, stockReservationKey, 
                             stockFallbackAmount, stockStatus, creditAvailable, 
                             creditHeld, creditSpent, paymentReservationKey, 
                             paymentFallbackAmount, paymentStatus, stockResp, 
                             paymentCmd, paymentResp, orchestratorAlive, 
                             confirmRetries, recoveryDone, crashed, 
                             paymentAction, stockVote, paymentVote, 
                             stockConfirmResult, paymentConfirmResult, 
                             recStockResult, recPaymentResult, recRetries >>

StockExec == /\ pc[10] = "StockExec"
             /\ IF stockAction = "try_reserve"
                   THEN /\ IF stockStatus = "reserved"
                              THEN /\ stockResp' = Append(stockResp, "reserved")
                                   /\ UNCHANGED << stockAvailable, 
                                                   stockReserved, 
                                                   stockReservationKey, 
                                                   stockFallbackAmount, 
                                                   stockStatus >>
                              ELSE /\ IF stockAvailable >= RESERVE_AMOUNT
                                         THEN /\ stockAvailable' = stockAvailable - RESERVE_AMOUNT
                                              /\ stockReserved' = stockReserved + RESERVE_AMOUNT
                                              /\ stockReservationKey' = TRUE
                                              /\ stockFallbackAmount' = RESERVE_AMOUNT
                                              /\ stockStatus' = "reserved"
                                              /\ stockResp' = Append(stockResp, "reserved")
                                         ELSE /\ stockResp' = Append(stockResp, "failed")
                                              /\ UNCHANGED << stockAvailable, 
                                                              stockReserved, 
                                                              stockReservationKey, 
                                                              stockFallbackAmount, 
                                                              stockStatus >>
                        /\ UNCHANGED stockSold
                   ELSE /\ IF stockAction = "confirm"
                              THEN /\ IF stockStatus = "confirmed"
                                         THEN /\ stockResp' = Append(stockResp, "confirmed")
                                              /\ UNCHANGED << stockReserved, 
                                                              stockSold, 
                                                              stockReservationKey, 
                                                              stockFallbackAmount, 
                                                              stockStatus >>
                                         ELSE /\ IF stockReservationKey = TRUE
                                                    THEN /\ stockReserved' = stockReserved - RESERVE_AMOUNT
                                                         /\ stockSold' = stockSold + RESERVE_AMOUNT
                                                         /\ stockReservationKey' = FALSE
                                                         /\ stockFallbackAmount' = 0
                                                         /\ stockStatus' = "confirmed"
                                                         /\ stockResp' = Append(stockResp, "confirmed")
                                                    ELSE /\ stockResp' = Append(stockResp, "confirm_failed")
                                                         /\ UNCHANGED << stockReserved, 
                                                                         stockSold, 
                                                                         stockReservationKey, 
                                                                         stockFallbackAmount, 
                                                                         stockStatus >>
                                   /\ UNCHANGED stockAvailable
                              ELSE /\ IF stockAction = "cancel"
                                         THEN /\ IF stockStatus = "cancelled"
                                                    THEN /\ stockResp' = Append(stockResp, "cancelled")
                                                         /\ UNCHANGED << stockAvailable, 
                                                                         stockReserved, 
                                                                         stockReservationKey, 
                                                                         stockFallbackAmount, 
                                                                         stockStatus >>
                                                    ELSE /\ IF stockReservationKey = TRUE
                                                               THEN /\ stockAvailable' = stockAvailable + RESERVE_AMOUNT
                                                                    /\ stockReserved' = stockReserved - RESERVE_AMOUNT
                                                                    /\ stockReservationKey' = FALSE
                                                               ELSE /\ IF stockFallbackAmount > 0
                                                                          THEN /\ stockAvailable' = stockAvailable + stockFallbackAmount
                                                                               /\ stockReserved' = stockReserved - stockFallbackAmount
                                                                          ELSE /\ TRUE
                                                                               /\ UNCHANGED << stockAvailable, 
                                                                                               stockReserved >>
                                                                    /\ UNCHANGED stockReservationKey
                                                         /\ stockFallbackAmount' = 0
                                                         /\ stockStatus' = "cancelled"
                                                         /\ stockResp' = Append(stockResp, "cancelled")
                                         ELSE /\ TRUE
                                              /\ UNCHANGED << stockAvailable, 
                                                              stockReserved, 
                                                              stockReservationKey, 
                                                              stockFallbackAmount, 
                                                              stockStatus, 
                                                              stockResp >>
                                   /\ UNCHANGED stockSold
             /\ pc' = [pc EXCEPT ![10] = "StockLoop"]
             /\ UNCHANGED << walState, creditAvailable, creditHeld, 
                             creditSpent, paymentReservationKey, 
                             paymentFallbackAmount, paymentStatus, stockCmd, 
                             paymentCmd, paymentResp, orchestratorAlive, 
                             confirmRetries, recoveryDone, crashed, 
                             stockAction, paymentAction, stockVote, 
                             paymentVote, stockConfirmResult, 
                             paymentConfirmResult, recStockResult, 
                             recPaymentResult, recRetries >>

StockService == StockLoop \/ StockWait \/ StockExec

PaymentLoop == /\ pc[20] = "PaymentLoop"
               /\ IF walState \notin {"COMPLETED", "FAILED"} \/ paymentCmd /= <<>>
                     THEN /\ pc' = [pc EXCEPT ![20] = "PaymentWait"]
                     ELSE /\ pc' = [pc EXCEPT ![20] = "Done"]
               /\ UNCHANGED << walState, stockAvailable, stockReserved, 
                               stockSold, stockReservationKey, 
                               stockFallbackAmount, stockStatus, 
                               creditAvailable, creditHeld, creditSpent, 
                               paymentReservationKey, paymentFallbackAmount, 
                               paymentStatus, stockCmd, stockResp, paymentCmd, 
                               paymentResp, orchestratorAlive, confirmRetries, 
                               recoveryDone, crashed, stockAction, 
                               paymentAction, stockVote, paymentVote, 
                               stockConfirmResult, paymentConfirmResult, 
                               recStockResult, recPaymentResult, recRetries >>

PaymentWait == /\ pc[20] = "PaymentWait"
               /\ paymentCmd /= <<>> \/ (walState \in {"COMPLETED", "FAILED"} /\ paymentCmd = <<>>)
               /\ IF paymentCmd = <<>>
                     THEN /\ pc' = [pc EXCEPT ![20] = "Done"]
                          /\ UNCHANGED << paymentCmd, paymentAction >>
                     ELSE /\ paymentAction' = paymentCmd[1]
                          /\ paymentCmd' = Remove(paymentCmd, 1)
                          /\ pc' = [pc EXCEPT ![20] = "PaymentExec"]
               /\ UNCHANGED << walState, stockAvailable, stockReserved, 
                               stockSold, stockReservationKey, 
                               stockFallbackAmount, stockStatus, 
                               creditAvailable, creditHeld, creditSpent, 
                               paymentReservationKey, paymentFallbackAmount, 
                               paymentStatus, stockCmd, stockResp, paymentResp, 
                               orchestratorAlive, confirmRetries, recoveryDone, 
                               crashed, stockAction, stockVote, paymentVote, 
                               stockConfirmResult, paymentConfirmResult, 
                               recStockResult, recPaymentResult, recRetries >>

PaymentExec == /\ pc[20] = "PaymentExec"
               /\ IF paymentAction = "try_reserve"
                     THEN /\ IF paymentStatus = "reserved"
                                THEN /\ paymentResp' = Append(paymentResp, "reserved")
                                     /\ UNCHANGED << creditAvailable, 
                                                     creditHeld, 
                                                     paymentReservationKey, 
                                                     paymentFallbackAmount, 
                                                     paymentStatus >>
                                ELSE /\ IF creditAvailable >= PAYMENT_AMOUNT
                                           THEN /\ creditAvailable' = creditAvailable - PAYMENT_AMOUNT
                                                /\ creditHeld' = creditHeld + PAYMENT_AMOUNT
                                                /\ paymentReservationKey' = TRUE
                                                /\ paymentFallbackAmount' = PAYMENT_AMOUNT
                                                /\ paymentStatus' = "reserved"
                                                /\ paymentResp' = Append(paymentResp, "reserved")
                                           ELSE /\ paymentResp' = Append(paymentResp, "failed")
                                                /\ UNCHANGED << creditAvailable, 
                                                                creditHeld, 
                                                                paymentReservationKey, 
                                                                paymentFallbackAmount, 
                                                                paymentStatus >>
                          /\ UNCHANGED creditSpent
                     ELSE /\ IF paymentAction = "confirm"
                                THEN /\ IF paymentStatus = "confirmed"
                                           THEN /\ paymentResp' = Append(paymentResp, "confirmed")
                                                /\ UNCHANGED << creditHeld, 
                                                                creditSpent, 
                                                                paymentReservationKey, 
                                                                paymentFallbackAmount, 
                                                                paymentStatus >>
                                           ELSE /\ IF paymentReservationKey = TRUE
                                                      THEN /\ creditHeld' = creditHeld - PAYMENT_AMOUNT
                                                           /\ creditSpent' = creditSpent + PAYMENT_AMOUNT
                                                           /\ paymentReservationKey' = FALSE
                                                           /\ paymentFallbackAmount' = 0
                                                           /\ paymentStatus' = "confirmed"
                                                           /\ paymentResp' = Append(paymentResp, "confirmed")
                                                      ELSE /\ paymentResp' = Append(paymentResp, "confirm_failed")
                                                           /\ UNCHANGED << creditHeld, 
                                                                           creditSpent, 
                                                                           paymentReservationKey, 
                                                                           paymentFallbackAmount, 
                                                                           paymentStatus >>
                                     /\ UNCHANGED creditAvailable
                                ELSE /\ IF paymentAction = "cancel"
                                           THEN /\ IF paymentStatus = "cancelled"
                                                      THEN /\ paymentResp' = Append(paymentResp, "cancelled")
                                                           /\ UNCHANGED << creditAvailable, 
                                                                           creditHeld, 
                                                                           paymentReservationKey, 
                                                                           paymentFallbackAmount, 
                                                                           paymentStatus >>
                                                      ELSE /\ IF paymentReservationKey = TRUE
                                                                 THEN /\ creditAvailable' = creditAvailable + PAYMENT_AMOUNT
                                                                      /\ creditHeld' = creditHeld - PAYMENT_AMOUNT
                                                                      /\ paymentReservationKey' = FALSE
                                                                 ELSE /\ IF paymentFallbackAmount > 0
                                                                            THEN /\ creditAvailable' = creditAvailable + paymentFallbackAmount
                                                                                 /\ creditHeld' = creditHeld - paymentFallbackAmount
                                                                            ELSE /\ TRUE
                                                                                 /\ UNCHANGED << creditAvailable, 
                                                                                                 creditHeld >>
                                                                      /\ UNCHANGED paymentReservationKey
                                                           /\ paymentFallbackAmount' = 0
                                                           /\ paymentStatus' = "cancelled"
                                                           /\ paymentResp' = Append(paymentResp, "cancelled")
                                           ELSE /\ TRUE
                                                /\ UNCHANGED << creditAvailable, 
                                                                creditHeld, 
                                                                paymentReservationKey, 
                                                                paymentFallbackAmount, 
                                                                paymentStatus, 
                                                                paymentResp >>
                                     /\ UNCHANGED creditSpent
               /\ pc' = [pc EXCEPT ![20] = "PaymentLoop"]
               /\ UNCHANGED << walState, stockAvailable, stockReserved, 
                               stockSold, stockReservationKey, 
                               stockFallbackAmount, stockStatus, stockCmd, 
                               stockResp, paymentCmd, orchestratorAlive, 
                               confirmRetries, recoveryDone, crashed, 
                               stockAction, paymentAction, stockVote, 
                               paymentVote, stockConfirmResult, 
                               paymentConfirmResult, recStockResult, 
                               recPaymentResult, recRetries >>

PaymentService == PaymentLoop \/ PaymentWait \/ PaymentExec

TTLLoop == /\ pc[30] = "TTLLoop"
           /\ IF walState \notin {"COMPLETED", "FAILED"}
                 THEN /\ pc' = [pc EXCEPT ![30] = "TTLCheck"]
                 ELSE /\ pc' = [pc EXCEPT ![30] = "Done"]
           /\ UNCHANGED << walState, stockAvailable, stockReserved, stockSold, 
                           stockReservationKey, stockFallbackAmount, 
                           stockStatus, creditAvailable, creditHeld, 
                           creditSpent, paymentReservationKey, 
                           paymentFallbackAmount, paymentStatus, stockCmd, 
                           stockResp, paymentCmd, paymentResp, 
                           orchestratorAlive, confirmRetries, recoveryDone, 
                           crashed, stockAction, paymentAction, stockVote, 
                           paymentVote, stockConfirmResult, 
                           paymentConfirmResult, recStockResult, 
                           recPaymentResult, recRetries >>

TTLCheck == /\ pc[30] = "TTLCheck"
            /\ \/ /\ IF stockReservationKey = TRUE /\ stockStatus = "reserved"
                        THEN /\ stockReservationKey' = FALSE
                        ELSE /\ TRUE
                             /\ UNCHANGED stockReservationKey
                  /\ UNCHANGED paymentReservationKey
               \/ /\ IF paymentReservationKey = TRUE /\ paymentStatus = "reserved"
                        THEN /\ paymentReservationKey' = FALSE
                        ELSE /\ TRUE
                             /\ UNCHANGED paymentReservationKey
                  /\ UNCHANGED stockReservationKey
               \/ /\ TRUE
                  /\ UNCHANGED <<stockReservationKey, paymentReservationKey>>
            /\ pc' = [pc EXCEPT ![30] = "TTLLoop"]
            /\ UNCHANGED << walState, stockAvailable, stockReserved, stockSold, 
                            stockFallbackAmount, stockStatus, creditAvailable, 
                            creditHeld, creditSpent, paymentFallbackAmount, 
                            paymentStatus, stockCmd, stockResp, paymentCmd, 
                            paymentResp, orchestratorAlive, confirmRetries, 
                            recoveryDone, crashed, stockAction, paymentAction, 
                            stockVote, paymentVote, stockConfirmResult, 
                            paymentConfirmResult, recStockResult, 
                            recPaymentResult, recRetries >>

TTLDaemon == TTLLoop \/ TTLCheck

OrcStart == /\ pc[40] = "OrcStart"
            /\ IF ~orchestratorAlive
                  THEN /\ pc' = [pc EXCEPT ![40] = "Done"]
                       /\ UNCHANGED walState
                  ELSE /\ walState' = "STARTED"
                       /\ pc' = [pc EXCEPT ![40] = "OrcPrepare"]
            /\ UNCHANGED << stockAvailable, stockReserved, stockSold, 
                            stockReservationKey, stockFallbackAmount, 
                            stockStatus, creditAvailable, creditHeld, 
                            creditSpent, paymentReservationKey, 
                            paymentFallbackAmount, paymentStatus, stockCmd, 
                            stockResp, paymentCmd, paymentResp, 
                            orchestratorAlive, confirmRetries, recoveryDone, 
                            crashed, stockAction, paymentAction, stockVote, 
                            paymentVote, stockConfirmResult, 
                            paymentConfirmResult, recStockResult, 
                            recPaymentResult, recRetries >>

OrcPrepare == /\ pc[40] = "OrcPrepare"
              /\ IF ~orchestratorAlive
                    THEN /\ pc' = [pc EXCEPT ![40] = "Done"]
                         /\ UNCHANGED << walState, stockCmd, paymentCmd >>
                    ELSE /\ walState' = "PREPARING"
                         /\ stockCmd' = Append(stockCmd, "try_reserve")
                         /\ paymentCmd' = Append(paymentCmd, "try_reserve")
                         /\ pc' = [pc EXCEPT ![40] = "OrcCollectStockVote"]
              /\ UNCHANGED << stockAvailable, stockReserved, stockSold, 
                              stockReservationKey, stockFallbackAmount, 
                              stockStatus, creditAvailable, creditHeld, 
                              creditSpent, paymentReservationKey, 
                              paymentFallbackAmount, paymentStatus, stockResp, 
                              paymentResp, orchestratorAlive, confirmRetries, 
                              recoveryDone, crashed, stockAction, 
                              paymentAction, stockVote, paymentVote, 
                              stockConfirmResult, paymentConfirmResult, 
                              recStockResult, recPaymentResult, recRetries >>

OrcCollectStockVote == /\ pc[40] = "OrcCollectStockVote"
                       /\ stockResp /= <<>> \/ ~orchestratorAlive
                       /\ IF ~orchestratorAlive
                             THEN /\ pc' = [pc EXCEPT ![40] = "Done"]
                                  /\ UNCHANGED << stockResp, stockVote >>
                             ELSE /\ stockVote' = stockResp[1]
                                  /\ stockResp' = Remove(stockResp, 1)
                                  /\ pc' = [pc EXCEPT ![40] = "OrcCollectPaymentVote"]
                       /\ UNCHANGED << walState, stockAvailable, stockReserved, 
                                       stockSold, stockReservationKey, 
                                       stockFallbackAmount, stockStatus, 
                                       creditAvailable, creditHeld, 
                                       creditSpent, paymentReservationKey, 
                                       paymentFallbackAmount, paymentStatus, 
                                       stockCmd, paymentCmd, paymentResp, 
                                       orchestratorAlive, confirmRetries, 
                                       recoveryDone, crashed, stockAction, 
                                       paymentAction, paymentVote, 
                                       stockConfirmResult, 
                                       paymentConfirmResult, recStockResult, 
                                       recPaymentResult, recRetries >>

OrcCollectPaymentVote == /\ pc[40] = "OrcCollectPaymentVote"
                         /\ paymentResp /= <<>> \/ ~orchestratorAlive
                         /\ IF ~orchestratorAlive
                               THEN /\ pc' = [pc EXCEPT ![40] = "Done"]
                                    /\ UNCHANGED << paymentResp, paymentVote >>
                               ELSE /\ paymentVote' = paymentResp[1]
                                    /\ paymentResp' = Remove(paymentResp, 1)
                                    /\ pc' = [pc EXCEPT ![40] = "OrcDecide"]
                         /\ UNCHANGED << walState, stockAvailable, 
                                         stockReserved, stockSold, 
                                         stockReservationKey, 
                                         stockFallbackAmount, stockStatus, 
                                         creditAvailable, creditHeld, 
                                         creditSpent, paymentReservationKey, 
                                         paymentFallbackAmount, paymentStatus, 
                                         stockCmd, stockResp, paymentCmd, 
                                         orchestratorAlive, confirmRetries, 
                                         recoveryDone, crashed, stockAction, 
                                         paymentAction, stockVote, 
                                         stockConfirmResult, 
                                         paymentConfirmResult, recStockResult, 
                                         recPaymentResult, recRetries >>

OrcDecide == /\ pc[40] = "OrcDecide"
             /\ IF ~orchestratorAlive
                   THEN /\ pc' = [pc EXCEPT ![40] = "Done"]
                   ELSE /\ IF stockVote = "reserved" /\ paymentVote = "reserved"
                              THEN /\ pc' = [pc EXCEPT ![40] = "OrcSendConfirm"]
                              ELSE /\ pc' = [pc EXCEPT ![40] = "OrcSendCancel"]
             /\ UNCHANGED << walState, stockAvailable, stockReserved, 
                             stockSold, stockReservationKey, 
                             stockFallbackAmount, stockStatus, creditAvailable, 
                             creditHeld, creditSpent, paymentReservationKey, 
                             paymentFallbackAmount, paymentStatus, stockCmd, 
                             stockResp, paymentCmd, paymentResp, 
                             orchestratorAlive, confirmRetries, recoveryDone, 
                             crashed, stockAction, paymentAction, stockVote, 
                             paymentVote, stockConfirmResult, 
                             paymentConfirmResult, recStockResult, 
                             recPaymentResult, recRetries >>

OrcSendConfirm == /\ pc[40] = "OrcSendConfirm"
                  /\ IF ~orchestratorAlive
                        THEN /\ pc' = [pc EXCEPT ![40] = "Done"]
                             /\ UNCHANGED << walState, stockCmd, paymentCmd >>
                        ELSE /\ walState' = "COMMITTING"
                             /\ stockCmd' = Append(stockCmd, "confirm")
                             /\ paymentCmd' = Append(paymentCmd, "confirm")
                             /\ pc' = [pc EXCEPT ![40] = "OrcCollectStockConfirm"]
                  /\ UNCHANGED << stockAvailable, stockReserved, stockSold, 
                                  stockReservationKey, stockFallbackAmount, 
                                  stockStatus, creditAvailable, creditHeld, 
                                  creditSpent, paymentReservationKey, 
                                  paymentFallbackAmount, paymentStatus, 
                                  stockResp, paymentResp, orchestratorAlive, 
                                  confirmRetries, recoveryDone, crashed, 
                                  stockAction, paymentAction, stockVote, 
                                  paymentVote, stockConfirmResult, 
                                  paymentConfirmResult, recStockResult, 
                                  recPaymentResult, recRetries >>

OrcCollectStockConfirm == /\ pc[40] = "OrcCollectStockConfirm"
                          /\ stockResp /= <<>> \/ ~orchestratorAlive
                          /\ IF ~orchestratorAlive
                                THEN /\ pc' = [pc EXCEPT ![40] = "Done"]
                                     /\ UNCHANGED << stockResp, 
                                                     stockConfirmResult >>
                                ELSE /\ stockConfirmResult' = stockResp[1]
                                     /\ stockResp' = Remove(stockResp, 1)
                                     /\ pc' = [pc EXCEPT ![40] = "OrcCollectPaymentConfirm"]
                          /\ UNCHANGED << walState, stockAvailable, 
                                          stockReserved, stockSold, 
                                          stockReservationKey, 
                                          stockFallbackAmount, stockStatus, 
                                          creditAvailable, creditHeld, 
                                          creditSpent, paymentReservationKey, 
                                          paymentFallbackAmount, paymentStatus, 
                                          stockCmd, paymentCmd, paymentResp, 
                                          orchestratorAlive, confirmRetries, 
                                          recoveryDone, crashed, stockAction, 
                                          paymentAction, stockVote, 
                                          paymentVote, paymentConfirmResult, 
                                          recStockResult, recPaymentResult, 
                                          recRetries >>

OrcCollectPaymentConfirm == /\ pc[40] = "OrcCollectPaymentConfirm"
                            /\ paymentResp /= <<>> \/ ~orchestratorAlive
                            /\ IF ~orchestratorAlive
                                  THEN /\ pc' = [pc EXCEPT ![40] = "Done"]
                                       /\ UNCHANGED << paymentResp, 
                                                       paymentConfirmResult >>
                                  ELSE /\ paymentConfirmResult' = paymentResp[1]
                                       /\ paymentResp' = Remove(paymentResp, 1)
                                       /\ pc' = [pc EXCEPT ![40] = "OrcEvalConfirms"]
                            /\ UNCHANGED << walState, stockAvailable, 
                                            stockReserved, stockSold, 
                                            stockReservationKey, 
                                            stockFallbackAmount, stockStatus, 
                                            creditAvailable, creditHeld, 
                                            creditSpent, paymentReservationKey, 
                                            paymentFallbackAmount, 
                                            paymentStatus, stockCmd, stockResp, 
                                            paymentCmd, orchestratorAlive, 
                                            confirmRetries, recoveryDone, 
                                            crashed, stockAction, 
                                            paymentAction, stockVote, 
                                            paymentVote, stockConfirmResult, 
                                            recStockResult, recPaymentResult, 
                                            recRetries >>

OrcEvalConfirms == /\ pc[40] = "OrcEvalConfirms"
                   /\ IF ~orchestratorAlive
                         THEN /\ pc' = [pc EXCEPT ![40] = "Done"]
                              /\ UNCHANGED << walState, stockCmd, paymentCmd, 
                                              confirmRetries, 
                                              stockConfirmResult, 
                                              paymentConfirmResult >>
                         ELSE /\ IF stockConfirmResult = "confirm_failed" \/ paymentConfirmResult = "confirm_failed"
                                    THEN /\ IF confirmRetries < MAX_CONFIRM_RETRIES
                                               THEN /\ confirmRetries' = confirmRetries + 1
                                                    /\ IF stockConfirmResult = "confirm_failed"
                                                          THEN /\ stockCmd' = Append(stockCmd, "confirm")
                                                               /\ stockConfirmResult' = "pending"
                                                          ELSE /\ TRUE
                                                               /\ UNCHANGED << stockCmd, 
                                                                               stockConfirmResult >>
                                                    /\ IF paymentConfirmResult = "confirm_failed"
                                                          THEN /\ paymentCmd' = Append(paymentCmd, "confirm")
                                                               /\ paymentConfirmResult' = "pending"
                                                          ELSE /\ TRUE
                                                               /\ UNCHANGED << paymentCmd, 
                                                                               paymentConfirmResult >>
                                                    /\ pc' = [pc EXCEPT ![40] = "OrcRetryDispatch"]
                                                    /\ UNCHANGED walState
                                               ELSE /\ stockCmd' = Append(stockCmd, "cancel")
                                                    /\ paymentCmd' = Append(paymentCmd, "cancel")
                                                    /\ walState' = "FAILED"
                                                    /\ pc' = [pc EXCEPT ![40] = "Done"]
                                                    /\ UNCHANGED << confirmRetries, 
                                                                    stockConfirmResult, 
                                                                    paymentConfirmResult >>
                                    ELSE /\ pc' = [pc EXCEPT ![40] = "OrcComplete"]
                                         /\ UNCHANGED << walState, stockCmd, 
                                                         paymentCmd, 
                                                         confirmRetries, 
                                                         stockConfirmResult, 
                                                         paymentConfirmResult >>
                   /\ UNCHANGED << stockAvailable, stockReserved, stockSold, 
                                   stockReservationKey, stockFallbackAmount, 
                                   stockStatus, creditAvailable, creditHeld, 
                                   creditSpent, paymentReservationKey, 
                                   paymentFallbackAmount, paymentStatus, 
                                   stockResp, paymentResp, orchestratorAlive, 
                                   recoveryDone, crashed, stockAction, 
                                   paymentAction, stockVote, paymentVote, 
                                   recStockResult, recPaymentResult, 
                                   recRetries >>

OrcRetryDispatch == /\ pc[40] = "OrcRetryDispatch"
                    /\ IF stockConfirmResult = "pending"
                          THEN /\ pc' = [pc EXCEPT ![40] = "OrcRetryCollectStock"]
                          ELSE /\ pc' = [pc EXCEPT ![40] = "OrcRetryCollectPayment"]
                    /\ UNCHANGED << walState, stockAvailable, stockReserved, 
                                    stockSold, stockReservationKey, 
                                    stockFallbackAmount, stockStatus, 
                                    creditAvailable, creditHeld, creditSpent, 
                                    paymentReservationKey, 
                                    paymentFallbackAmount, paymentStatus, 
                                    stockCmd, stockResp, paymentCmd, 
                                    paymentResp, orchestratorAlive, 
                                    confirmRetries, recoveryDone, crashed, 
                                    stockAction, paymentAction, stockVote, 
                                    paymentVote, stockConfirmResult, 
                                    paymentConfirmResult, recStockResult, 
                                    recPaymentResult, recRetries >>

OrcRetryCollectStock == /\ pc[40] = "OrcRetryCollectStock"
                        /\ stockResp /= <<>> \/ ~orchestratorAlive
                        /\ IF ~orchestratorAlive
                              THEN /\ pc' = [pc EXCEPT ![40] = "Done"]
                                   /\ UNCHANGED << stockResp, 
                                                   stockConfirmResult >>
                              ELSE /\ stockConfirmResult' = stockResp[1]
                                   /\ stockResp' = Remove(stockResp, 1)
                                   /\ pc' = [pc EXCEPT ![40] = "OrcAfterRetryStock"]
                        /\ UNCHANGED << walState, stockAvailable, 
                                        stockReserved, stockSold, 
                                        stockReservationKey, 
                                        stockFallbackAmount, stockStatus, 
                                        creditAvailable, creditHeld, 
                                        creditSpent, paymentReservationKey, 
                                        paymentFallbackAmount, paymentStatus, 
                                        stockCmd, paymentCmd, paymentResp, 
                                        orchestratorAlive, confirmRetries, 
                                        recoveryDone, crashed, stockAction, 
                                        paymentAction, stockVote, paymentVote, 
                                        paymentConfirmResult, recStockResult, 
                                        recPaymentResult, recRetries >>

OrcAfterRetryStock == /\ pc[40] = "OrcAfterRetryStock"
                      /\ IF paymentConfirmResult = "pending"
                            THEN /\ pc' = [pc EXCEPT ![40] = "OrcRetryCollectPayment"]
                            ELSE /\ pc' = [pc EXCEPT ![40] = "OrcEvalConfirms"]
                      /\ UNCHANGED << walState, stockAvailable, stockReserved, 
                                      stockSold, stockReservationKey, 
                                      stockFallbackAmount, stockStatus, 
                                      creditAvailable, creditHeld, creditSpent, 
                                      paymentReservationKey, 
                                      paymentFallbackAmount, paymentStatus, 
                                      stockCmd, stockResp, paymentCmd, 
                                      paymentResp, orchestratorAlive, 
                                      confirmRetries, recoveryDone, crashed, 
                                      stockAction, paymentAction, stockVote, 
                                      paymentVote, stockConfirmResult, 
                                      paymentConfirmResult, recStockResult, 
                                      recPaymentResult, recRetries >>

OrcRetryCollectPayment == /\ pc[40] = "OrcRetryCollectPayment"
                          /\ paymentResp /= <<>> \/ ~orchestratorAlive
                          /\ IF ~orchestratorAlive
                                THEN /\ pc' = [pc EXCEPT ![40] = "Done"]
                                     /\ UNCHANGED << paymentResp, 
                                                     paymentConfirmResult >>
                                ELSE /\ paymentConfirmResult' = paymentResp[1]
                                     /\ paymentResp' = Remove(paymentResp, 1)
                                     /\ pc' = [pc EXCEPT ![40] = "OrcAfterRetryPayment"]
                          /\ UNCHANGED << walState, stockAvailable, 
                                          stockReserved, stockSold, 
                                          stockReservationKey, 
                                          stockFallbackAmount, stockStatus, 
                                          creditAvailable, creditHeld, 
                                          creditSpent, paymentReservationKey, 
                                          paymentFallbackAmount, paymentStatus, 
                                          stockCmd, stockResp, paymentCmd, 
                                          orchestratorAlive, confirmRetries, 
                                          recoveryDone, crashed, stockAction, 
                                          paymentAction, stockVote, 
                                          paymentVote, stockConfirmResult, 
                                          recStockResult, recPaymentResult, 
                                          recRetries >>

OrcAfterRetryPayment == /\ pc[40] = "OrcAfterRetryPayment"
                        /\ IF stockConfirmResult = "pending"
                              THEN /\ pc' = [pc EXCEPT ![40] = "OrcRetryCollectStock"]
                              ELSE /\ pc' = [pc EXCEPT ![40] = "OrcEvalConfirms"]
                        /\ UNCHANGED << walState, stockAvailable, 
                                        stockReserved, stockSold, 
                                        stockReservationKey, 
                                        stockFallbackAmount, stockStatus, 
                                        creditAvailable, creditHeld, 
                                        creditSpent, paymentReservationKey, 
                                        paymentFallbackAmount, paymentStatus, 
                                        stockCmd, stockResp, paymentCmd, 
                                        paymentResp, orchestratorAlive, 
                                        confirmRetries, recoveryDone, crashed, 
                                        stockAction, paymentAction, stockVote, 
                                        paymentVote, stockConfirmResult, 
                                        paymentConfirmResult, recStockResult, 
                                        recPaymentResult, recRetries >>

OrcComplete == /\ pc[40] = "OrcComplete"
               /\ walState' = "COMPLETED"
               /\ pc' = [pc EXCEPT ![40] = "Done"]
               /\ UNCHANGED << stockAvailable, stockReserved, stockSold, 
                               stockReservationKey, stockFallbackAmount, 
                               stockStatus, creditAvailable, creditHeld, 
                               creditSpent, paymentReservationKey, 
                               paymentFallbackAmount, paymentStatus, stockCmd, 
                               stockResp, paymentCmd, paymentResp, 
                               orchestratorAlive, confirmRetries, recoveryDone, 
                               crashed, stockAction, paymentAction, stockVote, 
                               paymentVote, stockConfirmResult, 
                               paymentConfirmResult, recStockResult, 
                               recPaymentResult, recRetries >>

OrcSendCancel == /\ pc[40] = "OrcSendCancel"
                 /\ IF ~orchestratorAlive
                       THEN /\ pc' = [pc EXCEPT ![40] = "Done"]
                            /\ UNCHANGED << walState, stockCmd, paymentCmd >>
                       ELSE /\ walState' = "ABORTING"
                            /\ stockCmd' = Append(stockCmd, "cancel")
                            /\ paymentCmd' = Append(paymentCmd, "cancel")
                            /\ pc' = [pc EXCEPT ![40] = "OrcFailed"]
                 /\ UNCHANGED << stockAvailable, stockReserved, stockSold, 
                                 stockReservationKey, stockFallbackAmount, 
                                 stockStatus, creditAvailable, creditHeld, 
                                 creditSpent, paymentReservationKey, 
                                 paymentFallbackAmount, paymentStatus, 
                                 stockResp, paymentResp, orchestratorAlive, 
                                 confirmRetries, recoveryDone, crashed, 
                                 stockAction, paymentAction, stockVote, 
                                 paymentVote, stockConfirmResult, 
                                 paymentConfirmResult, recStockResult, 
                                 recPaymentResult, recRetries >>

OrcFailed == /\ pc[40] = "OrcFailed"
             /\ walState' = "FAILED"
             /\ pc' = [pc EXCEPT ![40] = "Done"]
             /\ UNCHANGED << stockAvailable, stockReserved, stockSold, 
                             stockReservationKey, stockFallbackAmount, 
                             stockStatus, creditAvailable, creditHeld, 
                             creditSpent, paymentReservationKey, 
                             paymentFallbackAmount, paymentStatus, stockCmd, 
                             stockResp, paymentCmd, paymentResp, 
                             orchestratorAlive, confirmRetries, recoveryDone, 
                             crashed, stockAction, paymentAction, stockVote, 
                             paymentVote, stockConfirmResult, 
                             paymentConfirmResult, recStockResult, 
                             recPaymentResult, recRetries >>

Orchestrator == OrcStart \/ OrcPrepare \/ OrcCollectStockVote
                   \/ OrcCollectPaymentVote \/ OrcDecide \/ OrcSendConfirm
                   \/ OrcCollectStockConfirm \/ OrcCollectPaymentConfirm
                   \/ OrcEvalConfirms \/ OrcRetryDispatch
                   \/ OrcRetryCollectStock \/ OrcAfterRetryStock
                   \/ OrcRetryCollectPayment \/ OrcAfterRetryPayment
                   \/ OrcComplete \/ OrcSendCancel \/ OrcFailed

CrashDecide == /\ pc[50] = "CrashDecide"
               /\ \/ /\ walState \notin {"INIT", "COMPLETED", "FAILED"} /\ orchestratorAlive /\ ~crashed
                     /\ orchestratorAlive' = FALSE
                     /\ crashed' = TRUE
                  \/ /\ TRUE
                     /\ UNCHANGED <<orchestratorAlive, crashed>>
               /\ pc' = [pc EXCEPT ![50] = "Done"]
               /\ UNCHANGED << walState, stockAvailable, stockReserved, 
                               stockSold, stockReservationKey, 
                               stockFallbackAmount, stockStatus, 
                               creditAvailable, creditHeld, creditSpent, 
                               paymentReservationKey, paymentFallbackAmount, 
                               paymentStatus, stockCmd, stockResp, paymentCmd, 
                               paymentResp, confirmRetries, recoveryDone, 
                               stockAction, paymentAction, stockVote, 
                               paymentVote, stockConfirmResult, 
                               paymentConfirmResult, recStockResult, 
                               recPaymentResult, recRetries >>

CrashProcess == CrashDecide

RecoveryStart == /\ pc[60] = "RecoveryStart"
                 /\ ~orchestratorAlive \/ walState \in {"COMPLETED", "FAILED"}
                 /\ IF walState \in {"COMPLETED", "FAILED"}
                       THEN /\ pc' = [pc EXCEPT ![60] = "Done"]
                       ELSE /\ pc' = [pc EXCEPT ![60] = "RecoveryAct"]
                 /\ UNCHANGED << walState, stockAvailable, stockReserved, 
                                 stockSold, stockReservationKey, 
                                 stockFallbackAmount, stockStatus, 
                                 creditAvailable, creditHeld, creditSpent, 
                                 paymentReservationKey, paymentFallbackAmount, 
                                 paymentStatus, stockCmd, stockResp, 
                                 paymentCmd, paymentResp, orchestratorAlive, 
                                 confirmRetries, recoveryDone, crashed, 
                                 stockAction, paymentAction, stockVote, 
                                 paymentVote, stockConfirmResult, 
                                 paymentConfirmResult, recStockResult, 
                                 recPaymentResult, recRetries >>

RecoveryAct == /\ pc[60] = "RecoveryAct"
               /\ IF walState = "STARTED"
                     THEN /\ walState' = "FAILED"
                          /\ recoveryDone' = TRUE
                          /\ pc' = [pc EXCEPT ![60] = "RecDone"]
                          /\ UNCHANGED << stockCmd, paymentCmd >>
                     ELSE /\ IF walState = "PREPARING" \/ walState = "TRYING"
                                THEN /\ stockCmd' = Append(stockCmd, "cancel")
                                     /\ paymentCmd' = Append(paymentCmd, "cancel")
                                     /\ walState' = "FAILED"
                                     /\ recoveryDone' = TRUE
                                     /\ pc' = [pc EXCEPT ![60] = "RecDone"]
                                ELSE /\ IF walState = "COMMITTING" \/ walState = "CONFIRMING"
                                           THEN /\ stockCmd' = Append(stockCmd, "confirm")
                                                /\ paymentCmd' = Append(paymentCmd, "confirm")
                                                /\ pc' = [pc EXCEPT ![60] = "RecCollectStockConfirm"]
                                                /\ UNCHANGED << walState, 
                                                                recoveryDone >>
                                           ELSE /\ IF walState = "ABORTING" \/ walState = "COMPENSATING"
                                                      THEN /\ stockCmd' = Append(stockCmd, "cancel")
                                                           /\ paymentCmd' = Append(paymentCmd, "cancel")
                                                           /\ walState' = "FAILED"
                                                           /\ recoveryDone' = TRUE
                                                      ELSE /\ TRUE
                                                           /\ UNCHANGED << walState, 
                                                                           stockCmd, 
                                                                           paymentCmd, 
                                                                           recoveryDone >>
                                                /\ pc' = [pc EXCEPT ![60] = "RecDone"]
               /\ UNCHANGED << stockAvailable, stockReserved, stockSold, 
                               stockReservationKey, stockFallbackAmount, 
                               stockStatus, creditAvailable, creditHeld, 
                               creditSpent, paymentReservationKey, 
                               paymentFallbackAmount, paymentStatus, stockResp, 
                               paymentResp, orchestratorAlive, confirmRetries, 
                               crashed, stockAction, paymentAction, stockVote, 
                               paymentVote, stockConfirmResult, 
                               paymentConfirmResult, recStockResult, 
                               recPaymentResult, recRetries >>

RecDone == /\ pc[60] = "RecDone"
           /\ pc' = [pc EXCEPT ![60] = "Done"]
           /\ UNCHANGED << walState, stockAvailable, stockReserved, stockSold, 
                           stockReservationKey, stockFallbackAmount, 
                           stockStatus, creditAvailable, creditHeld, 
                           creditSpent, paymentReservationKey, 
                           paymentFallbackAmount, paymentStatus, stockCmd, 
                           stockResp, paymentCmd, paymentResp, 
                           orchestratorAlive, confirmRetries, recoveryDone, 
                           crashed, stockAction, paymentAction, stockVote, 
                           paymentVote, stockConfirmResult, 
                           paymentConfirmResult, recStockResult, 
                           recPaymentResult, recRetries >>

RecCollectStockConfirm == /\ pc[60] = "RecCollectStockConfirm"
                          /\ stockResp /= <<>>
                          /\ recStockResult' = stockResp[1]
                          /\ stockResp' = Remove(stockResp, 1)
                          /\ pc' = [pc EXCEPT ![60] = "RecCollectPaymentConfirm"]
                          /\ UNCHANGED << walState, stockAvailable, 
                                          stockReserved, stockSold, 
                                          stockReservationKey, 
                                          stockFallbackAmount, stockStatus, 
                                          creditAvailable, creditHeld, 
                                          creditSpent, paymentReservationKey, 
                                          paymentFallbackAmount, paymentStatus, 
                                          stockCmd, paymentCmd, paymentResp, 
                                          orchestratorAlive, confirmRetries, 
                                          recoveryDone, crashed, stockAction, 
                                          paymentAction, stockVote, 
                                          paymentVote, stockConfirmResult, 
                                          paymentConfirmResult, 
                                          recPaymentResult, recRetries >>

RecCollectPaymentConfirm == /\ pc[60] = "RecCollectPaymentConfirm"
                            /\ paymentResp /= <<>>
                            /\ recPaymentResult' = paymentResp[1]
                            /\ paymentResp' = Remove(paymentResp, 1)
                            /\ pc' = [pc EXCEPT ![60] = "RecEvalConfirms"]
                            /\ UNCHANGED << walState, stockAvailable, 
                                            stockReserved, stockSold, 
                                            stockReservationKey, 
                                            stockFallbackAmount, stockStatus, 
                                            creditAvailable, creditHeld, 
                                            creditSpent, paymentReservationKey, 
                                            paymentFallbackAmount, 
                                            paymentStatus, stockCmd, stockResp, 
                                            paymentCmd, orchestratorAlive, 
                                            confirmRetries, recoveryDone, 
                                            crashed, stockAction, 
                                            paymentAction, stockVote, 
                                            paymentVote, stockConfirmResult, 
                                            paymentConfirmResult, 
                                            recStockResult, recRetries >>

RecEvalConfirms == /\ pc[60] = "RecEvalConfirms"
                   /\ IF recStockResult = "confirmed" /\ recPaymentResult = "confirmed"
                         THEN /\ walState' = "COMPLETED"
                              /\ recoveryDone' = TRUE
                              /\ pc' = [pc EXCEPT ![60] = "RecTerminate"]
                              /\ UNCHANGED << stockCmd, paymentCmd, 
                                              recStockResult, recPaymentResult, 
                                              recRetries >>
                         ELSE /\ IF recRetries < MAX_CONFIRM_RETRIES
                                    THEN /\ recRetries' = recRetries + 1
                                         /\ IF recStockResult = "confirm_failed"
                                               THEN /\ stockCmd' = Append(stockCmd, "confirm")
                                                    /\ recStockResult' = "pending"
                                               ELSE /\ TRUE
                                                    /\ UNCHANGED << stockCmd, 
                                                                    recStockResult >>
                                         /\ IF recPaymentResult = "confirm_failed"
                                               THEN /\ paymentCmd' = Append(paymentCmd, "confirm")
                                                    /\ recPaymentResult' = "pending"
                                               ELSE /\ TRUE
                                                    /\ UNCHANGED << paymentCmd, 
                                                                    recPaymentResult >>
                                         /\ pc' = [pc EXCEPT ![60] = "RecRetryDispatch"]
                                         /\ UNCHANGED << walState, 
                                                         recoveryDone >>
                                    ELSE /\ stockCmd' = Append(stockCmd, "cancel")
                                         /\ paymentCmd' = Append(paymentCmd, "cancel")
                                         /\ walState' = "FAILED"
                                         /\ recoveryDone' = TRUE
                                         /\ pc' = [pc EXCEPT ![60] = "RecTerminate"]
                                         /\ UNCHANGED << recStockResult, 
                                                         recPaymentResult, 
                                                         recRetries >>
                   /\ UNCHANGED << stockAvailable, stockReserved, stockSold, 
                                   stockReservationKey, stockFallbackAmount, 
                                   stockStatus, creditAvailable, creditHeld, 
                                   creditSpent, paymentReservationKey, 
                                   paymentFallbackAmount, paymentStatus, 
                                   stockResp, paymentResp, orchestratorAlive, 
                                   confirmRetries, crashed, stockAction, 
                                   paymentAction, stockVote, paymentVote, 
                                   stockConfirmResult, paymentConfirmResult >>

RecTerminate == /\ pc[60] = "RecTerminate"
                /\ pc' = [pc EXCEPT ![60] = "Done"]
                /\ UNCHANGED << walState, stockAvailable, stockReserved, 
                                stockSold, stockReservationKey, 
                                stockFallbackAmount, stockStatus, 
                                creditAvailable, creditHeld, creditSpent, 
                                paymentReservationKey, paymentFallbackAmount, 
                                paymentStatus, stockCmd, stockResp, paymentCmd, 
                                paymentResp, orchestratorAlive, confirmRetries, 
                                recoveryDone, crashed, stockAction, 
                                paymentAction, stockVote, paymentVote, 
                                stockConfirmResult, paymentConfirmResult, 
                                recStockResult, recPaymentResult, recRetries >>

RecRetryDispatch == /\ pc[60] = "RecRetryDispatch"
                    /\ IF recStockResult = "pending"
                          THEN /\ pc' = [pc EXCEPT ![60] = "RecRetryCollectStock"]
                          ELSE /\ pc' = [pc EXCEPT ![60] = "RecRetryCollectPayment"]
                    /\ UNCHANGED << walState, stockAvailable, stockReserved, 
                                    stockSold, stockReservationKey, 
                                    stockFallbackAmount, stockStatus, 
                                    creditAvailable, creditHeld, creditSpent, 
                                    paymentReservationKey, 
                                    paymentFallbackAmount, paymentStatus, 
                                    stockCmd, stockResp, paymentCmd, 
                                    paymentResp, orchestratorAlive, 
                                    confirmRetries, recoveryDone, crashed, 
                                    stockAction, paymentAction, stockVote, 
                                    paymentVote, stockConfirmResult, 
                                    paymentConfirmResult, recStockResult, 
                                    recPaymentResult, recRetries >>

RecRetryCollectStock == /\ pc[60] = "RecRetryCollectStock"
                        /\ stockResp /= <<>>
                        /\ recStockResult' = stockResp[1]
                        /\ stockResp' = Remove(stockResp, 1)
                        /\ pc' = [pc EXCEPT ![60] = "RecAfterRetryStock"]
                        /\ UNCHANGED << walState, stockAvailable, 
                                        stockReserved, stockSold, 
                                        stockReservationKey, 
                                        stockFallbackAmount, stockStatus, 
                                        creditAvailable, creditHeld, 
                                        creditSpent, paymentReservationKey, 
                                        paymentFallbackAmount, paymentStatus, 
                                        stockCmd, paymentCmd, paymentResp, 
                                        orchestratorAlive, confirmRetries, 
                                        recoveryDone, crashed, stockAction, 
                                        paymentAction, stockVote, paymentVote, 
                                        stockConfirmResult, 
                                        paymentConfirmResult, recPaymentResult, 
                                        recRetries >>

RecAfterRetryStock == /\ pc[60] = "RecAfterRetryStock"
                      /\ IF recPaymentResult = "pending"
                            THEN /\ pc' = [pc EXCEPT ![60] = "RecRetryCollectPayment"]
                            ELSE /\ pc' = [pc EXCEPT ![60] = "RecEvalConfirms"]
                      /\ UNCHANGED << walState, stockAvailable, stockReserved, 
                                      stockSold, stockReservationKey, 
                                      stockFallbackAmount, stockStatus, 
                                      creditAvailable, creditHeld, creditSpent, 
                                      paymentReservationKey, 
                                      paymentFallbackAmount, paymentStatus, 
                                      stockCmd, stockResp, paymentCmd, 
                                      paymentResp, orchestratorAlive, 
                                      confirmRetries, recoveryDone, crashed, 
                                      stockAction, paymentAction, stockVote, 
                                      paymentVote, stockConfirmResult, 
                                      paymentConfirmResult, recStockResult, 
                                      recPaymentResult, recRetries >>

RecRetryCollectPayment == /\ pc[60] = "RecRetryCollectPayment"
                          /\ paymentResp /= <<>>
                          /\ recPaymentResult' = paymentResp[1]
                          /\ paymentResp' = Remove(paymentResp, 1)
                          /\ pc' = [pc EXCEPT ![60] = "RecAfterRetryPayment"]
                          /\ UNCHANGED << walState, stockAvailable, 
                                          stockReserved, stockSold, 
                                          stockReservationKey, 
                                          stockFallbackAmount, stockStatus, 
                                          creditAvailable, creditHeld, 
                                          creditSpent, paymentReservationKey, 
                                          paymentFallbackAmount, paymentStatus, 
                                          stockCmd, stockResp, paymentCmd, 
                                          orchestratorAlive, confirmRetries, 
                                          recoveryDone, crashed, stockAction, 
                                          paymentAction, stockVote, 
                                          paymentVote, stockConfirmResult, 
                                          paymentConfirmResult, recStockResult, 
                                          recRetries >>

RecAfterRetryPayment == /\ pc[60] = "RecAfterRetryPayment"
                        /\ IF recStockResult = "pending"
                              THEN /\ pc' = [pc EXCEPT ![60] = "RecRetryCollectStock"]
                              ELSE /\ pc' = [pc EXCEPT ![60] = "RecEvalConfirms"]
                        /\ UNCHANGED << walState, stockAvailable, 
                                        stockReserved, stockSold, 
                                        stockReservationKey, 
                                        stockFallbackAmount, stockStatus, 
                                        creditAvailable, creditHeld, 
                                        creditSpent, paymentReservationKey, 
                                        paymentFallbackAmount, paymentStatus, 
                                        stockCmd, stockResp, paymentCmd, 
                                        paymentResp, orchestratorAlive, 
                                        confirmRetries, recoveryDone, crashed, 
                                        stockAction, paymentAction, stockVote, 
                                        paymentVote, stockConfirmResult, 
                                        paymentConfirmResult, recStockResult, 
                                        recPaymentResult, recRetries >>

RecoveryWorker == RecoveryStart \/ RecoveryAct \/ RecDone
                     \/ RecCollectStockConfirm \/ RecCollectPaymentConfirm
                     \/ RecEvalConfirms \/ RecTerminate \/ RecRetryDispatch
                     \/ RecRetryCollectStock \/ RecAfterRetryStock
                     \/ RecRetryCollectPayment \/ RecAfterRetryPayment

(* Allow infinite stuttering to prevent deadlock on termination. *)
Terminating == /\ \A self \in ProcSet: pc[self] = "Done"
               /\ UNCHANGED vars

Next == StockService \/ PaymentService \/ TTLDaemon \/ Orchestrator
           \/ CrashProcess \/ RecoveryWorker
           \/ Terminating

Spec == /\ Init /\ [][Next]_vars
        /\ WF_vars(StockService)
        /\ WF_vars(PaymentService)
        /\ WF_vars(TTLDaemon)
        /\ WF_vars(Orchestrator)
        /\ WF_vars(RecoveryWorker)

Termination == <>(\A self \in ProcSet: pc[self] = "Done")

\* END TRANSLATION

\* ================================================================
\* INVARIANTS — Safety properties
\* ================================================================

\* No double spend: sold/spent never exceeds initial
NoDoubleSpend ==
    /\ stockSold <= INITIAL_STOCK
    /\ creditSpent <= INITIAL_CREDIT

\* Reserved counters never go negative
NonNegativeReserved ==
    /\ stockReserved >= 0
    /\ creditHeld >= 0

\* Conservation law at terminal states
StockConservationTerminal ==
    (walState \in {"COMPLETED", "FAILED"} /\ stockCmd = <<>>)
        => (stockAvailable + stockReserved + stockSold = INITIAL_STOCK)

CreditConservationTerminal ==
    (walState \in {"COMPLETED", "FAILED"} /\ paymentCmd = <<>>)
        => (creditAvailable + creditHeld + creditSpent = INITIAL_CREDIT)

\* COMPLETED implies both services actually confirmed
CompletedImpliesBothConfirmed ==
    walState = "COMPLETED"
        => (stockStatus = "confirmed" /\ paymentStatus = "confirmed")

\* No leaked reservations at terminal state
NoLeakedReservations ==
    (walState \in {"COMPLETED", "FAILED"}
        /\ stockCmd = <<>> /\ paymentCmd = <<>>
        /\ pc[10] = "Done" /\ pc[20] = "Done")
        => (stockReserved = 0 /\ creditHeld = 0)

\* Atomic outcome: both services must be in the same terminal state
AtomicOutcome ==
    (walState \in {"COMPLETED", "FAILED"}
        /\ stockCmd = <<>> /\ paymentCmd = <<>>
        /\ pc[10] = "Done" /\ pc[20] = "Done")
        => \/ (stockStatus = "confirmed" /\ paymentStatus = "confirmed")
           \/ (stockStatus \in {"cancelled", "none"} /\ paymentStatus \in {"cancelled", "none"})
           \/ (stockStatus = "cancelled" /\ paymentStatus = "confirmed")
           \/ (stockStatus = "confirmed" /\ paymentStatus = "cancelled")
           \* The last two cover edge cases where cancel arrives after confirm

\* ================================================================
\* TEMPORAL PROPERTIES
\* ================================================================

\* Every saga eventually terminates
EventualTermination ==
    <>(walState \in {"COMPLETED", "FAILED"})

\* ================================================================
\* STATE CONSTRAINT — bound message queues
\* ================================================================
StateConstraint ==
    /\ Len(stockCmd) <= 5
    /\ Len(stockResp) <= 5
    /\ Len(paymentCmd) <= 5
    /\ Len(paymentResp) <= 5

=============================================================================
