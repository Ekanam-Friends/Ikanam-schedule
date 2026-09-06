# Развёртывание на VPS (Yandex Cloud, Timeweb, Cloud.ru и любой другой)

Минимальная виртуальная машина, Docker Compose, HTTPS через Let's Encrypt.
Домен покупать не нужно. Стартового гранта Yandex Cloud хватает на несколько
месяцев; после него такая машина стоит порядка 300–500 ₽/мес — прерываемая
дешевле, но её могут выключить в любой момент, для ночной синхронизации это
неприемлемо.

Почему именно российское облако: личный кабинет с зарубежных адресов требует
капчу, а вводить её ночью некому.

## 1. Виртуальная машина

Консоль → **Compute Cloud** → Создать ВМ:

| Параметр | Значение |
|---|---|
| Образ | Ubuntu 24.04 LTS |
| Зона | любая `ru-central1-*` |
| Платформа | Intel Ice Lake, **2 vCPU, 20 % гарантии, 2 ГБ RAM** |
| Диск | HDD 15 ГБ |
| Прерываемая | **нет** |
| Публичный IP | **автоматически** |
| Логин | `ubuntu`, вставить свой публичный SSH-ключ |

Публичный ключ на Windows: `type %USERPROFILE%\.ssh\id_ed25519.pub`; если его
нет — `ssh-keygen -t ed25519`.

После создания запомните **публичный IP** (например `51.250.1.2`).

## 2. Порты

**Virtual Private Cloud → Группы безопасности** → группа по умолчанию (или
новая, привязанная к ВМ). Входящие правила:

| Порт | Откуда | Зачем |
|---|---|---|
| 22 | ваш IP или 0.0.0.0/0 | SSH |
| 80 | 0.0.0.0/0 | выпуск сертификата Let's Encrypt |
| 443 | 0.0.0.0/0 | подписка на календарь |

Исходящий трафик — разрешить весь.

## 3. Docker на машине

```bash
ssh ubuntu@51.250.1.2
```

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker ubuntu
exit
```

Если скрипт отказывается ставить Docker на очень свежую Ubuntu, подойдёт
пакет из репозитория дистрибутива:

```bash
sudo apt-get install -y docker.io docker-compose-v2
sudo usermod -aG docker ubuntu
```

Войти заново, чтобы группа `docker` применилась.

## 4. Код и настройки

```bash
git clone https://github.com/Ekanam-Friends/Ikanam-schedule.git
cd Ikanam-schedule
cp .env.example .env
nano .env
```

Заполнить:

```
BOT_TOKEN=            # от @BotFather
OWNER_CHAT_ID=        # ваш Telegram ID — сюда придут алерты о поломках
CREDENTIALS_KEY=      # см. ниже
POSTGRES_PASSWORD=    # любой длинный случайный
DOMAIN=51-250-1-2.sslip.io            # ваш IP через дефисы + .sslip.io
PUBLIC_BASE_URL=https://51-250-1-2.sslip.io
```

`DATABASE_URL` в `.env` не нужен — compose собирает его сам из настроек
PostgreSQL.

Ключ шифрования и пароль базы:

```bash
python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
openssl rand -hex 24
```

`CREDENTIALS_KEY` — сохраните где-нибудь ещё. Потеря ключа означает, что
сохранённые токены доступа перестанут читаться, и всем пользователям придётся
подключать кабинет заново.

## 5. Запуск

```bash
docker compose up -d --build
docker compose logs -f --tail=50
```

В логе бота: «Применяю миграции… Схема базы актуальна… Start polling».
В логе Caddy: получение сертификата для домена. Через минуту:

```bash
curl -I https://51-250-1-2.sslip.io/healthz
```

Ожидается `HTTP/2 200`. После этого `/calendar` в боте выдаёт рабочие ссылки.

## 6. Обновление

```bash
cd ~/Ikanam-schedule
git pull
docker compose up -d --build
```

Миграции базы применяются при старте сами. Данные пользователей в volume
`pgdata`, пересборка контейнеров их не трогает.

## 7. Резервная копия

```bash
docker compose exec db pg_dump -U bot bot | gzip > backup-$(date +%F).sql.gz
```

Дамп содержит зашифрованные токены — без `CREDENTIALS_KEY` он бесполезен для
чужого, но хранить его всё равно стоит отдельно от ключа.

## Telegram из России

На российском хостинге (проверено на reg.ru) `api.telegram.org` недоступен:
TCP-соединение открывается, но TLS-рукопожатие с этим именем в SNI обрывается.
При этом кабинет РАНХиГС, наоборот, работает только с российского адреса —
из-за рубежа он требует капчу. Поэтому сервер остаётся в России, а к Telegram
бот ходит через прокси.

В compose для этого есть сервис `proxy` (sing-box), выключенный по умолчанию:

1. Положите конфиг sing-box в `proxy/config.json` — inbound `socks` на порту
   1080 и любой рабочий outbound за рубежом. Каталог `proxy/` в `.gitignore`:
   в конфиге ключи от серверов.
2. В `.env` добавьте `TELEGRAM_PROXY=socks5://proxy:1080`.
3. Запускайте с профилем: `docker compose --profile proxy up -d`.

Проверить связь из сети compose:

```bash
docker run --rm --network ikanam_default curlimages/curl \
  -sS -o /dev/null -w "%{http_code}\n" --proxy socks5h://proxy:1080 https://api.telegram.org/
```

Ответ `302` означает, что Telegram достижим. Клиент кабинета прокси не
использует принципиально (`trust_env=False`) — он должен ходить напрямую.

## Что делать, если

- **Caddy не получает сертификат** — проверьте, что порты 80 и 443 открыты в
  группе безопасности и что `DOMAIN` соответствует публичному IP машины.
- **Бот не отвечает** — `docker compose logs bot`. Чаще всего неверный
  `BOT_TOKEN`.
- **«Личный кабинет недоступен» при подключении** — `docker compose logs bot`
  покажет код ответа кабинета. 5xx — он лежит, подождать; 403 текстом — защита
  сочла запрос ботом, писать в issues.
