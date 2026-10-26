from __future__ import annotations

import json
import os
import re
import socket
import struct
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from .config import DeviceConfig, numeric_comm_password, password_for
from .door_params import (
    DOOR_REQUEST_TTL,
    READ_BATCH,
    DoorParamError,
    lock_count_of,
    parse_params,
    read_keys,
    validate_request,
)
from .hashing import sha256_hex


@dataclass
class CollectionResult:
    events: list[dict[str, object]]
    transport: str
    serial_number: str = ""
    firmware: str = ""


# The C3 stores bare PINs (9) while the X990 and the roster use NP0009, so C3
# PINs are mapped into the X990 format. Shared cards (Temporary) stay unlinked
# as C3-<pin> so their punches never count for whoever owns NP000<pin>.
C3_ID_FORMAT_DEFAULT = "NP{pin:04d}"
C3_UNLINKED_PINS_DEFAULT = "1,2"


def c3_user_id(pin: str, card: str = "") -> str:
    pin = str(pin or "").strip()
    if not pin:
        return str(card or "")
    unlinked = {
        p.strip() for p in os.getenv("ESSL_C3_UNLINKED_PINS", C3_UNLINKED_PINS_DEFAULT).split(",") if p.strip()
    }
    # PIN 0 means "no user" on the panel; NP0000 would be rejected as an
    # all-zero ID elsewhere, so it joins the unlinked path (KTD3).
    if pin in unlinked or not pin.isdigit() or int(pin) == 0:
        return f"C3-{pin}"
    template = os.getenv("ESSL_C3_ID_FORMAT", "").strip() or C3_ID_FORMAT_DEFAULT
    return template.format(pin=int(pin))


def c3_identity(pin: str, card: str, timestamp: str, door: str, event_type: str) -> dict[str, str]:
    """Fields both the history table and the realtime log report identically,
    so one swipe read by either path dedupes to one fingerprint. The card is
    left out when a PIN exists: the history table returns it ASCII-packed
    (2507221 arrives as 0x37303532), the realtime log as the real number."""
    return {
        "who": str(pin or "") or f"card:{card or ''}",
        "timestamp": str(timestamp or ""),
        "door": str(door or ""),
        "event_type": str(event_type or ""),
    }


# --- C3 table download ------------------------------------------------------
# GETDATA (0x08) on a table bigger than one frame is answered with 0x0D:
#   [flag u8][size u32][size u32][crc32 u32][chunk u32]
# and the client then reads the prepared buffer with 0x0E [offset u32][len u32];
# each 0xC8 reply is [offset u32] + up to `chunk` bytes. Worked out against the
# Gachibowli C3-400 (AC Ver 18.1.2); the vendored library only reads one frame.
C3_CMD_GETDATA = 0x08
C3_CMD_LARGE_DATA = 0x0D
C3_CMD_READ_BUFFER = 0x0E
C3_REPLY_OK = 0xC8
C3_TABLE_TIMEOUT_SECONDS = 60
# Access-granted punches. Others are denied swipes (27), exit button (202),
# device start (206) and door/aux alarms (2xx), which are not attendance.
C3_GRANTED_EVENT_TYPES = {"0", "1", "2", "3"}
# KTD11: event 20 "Too Short Punch Interval" is a swipe the door denied
# because it came inside the punch interval of the previous one, usually the
# second person walking through the first one's opening. The person passed,
# so it counts as attendance, flagged; hours.py collapses same-person repeats.
C3_INTERVAL_TOO_SHORT_EVENT = "20"
C3_INTERVAL_TOO_SHORT_NOTE = "interval too short"
C3_ATTENDANCE_EVENT_TYPES = C3_GRANTED_EVENT_TYPES | {C3_INTERVAL_TOO_SHORT_EVENT}
# KTD5: interlock (25) and anti-passback (24) denials depend on door state or
# swipe history, not on the person. Captured, flagged, only from the device's
# door_denied_since on ("Wait for door close" unchecked).
C3_DOOR_DENIED_NOTES = {"24": "door denied: anti-passback", "25": "door denied: interlock"}
# KTD4: events whose realtime in/out 15 is a real exit (byte 1). Event 20 was
# gated by the 2026-10-26 pre-implementation check; drop it here to undo.
C3_REALTIME_15_EXIT_EVENT_TYPES = frozenset(C3_GRANTED_EVENT_TYPES | {C3_INTERVAL_TOO_SHORT_EVENT})

