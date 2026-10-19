from ticket_analysis.taxonomy import Mention
from tools.common import split_by_evidence

TICKET = "Backups failing\nThe nightly full backup on SQLPROD01\\REPORTING\n  failed again.   Database: SalesDB"


def mention(text, quote, kind="instance"):
    return Mention(text=text, kind=kind, evidence_quote=quote)


def test_quote_with_different_whitespace_is_kept():
    kept, dropped = split_by_evidence([mention("SQLPROD01\\REPORTING", "on SQLPROD01\\REPORTING failed again.")], TICKET)
    assert len(kept) == 1 and not dropped


def test_quote_not_in_ticket_is_dropped():
    kept, dropped = split_by_evidence([mention("SQLPROD02", "backup on SQLPROD02 failed")], TICKET)
    assert not kept and len(dropped) == 1


def test_empty_quote_is_dropped():
    kept, dropped = split_by_evidence([mention("SalesDB", "   ", "database")], TICKET)
    assert not kept and len(dropped) == 1


def test_quote_check_is_case_sensitive():
    kept, dropped = split_by_evidence([mention("SalesDB", "database: salesdb", "database")], TICKET)
    assert not kept and len(dropped) == 1


def test_mixed_list_counts():
    mentions = [mention("SQLPROD01\\REPORTING", "SQLPROD01\\REPORTING"),
                mention("SalesDB", "Database: SalesDB", "database"),
                mention("GHOST", "GHOST server")]
    kept, dropped = split_by_evidence(mentions, TICKET)
    assert [m.text for m in kept] == ["SQLPROD01\\REPORTING", "SalesDB"]
    assert [m.text for m in dropped] == ["GHOST"]
