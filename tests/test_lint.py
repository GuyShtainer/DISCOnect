"""Source-level rules that keep encryption honest: one driver, no key leaks through SQL tracing."""

import pathlib
import re

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "disconect"


TESTS = pathlib.Path(__file__).resolve().parent


def _sources():
    return [p for p in SRC.rglob("*.py")] + [p for p in TESTS.glob("*.py")]


def test_only_storage_imports_the_sqlite_driver():
    offenders = []
    for path in _sources():
        text = path.read_text()
        if path.parent.name == "storage" and path.name in ("__init__.py", "migrations.py"):
            continue
        if re.search(r"\b(sqlite3|sqlcipher3)\b", text) and path.name != "test_lint.py":
            offenders.append(path.name)
    assert offenders == [], f"use disconect.storage.sqlite (sqlcipher3's errors are not sqlite3.Error): {offenders}"


def test_pragma_key_only_in_storage():
    offenders = [p.name for p in _sources() if "PRAGMA key" in p.read_text() and p.parent.name != "storage"
                 and p.name not in ("test_lint.py",)]
    assert offenders == [], "keying a connection belongs to storage.apply_key"


def test_no_trace_callbacks_or_key_logging():
    offenders = [p.name for p in _sources() if "set_trace_callback" in p.read_text() and p.name != "test_lint.py"]
    assert offenders == [], "a trace callback would log PRAGMA key statements"


def test_no_cli_option_takes_key_material():
    from disconect import cli
    parser = cli.build_parser()
    names = []

    def walk(p):
        for action in p._actions:
            names.extend(action.option_strings)
            if hasattr(action, "choices") and isinstance(action.choices, dict):
                for sub in action.choices.values():
                    walk(sub)
    walk(parser)
    bad = [n for n in names if any(w in n.lower() for w in ("pass", "key=", "secret", "words", "phrase"))]
    assert bad == [], f"key material must never travel in argv: {bad}"


def test_app_token_copy_matches_design_export():
    """projects/disconect-app/src/tokens.css is a copy of docs/design/tokens.css; drift breaks the design system."""
    root = SRC.parent.parent.parent.parent
    export = root / "projects" / "disconect" / "docs" / "design" / "tokens.css"
    copy = root / "projects" / "disconect-app" / "src" / "tokens.css"
    assert export.read_text() == copy.read_text()


def test_design_tokens_css_is_generated_from_json():
    """Every colour value in tokens.css must come from tokens.json (gen_tokens.py writes both)."""
    import json
    root = SRC.parent.parent.parent.parent / "projects" / "disconect" / "docs" / "design"
    data = json.loads((root / "tokens.json").read_text())
    known = {v for tok in data["color"]["tokens"] for v in tok["value"].values()}
    css_hexes = set(re.findall(r"#[0-9a-f]{6}", (root / "tokens.css").read_text()))
    assert css_hexes <= known, css_hexes - known


#: Modules that already print to stderr; no module may join them (serve's stdout is a protocol).
PRINTERS = {"cli.py", "mcp_server.py", "storage/__init__.py", "storage/keys.py"}


def test_no_new_print_outside_the_cli():
    offenders = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
                 if re.search(r"(?<![\w.])print\(", p.read_text()) and p.relative_to(SRC).as_posix() not in PRINTERS]
    assert offenders == [], f"print() belongs to cli.py (serve's stdout is a protocol): {offenders}"
    assert not re.search(r"(?<![\w.])print\(", (SRC / "serve.py").read_text())


def test_serve_never_reads_the_env_passphrase_or_caches_one():
    text = (SRC / "serve.py").read_text()
    assert "DISCONECT_PASSPHRASE" not in text, "serve ignores the env passphrase; name it only via keys.PASSPHRASE_ENV"
    for forbidden in ("_env_passphrase", "_session_passphrase", "keys.unlock(", "storage.prime(", "master_key_for("):
        assert forbidden not in text, f"serve must not use {forbidden}"


def test_identity_strings_live_only_in_identity_py():
    offenders = [p.name for p in SRC.rglob("*.py") if p.name != "identity.py"
                 and ("DISCOnect" in p.read_text() or "Not affiliated with or endorsed" in p.read_text())]
    assert offenders == [], f"import disconect.identity instead: {offenders}"
