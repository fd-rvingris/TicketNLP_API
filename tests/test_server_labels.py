"""Hand-labeling tests with made-up tickets; no network."""

import numpy as np
import pytest

import ticket_analysis.server_detector as sd
import tools.detector_ui as ui
import tools.server_labels as sl
from ticket_analysis.masking import prepare_ticket_text
from ticket_analysis.tickets import make_ticket


def human(tid, text, subject="Help", org="100", name="Acme Corp", dw=None):
    return make_ticket(tid, subject, text, organization_id=org, organization_name=name, is_alert=0, dw_servers=dw)


TICKETS = [human(i, f"Ticket {i}: SQLA0{i % 3 + 1} is slow") for i in range(1, 21)]


# --- Masking ---------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Server=SQL01;User Id=sa;Password=Hunter2!x;Encrypt=True",
    "Use postgres://admin:Pa55w0rd@db01.corp.local:5432/app now",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
    "Meeting ID: 216 227 769\nPasscode: R3nT7mh9\nPIN=4821",
    "Nothing secret on SQL01\\REPORTS here",
])
def test_same_length_masking_hides_what_the_model_masking_hides(text):
    masked, n = sl.mask_same_length(text)
    assert len(masked) == len(text)
    _, _, expected = prepare_ticket_text({"subject": text, "description": ""})
    assert n == expected
    for secret in ("Hunter2!x", "Pa55w0rd", "eyJhbGciOiJIUzI1NiJ9", "R3nT7mh9", "4821"):
        assert secret not in masked
    for kept in ("SQL01", "db01.corp.local", "Server="):
        if kept in text:
            assert kept in masked  # offsets of everything else are unchanged


# --- Spans -----------------------------------------------------------------------


def test_normalize_span_fills_names_and_checks_offsets():
    t = human(1, r"Restart SQLA01\REPORTS and the FINANCE instance on sqla02.corp.local")
    desc = t["description"]
    span = lambda text, kind, **kw: {"field": "description", "start": desc.index(text),
                                     "end": desc.index(text) + len(text), "kind": kind, **kw}
    s = sl.normalize_span(span(r"SQLA01\REPORTS", "host_instance"), t)
    assert (s["host"], s["instance"], s["text"]) == ("sqla01", "reports", r"SQLA01\REPORTS")
    assert sl.normalize_span(span("sqla02.corp.local", "host"), t)["host"] == "sqla02"
    s = sl.normalize_span(span("FINANCE", "bare_instance", host="SQLA02"), t)
    assert (s["instance"], s["host"]) == ("finance", "sqla02")
    with pytest.raises(ValueError, match="backslash"):
        sl.normalize_span(span("FINANCE", "host_instance"), t)
    with pytest.raises(ValueError, match="outside"):
        sl.normalize_span({"field": "description", "start": 5, "end": 999, "kind": "host"}, t)
    with pytest.raises(ValueError, match="kind"):
        sl.normalize_span({**span("FINANCE", "host"), "kind": "database"}, t)


def test_normalize_row_rejects_overlaps_and_requires_a_name():
    t = human(1, "SQLA01 down")
    a = {"field": "description", "start": 0, "end": 6, "kind": "host"}
    with pytest.raises(ValueError, match="overlap"):
        sl.normalize_row({"status": "done", "spans": [a, {**a, "start": 2}]}, t, "Pat Lee")
    with pytest.raises(ValueError, match="name"):
        sl.normalize_row({"status": "done", "spans": [a]}, t, "")
    row = sl.normalize_row({"status": "skipped", "spans": [a]}, t, "Pat Lee")
    assert row["spans"] == [] and row["labeler"] == "Pat Lee"
    assert sl.clean_labeler("  Pat   Lee ") == "Pat Lee" and sl.clean_labeler(" -- ") == ""


# --- Storage and assignment ------------------------------------------------------


def test_sample_is_fixed_by_seed():
    a = sl.sample_tickets(TICKETS, 10, seed=3)
    assert [t["ticket_id"] for t in a] == [t["ticket_id"] for t in sl.sample_tickets(list(reversed(TICKETS)), 10, seed=3)]
    assert len(a) == 10


def test_saves_keep_the_latest_row_per_ticket_and_labeler(tmp_path):
    path = tmp_path / "labels.jsonl"
    t = TICKETS[0]
    sl.save_label(sl.normalize_row({"status": "skipped"}, t, "Pat"), path)
    sl.save_label(sl.normalize_row({"status": "done"}, t, "Pat"), path)
    sl.save_label(sl.normalize_row({"status": "done"}, t, "Sam"), path)
    labels = sl.load_labels(path)
    assert labels[(t["ticket_id"], "Pat")]["status"] == "done" and len(labels) == 2


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_queue_gives_each_ticket_one_label_and_overlap_tickets_two(tmp_path):
    path, clock = tmp_path / "labels.jsonl", Clock()
    q = sl.LabelQueue(TICKETS[:10], overlap=0.2, path=path, clock=clock)
    assert sum(1 for n in q.need.values() if n == 2) == 2  # every 5th ticket

    def label_all(name):
        got = []
        while (tid := q.next_for(name)) is not None:
            got.append(tid)
            sl.save_label(sl.normalize_row({"status": "done"}, q.tickets[tid], name), path)
            q.release(tid, name)
        return got

    pat = label_all("Pat")
    assert len(pat) == 10  # one labeler alone gets every ticket once
    sam = label_all("Sam")
    assert sorted(sam) == sorted(t for t, n in q.need.items() if n == 2)  # the second person gets only overlap tickets
    p = q.progress("Sam")
    assert p["labels_done"] == p["labels_needed"] == 12 and p["tickets_complete"] == 10 and p["mine"] == 2


