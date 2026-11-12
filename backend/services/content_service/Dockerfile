FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY packages/agent_core /app/packages/agent_core
COPY services/content_service /app/services/content_service
RUN pip install --no-cache-dir /app/packages/agent_core /app/services/content_service
ENV CONTENT_DATA_DIR=/data
EXPOSE 8002
CMD ["uvicorn", "content_service.main:app", "--host", "0.0.0.0", "--port", "8002"]

