"""
Operator credentials for the fixture application.

"""

import hmac
import os

OPERATOR_USERNAME = os.environ.get("TARGET_APP_USER", "tmiller")
OPERATOR_PASSWORD = os.environ.get("TARGET_APP_PASSWORD", "")


def credentials_valid(username: str, password: str) -> bool:
    """Whether these are the configured operator credentials.

    Compared with hmac.compare_digest rather than ==.
    """
    if not OPERATOR_PASSWORD:
        raise RuntimeError("TARGET_APP_PASSWORD is not set. See .env.example.")
    return hmac.compare_digest(username, OPERATOR_USERNAME) and hmac.compare_digest(
        password, OPERATOR_PASSWORD
    )
