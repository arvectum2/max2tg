"""Pure Unicode-safe Telegram HTML message splitting.

No Bot API, transport, network or persistence dependencies.
The compatibility imports in tg_sender preserve existing consumers.
"""
from html import escape
from html.parser import HTMLParser


# Telegram validates text length AFTER parsing HTML entities; UTF-16 units
# are the safest bound (including astral emoji). Leave margin for entities.
TG_TEXT_PART_MAX = 3500
TG_CAPTION_PART_MAX = 900


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _take_part(value: str, capacity: int) -> int:
    """Choose a Unicode-safe cut, preferring paragraphs / word boundaries."""
    size = 0
    end = 0
    for index, char in enumerate(value):
        units = 2 if ord(char) > 0xFFFF else 1
        if size + units > capacity:
            break
        end = index + 1
        size += units
    if end == len(value):
        return end
    # Keep exact characters, including spaces and newlines, across splits.
    piece = value[:end]
    for mark in ("\n", " "):
        cut = piece.rfind(mark)
        if cut >= end // 2 and cut > 0:
            return cut + 1
    return end


class _HTMLChunker(HTMLParser):
    """Split Telegram HTML with balanced markup and no lost visible text."""

    def __init__(self, max_units: int):
        super().__init__(convert_charrefs=True)
        self.max_units = max_units
        self.stack: list[tuple[str, str]] = []
        self.buffer: list[str] = []
        self.units = 0
        self.parts: list[str] = []

    def _flush(self):
        if self.units == 0:
            return
        suffix = "".join(f"</{tag}>" for tag, _ in reversed(self.stack))
        self.parts.append("".join(self.buffer) + suffix)
        self.buffer = [opening for _, opening in self.stack]
        self.units = 0

    def handle_starttag(self, tag, attrs):
        opening = self.get_starttag_text()
        self.stack.append((tag, opening))
        self.buffer.append(opening)

    def handle_endtag(self, tag):
        # HTML sent by the bridge is balanced. Preserve well-formed nesting.
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
            self.buffer.append(f"</{tag}>")

    def handle_data(self, data):
        while data:
            if self.units >= self.max_units:
                self._flush()
            count = _take_part(data, self.max_units - self.units)
            if count <= 0:
                raise ValueError("Cannot split Telegram message at a Unicode boundary")
            portion, data = data[:count], data[count:]
            self.buffer.append(escape(portion, quote=False))
            self.units += _utf16_length(portion)
            if data:
                self._flush()

    def handle_startendtag(self, tag, attrs):
        self.buffer.append(self.get_starttag_text())


def split_telegram_html(text: str, max_units: int = TG_TEXT_PART_MAX) -> list[str]:
    """Return valid HTML parts; each displays <= max_units UTF-16 units."""
    if not text:
        return []
    if max_units < 2:
        raise ValueError("max_units must be >= 2")
    # Preserve short inputs literally; avoid unnecessary HTML normalization.
    if _utf16_length(text) <= max_units:
        return [text]
    parser = _HTMLChunker(max_units)
    parser.feed(text)
    parser.close()
    parser._flush()
    return parser.parts
