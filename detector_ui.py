"""A page for trying and labeling the server detector: paste text, or give a Zendesk ticket URL or
number, and see the hosts and instances the saved model finds, with every scored candidate; and label
a shared sample of tickets by hand (see server_labels.py); and browse one example of every alert type in
the ticket files, with its parsed fields highlighted.

Serves on 127.0.0.1 by default (port 8765). With --host 0.0.0.0 it serves the network so several
DBAs can label; there is no sign-in, labelers type their name, and Zendesk lookups are off unless
--zendesk on is given, because anyone who can reach the page could use them. Pasted text and fetched
tickets stay in memory and on the page; the console logs requests without their content. Secrets in
labeling text are masked character for character. Train the model first: python -m tools.server_detector train
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import socket
import webbrowser
from collections import defaultdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests

import ticket_analysis.server_detector as sd
import server_labels as sl
import ticket_analysis.ticket_types as tt
import tools.ticket_types as ttt
from ticket_analysis.alert_parser import alert_spans, parse_alert
from common import DATA_DIR, ZendeskClient, load_env, make_ticket, now_iso, read_jsonl

PAGE_PATH = Path(__file__).parent / "detector_ui.html"
DEFAULT_PORT = 8765
MAX_TEXT = 200_000  # characters accepted from the page


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model-path", type=Path, default=sd.MODEL_PATH)
    p.add_argument("--train", type=Path, default=DATA_DIR / "train_tickets.jsonl",
                   help="read for client names in the client list only")
    p.add_argument("--host", default="127.0.0.1",
                   help="address to serve on; 0.0.0.0 serves the network so others can label (no sign-in)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--allow-host", action="append", default=[], metavar="NAME[:PORT]",
                   help="Host header to accept, such as a DNS alias for this machine; repeatable. Default: "
                        "localhost names, plus this machine's name and addresses when serving the network")
    p.add_argument("--zendesk", choices=("auto", "on", "off"), default="auto",
                   help="Zendesk lookups; auto is on for 127.0.0.1 and off when serving the network")
    p.add_argument("--label-tickets", type=Path, default=DATA_DIR / "test_tickets.jsonl",
                   help="ticket file the labeling sample is drawn from")
    p.add_argument("--label-sample", type=int, default=200, help="tickets in the labeling sample")
    p.add_argument("--label-kind", choices=sl.SAMPLE_KINDS, default="human")
    p.add_argument("--label-overlap", type=float, default=0.15,
                   help="share of sampled tickets labeled by two people, to measure agreement")
    p.add_argument("--label-seed", type=int, default=0)
    p.add_argument("--labels-path", type=Path, default=sl.LABELS_PATH)
    p.add_argument("--alert-tickets", type=Path, nargs="*",
                   default=[DATA_DIR / "train_tickets.jsonl", DATA_DIR / "test_tickets.jsonl"],
                   help="ticket files the Alert types tab takes one example of each alert type from")
    p.add_argument("--open", action="store_true", help="open the page in the default browser")
    return p.parse_args()


# --- Analysis ------------------------------------------------------------------


def client_names(tickets: list[dict]) -> dict[str, str]:
    """Client key -> display name, from a ticket file (the directory stores names in lower case)."""
    names = {}
    for t in tickets:
        org, name_key = sd.ticket_client_keys(t)
        name = (t.get("organization_name") or "").strip()
        if name and (org or name_key):
            names.setdefault(org or name_key, name)
    return names


def ticket_for_text(subject: str, text: str, client: str | None, names: dict[str, str]) -> dict:
    """A ticket for pasted text. The client key (a Zendesk organization id or 'name:<name>') decides
    which client's known names are used; none means no client."""
    org = client if client and not client.startswith("name:") else None
    name = names.get(client) if client else None
    if client and client.startswith("name:") and not name:
        name = client[5:]
    return make_ticket("pasted", subject, text, organization_id=org, organization_name=name, source="pasted")


