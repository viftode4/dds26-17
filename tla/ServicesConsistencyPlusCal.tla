---- MODULE ServicesConsistencyPlusCal ----
EXTENDS Naturals, Sequences, TLC

CONSTANT Mode, PPU, CheckoutAmount, InitialStock, InitialCredits

ASSUME Mode \in {"2pc", "saga"}

PAYMENT_STATUS == {"pending", "prepared", "completed", "failed", "compensated"}
STOCK_STATUS == {"pending", "prepared", "completed", "failed", "compensated"}

(*--algorithm ServicesConsistency
variables
    done = FALSE,
    userCredits = InitialCredits,
    userHeldCredits = 0,
    userPaidCredits = 0,
    stockAvailable = InitialStock,
    stockHeld = 0,
    stockSold = 0,
    paymentStatus = "pending",
    stockStatus = "pending",
    orchestratorTxP = <<>>,
    orchestratorTxS = <<>>,
    orchestratorRxP = <<>>,
    orchestratorRxS = <<>>;

define
    TypeInvariant ==
        /\ done \in BOOLEAN
        /\ paymentStatus \in PAYMENT_STATUS
        /\ stockStatus \in STOCK_STATUS
        /\ userCredits \in Nat
        /\ userHeldCredits \in Nat
        /\ userPaidCredits \in Nat
        /\ stockAvailable \in Nat
        /\ stockHeld \in Nat
        /\ stockSold \in Nat
    
    UserCreditsConsistent == userCredits + userHeldCredits + userPaidCredits = InitialCredits
    StockConsistent == stockAvailable + stockHeld + stockSold = InitialStock

    EventuallyCompletes == <>(
        \/ paymentStatus = "completed" /\ stockStatus = "completed"
        \/ paymentStatus = "failed" /\ stockStatus \in {"failed", "compensated"}
        \/ stockStatus = "failed" /\ paymentStatus \in {"failed", "compensated"}
    )
end define;

fair process Orchestrator = 0
variables
    checkoutStatus = <<>>, message = "";
begin
Start:
    checkoutStatus := <<"notStarted", "notStarted">>;
    if Mode = "2pc" then
        goto Checkout2PC;
    else
        goto CheckoutSaga;
    end if;

Checkout2PC:
    goto CheckoutPayment2PC;
    CheckoutPayment2PC:
        orchestratorTxP := Append(orchestratorTxP, "prepare");
        checkoutStatus[1] := "started";
    CheckoutStock2PC:
        orchestratorTxS := Append(orchestratorTxS, "prepare");
        checkoutStatus[2] := "started";
    
    CheckoutProcess:
        if orchestratorRxP # <<>> then
            message := Head(orchestratorRxP);
            orchestratorRxP := Tail(orchestratorRxP);
            ProcessMessageCheckout:
                if message = "prepareOk" then
                    if checkoutStatus[1] = "started" then
                        checkoutStatus[1] := "prepared";
                    end if;
                elsif message = "prepareFailed" then
                    if checkoutStatus[1] = "started" then
                        checkoutStatus[1] := "failed";
                    end if;
                elsif message = "commitOk" then
                    if checkoutStatus[1] = "committing" then
                        checkoutStatus[1] := "committed";
                    end if;
                elsif message = "abortOk" then
                    if checkoutStatus[1] = "aborting" then
                        checkoutStatus[1] := "aborted";
                    end if;
                end if;
                goto CheckoutProcess;
        elsif orchestratorRxS # <<>> then
            message := Head(orchestratorRxS);
            orchestratorRxS := Tail(orchestratorRxS);
            ProcessMessageCheckoutStock:
                if message = "prepareOk" then
                    if checkoutStatus[2] = "started" then
                        checkoutStatus[2] := "prepared";
                    end if;
                elsif message = "prepareFailed" then
                    if checkoutStatus[2] = "started" then
                        checkoutStatus[2] := "failed";
                    end if;
                elsif message = "commitOk" then
                    if checkoutStatus[2] = "committing" then
                        checkoutStatus[2] := "committed";
                    end if;
                elsif message = "abortOk" then
                    if checkoutStatus[2] = "aborting" then
                        checkoutStatus[2] := "aborted";
                    end if;
                end if;
                goto CheckoutProcess;
        else
            if checkoutStatus = <<"prepared", "prepared">> then
                orchestratorTxP := Append(orchestratorTxP, "commit");
                orchestratorTxS := Append(orchestratorTxS, "commit");
                checkoutStatus := <<"committing", "committing">>;
                goto CheckoutProcess;
            elsif checkoutStatus[1] = "failed" /\ checkoutStatus[2] \in {"started", "prepared"} then
                orchestratorTxP := Append(orchestratorTxP, "abort");
                orchestratorTxS := Append(orchestratorTxS, "abort");
                checkoutStatus := <<"aborting", "aborting">>;
                goto CheckoutProcess;
            elsif checkoutStatus[2] = "failed" /\ checkoutStatus[1] \in {"started", "prepared"} then
                orchestratorTxP := Append(orchestratorTxP, "abort");
                orchestratorTxS := Append(orchestratorTxS, "abort");
                checkoutStatus := <<"aborting", "aborting">>;
                goto CheckoutProcess;
            elsif checkoutStatus = <<"committed", "committed">> then
                goto 2pcEnd;
            else
                goto CheckoutProcess;
            end if;
        end if;
    
    2pcEnd:
        done := TRUE;
        goto Done;

CheckoutSaga:
    goto CheckoutPaymentSaga;
    CheckoutPaymentSaga:
        orchestratorTxP := Append(orchestratorTxP, "checkout");
    CheckoutStockSaga:
        orchestratorTxS := Append(orchestratorTxS, "checkout");
    
    CheckoutSagaProcess:
        if orchestratorRxP # <<>> then
            message := Head(orchestratorRxP);
            orchestratorRxP := Tail(orchestratorRxP);

            if message = "checkoutOk" then
                checkoutStatus[1] := "completed";
            elsif message = "checkoutFailed" then
                checkoutStatus[1] := "failed";
            elsif message = "compensateOk" then
                if checkoutStatus[1] = "compensating" then
                    checkoutStatus[1] := "compensated";
                end if;
            end if;

            goto CheckoutSagaProcess;
        elsif orchestratorRxS # <<>> then
            message := Head(orchestratorRxS);
            orchestratorRxS := Tail(orchestratorRxS);

            if message = "checkoutOk" then
                checkoutStatus[2] := "completed";
            elsif message = "checkoutFailed" then
                checkoutStatus[2] := "failed";
            elsif message = "compensateOk" then
                if checkoutStatus[2] = "compensating" then
                    checkoutStatus[2] := "compensated";
                end if;
            end if;

            goto CheckoutSagaProcess;
        else
            if checkoutStatus = <<"completed", "completed">> then
                goto SagaEnd;
            elsif checkoutStatus[1] = "failed" /\ checkoutStatus[2] \in {"notStarted", "completed"} then
                orchestratorTxS := Append(orchestratorTxS, "compensate");
                checkoutStatus[2] := "compensating";
                goto CheckoutSagaProcess;
            elsif checkoutStatus[2] = "failed" /\ checkoutStatus[1] \in {"notStarted", "completed"} then
                orchestratorTxP := Append(orchestratorTxP, "compensate");
                checkoutStatus[1] := "compensating";
                goto CheckoutSagaProcess;
            elsif checkoutStatus[1] \in {"failed", "compensated"} /\ checkoutStatus[2] \in {"failed", "compensated"} then
                goto SagaEnd;
            else
                goto CheckoutSagaProcess;
            end if;
        end if;

    SagaEnd:
        done := TRUE;
        goto Done;
end process;

fair process PaymentWorker = 1
variables
    message = "";
begin
PaymentStart:
    await orchestratorTxP # <<>> \/ done;
    if done then
        goto Done;
    else
        message := Head(orchestratorTxP);
        orchestratorTxP := Tail(orchestratorTxP);
    end if;
PaymentReqProcessing:
    if message = "prepare" then
        if paymentStatus = "pending" then
            if userCredits >= CheckoutAmount * PPU then
                userCredits := userCredits - CheckoutAmount * PPU;
                userHeldCredits := userHeldCredits + CheckoutAmount * PPU;
                paymentStatus := "prepared";
                PrepareSuccessResponse:
                    orchestratorRxP := Append(orchestratorRxP, "prepareOk");
            else
                paymentStatus := "failed";
                PrepareFailedResponse:
                    orchestratorRxP := Append(orchestratorRxP, "prepareFailed");
            end if;
        elsif paymentStatus \in {"prepared", "completed"} then
            \* Idempotency
            orchestratorRxP := Append(orchestratorRxP, "prepareOk");
        end if;
    elsif message = "commit" then
        if paymentStatus = "prepared" then
            userHeldCredits := userHeldCredits - CheckoutAmount * PPU;
            userPaidCredits := userPaidCredits + CheckoutAmount * PPU;
            paymentStatus := "completed";
            ReplyCommitResponse:
                orchestratorRxP := Append(orchestratorRxP, "commitOk");
        elsif paymentStatus = "completed" then
            ReplyCommitResponse2:
                orchestratorRxP := Append(orchestratorRxP, "commitOk");
        end if;
    elsif message = "abort" then
        if paymentStatus = "prepared" then
            userHeldCredits := userHeldCredits - CheckoutAmount * PPU;
            userCredits := userCredits + CheckoutAmount * PPU;
            paymentStatus := "failed";
            ReplyAbortMessage:
                orchestratorRxP := Append(orchestratorRxP, "abortOk");
        elsif paymentStatus = "completed" then
            ReplyAbort2:
                orchestratorRxP := Append(orchestratorRxP, "abortOk");
        end if;
    elsif message = "checkout" then
        if paymentStatus = "pending" then
            if userCredits >= CheckoutAmount * PPU then
                userCredits := userCredits - CheckoutAmount * PPU;
                userPaidCredits := userPaidCredits + CheckoutAmount * PPU;
                paymentStatus := "completed";
                ReplyCheckoutResponse:
                    orchestratorRxP := Append(orchestratorRxP, "checkoutOk");
            else
                paymentStatus := "failed";
                FailedCheckoutResponse:
                    orchestratorRxP := Append(orchestratorRxP, "checkoutFailed");
            end if;
        elsif paymentStatus = "completed" then
            \* Idempotency
            orchestratorRxP := Append(orchestratorRxP, "checkoutOk");
        end if;
    elsif message = "compensate" then
        if paymentStatus = "completed" then
            userCredits := userCredits + CheckoutAmount * PPU;
            userPaidCredits := userPaidCredits - CheckoutAmount * PPU;
            paymentStatus := "compensated";
            ReplyCompensateResponse:
                orchestratorRxP := Append(orchestratorRxP, "compensateOk");
        elsif paymentStatus \in {"compensated", "failed"} then
            \* Idempotency
            orchestratorRxP := Append(orchestratorRxP, "compensateOk");
        end if;
    end if;
    step:
        goto PaymentStart;
end process;

fair process StockWorker = 2
variables
    message = "";
begin
StockStart:
    await orchestratorTxS # <<>> \/ done;
    if done then
        goto Done;
    else
        message := Head(orchestratorTxS);
        orchestratorTxS := Tail(orchestratorTxS);
    end if;
StockReqProcessing:
    if message = "prepare" then
        if stockStatus = "pending" then
            if stockAvailable >= CheckoutAmount then
                stockAvailable := stockAvailable - CheckoutAmount;
                stockHeld := stockHeld + CheckoutAmount;
                stockStatus := "prepared";
                PrepareSuccessResponse:
                    orchestratorRxS := Append(orchestratorRxS, "prepareOk");
            else
                stockStatus := "failed";
                PrepareFailedResponse:
                    orchestratorRxS := Append(orchestratorRxS, "prepareFailed");
            end if;
        elsif stockStatus \in {"prepared", "completed"} then
            \* Idempotency
            orchestratorRxS := Append(orchestratorRxS, "prepareOk");
        end if;
    elsif message = "commit" then
        if stockStatus = "prepared" then
            stockHeld := stockHeld - CheckoutAmount;
            stockSold := stockSold + CheckoutAmount;
            stockStatus := "completed";
            ReplyCommitResponse:
                orchestratorRxS := Append(orchestratorRxS, "commitOk");
        elsif stockStatus = "completed" then
            ReplyCommitResponse2:
                orchestratorRxS := Append(orchestratorRxS, "commitOk");
        end if;
    elsif message = "abort" then
        if stockStatus = "prepared" then
            stockHeld := stockHeld - CheckoutAmount;
            stockAvailable := stockAvailable + CheckoutAmount;
            stockStatus := "failed";
            ReplyAbortMessage:
                orchestratorRxS := Append(orchestratorRxS, "abortOk");
        elsif stockStatus = "completed" then
            ReplyAbort2:
                orchestratorRxS := Append(orchestratorRxS, "abortOk");
        end if;
    elsif message = "checkout" then
        if stockStatus = "pending" then
            if stockAvailable >= CheckoutAmount then
                stockAvailable := stockAvailable - CheckoutAmount;
                stockSold := stockSold + CheckoutAmount;
                stockStatus := "completed";
                ReplyCheckoutResponse:
                    orchestratorRxS := Append(orchestratorRxS, "checkoutOk");
            else
                stockStatus := "failed";
                FailedCheckoutResponse:
                    orchestratorRxS := Append(orchestratorRxS, "checkoutFailed");
            end if;
        elsif stockStatus = "completed" then
            \* Idempotency
            orchestratorRxS := Append(orchestratorRxS, "checkoutOk");
        end if;
    elsif message = "compensate" then
        if stockStatus = "completed" then
            stockAvailable := stockAvailable + CheckoutAmount;
            stockSold := stockSold - CheckoutAmount;
            stockStatus := "compensated";
            ReplyCompensateResponse:
                orchestratorRxS := Append(orchestratorRxS, "compensateOk");
        elsif stockStatus \in {"compensated", "failed"} then
            \* Idempotency
            orchestratorRxS := Append(orchestratorRxS, "compensateOk");
        end if;
    end if;
    step:
        goto StockStart;
end process;

end algorithm;*)
\* BEGIN TRANSLATION (chksum(pcal) = "aea265cf" /\ chksum(tla) = "d13ded7f")
\* Label PrepareSuccessResponse of process PaymentWorker at line 223 col 21 changed to PrepareSuccessResponse_
\* Label PrepareFailedResponse of process PaymentWorker at line 227 col 21 changed to PrepareFailedResponse_
\* Label ReplyCommitResponse of process PaymentWorker at line 239 col 17 changed to ReplyCommitResponse_
\* Label ReplyCommitResponse2 of process PaymentWorker at line 242 col 17 changed to ReplyCommitResponse2_
\* Label ReplyAbortMessage of process PaymentWorker at line 250 col 17 changed to ReplyAbortMessage_
\* Label ReplyAbort2 of process PaymentWorker at line 253 col 17 changed to ReplyAbort2_
\* Label ReplyCheckoutResponse of process PaymentWorker at line 262 col 21 changed to ReplyCheckoutResponse_
\* Label FailedCheckoutResponse of process PaymentWorker at line 266 col 21 changed to FailedCheckoutResponse_
\* Label ReplyCompensateResponse of process PaymentWorker at line 278 col 17 changed to ReplyCompensateResponse_
\* Label step of process PaymentWorker at line 285 col 9 changed to step_
\* Process variable message of process Orchestrator at line 51 col 28 changed to message_
\* Process variable message of process PaymentWorker at line 205 col 5 changed to message_P
VARIABLES pc, done, userCredits, userHeldCredits, userPaidCredits, 
          stockAvailable, stockHeld, stockSold, paymentStatus, stockStatus, 
          orchestratorTxP, orchestratorTxS, orchestratorRxP, orchestratorRxS

