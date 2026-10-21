"""Scheduled retraining: train both models into a new version, score them on the fixed label sets
next to the serving models, and publish the version only when it is not worse.

    python -m tools.retrain                  # train, gate, publish to data/models/versions/<version>/
    python -m tools.retrain --install        # also install a passing version into data/models/
    python -m tools.retrain --store URL      # also upload a passing version to <store>/models/<version>/
    python -m tools.retrain --dry-run        # train and gate, publish nothing

1. Train the server detector (tools.server_detector train; the Claude holdout tickets stay out of
   training) and the type model (tools.ticket_types train) into data/retrain/<version>/.
2. Score the new and the serving server detector on every span label set present: Claude's labels on
   the test file and on the training holdout, the rest of the test file, and DBA labels. Host and
   instance precision, recall and F1 per set.
3. Cross-validate the type model's training on its labels with the keyword rules on (baseline.py):
   type and intent accuracy and macro F1, and each rule's precision. The serving type model learned
   from the same labels, so scoring it directly would flatter it; the new figures are compared with the
   serving version's metrics.json instead, when there is one.
4. Gate: no F1, accuracy or macro F1 may drop more than --tolerance points (default 1.0) below the
   serving version, and every keyword rule that fires on at least 5 labeled tickets must stay at least
   90% precise.
5. Publish a passing version: both models, the taxonomy notes and metrics.json (figures, gate result,
   SHA-256 of each file) to data/models/versions/<version>/, and to the API's model store with --store.
   The API keeps serving its MODEL_VERSION; promoting the new version stays a reviewed change of that
   setting (docs/azure-deploy.md, step 8). --install also copies the files into data/models/ for local
   use, keeping the replaced ones as *.before-<version>.joblib.

The tickets and labels in data/ must be current before it runs; exporting from DW2.0 is a separate
step. Exits 0 when a version is published (or passes with --dry-run) and 1 when it is not, so a
scheduler shows a failed run. Prints counts and figures only, never ticket text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib

import ticket_analysis.server_detector as sd
import ticket_analysis.ticket_types as tt
import tools.server_labels as sl
import tools.ticket_types as ttt
from ticket_analysis.jsonl import read_jsonl
from ticket_analysis.paths import DATA_DIR
from ticket_analysis.type_rules import RULES
from tools.baseline import run_labeled

MODELS_DIR = DATA_DIR / "models"
WORK_DIR = DATA_DIR / "retrain"
SERVER_FILE, TYPES_FILE, METRICS_FILE = "server_detector.joblib", "ticket_types.joblib", "metrics.json"
NOTES_PATH = DATA_DIR / "claude_pack" / "returned" / "taxonomy_notes.md"
# (name, span labels, the ticket file they label); a set is scored when its label file exists
SPAN_SETS = [("test", DATA_DIR / "labels" / "claude_spans.jsonl", DATA_DIR / "test_tickets.jsonl"),
             ("holdout", DATA_DIR / "labels" / "claude_spans_holdout.jsonl", DATA_DIR / "train_tickets.jsonl"),
             ("test-rest", DATA_DIR / "labels" / "claude_spans_test_rest.jsonl", DATA_DIR / "test_tickets.jsonl"),
             ("dba", sl.LABELS_PATH, DATA_DIR / "test_tickets.jsonl")]
RULE_MIN_FIRES, RULE_MIN_PRECISION = 5, 0.90


def new_version(root: Path, now: datetime, store=None) -> str:
    """The UTC date, with -2, -3 and so on when that version already exists in root or in the store."""
    base, n = now.strftime("%Y-%m-%d"), 1
    while True:
        version = base if n == 1 else f"{base}-{n}"
        if not (root / version).exists() and not (store and store.list(f"models/{version}")):
            return version
        n += 1


def run(*args: str) -> None:
    """One tools command in a child process, its output passed through; raises when it fails."""
    print(f"$ python -m {' '.join(args)}", flush=True)
    subprocess.run([sys.executable, "-m", *args], check=True)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --- Scoring -----------------------------------------------------------------------


def _ratio(k: int, n: int) -> float | None:
    return k / n if n else None


def span_scores(bundle: dict, sets=SPAN_SETS) -> dict[str, dict]:
    """Host and instance precision, recall and F1 of a server detector bundle on each label set, keyed
    '<set> <class>'."""
    out = {}
    for name, labels_path, tickets_path in sets:
        labels = sl.load_labels(labels_path)
        if not labels:
            continue
        tickets = {str(t["ticket_id"]): t for t in read_jsonl(tickets_path)}
        r = sl.evaluate_labels(labels, tickets, bundle)
        for cls in ("host", "instance"):
            dets = [d for d in r["detections"] if d["cls"] == cls]
            spans = [s for s in r["spans"] if s["cls"] == cls]
            p, rec = _ratio(sum(d["correct"] for d in dets), len(dets)), _ratio(sum(s["found"] for s in spans), len(spans))
            # With spans to find, no detections is an F1 of 0, not "unknown": the gate must see it
            f1 = None if rec is None else 2 * p * rec / (p + rec) if p and rec else 0.0
            out[f"{name} {cls}"] = {"precision": p, "recall": rec, "f1": f1, "spans": len(spans), "detections": len(dets)}
    return out


def type_scores(tickets: list[dict], labels: dict[str, dict], seed: int = 0) -> dict:
    """Cross-validated type and intent accuracy and macro F1 with the keyword rules on, and each rule's
    fires and precision, as baseline.py --labels measures them."""
    out, rules = {}, []
    for target in ("primary_type", "intent"):
        r = run_labeled(target, tickets, labels, min_count=5, folds=5, max_chars=tt.MAX_CHARS, seed=seed,
                        clean=tt.MODEL_CLEAN, rules=RULES if target == "primary_type" else None)
        out[target] = {"accuracy": r["model"]["all"]["accuracy"], "macro_f1": r["model"]["all"]["macro_f1"],
                       "tickets": r["tickets"]}
        rules += [{"rule": s["rule"], "fires": s["fires"], "precision": s["precision"]} for s in r["rules"]]
    return {"targets": out, "rules": rules}


# --- Gate --------------------------------------------------------------------------


def gate(server_new: dict, server_old: dict | None, types_new: dict, types_old: dict | None,
         tolerance: float) -> list[str]:
    """Why the new version must not be published; empty when it may be. A figure counts against the
    serving version only where both have it; rules are checked on the new figures alone."""
    fails, tol = [], tolerance / 100
    for key, new in server_new.items():
        old = (server_old or {}).get(key)
        if old and old["f1"] is not None and new["f1"] is not None and new["f1"] < old["f1"] - tol:
            fails.append(f"server {key} F1 {new['f1']:.1%} is below the serving {old['f1']:.1%}")
    for target, new in types_new["targets"].items():
        old = ((types_old or {}).get("targets") or {}).get(target)
        for m in ("accuracy", "macro_f1"):
            if old and old.get(m) is not None and new[m] < old[m] - tol:
                fails.append(f"{target} {m.replace('_', ' ')} {new[m]:.1%} is below the serving {old[m]:.1%}")
    for r in types_new["rules"]:
        if r["fires"] >= RULE_MIN_FIRES and r["precision"] is not None and r["precision"] < RULE_MIN_PRECISION:
            fails.append(f"rule {r['rule']} is {r['precision']:.1%} precise on {r['fires']} tickets, "
                         f"under {RULE_MIN_PRECISION:.0%}")
    return fails


# --- Publishing --------------------------------------------------------------------


def publish(work: Path, version: str, metrics: dict, *, versions_dir: Path, install_dir: Path | None,
            store: str | None) -> Path:
    """Copy a passing version to versions_dir/<version>/ and, as asked, into install_dir and the store."""
    files = [work / SERVER_FILE, work / TYPES_FILE] + ([NOTES_PATH] if NOTES_PATH.exists() else [])
    (work / METRICS_FILE).write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    dest = versions_dir / version
    dest.mkdir(parents=True, exist_ok=True)
    for f in files + [work / METRICS_FILE]:
        shutil.copy2(f, dest / f.name)
    if install_dir:
        for name in (SERVER_FILE, TYPES_FILE):
            current = install_dir / name
            if current.exists():
                shutil.copy2(current, install_dir / name.replace(".joblib", f".before-{version}.joblib"))
            shutil.copy2(work / name, current)
        shutil.copy2(work / METRICS_FILE, install_dir / METRICS_FILE)
        print(f"Installed into {install_dir}; the replaced models are kept as *.before-{version}.joblib")
    if store:
        from api.storage import open_store
        s = open_store(store)
        for f in files + [work / METRICS_FILE]:
            s.write(f"models/{version}/{f.name}", f.read_bytes())
        print(f"Uploaded to {store} models/{version}/. To serve it, set MODEL_VERSION={version} and "
              f"MODEL_SHA256={','.join(f'{n}={h}' for n, h in metrics['sha256'].items() if n.endswith('.joblib'))}")
    return dest


# --- Command -----------------------------------------------------------------------


def _fmt(x) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--serving-dir", type=Path, default=MODELS_DIR,
                   help="folder with the serving server_detector.joblib, ticket_types.joblib and metrics.json")
    p.add_argument("--versions-dir", type=Path, default=MODELS_DIR / "versions")
    p.add_argument("--work-dir", type=Path, default=WORK_DIR)
    p.add_argument("--tolerance", type=float, default=1.0, help="points a figure may drop (default 1.0)")
    p.add_argument("--install", action="store_true", help="also install a passing version into --serving-dir")
    p.add_argument("--store", help="also upload a passing version to this model store (folder or Blob URL)")
    p.add_argument("--dry-run", action="store_true", help="train and gate, publish nothing")
    p.add_argument("--seed", type=int, default=0)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    now = datetime.now(timezone.utc)
    store = None
    if args.store:
        from api.storage import open_store
        store = open_store(args.store)
    version = new_version(args.versions_dir, now, store)
    work = args.work_dir / version
    work.mkdir(parents=True, exist_ok=True)
    print(f"Retraining version {version} in {work}", flush=True)
    try:
        run("tools.server_detector", "train", "--model-path", str(work / SERVER_FILE))
        run("tools.ticket_types", "train", "--model-path", str(work / TYPES_FILE))
    except subprocess.CalledProcessError as e:
        print(f"Not published: training failed (exit code {e.returncode})")
        return 1

    serving_server = args.serving_dir / SERVER_FILE
    server_new = span_scores(sd.load_model(work / SERVER_FILE))
    server_old = span_scores(sd.load_model(serving_server)) if serving_server.exists() else None
    tickets = read_jsonl(DATA_DIR / "train_tickets.jsonl") + read_jsonl(DATA_DIR / "test_tickets.jsonl")
    types_new = type_scores(tickets, ttt.current_labels(), args.seed)
    old_metrics = args.serving_dir / METRICS_FILE
    types_old = json.loads(old_metrics.read_text(encoding="utf-8")).get("types") if old_metrics.exists() else None

    for key, new in server_new.items():
        old = (server_old or {}).get(key) or {}
        print(f"  server {key:18} F1 {_fmt(new['f1']):>6} (serving {_fmt(old.get('f1')):>6})  "
              f"precision {_fmt(new['precision'])}  recall {_fmt(new['recall'])}  {new['spans']} spans")
    for target, new in types_new["targets"].items():
        old = ((types_old or {}).get("targets") or {}).get(target) or {}
        print(f"  {target:12} accuracy {_fmt(new['accuracy'])} (serving {_fmt(old.get('accuracy'))})  "
              f"macro F1 {_fmt(new['macro_f1'])} (serving {_fmt(old.get('macro_f1'))})  {new['tickets']} tickets")
    for r in types_new["rules"]:
        print(f"  rule {r['rule']:20} fires {r['fires']:4}  precision {_fmt(r['precision'])}")

    fails = gate(server_new, server_old, types_new, types_old, args.tolerance)
    metrics = {"version": version, "created_at": now.isoformat(timespec="seconds"), "tolerance": args.tolerance,
               "server": server_new, "server_serving": server_old, "types": types_new,
               "gate": {"passed": not fails, "failures": fails},
               "trained_on": {"server": joblib.load(work / SERVER_FILE).get("trained_on"),
                              "type_labels": [str(p) for p in ttt.LABEL_PATHS if Path(p).exists()]},
               "sha256": {f.name: sha256(f) for f in (work / SERVER_FILE, work / TYPES_FILE)}}
    (work / METRICS_FILE).write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    if fails:
        print("Not published:\n" + "\n".join(f"  - {f}" for f in fails))
        return 1
    if args.dry_run:
        print(f"Passed; not published (dry run). Files in {work}")
        return 0
    dest = publish(work, version, metrics, versions_dir=args.versions_dir,
                   install_dir=args.serving_dir if args.install else None, store=args.store)
    print(f"Published version {version} to {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