def scored_candidates(ticket: dict, directory, model: sd.ServerModel) -> list[dict]:
    """Every candidate with its host and instance probabilities, highest first, for the page."""
    know = model.knowledge(directory)
    cands = sd.ticket_candidates(ticket, know, alert_context=model.alert_context, max_chars=model.max_chars)
    if not cands:
        return []
    sd.add_features(cands, know, ticket)
    subject_len = len(ticket.get("subject") or "")
    out = []
    for c, (ph, pi) in zip(cands, model.scores(cands)):
        in_subject = c["start"] < subject_len
        offset = 0 if in_subject else subject_len + 1
        out.append({"text": c["text"], "field": "subject" if in_subject else "description",
                    "start": c["start"] - offset, "end": c["end"] - offset, "kind": c["kind"],
                    "host_candidate": c["host_ok"], "instance_candidate": c["inst_ok"],
                    "p_host": round(float(ph), 4), "p_instance": round(float(pi), 4), "rule": c.get("rule")})
    return sorted(out, key=lambda c: -max(c["p_host"], c["p_instance"]))


def _threshold(value, default: float) -> float:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(x, 0.0), 1.0)


def analyze(ticket: dict, bundle: dict, names: dict[str, str], *, host_threshold=None,
            instance_threshold=None) -> dict:
    model, directory = bundle["model"], bundle["directory"]
    th_h = _threshold(host_threshold, model.host_threshold)
    th_i = _threshold(instance_threshold, model.instance_threshold)
    client = directory.client_for(ticket)
    known = client in set(directory.clients())
    found = sd.detect_servers(ticket, directory, model, host_threshold=th_h, instance_threshold=th_i)
    return {
        "ticket": {k: ticket.get(k) for k in ("ticket_id", "subject", "description", "organization_id",
                                              "organization_name", "created_at", "source")},
        "is_alert": sd.is_alert_ticket(ticket),
        "client": {"key": client, "name": names.get(client) or ticket.get("organization_name"),
                   "in_directory": known,
                   "hosts": len(directory.hosts(client)) if known else 0,
                   "instances": len(directory.instances(client)) if known else 0},
        "thresholds": {"host": th_h, "instance": th_i},
        "detections": found,
        "alert_fields": ticket.get("alert_fields"),
        # Where each alert value sits in the subject or description, for highlighting (every layout)
        "alert_spans": alert_spans(ticket.get("subject") or "", ticket.get("description") or ""),
        "candidates": scored_candidates(ticket, directory, model),
        "max_chars": model.max_chars,
        "analyzed_at": now_iso(),
    }


# --- Alert types -------------------------------------------------------------------

# Layouts whose alerts have no alert name of their own
FIXED_ALERT_TYPES = {"backup_report": "Backup Exception Report"}


