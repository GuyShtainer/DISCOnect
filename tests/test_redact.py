from disconect.redact import redact_text


def test_redacts_paths_emails_and_ids():
    assert redact_text("OSError: /Users/someone/GARMIN/Monitor/X.FIT unreadable") == "OSError: {path} unreadable"
    assert redact_text("bad file 'C:\\Users\\me\\export\\a.fit' here") == "bad file '{path}' here"
    assert redact_text("someone.real@gmail.com_123456789.fit failed") == "{email}_{id}.fit failed"
    assert redact_text("serial 3489012345 rejected") == "serial {id} rejected"
    assert redact_text("value 12.3456789 kept") == "value 12.3456789 kept", "decimals are not identifiers"
    assert redact_text("2025-06-15T10:00:00Z stays") == "2025-06-15T10:00:00Z stays"
    assert redact_text(None) is None
