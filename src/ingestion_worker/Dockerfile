FROM python:3.11-slim

WORKDIR /app

COPY ingestion_worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared /app/shared
COPY ingestion_worker/ingestion_worker.py .
COPY ingestion_worker/delta_token_store.py .

EXPOSE 8081
CMD ["uvicorn", "ingestion_worker:app", "--host", "0.0.0.0", "--port", "8081"]
