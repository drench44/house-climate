# Exact tag, bumped on purpose: python:3.12-slim moves with every Python
# patch and Debian point release, so two builds of one commit could differ.
FROM python:3.12.14-slim-trixie
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
COPY config.json .
# house_climate/__init__ reads VERSION at the repo root (=/app) into
# __version__ at import; ship it so that read doesn't log the "VERSION
# unreadable — using 0.0.0+unknown" warning on every poller start.
COPY VERSION .
ENV PYTHONPATH=/app/src CONFIG_PATH=/app/config.json
# Drop root: the poller only reads its code + config and makes outbound calls.
RUN useradd --system --uid 10001 app
USER app
CMD ["python", "-m", "house_climate"]
