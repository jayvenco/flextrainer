# ---- build stage: install Python dependencies into an isolated prefix ----
FROM python:3.12-slim AS build
WORKDIR /src
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- runtime stage ----
FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/jayvenco/flextrainer" \
      org.opencontainers.image.title="FlexTrainer" \
      org.opencontainers.image.description="Self-hosted exam trainer: import questions, practise, track progress, check with ChatGPT"

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PUID=99 PGID=100 TZ=UTC
WORKDIR /app
COPY --from=build /install /usr/local
COPY app ./app
COPY CHANGELOG.md ./CHANGELOG.md
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /app/data
VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; [urllib.request.urlopen('http://127.0.0.1:8000/'+p, timeout=4) for p in ('health','ready')]" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
