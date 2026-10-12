from __future__ import annotations

import socket
import sys

from essl_hub.config import DEVICES, DeviceConfig, password_for


def tcp_check(device: DeviceConfig) -> tuple[bool, str]:
    try:
        with socket.create_connection((device.ip, device.port), timeout=5):
            return True, "TCP port is open"
    except OSError as exc:
        return False, f"TCP port is not reachable: {exc}"


def protocol_check(device: DeviceConfig) -> tuple[bool, str]:
    if device.protocol == "zk-attendance":
        return check_x990(device)
    return check_c3(device)


def check_x990(device: DeviceConfig) -> tuple[bool, str]:
    from zk import ZK

    try:
        password = int(password_for(device) or "0")
    except ValueError:
        return False, f"{device.password_env} must contain digits only"
    failures = []
    for transport, force_udp in (("TCP", False), ("UDP", True)):
        connection = None
        try:
            connection = ZK(
                device.ip,
                port=device.port,
                timeout=8,
                password=password,
                force_udp=force_udp,
                ommit_ping=True,
            ).connect()
            serial = _safe(connection.get_serialnumber)
            firmware = _safe(connection.get_firmware_version)
            return True, f"{transport} protocol connected; serial={serial or 'unknown'}, firmware={firmware or 'unknown'}"
        except Exception as exc:
            failures.append(f"{transport}: {exc}")
        finally:
            if connection is not None:
                try:
                    connection.disconnect()
                except Exception:
                    pass
    return False, "; ".join(failures)


def check_c3(device: DeviceConfig) -> tuple[bool, str]:
    from c3 import C3

    panel = C3(device.ip, port=device.port)
    try:
        if not panel.connect(password_for(device)):
            return False, "C3 controller rejected the session"
        params = panel.get_device_param(["~SerialNumber", "FirmVer", "DeviceName"])
        return True, (
            "C3 protocol connected; "
            f"serial={params.get('~SerialNumber') or 'unknown'}, "
            f"firmware={params.get('FirmVer') or 'unknown'}"
        )
    except Exception as exc:
        return False, str(exc).strip() or exc.__class__.__name__
    finally:
        try:
            panel.disconnect()
        except Exception:
            pass


def _safe(action) -> str:
    try:
        return str(action() or "")
    except Exception:
        return ""


def main() -> int:
    requested = {arg.lower() for arg in sys.argv[1:]}
    devices = [
        device
        for device in DEVICES
        if not requested or device.id.lower() in requested or device.model.lower() in requested
    ]
    if not devices:
        print("Use: python diagnose.py [uppal-x990|gachibowli-c3-400]")
        return 2
    all_ok = True
    for device in devices:
        print(f"\n{device.site} — {device.model} ({device.ip}:{device.port})")
        tcp_ok, tcp_message = tcp_check(device)
        print(f"  Network:  {'PASS' if tcp_ok else 'FAIL'} — {tcp_message}")
        protocol_ok, protocol_message = protocol_check(device)
        print(f"  Protocol: {'PASS' if protocol_ok else 'FAIL'} — {protocol_message}")
        if not protocol_ok:
            print(f"  Check: port {device.port}, {device.password_env}, device communication mode, and other active collector software")
        all_ok = all_ok and protocol_ok
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
