FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN useradd --create-home --uid 1000 veritas

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY veritas ./veritas
RUN mkdir -p /app/data && chown -R veritas:veritas /app

USER veritas
VOLUME ["/app/data"]
CMD ["python", "-m", "veritas"]
