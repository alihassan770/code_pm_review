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

    def call(self, model: str, method: str,
             args: list | None = None, kwargs: dict | None = None) -> Any:
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
    login, api_key = clients_mod.get_rpc_credentials(client.id, secret_key)
    if not login or not api_key:
        raise MissingCredentials(
            f"{client.slug} has no stored RPC credentials. Add them on the client page.")

    odoo = OdooClient(client.staging_url, client.staging_db, timeout=INSTANCE_TIMEOUT)
    version = ""
    try:
        version = str((odoo.version() or {}).get("server_version", ""))
    except OdooError:
        # Non-fatal: some reverse proxies restrict `common.version` while
        # leaving object calls open. Authentication below is the real check.
        log.info("%s: common.version unavailable, continuing", client.slug)
    uid = odoo.authenticate(login, api_key)
    return Connection(client=client, odoo=odoo, uid=uid, secret=api_key,
                      server_version=version)
