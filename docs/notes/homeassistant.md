# Home Assistant (LinDeck) – notes for the coordinator

## README section (English)

### Home Assistant

LinDeck controls your [Home Assistant](https://www.home-assistant.io/) directly, with no plugin needed. The actions
are in the action list under **Home Assistant**:

| Action | What it does |
|---|---|
| **Toggle Entity** | Switches a light, switch, fan, cover, lock, input_boolean … The key shows the state in colour (lights in amber, with brightness in %). On a Stream Deck + dial: turn to change the brightness (lights), target temperature (climate), speed (fans), position (covers) or volume (media players); press to toggle. |
| **Call Service** | Calls any service, e.g. `light.turn_on`, with an optional entity and optional JSON data such as `{"brightness_pct": 40, "color_name": "red"}`. |
| **Scene / Script** | Activates a scene or runs a script. |
| **Show State** | Shows a value with its unit, e.g. `21.5 °C`, updated live. Optionally an attribute instead of the state, and a fixed number of decimal places. |

**Connecting:** select a Home Assistant action, open the **Action** tab and click **Connect …**. Enter the address
(e.g. `http://homeassistant.local:8123`) and a long-lived access token (in Home Assistant: your profile →
Security → Long-lived access tokens). **Test connection** shows the Home Assistant version and location name or
the error. The settings are stored in `~/.config/streamlin/config.json` under `"homeassistant"`
(`url`, `token`, `verify_ssl`); as soon as it contains a token the file is readable only by you (0600). The token
is never written to the log.

**Choosing entities:** click **Choose …** next to the entity field for a searchable list of all entities (or type the
entity ID, e.g. `light.kitchen`).

LinDeck uses the REST API: only the entities shown on the current page are polled in the background (every 2 s,
with back-off when Home Assistant is unreachable). Pressing a key never blocks the deck. If Home Assistant is
offline, the token is wrong or an entity does not exist, the key turns red and says so (`Offline`, `Token?`,
`Unknown`, `Set up`).

Command line:

```bash
lindeck homeassistant test            # test the connection (version, location name)
lindeck homeassistant entities kitchen  # list entities, optionally filtered (alias: lindeck ha …)
```

## Release notes (English)

- New: Home Assistant integration in LinDeck – toggle lights, switches, fans, covers and more, call any service,
  activate scenes and scripts, and show sensor values live on your keys.
- Stream Deck + dials control the brightness of lights, target temperature of thermostats, fan speed, cover
  position or media volume.
- Connect from the action settings (“Connect …”, with “Test connection”) and pick entities from a searchable list;
  `lindeck homeassistant test|entities` on the command line. The access token is stored with file permissions 0600.

## Release notes (Deutsch)

- Neu: Home-Assistant-Anbindung in LinDeck – Lampen, Schalter, Ventilatoren, Rollläden und mehr schalten, beliebige
  Dienste aufrufen, Szenen und Skripte starten und Sensorwerte live auf den Tasten anzeigen.
- Die Dials des Stream Deck + steuern Helligkeit von Lampen, Solltemperatur von Thermostaten, Ventilatorstufe,
  Rollladenposition oder Lautstärke.
- Verbinden direkt in den Aktionseinstellungen („Verbinden …“, mit „Verbindung testen“), Entitäten aus einer
  durchsuchbaren Liste wählen; `lindeck homeassistant test|entities` auf der Kommandozeile. Der Zugangstoken wird
  mit Dateirechten 0600 gespeichert.
