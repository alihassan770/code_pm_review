"""Driving a real browser against a client's staging instance.

Screenshots are the evidence half of a review: a field value proves the record
is right, a picture proves a person would actually see it. Both are needed, and
only one of them can be got over RPC.

## Signing in

By injecting the `session_id` cookie, never by filling the login form. The form's
markup changes between Odoo 17, 18 and 19 and again with every theme a client
installs, so a selector written against one is a latent failure against the next.
`/web/session/authenticate` has been stable across all of them, so `personas`
opens the session and this seeds the cookie with it.

## Two gotchas, both found the hard way against a live instance

  * **Never wait for `networkidle`.** Odoo's bus long-polls forever, so the
    network is never idle and the wait always times out. Wait for the web
    client's own root element instead.
  * **The screenshot is of the viewport, not the page.** A full-page capture of
    a list view with a thousand rows is a useless several-megabyte image.

## This module never writes

Every method here navigates and looks. Creating records on a client's staging is
a different kind of act with different consequences, and it belongs behind the
pre-flight audit rather than inside a screenshot helper.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlparse

log = logging.getLogger(__name__)

#: Wide enough that Odoo renders its desktop layout rather than the mobile one,
#: and a form's fields sit where a reviewer expects them.
VIEWPORT = {"width": 1440, "height": 900}

#: Odoo's own root elements. `.o_web_client` is 16-18, `.o_action_manager`
#: covers the newer shell — waiting for either rather than for one keeps this
#: working across the versions the gate spans.
READY_SELECTOR = ".o_web_client, .o_action_manager"

NAV_TIMEOUT = 60_000
READY_TIMEOUT = 45_000
#: After the shell is up, Odoo still paints the view. Short and fixed rather
#: than another selector wait, because what comes next differs per view type.
SETTLE_MS = 2_500


class BrowserError(Exception):
    """Message is safe to show."""


@dataclass
class Shot:
    caption: str
    png: bytes
    url: str = ""

    @property
    def byte_count(self) -> int:
        return len(self.png)


class Session:
    """One logged-in browser against one staging instance."""

    def __init__(self, page, base_url: str) -> None:
        self._page = page
        self._base = base_url.rstrip("/")

    def goto(self, path: str) -> None:
        url = path if path.startswith("http") else f"{self._base}/{path.lstrip('/')}"
        self._page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        try:
            self._page.wait_for_selector(READY_SELECTOR, timeout=READY_TIMEOUT)
        except Exception:  # noqa: BLE001 - a page that never shows the shell is
            # still worth photographing: the picture is how somebody finds out
            # it rendered an error instead of the view.
            log.info("Odoo shell did not appear at %s", url)
        self._page.wait_for_timeout(SETTLE_MS)

    def record(self, model: str, record_id: int, action: str = "") -> None:
        """Open one record's form view.

        Uses the `/odoo/...` router present since 17 with the legacy `/web#`
        hash as the fallback, because a client on an older point release still
        has to be photographable.
        """
        if action:
            self.goto(f"/odoo/action-{action}/{record_id}")
        else:
            self.goto(f"/odoo/{model.replace('.', '-')}/{record_id}")
        if "/odoo/" in self._page.url and self._is_blank():
            self.goto(f"/web#id={record_id}&model={model}&view_type=form")

    def _is_blank(self) -> bool:
        try:
            return not self._page.query_selector(".o_form_view, .o_list_view")
        except Exception:  # noqa: BLE001
            return False

    #: How a highlighted field is drawn. A ring plus an offset rather than a
    #: filled box, so the value inside stays readable, and `status-critical` red
    #: because it has to survive being pasted into a chat window at half size.
    _HIGHLIGHT_CSS = ("outline:3px solid #d03b3b!important;"
                      "outline-offset:3px;border-radius:5px;"
                      "box-shadow:0 0 0 6px rgba(208,59,59,.16)!important;")

    def view_kind(self) -> str:
        """Which Odoo view is on screen: "form", "list", or "other".

        Asked before highlighting, because what a field name means depends on
        it. On a form `[name="active"]` is one widget; on a list it is one cell
        per row, and forty rows means forty of them.
        """
        try:
            if self._page.query_selector(".o_form_view"):
                return "form"
            if self._page.query_selector(".o_list_view"):
                return "list"
        except Exception:  # noqa: BLE001
            pass
        return "other"

    def highlight(self, targets: list[str]) -> list[str]:
        """Ring the fields or buttons a scenario is actually about.

        On a sale order form with sixty widgets, "we changed one field" is
        invisible in a screenshot. The picture is only evidence if it shows
        *which* thing changed, so the scenario names its targets and they get
        drawn before the capture.

        Targets are Odoo field or button names, not CSS. `[name="x"]` is how
        Odoo tags both in 17, 18 and 19, which makes one selector enough and
        keeps the plan writing field names it already knows rather than
        selectors it would have to guess.

        **Scoped, and never a whole column.** The first version ran
        `querySelectorAll` against the document and ringed every match. On a
        form that is right, one widget per field. On a list it ringed the cell
        in every visible row, so a picture meant to point at one thing pointed
        at forty and communicated nothing. So:

          * on a form, ring the matching widgets inside `.o_form_view`, which
            also keeps the ring out of any list embedded in a tab;
          * on a list, ring the column HEADER only, one element, which says
            which column to look at without claiming a particular row;
          * anywhere else, ring the first match and stop.

        Returns the targets it actually found, so the caller can say what was
        ringed rather than assert a ring that is not in the image. A miss is
        never an error: a field hidden by the view is a fact about the view.
        """
        kind = self.view_kind()
        found: list[str] = []
        for name in targets or []:
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()
            if kind == "form":
                sel, limit = f'.o_form_view [name="{name}"]', 0      # 0 = all
            elif kind == "list":
                # The header, not the cells. `th[data-name]` is how the list
                # view tags its columns in 17 through 19.
                sel, limit = (f'.o_list_view th[data-name="{name}"], '
                              f'.o_list_view th[data-name="{name}"] *'), 1
            else:
                sel, limit = f'[name="{name}"]', 1
            try:
                n = self._page.evaluate(
                    """([sel, css, limit]) => {
                        let els = Array.from(document.querySelectorAll(sel));
                        if (!els.length) return 0;
                        if (limit > 0) els = els.slice(0, limit);
                        els.forEach(e => e.style.cssText += css);
                        els[0].scrollIntoView({block: 'center', inline: 'center'});
                        return els.length;
                    }""",
                    [sel, self._HIGHLIGHT_CSS, limit])
            except Exception as exc:  # noqa: BLE001 - a selector that does not
                # match is not a failure worth ending a review over.
                log.info("could not highlight %s: %s", name, exc)
                continue
            if n:
                found.append(name)
        if found:
            # Scrolling moved the viewport; let Odoo finish any lazy render it
            # started before the shutter.
            self._page.wait_for_timeout(400)
        return found

    def shot(self, caption: str) -> Shot:
        return Shot(caption=caption, png=self._page.screenshot(), url=self._page.url)

    @property
    def url(self) -> str:
        return self._page.url

    @property
    def signed_in(self) -> bool:
        return "/web/login" not in self._page.url


@contextmanager
def session(staging_url: str, session_id: str):
    """A logged-in browser, closed on the way out however that happens."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise BrowserError(
            "Playwright is not installed, so no screenshots can be taken. Run "
            "`pip install playwright && playwright install chromium`.") from exc

    host = urlparse(staging_url).hostname
    if not host:
        raise BrowserError(f"{staging_url!r} is not a URL a browser can open.")

    with sync_playwright() as pl:
        try:
            browser = pl.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(
                f"Could not start Chromium: {exc}. If this is a fresh install, "
                "`playwright install chromium` downloads it.") from exc
        try:
            context = browser.new_context(viewport=VIEWPORT)
            context.add_cookies([{
                "name": "session_id", "value": session_id, "domain": host,
                "path": "/", "httpOnly": True,
                "secure": staging_url.lower().startswith("https"),
            }])
            yield Session(context.new_page(), staging_url)
        finally:
            browser.close()
