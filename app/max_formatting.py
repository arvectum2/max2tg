"""Safe MAX user-session elements[] -> Telegram HTML.

MAX web client ranges are character offsets of the unescaped plain text
(see max-api-docs/protocol/elements.md), unlike Telegram API entity
offsets which count UTF-16 code units. Invalid/unsupported ranges do not
modify message text. No HTML is accepted from untrusted MAX payloads.
"""
from __future__ import annotations

from html import escape
from urllib.parse import urlsplit

# Priority is nesting order (outer to inner). Telegram supports HTML tags
# below; raw HTML from MAX message content is always escaped.
_FORMAT_TAGS = {
    "STRONG": ("b", "b"),
    "BOLD": ("b", "b"),
    "EMPHASIZED": ("i", "i"),
    "ITALIC": ("i", "i"),
    "UNDERLINE": ("u", "u"),
    "STRIKETHROUGH": ("s", "s"),
    "MONOSPACED": ("code", "code"),
    "CODE": ("code", "code"),
    "HEADING": ("b", "b"),
    "HIGHLIGHTED": ("u", "u"),
    "QUOTE": ("blockquote", "blockquote"),
}

# Keep ordering deterministic, even for overlapping elements and split ranges.
_PRIORITY = {
    "blockquote": 0, "a": 1, "b": 2, "i": 3, "u": 4,
    "s": 5, "code": 6,
}
MAX_ELEMENT_LIMIT = 1000


def _safe_href(raw: object) -> str | None:
    if not isinstance(raw, str) or len(raw) > 2048:
        return None
    url = raw.strip()
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7f
           for ch in url):
        return None
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() == "https" or parsed.scheme.lower() == "http":
            if parsed.hostname and not parsed.username and not parsed.password:
                return url
        # Mentions in the official MAX Bot API are max://user/<id>,
        # but Telegram needs a tg://user?id=<numeric> link instead.
        if parsed.scheme.lower() == "tg" and parsed.netloc == "user":
            from urllib.parse import parse_qs
            values = parse_qs(parsed.query).get("id", [])
            if len(values) == 1 and values[0].isdigit():
                return f"tg://user?id={values[0]}"
    except ValueError:
        return None
    return None


def _parse_element(element: object, text_size: int):
    if not isinstance(element, dict):
        return None
    kind = element.get("type")
    if not isinstance(kind, str):
        return None
    kind = kind.upper()
    start = element.get("from", 0)
    length = element.get("length")
    # bool is a subclass of int in Python; never accept it as an offset.
    if (type(start) is not int or type(length) is not int or start < 0
            or length < 1 or start + length > text_size):
        return None
    attributes = element.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}
    if kind in ("LINK", "TEXT_LINK", "USER_MENTION", "MENTION"):
        url = attributes.get("url") or element.get("url")
        if kind in ("USER_MENTION", "MENTION") and not url:
            uid = attributes.get("user_id") or attributes.get("userId")
            if isinstance(uid, int) and uid > 0:
                url = f"tg://user?id={uid}"
        href = _safe_href(url)
        if not href:
            return None
        opening = f'<a href="{escape(href, quote=True)}">'
        return (start, start + length, "a", opening, "</a>")
    tags = _FORMAT_TAGS.get(kind)
    if tags is None:
        return None
    tag = tags[0]
    return (start, start + length, tag, f"<{tag}>", f"</{tag}>")


def format_max_html(text: str, elements: object) -> str:
    """Render MAX formatted text to balanced, HTML-escaped Telegram markup.

    Coordinates refer to *original* text, never to already escaped HTML.
    Crossing annotations are rendered as a sequence of correctly nested
    spans, without losing/repeating any visible character. Unknown markup is
    ignored. Code fragments win over other overlapping inline annotations,
    avoiding Telegram's forbidden code/link nested combinations.
    """
    if not isinstance(text, str):
        return ""
    if not text or not isinstance(elements, list) or not elements:
        return escape(text, quote=False)
    if len(elements) > MAX_ELEMENT_LIMIT:
        return escape(text, quote=False)
    spans = []
    for item in elements:
        parsed = _parse_element(item, len(text))
        if parsed is not None:
            spans.append(parsed)
    if not spans:
        return escape(text, quote=False)

    boundaries = {0, len(text)}
    for start, end, _, _, _ in spans:
        boundaries.add(start)
        boundaries.add(end)
    cuts = sorted(boundaries)
    fragments = []
    active_prev = []
    for start, end in zip(cuts, cuts[1:]):
        if start == end:
            continue
        active = [
            span for span in spans if span[0] <= start and end <= span[1]
        ]
        # Dedupe exact same annotation and make nesting deterministic.
        active_by_tag = {}
        for item in active:
            tag = item[2]
            if tag != "a" or tag not in active_by_tag:
                active_by_tag[tag] = item
        active = list(active_by_tag.values())
        if "code" in active_by_tag:
            # For monospace, keep blockquote as an outer container but skip
            # inline tags; Telegram does not accept markup inside <code>.
            active = [item for item in active if item[2] in ("blockquote", "code")]
        active.sort(key=lambda item: (
            _PRIORITY[item[2]], item[0], -item[1], item[3],
        ))
        # Active element identity is defined by opening tag and category:
        # consecutive differently ranged but equivalent bold spans may merge.
        selected = [(item[2], item[3], item[4]) for item in active]
        prefix = 0
        while (prefix < len(active_prev) and prefix < len(selected)
               and active_prev[prefix] == selected[prefix]):
            prefix += 1
        for tag, opening, closing in reversed(active_prev[prefix:]):
            fragments.append(closing)
        for tag, opening, closing in selected[prefix:]:
            fragments.append(opening)
        fragments.append(escape(text[start:end], quote=False))
        active_prev = selected

    fragments.extend(item[2] for item in reversed(active_prev))
    return "".join(fragments)
