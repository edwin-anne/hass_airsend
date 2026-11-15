[![airsend](https://img.shields.io/github/release/devmel/hass_airsend/all.svg?style=plastic&label=Current%20release)](https://github.com/devmel/hass_airsend) [![hacs_badge](https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=plastic)](https://github.com/hacs/integration) [![downloads](https://img.shields.io/github/downloads/devmel/hass_airsend/total?style=plastic&label=Total%20downloads)](https://github.com/devmel/hass_airsend)

# AirSend
This is a Home Assistant integration for AirSend boxes, which can capture and control more than 350 equipements (Remote Switchs, Lights, Shutters, Gate... ) from 50 brands (Dio, Faac, Nice, Somfy...) by RF433 or RF868 radio commands.

### Supported boxes
- [AirSend](https://devmel.com/fr/airsend.html) (RF433)
- [AirSend Duo](https://devmel.com/fr/airsend-duo.html) (RF433 & RF868)


Each AirSend device is represented as a proper **Home Assistant device** with its associated entities (cover, switch, button, light, sensor).


## Requirements

### 1. Install the AirSend addon

[![Add AirSend addon repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fdevmel%2Fhass_airsend-addon)

Install and start the **AirSend** addon.

### 2. Create `airsend.yaml`

Export your devices from [airsend.cloud](https://airsend.cloud) → Import/Export → Export YAML (select `spurl` for local connection).

Place the exported `airsend.yaml` in your `/config` folder.

Add your AirSend local connection string to `secrets.yaml`:

```yaml
spurl: sp://**************@[fe80::xxxx:xxxx:xxxx:xxxx]?gw=0&rhost=192.168.xxx.xxx
```

Example `airsend.yaml`:

```yaml
devices:
  AirSend box:
    type: 0
    spurl: !secret spurl
    sensors: true      # enables temperature and illuminance sensors
    bind: 1
    refresh: 300

  Volet salon:
    id: 12345
    type: 4098
    invert: true      # add this option for covers where open/close logic is reversed (e.g. shade sails, projector screens)
    apiKey: !secret apiKey
    spurl: !secret spurl
    channel:
      id: 25455
      source: 94311

  Lumiere pergola:
    id: 65838
    type: 4100         # dimmable light
    apiKey: !secret apiKey
    spurl: !secret spurl
    wait: true
    channel:
      id: 26848
      source: 1442421508
```

## Installation

### Manual installation (until the repository is available in HACS)


1. In HACS, go to **Integrations** → click the three-dot menu → **Custom repositories**.
2. Add the repository URL: `https://github.com/devmel/hass_airsend` and select **Integration** as the category.
3. Search for **AirSend** in HACS and click **Install**.
4. **Restart** Home Assistant.
5. Go to **Settings → Integrations → Add integration** and search for `AirSend`.

### HACS (recommended)

1. Ensure [HACS](https://hacs.xyz) is installed.
2. Search for `AirSend` in HACS and install it, or use the button below.
3. **Restart** Home Assistant.
4. Go to **Settings → Integrations → Add integration** and search for `AirSend`.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Devmel&repository=hass_airsend)



## Configuration

After the integration added, the internal URL of the addon will be auto-detected. Confirm and your devices will appear automatically.

To reload devices after modifying `airsend.yaml`, use the **Reconfigure** option on the integration.

## Device options reference

| Option | Description |
|--------|-------------|
| `type` | Device type (see table above) |
| `id` | AirSend cloud device ID |
| `apiKey` | AirSend cloud API key |
| `spurl` | Local connection string |
| `channel` | RF channel definition (`id`, `source`, `mac`, `seed`) |
| `wait` | Wait for RF confirmation before returning (`true`/`false`) |
| `sensors` | Enable temperature and illuminance sensors for AirSend box (`true`/`false`) |
| `bind` | Channel ID to bind for incoming RF messages |
| `refresh` | Poll interval in seconds (default: 300) |
| `invert` | Give possibility to reverse command UP/DOWN as DOWN/UP |

## Informations

This integration requires the [AirSend addon](https://github.com/devmel/hass_airsend-addon) to communicate with your AirSend hardware locally.
For local connection, the AirSend IPv4 address is required, you can find it in your router or [Airsend App for Windows](https://apps.microsoft.com/detail/9nblggh40m8w).
If you `DO NOT` provide ?gw=0&rhost=... then you may face unexpected `HTTP/500` status code or `HTTP/262` status code at Home Assistant level.

## Supported device types

| Type | Value | Description |
|------|-------|-------------|
| AirSend box | `0` | The AirSend hardware box (state, temperature, illuminance) |
| Sensor | `1` | Generic RF sensor |
| Cover | `4098` | Roller shutter, blind (open/close/stop) |
| Cover with position | `4099` | Roller shutter with position control (0-100%) |
| Switch | `4097` | On/Off switch |
| Button | `4096` | Single-action button (toggle) |
| Light | `4100` | Dimmable light (0-100%) |

## Preview

![Screenshot](https://raw.githubusercontent.com/devmel/hass_airsend/master/img/screenshot.png)

## Secure devices (PIN vault)

For sensitive equipment (garage door, gate...), add `secure: true` to the device in `airsend.yaml`.

* When you add or reconfigure the integration, you choose a **6-8 digit PIN** (trivial codes such as `123456` are refused). The device's `channel`, `spurl` and `apiKey` are then **encrypted** (AES-256-GCM; key derived from the PIN with scrypt and a random "pepper" stored in `/config/.storage/airsend.pepper`) and removed from Home Assistant's storage and entity attributes.
* **Delete these secrets from `airsend.yaml` afterwards**: that file stays in clear text on disk.
* A secure device refuses every command until it is unlocked:
  * the `lock.<name>_pin` entity: unlock with the PIN (Home Assistant shows a keypad). The authorization lasts **one command or 30 seconds**, then it locks again;
  * or the services `airsend.secure_open`, `airsend.secure_close`, `airsend.secure_stop` (`entity_id` of the cover + `pin`).
* 5 wrong PINs lock the vault for 5 min, then 15 min, then 1 h.
* The pepper file is required to unlock the vault: losing it makes the vault unrecoverable (remove and re-add the integration). If you exclude it from your backups, a stolen backup alone cannot be brute-forced.
* Do not expose a secure cover to voice assistants: use the lock entity.

Limits: this protects access *from Home Assistant* and the stored configuration. It does not protect against someone cloning your physical remote, nor against an attacker with live root access to the Home Assistant host while a command is being sent.

## Native pairing in Home Assistant (no airsend.cloud, no YAML)

`airsend.yaml` is now optional. Open **Settings > Devices & services > AirSend > Configure**:

* **Pair a new device** - enter the box locator (`spurl`), then choose:
  * **Capture an existing remote** (fixed-code protocols): the integration listens for 60 s (`bind` + `events`, always followed by `unbind`) and you press ONE button. Only one distinct remote may be heard, otherwise you retry.
  * **Create a virtual transmitter** (recommended for doors, works with rolling codes): you pick the protocol (list from the web service), the web service builds a new transmitter (`/channels/build`), you put the receiver in programming mode, and a `PROG` command is sent. The transmitter is independent from your other remotes and can be revoked on the receiver alone.
  * Then name the device, choose its type and whether it is **secure** (PIN vault). Secure devices are sealed immediately; nothing is stored in clear text.
* **Remove a device** destroys its sealed secrets (also remove the transmitter from the receiver to revoke it radio-side).
* **Change vault PIN** re-wraps the vault key; the old PIN is required.

Natively paired devices survive a YAML reconfiguration.

### Audit and alerts
The vault fires `airsend_vault_event` on the Home Assistant event bus: `sealed`, `unlocked`, `command_authorized`, `pin_failed`, `pin_locked`, `pin_changed`, `removed` (fields `type`, `device`; never the PIN or secrets). Three or more wrong PINs raise a persistent notification. Use these events for mobile notifications.

### Not verified on real hardware (please report)
This release was written from the AirSendWebService OpenAPI file and unit-tested against a simulated server only. Unconfirmed on a real box: the body accepted by `POST /channels/build` (the code sends `{"id": <protocol>}` and expects a channel with a `source`), the exact content of `GET /airsend/events`, whether `PROG` pairs your receiver model, and how rolling-code counters evolve. If a step fails, the wizard shows an error and nothing is stored. Never test on a door you cannot manually override.

### Threat model
Protects: configuration files, backups and entity attributes (radio address, locator and API key are encrypted; a stolen backup without the pepper file cannot be brute-forced), and remote use of the door from HA without the PIN (one-shot authorization, lockout, audit).
Does not protect: someone cloning your physical remote, an attacker with live root access on the host while a command is sent, or the plaintext secrets you leave in `airsend.yaml` or `secrets.yaml`.

