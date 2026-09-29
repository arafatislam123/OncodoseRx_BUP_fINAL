FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/srv/api:/srv

# same layout as the repo, so the api can reuse the agent's planner for what-if
WORKDIR /srv/api
RUN useradd --create-home --uid 10001 fsa

COPY api/requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY agent /srv/agent
COPY config /srv/config
COPY db /srv/db
COPY api/app app

USER fsa
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
