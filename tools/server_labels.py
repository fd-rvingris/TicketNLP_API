"""Hand labels for the server detector, from many labelers: which spans of a ticket's text are hosts,
HOST\\INSTANCE names or bare instance names. detector_ui.py's Label tab collects them;
`python -m tools.server_labels evaluate` scores the saved model against them and reports how often two
labelers agree on the tickets both labeled.

Labels are appended to data/labels/server_spans.jsonl, one row per save; the latest row per ticket
and labeler counts. Offsets are into the field named by 'field' (subject or description), as in
server_detector.detect_servers(). Labelers type their name; nothing verifies it. Prints figures only,
never ticket text.
"""

from __future__ import annotations

import argparse
import random
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


import ticket_analysis.server_detector as sd
from ticket_analysis.jsonl import append_jsonl, now_iso, read_jsonl
from ticket_analysis.masking import _SECRET_PATTERNS
from ticket_analysis.names import parse_server_name
from ticket_analysis.paths import DATA_DIR
from tools.common import REPORTS_DIR, fmt_pct, md_table

LABELS_PATH = DATA_DIR / "labels" / "server_spans.jsonl"
KINDS = ("host", "host_instance", "bare_instance")
STATUSES = ("done", "skipped")  # done with no spans means "no servers in this ticket"
SAMPLE_KINDS = ("human", "alerts", "all")
MASK_CHAR = "•"
RESERVE_SECONDS = 30 * 60  # a ticket handed to a labeler is held for them this long
_LOCK = threading.Lock()


def clean_labeler(name) -> str:
    """A labeler's typed name, trimmed and with runs of spaces collapsed; empty when unusable."""
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    return name[:60] if re.search(r"\w", name) else ""


# --- Text ----------------------------------------------------------------------


def mask_same_length(text: str) -> tuple[str, int]:
    """Mask the secrets common.py masks before text goes to the model, but character for character,
    so offsets into the masked text are offsets into the original. Used for the shared labeling page."""
    chars = list(text)
    count = 0
    for pattern, replacement in _SECRET_PATTERNS:
        for m in pattern.finditer(text):
            original, replaced = m.group(0), m.expand(replacement)
            head = 0
            while head < min(len(original), len(replaced)) and original[head] == replaced[head]:
                head += 1
            tail = 0
            while (tail < min(len(original), len(replaced)) - head
                   and original[-1 - tail] == replaced[-1 - tail]):
                tail += 1
            for i in range(m.start() + head, m.end() - tail):
                if not chars[i].isspace():
                    chars[i] = MASK_CHAR
            count += 1
    return "".join(chars), count


def field_text(ticket: dict, field: str, max_chars: int = sd.DEFAULT_MAX_CHARS) -> str:
    """The text a span's offsets refer to; the description is cut where the detector stops reading."""
    if field == "subject":
        return ticket.get("subject") or ""
    if field == "description":
        return (ticket.get("description") or "")[:max_chars]
    raise ValueError(f"unknown field: {field}")


# --- Sample and assignment -------------------------------------------------------


def sample_tickets(tickets: list[dict], n: int, seed: int, kind: str = "human") -> list[dict]:
    """A fixed random sample to label: the same file, size, seed and kind give the same tickets in the
    same order, so labeling can stop and resume."""
    if kind == "human":
        pool = [t for t in tickets if t.get("is_alert") is False]
    elif kind == "alerts":
        pool = [t for t in tickets if sd.is_alert_ticket(t)]
    else:
        pool = list(tickets)
    pool = sorted(pool, key=lambda t: str(t["ticket_id"]))
    random.Random(seed).shuffle(pool)
    return pool[:n]


