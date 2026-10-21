"""Directory snapshots: building from a made-up CMDB export and alert names, validation, publishing, the
API's cache, and equivalence with the model's own directory."""

import json

import pytest

import ticket_analysis.server_detector as sd
from api.directory import (CmdbRecord, CsvCmdbSource, DirectoryCache, DirectoryState, KeystoneCmdbSource,
                           NoCmdbSource, SnapshotError, SnapshotStore, build_snapshot, encode, parse_snapshot,
                           snapshot_directory, snapshot_name, validate)
from api.storage import BlobStore, LocalStore, StoreError
from ticket_analysis.analysis import ticket_for_org
from ticket_analysis.names import ParsedName
from tests.api_support import ORG, OTHER, TICKET, make_client, make_settings, trained, write_models

CSV = ("server_name,environment,zendesk_organization_id,client_name\n"
       "FAKESQL01\\REPORTS,Production,999001,Fake Co\n"
       "fakesql02.corp.fake.local,Test,999001,Fake Co\n"
       "FAKESQL03,,,Name Only Fake Co\n"
       "not a server name!,Production,999001,Fake Co\n")


def model_directory():
    d = sd.LocalServerDirectory()
    d.add(ORG, "name:fake co", ParsedName("fakesql09", None, None), "alert", "Production")
    d.add(ORG, "name:fake co", ParsedName("fakesql09", "finance", None), "alert")
    d.add(ORG, None, ParsedName("fakeword", None, None), "dw")
    return d


def csv_source(tmp_path):
    path = tmp_path / "instances.csv"
    path.write_text(CSV, encoding="utf-8")
    return CsvCmdbSource(path)


def test_a_snapshot_merges_the_cmdb_and_the_alert_names(tmp_path):
    s = build_snapshot(csv_source(tmp_path), model_directory(), created_at=None)
    assert s["format"] == "ticket-analysis/server-directory" and s["formatVersion"] == 1
    hosts = {h["host"]: h for h in s["clients"][ORG]["hosts"]}
    assert hosts["fakesql01"]["sources"] == ["cmdb"] and hosts["fakesql01"]["environments"] == {"Production": 1}
    assert hosts["fakesql02"]["environments"] == {"Test": 1}  # the FQDN normalized to the host
    assert hosts["fakesql09"]["sources"] == ["alert"] and "fakeword" not in hosts  # dw names left out by default
    assert {(p["host"], p["instance"]) for p in s["clients"][ORG]["instances"]} == {("fakesql01", "reports"),
                                                                                   ("fakesql09", "finance")}
    assert [h["host"] for h in s["clients"]["name:name only fake co"]["hosts"]] == ["fakesql03"]
    assert s["aliases"]["name:fake co"] == ORG
    assert s["sources"]["cmdb"] == {"kind": "csv", "records": 4, "skipped": 1}
    assert s["counts"]["clients"] == 2 and s["counts"]["bySource"]["cmdb"] == 4
    with_dw = build_snapshot(NoCmdbSource(), model_directory(), include_dw=True)
    assert {h["host"]: h["sources"] for h in with_dw["clients"][ORG]["hosts"]}["fakeword"] == ["dw"]


def test_the_snapshot_directory_marks_cmdb_and_alert_names_confirmed(tmp_path):
    d = snapshot_directory(build_snapshot(csv_source(tmp_path), model_directory(), include_dw=True))
    assert set(d.confirmed_hosts(ORG)) == {"fakesql01", "fakesql02", "fakesql09"}
    assert "fakeword" in d.hosts(ORG) and "fakeword" not in d.confirmed_hosts(ORG)
    assert d.environment("fakesql01", ORG) == "Production" and d.lookup("fakesql01", "reports", ORG)["status"] == "known"
    assert d.client_for({"organization_id": None, "organization_name": "Fake Co"}) == ORG


def test_names_survive_a_round_trip_through_json(tmp_path):
    s = build_snapshot(csv_source(tmp_path), model_directory())
    assert parse_snapshot(encode(s)) == s
    for bad in (b"not json", b"{}", json.dumps({**s, "formatVersion": 9}).encode(),
                json.dumps({**s, "clients": {ORG: {"hosts": [{"host": 1}]}}}).encode()):
        with pytest.raises(SnapshotError):
            parse_snapshot(bad)