_C3_HISTORY_AT: dict[str, float] = {}


def _c3_history_due(device_id: str) -> bool:
    """Auto mode pulls the full history at start and then every
    ESSL_C3_HISTORY_HOURS (default 6); the realtime log covers the gaps."""
    hours = float(os.getenv("ESSL_C3_HISTORY_HOURS", "6"))
    last = _C3_HISTORY_AT.get(device_id)
    return last is None or time.monotonic() - last >= hours * 3600


def _c3_is_punch(event_type: str) -> bool:
    """Attendance swipe: access granted, or denied only for punch interval."""
    return str(event_type).strip() in C3_ATTENDANCE_EVENT_TYPES


def _c3_keep(
    event_type: str,
    pin: str,
    card: str,
    timestamp: str = "",
    door_denied_since: datetime | None = None,
) -> bool:
    """Interval-too-short and door-denied rows with neither PIN nor card have
    no identity. Door-denied rows count only at/after ``door_denied_since``."""
    event_type = str(event_type).strip()
    has_identity = str(pin or "").strip() not in ("", "0") or str(card or "").strip() not in ("", "0")
    if event_type in C3_DOOR_DENIED_NOTES:
        if door_denied_since is None or not has_identity:
            return False
        try:
            return datetime.fromisoformat(str(timestamp)) >= door_denied_since
        except ValueError:
            return False
    if not _c3_is_punch(event_type):
        return False
    if event_type == C3_INTERVAL_TOO_SHORT_EVENT:
        return has_identity
    return True


def _c3_note(event_type: str) -> str:
    """The raw_state note day_story.punch_flags turns into a chip."""
    event_type = str(event_type).strip()
    if event_type == C3_INTERVAL_TOO_SHORT_EVENT:
        return C3_INTERVAL_TOO_SHORT_NOTE
    return C3_DOOR_DENIED_NOTES.get(event_type, "")


def _read_c3_frame(sock: socket.socket) -> tuple[int, bytes]:
    def exact(count: int) -> bytes:
        data = b""
        while len(data) < count:
            chunk = sock.recv(count - len(data))
            if not chunk:
                raise ConnectionError("controller closed the connection mid-download")
            data += chunk
        return data

    header = exact(5)
    size = header[3] | header[4] << 8
    body = exact(size + 3)
    return header[2], body[:size]


def parse_c3_table(buffer: bytes, fields: dict[int, tuple[str, str]]) -> list[dict[str, Any]]:
    """[table][field count][field indexes...] then records of [len][value] per field."""
    count = buffer[1]
    order = list(buffer[2 : 2 + count])
    data = memoryview(buffer)[2 + count :]
    records: list[dict[str, Any]] = []
    position = 0
    while position < len(data):
        record: dict[str, Any] = {}
        for index in order:
            name, kind = fields[index]
            length = data[position]
            value = bytes(data[position + 1 : position + 1 + length])
            position += 1 + length
            record[name] = int.from_bytes(value, "little") if kind == "i" else value.decode("ascii", "ignore")
        records.append(record)
    return records


