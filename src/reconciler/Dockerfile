FROM python:3.11-slim

WORKDIR /app

COPY reconciler/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY shared /app/shared
COPY reconciler/reconciler.py .

CMD ["python", "reconciler.py"]
