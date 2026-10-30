#!/usr/bin/env python3
"""Remove only files owned by an interrupted run, as recorded in report.json."""

import argparse
import json
from pathlib import Path
import re

from run import Board, OTA_FILE, OTA_TEMP, Suite, require


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyACM0")
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    with open(args.report, encoding="utf-8") as source:
        report = json.load(source)
    token = report["token"]
    require(re.fullmatch("[0-9a-f]{6}", token), "invalid run token")
    path = "/fs/nt_" + token + ".bin"
    require(report["file"] == path, "unexpected test filename in report")
    board = Board(args.port)
    try:
        # Reuse the checked filesystem methods without creating/replacing the report.
        suite = object.__new__(Suite)
        suite.board = board
        suite.heartbeat()
        paths = ([path] if report.get("owns_file") else [])
        if report.get("owns_ota_files"):
            paths.extend((OTA_FILE, OTA_TEMP))
        for filename in paths:
            suite.remove(filename)
            print("Removed:", filename)
        report["owns_file"] = False
        report["owns_ota_files"] = False
        Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    finally:
        board.close()


if __name__ == "__main__":
    main()
