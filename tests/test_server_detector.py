"""Server detector tests. All tickets, clients, hosts and instances are made up."""

import random

import pytest

import ticket_analysis.server_detector as sd
import tools.server_detector as sdt
from ticket_analysis.tickets import make_ticket


def human(tid, text, org="100", name="Acme", dw=None, subject="Help"):
    return make_ticket(tid, subject, text, organization_id=org, organization_name=name, is_alert=0, dw_servers=dw)


def know_for(tickets, stop=sd.BUILTIN_STOP):
    d = sd.LocalServerDirectory.from_sources(tickets, None)
    return d, sd.Knowledge(d, stop)


def cands_by_text(ticket, know, **kw):
    return {c["text"]: c for c in sd.ticket_candidates(ticket, know, **kw)}


# --- Tokenization ----------------------------------------------------------------


def parts(raw):
    return [(raw[s:e], sorted(f)) for s, e, f in sd.token_parts(raw, 0)]


def test_token_parts_keep_server_forms_together():
    assert parts("SQLPRD01\\REPORTS") == [("SQLPRD01\\REPORTS", [])]
    assert parts("db01.corp.local") == [("db01.corp.local", [])]
    assert parts("host01,1433") == [("host01,1433", ["port"])]
    assert parts("tcp:host01,1433") == [("host01,1433", ["port"])]
    assert parts("MSSQL$REPORTS") == [("REPORTS", ["service"])]
    assert parts("SQLAgent$FINANCE") == [("FINANCE", ["service"])]
    assert parts("\\\\fileserver01\\backups") == [("fileserver01", ["unc"])]
    assert parts("Server:SQLPRD01") == [("Server", []), ("SQLPRD01", [])]
    assert parts("10:30") == []
    assert parts("D:\\Data\\MSSQL\\Log") == [("D", []), ("Data", []), ("MSSQL", []), ("Log", [])]


def test_tokenize_trims_punctuation():
    text = "Is SQLPRD01\\REPORTS, or db01.corp.local. down?"
    assert [t[0] for t in sd.tokenize(text)] == ["Is", "SQLPRD01\\REPORTS", "or", "db01.corp.local", "down"]
    for raw, s, e in sd.tokenize(text):
        assert text[s:e] == raw


def test_connection_string_and_unc_values_are_flagged():
    _, know = know_for([])
    t = human(1, "Connection: Data Source=appsql07\\SALES;Initial Catalog=x; copy to \\\\nas04\\dumps")
    c = cands_by_text(t, know)
    assert c["appsql07\\SALES"]["in_conn"] and c["appsql07\\SALES"]["kind"] == "host_instance"
    assert c["nas04"]["in_unc"] and c["nas04"]["host_ok"]


def test_fqdn_and_port_normalize_to_host():
    _, know = know_for([])
    c = cands_by_text(human(1, "Connect to db01.corp.local and tcp:sqlx22,1433 today"), know)
    assert c["db01.corp.local"]["host"] == "db01"
    assert c["sqlx22,1433"]["host"] == "sqlx22" and c["sqlx22,1433"]["has_port"]


# --- Candidate rules -------------------------------------------------------------


def test_host_candidate_rules():
    _, know = know_for([])
    t = human(1, "Please check KB5012345 and INC0012345 on 2026-10-01 for error.log, mail bob@corp01.com, host appsrv9")
    c = cands_by_text(t, know)
    assert c["appsrv9"]["host_ok"]
    for noise in ("KB5012345", "INC0012345", "Please", "check"):
        assert noise not in c or not c[noise]["host_ok"], noise
    assert "error.log" not in c or not c["error.log"]["host_ok"]
    assert "corp01.com" not in c or not c["corp01.com"]["host_ok"]


def test_instance_names_that_are_common_words_survive_next_to_a_cue():
    _, know = know_for([])
    c = cands_by_text(human(1, "The reports instance on that box is slow"), know)
    assert c["reports"]["inst_ok"] and not c["reports"]["host_ok"]  # a stop-list word cannot be a host
    c = cands_by_text(human(2, "The reports were slow"), know)
    assert "reports" not in c


def test_instance_candidates_by_shape_vocabulary_and_service_suffix():
    d, know = know_for([human(1, "x", dw=["sqlp01\\finance"])])
    c = cands_by_text(human(2, "Check finance and PRD01 and MSSQL$HR"), know)
    assert c["finance"]["inst_ok"]  # a known instance name
    assert c["PRD01"]["inst_ok"]  # upper case with digits
    assert c["HR"]["inst_ok"] and c["HR"]["service"]


