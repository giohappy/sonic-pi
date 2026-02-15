# TAU Operations Guide

This file describes how Tau is started in Sonic Pi, the expected startup inputs/outputs, and the ping/ack style handshakes with Daemon and Spider.

## 1) How Tau Is Started

## 1.1 Daemon-managed startup (normal Sonic Pi boot)

Sonic Pi `daemon.rb` starts Tau via `TauBooter` and injects all required env vars.

- Tau is started before Spider in daemon boot sequence.  
  Source: `app/server/ruby/bin/daemon.rb:250`, `app/server/ruby/bin/daemon.rb:253`
- Tau boot command:
  - Windows prod: starts release Erlang VM directly (`erl.exe` with release args)
  - Windows non-prod: runs Tau boot batch file
  - macOS/Linux: `sh <tau_boot_path>` (`boot-mac.sh`/`boot-lin.sh`)
  Source: `app/server/ruby/bin/daemon.rb:975`, `app/server/ruby/bin/daemon.rb:994`

## 1.2 Manual startup scripts

From `app/server/beam/tau`:

- Linux: `boot-lin.sh`
- macOS: `boot-mac.sh`
- Windows: `boot-win.bat`

Mode is controlled by `TAU_ENV`:

- `prod`: runs `_build/.../tau start`
- `dev`: runs `mix assets.deploy.dev` then `mix run --no-halt`
- `test`: runs `mix run --no-halt` with MIDI/Link disabled

Source: `app/server/beam/tau/boot-lin.sh:12`, `app/server/beam/tau/boot-mac.sh:12`, `app/server/beam/tau/boot-win.bat:10`

## 2) Startup Inputs Tau Expects

Tau's runtime config is driven by environment variables read in `config/runtime.exs` and set by daemon `TauBooter`.

## 2.1 Core network inputs

- `TAU_API_PORT`: Tau API UDP listener (receives `/ping`, `/midi`, Link API, etc.)
- `TAU_OSC_IN_UDP_PORT`: Tau OSC cue listener (external OSC input)
- `TAU_SPIDER_PORT`: destination UDP port to Spider for forwarded cues and API replies
- `TAU_DAEMON_PORT`: daemon UDP port for `/tau/pid`
- `TAU_DAEMON_TOKEN`: token required for daemon PID handshake

Source: `app/server/beam/tau/config/runtime.exs:78`, `app/server/ruby/bin/daemon.rb:960`

## 2.2 Feature and behavior flags

- `TAU_CUES_ON`
- `TAU_OSC_IN_UDP_LOOPBACK_RESTRICTED`
- `TAU_MIDI_ON`
- `TAU_LINK_ON`
- `TAU_MIDI_ENABLED`
- `TAU_LINK_ENABLED`
- `TAU_PHX_PORT`
- `TAU_LOG_PATH`
- `TAU_BOOT_LOG_PATH`
- `TAU_ENV` / `MIX_ENV`

Source: `app/server/ruby/bin/daemon.rb:956`, `app/server/beam/tau/config/runtime.exs:71`

## 2.3 Spider process args related to Tau

Spider is started with these Tau-related args:

- `ARGV[6]` => `tau_port` (Tau API port)
- `ARGV[7]` => `listen_to_tau_port` (Spider's UDP listener for Tau messages)
- `ARGV[8]` => `token` (GUI/Spider API token, not Tau daemon token)

Source: `app/server/ruby/bin/spider-server.rb:137`

Daemon passes those values when launching Spider:

Source: `app/server/ruby/bin/daemon.rb:907`

## 3) Runtime Outputs Tau Produces

Primary outputs:

- UDP output to Spider (`TAU_SPIDER_PORT`) for:
  - `/external-osc-cue`
  - `/internal-cue`
  - `/tau-api-reply`
  - `/midi-ins`, `/midi-outs`
  - `/link-tempo-change`, `/link-num-peers`
  - `/tau-ready`
- UDP output to Daemon (`TAU_DAEMON_PORT`) for:
  - `/tau/pid`
- UDP reply on API socket for:
  - `/pong` in response to `/ping`

Source: `app/server/beam/tau/src/tau_server/tau_server_cue.erl:343`, `app/server/beam/tau/src/tau_server/tau_server_api.erl:129`

## 4) Ping/Ack Communication Contracts

## 4.1 Spider <-> Tau readiness handshake

Implemented in `TauComms#wait_for_tau!`:

1. Spider opens an ephemeral UDP server (`boot_s`) and repeatedly sends:
   - to Tau API port: `['/ping']` every 1 second.
2. Tau API handles `/ping` and replies to sender IP/port with:
   - `['/pong']`
3. Spider treats first received reply as connection established and flushes buffered outbound Tau messages.

Source: `app/server/ruby/lib/sonicpi/tau_comms.rb:106`, `app/server/beam/tau/src/tau_server/tau_server_api.erl:129`

Operational note:
- Spider's handshake only requires receiving a UDP response from Tau; in practice Tau returns `/pong`.
- Timeout is 30 seconds before Spider exits.

Source: `app/server/ruby/lib/sonicpi/tau_comms.rb:90`

## 4.2 Daemon <-> Tau PID handshake

Implemented by `TauBooter` + Tau API:

1. Daemon repeatedly sends to Tau API port once per second:
   - `['/send-pid-to-daemon', daemon_token]`
2. Tau accepts only the exact configured token and sends to daemon port:
   - `['/tau/pid', daemon_token, os_pid]`
3. Daemon `/tau/pid` handler validates token and records PID.

Source: `app/server/ruby/bin/daemon.rb:928`, `app/server/beam/tau/src/tau_server/tau_server_api.erl:154`, `app/server/ruby/bin/daemon.rb:222`

Operational note:
- If token mismatches, Tau does not match the command clause and PID is not returned.
- Daemon waits up to 30s when it needs Tau PID for kill/restart paths.

Source: `app/server/ruby/bin/daemon.rb:1008`

## 4.3 Tau -> Spider boot signal

On Tau API init, Tau sends internal message `{tau_ready}` to cue server, which emits:
- `['/tau-ready']` to Spider's Tau-listen UDP port.

Source: `app/server/beam/tau/src/tau_server/tau_server_api.erl:111`, `app/server/beam/tau/src/tau_server/tau_server_cue.erl:285`

Operational note:
- Current Ruby `TauComms` readiness check uses `/ping`->`/pong`; it does not wait on `/tau-ready` specifically.

## 5) Minimal requirements if you replace Daemon or Spider

To interoperate with Tau correctly, your replacement services must implement:

- Spider-side readiness:
  - Send `['/ping']` to `TAU_API_PORT` until response received.
  - Accept UDP response (normally `['/pong']`).
- Daemon-side PID registration:
  - Send `['/send-pid-to-daemon', TAU_DAEMON_TOKEN]` to `TAU_API_PORT` until you receive `['/tau/pid', TAU_DAEMON_TOKEN, pid]` on `TAU_DAEMON_PORT`.
- Spider-side inbound listener on `TAU_SPIDER_PORT` for cue/API messages (`/external-osc-cue`, `/internal-cue`, `/tau-api-reply`, etc.).

## 6) Quick verification checklist

- Tau API UDP port open and reachable on localhost.
- `/ping` returns `/pong`.
- Daemon receives `/tau/pid` with correct token.
- Spider receives `/external-osc-cue` and `/internal-cue` messages on its Tau-listen port.
- Optional: `/tau-ready` appears once during Tau startup.
