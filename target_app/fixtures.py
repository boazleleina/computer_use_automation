"""In-memory member records for the legacy target application.

A dict, not a database. The app is a fixture: it exists so the automation has a
realistic legacy UI to work against, and every scenario the classifier must
distinguish has a member behind it.

Names are synthetic.
Member records only. 

Mutations made by the sub-account flow land in the working copy. reset_members()
restores the seed, which is what /_test/reset calls between runs. The copy is
deep because a member holds a nested sub_accounts list: a shallow copy would let
an append reach the seed, and every later reset would restore the corruption.
"""

from copy import deepcopy
from typing import Any, Final

STATUS_ACTIVE: Final = "ACTIVE"
STATUS_RESTRICTED: Final = "RESTRICTED"
STATUS_CLOSED: Final = "CLOSED"

SUB_ACCOUNT_TYPES: Final = (
    ("02", "Regular Savings"),
    ("07", "Vacation Club"),
    ("11", "Holiday Club"),
    ("22", "Custodial Savings"),
)

SUB_ACCOUNT_PURPOSES: Final = (
    ("PERSONAL", "Personal savings"),
    ("HOUSEHOLD", "Household expenses"),
    ("EDUCATION", "Education"),
)

# Several balance-shaped fields on purpose. A capability that means to read the
# savings balance must say which one it means, rather than taking the first
# number it finds on the page.
_SEED_MEMBERS: Final[dict[str, dict[str, Any]]] = {
    "100045": {
        "number": "100045",
        "name": "Test Member One",
        "status": STATUS_ACTIVE,
        "branch": "0031 - Riverside",
        "joined": "14/03/2009",
        "savings_balance": "4820.55",
        "share_draft_balance": "612.09",
        "certificate_balance": "10000.00",
        "available_credit": "2500.00",
        "last_contact": "02/09/2026",
        "mailing_preference": "Paper statement",
        "tax_id_masked": "TIN ****4471",
        "officer": "T. Miller",
        "sub_accounts": [],
    },
    "100046": {
        "number": "100046",
        "name": "Test Member Two",
        "status": STATUS_ACTIVE,
        "branch": "0031 - Riverside",
        "joined": "28/11/2021",
        "savings_balance": "12.40",
        "share_draft_balance": "0.00",
        "certificate_balance": "0.00",
        "available_credit": "0.00",
        "last_contact": "19/08/2026",
        "mailing_preference": "Electronic statement",
        "tax_id_masked": "TIN ****9902",
        "officer": "T. Miller",
        "sub_accounts": [],
    },
    "100047": {
        "number": "100047",
        "name": "Test Member Three",
        "status": STATUS_RESTRICTED,
        "branch": "0044 - Northgate",
        "joined": "05/06/2016",
        "savings_balance": "0.00",
        "share_draft_balance": "0.00",
        "certificate_balance": "0.00",
        "available_credit": "0.00",
        "last_contact": "11/01/2026",
        "mailing_preference": "Paper statement",
        "tax_id_masked": "TIN ****1130",
        "officer": "R. Okafor",
        "sub_accounts": [],
    },
    "100048": {
        "number": "100048",
        "name": "Test Member Four",
        "status": STATUS_CLOSED,
        "branch": "0031 - Riverside",
        "joined": "22/07/2011",
        "savings_balance": "0.00",
        "share_draft_balance": "0.00",
        "certificate_balance": "0.00",
        "available_credit": "0.00",
        "last_contact": "30/04/2025",
        "mailing_preference": "Paper statement",
        "tax_id_masked": "TIN ****6658",
        "officer": "R. Okafor",
        "sub_accounts": [],
    },
}

_members: dict[str, dict[str, Any]] = deepcopy(_SEED_MEMBERS)


def reset_members() -> None:
    """Discard every mutation and restore the seed records."""
    global _members
    _members = deepcopy(_SEED_MEMBERS)


def get_member(number: str) -> dict[str, Any] | None:
    """One member, or None when no record matches."""
    return _members.get(number)


def add_sub_account(number: str, account_type: str, nickname: str, deposit: str) -> str:
    """Attach a sub-account and return its generated suffix.

    The suffix is derived from how many sub-accounts already exist, so the
    confirmation page shows a value that could not have been known before the
    flow completed.
    """
    member = _members[number]
    existing = member["sub_accounts"]
    suffix = f"{number}-{len(existing) + 1:02d}"
    existing.append(
        {
            "suffix": suffix,
            "type_code": account_type,
            "type_label": dict(SUB_ACCOUNT_TYPES).get(account_type, account_type),
            "nickname": nickname,
            "opening_deposit": deposit,
        }
    )
    return suffix
