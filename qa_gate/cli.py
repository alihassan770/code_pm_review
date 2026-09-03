"""`qa-gate` command line entry point."""
from __future__ import annotations

import argparse
import logging
import os
import sys

from . import config as config_mod
from . import db, paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="qa-gate", description="Odoo PM Agent control plane.")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the web app (default)")
    # Railway (and most PaaS) inject PORT and expect the process to bind 0.0.0.0.
    # Defaulting to loopback locally keeps client credentials off the network
    # on a laptop, where nothing is terminating TLS.
    serve.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8770")))
    serve.add_argument("--reload", action="store_true")
    serve.add_argument("--log-level", default="info")

    sub.add_parser("migrate", help="apply pending database migrations and exit")
    sub.add_parser("check", help="diagnose configuration and connectivity")

    ident = sub.add_parser(
        "set-identity",
        help="change which Odoo staff authenticate against",
        description="The /setup page closes itself once an identity is saved, so a "
                    "wrong URL or database would otherwise lock everyone out — nobody "
                    "can log in, and only a logged-in user could fix it. This is that "
                    "escape hatch. The connection and the database are both verified "
                    "before anything is written.",
    )
    ident.add_argument("--url", required=True)
    ident.add_argument("--db", required=True)

    grant = sub.add_parser(
        "grant-admin",
        help="make an existing user an administrator of this app",
        description="Administrator here is not the same as Odoo's base.group_system: "
                    "the person who runs the gate is often not an Odoo sysadmin. The "
                    "first user to sign in is promoted automatically; this is how to "
                    "promote anyone after that.")
    grant.add_argument("login")

    audit = sub.add_parser(
        "audit",
        help="run the read-only hygiene audit against client staging instances",
        description="UC-16. Eight checks per instance, nothing written anywhere. "
                    "Exits non-zero if any instance would be refused, so it can be "
                    "wired into a nightly job.",
    )
    audit.add_argument("slug", nargs="*",
                       help="client slugs; omit to audit every client with credentials")
    audit.add_argument("--no-store", action="store_true",
                       help="print the result without recording it")

    know = sub.add_parser(
        "knowledge",
        help="read a client's qa/knowledge.yml and qa/scenarios/ from GitHub",
        description="Refreshes the parsed cache of the client repo and prints what it "
                    "found. The knowledge base itself stays in git; this only reads it.",
    )
    know.add_argument("slug", nargs="*",
                      help="client slugs; omit to sync every client with a GitHub repo")

    sub.add_parser(
        "leftovers",
        help="list records reviews created and could not remove",
        description="The ledger in review_fixtures exists so that 'did a review "
                    "leave anything in a client's database?' is an answerable "
                    "question. This answers it. Nothing is deleted — removing "
                    "records from a client's instance on a timer is exactly the "
                    "unattended write the gate is careful about.")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(args, "log_level", "info").upper(),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
    )

    if args.command == "migrate":
        return _migrate()
    if args.command == "check":
        return _check()
    if args.command == "grant-admin":
        from . import users as users_mod
        db.init_pool(config_mod.load().database_url)
        if users_mod.grant_admin(args.login):
            print(f"  {args.login} is now an administrator.")
            return 0
        print(f"  No user with login {args.login!r}. They must sign in once first.")
        return 1
    if args.command == "set-identity":
        return _set_identity(args.url, args.db)
    if args.command == "audit":
        return _audit(args)
    if args.command == "knowledge":
        return _knowledge(args)
    if args.command == "leftovers":
        return _leftovers()
    return _serve(args)


def _leftovers() -> int:
    from . import fixtures as fixtures_mod

    cfg = config_mod.load()
    db.init_pool(cfg.database_url)
    rows = fixtures_mod.orphans()
    if not rows:
        print("  No review left anything behind.")
        return 0
    print(f"  {len(rows)} record(s) created by a finished run and not removed:\n")
    for r in rows:
        print(f"    {r['slug']:<20} {r['model']}#{r['res_id']}  (run {r['run_id']}, "
              f"{r['state']})")
        if r["remove_error"]:
            print(f"        refused: {r['remove_error'][:110]}")
    print("\n  Remove them in Odoo by hand; nothing here deletes from a client "
          "instance unattended.")
    return 1


def _knowledge(args) -> int:
    """Sync the client-repo cache from a terminal.

    Useful before onboarding — it answers "does this client even have a
    qa/ directory yet" without anyone logging in — and it is the hook a nightly
    job would use, since a knowledge file that changed yesterday should be the
    one tomorrow's run reads.
    """
    from . import clients as clients_mod
    from . import github, repo_sync
    from . import knowledge as knowledge_mod

    cfg = config_mod.load()
    db.init_pool(cfg.database_url)
    token = github.resolve_token(cfg.github_token)
    if not token:
        print("  No GitHub token found. Public repos will work; private ones will 404.")

    targets = []
    if args.slug:
        for slug in args.slug:
            client = clients_mod.get_by_slug(slug)
            if not client:
                print(f"  No client with slug {slug!r}.")
                return 2
            targets.append(client)
    else:
        targets = [c for c in clients_mod.list_all(include_inactive=True) if c.github]
        if not targets:
            print("  No client has a GitHub owner/name set.")
            return 1

    failed = 0
    for client in targets:
        try:
            snap = repo_sync.sync(client, token=token)
        except repo_sync.NoRepository as exc:
            print(f"\n  {client.slug:<24} skipped — {exc}")
            failed += 1
            continue
        if snap.error:
            print(f"\n  {client.slug:<24} FAILED — {snap.error}")
            failed += 1
            continue
        k = snap.knowledge
        print(f"\n  {client.slug:<24} {snap.github}@{snap.short_sha}")
        print(f"      {len(snap.modules)} module(s), {len(snap.scenarios)} scenario(s), "
              f"{len(k.invariants)} invariant(s), {len(k.danger_zones)} danger zone(s)")
        if not k.present:
            print(f"      [note]  no {knowledge_mod.PATH} in this branch")
        for entry in k.stale():
            print(f"      [stale] {entry.id} — review_after {entry.review_after} has passed")
        for scenario in snap.scenario_errors:
            for err in scenario.errors:
                print(f"      [bad]   {scenario.path}: {err}")
        for warning in snap.warnings:
            print(f"      [note]  {warning}")

    print(f"\n  {len(targets)} repo(s) read, {failed} failed.")
    return 1 if failed else 0


