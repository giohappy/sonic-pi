# Tau Build Notes (Windows)

This file documents the commands used to prepare Tau dependencies and build the `sp_midi` and `sp_link` NIF libraries without building the full Sonic Pi GUI project.

## 1) Prepare Elixir/Mix deps

PowerShell can block `mix.ps1` by execution policy, so use `cmd /c`:

```powershell
cd d:\music\dev\sonic-pi\app\server\beam\tau
cmd /c mix deps.get
```

## 2) Build Tau-only NIF externals via CMake

From repo root:

```powershell
cd d:\music\dev\sonic-pi
cmake -S app/server/beam/tau -B app/server/beam/tau/build
cmake --build app/server/beam/tau/build --config Release
```

This builds:
- `app/external/sp_midi`
- `app/external/sp_link`

and installs:
- `app/server/beam/tau/priv/libsp_midi.dll`
- `app/server/beam/tau/priv/libsp_link.dll`

## 3) Verify built NIF DLLs

```powershell
Get-ChildItem app/server/beam/tau/priv | Where-Object { $_.Name -match 'libsp_(midi|link)\.dll' } | Select-Object Name,Length,LastWriteTime
```

## 4) Useful Tau run commands

## Dev

```powershell
cd d:\music\dev\sonic-pi\app\server\beam\tau
cmd /c "set MIX_ENV=dev && set TAU_ENV=dev && mix run --no-halt"
```

## Test

```powershell
cd d:\music\dev\sonic-pi\app\server\beam\tau
cmd /c "set MIX_ENV=test && set TAU_ENV=test && mix run --no-halt"
```

## Prod (release build and start)

```powershell
cd d:\music\dev\sonic-pi\app\server\beam\tau
$env:MIX_ENV = "prod"
$env:TAU_ENV = "prod"
cmd /c "mix tau.release"
cmd /c "set TAU_ENV=prod && _build\prod\rel\tau\bin\tau start"
```

This path is validated in this workspace.

## Prod (runtime mode, tested and working)

If you need a working prod-mode server right now, run Tau with `MIX_ENV=prod`:

```powershell
cd d:\music\dev\sonic-pi\app\server\beam\tau
cmd /c "set MIX_ENV=prod && set TAU_ENV=prod && set SECRET_KEY_BASE=tau-prod-test-secret-key-base-please-change && set TAU_CUES_ON=true && set TAU_OSC_IN_UDP_LOOPBACK_RESTRICTED=true && set TAU_MIDI_ON=true && set TAU_LINK_ON=false && set TAU_OSC_IN_UDP_PORT=5000 && set TAU_API_PORT=5001 && set TAU_SPIDER_PORT=5002 && set TAU_DAEMON_PORT=51234 && set TAU_MIDI_ENABLED=true && set TAU_LINK_ENABLED=false && set TAU_DAEMON_TOKEN=424242 && set TAU_PHX_PORT=8002 && mix run --no-halt"
```

Notes:
- `SECRET_KEY_BASE` is mandatory in prod runtime.
- Keep this terminal open while running client tests.
- `SECRET_KEY_BASE` is not required at release build time, but is required when running Tau in prod.

## 5) Important Windows env assignment detail

When building a prod release from PowerShell, prefer setting env vars in PowerShell first:

```powershell
$env:MIX_ENV = "prod"
cmd /c "mix release --overwrite"
```

Using only inline `cmd /c "set MIX_ENV=prod && ..."` can be brittle depending on calling context. If `MIX_ENV` is not applied, Mix may default to `dev`, causing misleading release errors.

When chaining commands in `cmd`, use quoted `set` form to avoid trailing-space bugs:

```cmd
set "MIX_ENV=test" && set "TAU_ENV=test" && mix run --no-halt
```

Using `set VAR=value && ...` can accidentally produce `value ` (trailing space), which breaks `import_config "#{config_env()}.exs"` lookups.
