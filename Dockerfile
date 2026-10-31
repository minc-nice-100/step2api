FROM python:3.12-alpine

WORKDIR /app
COPY step2api/ ./step2api/
COPY step2api.py README.md ./

ENV STEP_HOST=0.0.0.0 \
    STEP_PORT=8790 \
    STEP_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8790

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s \
  CMD wget -qO- http://127.0.0.1:8790/ping || exit 1

CMD ["python", "step2api.py"]
