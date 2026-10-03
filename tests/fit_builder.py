"""A tiny FIT *writer* for tests, driven by fitdecode's own profile.

Real FIT files are personal health data and cannot live in the repository, so
fixtures are synthesised. Values are encoded through the same scale/offset,
enum and date_time definitions fitdecode decodes with, so a fixture round-trips
into exactly the Python values the decoder under test will see.

Deliberately minimal: little-endian, scalar fields (or fixed-size arrays when
a list is passed), uint8 developer fields on request, no compressed headers. One definition
per distinct (message, field set), up to the 16 local message slots.
"""

from __future__ import annotations

import datetime
import struct

from fitdecode import profile

FIT_EPOCH = datetime.datetime(1989, 12, 31, tzinfo=datetime.timezone.utc)
_CRC_TABLE = (0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
              0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400)


def crc16(data: bytes, crc: int = 0) -> int:
    """The FIT CRC-16 (public algorithm from the FIT protocol description)."""
    for byte in data:
        tmp = _CRC_TABLE[crc & 0xF]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _CRC_TABLE[byte & 0xF]
        tmp = _CRC_TABLE[crc & 0xF]
        crc = ((crc >> 4) & 0x0FFF) ^ tmp ^ _CRC_TABLE[(byte >> 4) & 0xF]
    return crc


_MESSAGES_BY_NAME = {m.name: m for m in profile.MESSAGE_TYPES.values()}


def _field(mesg, name: str):
    """(field, subfield-or-None): a subfield name (``steps`` under ``cycles``,
    ``garmin_product`` under ``product``) resolves to its parent for sizing."""
    for field in mesg.fields.values():
        if field.name == name:
            return field, None
    for field in mesg.fields.values():
        for subfield in getattr(field, "subfields", None) or ():
            if subfield.name == name:
                return field, subfield
    raise KeyError(f"{mesg.name} has no field {name!r}")


def _base_type(field):
    kind = field.type
    return kind.base_type if hasattr(kind, "base_type") else kind


def _encode_scalar(field, value, subfield=None):
    kind = (subfield or field).type
    base = _base_type(field)
    if isinstance(value, datetime.datetime):
        value = int((value - FIT_EPOCH).total_seconds())
    elif isinstance(value, str) and value.isdigit():
        value = int(value)  # an undocumented enum number, e.g. file type "49"
    elif isinstance(value, str):
        enum = getattr(kind, "enum", None) or {}
        inverse = {name: number for number, name in enum.items()}
        if value not in inverse:
            raise KeyError(f"{field.name}: {value!r} is not a known {kind.name} value")
        value = inverse[value]
    elif isinstance(value, bool):
        value = int(value)
    if base.fmt in "fd":
        return float(value)
    scale = getattr(subfield or field, "scale", None) or 1
    offset = getattr(subfield or field, "offset", None) or 0
    return int(round((value + offset) * scale))


#: fitdecode's uint8 base type (identifier 0x02), for fields the profile does not name.
_UINT8 = profile.BASE_TYPES[0x02]


class _RawField:
    """A field known only by number (no profile entry): sized and encoded as a plain uint8."""

    def __init__(self, def_num: int):
        self.def_num = def_num
        self.name = f"unknown_{def_num}"
        self.type = _UINT8


class FitBuilder:
    """Append messages by profile name, then ``build()`` the bytes."""

    def __init__(self, file_type: str = "monitoring_b", serial: int = 1234567890,
                 created: datetime.datetime | None = None, product: int = 4536):
        self._records = bytearray()
        self._locals: dict[tuple, int] = {}
        self._dev_declared = False
        self.add("file_id", type=file_type, manufacturer="garmin", product=product,
                 serial_number=serial,
                 time_created=created or datetime.datetime(2025, 6, 15, 6, 0, tzinfo=datetime.timezone.utc))

    def describe_dev_field(self, field_num: int, name: str, units: str = "") -> None:
        """Declare a developer field (uint8, developer index 0) the way a Connect IQ app would:
        a ``developer_data_id`` message once, then a ``field_description`` per field."""
        if not self._dev_declared:
            self.add("developer_data_id", developer_data_index=0, application_version=1)
            self._dev_declared = True
        self.add("field_description", developer_data_index=0, field_definition_number=field_num,
                 fit_base_type_id="uint8", field_name=name, units=units)

    def add(self, message: str, raw_uint8: dict[int, int] | None = None,
            dev_uint8: dict[int, int] | None = None, **fields) -> None:
        """Append one message. ``raw_uint8`` adds fields by number that the profile does not
        name (one unsigned byte each), the way the watch writes its undocumented fields;
        ``dev_uint8`` adds developer fields (declared with :meth:`describe_dev_field`)."""
        mesg = _MESSAGES_BY_NAME[message]
        specs = []
        for name, value in fields.items():
            field, subfield = _field(mesg, name)
            base = _base_type(field)
            if base.fmt == "s":                     # a string field: null-terminated UTF-8, size = bytes
                value = value.encode("utf-8") + b"\0"
                count = len(value)
            else:
                count = len(value) if isinstance(value, (list, tuple)) else 1
            specs.append((field, subfield, base, count, value))
        for def_num, value in (raw_uint8 or {}).items():
            specs.append((_RawField(def_num), None, _UINT8, 1, value))
        dev = list((dev_uint8 or {}).items())
        key = (mesg.mesg_num, tuple((f.def_num, c) for f, _, _, c, _ in specs), tuple(n for n, _ in dev))
        local = self._locals.get(key)
        if local is None:
            if len(self._locals) >= 16:
                raise RuntimeError("test builder supports at most 16 distinct definitions")
            local = len(self._locals)
            self._locals[key] = local
            definition = bytearray([(0x60 if dev else 0x40) | local, 0, 0])
            definition += struct.pack("<H", mesg.mesg_num) + bytes([len(specs)])
            for field, _subfield, base, count, _value in specs:
                definition += bytes([field.def_num, struct.calcsize(base.fmt) * count, base.identifier])
            if dev:
                definition += bytes([len(dev)])
                for field_num, _value in dev:
                    definition += bytes([field_num, 1, 0])   # size 1, developer data index 0
            self._records += definition
        data = bytearray([local])
        for field, subfield, base, count, value in specs:
            if base.fmt == "s":
                data += value
                continue
            values = value if isinstance(value, (list, tuple)) else [value]
            for item in values:
                data += struct.pack("<" + base.fmt, _encode_scalar(field, item, subfield))
        for _field_num, value in dev:
            data += struct.pack("<B", int(value))
        self._records += data

    def build(self) -> bytes:
        header = bytearray(struct.pack("<BBHI4s", 14, 0x20, 2140, len(self._records), b".FIT"))
        header += struct.pack("<H", crc16(bytes(header)))
        body = bytes(header) + bytes(self._records)
        return body + struct.pack("<H", crc16(body))