def test_the_same_names_give_the_same_detections_as_the_models_directory():
    model, directory, _ = trained()
    hydrated = snapshot_directory(parse_snapshot(encode(build_snapshot(NoCmdbSource(), directory, include_dw=True))))
    assert hydrated.to_dict() == directory.to_dict()
    texts = [TICKET["text"], "Please check FAKESQL01 and fakesql1 today", "The FINANCE instance is down",
             "OTHSQL03\\SALES and fakesql02,1433 are slow", "Server=FAKESQL05;Database=FAKE_DB"]
    for org in (ORG, OTHER, "999999", None):
        for text in texts:
            t = ticket_for_org("Help", text, org)
            assert sd.detect_servers(t, hydrated, model) == sd.detect_servers(t, directory, model), (org, text)


def test_validation_refuses_an_empty_or_shrunken_snapshot(tmp_path):
    full = build_snapshot(csv_source(tmp_path), model_directory())
    assert validate(full) == []
    assert validate(build_snapshot(NoCmdbSource(), None)) == ["the snapshot is empty"]
    small = build_snapshot(NoCmdbSource(), model_directory())
    problems = validate(small, previous=full, min_share=0.9)
    assert problems and "fewer than 90%" in problems[0]
    assert validate(small, previous=full, min_share=0.2) == []


def test_publish_writes_validates_and_keeps_the_newest(tmp_path):
    from datetime import datetime, timedelta, timezone
    store = SnapshotStore(LocalStore(tmp_path / "dir"))
    start = datetime(2026, 10, 19, 6, 0, tzinfo=timezone.utc)
    for day in range(4):
        s = build_snapshot(csv_source(tmp_path), model_directory(), created_at=start + timedelta(days=day))
        store.publish(s, keep=3, min_share=0.9)
    assert store.names() == ["directory-20261022T060000Z.json", "directory-20261021T060000Z.json",
                             "directory-20261020T060000Z.json"]
    shrunken = build_snapshot(NoCmdbSource(), model_directory(), created_at=start + timedelta(days=5))
    with pytest.raises(SnapshotError, match="not published"):
        store.publish(shrunken, keep=3, min_share=0.9)
    assert len(store.names()) == 3
    assert snapshot_name("2026-10-19T06:00:00Z") == "directory-20261019T060000Z.json"


def test_the_cache_swaps_in_a_newer_snapshot_and_keeps_the_last_on_failure(tmp_path):
    from datetime import datetime, timezone
    store = SnapshotStore(LocalStore(tmp_path / "dir"))
    fallback = DirectoryState(model_directory(), "model:x", "model", None, None)
    loaded = []
    cache = DirectoryCache(store, fallback, on_load=loaded.append)
    assert cache.refresh() is False and cache.current is fallback  # an empty store: the model's directory
    first = build_snapshot(csv_source(tmp_path), model_directory(), created_at=datetime(2026, 10, 19, tzinfo=timezone.utc))
    store.publish(first, keep=5, min_share=0.9)
    assert cache.refresh() is True and cache.current.version == "2026-10-19T00:00:00Z" and len(loaded) == 1
    assert cache.refresh() is False  # nothing newer
    store.store.write("directory-20261020T000000Z.json", b"broken")  # a newer file that does not parse
    assert cache.refresh() is False and cache.current.version == "2026-10-19T00:00:00Z"

    class Down:
        location = "fake://down"

        def list(self, prefix=""):
            raise StoreError("Cannot list fake://down: ConnectionError")
    cache.store = SnapshotStore(Down())
    assert cache.refresh() is False and cache.current.version == "2026-10-19T00:00:00Z"
    assert "ConnectionError" in cache.last_error and cache.age_seconds() > 0