(* define statement *)
TypeInvariant ==
    /\ done \in BOOLEAN
    /\ paymentStatus \in PAYMENT_STATUS
    /\ stockStatus \in STOCK_STATUS
    /\ userCredits \in Nat
    /\ userHeldCredits \in Nat
    /\ userPaidCredits \in Nat
    /\ stockAvailable \in Nat
    /\ stockHeld \in Nat
    /\ stockSold \in Nat

UserCreditsConsistent == userCredits + userHeldCredits + userPaidCredits = InitialCredits
StockConsistent == stockAvailable + stockHeld + stockSold = InitialStock

EventuallyCompletes == <>(
    \/ paymentStatus = "completed" /\ stockStatus = "completed"
    \/ paymentStatus = "failed" /\ stockStatus \in {"failed", "compensated"}
    \/ stockStatus = "failed" /\ paymentStatus \in {"failed", "compensated"}
)

VARIABLES checkoutStatus, message_, message_P, message

vars == << pc, done, userCredits, userHeldCredits, userPaidCredits, 
           stockAvailable, stockHeld, stockSold, paymentStatus, stockStatus, 
           orchestratorTxP, orchestratorTxS, orchestratorRxP, orchestratorRxS, 
           checkoutStatus, message_, message_P, message >>

ProcSet == {0} \cup {1} \cup {2}

