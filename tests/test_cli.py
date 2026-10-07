"""The CLI's published exit codes and output separation, exercised in-process."""

import io
import json
import sys

import pytest

from disconect import cli
from test_import import _build_export


def _run(argv, capsys):
    code = cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


#: The `import --json` / `reparse --json` keys, pinned on the Rust side too (`relay_cli_test.rs`): the two CLIs twin.
IMPORT_JSON_KEYS = ["dates_assumed_utc", "derived_days", "dropped", "failures", "files_duplicate", "files_failed",
                    "files_imported", "files_seen", "ignored", "records_written", "status", "streams", "transport", "warnings"]


def test_import_status_contract_round_trip(tmp_path, db_path, capsys):
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    code, out, err = _run(["--db", str(db_path), "import", str(root)], capsys)
    assert code == cli.EXIT_OK and out.startswith("ok:") and err == ""

    code, out, _ = _run(["--db", str(db_path), "--json", "import", str(root)], capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_OK and payload["status"] == "ok" and payload["files_imported"] == 0
    assert sorted(payload) == IMPORT_JSON_KEYS, "the Rust CLI's key set (relay_cli_test.rs); `cancelled` is serve-only (BL-3)"

    code, out, _ = _run(["--db", str(db_path), "--json", "status", "--days", "3650"], capsys)
    report = json.loads(out)
    assert code == cli.EXIT_OK and report["never_imported"] is False
    assert report["recent_imports"][0]["files_duplicate"] > 0

    code, out, _ = _run(["--db", str(db_path), "status"], capsys)
    assert code == cli.EXIT_OK and "streams:" in out and "@" not in out

    code, out, _ = _run(["--json", "contract"], capsys)
    assert code == cli.EXIT_OK and json.loads(out)["contract_version"]

    code, out, _ = _run(["--db", str(db_path), "--json", "reparse"], capsys)   # last: it books a run of its own
    assert code == cli.EXIT_OK and sorted(json.loads(out)) == IMPORT_JSON_KEYS


def test_exit_codes(tmp_path, db_path, capsys):
    code, _, err = _run(["--db", str(db_path), "status"], capsys)
    assert code == cli.EXIT_NOT_CONFIGURED and "import" in err

    code, _, err = _run(["--db", str(db_path), "import", str(tmp_path / "nowhere")], capsys)
    assert code == cli.EXIT_USAGE and "no such file" in err

    folder = tmp_path / "drop"
    folder.mkdir()
    (folder / "bad.fit").write_bytes(b"\x00" * 30)
    code, out, _ = _run(["--db", str(db_path), "import", str(folder)], capsys)
    assert code == cli.EXIT_FAILED and out.startswith("failed:") and "FAILED bad.fit" in out

    with pytest.raises(SystemExit) as exit_info:
        cli.main(["bogus-command"])
    assert exit_info.value.code == 2, "argparse usage errors keep exit code 2"


def test_facts_export_backup_restore_reparse(tmp_path, db_path, capsys):
    root = tmp_path / "export"
    root.mkdir()
    _build_export(root)
    assert _run(["--db", str(db_path), "import", str(root)], capsys)[0] == cli.EXIT_OK

    code, out, _ = _run(["--db", str(db_path), "--json", "facts", "--days", "1", "--baseline", "1"], capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_OK and payload["as_of"] == "2025-06-16"
    assert any(f["metric"] == "sleep_score" for f in payload["facts"])
    code, out, _ = _run(["--db", str(db_path), "facts", "sleep_score", "--days", "1", "--baseline", "1"], capsys)
    assert code == cli.EXIT_OK and "sleep_score" in out and "as of 2025-06-16" in out

    code, out, _ = _run(["--db", str(db_path), "export"], capsys)
    assert code == cli.EXIT_OK and out.startswith("date,") and "steps[local]" in out.splitlines()[0]
    code, out, _ = _run(["--db", str(db_path), "export", "--format", "long", "--metric", "steps"], capsys)
    assert code == cli.EXIT_OK and out.splitlines()[0] == "date,metric,unit,source_scope,value"
    code, out, _ = _run(["--db", str(db_path), "export", "--format", "samples", "--metric", "stress"], capsys)
    assert code == cli.EXIT_OK and out.startswith("ts_utc,value,source_scope,unit")
    assert _run(["--db", str(db_path), "export", "--format", "samples"], capsys)[0] == cli.EXIT_USAGE

    snaps = tmp_path / "snaps"
    code, out, _ = _run(["--db", str(db_path), "--json", "backup", "--to", str(snaps), "--note", "t"], capsys)
    manifest = json.loads(out)
    assert code == cli.EXIT_OK and (snaps / manifest["file"]).exists()
    code, out, _ = _run(["--db", str(db_path), "--json", "backups", "--dir", str(snaps)], capsys)
    assert code == cli.EXIT_OK and len(json.loads(out)["backups"]) == 1
    code, _, err = _run(["--db", str(db_path), "restore", str(snaps / manifest["file"])], capsys)
    assert code == cli.EXIT_USAGE and "--yes" in err
    code, out, _ = _run(["--db", str(db_path), "restore", str(snaps / manifest["file"]), "--yes"], capsys)
    assert code == cli.EXIT_OK and "restored from" in out
    code, _, err = _run(["--db", str(db_path), "restore", str(snaps / "missing.db"), "--yes"], capsys)
    assert code == cli.EXIT_BACKUP

    code, out, _ = _run(["--db", str(db_path), "--json", "reparse"], capsys)
    payload = json.loads(out)
    assert code == cli.EXIT_OK and payload["transport"] == "reparse" and payload["files_failed"] == 0
    code, out, _ = _run(["--db", str(db_path), "--json", "status", "--days", "3650"], capsys)
    assert json.loads(out)["recent_imports"][0]["transport"] == "reparse"


def test_stored_error_text_is_redacted(tmp_path, db_path, capsys):
    folder = tmp_path / "drop"
    folder.mkdir()
    (folder / "someone.real@gmail.com_123456789.fit").write_bytes(b"\x00" * 30)
    code, out, _ = _run(["--db", str(db_path), "import", str(folder)], capsys)
    assert code == cli.EXIT_FAILED and "@gmail.com" not in out and "{email}" in out
    code, out, _ = _run(["--db", str(db_path), "--json", "status", "--days", "3650"], capsys)
    text = json.dumps(json.loads(out))
    assert "@gmail.com" not in text and "/Users/" not in text and str(tmp_path) not in text
