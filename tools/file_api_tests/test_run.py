from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest

from run import Suite, TestFailure, padded_url, payload


def reply(data, *, valid=True, legacy=False):
    return {"received": True, "valid": valid, "legacy_error": legacy,
            "length": len(data), "hex": data.hex(), "end": 238 if valid else 0}


class FakeServer:
    def __init__(self):
        self.count = 0

    def request_count(self):
        return self.count


class AcceptsBadHTTPBoard:
    """Model the observable bug: valid OTA envelope accepted despite bad HTTP."""
    def __init__(self, server):
        self.server = server

    def call(self, verb, *args):
        if verb == "EXISTS":
            return reply(struct.pack("<i", -1))
        if verb == "OTA":
            self.server.count += 1
            return reply(b"\0")
        raise AssertionError("unexpected command " + verb)


class RunnerTest(unittest.TestCase):
    def test_exact_url_boundaries(self):
        for size in (64, 128):
            self.assertEqual(size, len(padded_url("http://192.168.1.2:8000/token/ota", size)))

    def test_invalid_exists_terminator_is_not_silently_accepted(self):
        with self.assertRaises(TestFailure):
            payload(reply(struct.pack("<i", 123), valid=False), 4)

    def test_generic_download_reply_is_distinguished_from_real_result(self):
        generic = reply(b"", legacy=True)
        with self.assertRaises(TestFailure):
            payload(generic)
        self.assertEqual(b"", payload(generic, allow_legacy=True))

    def test_ota_success_does_not_mask_incomplete_http_response(self):
        with tempfile.TemporaryDirectory() as temporary:
            server = FakeServer()
            suite = Suite(AcceptsBadHTTPBoard(server), server,
                          "http://localhost/token", Path(temporary) / "report.json", "token")
            with redirect_stdout(io.StringIO()):
                suite.check("incomplete_http", lambda: suite.ota_case("http://localhost/token/lie", "reject"))
            self.assertEqual("FAIL", suite.results[0]["status"])
            result = json.loads(suite.report.read_text())
            self.assertEqual("FAIL", result["results"][0]["status"])

    def test_preexisting_ota_is_not_claimed_or_removed(self):
        class ExistingOTABoard:
            def call(self, verb, *args):
                if verb != "EXISTS":
                    raise AssertionError("preexisting OTA must never be modified")
                return reply(struct.pack("<i", 123))

        with tempfile.TemporaryDirectory() as temporary:
            suite = Suite(ExistingOTABoard(), FakeServer(), "http://localhost/token",
                          Path(temporary) / "report.json", "token")
            with self.assertRaises(TestFailure):
                suite.run_ota()
            self.assertFalse(suite.own_ota_files)
            self.assertEqual([], suite.cleanup())


if __name__ == "__main__":
    unittest.main()