Init == (* Global variables *)
        /\ done = FALSE
        /\ userCredits = InitialCredits
        /\ userHeldCredits = 0
        /\ userPaidCredits = 0
        /\ stockAvailable = InitialStock
        /\ stockHeld = 0
        /\ stockSold = 0
        /\ paymentStatus = "pending"
        /\ stockStatus = "pending"
        /\ orchestratorTxP = <<>>
        /\ orchestratorTxS = <<>>
        /\ orchestratorRxP = <<>>
        /\ orchestratorRxS = <<>>
        (* Process Orchestrator *)
        /\ checkoutStatus = <<>>
        /\ message_ = ""
        (* Process PaymentWorker *)
        /\ message_P = ""
        (* Process StockWorker *)
        /\ message = ""
        /\ pc = [self \in ProcSet |-> CASE self = 0 -> "Start"
                                        [] self = 1 -> "PaymentStart"
                                        [] self = 2 -> "StockStart"]

Start == /\ pc[0] = "Start"
         /\ checkoutStatus' = <<"notStarted", "notStarted">>
         /\ IF Mode = "2pc"
               THEN /\ pc' = [pc EXCEPT ![0] = "Checkout2PC"]
               ELSE /\ pc' = [pc EXCEPT ![0] = "CheckoutSaga"]
         /\ UNCHANGED << done, userCredits, userHeldCredits, userPaidCredits, 
                         stockAvailable, stockHeld, stockSold, paymentStatus, 
                         stockStatus, orchestratorTxP, orchestratorTxS, 
                         orchestratorRxP, orchestratorRxS, message_, message_P, 
                         message >>

Checkout2PC == /\ pc[0] = "Checkout2PC"
               /\ pc' = [pc EXCEPT ![0] = "CheckoutPayment2PC"]
               /\ UNCHANGED << done, userCredits, userHeldCredits, 
                               userPaidCredits, stockAvailable, stockHeld, 
                               stockSold, paymentStatus, stockStatus, 
                               orchestratorTxP, orchestratorTxS, 
                               orchestratorRxP, orchestratorRxS, 
                               checkoutStatus, message_, message_P, message >>

CheckoutPayment2PC == /\ pc[0] = "CheckoutPayment2PC"
                      /\ orchestratorTxP' = Append(orchestratorTxP, "prepare")
                      /\ checkoutStatus' = [checkoutStatus EXCEPT ![1] = "started"]
                      /\ pc' = [pc EXCEPT ![0] = "CheckoutStock2PC"]
                      /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                      userPaidCredits, stockAvailable, 
                                      stockHeld, stockSold, paymentStatus, 
                                      stockStatus, orchestratorTxS, 
                                      orchestratorRxP, orchestratorRxS, 
                                      message_, message_P, message >>

CheckoutStock2PC == /\ pc[0] = "CheckoutStock2PC"
                    /\ orchestratorTxS' = Append(orchestratorTxS, "prepare")
                    /\ checkoutStatus' = [checkoutStatus EXCEPT ![2] = "started"]
                    /\ pc' = [pc EXCEPT ![0] = "CheckoutProcess"]
                    /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                    userPaidCredits, stockAvailable, stockHeld, 
                                    stockSold, paymentStatus, stockStatus, 
                                    orchestratorTxP, orchestratorRxP, 
                                    orchestratorRxS, message_, message_P, 
                                    message >>

CheckoutProcess == /\ pc[0] = "CheckoutProcess"
                   /\ IF orchestratorRxP # <<>>
                         THEN /\ message_' = Head(orchestratorRxP)
                              /\ orchestratorRxP' = Tail(orchestratorRxP)
                              /\ pc' = [pc EXCEPT ![0] = "ProcessMessageCheckout"]
                              /\ UNCHANGED << orchestratorTxP, orchestratorTxS, 
                                              orchestratorRxS, checkoutStatus >>
                         ELSE /\ IF orchestratorRxS # <<>>
                                    THEN /\ message_' = Head(orchestratorRxS)
                                         /\ orchestratorRxS' = Tail(orchestratorRxS)
                                         /\ pc' = [pc EXCEPT ![0] = "ProcessMessageCheckoutStock"]
                                         /\ UNCHANGED << orchestratorTxP, 
                                                         orchestratorTxS, 
                                                         checkoutStatus >>
                                    ELSE /\ IF checkoutStatus = <<"prepared", "prepared">>
                                               THEN /\ orchestratorTxP' = Append(orchestratorTxP, "commit")
                                                    /\ orchestratorTxS' = Append(orchestratorTxS, "commit")
                                                    /\ checkoutStatus' = <<"committing", "committing">>
                                                    /\ pc' = [pc EXCEPT ![0] = "CheckoutProcess"]
                                               ELSE /\ IF checkoutStatus[1] = "failed" /\ checkoutStatus[2] \in {"started", "prepared"}
                                                          THEN /\ orchestratorTxP' = Append(orchestratorTxP, "abort")
                                                               /\ orchestratorTxS' = Append(orchestratorTxS, "abort")
                                                               /\ checkoutStatus' = <<"aborting", "aborting">>
                                                               /\ pc' = [pc EXCEPT ![0] = "CheckoutProcess"]
                                                          ELSE /\ IF checkoutStatus[2] = "failed" /\ checkoutStatus[1] \in {"started", "prepared"}
                                                                     THEN /\ orchestratorTxP' = Append(orchestratorTxP, "abort")
                                                                          /\ orchestratorTxS' = Append(orchestratorTxS, "abort")
                                                                          /\ checkoutStatus' = <<"aborting", "aborting">>
                                                                          /\ pc' = [pc EXCEPT ![0] = "CheckoutProcess"]
                                                                     ELSE /\ IF checkoutStatus = <<"committed", "committed">>
                                                                                THEN /\ pc' = [pc EXCEPT ![0] = "2pcEnd"]
                                                                                ELSE /\ pc' = [pc EXCEPT ![0] = "CheckoutProcess"]
                                                                          /\ UNCHANGED << orchestratorTxP, 
                                                                                          orchestratorTxS, 
                                                                                          checkoutStatus >>
                                         /\ UNCHANGED << orchestratorRxS, 
                                                         message_ >>
                              /\ UNCHANGED orchestratorRxP
                   /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                   userPaidCredits, stockAvailable, stockHeld, 
                                   stockSold, paymentStatus, stockStatus, 
                                   message_P, message >>

