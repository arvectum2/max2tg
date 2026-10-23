# MAX2TG — P0.2 Persistent Outbox (23.10.2026)

## Реализовано

Отдельная SQLite очередь исходящих сообщений MAX → Telegram:

- На стадии обработки сообщения MAX событие с его chat_id/message_id,
  text/link/attaches/metadata сохраняется в SQLite ДО отправки в Telegram.
- Уникальный ключ (chat_id, max_message_id) блокирует повторную
  пересылку одного события и сохраняется после рестартов.
- Каждая Telegram API операция внутри события записывается отдельной
  частью с ordinal, методом, параметрами, SHA-256 отпечатком
  содержимого и (для медиа) байтами вложения.
- Части длинного текста и подписи к медиа фиксируются отдельно.
  Уже подтверждённые части не отправляются повторно.
- Для недоставленных медиа используются сохранённые байты, даже когда
  исходная MAX CDN-ссылка истекла.
- Полученные telegram_message_id записываются в SQLite; первичный
  MAX↔TG ID продублирован в TopicStore. При повреждении/отставании
  JSON mapping восстанавливается по завершённым SQLite-записям.
- Статусы RECEIVED, SENDING, SENT, RETRY, UNKNOWN, FAILED. При RetryAfter
  гарантированно отказавшийся API-вызов повторяется после срока
  ограничения; фоновый процесс раз в 15 секунд выбирает due-задачи.
- NetworkError, TimedOut, отменённая задача / kill в ходе активного
  API-вызова означают UNKNOWN. Никакого слепого автоматического повтора.
- При старте SENDING-part превращается в UNKNOWN, но задача, не
  начавшая сетевую отправку, может быть безопасно повторена.
- Полная обработка истории MAX, live-dispatch и собственных сообщений
  MAX использует один outbox-слой; исходящие Telegram→MAX используют
  ранее существующий механизм подтверждений и mappings.

## Где хранится

На Mac mini:

- SQLite: /Users/master/.local/state/max2tg/outbox.sqlite3
- Ключи / сессии MAX внутри outbox не хранятся. Хранятся текст,
  метаданные, потенциально временные URL вложений и байты ожидающих
  файлов. Файл SQLite доступен только владельцу (0600), каталог 0700.
- SQLite WAL, synchronous=FULL, foreign_keys=ON.
- JSON topics: /Users/master/.local/state/max2tg/topics.json

Не отправлять приватное содержимое журнала outbox в публичные логи.

## Операторская диагностика (только чтение)

sqlite3 ~/.local/state/max2tg/outbox.sqlite3 \
  'SELECT status,COUNT(*) FROM jobs GROUP BY status;'

sqlite3 ~/.local/state/max2tg/outbox.sqlite3 \
  "SELECT chat_id,max_id,status,attempts,datetime(updated,'unixepoch') AS changed FROM jobs WHERE status IN ('UNKNOWN','FAILED') ORDER BY updated DESC LIMIT 20;"

UNKNOWN нельзя автоматически сбрасывать в RETRY — сначала установить,
дошла ли часть в Telegram, и зафиксировать фактический tg_message_id.

## Тесты

- tests/test_outbox.py: persistence, idempotency, мультичастные сообщения,
  RetryAfter, TimedOut, forbidden, media fallback, истёкшие MAX CDN ссылки,
  recovery после рестарта и защиту приватности файлов.
- Остальные unit/regression тесты: pytest -q, compileall, pip check.
- Deployment: проверка launchd, MAX auth, Telegram polling, SQLite и JSON
  без отправки тестовых сообщений реальным собеседникам.

## Осознанные ограничения / дальнейшие работы

1. Telegram Bot API не предоставляет idempotency token. Статус UNKNOWN
   требует сверки оператором; полностью гарантировать exactly-once
   нельзя. Интерфейс диагностики и ручного безопасного разрешения
   неоднозначных доставок войдёт в P0.4.
2. Сейчас outbox обслуживает направление MAX → Telegram. Для
   Telegram → MAX остаётся существующий confirmation/reply механизм;
   отдельная очередь обратного направления возможна при необходимости.
3. Системные уведомления, команды управления ботом и операции создания
   тем используют прямые Telegram API-вызовы, не очередь сообщений MAX.
4. Отпечаток payload предотвращает отправку изменившейся части на
   повторе; при изменении структуры события после обновления кода
   задача станет UNKNOWN, пока оператор не подтвердит статус.
5. Сбор статистики очередей, лимиты размера БД, сроки хранения,
   отдельные процессы worker, outbox UI и оповещения оператора —
   часть P0.4 и эксплуатационного этапа.
