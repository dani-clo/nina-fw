#!/usr/bin/env python3
"""Run real NINA file API regression tests through the FileApiTests sketch."""

import argparse
from datetime import datetime, timezone
import getpass
import json
from pathlib import Path
import secrets
import struct
import subprocess
import sys
import threading
import time

from server import BINARY, OTA_PAYLOAD, FixtureServer, ota_image


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OTA_FILE = "/fs/UPDATE.BIN.LZSS"
OTA_TEMP = OTA_FILE + ".TMP"


class TestFailure(Exception):
    pass


def require(condition, message):
    if not condition:
        raise TestFailure(message)


def padded_url(url, length):
    require(len(url) + 1 <= length, "server URL is too long for this boundary test")
    return url + "?" + "x" * (length - len(url) - 1)


def payload(reply, length=None, allow_legacy=False):
    require("error" not in reply, "sketch: " + reply.get("error", ""))
    if not reply.get("received"):
        raise TimeoutError("No SPI response; reset the board before attempting cleanup")
    require(reply.get("valid"), f"invalid SPI framing: {reply}")
    require(allow_legacy or not reply.get("legacy_error"), "unexpected generic SPI error response")
    data = bytes.fromhex(reply.get("hex", ""))
    require(len(data) == reply.get("length"), "inconsistent SPI payload length")
    if length is not None:
        require(len(data) == length, f"expected {length} payload bytes, got {len(data)}")
    return data


class Board:
    def __init__(self, port, timeout=75):
        try:
            import serial
        except ImportError:
            raise RuntimeError("Install pyserial: python3 -m pip install pyserial") from None
        self.serial = serial.Serial(port, 115200, timeout=0.2, write_timeout=5)
        self.timeout = timeout
        self.counter = 0
        self.pending = bytearray()

    def close(self):
        self.serial.close()

    def call(self, verb, *arguments):
        self.counter += 1
        fields = [str(self.counter), verb, *(str(arg) for arg in arguments)]
        if any(any(c in value for c in "\t\r\n\0") for value in fields):
            raise ValueError("Serial arguments cannot contain tabs, CR, LF or NUL")
        wire = ("\t".join(fields) + "\n").encode()
        if len(wire) >= 600:
            raise ValueError("Serial command exceeds sketch buffer")
        # Do not log requests: CONNECT contains the password.
        self.serial.write(wire)
        self.serial.flush()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self.pending.extend(self.serial.read(4096))
            while b"\n" in self.pending:
                line, _, rest = self.pending.partition(b"\n")
                self.pending = bytearray(rest)
                text = line.decode(errors="replace").strip()
                if not text.startswith("NINA_TEST "):
                    continue
                try:
                    reply = json.loads(text[len("NINA_TEST "):])
                except json.JSONDecodeError:
                    continue
                if reply.get("id") == self.counter:
                    return reply
        raise TimeoutError(f"No response to {verb}. Load FileApiTests, close other serial monitors, or reset the board.")


