"""An inventory of the alert layouts (templates) in the ticket files.

Alerts are machine-generated from a few fixed templates. layout_signature() reduces an alert to the
ordered template labels and header lines of its description (values removed) plus the template
words of its subject, so alerts from one template share a signature. The inventory command groups
the alerts of the ticket files by signature, merges signatures that differ only in optional lines,
and writes a report (data/reports/alert-layouts-<timestamp>.md) and one masked example file per
layout (data/alert_layouts/).

Ticket text is never printed. Examples are masked: a word is kept only when it is a template word
(a parser label, or a word seen in the alerts of many organizations that is not part of any
organization or server name); every other word becomes its compressed shape (SQL01 becomes L3 D2).

    python alert_layouts.py inventory [--tickets data/train_tickets.jsonl data/test_tickets.jsonl]
"""

from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from ticket_analysis.alert_parser import COLLECTOR_LABELS, FIRING_LABELS, parse_alert
from common import DATA_DIR, REPORTS_DIR, md_table, read_jsonl
from server_detector import BUILTIN_STOP, compress, is_alert_ticket, ticket_names

LAYOUTS_DIR = DATA_DIR / "alert_layouts"
DEFAULT_TICKETS = [DATA_DIR / "train_tickets.jsonl", DATA_DIR / "test_tickets.jsonl"]

# Words of the templates the parsers know, always kept when masking
LABEL_WORDS = frozenset(w.lower() for label in [*COLLECTOR_LABELS, *FIRING_LABELS, "Environment Name", "Alert Type",
                                                 "Open Date (UTC)", "DATABASENAME BACKUP TYPE", "Backup Exception Report",
                                                 "from server", "Volume Capacity", "FIRING", "RESOLVED", "UTC",
                                                 "ServerName", "Host(s)", "Database", "Error Type",
                                                 "Critical Warning Production"]
                        for w in re.findall(r"[^\W\d_]+", label))
# Lines that any email-borne alert may carry (the external-sender banner); never part of a layout
BANNER_LINES = frozenset({"CAUTION:"})
MARKDOWN_LINE = "* *Label*: value pairs on one line"
MIN_LABEL_ALERTS = 10  # labels in fewer alerts are free text (message lines), not template lines
_WORD = re.compile(r"[^\W\d_]+")
_CHUNK = re.compile(r"\S+")
_LINE = re.compile(r"[^\r\n]+")
_LINE_LABEL = re.compile(r"^(?P<label>[A-Za-z@][A-Za-z0-9 ()/_.\-]{0,40}?)[^\S\r\n]*(?::|=)(?:[^\S\r\n]|$)")
# People's names: "Stephen,", "Hi Stephen", and the line after "Thanks," or "Regards,"
_GREETING = re.compile(r"^[^\S\r\n]*(?:(?i:hi|hello|hey|dear|thanks|thank you)[^\S\r\n]+(?P<named>[A-Za-z]+(?:[^\S\r\n]+[A-Za-z]+)?)"
                       r"|(?P<names>[A-Z][a-z]+(?:[^\S\r\n]+[A-Z][a-z]+)?)[^\S\r\n]*,)[^\S\r\n]*[,!.]?[^\S\r\n]*\r?$", re.M)
_SIGN_OFF = re.compile(r"^[^\S\r\n]*(?i:thanks|thank you|regards|best regards|best|cheers|sincerely)[^\S\r\n]*[,!.]?[^\S\r\n]*\r?\n"
                       r"(?:[^\S\r\n]*\r?\n)*[^\S\r\n]*(?P<names>[A-Z][a-z]+(?:[^\S\r\n]+[A-Z][a-z]+){0,2})[^\S\r\n]*\r?$", re.M)


def _people_words(text: str) -> set[str]:
    out = set()
    for pattern in (_GREETING, _SIGN_OFF):
        for m in pattern.finditer(text):
            out.update(w.lower() for w in _WORD.findall(m["names"] or m.groupdict().get("named") or ""))
    return out - BUILTIN_STOP


