"""Non-LLM baseline: TF-IDF features and logistic regression that predict the
Zendesk type and category from the ticket subject and description. Trains on
one export, scores on a later one, and reports a learning curve, per-class
precision and recall, and accuracy for alerts and human tickets separately.
Writes a Markdown report to data/reports/. Prints metrics only, never ticket
text. Nothing leaves this machine."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score, precision_recall_fscore_support
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline

from common import DATA_DIR, REPORTS_DIR, fmt_pct, md_table, now_iso, read_jsonl

OTHER = "(merged rare)"  # not "Other", which is a real Zendesk category
DEFAULT_TARGETS = ["zendesk_type", "category"]
DEFAULT_SIZES = [500, 1000, 2000]
SEGMENTS = ("all", "alerts", "human")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--train", type=Path, default=DATA_DIR / "train_tickets.jsonl")
    p.add_argument("--test", type=Path, default=DATA_DIR / "test_tickets.jsonl")
    p.add_argument("--targets", default=",".join(DEFAULT_TARGETS), help="weak label fields to predict")
    p.add_argument("--min-count", type=int, default=20, help="classes with fewer training tickets become Other")
    p.add_argument("--sizes", default=",".join(map(str, DEFAULT_SIZES)),
                   help="training sizes for the learning curve; the full training set is always added")
    p.add_argument("--max-chars", type=int, default=8000, help="description characters used per ticket")
    p.add_argument("--seed", type=int, default=0)
    return p.parse_args()


# --- Labels ----------------------------------------------------------------


def weak_label(ticket: dict, target: str) -> str | None:
    value = (ticket.get("weak_labels") or {}).get(target)
    return str(value).strip() or None if value is not None else None


def kept_classes(labels: list[str], min_count: int) -> set[str]:
    """Classes with at least min_count training tickets; the rest are merged into Other."""
    return {c for c, n in Counter(labels).items() if n >= min_count}


def merge_label(value: str, kept: set[str]) -> str:
    return value if value in kept else OTHER


def redundancy(tickets: list[dict], a: str, b: str) -> dict:
    """How well field a determines field b: the share of tickets with both values whose
    b equals the most common b for their a. 1.0 means b adds nothing beyond a."""
    pairs = [(weak_label(t, a), weak_label(t, b)) for t in tickets]
    both = [(x, y) for x, y in pairs if x and y]
    by_a: dict[str, Counter] = {}
    for x, y in both:
        by_a.setdefault(x, Counter())[y] += 1
    agree = sum(c.most_common(1)[0][1] for c in by_a.values())
    return {"tickets": len(tickets), "with_both": len(both), "missing_b": sum(1 for x, y in pairs if x and not y),
            "agreement": agree / len(both) if both else None,
            "mapping": {x: c.most_common(1)[0][0] for x, c in sorted(by_a.items())}}


# --- Model -----------------------------------------------------------------


def features(tickets: list[dict], max_chars: int) -> np.ndarray:
    """Two text columns, subject and description, as a 2-D object array."""
    return np.array([[t.get("subject") or "", (t.get("description") or "")[:max_chars]] for t in tickets],
                    dtype=object)


def build_model(seed: int) -> Pipeline:
    def tfidf(max_features):
        return TfidfVectorizer(ngram_range=(1, 2), min_df=2, max_df=0.9, sublinear_tf=True,
                               max_features=max_features, strip_accents="unicode")
    return Pipeline([
        ("tfidf", ColumnTransformer([("subject", tfidf(20_000), 0), ("description", tfidf(100_000), 1)])),
        ("clf", LogisticRegression(C=5.0, max_iter=3000, random_state=seed)),
    ])


def subsample(n_total: int, size: int, labels: list[str], seed: int) -> np.ndarray:
    """Indices of a stratified random subsample, or a plain random one when a class is too small."""
    idx = np.arange(n_total)
    if size >= n_total:
        return idx
    try:
        picked, _ = train_test_split(idx, train_size=size, stratify=labels, random_state=seed)
    except ValueError:
        picked = np.random.default_rng(seed).choice(idx, size=size, replace=False)
    return np.sort(picked)


# --- Scoring ---------------------------------------------------------------


def segment_masks(tickets: list[dict]) -> dict[str, np.ndarray]:
    alert = np.array([t.get("is_alert") is True for t in tickets])
    human = np.array([t.get("is_alert") is False for t in tickets])
    return {"all": np.ones(len(tickets), dtype=bool), "alerts": alert, "human": human}


def score(y_true: list[str], y_pred: list[str], masks: dict[str, np.ndarray]) -> dict:
    """Accuracy and macro F1 overall and for each segment."""
    yt, yp = np.array(y_true, dtype=object), np.array(y_pred, dtype=object)
    out = {}
    for name, mask in masks.items():
        n = int(mask.sum())
        out[name] = {"n": n,
                     "accuracy": accuracy_score(yt[mask], yp[mask]) if n else None,
                     "macro_f1": f1_score(yt[mask], yp[mask], average="macro", zero_division=0) if n else None}
    return out


def per_class(y_true: list[str], y_pred: list[str]) -> list[dict]:
    classes = sorted(set(y_true) | set(y_pred), key=lambda c: (-y_true.count(c), c))
    p, r, f, s = precision_recall_fscore_support(y_true, y_pred, labels=classes, zero_division=0)
    return [{"class": c, "precision": p[i], "recall": r[i], "f1": f[i], "support": int(s[i]),
             "predicted": y_pred.count(c)} for i, c in enumerate(classes)]


def top_confusions(y_true: list[str], y_pred: list[str], n: int = 15) -> list[tuple[str, str, int]]:
    pairs = Counter((t, p) for t, p in zip(y_true, y_pred) if t != p)
    return [(t, p, k) for (t, p), k in pairs.most_common(n)]


def run_target(target: str, train: list[dict], test: list[dict], *, min_count: int, sizes: list[int],
               max_chars: int, seed: int) -> dict:
    """Train and score one target over the learning curve sizes. Tickets without the label are left out."""
    train = [t for t in train if weak_label(t, target)]
    test_all = test
    test = [t for t in test if weak_label(t, target)]
    raw_train = [weak_label(t, target) for t in train]
    kept = kept_classes(raw_train, min_count)
    y_train = [merge_label(v, kept) for v in raw_train]
    y_test = [merge_label(weak_label(t, target), kept) for t in test]
    X_train, X_test = features(train, max_chars), features(test, max_chars)
    masks = segment_masks(test)

    majority = Counter(y_train).most_common(1)[0][0]
    result = {
        "target": target,
        "train_tickets": len(train), "test_tickets": len(test),
        "test_missing_label": len(test_all) - len(test),
        "classes_kept": sorted(kept), "merged_values": len(set(raw_train) - kept),
        "merged_train_tickets": sum(1 for v in raw_train if v not in kept),
        "unseen_test_values": len({weak_label(t, target) for t in test} - set(raw_train)),
        "majority_class": majority,
        "majority": score(y_test, [majority] * len(y_test), masks),
        "curve": [],
    }
    for size in sorted({s for s in sizes if s < len(train)} | {len(train)}):
        idx = subsample(len(train), size, y_train, seed)
        y_sub = [y_train[i] for i in idx]
        if len(set(y_sub)) < 2:
            continue
        model = build_model(seed).fit(X_train[idx], y_sub)
        y_pred = list(model.predict(X_test))
        result["curve"].append({"size": len(idx), **score(y_test, y_pred, masks)})
        result["y_pred"] = y_pred  # the last (largest) size is kept for the detail tables
    y_pred = result.pop("y_pred")
    result["per_class"] = per_class(y_test, y_pred)
    result["per_class_by_segment"] = {name: per_class([y for y, m in zip(y_test, mask) if m],
                                                      [y for y, m in zip(y_pred, mask) if m])
                                      for name, mask in masks.items() if name != "all" and mask.any()}
    result["confusions"] = top_confusions(y_test, y_pred)
    return result


# --- Report ----------------------------------------------------------------


def curve_table(r: dict) -> str:
    rows = [["majority class"] + [f"{fmt_pct(r['majority'][s]['accuracy'])} / {fmt_pct(r['majority'][s]['macro_f1'])}"
                                  for s in SEGMENTS]]
    for point in r["curve"]:
        rows.append([f"{point['size']:,}"] + [f"{fmt_pct(point[s]['accuracy'])} / {fmt_pct(point[s]['macro_f1'])}"
                                             for s in SEGMENTS])
    n = r["curve"][-1] if r["curve"] else r["majority"]
    headers = ["training tickets"] + [f"{s} (n={n[s]['n']:,})" for s in SEGMENTS]
    return md_table(headers, rows)


def per_class_table(rows: list[dict]) -> str:
    return md_table(["class", "precision", "recall", "F1", "test tickets", "predicted"],
                    [[c["class"], fmt_pct(c["precision"]), fmt_pct(c["recall"]), fmt_pct(c["f1"]),
                      c["support"], c["predicted"]] for c in rows])


def render(results: list[dict], red: dict | None, args, train_n: int, test_n: int) -> str:
    lines = [
        "# Baseline: TF-IDF and logistic regression",
        "",
        f"Generated {now_iso()}. Training file {args.train.name} ({train_n:,} tickets), test file "
        f"{args.test.name} ({test_n:,} tickets). Features: word 1-2 gram TF-IDF on the subject and on the first "
        f"{args.max_chars:,} description characters, fitted separately. Classes with fewer than {args.min_count} "
        f"training tickets are merged into {OTHER}. Seed {args.seed}.",
        "",
        "Each cell is accuracy / macro F1 on the test file. Macro F1 weights every class equally, so it shows "
        "how the model does on small classes that accuracy hides.",
        "",
    ]
    if red:
        lines += ["## Is sub type redundant with zendesk_type?", "",
                  f"Among {red['with_both']:,} training tickets with both values, sub type equals the most common "
                  f"sub type for the ticket's zendesk_type in {fmt_pct(red['agreement'])} of cases. "
                  f"{red['missing_b']:,} tickets have a zendesk_type but no sub type.", "",
                  md_table(["zendesk_type", "usual sub type"], [[k, v] for k, v in red["mapping"].items()]), ""]
    for r in results:
        lines += [
            f"## {r['target']}", "",
            f"{r['train_tickets']:,} training and {r['test_tickets']:,} test tickets have a value "
            f"({r['test_missing_label']:,} test tickets without one are left out). {len(r['classes_kept'])} classes "
            f"kept; {r['merged_values']} rarer values ({r['merged_train_tickets']:,} training tickets) merged into "
            f"{OTHER}. {r['unseen_test_values']} test values never appear in training. Majority class: "
            f"{r['majority_class']}.", "",
            "### Learning curve", "", curve_table(r), "",
            f"### Per class (all test tickets, trained on {r['curve'][-1]['size']:,})", "",
            per_class_table(r["per_class"]), "",
        ]
        for name, rows in r["per_class_by_segment"].items():
            lines += [f"### Per class: {name}", "", per_class_table(rows), ""]
        if r["confusions"]:
            lines += ["### Most common errors", "",
                      md_table(["actual", "predicted", "test tickets"], [list(c) for c in r["confusions"]]), ""]
    return "\n".join(lines)


def main():
    args = parse_args()
    train, test = read_jsonl(args.train), read_jsonl(args.test)
    if not train or not test:
        raise SystemExit(f"Missing or empty ticket file: {args.train if not train else args.test}")
    overlap = {t["ticket_id"] for t in train} & {t["ticket_id"] for t in test}
    if overlap:
        raise SystemExit(f"{len(overlap)} tickets are in both the training and the test file")
    targets = [t.strip() for t in args.targets.split(",") if t.strip()]
    sizes = [int(s) for s in args.sizes.split(",") if s.strip()]

    red = redundancy(train, "zendesk_type", "sub type") if any(weak_label(t, "sub type") for t in train) else None
    results = []
    for target in targets:
        print(f"Training {target} ...")
        r = run_target(target, train, test, min_count=args.min_count, sizes=sizes, max_chars=args.max_chars,
                       seed=args.seed)
        results.append(r)
        print(f"  {'size':>8}  {'all':>15}  {'alerts':>15}  {'human':>15}   (accuracy / macro F1)")
        for label, point in [("majority", r["majority"])] + [(f"{p['size']:,}", p) for p in r["curve"]]:
            print(f"  {label:>8}  " + "  ".join(
                f"{fmt_pct(point[s]['accuracy']):>6} / {fmt_pct(point[s]['macro_f1']):>6}" for s in SEGMENTS))
    if red:
        print(f"sub type matches zendesk_type's usual sub type in {fmt_pct(red['agreement'])} of "
              f"{red['with_both']:,} training tickets")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"baseline-{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text(render(results, red, args, len(train), len(test)), encoding="utf-8")
    print(f"Report: {path}")


if __name__ == "__main__":
    main()
