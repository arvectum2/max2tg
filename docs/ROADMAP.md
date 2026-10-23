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

## Новый план релизов — Reliability → Personal UX → SaaS (23.10.2026)

Первая цель — не терять сообщения и не создавать дубли при сбоях, затем
улучшить UX и подготовить многопользовательский продукт. Personal UX v1.0
сохраняет статус закрытого этапа; Reliability v1.1 — новый активный gate.

### P0 — Reliability v1.1 (в работе)

- [x] **P0.1 — догрузка истории (первый срез реализован 23.10.2026).** Известные чаты после
  reconnect/restart: persisted cursor, пагинация MAX HISTORY, проверка ID,
  хронологический импорт только пропущенных событий. Без безопасной точки
  восстановления первый запуск не должен слепо пересылать старую историю.
  Тест: >100 сообщений, гонки с live, собственные исходящие и TG echoes.
- [x] **P0.2 — Persistent Outbox (реализован для MAX → Telegram 23.10.2026).** SQLite/PostgreSQL queue: RECEIVED →
  PENDING → SENDING → SENT, RETRY/UNKNOWN при ошибке, durable payload,
  retry с backoff, сохранение каждой части текста/медиа. Не считать сетевой
  timeout гарантированным отказом: Telegram API не даёт exactly-once.
- [x] **P0.3 — edit/delete/reactions (безопасный поддерживаемый срез 23.10.2026).** Проверка реальных MAX opcodes;
  двусторонние изменения/удаления/реакции только если API поддерживает,
  корректное отображение и idempotency.
- [x] **P0.4 — health + autorecovery (первый рабочий срез 23.10.2026).** Монитор WebSocket, Telegram API/polling,
  MAX auth, queue lag, cursor, topic state; автоперезапуск, controlled retries,
  уведомление с действием REAUTH_REQUIRED.
- [x] **P0.5a — безопасные сбойные испытания (23.10.2026).** Аварийные
  дочерние процессы, SQLite locks, Telegram timeout/retry, многочастные
  сообщения, сбои MAX/polling, catch-up и timestamp-границы. Подробности:
  docs/RELIABILITY_P0_5.md.
- [ ] **P0.5b — физические испытания на небоевом стенде.** Тестовые MAX/TG,
  отключение сети, logout/login, отключение тестового хранилища и
  длительный простой. На рабочем Mac mini разрушительные тесты не проводим.

**Gate:** после остановки и восстановления пропущенные сообщения появляются
в правильных темах, reply mappings сохраняются, необъяснимых потерь нет;
неопределённые отправки видны оператору и не повторяются слепо.

### P1 — Personal UX v1.2

- [x] **P1.1 — MAX elements → Telegram HTML (23.10.2026).** Bold/italic/underline/strike/code/quote/links/verified mentions, безопасные ссылки, Unicode и длинные сообщения. Маркеры списков без структуры сохраняются текстом; docs/PERSONAL_UX_P1_1.md.
- [x] **P1.2 — медиа (23.10.2026).** Альбомы фото 2–10,
  оригинальные видео при наличии прямого URL, FILE из истории,
  голосовые, проигрываемое аудио, превью с явными fallback.
  Устойчивый outbox с сохранением ID всех фото: docs/PERSONAL_UX_P1_2.md.
- [x] **P1.3 — поиск сообщений и история (23.10.2026).** /find,
  /findall, фильтры по датам (МСК), поиск по подключённым чатам,
  навигация кнопкой и ссылки на пересланные сообщения. /history
  догружает старые страницы по 20 с сохранённым курсором и защитой
  от повторов: docs/PERSONAL_UX_P1_3.md.
- [ ] Индивидуальные настройки чатов: sync, mute, media, history depth,
  archive/hide и синхронизация имени/аватара.

### P2 — Telegram-first SaaS

- [ ] Onboarding: /start → Подключить MAX → временная SMS-веб-сессия →
  ONLINE/REAUTH/Disconnect. Код SMS и секреты не сохранять в логах.
- [ ] Multi-user: User/MaxSession/TelegramConnection, изолированные browser
  profiles и workers, Supervisor, ACL, PostgreSQL, encrypted session state,
  backup, health checks, worker crash recovery.
- [ ] Личные темы бота при подтверждённой поддержке нужного режима
  Telegram Bot API; fallback — приватная форум-супергруппа.
- [ ] Веб-кабинет и панель оператора: подключения, статусы, очередь, ошибки,
  настройки, аналитика и административные действия.
- [ ] SaaS prototype на Mac mini: минимум два независимых аккаунта, замеры
  CPU/RAM/сети, стоимость одной круглосуточной сессии и нагрузочный тест.
- [ ] SaaS beta на Timeweb Cloud (РФ): TLS, защита ПДн, резервирование,
  мониторинг, политики удаления, требования/уведомления РКН.
- [ ] Trial 7 дней, платежи, лимиты и тарифы. 99 ₽/аккаунт — гипотеза,
  до теста себестоимости и retention бизнес-модель не зафиксирована.
