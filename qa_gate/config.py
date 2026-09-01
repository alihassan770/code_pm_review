"""Machine-local configuration: `~/.config/odoo-qa-gate/config.yaml`.

Deliberately small. Everything that varies per *client* lives in Postgres, not
here, because clients are added through the UI by people who should not have to
edit YAML on the server. This file holds only what the process needs before it
can reach the database at all:

  * `database_url`  — where Postgres is.
  * `secret_key`    — encrypts client Odoo credentials at rest.
  * `odoo`          — the Odoo that acts as our identity provider (§ auth).
  * `session_hours` — how long a staff login lasts.

Generated with a fresh `secret_key` on first run, mode 0600.

`secret_key` is the one value here that cannot be regenerated casually: rotating
it makes every stored client credential undecryptable. `rotate_secret_key()` in
crypto.py exists for when that has to happen deliberately.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from . import paths

log = logging.getLogger(__name__)

CONFIG_FILE_ENV_VAR = "QA_GATE_CONFIG"
DEFAULT_DATABASE_URL = "postgresql:///odoo_qa_gate"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class OdooIdentity:
    """The Odoo instance we authenticate staff against.

    This is *our* Odoo — the one already holding `project.task` and the team's
    `res.users` — not a client's staging instance. Client instances are rows in
    Postgres and never appear here.
    """
    url: str = ""
    db: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.url and self.db)


@dataclass(frozen=True)
class Config:
    path: Path
    database_url: str = DEFAULT_DATABASE_URL
    secret_key: str = ""
    odoo: OdooIdentity = field(default_factory=OdooIdentity)
    session_hours: int = 12
    # Set to False only behind a TLS-terminating proxy you control; the session
    # cookie is marked Secure otherwise and will not survive plain HTTP.
    secure_cookies: bool = False
    # Reads client repositories: qa/knowledge.yml and qa/scenarios/. Optional —
    # a public repo works without one at a much lower rate limit — and left
    # empty here in favour of the environment or `gh auth token` on a laptop.
    # Read access is all it needs; the gate never pushes.
    github_token: str = ""


def config_path() -> Path:
    override = os.environ.get(CONFIG_FILE_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return paths.config_dir() / "config.yaml"


def new_secret_key() -> str:
    """A Fernet key. Kept here rather than in crypto.py so that generating a
    config never imports the cryptography stack."""
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


CONFIG_HEADER = """\
# odoo-qa-gate configuration.
#
# database_url   Postgres connection string. Defaults to a local `odoo_qa_gate` db.
# secret_key     Encrypts client Odoo credentials at rest. DO NOT change casually:
#                rotating it makes every stored client credential undecryptable.
# odoo           The Odoo we authenticate STAFF against - our own, not a client's.
#                Staff log in with their normal Odoo account; there is no signup.
# session_hours  How long a staff login lasts before re-authentication.
# secure_cookies Set true when served over HTTPS.
# github_token   Reads client repos (qa/knowledge.yml, qa/scenarios/). Optional:
#                falls back to QA_GATE_GITHUB_TOKEN, GITHUB_TOKEN, then
#                `gh auth token`. Read access only - the gate never pushes.
#
# Client staging instances are NOT configured here. They are rows in Postgres,
# added through the UI, with their credentials encrypted using secret_key.
"""


def load(path: Path | None = None) -> Config:
    """Read the config, then let the environment override it.

    Two deployment shapes have to work from one code path:

      * **A host we own.** Config file at `~/.config/odoo-qa-gate/config.yaml`,
        generated on first run, edited through the UI.
      * **Railway or any container.** The filesystem is ephemeral, so a
        generated file is worse than useless — the secret key would change on
        every deploy and orphan every stored client credential. There, the
        environment is the source of truth and the file may not exist at all.

    Environment wins over file, so a container can set only what it needs and a
    laptop can ignore the environment entirely.
    """
    p = path or config_path()
    raw: dict = {}

    if p.is_file():
        _warn_if_loose_permissions(p)
        with p.open() as fh:
            raw = yaml.safe_load(fh) or {}
    elif not _env_provides_everything():
        # Only bother generating a file when the environment has not already
        # supplied what we need; on a read-only or throwaway filesystem this
        # would otherwise fail or silently rotate the key.
        try:
            paths.ensure_dirs()
            cfg = _from_raw(p, {}, secret=new_secret_key())
            save(cfg)
            log.info("Generated a new config at %s", p)
            return _apply_env(cfg)
        except OSError as exc:
            log.warning("Could not write a config file at %s (%s). "
                        "Falling back to environment only.", p, exc)

    cfg = _apply_env(_from_raw(p, raw))

    if not cfg.secret_key:
        # An empty key would mean storing client credentials in clear text.
        # Mint one rather than starting up in that state — but say so loudly in
        # a container, where the new key will not survive the next deploy.
        cfg = replace(cfg, secret_key=new_secret_key())
        try:
            save(cfg)
            log.warning("Config had no secret_key; generated one in %s", p)
        except OSError:
            log.error(
                "No %s set and no writable config file. A key was generated for "
                "this process only — every client credential stored now becomes "
                "undecryptable on restart. Set %s.",
                ENV_SECRET_KEY, ENV_SECRET_KEY,
            )
    return cfg


# ---- environment overlay ---------------------------------------------------

ENV_DATABASE_URL = "DATABASE_URL"          # Railway injects this for its Postgres
ENV_SECRET_KEY = "QA_GATE_SECRET_KEY"
ENV_ODOO_URL = "QA_GATE_ODOO_URL"
ENV_ODOO_DB = "QA_GATE_ODOO_DB"
ENV_SESSION_HOURS = "QA_GATE_SESSION_HOURS"
ENV_SECURE_COOKIES = "QA_GATE_SECURE_COOKIES"
ENV_GITHUB_TOKEN = "QA_GATE_GITHUB_TOKEN"


def _from_raw(p: Path, raw: dict, *, secret: str = "") -> Config:
    odoo_raw = raw.get("odoo") or {}
    return Config(
        path=p,
        database_url=raw.get("database_url") or DEFAULT_DATABASE_URL,
        secret_key=secret or raw.get("secret_key") or "",
        odoo=OdooIdentity(
            url=(odoo_raw.get("url") or "").rstrip("/"),
            db=odoo_raw.get("db") or "",
        ),
        session_hours=int(raw.get("session_hours") or 12),
        secure_cookies=bool(raw.get("secure_cookies", False)),
        github_token=raw.get("github_token") or "",
    )


def _env_provides_everything() -> bool:
    """True when the environment alone can boot the app, file or no file."""
    return bool(os.environ.get(ENV_DATABASE_URL) and os.environ.get(ENV_SECRET_KEY))


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _apply_env(cfg: Config) -> Config:
    """Overlay environment variables. Anything unset leaves the file value."""
    odoo = cfg.odoo
    url = os.environ.get(ENV_ODOO_URL)
    db = os.environ.get(ENV_ODOO_DB)
    if url or db:
        odoo = OdooIdentity(
            url=(url or odoo.url).rstrip("/"),
            db=db or odoo.db,
        )
    return replace(
        cfg,
        database_url=os.environ.get(ENV_DATABASE_URL) or cfg.database_url,
        secret_key=os.environ.get(ENV_SECRET_KEY) or cfg.secret_key,
        odoo=odoo,
        session_hours=int(os.environ.get(ENV_SESSION_HOURS) or cfg.session_hours),
        secure_cookies=_bool_env(ENV_SECURE_COOKIES, cfg.secure_cookies),
        github_token=os.environ.get(ENV_GITHUB_TOKEN) or cfg.github_token,
    )


def env_driven() -> bool:
    """Whether identity came from the environment rather than the file.

    The /setup page uses this: on Railway, writing the answer to a file that
    vanishes on redeploy would look like it worked and then silently revert.
    """
    return bool(os.environ.get(ENV_ODOO_URL) and os.environ.get(ENV_ODOO_DB))


def save(cfg: Config) -> None:
    cfg.path.parent.mkdir(parents=True, exist_ok=True)
    text = CONFIG_HEADER + yaml.safe_dump(
        {
            "database_url": cfg.database_url,
            "secret_key": cfg.secret_key,
            "odoo": {"url": cfg.odoo.url, "db": cfg.odoo.db},
            "session_hours": cfg.session_hours,
            "secure_cookies": cfg.secure_cookies,
            "github_token": cfg.github_token,
        },
        sort_keys=False, default_flow_style=False,
    )
    # Write via a temp file so a crash mid-write cannot leave a config with a
    # truncated secret_key, which would silently orphan every stored credential.
    tmp = cfg.path.with_suffix(cfg.path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, paths.SECURE_FILE_MODE)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(tmp, paths.SECURE_FILE_MODE)
    os.replace(tmp, cfg.path)
    os.chmod(cfg.path, paths.SECURE_FILE_MODE)


def _warn_if_loose_permissions(p: Path) -> None:
    try:
        mode = p.stat().st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        log.warning(
            "%s has loose permissions (%o). Run `chmod 600 %s` — it holds the "
            "key that decrypts every client's Odoo credentials.", p, mode, p,
        )
