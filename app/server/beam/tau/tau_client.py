#!/usr/bin/env python3
"""
Minimal Tau client for Sonic Pi Tau server.

Features:
- Optionally starts Tau (`boot-lin.sh`/`boot-mac.sh`/`boot-win.bat`).
- Implements Spider<->Tau readiness handshake (`/ping` -> `/pong`).
- Implements Daemon<->Tau PID handshake (`/send-pid-to-daemon` -> `/tau/pid`).
- Listens on Spider and Daemon UDP ports and decodes incoming OSC.
- Sends timed OSC (`/send-after`) and timed MIDI (`/midi-at`) via OSC bundle.

No external dependencies.
"""

from __future__ import annotations

import argparse
import os
import platform
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

NTP_EPOCH_OFFSET = 2208988800


def pad4(n: int) -> int:
    return (4 - (n % 4)) % 4


def pack_osc_string(s: str) -> bytes:
    b = s.encode("utf-8") + b"\x00"
    return b + (b"\x00" * pad4(len(b)))


def pack_osc_blob(b: bytes) -> bytes:
    out = struct.pack(">i", len(b)) + b
    return out + (b"\x00" * pad4(len(out)))


def encode_osc_message(address: str, args: Sequence[Any]) -> bytes:
    if not address.startswith("/"):
        raise ValueError(f"OSC address must start with '/': {address}")

    tags = [","]
    payload = bytearray()

    for arg in args:
        if isinstance(arg, bool):
            tags.append("T" if arg else "F")
        elif isinstance(arg, int):
            tags.append("i")
            payload.extend(struct.pack(">i", arg))
        elif isinstance(arg, float):
            tags.append("f")
            payload.extend(struct.pack(">f", arg))
        elif isinstance(arg, str):
            tags.append("s")
            payload.extend(pack_osc_string(arg))
        elif isinstance(arg, (bytes, bytearray)):
            tags.append("b")
            payload.extend(pack_osc_blob(bytes(arg)))
        elif isinstance(arg, tuple) and len(arg) == 2 and arg[0] == "int64":
            tags.append("h")
            payload.extend(struct.pack(">q", int(arg[1])))
        else:
            raise TypeError(f"Unsupported OSC arg type: {type(arg)} ({arg!r})")

    return pack_osc_string(address) + pack_osc_string("".join(tags)) + bytes(payload)


def osc_time_from_unix(unix_ts: float) -> bytes:
    t = unix_ts + NTP_EPOCH_OFFSET
    ipart = int(t)
    frac = t - ipart
    fpart = int(frac * (2**32)) & 0xFFFFFFFF
    return struct.pack(">II", ipart, fpart)


def encode_osc_bundle_at(unix_ts: float, message_bins: Sequence[bytes]) -> bytes:
    out = bytearray()
    out.extend(pack_osc_string("#bundle"))
    out.extend(osc_time_from_unix(unix_ts))
    for msg in message_bins:
        out.extend(struct.pack(">i", len(msg)))
        out.extend(msg)
    return bytes(out)


def _read_padded_string(data: bytes, idx: int) -> Tuple[str, int]:
    end = data.find(b"\x00", idx)
    if end < 0:
        raise ValueError("Malformed OSC string")
    s = data[idx:end].decode("utf-8", errors="replace")
    size = end - idx + 1
    idx = end + 1 + pad4(size)
    return s, idx


def _read_blob(data: bytes, idx: int) -> Tuple[bytes, int]:
    if idx + 4 > len(data):
        raise ValueError("Malformed OSC blob")
    ln = struct.unpack(">i", data[idx : idx + 4])[0]
    idx += 4
    blob = data[idx : idx + ln]
    idx += ln
    idx += pad4(4 + ln)
    return blob, idx


