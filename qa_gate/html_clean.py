"""Sanitising Odoo task descriptions before rendering them in our pages.

A `project.task` description is an HTML field. It is written by colleagues, not
by strangers, so this is not a hostile-input problem in the usual sense — but it
is still arbitrary HTML being injected into an authenticated page of a tool that
holds every client's staging credentials. "Our own staff wrote it" is not a
security boundary: a description can be pasted from an email, a client portal
submission, or a bug report, and it only has to go wrong once.

Rather than take a dependency for one field, this is a small allowlist parser:
anything not explicitly permitted is dropped. That is the safe default — an
unknown tag disappears rather than passing through — and it is short enough to
read in full and test exhaustively, which a regex-based cleaner never is.

Images get special handling. Odoo embeds them as `/web/image/<id>`, which needs
an authenticated session to fetch, so those are rewritten to point at our own
proxy (see `web/routes/tasks.py`). External and `data:` images are left alone.
"""
from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser

#: Tags allowed through. Deliberately narrow: this renders a description, not a
#: web page. No form, no iframe, no object, no style, no script.
ALLOWED_TAGS = {
    "p", "br", "div", "span", "b", "strong", "i", "em", "u", "s", "sub", "sup",
    "ul", "ol", "li", "blockquote", "pre", "code", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "thead", "tbody", "tr", "td", "th",
    "a", "img",
}
VOID_TAGS = {"br", "hr", "img"}

#: Attributes allowed per tag. `style` is excluded everywhere — it is the usual
#: way to smuggle behaviour past a tag allowlist, and Odoo's inline styles add
#: nothing we want inside our own layout.
ALLOWED_ATTRS = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan"},
}

#: Only these URL schemes may appear in href/src. Notably absent: javascript:,
#: vbscript:, and file:.
SAFE_SCHEMES = ("http://", "https://", "mailto:", "/", "#", "data:image/")

_WEB_IMAGE = re.compile(r"^/web/image/(\d+)", re.I)


class _Cleaner(HTMLParser):
    def __init__(self, image_url, allow_images: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.image_url = image_url
        self.allow_images = allow_images
        #: How many images were dropped, so the caller can say so rather than
        #: quietly serving a shorter description than the one in Odoo.
        self.dropped_images = 0
        self._skip_depth = 0

    # Content of a disallowed *dangerous* tag is dropped entirely, not unwrapped:
    # unwrapping <script> would emit its body as visible text, which is worse.
    DROP_CONTENT = {"script", "style", "iframe", "object", "embed", "template"}

    def handle_starttag(self, tag, attrs):
        if tag in self.DROP_CONTENT:
            self._skip_depth += 1
            return
        if tag == "img" and not self.allow_images:
            if not self._skip_depth:
                self.dropped_images += 1
            return
        if self._skip_depth or tag not in ALLOWED_TAGS:
            return
        kept = []
        for name, value in attrs:
            name = (name or "").lower()
            if name not in ALLOWED_ATTRS.get(tag, set()):
                continue
            value = self._clean_value(tag, name, value or "")
            if value is None:
                continue
            kept.append(f' {name}="{escape(value, quote=True)}"')
        closing = "/>" if tag in VOID_TAGS else ">"
        self.out.append(f"<{tag}{''.join(kept)}{closing}")

    def handle_endtag(self, tag):
        if tag in self.DROP_CONTENT:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth or tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if not self._skip_depth:
            self.out.append(escape(data, quote=False))

    def _clean_value(self, tag: str, name: str, value: str) -> str | None:
        value = value.strip()
        if name in ("href", "src"):
            lowered = value.lower().replace("\t", "").replace("\n", "")
            if name == "src" and tag == "img":
                m = _WEB_IMAGE.match(value)
                if m:
                    # An Odoo-hosted image: point it at our authenticated proxy.
                    return self.image_url(int(m.group(1)))
            if not lowered.startswith(SAFE_SCHEMES):
                return None
            return value
        if name in ("width", "height") and not value.isdigit():
            return None
        return value


def clean(html: str, *, image_url=None, allow_images: bool = False) -> tuple[str, int]:
    """Return (cleaned_html, images_dropped).

    Images are **off by default**. Odoo serves description images from
    `/web/image/<id>`, which needs an Odoo session and — more importantly —
    requires the service account to be an Internal User, since `ir.attachment`
    is closed to portal accounts. When that is not the case every image renders
    as a broken icon, which is worse than not showing it: it looks like the app
    is failing rather than like the account lacks a permission.

    Pass `allow_images=True` together with an `image_url(attachment_id) -> str`
    to render them through the authenticated proxy instead.

    The count is returned rather than discarded so a caller can mention that the
    description had pictures in it. Silently serving a shorter description than
    the one in Odoo would be its own small lie.
    """
    if not html:
        return "", 0
    cleaner = _Cleaner(image_url or (lambda _id: None), allow_images)
    cleaner.feed(html)
    cleaner.close()
    return "".join(cleaner.out).strip(), cleaner.dropped_images


def to_text(html: str, limit: int = 240) -> str:
    """A one-line plain-text summary, for collapsed rows and list views."""
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")
