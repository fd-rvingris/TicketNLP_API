"""Server detection for one piece of text, shaped for the detector UI and the API: the ticket built from
pasted text, the client it resolves to, the detections, and every scored candidate."""

from __future__ import annotations

from . import server_detector as sd
from .alert_parser import alert_spans
from .jsonl import now_iso
from .tickets import make_ticket

MAX_TEXT = 200_000  # characters accepted for one ticket
# The client key of a ticket that names no client. It matches no directory entry, so no client's known
# names apply; without it, a ticket with no client is looked up against every client's names.
NO_CLIENT = "no-client"


def ticket_for_text(subject: str, text: str, client: str | None, names: dict[str, str]) -> dict:
    """A ticket for pasted text. The client key (a Zendesk organization id or 'name:<name>') decides
    which client's known names are used; none means no client."""
    org = client if client and not client.startswith("name:") else None
    name = names.get(client) if client else None
    if client and client.startswith("name:") and not name:
        name = client[5:]
    return make_ticket("pasted", subject, text, organization_id=org, organization_name=name, source="pasted")


def ticket_for_org(subject: str, text: str, zendesk_org_id: str | None) -> dict:
    """A ticket for the API: the Zendesk organization id is the client key, and a missing id is
    NO_CLIENT, so only that organization's known names ever apply."""
    return ticket_for_text(subject, text, zendesk_org_id or NO_CLIENT, {})


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


def detect(ticket: dict, model: sd.ServerModel, directory, *, host_threshold=None, instance_threshold=None) -> dict:
    """The detections in one ticket with the client they were resolved against and the thresholds used
    (the model's own unless given)."""
    th_h = _threshold(host_threshold, model.host_threshold)
    th_i = _threshold(instance_threshold, model.instance_threshold)
    client = directory.client_for(ticket)
    known = client in set(directory.clients())
    return {
        "is_alert": sd.is_alert_ticket(ticket),
        "client": {"key": client, "in_directory": known,
                   "hosts": len(directory.hosts(client)) if known else 0,
                   "instances": len(directory.instances(client)) if known else 0},
        "thresholds": {"host": th_h, "instance": th_i},
        "detections": sd.detect_servers(ticket, directory, model, host_threshold=th_h, instance_threshold=th_i),
    }


def analyze(ticket: dict, bundle: dict, names: dict[str, str], *, host_threshold=None,
            instance_threshold=None) -> dict:
    """detect() with the bundle's model and directory, plus what the detector UI shows: the ticket, the
    client's display name, the parsed alert fields and where they sit, and every scored candidate."""
    model, directory = bundle["model"], bundle["directory"]
    result = detect(ticket, model, directory, host_threshold=host_threshold, instance_threshold=instance_threshold)
    client = result["client"]
    return {
        "ticket": {k: ticket.get(k) for k in ("ticket_id", "subject", "description", "organization_id",
                                              "organization_name", "created_at", "source")},
        "is_alert": result["is_alert"],
        "client": {"key": client["key"], "name": names.get(client["key"]) or ticket.get("organization_name"),
                   "in_directory": client["in_directory"], "hosts": client["hosts"], "instances": client["instances"]},
        "thresholds": result["thresholds"],
        "detections": result["detections"],
        "alert_fields": ticket.get("alert_fields"),
        # Where each alert value sits in the subject or description, for highlighting (every layout)
        "alert_spans": alert_spans(ticket.get("subject") or "", ticket.get("description") or ""),
        "candidates": scored_candidates(ticket, directory, model),
        "max_chars": model.max_chars,
        "analyzed_at": now_iso(),
    }