ProcessMessageCheckout == /\ pc[0] = "ProcessMessageCheckout"
                          /\ IF message_ = "prepareOk"
                                THEN /\ IF checkoutStatus[1] = "started"
                                           THEN /\ checkoutStatus' = [checkoutStatus EXCEPT ![1] = "prepared"]
                                           ELSE /\ TRUE
                                                /\ UNCHANGED checkoutStatus
                                ELSE /\ IF message_ = "prepareFailed"
                                           THEN /\ IF checkoutStatus[1] = "started"
                                                      THEN /\ checkoutStatus' = [checkoutStatus EXCEPT ![1] = "failed"]
                                                      ELSE /\ TRUE
                                                           /\ UNCHANGED checkoutStatus
                                           ELSE /\ IF message_ = "commitOk"
                                                      THEN /\ IF checkoutStatus[1] = "committing"
                                                                 THEN /\ checkoutStatus' = [checkoutStatus EXCEPT ![1] = "committed"]
                                                                 ELSE /\ TRUE
                                                                      /\ UNCHANGED checkoutStatus
                                                      ELSE /\ IF message_ = "abortOk"
                                                                 THEN /\ IF checkoutStatus[1] = "aborting"
                                                                            THEN /\ checkoutStatus' = [checkoutStatus EXCEPT ![1] = "aborted"]
                                                                            ELSE /\ TRUE
                                                                                 /\ UNCHANGED checkoutStatus
                                                                 ELSE /\ TRUE
                                                                      /\ UNCHANGED checkoutStatus
                          /\ pc' = [pc EXCEPT ![0] = "CheckoutProcess"]
                          /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                          userPaidCredits, stockAvailable, 
                                          stockHeld, stockSold, paymentStatus, 
                                          stockStatus, orchestratorTxP, 
                                          orchestratorTxS, orchestratorRxP, 
                                          orchestratorRxS, message_, message_P, 
                                          message >>

ProcessMessageCheckoutStock == /\ pc[0] = "ProcessMessageCheckoutStock"
                               /\ IF message_ = "prepareOk"
                                     THEN /\ IF checkoutStatus[2] = "started"
                                                THEN /\ checkoutStatus' = [checkoutStatus EXCEPT ![2] = "prepared"]
                                                ELSE /\ TRUE
                                                     /\ UNCHANGED checkoutStatus
                                     ELSE /\ IF message_ = "prepareFailed"
                                                THEN /\ IF checkoutStatus[2] = "started"
                                                           THEN /\ checkoutStatus' = [checkoutStatus EXCEPT ![2] = "failed"]
                                                           ELSE /\ TRUE
                                                                /\ UNCHANGED checkoutStatus
                                                ELSE /\ IF message_ = "commitOk"
                                                           THEN /\ IF checkoutStatus[2] = "committing"
                                                                      THEN /\ checkoutStatus' = [checkoutStatus EXCEPT ![2] = "committed"]
                                                                      ELSE /\ TRUE
                                                                           /\ UNCHANGED checkoutStatus
                                                           ELSE /\ IF message_ = "abortOk"
                                                                      THEN /\ IF checkoutStatus[2] = "aborting"
                                                                                 THEN /\ checkoutStatus' = [checkoutStatus EXCEPT ![2] = "aborted"]
                                                                                 ELSE /\ TRUE
                                                                                      /\ UNCHANGED checkoutStatus
                                                                      ELSE /\ TRUE
                                                                           /\ UNCHANGED checkoutStatus
                               /\ pc' = [pc EXCEPT ![0] = "CheckoutProcess"]
                               /\ UNCHANGED << done, userCredits, 
                                               userHeldCredits, 
                                               userPaidCredits, stockAvailable, 
                                               stockHeld, stockSold, 
                                               paymentStatus, stockStatus, 
                                               orchestratorTxP, 
                                               orchestratorTxS, 
                                               orchestratorRxP, 
                                               orchestratorRxS, message_, 
                                               message_P, message >>

2pcEnd == /\ pc[0] = "2pcEnd"
          /\ done' = TRUE
          /\ pc' = [pc EXCEPT ![0] = "Done"]
          /\ UNCHANGED << userCredits, userHeldCredits, userPaidCredits, 
                          stockAvailable, stockHeld, stockSold, paymentStatus, 
                          stockStatus, orchestratorTxP, orchestratorTxS, 
                          orchestratorRxP, orchestratorRxS, checkoutStatus, 
                          message_, message_P, message >>

CheckoutSaga == /\ pc[0] = "CheckoutSaga"
                /\ pc' = [pc EXCEPT ![0] = "CheckoutPaymentSaga"]
                /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                userPaidCredits, stockAvailable, stockHeld, 
                                stockSold, paymentStatus, stockStatus, 
                                orchestratorTxP, orchestratorTxS, 
                                orchestratorRxP, orchestratorRxS, 
                                checkoutStatus, message_, message_P, message >>

CheckoutPaymentSaga == /\ pc[0] = "CheckoutPaymentSaga"
                       /\ orchestratorTxP' = Append(orchestratorTxP, "checkout")
                       /\ pc' = [pc EXCEPT ![0] = "CheckoutStockSaga"]
                       /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                       userPaidCredits, stockAvailable, 
                                       stockHeld, stockSold, paymentStatus, 
                                       stockStatus, orchestratorTxS, 
                                       orchestratorRxP, orchestratorRxS, 
                                       checkoutStatus, message_, message_P, 
                                       message >>

CheckoutStockSaga == /\ pc[0] = "CheckoutStockSaga"
                     /\ orchestratorTxS' = Append(orchestratorTxS, "checkout")
                     /\ pc' = [pc EXCEPT ![0] = "CheckoutSagaProcess"]
                     /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                     userPaidCredits, stockAvailable, 
                                     stockHeld, stockSold, paymentStatus, 
                                     stockStatus, orchestratorTxP, 
                                     orchestratorRxP, orchestratorRxS, 
                                     checkoutStatus, message_, message_P, 
                                     message >>

