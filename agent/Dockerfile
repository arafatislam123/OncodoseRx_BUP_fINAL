FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENT_DATA_DIR=/data

WORKDIR /app
RUN useradd --create-home --uid 10001 fsa \
    && mkdir -p /data && chown fsa /data

COPY agent/requirements.txt agent/requirements.txt
RUN pip install --no-cache-dir -r agent/requirements.txt

COPY agent agent
COPY config config
COPY db db

USER fsa
EXPOSE 9100
HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9100/healthz')" || exit 1

CMD ["python", "-m", "agent.main"]
