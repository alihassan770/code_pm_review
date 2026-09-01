"""JSON-RPC client for Odoo.

Used for two different jobs that happen to share a protocol:

  1. **Identity.** Authenticating HST staff against our own Odoo. There is
     no signup: a person can log in because they exist in `res.users`, and
     removing them there removes their access here.
  2. **Client staging instances** (phase B onwards): census, pre-flight audit,
     fingerprint, and dispatching scenarios to the probe.

JSON-RPC over HTTPS rather than XML-RPC because it is the floor that exists for
every client — on Odoo.sh and Cloudpepper there is no shell and no Postgres, so
nothing may assume more than this.

## The credential rule, verified in odoo/addons/base/models/res_users.py

`_check_credentials` puts the API-key branch behind `if not interactive:`.
Interactive means a web login. So:

  * RPC (this module):     an API key works, and is REQUIRED when the user has
                           2FA enabled. A plain password fails for 2FA users.
  * Browser session:       an API key does NOT work. Playwright needs a real
                           password (see phase G / plan §7 personas).

That asymmetry is why `authenticate()` mentions API keys in its failure message:
for a 2FA account the password is not merely wrong, it is not accepted at all,
and saying "invalid credentials" would send someone resetting a working password.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20.0


class OdooError(Exception):
    """A transport or protocol failure. Not the same as bad credentials."""


class OdooAuthError(Exception):
    """Credentials were rejected. Carries a message safe to show a user."""


@dataclass(frozen=True)
class OdooUser:
    uid: int
    login: str
    name: str
    email: str
    is_admin: bool


class OdooClient:
    """One instance, one database, optionally one authenticated user.

    Deliberately not a connection pool: Odoo's JSON-RPC is stateless and every
    call carries its own credentials, so there is no session to keep warm.
    """

    def __init__(self, url: str, db: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        if not url:
            raise OdooError("No Odoo URL configured.")
        self.url = url.rstrip("/")
        self.db = db
        self.timeout = timeout

    # ---- low level ---------------------------------------------------------

    def _rpc(self, service: str, method: str, args: list[Any]) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": None,
        }
        try:
            resp = httpx.post(
                f"{self.url}/jsonrpc", json=payload, timeout=self.timeout,
                follow_redirects=True,
            )
            resp.raise_for_status()
            body = resp.json()
        except httpx.HTTPError as exc:
            raise OdooError(f"Could not reach Odoo at {self.url}: {exc}") from exc
        except ValueError as exc:
            # Almost always an HTML error page or a login redirect, which means
            # the URL points at something that is not an Odoo JSON-RPC endpoint.
            raise OdooError(
                f"{self.url}/jsonrpc did not return JSON. Check the URL points "
                "at an Odoo instance."
            ) from exc

        if "error" in body:
            err = body["error"]
            data = err.get("data") or {}
            message = data.get("message") or err.get("message") or "Unknown Odoo error"
            name = data.get("name", "")
            if "AccessDenied" in name:
                raise OdooAuthError(message)
            raise OdooError(f"{name or 'Odoo error'}: {message}")
        return body.get("result")

    def execute_kw(
        self, uid: int, password: str, model: str, method: str,
        args: list[Any] | None = None, kwargs: dict[str, Any] | None = None,
    ) -> Any:
        return self._rpc(
            "object", "execute_kw",
            [self.db, uid, password, model, method, args or [], kwargs or {}],
        )

    # ---- identity ----------------------------------------------------------

    def version(self) -> dict:
        """Server version. Reaches the host but says nothing about the database.

        `common.version` ignores the `db` argument completely, so it will happily
        succeed against a database name that does not exist. Use
        `check_database()` when the point is to validate the database.
        """
        return self._rpc("common", "version", []) or {}

    def check_database(self) -> None:
        """Raise OdooError unless this database exists and can be opened.

        There is no "does this database exist" call, and `db.list` is blocked on
        every hardened deployment (`list_db = False`, the default on Odoo Online).
        So we attempt an authentication with credentials that cannot be valid and
        read the failure mode:

          * database missing  -> the server raises while opening the registry,
                                 which surfaces as a psycopg error
          * database fine     -> authentication simply returns falsy

        In other words a *rejected login* is the success signal here.
        """
        probe_login = "__qa_gate_connectivity_probe__"
        try:
            self._rpc("common", "authenticate", [self.db, probe_login, "", {}])
        except OdooAuthError:
            # Credentials refused, which is the expected outcome: the database
            # opened fine and simply declined an unknown user.
            return
        except OdooError as exc:
            message = str(exc)
            if "does not exist" in message or "database" in message.lower():
                raise OdooError(
                    f"Reached the server, but it has no database named "
                    f"{self.db!r}. Check the exact name — on Odoo Online it "
                    f"looks like `company-main-1234567`, not the subdomain."
                ) from exc
            raise

    def authenticate(self, login: str, secret: str) -> int:
        """Return the uid, or raise OdooAuthError.

        `secret` is a password or an API key; Odoo accepts either here and we
        cannot tell which we were given, which is fine — we never store it.
        """
        if not login or not secret:
            raise OdooAuthError("Enter your Odoo login and password.")
        try:
            uid = self._rpc("common", "authenticate", [self.db, login, secret, {}])
        except OdooAuthError:
            uid = False
        if not uid:
            raise OdooAuthError(
                "Odoo rejected those credentials. If your account has "
                "two-factor authentication enabled, a password will never work "
                "here — create an API key in Odoo under Preferences → Account "
                "Security and use that instead."
            )
        return int(uid)

    def user_details(self, uid: int, secret: str) -> OdooUser:
        rows = self.execute_kw(
            uid, secret, "res.users", "read",
            [[uid]], {"fields": ["login", "name", "email"]},
        )
        if not rows:
            raise OdooError(f"Authenticated as uid {uid} but could not read the user record.")
        row = rows[0]

        # Non-fatal: a version whose has_group signature differs should degrade
        # to "not an admin" rather than block the login entirely.
        is_admin = False
        try:
            is_admin = bool(self.execute_kw(
                uid, secret, "res.users", "has_group", [uid, "base.group_system"],
            ))
        except (OdooError, OdooAuthError) as exc:
            log.warning("has_group check failed for uid %s, assuming non-admin: %s", uid, exc)

        return OdooUser(
            uid=uid,
            login=row.get("login") or "",
            name=row.get("name") or row.get("login") or "",
            email=row.get("email") or "",
            is_admin=is_admin,
        )

    def login(self, login: str, secret: str) -> OdooUser:
        """authenticate + read, which is what the login form actually wants."""
        uid = self.authenticate(login, secret)
        return self.user_details(uid, secret)
