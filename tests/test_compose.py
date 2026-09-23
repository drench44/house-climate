"""docker-compose.yml and Dockerfile contract: log caps, memory limits, pins.

These pin the deploy-safety settings at the file level, so a later edit that
drops one (a new service without a log cap, a limit without its swap twin, an
image back on a floating tag) fails CI instead of surfacing as a full disk or
an OOM-killed database months later.
"""
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
SERVICES = COMPOSE["services"]
DOCKERFILES = sorted({*ROOT.glob("*.Dockerfile"), *ROOT.glob("Dockerfile")})

_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def _bytes(value):
    """Compose's byte notation ("256m", "1g", or a bare int) as an int."""
    if isinstance(value, int):
        return value
    m = re.fullmatch(r"(\d+)([bkmg]?)", str(value).strip().lower())
    assert m, f"unparseable byte value {value!r}"
    return int(m.group(1)) * _UNITS[m.group(2)]


def test_there_are_services_to_check():
    # A rename that nests services elsewhere must not leave every
    # parametrized test below with nothing to run.
    assert set(SERVICES) == {"db", "poller", "web"}
    # The pulled-image pin test below only sees services with `image:`.
    assert "image" in SERVICES["db"], "db no longer pulls an image; revisit the pin tests"
    assert DOCKERFILES, "no *.Dockerfile found at the repo root"


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_every_service_caps_its_json_log(name):
    logging = SERVICES[name].get("logging")
    assert logging, f"{name} has no logging block: json-file keeps logs forever"
    assert logging["driver"] == "json-file"
    assert logging["options"]["max-size"] == "10m"
    assert str(logging["options"]["max-file"]) == "5"


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_every_service_has_a_healthcheck(name):
    hc = SERVICES[name].get("healthcheck")
    assert hc and hc.get("test"), f"{name} has no healthcheck"


# web's floor is sized for 400 days of history (see its comment), not today's peak.
@pytest.mark.parametrize("name, floor", [("poller", 256 * 1024**2), ("web", 1024**3)])
def test_app_services_have_a_memory_limit_with_no_swap_on_top(name, floor):
    svc = SERVICES[name]
    assert "mem_limit" in svc, f"{name} has no mem_limit"
    limit = _bytes(svc["mem_limit"])
    # Sized at 3-4x the measured peak; a lower limit risks killing a healthy
    # service, so lowering it needs a new measurement, not just an edit.
    assert limit >= floor, f"{name} mem_limit {svc['mem_limit']} is below its measured-safe floor"
    # Without memswap_limit, Docker lets the container use as much swap again
    # as mem_limit, so the real ceiling would be twice what the file says.
    assert _bytes(svc.get("memswap_limit", -1)) == limit, f"{name} memswap_limit must equal mem_limit"


def test_database_has_no_memory_limit():
    # See the comment on db in docker-compose.yml: timescaledb-tune sizes
    # shared_buffers from the host's RAM at initdb, so a fixed cgroup limit
    # can OOM-kill Postgres mid-write. Adding one needs that retune first.
    db = SERVICES["db"]
    assert "mem_limit" not in db and "deploy" not in db


@pytest.mark.parametrize("name", sorted(n for n, s in SERVICES.items() if "image" in s))
def test_pulled_images_are_pinned_to_an_exact_version(name):
    image = SERVICES[name]["image"]
    tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""
    assert tag and tag != "latest", f"{name}: {image} floats"
    assert re.match(r"\d+\.\d+\.\d+", tag), f"{name}: {image} is not an exact x.y.z tag"


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_dockerfile_base_images_are_pinned_to_an_exact_version(dockerfile):
    froms = re.findall(r"^FROM\s+(\S+)", dockerfile.read_text(), re.M)
    assert froms, f"{dockerfile.name} has no FROM line"
    for image in froms:
        tag = image.split(":", 1)[1] if ":" in image else ""
        assert re.match(r"\d+\.\d+\.\d+-", tag), f"{dockerfile.name}: FROM {image} is not an exact x.y.z tag"
