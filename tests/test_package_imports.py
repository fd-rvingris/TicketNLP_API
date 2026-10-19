"""The package is what the API ships, so it must load without the tools or the LLM and Zendesk clients."""
import pkgutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
BLOCKED = ("anthropic", "dotenv", "requests", "tools")
EXPECTED = {"paths", "jsonl", "tickets", "masking", "names", "taxonomy", "alert_parser", "type_rules", "text_clean",
            "ticket_types"}


def package_modules() -> list[str]:
    import ticket_analysis
    return sorted(m.name for m in pkgutil.iter_modules(ticket_analysis.__path__))


def test_package_has_the_foundation_modules():
    assert EXPECTED <= set(package_modules())


def test_package_imports_nothing_from_tools_or_the_api_clients():
    code = ("import importlib, sys\n"
            f"for name in {BLOCKED!r}:\n"
            "    sys.modules[name] = None  # any import of these raises ImportError\n"
            f"for name in {package_modules()!r}:\n"
            "    importlib.import_module('ticket_analysis.' + name)\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_paths_point_at_the_repository():
    from ticket_analysis.paths import DATA_DIR, INSTANCES_PATH, REPO_ROOT as root
    assert root == REPO_ROOT.resolve()
    assert DATA_DIR == root / "data" and INSTANCES_PATH == root / "data" / "instances.csv"


def test_taxonomy_file_ships_with_the_package():
    from ticket_analysis.taxonomy import TAXONOMY_PATH
    assert TAXONOMY_PATH.parent.name == "ticket_analysis" and TAXONOMY_PATH.exists()


def test_parsing_and_cleaning_run_without_the_tools():
    code = ("import sys\n"
            f"for name in {BLOCKED!r}:\n"
            "    sys.modules[name] = None\n"
            "from ticket_analysis.tickets import make_ticket\n"
            "from ticket_analysis.text_clean import clean_text\n"
            "t = make_ticket('1', '[FIRING:1] Made Up Alert', 'Hello,\\nserver FAKESQL01 is down.\\nThanks')\n"
            "print(clean_text(t['subject'], t['description'])[1])\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_type_classification_runs_without_the_tools():
    code = ("import sys\n"
            f"for name in {BLOCKED!r}:\n"
            "    sys.modules[name] = None\n"
            "from ticket_analysis.ticket_types import features\n"
            "print(features([{'subject': 'Made up', 'description': 'text'}], 100).shape)\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "(1, 2)"
