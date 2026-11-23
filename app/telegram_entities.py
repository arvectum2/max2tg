"""Convert Telegram UTF-16 message entities into MAX codepoint formatting.

This module performs deterministic transformations without sending messages.
"""
from telegram.constants import MessageEntityType

# Telegram entity type → MAX element type. The MAX names match what the
# existing codebase used (STRONG) and what MAX renders for the formatting
# styles surfaced in its UI (bold/italic/strike/underline/code).
_TG_TO_MAX_ELEMENT_TYPE = {
    MessageEntityType.BOLD: "STRONG",
    MessageEntityType.ITALIC: "EMPHASIZED",
    MessageEntityType.STRIKETHROUGH: "STRIKETHROUGH",
    MessageEntityType.UNDERLINE: "UNDERLINE",
    MessageEntityType.CODE: "MONOSPACED",
    # MAX's WebSocket protocol exposes a smaller element enum than its bot
    # HTTP API: BLOCKQUOTE / CODE_BLOCK / HIGHLIGHTED / HEADING are all
    # rejected with "No enum constant". Best fallback for multi-line code
    # is the same monospace style as inline code. Telegram blockquotes
    # have no MAX counterpart at all — let them through as plain text
    # rather than fail the whole send_message.
    MessageEntityType.PRE: "MONOSPACED",
}


def _utf16_to_char_offset(text: str, utf16_offset: int) -> int:
    """Convert a UTF-16 code-units offset (what Telegram uses for entity
    positions) into a Python codepoint index (which MAX appears to use)."""
    if utf16_offset <= 0 or not text:
        return 0
    encoded = text.encode("utf-16-le")
    truncated = encoded[: utf16_offset * 2]
    return len(truncated.decode("utf-16-le", errors="ignore"))


def _entities_to_max_elements(text: str, entities) -> list:
    """Map Telegram message entities to MAX `elements` descriptors so basic
    inline formatting (bold/italic/strike/underline/code/link) survives the
    Telegram → MAX bridge."""
    if not entities:
        return []
    elements: list[dict] = []
    for e in entities:
        start = _utf16_to_char_offset(text, e.offset)
        end = _utf16_to_char_offset(text, e.offset + e.length)
        length = end - start
        if length <= 0:
            continue
        max_type = _TG_TO_MAX_ELEMENT_TYPE.get(e.type)
        if max_type:
            elements.append({"type": max_type, "from": start, "length": length})
        elif e.type == MessageEntityType.TEXT_LINK and getattr(e, "url", None):
            elements.append({
                "type": "LINK",
                "from": start,
                "length": length,
                "attributes": {"url": e.url},
            })
    return elements