CheckoutSagaProcess == /\ pc[0] = "CheckoutSagaProcess"
                       /\ IF orchestratorRxP # <<>>
                             THEN /\ message_' = Head(orchestratorRxP)
                                  /\ orchestratorRxP' = Tail(orchestratorRxP)
                                  /\ IF message_' = "checkoutOk"
                                        THEN /\ checkoutStatus' = [checkoutStatus EXCEPT ![1] = "completed"]
                                        ELSE /\ IF message_' = "checkoutFailed"
                                                   THEN /\ checkoutStatus' = [checkoutStatus EXCEPT ![1] = "failed"]
                                                   ELSE /\ IF message_' = "compensateOk"
                                                              THEN /\ IF checkoutStatus[1] = "compensating"
                                                                         THEN /\ checkoutStatus' = [checkoutStatus EXCEPT ![1] = "compensated"]
                                                                         ELSE /\ TRUE
                                                                              /\ UNCHANGED checkoutStatus
                                                              ELSE /\ TRUE
                                                                   /\ UNCHANGED checkoutStatus
                                  /\ pc' = [pc EXCEPT ![0] = "CheckoutSagaProcess"]
                                  /\ UNCHANGED << orchestratorTxP, 
                                                  orchestratorTxS, 
                                                  orchestratorRxS >>
                             ELSE /\ IF orchestratorRxS # <<>>
                                        THEN /\ message_' = Head(orchestratorRxS)
                                             /\ orchestratorRxS' = Tail(orchestratorRxS)
                                             /\ IF message_' = "checkoutOk"
                                                   THEN /\ checkoutStatus' = [checkoutStatus EXCEPT ![2] = "completed"]
                                                   ELSE /\ IF message_' = "checkoutFailed"
                                                              THEN /\ checkoutStatus' = [checkoutStatus EXCEPT ![2] = "failed"]
                                                              ELSE /\ IF message_' = "compensateOk"
                                                                         THEN /\ IF checkoutStatus[2] = "compensating"
                                                                                    THEN /\ checkoutStatus' = [checkoutStatus EXCEPT ![2] = "compensated"]
                                                                                    ELSE /\ TRUE
                                                                                         /\ UNCHANGED checkoutStatus
                                                                         ELSE /\ TRUE
                                                                              /\ UNCHANGED checkoutStatus
                                             /\ pc' = [pc EXCEPT ![0] = "CheckoutSagaProcess"]
                                             /\ UNCHANGED << orchestratorTxP, 
                                                             orchestratorTxS >>
                                        ELSE /\ IF checkoutStatus = <<"completed", "completed">>
                                                   THEN /\ pc' = [pc EXCEPT ![0] = "SagaEnd"]
                                                        /\ UNCHANGED << orchestratorTxP, 
                                                                        orchestratorTxS, 
                                                                        checkoutStatus >>
                                                   ELSE /\ IF checkoutStatus[1] = "failed" /\ checkoutStatus[2] \in {"notStarted", "completed"}
                                                              THEN /\ orchestratorTxS' = Append(orchestratorTxS, "compensate")
                                                                   /\ checkoutStatus' = [checkoutStatus EXCEPT ![2] = "compensating"]
                                                                   /\ pc' = [pc EXCEPT ![0] = "CheckoutSagaProcess"]
                                                                   /\ UNCHANGED orchestratorTxP
                                                              ELSE /\ IF checkoutStatus[2] = "failed" /\ checkoutStatus[1] \in {"notStarted", "completed"}
                                                                         THEN /\ orchestratorTxP' = Append(orchestratorTxP, "compensate")
                                                                              /\ checkoutStatus' = [checkoutStatus EXCEPT ![1] = "compensating"]
                                                                              /\ pc' = [pc EXCEPT ![0] = "CheckoutSagaProcess"]
                                                                         ELSE /\ IF checkoutStatus[1] \in {"failed", "compensated"} /\ checkoutStatus[2] \in {"failed", "compensated"}
                                                                                    THEN /\ pc' = [pc EXCEPT ![0] = "SagaEnd"]
                                                                                    ELSE /\ pc' = [pc EXCEPT ![0] = "CheckoutSagaProcess"]
                                                                              /\ UNCHANGED << orchestratorTxP, 
                                                                                              checkoutStatus >>
                                                                   /\ UNCHANGED orchestratorTxS
                                             /\ UNCHANGED << orchestratorRxS, 
                                                             message_ >>
                                  /\ UNCHANGED orchestratorRxP
                       /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                       userPaidCredits, stockAvailable, 
                                       stockHeld, stockSold, paymentStatus, 
                                       stockStatus, message_P, message >>

SagaEnd == /\ pc[0] = "SagaEnd"
           /\ done' = TRUE
           /\ pc' = [pc EXCEPT ![0] = "Done"]
           /\ UNCHANGED << userCredits, userHeldCredits, userPaidCredits, 
                           stockAvailable, stockHeld, stockSold, paymentStatus, 
                           stockStatus, orchestratorTxP, orchestratorTxS, 
                           orchestratorRxP, orchestratorRxS, checkoutStatus, 
                           message_, message_P, message >>

Orchestrator == Start \/ Checkout2PC \/ CheckoutPayment2PC
                   \/ CheckoutStock2PC \/ CheckoutProcess
                   \/ ProcessMessageCheckout \/ ProcessMessageCheckoutStock
                   \/ 2pcEnd \/ CheckoutSaga \/ CheckoutPaymentSaga
                   \/ CheckoutStockSaga \/ CheckoutSagaProcess \/ SagaEnd

PaymentStart == /\ pc[1] = "PaymentStart"
                /\ orchestratorTxP # <<>> \/ done
                /\ IF done
                      THEN /\ pc' = [pc EXCEPT ![1] = "Done"]
                           /\ UNCHANGED << orchestratorTxP, message_P >>
                      ELSE /\ message_P' = Head(orchestratorTxP)
                           /\ orchestratorTxP' = Tail(orchestratorTxP)
                           /\ pc' = [pc EXCEPT ![1] = "PaymentReqProcessing"]
                /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                userPaidCredits, stockAvailable, stockHeld, 
                                stockSold, paymentStatus, stockStatus, 
                                orchestratorTxS, orchestratorRxP, 
                                orchestratorRxS, checkoutStatus, message_, 
                                message >>

