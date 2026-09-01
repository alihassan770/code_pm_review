"""A fake *client staging instance*, enough to exercise the census and the audit.

Distinct from `fake_odoo.py`, which stands in for our identity Odoo and only
implements the login path. This one answers `search_read` / `search_count`
against a mutable table of records, because every audit check is a question
about what rows exist on the instance — and the tests need to be able to make an
instance dirty, re-audit it, and see the verdict flip.

Deliberately not a general Odoo emulator. The domain evaluator handles `=`, `!=`
and `in` on a scalar field, which is everything the gate asks for; anything more
would be building a second ORM to test the first one.
"""
from __future__ import annotations

import json
import threading
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, HTTPServer

DB = "acme_staging"
LOGIN = "qa_bot"
API_KEY = "key-staging"
UID = 4
VERSION = "17.0"

# A clean instance: opted in, no crons, no mail, one payment provider in test
# mode, a base URL that matches where the gate reaches it.
CLEAN = {
    "ir.config_parameter": [
        {"id": 1, "key": "web.base.url", "value": "http://127.0.0.1:8901"},
        {"id": 2, "key": "database.uuid", "value": "abc-123"},
    ],
    "ir.module.module": [
        {"id": 1, "name": "base", "latest_version": "17.0.1.3",
         "state": "installed", "author": "Odoo S.A.", "shortdesc": "Base"},
        {"id": 2, "name": "hst_kill_sheet", "latest_version": "17.0.1.0.0",
         "state": "installed", "author": "HSxTech", "shortdesc": "Kill sheet"},
        {"id": 3, "name": "muk_web_theme", "latest_version": "17.0.1.2.1",
         "state": "installed", "author": "MuK IT", "shortdesc": "Theme"},
    ],
    "ir.ui.view": [
        {"id": 10, "name": "mrp.production.form", "model": "mrp.production",
         "inherit_id": False, "write_date": "2026-08-01 09:00:00"},
        {"id": 11, "name": "kill sheet patch", "model": "mrp.production",
         "inherit_id": [10, "mrp.production.form"], "write_date": "2026-08-20 11:00:00"},
        {"id": 12, "name": "theme patch", "model": "mrp.production",
         "inherit_id": [10, "mrp.production.form"], "write_date": "2026-08-21 11:00:00"},
    ],
    "ir.model.data": [
        {"id": 1, "model": "ir.ui.view", "res_id": 11, "module": "hst_kill_sheet"},
        {"id": 2, "model": "ir.ui.view", "res_id": 12, "module": "muk_web_theme"},
        {"id": 3, "model": "ir.model.fields", "res_id": 3, "module": "hst_kill_sheet"},
    ],
    "ir.model.fields": [
        {"id": 1, "name": "x_pen_number", "model": "mrp.production", "ttype": "char",
         "store": True, "state": "manual", "relation": False},
        {"id": 2, "name": "name", "model": "mrp.production", "ttype": "char",
         "store": True, "state": "base", "relation": False},
        # Owned by hst_kill_sheet through ir.model.data below: this is how the
        # census learns which models a module touches without reading source.
        {"id": 3, "name": "x_carcass_weight", "model": "mrp.production",
         "ttype": "float", "store": True, "state": "base", "relation": False},
    ],
    "ir.cron": [
        {"id": 1, "cron_name": "Nightly invoicing", "active": False,
         "model_id": [55, "account.move"]},
    ],
    "ir.mail_server": [
        {"id": 1, "name": "Client SMTP", "active": False},
    ],
    "payment.provider": [
        {"id": 1, "name": "Stripe", "state": "test", "code": "stripe"},
    ],
}


class State:
    """One instance's records, mutable from a test."""

    def __init__(self, records: dict | None = None):
        self.records = deepcopy(records if records is not None else CLEAN)

    def reset(self, records: dict | None = None) -> None:
        self.records = deepcopy(records if records is not None else CLEAN)

    # Small helpers so tests read as statements about the instance rather than
    # as list surgery.
    def set_param(self, key: str, value: str) -> None:
        for row in self.records["ir.config_parameter"]:
            if row["key"] == key:
                row["value"] = value
                return
        self.records["ir.config_parameter"].append(
            {"id": 900 + len(self.records["ir.config_parameter"]),
             "key": key, "value": value})

    def drop_param(self, key: str) -> None:
        self.records["ir.config_parameter"] = [
            r for r in self.records["ir.config_parameter"] if r["key"] != key]

    def add(self, model_name: str, **values) -> None:
        # `model_name`, not `model`: several Odoo models have a field called
        # `model` (ir.model.data, ir.model.fields), and naming the parameter
        # after the table would make those records impossible to add.
        rows = self.records.setdefault(model_name, [])
        rows.append({"id": 500 + len(rows), **values})

    def remove_model(self, model: str) -> None:
        self.records.pop(model, None)


