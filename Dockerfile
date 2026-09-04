# Один образ на оба процесса — бота и сервер подписки. Они делят код и
# зависимости целиком, а какой процесс запускать, решает compose через command.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Зависимости — отдельным слоем: правка кода не должна переустанавливать их.
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

# Не root: процессу нечего делать с правами на контейнер.
RUN useradd --create-home --uid 1000 bot && chown -R bot:bot /app
USER bot

CMD ["python", "-m", "app.bot.main"]
