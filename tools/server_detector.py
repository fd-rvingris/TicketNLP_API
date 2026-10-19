"""Train, evaluate and run the server and instance detector (ticket_analysis.server_detector).

    python -m tools.server_detector train      fit and save data/models/server_detector.joblib
    python -m tools.server_detector evaluate   all tests, Markdown report under data/reports/
    python -m tools.server_detector detect     detections for a ticket file, written to a new .jsonl

Prints counts and rates only, never ticket text. Nothing leaves this machine; the CMDB API is a stub.
"""

from __future__ import annotations

import argparse
import re
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
from sklearn.metrics import average_precision_score, precision_recall_curve

from ticket_analysis.jsonl import now_iso, read_jsonl, write_jsonl
from ticket_analysis.paths import DATA_DIR, INSTANCES_PATH
from ticket_analysis.server_detector import (BUILTIN_STOP, DEFAULT_INSTANCE, DEFAULT_MAX_CHARS, DEFAULT_THRESHOLD,
                                             MODEL_PATH, RULE_SCORE, Knowledge, LocalServerDirectory, ServerDirectory,
                                             ServerModel, add_features, build_stop_words, compress, detect_servers,
                                             is_alert_ticket, load_model, normalize_reference, ticket_candidates,
                                             ticket_names, ticket_text)
from tools.common import REPORTS_DIR, fmt_pct, md_table

# Claude-labeled training tickets kept out of training and never tuned on: the unseen-name test set.
HOLDOUT_LABELS_PATH = DATA_DIR / "labels" / "claude_spans_holdout.jsonl"
CLASSES = ("host", "instance", "none")
DEFAULT_NEG_RATIO = 10  # at most this many "none" training examples per positive
# Share of training tickets whose features are computed as if their client were unknown. Nearly every
# training ticket has a client with known names, so without this the model leans on "resembles the
# client's names" and scores every candidate as none when the client is unknown or a name is new.
DEFAULT_CLIENT_DROPOUT = 0.3
ALERT_CONTEXT_MODES = ("strip_labels", "token_only")
MODEL_KINDS = ("logreg", "hgb")


# --- Labels --------------------------------------------------------------------


def gold_names(ticket: dict, label_dir: ServerDirectory) -> tuple[set[str], set[str]]:
    """The hosts and bare instance names a ticket's candidates are labeled against. Alerts: the parsed
    server and the host part of a server-shaped instance; no bare instances. Human tickets: hosts and
    instance parts of the ticket's own dw_servers, plus the hosts and instance names the directory
    knows for the ticket's client."""
    if is_alert_ticket(ticket):
        return {p.host for p in ticket_names({"alert_fields": ticket.get("alert_fields")})}, set()
    own = [p for p in (normalize_reference(v) for v in ticket.get("dw_servers") or []) if p]
    client = label_dir.client_for(ticket)
    hosts = {p.host for p in own} | (set(label_dir.hosts(client)) if client else set())
    instances = {p.instance for p in own if p.instance} | ({i for _, i in label_dir.instances(client)} if client else set())
    return hosts, instances


def label_of(c: dict, hosts: set[str], instances: set[str]) -> str:
    if c["host_ok"] and c["host"] in hosts:
        return "host"
    if c["inst_ok"] and c["kind"] == "token" and c["host"] in instances:
        return "instance"
    return "none"


def synthetic_instance(c: dict) -> dict | None:
    """A bare instance example from a HOST\\INSTANCE mention, with the host removed: the instance part
    as its own token, in the same context, with no backslash cue."""
    if c["kind"] != "host_instance" or not c["instance"] or c["instance"] == DEFAULT_INSTANCE:
        return None
    cut = c["text"].index("\\") + 1
    return {**c, "text": c["text"][cut:], "start": c["start"] + cut, "kind": "token", "host": c["instance"],
            "instance": None, "name": c["instance"], "host_ok": False, "inst_ok": True, "has_port": False,
            "in_unc": False, "after_label": False, "cue": c["cue"], "label": "instance", "source": "synthetic"}


def make_examples(tickets: list[dict], know: Knowledge, label_dir: ServerDirectory, *, alert_context: str,
                  max_chars: int = DEFAULT_MAX_CHARS, synthetic: bool = True, exclude_hosts=frozenset(),
                  exclude_instances=frozenset(), neg_ratio: float | None = DEFAULT_NEG_RATIO,
                  client_dropout: float = DEFAULT_CLIENT_DROPOUT, seed: int = 0) -> tuple[list[dict], dict]:
    """Labeled training candidates with features. Candidates naming an excluded host or instance are
    dropped entirely. 'none' examples are sampled down to neg_ratio per positive (None keeps all).
    For a client_dropout share of tickets the features ignore the client (labels do not).
    Returns the examples and counts per label and source."""
    rng = np.random.default_rng(seed)
    pos, neg = [], []
    hidden = 0
    for t in tickets:
        hosts, instances = gold_names(t, label_dir)
        source = "alert" if is_alert_ticket(t) else "human"
        cands = ticket_candidates(t, know, alert_context=alert_context, max_chars=max_chars)
        kept = []
        for c in cands:
            if c["host"] in exclude_hosts or c["host"] in exclude_instances or c["instance"] in exclude_instances:
                continue
            c["label"], c["source"] = label_of(c, hosts, instances), source
            kept.append(c)
            if synthetic and c["label"] == "host":
                s = synthetic_instance(c)
                if s:
                    kept.append(s)
        hide = bool(rng.random() < client_dropout)
        hidden += hide
        add_features(kept, know, t, hide_client=hide)
        for c in kept:
            (neg if c["label"] == "none" else pos).append(c)
    counts = Counter((c["label"], c["source"]) for c in pos + neg)
    if neg_ratio is not None and len(neg) > neg_ratio * max(len(pos), 1):
        pick = rng.choice(len(neg), size=int(neg_ratio * max(len(pos), 1)), replace=False)
        neg = [neg[i] for i in sorted(pick)]
    return pos + neg, {"before_sampling": counts, "none_kept": len(neg), "client_hidden_tickets": hidden}


