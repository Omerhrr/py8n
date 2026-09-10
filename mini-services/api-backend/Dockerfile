# Py8n backend image (FastAPI + engine + Celery worker)
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY demo ./demo
COPY migrations ./migrations
COPY alembic.ini .

ENV PY8N_DEBUG=false
EXPOSE 8000

# Apply/stamp Alembic migrations, then serve - see migrations/bootstrap.py
# for why this runs as a separate process before uvicorn (not an import
# inside the async app) and how existing databases get adopted.
CMD ["sh", "-c", "python -m migrations.bootstrap && exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"]
