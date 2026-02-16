#!/usr/bin/env python3
"""Reusable persistent Tau client library.

Provides:
- Minimal OSC encode/decode (no external deps)
- Tau process bootstrap helpers
- Persistent UDP listeners for spider/daemon channels
- Handshakes: /ping->/pong and /send-pid-to-daemon->/tau/pid
- Timed scheduling helpers for OSC and MIDI
"""

from __future__ import annotations

import os
import platform
import socket
import struct
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

NTP_EPOCH_OFFSET = 2208988800


def _pad4(n: int) -> int:
    return (4 - (n % 4)) % 4


def _pack_str(s: str) -> bytes:
    b = s.encode("utf-8") + b"\x00"
    return b + (b"\x00" * _pad4(len(b)))


def _pack_blob(b: bytes) -> bytes:
    out = struct.pack(">i", len(b)) + b
    return out + (b"\x00" * _pad4(len(out)))


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
            payload.extend(_pack_str(arg))
        elif isinstance(arg, (bytes, bytearray)):
            tags.append("b")
            payload.extend(_pack_blob(bytes(arg)))
        elif isinstance(arg, tuple) and len(arg) == 2 and arg[0] == "int64":
            tags.append("h")
            payload.extend(struct.pack(">q", int(arg[1])))
        else:
            raise TypeError(f"Unsupported OSC arg type: {type(arg)} ({arg!r})")

    return _pack_str(address) + _pack_str("".join(tags)) + bytes(payload)


def _osc_time_from_unix(unix_ts: float) -> bytes:
    t = unix_ts + NTP_EPOCH_OFFSET
    ipart = int(t)
    frac = t - ipart
    fpart = int(frac * (2**32)) & 0xFFFFFFFF
    return struct.pack(">II", ipart, fpart)


def encode_osc_bundle_at(unix_ts: float, message_bins: Sequence[bytes]) -> bytes:
    out = bytearray()
    out.extend(_pack_str("#bundle"))
    out.extend(_osc_time_from_unix(unix_ts))
    for msg in message_bins:
        out.extend(struct.pack(">i", len(msg)))
        out.extend(msg)
    return bytes(out)


def _read_padded_str(data: bytes, idx: int) -> Tuple[str, int]:
    end = data.find(b"\x00", idx)
    if end < 0:
        raise ValueError("Malformed OSC string")
    s = data[idx:end].decode("utf-8", errors="replace")
    size = (end - idx) + 1
    idx = end + 1 + _pad4(size)
    return s, idx


def _read_blob(data: bytes, idx: int) -> Tuple[bytes, int]:
    if idx + 4 > len(data):
        raise ValueError("Malformed OSC blob")
    ln = struct.unpack(">i", data[idx : idx + 4])[0]
    idx += 4
    blob = data[idx : idx + ln]
    idx += ln
    idx += _pad4(4 + ln)
    return blob, idx


def decode_osc_packet(packet: bytes) -> Tuple[str, List[Any]]:
    address, idx = _read_padded_str(packet, 0)

    if address == "#bundle":
        return "#bundle", [packet]

    tags, idx = _read_padded_str(packet, idx)
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
            s, idx = _read_padded_str(packet, idx)
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
    api_port: int = 5001
    osc_in_udp_port: int = 5000
    spider_port: int = 5002
    daemon_port: int = 51234


@dataclass
class TauRuntime:
    tau_dir: Path
    tau_env: str = "dev"
    phx_port: int = 8002
    daemon_token: int = 424242
    midi_enabled: bool = True
    link_enabled: bool = True
    start_tau: bool = False


