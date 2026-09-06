# Один образ на оба процесса — бота и сервер подписки. Они делят код и
# зависимости целиком, а какой процесс запускать, решает compose через command.
# Debian закреплён: от него зависят имена системных библиотек для Chromium ниже.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# Зависимости — отдельным слоем: правка кода не должна переустанавливать их.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install ".[browser]"

# Headless Chromium для JS-проверки антибота кабинета: с адресов дата-центров
# кабинет отдаёт вместо API страницу, которую нужно исполнить в браузере.
#
# Именно headless-shell и ручной список библиотек, а не `--with-deps chromium`:
# тот тянет полный браузер и Mesa с LLVM — больше 3 ГБ, на VPS с 10 ГБ диска
# сборка просто не помещается. Здесь — около 400 МБ. Браузер кладём в общий
# каталог (PLAYWRIGHT_BROWSERS_PATH), а не в домашний каталог root: процесс
# работает от пользователя bot. Сервер подписки браузером не пользуется, но
# образ у них один — это проще, чем два Dockerfile.
RUN apt-get update     && apt-get install -y --no-install-recommends         libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0 libcups2         libdbus-1-3 libdrm2 libgbm1 libglib2.0-0 libxkbcommon0 libx11-6 libxcb1         libxext6 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libasound2         libpango-1.0-0 libcairo2 fonts-liberation     && rm -rf /var/lib/apt/lists/*     && playwright install chromium-headless-shell     && chmod -R a+rX /ms-playwright

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

# Не root: процессу нечего делать с правами на контейнер.
RUN useradd --create-home --uid 1000 bot && chown -R bot:bot /app
USER bot

CMD ["python", "-m", "app.bot.main"]
