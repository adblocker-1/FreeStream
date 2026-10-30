//! Weitere Aktionen (Tastenkürzel, Text, Medien, Uhr, Timer, Soundboard, Multi-Aktionen …) – werden
//! nach und nach aus der Python-Version übernommen.

use crate::actions::Behavior;

pub fn behavior_for(_uuid: &str) -> Option<Box<dyn Behavior>> {
    None
}
