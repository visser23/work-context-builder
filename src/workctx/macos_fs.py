"""macOS: let this process download cloud placeholders (OneDrive/iCloud) on access.

A process started by ``launchd`` has the per-process I/O policy
``IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES`` set to *off*. Any ``stat``/``listdir``/``mkdir``/
``open`` that needs a dataless (cloud-only) file or folder then fails immediately with
``EDEADLK`` ("Resource deadlock avoided") instead of waiting for the File Provider to fetch it,
and no amount of retrying helps. A shell started by the user has the policy on, which is why
the same path works interactively. Switching it on for this process fixes the daemon.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)

# <sys/resource.h>
IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES = 3
IOPOL_SCOPE_PROCESS = 0
IOPOL_MATERIALIZE_DATALESS_FILES_OFF = 1
IOPOL_MATERIALIZE_DATALESS_FILES_ON = 2


def enable_dataless_materialization(libc: Any = None) -> bool:
    """Turn dataless-file materialisation on for this process. Returns True if it is on.

    No-op (False) off macOS. Never raises: failing to set the policy only means cloud-only
    files keep failing with EDEADLK, as before.
    """
    if sys.platform != "darwin":
        return False
    try:
        libc = libc if libc is not None else ctypes.CDLL(None, use_errno=True)
        kind, scope = IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, IOPOL_SCOPE_PROCESS
        if libc.getiopolicy_np(kind, scope) == IOPOL_MATERIALIZE_DATALESS_FILES_ON:
            return True
        if libc.setiopolicy_np(kind, scope, IOPOL_MATERIALIZE_DATALESS_FILES_ON) != 0:
            logger.warning("Could not enable materialisation of cloud-only files (setiopolicy_np)")
            return False
    except (OSError, AttributeError) as exc:
        logger.warning("Could not enable materialisation of cloud-only files: %s", exc)
        return False
    logger.info("Enabled materialisation of cloud-only (dataless) files for this process")
    return True