# --- Training and selection ----------------------------------------------------


def split_validation(tickets: list[dict], share: float, seed: int) -> tuple[list[dict], list[dict]]:
    idx = np.random.default_rng(seed).permutation(len(tickets))
    n_val = int(round(share * len(tickets)))
    val = set(idx[:n_val].tolist())
    return [t for i, t in enumerate(tickets) if i not in val], [t for i, t in enumerate(tickets) if i in val]


def holdout_names(names, share: float, seed: int) -> frozenset:
    """A seeded random share of the given names, for a grouped holdout."""
    names = sorted(set(names))
    k = int(round(share * len(names)))
    if not k:
        return frozenset()
    pick = np.random.default_rng(seed).choice(len(names), size=k, replace=False)
    return frozenset(names[i] for i in pick)


def score_tickets(model: ServerModel, tickets: list[dict], directory: ServerDirectory, label_dir: ServerDirectory,
                  batch: int = 200) -> tuple[list[dict], list[dict]]:
    """Score every candidate in the tickets (no sampling) against labels from label_dir. Returns compact
    candidate records and gold-name occurrence records (for candidate recall); no ticket text."""
    know = model.knowledge(directory)
    records, occurrences = [], []
    for i in range(0, len(tickets), batch):
        chunk, cands_all = tickets[i:i + batch], []
        for ti, t in enumerate(chunk, start=i):
            hosts, instances = gold_names(t, label_dir)
            seg = "alerts" if is_alert_ticket(t) else "human"
            cands = ticket_candidates(t, know, alert_context=model.alert_context, max_chars=model.max_chars)
            add_features(cands, know, t)
            for c in cands:
                c["label"], c["seg"], c["ticket"] = label_of(c, hosts, instances), seg, ti
            cands_all += cands
            occurrences += _occurrences(t, cands, hosts, instances, seg, ti, model.max_chars)
        scores = model.scores(cands_all)
        for c, (ph, pi) in zip(cands_all, scores):
            records.append({"label": c["label"], "seg": c["seg"], "ticket": c["ticket"], "ph": float(ph),
                            "pi": float(pi), "kind": c["kind"], "host": c["host"], "instance": c["instance"],
                            "pattern": compress(c["host"]), "start": c["start"], "end": c["end"],
                            "rule": c.get("rule")})
    return records, occurrences


def _occurrences(ticket, cands, hosts, instances, seg, ti, max_chars) -> list[dict]:
    """Each place a gold host or bare instance name occurs in the text, and whether a candidate of the
    right kind covers it. 'own' marks names from the ticket's own dw_servers or alert fields, as
    opposed to names the directory knows for the client, which include email domains, short names and
    words that are not server mentions in that text."""
    text = ticket_text(ticket, max_chars)[0].lower()
    own = ticket_names(ticket)
    own_names = {"host": {p.host for p in own}, "instance": {p.instance for p in own if p.instance}}
    out = []
    for cls, names in (("host", hosts), ("instance", instances)):
        for name in names:
            behind = r"(?<![a-z0-9_\-.\\])" if cls == "instance" else r"(?<![a-z0-9_\-.])"
            for m in re.finditer(behind + re.escape(name) + r"(?![a-z0-9_\-])", text):
                if cls == "instance" and text[m.end():m.end() + 1] == "\\":
                    continue
                if cls == "host":
                    covered = any(c["host_ok"] and c["host"] == name and c["start"] <= m.start() < c["end"]
                                  for c in cands)
                else:
                    covered = any(c["inst_ok"] and c["kind"] == "token" and c["host"] == name
                                  and c["start"] <= m.start() < c["end"] for c in cands)
                out.append({"cls": cls, "name": name, "seg": seg, "ticket": ti, "covered": covered,
                            "own": name in own_names[cls]})
    return out


def average_precision(records: list[dict], cls: str) -> float | None:
    y = np.array([r["label"] == cls for r in records])
    if not y.any():
        return None
    return float(average_precision_score(y, [r["ph" if cls == "host" else "pi"] for r in records]))


