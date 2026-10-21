"""The OpenAPI document: OAS 3.1, complete, and the committed docs/openapi.yaml matches the code."""

import yaml

from api.export_openapi import OPENAPI_PATH, openapi_document, render


def test_the_committed_openapi_yaml_matches_the_generated_schema():
    assert OPENAPI_PATH.exists(), "run python -m api.export_openapi"
    assert OPENAPI_PATH.read_text(encoding="utf-8") == render(), "docs/openapi.yaml is stale: python -m api.export_openapi"
    assert yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8")) == openapi_document()


def test_it_is_oas_3_1_with_the_v1_paths_and_probes():
    doc = openapi_document()
    assert doc["openapi"].startswith("3.1")
    assert set(doc["paths"]) == {"/v1/detect", "/v1/classify", "/v1/analyze", "/v1/catalog", "/health", "/ready"}
    assert "tolerate" in doc["info"]["description"] or "must ignore" in doc["info"]["description"]
    assert "/v1/catalog" in doc["info"]["description"]


def test_every_operation_model_and_field_is_described_with_examples():
    doc = openapi_document()
    for path, item in doc["paths"].items():
        for method, op in item.items():
            assert op.get("summary") and op.get("description"), (method, path)
            if method == "post":
                examples = op["requestBody"]["content"]["application/json"]["examples"]
                assert {"access_request", "servers", "alert", "text_only"} <= set(examples)
                assert "Server=FAKESQL01" in examples["access_request"]["value"]["text"]
                for status in ("400", "401", "403", "413", "415", "422", "503"):
                    assert status in op["responses"], (path, status)
    missing = []
    for name, schema in doc["components"]["schemas"].items():
        if name in ("HTTPValidationError", "ValidationError"):
            continue
        if not schema.get("description"):
            missing.append(f"{name}: description")
        for field, prop in schema.get("properties", {}).items():
            if not prop.get("description"):
                missing.append(f"{name}.{field}: description")
            nested = "$ref" in prop or "$ref" in str(prop.get("items", "")) or "$ref" in str(prop.get("anyOf", ""))
            if not nested and "examples" not in prop and "example" not in prop and field != "debug":
                missing.append(f"{name}.{field}: example")
    assert not missing, missing


def test_example_values_are_made_up():
    text = render().lower()
    for needle in ("fakesql01", "999001", "fake co", "corp\\\\svc-fake"):
        assert needle in text, needle