- [ ] Метрики acquisition, activation, retention, delivery reliability,
  conversion, LTV и unit economics.

### Архитектура на будущее

- [ ] Source Adapter → Bridge Core (event model, queue, dedup, mappings,
  ACL, monitoring) → Destination Adapter. MAX/TG — первые реализации;
  новые платформы только после стабилизации ядра.
- [ ] Официальный MAX Bot API рассматривать отдельно от пользовательского
  reverse-engineered WebSocket: возможности/права не эквивалентны.
- [ ] GitHub arvectum2/max2tg (main) — source of truth; GitVerse — зеркало;
  выпуск только после проверенного релиза.

**Следующее рабочее задание:** P1.4 (индивидуальные настройки
чата: sync/mute/media/history depth/archive и изменение метаданных).
P1.1—P1.3 реализованы и покрыты регрессионными тестами.
P0.1—P0.4 работают, P0.5a выполнен на изолированных сбойных испытаниях.
P0.5b остаётся обязательным инфраструктурным этапом перед полноценным
SaaS SLA. Неоднозначные разрывы истории отмечаются HISTORY_GAP вместо
ложного успешного завершения синхронизации.
Отчёты: docs/RELIABILITY_P0_1.md — docs/RELIABILITY_P0_5.md.

**Последовательность:** P0.1 → P0.2 → P0.3 → P0.4 → P0.5 → P1 →
SaaS prototype → SaaS beta → коммерческий запуск.

---

## Стратегическое направление — MAX2TG SaaS

Целевая модель: пользователь один раз авторизует MAX, после чего его MAX-сессия живёт на нашей инфраструктуре и продолжает работать автономно. Telegram остаётся основным клиентом, а сайт/личный кабинет — вторым полноценным интерфейсом управления.

Архитектурное решение:
- переходный слой — Browser Farm / Session Capsule: один MAX-аккаунт = отдельный persistent browser profile + worker;
- Browser Farm используется как compatibility/bootstrap layer и для fallback;
- конечная цель масштабирования — собственный лёгкий MAX Protocol Client без постоянно работающего Chromium;
- приложение работает через нормализованный внутренний event layer, чтобы MAX adapter можно было менять без переписывания Telegram-части.

Инфраструктурный план:
1. multi-user прототип и нагрузочные тесты — на текущем Mac mini;
2. после подтверждения архитектуры — отдельный сервер Timeweb Cloud;
3. серверная инфраструктура, БД и хранилище пользовательских сессий переносятся на Timeweb Cloud;
4. сервер планируется использовать как место хранения персональных данных и учитывать при подготовке/актуализации документов и уведомлений в РКН;
5. далее — горизонтальное масштабирование workers при росте нагрузки.

Входные воронки:
- сайт MAX2TG → регистрация/вход → личный кабинет → «Подключить MAX»;
- Telegram-бот → /start → «Подключить MAX» → onboarding;
- обе воронки создают один и тот же User/MaxSession и ведут в единый backend.

Рабочая гипотеза монетизации:
- 7 дней free trial;
- затем one-time purchase;
- стартовый ценовой ориентир — 99 ₽ за подключаемый аккаунт;
- цена и модель должны быть подтверждены юнит-экономикой до production billing.

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
- [x] reconciliation после reconnect: новые MAX-чаты, появившиеся во время простоя, автоматически получают Telegram-топик; self-message в новом чате догружает metadata через opcode 48;
- [x] автономный macOS runtime: bridge под `launchd` с автозапуском/KeepAlive; session-watch каждые 30 секунд синхронизирует ротацию MAX-сессии из выделенного Chrome-профиля `MAX` и автоматически перезапускает bridge;
- [x] финальная полировка Personal v1.0.

**Gate закрыт 22.09.2026.** Personal UX v1.0 готов; переход к этапам 2–3 — только отдельным следующим решением.

## Этап 2 — Session Capsule v1 на Mac mini

Цель: доказать, что текущую личную архитектуру можно превратить в независимые серверные пользовательские сессии.

- выделить `session_id` / `user_id` вместо предположения об одном MAX-аккаунте;
- один MAX-аккаунт = один persistent browser profile;
- сделать `max-session-worker`, принимающий profile/session config;
- поднять рядом минимум две независимые MAX-сессии;
- проверить одновременную двустороннюю работу нескольких MAX-аккаунтов;
- изолировать session state, mappings и runtime каждого пользователя;
- добавить heartbeat, restart и состояние ONLINE / REAUTH_REQUIRED / ERROR;
- после рестарта Mac mini автоматически восстанавливать все активные Session Capsule;
- проверить отсутствие cross-tenant утечек событий/сообщений;
- подготовить интерфейс Session Supervisor.

**Gate:** минимум две независимые MAX-сессии стабильно работают параллельно через один экземпляр backend.

## Этап 3 — onboarding: сайт + Telegram-бот

### 3.1 Telegram funnel

- /start и экран состояния;
- кнопка «Подключить MAX»;
- одноразовая onboarding-ссылка;
- временная browser UI-сессия для SMS-входа;
- callback после успешной авторизации;
- кнопка повторной авторизации при logout;
- команда полного отключения и удаления session state.

