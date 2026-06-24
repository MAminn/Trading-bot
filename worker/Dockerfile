FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ENGINE_BASE_DIR=/app/runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# Bundle artifacts in the exact layout live_code.py expects under ENGINE_BASE_DIR
COPY runtime /app/runtime
COPY data /app/data
COPY engine /app/engine
COPY main.py ingester.py ./

CMD ["python", "-u", "main.py"]
