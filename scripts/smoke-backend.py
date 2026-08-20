#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import select
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "sonos-backend"
TIMEOUT_SEC = 45
SONOS_PORT = 1400
CONNECT_TIMEOUT_SEC = 2.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose OmaSonos discovery")
    parser.add_argument(
        "--host",
        action="append",
        default=[],
        metavar="IP",
        help="probe a known Sonos IP on TCP port 1400 (repeatable)",
    )
    return parser.parse_args()


def local_networks() -> list[tuple[str, str, str]]:
    try:
        result = subprocess.run(
            ["ip", "-json", "address", "show"],
            check=True,
            capture_output=True,
            text=True,
        )
        interfaces = json.loads(result.stdout)
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError):
        return []

    networks = []
    for interface in interfaces:
        name = str(interface.get("ifname", "unknown"))
        for address in interface.get("addr_info", []):
            if address.get("family") != "inet":
                continue
            local = str(address.get("local", ""))
            prefix = int(address.get("prefixlen", 32))
            if local:
                subnet = str(ipaddress.ip_interface(f"{local}/{prefix}").network)
                networks.append((name, f"{local}/{prefix}", subnet))
    return networks


def snapshot_hosts(snapshot: dict) -> set[str]:
    return {
        str(room.get("ip", ""))
        for household in snapshot.get("households", [])
        for room in household.get("rooms", [])
        if room.get("ip")
    }


def probe_port(host: str) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, SONOS_PORT), CONNECT_TIMEOUT_SEC):
            return True, "reachable"
    except OSError as exc:
        return False, f"unreachable ({type(exc).__name__}: {exc})"


def print_discovery(label: str, snapshot: dict) -> None:
    discovery = (snapshot.get("status") or {}).get("discovery") or {}
    print(f"{label}:")
    if not discovery:
        print("  no diagnostics returned")
        return
    attempts = discovery.get("attempts", [])
    if not attempts:
        print("  no attempt details returned")
    for attempt in attempts:
        detail = attempt.get("result", "unknown")
        if "target" in attempt:
            detail += f" target={attempt['target']}"
        if "found" in attempt:
            detail += f" found={attempt['found']}"
        if "reason" in attempt:
            detail += f" reason={attempt['reason']}"
        if "error" in attempt:
            detail += f" error={attempt['error']}"
        print(f"  {attempt.get('method', 'unknown')}: {detail}")


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


def stop_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def main() -> int:
    args = parse_args()
    print("network interfaces:")
    networks = local_networks()
    if networks:
        for name, address, subnet in networks:
            print(f"  {name}: {address} subnet={subnet}")
    else:
        print("  unavailable (could not read `ip -json address show`)")

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
        print_discovery("discovery cycle 1 (backend startup)", first)

        assert proc.stdin is not None
        proc.stdin.write('{"id":"smoke","op":"refresh"}\n')
        proc.stdin.flush()

        result = read_json_line(proc, deadline)
        snapshot = read_json_line(proc, deadline)
        if result.get("type") != "result" or result.get("ok") is not True:
            raise RuntimeError(f"Refresh failed: {result}")
        if snapshot.get("type") != "snapshot":
            raise RuntimeError(f"Expected snapshot after refresh, got: {snapshot}")
        print_discovery("discovery cycle 2 (explicit refresh)", snapshot)

        status = snapshot.get("status", {})
        target = snapshot.get("target")
        households = snapshot.get("households", [])
        room_count = sum(len(h.get("rooms", [])) for h in households)
        print(f"backend: ok")
        print(f"status: {status.get('state', 'unknown')}")
        if status.get("message"):
            print(f"message: {status['message']}")
        live_updates = status.get("liveUpdates") or {}
        if live_updates:
            print(
                "live-updates: "
                f"{live_updates.get('mode', 'unknown')}, "
                f"subscriptions {live_updates.get('subscribed', 0)}/"
                f"{live_updates.get('requested', 0)}, "
                f"listener {live_updates.get('listener') or 'unavailable'}"
            )
            for error in live_updates.get("errors", []):
                print(f"live-update-error: {error}")
        hosts = set(args.host) | snapshot_hosts(first) | snapshot_hosts(snapshot)
        print(f"tcp/{SONOS_PORT} probes:")
        if hosts:
            for host in sorted(hosts):
                reachable, detail = probe_port(host)
                marker = "ok" if reachable else "failed"
                print(f"  {host}:{SONOS_PORT}: {marker} - {detail}")
        else:
            print("  no Sonos IP known; rerun with --host SPEAKER_IP")
        print(f"households: {len(households)}")
        print(f"rooms: {room_count}")
        if target:
            print(f"target: {target.get('roomLabel', target.get('groupUid', 'unknown'))}")
        return 0
    except Exception as exc:
        print(f"smoke test failed: {exc}", file=sys.stderr)
        stop_process(proc)
        if proc.stderr is not None:
            err = proc.stderr.read().strip()
            if err:
                print(err, file=sys.stderr)
        return 1
    finally:
        stop_process(proc)


if __name__ == "__main__":
    raise SystemExit(main())
