"""Candidate and text extraction rules shared by the two page front-ends.

`page.py` turns fetched HTML into the page model, `dom.py` reads the rendered
DOM into the same model. Both must apply the same rules, so the rules live
here once: `page.py` consumes these objects directly and `dom.py` renders them
into the JS literals of `SNAPSHOT_JS` at import time.

Label precedence (`LABEL_PRECEDENCE`), highest first: the first non-empty
source wins, except that a source flagged `False` loses to the element's own
text. With no source left: the element's text, else the slug of the href's
path (its path segments, slashes and dashes as spaces), else the raw href.
Labels are cut to `LABEL_MAX` characters.

Regions: `LANDMARK` elements delimit regions (`body` outside them); anything
outside `REGION_CAP` counts as `body`. Candidates are deduplicated and capped
per region, then emitted in `REGION_ORDER`. Text blocks are merged (`LABELS`
hold for their `VALUES` cell), cut to `MAX_TEXT_CHARS`, dropped under
`MIN_PROSE`/`MIN_VALUE`, kept only in `TEXT_REGIONS`, deduplicated and pooled
up to `MAX_TEXT_POOL`.
"""

from __future__ import annotations

# (attribute, wins over the element's own text), highest precedence first.
LABEL_PRECEDENCE = (
    ("aria-label", True),
    ("title", True),
    ("placeholder", False),
    ("alt", True),
    ("value", False),
    ("name", False),
)
LABEL_MAX = 90

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}
SKIP = {"script", "style", "noscript", "template", "svg", "head", "iframe",
        "video", "audio", "canvas", "map", "object"}
LANDMARK = {"nav", "header", "main", "footer", "aside", "form", "article"}
TEXTY = {"h1", "h2", "h3", "h4", "p", "li", "td", "dd", "dt", "th", "caption",
         "figcaption", "blockquote"}
INTERACTIVE = {"a", "button", "input", "select", "textarea", "summary"}
ROLES = {"button", "link", "tab", "menuitem", "menuitemcheckbox", "menuitemradio",
         "checkbox", "radio", "switch", "combobox", "searchbox", "textbox",
         "option", "slider"}

REGION_ORDER = ("main", "article", "body", "form", "nav", "header", "aside",
                "footer")
REGION_CAP = {"main": 70, "article": 70, "body": 18, "form": 10, "nav": 20,
              "header": 12, "aside": 8, "footer": 12}
TEXT_REGIONS = {"main", "article", "body"}

PROSE = ("p", "li", "blockquote", "caption", "figcaption")
VALUES = ("td", "dd")
LABELS = ("th", "dt")
MIN_PROSE = 40
MIN_VALUE = 6
MAX_TEXT_CHARS = 260
MAX_TEXT_POOL = 400
