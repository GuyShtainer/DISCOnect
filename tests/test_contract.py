"""The contract is a promise; these tests make changing it deliberate."""

from disconect import contract


def test_metric_names_are_unique():
    names = contract.metric_names() + contract.label_names()
    assert len(names) == len(set(names))


def test_every_metric_has_unit_cadence_and_description():
    for item in contract.METRICS:
        assert item.unit and item.description
        assert item.cadence in (contract.CADENCE_SAMPLE, contract.CADENCE_DAILY)


def test_the_missing_value_promise_stays_explicit():
    # Product constraint enforced as a test: changing it must be a conscious act.
    assert "never filled with 0" in contract.MISSING_VALUE_CONVENTION
    assert "no network" in contract.PRIVACY_NOTE.lower()
    assert set(contract.SOURCE_SCOPES) == {"device", "vendor_cloud", "local"}


def test_lookup_helpers():
    assert contract.unit_for("heart_rate") == "bpm"
    assert contract.cadence_for("steps") == "daily"
    assert contract.unit_for("hrv_status") == "label"
    assert contract.unit_for("not_a_metric") is None
    assert contract.as_dict()["contract_version"] == contract.CONTRACT_VERSION