def best_f1_threshold(records: list[dict], cls: str) -> float:
    """The model's threshold with the best F1, over candidates no rule marked. Rule matches score
    RULE_SCORE whatever the threshold, so including them would pull the threshold up to that score
    and leave the model nothing to decide."""
    records = [r for r in records if not r.get("rule")]
    y = np.array([r["label"] == cls for r in records])
    if not y.any():
        return DEFAULT_THRESHOLD
    p, r, t = precision_recall_curve(y, [x["ph" if cls == "host" else "pi"] for x in records])
    f1 = 2 * p[:-1] * r[:-1] / np.maximum(p[:-1] + r[:-1], 1e-12)
    return float(np.clip(t[int(np.argmax(f1))], 0.05, RULE_SCORE - 0.05))


def fit_config(train: list[dict], directory: ServerDirectory, *, kind: str, alert_context: str,
               instance_model: bool, seed: int, max_chars: int, neg_ratio: float,
               exclude_hosts=frozenset(), exclude_instances=frozenset(),
               linear_params: dict | None = None) -> tuple[ServerModel, dict]:
    stop = build_stop_words(train, directory)
    model = ServerModel(kind=kind, alert_context=alert_context, instance_model=instance_model, stop=stop,
                        max_chars=max_chars, seed=seed, linear_params=linear_params)
    examples, counts = make_examples(train, model.knowledge(directory), directory, alert_context=alert_context,
                                     max_chars=max_chars, exclude_hosts=exclude_hosts,
                                     exclude_instances=exclude_instances, neg_ratio=neg_ratio, seed=seed)
    model.fit(examples)
    counts["examples"] = len(examples)
    return model, counts


def select_config(train: list[dict], *, seed: int, max_chars: int, neg_ratio: float, val_share: float = 0.2,
                  kinds=MODEL_KINDS, modes=ALERT_CONTEXT_MODES, instances_path: Path | None = INSTANCES_PATH,
                  log=print) -> dict:
    """Fit each model kind and alert-context mode on 80% of the training tickets, score the other 20%,
    and keep the best macro average precision over host and instance. Then try a separate binary
    instance model, and pick thresholds by best F1 on the validation split."""
    fit_t, val_t = split_validation(train, val_share, seed)
    fit_dir = LocalServerDirectory.from_sources(fit_t, instances_path)
    oracle = LocalServerDirectory.from_sources(train, instances_path)
    trials = []
    for mode in modes:
        for kind in kinds:
            start = time.time()
            model, counts = fit_config(fit_t, fit_dir, kind=kind, alert_context=mode, instance_model=False,
                                       seed=seed, max_chars=max_chars, neg_ratio=neg_ratio)
            recs, _ = score_tickets(model, val_t, fit_dir, oracle)
            ap_h, ap_i = average_precision(recs, "host"), average_precision(recs, "instance")
            macro = np.mean([x for x in (ap_h, ap_i) if x is not None]) if (ap_h or ap_i) else 0.0
            trials.append({"kind": kind, "alert_context": mode, "instance_model": False, "ap_host": ap_h,
                           "ap_instance": ap_i, "macro_ap": float(macro), "model": model, "records": recs,
                           "examples": counts["examples"], "seconds": time.time() - start})
            log(f"  {kind:7} {mode:13} AP host {fmt_pct(ap_h)}  AP instance {fmt_pct(ap_i)}  "
                f"({counts['examples']:,} examples, {time.time() - start:.0f}s)")
    best = max(trials, key=lambda t: t["macro_ap"])
    start = time.time()
    model, counts = fit_config(fit_t, fit_dir, kind=best["kind"], alert_context=best["alert_context"],
                               instance_model=True, seed=seed, max_chars=max_chars, neg_ratio=neg_ratio)
    recs, _ = score_tickets(model, val_t, fit_dir, oracle)
    ap_i = average_precision(recs, "instance")
    inst_trial = {"kind": best["kind"], "alert_context": best["alert_context"], "instance_model": True,
                  "ap_host": average_precision(recs, "host"), "ap_instance": ap_i, "records": recs,
                  "examples": counts["examples"], "seconds": time.time() - start}
    inst_trial["macro_ap"] = float(np.mean([x for x in (inst_trial["ap_host"], ap_i) if x is not None] or [0]))
    trials.append(inst_trial)
    log(f"  {best['kind']:7} + binary instance model: AP instance {fmt_pct(ap_i)}")
    chosen = inst_trial if (ap_i or 0) > (best["ap_instance"] or 0) else best
    return {"kind": chosen["kind"], "alert_context": chosen["alert_context"],
            "instance_model": chosen["instance_model"],
            "host_threshold": best_f1_threshold(chosen["records"], "host"),
            "instance_threshold": best_f1_threshold(chosen["records"], "instance"),
            "trials": [{k: v for k, v in t.items() if k not in ("model", "records")} for t in trials],
            "validation_tickets": len(val_t), "fit_tickets": len(fit_t)}


# --- Evaluation ----------------------------------------------------------------

SEGMENTS = ("all", "alerts", "human")
REPORT_THRESHOLDS = (0.3, 0.5, 0.7, 0.9)