class LabelQueue:
    """Hands out tickets so each gets one label, and an evenly spread overlap share gets two from
    different labelers (for measuring agreement). A ticket handed out is reserved for that labeler for
    RESERVE_SECONDS, so two people are not given the same single-label ticket at once."""

    def __init__(self, tickets: list[dict], overlap: float = 0.15, path: Path = LABELS_PATH, clock=time.monotonic):
        self.tickets = {str(t["ticket_id"]): t for t in tickets}
        self.order = list(self.tickets)
        step = max(1, round(1 / overlap)) if overlap > 0 else 0
        self.need = {tid: 2 if step and i % step == 0 else 1 for i, tid in enumerate(self.order)}
        self.path, self.clock = path, clock
        self.reserved: dict[str, dict[str, float]] = defaultdict(dict)

    def labels(self) -> dict[tuple[str, str], dict]:
        return load_labels(self.path)

    def _active(self, tid: str, now: float) -> dict[str, float]:
        res = self.reserved[tid]
        for who in [w for w, t in res.items() if now - t > RESERVE_SECONDS]:
            del res[who]
        return res

    def next_for(self, labeler: str) -> str | None:
        """The ticket this labeler should label next: one already reserved for them, else the first in
        the sample order they have not handled that still needs a label."""
        with _LOCK:
            labels = self.labels()
            handled = {tid for (tid, who) in labels if who == labeler}
            done = Counter(tid for (tid, who), r in labels.items() if r.get("status") == "done")
            now = self.clock()
            for tid in self.order:
                if tid not in handled and labeler in self._active(tid, now):
                    return tid
            for tid in self.order:
                if tid in handled:
                    continue
                others = [w for w in self._active(tid, now) if w != labeler]
                if done[tid] + len(others) < self.need[tid]:
                    self.reserved[tid][labeler] = now
                    return tid
            return None

    def release(self, tid: str, labeler: str) -> None:
        with _LOCK:
            self.reserved[tid].pop(labeler, None)

    def progress(self, labeler: str | None = None) -> dict:
        labels = self.labels()
        rows = [(tid, who, r) for (tid, who), r in labels.items() if tid in self.tickets]
        done = Counter(tid for tid, _, r in rows if r.get("status") == "done")
        per_labeler = Counter(who for _, who, _ in rows)
        needed = sum(self.need.values())
        given = sum(min(done[tid], n) for tid, n in self.need.items())
        return {"sample": len(self.order), "overlap_tickets": sum(1 for n in self.need.values() if n > 1),
                "labels_needed": needed, "labels_done": given,
                "tickets_complete": sum(1 for tid, n in self.need.items() if done[tid] >= n),
                "labelers": [{"name": who, "tickets": n} for who, n in per_labeler.most_common()],
                "mine": per_labeler.get(labeler, 0) if labeler else 0}


# --- Spans ---------------------------------------------------------------------


