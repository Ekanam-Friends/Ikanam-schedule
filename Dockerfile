# Один образ на оба процесса — бота и сервер подписки. Они делят код и
# зависимости целиком, а какой процесс запускать, решает compose через command.
FROM python:3.12-slim

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
# Браузер кладём в общий каталог (см. PLAYWRIGHT_BROWSERS_PATH), а не в
# домашний каталог root: процесс работает от пользователя bot. Образ тяжелеет
# примерно на 400 МБ; сервер подписки браузером не пользуется, но образ у
# них один — это проще, чем два Dockerfile.
RUN playwright install --with-deps chromium && chmod -R a+rX /ms-playwright

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

# Не root: процессу нечего делать с правами на контейнер.
RUN useradd --create-home --uid 1000 bot && chown -R bot:bot /app
USER bot

CMD ["python", "-m", "app.bot.main"]
