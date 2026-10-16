# Noise suppression (group `noise`)

## README section (English)

### Noise suppression for the microphone

Linewave can remove keyboard clatter, fans and other background noise from your microphone before it reaches
the mixer. Turn on **Noise suppression** in the **Microphone** view. The switch works for any microphone, not
only Elgato Wave mics. The processing sits in front of the microphone channel's effects:

    Microphone ─► Noise suppression ─► Effects of the mic channel ─► Monitor/Stream Mix

Linewave picks the method automatically:

| Method | Needs | Strength control |
|---|---|---|
| **RNNoise** (preferred) | the LADSPA plugin `librnnoise_ladspa.so` from [noise-suppression-for-voice](https://github.com/werman/noise-suppression-for-voice) (not packaged in Debian/Ubuntu; install it manually or from your distribution) | yes: *Strength* is the voice detection threshold (0–95 %). Higher values mute everything that does not clearly sound like a voice |
| **PipeWire WebRTC** (fallback) | PipeWire's echo-cancel module with WebRTC processing, packages `libpipewire-0.3-modules` and `libspa-0.2-modules` (usually installed with PipeWire) | no |

If neither is installed, the view tells you which package to install. Noise suppression runs while the mixer is
set up. Linewave restarts it when it starts, and **Remove mixer** stops it. You can also select the filtered
signal directly in Discord or OBS as **“Linewave Microphone (noise suppression)”**.

Command line (also works as `lindeck wave noise …`):

    linewave mic noise on|off|status
    linewave mic noise strength 60           # RNNoise only, 0–95
    linewave mic noise backend auto|rnnoise|webrtc

The settings are stored in `config.json` under `"mixer": {"noise": {"enabled": true, "backend": "auto",
"strength": 50}}`.

## Release notes (English)

- New: **noise suppression** for the microphone in Linewave (Microphone view, or `linewave mic noise on|off|status`).
  It uses RNNoise when `librnnoise_ladspa.so` is installed and PipeWire's built-in WebRTC noise suppression
  otherwise.
- With RNNoise you can set the strength (voice detection threshold) live with a slider or with
  `linewave mic noise strength <0…95>`.
- The filtered microphone feeds the mixer's microphone channel, before its effects. It is also available as
  “Linewave Microphone (noise suppression)” for Discord/OBS.
- The Linewave package now recommends `libspa-0.2-modules` and `libpipewire-0.3-modules`.

## Release notes (Deutsch)

- Neu: **Rauschunterdrückung** für das Mikrofon in Linewave (Ansicht „Mikrofon“ oder
  `linewave mic noise on|off|status`). Ist `librnnoise_ladspa.so` installiert, nutzt Linewave RNNoise, sonst die
  eingebaute WebRTC-Rauschunterdrückung von PipeWire.
- Mit RNNoise lässt sich die Stärke (Schwelle der Spracherkennung) live per Regler oder mit
  `linewave mic noise strength <0…95>` einstellen.
- Das gefilterte Mikrofon speist den Mikrofon-Kanal des Mixers, noch vor dessen Effekten. Für Discord/OBS steht es
  außerdem als „Linewave Mikrofon (Rauschunterdrückung)“ bereit.
- Das Linewave-Paket empfiehlt jetzt zusätzlich `libspa-0.2-modules` und `libpipewire-0.3-modules`.
