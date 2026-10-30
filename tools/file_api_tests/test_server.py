import http.client
import struct
import threading
import unittest
import zlib

from server import BINARY, OTA_PAYLOAD, FixtureServer, ota_image


class FixturesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FixtureServer(("127.0.0.1", 0), "test")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def response(self, endpoint):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        self.addCleanup(connection.close)
        connection.request("GET", "/test/" + endpoint)
        return connection.getresponse()

    def test_binary_and_empty(self):
        self.assertEqual(BINARY, self.response("bin?padding=xxx").read())
        self.assertEqual(b"", self.response("zero").read())

    def test_chunked_data_is_decoded_exactly(self):
        response = self.response("chunk")
        self.assertEqual("chunked", response.getheader("Transfer-Encoding"))
        self.assertEqual(OTA_PAYLOAD, response.read())

    def test_valid_ota_header_and_crc(self):
        for endpoint in ("ota", "otac"):
            body = self.response(endpoint).read()
            length, crc = struct.unpack("<II", body[:8])
            self.assertEqual(len(body) - 8, length)
            self.assertEqual(zlib.crc32(body[8:]), crc)

    def test_bad_crc_and_length_are_independent(self):
        bad_crc = self.response("crc").read()
        length, crc = struct.unpack("<II", bad_crc[:8])
        self.assertEqual(len(bad_crc) - 8, length)
        self.assertNotEqual(zlib.crc32(bad_crc[8:]), crc)
        bad_length = self.response("len").read()
        length, crc = struct.unpack("<II", bad_length[:8])
        self.assertNotEqual(len(bad_length) - 8, length)
        self.assertEqual(zlib.crc32(bad_length[8:]), crc)

    def test_error_statuses_have_valid_ota_body_to_detect_false_acceptance(self):
        for status in (404, 500, 206, 301):
            response = self.response("s" + str(status))
            self.assertEqual(status, response.status)
            self.assertEqual(ota_image(), response.read())

    def test_http_length_lie_preserves_valid_ota_envelope(self):
        with self.assertRaises(http.client.IncompleteRead) as caught:
            self.response("lie").read()
        self.assertEqual(ota_image(), caught.exception.partial)

    def test_missing_chunk_terminator_preserves_valid_ota_envelope(self):
        with self.assertRaises(http.client.IncompleteRead) as caught:
            self.response("noterm").read()
        self.assertEqual(ota_image(), caught.exception.partial)

    def test_truncated_body_has_missing_payload(self):
        with self.assertRaises(http.client.IncompleteRead) as caught:
            self.response("trunc").read()
        self.assertEqual(ota_image()[:-17], caught.exception.partial)


if __name__ == "__main__":
    unittest.main()
