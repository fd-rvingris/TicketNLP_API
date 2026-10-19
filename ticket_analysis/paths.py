"""Repository locations. The package sits one level below the repository root."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
INSTANCES_PATH = DATA_DIR / "instances.csv"
