"""A minimal Odoo JSON-RPC endpoint, enough to exercise qa_gate.odoo_client.

Implements the three calls the login path makes:
  common.version, common.authenticate, object.execute_kw(res.users read/has_group)

Mirrors the real semantics we care about, including the one that matters most:
a user with 2FA rejects a password and accepts only an API key.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

DB = "qa_identity"
USERS = {
    # login: (uid, password, api_key, name, email, is_admin, totp_enabled)
    "hamza": (7, "hunter2", "key-hamza", "Hamza Q", "hamza@hsx.test", True, False),
    "pm": (9, "pmpass", "key-pm", "Priya M", "pm@hsx.test", False, False),
    "twofa": (11, None, "key-twofa", "Two Factor", "2fa@hsx.test", False, True),
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def do_POST(self):
        if self.path != "/jsonrpc":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        p = body["params"]
        try:
            result = self.dispatch(p["service"], p["method"], p["args"])
            payload = {"jsonrpc": "2.0", "id": None, "result": result}
        except Exception as exc:  # noqa: BLE001
            payload = {"jsonrpc": "2.0", "id": None, "error": {
                "message": "Odoo Server Error",
                "data": {"name": type(exc).__name__, "message": str(exc)},
            }}
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def dispatch(self, service, method, args):
        if service == "common" and method == "version":
            return {"server_version": "19.0", "protocol_version": 1}

        if service == "common" and method == "authenticate":
            db, login, secret, _ = args
            if db != DB:
                raise RuntimeError("database not found")
            return self._auth(login, secret)

        if service == "object" and method == "execute_kw":
            db, uid, secret, model, meth, m_args, m_kwargs = args
            login = self._login_for_uid(uid)
            if login is None or not self._auth(login, secret):
                raise RuntimeError("AccessDenied")
            rec = USERS[login]
            if model == "res.users" and meth == "read":
                return [{"id": uid, "login": login, "name": rec[3], "email": rec[4]}]
            if model == "res.users" and meth == "has_group":
                return rec[5] if m_args[1] == "base.group_system" else False
            raise RuntimeError(f"unsupported {model}.{meth}")

        raise RuntimeError(f"unsupported {service}.{method}")

    def _auth(self, login, secret):
        rec = USERS.get(login)
        if not rec:
            return False
        uid, password, api_key, _n, _e, _a, totp = rec
        # The rule verified in res_users.py: this endpoint is non-interactive,
        # so an API key always works and a password fails when 2FA is on.
        if secret == api_key:
            return uid
        if not totp and password is not None and secret == password:
            return uid
        return False

    def _login_for_uid(self, uid):
        for login, rec in USERS.items():
            if rec[0] == uid:
                return login
        return None


def serve(port: int = 8899) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    serve()
    threading.Event().wait()