def _seg(records: list[dict], seg: str) -> list[dict]:
    return records if seg == "all" else [r for r in records if r["seg"] == seg]


def prf(records: list[dict], cls: str, threshold: float) -> dict:
    key = "ph" if cls == "host" else "pi"
    tp = sum(1 for r in records if r[key] >= threshold and r["label"] == cls)
    pred = sum(1 for r in records if r[key] >= threshold)
    actual = sum(1 for r in records if r["label"] == cls)
    p = tp / pred if pred else None
    rc = tp / actual if actual else None
    f = 2 * p * rc / (p + rc) if p and rc else None
    return {"precision": p, "recall": rc, "f1": f, "actual": actual, "predicted": pred}


def recall_of(occ: list[dict], cls: str, seg: str = "all", names=None, own_only: bool = False) -> tuple[int, int]:
    rows = [o for o in _seg(occ, seg) if o["cls"] == cls and (names is None or o["name"] in names)
            and (o["own"] or not own_only)]
    return sum(o["covered"] for o in rows), len(rows)


def false_positive_shapes(records: list[dict], cls: str, threshold: float, stop: frozenset, n: int = 12):
    key = "ph" if cls == "host" else "pi"
    fps = [r for r in records if r[key] >= threshold and r["label"] != cls]
    shapes = Counter(r["pattern"] for r in fps).most_common(n)
    english = sum(1 for r in fps if r["host"] in stop or r["host"] in BUILTIN_STOP)
    return shapes, len(fps), english


def held_out_recall(records: list[dict], occ: list[dict], *, hosts=None, instances=None, model: ServerModel,
                    shared=None) -> dict:
    """Recall on mentions of held-out names: host candidates whose host is held out, or bare and
    HOST\\INSTANCE candidates whose instance is held out (optionally only names in `shared`)."""
    th_h, th_i = model.host_threshold, model.instance_threshold
    out = {}
    for seg in SEGMENTS:
        rs = _seg(records, seg)
        row = {}
        if hosts is not None:
            gold = [r for r in rs if r["label"] == "host" and r["host"] in hosts]
            row["host"] = (sum(r["ph"] >= th_h for r in gold), len(gold))
            row["host_candidates"] = recall_of(occ, "host", seg, hosts)
        if instances is not None:
            bare = [r for r in rs if r["label"] == "instance" and r["host"] in instances and (shared is None or r["host"] in shared)]
            pair = [r for r in rs if r["label"] == "host" and r["instance"] in instances
                    and (shared is None or r["instance"] in shared)]
            row["bare"] = (sum(r["pi"] >= th_i for r in bare), len(bare))
            row["host_instance"] = (sum(r["ph"] >= th_h for r in pair), len(pair))
            names = instances if shared is None else instances & shared
            row["bare_candidates"] = recall_of(occ, "instance", seg, names)
        out[seg] = row
    return out


def pairing_accuracy(model: ServerModel, tickets: list[dict], directory: ServerDirectory) -> Counter:
    """For bare instances whose true host is known from the same ticket's dw_servers: paired with the
    correct host, ambiguous, unpaired, paired with another host, or not detected as a bare instance."""
    counts = Counter()
    know = model.knowledge(directory)
    for t in tickets:
        truth = defaultdict(set)
        for p in (normalize_reference(v) for v in t.get("dw_servers") or []):
            if p and p.instance:
                truth[p.instance].add(p.host)
        if not truth:
            continue
        cands = ticket_candidates(t, know, alert_context=model.alert_context, max_chars=model.max_chars)
        gold = [c for c in cands if c["inst_ok"] and c["kind"] == "token" and c["host"] in truth]
        if not gold:
            continue
        subject_len = len(t.get("subject") or "")
        dets = {}
        for d in detect_servers(t, directory, model):
            if d["kind"] == "bare_instance":
                start = d["start"] + (0 if d["field"] == "subject" else subject_len + 1)
                dets[start] = d
        for c in gold:
            d = dets.get(c["start"])
            if d is None:
                counts["not detected"] += 1
            elif d["host"] in truth[c["host"]]:
                counts["correct host"] += 1
            elif d["status"] == "ambiguous":
                counts["ambiguous"] += 1
            elif d["host"] is None:
                counts["unpaired"] += 1
            else:
                counts["wrong host"] += 1
    return counts


