FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
      "fastapi>=0.115" "uvicorn[standard]>=0.30" "motor>=3.5" "pymongo>=4.6" \
      "pydantic>=2.7" "pydantic-settings>=2.3" "httpx[http2]>=0.27" \
      "selectolax>=0.3.21" "apscheduler>=3.10" "python-dateutil>=2.9"

COPY src ./src
COPY scripts ./scripts

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