def test_queue_reserves_a_ticket_for_whoever_got_it(tmp_path):
    clock = Clock()
    q = sl.LabelQueue(TICKETS[:3], overlap=0, path=tmp_path / "l.jsonl", clock=clock)
    first = q.next_for("Pat")
    assert q.next_for("Pat") == first  # a reload gives Pat the same ticket
    assert q.next_for("Sam") != first  # Sam is not handed Pat's ticket
    clock.now += sl.RESERVE_SECONDS + 1
    assert first in {q.next_for("Sam"), q.next_for("Lee")}  # an abandoned reservation expires


# --- Evaluation ------------------------------------------------------------------


class FixedModel(sd.ServerModel):
    def __init__(self, hosts):
        super().__init__(host_threshold=0.5, instance_threshold=0.5)
        self.fixed = set(hosts)

    def scores(self, cands):
        return np.array([[0.9 if c["host_ok"] and c["host"] in self.fixed else 0.0, 0.0] for c in cands])


def test_evaluate_against_labels_splits_known_and_new_names_and_measures_agreement(tmp_path):
    train = [human(50, "x", dw=["sqla01"])]
    bundle = {"model": FixedModel({"sqla01", "newbox9"}), "directory": sd.LocalServerDirectory.from_sources(train, None)}
    t = human(1, "sqla01 and newbox9 and oldbox4 are down")
    d = t["description"]
    mark = lambda name: {"field": "description", "start": d.index(name), "end": d.index(name) + len(name), "kind": "host"}
    path = tmp_path / "l.jsonl"
    sl.save_label(sl.normalize_row({"status": "done", "spans": [mark("sqla01"), mark("newbox9"), mark("oldbox4")]},
                                   t, "Pat"), path)
    sl.save_label(sl.normalize_row({"status": "done", "spans": [mark("sqla01"), mark("newbox9")]}, t, "Sam"), path)
    r = sl.evaluate_labels(sl.load_labels(path), {"1": t}, bundle)
    hosts = [s for s in r["spans"] if s["cls"] == "host"]
    assert len(hosts) == 3  # scored on one labeler's row per ticket
    assert [s["found"] for s in hosts if s["known"]] == [True]
    assert sorted(s["found"] for s in hosts if not s["known"]) == [False, True]
    assert all(d["correct"] for d in r["detections"])
    assert r["agreement"]["tickets"] == 1 and r["agreement"]["span_f1"] == pytest.approx(4 / 5)


# --- The page's endpoints --------------------------------------------------------


def label_app(tmp_path):
    bundle = {"model": FixedModel({"sqla01"}), "directory": sd.LocalServerDirectory.from_sources([], None)}
    q = sl.LabelQueue(TICKETS[:4], overlap=0, path=tmp_path / "l.jsonl")
    return ui.App(bundle, {}, None, None, 8765, q)


def test_label_endpoints_hand_out_mask_and_save(tmp_path):
    a = label_app(tmp_path)
    with pytest.raises(ValueError, match="name"):
        a.label_next({"labeler": " "})
    first = a.label_next({"labeler": "Pat"})
    assert first["ticket_id"] in a.queue.tickets and first["saved"] is None
    desc = first["description"]
    start = desc.index("SQLA0")
    saved = a.label_save({"labeler": "Pat", "ticket_id": first["ticket_id"], "status": "done",
                          "spans": [{"field": "description", "start": start, "end": start + 6, "kind": "host"}]})
    assert saved["spans"] == 1 and saved["progress"]["mine"] == 1
    again = a.label_open({"labeler": "Pat", "ticket_id": first["ticket_id"]})
    assert again["saved"]["spans"][0]["text"] == desc[start:start + 6]
    assert a.label_next({"labeler": "Pat"})["ticket_id"] != first["ticket_id"]
    mine = a.label_mine({"labeler": "Pat"})
    assert [t["ticket_id"] for t in mine["tickets"]] == [first["ticket_id"]]
    with pytest.raises(ValueError, match="sample"):
        a.label_open({"labeler": "Pat", "ticket_id": "nope"})


def test_label_page_masks_secrets_but_keeps_offsets(tmp_path):
    t = human(77, "Server=SQLA01;Password=Hunter2;")
    bundle = {"model": FixedModel({"sqla01"}), "directory": sd.LocalServerDirectory.from_sources([], None)}
    a = ui.App(bundle, {}, None, None, 8765, sl.LabelQueue([t], overlap=0, path=tmp_path / "l.jsonl"))
    r = a.label_next({"labeler": "Pat"})
    assert "Hunter2" not in r["description"] and len(r["description"]) == len(t["description"]) and r["masked"] == 1
    assert [s["text"] for s in r["suggestions"]] == ["SQLA01"]


def test_network_host_names_include_this_machine_and_aliases():
    local = ui.allowed_host_names("127.0.0.1", 8765, [])
    assert local == {"127.0.0.1:8765", "localhost:8765"}
    shared = ui.allowed_host_names("0.0.0.0", 8765, ["labels.corp.example"])
    assert {"127.0.0.1:8765", "labels.corp.example:8765"} <= shared and len(shared) > 2
    assert ui.is_loopback("127.0.0.1") and not ui.is_loopback("0.0.0.0")
