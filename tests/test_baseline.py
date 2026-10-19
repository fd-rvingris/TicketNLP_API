import pytest

from ticket_analysis.tickets import make_ticket
from tools.baseline import (OTHER, kept_classes, merge_label, redundancy, run_target, score, segment_masks, subsample,
                            top_confusions, weak_label)

BACKUP = "backup job failed on the nightly full backup, restore test needed"
DISK = "disk space low on the data drive, volume capacity alert"
LOGIN = "user cannot log in, please grant access to the reporting database"


def ticket(tid, text, *, category, ztype="incident", sub="Incident", alert=False):
    return make_ticket(tid, text.split(",")[0], text, is_alert=alert,
                       weak_labels={"zendesk_type": ztype, "sub type": sub, "category": category})


def synthetic(n_per_class, start=0):
    tickets = []
    for i in range(n_per_class):
        tickets.append(ticket(f"b{start + i}", f"{BACKUP} {i}", category="Backups", alert=True))
        tickets.append(ticket(f"d{start + i}", f"{DISK} {i}", category="Storage", alert=True))
        tickets.append(ticket(f"l{start + i}", f"{LOGIN} {i}", category="Security", ztype="task",
                              sub="Service Request"))
    return tickets


def test_weak_label_blank_is_none():
    t = make_ticket("1", "s", "d", weak_labels={"category": "  Backups "})
    assert weak_label(t, "category") == "Backups"
    assert weak_label(t, "sub type") is None


def test_rare_classes_merge_into_other():
    kept = kept_classes(["a"] * 20 + ["b"] * 19 + ["c"] * 25, min_count=20)
    assert kept == {"a", "c"}
    assert merge_label("b", kept) == OTHER
    assert merge_label("never seen", kept) == OTHER
    assert merge_label("a", kept) == "a"


def test_redundancy():
    tickets = ([make_ticket(str(i), "", "", weak_labels={"zendesk_type": "incident", "sub type": "Incident"})
                for i in range(9)]
               + [make_ticket("9", "", "", weak_labels={"zendesk_type": "incident", "sub type": "Service Request"}),
                  make_ticket("10", "", "", weak_labels={"zendesk_type": "task"})])
    r = redundancy(tickets, "zendesk_type", "sub type")
    assert r["with_both"] == 10 and r["missing_b"] == 1
    assert r["agreement"] == pytest.approx(0.9)
    assert r["mapping"] == {"incident": "Incident"}


def test_subsample_is_stratified_and_repeatable():
    labels = ["a"] * 80 + ["b"] * 20
    idx = subsample(100, 50, labels, seed=1)
    assert len(idx) == 50 and len(set(idx)) == 50
    assert sum(1 for i in idx if labels[i] == "b") == 10
    assert list(idx) == list(subsample(100, 50, labels, seed=1))
    assert len(subsample(100, 500, labels, seed=1)) == 100


def test_subsample_falls_back_when_a_class_is_too_small():
    labels = ["a"] * 99 + ["b"]
    assert len(subsample(100, 10, labels, seed=0)) == 10


def test_score_splits_alerts_and_human():
    tickets = [make_ticket(str(i), "", "", is_alert=a) for i, a in enumerate([True, True, False, False, None])]
    masks = segment_masks(tickets)
    s = score(["x", "x", "y", "y", "y"], ["x", "y", "y", "y", "x"], masks)
    assert s["all"]["n"] == 5 and s["all"]["accuracy"] == pytest.approx(0.6)
    assert s["alerts"]["n"] == 2 and s["alerts"]["accuracy"] == pytest.approx(0.5)
    assert s["human"]["n"] == 2 and s["human"]["accuracy"] == pytest.approx(1.0)


def test_score_empty_segment():
    tickets = [make_ticket("1", "", "", is_alert=False)]
    s = score(["x"], ["x"], segment_masks(tickets))
    assert s["alerts"] == {"n": 0, "accuracy": None, "macro_f1": None}


def test_top_confusions_ignores_correct_predictions():
    assert top_confusions(["a", "a", "b", "b"], ["a", "b", "a", "a"]) == [("b", "a", 2), ("a", "b", 1)]


def test_run_target_learns_separable_classes():
    train, test = synthetic(30), synthetic(5, start=100)
    test.append(make_ticket("x", "no label", "no label", weak_labels={}))
    train += [ticket(f"r{i}", f"{LOGIN} rare {i}", category="Rare") for i in range(3)]
    r = run_target("category", train, test, min_count=20, sizes=[30], max_chars=8000, seed=0)
    assert r["classes_kept"] == ["Backups", "Security", "Storage"]
    assert r["merged_values"] == 1 and r["merged_train_tickets"] == 3
    assert r["test_tickets"] == 15 and r["test_missing_label"] == 1
    assert [p["size"] for p in r["curve"]] == [30, 93]
    final = r["curve"][-1]
    assert final["all"]["accuracy"] == pytest.approx(1.0)
    assert final["alerts"]["n"] == 10 and final["human"]["n"] == 5
    assert {c["class"] for c in r["per_class"]} == {"Backups", "Security", "Storage"}
    assert set(r["per_class_by_segment"]) == {"alerts", "human"}


def test_run_labeled_cross_validates_label_file_classes():
    from tools.baseline import run_labeled
    tickets = synthetic(15)
    texts = {"b": ("backup_restore", "fix_incident"), "d": ("capacity_storage", "fix_incident"),
             "l": ("access_security", "request_change")}
    labels = {t["ticket_id"]: {"ticket_id": t["ticket_id"], "primary_type": texts[t["ticket_id"][0]][0],
                               "intent": texts[t["ticket_id"][0]][1], "confidence": "high"} for t in tickets}
    labels["b0"]["primary_type"] = "other"  # a single-ticket class is merged
    labels["extra"] = {"ticket_id": "extra", "primary_type": "performance", "intent": "fix_incident"}  # no ticket
    r = run_labeled("primary_type", tickets, labels, min_count=5, folds=3, max_chars=1000, seed=0)
    assert r["tickets"] == len(tickets) and r["merged_values"] == ["other"] and r["merged_tickets"] == 1
    assert r["model"]["all"]["accuracy"] > 0.9 and r["model"]["medium or low"]["n"] == 0