def _audit(args) -> int:
    """UC-16 from a terminal.

    Exists alongside the web page because the first useful run of this happens
    before anyone has onboarded a single client onto the gate, and because a
    non-zero exit is what lets it become a nightly cron without anyone writing
    a scraper against an HTML table.
    """
    from . import audit as audit_mod
    from . import clients as clients_mod

    cfg = config_mod.load()
    db.init_pool(cfg.database_url)

    if args.slug:
        targets = []
        for slug in args.slug:
            client = clients_mod.get_by_slug(slug)
            if not client:
                print(f"  No client with slug {slug!r}.")
                return 2
            targets.append(client)
    else:
        targets = [c for c in clients_mod.list_all(include_inactive=True)
                   if c.has_credentials]
        if not targets:
            print("  No client has stored RPC credentials yet.")
            return 1

    refused = 0
    for client in targets:
        result = audit_mod.run(client, cfg.secret_key, store=not args.no_store)
        marker = {"pass": "ok", "refuse": "REFUSE", "error": "unknown"}[result.verdict]
        print(f"\n  {client.slug:<24} {marker}  ({result.duration_ms} ms)")
        if result.error:
            print(f"      {result.error}")
        for check in result.checks:
            if check.status in (audit_mod.PASS, audit_mod.SKIPPED):
                continue
            print(f"      [{check.status:<5}] {check.title}")
            if check.detail:
                print(f"              {check.detail}")
            for line in check.evidence[:10]:
                print(f"              · {line}")
        if result.verdict == audit_mod.VERDICT_REFUSE:
            refused += 1

    print(f"\n  {len(targets)} instance(s) audited, {refused} would be refused.")
    return 1 if refused else 0


def _set_identity(url: str, db: str) -> int:
    from dataclasses import replace
    from .odoo_client import OdooClient, OdooError

    url = url.strip().rstrip("/")
    db = db.strip()
    cfg = config_mod.load()

    try:
        client = OdooClient(url, db)
        version = client.version()
        client.check_database()
    except OdooError as exc:
        print(f"  Refused: {exc}")
        return 1

    config_mod.save(replace(cfg, odoo=config_mod.OdooIdentity(url=url, db=db)))
    print(f"  Verified {url} (db {db}, server {version.get('server_version', '?')})")
    print(f"  Saved to {cfg.path}")
    print("  Restart the server for it to take effect.")
    return 0


def _serve(args) -> int:
    import uvicorn
    cfg = config_mod.load()
    _banner(cfg, args.host, args.port)
    uvicorn.run(
        "qa_gate.web.app:create_app",
        factory=True, host=args.host, port=args.port,
        reload=args.reload, log_level=args.log_level,
    )
    return 0


def _migrate() -> int:
    cfg = config_mod.load()
    applied = db.migrate(cfg.database_url)
    print(f"Applied {len(applied)} migration(s): {', '.join(applied) or 'none pending'}")
    return 0


def _check() -> int:
    """Diagnostics before anything is started.

    Mirrors odoo-dev-loop's `setup.sh --check`: the most common failure is a
    misconfigured dependency, and finding it here is far cheaper than finding it
    as a traceback on the first request.
    """
    cfg = config_mod.load()
    ok = True
    print(f"  config       {cfg.path}")
    print(f"  state        {paths.state_dir()}")
    print(f"  database_url {cfg.database_url}")

    try:
        db.init_pool(cfg.database_url)
        db.query_one("SELECT 1 AS ok")
        print("  postgres     reachable")
    except Exception as exc:
        print(f"  postgres     UNREACHABLE — {exc}")
        ok = False

    if cfg.odoo.configured:
        from .odoo_client import OdooClient, OdooError
        try:
            v = OdooClient(cfg.odoo.url, cfg.odoo.db).version()
            print(f"  identity     {cfg.odoo.url} (db {cfg.odoo.db}) "
                  f"server {v.get('server_version', '?')}")
        except OdooError as exc:
            print(f"  identity     UNREACHABLE — {exc}")
            ok = False
    else:
        print("  identity     not configured — visit /setup after starting")

    print("\n  OK" if ok else "\n  Problems found above.")
    return 0 if ok else 1


def _banner(cfg, host: str, port: int) -> None:
    lines = [
        "",
        "  Odoo PM Agent",
        f"  Listening on  http://{host}:{port}",
        f"  Config        {cfg.path}",
        f"  Database      {cfg.database_url}",
        "",
    ]
    if not cfg.odoo.configured:
        lines += ["  No identity Odoo configured yet.",
                  f"  Open http://{host}:{port}/setup to point it at your Odoo.", ""]
    else:
        lines += [f"  Staff sign in against {cfg.odoo.url} (db {cfg.odoo.db})", ""]
    print("\n".join(lines), file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
