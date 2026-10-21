"""Build a known-names directory snapshot from the CMDB and the model's alert names, validate it and
publish it to DIRECTORY_STORE, keeping the newest DIRECTORY_KEEP. In Azure this runs as a scheduled
Container Apps job (daily); the API picks the new snapshot up within DIRECTORY_REFRESH_MINUTES.

    python -m api.refresh_directory              settings from the environment
    python -m api.refresh_directory --local      the local profile: data/instances.csv into data/directory
    python -m api.refresh_directory --dry-run    build and validate, publish nothing

Prints counts only, never names. Exits 1 when the snapshot is not published (empty, or fewer than
DIRECTORY_MIN_SHARE of the previous snapshot's records), so the job run shows as failed.
"""

from __future__ import annotations

import argparse
import logging
import os

from .directory import SnapshotError, SnapshotStore, build_snapshot, cmdb_source, validate
from .models import ModelError, fetch_file, load_server_bundle, version_of
from .settings import ConfigError, Settings, load_settings, local_profile
from .storage import LocalStore, StoreError, open_store

log = logging.getLogger("api.refresh")


def refresh(settings: Settings, *, local: bool = False, dry_run: bool = False) -> str | None:
    """Build, validate and (unless dry_run) publish one snapshot. Returns the published name."""
    if not settings.directory_store:
        raise ConfigError("DIRECTORY_STORE is not set")
    store = SnapshotStore(open_store(settings.directory_store))
    bundle = load_server_bundle(fetch_file(settings, settings.server_model_file))
    model_version = version_of(bundle.get("created_at"))
    snapshot = build_snapshot(cmdb_source(settings, local=local), bundle["directory"],
                              include_dw=settings.directory_include_dw, model_version=model_version)
    c, src = snapshot["counts"], snapshot["sources"]
    log.info("Snapshot %s: %d clients, %d hosts, %d instances (CMDB %s: %d records, %d skipped; alert names from "
             "model %s: %d; dw names %s)", snapshot["createdAt"], c["clients"], c["hosts"], c["instances"],
             src["cmdb"]["kind"], src["cmdb"]["records"], src["cmdb"]["skipped"], model_version, src["alert"]["records"],
             "included" if src["dw"]["included"] else "left out")
    if dry_run:
        latest = store.newest_valid()
        problems = validate(snapshot, latest[1] if latest else None, settings.directory_min_share)
        log.info("Dry run: %s", "would publish" if not problems else "would not publish: " + "; ".join(problems))
        return None
    name = store.publish(snapshot, keep=settings.directory_keep, min_share=settings.directory_min_share)
    log.info("Published %s to %s", name, store.location)
    return name


def bootstrap_local_snapshot(settings: Settings) -> None:
    """On a local run's first start, build a snapshot when the local directory store has none."""
    if not settings.directory_store or settings.directory_store.startswith("https://"):
        return
    store = SnapshotStore(LocalStore(settings.directory_store))
    if store.names():
        return
    log.info("No directory snapshot in %s yet; building one from the CMDB export and the model's alert names",
             store.location)
    try:
        refresh(settings, local=True)
    except (SnapshotError, StoreError, ModelError) as e:
        log.warning("No snapshot built (%s); the API serves the model's own directory", e)


def main(argv=None) -> None:
    from . import logs
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--local", action="store_true", help="use the local profile (data/ paths, CMDB_SOURCE=csv)")
    p.add_argument("--dry-run", action="store_true", help="build and validate only")
    args = p.parse_args(argv)
    if args.local:
        os.environ.update(local_profile())
    try:
        settings = load_settings()
    except ConfigError as e:
        raise SystemExit(str(e)) from None
    logs.configure(settings.log_level, settings.log_format)
    try:
        refresh(settings, local=args.local, dry_run=args.dry_run)
    except (SnapshotError, StoreError, ModelError, ConfigError, NotImplementedError) as e:
        log.error("Directory refresh failed: %s", e)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