# --- Directory -------------------------------------------------------------------


def directory_fixture():
    tickets = [
        human(1, "x", dw=["SQLA01\\REPORTS", "SQLA02\\REPORTS", "SQLA03"]),
        human(2, "x", org="200", name="Bolt", dw=["bolt-db1\\PROD", "bolt-db2\\MSSQLSERVER"]),
        make_ticket(3, "Alert #1", "Server SQLA04\nInstance SQLA04\\FINANCE", organization_id="100",
                    organization_name="Acme", is_alert=1),
        make_ticket(4, "Alert #2", "Instance Job: nightly", organization_id="100", organization_name="Acme", is_alert=1),
        human(5, "x", org=None, name="Cobalt", dw=["cob01\\PROD"]),
    ]
    return sd.LocalServerDirectory.from_sources(tickets, None)


def test_directory_splits_host_instance_and_counts_vocabulary():
    d = directory_fixture()
    assert d.hosts("100") == ["sqla01", "sqla02", "sqla03", "sqla04"]
    assert ("sqla04", "finance") in d.instances("100")
    assert d.hosts("200") == ["bolt-db1", "bolt-db2"]  # MSSQLSERVER is stored as the host alone
    assert d.instance_names() == {"reports": 2, "finance": 1, "prod": 2}
    assert d.instance_clients()["prod"] == 2
    assert d.client_for({"organization_id": None, "organization_name": "Cobalt"}) == "name:cobalt"
    assert d.client_for({"organization_id": "999", "organization_name": "acme"}) == "100"  # name alias
    s = d.stats()
    assert s["clients"] == 3 and s["names_on_several_hosts"] == 2 and s["names_at_several_clients"] == 1


def test_lookup_statuses():
    d = directory_fixture()
    assert d.lookup("sqla01", None, "100")["status"] == "known"
    assert d.lookup("sqla01", "reports", "100")["status"] == "known"
    assert d.lookup("sqla03", "reports", "100")["status"] == "new_instance_on_known_host"
    assert d.lookup("sqla09", "reports", "100")["status"] == "unknown_host"
    assert d.lookup(None, "reports", "100") == {"status": "ambiguous", "hosts": ["sqla01", "sqla02"]}
    assert d.lookup(None, "finance", "100") == {"status": "known", "hosts": ["sqla04"]}
    assert d.lookup(None, "hr", "100")["status"] == "unknown_instance"
    assert d.lookup("bolt-db2", "mssqlserver", "200")["status"] == "known"


def test_cmdb_api_directory_is_a_stub():
    with pytest.raises(NotImplementedError):
        sd.CmdbApiDirectory().hosts()


def test_normalize_reference_rejects_non_servers():
    assert sd.normalize_reference("Job: nightly backup") is None
    assert sd.normalize_reference("node07:9100").host == "node07"
    assert sd.normalize_reference("tcp:SQL01.corp.local,1433").host == "sql01"
    assert sd.normalize_reference("SQL01\\MSSQLSERVER").instance is None


# --- Profiles, siblings and typos ------------------------------------------------


def test_compress_and_profiles():
    assert sd.compress("aautdb01") == "L6 D2"
    assert sd.compress("sql-prd-01") == "L3 - L3 - D2"
    p = sd.NameProfile(["aautdb01", "aautdb02", "aautweb01"])
    assert "aaut" in p.prefixes and "L6 D2" in p.patterns


def test_sibling_and_typo_classification():
    known = {"aautdb01", "aautdb02"}
    assert sd.near_match("aautdb03", known) == ("sibling", "aautdb01")
    assert sd.near_match("aautbd01", known) == ("typo", "aautdb01")  # swapped letters count as one edit
    assert sd.near_match("aautdb01", known) == ("known", "aautdb01")
    assert sd.near_match("zzzz99", known) == (None, None)


def test_profile_features_use_own_client_only():
    d = sd.LocalServerDirectory.from_sources(
        [human(1, "x", dw=["aautdb01", "aautdb02", "aautdb05"]),
         human(2, "x", org="200", name="Bolt", dw=["blt-sql-01", "blt-sql-02", "blt-sql-03"])], None)
    know = sd.Knowledge(d, sd.BUILTIN_STOP)
    own = know.profile_features("100", "host", "aautdb07")
    assert own["h_own_prefix"] == 1.0 and own["h_own_pattern"] == 1.0 and own["h_other_conv"] == 0.0
    other = know.profile_features("100", "host", "blt-sql-09")
    assert other["h_own_prefix"] == 0.0 and other["h_other_conv"] == 1.0


