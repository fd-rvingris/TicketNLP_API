import pytest

from ticket_analysis.names import Cmdb, Instance, ParsedName, load_cmdb, match_mention, parse_server_name


def inst(name: str) -> Instance:
    return Instance(name, parse_server_name(name), "prod")


@pytest.mark.parametrize("raw, expected", [
    ("SQLPROD01", ParsedName("sqlprod01", None, None)),
    ("SQLPROD01.corp.fortified.local", ParsedName("sqlprod01", None, None)),
    ("SQLPROD01\\REPORTING", ParsedName("sqlprod01", "reporting", None)),
    ("sqlprod01.corp.local\\Reporting,1433", ParsedName("sqlprod01", "reporting", "1433")),
    ("SQLPROD01,1433", ParsedName("sqlprod01", None, "1433")),
    ("tcp:myserver.database.windows.net,1433", ParsedName("myserver", None, "1433")),
    ("10.20.30.40", ParsedName("10.20.30.40", None, None)),
    ("[SQLPROD01]", ParsedName("sqlprod01", None, None)),
])
def test_parse_server_name(raw, expected):
    assert parse_server_name(raw) == expected


def test_parse_empty_returns_none():
    assert parse_server_name("  ") is None


CLIENT = [inst("SQLPROD01"), inst("SQLPROD01\\REPORTING"), inst("SQLDEV02\\A"), inst("SQLDEV02\\B"),
          inst("APPDB03.corp.local")]


@pytest.mark.parametrize("mention, status, names", [
    ("sqlprod01", "matched", ["SQLPROD01"]),                          # case-insensitive
    ("SQLPROD01.corp.fortified.local", "matched", ["SQLPROD01"]),     # FQDN suffix stripped
    ("SQLPROD01,1433", "matched", ["SQLPROD01"]),                     # port ignored
    ("SQLPROD01\\reporting", "matched", ["SQLPROD01\\REPORTING"]),    # exact named instance
    ("SQLPROD01\\OTHER", "matched", ["SQLPROD01"]),                   # unknown instance -> host record
    ("SQLDEV02", "ambiguous", ["SQLDEV02\\A", "SQLDEV02\\B"]),        # bare host, several instances
    ("SQLDEV02\\B", "matched", ["SQLDEV02\\B"]),
    ("SQLDEV02\\C", "unmatched", []),                                 # no such instance, no host record
    ("appdb03", "matched", ["APPDB03.corp.local"]),                   # CMDB side is an FQDN
    ("SQLTEST99", "unmatched", []),
])
def test_match_mention(mention, status, names):
    assert match_mention(mention, CLIENT) == {"status": status, "server_names": names}


def test_match_without_instances_is_unmatched():
    assert match_mention("SQLPROD01", []) == {"status": "unmatched", "server_names": []}


def test_cmdb_only_returns_the_tickets_own_client(tmp_path):
    path = tmp_path / "instances.csv"
    path.write_text(
        "client_name,zendesk_organization_id,server_name,environment\n"
        "Acme,100,ACMESQL01,prod\n"
        "Acme,100,ACMESQL02,dev\n"
        "Globex,200,GLXSQL01,prod\n"
        "Initech,,INISQL01,prod\n",
        encoding="utf-8",
    )
    cmdb = load_cmdb(path)
    acme = {i.server_name for i in cmdb.for_ticket({"organization_id": "100", "organization_name": "Globex"})}
    assert acme == {"ACMESQL01", "ACMESQL02"}  # org id wins over name
    by_name = {i.server_name for i in cmdb.for_ticket({"organization_id": "999", "organization_name": "initech"})}
    assert by_name == {"INISQL01"}
    assert cmdb.for_ticket({"organization_id": None, "organization_name": "Unknown Co"}) == []


def test_missing_cmdb_file(tmp_path):
    cmdb = load_cmdb(tmp_path / "missing.csv")
    assert isinstance(cmdb, Cmdb)
    assert cmdb.for_ticket({"organization_id": "100"}) == []