def alert_catalog(tickets: list[dict]) -> list[dict]:
    """One entry per alert type (layout and alert name) in the tickets: how many alerts and clients it
    has, the share whose parsed fields name a server, and an example ticket of typical length (the
    median description). Alerts are tickets flagged is_alert or recognised by a layout; fields are
    parsed again, so the catalog follows the current parser."""
    groups: dict[tuple[str, str], list[tuple[dict, dict | None]]] = defaultdict(list)
    for t in tickets:
        fields = parse_alert(t.get("subject") or "", t.get("description") or "")
        if t.get("is_alert") is not True and not fields:
            continue
        layout = (fields or {}).get("layout") or "unrecognised"
        name = ((fields or {}).get("alert_name") or "").strip() or FIXED_ALERT_TYPES.get(layout) or "(no alert name)"
        groups[(layout, name)].append((t, fields))
    out = []
    for (layout, name), members in groups.items():
        members.sort(key=lambda m: (len(m[0].get("description") or ""), str(m[0].get("ticket_id"))))
        example, fields = members[len(members) // 2]
        out.append({"key": f"{layout}|{name}", "layout": layout, "alert_type": name, "alerts": len(members),
                    "clients": len({sd.ticket_client_keys(t) for t, _ in members}),
                    "with_server": round(sum(bool(f and sd.ticket_names({"alert_fields": f})) for _, f in members)
                                         / len(members), 3),
                    "example_fields": len(set(fields or {}) - {"family", "layout"}), "example": example})
    return sorted(out, key=lambda r: (r["layout"], -r["alerts"], r["alert_type"].lower()))


# --- Zendesk -----------------------------------------------------------------------

_TICKET_URL = re.compile(r"^https://([a-z0-9-]+)\.zendesk\.com/(?:agent/tickets|tickets|api/v2/tickets|hc/[^/]+/requests)/(\d+)",
                         re.I)


def ticket_id_from(value: str, subdomain: str) -> str:
    """The ticket number from a Zendesk ticket URL on the configured subdomain, or from a bare number.
    Other hosts are refused, so the API token is only ever sent to the configured Zendesk."""
    value = (value or "").strip()
    if re.fullmatch(r"#?\d+", value):
        return value.lstrip("#")
    m = _TICKET_URL.match(value)
    if not m:
        raise ValueError("Enter a Zendesk ticket URL such as https://<subdomain>.zendesk.com/agent/tickets/12345, "
                         "or the ticket number")
    if m.group(1).lower() != subdomain.lower():
        raise ValueError(f"That URL is not on the configured Zendesk ({subdomain}.zendesk.com)")
    return m.group(2)


def fetch_ticket(zd: ZendeskClient, ticket_id: str) -> dict:
    """One ticket (subject and description, the first comment) with its organization name."""
    t = zd.get(f"tickets/{ticket_id}.json")["ticket"]
    org_id = t.get("organization_id")
    org_name = None
    if org_id:
        try:
            org_name = zd.get(f"organizations/{org_id}.json")["organization"]["name"]
        except requests.HTTPError:
            org_name = None  # the agent may not see the organization; the id still selects the client
    return make_ticket(t["id"], t.get("subject"), t.get("description"), organization_id=org_id,
                       organization_name=org_name, created_at=t.get("created_at"), tags=t.get("tags"),
                       source="zendesk_api")


# --- HTTP ------------------------------------------------------------------------------


class App:
    def __init__(self, bundle: dict, names: dict[str, str], zendesk: ZendeskClient | None, subdomain: str | None,
                 port: int, queue: sl.LabelQueue | None = None, allowed_hosts: set[str] | None = None,
                 catalog: list[dict] | None = None, types: dict | None = None, alert_map: dict | None = None):
        self.bundle, self.names, self.zendesk, self.subdomain, self.port = bundle, names, zendesk, subdomain, port
        self.queue = queue
        self.types, self.alert_map = types, alert_map or {}
        self.catalog = {c["key"]: c for c in catalog or []}
        self.allowed_hosts = allowed_hosts or {f"127.0.0.1:{port}", f"localhost:{port}"}

    # --- Labeling ----------------------------------------------------------------

    def _queue(self) -> sl.LabelQueue:
        if not self.queue:
            raise ValueError("Labeling is off: the ticket file for the labeling sample was not found")
        return self.queue

    @staticmethod
    def _labeler(body: dict) -> str:
        name = sl.clean_labeler(body.get("labeler"))
        if not name:
            raise ValueError("Enter your name first")
        return name

    def _label_payload(self, tid: str, labeler: str) -> dict:
        queue, model, directory = self._queue(), self.bundle["model"], self.bundle["directory"]
        t = queue.tickets[tid]
        texts, masked = {}, 0
        for field in ("subject", "description"):
            texts[field], n = sl.mask_same_length(sl.field_text(t, field, model.max_chars))
            masked += n

        def shown(span: dict) -> dict:  # the span's text as the page shows it, secrets masked
            return {**span, "text": texts[span["field"]][span["start"]:span["end"]]}

        suggestions = [shown({k: d[k] for k in ("field", "start", "end", "kind", "host", "instance", "score", "rule")})
                       for d in sd.detect_servers(t, directory, model)]
        saved = queue.labels().get((tid, labeler))
        return {"ticket_id": tid, "client": t.get("organization_name"), "created_at": t.get("created_at"),
                "subject": texts["subject"], "description": texts["description"],
                "truncated": len(t.get("description") or "") > model.max_chars, "max_chars": model.max_chars,
                "masked": masked, "double": queue.need.get(tid, 1) > 1, "suggestions": suggestions,
                "saved": {"status": saved["status"], "notes": saved.get("notes", ""),
                          "spans": [shown(s) for s in saved["spans"]]} if saved else None,
                "progress": queue.progress(labeler)}

    def label_next(self, body: dict) -> dict:
        labeler = self._labeler(body)
        tid = self._queue().next_for(labeler)
        if tid is None:
            return {"done": True, "progress": self.queue.progress(labeler)}
        return self._label_payload(tid, labeler)

    def label_open(self, body: dict) -> dict:
        labeler = self._labeler(body)
        tid = str(body.get("ticket_id") or "")
        if tid not in self._queue().tickets:
            raise ValueError("That ticket is not in the labeling sample")
        return self._label_payload(tid, labeler)

    def label_save(self, body: dict) -> dict:
        labeler = self._labeler(body)
        queue = self._queue()
        tid = str(body.get("ticket_id") or "")
        if tid not in queue.tickets:
            raise ValueError("That ticket is not in the labeling sample")
        row = sl.normalize_row(body, queue.tickets[tid], labeler, self.bundle["model"].max_chars)
        sl.save_label(row, queue.path)
        queue.release(tid, labeler)
        return {"saved": tid, "spans": len(row["spans"]), "progress": queue.progress(labeler)}

    def label_mine(self, body: dict) -> dict:
        labeler = self._labeler(body)
        queue = self._queue()
        rows = sorted((r for (tid, who), r in queue.labels().items() if who == labeler and tid in queue.tickets),
                      key=lambda r: r.get("labeled_at") or "", reverse=True)
        return {"tickets": [{"ticket_id": r["ticket_id"], "status": r["status"], "spans": len(r["spans"]),
                             "labeled_at": r.get("labeled_at")} for r in rows],
                "progress": queue.progress(labeler)}

    # --- Alert types -------------------------------------------------------------

    def alert_types(self, body: dict) -> dict:
        return {"types": [{k: v for k, v in c.items() if k != "example"} for c in self.catalog.values()]}

    def alert_example(self, body: dict) -> dict:
        """The analysis of a type's example ticket, secrets masked character for character as on the
        labeling tab, with the type's counts."""
        entry = self.catalog.get(str(body.get("key") or ""))
        if not entry:
            raise ValueError("Unknown alert type")
        t = dict(entry["example"])
        t["subject"] = sl.mask_same_length(t.get("subject") or "")[0]
        t["description"] = sl.mask_same_length(t.get("description") or "")[0]
        t["alert_fields"] = parse_alert(t["subject"], t["description"])
        result = analyze(t, self.bundle, self.names, host_threshold=body.get("host_threshold"),
                         instance_threshold=body.get("instance_threshold"))
        result["alert_type"] = {k: v for k, v in entry.items() if k != "example"}
        return result

    def info(self) -> dict:
        directory, model = self.bundle["directory"], self.bundle["model"]
        clients = sorted(({"key": k, "name": self.names.get(k) or (k[5:] if k.startswith("name:") else k),
                           "hosts": len(directory.hosts(k))} for k in directory.clients()),
                         key=lambda c: c["name"].lower())
        return {"clients": clients, "thresholds": {"host": model.host_threshold, "instance": model.instance_threshold},
                "model": {"kind": model.kind, "alert_context": model.alert_context,
                          "instance_model": model.use_instance_model, "trained_on": self.bundle.get("trained_on"),
                          "created_at": self.bundle.get("created_at")},
                "directory": directory.stats(), "zendesk": self.subdomain if self.zendesk else None,
                "labeling": {"sample": len(self.queue.order), "overlap": self.queue.progress()["overlap_tickets"]}
                if self.queue else None,
                "alert_types": len(self.catalog),
                "ticket_types": {"tickets": self.types["tickets"], "created_at": self.types.get("created_at"),
                                 "alert_names": len(self.alert_map)} if self.types else None}

    # --- Taxonomy ----------------------------------------------------------------

    def taxonomy(self, body: dict) -> dict:
        return ttt.summary(self.types, self.alert_map)

    def classify_text(self, body: dict) -> dict:
        """The taxonomy type and intent for pasted text: the alert-name table for alerts, else the
        local model. The text is not stored."""
        if not self.types:
            raise ValueError(f"The Classify tab needs Claude's sample labels at {ttt.LABELS_PATH}")
        text, subject = str(body.get("text") or ""), str(body.get("subject") or "")
        if not (text.strip() or subject.strip()):
            raise ValueError("Paste some text first")
        if len(text) + len(subject) > MAX_TEXT:
            raise ValueError(f"Text is longer than {MAX_TEXT:,} characters")
        return tt.classify(subject, text, self.types, self.alert_map)

    def analyze_text(self, body: dict) -> dict:
        text, subject = str(body.get("text") or ""), str(body.get("subject") or "")
        if not (text.strip() or subject.strip()):
            raise ValueError("Paste some text first")
        if len(text) + len(subject) > MAX_TEXT:
            raise ValueError(f"Text is longer than {MAX_TEXT:,} characters")
        ticket = ticket_for_text(subject, text, body.get("client") or None, self.names)
        return analyze(ticket, self.bundle, self.names, host_threshold=body.get("host_threshold"),
                       instance_threshold=body.get("instance_threshold"))

    def analyze_zendesk(self, body: dict) -> dict:
        if not self.zendesk:
            raise ValueError("Zendesk is not configured: set ZENDESK_SUBDOMAIN, ZENDESK_EMAIL and ZENDESK_API_TOKEN in .env")
        ticket_id = ticket_id_from(str(body.get("url") or ""), self.subdomain)
        try:
            ticket = fetch_ticket(self.zendesk, ticket_id)
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            reason = {401: "authentication failed (check the .env token)", 403: "no permission to read it",
                      404: "not found"}.get(code, f"Zendesk returned {code}")
            raise ValueError(f"Ticket {ticket_id}: {reason}") from None
        except requests.RequestException as e:
            raise ValueError(f"Could not reach Zendesk: {type(e).__name__}") from None
        return analyze(ticket, self.bundle, self.names, host_threshold=body.get("host_threshold"),
                       instance_threshold=body.get("instance_threshold"))


def make_handler(app: App):
    allowed_hosts = app.allowed_hosts

    class Handler(BaseHTTPRequestHandler):
        server_version = "DetectorUI"

        def log_message(self, fmt, *args):  # method, path and status only; never request bodies
            print(f"{self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: dict) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _host_ok(self) -> bool:
            # Rejects DNS-rebinding requests, which arrive with another Host name.
            if self.headers.get("Host") not in allowed_hosts:
                self._json(HTTPStatus.FORBIDDEN, {"error": "unexpected Host header"})
                return False
            return True

        def do_GET(self):
            if not self._host_ok():
                return
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, PAGE_PATH.read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/info":
                self._json(HTTPStatus.OK, app.info())
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return
            # A JSON content type cannot be sent cross-site without a CORS preflight, which this server
            # never approves, so other web pages cannot make it fetch tickets.
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                return self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "send application/json"})
            origin = self.headers.get("Origin")
            if origin and origin.split("://", 1)[-1] != self.headers.get("Host"):
                return self._json(HTTPStatus.FORBIDDEN, {"error": "cross-origin request refused"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(min(length, 4 * MAX_TEXT)) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
            routes = {"/api/analyze": app.analyze_text, "/api/zendesk": app.analyze_zendesk,
                      "/api/label/next": app.label_next, "/api/label/open": app.label_open,
                      "/api/label/save": app.label_save, "/api/label/mine": app.label_mine,
                      "/api/alert-types": app.alert_types, "/api/alert-types/example": app.alert_example,
                      "/api/taxonomy": app.taxonomy, "/api/classify": app.classify_text}
            route = routes.get(self.path.split("?")[0])
            if not route:
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            try:
                self._json(HTTPStatus.OK, route(body if isinstance(body, dict) else {}))
            except ValueError as e:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            except Exception as e:  # keep the server up; report the error type only
                print(f"error: {type(e).__name__}")
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"analysis failed: {type(e).__name__}"})

    return Handler


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def allowed_host_names(host: str, port: int, extra: list[str]) -> set[str]:
    """Host headers to accept. Serving the network adds this machine's name, its fully qualified name
    and its addresses, so DBAs can use any of them; --allow-host adds others (for example a DNS
    alias). Anything else is refused, which blocks DNS-rebinding pages."""
    names = {"127.0.0.1", "localhost"}
    if not is_loopback(host):
        hostname = socket.gethostname()
        names |= {hostname.lower(), socket.getfqdn().lower()}
        try:
            names |= set(socket.gethostbyname_ex(hostname)[2])
        except OSError:
            pass
        if host not in ("0.0.0.0", "::"):
            names.add(host)
    names |= {e.lower() for e in extra}
    return {n if ":" in n else f"{n}:{port}" for n in names}


