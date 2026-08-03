#!/usr/bin/env python3
"""Lightweight credential loading for container-based WEEX API access."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional


API_KEY_ENV = "WEEX_API_KEY"
API_SECRET_ENV = "WEEX_API_SECRET"
API_PASSPHRASE_ENV = "WEEX_API_PASSPHRASE"
ENVIRONMENT_CREDENTIAL_NAMES = (API_KEY_ENV, API_SECRET_ENV, API_PASSPHRASE_ENV)


@dataclass(frozen=True)
class ApiCredentials:
    api_key: str
    api_secret: str
    api_passphrase: str


def load_environment_credentials(
    env: Optional[Mapping[str, str]] = None,
) -> Optional[ApiCredentials]:
    """Return the standard WEEX environment credentials or fail on a partial set."""

    source = os.environ if env is None else env
    present = [name for name in ENVIRONMENT_CREDENTIAL_NAMES if name in source]
    if not present:
        return None

    values = {
        name: str(source.get(name) or "").strip()
        for name in ENVIRONMENT_CREDENTIAL_NAMES
    }

    missing = [name for name, value in values.items() if not value]
    if missing:
        raise SystemExit(
            "WEEX environment credentials must set WEEX_API_KEY, WEEX_API_SECRET, "
            "and WEEX_API_PASSPHRASE together. Missing: " + ", ".join(missing)
        )

    return ApiCredentials(
        api_key=values[API_KEY_ENV],
        api_secret=values[API_SECRET_ENV],
        api_passphrase=values[API_PASSPHRASE_ENV],
    )
