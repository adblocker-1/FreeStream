# FreeStream: Stream Deck and Wave Link for Linux

FreeStream brings the Elgato streaming setup to Linux with **two programs**:

| Program | Replaces | Purpose |
|---|---|---|
| **[FreeDeck](#freedeck)** | Stream Deck app | keys, profiles, plugins – reads your Elgato profiles **directly and unchanged** |
| **[FreeWave](#freewave)** | Wave Link | audio mixer with your own mixes (Personal, Stream, Chat …), effects, Wave microphone |

| FreeDeck | FreeWave |
|---|---|
| ![FreeDeck window with a Stream Deck MK.2](docs/images/freedeck-window.png) | ![FreeWave mixer](docs/images/freewave-mixer.png) |

Available in **English, German, Spanish, French and Portuguese (Brazil)**.

**Contents**

- [Installation](#installation)
- [FreeDeck](#freedeck): [first start](#first-start) · [the window](#the-window) ·
  [icons and backgrounds](#icons-and-backgrounds) · [actions](#actions) ·
  [plugins and connections](#plugins-and-connections) · [virtual deck](#virtual-deck) · [settings](#freedeck-settings) ·
  [troubleshooting](#freedeck-troubleshooting)
- [FreeWave](#freewave): [first start](#first-start-1) · [the mixer](#the-mixer) ·
  [apps and channels](#apps-and-channels) · [audio flow and soundboard](#audio-flow-and-soundboard) ·
  [more features](#more-features) ·
  [FreeWave on the deck](#freewave-on-the-deck) · [troubleshooting](#freewave-troubleshooting)
- [For both programs](#for-both-programs): settings file, help, legal

## Installation

1. Download from [Releases](../../releases) and double-click it, or run
   `sudo apt install ./<file>.deb`:
   - `freestream_<version>_all.deb` – both programs (recommended)
   - `freedeck_<version>_all.deb` – only FreeDeck
   - `freewave_<version>_all.deb` – only FreeWave

   You can install FreeDeck and FreeWave separately and add the other one later.
2. Start **FreeDeck** or **FreeWave** from the menu. Both also start invisibly when
   you log in; closing a window keeps the program running in the background.

**Requirements:** a Debian-based distribution (Debian 12/13, Ubuntu 22.04+,
Linux Mint 21+, Pop!_OS, Zorin …) with Python ≥ 3.10, X11 or Wayland. The package
sets up device access, hotkeys, autostart and menu entries. If FreeDeck does not
start right after installing, log out and back in once.

**Updates:** at every start, FreeDeck and FreeWave look for a new release and
install it after you confirm (can be switched off in **Settings**).
**Uninstall:** `sudo apt remove freestream` (or `freedeck` / `freewave`); your
profiles stay in `~/.local/share/freestream`.

### Preview: without Python (Rust)

Since 0.39 there is also a native version of FreeDeck and FreeWave written in Rust. It needs no
Python, starts faster and uses less memory. Download `freedeck-rs_<version>_amd64_<system>.deb` for
your system (`ubuntu-24.04`, `debian-12` or `ubuntu-22.04`) and start **FreeDeck (Rust)** or
**FreeWave (Rust)** from the menu. It installs next to the Python packages and uses the **same
profiles and settings**, so you can switch back and forth. Quit the Python FreeDeck first – only one
program can use the deck at a time. Details: [rust/README.md](rust/README.md).

![FreeDeck (Rust)](docs/images/rust-freedeck.png)

> **Status:** version 0.28, tested with simulated devices of every model.
> Tests on real hardware are still pending; reports are welcome.

---

# FreeDeck

FreeDeck runs every Elgato Stream Deck – keys, icons, folders, pages, multi
actions, dials and touch strips – with your profiles from the Elgato app. You can
also edit profiles or create new ones right on Linux.

## First start

On Windows, copy the folder `%APPDATA%\Elgato\StreamDeck\ProfilesV3` to a USB
stick and plug it in on Linux. At the first start, the **setup** finds your deck
and takes over the profiles from the stick, your Downloads folder or files you
choose – or starts you off with an empty profile.

![FreeDeck setup](docs/images/freedeck-setup.png)

Later, FreeDeck still imports new profiles (`.streamDeckProfile` files and
backups) from USB sticks and Downloads by itself. **Settings → Setup wizard** runs the setup again.

## The window

Laid out like the Elgato app: the **device** in large letters at the top left with the
**profile** below it (click either to switch), the keys in the middle with the **page
numbers** (1 2 3 … +) underneath, the settings of the selected key at the bottom and all
actions on the right – in categories you open and close with a click, with a search on top
and the tabs **Keys** and **Plugins**. At the top right sit the **bell** (check for updates),
the **plugin store** and the **settings**. Both programs use the same neutral grey look as
the Elgato software.

- **Drag an action onto a key** (or double-click it), drag keys to move them (also onto a
  page number), right-click for copy, paste and delete. Changes show up on the deck right away.
- **Key settings** like in the Elgato app: the key on the left (dots below it switch its
  states), next to it the **title** with **T⌄** for font, colour and position, then the
  settings of the action.
- It also works the other way round: turn the page, open a folder or switch the
  profile on the deck and the window follows; keys that change by themselves
  (volume, mute, clock …) look exactly like on the deck.
- The device is drawn like your model: touch strip and dials on the Stream Deck +,
  the info bar on the Neo (‹ › turn the pages), pedals on the Pedal. Free keys are
  plain dark squares.
- **Profiles:** the profile field at the top lists them; **New profile** only asks for a
  name – FreeDeck knows the connected model. **Import** and **Export** sit at the bottom of
  the list. Right-click a profile to rename, duplicate, export or delete it. Profiles
  stay in the Elgato format, so **Export…** takes them back to Windows.
- **On the deck**, a key briefly shows **Done** or **Error** after its action; a
  Multi Action shows **Step 2 / 5** with a progress bar.

## Icons and backgrounds

**+** on the key (in the key settings) opens the **icon library**: over 100 icons
drawn for FreeDeck in three packs (**Color**, **White**, **Black**) with search and
zoom. **⌄** on the key offers **Choose image file …**, **Paste from clipboard** and the
icon collection.

![Icon library](docs/images/freedeck-icons.png)

**Background** above the device spreads a picture across all keys of a page; the
touch strip of the Stream Deck + can get its own image. **Opacity** (⌄ on the key) lets the background shine through the icon – on FreeWave keys only their dark
background becomes see-through, the symbol, fader and text stay sharp.

**When FreeDeck is not running** (Settings → Devices): instead of the logo, the deck keeps
showing a picture of your choice across all keys after you quit FreeDeck or shut down –
as long as the deck has power.

| | |
|---|---|
| ![Background image across all keys](docs/images/freedeck-background.png) | ![Background dialog](docs/images/freedeck-background-dialog.png) |

## Actions

All built-in actions of the Elgato app work:

| Action | On Linux |
|---|---|
| **Hotkey**, **Hotkey Switch** | virtual keyboard, works on X11 and Wayland |
| **Text** | pasted via the clipboard (emojis too) |
| **Multimedia** | media keys; on dials: system volume |
| **Open**, **Open Application**, **Close** | files, folders, URLs, programs; Windows programs are mapped to their Linux versions |
| **Website** | default or chosen browser |
| **Multi Action**, **Random Action**, **Delay** | as in the original |
| **Folders**, **Pages**, **Switch Profile** | incl. swiping on the touch strip |
| **Brightness**, **Sleep**, **Timer** | |
| **Soundboard** | play/stop audio with volume and loop |
| **Key Logic**, **Action Wheel**, **Dial Stack**, **Mouse Wheel** | |
| **Volume Controller** | input/output devices and app volume |
| **Smart Profiles** | switch profiles by active window (X11, KDE, Hyprland, Sway) – see **Settings → Profiles** |

FreeDeck adds its own **live keys**: **Clock**, **Countdown** (flashes and plays a
sound at zero), **Stopwatch** and a **system monitor** (CPU, memory, temperature,
network, battery, disk space).

## Plugins and connections

**OBS, Spotify, Discord, Home Assistant and Wave Link** are built in. Other Elgato
plugins: **Settings → Plugins → Install plugin …** and choose the
`.streamDeckPlugin` file. There you also see whether it runs, its log, and can
restart or remove it. Plugins built with the official SDK need
[Node.js](https://nodejs.org/en/download) (version 20 recommended); Windows-only
and macOS-only plugins cannot run.

![Plugin management](docs/images/freedeck-plugins.png)

The services are set up under **Settings → Connections**, each with a
connection test:

| Service | What to do |
|---|---|
| **OBS** | turn on the WebSocket server ([OBS guide](https://obsproject.com/kb/remote-control-guide)) and enter its password |
| **Discord** | create an app in the [Discord Developer Portal](https://discord.com/developers/applications) with the redirect URL `http://localhost`, enter its Client ID and Client Secret and click **Connect …** |
| **Home Assistant** | enter your address and a [long-lived access token](https://www.home-assistant.io/docs/authentication/#your-account-profile) |
| **Spotify and other players** | nothing to do – every MPRIS player works |

![Connections](docs/images/settings-connections.png)

## Virtual deck

No deck at hand, or want more keys? FreeDeck shows a **virtual deck** on your screen –
it works like a real one: profiles, pages, folders, every action and plugin.
Turn it on under **Settings → Devices → Virtual deck** and choose how it looks:

| | |
|---|---|
| **Floating window** | move it anywhere by its title bar; **always on top** if you like; profile and pages at the bottom |
| **Edge bar** | docked upright at the right or left screen edge (e.g. 2 × 4 instead of 4 × 2 – shown the same way in the FreeDeck window); a click on the grip at its inner side lets it slide away (only the grip stays visible) and back again |

Also there: the **size** (3 × 2, 4 × 2, 5 × 3 or 8 × 4 keys; FreeDeck creates one empty profile for it and moves it along when you try other sizes), a **keyboard shortcut** to show
and hide it from anywhere (e.g. Super + D – or run `freedeck virtual-deck`) and the profile.
Assign keys like on a real deck: choose **Virtual deck** in the device field at the top of the FreeDeck window
and drag actions onto it. Clicking a key doesn't take the focus away, so hotkeys go to the
program you are working in. Sizes that match a real deck (Mini, Neo, Original, XL) can use
its profiles too.

| | |
|---|---|
| ![Virtual deck as a floating window](docs/images/virtual-deck.png) | ![Virtual deck settings](docs/images/settings-virtual-deck.png) |

<a id="freedeck-settings"></a>
## Settings

**Settings** (gear at the top right) have tabs, like the Elgato app:

| Tab | |
|---|---|
| **General** | version and updates, start when you log in, light/dark, language, help and backup |
| **Devices** | per deck: name, on/off, **orientation** (upside down 180°), **sleep after**, brightness, **picture when FreeDeck is not running**, serial number, firmware, profile – and the [virtual deck](#virtual-deck) |
| **Profiles** | switch profiles **automatically** by the program in the foreground, and the **default profile** |
| **Plugins** | installed plugins with status, log, restart and remove |
| **Connections** | OBS, Discord, Home Assistant – and **Windows profiles**: keyboard layout and which Linux program or folder replaces a Windows one |
| **Style** | progress of a **Multi Action** on the key, and a **font for all keys** – **Apply to all keys** changes every profile |

| | |
|---|---|
| ![General settings](docs/images/settings.png) | ![Device settings](docs/images/settings-devices.png) |
| ![Profile settings](docs/images/settings-profiles.png) | ![Style settings](docs/images/settings-style.png) |

<a id="freedeck-troubleshooting"></a>
## Troubleshooting

| Problem | Solution |
|---|---|
| Device not detected | replug the device; log out and back in once after installing |
| A key shows “No profile – choose in FreeDeck” | there is no profile for this model yet: import one or create it in the window |
| Hotkeys don't work | log out and back in; check `ls -l /dev/uinput` |
| Wrong characters from hotkeys | **Settings → Connections → Windows profiles**: choose your Windows keyboard layout |
| A program doesn't start | `freedeck report` shows what's missing → add it under **Settings → Connections → Windows profiles** |
| Key shows a warning symbol | the plugin is missing or Windows-only |

---

# FreeWave

FreeWave replaces Elgato Wave Link: an audio mixer for PipeWire with your own
mixes, effects and support for Elgato Wave microphones.

## First start

A short wizard asks for your microphone, your headphones, the sound and your
channels. **Settings → Setup wizard** opens it again at any time.

## The mixer

Programs play into **channels** (System, Music, Browser, Voice Chat, Game, SFX,
Microphone). The mixer is a table like **Mixes** in Wave Link: one row per
channel with a **master slider**, one column per **mix** with its own slider and
mute for every channel. It looks like Wave Link too: flat grey rows, slim sliders with
the live level as a bar below – green, yellow from 70 %, red from 90 % – and the mix columns fill
the width of the window. The
device you listen on is chosen at the top right.

- **Personal Mix** – what you hear on your headphones (also on several devices at once).
- **Stream Mix** – what your audience hears. In OBS, choose the audio source
  **“Monitor of FreeWave Stream Mix”**.
- **Your own mixes** – **Create mix** in the sidebar, e.g. a **Chat Mix** for
  Discord (choose “Monitor of FreeWave Chat Mix” as microphone there). A mix goes
  to any devices you tick and/or to a virtual source for other programs.

![Programs go into channels, each channel has a slider in every mix](docs/images/freewave-flow.png)

- **Click an empty cell** (it shows ⊕) to put a channel into a mix; the small ✕
  on a cell takes it out. New mixes and channels start empty.
- **Right-click a mix** to edit, listen to or delete it. The headphones button in
  a mix header lets you **listen** to that mix for a moment.

## Apps and channels

Under **Apps** you choose which channel a program plays into – click it, or **drag it
onto another channel** (also onto a channel row in the mixer); new programs go into
the matching channel by themselves. **Add channel** creates your own channels,
also for an input device such as a second microphone or a capture card.

You can also pick the apps **in advance**, before they ever play: click a channel's
name in the mixer and open the **Apps** tab. It lists every app that lands in this
channel – apps that aren't running right now are marked *(not active)*, the bin
takes one out again (also the built-in ones like Firefox → Browser). **Add app**
offers what is playing now and everything installed, with a search; an app missing
from the list can be added by its name.

![Apps tab of a channel: apps assigned in advance](docs/images/freewave-channel-apps.png)

## Audio Flow and Soundboard

**Audio Flow** shows the whole signal path as a graph: inputs (playing apps and
hardware inputs) → channels → mixes → output devices. Cyan lines go from an input
to its channel, magenta from a channel to a mix, green from a mix to a device; dashed
grey means muted. **Click any element** to follow its path – everything connected to
it stays lit, the rest is dimmed; **Clear selection** shows everything again.

![Audio Flow: inputs, channels, mixes and outputs with their connections](docs/images/freewave-audioflow.png)

The **Soundboard** plays short sounds (WAV, MP3, OGG, FLAC) straight into your mixes.
**Add sound** copies the file to `~/.local/share/freestream/sounds/`; click a pad to
play it, click again to stop. The panel on the right sets name, file, color, volume
and **loop**. Below the pads you choose **Play in** (one mix or all mixes – e.g. the
Stream Mix, so your audience hears it), **Monitor in** (hear it yourself on the
Personal Mix or any output) and the overall volume. With FreeDeck, the key action
**Soundboard: Play Sound** plays a sound from this list.

![Soundboard with sound pads and the settings of the selected sound](docs/images/freewave-soundboard.png)

## More features

| Feature | |
|---|---|
| **Effects** | **FX** below a channel: high-pass, EQ, gate, compressor, reverb and any LADSPA/LV2 plugin (`swh-plugins`, `tap-plugins`, `x42-plugins` …) |
| **Linked sliders** | the chain between two mix sliders links them: moving one moves all mixes of the channel by the same amount (also from the deck) |
| **Hear yourself** | the headphones button on the microphone row puts your voice on your headphones (Personal Mix) |
| **Mix levels** | each mix shows its level in dB; **CLIP** in red means it is too loud |
| **Scenes** | save the whole mix under a name and switch with one click, a shortcut or a deck key |
| **Auto-ducking** | music gets quieter while you speak and comes back afterwards |
| **Noise suppression** | for any microphone; uses [RNNoise](https://github.com/werman/noise-suppression-for-voice) if installed, otherwise PipeWire's built-in filter |
| **Keyboard shortcuts** | mute the microphone or apply a scene from any app, also on Wayland |
| **Wave microphones** | Wave:3: gain, mute, Low Cut, Clipguard, LEDs and monitoring; Wave:1, Neo, XLR: gain and mute |

![Wave:3 settings](docs/images/freewave-microphone.png)

## FreeWave on the deck

With FreeDeck installed too, **Export to FreeDeck** in the mixer header creates a
profile “FreeWave” for your deck – laid out like the Wave Link profile – and keeps
it up to date when you add or delete channels:

- top row: mix keys (All and every mix) and “Channels 1–7” – choosing a mix shows only
  the channels in that mix, one after the other; the remaining keys stay dark
- per channel: a tile with icon and volume (press = mute) and a fader over two
  keys (upper = louder, lower = quieter – hold the key to keep going); the bar next to the
  fader shows the **live level** of the channel after its volume, so you can set it by ear
  and by eye (yellow/red at the top = loud)
- on the Stream Deck +: the dials
- **Look**: *Transparent keys* and a *background image* for all pages – kept when
  FreeWave updates the profile (also a background you set in FreeDeck)

![FreeWave keys on a deck: mix choice, channel tiles and faders with live level](docs/images/freewave-deck.png)

The actions **Mixer Channel**, **Mixer Scene**, **Wave Microphone** and **Soundboard: Play Sound**
work even when the FreeWave window is closed.

<a id="freewave-troubleshooting"></a>
## Troubleshooting

| Problem | Solution |
|---|---|
| No sound after setting up the mixer | choose your headphones under **Listen** in the mixer; **Settings → Remove mixer** restores everything |
| OBS hears nothing | use the audio source “Monitor of FreeWave Stream Mix” and check that the channel is in the Stream Mix |

FreeWave's **Settings** have the general part plus **Remove mixer / Set up mixer**.

---

# For both programs

**Settings file:** everything is stored in `~/.config/freestream/config.json`.
Passwords and access tokens (OBS, Discord, Home Assistant) go into your desktop's
**keyring** (GNOME Keyring, KWallet); without a keyring they stay in the file,
which then only you can read. The Windows-profile values look like this:

```json
{
  "keyboard_layout": "auto",
  "app_map": { "obs64.exe": "flatpak run com.obsproject.Studio" },
  "path_map": { "C:/Users/Alex/Music": "/home/alex/Music" }
}
```

**Help:** **Settings → Help → Create support report…** and attach the file to a
[GitHub issue](../../issues/new).

**Legal:** FreeStream is an independent project and is not affiliated with Elgato
or Corsair. “Elgato”, “Stream Deck” and “Wave Link” are trademarks of Corsair
Memory, Inc. It contains no code and no graphics from Elgato; default icons are
only taken from your own Elgato installer. It bundles
[python-elgato-streamdeck](https://github.com/abcminiuser/python-elgato-streamdeck)
(MIT license).
