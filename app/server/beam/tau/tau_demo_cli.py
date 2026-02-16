#!/usr/bin/env python3
"""Demo CLI for tau_client_lib.

Example:
python app/server/beam/tau/tau_demo_cli.py --start-tau --tau-env dev --midi-port "My MIDI Port"
"""

from __future__ import annotations

import argparse
import socket
import threading
import time
from pathlib import Path

from tau_client_lib import TauClient, TauPorts, TauRuntime, decode_osc_packet


class UdpPrinter:
    def __init__(self, host: str, port: int):
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
    p = argparse.ArgumentParser(description="Tau demo CLI")
    p.add_argument("--tau-dir", default=str(Path(__file__).resolve().parent), help="Path to app/server/beam/tau")
    p.add_argument("--start-tau", action="store_true", help="Start tau process via boot script")
    p.add_argument("--tau-env", default="dev", choices=["dev", "prod", "test"])
    p.add_argument("--api-port", type=int, default=5001)
    p.add_argument("--osc-in-port", type=int, default=5000)
    p.add_argument("--spider-port", type=int, default=5002)
    p.add_argument("--daemon-port", type=int, default=51234)
    p.add_argument("--phx-port", type=int, default=8002)
    p.add_argument("--daemon-token", type=int, default=424242)
    p.add_argument("--disable-midi", action="store_true")
    p.add_argument("--disable-link", action="store_true")
    p.add_argument("--midi-port", default=None)
    p.add_argument("--osc-target-port", type=int, default=9123)
    p.add_argument("--lead-time", type=float, default=2.0)
    p.add_argument("--hold-seconds", type=float, default=8.0)
    p.add_argument("--link-test", action="store_true", help="Run Ableton Link probe (prints BPM/measure/phase)")
    p.add_argument("--link-samples", type=int, default=8, help="Number of Link probe samples")
    p.add_argument("--link-interval", type=float, default=1.0, help="Seconds between Link probe samples")
    p.add_argument("--link-quantum", type=float, default=4.0, help="Quantum used for measure/phase computation")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    runtime = TauRuntime(
        tau_dir=Path(args.tau_dir).resolve(),
        tau_env=args.tau_env,
        phx_port=args.phx_port,
        daemon_token=args.daemon_token,
        midi_enabled=not args.disable_midi,
        link_enabled=not args.disable_link,
        start_tau=args.start_tau,
    )
    ports = TauPorts(
        api_port=args.api_port,
        osc_in_udp_port=args.osc_in_port,
        spider_port=args.spider_port,
        daemon_port=args.daemon_port,
    )

    osc_target = UdpPrinter("127.0.0.1", args.osc_target_port)
    osc_target.start()

    with TauClient(runtime=runtime, ports=ports) as client:
        client.add_handler("/tau-ready", lambda a: print(f"[spider] /tau-ready {a}"))
        client.add_handler("/tau/pid", lambda a: print(f"[daemon] /tau/pid {a}"))
        client.add_handler("/midi-outs", lambda a: print(f"[spider] /midi-outs {a}"))
        client.add_handler("/external-osc-cue", lambda a: print(f"[spider] /external-osc-cue {a}"))
        client.add_handler("/link-tempo-change", lambda a: print(f"[spider] /link-tempo-change {a}"))
        client.add_handler("/link-num-peers", lambda a: print(f"[spider] /link-num-peers {a}"))

        if not client.wait_for_pong(timeout_s=30.0):
            print("[demo] ERROR: /pong timeout")
            osc_target.stop()
            return 2
        print("[demo] tau readiness handshake OK (/ping -> /pong)")

        pid = client.wait_for_tau_pid(timeout_s=30.0)
        if pid is None:
            print("[demo] WARNING: daemon PID handshake not received")
        else:
            print(f"[demo] daemon PID handshake OK (/tau/pid={pid})")

        if args.link_test:
            print(
                "[demo] running Link probe "
                f"(samples={args.link_samples}, interval={args.link_interval}s, quantum={args.link_quantum})"
            )
            client.send("/link-enable")
            for i in range(args.link_samples):
                peers_r = client.api_rpc("/link-get-num-peers", timeout_s=2.0)
                tempo_r = client.api_rpc("/link-get-tempo", timeout_s=2.0)
                t_r = client.api_rpc("/link-get-current-time", timeout_s=2.0)

                if peers_r is None or tempo_r is None or t_r is None or len(peers_r) < 1 or len(tempo_r) < 1 or len(t_r) < 1:
                    print(f"[link] sample={i + 1} rpc-timeout or malformed reply")
                    time.sleep(args.link_interval)
                    continue

                peers = int(peers_r[0])
                bpm = float(tempo_r[0])
                now_micros = int(t_r[0])

                beat_r = client.api_rpc(
                    "/link-get-beat-at-time", ("int64", now_micros), float(args.link_quantum), timeout_s=2.0
                )
                phase_r = client.api_rpc(
                    "/link-get-phase-at-time", ("int64", now_micros), float(args.link_quantum), timeout_s=2.0
                )
                if beat_r is None or phase_r is None or len(beat_r) < 1 or len(phase_r) < 1:
                    print(f"[link] sample={i + 1} beat/phase rpc-timeout or malformed reply")
                    time.sleep(args.link_interval)
                    continue

                beat = float(beat_r[0])
                phase = float(phase_r[0])
                measure = int(beat // args.link_quantum) + 1

                print(
                    f"[link] sample={i + 1} peers={peers} bpm={bpm:.3f} "
                    f"measure={measure} phase={phase:.6f} beat={beat:.6f}"
                )
                time.sleep(args.link_interval)

        fire_at = time.time() + args.lead_time
        print(f"[demo] scheduling timed messages for {fire_at:.3f}")

        client.send_timed_osc(
            unix_ts=fire_at,
            host="127.0.0.1",
            port=args.osc_target_port,
            path="/demo/timed-osc",
            args=["hello-from-tau", 123, 0.5],
        )

        midi_port = args.midi_port
        if midi_port is None and client.midi_out_ports:
            midi_port = client.midi_out_ports[0]

        if midi_port is not None:
            client.send_timed_midi_note_on(
                unix_ts=fire_at,
                midi_port_name=midi_port,
                chan=1,
                note=60,
                vel=100,
            )
            client.send_timed_midi_note_off(
                unix_ts=fire_at + 2.0,
                midi_port_name=midi_port,
                chan=1,
                note=60,
                vel=0,
            )
            print(f"[demo] scheduled timed MIDI /note_on and /note_off (+2s) on port: {midi_port}")
        else:
            print("[demo] timed MIDI skipped (no --midi-port and none discovered)")

        print(f"[demo] waiting {args.hold_seconds}s for callbacks")
        time.sleep(args.hold_seconds)

    osc_target.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
