"""Keyed pseudonyms for data leaving the backend (research exports, external Jev).

Port of common.Pseudonyms: HMAC-SHA256(salt, learnerRef), first 16 bytes hex, prefixed
"L-". Stable for one salt, unlinkable after the salt is rotated (docs/privacy.md).
"""
from __future__ import annotations

import hashlib
import hmac

# The shipped default of research.pseudonym_salt; public, hence insecure.
DEFAULT_SALT = "change-me"


def is_insecure_salt(salt: str | None) -> bool:
    """Pseudonyms keyed with a blank or public salt can be reversed by hashing guessed
    external refs, so such a salt must never key data that leaves the backend."""
    return salt is None or salt.strip() == "" or salt == DEFAULT_SALT


def of(learner_ref: str, salt: str) -> str:
    digest = hmac.new(salt.encode("utf-8"), learner_ref.encode("utf-8"), hashlib.sha256).hexdigest()
    return "L-" + digest[:32]  # first 16 bytes as hex
