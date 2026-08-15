FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
COPY config.json .
ENV PYTHONPATH=/app/src CONFIG_PATH=/app/config.json
# Drop root: the poller only reads its code + config and makes outbound calls.
RUN useradd --system --uid 10001 app
USER app
CMD ["python", "-m", "house_climate"]