### 3.2 Web funnel / личный кабинет

- landing → регистрация/вход;
- dashboard пользователя;
- «Подключить MAX»;
- временное интерактивное окно серверного браузера для первичной авторизации;
- автоматическое определение успешного MAX login;
- экран статуса MAX-сессии;
- reauth при logout;
- подключение Telegram к существующему user account;
- список MAX-чатов и созданных мостов;
- управление bind/search/leave/delete из UI;
- настройки импорта истории и поведения bridge.

### 3.3 Единый account linking

- один User независимо от входа через сайт или Telegram;
- безопасные одноразовые nonce/deep links для привязки;
- исключить создание дубликатов аккаунтов;
- возможность начать onboarding на сайте и закончить в Telegram и наоборот.

## Этап 4 — multi-user core

- сущности User / MaxSession / TelegramConnection / Subscription;
- tenant-aware TopicStore;
- message_mappings в постоянном хранилище;
- Session Supervisor;
- Browser Session Manager;
- отдельный browser profile на пользователя;
- очередь событий между MAX adapter и Telegram adapter;
- normalized internal message/event schema;
- лимиты ресурсов и health checks;
- восстановление worker после crash;
- автоматическое восстановление сессий после перезапуска хоста;
- encrypted session storage;
- audit trail административных действий.

## Этап 5 — Telegram как основной клиент

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

## Этап 6 — shared/family mode

- одна MAX-сессия → несколько Telegram users;
- явный ACL;
- ответы всех разрешённых пользователей уходят от одного MAX-аккаунта;
- журнал административных действий;
- отключение конкретного Telegram-пользователя без потери MAX-сессии.

## Этап 7 — перенос production-инфраструктуры на Timeweb Cloud

После прохождения Mac mini gate:

- выбрать и приобрести production VPS/сервер Timeweb Cloud;
- развернуть backend/API;
- PostgreSQL вместо локального JSON для multi-user state;
- persistent encrypted storage для MAX session profiles;
- reverse proxy + TLS;
- worker supervisor;
- резервное копирование mappings без browser secrets;
- резервное копирование БД;
- мониторинг ONLINE / REAUTH / ERROR;
- централизованные логи;
- rate limits Telegram-side и web-side;
- firewall и минимизация открытых портов;
- secrets management;
- подготовить/актуализировать документы по обработке ПД;
- перед production запуском отдельно проверить актуальные требования к уведомлению РКН и корректно указать используемую инфраструктуру хранения ПД;
- CI/CD GitHub → production;
- сохранить автоматическое зеркало GitHub → GitVerse.

## Этап 8 — billing и коммерческий запуск

Рабочая гипотеза:

```text
регистрация
  ↓
7 дней free trial
  ↓
разовая покупка
  ↓
активный MAX account
```

Задачи:
- trial_started_at / trial_expires_at / entitlement;
- trial без платёжной карты;
- понятный статус оставшихся дней;
- paywall после окончания trial без удаления сохранённой конфигурации;
- платёжный провайдер для рублей;
- webhook оплаты;
- idempotent payment processing;
- чеки/фискализация — по выбранной платёжной схеме;
- one-time entitlement;
- стартовый тест цены: 99 ₽ за подключаемый аккаунт;
- возможность изменить цену без релиза клиента;
- promo/referral codes — позже;
- аналитика воронки: landing → onboarding → MAX login → Telegram bind → trial active → purchase;
- проверить unit economics до масштабной рекламы.

## Этап 9 — уход от постоянного Chromium

Цель: Browser Farm перестаёт быть основным runtime и остаётся bootstrap/fallback слоем.

- исследовать MAX WebSocket/HTTP protocol на основании уже наблюдаемого web-клиента;
- выделить `max-adapter` с единым контрактом;
- реализовать MAX Protocol Client;
- Chrome использовать только для первоначальной авторизации/reauth, если это технически возможно;
- сравнить поведение protocol client с browser worker;
- автоматический fallback на Browser Worker при несовместимом изменении протокола;
- rolling update protocol adapter;
- существенно увеличить количество одновременных MAX-сессий на один сервер.

## Порядок ближайших работ

0. **Reliability v1.1 P0** — P0.1 catch-up реализован; далее P0.2
   Persistent Outbox, P0.3 edit/delete/reactions, P0.4 health/autorecovery,
   P0.5 тестирование сетевых/дисковых отказов.
0. **Personal UX v1.2 (P1)** — форматирование, медиа, поиск сообщений
   и индивидуальные настройки чатов.
1. Session Capsule v1 на Mac mini.
2. Вторая независимая MAX-сессия и доказательство multi-user isolation.
3. Session Supervisor + автоматическое восстановление.
4. Telegram onboarding.
5. Минимальный web cabinet и web onboarding.
6. Единая User/MaxSession модель.
7. После прохождения gate — перенос инфраструктуры на Timeweb Cloud.
8. Billing/free trial.
9. Protocol Client как отдельная ветка масштабирования.
