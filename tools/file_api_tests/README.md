# NINA file API hardware tests

This directory contains an Arduino test bridge, a Python HTTP fixture server,
and an automated runner for testing the actual NINA firmware. The runner connects
the board to Wi-Fi, sends SPI commands, and checks response framing, file sizes,
and downloaded content byte for byte.

The NINA firmware is not changed by these tools. `--upload` replaces the
**application sketch on the host board**, so reload the application sketch after
testing. Installed WiFiNINA libraries are not modified.

## Running on hardware

### Requirements and board compatibility

- A board running compatible NINA firmware containing the fixes under test.
  Build and flash the appropriate NINA firmware variant for that board first.
- Arduino CLI and the Arduino core for the host board.
- WiFiNINA and Arduino_SpiNINA. The bridge uses the SPI driver exposed by
  `spi_drv.h` and requires the board's `SPIWIFI_ACK` pin to be accessible through
  `digitalRead()`.
- Python 3 and `pyserial` on the computer running the tests.

The SPI tests target NINA firmware rather than a particular host processor.
They can be used with other compatible NINA/WiFiNINA boards by selecting the
correct core, serial port, and `--fqbn`. Board-specific interfaces need additional
support: for example, the MKR Vidor 4000 routes NINA
signals through its FPGA, so this bridge's direct GPIO readiness check requires
adaptation. Having a NINA module alone does not guarantee compatibility with
these firmware commands.

Install the Python dependency, for example in a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install pyserial
```

The computer and board must be able to communicate over the network. Use a
2.4 GHz Wi-Fi network accessible to the board and the **computer's reachable IPv4
address**, not `127.0.0.1` or the board's address. Firewalls and Wi-Fi client
isolation must allow TCP connections to the computer on the selected port,
8000 by default.

### Commands

From the repository root, close other serial monitors and run the following,
replacing the board identifier, port, computer address, and SSID:

```sh
python3 tools/file_api_tests/run.py \
  --upload \
  --fqbn VENDOR:ARCHITECTURE:BOARD \
  --port SERIAL_PORT \
  --server-ip COMPUTER_IPV4_ADDRESS \
  --ssid 'YOUR_SSID' \
  --ota
```

For example, for a Nano RP2040 Connect on a Linux computer:

```sh
python3 tools/file_api_tests/run.py \
  --upload \
  --fqbn arduino:mbed_nano:nanorp2040connect \
  --port /dev/ttyACM0 \
  --server-ip 192.168.1.10 \
  --ssid 'YOUR_SSID' \
  --ota
