FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Learn the seasonal baseline at build time so the container starts in about a second.
RUN python -c "from app.simulator import bootstrap_world; bootstrap_world(seed=0, cache_dir='.cache')"
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
