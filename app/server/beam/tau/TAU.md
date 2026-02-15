# TAU I/O API Reference

This document describes the implemented I/O endpoints and OSC message formats in `app/server/beam/tau`.

## Scope

- API ingress and scheduler: `app/server/beam/tau/src/tau_server/tau_server_api.erl`
- Cue ingress/egress bridge: `app/server/beam/tau/src/tau_server/tau_server_cue.erl`
- MIDI in/out translation: `app/server/beam/tau/src/tau_server/tau_server_midi*.erl`
- Ableton Link RPC/notifications: `app/server/beam/tau/src/tau_server/tau_server_link.erl`
- Default ports/env: `app/server/beam/tau/config/runtime.exs`

## Network Endpoints

- UDP API listener (loopback): `TAU_API_PORT` (default `5001`)  
  Source: `app/server/beam/tau/config/runtime.exs:79`, `app/server/beam/tau/src/tau_server/tau_server_api.erl:90`
- UDP OSC cue listener: `TAU_OSC_IN_UDP_PORT` (default `5000`)  
  Source: `app/server/beam/tau/config/runtime.exs:78`, `app/server/beam/tau/src/tau_server/tau_server_cue.erl:63`
- Spider cue destination (outbound from Tau): `TAU_SPIDER_PORT` (default `5002`) on `127.0.0.1`  
  Source: `app/server/beam/tau/config/runtime.exs:80`, `app/server/beam/tau/src/tau_server/tau_server_cue.erl:46`
- Daemon destination (for PID report): `TAU_DAEMON_PORT`/`TAU_DAEMON_TOKEN`, host `127.0.0.1`  
  Source: `app/server/beam/tau/config/runtime.exs:81`, `app/server/beam/tau/config/runtime.exs:83`

## 1) Inbound API Commands (UDP -> Tau API)

All commands below are OSC commands received on `TAU_API_PORT`.

### Connectivity

- `['/ping']`  
  Reply to sender: `['/pong']`.  
  Source: `app/server/beam/tau/src/tau_server/tau_server_api.erl:129`

- `['/send-pid-to-daemon', DaemonToken]`  
  Sends to daemon: `['/tau/pid', DaemonToken, OSPid]`.  
  Source: `app/server/beam/tau/src/tau_server/tau_server_api.erl:154`

### Runtime toggles

- `['/osc-in-udp-loopback-restricted', Flag]`  
  Reconfigures cue input listener binding (loopback-only vs open).  
  Source: `app/server/beam/tau/src/tau_server/tau_server_api.erl:183`

- `['/stop-start-cue-server', Flag]`  
  Enables/disables forwarding of incoming OSC cues.  
  Source: `app/server/beam/tau/src/tau_server/tau_server_api.erl:188`

- `['/stop-start-midi-cues', Flag]`  
  Enables/disables forwarding of MIDI-generated cues.  
  Source: `app/server/beam/tau/src/tau_server/tau_server_api.erl:193`

### MIDI send/flush

- `['/midi', OSCBinary]`  
  `OSCBinary` is decoded by `tau_server_midi_out:encode_midi_from_osc/1` and sent to MIDI out NIF.  
  Source: `app/server/beam/tau/src/tau_server/tau_server_api.erl:165`

- `['/midi-flush']`  
  Flushes MIDI output.  
  Source: `app/server/beam/tau/src/tau_server/tau_server_api.erl:171`

### Scheduled command groups

- `['/flush', Tag]`  
  Cancels pending scheduled timers for the tag group.  
  Source: `app/server/beam/tau/src/tau_server/tau_server_api.erl:177`

## 2) Timestamped Bundle Commands (scheduled by Tau)

Tau handles OSC bundles (`{bundle, Time, Items}`) and supports the following item payloads:

- `['/send-after', Host, Port, OSC]`
- `['/send-after-tagged', Tag, Host, Port, OSC]`
- `['/midi-at', MIDI]`
- `['/midi-at-tagged', Tag, MIDI]`
- `['/link-set-tempo', Tempo]`
- `['/link-set-tempo-tagged', Tag, Tempo]`
- `['/link-set-is-playing', Enabled]`
- `['/link-set-is-playing-tagged', Tag, Enabled]`
- `['/hydra_eval', Code]` (bundle parser recognizes it, but the runtime `/hydra_eval` command handler is currently commented out)