def test_the_api_picks_up_a_new_snapshot_without_a_restart(tmp_path):
    from datetime import datetime, timezone
    model, directory, _ = trained()
    store = SnapshotStore(LocalStore(tmp_path / "directory"))
    store.publish(build_snapshot(NoCmdbSource(), directory, include_dw=True,
                                 created_at=datetime(2026, 10, 19, tzinfo=timezone.utc)), keep=5, min_share=0.9)
    body = {"text": "Please check FAKESQL77 today", "zendeskOrgId": ORG}
    with make_client(tmp_path) as c:
        before = c.post("/v1/detect", json=body).json()
        assert before["models"]["directory"] == "2026-10-19T00:00:00Z"
        assert before["detections"][0]["status"] == "possible_new_host"
        grown = build_snapshot(CsvCmdbSource(_csv(tmp_path, "FAKESQL77,Production,999001,Fake Co\n")), directory,
                               include_dw=True, created_at=datetime(2026, 10, 20, tzinfo=timezone.utc))
        store.publish(grown, keep=5, min_share=0.9)
        assert c.app.state.service.refresh_directory() is True
        after = c.post("/v1/detect", json=body).json()
        assert after["models"]["directory"] == "2026-10-20T00:00:00Z"
        assert after["detections"][0]["status"] == "known" and after["detections"][0]["environment"] == "Production"
        assert len(model._know) <= 3  # the old directories' features are dropped


def _csv(tmp_path, rows: str):
    path = tmp_path / "more.csv"
    path.write_text("server_name,environment,zendesk_organization_id,client_name\n" + rows, encoding="utf-8")
    return path


def test_the_refresh_command_builds_and_publishes_from_the_csv(tmp_path, monkeypatch):
    from api import refresh_directory
    write_models(tmp_path / "models")
    for k, v in {"MODEL_DIR": str(tmp_path / "models"), "DIRECTORY_STORE": str(tmp_path / "directory"),
                 "CMDB_SOURCE": "csv", "CMDB_CSV_PATH": str(csv_source(tmp_path).path), "LOG_FORMAT": "text"}.items():
        monkeypatch.setenv(k, v)
    refresh_directory.main([])
    names = SnapshotStore(LocalStore(tmp_path / "directory")).names()
    assert len(names) == 1
    monkeypatch.setenv("CMDB_SOURCE", "keystone")
    with pytest.raises(SystemExit) as e:  # the Keystone source is a stub
        refresh_directory.main([])
    assert e.value.code == 1


def test_the_keystone_source_is_a_stub():
    source = KeystoneCmdbSource("https://keystone.fake.example", page_size=100)
    with pytest.raises(NotImplementedError, match="Keystone"):
        list(source.records())
    assert source.page_size == 100


def test_csv_source_reads_the_export_format(tmp_path):
    records = list(csv_source(tmp_path).records())
    assert records[0] == CmdbRecord("999001", "Fake Co", "FAKESQL01\\REPORTS", "Production")
    with pytest.raises(SnapshotError):
        list(CsvCmdbSource(tmp_path / "missing.csv").records())
    assert list(CsvCmdbSource(tmp_path / "missing.csv", allow_missing=True).records()) == []


class FakeBlob:
    def __init__(self, data):
        self.data = data

    def readall(self):
        return self.data

    def readinto(self, f):
        f.write(self.data)


class FakeContainer:
    def __init__(self):
        self.blobs = {}

    def list_blobs(self, name_starts_with=None):
        from types import SimpleNamespace
        return [SimpleNamespace(name=n) for n in sorted(self.blobs) if n.startswith(name_starts_with or "")]

    def download_blob(self, name):
        if name not in self.blobs:
            raise KeyError(name)
        return FakeBlob(self.blobs[name])

    def upload_blob(self, name, data, overwrite=False):
        self.blobs[name] = data

    def delete_blob(self, name):
        self.blobs.pop(name)


def test_blob_store_keeps_snapshots_under_its_path(tmp_path):
    container = FakeContainer()
    store = BlobStore("https://fakestorage.blob.core.windows.net/ticket-analysis/directory", container_client=container)
    snapshots = SnapshotStore(store)
    snapshots.publish(build_snapshot(csv_source(tmp_path), model_directory()), keep=2, min_share=0.9)
    assert [n.split("/")[0] for n in container.blobs] == ["directory"]
    assert snapshots.newest_valid()[1]["counts"]["clients"] == 2
    store.write("sub/nested.json", b"{}")
    assert all("/" not in n for n in store.list())  # direct children only
    with pytest.raises(StoreError):
        store.read("missing.json")
    with pytest.raises(StoreError):
        store.read("../escape.json")
    with pytest.raises(StoreError):
        BlobStore("https://example.com/container", container_client=container)