def read_c3_table(panel: Any, table_name: str) -> list[dict[str, Any]]:
    cfg = next((c for c in panel._get_device_data_cfg() if c.name == table_name), None)
    if cfg is None:
        raise ValueError(f"C3 has no {table_name} table")
    fields = {f.index: (f.name, f.type) for f in cfg.fields}
    indexes = sorted(fields)
    sock = panel._sock
    sock.settimeout(C3_TABLE_TIMEOUT_SECONDS)
    panel._send(C3_CMD_GETDATA, [cfg.index, len(indexes), *indexes, 0, 0])
    command, payload = _read_c3_frame(sock)
    if command == C3_REPLY_OK:
        return parse_c3_table(payload, fields) if payload else []
    if command != C3_CMD_LARGE_DATA or len(payload) < 17:
        raise ConnectionError(f"unexpected reply 0x{command:02x} to GETDATA {table_name}")
    total, _, crc, chunk = struct.unpack_from("<IIII", payload, 1)
    buffer = bytearray()
    while len(buffer) < total:
        panel._send(C3_CMD_READ_BUFFER, list(struct.pack("<II", len(buffer), min(chunk, total - len(buffer)))))
        command, part = _read_c3_frame(sock)
        if command != C3_REPLY_OK or struct.unpack_from("<I", part, 0)[0] != len(buffer):
            raise ConnectionError(f"C3 buffer read failed at offset {len(buffer)}")
        if len(part) <= 4:
            raise ConnectionError(f"C3 returned an empty chunk at offset {len(buffer)}")
        buffer += part[4:]
    if zlib.crc32(bytes(buffer)) != crc:
        raise ConnectionError(f"C3 {table_name} download failed its checksum")
    return parse_c3_table(bytes(buffer), fields)


def collect(device: DeviceConfig) -> CollectionResult:
    if device.protocol == "zk-attendance":
        return collect_x990(device)
    if device.protocol == "c3-pullsdk":
        return collect_c3(device)
    raise ValueError(f"Unsupported device protocol: {device.protocol}")


def collect_x990(device: DeviceConfig) -> CollectionResult:
    from zk import ZK

    password_text = password_for(device)
    password = numeric_comm_password(device.site, password_text or "0")

    failures: list[str] = []
    for transport, force_udp in (("TCP", False), ("UDP", True)):
        connection = None
        try:
            client = ZK(
                device.ip,
                port=device.port,
                timeout=8,
                password=password,
                force_udp=force_udp,
                ommit_ping=True,
            )
            connection = client.connect()
            users = connection.get_users()
            names = {str(user.user_id): str(user.name or "") for user in users}
            attendance = connection.get_attendance()
            serial_number = _safe_call(connection.get_serialnumber)
            firmware = _safe_call(connection.get_firmware_version)
            events = []
            for record in attendance:
                raw = {
                    "uid": record.uid,
                    "user_id": str(record.user_id),
                    "timestamp": _format_datetime(record.timestamp),
                    "status": record.status,
                    "punch": record.punch,
                }
                events.append(
                    _normalized_event(
                        device,
                        raw=raw,
                        serial_number=serial_number,
                        user_id=str(record.user_id),
                        employee_name=names.get(str(record.user_id), ""),
                        timestamp=raw["timestamp"],
                        raw_state=f"status {record.status} · punch {record.punch}",
                        raw_direction_byte=int(record.punch) if record.punch is not None else None,
                        trusted_direction=None,  # KTD4: pyzk leaves this byte uninterpreted; always alternate
                    )
                )
            return CollectionResult(events, transport, serial_number, firmware)
        except Exception as exc:
            failures.append(f"{transport}: {_friendly_error(exc)}")
        finally:
            if connection is not None:
                try:
                    connection.disconnect()
                except Exception:
                    pass
    raise ConnectionError("; ".join(failures))