# --- Pairing ---------------------------------------------------------------------


class FixedModel(sd.ServerModel):
    """Scores fixed by name, so pairing and status rules can be tested without fitting."""

    def __init__(self, hosts, instances):
        super().__init__(host_threshold=0.5, instance_threshold=0.5)
        self.fixed_hosts, self.fixed_instances = set(hosts), set(instances)

    def scores(self, cands):
        import numpy as np
        return np.array([[1.0 if c["host_ok"] and c["host"] in self.fixed_hosts else 0.0,
                          1.0 if c["inst_ok"] and c["host"] in self.fixed_instances else 0.0] for c in cands])


def pairing_directory():
    return sd.LocalServerDirectory.from_sources([
        human(1, "x", dw=["SQLA01\\REPORTS", "SQLA02\\REPORTS", "SQLA03\\FINANCE", "SQLA05"]),
    ], None)


def by_text(found):
    return {d["text"]: d for d in found}


def test_pairing_prefers_nearest_host_in_ticket():
    d = pairing_directory()
    model = FixedModel({"sqla01", "sqla02"}, {"reports"})
    t = human(9, "Server SQLA02 is fine. The REPORTS instance on SQLA01 is down.")
    found = by_text(sd.detect_servers(t, d, model))
    assert found["REPORTS"]["host"] == "sqla01" and found["REPORTS"]["status"] == "known"
    assert found["SQLA02"]["kind"] == "host" and found["SQLA02"]["status"] == "known"


def test_pairing_with_known_host_without_instances_is_a_new_instance():
    model = FixedModel({"sqla05"}, {"hr"})
    found = by_text(sd.detect_servers(human(9, "Create the HR instance on SQLA05"), pairing_directory(), model))
    assert found["HR"]["host"] == "sqla05" and found["HR"]["status"] == "new_instance_on_known_host"


def test_pairing_single_client_host_resolves():
    model = FixedModel(set(), {"finance"})
    found = by_text(sd.detect_servers(human(9, "FINANCE instance is slow"), pairing_directory(), model))
    assert found["FINANCE"]["host"] == "sqla03" and found["FINANCE"]["status"] == "known"


def test_pairing_several_client_hosts_is_ambiguous():
    model = FixedModel(set(), {"reports"})
    found = by_text(sd.detect_servers(human(9, "REPORTS instance is slow"), pairing_directory(), model))
    assert found["REPORTS"]["status"] == "ambiguous" and found["REPORTS"]["candidates"] == ["sqla01", "sqla02"]
    assert found["REPORTS"]["host"] is None


def test_pairing_unknown_instance_is_unpaired():
    model = FixedModel(set(), {"payroll"})
    found = by_text(sd.detect_servers(human(9, "PAYROLL instance is slow"), pairing_directory(), model))
    assert found["PAYROLL"]["host"] is None and found["PAYROLL"]["status"] == "possible_new_instance"


def test_host_statuses_typo_sibling_and_new_instance():
    model = FixedModel({"sqla01", "sqal01", "sqla09"}, set())
    t = human(9, "Hosts SQAL01 and SQLA09, and SQLA01\\PAYROLL plus SQLA01\\REPORTS")
    found = by_text(sd.detect_servers(t, pairing_directory(), model))
    assert found["SQAL01"]["status"] == "typo_of_known" and found["SQAL01"]["typo_of"] == "sqla01"
    assert found["SQLA09"]["status"] == "possible_new_host" and found["SQLA09"]["sibling_of"] == "sqla01"
    assert found["SQLA01\\PAYROLL"]["status"] == "new_instance_on_known_host"
    assert found["SQLA01\\REPORTS"]["status"] == "known" and found["SQLA01\\REPORTS"]["kind"] == "host_instance"
    d = found["SQLA09"]
    assert d["field"] == "description" and t["description"][d["start"]:d["end"]] == "SQLA09"


# --- Grouped holdouts --------------------------------------------------------------