def test_model_store_version_folders(tmp_path):
    """MODEL_STORE + MODEL_VERSION: files come from models/<version>/ into MODEL_DIR."""
    from api.models import fetch_model_files
    write_models(tmp_path / "store" / "models" / "2026-10-17")
    settings = make_settings(tmp_path, model_store=str(tmp_path / "store"), model_version="2026-10-17",
                             model_dir=tmp_path / "cache")
    files = fetch_model_files(settings)
    assert files.server == (tmp_path / "cache" / "server_detector.joblib").resolve() and files.notes.exists()
    with make_client(tmp_path, write=False, model_store=str(tmp_path / "store"), model_version="2026-10-17",
                     model_dir=tmp_path / "cache") as c:
        assert c.post("/v1/detect", json=TICKET).json()["models"]["modelVersion"] == "2026-10-17"
    with pytest.raises(Exception, match="MODEL_VERSION"):
        make_settings(tmp_path, model_store=str(tmp_path / "store"))


def test_malformed_snapshots_are_skipped_and_an_older_valid_one_is_used(tmp_path):
    from datetime import datetime, timezone
    store = SnapshotStore(LocalStore(tmp_path / "dir"))
    good = build_snapshot(csv_source(tmp_path), model_directory(), created_at=datetime(2026, 10, 19, tzinfo=timezone.utc))
    store.publish(good, keep=5, min_share=0.9)
    bad_hosts = {**good, "clients": {ORG: {"hosts": {"fakesql01": {}}, "instances": []}}}
    bad_sources = {**good, "clients": {ORG: {"hosts": [{"host": "fakesql01", "sources": 5}], "instances": []}}}
    bad_env = {**good, "clients": {ORG: {"hosts": [{"host": "fakesql01", "sources": ["cmdb"],
                                                    "environments": {"Production": "many"}}], "instances": []}}}
    for day, bad in ((20, bad_hosts), (21, bad_sources), (22, bad_env)):
        with pytest.raises(SnapshotError):
            parse_snapshot(encode(bad))
        store.store.write(f"directory-202610{day}T000000Z.json", encode(bad))
    assert store.newest_valid()[0] == "directory-20261019T000000Z.json"
    cache = DirectoryCache(store, DirectoryState(model_directory(), "model:x", "model", None, None))
    assert cache.refresh() is True and cache.current.version == "2026-10-19T00:00:00Z"


def test_the_service_loads_with_a_malformed_snapshot_in_the_store(tmp_path):
    store = SnapshotStore(LocalStore(tmp_path / "directory"))
    good = build_snapshot(csv_source(tmp_path), model_directory())
    store.store.write("directory-20261030T000000Z.json", encode({**good, "clients": {ORG: {"hosts": 5}}}))
    with make_client(tmp_path) as c:
        assert c.get("/ready").status_code == 200
        assert c.post("/v1/detect", json=TICKET).json()["models"]["directory"].startswith("model:")


def test_removing_the_current_snapshot_rolls_back_to_the_newest_remaining(tmp_path):
    from datetime import datetime, timezone
    store = SnapshotStore(LocalStore(tmp_path / "dir"))
    for day in (19, 20):
        store.publish(build_snapshot(csv_source(tmp_path), model_directory(),
                                     created_at=datetime(2026, 10, day, tzinfo=timezone.utc)), keep=5, min_share=0.9)
    cache = DirectoryCache(store, DirectoryState(model_directory(), "model:x", "model", None, None))
    assert cache.refresh() and cache.current.version == "2026-10-20T00:00:00Z"
    store.store.delete("directory-20261020T000000Z.json")
    assert cache.refresh() is True and cache.current.version == "2026-10-19T00:00:00Z"


def test_local_store_errors_are_store_errors(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    store = LocalStore(blocker)
    with pytest.raises(StoreError):
        store.write("directory-20261019T000000Z.json", b"{}")
    with pytest.raises(StoreError):
        store.read("missing.json")


def test_a_snapshot_time_without_a_timezone_is_refused(tmp_path):
    snapshot = build_snapshot(csv_source(tmp_path), model_directory())
    snapshot["createdAt"] = "2026-10-19T06:00:00"
    with pytest.raises(SnapshotError, match="createdAt"):
        parse_snapshot(encode(snapshot))
