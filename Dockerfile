FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.yaml .
COPY src/ ./src/

# Railway cron services run a one-shot command and exit.
# Set the start command per service in railway.toml or the dashboard.
CMD ["python", "-m", "src.main", "build"]