def synthetic_tickets(n=120, seed=1):
    """Made-up human tickets for two clients. Hosts are mentioned as HOST\\INSTANCE or alone, and each
    ticket lists its servers in dw_servers, so labels come from the data, as on the real exports."""
    rng = random.Random(seed)
    clients = [("100", "Acme", "acmsql", ["REPORTS", "FINANCE", "HR"]),
               ("200", "Bolt", "bltdb", ["SALES", "STAGING", "REPORTS"])]
    fillers = ["Please look at", "We see slowness on", "Can you check", "Backups failed for", "Patch"]
    tickets = []
    for i in range(n):
        org, name, prefix, insts = clients[i % 2]
        host = f"{prefix}{rng.randint(1, 30):02d}"
        inst = rng.choice(insts)
        if i % 3:
            text = f"{rng.choice(fillers)} {host}\\{inst} today, thanks."
            dw = [f"{host}\\{inst}"]
        else:
            text = f"{rng.choice(fillers)} {host} today, it is slow."
            dw = [host]
        tickets.append(human(i, text, org=org, name=name, dw=dw))
    return tickets


def test_grouped_holdouts_remove_names_from_training_and_directory():
    tickets = synthetic_tickets()
    d = sd.LocalServerDirectory.from_sources(tickets, None)
    held_hosts = sdt.holdout_names(d.hosts(), 0.2, seed=3)
    held_inst = sdt.holdout_names(d.instance_names(), 0.34, seed=3)
    assert held_hosts and held_inst
    dh, di = d.without(hosts=held_hosts), d.without(instances=held_inst)
    assert not held_hosts & set(dh.hosts()) and not {h for h, _ in dh.instances()} & held_hosts
    assert not held_inst & set(di.instance_names())
    ex_h, _ = sdt.make_examples(tickets, sd.Knowledge(dh, sd.BUILTIN_STOP), dh, alert_context="strip_labels",
                               exclude_hosts=held_hosts, neg_ratio=None)
    assert not any(c["host"] in held_hosts for c in ex_h)
    ex_i, _ = sdt.make_examples(tickets, sd.Knowledge(di, sd.BUILTIN_STOP), di, alert_context="strip_labels",
                               exclude_instances=held_inst, neg_ratio=None)
    assert not any(c["instance"] in held_inst or (c["kind"] == "token" and c["host"] in held_inst) for c in ex_i)


def test_holdout_names_is_seeded():
    names = [f"h{i}" for i in range(50)]
    assert sdt.holdout_names(names, 0.2, 7) == sdt.holdout_names(names, 0.2, 7)
    assert len(sdt.holdout_names(names, 0.2, 7)) == 10


# --- End to end ----------------------------------------------------------------------


def test_synthetic_examples_teach_bare_instances():
    tickets = synthetic_tickets()
    d = sd.LocalServerDirectory.from_sources(tickets, None)
    _, counts = sdt.make_examples(tickets, sd.Knowledge(d, sd.BUILTIN_STOP), d, alert_context="strip_labels",
                                 neg_ratio=None)
    assert counts["before_sampling"][("instance", "synthetic")] > 0
    assert counts["before_sampling"].get(("instance", "human"), 0) == 0  # no natural bare mentions in training


def test_end_to_end_finds_a_bare_instance_seen_only_inside_host_instance():
    tickets = synthetic_tickets(n=180)
    d = sd.LocalServerDirectory.from_sources(tickets, None)
    model, counts = sdt.fit_config(tickets, d, kind="logreg", alert_context="strip_labels", instance_model=False,
                                  seed=0, max_chars=5000, neg_ratio=10)
    assert counts["examples"] > 0
    t = human(999, "The FINANCE instance is down again, please look.", subject="Outage")
    found = by_text(sd.detect_servers(t, d, model, host_threshold=0.5, instance_threshold=0.3))
    assert "FINANCE" in found, found
    f = found["FINANCE"]
    assert f["kind"] == "bare_instance" and f["instance"] == "finance"
    assert f["status"] in ("ambiguous", "known")  # several Acme hosts carry FINANCE in the made-up data
    assert "please" not in {k.lower() for k in found}


