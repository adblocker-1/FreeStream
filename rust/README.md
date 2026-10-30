# FreeStream in Rust

The Rust version of FreeDeck (and later FreeWave). It reads and writes the same folders,
`config.json`, profiles and translations as the Python version.

## Try it

Easiest: install the package `freedeck-rs_<version>_amd64_<system>.deb` that fits your system
(`ubuntu-24.04`, `debian-12` or `ubuntu-22.04`). It is in the download of the latest run of the
**Rust** workflow (Actions tab). Double-click it or run `sudo apt install ./freedeck-rs_….deb`, then
start **FreeDeck (Rust)** or **FreeWave (Rust)** from the app menu, or run `freedeck-rs` / `freewave-rs`. It can be installed next to the
Python FreeDeck and brings its own udev rule.

Build it yourself:

```sh
rust/packaging/build-deb.sh   # writes rust/dist/freedeck-rs_<version>_<arch>.deb
```

Build dependencies (Debian/Ubuntu):

```sh
sudo apt install build-essential pkg-config libgtk-4-dev libhidapi-dev libudev-dev
curl -sSf https://sh.rustup.rs | sh   # Rust, if not installed
```

Build and run:

```sh
cd rust
cargo run --release -p freedeck              # window with the device service
cargo run --release -p freedeck -- run       # service only (no window)
cargo run --release -p freedeck -- profiles  # list profiles
cargo run --release -p freedeck -- preview Streaming 1 -o page.png
cargo run --release -p freewave              # FreeWave window: mixer, microphone, effects, apps
cargo run --release -p freewave -- --hidden  # FreeWave service only (autostart)
cargo run --release -p freewave -- mixer status
```

The package also contains **FreeWave (Rust)** (`freewave-rs`). Quit the Python FreeWave first
(`freewave --quit`): both use the same PipeWire mixer and settings.

Stop the Python FreeDeck first (`freedeck quit` or end it in the tray). Both cannot use the
deck at the same time.

The deck needs a udev rule for access without root. The `freedeck-rs` package and the Python
package both install one (`/lib/udev/rules.d/70-freedeck-rs.rules` or `70-freedeck.rules`).

## Layout

| Folder | Content |
|---|---|
| `crates/core` | paths, settings, translations, device models, Elgato profile format |
| `crates/deck` | devices (USB), key images, actions, controller, service |
| `crates/wave` | FreeWave core: PipeWire mixer (channels, mixes, levels, ducking, effects, noise suppression), Wave microphones, background service |
| `apps/freedeck` | the `freedeck` program: window (GTK4) and command line |
| `apps/freewave` | the `freewave` program: mixer window (GTK4), setup wizard, dialogs, export to FreeDeck, command line |
