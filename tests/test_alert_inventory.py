from tests.test_alert_layouts import LABELED, LABELED_V2, MARKDOWN
from tests.test_alert_parser import TABLE
from tools.alert_layouts import Vocabulary, group_layouts, inventory, layout_signature, same_layout


def tickets():
    out = []
    for i in range(10):  # ten organizations, so the template words pass the eight-organization rule
        out.append({"ticket_id": f"a{i}", "is_alert": True, "organization_id": i, "organization_name": f"Fake Org{i}",
                    "subject": f"Alert #{i} - FD - SQL Agent Job Failure", "description": LABELED,
                    "dw_servers": ["FAKESQL01"]})
        out.append({"ticket_id": f"b{i}", "is_alert": True, "organization_id": i, "organization_name": f"Fake Org{i}",
                    "subject": f"OPEN - Alert #{i} - Blocking Alert", "description": LABELED_V2})
    out.append({"ticket_id": "t", "is_alert": True, "organization_id": 1, "organization_name": "Fake Org1",
                "subject": "", "description": TABLE})
    out.append({"ticket_id": "h", "is_alert": False, "organization_id": 2, "organization_name": "Fake Org2",
                "subject": "Help", "description": "Hi Robin,\nThe server is slow.\nThanks,\nSam"})
    return out


def test_mask_keeps_template_words_only():
    v = Vocabulary(tickets())
    assert v.mask("Server FAKESQL01") == "Server L7 D2"
    assert v.mask("Client Name Fake Co") == "Client Name L4 L2"  # organization name words are never kept
    assert not v.keep("robin") and not v.keep("sam")  # greetings and sign-offs
    assert v.keep("severity") and v.keep("warning")


def test_signature_ignores_values_and_merges_variants():
    v = Vocabulary(tickets())
    subj, labels = layout_signature(tickets()[0], v)
    assert subj == "Alert #D" and labels[:4] == ("AlertId", "Alert", "Client Name", "Server")
    assert layout_signature(tickets()[2], v)[1] == labels  # same template, other values
    _, v2 = layout_signature(tickets()[1], v)
    assert not same_layout(labels, v2)  # the newer collector version is its own layout
    assert same_layout(labels, tuple(x for x in labels if x != "Alias"))  # one optional line
    _, md = layout_signature({"subject": "", "description": MARKDOWN}, v)
    assert md == ("* *Label*: value pairs on one line",)
    groups = group_layouts(__import__("collections").Counter({labels: 5, v2: 4, labels[:-1]: 1}))
    assert len(groups) == 2 and groups[0] == [labels, labels[:-1]]


def test_inventory_counts_and_masked_examples():
    inv = inventory(tickets(), Vocabulary(tickets()))
    assert inv["alerts"] == 21 and inv["humans"] == 1 and inv["human_hits"] == 0
    sizes = sorted(lay["alerts"] for lay in inv["layouts"])
    assert sizes == [1, 10, 10]
    text = "\n".join(ex for lay in inv["layouts"] for ex in lay["examples"])
    assert "FAKESQL01" not in text and "Fake Co" not in text and "Q9ZZPAP07K" not in text
