# FreeStream in Rust

The Rust version of FreeDeck (and later FreeWave). It reads and writes the same folders,
`config.json`, profiles and translations as the Python version.

## Try it

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
```

Stop the Python FreeDeck first (`freedeck quit` or end it in the tray). Both cannot use the
deck at the same time.

The deck needs the udev rule of the FreeDeck package for access without root
(`/lib/udev/rules.d/70-freedeck.rules`). If the Python package is installed, the rule is
already there.

## Layout

| Folder | Content |
|---|---|
| `crates/core` | paths, settings, translations, device models, Elgato profile format |
| `crates/deck` | devices (USB), key images, actions, controller, service |
| `apps/freedeck` | the `freedeck` program: window (GTK4) and command line |
