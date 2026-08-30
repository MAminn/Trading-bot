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

# Mount points only — deliberately EMPTY in the image.
#
#   /app/runtime  (ENGINE_BASE_DIR)  the artifact tree: four historical enriched
#                                    CSVs at this level, eth_feature_shortlist_outputs/,
#                                    and "model files/" holding the 10 frozen
#                                    artifacts, v22_live_engine_export/run_*/,
#                                    runtime_cache/ and the five-file audit dir.
#   /app/outbox                      the durable signal outbox (SQLite + WAL).
#
# NOTHING from either tree is baked in. `runtime/` is git-ignored and staged
# out-of-band, so copying it would make the build fail on a clean checkout; it is
# also multi-GB and would force an image rebuild to rotate a single artifact.
# Baking the outbox in would be worse: undelivered PREPARED/READY rows would be
# reset by every rebuild, which is exactly what the two-phase protocol exists to
# prevent. Both paths are supplied at runtime as host directory mounts — see
# docker-compose.yml and README.md.
RUN mkdir -p /app/runtime /app/outbox

COPY engine /app/engine
COPY main.py ingester.py outbox.py ./

CMD ["python", "-u", "main.py"]