class Vocabulary:
    """The template words: parser labels, plus words seen in the alerts of at least min_orgs
    organizations that occur in no organization name, server name, parsed client, environment or
    collector name, and no greeting or sign-off (people's names)."""

    def __init__(self, tickets: list[dict], min_orgs: int = 8):
        orgs: dict[str, set] = defaultdict(set)
        unsafe: set[str] = set()
        for t in tickets:
            unsafe.update(w.lower() for w in _WORD.findall(t.get("organization_name") or ""))
            unsafe.update(_people_words(t.get("description") or ""))
            fields = (t.get("alert_fields") if "alert_fields" in t else
                      parse_alert(t.get("subject") or "", t.get("description") or "")) or {}
            for key in ("client_name", "environment", "collector"):
                unsafe.update(w.lower() for w in _WORD.findall(fields.get(key) or ""))
            for p in ticket_names(t):
                unsafe.update(w.lower() for w in _WORD.findall(f"{p.host} {p.instance or ''}"))
            unsafe.update(w.lower() for v in (t.get("dw_servers") or []) for w in _WORD.findall(str(v)))
            org = str(t.get("organization_id") or t.get("organization_name") or "")
            for w in set(_WORD.findall(f"{t.get('subject') or ''}\n{t.get('description') or ''}")):
                orgs[w.lower()].add(org)
        self.words = frozenset(w for w, o in orgs.items() if len(o) >= min_orgs and w not in unsafe) | LABEL_WORDS

    def keep(self, word: str) -> bool:
        return word.lower() in self.words

    def mask(self, text: str) -> str:
        """The text with every word that is not a template word replaced by its compressed shape and
        every number by D<length>. Line breaks and spacing between chunks are kept."""
        def chunk(m: re.Match) -> str:
            c = m.group()
            if all(self.keep(w) for w in _WORD.findall(c)):
                return re.sub(r"\d+", lambda d: f"D{len(d.group())}", c)
            return compress(c)
        return _CHUNK.sub(chunk, text or "")

    def is_template(self, text: str) -> bool:
        """True when every word of the text is a template word and it has no digits."""
        return not re.search(r"\d", text) and all(self.keep(w) for w in _WORD.findall(text))


_VOCAB: Vocabulary | None = None


def default_vocabulary() -> Vocabulary:
    global _VOCAB
    if _VOCAB is None:
        _VOCAB = Vocabulary([t for p in DEFAULT_TICKETS for t in read_jsonl(p)])
    return _VOCAB


# --- Signatures ----------------------------------------------------------------

_KNOWN_LABELS = sorted({*COLLECTOR_LABELS, *FIRING_LABELS}, key=len, reverse=True)


def subject_template(subject: str, vocab: Vocabulary) -> str:
    """The subject's template words in order, with runs of other words collapsed to '*' and numbers
    to 'D', up to the alert number ('Alert #D') or six tokens, whichever comes first."""
    out: list[str] = []
    for c in _CHUNK.findall(subject or ""):
        words = _WORD.findall(c)
        tok = re.sub(r"\d+", "D", c) if all(vocab.keep(w) for w in words) and (words or "#" in c) else "*"
        if tok == "*" and out and out[-1] == "*":
            continue
        out.append(tok)
        if tok.startswith("#D") and len(out) > 1 and out[-2] == "Alert" or len(out) >= 6:
            break
    return " ".join(out)


def line_label(line: str, vocab: Vocabulary) -> str | None:
    """The template part of one description line, or None for a value-only line: a header (template
    text with three or more known labels, such as the table layout's column line), a collector or
    Grafana label at its start, a 'Label:' or 'label =' made of template words, or the whole line
    when it is short template text (a heading such as 'DATABASENAME BACKUP TYPE ...')."""
    s = re.sub(r"[^\S\r\n]+", " ", line.replace("\xa0", " ")).strip()
    if not s:
        return None
    if vocab.is_template(s) and is_header(s):
        return s
    # The collector's labels flattened onto one Markdown line: "* *AlertId*: 1 * *Alert*: ..."
    starred = re.findall(r"\*\*?([A-Za-z][A-Za-z ()]{0,30}?)\*\*?:", s)
    if len(starred) >= 3 and all(vocab.is_template(x) for x in starred):
        return MARKDOWN_LINE
    for name in _KNOWN_LABELS:
        if s == name or s.startswith((name + " ", name + "=")):
            # "Alert Collection errors" is the label Alert with the value "Collection errors"
            return "Alert" if name == "Alert Collection" else name
    m = _LINE_LABEL.match(s)
    if m and vocab.is_template(m["label"]):
        return m["label"].strip() + ":"
    if len(s) <= 120 and vocab.is_template(s) and _WORD.search(s):
        return s
    return None


