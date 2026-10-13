from __future__ import annotations

import json
import os
import socket
import struct
import time
import zlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .config import DeviceConfig, numeric_comm_password, password_for
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
# Access-granted punches. Others are denied swipes (20, 27), exit button
# (202), device start (206) and door/aux alarms (2xx), which are not attendance.
C3_PUNCH_EVENT_TYPES = {"0", "1", "2", "3"}

_C3_HISTORY_AT: dict[str, float] = {}


def _c3_history_due(device_id: str) -> bool:
    """Auto mode pulls the full history at start and then every
    ESSL_C3_HISTORY_HOURS (default 6); the realtime log covers the gaps."""
    hours = float(os.getenv("ESSL_C3_HISTORY_HOURS", "6"))
    last = _C3_HISTORY_AT.get(device_id)
    return last is None or time.monotonic() - last >= hours * 3600


def _c3_is_punch(event_type: str) -> bool:
    return str(event_type).strip() in C3_PUNCH_EVENT_TYPES


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
            if not _c3_is_punch(value("EventType", "event_type")):
                continue
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
            in_out = value("InOutState", "InOutStatus")
            raw_state = " · ".join(
                part
                for part in (
                    f"event {event_type}" if event_type else "",
                    f"in/out {in_out}" if in_out else "",
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
                if record.is_event() and _c3_is_punch(str(int(record.event_type)))
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
                        raw_state=f"{record.event_type} · {record.in_out_state}",
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
    exposed. On a card (punch) event 15 is therefore an exit: on the
    Gachibowli panel 21 of 22 such rows sit between two entries. Anything
    else is left for hours.py to alternate."""
    if in_out_state == 0:
        return "entry"
    if in_out_state == 3:
        return "exit"
    if in_out_state == 15 and event_type is not None and _c3_is_punch(str(event_type)):
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