class TauClient:
    def __init__(self, runtime: TauRuntime, ports: TauPorts):
        self.runtime = runtime
        self.ports = ports

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

        self._handlers: Dict[str, List[Callable[[List[Any]], None]]] = {}
        self.midi_out_ports: List[str] = []
        self._rpc_lock = threading.Lock()
        self._rpc_pending: Dict[str, Tuple[threading.Event, Dict[str, Any]]] = {}

        self.spider_thread = threading.Thread(target=self._spider_listener, daemon=True)
        self.daemon_thread = threading.Thread(target=self._daemon_listener, daemon=True)

    def add_handler(self, address: str, fn: Callable[[List[Any]], None]) -> None:
        self._handlers.setdefault(address, []).append(fn)

    def start(self) -> None:
        self.spider_thread.start()
        self.daemon_thread.start()
        if self.runtime.start_tau:
            self._start_tau_process()

    def stop(self) -> None:
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

    def __enter__(self) -> "TauClient":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        self.stop()

    def _start_tau_process(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "TAU_ENV": self.runtime.tau_env,
                "MIX_ENV": self.runtime.tau_env,
                "TAU_CUES_ON": "true",
                "TAU_OSC_IN_UDP_LOOPBACK_RESTRICTED": "true",
                "TAU_MIDI_ON": "true",
                "TAU_LINK_ON": "true",
                "TAU_OSC_IN_UDP_PORT": str(self.ports.osc_in_udp_port),
                "TAU_API_PORT": str(self.ports.api_port),
                "TAU_SPIDER_PORT": str(self.ports.spider_port),
                "TAU_DAEMON_PORT": str(self.ports.daemon_port),
                "TAU_MIDI_ENABLED": "true" if self.runtime.midi_enabled else "false",
                "TAU_LINK_ENABLED": "true" if self.runtime.link_enabled else "false",
                "TAU_DAEMON_TOKEN": str(self.runtime.daemon_token),
                "TAU_PHX_PORT": str(self.runtime.phx_port),
                "SECRET_KEY_BASE": "tau-client-lib-secret-key-base-not-for-prod",
                "TAU_LOG_PATH": str(self.runtime.tau_dir / "log" / "tau.log"),
                "TAU_BOOT_LOG_PATH": str(self.runtime.tau_dir / "log" / "tau_stdouterr.log"),
            }
        )

        sysname = platform.system().lower()
        # Tau boot scripts force TAU_MIDI_ENABLED=false in test mode.
        # Start Tau directly in test mode so caller-provided MIDI flags are honored.
        if self.runtime.tau_env == "test":
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

        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.runtime.tau_dir),
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
                    self.tau_ready_event.set()
                    return True
            except socket.timeout:
                pass
            except Exception:
                pass
            time.sleep(1.0)
        return False

    def wait_for_tau_pid(self, timeout_s: float = 30.0) -> Optional[int]:
        deadline = time.time() + timeout_s
        while time.time() < deadline and not self.stop_event.is_set():
            self.send("/send-pid-to-daemon", self.runtime.daemon_token)
            if self.tau_pid_event.wait(timeout=1.0):
                return self.tau_pid
        return None

    def send(self, address: str, *args: Any) -> None:
        packet = encode_osc_message(address, list(args))
        self.api_sock.sendto(packet, ("127.0.0.1", self.ports.api_port))

    def api_rpc(self, method: str, *args: Any, timeout_s: float = 3.0) -> Optional[List[Any]]:
        req_id = str(uuid.uuid4())
        ev = threading.Event()
        slot: Dict[str, Any] = {"reply": None}
        with self._rpc_lock:
            self._rpc_pending[req_id] = (ev, slot)
        self.send("/api-rpc", req_id, method, *args)
        ok = ev.wait(timeout=timeout_s)
        with self._rpc_lock:
            self._rpc_pending.pop(req_id, None)
        if not ok:
            return None
        reply = slot.get("reply")
        if isinstance(reply, list):
            return reply
        return None

    def send_bundle_at(self, unix_ts: float, messages: Sequence[bytes]) -> None:
        bundle = encode_osc_bundle_at(unix_ts, messages)
        self.api_sock.sendto(bundle, ("127.0.0.1", self.ports.api_port))

    def send_timed_osc(self, unix_ts: float, host: str, port: int, path: str, args: Sequence[Any]) -> None:
        nested = encode_osc_message(path, list(args))
        cmd = encode_osc_message("/send-after", [host, port, nested])
        self.send_bundle_at(unix_ts, [cmd])

    def send_timed_midi_note_on(self, unix_ts: float, midi_port_name: str, chan: int, note: int, vel: int) -> None:
        midi_nested = encode_osc_message("/note_on", [midi_port_name, chan, note, vel])
        cmd = encode_osc_message("/midi-at", [midi_nested])
        self.send_bundle_at(unix_ts, [cmd])

    def send_timed_midi_note_off(self, unix_ts: float, midi_port_name: str, chan: int, note: int, vel: int = 0) -> None:
        midi_nested = encode_osc_message("/note_off", [midi_port_name, chan, note, vel])
        cmd = encode_osc_message("/midi-at", [midi_nested])
        self.send_bundle_at(unix_ts, [cmd])

    def _dispatch(self, address: str, args: List[Any]) -> None:
        for fn in self._handlers.get(address, []):
            try:
                fn(args)
            except Exception:
                pass

    def _spider_listener(self) -> None:
        while not self.stop_event.is_set():
            try:
                packet, _addr = self.spider_sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return

            try:
                address, args = decode_osc_packet(packet)
            except Exception:
                continue

            if address == "/tau-api-reply" and len(args) >= 2:
                req_id = str(args[1])
                payload = args[2:]
                with self._rpc_lock:
                    pending = self._rpc_pending.get(req_id)
                if pending is not None:
                    ev, slot = pending
                    slot["reply"] = payload
                    ev.set()

            if address == "/midi-outs" and len(args) >= 1:
                self.midi_out_ports = [str(x) for x in args[1:]]

            self._dispatch(address, args)

    def _daemon_listener(self) -> None:
        while not self.stop_event.is_set():
            try:
                packet, _addr = self.daemon_sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return

            try:
                address, args = decode_osc_packet(packet)
            except Exception:
                continue

            if address == "/tau/pid" and len(args) >= 2:
                token = int(args[0])
                pid = int(args[1])
                if token == self.runtime.daemon_token:
                    self.tau_pid = pid
                    self.tau_pid_event.set()

            self._dispatch(address, args)
