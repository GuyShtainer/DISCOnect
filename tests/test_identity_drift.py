"""One product identity in three languages: the Python core, the desktop UI and the Rust core must agree.

ADR 0001's mitigations (a cheap rename, one notice, no manufacturer marks) only hold while
``identity.py``, ``disconect-app/src/identity.ts`` and ``disconect-core/src/identity.rs`` say the same
thing, and while the Rust manufacturer scrub is the pattern ``serve.py`` really applies.
"""

import pathlib
import re

from disconect import identity

ROOT = pathlib.Path(__file__).resolve().parents[2]
TS = ROOT / "disconect-app" / "src" / "identity.ts"
RS = ROOT / "disconect-core" / "src" / "identity.rs"


def _ts_const(name: str) -> str:
    return re.search(rf'export const {name} = "((?:[^"\\]|\\.)*)";', TS.read_text()).group(1)


def _rs_const(name: str) -> str:
    return re.search(rf'pub const {name}: &str = "((?:[^"\\]|\\.)*)";', RS.read_text()).group(1)


def test_product_and_notice_agree_across_python_typescript_and_rust():
    for name in ("PRODUCT", "NOTICE"):
        python = getattr(identity, name)
        assert _ts_const(name) == python, f"identity.ts {name} drifted from identity.py"
        assert _rs_const(name) == python, f"identity.rs {name} drifted from identity.py"


def test_the_rust_manufacturer_scrub_is_the_pattern_serve_applies():
    assert _rs_const("MANUFACTURER_PATTERN") == identity.MANUFACTURER.pattern
    assert identity.MANUFACTURER.flags & re.IGNORECASE
    assert _rs_const("VENDOR_PLACEHOLDER") == "{vendor}" == identity.MANUFACTURER.sub("{vendor}", "garmin")


def test_the_scrub_cases_the_rust_unit_test_pins_are_the_python_answers():
    cases = {"Garmin Connect": "{vendor}", "GARMIN CONNECT and garmin": "{vendor} and {vendor}",
             "garmin  connect": "{vendor}  connect", "garmin connectx": "{vendor}x",
             "garmın İx GARMİN": "{vendor} İx {vendor}", "garmi": "garmi"}
    for text, expected in cases.items():
        assert identity.neutral(text) == expected, text


def test_the_mcp_server_version_is_the_rust_crates_version():
    """``serverInfo.version`` is ``identity.VERSION``; the crate (and so the Rust MCP) must report the same."""
    cargo = (ROOT / "disconect-core" / "Cargo.toml").read_text()
    assert re.search(r'^version = "([^"]+)"', cargo, re.MULTILINE).group(1) == identity.VERSION


def test_the_keychain_service_names_differ_on_purpose():
    """The Rust core and the Python core (so the MCP) keep separate keychain items.

    A generic password is unique per (service, account) and macOS binds it to the program that made it,
    so a shared service would make the two cores evict or lock out each other's item (11d keychain matrix).
    """
    from disconect.storage import keys

    assert keys.KEYCHAIN_SERVICE == "disconect-cli" == identity.CLI_KEYCHAIN_SERVICE
    assert _rs_const("KEYCHAIN_SERVICE") == "disconect"
    assert _rs_const("KEYCHAIN_SERVICE") != keys.KEYCHAIN_SERVICE


APP_TS = ROOT / "disconect-app" / "src" / "app.ts"


def test_the_key_screen_words_the_core_quotes_are_the_words_the_app_shows():
    """The MCP's locked-store messages send a person to a screen and a switch by name: the app is the source."""
    for name in ("KEY_SCREEN", "KEYCHAIN_SWITCH"):
        python = getattr(identity, name)
        assert _ts_const(name) == python, f"identity.ts {name} drifted from identity.py"
        assert _rs_const(name) == python, f"identity.rs {name} drifted from identity.py"
    app = APP_TS.read_text()
    assert f'title: "{identity.KEY_SCREEN}"' in app and f'header(out, "{identity.KEY_SCREEN}"' in app
    assert f'el("span", "", "{identity.KEYCHAIN_SWITCH}")' in app, "app.ts words the switch differently"


STRING_NAMES = ("DATA_DIR", "DB_FILENAME", "ENV_PREFIX", "CLI_KEYCHAIN_SERVICE", "MCP_SERVER_NAME", "COMMAND",
                "BACKUP_PREFIX", "LEGACY_DB_FILENAME", "LEGACY_ENV_PREFIX")
LIST_NAMES = ("LEGACY_HOMES", "LEGACY_BACKUP_PREFIXES", "LEGACY_CLI_KEYCHAIN_SERVICES")


def _ts_list(name: str) -> list[str]:
    body = re.search(rf"export const {name} = \[([^\]]*)\] as const;", TS.read_text()).group(1)
    return re.findall(r'"([^"]*)"', body)


def _rs_list(name: str) -> list[str]:
    body = re.search(rf"pub const {name}: &\[&str\] = &\[([^\]]*)\];", RS.read_text()).group(1)
    return re.findall(r'"([^"]*)"', body)


def test_the_name_constants_agree_across_python_typescript_and_rust():
    for name in STRING_NAMES:
        python = getattr(identity, name)
        assert _ts_const(name) == python, f"identity.ts {name} drifted from identity.py"
        assert _rs_const(name) == python, f"identity.rs {name} drifted from identity.py"
    for name in LIST_NAMES:
        python = getattr(identity, name)
        assert _ts_list(name) == python, f"identity.ts {name} drifted from identity.py"
        assert _rs_list(name) == python, f"identity.rs {name} drifted from identity.py"


def test_the_new_names_are_what_adr_0001_and_bet_02a_chose():
    assert (identity.DATA_DIR, identity.DB_FILENAME, identity.ENV_PREFIX) == (".disconect", "disconect.db", "DISCONECT_")
    assert identity.LEGACY_HOMES == [".hearthbeat"] and identity.LEGACY_BACKUP_PREFIXES == ["hearthbeat"]
    assert identity.COMMAND == identity.MCP_SERVER_NAME == identity.BACKUP_PREFIX == "disconect"


def test_mcp_instructions_carry_the_product_name_and_no_manufacturer():
    from disconect import mcp_server

    assert mcp_server.INSTRUCTIONS.startswith(f"{identity.PRODUCT} serves one person's watch health data")
    assert mcp_server.server.name == identity.MCP_SERVER_NAME
    assert not identity.MANUFACTURER.search(mcp_server.INSTRUCTIONS.split("\n")[0]), "ADR 0001: no vendor name"