def collect_c3(device: DeviceConfig) -> CollectionResult:
    from c3 import C3
    from c3.utils import C3DateTime

    panel = C3(device.ip, port=device.port)
    try:
        if not panel.connect(password_for(device)):
            raise ConnectionError("controller rejected the PullSDK connection")
        params = panel.get_device_param(["~SerialNumber", "FirmVer", "DeviceName"])
        serial_number = str(params.get("~SerialNumber", "") or "")
        firmware = str(params.get("FirmVer", "") or "")
        try:
            users = panel.get_device_data("user")
        except Exception:
            users = []
        names_by_pin: dict[str, str] = {}
        names_by_card: dict[str, str] = {}
        for user in users:
            value = _record_values(user)
            pin = value("Pin")
            card = value("Cardno", "CardNo", "Card")
            name = value("Name")
            if pin:
                names_by_pin[pin] = name
            if card:
                names_by_card[card] = name

        mode = os.getenv("ESSL_C3_MODE", "auto").strip().lower()
        if mode not in {"auto", "history", "realtime"}:
            raise ValueError("ESSL_C3_MODE must be auto, history, or realtime")
        if mode == "realtime" or (mode == "auto" and not _c3_history_due(device.id)):
            transactions = []
            history_error = ConnectionError("history not due; reading the realtime log")
        else:
            try:
                transactions = read_c3_table(panel, "transaction")
                history_error = None
                _C3_HISTORY_AT[device.id] = time.monotonic()
            except Exception as exc:
                if mode == "history":
                    raise
                transactions = []
                history_error = exc
        events = []
        for row in transactions:
            value = _record_values(row)
            pin = value("Pin")
            card = value("Cardno", "CardNo", "Card")
            raw_time = value("Time_second", "TimeSecond")
            timestamp = ""
            if raw_time:
                try:
                    timestamp = _format_datetime(
                        C3DateTime.from_value(int(raw_time))
                    )
                except (TypeError, ValueError):
                    timestamp = str(raw_time)
            event_type = value("EventType", "event_type")
            if not _c3_keep(event_type, pin, card, timestamp, device.door_denied_since):
                continue
            in_out = value("InOutState", "InOutStatus")
            raw_state = " · ".join(
                part
                for part in (
                    f"event {event_type}" if event_type else "",
                    f"in/out {in_out}" if in_out else "",
                    _c3_note(event_type),
                )
                if part
            )
            events.append(
                _normalized_event(
                    device,
                    raw=dict(row),
                    serial_number=serial_number,
                    user_id=c3_user_id(pin, card),
                    employee_name=names_by_pin.get(pin)
                    or names_by_card.get(card)
                    or "",
                    timestamp=timestamp,
                    raw_state=raw_state,
                    door=value("DoorID", "Door"),
                    reader=value("ReaderID", "Reader"),
                    raw_direction_byte=int(in_out) if in_out.isdigit() else None,
                    trusted_direction=_c3_history_direction(in_out),
                    identity=c3_identity(pin, card, timestamp, value("DoorID", "Door"), event_type),
                )
            )
        if history_error is None:
            return CollectionResult(events, "TCP history", serial_number, firmware)

        try:
            panel.disconnect()
        except Exception:
            pass
        panel = C3(device.ip, port=device.port)
        if not panel.connect(password_for(device)):
            raise ConnectionError(
                f"historical transaction read failed ({history_error}); "
                "realtime fallback could not reconnect"
            )
        batch_size = max(1, min(int(os.getenv("ESSL_C3_RTLOG_BATCH", "250")), 2000))
        for _ in range(batch_size):
            records = panel.get_rt_log()
            realtime_events = [
                record for record in records
                if record.is_event()
                and _c3_keep(
                    str(int(record.event_type)),
                    str(record.pin or ""),
                    str(record.card_no or ""),
                    _format_datetime(record.time_second),
                    device.door_denied_since,
                )
            ]
            if not realtime_events:
                break
            for record in realtime_events:
                pin = str(record.pin or "")
                card = str(record.card_no or "")
                raw = {
                    "pin": pin,
                    "card_no": card,
                    "verified": int(record.verified),
                    "door": record.port_nr,
                    "event_type": int(record.event_type),
                    "in_out_state": int(record.in_out_state),
                    "timestamp": _format_datetime(record.time_second),
                    "source": "realtime_fallback",
                }
                events.append(
                    _normalized_event(
                        device,
                        raw=raw,
                        serial_number=serial_number,
                        user_id=c3_user_id(pin, card),
                        employee_name=names_by_pin.get(pin)
                        or names_by_card.get(card)
                        or "",
                        timestamp=raw["timestamp"],
                        raw_state=" · ".join(
                            part
                            for part in (
                                str(record.event_type),
                                str(record.in_out_state),
                                _c3_note(str(int(record.event_type))),
                            )
                            if part
                        ),
                        door=str(record.port_nr),
                        raw_direction_byte=int(record.in_out_state),
                        trusted_direction=_c3_trusted_direction(
                            int(record.in_out_state), int(record.event_type)
                        ),
                        identity=c3_identity(
                            pin, card, raw["timestamp"], str(record.port_nr), str(int(record.event_type))
                        ),
                    )
                )
        return CollectionResult(events, "TCP realtime", serial_number, firmware)
    except Exception as exc:
        raise ConnectionError(_friendly_error(exc)) from exc
    finally:
        try:
            panel.disconnect()
        except Exception:
            pass


