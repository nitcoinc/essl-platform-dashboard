from __future__ import annotations

import unittest

from essl_hub.door_params import (
    DoorParamError,
    key_label,
    lock_count_of,
    parse_params,
    read_keys,
    validate_request,
    writable_keys,
)


class ValidateRequestTest(unittest.TestCase):
    """KTD8/R12: one closed whitelist for the web and the collector."""

    def test_rejects_connection_identity_and_verify_type_keys(self) -> None:
        for key in ("ComPwd", "IPAddress", "NetMask", "GATEIPAddress", "RS232BaudRate",
                    "DeviceID", "Door1VerifyType"):
            with self.assertRaisesRegex(DoorParamError, "never written"):
                validate_request({key: 0}, 4)

    def test_rejects_doors_beyond_lock_count_and_unknown_keys(self) -> None:
        with self.assertRaisesRegex(DoorParamError, "4 door"):
            validate_request({"Door5Intertime": 0}, 4)
        with self.assertRaisesRegex(DoorParamError, "2 door"):
            validate_request({"Door3Drivertime": 5}, 2)
        for key in ("Door1Foo", "LockCount", "DateTime", "Door1Intertime,ComPwd"):
            with self.assertRaises(DoorParamError):
                validate_request({key: 0}, 4)

    def test_rejects_out_of_range_values(self) -> None:
        for request in ({"Door1Intertime": 11}, {"Door1Intertime": -1}, {"Door1Drivertime": 0},
                        {"Door1Detectortime": 255}, {"Door1CloseAndLock": 2},
                        {"Door1SensorType": 3}, {"AntiPassback": 7}, {"InterLock": 6}):
            with self.assertRaises(DoorParamError):
                validate_request(request, 4)

    def test_accepts_only_json_integers(self) -> None:
        for value in ("0,ComPwd=1", "0", True, False, 1.0, None, [0]):
            with self.assertRaisesRegex(DoorParamError, "whole number"):
                validate_request({"Door1Intertime": value}, 4)
        for bad in ([("Door1Intertime", 0)], "Door1Intertime=0", None, {1: 0}):
            with self.assertRaises(DoorParamError):
                validate_request(bad, 4)

    def test_refuses_writes_before_the_first_read(self) -> None:
        with self.assertRaisesRegex(DoorParamError, "read the door settings"):
            validate_request({"Door1Intertime": 0}, None)
        self.assertEqual(validate_request({}, None), {})

    def test_returns_a_clean_copy_at_the_range_edges(self) -> None:
        request = {"Door1Intertime": 0, "Door4Intertime": 10, "Door2Drivertime": 254,
                   "Door3SensorType": 2, "AntiPassback": 6, "InterLock": 0}
        clean = validate_request(request, 4)
        self.assertEqual(clean, request)
        self.assertIsNot(clean, request)
        self.assertTrue(all(type(v) is int for v in clean.values()))


class HelpersTest(unittest.TestCase):
    def test_read_keys_cover_base_and_every_door(self) -> None:
        keys = read_keys(4)
        self.assertEqual(keys[:4], ["LockCount", "ReaderCount", "AntiPassback", "InterLock"])
        self.assertEqual(len(keys), 4 + 4 * 6)
        self.assertIn("Door4VerifyType", keys)
        self.assertNotIn("Door5Intertime", keys)
        self.assertNotIn("Door1VerifyType", writable_keys(4))
        self.assertEqual(len(writable_keys(1)), 2 + 5)

    def test_parse_params_turns_strings_into_ints_and_drops_junk(self) -> None:
        raw = {"LockCount": "4", "Door1Intertime": " 1 ", "InterLock": "0", "DeviceName": "C3-400",
               "Door1Drivertime": "5,ComPwd=1"}
        self.assertEqual(parse_params(raw), {"LockCount": 4, "Door1Intertime": 1, "InterLock": 0})

    def test_lock_count_and_labels(self) -> None:
        self.assertEqual(lock_count_of({"LockCount": 4}), 4)
        for params in (None, {}, {"LockCount": "4"}, {"LockCount": True}, {"LockCount": 9}):
            self.assertIsNone(lock_count_of(params))
        self.assertEqual(key_label("Door1Intertime"), "Punch interval, door 1")
        self.assertEqual(key_label("InterLock"), "Interlock")
        self.assertEqual(key_label("ComPwd"), "ComPwd")


if __name__ == "__main__":
    unittest.main()
