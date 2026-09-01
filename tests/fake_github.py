"""A fake GitHub API, enough to exercise qa_gate.github and qa_gate.repo_sync.

Four endpoints, which is all the gate uses: resolve a ref to a sha, read the
tree at that sha, read a blob, and ask when a path last changed.

Faked at the HTTP boundary rather than by mocking the GitHub class, so the tests
actually exercise the base64 decoding, the 404-versus-403 mapping, and the
recursive tree handling instead of asserting that a mock was called.
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

REPO = "hsxtech/acme"
HEAD = "a" * 40

KNOWLEDGE = """\
client: acme
invariants:
  - id: INV-01
    text: >
      The kill sheet must fit on a single page. A layout change that pushes to a
      second page is a regulatory problem, not a cosmetic one.
    scope: {models: [mrp.production], modules: [hst_kill_sheet]}
    added_by: hamza
    last_confirmed: 2026-08-12
    review_after: 2027-03-01
  - id: INV-02
    text: Stale on purpose, so the decay mechanism has something to find.
    scope: {modules: [hst_kill_sheet]}
    review_after: 2025-01-01
danger_zones:
  - id: DZ-01
    text: >
      The QuickBooks connector posts to the client's live accounting. Never
      exercise it without a registered stub, in any tier.
    scope: {modules: [pragmatic_quickbooks_connector]}
    review_after: 2026-12-01
expected_values:
  sales_tax_default: 8.25
  weight_uom: lb
unused_apps: [website_sale]
"""

SCENARIO_OK = """\
id: mrp.kill_sheet_totals
title: The kill sheet totals match the manufacturing order
tags: [mrp, regression, ratified]
versions: ["17.0"]
personas: [production_user]
tier: 2
drift: immune
covers: [AC1]
fixtures:
  order: {model: mrp.production, pick: {state: {"=": done}}, limit: 1}
steps:
  - assert: {expr: "$order.qty_produced", equals: 10.0, because: "ten head"}
"""

SCENARIO_NO_TIER = """\
id: sale.discount
title: A line discount reduces the order total
steps:
  - create: {model: sale.order, values: {}, as: order}
"""

SCENARIO_TIER_4 = """\
id: stock.legacy_clone
title: Carried over from revision 2
tier: 4
steps:
  - create: {model: stock.picking, values: {}}
"""

# path -> file content. A repo with two of our modules, one of which has no
# scenario at all — the row the coverage map exists to surface.
FILES = {
    "README.md": "# Acme\n",
    "hst_kill_sheet/__manifest__.py": "{'name': 'Kill sheet'}\n",
    "hst_kill_sheet/models/production.py": "class X: pass\n",
    "hst_lot_weight/__manifest__.py": "{'name': 'Lot weight'}\n",
    "qa/knowledge.yml": KNOWLEDGE,
    "qa/scenarios/mrp/kill_sheet_totals.yml": SCENARIO_OK,
    "qa/scenarios/sale/discount.yml": SCENARIO_NO_TIER,
    "qa/scenarios/stock/legacy_clone.yml": SCENARIO_TIER_4,
    "qa/scenarios/notes.txt": "not a scenario, must be ignored\n",
}

COMMIT_DATES = {
    "hst_kill_sheet": "2026-08-29T09:14:00Z",
    "hst_lot_weight": "2026-08-30T16:02:00Z",
}


class State:
    def __init__(self):
        self.files = dict(FILES)
        self.head = HEAD
        self.repos = {REPO}

    def reset(self):
        self.files = dict(FILES)
        self.head = HEAD

    def blob_sha(self, path: str) -> str:
        return hashlib.sha1(path.encode()).hexdigest()

    def path_for(self, sha: str) -> str | None:
        for path in self.files:
            if self.blob_sha(path) == sha:
                return path
        return None


STATE = State()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        query = parse_qs(url.query)
        try:
            status, body = self.route(parts, query)
        except Exception as exc:  # noqa: BLE001
            status, body = 500, {"message": str(exc)}
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def route(self, parts, query):
        # /repos/{owner}/{repo}/...
        if len(parts) < 3 or parts[0] != "repos":
            return 404, {"message": "Not Found"}
        repo = f"{parts[1]}/{parts[2]}"
        if repo not in STATE.repos:
            return 404, {"message": "Not Found"}
        rest = parts[3:]

        # GET /repos/{repo}/commits/{ref}
        if len(rest) == 2 and rest[0] == "commits":
            if rest[1] in ("main", "master", STATE.head):
                return 200, {"sha": STATE.head}
            return 404, {"message": "No commit found for SHA"}

        # GET /repos/{repo}/commits?path=&sha=
        if rest == ["commits"]:
            path = (query.get("path") or [""])[0]
            module = path.rsplit("/", 1)[-1]
            date = COMMIT_DATES.get(module)
            if not date:
                return 200, []
            return 200, [{
                "sha": "c" * 40,
                "commit": {"message": f"touch {module}\n\nbody",
                           "author": {"date": date, "name": "Hamza Q"}},
            }]

        # GET /repos/{repo}/git/trees/{sha}?recursive=1
        if len(rest) == 3 and rest[:2] == ["git", "trees"]:
            return 200, {
                "sha": STATE.head, "truncated": False,
                "tree": [
                    {"path": p, "type": "blob", "sha": STATE.blob_sha(p),
                     "size": len(c)}
                    for p, c in sorted(STATE.files.items())
                ],
            }

        # GET /repos/{repo}/git/blobs/{sha}
        if len(rest) == 3 and rest[:2] == ["git", "blobs"]:
            path = STATE.path_for(rest[2])
            if path is None:
                return 404, {"message": "Not Found"}
            return 200, {
                "encoding": "base64",
                "content": base64.b64encode(STATE.files[path].encode()).decode(),
            }

        return 404, {"message": "Not Found"}


def serve(port: int = 8902) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    serve()
    threading.Event().wait()
