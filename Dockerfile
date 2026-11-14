FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Australia/Sydney

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd -g ${APP_GID} app && useradd -u ${APP_UID} -g ${APP_GID} -m -s /usr/sbin/nologin app \
 && mkdir -p /app/data && chown app:app /app/data

COPY --chown=app:app pytest.ini ./
COPY --chown=app:app app ./app
COPY --chown=app:app tests ./tests

USER app
EXPOSE 8000
# Single worker on purpose: the scheduler runs in-process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