def layout_signature(ticket: dict, vocab: Vocabulary | None = None) -> tuple[str, tuple[str, ...]]:
    """(subject template, the description's template labels and header lines in order). Consecutive
    repeats are removed, so a table's rows give one label."""
    vocab = vocab or default_vocabulary()
    labels: list[str] = []
    for m in _LINE.finditer(ticket.get("description") or ""):
        lab = line_label(m.group(), vocab)
        if lab and (not labels or labels[-1] != lab):
            labels.append(lab)
    return subject_template(ticket.get("subject") or "", vocab), tuple(labels)


def body_core(labels: tuple[str, ...], template: set[str]) -> tuple[tuple[str, ...], int]:
    """The labels kept for grouping (template labels, no banner) and the number of repeated blocks:
    alerts that carry several alerts repeat their labels, and only the first block is kept."""
    core = [lab for lab in labels if lab in template and lab not in BANNER_LINES]
    blocks = 1
    if core:
        starts = [i for i, lab in enumerate(core) if lab == core[0]]
        blocks = len(starts)
        if blocks > 1:
            core = core[:starts[1]]
    return tuple(core), blocks


def _edit_distance(a: tuple, b: tuple, cap: int = 3) -> int:
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def _subsequence(short: tuple, long: tuple) -> bool:
    it = iter(long)
    return all(x in it for x in short)


def is_header(label: str) -> bool:
    """A column-header line: three or more known labels on one line, or four or more upper-case words
    and nothing else ('DATABASENAME BACKUP TYPE LATEST BACKUP DATE ...')."""
    if re.fullmatch(r"[A-Z]+(?: [A-Z]+){3,}", label):
        return True
    return sum(1 for n in _KNOWN_LABELS if re.search(rf"(?<!\w){re.escape(n)}(?!\w)", label)) >= 3


def same_layout(a: tuple, b: tuple) -> bool:
    """Two cores belong to one layout when they share a label and differ by at most two labels
    (inserted, removed or replaced, in order; one when either has fewer than four), or when the
    shorter, with three or more labels or a column header, is contained in the other in order (the
    other's extra lines are optional), or when they share a column header line."""
    if not a or not b:
        return a == b
    short, long = sorted((a, b), key=len)
    limit = 2 if len(short) >= 4 else 1
    if any(is_header(lab) for lab in set(a) & set(b)):
        return True
    return (bool(set(a) & set(b)) and _edit_distance(a, b) <= limit) or \
        ((len(short) >= 3 or is_header(short[0])) and _subsequence(short, long))


def group_layouts(cores: Counter) -> list[list[tuple]]:
    """Cores grouped into layouts, most common first. A core joins the first layout whose most common
    core it matches (same_layout), so rare cores cannot chain two layouts together."""
    groups: list[list[tuple]] = []
    for core, _ in cores.most_common():
        group = next((g for g in groups if same_layout(core, g[0])), None)
        if group is None:
            groups.append([core])
        else:
            group.append(core)
    return groups


# --- Inventory -------------------------------------------------------------------


def _share(n: int, d: int) -> str:
    return f"{n / d:.0%}" if d else "n/a"


def _top(counter: Counter, n: int) -> str:
    return "; ".join(f"{k} ({v})" for k, v in counter.most_common(n) if k)


def masked_example(ticket: dict, vocab: Vocabulary, max_lines: int = 60) -> str:
    lines = [vocab.mask(m.group()) for m in _LINE.finditer(ticket.get("description") or "") if m.group().strip()]
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines)"]
    return f"Subject: {vocab.mask(ticket.get('subject') or '')}\n\n" + "\n".join(lines)