PaymentReqProcessing == /\ pc[1] = "PaymentReqProcessing"
                        /\ IF message_P = "prepare"
                              THEN /\ IF paymentStatus = "pending"
                                         THEN /\ IF userCredits >= CheckoutAmount * PPU
                                                    THEN /\ userCredits' = userCredits - CheckoutAmount * PPU
                                                         /\ userHeldCredits' = userHeldCredits + CheckoutAmount * PPU
                                                         /\ paymentStatus' = "prepared"
                                                         /\ pc' = [pc EXCEPT ![1] = "PrepareSuccessResponse_"]
                                                    ELSE /\ paymentStatus' = "failed"
                                                         /\ pc' = [pc EXCEPT ![1] = "PrepareFailedResponse_"]
                                                         /\ UNCHANGED << userCredits, 
                                                                         userHeldCredits >>
                                              /\ UNCHANGED orchestratorRxP
                                         ELSE /\ IF paymentStatus \in {"prepared", "completed"}
                                                    THEN /\ orchestratorRxP' = Append(orchestratorRxP, "prepareOk")
                                                    ELSE /\ TRUE
                                                         /\ UNCHANGED orchestratorRxP
                                              /\ pc' = [pc EXCEPT ![1] = "step_"]
                                              /\ UNCHANGED << userCredits, 
                                                              userHeldCredits, 
                                                              paymentStatus >>
                                   /\ UNCHANGED userPaidCredits
                              ELSE /\ IF message_P = "commit"
                                         THEN /\ IF paymentStatus = "prepared"
                                                    THEN /\ userHeldCredits' = userHeldCredits - CheckoutAmount * PPU
                                                         /\ userPaidCredits' = userPaidCredits + CheckoutAmount * PPU
                                                         /\ paymentStatus' = "completed"
                                                         /\ pc' = [pc EXCEPT ![1] = "ReplyCommitResponse_"]
                                                    ELSE /\ IF paymentStatus = "completed"
                                                               THEN /\ pc' = [pc EXCEPT ![1] = "ReplyCommitResponse2_"]
                                                               ELSE /\ pc' = [pc EXCEPT ![1] = "step_"]
                                                         /\ UNCHANGED << userHeldCredits, 
                                                                         userPaidCredits, 
                                                                         paymentStatus >>
                                              /\ UNCHANGED << userCredits, 
                                                              orchestratorRxP >>
                                         ELSE /\ IF message_P = "abort"
                                                    THEN /\ IF paymentStatus = "prepared"
                                                               THEN /\ userHeldCredits' = userHeldCredits - CheckoutAmount * PPU
                                                                    /\ userCredits' = userCredits + CheckoutAmount * PPU
                                                                    /\ paymentStatus' = "failed"
                                                                    /\ pc' = [pc EXCEPT ![1] = "ReplyAbortMessage_"]
                                                               ELSE /\ IF paymentStatus = "completed"
                                                                          THEN /\ pc' = [pc EXCEPT ![1] = "ReplyAbort2_"]
                                                                          ELSE /\ pc' = [pc EXCEPT ![1] = "step_"]
                                                                    /\ UNCHANGED << userCredits, 
                                                                                    userHeldCredits, 
                                                                                    paymentStatus >>
                                                         /\ UNCHANGED << userPaidCredits, 
                                                                         orchestratorRxP >>
                                                    ELSE /\ IF message_P = "checkout"
                                                               THEN /\ IF paymentStatus = "pending"
                                                                          THEN /\ IF userCredits >= CheckoutAmount * PPU
                                                                                     THEN /\ userCredits' = userCredits - CheckoutAmount * PPU
                                                                                          /\ userPaidCredits' = userPaidCredits + CheckoutAmount * PPU
                                                                                          /\ paymentStatus' = "completed"
                                                                                          /\ pc' = [pc EXCEPT ![1] = "ReplyCheckoutResponse_"]
                                                                                     ELSE /\ paymentStatus' = "failed"
                                                                                          /\ pc' = [pc EXCEPT ![1] = "FailedCheckoutResponse_"]
                                                                                          /\ UNCHANGED << userCredits, 
                                                                                                          userPaidCredits >>
                                                                               /\ UNCHANGED orchestratorRxP
                                                                          ELSE /\ IF paymentStatus = "completed"
                                                                                     THEN /\ orchestratorRxP' = Append(orchestratorRxP, "checkoutOk")
                                                                                     ELSE /\ TRUE
                                                                                          /\ UNCHANGED orchestratorRxP
                                                                               /\ pc' = [pc EXCEPT ![1] = "step_"]
                                                                               /\ UNCHANGED << userCredits, 
                                                                                               userPaidCredits, 
                                                                                               paymentStatus >>
                                                               ELSE /\ IF message_P = "compensate"
                                                                          THEN /\ IF paymentStatus = "completed"
                                                                                     THEN /\ userCredits' = userCredits + CheckoutAmount * PPU
                                                                                          /\ userPaidCredits' = userPaidCredits - CheckoutAmount * PPU
                                                                                          /\ paymentStatus' = "compensated"
                                                                                          /\ pc' = [pc EXCEPT ![1] = "ReplyCompensateResponse_"]
                                                                                          /\ UNCHANGED orchestratorRxP
                                                                                     ELSE /\ IF paymentStatus \in {"compensated", "failed"}
                                                                                                THEN /\ orchestratorRxP' = Append(orchestratorRxP, "compensateOk")
                                                                                                ELSE /\ TRUE
                                                                                                     /\ UNCHANGED orchestratorRxP
                                                                                          /\ pc' = [pc EXCEPT ![1] = "step_"]
                                                                                          /\ UNCHANGED << userCredits, 
                                                                                                          userPaidCredits, 
                                                                                                          paymentStatus >>
                                                                          ELSE /\ pc' = [pc EXCEPT ![1] = "step_"]
                                                                               /\ UNCHANGED << userCredits, 
                                                                                               userPaidCredits, 
                                                                                               paymentStatus, 
                                                                                               orchestratorRxP >>
                                                         /\ UNCHANGED userHeldCredits
                        /\ UNCHANGED << done, stockAvailable, stockHeld, 
                                        stockSold, stockStatus, 
                                        orchestratorTxP, orchestratorTxS, 
                                        orchestratorRxS, checkoutStatus, 
                                        message_, message_P, message >>

PrepareSuccessResponse_ == /\ pc[1] = "PrepareSuccessResponse_"
                           /\ orchestratorRxP' = Append(orchestratorRxP, "prepareOk")
                           /\ pc' = [pc EXCEPT ![1] = "step_"]
                           /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                           userPaidCredits, stockAvailable, 
                                           stockHeld, stockSold, paymentStatus, 
                                           stockStatus, orchestratorTxP, 
                                           orchestratorTxS, orchestratorRxS, 
                                           checkoutStatus, message_, message_P, 
                                           message >>

PrepareFailedResponse_ == /\ pc[1] = "PrepareFailedResponse_"
                          /\ orchestratorRxP' = Append(orchestratorRxP, "prepareFailed")
                          /\ pc' = [pc EXCEPT ![1] = "step_"]
                          /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                          userPaidCredits, stockAvailable, 
                                          stockHeld, stockSold, paymentStatus, 
                                          stockStatus, orchestratorTxP, 
                                          orchestratorTxS, orchestratorRxS, 
                                          checkoutStatus, message_, message_P, 
                                          message >>

ReplyCommitResponse_ == /\ pc[1] = "ReplyCommitResponse_"
                        /\ orchestratorRxP' = Append(orchestratorRxP, "commitOk")
                        /\ pc' = [pc EXCEPT ![1] = "step_"]
                        /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                        userPaidCredits, stockAvailable, 
                                        stockHeld, stockSold, paymentStatus, 
                                        stockStatus, orchestratorTxP, 
                                        orchestratorTxS, orchestratorRxS, 
                                        checkoutStatus, message_, message_P, 
                                        message >>

ReplyCommitResponse2_ == /\ pc[1] = "ReplyCommitResponse2_"
                         /\ orchestratorRxP' = Append(orchestratorRxP, "commitOk")
                         /\ pc' = [pc EXCEPT ![1] = "step_"]
                         /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                         userPaidCredits, stockAvailable, 
                                         stockHeld, stockSold, paymentStatus, 
                                         stockStatus, orchestratorTxP, 
                                         orchestratorTxS, orchestratorRxS, 
                                         checkoutStatus, message_, message_P, 
                                         message >>

ReplyAbortMessage_ == /\ pc[1] = "ReplyAbortMessage_"
                      /\ orchestratorRxP' = Append(orchestratorRxP, "abortOk")
                      /\ pc' = [pc EXCEPT ![1] = "step_"]
                      /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                      userPaidCredits, stockAvailable, 
                                      stockHeld, stockSold, paymentStatus, 
                                      stockStatus, orchestratorTxP, 
                                      orchestratorTxS, orchestratorRxS, 
                                      checkoutStatus, message_, message_P, 
                                      message >>

ReplyAbort2_ == /\ pc[1] = "ReplyAbort2_"
                /\ orchestratorRxP' = Append(orchestratorRxP, "abortOk")
                /\ pc' = [pc EXCEPT ![1] = "step_"]
                /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                userPaidCredits, stockAvailable, stockHeld, 
                                stockSold, paymentStatus, stockStatus, 
                                orchestratorTxP, orchestratorTxS, 
                                orchestratorRxS, checkoutStatus, message_, 
                                message_P, message >>

ReplyCheckoutResponse_ == /\ pc[1] = "ReplyCheckoutResponse_"
                          /\ orchestratorRxP' = Append(orchestratorRxP, "checkoutOk")
                          /\ pc' = [pc EXCEPT ![1] = "step_"]
                          /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                          userPaidCredits, stockAvailable, 
                                          stockHeld, stockSold, paymentStatus, 
                                          stockStatus, orchestratorTxP, 
                                          orchestratorTxS, orchestratorRxS, 
                                          checkoutStatus, message_, message_P, 
                                          message >>