# --- C3 door parameters (U4) -------------------------------------------------
# KTD7: SetDeviceParam is the 0x03 command set_device_datetime already sends
# (consts.Command.DATETIME), one Key=Value per call, trusted only on GETPARAM
# read-back. A 0xC9 reply surfaces from the library as
# ConnectionError("Error <n> received in reply: ..."); anything else is a
# lost connection. The result JSON is documented in essl_hub.door_params.
C3_CMD_SETPARAM = 0x03
_C3_REPLY_ERROR = re.compile(r"^Error (-?\d+) received in reply")
C3_ERROR_TEXT = {-14: "communication password rejected", -13: "command not available"}


@dataclass
class DoorParamsOutcome:
    """What one door step did. ``params``/``read_at`` are the latest read
    (None when the panel was not contacted); ``result`` answers the request
    (None for a plain history-time read); ``previous`` is the pre-write read,
    set only when a SETPARAM was attempted (KTD9)."""

    params: dict[str, int] | None = None
    read_at: datetime | None = None
    result: dict[str, Any] | None = None
    previous: dict[str, int] | None = None


DoorStep = Callable[..., DoorParamsOutcome]


def door_request_expired(requested_at: datetime | None, now: datetime) -> bool:
    """KTD6: the admin confirmed against values that may since have changed."""
    return requested_at is not None and now - requested_at > DOOR_REQUEST_TTL


def read_c3_door_params(panel: Any) -> dict[str, int]:
    """LockCount first (it sets how many doors to ask about), then the
    door keys in GETPARAM batches of at most READ_BATCH."""
    lock_count = lock_count_of(parse_params(panel.get_device_param(["LockCount"])))
    if lock_count is None:
        raise ConnectionError("controller did not report a usable LockCount")
    keys = read_keys(lock_count)
    params: dict[str, int] = {}
    for start in range(0, len(keys), READ_BATCH):
        params.update(parse_params(panel.get_device_param(keys[start : start + READ_BATCH])))
    params["LockCount"] = lock_count
    return params


