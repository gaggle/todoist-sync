from importlib.metadata import version
from pathlib import Path

# A repo checkout (staging, tests) reads VERSION directly, so a bump shows
# up without reinstalling. An installed snapshot (prod) has no repo around
# it and falls back to its package metadata, stamped at build time.
_VERSION_FILE = Path(__file__).resolve().parent.parent.parent / "VERSION"
__version__ = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else version("todoist-sync")