def test_profile_and_vocabulary_features_leave_the_name_itself_out():
    d = sd.LocalServerDirectory.from_sources(
        [human(1, "x", dw=[r"aautdb01\SALES", "aautdb02", "zz9"]),
         human(2, "x", org="200", name="Bolt", dw=[r"blt-sql-01\SALES"])], None)
    know = sd.Knowledge(d, sd.BUILTIN_STOP)
    f = know.profile_features("100", "host", "aautdb01")
    assert 0 < f["h_sim"] < 1  # its own entry is not a match; aautdb02 is one edit away
    assert know.profile_features(None, "host", "aautdb01")["h_sim"] == 0.0  # no client: no information
    assert know.profile_features("100", "host", "zz9")["h_own_prefix"] == 0.0  # no other name shares its prefix
    assert know.instance_vocabulary("100", "sales") == (1, 1)  # only Bolt's use counts for an Acme ticket


def test_model_saved_by_the_script_loads_in_another_program(tmp_path):
    """Running the training command must save the package's classes, not __main__.ServerModel."""
    import json
    import subprocess
    import sys
    from pathlib import Path
    train = tmp_path / "train.jsonl"
    train.write_text("".join(json.dumps(t) + "\n" for t in synthetic_tickets(n=150)), encoding="utf-8")
    model_path = tmp_path / "m.joblib"
    subprocess.run([sys.executable, "-m", "tools.server_detector", "train", "--train", str(train), "--model-path",
                    str(model_path)], capture_output=True, text=True, check=True, cwd=Path(__file__).parent.parent)
    load = ("import joblib, sys; b = joblib.load(sys.argv[1]); "
            "print(type(b['model']).__module__, type(b['model']).__name__)")
    out = subprocess.run([sys.executable, "-c", load, str(model_path)], capture_output=True, text=True, check=True)
    assert out.stdout.split() == ["ticket_analysis.server_detector", "ServerModel"]


def test_client_dropout_hides_client_features_for_a_share_of_tickets():
    tickets = synthetic_tickets(n=100)
    d = sd.LocalServerDirectory.from_sources(tickets, None)
    know = sd.Knowledge(d, sd.BUILTIN_STOP)
    _, none = sdt.make_examples(tickets, know, d, alert_context="strip_labels", neg_ratio=None, client_dropout=0.0)
    ex, half = sdt.make_examples(tickets, know, d, alert_context="strip_labels", neg_ratio=None, client_dropout=0.5)
    assert none["client_hidden_tickets"] == 0 and 30 < half["client_hidden_tickets"] < 70
    assert {c["dense"]["client_known"] for c in ex} == {0.0, 1.0}
    assert {c["label"] for c in ex} >= {"host", "instance"}  # labels still use the client


class _AllNone:
    def proba(self, cands):
        import numpy as np
        return {"host": np.zeros(len(cands)), "instance": np.zeros(len(cands)), "none": np.ones(len(cands))}


def rules_only_model():
    """A model that scores every candidate as none, so only the rules can detect anything."""
    m = sd.ServerModel(host_threshold=0.5, instance_threshold=0.5)
    m.main, m.instance = _AllNone(), None
    return m


