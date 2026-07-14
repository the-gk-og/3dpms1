FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# libpq-dev/build-essential cover psycopg2 building from source on platforms without a
# prebuilt wheel (e.g. some ARM hosts); safe to keep even when the binary wheel is used.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Data directories — these get volume-mounted at runtime so uploads and the SQLite
# database survive container rebuilds/redeploys. Created here too so the app still
# works if someone runs the image without mounting volumes (e.g. local testing).
RUN mkdir -p instance app/uploads app/static/uploads

EXPOSE 8000

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120", "run:app"]
