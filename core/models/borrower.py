from dataclasses import dataclass
from enum import Enum


class BorrowerStatus(str, Enum):
    PENDING = "PENDING"      # not yet called
    IN_PROGRESS = "IN_PROGRESS"  # currently reserved / being called
    DONE = "DONE"             # call completed (any outcome)


@dataclass
class Borrower:
    id: str
    name: str
    phone: str
    status: BorrowerStatus = BorrowerStatus.PENDING