def decode_osc_packet(packet: bytes) -> Tuple[str, List[Any]]:
    address, idx = _read_padded_string(packet, 0)

    if address == "#bundle":
        # Minimal bundle decode for logging.
        return "#bundle", [packet]

    tags, idx = _read_padded_string(packet, idx)
    if not tags.startswith(","):
        raise ValueError("Malformed OSC type tag string")

    args: List[Any] = []
    for t in tags[1:]:
        if t == "i":
            args.append(struct.unpack(">i", packet[idx : idx + 4])[0])
            idx += 4
        elif t == "f":
            args.append(struct.unpack(">f", packet[idx : idx + 4])[0])
            idx += 4
        elif t == "h":
            args.append(struct.unpack(">q", packet[idx : idx + 8])[0])
            idx += 8
        elif t == "s":
            s, idx = _read_padded_string(packet, idx)
            args.append(s)
        elif t == "b":
            b, idx = _read_blob(packet, idx)
            args.append(b)
        elif t == "T":
            args.append(True)
        elif t == "F":
            args.append(False)
        else:
            args.append(f"<unsupported:{t}>")
    return address, args


@dataclass
class TauPorts:
    api_port: int
    osc_in_udp_port: int
    spider_port: int
    daemon_port: int


class TauClient:
    def __init__(
        self,
        tau_dir: Path,
        ports: TauPorts,
        daemon_token: int,
        tau_env: str,
        phx_port: int,
        start_tau: bool,
        midi_enabled: bool,
        link_enabled: bool,
    ) -> None:
        self.tau_dir = tau_dir
        self.ports = ports
        self.daemon_token = daemon_token
        self.tau_env = tau_env
        self.phx_port = phx_port
        self.start_tau_flag = start_tau
        self.midi_enabled = midi_enabled
        self.link_enabled = link_enabled

        self.proc: Optional[subprocess.Popen[str]] = None

        self.api_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.api_sock.bind(("127.0.0.1", 0))
        self.api_sock.settimeout(1.0)

        self.spider_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.spider_sock.bind(("127.0.0.1", self.ports.spider_port))
        self.spider_sock.settimeout(0.5)

        self.daemon_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.daemon_sock.bind(("127.0.0.1", self.ports.daemon_port))
        self.daemon_sock.settimeout(0.5)

        self.stop_event = threading.Event()
        self.tau_ready_event = threading.Event()
        self.tau_pid_event = threading.Event()

        self.tau_pid: Optional[int] = None
        self.midi_out_ports: List[str] = []

        self.spider_thread = threading.Thread(target=self._spider_listener, daemon=True)
        self.daemon_thread = threading.Thread(target=self._daemon_listener, daemon=True)

    def start(self) -> None:
        self.spider_thread.start()
        self.daemon_thread.start()

        if self.start_tau_flag:
            self._start_tau_process()

    def close(self) -> None:
        self.stop_event.set()
        time.sleep(0.1)

        for sock in (self.api_sock, self.spider_sock, self.daemon_sock):
            try:
                sock.close()
            except OSError:
                pass

        if self.proc is not None:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def _start_tau_process(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "TAU_ENV": self.tau_env,
                "MIX_ENV": self.tau_env,
                "TAU_CUES_ON": "true",
                "TAU_OSC_IN_UDP_LOOPBACK_RESTRICTED": "true",
                "TAU_MIDI_ON": "true",
                "TAU_LINK_ON": "true",
                "TAU_OSC_IN_UDP_PORT": str(self.ports.osc_in_udp_port),
                "TAU_API_PORT": str(self.ports.api_port),
                "TAU_SPIDER_PORT": str(self.ports.spider_port),
                "TAU_DAEMON_PORT": str(self.ports.daemon_port),
                "TAU_MIDI_ENABLED": "true" if self.midi_enabled else "false",
                "TAU_LINK_ENABLED": "true" if self.link_enabled else "false",
                "TAU_DAEMON_TOKEN": str(self.daemon_token),
                "TAU_PHX_PORT": str(self.phx_port),
                "SECRET_KEY_BASE": "tau-client-secret-key-base-not-for-prod",
                "TAU_LOG_PATH": str(self.tau_dir / "log" / "tau.log"),
                "TAU_BOOT_LOG_PATH": str(self.tau_dir / "log" / "tau_stdouterr.log"),
            }
        )

        sysname = platform.system().lower()
        # Tau boot scripts force TAU_MIDI_ENABLED=false in test mode.
        # Start Tau directly in test mode so caller-provided MIDI flags are honored.
        if self.tau_env == "test":
            if "windows" in sysname:
                cmd = ["cmd", "/c", "mix run --no-halt"]
            else:
                cmd = ["sh", "-lc", "mix run --no-halt"]
        elif "windows" in sysname:
            cmd = ["cmd", "/c", "boot-win.bat"]
        elif "darwin" in sysname:
            cmd = ["sh", "boot-mac.sh"]
        else:
            cmd = ["sh", "boot-lin.sh"]

        print(f"[tau-client] starting tau: {' '.join(cmd)} (cwd={self.tau_dir})")
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.tau_dir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def wait_for_pong(self, timeout_s: float = 30.0) -> bool:
        deadline = time.time() + timeout_s
        ping = encode_osc_message("/ping", [])

        while time.time() < deadline and not self.stop_event.is_set():
            self.api_sock.sendto(ping, ("127.0.0.1", self.ports.api_port))
            try:
                packet, _addr = self.api_sock.recvfrom(65535)
                address, _args = decode_osc_packet(packet)
                if address == "/pong":
                    print("[tau-client] received /pong from tau")
                    self.tau_ready_event.set()
                    return True
            except socket.timeout:
                pass
            except Exception as e:
                print(f"[tau-client] ping decode error: {e}")

            time.sleep(1.0)

        return False

    def wait_for_tau_pid(self, timeout_s: float = 30.0) -> Optional[int]:
        deadline = time.time() + timeout_s

        while time.time() < deadline and not self.stop_event.is_set():
            msg = encode_osc_message("/send-pid-to-daemon", [self.daemon_token])
            self.api_sock.sendto(msg, ("127.0.0.1", self.ports.api_port))
            if self.tau_pid_event.wait(timeout=1.0):
                return self.tau_pid

        return None

    def send_timed_osc(self, at_unix_s: float, host: str, port: int, path: str, args: Sequence[Any]) -> None:
        payload = encode_osc_message(path, list(args))
        cmd = encode_osc_message("/send-after", [host, port, payload])
        bundle = encode_osc_bundle_at(at_unix_s, [cmd])
        self.api_sock.sendto(bundle, ("127.0.0.1", self.ports.api_port))

    def send_timed_midi_note_on(
        self,
        at_unix_s: float,
        midi_port_name: str,
        channel: int,
        note: int,
        velocity: int,
    ) -> None:
        # MIDI command transported as OSC blob inside /midi-at.
        midi_cmd = encode_osc_message(
            "/note_on",
            [midi_port_name, channel, note, velocity],
        )
        cmd = encode_osc_message("/midi-at", [midi_cmd])
        bundle = encode_osc_bundle_at(at_unix_s, [cmd])
        self.api_sock.sendto(bundle, ("127.0.0.1", self.ports.api_port))

    def _spider_listener(self) -> None:
        while not self.stop_event.is_set():
            try:
                packet, addr = self.spider_sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return

            try:
                address, args = decode_osc_packet(packet)
            except Exception as e:
                print(f"[spider-listener] decode error from {addr}: {e}")
                continue

            if address == "/midi-outs" and len(args) >= 1:
                # args: ["erlang", *ports]
                ports = [str(x) for x in args[1:]]
                self.midi_out_ports = ports

            print(f"[spider-listener] {address} {args}")

    def _daemon_listener(self) -> None:
        while not self.stop_event.is_set():
            try:
                packet, addr = self.daemon_sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return

            try:
                address, args = decode_osc_packet(packet)
            except Exception as e:
                print(f"[daemon-listener] decode error from {addr}: {e}")
                continue

            if address == "/tau/pid" and len(args) >= 2:
                token = int(args[0])
                pid = int(args[1])
                if token == self.daemon_token:
                    self.tau_pid = pid
                    self.tau_pid_event.set()
            print(f"[daemon-listener] {address} {args}")


