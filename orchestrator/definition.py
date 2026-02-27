from dataclasses import dataclass, field


@dataclass
class Step:
    """A single step in a distributed transaction."""
    name: str
    service: str           # "stock" or "payment"
    action: str            # "try_reserve" (saga) / "prepare" (2pc)
    compensate: str        # "cancel" (saga compensation)
    confirm: str           # "confirm" (saga/2pc commit)

    def build_command(self, saga_id: str, action_override: str | None = None,
                      **extra_fields) -> dict:
        """Build a stream command dict for this step."""
        cmd = {
            "saga_id": saga_id,
            "action": action_override or self.action,
        }
        cmd.update(extra_fields)
        return cmd


@dataclass
class TransactionDefinition:
    """Declarative definition of a distributed transaction."""
    name: str
    steps: list[Step] = field(default_factory=list)


# Pre-defined checkout transaction
checkout_tx = TransactionDefinition(
    name="checkout",
    steps=[
        Step(
            name="reserve_stock",
            service="stock",
            action="try_reserve",
            compensate="cancel",
            confirm="confirm",
        ),
        Step(
            name="reserve_payment",
            service="payment",
            action="try_reserve",
            compensate="cancel",
            confirm="confirm",
        ),
    ],
)