@pytest.mark.parametrize("text, expected", [
    ("The REPORTS instance is down", {"REPORTS": ("bare_instance", "instance:name_before_instance")}),
    ("the prod instance is slow", {"prod": ("bare_instance", "instance:name_before_instance")}),
    ("Restart the instance named FINANCE please", {"FINANCE": ("bare_instance", "instance:name_after_instance")}),
    ("Check service MSSQL$PAYROLL now", {"PAYROLL": ("bare_instance", "instance:service_suffix")}),
    ("Failover on instance abc-sql01 today", {"abc-sql01": ("host", "host:after_server_word")}),
    ("Connect to dbfake7:1433 now", {"dbfake7": ("host", "host:port")}),
    ("Connect to tcp:dbfake8 now", {"dbfake8": ("host", "host:port")}),
    ("Connect to mysqlfake4:3306 now", {"mysqlfake4": ("host", "host:port")}),
    ("psql to pgfake5:5432 failed", {"pgfake5": ("host", "host:port")}),
    ("listener on orafake6:1521 is down", {"orafake6": ("host", "host:port")}),
    ("exporter fakemaria2:9104 is not scraping", {"fakemaria2": ("host", "host:port")}),
    ("Open webfake3:8080 now", {}),  # a web port is not a database server
    ("Connect with SID=ORAFAKE1 please", {"ORAFAKE1": ("bare_instance", "instance:oracle")}),
    ("export ORACLE_SID=finfake2", {"finfake2": ("bare_instance", "instance:oracle")}),
    ("(ADDRESS=(PROTOCOL=TCP)(HOST=orafake7)(PORT=1521))(CONNECT_DATA=(SERVICE_NAME = salespdb.corp.local))",
     {"orafake7": ("host", "host:after_server_word"), "salespdb.corp.local": ("bare_instance", "instance:oracle")}),
    ("sqlplus scott@orafake8:1521/FINPDB", {"orafake8": ("host", "host:port"), "FINPDB": ("bare_instance", "instance:oracle")}),
    ("jdbc:oracle:thin:@orafake9:1521:ORCLX", {"orafake9": ("host", "host:port"), "ORCLX": ("bare_instance", "instance:oracle")}),
    ("The Oracle SID PRODFIN1 is down", {"PRODFIN1": ("bare_instance", "instance:oracle")}),
    ("Oracle: the HRPRD database will not open", {"HRPRD": ("bare_instance", "instance:oracle")}),
    ("The SID PRODFIN1 is down", {}),  # prose counts only in a ticket that mentions Oracle
    ("SQL Server: the SALESDB database is full", {}),
    ("Oracle login failed for Windows SID S-1-5-21-1004", {}),
    ("Failover on instance db01.corp.local today", {"db01.corp.local": ("host", "host:after_server_word")}),
    (r"Data Source=fakesql02\SALES,1433;", {r"fakesql02\SALES,1433": ("host_instance", "host:host_instance")}),
    ("Server=dbfake;Database=x", {"dbfake": ("host", "host:connection_string")}),
    (r"copy to \\fakenas03\dumps", {"fakenas03": ("host", "host:unc_path")}),
    ("the REPORTS instance on FAKESQL01 is down", {"REPORTS": ("bare_instance", "instance:name_before_instance"),
                                                   "FAKESQL01": ("host", "host:after_server_word")}),
    ("please reboot server appbox7", {"appbox7": ("host", "host:after_server_word")}),
    ("Host(s): Q9ZZPAP07K\nDatabase:", {"Q9ZZPAP07K": ("host", "host:after_server_word")}),
    ("Server(s): fakesql03, fakesql04", {"fakesql03": ("host", "host:after_server_word")}),
])
def test_rules_detect_without_the_model(text, expected):
    d = sd.LocalServerDirectory.from_sources([], None)
    found = {x["text"]: (x["kind"], x["rule"]) for x in sd.detect_servers(human(1, text), d, rules_only_model())}
    assert found == expected


@pytest.mark.parametrize("text", [
    r"Log in as CORP\jsmith please",  # an AD login: no digit in the host part, not known
    "Open localhost:8080 and portal7:443 in a browser",  # web ports, not SQL Server ports
    "the instance on this server is slow",  # filler words next to the cues
    "Server rebooted at noon",
    "it's fake7 again",  # a split-off "s" after a word that is not a server word
])
def test_rules_do_not_fire_on_ordinary_wording(text):
    d = sd.LocalServerDirectory.from_sources([], None)
    assert sd.detect_servers(human(1, text), d, rules_only_model()) == []


@pytest.mark.parametrize("text", [
    "Server=fakesql31,1433 is down and nothing else is fine",
    "Data Source = fakesql31,1433 then the job failed",
    "use Server=fakesql31,1433; Database=x and see logs",
])
def test_connection_string_value_ends_at_whitespace(text):
    d = sd.LocalServerDirectory.from_sources([], None)
    found = [(x["text"], x["rule"]) for x in sd.detect_servers(human(1, text), d, rules_only_model())]
    assert found == [("fakesql31,1433", "host:connection_string")]


def test_frequent_words_next_to_instance_need_upper_case():
    d = sd.LocalServerDirectory.from_sources([], None)
    m = rules_only_model()
    m.stop = sd.BUILTIN_STOP | {"managed", "prod"}  # as if frequent in human tickets
    assert sd.detect_servers(human(1, "the managed instance is slow"), d, m) == []
    assert sd.detect_servers(human(1, "the prod instance is slow"), d, m) == []
    found = sd.detect_servers(human(1, "the PROD instance is slow"), d, m)
    assert [(x["text"], x["rule"]) for x in found] == [("PROD", "instance:name_before_instance")]


def test_instance_rules_skip_alert_template_lines():
    d = sd.LocalServerDirectory.from_sources([], None)
    alert = make_ticket(1, "Alert #5 CPU", "Server SQLX01\nInstance SQLX01\nSeverity High", is_alert=1)
    found = sd.detect_servers(alert, d, rules_only_model())
    assert all(x["kind"] != "bare_instance" for x in found)


