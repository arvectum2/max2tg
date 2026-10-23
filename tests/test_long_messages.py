"""Full-length Telegram texts and media captions."""
import asyncio
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from app.tg_sender import TelegramSender, split_telegram_html, _utf16_length

class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.data = []
        self.tags = []
    def handle_data(self, data): self.data.append(data)
    def handle_starttag(self, tag, attrs): self.tags.append(tag)
    def handle_endtag(self, tag):
        assert self.tags.pop() == tag

def visible(html):
    p=Text(); p.feed(html); p.close(); assert not p.tags
    return ''.join(p.data)

def test_long_html_is_lossless_and_balanced():
    message = '<i>23.10.2026</i>\n<b>Вы</b>\n<blockquote>' + ('🕘 &amp; абзац https://example.org?q=1&amp;x=2\n' * 300) + '</blockquote>'
    parts = split_telegram_html(message)
    assert len(parts) > 1
    assert ''.join(visible(p) for p in parts) == visible(message)
    assert all(_utf16_length(visible(p)) < 4096 for p in parts)

def test_long_word_and_utf16_emojis():
    message = '<b>' + '🕘Ж' * 6000 + '</b>'
    parts = split_telegram_html(message)
    assert ''.join(visible(p) for p in parts) == visible(message)
    assert all(_utf16_length(visible(p)) < 4096 for p in parts)

def test_short_html_is_unchanged():
    s='<a href="https://example.org/?a=1&amp;b=2">Ссылка</a>'
    assert split_telegram_html(s) == [s]

async def test_send_splits_and_returns_first_message():
    sender=TelegramSender.__new__(TelegramSender)
    sender._chat_id='-100'
    sender._bot=MagicMock()
    sender._bot.send_message=AsyncMock(side_effect=[SimpleNamespace(message_id=i) for i in range(100,110)])
    first=await sender.send('<b>' + ('A'*12000) + '</b>', message_thread_id=77, reply_to_message_id=9)
    calls=sender._bot.send_message.await_args_list
    assert first.message_id == 100
    assert len(calls)==4
    assert ''.join(visible(c.kwargs['text']) for c in calls)=='A'*12000
    assert all(c.kwargs['message_thread_id']==77 for c in calls)
    assert calls[0].kwargs['reply_to_message_id']==9
    assert all(c.kwargs['reply_to_message_id']==100 for c in calls[1:])

async def test_long_photo_caption_remainder():
    sender=TelegramSender.__new__(TelegramSender)
    sender._chat_id='-100'; sender._bot=MagicMock()
    sender._bot.send_photo=AsyncMock(return_value=SimpleNamespace(message_id=55))
    sender._bot.send_message=AsyncMock(return_value=SimpleNamespace(message_id=56))
    cap='<b>' + ('Посмотри 🕘 &amp; ' * 150) + '</b>'
    result=await sender.send_photo(b'photo', caption=cap, message_thread_id=88)
    assert result.message_id==55
    first=sender._bot.send_photo.await_args.kwargs['caption']
    rest=[x.kwargs['text'] for x in sender._bot.send_message.await_args_list]
    assert rest and ''.join(visible(p) for p in [first]+rest)==visible(cap)
    assert sender._bot.send_message.await_args_list[0].kwargs['reply_to_message_id']==55
    assert _utf16_length(visible(first)) < 1024
