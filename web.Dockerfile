FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
COPY config.json .
ENV PYTHONPATH=/app/src CONFIG_PATH=/app/config.json
CMD ["uvicorn", "house_climate.web.app:app", "--host", "0.0.0.0", "--port", "8090"]
