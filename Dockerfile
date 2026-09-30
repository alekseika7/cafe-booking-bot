FROM python:3.14-slim
ARG BOOKING_UID=10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
COPY pyproject.toml poetry.lock ./
RUN pip install --no-cache-dir poetry==2.5.1 \
    && poetry install --only main --no-root --no-interaction --no-ansi \
    && useradd --uid "${BOOKING_UID}" --create-home booking \
    && mkdir /data && chown booking:booking /data
COPY bot.py .
COPY src ./src
# Контакты и Telegram передаются окружением при запуске; .env в образ не копируется.
USER booking
ENTRYPOINT ["python", "bot.py", "--config", "/config/config.toml", "--state-dir", "/data"]
CMD ["run"]