FailedCheckoutResponse_ == /\ pc[1] = "FailedCheckoutResponse_"
                           /\ orchestratorRxP' = Append(orchestratorRxP, "checkoutFailed")
                           /\ pc' = [pc EXCEPT ![1] = "step_"]
                           /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                           userPaidCredits, stockAvailable, 
                                           stockHeld, stockSold, paymentStatus, 
                                           stockStatus, orchestratorTxP, 
                                           orchestratorTxS, orchestratorRxS, 
                                           checkoutStatus, message_, message_P, 
                                           message >>

ReplyCompensateResponse_ == /\ pc[1] = "ReplyCompensateResponse_"
                            /\ orchestratorRxP' = Append(orchestratorRxP, "compensateOk")
                            /\ pc' = [pc EXCEPT ![1] = "step_"]
                            /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                            userPaidCredits, stockAvailable, 
                                            stockHeld, stockSold, 
                                            paymentStatus, stockStatus, 
                                            orchestratorTxP, orchestratorTxS, 
                                            orchestratorRxS, checkoutStatus, 
                                            message_, message_P, message >>

step_ == /\ pc[1] = "step_"
         /\ pc' = [pc EXCEPT ![1] = "PaymentStart"]
         /\ UNCHANGED << done, userCredits, userHeldCredits, userPaidCredits, 
                         stockAvailable, stockHeld, stockSold, paymentStatus, 
                         stockStatus, orchestratorTxP, orchestratorTxS, 
                         orchestratorRxP, orchestratorRxS, checkoutStatus, 
                         message_, message_P, message >>

PaymentWorker == PaymentStart \/ PaymentReqProcessing
                    \/ PrepareSuccessResponse_ \/ PrepareFailedResponse_
                    \/ ReplyCommitResponse_ \/ ReplyCommitResponse2_
                    \/ ReplyAbortMessage_ \/ ReplyAbort2_
                    \/ ReplyCheckoutResponse_ \/ FailedCheckoutResponse_
                    \/ ReplyCompensateResponse_ \/ step_

StockStart == /\ pc[2] = "StockStart"
              /\ orchestratorTxS # <<>> \/ done
              /\ IF done
                    THEN /\ pc' = [pc EXCEPT ![2] = "Done"]
                         /\ UNCHANGED << orchestratorTxS, message >>
                    ELSE /\ message' = Head(orchestratorTxS)
                         /\ orchestratorTxS' = Tail(orchestratorTxS)
                         /\ pc' = [pc EXCEPT ![2] = "StockReqProcessing"]
              /\ UNCHANGED << done, userCredits, userHeldCredits, 
                              userPaidCredits, stockAvailable, stockHeld, 
                              stockSold, paymentStatus, stockStatus, 
                              orchestratorTxP, orchestratorRxP, 
                              orchestratorRxS, checkoutStatus, message_, 
                              message_P >>

StockReqProcessing == /\ pc[2] = "StockReqProcessing"
                      /\ IF message = "prepare"
                            THEN /\ IF stockStatus = "pending"
                                       THEN /\ IF stockAvailable >= CheckoutAmount
                                                  THEN /\ stockAvailable' = stockAvailable - CheckoutAmount
                                                       /\ stockHeld' = stockHeld + CheckoutAmount
                                                       /\ stockStatus' = "prepared"
                                                       /\ pc' = [pc EXCEPT ![2] = "PrepareSuccessResponse"]
                                                  ELSE /\ stockStatus' = "failed"
                                                       /\ pc' = [pc EXCEPT ![2] = "PrepareFailedResponse"]
                                                       /\ UNCHANGED << stockAvailable, 
                                                                       stockHeld >>
                                            /\ UNCHANGED orchestratorRxS
                                       ELSE /\ IF stockStatus \in {"prepared", "completed"}
                                                  THEN /\ orchestratorRxS' = Append(orchestratorRxS, "prepareOk")
                                                  ELSE /\ TRUE
                                                       /\ UNCHANGED orchestratorRxS
                                            /\ pc' = [pc EXCEPT ![2] = "step"]
                                            /\ UNCHANGED << stockAvailable, 
                                                            stockHeld, 
                                                            stockStatus >>
                                 /\ UNCHANGED stockSold
                            ELSE /\ IF message = "commit"
                                       THEN /\ IF stockStatus = "prepared"
                                                  THEN /\ stockHeld' = stockHeld - CheckoutAmount
                                                       /\ stockSold' = stockSold + CheckoutAmount
                                                       /\ stockStatus' = "completed"
                                                       /\ pc' = [pc EXCEPT ![2] = "ReplyCommitResponse"]
                                                  ELSE /\ IF stockStatus = "completed"
                                                             THEN /\ pc' = [pc EXCEPT ![2] = "ReplyCommitResponse2"]
                                                             ELSE /\ pc' = [pc EXCEPT ![2] = "step"]
                                                       /\ UNCHANGED << stockHeld, 
                                                                       stockSold, 
                                                                       stockStatus >>
                                            /\ UNCHANGED << stockAvailable, 
                                                            orchestratorRxS >>
                                       ELSE /\ IF message = "abort"
                                                  THEN /\ IF stockStatus = "prepared"
                                                             THEN /\ stockHeld' = stockHeld - CheckoutAmount
                                                                  /\ stockAvailable' = stockAvailable + CheckoutAmount
                                                                  /\ stockStatus' = "failed"
                                                                  /\ pc' = [pc EXCEPT ![2] = "ReplyAbortMessage"]
                                                             ELSE /\ IF stockStatus = "completed"
                                                                        THEN /\ pc' = [pc EXCEPT ![2] = "ReplyAbort2"]
                                                                        ELSE /\ pc' = [pc EXCEPT ![2] = "step"]
                                                                  /\ UNCHANGED << stockAvailable, 
                                                                                  stockHeld, 
                                                                                  stockStatus >>
                                                       /\ UNCHANGED << stockSold, 
                                                                       orchestratorRxS >>
                                                  ELSE /\ IF message = "checkout"
                                                             THEN /\ IF stockStatus = "pending"
                                                                        THEN /\ IF stockAvailable >= CheckoutAmount
                                                                                   THEN /\ stockAvailable' = stockAvailable - CheckoutAmount
                                                                                        /\ stockSold' = stockSold + CheckoutAmount
                                                                                        /\ stockStatus' = "completed"
                                                                                        /\ pc' = [pc EXCEPT ![2] = "ReplyCheckoutResponse"]
                                                                                   ELSE /\ stockStatus' = "failed"
                                                                                        /\ pc' = [pc EXCEPT ![2] = "FailedCheckoutResponse"]
                                                                                        /\ UNCHANGED << stockAvailable, 
                                                                                                        stockSold >>
                                                                             /\ UNCHANGED orchestratorRxS
                                                                        ELSE /\ IF stockStatus = "completed"
                                                                                   THEN /\ orchestratorRxS' = Append(orchestratorRxS, "checkoutOk")
                                                                                   ELSE /\ TRUE
                                                                                        /\ UNCHANGED orchestratorRxS
                                                                             /\ pc' = [pc EXCEPT ![2] = "step"]
                                                                             /\ UNCHANGED << stockAvailable, 
                                                                                             stockSold, 
                                                                                             stockStatus >>
                                                             ELSE /\ IF message = "compensate"
                                                                        THEN /\ IF stockStatus = "completed"
                                                                                   THEN /\ stockAvailable' = stockAvailable + CheckoutAmount
                                                                                        /\ stockSold' = stockSold - CheckoutAmount
                                                                                        /\ stockStatus' = "compensated"
                                                                                        /\ pc' = [pc EXCEPT ![2] = "ReplyCompensateResponse"]
                                                                                        /\ UNCHANGED orchestratorRxS
                                                                                   ELSE /\ IF stockStatus \in {"compensated", "failed"}
                                                                                              THEN /\ orchestratorRxS' = Append(orchestratorRxS, "compensateOk")
                                                                                              ELSE /\ TRUE
                                                                                                   /\ UNCHANGED orchestratorRxS
                                                                                        /\ pc' = [pc EXCEPT ![2] = "step"]
                                                                                        /\ UNCHANGED << stockAvailable, 
                                                                                                        stockSold, 
                                                                                                        stockStatus >>
                                                                        ELSE /\ pc' = [pc EXCEPT ![2] = "step"]
                                                                             /\ UNCHANGED << stockAvailable, 
                                                                                             stockSold, 
                                                                                             stockStatus, 
                                                                                             orchestratorRxS >>
                                                       /\ UNCHANGED stockHeld
                      /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                      userPaidCredits, paymentStatus, 
                                      orchestratorTxP, orchestratorTxS, 
                                      orchestratorRxP, checkoutStatus, 
                                      message_, message_P, message >>

