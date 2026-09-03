"""Reaching a client's staging instance over RPC.

Phase A could only ask "are these credentials valid". Everything from phase B on
needs to keep asking questions of the same instance, so this is the one place
that turns a `Client` row plus the encrypted credential into something callable.

Two things it deliberately does NOT do:

  * It does not cache connections. Odoo's JSON-RPC is stateless and every call
    carries its credentials, so a pool would only add a lifetime to manage.
  * It does not check the instance contract. `audit.py` does that, and keeping
    the two apart is what lets the hygiene audit (UC-16) run against instances
    that have not opted in yet — which is the entire point of the audit: telling
    you which instances are *not* safe.

Anything that intends to write must go through the audit first. Reading is what
this module is for.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from . import clients as clients_mod
from .clients import Client
from .odoo_client import OdooAuthError, OdooClient, OdooError

log = logging.getLogger(__name__)

#: Open browser sessions, by (client id, login). Odoo throttles repeated logins
#: — observed live: a persona that authenticated fine all morning began refusing
#: every attempt after a burst of session opens, and stayed refused. Each
#: `connect()` used to mint a fresh session, so a single review could ask for a
#: dozen. Reusing one is both kinder to the instance and the difference between
#: a review that runs and one that locks the account out mid-way.
#:
#: Deliberately per-process and never persisted: a session id is a live
#: credential, and one written to disk outlives the reason it existed.
_SESSIONS: dict[tuple[int, str], str] = {}


def forget_session(client_id: int, login: str = "") -> None:
    """Drop cached sessions so the next connect authenticates afresh."""
    for key in [k for k in _SESSIONS if k[0] == client_id and (not login or k[1] == login)]:
        _SESSIONS.pop(key, None)

# Longer than the identity default: a census asks for a few thousand rows from
# an instance that may be on a small managed plan, and a timeout here reads to
# the user as "the audit is broken" rather than "staging is slow".
INSTANCE_TIMEOUT = 45.0


class MissingCredentials(Exception):
    """No RPC credential stored for this client. Not an instance failure."""


@dataclass
class Connection:
    """An authenticated handle on one client staging instance.

    `uid` is resolved once at connect time rather than per call, because
    `common.authenticate` is a password check on every invocation and doing it
    per model read triples the cost of a census for nothing.
    """
    client: Client
    odoo: OdooClient
    uid: int
    secret: str
    server_version: str = ""
    #: Set when this connection is authenticated by a browser session rather
    #: than an API key. Both reach the same ORM; see `call`.
    session_id: str = ""
    #: "browser" or "api_key" — which credential opened it. Reported in the UI
    #: so nobody has to infer it from which fields happen to be filled in.
    via: str = "api_key"

    def call(self, model: str, method: str,
             args: list | None = None, kwargs: dict | None = None) -> Any:
        """One ORM call, over whichever credential this connection was opened with.

        The two transports are interchangeable for reading: `/jsonrpc` with an
        API key and `/web/dataset/call_kw` with a session cookie both land in
        `execute_kw` on the server. Keeping the choice here means census, audit
        and every future assertion are written once and work either way.
        """
        if self.session_id:
            return self.odoo.call_kw_session(self.session_id, model, method, args, kwargs)
        return self.odoo.execute_kw(self.uid, self.secret, model, method, args, kwargs)

    # ---- tolerant reads ----------------------------------------------------
    #
    # The gate spans Odoo 17, 18 and 19 and every client has a different set of
    # third-party modules. A field that exists on one instance is absent on the
    # next, and a model that ships with an app the client never installed simply
    # is not there. The alternative to tolerating that here is a version matrix
    # of field lists in every caller, which is the thing §16 warns turns into
    # unmaintainable per-version branches.

    def model_exists(self, model: str) -> bool:
        """Whether the model is present on this instance.

        Asked through `ir.model` rather than by calling the model and catching
        the error, so that a genuine failure (permissions, a broken override)
        is not silently reported as "the app is not installed".
        """
        try:
            return bool(self.call("ir.model", "search_count", [[("model", "=", model)]]))
        except (OdooError, OdooAuthError) as exc:
            log.warning("%s: could not check for model %s: %s", self.client.slug, model, exc)
            return False

    def search_read(self, model: str, domain: list, fields: list[str],
                    *, limit: int | None = None, order: str | None = None) -> list[dict]:
        """search_read that drops fields the instance does not have.

        Retries once with the intersection of the requested fields and what
        `ir.model.fields` reports, rather than parsing the error message — error
        text is translated and reworded between versions, field metadata is not.
        """
        kwargs: dict[str, Any] = {"fields": list(fields)}
        if limit:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order
        try:
            return self.call(model, "search_read", [domain], kwargs) or []
        except (OdooError, OdooAuthError):
            available = self.field_names(model)
            usable = [f for f in fields if f in available] or ["id"]
            if set(usable) == set(fields):
                raise
            log.info("%s: retrying %s.search_read without %s", self.client.slug, model,
                     sorted(set(fields) - set(usable)))
            kwargs["fields"] = usable
            return self.call(model, "search_read", [domain], kwargs) or []

    def field_names(self, model: str) -> set[str]:
        rows = self.call(
            "ir.model.fields", "search_read",
            [[("model", "=", model)]], {"fields": ["name"]},
        ) or []
        return {r["name"] for r in rows}


def connect(client: Client, secret_key: str) -> Connection:
    """Authenticate against a client's staging instance.

    Raises MissingCredentials when nothing is stored, OdooAuthError when the
    stored key no longer works (usually revoked in Odoo), and OdooError when the
    instance is unreachable. Three distinct failures because they need three
    different humans to do three different things.
    """
    if not client.staging_url or not client.staging_db:
        raise MissingCredentials(
            f"{client.slug} has no staging URL or database name configured.")

    odoo = OdooClient(client.staging_url, client.staging_db, timeout=INSTANCE_TIMEOUT)
    version = ""
    try:
        version = str((odoo.version() or {}).get("server_version", ""))
    except OdooError:
        # Non-fatal: some reverse proxies restrict `common.version` while
        # leaving object calls open. Authentication below is the real check.
        log.info("%s: common.version unavailable, continuing", client.slug)

    # The client says which credential it is configured with, and that choice is
    # honoured rather than guessed at. Falling back between the two would make a
    # revoked API key look like a working instance reached as somebody else —
    # the audit would then attribute its results to the wrong user.
    if client.access_mode == "api_key":
        login, api_key = clients_mod.get_rpc_credentials(client.id, secret_key)
        if not login or not api_key:
            raise MissingCredentials(
                f"{client.slug} is set to reach staging by API key, but none is "
                "stored. Add one on the client page, or switch it to browser "
                "sign-in — a browser login can read data as well as take "
                "screenshots, so it needs no API key.")
        uid = odoo.authenticate(login, api_key)
        return Connection(client=client, odoo=odoo, uid=uid, secret=api_key,
                          server_version=version, via="api_key")

    # A session opened with a real password reaches the same ORM *and* can drive
    # a browser, while an API key can only do the first. This is the default for
    # that reason, not as a fallback.
    from . import personas as personas_mod

    verified = [p for p in personas_mod.for_client(client.id)
                if p.state == "verified" and p.has_password]
    if not verified:
        raise MissingCredentials(
            f"{client.slug} has no way to reach its staging instance. Add either "
            "a browser sign-in (a real password, verified on the client page) or "
            "an API key. A browser sign-in is enough on its own — it can both "
            "take screenshots and read data.")

    # Deterministic rather than "whichever came back first", so two runs of the
    # same audit are not attributed to two different users.
    persona = sorted(verified, key=lambda p: (p.key != "primary", p.key))[0]
    password = personas_mod.password_of(persona.id, secret_key)
    if not password:
        raise MissingCredentials(
            f"{client.slug}: persona {persona.key!r} is marked verified but has "
            "no stored password. Re-enter it on the client page.")

    # Reuse a session we already hold for this persona, and only prove it is
    # still good with one cheap call. A dead session answers that call with
    # SessionExpired, which is the signal to log in again — and logging in again
    # is exactly what has to stay rare.
    cache_key = (client.id, persona.login)
    session_id = _SESSIONS.get(cache_key, "")
    if session_id:
        try:
            odoo.call_kw_session(session_id, "res.users", "context_get", [])
        except (OdooAuthError, OdooError):
            log.info("%s: cached session for %s is stale", client.slug, persona.login)
            _SESSIONS.pop(cache_key, None)
            session_id = ""

    try:
        if not session_id:
            session_id = odoo.open_session(persona.login, password)
            _SESSIONS[cache_key] = session_id
    except OdooAuthError as exc:
        # The credential worked when it was verified and does not now. Record it
        # against the persona so the client page stops claiming otherwise, then
        # raise something that names the fix rather than "Access Denied".
        personas_mod.record_failure(persona.id, str(exc))
        raise MissingCredentials(
            f"{client.slug}: the staging instance rejected the browser sign-in "
            f"for {persona.login} ({exc}). Three things do this, in order of "
            "likelihood: Odoo is rate-limiting after too many logins in a short "
            "time — wait a few minutes and try again, and note it clears on its "
            "own; the password was changed; or the staging database was rebuilt, "
            "which resets it. If waiting does not help, re-enter the password on "
            "the client page and press Verify login."
        ) from exc
    uid = 0
    try:
        uid = int((odoo.call_kw_session(session_id, "res.users", "context_get", [])
                   or {}).get("uid") or 0)
    except (OdooError, OdooAuthError):
        # The session is open; not knowing the uid costs nothing for reads.
        log.info("%s: session opened but context_get failed", client.slug)
    return Connection(client=client, odoo=odoo, uid=uid, secret="",
                      server_version=version, session_id=session_id, via="browser")
