"""Keychain-backed secret management."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

SERVICE_NAME = "WorkContextMirror"


def get_secret(secret_ref: str) -> str | None:
    """Retrieve a secret by its logical reference.

    Tries the OS credential store first (Keychain on macOS, Credential
    Locker on Windows, Secret Service on Linux), then falls back to
    environment variables (useful for development/CI).
    """
    env_key = _ref_to_env_key(secret_ref)
    env_val = os.environ.get(env_key)
    if env_val:
        return env_val

    try:
        import keyring

        value = keyring.get_password(SERVICE_NAME, secret_ref)
        if value:
            return value
    except Exception:
        logger.debug("Keychain lookup failed for %s", secret_ref, exc_info=True)

    return None


def set_secret(secret_ref: str, value: str) -> None:
    """Store a secret in the OS credential store."""
    import keyring

    keyring.set_password(SERVICE_NAME, secret_ref, value)
    logger.info("Secret stored: %s", secret_ref)


def delete_secret(secret_ref: str) -> None:
    """Remove a secret from the OS credential store."""
    import keyring

    try:
        keyring.delete_password(SERVICE_NAME, secret_ref)
        logger.info("Secret removed: %s", secret_ref)
    except keyring.errors.PasswordDeleteError:
        logger.warning("Secret not found: %s", secret_ref)


def _ref_to_env_key(secret_ref: str) -> str:
    """Convert a secret_ref like 'workctx/project/jira' to WORKCTX_PROJECT_JIRA."""
    return secret_ref.upper().replace("/", "_").replace("-", "_")
