"""Write a self-contained HTML page for browsing a ticket file, optionally with one
run's results, to data/viewer/. The page loads nothing from the network; open it
in a browser."""

import argparse
import json
import webbrowser
from pathlib import Path

from common import DATA_DIR, TICKETS_PATH, latest_rows, now_iso, prepare_ticket_text, read_jsonl, resolve_run

TEMPLATE_PATH = Path(__file__).parent / "viewer_template.html"
PLACEHOLDER = "__VIEWER_DATA__"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tickets", type=Path, default=TICKETS_PATH, help="default data/tickets.jsonl")
    p.add_argument("--run", help="also show this run's results: a run name, or 'latest'")
    p.add_argument("--output", type=Path, help="default data/viewer/<ticket file name>[-<run>].html")
    p.add_argument("--open", action="store_true", help="open the page in the default browser")
    return p.parse_args()


def viewer_ticket(ticket: dict, row: dict | None) -> dict:
    """The ticket plus its text exactly as classify.py would send it (secrets masked)."""
    subject, description, masked = prepare_ticket_text(ticket)
    return {**ticket, "sent_subject": subject, "sent_description": description, "secrets_masked": masked,
            "result": row}


def build_html(tickets: list[dict], rows_by_id: dict[str, dict], *, source: str, run_name: str | None = None) -> str:
    data = {"source": source, "run": run_name, "generated_at": now_iso(),
            "tickets": [viewer_ticket(t, rows_by_id.get(t["ticket_id"])) for t in tickets]}
    # Escaping "<" keeps ticket text such as "</script>" from ending the data block early.
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if template.count(PLACEHOLDER) != 1:
        raise SystemExit(f"{TEMPLATE_PATH.name} must contain {PLACEHOLDER} exactly once")
    return template.replace(PLACEHOLDER, payload)


def main():
    args = parse_args()
    tickets = read_jsonl(args.tickets)
    if not tickets:
        raise SystemExit(f"No tickets in {args.tickets}. Run fetch_tickets.py or load_export.py first.")
    run_name, rows_by_id = None, {}
    if args.run:
        run_dir = resolve_run(args.run)
        run_name = run_dir.name
        rows_by_id = latest_rows(read_jsonl(run_dir / "results.jsonl"))

    output = args.output or DATA_DIR / "viewer" / f"{args.tickets.stem}{'-' + run_name if run_name else ''}.html"
    if DATA_DIR.resolve() not in output.resolve().parents:
        print(f"warning: {output} is outside data/, so git does not ignore it; it contains ticket text")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(build_html(tickets, rows_by_id, source=args.tickets.name, run_name=run_name), encoding="utf-8")
    print(f"Wrote {len(tickets)} tickets{f' with run {run_name}' if run_name else ''} to {output}")
    if args.open:
        webbrowser.open(output.resolve().as_uri())


if __name__ == "__main__":
    main()
