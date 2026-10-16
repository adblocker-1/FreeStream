# keys – Live keys: clock, countdown, stopwatch, system monitor

## README section (English)

### Live keys: clock, timers and system monitor

LinDeck has keys that draw themselves and update every second. You find them in the action library under
**Clock & timer** and **System monitor**; drag them onto a key or a dial (Stream Deck +).

| Action | What it shows | Controls |
|---|---|---|
| **Clock** | Time in big digits (24 h or 12 h, optional seconds) and/or the date, optionally in another time zone (e.g. `America/New_York`) | – |
| **Countdown** | Remaining time with a progress bar. When it reaches zero the key flashes red, plays a sound (your own file or the system alarm sound, optionally repeated) and shows a desktop notification | Press: start/pause · long press: reset · press after the alarm: stop and reset · dial: turn to change the duration, press/tap to start/pause, long press to reset |
| **Stopwatch** | Elapsed time | Press: start/stop · long press: reset |
| **CPU Load**, **Memory**, **Temperature**, **Network**, **Battery**, **Disk Space** | Current value, optionally with a small graph of the last 30 seconds; colors turn orange/red near the limit | – |

- The duration accepts `5:00`, `1:30:00`, `90`, `90s` or `1h30m`. A long press is anything held longer than
  `long_press_ms` (config, default 500 ms).
- Countdown and stopwatch keep running when you switch pages or folders; the alarm also rings while another page
  is shown.
- The system monitor reads `/proc` and `/sys` directly (no extra packages). Temperature picks the CPU sensor
  automatically (coretemp, k10temp, …) – enter a sensor name if you want another one. Network shows the
  download/upload rate of one interface (e.g. `wlan0`) or of all real interfaces. Battery shows the charge,
  charging state and the remaining time; Disk Space shows how full a folder's drive is.
- Every live key has an accent color. On the Stream Deck + touch strip they show their icon, the value and a bar.

## Release notes (English)

- New live keys **Clock**, **Countdown** and **Stopwatch** (new category “Clock & timer”): big digits, time zones,
  start/pause with a press, reset with a long press; the countdown flashes, plays a sound and sends a notification
  when it ends, and its duration can be set with a Stream Deck + dial.
- New **System monitor** keys: CPU load, memory, CPU temperature, network rate, battery and disk space – with an
  optional mini graph, read directly from /proc and /sys.
- Countdown and stopwatch keep running across page and folder changes.

## Release notes (Deutsch)

- Neue Live-Tasten **Uhr**, **Countdown** und **Stoppuhr** (neue Kategorie „Uhr & Timer“): große Ziffern,
  Zeitzonen, Start/Pause per Druck, Zurücksetzen per langem Druck; der Countdown blinkt am Ende, spielt einen Ton
  und zeigt eine Benachrichtigung, die Dauer lässt sich am Stream Deck + per Dial einstellen.
- Neue **Systemmonitor**-Tasten: CPU-Last, Arbeitsspeicher, CPU-Temperatur, Netzwerkrate, Akku und Speicherplatz –
  wahlweise mit kleinem Verlaufsgraphen, direkt aus /proc und /sys gelesen.
- Countdown und Stoppuhr laufen beim Wechsel von Seite oder Ordner weiter.
