"""Where mutable state and configuration live.

Lifted from odoo-dev-loop's `paths.py`, which solved the same problem: a local
install wants state beside the package, a container wants it on a mounted
volume. Read from the environment at call time rather than at import, so tests
can point it somewhere temporary without reimporting the module.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

STATE_ENV_VAR = "QA_GATE_STATE_DIR"
CONFIG_ENV_VAR = "QA_GATE_CONFIG_DIR"

# 0700: the config directory holds the encryption key that protects every
# client's Odoo credentials. Nothing else on the machine needs to read it.
SECURE_DIR_MODE = 0o700
SECURE_FILE_MODE = 0o600


def state_dir() -> Path:
    """Bundles, screenshots, and anything else large and regenerable."""
    override = os.environ.get(STATE_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parent.parent / "state"


def config_dir() -> Path:
    """Secrets and machine-local settings. Never inside the repo."""
    override = os.environ.get(CONFIG_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "odoo-qa-gate"


def ensure_dirs() -> bool:
    """Best-effort. Returns True when the config directory is usable.

    Deliberately does not raise. In a container `$HOME` is often absent or
    read-only, and there the environment supplies the configuration anyway (see
    config.load) — so an unwritable config directory is a normal deployment
    shape, not a startup failure. Raising here would make the app refuse to boot
    on Railway for no reason.
    """
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("State directory %s is not writable (%s). Evidence bundles "
                    "will need QA_GATE_STATE_DIR pointed at a mounted volume.",
                    state_dir(), exc)

    cfg = config_dir()
    try:
        cfg.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    try:
        cfg.chmod(SECURE_DIR_MODE)
    except OSError:
        # Windows, or a mount that does not support chmod. The warning in
        # config.load() will still fire if the file itself ends up readable.
        pass
    return True