def normalize_span(span: dict, ticket: dict, max_chars: int = sd.DEFAULT_MAX_CHARS) -> dict:
    """Check a span against the ticket text and fill in its text, normalized host and instance. A bare
    instance may name the host it lives on ('host'); the others take theirs from the text."""
    kind, field = span.get("kind"), span.get("field")
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    text = field_text(ticket, field, max_chars)
    try:
        start, end = int(span["start"]), int(span["end"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("a span needs start and end offsets") from None
    if not 0 <= start < end <= len(text):
        raise ValueError(f"a span is outside the {field}; reload the ticket")
    raw = text[start:end]
    out = {"field": field, "start": start, "end": end, "text": raw, "kind": kind, "host": None, "instance": None}
    parsed = parse_server_name(raw)
    if kind == "host":
        out["host"] = parsed.host if parsed else raw.lower()
    elif kind == "host_instance":
        if not parsed or not parsed.instance:
            raise ValueError(f"'{raw}' has no backslash between host and instance; mark it as a host instead")
        out["host"], out["instance"] = parsed.host, parsed.instance
    else:
        out["instance"] = raw.lower()
        host = str(span.get("host") or "").strip()
        if host:
            p = parse_server_name(host)
            out["host"] = p.host if p else host.lower()
    return out


def normalize_row(row: dict, ticket: dict, labeler: str, max_chars: int = sd.DEFAULT_MAX_CHARS) -> dict:
    status = row.get("status")
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    if not labeler:
        raise ValueError("enter your name before labeling")
    spans = [normalize_span(s, ticket, max_chars) for s in row.get("spans") or []] if status == "done" else []
    spans.sort(key=lambda s: (s["field"] != "subject", s["start"]))
    for a, b in zip(spans, spans[1:]):
        if a["field"] == b["field"] and b["start"] < a["end"]:
            raise ValueError("two marked spans overlap")
    return {"ticket_id": str(ticket["ticket_id"]), "labeler": labeler, "status": status, "spans": spans,
            "notes": str(row.get("notes") or "").strip()[:2000], "labeled_at": now_iso()}


# --- Storage -------------------------------------------------------------------


def load_labels(path: Path = LABELS_PATH) -> dict[tuple[str, str], dict]:
    """The latest row per (ticket, labeler)."""
    out = {}
    for r in read_jsonl(path):
        if r.get("ticket_id") is not None and r.get("labeler"):
            out[(str(r["ticket_id"]), r["labeler"])] = r
    return out


def save_label(row: dict, path: Path = LABELS_PATH) -> None:
    """Append one row. Appends are serialized by a lock, and a reader keeps the latest row per ticket
    and labeler, so an edit never rewrites anyone else's labels."""
    with _LOCK:
        append_jsonl(path, row)


def primary_labels(labels: dict[tuple[str, str], dict]) -> dict[str, dict]:
    """One label row per ticket for scoring: the earliest 'done' row, else the earliest row."""
    out = {}
    for (tid, _), r in sorted(labels.items(), key=lambda kv: kv[1].get("labeled_at") or ""):
        cur = out.get(tid)
        if cur is None or (cur.get("status") != "done" and r.get("status") == "done"):
            out[tid] = r
    return out


# --- Evaluation ----------------------------------------------------------------


def _overlaps(a: dict, b: dict) -> bool:
    return a["field"] == b["field"] and a["start"] < b["end"] and b["start"] < a["end"]


def _class(kind: str) -> str:
    """host and HOST\\INSTANCE are both host mentions; a bare instance is its own class."""
    return "instance" if kind == "bare_instance" else "host"


def agreement(labels: dict[tuple[str, str], dict]) -> dict:
    """Span agreement between two labelers on tickets both marked 'done': a span of one labeler agrees
    when the other marked an overlapping span of the same class. Reported as F1 over the two."""
    by_ticket = defaultdict(list)
    for (tid, _), r in labels.items():
        if r.get("status") == "done":
            by_ticket[tid].append(r)
    matched = total = same_kind = tickets = both_empty = 0
    for rows in by_ticket.values():
        if len(rows) < 2:
            continue
        a, b = sorted(rows, key=lambda r: r["labeler"])[:2]
        tickets += 1
        if not a["spans"] and not b["spans"]:
            both_empty += 1
        for x, y in ((a, b), (b, a)):
            for s in x["spans"]:
                total += 1
                hits = [t for t in y["spans"] if _overlaps(s, t) and _class(t["kind"]) == _class(s["kind"])]
                matched += bool(hits)
                same_kind += any(t["kind"] == s["kind"] for t in hits)
    return {"tickets": tickets, "both_no_servers": both_empty, "span_f1": matched / total if total else None,
            "kind_agreement": same_kind / matched if matched else None, "spans": total}


def evaluate_labels(labels: dict[tuple[str, str], dict], tickets: dict[str, dict], bundle: dict, *,
                    host_threshold: float | None = None, instance_threshold: float | None = None) -> dict:
    """Detections of the saved model against hand-labeled spans, on one 'done' row per ticket.

    A labeled span is found when a detection of the same class (host or instance) overlaps it, and
    found with the right kind when the detection's kind also matches. A detection is correct when it
    overlaps a labeled span of its class. Spans are split by whether the directory already knew the
    name for the ticket's client, which is the case the derived labels cannot measure."""
    model, directory = bundle["model"], bundle["directory"]
    spans, dets = [], []
    pairing = Counter()
    missing = 0
    primary = primary_labels(labels)
    for tid, row in primary.items():
        if row.get("status") != "done":
            continue
        t = tickets.get(tid)
        if t is None:
            missing += 1
            continue
        client = directory.client_for(t)
        known_hosts = set(directory.hosts(client)) if client else set()
        known_instances = {i for _, i in directory.instances(client)} if client else set()
        found = sd.detect_servers(t, directory, model, host_threshold=host_threshold,
                                  instance_threshold=instance_threshold)
        seg = "alerts" if sd.is_alert_ticket(t) else "human"
        for s in row["spans"]:
            cls = _class(s["kind"])
            name_known = (s["host"] in known_hosts) if cls == "host" else (s["instance"] in known_instances)
            hit = [d for d in found if _overlaps(d, s) and _class(d["kind"]) == cls]
            spans.append({"kind": s["kind"], "cls": cls, "seg": seg, "known": name_known, "found": bool(hit),
                          "right_kind": any(d["kind"] == s["kind"] for d in hit)})
            if s["kind"] == "bare_instance" and s.get("host"):
                d = next((d for d in hit if d["kind"] == "bare_instance"), None)
                pairing["not detected" if d is None else "correct host" if d["host"] == s["host"]
                        else "ambiguous" if d["status"] == "ambiguous" else "unpaired" if d["host"] is None
                        else "wrong host"] += 1
        for d in found:
            cls = _class(d["kind"])
            dets.append({"cls": cls, "seg": seg, "rule": d.get("rule"),
                         "correct": any(_overlaps(d, s) and _class(s["kind"]) == cls for s in row["spans"])})
    status = Counter(r.get("status") for r in primary.values())
    return {"spans": spans, "detections": dets, "pairing": pairing, "missing_tickets": missing,
            "tickets_done": status["done"], "tickets_skipped": status["skipped"],
            "tickets_without_servers": sum(1 for r in primary.values() if r.get("status") == "done" and not r["spans"]),
            "labelers": len({who for _, who in labels}), "agreement": agreement(labels),
            "thresholds": {"host": model.host_threshold if host_threshold is None else host_threshold,
                           "instance": model.instance_threshold if instance_threshold is None else instance_threshold}}


def _frac(k: int, n: int) -> str:
    return f"{fmt_pct(k / n) if n else 'n/a'} ({k:,}/{n:,})"


def summary_rows(r: dict) -> list[list[str]]:
    rows = []
    for cls in ("host", "instance"):
        for seg in ("all", "alerts", "human"):
            sp = [s for s in r["spans"] if s["cls"] == cls and seg in ("all", s["seg"])]
            dt = [d for d in r["detections"] if d["cls"] == cls and seg in ("all", d["seg"])]
            known = [s for s in sp if s["known"]]
            new = [s for s in sp if not s["known"]]
            rows.append([cls, seg, _frac(sum(d["correct"] for d in dt), len(dt)),
                         _frac(sum(s["found"] for s in sp), len(sp)),
                         _frac(sum(s["found"] for s in known), len(known)),
                         _frac(sum(s["found"] for s in new), len(new)),
                         _frac(sum(s["right_kind"] for s in sp), len(sp))])
    return rows


def render_report(r: dict, args) -> str:
    th, ag = r["thresholds"], r["agreement"]
    lines = [
        "# Server detector against hand labels", "",
        f"Generated {now_iso()}. Labels {args.labels}, tickets {args.tickets.name}, model {args.model_path.name}. "
        f"{r['labelers']:,} labelers; {r['tickets_done']:,} tickets labeled ({r['tickets_without_servers']:,} with no "
        f"servers), {r['tickets_skipped']:,} skipped, {r['missing_tickets']:,} labeled tickets not in the ticket file. "
        f"Thresholds: host {th['host']:.2f}, instance {th['instance']:.2f}.", "",
        "Precision: detections that overlap a labeled span of their class. Recall: labeled spans overlapped by a "
        "detection of their class, split by whether the directory already knew the name for the ticket's client. "
        "'Right kind' also requires host vs HOST\\INSTANCE vs bare instance to match. Tickets labeled by two people "
        "are scored on the first person's labels.", "",
        md_table(["class", "segment", "precision", "recall", "recall, known names", "recall, new names",
                  "recall, right kind"], summary_rows(r)), "",
        "## Labeler agreement", "",
        f"{ag['tickets']:,} tickets were labeled by two people ({ag['both_no_servers']:,} where both found no servers). "
        f"Span agreement F1: {fmt_pct(ag['span_f1'])} over {ag['spans']:,} spans; of the agreeing spans, "
        f"{fmt_pct(ag['kind_agreement'])} have the same kind. Low agreement means the labeling guidance needs "
        "tightening before the labels are trusted.", "",
        "## Labeled spans", "",
        md_table(["kind", "spans", "known to the directory"],
                 [[k, f"{sum(1 for s in r['spans'] if s['kind'] == k):,}",
                   f"{sum(1 for s in r['spans'] if s['kind'] == k and s['known']):,}"] for k in KINDS]), "",
        "## Detections by source", "",
    ]
    by_source = Counter((d["rule"] or "model", d["correct"]) for d in r["detections"])
    sources = sorted({s for s, _ in by_source}, key=lambda s: (s == "model", s))
    lines += [md_table(["source", "detections", "correct", "precision"],
                       [[s, f"{by_source[(s, True)] + by_source[(s, False)]:,}", f"{by_source[(s, True)]:,}",
                         fmt_pct(by_source[(s, True)] / (by_source[(s, True)] + by_source[(s, False)]))]
                        for s in sources]) if sources else "No detections.", ""]
    total = sum(r["pairing"].values())
    lines += ["## Bare-instance pairing", "", "Bare instances whose host was given when labeling.", "",
              md_table(["outcome", "spans"], [[k, f"{v:,}"] for k, v in r["pairing"].most_common()])
              if total else "No bare instance was labeled with its host.", ""]
    return "\n".join(lines)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("evaluate", help="score the saved model against the hand labels")
    e.add_argument("--labels", type=Path, default=LABELS_PATH)
    e.add_argument("--tickets", type=Path, default=DATA_DIR / "test_tickets.jsonl")
    e.add_argument("--model-path", type=Path, default=sd.MODEL_PATH)
    e.add_argument("--host-threshold", type=float)
    e.add_argument("--instance-threshold", type=float)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    labels = load_labels(args.labels)
    if not labels:
        raise SystemExit(f"No labels in {args.labels}. Label tickets in the detector UI first: python -m tools.detector_ui")
    if not args.model_path.exists():
        raise SystemExit(f"No model at {args.model_path}. Run: python -m tools.server_detector train")
    tickets = {str(t["ticket_id"]): t for t in read_jsonl(args.tickets)}
    r = evaluate_labels(labels, tickets, sd.load_model(args.model_path), host_threshold=args.host_threshold,
                        instance_threshold=args.instance_threshold)
    print(f"{r['labelers']:,} labelers, {r['tickets_done']:,} labeled tickets, {len(r['spans']):,} spans, "
          f"{len(r['detections']):,} detections")
    for row in summary_rows(r):
        if row[1] == "all":
            print(f"  {row[0]:8} precision {row[2]:>22}  recall {row[3]:>22}  new names {row[5]:>22}")
    ag = r["agreement"]
    print(f"  agreement: {ag['tickets']:,} double-labeled tickets, span F1 {fmt_pct(ag['span_f1'])}")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"server-labels-{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text(render_report(r, args), encoding="utf-8")
    print(f"Report: {path}")


if __name__ == "__main__":
    # Run through the imported module, as server_detector.py does, so pickled classes resolve.
    from tools import server_labels
    server_labels.main()