def make_app(model_path: Path, train_path: Path, port: int, *, zendesk_mode: str = "auto", network: bool = False,
             label_tickets: Path | None = None, label_sample: int = 200, label_kind: str = "human",
             label_overlap: float = 0.15, label_seed: int = 0, labels_path: Path = sl.LABELS_PATH,
             allowed_hosts: set[str] | None = None, alert_tickets: list[Path] | None = None,
             types_path: Path | None = None) -> App:
    if not model_path.exists():
        raise SystemExit(f"No model at {model_path}. Run: python -m tools.server_detector train")
    bundle = sd.load_model(model_path)
    names = client_names(read_jsonl(train_path))
    zendesk = subdomain = None
    if zendesk_mode == "on" or (zendesk_mode == "auto" and not network):
        load_env()
        try:
            zendesk = ZendeskClient.from_env()
            subdomain = zendesk.base.split("//", 1)[1].split(".", 1)[0]
        except SystemExit as e:
            print(f"Zendesk lookups disabled: {e}")
    else:
        print("Zendesk lookups are off while serving the network (use --zendesk on to allow them)")
    queue = None
    if label_tickets and label_tickets.exists():
        sample = sl.sample_tickets(read_jsonl(label_tickets), label_sample, label_seed, label_kind)
        queue = sl.LabelQueue(sample, label_overlap, labels_path)
        print(f"Labeling: {len(sample)} {label_kind} tickets from {label_tickets.name}, "
              f"{queue.progress()['overlap_tickets']} of them labeled twice; labels go to {labels_path}")
    else:
        print(f"Labeling is off: {label_tickets} was not found")
    found = [p for p in alert_tickets or [] if p.exists()]
    tickets = [t for p in found for t in read_jsonl(p)]
    catalog = alert_catalog(tickets)
    print(f"Alert types: {len(catalog)} types from {', '.join(p.name for p in found) or 'no ticket files'}")
    types = ttt.load_or_train(types_path or tt.MODEL_PATH, tickets=tickets)
    alert_map = tt.load_alert_map()
    print(f"Taxonomy {tt.TAXONOMY_VERSION}: {len(alert_map)} alert names in the alert table")
    return App(bundle, names, zendesk, subdomain, port, queue, allowed_hosts, catalog, types, alert_map)


def main():
    args = parse_args()
    network = not is_loopback(args.host)
    app = make_app(args.model_path, args.train, args.port, zendesk_mode=args.zendesk, network=network,
                   label_tickets=args.label_tickets, label_sample=args.label_sample, label_kind=args.label_kind,
                   label_overlap=args.label_overlap, label_seed=args.label_seed, labels_path=args.labels_path,
                   allowed_hosts=allowed_host_names(args.host, args.port, args.allow_host),
                   alert_tickets=args.alert_tickets)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Server detector UI at {url} (model {app.bundle.get('created_at')}, "
          f"Zendesk {'on' if app.zendesk else 'off'}). Ctrl+C to stop.")
    if network:
        shared = sorted(h for h in app.allowed_hosts if not h.startswith(("127.0.0.1", "localhost")))
        print("Serving the network. DBAs can open: " + ", ".join(f"http://{h}/" for h in shared))
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
