#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "sonos-backend"
TIMEOUT_SEC = 25


def read_json_line(proc: subprocess.Popen[str], deadline: float) -> dict:
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        timeout = max(0.0, deadline - time.monotonic())
        readable, _, _ = select.select([proc.stdout], [], [], timeout)
        if not readable:
            break
        line = proc.stdout.readline()
        if line == "":
            break
        line = line.strip()
        if not line:
            continue
        return json.loads(line)
    raise TimeoutError("Timed out waiting for backend JSON")


def main() -> int:
    proc = subprocess.Popen(
        [str(BACKEND)],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    try:
        deadline = time.monotonic() + TIMEOUT_SEC
        first = read_json_line(proc, deadline)
        if first.get("type") != "snapshot":
            raise RuntimeError(f"Expected initial snapshot, got: {first}")

        assert proc.stdin is not None
        proc.stdin.write('{"id":"smoke","op":"refresh"}\n')
        proc.stdin.flush()

        result = read_json_line(proc, deadline)
        snapshot = read_json_line(proc, deadline)
        if result.get("type") != "result" or result.get("ok") is not True:
            raise RuntimeError(f"Refresh failed: {result}")
        if snapshot.get("type") != "snapshot":
            raise RuntimeError(f"Expected snapshot after refresh, got: {snapshot}")

        status = snapshot.get("status", {})
        target = snapshot.get("target")
        households = snapshot.get("households", [])
        room_count = sum(len(h.get("rooms", [])) for h in households)
        print(f"backend: ok")
        print(f"status: {status.get('state', 'unknown')}")
        if status.get("message"):
            print(f"message: {status['message']}")
        print(f"households: {len(households)}")
        print(f"rooms: {room_count}")
        if target:
            print(f"target: {target.get('roomLabel', target.get('groupUid', 'unknown'))}")
        return 0
    except Exception as exc:
        print(f"smoke test failed: {exc}", file=sys.stderr)
        if proc.stderr is not None:
            err = proc.stderr.read().strip()
            if err:
                print(err, file=sys.stderr)
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