def c3_door_step(
    device: DeviceConfig,
    request: dict[str, Any] | None,
    requested_at: datetime | None,
    *,
    now: datetime,
) -> DoorParamsOutcome:
    """Read the door parameters and apply ``request`` (KTD6, KTD7, KTD9).

    ``request`` None is a plain read alongside a history read; ``{}`` is
    "Read from device". Raises ConnectionError when the panel cannot be
    reached or read before any write, which leaves the request pending (and
    subject to expiry). Once a SETPARAM was attempted it always returns a
    result and never re-sends."""
    if request is None or requested_at is None:
        request, requested_at = None, None
    elif door_request_expired(requested_at, now):
        return DoorParamsOutcome(result=_door_result(
            "expired", requested_at, now,
            {
                str(key): _door_key("not_attempted", None, value, message="expired, not applied")
                for key, value in request.items()
            },
            message="Expired, not applied: requested more than 15 minutes before the collector reached it",
        ))

    from c3 import C3

    password = password_for(device)
    panel = C3(device.ip, port=device.port)
    try:
        try:
            if not panel.connect(password):
                raise ConnectionError("controller rejected the PullSDK connection")
            current = read_c3_door_params(panel)
        except Exception as exc:
            raise ConnectionError(_scrub(_friendly_error(exc), password)) from None
        if request is None:
            return DoorParamsOutcome(params=current, read_at=now)
        try:
            wanted = validate_request(request, lock_count_of(current))
        except DoorParamError as exc:
            return DoorParamsOutcome(
                params=current, read_at=now,
                result=_door_result("failed", requested_at, now, {}, message=str(exc)),
            )
        if not wanted:
            return DoorParamsOutcome(
                params=current, read_at=now, result=_door_result("read", requested_at, now, {}),
            )

        params = dict(current)
        keys: dict[str, dict[str, Any]] = {}
        attempted = lost = False
        for key, value in wanted.items():
            old = current.get(key)
            if lost:
                keys[key] = _door_key("not_attempted", old, value, message="not sent; connection lost")
                continue
            if old == value:
                keys[key] = _door_key("unchanged", old, value, now=old)
                continue
            attempted = True
            keys[key], lost = _write_door_key(panel, key, value, old)
            if keys[key]["now"] is not None:
                params[key] = keys[key]["now"]
        ok = all(entry["outcome"] in ("applied", "unchanged") for entry in keys.values())
        return DoorParamsOutcome(
            params=params,
            read_at=now,
            result=_door_result(
                "applied" if ok else "failed", requested_at, now, keys,
                message="connection lost; the remaining keys were not sent" if lost else "",
            ),
            previous=dict(current) if attempted else None,
        )
    finally:
        try:
            panel.disconnect()
        except Exception:
            pass


def _write_door_key(panel: Any, key: str, value: int, old: int | None) -> tuple[dict[str, Any], bool]:
    """One SETPARAM then its read-back. Returns the key's entry and whether
    the connection was lost. ``key`` and ``value`` come from validate_request,
    so the frame text is a whitelisted name and a formatted int (KTD8)."""
    try:
        panel._send_receive(C3_CMD_SETPARAM, panel._kv_to_message({key: int(value)}))
    except Exception as exc:
        code = _c3_error_code(exc)
        if code is not None:
            return _door_key("failed", old, value, message=_c3_error_text(code)), False
        if _never_sent(exc):
            return _door_key("not_attempted", old, value, message="not sent; connection lost"), True
        return _door_key("failed", old, value, message="connection lost; value not confirmed"), True
    try:
        back = parse_params(panel.get_device_param([key])).get(key)
    except Exception as exc:
        code = _c3_error_code(exc)
        if code is not None:
            return _door_key("failed", old, value, message=f"read-back refused: {_c3_error_text(code)}"), False
        return _door_key("failed", old, value, message="connection lost before read-back; value not confirmed"), True
    if back is None:
        return _door_key("failed", old, value, message="panel did not report the value after the write"), False
    if back != value:
        return _door_key("failed", old, value, now=back, message=f"panel kept {back}"), False
    return _door_key("applied", old, value, now=back), False


def _door_key(
    outcome: str, old: int | None, new: Any, *, now: int | None = None, message: str = ""
) -> dict[str, Any]:
    return {"outcome": outcome, "old": old, "new": new, "now": now, "message": message}


def _door_result(
    state: str, requested_at: datetime, finished_at: datetime, keys: dict, *, message: str = ""
) -> dict[str, Any]:
    return {
        "state": state,
        "requested_at": _format_datetime(requested_at),
        "finished_at": _format_datetime(finished_at),
        "message": message,
        "keys": keys,
    }