STATE = State()


def _match(row: dict, domain: list) -> bool:
    for clause in domain or []:
        if not isinstance(clause, (list, tuple)) or len(clause) != 3:
            continue  # '&' / '|' are never sent by the gate
        field, op, want = clause
        have = row.get(field)
        if isinstance(have, list) and have:  # many2one comes back as [id, name]
            have = have[0]
        if op == "=":
            ok = have == want
        elif op == "!=":
            ok = have != want
        elif op == "in":
            ok = have in want
        else:
            raise RuntimeError(f"fake_staging cannot evaluate {op!r}")
        if not ok:
            return False
    return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.path != "/jsonrpc":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        p = body["params"]
        try:
            payload = {"jsonrpc": "2.0", "id": None,
                       "result": self.dispatch(p["service"], p["method"], p["args"])}
        except Exception as exc:  # noqa: BLE001
            payload = {"jsonrpc": "2.0", "id": None, "error": {
                "message": "Odoo Server Error",
                "data": {"name": type(exc).__name__, "message": str(exc)}}}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def dispatch(self, service, method, args):
        if service == "common" and method == "version":
            return {"server_version": VERSION, "protocol_version": 1}
        if service == "common" and method == "authenticate":
            db, login, secret, _ = args
            if db != DB:
                raise RuntimeError("database not found")
            return UID if (login == LOGIN and secret == API_KEY) else False
        if service == "object" and method == "execute_kw":
            db, uid, secret, model, meth, m_args, m_kwargs = args
            if uid != UID or secret != API_KEY:
                raise RuntimeError("AccessDenied")
            return self.model_call(model, meth, m_args, m_kwargs or {})
        raise RuntimeError(f"unsupported {service}.{method}")

    def model_call(self, model, meth, args, kwargs):
        records = STATE.records

        # The credential form calls OdooClient.login, which reads the user back
        # after authenticating. The QA bot is a plain internal user: not an
        # administrator, which is what a client should be provisioning for us.
        if model == "res.users" and meth == "read":
            return [{"id": UID, "login": LOGIN, "name": "QA Bot",
                     "email": "qa@acme.test"}]
        if model == "res.users" and meth == "has_group":
            return False

        # `ir.model` is answered from the keys of the record table: a model the
        # instance has no rows for is a model the instance does not have, which
        # is exactly how an uninstalled app behaves over RPC.
        if model == "ir.model" and meth == "search_count":
            domain = args[0] if args else []
            target = next((c[2] for c in domain if c[0] == "model" and c[1] == "="), None)
            return 1 if target in records else 0

        if model not in records:
            raise RuntimeError(f"Object {model} doesn't exist")

        rows = [r for r in records[model] if _match(r, args[0] if args else [])]
        if meth == "search_count":
            return len(rows)
        if meth != "search_read":
            raise RuntimeError(f"unsupported {model}.{meth}")

        fields = kwargs.get("fields")
        if fields:
            known = {k for r in records[model] for k in r}
            missing = set(fields) - known - {"id"}
            if missing:
                # Mirrors the real server: an unknown field is an error, which
                # is what Connection.search_read's tolerant retry exists for.
                raise RuntimeError(f"Invalid field {sorted(missing)[0]!r} on model {model!r}")
        order = kwargs.get("order") or ""
        if order.startswith("write_date desc"):
            rows = sorted(rows, key=lambda r: r.get("write_date") or "", reverse=True)
        if kwargs.get("limit"):
            rows = rows[:kwargs["limit"]]
        if not fields:
            return [dict(r) for r in rows]
        return [{"id": r["id"], **{f: r.get(f, False) for f in fields if f != "id"}}
                for r in rows]


def serve(port: int = 8901) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    serve()
    threading.Event().wait()
