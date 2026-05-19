# dbus-goecharger-v2

Integrate go-eCharger (Hardware v3/v4) into Victron Energy Venus OS using the **go-eCharger HTTP API v2**.

Based on the original [vikt0rm/dbus-goecharger](https://github.com/vikt0rm/dbus-goecharger) project, rewritten for API v2.

## Requirements

- go-eCharger hardware **v3 or v4**
- go-eCharger firmware **051.4+** (for filter parameter support)
- Venus OS on Raspberry Pi or GX device
- API v2 enabled in the go-eCharger app

## Key differences to the v1 integration

| Topic | API v1 | API v2 (this project) |
|---|---|---|
| Set values | `/mqtt?payload=key=value` | `/api/set?key=value` |
| `alw` (allow charging) | read/write | **read-only** |
| Charging control | write `alw=0/1` | write `frc=1` (off) / `frc=2` (on) |
| `nrg` array values | integers | **floats** (e.g. 15.3 A) |
| Temperature | `tmp` (int) or `tma[0]` | `tma[0]` (float) |
| Energy (session) | `wh` (HW v4) | `wh` (preferred), fallback `eto` |

## How it works

- Runs as a daemon service on Venus OS
- Registers on D-Bus as `com.victronenergy.evcharger.http_{Deviceinstance}`
- Polls go-eCharger every `PauseBetweenRequests` ms via `GET /api/status?filter=...`
- Writes values via `GET /api/set?key=value`

## Install

```bash
wget https://github.com/YOUR_USER/dbus-goecharger-v2/archive/refs/heads/main.zip
unzip main.zip "dbus-goecharger-v2-main/*" -d /data
mv /data/dbus-goecharger-v2-main /data/dbus-goecharger-v2
chmod a+x /data/dbus-goecharger-v2/install.sh
/data/dbus-goecharger-v2/install.sh
rm main.zip
```

> After install, edit `config.ini` before the service starts polling.

## Configuration (`config.ini`)

| Section | Key | Description |
|---|---|---|
| DEFAULT | AccessType | Fixed: `OnPremise` |
| DEFAULT | Deviceinstance | Unique ID in Venus OS (e.g. `43`) |
| DEFAULT | HardwareVersion | `3` or `4` |
| DEFAULT | AcPosition | `0` = AC out, `1` = AC in |
| DEFAULT | SignOfLifeLog | Interval in minutes for status log entry |
| DEFAULT | Logging | `ERROR`, `WARNING`, `INFO`, or `DEBUG` |
| ONPREMISE | Host | IP address of your go-eCharger |
| ONPREMISE | PauseBetweenRequests | Polling interval in ms (min. 21) |

## Supported D-Bus paths

| Path | R/W | Description |
|---|---|---|
| `/Ac/Power` | R | Total AC power (W) |
| `/Ac/L1/Power` | R | L1 power (W) |
| `/Ac/L2/Power` | R | L2 power (W) |
| `/Ac/L3/Power` | R | L3 power (W) |
| `/Ac/Voltage` | R | L1 voltage (V) |
| `/Ac/Energy/Forward` | R | Session energy (kWh) |
| `/Current` | R | Max phase current (A) |
| `/SetCurrent` | R/W | Desired charging current → `amp` |
| `/MaxCurrent` | R/W | Maximum current limit → `ama` |
| `/StartStop` | R/W | Start/stop charging → `frc` |
| `/ChargingTime` | R | Active charging time (s) |
| `/MCU/Temperature` | R | Board temperature °C |
| `/Status` | R | Charger status (Victron codes) |

## Useful links

- [go-eCharger API v2 documentation](https://github.com/goecharger/go-eCharger-API-v2)
- [Victron dbus-api documentation](https://github.com/victronenergy/venus/wiki/dbus-api)
- [Original dbus-goecharger (API v1)](https://github.com/vikt0rm/dbus-goecharger)
