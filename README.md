# tg-max-bridge

Отдельный резервный мост из Telegram в MAX. Он не имеет отношения к `openmap` и
не требует доступа к его коду или данным.

Разрешённый участник отвечает командой `/max` на важное сообщение в Telegram.
Бот сохраняет текст в SQLite, а отдельный диспетчер доставляет копию в
настроенную группу или канал MAX через `max-mcp`. Полную переписку Telegram бот
не читает: Privacy Mode можно и нужно оставить включённым.

## Что выбрать в MAX

Для резервной связи лучше **закрытая группа**: там участники смогут не только
получать объявления, но и отвечать, когда Telegram недоступен. Канал подходит,
только если нужен односторонний режим «администратор → подписчики».

Мост работает от обычного пользовательского аккаунта MAX через неофициальный
клиент [`max-mcp`](https://github.com/renosaza/max-mcp). Если существующий
аккаунт уже состоит в нужной MAX-группе, новый аккаунт создавать не надо. В ином
случае установите MAX на телефон, зарегистрируйтесь по поддерживаемому номеру и
добавьте этот аккаунт в группу. Официальный MAX-бот потребовал бы отдельной
регистрации в платформе для бизнеса; для этого MVP он не нужен.

## Что нельзя автоматизировать за владельца

Остаются только два действия с подтверждением личности:

1. В Telegram откройте `@BotFather`, выполните `/newbot`, сохраните выданный
   токен, затем в `/setprivacy` оставьте Privacy Mode **Enabled**. Добавьте бота
   в исходную Telegram-группу; администратором делать его не нужно.
2. Один раз авторизуйте пользовательскую MAX-сессию по QR-коду либо SMS:

   ```bash
   cd /absolute/path/to/max-mcp
   uv sync --no-dev --frozen
   uv run --no-dev --frozen max-mcp-login login-qr
   ```

   Для SMS вместо последней команды:

   ```bash
   uv run --no-dev --frozen max-mcp-login login-sms --phone +79990000000
   ```

QR-код сканируется в мобильном приложении MAX. Сессия хранится локально в
`~/.max-mcp`, поэтому каталог нельзя публиковать, копировать в репозиторий или
передавать другим людям.

## Локальная настройка

Требуются Python 3.12–3.13 и [`uv`](https://docs.astral.sh/uv/).

```bash
cd /absolute/path/to/tg-max-bridge
uv sync --all-groups --frozen
cp .env.example .env
chmod 600 .env
```

Заполните `.env`:

- `TELEGRAM_BOT_TOKEN` — токен от BotFather;
- `TELEGRAM_ALLOWED_CHAT_IDS` — ID исходной Telegram-группы (обычно начинается
  с `-100`); допускается список через запятую;
- `TELEGRAM_ALLOWED_USER_IDS` — ID людей, которым разрешена команда `/max`;
- `MAX_CHAT_ID` — ID целевой группы или канала MAX;
- `MAX_MCP_DIRECTORY` — абсолютный путь к клону `max-mcp`.

Пустые allow-list запрещены: сервис специально завершится с ошибкой, чтобы
случайно не принимать команды от посторонних.

Чтобы узнать Telegram ID, сначала укажите только настоящий токен, добавьте бота
в группу и отправьте там `/max`. Остальные обязательные поля пока могут быть
пустыми. До запуска моста выполните:

```bash
uv run tg-max-bridge discover-telegram
```

Команда только читает последние bot updates и печатает `chat_id`/`user_id`, не
показывая токен. Затем замените примерные значения в `.env` на найденные.

После входа в MAX найдите ID группы без отправки сообщений:

```bash
uv run tg-max-bridge discover-max --query "часть названия"
```

Проверьте конфигурацию и базу — команда `doctor` ничего не отправляет:

```bash
uv run tg-max-bridge init-db
uv run tg-max-bridge doctor
```

Запуск:

```bash
uv run tg-max-bridge run
```

## Использование

В исходной Telegram-группе ответьте на важное текстовое сообщение или подпись
к медиа:

```text
/max
```

Если Telegram требует обращение к конкретному боту, используйте
`/max@BotUsername`. Команды с аргументами отклоняются.

Сначала сообщение атомарно попадает в SQLite, и только потом бот подтверждает
постановку в очередь. Повторная команда для того же сообщения не создаёт вторую
запись. В MAX добавляется стабильная метка вида `[tg:-100123/456]`; после
неоднозначного сетевого обрыва мост ищет эту точную метку перед повтором.

Текст ограничивается 4000 символами MAX. Слишком длинная копия безопасно
сокращается с явной пометкой. Для сообщения с подписью передаётся подпись и
пометка о том, что вложение не скопировано; медиа без подписи отклоняется.
Защищённый от пересылки контент также отклоняется.

Состояние очереди:

```bash
uv run tg-max-bridge outbox
uv run tg-max-bridge outbox --status ambiguous
```

## Ограничение при отключении интернета

Мост не может получить сообщение, которое не дошло до Telegram, и не может
доставить его, пока хост не видит MAX. Поэтому для московского сценария его
лучше запускать на постоянно включённом VPS с доступом к обеим платформам.
SQLite сохраняет уже принятые задания между перезапусками и продолжит доставку
после восстановления сети. Саму MAX-группу участникам стоит открыть и проверить
заранее — она остаётся самостоятельным каналом связи без моста.

## Запуск через systemd на VPS

Шаблон предполагает:

- приложение в `/opt/tg-max-bridge`;
- `max-mcp` в `/opt/max-mcp`;
- секреты в `/etc/tg-max-bridge.env` с правами `0640`, доступные только root и
  группе сервиса;
- отдельного системного пользователя `tg-max-bridge`;
- данные и MAX-сессию в `/var/lib/tg-max-bridge`.

Пример установки (репозитории клонируются обычным `git`, без GitHub-коннектора;
для приватного bridge-репозитория на VPS заранее нужен deploy key или иной GitHub
credential):

```bash
sudo useradd --system --home /var/lib/tg-max-bridge \
  --create-home --shell /usr/sbin/nologin tg-max-bridge
sudo git clone https://github.com/ryzenovod/tg-max-bridge.git /opt/tg-max-bridge
sudo git clone https://github.com/ryzenovod/max-mcp.git /opt/max-mcp
sudo git -C /opt/max-mcp checkout b2922b9314056947c60d774cb0bfd48b99a6fc3c
sudo uv sync --directory /opt/tg-max-bridge --no-dev --frozen
sudo uv sync --directory /opt/max-mcp --no-dev --frozen
sudo install -o root -g tg-max-bridge -m 0640 \
  /opt/tg-max-bridge/.env.example /etc/tg-max-bridge.env
```

В `/etc/tg-max-bridge.env` задайте:

```dotenv
MAX_MCP_DIRECTORY=/opt/max-mcp
SQLITE_PATH=/var/lib/tg-max-bridge/bridge.sqlite3
```

Остальные значения заполните как в локальном `.env`. Затем создайте MAX-сессию
именно от сервисного пользователя (интерактивный QR появится в терминале):

```bash
sudo -u tg-max-bridge env HOME=/var/lib/tg-max-bridge \
  uv run --no-dev --frozen --directory /opt/max-mcp max-mcp-login login-qr
```

Установка unit-файла:

```bash
sudo install -m 0644 /opt/tg-max-bridge/systemd/tg-max-bridge.service \
  /etc/systemd/system/tg-max-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now tg-max-bridge
sudo systemctl status tg-max-bridge
```

## Docker

Docker-образ закрепляет `max-mcp` на проверенном commit
`b2922b9314056947c60d774cb0bfd48b99a6fc3c`. Он основан на upstream commit
`0485269e3fa7fc1d9dce00ac8005b255402951a0`, обновляет уязвимые зависимости и
исправляет нормализацию чатов с бинарными полями; `pip-audit` для этого окружения
проходит без находок. Compose использует отдельные
именованные тома для очереди и MAX-сессии и запускает процесс без root-прав.

```bash
cp .env.example .env
chmod 600 .env
docker compose -f docker-compose.example.yml build
docker compose -f docker-compose.example.yml run --rm tg-max-bridge \
  uv run --no-dev --frozen --directory /opt/max-mcp max-mcp-login login-qr
docker compose -f docker-compose.example.yml up -d
```

Не запускайте одновременно локальную, systemd- и Docker-копии с одной и той же
Telegram-конфигурацией: два long-polling процесса будут конкурировать за updates.

## Cloud.ru Container Apps

Для Cloud.ru добавлен отдельный webhook-режим. Он не держит контейнер горячим:
Telegram будит публичный HTTPS endpoint только при новом update, контейнер
обрабатывает одну команду `/max`, синхронно делает один проход доставки в MAX и
возвращает Telegram успех только после состояния `sent` в outbox. Если MAX
временно недоступен или доставка неоднозначна, endpoint отвечает `503`, чтобы
Telegram повторил запрос позже.

Минимальные настройки контейнерного приложения:

- образ из приватного registry;
- публичный HTTPS endpoint, порт `8080`;
- `min_instances=0`, `max_instances=1`;
- приватный volume, смонтированный в `/state`;
- env `SQLITE_PATH=/state/bridge.sqlite3` и `HOME=/state/home`;
- health probe `GET /healthz`.

Cloud.ru рекомендует SQLite на Object Storage только для небольшой/test-нагрузки.
Для этого моста с одной группой используется rollback-journal `DELETE`, ровно
один экземпляр и сериализованные записи. Для нескольких групп или интенсивного
потока нужна отдельная PostgreSQL, а не увеличение числа экземпляров.

Webhook-переменные:

```dotenv
TELEGRAM_MODE=webhook
TELEGRAM_WEBHOOK_URL=https://service-name.containerapps.ru/telegram/webhook
TELEGRAM_WEBHOOK_SECRET=replace-with-random-allowed-token
PORT=8080
SQLITE_PATH=/state/bridge.sqlite3
HOME=/state/home
```

Webhook secret передаётся Telegram как
`X-Telegram-Bot-Api-Secret-Token`; допускаются только латинские буквы, цифры,
`_`, `.` и `-`, длина 1–256 символов. URL должен быть HTTPS.

MAX-сессию в облаке не кладите в образ. Сделайте локально gzip-tar только из
ожидаемых файлов сессии и положите результат в secret env
`MAX_MCP_SESSION_TARB64`:

```bash
tar -C "$HOME" -czf - \
  .max-mcp/session.db .max-mcp/session.kind .max-mcp/session.phone \
  | base64 | tr -d '\n'
```

Если какого-то metadata-файла нет, уберите только его из команды; файл
`session.db` обязателен. При
старте entrypoint безопасно распакует seed только один раз, только в
`~/.max-mcp`, не перезаписывая уже существующую persistent-сессию, выставит права
`0700/0600` и удалит `MAX_MCP_SESSION_TARB64` из окружения перед запуском моста.

Перед установкой cloud webhook остановите локальный LaunchAgent, иначе локальный
polling и webhook будут конкурировать за Telegram updates:

```bash
launchctl unload "$HOME/Library/LaunchAgents/com.ryzenovod.tg-max-bridge.plist"
```

Откат: удалите webhook у Telegram Bot API и снова загрузите LaunchAgent:

```bash
launchctl load "$HOME/Library/LaunchAgents/com.ryzenovod.tg-max-bridge.plist"
```