class Suite:
    def __init__(self, board, server, base_url, report, token):
        self.board = board
        self.server = server
        self.base_url = base_url
        self.report = report
        self.token = token
        self.filename = "nt_" + token + ".bin"
        self.path = "/fs/" + self.filename
        self.results = []
        self.own_file = False
        self.own_ota_files = False
        self.unresponsive = False
        self.save()

    def save(self):
        document = {
            "token": self.token, "base_url": self.base_url,
            "file": self.path, "owns_file": self.own_file,
            "owns_ota_files": self.own_ota_files, "results": self.results,
        }
        self.report.write_text(json.dumps(document, indent=2) + "\n")

    def check(self, name, action):
        started = time.monotonic()
        try:
            action()
        except TestFailure as error:
            result = {"name": name, "status": "FAIL", "detail": str(error)}
        except Exception as error:
            self.results.append({"name": name, "status": "ERROR", "detail": str(error)})
            self.save()
            raise
        else:
            result = {"name": name, "status": "PASS"}
        result["seconds"] = round(time.monotonic() - started, 3)
        self.results.append(result)
        self.save()
        print(f"{result['status']:4} {name}" + (": " + result["detail"] if "detail" in result else ""), flush=True)

    def exists(self, path):
        # Strict parsing checks END_CMD as well as the four-byte payload.
        data = payload(self.board.call("EXISTS", path), 4)
        return struct.unpack("<i", data)[0]

    def remove(self, path):
        if self.exists(path) >= 0:
            # DELETE_FILE currently returns the generic error frame even
            # when unlink succeeds. Verify the observable filesystem state.
            payload(self.board.call("DELETE", path), allow_legacy=True)
        require(self.exists(path) == -1, "file still exists after delete: " + path)

    def read_file(self, path):
        size = self.exists(path)
        require(0 <= size <= 4096, f"unexpected test file size: {size}")
        data = bytearray()
        while len(data) < size:
            count = min(128, size - len(data))
            data.extend(payload(self.board.call("READ", path, len(data), count), count))
        return bytes(data)

    def heartbeat(self):
        require(payload(self.board.call("PING")), "empty firmware version response")

    def file_case(self, endpoint, expected, boundary=False):
        self.remove(self.path)
        url = self.base_url + "/" + endpoint
        if boundary:
            url = padded_url(url, 64)
        require(len(url) <= 64, "DOWNLOAD_FILE URL exceeds its 64-byte buffer")
        before = self.server.request_count()
        # DOWNLOAD_FILE also returns a generic error frame unconditionally.
        payload(self.board.call("DOWNLOAD", url, self.filename), allow_legacy=True)
        if self.server.request_count() <= before:
            raise RuntimeError("Fixture server was not reached; check PC IP, LAN and firewall before continuing")
        require(self.read_file(self.path) == expected, "downloaded file content differs from expectation")
        self.heartbeat()

    def clear_ota(self):
        self.remove(OTA_FILE)
        self.remove(OTA_TEMP)

    def ota_case(self, url, expected, arguments=1, expect_request=True):
        self.clear_ota()
        before = self.server.request_count()
        code = payload(self.board.call("OTA", url, arguments), 1)[0]
        if expect_request:
            if self.server.request_count() <= before:
                raise RuntimeError("Fixture server was not reached; check PC IP, LAN and firewall")
        else:
            require(self.server.request_count() == before, "invalid arguments unexpectedly reached HTTP server")
        if expected == "reject":
            require(code != 0,
                    "OTA accepted an invalid HTTP response: downloadOTA() ignores downloadAndSaveFile()'s return value")
        else:
            require(code == expected, f"OTA code {code}, expected {expected}")
        if code == 0:
            require(self.read_file(OTA_FILE) == ota_image(), "stored OTA bytes differ")
        else:
            require(self.exists(OTA_FILE) == -1, "failed OTA left a final update file")
        require(self.exists(OTA_TEMP) == -1, "temporary OTA file was not removed/renamed")
        self.heartbeat()

    def rename_failure(self):
        self.ota_case(self.base_url + "/ota", 0)
        original = self.read_file(OTA_FILE)
        # SPIFFS refuses rename onto an existing name: this deterministically
        # exercises ERR_RENAME and the former double-fclose path.
        code = payload(self.board.call("OTA", self.base_url + "/ota", 1), 1)[0]
        require(code == 4, f"expected ERR_RENAME=4, got {code}")
        require(self.read_file(OTA_FILE) == original, "rename error changed the existing OTA file")
        require(self.exists(OTA_TEMP) == -1, "rename error left temporary file")
        self.heartbeat()

    def run_files(self, repeat):
        require(self.exists(self.path) == -1, "test filename already exists; refusing to overwrite")
        self.own_file = True
        self.save()
        self.check("exists_missing_frame_and_minus_one", lambda: require(self.exists(self.path) == -1, "missing file sentinel"))
        self.check("empty_file_and_exists_zero", lambda: self.file_case("zero", b""))
        self.check("binary_download_and_readback", lambda: self.file_case("bin", BINARY))
        self.check("chunked_download_crosses_128_byte_buffers", lambda: self.file_case("chunk", OTA_PAYLOAD))
        self.check("download_url_exactly_64_bytes", lambda: self.file_case("bin", BINARY, boundary=True))
        for code in (404, 500, 206, 301, 204):
            self.check(f"file_rejects_http_{code}", lambda code=code: self.file_case("s" + str(code), b""))
        for iteration in range(repeat):
            self.check(f"repeat_chunked_{iteration+1}", lambda: self.file_case("chunk", OTA_PAYLOAD))

    def run_ota(self):
        # No backups or silent removal of a previously staged real update.
        require(self.exists(OTA_FILE) == -1 and self.exists(OTA_TEMP) == -1,
                "existing OTA file found; OTA tests stopped without touching it")
        self.own_ota_files = True
        self.save()
        for name, endpoint, expected in [
            ("ota_valid_length_crc", "ota", 0),
            ("ota_chunked_valid", "otac", 0),
            ("ota_wrong_embedded_length", "len", 2),
            ("ota_wrong_crc", "crc", 3),
            ("ota_truncated_body", "trunc", 2),
            ("ota_rejects_http_404", "s404", "reject"),
            ("ota_rejects_http_500", "s500", "reject"),
            ("ota_rejects_http_206", "s206", "reject"),
            ("ota_rejects_http_301", "s301", "reject"),
            ("ota_http_length_exceeds_valid_ota_body", "lie", "reject"),
            ("ota_chunked_missing_terminator", "noterm", "reject"),
        ]:
            self.check(name, lambda endpoint=endpoint, expected=expected:
                       self.ota_case(self.base_url + "/" + endpoint, expected))
        self.check("ota_url_exactly_128_bytes", lambda:
                   self.ota_case(padded_url(self.base_url + "/ota", 128), 0))
        for length in (129, 255):
            self.check(f"ota_firmware_rejects_url_{length}_bytes", lambda length=length:
                       self.ota_case("http://" + "x" * (length - 7), 5, expect_request=False))
        for count in (0, 2):
            self.check(f"ota_firmware_rejects_{count}_parameters", lambda count=count:
                       self.ota_case(self.base_url + "/ota", 5, arguments=count, expect_request=False))
        self.check("empty_url_no_http_request", lambda: self.ota_case("", "reject", expect_request=False))
        self.check("malformed_url_no_http_request", lambda: self.ota_case("not-a-url", "reject", expect_request=False))
        self.check("ota_rename_failure_cleanup_and_heartbeat", self.rename_failure)

    def cleanup(self):
        errors = []
        failed_paths = []
        paths = ([self.path] if self.own_file else []) + ([OTA_FILE, OTA_TEMP] if self.own_ota_files else [])
        if self.unresponsive:
            print("Board unresponsive; cleanup deferred. Use cleanup.py with " + str(self.report), file=sys.stderr)
            return ["cleanup deferred after transport timeout"] if paths else []
        for path in paths:
            try:
                self.remove(path)
            except Exception as error:
                errors.append(f"{path}: {error}")
                failed_paths.append(path)
        if self.path not in failed_paths:
            self.own_file = False
        if OTA_FILE not in failed_paths and OTA_TEMP not in failed_paths:
            self.own_ota_files = False
        self.save()
        if errors:
            print("Cleanup incomplete: " + "; ".join(errors), file=sys.stderr)
        return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyACM0", help="board serial port")
    parser.add_argument("--server-ip", required=True, help="PC IPv4 address reachable from the board; not localhost")
    parser.add_argument("--http-port", type=int, default=8000)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--ssid", help="Wi-Fi SSID; prompted if omitted")
    parser.add_argument("--ota", action="store_true", help="also stage synthetic OTA files; never apply them; refuse preexisting OTA files")
    parser.add_argument("--repeat", type=int, default=3, help="additional chunked downloads, 0..50; not a leak measurement")
    parser.add_argument("--upload", action="store_true", help="compile/upload FileApiTests, replacing the RP2040 application sketch")
    parser.add_argument("--fqbn", default="arduino:mbed_nano:nanorp2040connect")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 0 <= args.repeat <= 50:
        parser.error("--repeat must be between 0 and 50")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output or ROOT / "build/file-api-tests" / stamp
    output.mkdir(parents=True, exist_ok=True)
    if args.upload:
        build = output / "sketch"
        subprocess.run(["arduino-cli", "compile", "--fqbn", args.fqbn, "--build-path", str(build), str(HERE / "FileApiTests")], check=True)
        subprocess.run(["arduino-cli", "upload", "--fqbn", args.fqbn, "--port", args.port, "--input-dir", str(build)], check=True)

    ssid = args.ssid if args.ssid is not None else input("SSID: ")
    password = getpass.getpass("Wi-Fi password (empty for open network): ")
    token = secrets.token_hex(3)
    with FixtureServer((args.bind, args.http_port), token, output / "http-requests.jsonl") as server:
        base_url = f"http://{args.server_ip}:{server.server_port}/{token}"
        require(len(base_url + "/bin") <= 63, "server address is too long for URL boundary tests")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        board = None
        suite = None
        cleanup_errors = []
        try:
            board = Board(args.port)
            suite = Suite(board, server, base_url, output / "report.json", token)
            suite.heartbeat()
            connection = board.call("CONNECT", ssid, password)
            require(connection.get("status") == 3, f"Wi-Fi connection failed: {connection}")
            print(f"Board IP: {connection['ip']}; HTTP fixtures: {base_url}", flush=True)
            suite.run_files(args.repeat)
            if args.ota:
                suite.run_ota()
        except (TimeoutError, OSError, KeyboardInterrupt):
            if suite:
                suite.unresponsive = True
            raise
        finally:
            if suite:
                cleanup_errors = suite.cleanup()
            if board:
                board.close()
            server.shutdown()
            thread.join(timeout=5)
        failed = [result for result in suite.results if result["status"] != "PASS"]
        print(f"Results: {len(suite.results)-len(failed)} PASS, {len(failed)} FAIL/ERROR. Report: {output / 'report.json'}")
        return 1 if failed or cleanup_errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, TestFailure, TimeoutError, OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
