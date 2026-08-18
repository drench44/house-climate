"""house-climate — an open-core home-climate dashboard (FastAPI + TimescaleDB
+ vanilla JS)."""
from .version import read_version

# The running app's version, read once from the repo-root VERSION file. The
# release script (scripts/release.py) is the only writer; everything that shows
# a version (the API, the footer readout, the asset cache-bust) derives from it.
__version__ = read_version()
