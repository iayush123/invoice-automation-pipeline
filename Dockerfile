FROM python:3.12-slim

WORKDIR /app

# Install system deps for psycopg2 and pdf processing
RUN apt-get update && apt-get install -y \
    libpq-dev gcc poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run alembic migrations then start the app
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