def _c3_error_code(exc: BaseException) -> int | None:
    """The panel's error code from a 0xC9 reply, None for anything else."""
    if not isinstance(exc, ConnectionError):
        return None
    match = _C3_REPLY_ERROR.match(str(exc))
    return int(match.group(1)) if match else None


def _c3_error_text(code: int) -> str:
    return C3_ERROR_TEXT.get(code, f"panel error {code}")


def _never_sent(exc: BaseException) -> bool:
    """The library wraps a failed socket send (BrokenPipeError) as
    "Unexpected connection end"; then the frame never left this host."""
    return isinstance(exc, BrokenPipeError) or isinstance(exc.__cause__, BrokenPipeError)


def _scrub(message: str, password: str | None) -> str:
    """Error text is stored and shown; it never carries the comm password."""
    if password:
        message = message.replace(password, "***")
    return message


def _normalized_event(
    device: DeviceConfig,
    *,
    raw: dict[str, Any],
    serial_number: str,
    user_id: str,
    employee_name: str,
    timestamp: str,
    raw_state: str,
    door: str = "",
    reader: str = "",
    raw_direction_byte: int | None = None,
    trusted_direction: str | None = None,
    identity: dict[str, str] | None = None,
) -> dict[str, object]:
    canonical = json.dumps(
        {
            "device": device.id,
            "raw": identity if identity is not None else raw,
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return {
        "fingerprint": sha256_hex(canonical),
        "device_id": device.id,
        "site": device.site,
        "device_serial": serial_number,
        "device_user_id": user_id,
        "employee_name": employee_name,
        "event_timestamp": timestamp,
        "raw_state": raw_state,
        "door": door,
        "reader": reader,
        "raw_direction_byte": raw_direction_byte,
        "trusted_direction": trusted_direction,
        "raw": raw,
    }


def _c3_trusted_direction(in_out_state: int, event_type: int | None = None) -> str | None:
    """KTD4: this library's InOutDirection enum only decodes bytes 0 and 3 to
    ENTRY/EXIT. A real exit (protocol byte 1) fails its decode and the record
    keeps only the fallback UNKNOWN_UNSUPPORTED (15); the raw byte is not
    exposed. The Gachibowli history table carries only bytes 0 and 1 (14,195
    and 13,958 rows, 2026-10-12 replica), so 15 is an exit for the swipe
    events in C3_REALTIME_15_EXIT_EVENT_TYPES. Door-denied rows (24/25) with
    15 stay unknown until a history read fills them."""
    if in_out_state == 0:
        return "entry"
    if in_out_state == 3:
        return "exit"
    if (
        in_out_state == 15
        and event_type is not None
        and str(event_type).strip() in C3_REALTIME_15_EXIT_EVENT_TYPES
    ):
        return "exit"
    return None


def _c3_history_direction(in_out: str) -> str | None:
    """The history table carries the protocol byte itself: 0 entry, 1 exit."""
    return {"0": "entry", "1": "exit"}.get(str(in_out).strip())


def _record_values(record: dict[str, Any]) -> Callable[..., str]:
    lower = {str(key).lower(): value for key, value in record.items()}

    def value_for(*names: str) -> str:
        for name in names:
            value = record.get(name, lower.get(name.lower()))
            if value not in (None, ""):
                return str(value)
        return ""

    return value_for


def _format_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value or "")


def _safe_call(action: Callable[[], Any]) -> str:
    try:
        return str(action() or "")
    except Exception:
        return ""


def _friendly_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    lowered = message.lower()
    if "timed out" in lowered:
        return "connection timed out; verify the device port, communication key, and network path"
    if "unauth" in lowered:
        return "authentication failed; set the device communication password"
    if "forcibly closed" in lowered or "10054" in lowered:
        return "device rejected the request; verify the configured port and communication key"
    if "insufficient length" in lowered or "expected 5 bytes" in lowered:
        return "controller closed the PullSDK session; verify its communication password and pause other collectors"
    return message
