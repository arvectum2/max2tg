# MAX2TG — product architecture

## Цель

MAX2TG должен стать сервисом, в котором Telegram является единственным пользовательским интерфейсом. Пользователь не устанавливает отдельное приложение MAX2TG и не обязан постоянно пользоваться клиентом MAX.

Базовый сценарий:
1. пользователь запускает Telegram-бота MAX2TG;
2. нажимает «Подключить MAX»;
3. сервер создаёт отдельную браузерную сессию MAX;
4. пользователь один раз авторизуется в web.max.ru по номеру телефона и SMS;
5. браузерная сессия остаётся жить на сервере 24/7;
6. чаты MAX появляются в Telegram;
7. пользователь читает сообщения и отвечает из Telegram.

Текущий код уже реализует основное ядро bridge MAX ↔ Telegram. Следующий этап — multi-user orchestration и onboarding.

## Основной UX

Telegram-бот — единственная точка входа и управления. Состояния подключения: MAX не подключён; авторизация; ONLINE; требуется повторный вход; временная ошибка; сессия отключена.

Путь пользователя: /start → «Подключить MAX» → временная web-страница/Mini App с удалённым браузером → телефон + SMS → MAX session ONLINE → автоматическое создание Telegram-тем → дальнейшая работа только из Telegram.

Пароли и SMS-коды не сохраняются. Долговременно хранится только изолированный browser profile/session state, необходимый для авторизованной web.max.ru-сессии.

## Telegram-модель

Предпочтительный режим для одного пользователя — личный диалог с ботом с forum topics, если используемая версия Telegram Bot API и настройки бота это поддерживают.

Логическая модель: MAX chat A ↔ Telegram topic A; MAX chat B ↔ Telegram topic B; MAX chat C ↔ Telegram topic C.

Если private forum topics недоступны или неудобны, fallback — приватная супергруппа с включёнными темами.

Отдельный shared/family режим: одна MAX-сессия связана с Telegram-супергруппой, где несколько разрешённых Telegram-пользователей читают и отвечают от одного MAX-аккаунта.

## Соответствия сообщений

Bridge хранит двустороннюю карту: max_chat_id + max_message_id ↔ telegram_chat_id + telegram_thread_id + telegram_message_id.

Это необходимо для native reply, edit, delete и reactions. MAX reply должен становиться Telegram reply_to_message, а Telegram reply — MAX REPLY link.

Текущая ветка уже содержит первую реализацию этой карты и native reply.

## Серверная архитектура

Telegram → Telegram Bot / Control API → User Service + Bridge Core + Session Supervisor + Auth Gateway → Browser Session Manager → отдельные MAX-сессии пользователей.

Для каждого MAX-пользователя создаётся отдельная логическая сессия. На первом этапе предпочтительна сильная изоляция: отдельный browser profile и, при необходимости, отдельный browser container/worker на пользователя.

## Основные сущности

User, TelegramConnection, MaxSession, BrowserProfile, MaxChat, TelegramTopic, MessageMapping, BrowserWorker, SessionEvent.

Минимальные постоянные таблицы: users, telegram_connections, max_sessions, max_chats, telegram_topics, message_mappings, browser_workers.

## Session Supervisor

Рекомендуемые состояния: AUTH_REQUIRED, AUTH_IN_PROGRESS, ONLINE, RECONNECTING, MAX_UNAVAILABLE, BROWSER_CRASHED, PAUSED, DELETED.

При потере авторизации бот отправляет кнопку повторного входа. После повторной SMS-авторизации существующие topic/message mappings сохраняются.

## Безопасность данных

- .env, cookies, browser profiles, runtime state и чувствительные логи не коммитятся;
- сессионные данные хранятся раздельно по tenant/user;
- browser profiles шифруются на диске;
- Telegram user id используется как первичный идентификатор onboarding;
- shared bridge использует явный ACL;
- SMS-коды не попадают в логи.

## Ограничения и статус

Проект неофициальный и использует reverse-engineered взаимодействие с MAX. Изменения MAX могут нарушить совместимость или привести к разрыву сессии.

В проект не закладывается функциональность обхода CAPTCHA, антибот-защиты, fingerprint/stealth-механизмов или иных средств уклонения от блокировок. Bridge поддерживает обычную совместимость с web MAX и корректно обрабатывает logout/reconnect/error states.