class UdpOscPrinter:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.sock.settimeout(0.5)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                packet, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                address, args = decode_osc_packet(packet)
                print(f"[osc-target] from={addr} {address} {args}")
            except Exception as e:
                print(f"[osc-target] decode error: {e}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tau client demo")
    parser.add_argument("--tau-dir", default=str(Path(__file__).resolve().parent), help="Path to app/server/beam/tau")
    parser.add_argument("--start-tau", action="store_true", help="Start tau process using boot script")
    parser.add_argument("--tau-env", default="dev", choices=["dev", "prod", "test"], help="Tau environment")
    parser.add_argument("--api-port", type=int, default=5001)
    parser.add_argument("--osc-in-port", type=int, default=5000)
    parser.add_argument("--spider-port", type=int, default=5002)
    parser.add_argument("--daemon-port", type=int, default=51234)
    parser.add_argument("--phx-port", type=int, default=8002)
    parser.add_argument("--daemon-token", type=int, default=424242)
    parser.add_argument("--disable-midi", action="store_true")
    parser.add_argument("--disable-link", action="store_true")
    parser.add_argument("--midi-port", default=None, help="MIDI output port name for demo note")
    parser.add_argument("--osc-target-port", type=int, default=9123)
    parser.add_argument("--lead-time", type=float, default=2.0, help="Seconds from now for timed messages")
    parser.add_argument("--hold-seconds", type=float, default=8.0, help="How long to keep listeners alive")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    tau_dir = Path(args.tau_dir).resolve()
    ports = TauPorts(
        api_port=args.api_port,
        osc_in_udp_port=args.osc_in_port,
        spider_port=args.spider_port,
        daemon_port=args.daemon_port,
    )

    osc_target = UdpOscPrinter("127.0.0.1", args.osc_target_port)
    osc_target.start()

    client = TauClient(
        tau_dir=tau_dir,
        ports=ports,
        daemon_token=args.daemon_token,
        tau_env=args.tau_env,
        phx_port=args.phx_port,
        start_tau=args.start_tau,
        midi_enabled=not args.disable_midi,
        link_enabled=not args.disable_link,
    )

    try:
        client.start()

        if not client.wait_for_pong(timeout_s=30.0):
            print("[tau-client] ERROR: Tau did not answer /pong within timeout")
            return 2

        tau_pid = client.wait_for_tau_pid(timeout_s=30.0)
        if tau_pid is None:
            print("[tau-client] WARNING: did not receive /tau/pid (daemon handshake)")
        else:
            print(f"[tau-client] tau pid from daemon handshake: {tau_pid}")

        fire_at = time.time() + args.lead_time
        print(f"[tau-client] scheduling messages for t={fire_at:.3f} (in {args.lead_time:.2f}s)")

        # Timed OSC example via Tau scheduler.
        client.send_timed_osc(
            at_unix_s=fire_at,
            host="127.0.0.1",
            port=args.osc_target_port,
            path="/demo/timed-osc",
            args=["hello-from-tau", 123, 0.5],
        )

        # Timed MIDI example via Tau scheduler.
        midi_port = args.midi_port
        if midi_port is None and client.midi_out_ports:
            midi_port = client.midi_out_ports[0]

        if midi_port is not None:
            client.send_timed_midi_note_on(
                at_unix_s=fire_at,
                midi_port_name=midi_port,
                channel=1,
                note=60,
                velocity=100,
            )
            print(f"[tau-client] scheduled timed MIDI note_on on port: {midi_port}")
        else:
            print("[tau-client] timed MIDI demo skipped: no --midi-port provided and no /midi-outs discovered yet")

        print(f"[tau-client] waiting {args.hold_seconds}s for callbacks...")
        time.sleep(args.hold_seconds)
        return 0
    finally:
        client.close()
        osc_target.stop()


if __name__ == "__main__":
    raise SystemExit(main())