Source: `app/server/beam/tau/src/tau_server/tau_server_api.erl:326`

Notes:
- Tag-based scheduling uses per-tag tracker processes.
- Commands with delay <= 1ms are sent immediately instead of timer-scheduled.

Source: `app/server/beam/tau/src/tau_server/tau_server_api.erl:350`

## 3) Link API (commands received on API port)

### Control commands

- `['/link-disable']`
- `['/link-enable']`
- `['/link-reset']`
- `['/link-set-start-stop-sync-enabled', Enabled]`
- `['/link-set-tempo', Tempo]`

Source: `app/server/beam/tau/src/tau_server/tau_server_api.erl:205`

### RPC commands

All RPC requests use:
- request: `['/api-rpc', UUID, Method, ...args]`
- response (to spider via cue server): `['/tau-api-reply', 'erlang', UUID, ...result]`

Methods:
- `'/link-is-on'`
- `'/link-get-start-stop-sync-enabled'`
- `'/link-get-num-peers'`
- `'/link-get-tempo'`
- `'/link-get-beat-at-time'` `(Time, Quantum)`
- `'/link-get-phase-at-time'` `(Time, Quantum)`
- `'/link-get-phase-and-beat-at-time'` `(Time, Quantum)`
- `'/link-get-time-at-beat'` `(Beat, Quantum)` -> returns `{int64, TimeMicros}`
- `'/link-get-next-beat-and-time-at-phase'` `(Phase, Quantum, SafetyT)` -> returns `[Beat, {int64, TimeMicros}]`
- `'/link-get-is-playing'`
- `'/link-get-time-for-is-playing'` -> returns `{int64, TimeMicros}`
- `'/link-get-current-time'` -> returns `{int64, TimeMicros}`

Source: `app/server/beam/tau/src/tau_server/tau_server_api.erl:200`, `app/server/beam/tau/src/tau_server/tau_server_link.erl:92`, `app/server/beam/tau/src/tau_server/tau_server_cue.erl:343`

## 4) Inbound OSC Cue Endpoint (UDP -> Tau Cue Server)

Tau cue server listens on `TAU_OSC_IN_UDP_PORT` and accepts OSC commands and bundles.

Behavior:
- Incoming OSC command is forwarded to spider as:  
  `['/external-osc-cue', SourceIp, SourcePort, ...OriginalCmdElements]`
- Incoming bundle is iterated and each command is forwarded using same format.
- Forwarding is gated by `cues_on`.

Source: `app/server/beam/tau/src/tau_server/tau_server_cue.erl:195`, `app/server/beam/tau/src/tau_server/tau_server_cue.erl:369`

## 5) Outbound OSC Messages from Tau (to spider cue port)

### Startup/status

- `['/tau-ready']` (Tau API init notifies cue server)

Source: `app/server/beam/tau/src/tau_server/tau_server_api.erl:111`, `app/server/beam/tau/src/tau_server/tau_server_cue.erl:285`

### External OSC forwarding

- `['/external-osc-cue', SourceIp, SourcePort, ...OriginalCmdElements]`

Source: `app/server/beam/tau/src/tau_server/tau_server_cue.erl:369`

### Internal cue forwarding

- `['/internal-cue', 'erlang', Path, ...Args]`
- Used for MIDI cues and Link state notifications.

Source: `app/server/beam/tau/src/tau_server/tau_server_cue.erl:363`

### Link cue notifications

- `Path='/link/num-peers'` args: `[NumPeers]`
- `Path='/link/tempo-change'` args: `[Tempo]`
- `Path='/link/start'` args: `[]`
- `Path='/link/stop'` args: `[]`
- `Path='/link/connected'` args: `[]`
- `Path='/link/disconnected'` args: `[]`

Source: `app/server/beam/tau/src/tau_server/tau_server_cue.erl:107`

### Link updates sent with dedicated paths

- `['/link-tempo-change', 'erlang', Tempo]`
- `['/link-num-peers', 'erlang', NumPeers]`