def known_names_directory():
    return sd.LocalServerDirectory.from_sources(
        [human(1, "x", dw=[r"aautdb01\REPORTS", "aautdb02", "appmain"])], None)


def test_known_and_near_known_names_are_detected_without_the_model():
    t = human(9, "aautdb02 and appmain are fine; AAUTBD01 typo, aautdb07 is new; REPORTS is slow")
    found = {x["text"]: x for x in sd.detect_servers(t, known_names_directory(), rules_only_model())}
    assert found["aautdb02"]["rule"] == "host:known_name" and found["aautdb02"]["status"] == "known"
    assert "appmain" not in found  # known, but digit-free directory names are left to the model
    assert found["AAUTBD01"]["rule"] == "host:near_known_name"
    assert found["AAUTBD01"]["status"] == "typo_of_known" and found["AAUTBD01"]["host"] == "aautdb01"
    assert found["aautdb07"]["rule"] == "host:near_known_name"
    assert found["aautdb07"]["status"] == "possible_new_host" and found["aautdb07"]["sibling_of"] == "aautdb01"
    assert found["REPORTS"]["rule"] == "instance:known_name"
    alone = sd.detect_servers(human(10, "REPORTS is slow today"), known_names_directory(), rules_only_model())
    assert [(x["rule"], x["host"], x["status"]) for x in alone] == [("instance:known_name", "aautdb01", "known")]


def test_known_name_rules_need_the_client_and_skip_ordinary_words():
    d = known_names_directory()
    other_client = human(9, "aautdb02 and REPORTS", org="999", name="Other")
    assert sd.detect_servers(other_client, d, rules_only_model()) == []
    # lower case 'reports' in running text, and a short or digit-free near miss, do not fire
    t = human(9, "the reports were slow; appmaim is a typo without digits")
    assert sd.detect_servers(t, d, rules_only_model()) == []


def alert(tid, fields, org="100", name="Acme"):
    t = make_ticket(tid, "Alert #1", "x", organization_id=org, organization_name=name, is_alert=1)
    t["alert_fields"] = fields
    return t


def dictionary_directory():
    """Confirmed names from alerts (with an environment), plus dw_servers-only names."""
    return sd.LocalServerDirectory.from_sources([
        alert(1, {"server": "fakesql11", "environment": "Production"}),
        alert(2, {"instance": r"fakesql12\FINANCE", "env": "uat"}),
        alert(3, {"server": "payroll"}),
        alert(4, {"server": "fakesql11", "environment": "Production"}),
        human(5, "x", dw=["appmain", "fakeapp03", "fakesql11"]),
    ], None)


def test_directory_keeps_confirmed_names_and_environments_apart_from_dw_servers():
    d = dictionary_directory()
    assert d.hosts("100") == ["appmain", "fakeapp03", "fakesql11", "fakesql12", "payroll"]
    assert d.confirmed_hosts("100") == ["fakesql11", "fakesql12", "payroll"]
    assert d.confirmed_instances("100") == [("fakesql12", "finance")]
    assert d.environment("fakesql11", "100") == "Production" and d.environment("fakesql12", "100") == "uat"
    assert d.environment("fakeapp03", "100") is None and d.environment("fakesql11", "999") is None
    kept = d.without(hosts={"fakesql11"})
    assert kept.confirmed_hosts("100") == ["fakesql12", "payroll"] and kept.environment("fakesql11", "100") is None
    assert d.stats()["confirmed_hosts"] == 3 and d.stats()["hosts_with_environment"] == 2


def test_dictionary_pass_detects_confirmed_names_first_with_their_environment():
    t = human(9, r"fakesql11 is slow and fakesql12\FINANCE failed over")
    found = {x["text"]: x for x in sd.detect_servers(t, dictionary_directory(), rules_only_model())}
    assert found["fakesql11"]["rule"] == "host:dictionary" and found["fakesql11"]["score"] == 1.0
    assert found["fakesql11"]["status"] == "known" and found["fakesql11"]["environment"] == "Production"
    pair = found[r"fakesql12\FINANCE"]
    assert pair["rule"] == "host:dictionary" and pair["kind"] == "host_instance" and pair["environment"] == "uat"


