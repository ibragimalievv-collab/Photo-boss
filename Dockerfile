FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip check
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin bot
COPY --chown=bot:bot app ./app

FROM base AS test
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt && pip check
COPY --chown=bot:bot tests ./tests
USER bot
CMD ["python", "-m", "pytest", "-q", "tests"]

# Keep the production stage last: platforms that build the Dockerfile without
# an explicit target (for example Railway) must start the bot, not the tests.
FROM base AS runtime
USER bot
CMD ["python", "-m", "app.main"]
