FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Explicit runtime files only. .dockerignore also filters nested local data.
COPY app.py config.py web_app.py initialize_database.py ./
COPY database/ ./database/
COPY importers/ ./importers/
COPY scheduling/ ./scheduling/
COPY services/ ./services/
COPY web/ ./web/
COPY main_workflow/ ./main_workflow/
# Imported by the shared HTTP router; retains the existing /test endpoint.
COPY flow_test/ ./flow_test/
COPY demo/ ./demo/

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "from urllib.request import urlopen; urlopen('http://127.0.0.1:8765/', timeout=3)"

CMD ["python", "-c", "from web_app import run; run(host='0.0.0.0', port=8765)"]
