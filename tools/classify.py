"""Classify tickets with one Claude API call per ticket and write
data/runs/<run-name>/results.jsonl. Runs are resumable."""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic

from ticket_analysis.jsonl import append_jsonl, now_iso, read_jsonl
from ticket_analysis.masking import prepare_ticket_text
from ticket_analysis.names import load_cmdb
from ticket_analysis.paths import DATA_DIR
from ticket_analysis.taxonomy import TAXONOMY_VERSION
from tools.common import (DEFAULT_EFFORT, DEFAULT_MAX_TOKENS, DEFAULT_MODEL, EFFORT_LEVELS, RUNS_DIR, TICKETS_PATH,
                          FatalAPIError, build_request, classify_ticket, client_server_hints, fmt_usd, load_env,
                          load_prompt, make_client, render_user_message)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tickets", type=Path, default=TICKETS_PATH, help="default data/tickets.jsonl")
    p.add_argument("--limit", type=int, help="only the first N tickets in the file")
    p.add_argument("--model", default=DEFAULT_MODEL, help=f"default {DEFAULT_MODEL}")
    p.add_argument("--effort", default=DEFAULT_EFFORT, choices=EFFORT_LEVELS, help=f"default {DEFAULT_EFFORT}")
    p.add_argument("--prompt-version", default="v2", help="uses prompts/classify_<version>.md (default v2)")
    p.add_argument("--run-name", help="default <model family>-<prompt version>[-<effort>], e.g. opus-v1")
    p.add_argument("--workers", type=int, default=4, help="parallel API calls (default 4)")
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                   help=f"output limit per call, thinking included (default {DEFAULT_MAX_TOKENS})")
    p.add_argument("--retry-failed", action="store_true", help="re-classify tickets whose last row failed")
    p.add_argument("--dry-run", action="store_true", help="print the prompt for the first ticket; no API call")
    return p.parse_args()


def default_run_name(model: str, prompt_version: str, effort: str) -> str:
    family = model.removeprefix("claude-").split("-")[0]
    name = f"{family}-{prompt_version}"
    return name if effort == DEFAULT_EFFORT else f"{name}-{effort}"


def check_run_config(run_dir: Path, config: dict) -> None:
    """Refuse to resume a run with a different model, effort, prompt, or taxonomy."""
    path = run_dir / "run.json"
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        diffs = [k for k in ("model", "effort", "prompt_version", "taxonomy_version") if existing.get(k) != config[k]]
        if diffs:
            detail = ", ".join(f"{k}={existing.get(k)}" for k in diffs)
            raise SystemExit(f"Run '{run_dir.name}' was created with {detail}. Use a different --run-name.")
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**config, "created_at": now_iso()}, indent=2), encoding="utf-8")


def dry_run(ticket: dict, prompt, cmdb, args) -> None:
    """Print the rendered prompt. The description is withheld from the console
    (data handling rule); the full rendered prompt is saved under data/dry-run/."""
    subject, description, masked = prepare_ticket_text(ticket)
    servers = client_server_hints(cmdb.for_ticket(ticket))
    full = render_user_message(prompt, subject, description, servers)
    out = DATA_DIR / "dry-run" / f"{ticket['ticket_id']}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"=== SYSTEM ===\n{prompt.system}\n\n=== USER ===\n{full}\n", encoding="utf-8")

    placeholder = (f"[description withheld from console: {len(description)} characters, {masked} secret(s) "
                   f"masked; full prompt saved to {out.relative_to(DATA_DIR.parent)}]")
    shown = render_user_message(prompt, subject, placeholder, servers)
    request = build_request(prompt, "", model=args.model, effort=args.effort, max_tokens=args.max_tokens)
    print(f"Ticket {ticket['ticket_id']}: {ticket['subject'][:80]}")
    print(f"Model {request['model']}, effort {args.effort}, max_tokens {request['max_tokens']}, "
          f"fallbacks {request.get('fallbacks', 'none')}, prompt {prompt.version}, taxonomy {TAXONOMY_VERSION}, "
          f"client servers listed {len(servers)}")
    print("\n=== SYSTEM ===")
    print(prompt.system)
    print("\n=== USER ===")
    print(shown)


def describe(row: dict) -> str:
    if row["error"]:
        kind = row["error"].split(":")[0]
        return f"FAILED {kind}" + (f" ({row['stop_category']})" if row.get("stop_category") else "")
    out = row["output"]
    drops = f", {row['dropped_mentions']} quote(s) dropped" if row["dropped_mentions"] else ""
    return (f"{out['primary_type']} / {out['intent']} ({out['confidence']}), "
            f"{len(row['matches'])} mention(s){drops}, {row['latency_ms']} ms, {fmt_usd(row['estimated_cost_usd'])}")


def main():
    args = parse_args()
    load_env()
    tickets = read_jsonl(args.tickets)
    if not tickets:
        raise SystemExit(f"No tickets in {args.tickets}. Run python -m tools.fetch_tickets or python -m tools.load_export first.")
    selected = tickets[: args.limit] if args.limit else tickets
    prompt = load_prompt(args.prompt_version)
    cmdb = load_cmdb()

    if args.dry_run:
        dry_run(selected[0], prompt, cmdb, args)
        return

    run_name = args.run_name or default_run_name(args.model, args.prompt_version, args.effort)
    run_dir = RUNS_DIR / run_name
    check_run_config(run_dir, {"model": args.model, "effort": args.effort, "prompt_version": prompt.version,
                               "taxonomy_version": TAXONOMY_VERSION, "max_tokens": args.max_tokens})
    results_path = run_dir / "results.jsonl"
    last = {r["ticket_id"]: r for r in read_jsonl(results_path)}
    done = {tid for tid, r in last.items() if not (args.retry_failed and r.get("error"))}
    todo = [t for t in selected if t["ticket_id"] not in done]
    print(f"Run {run_name}: {len(todo)} to classify, {len(selected) - len(todo)} already done "
          f"({args.model}, effort {args.effort}, prompt {prompt.version})")
    if not todo:
        return

    try:
        client = make_client()
    except anthropic.AnthropicError as e:
        raise SystemExit(f"Could not create the Anthropic client: {e}")

    ok = failed = 0
    cost = 0.0
    pool = ThreadPoolExecutor(max_workers=args.workers)
    futures = {
        pool.submit(classify_ticket, client, t, prompt, cmdb, run_name=run_name, model=args.model,
                    effort=args.effort, max_tokens=args.max_tokens): t
        for t in todo
    }
    try:
        for n, future in enumerate(as_completed(futures), 1):
            ticket = futures[future]
            row = future.result()
            append_jsonl(results_path, row)
            ok, failed = (ok + 1, failed) if not row["error"] else (ok, failed + 1)
            cost += row["estimated_cost_usd"] or 0
            print(f"[{n}/{len(todo)}] {ticket['ticket_id']} {ticket['subject'][:60]!r}: {describe(row)}")
    except FatalAPIError as e:
        pool.shutdown(wait=True, cancel_futures=True)
        raise SystemExit(f"Stopped: {e}")
    except KeyboardInterrupt:
        pool.shutdown(wait=True, cancel_futures=True)
        raise SystemExit("Interrupted. Completed rows are saved; rerun the same command to resume.")
    pool.shutdown()
    print(f"Done: {ok} classified, {failed} failed, estimated cost {fmt_usd(cost)}. "
          f"Results in {results_path}. Next: python -m tools.report --run {run_name}")


if __name__ == "__main__":
    main()
