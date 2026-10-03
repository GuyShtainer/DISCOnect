"""The blind relay (ADR 0005, pitch 10): encrypted raw-record bundles between the user's devices.

The relay is a dumb object store the user already controls (a folder that WebDAV, rsync or
Syncthing carries). It sees only ciphertext under an account id derived from the master key:
no record, stream name, device id, date or hash is ever outside the ciphertext. Two things
travel: ``raw_records`` and the coverage claims in ``export_ranges``. Everything else is
recomputed locally by the normal import path.
"""
