"""Strip email boilerplate from ticket text before the taxonomy model scores it.

Banners, confidentiality footers, image and attachment links, URLs, quoted reply history, greeting and
sign-off lines, the support mailbox name and @-mentions add many small pushes toward the wrong type, so
baseline.features() and ticket_types.classify() both read cleaned text. The server detector reads raw
text and does not use this module.

The patterns are conservative: a line that also carries a server-shaped name, a SQL keyword or an error
number is never removed, and when cleaning would leave no text the input is returned unchanged.

Run directly to measure what the cleaning removes from the ticket files (counts only, never ticket text).
"""

from __future__ import annotations

import argparse
import re
import statistics
from collections import Counter
from pathlib import Path

from common import DATA_DIR, fmt_pct, read_jsonl

PATTERNS = ("quoted", "footer", "banner", "images", "urls", "greeting", "signoff", "mailbox", "mentions")
TICKET_FILES = [DATA_DIR / "train_tickets.jsonl", DATA_DIR / "test_tickets.jsonl"]

_H = r"[^\S\r\n]"  # horizontal space, including non-breaking spaces

# --- Lines that are never removed ----------------------------------------------------------

_SERVERISH = re.compile(r"\b[A-Za-z][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*\b"  # a name with a digit: SQL01, CHG0070937
                        r"|\w\\\w"  # HOST\INSTANCE, DOMAIN\user
                        r"|(?i:\b(?:server|data source|host|database|initial catalog)" + _H + r"*=)")
_SQL = re.compile(r"\b(?:SELECT|INSERT|UPDATE|DELETE|GRANT|REVOKE|DENY|ALTER|CREATE|DROP|EXEC|EXECUTE|TRUNCATE"
                  r"|BACKUP|RESTORE|DBCC|USE|DECLARE|WHERE)\b|\b(?:db|sp|xp|fn|sys)_\w+")
_ERROR = re.compile(r"(?i)\b(?:error|msg|err|sev(?:erity)?|state)" + _H + r"*[:#]?" + _H + r"*-?\d{2,6}\b"
                    r"|\b0x[0-9A-Fa-f]{6,}\b")


def protected(line: str) -> bool:
    """True when a line carries a server-shaped name, a SQL keyword or an error number."""
    return bool(_SERVERISH.search(line) or _SQL.search(line) or _ERROR.search(line))


# --- Patterns ------------------------------------------------------------------------------

_BANNER = re.compile(r"(?i)\bCAUTION\b.{0,40}\b(?:originated|came|sent|is)\b.{0,30}\boutside\b"
                     r"|\[EXTERNAL\]|^\W*EXTERNAL" + _H + r"+(?:E-?MAIL|SENDER|MESSAGE)\b"
                     r"|\bthis (?:message|e-?mail) (?:came|originated|was sent) from outside\b"
                     r"|\bdo not click (?:on )?links or open attachments\b"
                     r"|\byou don't often get email from\b")
_FOOTER_START = re.compile(
    r"(?i)^\W*(?:confidentiality notice|confidential(?:ity)?" + _H + r"*:|disclaimer" + _H + r"*:|notice" + _H + r"*:"
    r"|privileged (?:and|&) confidential"
    r"|this (?:e-?mail|message|communication|transmission)(?:,)?(?: and any (?:attachments?|files?|documents?)[^.]{0,40})?"
    + _H + r"+(?:is|are|may|contains?|including|and its)\b"
    r"|the (?:information|content) (?:contained )?in this (?:e-?mail|message|communication)"
    r"|if you (?:are not|have received) (?:the intended|this (?:e-?mail|message|communication) in error)"
    r"|any (?:review|disclosure|use|dissemination|distribution|copying)\b)")
_FOOTER_WORDS = re.compile(r"(?i)intended recipient|confidential|privileged|disclos|dissemina|distribut|unauthori[sz]ed"
                           r"|received this (?:e-?mail|message|communication) in error|notify the sender|virus")
_QUOTED_FROM = re.compile(r"(?i)^\W*from" + _H + r"*:" + _H + r"*\S")
_QUOTED_SENT = re.compile(r"(?i)^\W*(?:sent|date)" + _H + r"*:")
_QUOTED_START = re.compile(r"(?i)^\W*-{2,}" + _H + r"*(?:original|forwarded) message" + _H + r"*-{2,}"
                           r"|^" + _H + r"*on\b.{5,200}\bwrote:" + _H + r"*$"
                           r"|^\W*from" + _H + r"*:.{1,200}\bsent" + _H + r"*:")
