import os
from .config import load_config, load_secrets
from .poller import run

if __name__ == "__main__":
    cfg = load_config(os.environ.get("CONFIG_PATH", "config.json"))
    run(cfg, load_secrets(os.environ))
