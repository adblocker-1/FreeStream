//! Standard-Icons der Elgato-Aktionen aus den Assets, die der Nutzer selbst aus dem Elgato-Installer
//! übernommen hat (`freedeck extract-assets`, wie in der Python-Version). Ohne diese Assets: None.

use std::path::PathBuf;

/// Plugin-Ordner der eingebauten Aktionen (sonst „<UUID>.sdPlugin“).
const PLUGIN_DIR: &[(&str, &str)] = &[
    ("com.elgato.streamdeck.dial.stack", "com.elgato.streamdeck.dial.sdPlugin"),
    ("com.elgato.streamdeck.keys.stack", "com.elgato.streamdeck.keys.sdPlugin"),
    ("com.elgato.streamdeck.keys.adaptor", "com.elgato.streamdeck.keys.sdPlugin"),
    ("com.elgato.streamdeck.keys.logic", "com.elgato.streamdeck.keys.sdPlugin"),
    ("com.elgato.streamdeck.multiactions.routine", "com.elgato.streamdeck.multiactions.sdPlugin"),
    ("com.elgato.streamdeck.multiactions.routine2", "com.elgato.streamdeck.multiactions.sdPlugin"),
    ("com.elgato.streamdeck.multiactions.random", "com.elgato.streamdeck.multiactions.sdPlugin"),
    ("com.elgato.streamdeck.multiactions.delay", "com.elgato.streamdeck.multiactions.sdPlugin"),
    ("com.elgato.streamdeck.page.previous", "com.elgato.streamdeck.page.sdPlugin"),
    ("com.elgato.streamdeck.page.next", "com.elgato.streamdeck.page.sdPlugin"),
    ("com.elgato.streamdeck.page.goto", "com.elgato.streamdeck.page.sdPlugin"),
    ("com.elgato.streamdeck.page.indicator", "com.elgato.streamdeck.page.sdPlugin"),
    ("com.elgato.streamdeck.profile.backtoparent", "com.elgato.streamdeck.profile.backtoparent.sdPlugin"),
    ("com.elgato.streamdeck.profile.openchild", "com.elgato.streamdeck.profile.openchild.sdPlugin"),
    ("com.elgato.streamdeck.profile.rotate", "com.elgato.streamdeck.profile.rotate.sdPlugin"),
    ("com.elgato.streamdeck.soundboard.playaudio", "com.elgato.streamdeck.soundboard.sdPlugin"),
    ("com.elgato.streamdeck.soundboard.stopaudioplay", "com.elgato.streamdeck.soundboard.sdPlugin"),
    ("com.elgato.streamdeck.system.mouse-wheel", "com.elgato.streamdeck.system.mouse.sdPlugin"),
    ("com.elgato.streamdeck.system.timer", "com.elgato.streamdeck.timer.sdPlugin"),
];

