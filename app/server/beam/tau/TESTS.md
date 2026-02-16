# Tau E2E Test Commands (Windows)

This file records the commands used to run end-to-end tests with the Python Tau client/demo.

## Preconditions

- Elixir/Erlang installed
- `mix deps.get` already run in `app/server/beam/tau`
- `sp_midi`/`sp_link` DLLs built into `app/server/beam/tau/priv` (see `BUILD.md`)
- loopMIDI running (for MIDI-out verification)

## 1) Python syntax sanity checks

```powershell
python -c "import pathlib,ast; ast.parse(pathlib.Path('app/server/beam/tau/tau_client.py').read_text(encoding='utf-8')); print('tau_client.py OK')"
python -c "import pathlib,ast; ast.parse(pathlib.Path('app/server/beam/tau/tau_client_lib.py').read_text(encoding='utf-8')); print('tau_client_lib.py OK')"
python -c "import pathlib,ast; ast.parse(pathlib.Path('app/server/beam/tau/tau_demo_cli.py').read_text(encoding='utf-8')); print('tau_demo_cli.py OK')"
```

## 2) E2E test (test mode, start Tau from CLI)

This validates:
- Tau start
- `/ping -> /pong`
- `/send-pid-to-daemon -> /tau/pid`
- timed OSC scheduling

```powershell
python app/server/beam/tau/tau_demo_cli.py --start-tau --tau-env test --disable-midi --disable-link --hold-seconds 3 --lead-time 1
```

## 3) MIDI-out E2E (test mode, explicit loopMIDI port)

This validates timed MIDI send through Tau API.

```powershell
python app/server/beam/tau/tau_demo_cli.py --start-tau --tau-env test --disable-link --midi-port "loopmidi_port_1" --hold-seconds 8 --lead-time 2
```

After client update, this sends:
- timed `note_on`
- timed `note_off` +2s later

## 4) MIDI device name probe

Used to identify exact normalized port names from `sp_midi`:

```powershell
app\server\beam\tau\build\sp_midi-prefix\src\sp_midi-build\Release\sp_midi_test.exe
```

Observed example output included:
- `loopmidi_port_1`
- `loopmidi_port_0`

## 5) Dev-mode E2E command variant

```powershell
python app/server/beam/tau/tau_demo_cli.py --start-tau --tau-env dev --disable-link --midi-port "loopmidi_port_1" --hold-seconds 8 --lead-time 2
```

## 6) Prod-mode E2E command variant

Release build + release start:

```powershell
cd d:\music\dev\sonic-pi\app\server\beam\tau
$env:MIX_ENV = "prod"
$env:TAU_ENV = "prod"
cmd /c "mix tau.release"
cmd /c "set TAU_ENV=prod && _build\prod\rel\tau\bin\tau start"
```

If your shell fails to propagate `MIX_ENV`, Mix may silently run in `dev`; set `$env:MIX_ENV="prod"` before `mix` commands.

Working prod test path used in this workspace:

```powershell
cd d:\music\dev\sonic-pi\app\server\beam\tau
cmd /c "set MIX_ENV=prod && set TAU_ENV=prod && set SECRET_KEY_BASE=tau-prod-test-secret-key-base-please-change && set TAU_CUES_ON=true && set TAU_OSC_IN_UDP_LOOPBACK_RESTRICTED=true && set TAU_MIDI_ON=true && set TAU_LINK_ON=false && set TAU_OSC_IN_UDP_PORT=5000 && set TAU_API_PORT=5001 && set TAU_SPIDER_PORT=5002 && set TAU_DAEMON_PORT=51234 && set TAU_MIDI_ENABLED=true && set TAU_LINK_ENABLED=false && set TAU_DAEMON_TOKEN=424242 && set TAU_PHX_PORT=8002 && mix run --no-halt"
```

Then run client against running prod Tau (do not pass `--start-tau`):

```powershell
python app/server/beam/tau/tau_demo_cli.py --tau-env prod --disable-link --midi-port "loopmidi_port_1" --hold-seconds 8 --lead-time 1
```

Expected success markers:
- `[demo] tau readiness handshake OK (/ping -> /pong)`
- `[demo] daemon PID handshake OK (/tau/pid=...)`
- `[demo] scheduled timed MIDI /note_on and /note_off (+2s) on port: loopmidi_port_1`
- `[osc-target] ... /demo/timed-osc ...`

## 7) Process cleanup helpers

```powershell
Get-NetUDPEndpoint -LocalPort 5000,5001,5002,51234 -ErrorAction SilentlyContinue | Select-Object LocalAddress,LocalPort,OwningProcess

$procIds = Get-NetUDPEndpoint -LocalPort 5000,5001,5002,51234 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
foreach($procId in $procIds){ Stop-Process -Id $procId -Force }
```

## Confirmation status

- Test-mode E2E: executed and verified.
- Dev-mode E2E: partially exercised during debugging, but final validated path used test mode.
- Prod-mode E2E: executed and verified in runtime mode (`MIX_ENV=prod` + `mix run --no-halt`).
- Prod release binary path (`mix tau.release` + `_build\prod\rel\tau\bin\tau start`): executed and verified after setting `MIX_ENV=prod` correctly in PowerShell.
