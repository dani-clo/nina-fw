#!/usr/bin/env python3
"""Test the real OTA handler with injected file errors and native sanitizers."""

from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
PREAMBLE = r'''
#include <cassert>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
static int scenario, opens, closes, unlinks, downloads;
static FILE* active;
static uint32_t crc_update(uint32_t, const void*, size_t);
static FILE* fake_open(const char*, const char*) {
  ++opens;
  if (scenario == 1) { errno = ENOSPC; return nullptr; }
  active = tmpfile(); assert(active); return active;
}
static int fake_close(FILE* file) {
  assert(file && file == active);
  active = nullptr; ++closes; return fclose(file);
}
static int fake_error(FILE* file) { assert(file); return ferror(file); }
static int fake_unlink(const char*) { ++unlinks; return 0; }
static int fake_rename(const char*, const char*) {
  assert(!active);
  if (scenario == 4) { errno = EACCES; return -1; }
  return 0;
}
static int downloadAndSaveFile(char*, FILE* file, const char*) {
  ++downloads;
  uint8_t payload = 0x42;
  uint32_t header[2] = {scenario == 2 ? 2u : 1u,
    crc_update(0xffffffffu, &payload, 1) ^ 0xffffffffu};
  if (scenario == 3) header[1] ^= 1;
  assert(fwrite(header, sizeof(header), 1, file) == 1);
  assert(fwrite(&payload, 1, 1, file) == 1);
  return 0;
}
#define ESP_LOGI(...) ((void)0)
#define ESP_LOGE(...) ((void)0)
'''

MAIN = r'''
int main(int argc, char** argv) {
  assert(argc == 2); scenario = atoi(argv[1]);
  uint8_t command[260] = {}, response[8] = {};
  const char* url = "http://test/fw";
  command[2] = 1; command[3] = strlen(url); memcpy(command + 4, url, strlen(url));
  if (scenario == 5) command[2] = 0;
  if (scenario == 6) command[2] = 2;
  if (scenario >= 7) {
    command[3] = scenario == 7 ? 129 : scenario == 8 ? 255 : 128;
    memset(command + 4, 'x', command[3]);
  }
  const bool invalid = scenario >= 5 && scenario <= 8;
  const int expected = invalid ? 5 : scenario == 9 ? 0 : scenario;
  assert(downloadOTA(command, response) == 6);
  assert(response[2] == 1 && response[3] == 1 && response[4] == expected);
  assert(!active);
  assert(opens == (invalid ? 0 : 1));
  assert(closes == (invalid || scenario == 1 ? 0 : 1));
  assert(downloads == (invalid || scenario == 1 ? 0 : 1));
  assert(unlinks == (scenario >= 1 && scenario <= 4 ? 1 : 0));
  printf("PASS scenario=%d opens=%d closes=%d unlinks=%d\n", scenario, opens, closes, unlinks);
}
'''

CASES = (
    "success", "open_failure_no_null_FILE_access", "length_failure", "crc_failure",
    "rename_failure_no_double_close", "zero_parameters", "two_parameters",
    "url_129_bytes", "url_255_bytes", "url_128_bytes",
)


def main():
    source = (ROOT / "main/CommandHandler.cpp").read_text()
    body = source[source.index("static const uint32_t crc_table[256]"):
                  source.index("\nint brSetECTrustAnchor(")]
    for original, replacement in (("fopen(", "fake_open("), ("fclose(", "fake_close("),
                                  ("ferror(", "fake_error("), ("unlink(", "fake_unlink("),
                                  ("rename(", "fake_rename(")):
        body = body.replace(original, replacement)
    output = ROOT / "build/file-api-tests/native-ota"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nina-ota-faults-") as temporary:
        code = Path(temporary) / "test.cpp"
        binary = Path(temporary) / "test"
        code.write_text(PREAMBLE + body + MAIN)
        subprocess.run(["g++", "-std=c++11", "-g", "-fsanitize=address,undefined",
                        str(code), "-o", str(binary)], check=True)
        logs = []
        failed = False
        for index, name in enumerate(CASES):
            result = subprocess.run([str(binary), str(index)], capture_output=True, text=True)
            status = "PASS" if result.returncode == 0 else "FAIL"
            message = f"{status} {name}\n{result.stdout}{result.stderr}"
            print(message, end="")
            logs.append(message)
            failed |= result.returncode != 0
        (output / "results.log").write_text("".join(logs))
        return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
