#!/usr/bin/env python3
"""Small, deterministic HTTP fixtures for the NINA file API tests."""

import argparse
import json
import secrets
import socket
import struct
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


BINARY = bytes(range(256)) * 4
OTA_PAYLOAD = bytes((i * 37 + 11) % 256 for i in range(513))


def ota_image(payload=OTA_PAYLOAD, *, wrong_length=False, wrong_crc=False):
    length = len(payload) + int(wrong_length)
    crc = zlib.crc32(payload) ^ int(wrong_crc)
    return struct.pack("<II", length, crc) + payload


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, token=None, log_path=None):
        self.token = token or secrets.token_hex(3)
        self.records = []
        self.record_lock = threading.Lock()
        self.log_path = log_path
        super().__init__(address, Handler)

    def record(self, path, status):
        item = {"time": time.time(), "path": path, "status": status}
        with self.record_lock:
            self.records.append(item)
            if self.log_path:
                with open(self.log_path, "a", encoding="utf-8") as output:
                    output.write(json.dumps(item) + "\n")

    def request_count(self):
        with self.record_lock:
            return len(self.records)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        prefix = "/" + self.server.token + "/"
        if not path.startswith(prefix):
            self.send_error(404)
            return
        name = path[len(prefix):]
        body = ota_image()
        status = 200
        extra_headers = {}
        chunked = False
        terminated = True
        advertised_length = None
        if name == "bin":
            body = BINARY
        elif name == "zero":
            body = b""
        elif name == "chunk":
            body = OTA_PAYLOAD
            chunked = True
        elif name == "otac":
            chunked = True
        elif name == "crc":
            body = ota_image(wrong_crc=True)
        elif name == "len":
            body = ota_image(wrong_length=True)
        elif name == "trunc":
            advertised_length = len(body)
            body = body[:-17]
        elif name == "lie":
            # Valid OTA envelope, incomplete HTTP response: the caller must
            # respect downloadAndSaveFile()'s failure, even if CRC/length pass.
            advertised_length = len(body) + 17
        elif name == "noterm":
            chunked = True
            terminated = False
        elif name in ("s404", "s500", "s206", "s301", "s204"):
            status = int(name[1:])
            if status == 206:
                extra_headers["Content-Range"] = f"bytes 0-{len(body)-1}/{len(body)+100}"
            elif status == 301:
                extra_headers["Location"] = prefix + "ota"
            elif status == 204:
                body = b""
        elif name != "ota":
            self.send_error(404)
            return

        self.server.record(self.path, status)
        self.close_connection = True
        try:
            self.send_response(status)
            self.send_header("Connection", "close")
            self.send_header("Content-Type", "application/octet-stream")
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                length = len(body) if advertised_length is None else advertised_length
                self.send_header("Content-Length", str(length))
            for key, value in extra_headers.items():
                self.send_header(key, value)
            self.end_headers()
            if chunked:
                offset = 0
                for size in (1, 7, 127, 128, 250, 256):
                    chunk = body[offset:offset + size]
                    if not chunk:
                        break
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    offset += len(chunk)
                if offset < len(body):
                    chunk = body[offset:]
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                if terminated:
                    self.wfile.write(b"0\r\n\r\n")
            else:
                self.wfile.write(body)
            self.wfile.flush()
            if not terminated or advertised_length is not None:
                self.connection.shutdown(socket.SHUT_WR)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Expected when NINA rejects a status without reading its body.
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--token", default="nina")
    args = parser.parse_args()
    with FixtureServer((args.bind, args.port), args.token) as server:
        print(f"Fixtures: http://<PC-IP>:{server.server_port}/{server.token}/bin", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
