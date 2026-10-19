"""Rewrite saved server detector models for the ticket_analysis package.

Models saved before the package existed store their classes as server_detector.ServerModel and the
like, which no longer imports. This loads each file with that name mapped to
ticket_analysis.server_detector and saves it back, keeping the original as
<name>.before-package.joblib. The weights, thresholds and directory are unchanged.

    python -m tools.convert_model [MODEL.joblib ...]   default: data/models/server_detector.joblib
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import joblib

import ticket_analysis.server_detector as sd

OLD_MODULE = "server_detector"
BACKUP_SUFFIX = ".before-package"


def convert(path: Path) -> Path:
    """Convert one model file in place and return the backup's path."""
    backup = path.with_name(path.stem + BACKUP_SUFFIX + path.suffix)
    if backup.exists():
        raise SystemExit(f"{backup} exists, so {path} was already converted")
    previous = sys.modules.get(OLD_MODULE)
    sys.modules[OLD_MODULE] = sd
    try:
        bundle = joblib.load(path)
    finally:
        if previous is None:
            del sys.modules[OLD_MODULE]
        else:
            sys.modules[OLD_MODULE] = previous
    shutil.copy2(path, backup)
    joblib.dump(bundle, path)
    return backup


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("models", nargs="*", type=Path, default=[sd.MODEL_PATH])
    for path in p.parse_args(argv).models:
        print(f"{path}: converted; original kept as {convert(path)}")


if __name__ == "__main__":
    main()
