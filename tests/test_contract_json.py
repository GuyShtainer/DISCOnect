"""The language-neutral contract file the Rust core loads (`projects/disconect-core/contract.json`).

Python's live tables are the source. This test serialises them and demands the committed file be
byte-identical, so a contract change is one deliberate act: edit the Python, run
``python tests/test_contract_json.py --write``, commit both. The Rust crate parses the file with
``include_str!`` (``disconect-core/src/contract.rs``) and its own tests pin what it parsed.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from disconect import contract, coverage, insight, storage  # noqa: E402

CONTRACT_FILE = pathlib.Path(__file__).resolve().parents[2] / "disconect-core" / "contract.json"


def _refinement_texts() -> dict[str, str]:
    """The two coverage ``refinements`` sentences, taken from the live ledger of a v3 and a v1-shaped store."""
    with tempfile.TemporaryDirectory() as folder:
        db_path = pathlib.Path(folder) / "contract.db"
        with storage.open_for_write(db_path, "contract") as conn:
            available = coverage.ledger(conn, "2025-01-01", 1)
            assert available["refinements_available"] is True
            conn.execute("DROP TABLE export_ranges")
            unavailable = coverage.ledger(conn, "2025-01-01", 1)
            assert unavailable["refinements_available"] is False
    return {"available": available["refinements"], "unavailable": unavailable["refinements"]}


def snapshot() -> dict:
    """Every table the read layer quotes, as plain JSON data, in the order the Python code holds it."""
    return {
        "contract_version": contract.CONTRACT_VERSION,
        "conventions": {
            "time": contract.TIME_CONVENTION,
            "missing_values": contract.MISSING_VALUE_CONVENTION,
            "sources": contract.SOURCE_CONVENTION,
            "privacy": contract.PRIVACY_NOTE,
            "coverage": contract.COVERAGE_CONVENTION,
        },
        "source_scopes": list(contract.SOURCE_SCOPES),
        "metrics": [dataclasses.asdict(item) for item in contract.METRICS],
        "labels": [dataclasses.asdict(item) for item in contract.LABELS],
        "streams_for": [{"metric": metric, "source_scope": scope, "streams": list(streams)}
                        for (metric, scope), streams in contract.STREAMS_FOR.items()],
        "sparse_metrics": sorted(contract.SPARSE_METRICS),
        "comparison_targets": [{"metric": metric, "source_scope": scope, "compared_with": other,
                                "compared_scope": other_scope}
                               for (metric, scope), (other, other_scope) in contract.COMPARISON_TARGETS.items()],
        "insight": {"rules": list(insight.RULES),
                    "confidence_bands": [{"min_days": threshold, "label": label}
                                         for threshold, label in insight.CONFIDENCE_BANDS]},
        "coverage_refinements": _refinement_texts(),
    }


def rendered() -> str:
    """The file's exact text: two-space indent, UTF-8, trailing newline."""
    return json.dumps(snapshot(), indent=2, ensure_ascii=False) + "\n"


def test_the_committed_contract_file_is_the_live_python_contract():
    assert CONTRACT_FILE.read_bytes() == rendered().encode("utf-8"), (
        "contract.json is stale: run `python tests/test_contract_json.py --write` and commit it")


def test_the_snapshot_is_not_empty_where_the_rust_core_relies_on_it():
    data = snapshot()
    assert data["metrics"] and data["labels"] and data["streams_for"] and data["comparison_targets"]
    assert [band["label"] for band in data["insight"]["confidence_bands"]] == ["high", "medium", "low"]
    assert data["source_scopes"] == ["device", "vendor_cloud", "local"]
    assert data["coverage_refinements"]["available"] != data["coverage_refinements"]["unavailable"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Print or rewrite projects/disconect-core/contract.json.")
    parser.add_argument("--write", action="store_true", help="rewrite the committed file from the live tables")
    if parser.parse_args().write:
        CONTRACT_FILE.write_text(rendered(), encoding="utf-8")
        print(f"wrote {CONTRACT_FILE.name}: {CONTRACT_FILE.stat().st_size} bytes")
    else:
        sys.stdout.write(rendered())