def inventory(tickets: list[dict], vocab: Vocabulary) -> dict:
    """Alerts grouped into layouts, with counts, parser coverage and masked examples."""
    alerts = [t for t in tickets if t.get("is_alert") is True]
    sigs = [layout_signature(t, vocab) for t in alerts]
    seen = Counter(lab for _, labels in sigs for lab in set(labels))
    template = {lab for lab, n in seen.items() if n >= MIN_LABEL_ALERTS or is_header(lab) or lab == MARKDOWN_LINE}
    cores = [body_core(labels, template) for _, labels in sigs]
    groups = group_layouts(Counter(c for c, _ in cores))
    group_of = {c: i for i, g in enumerate(groups) for c in g}
    members: dict[int, list[int]] = defaultdict(list)
    for i, (core, _) in enumerate(cores):
        members[group_of[core]].append(i)
    layouts = []
    for gi, idx in sorted(members.items(), key=lambda kv: -len(kv[1])):
        n = len(idx)
        label_share = Counter(lab for i in idx for lab in set(cores[i][0]))
        main = Counter(cores[i][0] for i in idx).most_common(1)[0][0]
        families, names, subjects, blocks = Counter(), Counter(), Counter(), Counter()
        recognised = with_fields = with_server = 0
        for i in idx:
            t = alerts[i]
            fields = parse_alert(t.get("subject") or "", t.get("description") or "")
            fam = (fields or {}).get("family")
            families[f"{fam}/{fields['layout']}" if fields and fields.get("layout") else fam or "none"] += 1
            recognised += fields is not None
            with_fields += bool(fields and set(fields) - {"family", "layout"})
            with_server += bool(fields and ticket_names({"alert_fields": fields}))
            names[vocab.mask((fields or {}).get("alert_name") or "")] += 1
            subjects[sigs[i][0]] += 1
            blocks[cores[i][1]] += 1
        # Examples: the most common core first, then other cores, at most three
        examples, used = [], set()
        for i in sorted(idx, key=lambda i: (cores[i][0] != main, len(alerts[i].get("description") or ""))):
            if cores[i][0] not in used and len(examples) < 3:
                used.add(cores[i][0])
                examples.append(masked_example(alerts[i], vocab))
        layouts.append({
            "alerts": n, "core": main, "variants": len(groups[gi]),
            "required": [lab for lab in main if label_share[lab] == n],
            "optional": sorted((lab for lab, c in label_share.items() if c < n), key=lambda lab: -label_share[lab]),
            "label_share": label_share, "families": families, "alert_names": names, "subjects": subjects,
            "blocks": blocks, "recognised": recognised, "with_fields": with_fields, "with_server": with_server,
            "examples": examples})
    for k, lay in enumerate(layouts, 1):
        lay["id"] = f"L{k:02d}"
    humans = [t for t in tickets if t.get("is_alert") is not True]
    human_hits = sum(parse_alert(t.get("subject") or "", t.get("description") or "") is not None for t in humans)
    return {"alerts": len(alerts), "signatures": len(set(sigs)), "cores": len(set(c for c, _ in cores)),
            "layouts": layouts, "humans": len(humans), "human_hits": human_hits}