/// Standard-Icon je Aktion (Name ohne Endung, je Zustand).
const DEFAULT_ICONS: &[(&str, &[&str])] = &[
    ("com.elgato.streamdeck.dial.stack", &["Images/btn_dialStack"]),
    ("com.elgato.streamdeck.keys.stack", &["Images/btn_keyStack"]),
    ("com.elgato.streamdeck.keys.adaptor", &["Images/btn_keyAdaptor"]),
    ("com.elgato.streamdeck.keys.logic", &["Images/btn_keyLogic"]),
    ("com.elgato.streamdeck.multiactions.routine", &["Images/btn_multiAction"]),
    ("com.elgato.streamdeck.multiactions.routine2", &["Images/btn_toggleMultiActionOff", "Images/btn_toggleMultiActionOn"]),
    ("com.elgato.streamdeck.multiactions.random", &["Images/btn_randomAction"]),
    ("com.elgato.streamdeck.multiactions.delay", &["Images/btn_duration"]),
    ("com.elgato.streamdeck.page.previous", &["Images/btn_previousPage"]),
    ("com.elgato.streamdeck.page.next", &["Images/btn_nextPage"]),
    ("com.elgato.streamdeck.page.goto", &["Images/btn_goToPage"]),
    ("com.elgato.streamdeck.page.indicator", &["Images/btn_pageIndicator"]),
    ("com.elgato.streamdeck.profile.backtoparent", &["Images/btn_backToParent"]),
    ("com.elgato.streamdeck.profile.openchild", &["Images/btn_folder"]),
    ("com.elgato.streamdeck.profile.rotate", &["Images/btn_switchProfile"]),
    ("com.elgato.streamdeck.soundboard.playaudio", &["Images/btn_playAudio"]),
    ("com.elgato.streamdeck.soundboard.stopaudioplay", &["Images/btn_stopAudio"]),
    ("com.elgato.streamdeck.system.close", &["Images/btn_close"]),
    ("com.elgato.streamdeck.system.digitaltime", &["Images/btn_digitalTime"]),
    ("com.elgato.streamdeck.system.hotkey", &["Images/btn_hotkey"]),
    ("com.elgato.streamdeck.system.hotkeyswitch", &["Images/btn_toggleHotkeyOn", "Images/btn_toggleHotkeyOff"]),
    ("com.elgato.streamdeck.system.keybrightness", &["Images/btn_keybrightness_max"]),
    ("com.elgato.streamdeck.system.mouse-wheel", &["Images/btn_custom_trigger_wheel"]),
    ("com.elgato.streamdeck.system.multimedia", &["Images/btn_media_play_pause"]),
    ("com.elgato.streamdeck.system.open", &["Images/btn_open"]),
    ("com.elgato.streamdeck.system.openapp", &["Images/btn_open"]),
    ("com.elgato.streamdeck.system.pagination", &["Images/btn_pagination"]),
    ("com.elgato.streamdeck.system.sleep", &["Images/btn_sleep"]),
    ("com.elgato.streamdeck.system.text", &["Images/btn_text"]),
    ("com.elgato.streamdeck.system.website", &["Images/btn_website"]),
    ("com.elgato.streamdeck.system.timer", &["Images/btn_timer"]),
    ("com.elgato.streamdeck.system.vsdtoggle", &["Images/btn_vsdtoggle"]),
];

/// Multimedia: Icon je Funktion.
pub const MEDIA_ICONS: &[(i64, &str)] = &[
    (0, "btn_media_prev_track"),
    (1, "btn_media_play_pause"),
    (2, "btn_media_next_track"),
    (3, "btn_media_stop"),
    (4, "btn_media_mute"),
    (5, "btn_media_volume_up"),
    (6, "btn_media_volume_down"),
    (13, "btn_media_fast_forward"),
    (14, "btn_media_rewind"),
    (18, "btn_media_volume"),
];

fn plugin_dir_for(uuid: &str) -> String {
    PLUGIN_DIR.iter().find(|(u, _)| *u == uuid).map(|(_, d)| d.to_string()).unwrap_or_else(|| format!("{uuid}.sdPlugin"))
}

/// Pfad zu einem Standard-Icon aus den übernommenen Assets.
pub fn builtin_icon_path(rel_image: &str, plugin_dir: &str) -> Option<PathBuf> {
    if rel_image.is_empty() {
        return None;
    }
    let base = freestream_core::paths::assets_dir().join("Plugins").join(plugin_dir).join(rel_image);
    ["", ".png", "@2x.png", ".svg"].iter().map(|ext| PathBuf::from(format!("{}{ext}", base.display()))).find(|p| p.exists())
}

/// Standard-Icon einer Aktion im Zustand `state`.
pub fn default_icon(uuid: &str, state: usize) -> Option<PathBuf> {
    let icons = DEFAULT_ICONS.iter().find(|(u, _)| *u == uuid)?.1;
    builtin_icon_path(icons.get(state.min(icons.len().saturating_sub(1)))?, &plugin_dir_for(uuid))
}

/// Multimedia-Icon für die Funktion `idx`.
pub fn media_icon(idx: i64) -> Option<PathBuf> {
    let name = MEDIA_ICONS.iter().find(|(i, _)| *i == idx)?.1;
    builtin_icon_path(&format!("Images/{name}"), "com.elgato.streamdeck.system.multimedia.sdPlugin")
}