def evaluate(train: list[dict], test: list[dict], *, seed: int, holdout_share: float, max_chars: int,
             neg_ratio: float, host_threshold: float | None, instance_threshold: float | None,
             instances_path: Path | None = INSTANCES_PATH, log=print) -> dict:
    overlap = {t["ticket_id"] for t in train} & {t["ticket_id"] for t in test}
    if overlap:
        raise SystemExit(f"{len(overlap)} tickets are in both the training and the test file")
    result = {"train_tickets": len(train), "test_tickets": len(test), "seed": seed, "holdout_share": holdout_share,
              "max_chars": max_chars, "neg_ratio": neg_ratio}
    train_dir = LocalServerDirectory.from_sources(train, instances_path)
    oracle = LocalServerDirectory.from_sources(train + test, instances_path)
    result["directory"] = train_dir.stats()
    result["oracle_directory"] = oracle.stats()
    log("Directory (training): " + ", ".join(f"{k} {v:,}" for k, v in result["directory"].items()))

    log("Selecting the model on a validation split of the training file ...")
    cfg = select_config(train, seed=seed, max_chars=max_chars, neg_ratio=neg_ratio, instances_path=instances_path,
                        log=log)
    if host_threshold is not None:
        cfg["host_threshold"] = host_threshold
    if instance_threshold is not None:
        cfg["instance_threshold"] = instance_threshold
    result["config"] = cfg
    common = dict(kind=cfg["kind"], alert_context=cfg["alert_context"], instance_model=cfg["instance_model"],
                  seed=seed, max_chars=max_chars, neg_ratio=neg_ratio)

    def finish(model):
        model.host_threshold, model.instance_threshold = cfg["host_threshold"], cfg["instance_threshold"]
        return model

    log("Standard test: train on the training file, score the test file ...")
    model, counts = fit_config(train, train_dir, **common)
    finish(model)
    result["training_counts"] = counts
    recs, occ = score_tickets(model, test, train_dir, oracle)
    result["standard"] = {"records": recs, "occurrences": occ}
    result["stop_words"] = len(model.stop)
    result["pairing"] = pairing_accuracy(model, test, train_dir)
    result["natural_bare_instances"] = {
        "train_human": counts["before_sampling"].get(("instance", "human"), 0),
        "test_human": sum(1 for r in recs if r["label"] == "instance" and r["seg"] == "human")}
    result["fp"] = {cls: false_positive_shapes(recs, cls, cfg[f"{cls}_threshold"], model.stop)
                    for cls in ("host", "instance")}

    log("Unseen-host test ...")
    held_hosts = holdout_names(oracle.hosts(), holdout_share, seed)
    dir_h = train_dir.without(hosts=held_hosts)
    model_h, _ = fit_config(train, dir_h, exclude_hosts=held_hosts, **common)
    finish(model_h)
    recs_h, occ_h = score_tickets(model_h, test, dir_h, oracle)
    result["unseen_host"] = {"held_out": len(held_hosts),
                             "recall": held_out_recall(recs_h, occ_h, hosts=held_hosts, model=model_h)}

    log("Unseen-instance test ...")
    held_inst = holdout_names(oracle.instance_names(), holdout_share, seed)
    shared = frozenset(n for n, k in oracle.instance_clients().items() if k > 1)
    unique = frozenset(oracle.instance_names()) - shared
    dir_i = train_dir.without(instances=held_inst)
    model_i, _ = fit_config(train, dir_i, exclude_instances=held_inst, **common)
    finish(model_i)
    recs_i, occ_i = score_tickets(model_i, test, dir_i, oracle)
    result["unseen_instance"] = {
        "held_out": len(held_inst), "shared": len(held_inst & shared), "unique": len(held_inst & unique),
        "all": held_out_recall(recs_i, occ_i, instances=held_inst, model=model_i),
        "shared_names": held_out_recall(recs_i, occ_i, instances=held_inst, shared=shared, model=model_i),
        "unique_names": held_out_recall(recs_i, occ_i, instances=held_inst, shared=unique, model=model_i)}
    return result


# --- Report --------------------------------------------------------------------


def _frac(pair: tuple[int, int]) -> str:
    k, n = pair
    return f"{fmt_pct(k / n) if n else 'n/a'} ({k:,}/{n:,})"