_LINKS = [re.compile(r"!\[[^\]\r\n]*\]\([^)\r\n]*\)"),  # Markdown image
          re.compile(r"(?i)\[(?:cid|image):[^\]\r\n]*\]"),
          re.compile(r"(?i)<?\b[\w.-]*image\d+\.(?:png|jpe?g|gif|bmp)\b(?:@[\w.]+)?>?")]
_URLS = [re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>()\[\]]+")]
_MD_LINK = re.compile(r"\[([^\]\r\n]+)\]\(\s*\)")  # a Markdown link whose URL was removed: keep its text
_GREETING = re.compile(r"(?i)^\W*(?:hi|hello|hey|dear|greetings|good" + _H + r"+(?:morning|afternoon|evening|day))"
                       r"(?:" + _H + r"+[\w.'&-]+){0,3}" + _H + r"*[,!:.]?" + _H + r"*$")
_SIGNOFF = re.compile(r"(?i)^\W*(?:thanks|thank" + _H + r"+you|thanks" + _H + r"+again|many" + _H + r"+thanks|thx|ty"
                      r"|(?:best|kind|warm|warmest)?" + _H + r"*regards|best|cheers|sincerely|respectfully|v/r"
                      r"|sent from my \w+)"
                      r"(?:" + _H + r"+[\w.'&-]+){0,4}" + _H + r"*[,!.]?" + _H + r"*$")
_SIGNATURE_MAX = 80  # signature lines after a sign-off are short
_SIGNATURE_LINES = 8
_MAILBOX = re.compile(r"(?i)\bfortified" + _H + r"*data" + _H + r"+support\b|\bsupport@fortifieddata\.com\b")
_MENTION = re.compile(r"(?<![\w.@])@[A-Z][a-z'-]+(?:" + _H + r"[A-Z][a-z'-]+)?\b")  # @Jane Doe
_SUBJECT_TAG = re.compile(r"(?i)\[EXTERNAL\]|^\W*EXTERNAL" + _H + r"*:" + _H + r"*|\bCAUTION" + _H + r"*:" + _H + r"*")


def _paragraph_end(lines: list[str], i: int) -> int:
    """The index after the last line of the paragraph that line i is in."""
    while i < len(lines) and lines[i].strip():
        i += 1
    return i


def _drop(lines: list[str], start: int, end: int, removed: Counter, name: str) -> list[str]:
    """Lines start to end blanked out, except protected ones; counts removed characters under name."""
    for j in range(start, end):
        if lines[j] and not protected(lines[j]):
            removed[name] += len(lines[j])
            lines[j] = ""
    return lines


def _quoted(lines: list[str], removed: Counter) -> list[str]:
    for i, line in enumerate(lines):
        start = bool(_QUOTED_START.search(line)) or (
            _QUOTED_FROM.search(line) and any(_QUOTED_SENT.search(x) for x in lines[i + 1:i + 5]))
        if not start:
            continue
        # Keep the history when the reply above it says almost nothing ("see below")
        if len(re.findall(r"[A-Za-z]{2,}", "\n".join(lines[:i]))) < 5:
            return lines
        return _drop(lines, i, len(lines), removed, "quoted")
    return lines


def _footers(lines: list[str], removed: Counter) -> list[str]:
    i = 0
    while i < len(lines):
        if not _FOOTER_START.search(lines[i]):
            i += 1
            continue
        end = _paragraph_end(lines, i)
        # Following paragraphs that read like more of the disclaimer go too
        while True:
            nxt = end
            while nxt < len(lines) and not lines[nxt].strip():
                nxt += 1
            stop = _paragraph_end(lines, nxt)
            if nxt >= len(lines) or len(_FOOTER_WORDS.findall("\n".join(lines[nxt:stop]))) < 2:
                break
            end = stop
        _drop(lines, i, end, removed, "footer")
        i = end
    return lines


def _banners(lines: list[str], removed: Counter) -> list[str]:
    i = 0
    while i < len(lines):
        if not _BANNER.search(lines[i]):
            i += 1
            continue
        end = _paragraph_end(lines, i)
        block = "\n".join(lines[i:end])
        # The whole banner paragraph when it is banner-sized, else the matching line only
        _drop(lines, i, end if len(block) <= 500 else i + 1, removed, "banner")
        i = max(end, i + 1)
    return lines


def _inline(lines: list[str], removed: Counter, name: str, patterns: list[re.Pattern], skip_protected: bool):
    for i, line in enumerate(lines):
        if not line or (skip_protected and protected(line)):
            continue
        new = line
        for pat in patterns:
            new = pat.sub("", new)
        if name in ("images", "urls"):
            new = _MD_LINK.sub(r"\1", new)
        removed[name] += len(line) - len(new)
        lines[i] = new
    return lines