Source: `app/server/beam/tau/src/tau_server/tau_server_cue.erl:349`

### API RPC replies

- `['/tau-api-reply', 'erlang', UUID, ...Args]`

Source: `app/server/beam/tau/src/tau_server/tau_server_cue.erl:343`

### MIDI port inventory notifications

- `['/midi-ins', 'erlang', ...InputPortNames]`
- `['/midi-outs', 'erlang', ...OutputPortNames]`

Source: `app/server/beam/tau/src/tau_server/tau_server_cue.erl:328`

## 6) MIDI Input -> Internal Cue Mapping

Incoming MIDI bytes are parsed, converted to tau events, then forwarded (if `midi_on`) as:
- `['/internal-cue', 'erlang', Path, ...Args]`

Where:
- `Path` is generated as `"/midi:<Device>[:<Channel>]/<event>"`
- `Args` are event-dependent values

Source: `app/server/beam/tau/src/tau_server/tau_server_midi.erl:81`, `app/server/beam/tau/src/tau_server/tau_server_midi.erl:110`

Supported parsed MIDI events:
- `note_off` args `[ControllerNum, Value]`
- `note_on` args `[ControllerNum, Value]`
- `control_change` args `[ControllerNum, Value]`
- `aftertouch` args `[ControllerNum, Value]`
- `program_change` args `[ProgramNum]`
- `channel_pressure` args `[Value]`
- `pitch_bend` args `[Value]`
- `time_code_quarter_frame` args `[MessageType, Value]`
- `song_position_pointer` args `[Value]`
- `song_select` args `[Value]`
- `tune_request` args `[]`
- `clock` args `[]` (parsed but ignored for cue forwarding)
- `start` args `[]`
- `continue` args `[]`
- `stop` args `[]`
- `active_sensing` args `[]` (parsed but ignored for cue forwarding)
- `reset` args `[]`
- `sysex` args `binary_to_list(Data)`

Source: `app/server/beam/tau/src/tau_server/tau_server_midi_in.erl:77`, `app/server/beam/tau/src/tau_server/tau_server_midi.erl:96`

## 7) MIDI Output OSC Payloads (inside `/midi` command)

When API receives `['/midi', OSCBinary]`, `OSCBinary` must decode to one of:

- `['/note_on', PortName, Chan, NoteNum, Value]`
- `['/note_off', PortName, Chan, NoteNum, Value]`
- `['/aftertouch', PortName, Chan, ControllerNum, Value]`
- `['/control_change', PortName, Chan, ControllerNum, Value]`
- `['/channel_pressure', PortName, Chan, Pressure]`
- `['/pitch_bend', PortName, Chan, Value]`
- `['/program_change', PortName, Chan, Value]`
- `['/raw', PortName, ...Bytes]`
- `['/sysex', PortName, ...Bytes]`
- `['/clock', PortName]`
- `['/start', PortName]`
- `['/clock_beat', PortName, BeatDurationMs]` (sends 24 MIDI clock ticks over one beat)
- `['/stop', PortName]`
- `['/continue', PortName]`

Channel semantics:
- `Chan` in `1..16`: send on that channel
- `Chan = -1`: fan out to all channels `1..16`

Source: `app/server/beam/tau/src/tau_server/tau_server_midi_out.erl:64`

## 8) Link Callback Messages (NIF -> Tau -> spider)

Link server receives callback tuples and relays to cue server:
- `{link_tempo, Tempo}` -> internal cue `/link/tempo-change` + `/link-tempo-change`
- `{link_num_peers, Peers}` -> internal cue `/link/num-peers` + `/link-num-peers`
- `{link_start}` -> internal cue `/link/start`
- `{link_stop}` -> internal cue `/link/stop`

Source: `app/server/beam/tau/src/tau_server/tau_server_link.erl:70`, `app/server/beam/tau/src/tau_server/tau_server_cue.erl:122`

## 9) Optional Phoenix UI Endpoints (not timing API)

HTTP/live routes currently present:
- `/` -> `MainLive`
- `/dev/log` (dev only)
- `/dev/dashboard` (dev only)

Source: `app/server/beam/tau/lib/tau_web/router.ex:17`