def _num(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


def render_report(r: dict, args) -> str:
    cfg = r["config"]
    recs, occ = r["standard"]["records"], r["standard"]["occurrences"]
    d = r["directory"]
    lines = [
        "# Server and instance detector",
        "",
        f"Generated {now_iso()}. Training file {args.train.name} ({r['train_tickets']:,} tickets), test file "
        f"{args.test.name} ({r['test_tickets']:,} tickets). Seed {r['seed']}, holdout share "
        f"{r['holdout_share']:.0%}, first {r['max_chars']:,} description characters per ticket.",
        "",
        "## Known-names directory",
        "",
        md_table(["measure", "training directory", "training + test (label oracle)"],
                 [[k.replace("_", " "), f"{d[k]:,}", f"{r['oracle_directory'][k]:,}"] for k in d]),
        "",
        "The detector's features read only the training directory. Labels for the test file use the oracle, "
        "which adds the test tickets' own dw_servers and alert fields.",
        "",
        "## Model selection (validation split of the training file)",
        "",
        f"{cfg['fit_tickets']:,} training tickets fit each option and {cfg['validation_tickets']:,} score it. The "
        "option with the best macro average precision over host and instance is kept; a separate binary "
        "instance model replaces the multiclass instance score only when its instance AP is higher.",
        "",
        md_table(["model", "alert context", "binary instance model", "AP host", "AP instance", "macro AP",
                  "examples", "seconds"],
                 [[t["kind"], t["alert_context"], "yes" if t["instance_model"] else "no", fmt_pct(t["ap_host"]),
                   fmt_pct(t["ap_instance"]), fmt_pct(t["macro_ap"]), f"{t['examples']:,}", f"{t['seconds']:.0f}"]
                  for t in cfg["trials"]]),
        "",
        f"Chosen: {cfg['kind']}, alert context {cfg['alert_context']}, binary instance model "
        f"{'yes' if cfg['instance_model'] else 'no'}. Thresholds: host {cfg['host_threshold']:.2f}, instance "
        f"{cfg['instance_threshold']:.2f} (best F1 on the validation split unless set on the command line).",
        "",
        "## Training examples",
        "",
    ]
    before = r["training_counts"]["before_sampling"]
    lines += [md_table(["label", "source", "candidates"],
                       [[lab, src, f"{n:,}"] for (lab, src), n in sorted(before.items())]), "",
              f"'none' candidates were sampled down to {r['neg_ratio']:g} per positive "
              f"({r['training_counts']['none_kept']:,} kept); no class weights. Synthetic bare instances "
              f"(instance part of a HOST\\INSTANCE mention, host removed) added: "
              f"{before.get(('instance', 'synthetic'), 0):,}; they are used in training only. Client features were "
              f"hidden for {r['training_counts']['client_hidden_tickets']:,} training tickets (client dropout "
              f"{DEFAULT_CLIENT_DROPOUT:.0%}). Stop list: "
              f"{r['stop_words']:,} words.", "",
              "## Candidate generation recall (test file)", "",
              "Share of places a labeled host or bare instance name occurs in the text that became a candidate "
              "of the right kind. 'Own names' are the ticket's own dw_servers and alert fields. 'With directory "
              "names' adds every name the directory knows for the client, which includes email domains, "
              "two-character names and ordinary words that are not server mentions where they occur, so it "
              "understates recall.", "",
              md_table(["class", "names"] + list(SEGMENTS),
                       [[cls, label] + [_frac(recall_of(occ, cls, seg, own_only=own)) for seg in SEGMENTS]
                        for cls in ("host", "instance") for label, own in (("own names", True),
                                                                           ("with directory names", False))]),
              "", "## Standard test", ""]
    for cls in ("host", "instance"):
        th = cfg[f"{cls}_threshold"]
        rows = []
        for seg in SEGMENTS:
            rs = _seg(recs, seg)
            ap = average_precision(rs, cls)
            for t in sorted(set(REPORT_THRESHOLDS) | {round(th, 2)}):
                m = prf(rs, cls, t)
                rows.append([seg, f"{t:.2f}" + (" (chosen)" if abs(t - th) < 1e-9 or t == round(th, 2) else ""),
                             fmt_pct(m["precision"]), fmt_pct(m["recall"]), fmt_pct(m["f1"]), f"{m['actual']:,}",
                             f"{m['predicted']:,}", fmt_pct(ap)])
        lines += [f"### {cls}", "", md_table(["segment", "threshold", "precision", "recall", "F1", "labeled",
                                               "predicted", "AP"], rows), ""]
    rule_rows = []
    for rule, n in Counter(r["rule"] for r in recs if r.get("rule")).most_common():
        cls = rule.split(":")[0]
        fired = [r for r in recs if r.get("rule") == rule]
        agree = sum(1 for r in fired if r["label"] == cls)
        rule_rows.append([rule, f"{n:,}", f"{agree:,}", fmt_pct(agree / n),
                          f"{sum(1 for r in fired if r['seg'] == 'alerts'):,}", f"{sum(1 for r in fired if r['seg'] == 'human'):,}"])
    lines += ["### Rules", "",
              "Candidates a rule marked (scored at least " f"{RULE_SCORE:.2f}" ", so detected at the chosen thresholds). "
              "'Agree' counts those whose derived label is the rule's class. Known-name rules use the training "
              "directory, which is also where human-ticket labels come from, so their agreement is high by "
              "construction; the held-out tests below are the honest check.", "",
              md_table(["rule", "fired", "agree", "agree share", "alerts", "human"], rule_rows) if rule_rows else "No rule fired.",
              ""]
    lines += ["Precision and recall are per candidate, one class against the rest. Labels are derived, not hand "
              "labeled: a correct detection of a name the labels do not know counts as a false positive.", ""]
    for cls in ("host", "instance"):
        shapes, n, english = r["fp"][cls]
        lines += [f"### Most common false-positive shapes: {cls}", "",
                  f"{n:,} false positives at the chosen threshold"
                  + (f"; {english:,} of them are common English or support words (stop list)." if cls == "instance"
                     else "."), "",
                  md_table(["shape", "count"], [[s, f"{k:,}"] for s, k in shapes]) if shapes else "None.", ""]
    uh = r["unseen_host"]
    lines += ["## Unseen-host test", "",
              f"{uh['held_out']:,} hosts held out (removed from the directory and from training). Recall on their "
              "mentions in the test file, at the chosen threshold, and candidate recall:", "",
              md_table(["segment", "detected as host", "became a candidate"],
                       [[seg, _frac(uh["recall"][seg]["host"]), _frac(uh["recall"][seg]["host_candidates"])]
                        for seg in SEGMENTS]), ""]
    ui = r["unseen_instance"]
    lines += ["## Unseen-instance test", "",
              f"{ui['held_out']:,} instance names held out ({ui['unique']:,} used by one client, {ui['shared']:,} "
              "shared across clients), with every host pair that uses them. Recall on their test mentions:", ""]
    rows = []
    for label, key in (("all held-out names", "all"), ("unique to one client", "unique_names"),
                       ("shared across clients", "shared_names")):
        for seg in SEGMENTS:
            x = ui[key][seg]
            rows.append([label, seg, _frac(x["bare"]), _frac(x["bare_candidates"]), _frac(x["host_instance"])])
    lines += [md_table(["names", "segment", "bare detected as instance", "bare became a candidate",
                        "HOST\\INSTANCE detected"], rows), ""]
    pairing = r["pairing"]
    total = sum(pairing.values())
    lines += ["## Bare-instance pairing (test file)", "",
              "Bare instance mentions whose true host is known from the same ticket's dw_servers.", "",
              md_table(["outcome", "mentions", "share"],
                       [[k, f"{v:,}", fmt_pct(v / total if total else None)] for k, v in pairing.most_common()])
              if total else "No such mentions in the test file.", ""]
    nb = r["natural_bare_instances"]
    lines += ["## Open questions", "",
              f"- Natural bare-instance mentions in human tickets: {nb['train_human']:,} in training, "
              f"{nb['test_human']:,} in the test file."
              + (" Fewer than about 50 in the test file is too few to evaluate on; a hand-labeled sample is "
                 "recommended." if nb["test_human"] < 50 else ""),
              "- How the CMDB API identifies a client (Zendesk organization id, Keystone id or name) and whether it "
              "returns instances as HOST\\INSTANCE or as separate fields.",
              "- Whether the default instance (MSSQLSERVER) should be stored as a host-level record, as "
              "match_mention() assumes and this detector does today.",
              "- Whether database names should be a separate kind; for now they are 'none' unless they are also "
              "host or instance names.", ""]
    return "\n".join(lines)


def print_summary(r: dict) -> None:
    cfg = r["config"]
    recs, occ = r["standard"]["records"], r["standard"]["occurrences"]
    print(f"Chosen: {cfg['kind']}, alert context {cfg['alert_context']}, binary instance model "
          f"{'yes' if cfg['instance_model'] else 'no'}; thresholds host {cfg['host_threshold']:.2f}, "
          f"instance {cfg['instance_threshold']:.2f}")
    print(f"  {'':22}{'alerts':>24}{'human':>24}")
    for cls in ("host", "instance"):
        th = cfg[f"{cls}_threshold"]
        cand = [_frac(recall_of(occ, cls, seg, own_only=True)) for seg in ("alerts", "human")]
        print(f"  {cls + ' cand. recall (own)':22}{cand[0]:>24}{cand[1]:>24}")
        m = [prf(_seg(recs, seg), cls, th) for seg in ("alerts", "human")]
        print(f"  {cls + ' precision':22}{fmt_pct(m[0]['precision']):>24}{fmt_pct(m[1]['precision']):>24}")
        print(f"  {cls + ' recall':22}{fmt_pct(m[0]['recall']):>24}{fmt_pct(m[1]['recall']):>24}")
    uh = r["unseen_host"]["recall"]
    print(f"  {'held-out host recall':22}{_frac(uh['alerts']['host']):>24}{_frac(uh['human']['host']):>24}")
    ui = r["unseen_instance"]["all"]
    print(f"  {'held-out bare inst.':22}{_frac(ui['alerts']['bare']):>24}{_frac(ui['human']['bare']):>24}")
    print(f"  {'held-out HOST\\INST':22}{_frac(ui['alerts']['host_instance']):>24}"
          f"{_frac(ui['human']['host_instance']):>24}")
    pairing = r["pairing"]
    print("  bare-instance pairing: " + (", ".join(f"{k} {v:,}" for k, v in pairing.most_common()) or "no mentions"))


# --- CLI -----------------------------------------------------------------------


def parse_args(argv=None):
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--train", type=Path, default=DATA_DIR / "train_tickets.jsonl")
    shared.add_argument("--test", type=Path, default=DATA_DIR / "test_tickets.jsonl")
    shared.add_argument("--host-threshold", type=float, help="default: best F1 on a validation split")
    shared.add_argument("--instance-threshold", type=float, help="default: best F1 on a validation split")
    shared.add_argument("--seed", type=int, default=0)
    shared.add_argument("--holdout-share", type=float, default=0.2, help="share of hosts or instance names held out")
    shared.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="description characters read")
    shared.add_argument("--neg-ratio", type=float, default=DEFAULT_NEG_RATIO,
                        help="'none' training examples kept per positive")
    shared.add_argument("--model-path", type=Path, default=MODEL_PATH)
    shared.add_argument("--exclude-labels", type=Path, nargs="*", default=[HOLDOUT_LABELS_PATH],
                        help="label files whose tickets are left out of training (default: the Claude holdout "
                             "set, when it exists); pass with no files to keep every ticket")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    tr = sub.add_parser("train", parents=[shared], help="fit on --train and save the model")
    # The validation split's instance labels are derived, so its choice can lose on hand labels.
    tr.add_argument("--kind", choices=MODEL_KINDS, nargs="+", help="model kinds to try (default: all)")
    tr.add_argument("--alert-context", choices=ALERT_CONTEXT_MODES, nargs="+", help="alert context modes to try")
    sub.add_parser("evaluate", parents=[shared], help="run all tests and write a report")
    d = sub.add_parser("detect", parents=[shared], help="detect servers in a ticket file")
    d.add_argument("--tickets", type=Path, default=DATA_DIR / "test_tickets.jsonl")
    d.add_argument("--output", type=Path, help="default data/<tickets>-servers.jsonl")
    d.add_argument("--write", action="store_true",
                   help="write a copy of each ticket with detected_servers added, instead of id + detections rows")
    return p.parse_args(argv)