PrepareSuccessResponse == /\ pc[2] = "PrepareSuccessResponse"
                          /\ orchestratorRxS' = Append(orchestratorRxS, "prepareOk")
                          /\ pc' = [pc EXCEPT ![2] = "step"]
                          /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                          userPaidCredits, stockAvailable, 
                                          stockHeld, stockSold, paymentStatus, 
                                          stockStatus, orchestratorTxP, 
                                          orchestratorTxS, orchestratorRxP, 
                                          checkoutStatus, message_, message_P, 
                                          message >>

PrepareFailedResponse == /\ pc[2] = "PrepareFailedResponse"
                         /\ orchestratorRxS' = Append(orchestratorRxS, "prepareFailed")
                         /\ pc' = [pc EXCEPT ![2] = "step"]
                         /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                         userPaidCredits, stockAvailable, 
                                         stockHeld, stockSold, paymentStatus, 
                                         stockStatus, orchestratorTxP, 
                                         orchestratorTxS, orchestratorRxP, 
                                         checkoutStatus, message_, message_P, 
                                         message >>

ReplyCommitResponse == /\ pc[2] = "ReplyCommitResponse"
                       /\ orchestratorRxS' = Append(orchestratorRxS, "commitOk")
                       /\ pc' = [pc EXCEPT ![2] = "step"]
                       /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                       userPaidCredits, stockAvailable, 
                                       stockHeld, stockSold, paymentStatus, 
                                       stockStatus, orchestratorTxP, 
                                       orchestratorTxS, orchestratorRxP, 
                                       checkoutStatus, message_, message_P, 
                                       message >>

ReplyCommitResponse2 == /\ pc[2] = "ReplyCommitResponse2"
                        /\ orchestratorRxS' = Append(orchestratorRxS, "commitOk")
                        /\ pc' = [pc EXCEPT ![2] = "step"]
                        /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                        userPaidCredits, stockAvailable, 
                                        stockHeld, stockSold, paymentStatus, 
                                        stockStatus, orchestratorTxP, 
                                        orchestratorTxS, orchestratorRxP, 
                                        checkoutStatus, message_, message_P, 
                                        message >>

ReplyAbortMessage == /\ pc[2] = "ReplyAbortMessage"
                     /\ orchestratorRxS' = Append(orchestratorRxS, "abortOk")
                     /\ pc' = [pc EXCEPT ![2] = "step"]
                     /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                     userPaidCredits, stockAvailable, 
                                     stockHeld, stockSold, paymentStatus, 
                                     stockStatus, orchestratorTxP, 
                                     orchestratorTxS, orchestratorRxP, 
                                     checkoutStatus, message_, message_P, 
                                     message >>

ReplyAbort2 == /\ pc[2] = "ReplyAbort2"
               /\ orchestratorRxS' = Append(orchestratorRxS, "abortOk")
               /\ pc' = [pc EXCEPT ![2] = "step"]
               /\ UNCHANGED << done, userCredits, userHeldCredits, 
                               userPaidCredits, stockAvailable, stockHeld, 
                               stockSold, paymentStatus, stockStatus, 
                               orchestratorTxP, orchestratorTxS, 
                               orchestratorRxP, checkoutStatus, message_, 
                               message_P, message >>

ReplyCheckoutResponse == /\ pc[2] = "ReplyCheckoutResponse"
                         /\ orchestratorRxS' = Append(orchestratorRxS, "checkoutOk")
                         /\ pc' = [pc EXCEPT ![2] = "step"]
                         /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                         userPaidCredits, stockAvailable, 
                                         stockHeld, stockSold, paymentStatus, 
                                         stockStatus, orchestratorTxP, 
                                         orchestratorTxS, orchestratorRxP, 
                                         checkoutStatus, message_, message_P, 
                                         message >>

FailedCheckoutResponse == /\ pc[2] = "FailedCheckoutResponse"
                          /\ orchestratorRxS' = Append(orchestratorRxS, "checkoutFailed")
                          /\ pc' = [pc EXCEPT ![2] = "step"]
                          /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                          userPaidCredits, stockAvailable, 
                                          stockHeld, stockSold, paymentStatus, 
                                          stockStatus, orchestratorTxP, 
                                          orchestratorTxS, orchestratorRxP, 
                                          checkoutStatus, message_, message_P, 
                                          message >>

ReplyCompensateResponse == /\ pc[2] = "ReplyCompensateResponse"
                           /\ orchestratorRxS' = Append(orchestratorRxS, "compensateOk")
                           /\ pc' = [pc EXCEPT ![2] = "step"]
                           /\ UNCHANGED << done, userCredits, userHeldCredits, 
                                           userPaidCredits, stockAvailable, 
                                           stockHeld, stockSold, paymentStatus, 
                                           stockStatus, orchestratorTxP, 
                                           orchestratorTxS, orchestratorRxP, 
                                           checkoutStatus, message_, message_P, 
                                           message >>

step == /\ pc[2] = "step"
        /\ pc' = [pc EXCEPT ![2] = "StockStart"]
        /\ UNCHANGED << done, userCredits, userHeldCredits, userPaidCredits, 
                        stockAvailable, stockHeld, stockSold, paymentStatus, 
                        stockStatus, orchestratorTxP, orchestratorTxS, 
                        orchestratorRxP, orchestratorRxS, checkoutStatus, 
                        message_, message_P, message >>

StockWorker == StockStart \/ StockReqProcessing \/ PrepareSuccessResponse
                  \/ PrepareFailedResponse \/ ReplyCommitResponse
                  \/ ReplyCommitResponse2 \/ ReplyAbortMessage
                  \/ ReplyAbort2 \/ ReplyCheckoutResponse
                  \/ FailedCheckoutResponse \/ ReplyCompensateResponse
                  \/ step

(* Allow infinite stuttering to prevent deadlock on termination. *)
Terminating == /\ \A self \in ProcSet: pc[self] = "Done"
               /\ UNCHANGED vars

Next == Orchestrator \/ PaymentWorker \/ StockWorker
           \/ Terminating

Spec == /\ Init /\ [][Next]_vars
        /\ WF_vars(Orchestrator)
        /\ WF_vars(PaymentWorker)
        /\ WF_vars(StockWorker)

Termination == <>(\A self \in ProcSet: pc[self] = "Done")

\* END TRANSLATION 

=============================================================================
