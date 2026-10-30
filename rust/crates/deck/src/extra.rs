//! Weitere eingebaute Aktionen (Tastenkürzel, Text, Medien, Uhr, Timer, Soundboard, Multi-Aktionen,
//! Systemmonitor, Programm beenden) – [`crate::actions::behavior_for`] fragt hier nach, wenn es eine UUID
//! nicht selbst kennt.

use crate::actions::Behavior;
use crate::{actions_app, actions_input, actions_multi, actions_sound, actions_sysmon, actions_time};

pub fn behavior_for(uuid: &str) -> Option<Box<dyn Behavior>> {
    actions_input::behavior_for(uuid)
        .or_else(|| actions_time::behavior_for(uuid))
        .or_else(|| actions_sound::behavior_for(uuid))
        .or_else(|| actions_multi::behavior_for(uuid))
        .or_else(|| actions_sysmon::behavior_for(uuid))
        .or_else(|| actions_app::behavior_for(uuid))
}

#[cfg(test)]
mod tests {
    #[test]
    fn all_builtin_uuids_are_supported() {
        for uuid in [
            "com.elgato.streamdeck.system.hotkey", "com.elgato.streamdeck.system.hotkeyswitch", "com.elgato.streamdeck.system.text",
            "com.elgato.streamdeck.system.multimedia", "com.elgato.streamdeck.system.mouse-wheel",
            "com.elgato.streamdeck.system.digitaltime", "com.elgato.streamdeck.system.timer", "com.elgato.streamdeck.timer",
            "com.lindeck.clock.clock", "com.lindeck.clock.countdown", "com.lindeck.clock.stopwatch",
            "com.elgato.streamdeck.soundboard.playaudio", "com.elgato.streamdeck.soundboard.stopaudioplay",
            "com.elgato.streamdeck.multiactions.routine", "com.elgato.streamdeck.multiactions.routine2",
            "com.elgato.streamdeck.multiactions.delay", "com.elgato.streamdeck.multiactions.random",
            "com.elgato.streamdeck.keys.logic", "com.elgato.streamdeck.keys.stack", "com.elgato.streamdeck.dial.stack",
            "com.elgato.streamdeck.keys.adaptor",
            "com.lindeck.sysmon.cpu", "com.lindeck.sysmon.memory", "com.lindeck.sysmon.disk", "com.lindeck.sysmon.network",
            "com.lindeck.sysmon.battery", "com.lindeck.sysmon.temperature", "com.elgato.streamdeck.system.close",
        ] {
            assert!(super::behavior_for(uuid).is_some(), "{uuid}");
            assert!(crate::actions::behavior_for(uuid).supported(), "{uuid}");
        }
        assert!(super::behavior_for("com.example.plugin.x").is_none());
    }
}