def without_labeled(tickets: list[dict], label_files) -> list[dict]:
    """The tickets minus those labeled in any of label_files (missing files are ignored), so a
    labeled holdout set drawn from the training file stays unseen by the model and the directory."""
    ids = {str(r["ticket_id"]) for path in label_files if Path(path).exists() for r in read_jsonl(path)
           if r.get("ticket_id") is not None}
    kept = [t for t in tickets if str(t["ticket_id"]) not in ids]
    if len(kept) < len(tickets):
        print(f"Left out {len(tickets) - len(kept):,} labeled holdout tickets from training")
    return kept


def cmd_train(args) -> None:
    train = without_labeled(read_jsonl(args.train), args.exclude_labels)
    if not train:
        raise SystemExit(f"Missing or empty ticket file: {args.train}")
    print("Selecting the model on a validation split ...")
    cfg = select_config(train, seed=args.seed, max_chars=args.max_chars, neg_ratio=args.neg_ratio,
                        kinds=args.kind or MODEL_KINDS, modes=args.alert_context or ALERT_CONTEXT_MODES)
    directory = LocalServerDirectory.from_sources(train)
    model, counts = fit_config(train, directory, kind=cfg["kind"], alert_context=cfg["alert_context"],
                               instance_model=cfg["instance_model"], seed=args.seed, max_chars=args.max_chars,
                               neg_ratio=args.neg_ratio)
    model.host_threshold = args.host_threshold if args.host_threshold is not None else cfg["host_threshold"]
    model.instance_threshold = (args.instance_threshold if args.instance_threshold is not None
                                else cfg["instance_threshold"])
    args.model_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": model, "directory": directory, "trained_on": args.train.name, "created_at": now_iso(),
                 "config": {k: v for k, v in cfg.items() if k != "trials"}}, args.model_path)
    print(f"Trained {model.kind} ({counts['examples']:,} examples); thresholds host {model.host_threshold:.2f}, "
          f"instance {model.instance_threshold:.2f}. Saved to {args.model_path}")


