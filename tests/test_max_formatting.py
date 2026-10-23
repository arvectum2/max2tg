"""MAX rich elements: safe, lossless Telegram markup and bridge integrations."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest

from app.max_formatting import format_max_html, MAX_ELEMENT_LIMIT
from app.max_client import MaxMessage, MaxClient
from app.max_listener import _message_body, _handle_linked_message, import_recent_history
from app.tg_sender import split_telegram_html, _utf16_length
from tests.test_long_messages import visible
from tests.test_outbox import fixture
from tests.test_message_sync import bootstrap, mark_bot_authored


def mark(kind,start,length,**attrs):
    item={"type":kind,"from":start,"length":length}
    if attrs: item["attributes"]=attrs
    return item


def rendered(text,items):
    html=format_max_html(text,items)
    assert visible(html)==text
    return html


@pytest.mark.parametrize("kind,tag", [
    ("STRONG","b"),("EMPHASIZED","i"),("UNDERLINE","u"),
    ("STRIKETHROUGH","s"),("MONOSPACED","code"),
    ("HEADING","b"),("HIGHLIGHTED","u"),("QUOTE","blockquote"),
    ("strong","b"),("emphasized","i"),
])
def test_supported_types(kind,tag):
    assert f"<{tag}>1234</{tag}>" in rendered("012345",[mark(kind,1,4)])


@pytest.mark.parametrize("target", [
    "javascript:alert(1)","file:///etc/passwd","http://127.0.0.1\nX",
    "data:text/html,<h1>X</h1>","https://a:b@host.tld/",
    "tg://user?id=abc","https://",
])
def test_untrusted_links_are_not_clickable(target):
    assert rendered("ссылка",[mark("LINK",0,6,url=target)])=="ссылка"


@pytest.mark.parametrize("item",[
    None,"bad",{},{"type":"STRONG","from":-1,"length":2},
    {"type":"LINK","from":0,"length":9999},
    {"type":"STRONG","from":True,"length":3},
    {"type":"ITALIC","from":0,"length":"6"},
    {"type":"EMOJI","from":0,"length":6},
])
def test_malformed_elements_are_ignored(item):
    assert rendered("ссылка",[item])=="ссылка"


def test_mixed_overlapping_spans_balance_and_preserve_text():
    txt="0123456789"
    rich=rendered(txt,[
        mark("STRONG",0,7),mark("UNDERLINE",4,6),
        mark("EMPHASIZED",2,6),
        mark("LINK",3,5,url='https://host.tld/?q=1&y="2"'),
    ])
    assert all(f"<{tag}>" in rich for tag in ("b","i","u"))
    assert '<a href="https://host.tld/?q=1&amp;y=&quot;2&quot;">' in rich


def test_unicode_surrogate_pairs_codepoint_offsets_and_code_precedence():
    txt="🍞Хлеб🥖 <&>"
    html=rendered(txt,[mark("STRONG",1,4),mark("MONOSPACED",7,3)])
    assert "<b>Хлеб</b>" in html
    assert "<code>&lt;&amp;&gt;</code>" in html


def test_mention_uses_verified_telegram_user_id():
    html=rendered("Иван",[{"type":"USER_MENTION","from":0,
                           "length":4,"attributes":{"user_id":12345}}])
    assert html=='<a href="tg://user?id=12345">Иван</a>'


def test_unknown_and_large_element_arrays_fall_back_to_escaped_text():
    assert format_max_html("A < B",None)=="A &lt; B"
    assert rendered("hello",[mark("STRONG",0,1)]*(MAX_ELEMENT_LIMIT+1))=="hello"


def test_message_body_has_markup_without_duplicate_share():
    txt="https://example.org"
    msg=MaxMessage(text=txt,
        raw={"message":{"elements":[mark("LINK",0,len(txt),url=txt)]}},
        attaches=[{"_type":"SHARE","url":txt,"title":"example"}])
    html=_message_body(msg)
    assert visible(html)==txt
    assert html.count("example.org")==2


def test_max_parse_and_html_long_split():
    txt="🍞Городской музей хлеба & <пример>\n" * 400
    m=MaxClient("token","device")._parse_message({"chatId":42,"message":{
       "id":"long","text":txt,
       "elements":[mark("STRONG",0,len(txt))],
    }})
    html=_message_body(m)
    chunks=split_telegram_html("<i>23.10</i>\n"+html)
    assert len(chunks)>2
    assert "".join(visible(p) for p in chunks)=="23.10\n"+txt
    assert all(_utf16_length(visible(p))<=3500 for p in chunks)


async def test_forward_keeps_nested_elements():
    msg=MaxMessage(chat_id=42,message_id="fwd",text="Комментарий")
    resolver=MagicMock()
    resolver.resolve_user=AsyncMock(return_value="Отправитель")
    sender=MagicMock()
    sender.send=AsyncMock(return_value=SimpleNamespace(message_id=555))
    link={"type":"FORWARD","message":{
        "text":"Жирный","elements":[mark("STRONG",0,6)],"sender":200,
    }}
    await _handle_linked_message(link,"FORWARD","Header",
                                 MagicMock(),sender,resolver,9,msg)
    assert "<b>Жирный</b>" in sender.send.await_args.args[0]


async def test_history_import_preserves_elements(tmp_path):
    client,sender,state,db=fixture(tmp_path)
    client.fetch_history=AsyncMock(return_value={"messages":[{
        "id":"formatted-history","sender":200,"time":1792700000000,
        "text":"пшеница и рожь",
        "elements":[mark("UNDERLINE",0,7),mark("STRONG",10,4)],
    }]})
    sender._bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=202))
    assert await import_recent_history(client,sender,client.resolver,42,777,20)==1
    content=sender._bot.send_message.await_args.kwargs["text"]
    assert "<u>пшеница</u>" in content and "<b>рожь</b>" in content
    db.close()


async def test_edit_max_message_preserves_elements(tmp_path):
    sync,topics,ledger,bot=bootstrap(tmp_path)
    mark_bot_authored(sync,topics,ledger,"777")
    packet={"chatId":42,"message":{
        "id":"777","status":"EDITED","updateTime":50,
        "text":"исправлено","elements":[mark("STRONG",0,10)],
    }}
    assert await sync.accept(128,packet)
    assert "<b>исправлено</b>" in bot.edit_message_text.await_args.kwargs["text"]
    ledger.close()


async def test_live_outbox_forwarding_preserves_markup(tmp_path):
    from tests.test_outbox import fixture, event
    client,sender,state,db=fixture(tmp_path)
    msg=event("live-formatted","Здравствуй")
    msg.raw={"chatId":42,"message":{"id":"live-formatted","text":msg.text,
             "elements":[mark("STRONG",0,len(msg.text))]}}
    sender._bot.send_message=AsyncMock(
        return_value=SimpleNamespace(message_id=956))
    await client._on_message_cb(msg)
    result=sender._bot.send_message.await_args.kwargs["text"]
    assert "<b>Здравствуй</b>" in result
    assert db.job(db.job_key(42,"live-formatted"))["status"]=="SENT"
    await client._on_message_cb(msg)
    sender._bot.send_message.assert_awaited_once()
    db.close()


async def test_photo_caption_renders_max_markup(tmp_path):
    from tests.test_outbox import fixture, event
    client,sender,state,db=fixture(tmp_path)
    msg=event("photo-fmt","Подпись")
    msg.attaches=[{"_type":"PHOTO","url":"https://i.oneme.ru/photo"}]
    msg.raw={"chatId":42,"message":{
        "id":"photo-fmt","text":"Подпись",
        "elements":[mark("EMPHASIZED",0,7)],
    }}
    client.download_file=AsyncMock(return_value=b"photo-bytes")
    sender._bot.send_photo=AsyncMock(
        return_value=SimpleNamespace(message_id=222))
    await client._on_message_cb(msg)
    assert "<i>Подпись</i>" in sender._bot.send_photo.await_args.kwargs["caption"]
    db.close()


def test_numeric_offset_range_preserves_nonbmp_emoji_and_symbols():
    txt="❤️🕘🙈 & <>"
    rich=rendered(txt,[mark("STRONG",2,2),mark("EMPHASIZED",6,4)])
    assert "<b>🕘🙈</b>" in rich
    assert "&amp;" in rich


def test_safe_link_cannot_include_spaces_controls_or_overlong_value():
    text="test"
    for target in ("https://host.tld/with space",
                   "https://host.tld/a\tbad",
                   "https://host.tld/"+"x"*3000):
        assert rendered(text,[mark("LINK",0,4,url=target)])==text
