FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /artifact
COPY . .
RUN python -m pip install --no-cache-dir -e ".[dev,duckdb]"

CMD ["python", "artifact/verify.py"]
