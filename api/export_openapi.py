"""Write the OpenAPI (OAS 3.1) document to docs/openapi.yaml, generated from the code.

    python -m api.export_openapi            write docs/openapi.yaml
    python -m api.export_openapi --check    exit 1 if the committed file differs

The document does not depend on the settings (both security schemes are always declared), so the
committed file matches every deployment. tests/test_api_openapi.py fails when it is out of date.
"""

from __future__ import annotations

import argparse
import sys

from ticket_analysis.paths import REPO_ROOT

OPENAPI_PATH = REPO_ROOT / "docs" / "openapi.yaml"


def openapi_document() -> dict:
    from .app import create_app
    from .settings import load_settings
    # Any valid settings give the same document; these load nothing and serve nothing.
    settings = load_settings(auth_mode="api_key", api_keys="x" * 32, enable_docs=False)
    return create_app(settings, load=False, configure_logging=False).openapi()


def render() -> str:
    import yaml

    class Dumper(yaml.SafeDumper):
        pass

    def text(dumper, value: str):  # multi-line text as a | block, so the file reads like the source
        style = "|" if "\n" in value else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

    Dumper.add_representer(str, text)
    return yaml.dump(openapi_document(), Dumper=Dumper, sort_keys=False, allow_unicode=True, width=110)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--check", action="store_true", help="compare with the committed file instead of writing it")
    args = p.parse_args(argv)
    text = render()
    if args.check:
        current = OPENAPI_PATH.read_text(encoding="utf-8") if OPENAPI_PATH.exists() else ""
        if current != text:
            sys.exit(f"{OPENAPI_PATH} is out of date; run python -m api.export_openapi")
        print(f"{OPENAPI_PATH} is up to date")
        return
    OPENAPI_PATH.write_text(text, encoding="utf-8", newline="\n")
    print(f"Wrote {OPENAPI_PATH}")


if __name__ == "__main__":
    main()