def cmd_evaluate(args) -> None:
    train, test = without_labeled(read_jsonl(args.train), args.exclude_labels), read_jsonl(args.test)
    if not train or not test:
        raise SystemExit(f"Missing or empty ticket file: {args.train if not train else args.test}")
    r = evaluate(train, test, seed=args.seed, holdout_share=args.holdout_share, max_chars=args.max_chars,
                 neg_ratio=args.neg_ratio, host_threshold=args.host_threshold,
                 instance_threshold=args.instance_threshold)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"server-detector-{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text(render_report(r, args), encoding="utf-8")
    print_summary(r)
    print(f"Report: {path}")


def cmd_detect(args) -> None:
    if not args.model_path.exists():
        raise SystemExit(f"No model at {args.model_path}. Run: python -m tools.server_detector train")
    bundle = load_model(args.model_path)
    tickets = read_jsonl(args.tickets)
    if not tickets:
        raise SystemExit(f"Missing or empty ticket file: {args.tickets}")
    output = args.output or args.tickets.with_name(f"{args.tickets.stem}-servers.jsonl")
    if output.resolve() == args.tickets.resolve():
        raise SystemExit("The output must be a new file; the source ticket file is never changed")
    rows, kinds, statuses = [], Counter(), Counter()
    for t in tickets:
        found = detect_servers(t, bundle["directory"], bundle["model"], host_threshold=args.host_threshold,
                               instance_threshold=args.instance_threshold)
        kinds.update(d["kind"] for d in found)
        statuses.update(d["status"] for d in found)
        rows.append({**t, "detected_servers": found} if args.write else
                    {"ticket_id": t["ticket_id"], "detected_servers": found})
    write_jsonl(output, rows)
    print(f"{len(tickets):,} tickets, {sum(kinds.values()):,} detections")
    print("  kinds: " + ", ".join(f"{k} {v:,}" for k, v in kinds.most_common()))
    print("  statuses: " + ", ".join(f"{k} {v:,}" for k, v in statuses.most_common()))
    print(f"Wrote {output}")


def main(argv=None):
    args = parse_args(argv)
    {"train": cmd_train, "evaluate": cmd_evaluate, "detect": cmd_detect}[args.command](args)


if __name__ == "__main__":
    main()  # the model classes live in ticket_analysis.server_detector, so saved models never name __main__
