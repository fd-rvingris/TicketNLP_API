"""Training-side directory construction: from tickets and the CMDB export, and copies with names held out."""

import ticket_analysis.server_detector as sd
from ticket_analysis.tickets import make_ticket
from tools.directory_build import build_directory, without_names

CSV = ("server_name,environment,zendesk_organization_id,client_name\n"
       "FAKESQL01\\REPORTS,Production,999001,Fake Co\n"
       "not a server name!,Production,999001,Fake Co\n")


def alert(tid, org, server):
    t = make_ticket(tid, "Alert", "", organization_id=org, organization_name="Fake Co", is_alert=1)
    t["alert_fields"] = {"server": server, "environment": "UAT"}
    return t


def test_build_directory_reads_dw_names_alert_fields_and_the_cmdb_export(tmp_path):
    path = tmp_path / "instances.csv"
    path.write_text(CSV, encoding="utf-8")
    dw = make_ticket(1, "Help", "", organization_id="999001", organization_name="Fake Co", is_alert=0,
                     dw_servers=["FAKESQL02"])
    d = build_directory([dw, alert(2, "999001", "FAKESQL03")], path)
    assert sorted(d.hosts("999001")) == ["fakesql01", "fakesql02", "fakesql03"]
    assert sorted(d.confirmed_hosts("999001")) == ["fakesql01", "fakesql03"]  # dw_servers alone is not confirmed
    assert d.instances("999001") == [("fakesql01", "reports")]
    assert d.environment("fakesql03", "999001") == "UAT" and d.skipped == 1
    assert d.client_for({"organization_name": "Fake Co"}) == "999001"  # the name is an alias of the id


def test_build_directory_without_an_export_uses_the_tickets_alone(tmp_path):
    d = build_directory([alert(1, "999001", "FAKESQL03")], tmp_path / "missing.csv")
    assert d.hosts("999001") == ["fakesql03"]


def test_without_names_drops_hosts_with_their_pairs_and_instance_names():
    d = sd.LocalServerDirectory()
    for name, source in (("fakesql01\\reports", "cmdb"), ("fakesql02\\finance", "cmdb"), ("fakesql03", "dw")):
        d.add("999001", "name:fake co", sd.normalize_reference(name), source, "Production")
    kept = without_names(d, hosts={"fakesql01"}, instances={"finance"})
    assert sorted(kept.hosts("999001")) == ["fakesql02", "fakesql03"]
    assert kept.instances("999001") == []
    assert kept.confirmed_hosts("999001") == ["fakesql02"]
    assert kept.environment("fakesql02", "999001") == "Production"
    assert kept.client_for({"organization_name": "Fake Co"}) == "999001"
    assert sorted(d.hosts("999001")) == ["fakesql01", "fakesql02", "fakesql03"]  # the original is unchanged


def test_the_runtime_directory_has_no_training_helpers():
    for name in ("from_sources", "add_ticket", "add_instances_csv", "without"):
        assert not hasattr(sd.LocalServerDirectory, name)