def render_report(inv: dict, sources: list[str]) -> str:
    out = [f"# Alert layouts, {datetime.now():%Y-%m-%d %H:%M}", "",
           f"Ticket files: {', '.join(sources)}. {inv['alerts']:,} alerts, {inv['signatures']:,} distinct "
           f"signatures, {inv['cores']} after dropping free-text labels (fewer than {MIN_LABEL_ALERTS} alerts) and "
           f"the email banner, grouped into {len(inv['layouts'])} layouts. Non-alert tickets the current parser "
           f"recognises: {inv['human_hits']} of {inv['humans']:,}.", "",
           "Values are masked: template words are kept, every other word is its compressed shape "
           "(SQL01 becomes L3 D2).", "",
           "## Layouts", ""]
    rows = []
    for lay in inv["layouts"]:
        rows.append([lay["id"], lay["alerts"], _top(lay["families"], 3), " / ".join(lay["core"][:6]) +
                     (" ..." if len(lay["core"]) > 6 else ""), _share(lay["recognised"], lay["alerts"]),
                     _share(lay["with_fields"], lay["alerts"]), _share(lay["with_server"], lay["alerts"])])
    out += [md_table(["Layout", "Alerts", "Current family", "Signature (first labels)", "Recognised",
                      "With fields", "With a server"], rows), ""]
    for lay in inv["layouts"]:
        n = lay["alerts"]
        out += [f"## {lay['id']} ({n:,} alerts)", "",
                f"- Current family: {_top(lay['families'], 5)}",
                f"- Recognised by the current parser: {_share(lay['recognised'], n)}; with fields beyond the family: "
                f"{_share(lay['with_fields'], n)}; with a server: {_share(lay['with_server'], n)}",
                f"- Signature variants merged: {lay['variants']}",
                f"- Required lines: {', '.join(lay['required']) or 'none'}",
                "- Optional lines (alerts with the line): " + (", ".join(f"{lab} ({lay['label_share'][lab]:,})"
                                                  for lab in lay["optional"]) or "none"),
                f"- Subjects: {_top(lay['subjects'], 6)}",
                f"- Alert names (masked): {_top(lay['alert_names'], 8) or 'none parsed'}"]
        if len(lay["blocks"]) > 1 or 1 not in lay["blocks"]:
            out.append("- Repeated alert blocks per ticket: " + ", ".join(f"{b} ({c})" for b, c in sorted(lay["blocks"].items())))
        out += ["", "Example (masked):", "", "```", lay["examples"][0] if lay["examples"] else "", "```", ""]
    return "\n".join(out)


def write_examples(inv: dict, folder: Path) -> list[Path]:
    """One masked example file per layout, with its lines and up to three examples (one per variant)."""
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("L*.txt"):
        old.unlink()
    paths = []
    for lay in inv["layouts"]:
        path = folder / f"{lay['id']}.txt"
        head = [f"Alert layout {lay['id']}: {lay['alerts']} alerts in the sample.",
                "Values are masked: template words are kept, every other word is its shape (L = letters,",
                "D = digits, with the run length: SQL01 becomes L3 D2).", "",
                "Required lines: " + (", ".join(lay["required"]) or "none"),
                "Optional lines: " + (", ".join(lay["optional"]) or "none"), ""]
        body = [f"--- Example {k} ---\n{ex}\n" for k, ex in enumerate(lay["examples"], 1)]
        path.write_text("\n".join(head + body), encoding="utf-8")
        paths.append(path)
    return paths


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    inv_p = sub.add_parser("inventory", help="group the alerts by layout and write the report and example files")
    inv_p.add_argument("--tickets", nargs="+", type=Path, default=DEFAULT_TICKETS)
    inv_p.add_argument("--min-orgs", type=int, default=8, help="organizations a word must appear in to be kept")
    args = ap.parse_args(argv)
    tickets = [t for p in args.tickets for t in read_jsonl(p)]
    vocab = Vocabulary(tickets, args.min_orgs)
    inv = inventory(tickets, vocab)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORTS_DIR / f"alert-layouts-{datetime.now():%Y%m%d-%H%M%S}.md"
    report.write_text(render_report(inv, [p.name for p in args.tickets]), encoding="utf-8")
    files = write_examples(inv, LAYOUTS_DIR)
    print(f"{inv['alerts']} alerts, {inv['signatures']} signatures, {len(inv['layouts'])} layouts; "
          f"non-alert tickets recognised by the parser: {inv['human_hits']}")
    for lay in inv["layouts"]:
        print(f"  {lay['id']} alerts={lay['alerts']} recognised={_share(lay['recognised'], lay['alerts'])} "
              f"server={_share(lay['with_server'], lay['alerts'])} variants={lay['variants']}")
    print(f"report: {report}\nexamples: {len(files)} files in {LAYOUTS_DIR}")


if __name__ == "__main__":
    main()


