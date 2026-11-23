"""Pure message-splitting contracts, independent of Telegram Bot API calls."""
import ast
from html.parser import HTMLParser
from pathlib import Path

import pytest

from app.telegram_html import _utf16_length, split_telegram_html
from app.tg_sender import split_telegram_html as legacy_split


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack.pop() == tag

    def handle_data(self, data):
        self.text.append(data)


def flatten(parts):
    visible = []
    for part in parts:
        p = VisibleText()
        p.feed(part)
        p.close()
        assert not p.stack
        visible.append("".join(p.text))
    return "".join(visible)


@pytest.mark.parametrize("limit", [32, 64, 128, 900])
def test_balanced_lossless_unicode_and_html(limit):
    html = (
        '<b>Проект &amp; чаты 👩🏽‍💻 ' * 1
        + ("🧩e\u0301 &amp; ссылка\n" * 350)
        + "</b>"
    )
    chunks = split_telegram_html(html, max_units=limit)
    assert len(chunks) > 1
    assert flatten(chunks) == flatten([html])
    assert all(_utf16_length(flatten([chunk])) <= limit for chunk in chunks)


def test_short_html_and_legacy_import_are_unchanged():
    sample = '<a href="https://example.org/?a=1&amp;b=2">Ссылка</a>'
    assert split_telegram_html(sample) == [sample]
    assert legacy_split is split_telegram_html


def test_empty_invalid_capacity_and_unicode_units():
    assert split_telegram_html("") == []
    assert _utf16_length("🕘") == 2
    with pytest.raises(ValueError, match="max_units"):
        split_telegram_html("a" * 10, max_units=1)


def test_html_module_is_pure_without_network_or_sdk_imports():
    root = Path(__file__).resolve().parents[1]
    source = (root / "app" / "telegram_html.py").read_text()
    module = ast.parse(source)
    imported = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported == {"html"}