def _greetings(lines: list[str], removed: Counter) -> list[str]:
    for i, line in enumerate(lines):
        if line and _GREETING.match(line) and not protected(line):
            removed["greeting"] += len(line)
            lines[i] = ""
    return lines


def _signoffs(lines: list[str], removed: Counter) -> list[str]:
    i = 0
    while i < len(lines):
        if not (lines[i] and _SIGNOFF.match(lines[i]) and not protected(lines[i])):
            i += 1
            continue
        removed["signoff"] += len(lines[i])
        lines[i] = ""
        # The signature block under it: short, unprotected lines up to the next blank line
        j = i + 1
        while j < len(lines) and j <= i + _SIGNATURE_LINES and lines[j].strip() \
                and len(lines[j]) <= _SIGNATURE_MAX and not protected(lines[j]):
            removed["signoff"] += len(lines[j])
            lines[j] = ""
            j += 1
        i = j
    return lines


def _clean(subject: str, description: str, patterns, removed: Counter) -> tuple[str, str]:
    subject, description = subject or "", description or ""
    text = description.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    lines = text.split("\n")
    if "quoted" in patterns:
        lines = _quoted(lines, removed)
    if "footer" in patterns:
        lines = _footers(lines, removed)
    if "banner" in patterns:
        lines = _banners(lines, removed)
    if "images" in patterns:
        lines = _inline(lines, removed, "images", _LINKS, skip_protected=False)
    if "urls" in patterns:
        lines = _inline(lines, removed, "urls", _URLS, skip_protected=False)
    if "mailbox" in patterns:
        lines = _inline(lines, removed, "mailbox", [_MAILBOX], skip_protected=False)
    if "mentions" in patterns:
        lines = _inline(lines, removed, "mentions", [_MENTION], skip_protected=True)
    if "greeting" in patterns:
        lines = _greetings(lines, removed)
    if "signoff" in patterns:
        lines = _signoffs(lines, removed)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(x.rstrip() for x in lines)).strip()
    new_subject = subject
    if "banner" in patterns:
        new_subject = _SUBJECT_TAG.sub("", subject).strip()
        removed["banner"] += len(subject) - len(new_subject)
    # Never leave nothing when the input had words
    if not re.search(r"\w", out) and re.search(r"\w", description):
        removed.clear()
        removed["fallback"] = 1
        return (new_subject or subject), description
    if not re.search(r"\w", new_subject):
        new_subject = subject
    return new_subject, out


def clean_text(subject: str, description: str, patterns=PATTERNS) -> tuple[str, str]:
    """The subject and description with email boilerplate removed. patterns picks which removals run."""
    return _clean(subject, description, patterns, Counter())


def removed_chars(subject: str, description: str, patterns=PATTERNS) -> Counter:
    """Characters each pattern removes from one ticket; 'fallback' is 1 when the raw text was kept."""
    removed = Counter()
    _clean(subject, description, patterns, removed)
    return +removed


# --- Measurement ---------------------------------------------------------------------------


def measure(tickets: list[dict]) -> dict:
    """Per pattern: tickets touched and median characters removed where touched, plus the totals."""
    per, total, fallback = {p: [] for p in PATTERNS}, [], 0
    for t in tickets:
        r = removed_chars(t.get("subject") or "", t.get("description") or "")
        fallback += r.pop("fallback", 0)
        for p, n in r.items():
            per[p].append(n)
        if r:
            total.append(sum(r.values()))
    return {"tickets": len(tickets), "total": total, "per": per, "fallback": fallback}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("files", nargs="*", type=Path, default=TICKET_FILES)
    args = p.parse_args(argv)
    tickets = [t for f in args.files if f.exists() for t in read_jsonl(f)]
    if not tickets:
        raise SystemExit("No tickets found")
    for name, subset in (("all", tickets), ("human", [t for t in tickets if t.get("is_alert") is False]),
                         ("alerts", [t for t in tickets if t.get("is_alert") is True])):
        if not subset:
            continue
        m = measure(subset)
        n = m["tickets"]
        print(f"{name}: {n:,} tickets, {fmt_pct(len(m['total']) / n)} touched, median "
              f"{statistics.median(m['total']) if m['total'] else 0:,.0f} characters removed where touched, "
              f"{m['fallback']:,} fell back to the raw text")
        for pat in PATTERNS:
            hits = m["per"][pat]
            print(f"  {pat:<9} {fmt_pct(len(hits) / n):>6} of tickets, median "
                  f"{statistics.median(hits) if hits else 0:>6,.0f} characters")


if __name__ == "__main__":
    main()