```

The password is prompted without echo and is not saved in the logs. Press Enter
for an open network. The HTTP server starts and stops automatically. Omit
`--upload` if the current bridge sketch is already loaded. Upload it again after
updating the sketch source. Before each run, the bridge disconnects the previous
Wi-Fi session and waits for a final connection result, including when
`WiFi.begin()` returns a transient disconnected status. Failed connections also
report NINA's disconnect reason code. The default FQBN is
`arduino:mbed_nano:nanorp2040connect`; specify `--fqbn` when uploading to another
board. If automatic upload reset fails, use the board's normal bootloader entry
procedure and retry. For the Nano RP2040 Connect, this is a double reset to enter
RPI-RP2 mode.

`--http-port` selects a different HTTP port. `--repeat 10` adds ten repeated
chunked downloads, with a maximum of 50. Without `--ota`, only ordinary file
tests run. With the default repeat count there are 13 hardware tests, or 32 when
`--ota` is enabled.

The runner prints `PASS`, `FAIL`, or `ERROR` and saves `report.json` and HTTP
request logs under `build/file-api-tests/<timestamp>/`. It exits with status 0
only if all tests and cleanup succeed. Transport errors interrupt the suite.

## Test coverage

| Test | Coverage |
|---|---|
| Missing, empty, and binary files; readback and deletion | Four-byte size payload, correct SPI terminator, and file content verification |
| Download URL exactly 64 bytes long | `downloadFile()` URL buffer boundary |
| Content-Length and chunked downloads, crossing 128-byte buffers | Complete reads and byte-for-byte comparison |
| HTTP 404, 500, 206, 301, and 204 | Response rejection without saving the body, including otherwise valid OTA data |
| Valid OTA, incorrect CRC, incorrect length, and truncated body | OTA envelope validation and temporary file cleanup |
| OTA URLs of 128, 129, and 255 bytes; zero/two parameters | Firmware argument validation via direct SPI; invalid arguments return error 5 without an HTTP request |
| Empty or malformed URL | No request to the fixture server, OTA failure, and a responsive module |
| Existing OTA destination | Rename error 4, unchanged existing file, temporary file cleanup, and a responsive module |
| Complete OTA envelope with excessive HTTP Content-Length, or a missing final chunk | HTTP download failure must be respected even when the OTA envelope has a valid length and CRC |

### Current API limitations

`downloadAndSaveFile()` runs **inside the NINA module**. The host sketch reaches
it through `DOWNLOAD_FILE` and `DOWNLOAD_OTA`. The bridge uses WiFiNINA's SPI
driver and explicitly checks the response frame terminator.

`downloadFile()` ignores the downloader's return value and always returns zero
to the command dispatcher, which produces a generic SPI error frame.
`DELETE_FILE` uses that frame too. The runner distinguishes this from a download
result and checks the filesystem directly. A rejected HTTP response can leave an
empty ordinary file; that is not treated as a successful download.

`downloadOTA()` also ignores the return value of `downloadAndSaveFile()`.
Consequently, `ota_http_length_exceeds_valid_ota_body` and
`ota_chunked_missing_terminator` can report **FAIL** on the current firmware:
the OTA handler can accept a body with a valid CRC even though the downloader
rejected the incomplete HTTP response. The runner reports this rather than
masking it as PASS or SKIP. Fixing it requires a firmware change, which these
tools do not apply.

The ordinary `DOWNLOAD_FILE` command still has a 64-byte URL buffer without a
firmware check for longer arguments. The bridge rejects those requests. URLs
of 129 and 255 bytes are sent **only to DOWNLOAD_OTA**, which validates the
parameter count and URL length before copying the URL.

## OTA files and cleanup

Ordinary tests use a single `/fs/nt_<token>.bin` file, with a new token for each
run. Optional OTA tests use the firmware's fixed filenames:

```text
/fs/UPDATE.BIN.LZSS
/fs/UPDATE.BIN.LZSS.TMP
```

**If either file exists before the OTA test group, the runner stops without
touching it.** Check whether it belongs to a real pending update. The runner
does not silently delete or back up existing updates.

OTA fixture data is synthetic; the tests never call `applyOTA()`. Final cleanup
removes files created by the test. If the process is interrupted, the board
resets, or communication is lost, inspect the report and finish cleanup **before
reloading the application sketch**:

```sh
# The board must still run FileApiTests. Reset it first if it was unresponsive.
python3 tools/file_api_tests/cleanup.py \
  --port /dev/ttyACM0 \
  --report build/file-api-tests/<timestamp>/report.json
```

`cleanup.py` removes only names recorded as owned by that run. Do not use an old
incomplete report after staging a new real OTA update. NVS, certificates, and
unrelated application files are not erased.

## Host-side fault injection

Some failures cannot be triggered over SPI without modifying the firmware or
altering the board's state. These tests compile the **current repository source**,
replacing external dependencies with fixtures:

```sh
# Requires gcc/g++ with AddressSanitizer and UndefinedBehaviorSanitizer.
python3 tools/file_api_tests/native_fault_tests.py
python3 tools/file_api_tests/native_ota_tests.py
```

The first script runs 25 cases against `main/http_client.c`: NULL/empty URL,
allocation failure, missing/undersized certificate partition, mmap failure,
bundle registration failure, HTTP setup and connection failures, headers,
status codes, incomplete bodies, read/write errors, PEM cleanup, and retained
bundle mappings. The second extracts `downloadOTA()` and its CRC function to run
10 cases, including failed `fopen`, failed rename, and argument boundaries.
Results are saved under `build/file-api-tests/native-http/` and `native-ota/`.

These tests use simulated I/O; they do not execute ESP-IDF or a real TLS
handshake. The short-partition test covers the guard before `memcmp`; it does
not establish that arbitrary corrupt PEM or bundle contents are handled safely.

The hardware suite does not measure heap or mmap pages. Repeated downloads check
content and module availability, not whether the remaining mapping leak is
resolved. Measuring that requires an internal NINA test or QEMU. The local
hardware suite uses HTTP. PEM and bundle ownership are checked with simulated
I/O; a real HTTPS test also requires an appropriate server and certificates.

## Checking the tools without hardware

```sh
python3 -m unittest discover -s tools/file_api_tests -p 'test_*.py' -v

# Example compilation target; substitute the FQBN for another board.
arduino-cli compile --fqbn arduino:mbed_nano:nanorp2040connect tools/file_api_tests/FileApiTests
```

The Python tests exercise the fixture server on loopback, chunked and truncated
responses, CRC envelopes, and the runner's detection of framing errors and
incorrect OTA acceptance. The server can also be started independently to
inspect its fixtures:

```sh
python3 tools/file_api_tests/server.py --bind 127.0.0.1 --port 8000 --token nina
# Example: http://127.0.0.1:8000/nina/bin
```