def test_dictionary_pass_needs_a_cue_for_word_like_names():
    d, m = dictionary_directory(), rules_only_model()
    m.stop = sd.BUILTIN_STOP | {"payroll"}  # as if frequent in human tickets
    assert sd.detect_servers(human(9, "payroll ran late"), d, m) == []
    # a frequent word stays a candidate only after a server word, so prose mentions are not training labels
    know = m.knowledge(d)
    assert not any(c["host_ok"] for c in sd.ticket_candidates(human(9, "payroll ran late"), know) if c["host"] == "payroll")
    found = sd.detect_servers(human(9, "please reboot server payroll"), d, m)
    assert [(x["text"], x["rule"]) for x in found] == [("payroll", "host:dictionary")]
    # without a cue an upper-case known instance falls back to the 0.95 known-name rule
    found = sd.detect_servers(human(9, "FINANCE is slow"), d, m)
    assert [(x["rule"], x["score"]) for x in found] == [("instance:known_name", 0.95)]
    found = sd.detect_servers(human(9, "the FINANCE instance is slow"), d, m)
    assert [(x["text"], x["rule"], x["host"]) for x in found] == [("FINANCE", "instance:dictionary", "fakesql12")]


def test_word_like_names_known_only_from_dw_servers_are_not_host_candidates():
    d = dictionary_directory()
    _, know = d, sd.Knowledge(d, sd.BUILTIN_STOP)
    cands = sd.ticket_candidates(human(9, "appmain and fakeapp03 are slow; Server=appmain;"), know)
    # a strong context (connection string) still makes it a host candidate
    assert [(c["text"], c["in_conn"]) for c in cands if c["host_ok"]] == [("fakeapp03", False), ("appmain", True)]
    assert sd.ticket_candidates(human(9, "appmain is slow"), know) == []
    # another client's ticket does not inherit the suppression
    assert cands_by_text(human(9, "appmain is slow", org="999", name="Other"), know)["appmain"]["host_ok"]


def test_thresholds_are_chosen_without_rule_matches():
    recs = [{"label": "host", "ph": 0.95, "pi": 0.0, "rule": "host:port"} for _ in range(50)]
    recs += [{"label": "host", "ph": 0.6, "pi": 0.0, "rule": None}, {"label": "none", "ph": 0.3, "pi": 0.0, "rule": None}]
    assert sdt.best_f1_threshold(recs, "host") == pytest.approx(0.6)


def test_without_labeled_drops_holdout_tickets(tmp_path):
    from ticket_analysis.jsonl import write_jsonl
    tickets = [human(str(i), "on server fakesql01") for i in range(4)]
    labels = tmp_path / "holdout.jsonl"
    write_jsonl(labels, [{"ticket_id": "1", "labeler": "claude"}, {"ticket_id": "3", "labeler": "claude"}])
    kept = sdt.without_labeled(tickets, [labels, tmp_path / "missing.jsonl"])
    assert [t["ticket_id"] for t in kept] == ["0", "2"]
    assert sdt.without_labeled(tickets, []) == tickets


def test_table_alert_server_is_detected_everywhere_it_appears():
    from tests.test_alert_parser import TABLE
    t = make_ticket("9", "", TABLE)  # pasted without the alert subject: recognised by the table header
    assert sd.is_alert_ticket(t)
    d = sd.LocalServerDirectory.from_sources([], None)
    found = sd.detect_servers(t, d, rules_only_model())
    assert [(x["text"], x["rule"]) for x in found] == [("Q9ZZPAP07K", "host:alert_field")] * 3


def test_grafana_master_host_is_an_alert_server():
    t = make_ticket("8", "[WARNING] Alert : MysqlSlaveReplicationLag_Warn",
                    "instance = fakemaria1.corp.test:9104\nmaster_host = 10.20.30.40\n", is_alert=1)
    t["alert_fields"] = {"family": "grafana", "layout": "grafana", "instance": "fakemaria1.corp.test:9104",
                         "master_host": "10.20.30.40"}
    assert {p.host for p in sd.ticket_names(t)} == {"fakemaria1", "10.20.30.40"}
    found = sd.detect_servers(t, sd.LocalServerDirectory.from_sources([], None), rules_only_model())
    assert {(x["text"], x["rule"]) for x in found} == {("fakemaria1.corp.test", "host:alert_field"),
                                                      ("10.20.30.40", "host:alert_field")}
