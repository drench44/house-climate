FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ ./src/
COPY config.json .
# The running app reads VERSION at the repo root (=/app here) for /api/version
# and house_climate.__version__. Without this COPY the readout silently falls
# back to 0.0.0+unknown (test_static.py::test_dockerfile_ships_version guards it).
COPY VERSION .
ENV PYTHONPATH=/app/src CONFIG_PATH=/app/config.json
# Drop root: this is the network-exposed process (uvicorn on 0.0.0.0:8090, no
# auth). It only ever reads its code + config, so an unprivileged user suffices;
# 8090 is unprivileged. Defence-in-depth for a repo people self-host.
RUN useradd --system --uid 10001 app
USER app
CMD ["uvicorn", "house_climate.web.app:app", "--host", "0.0.0.0", "--port", "8090"]
