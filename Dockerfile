# OpenImmersive / yulingling — BabelDOC (pdf2zh-next) translation microservice.
# CPU-only inference (onnxruntime); image is sizeable but no GPU needed.
FROM python:3.12-slim

RUN pip install --no-cache-dir pdf2zh-next

WORKDIR /app
COPY wrapper.py ./

# Jobs/results live in /data (named volume: survives rebuilds, 24h TTL).
# DocLayout-YOLO model + fonts are downloaded on first run into
# /root/.cache/babeldoc — mount a named volume at /root/.cache or the
# multi-hundred-MB download repeats on every container rebuild.
RUN mkdir -p /data /root/.cache
ENV DATA_DIR=/data \
    PORT=21012 \
    TRANSLATE_ENGINE=google

EXPOSE 21012
CMD ["python", "wrapper.py"]
