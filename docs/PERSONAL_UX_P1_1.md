# MAX2TG Personal UX P1.1 — форматирование (23.10.2026)

В app/max_formatting.py реализовано безопасное преобразование MAX WebSocket
elements в Telegram HTML: STRONG → bold, EMPHASIZED → italic,
UNDERLINE, STRIKETHROUGH, MONOSPACED, QUOTE, LINK, HEADING,
HIGHLIGHTED и цифровые USER_MENTION.

Отрисовка работает в live MAX→TG, истории, FORWARD, подписях к медиа
и синхронизации edits. Пересекающиеся диапазоны преобразуются в валидный
вложенный HTML без потери символов, длинный HTML по-прежнему разбивается
на части с сохранением текста и форматирования. URL проходят строгую
проверку схемы, длины, credentials и управляющих символов. Подозрительные
ссылки сохраняются обычным текстом.

Границы: подтверждённые в пользовательском WebSocket MAX типы —
STRONG, EMPHASIZED, UNDERLINE, LINK. Дополнительные типы по известным
схемам имеют безопасный fallback. Списки без особых MAX elements
сохраняют текстовые маркеры. Telegram→MAX использует только уже
подтверждённые WebSocket-типы, не всю разметку официального Bot API.
Обработка emoji с диапазонами по символам покрыта тестами; физический
smoke на тестовом аккаунте остаётся полезен.

Тесты: tests/test_max_formatting.py (38 новых); полный pytest,
compileall, pip check, git diff --check. Реальным собеседникам
контрольные сообщения не отправлялись.

Документация протокола:
https://github.com/pr0bel1230/max-api-docs/blob/main/protocol/elements.md

Следующая задача: P1.2 — альбомы, документы, видео, голосовые сообщения
и безопасные fallback.
