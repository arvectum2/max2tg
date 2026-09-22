# MAX2TG roadmap

## Текущая точка

Уже работает личный двусторонний bridge MAX ↔ Telegram: входящие сообщения, темы, ответы, медиа, семейный ACL, reconnect и базовое управление чатами.

Последние изменения:
- репозитории сведены: единственный source of truth — `arvectum2/max2tg` (`main`), GitVerse `arvectum/max2tg` — автоматическое зеркало, исходный `ircitdev/MAX2TG-Bridge` — read-only upstream;
- добавлен MAX CHAT_LEAVE;
- добавлена карта MAX message ↔ Telegram message;
- MAX reply переводится в настоящий Telegram reply;
- Telegram reply переводится в MAX REPLY;
- обработка вложений теперь возвращает Telegram message для message mapping.

## Этап 1 — стабилизация личного bridge

- закрыть тестами reply mapping в обе стороны;
- проверить leave на канале/группе;
- проверить edit/delete/reaction semantics;
- проверить reconnect после sleep/network failure;
- не терять message mappings после рестарта;
- исключить дубли топиков и сообщений.

## Этап 1.5 — Personal UX v1.0 — закрыт

До перехода к onboarding/multi-user полностью доводим личную версию как Telegram-first клиент.

- [x] закреплённая панель управления в General;
- [x] выход из группы/канала MAX кнопкой с подтверждением;
- [x] обзор активных личных диалогов, групп и каналов MAX;
- [x] отметка уже созданных Telegram-топиков и пагинация списка;
- [x] создание/восстановление Telegram-топика из меню без chat_id и команд;
- [x] поиск людей, групп и каналов MAX из Telegram через глобальный MAX search (opcode 60);
- [x] карточка результата + действие: создать DIALOG-топик или открыть/вступить/подписаться и создать топик;
- [x] исправлен startup deadlock: CONTACT_GET больше не блокирует WebSocket receive loop;
- [x] lifecycle-кнопки работают и на текстовых, и на фото-карточках: leave/delete редактируют caption при необходимости;
- [x] после delete/leave понятное подтверждение остаётся в General, даже если сам топик уже удалён;
- [x] reconnect сообщает об обрыве и восстановлении соединения; покрыт отдельными тестами;
- [x] живая UX-проверка панели, поиска, connect/join и lifecycle топиков;
- [x] единые понятные подтверждения/ошибки для bind/delete/leave/reconnect/search;
- [x] постоянная кнопка `☰ Меню` у поля ввода Telegram; контекстная для General и отдельных топиков;
- [x] callback-кнопки устойчивы к кратким сбоям Telegram API/локального прокси: служебный ACK не блокирует действие;
- [x] импорт истории MAX при создании нового топика через opcode 49: по умолчанию 20 сообщений, настраивается 0–100 из General;
- [x] финальная полировка Personal v1.0.

**Gate закрыт 22.09.2026.** Personal UX v1.0 готов; переход к этапам 2–3 — только отдельным следующим решением.

## Этап 2 — Telegram bot onboarding

- /start и экран состояния;
- кнопка «Подключить MAX»;
- одноразовая onboarding-ссылка;
- временная browser UI-сессия для SMS-входа;
- callback после успешной авторизации;
- кнопка повторной авторизации при logout;
- команда полного отключения и удаления session state.

## Этап 3 — multi-user core

- сущности User / MaxSession / TelegramConnection;
- tenant-aware TopicStore;
- message_mappings в постоянном хранилище;
- Session Supervisor;
- Browser Session Manager;
- отдельный browser profile на пользователя;
- лимиты ресурсов и health checks;
- восстановление worker после crash.

## Этап 4 — Telegram как основной клиент

- private forum topics как основной UX, если режим доступен;
- fallback на приватную forum-supergroup;
- автоматическое создание темы на каждый MAX-чат;
- синхронизация названия/аватара;
- native reply/edit/delete/reactions;
- фото, видео, файлы и voice;
- архивирование/скрытие чатов;
- **поиск MAX из Telegram**: единый интерфейс поиска людей, групповых чатов и каналов, а не только уже известных текущей MAX-сессии;
- результаты поиска показывать кнопками с типом сущности, названием и кратким контекстом;
- из результата: открыть/подписаться/вступить, создать Telegram-топик и сразу привязать его к найденной сущности;
- пагинация/дозагрузка результатов и защита от неоднозначных совпадений;
- поиск должен работать из кнопочного меню бота без необходимости знать MAX chat_id или вручную открывать MAX.

## Этап 5 — shared/family mode

- одна MAX-сессия → несколько Telegram users;
- явный ACL;
- ответы всех разрешённых пользователей уходят от одного MAX-аккаунта;
- журнал административных действий;
- отключение конкретного Telegram-пользователя без потери MAX-сессии.

## Этап 6 — эксплуатация

- Docker deployment;
- PostgreSQL вместо локального JSON для multi-user state;
- encrypted session storage;
- резервное копирование mappings без browser secrets;
- метрики ONLINE/REAUTH/ERROR;
- rate limits Telegram-side;
- CI и автоматическое зеркало GitHub → GitVerse.
