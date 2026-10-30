//! Zusammengesetzte Aktionen (wie in `freestream/actions.py`): Multi Action, Multi Action Switch, zufällige
//! Aktion, Wartezeit, Key Logic, Action Wheel (keys.stack), Dial Stack und Action Trigger (keys.adaptor).
//!
//! Die Unteraktionen stehen im Profil unter „Actions“: entweder direkt als Aktion (mit UUID) oder als
//! Gruppe `{"Actions": [...]}` (Multi Action Switch: eine Gruppe je Zustand). Key Logic und Action Trigger
//! haben genau drei Plätze (leere Plätze ohne UUID).
//!
//! Abläufe ohne Wartezeit laufen sofort in der Steuerung (Navigation bleibt in der Reihenfolge); mit
//! Wartezeit in einem eigenen Thread, der seine Befehle über [`crate::actions::Outbox`] schickt.

use serde_json::{Map, Value};
use std::cell::OnceCell;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::actions::{Action, ActionData, Behavior, Command, Env, Feedback};

pub const DELAY: &str = "com.elgato.streamdeck.multiactions.delay";
/// Pause nach jedem Schritt (manche Programme verschlucken sonst Tastendrücke)
pub const STEP_GAP_MS: u64 = 30;

/// Ein Schritt eines Ablaufs.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Step<T> {
    Exec(T),
    Wait(u64),
}

/// Ablauf ausführen: Schritte der Reihe nach, Wartezeiten abwarten; jeder Befehl geht sofort an `out`.
pub fn run_steps<T: Copy>(steps: &[Step<T>], mut exec: impl FnMut(T) -> Command, mut wait: impl FnMut(u64), mut out: impl FnMut(Command)) {
    for st in steps {
        match *st {
            Step::Exec(t) => {
                let c = exec(t);
                if c != Command::None {
                    out(c);
                }
                wait(STEP_GAP_MS);
            }
            Step::Wait(ms) => wait(ms),
        }
    }
}

/// Wartezeit einer Delay-Aktion in ms (None für andere Aktionen).
pub fn delay_ms(a: &ActionData) -> Option<u64> {
    (a.uuid == DELAY).then(|| {
        let v = a.data.get("Settings").and_then(|s| s.get("delay"));
        v.and_then(|v| v.as_f64().or_else(|| v.as_str()?.trim().parse().ok())).unwrap_or(0.0).max(0.0) as u64
    })
}

/// Gruppen von Unteraktionen (wie `_children`).
pub fn child_groups(data: &Map<String, Value>) -> Vec<Vec<Map<String, Value>>> {
    let has_uuid = |a: &Map<String, Value>| a.get("UUID").and_then(Value::as_str).map(|u| !u.is_empty()).unwrap_or(false);
    let mut groups = Vec::new();
    for entry in data.get("Actions").and_then(Value::as_array).into_iter().flatten() {
        let Some(e) = entry.as_object() else { continue };
        if e.contains_key("UUID") {
            groups.push(vec![e.clone()]);
        } else {
            groups.push(e.get("Actions").and_then(Value::as_array).into_iter().flatten()
                .filter_map(Value::as_object).filter(|a| has_uuid(a)).cloned().collect());
        }
    }
    groups
}

/// Feste Plätze (Key Logic, Action Trigger): mindestens drei, leere Plätze = None.
pub fn child_slots(data: &Map<String, Value>) -> Vec<Option<Map<String, Value>>> {
    let mut slots: Vec<Option<Map<String, Value>>> = data.get("Actions").and_then(Value::as_array).into_iter().flatten()
        .map(|e| e.as_object().filter(|a| a.get("UUID").and_then(Value::as_str).map(|u| !u.is_empty()).unwrap_or(false)).cloned())
        .collect();
    while slots.len() < 3 {
        slots.push(None);
    }
    slots
}

fn make(d: &Map<String, Value>, parent: &ActionData) -> Action {
    let mut a = Action::new(d.clone(), parent.page_dir.clone());
    a.data.dial = parent.dial;
    a
}

// ------------------------------------------------------------------------------- Multi Action
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum MultiKind {
    /// alle Schritte nacheinander
    Routine,
    /// Gruppe des aktuellen Zustands, dann nächster Zustand
    Switch,
    /// ein zufälliger Schritt
    Random,
}

type Groups = Arc<Mutex<Vec<Vec<Action>>>>;

pub struct MultiAction {
    kind: MultiKind,
    groups: Option<Groups>,
}

impl MultiAction {
    fn new(kind: MultiKind) -> Self {
        Self { kind, groups: None }
    }

    fn groups(&mut self, a: &ActionData) -> Groups {
        self.groups.get_or_insert_with(|| {
            Arc::new(Mutex::new(child_groups(&a.data).iter().map(|g| g.iter().map(|d| make(d, a)).collect()).collect()))
        }).clone()
    }
}

/// Ablauf starten: ohne Wartezeit sofort (Befehle als Ergebnis), sonst im Hintergrund.
/// Läuft der Ablauf schon, passiert nichts.
fn launch(groups: Groups, pick: impl FnOnce(&[Vec<Action>]) -> Vec<(usize, usize)>, env: &Env) -> Command {
    let Ok(mut g) = groups.try_lock() else { return Command::None };
    let steps: Vec<Step<(usize, usize)>> = pick(&g).into_iter()
        .map(|(i, j)| match delay_ms(&g[i][j].data) {
            Some(ms) => Step::Wait(ms),
            None => Step::Exec((i, j)),
        })
        .collect();
    if !steps.iter().any(|s| matches!(s, Step::Wait(_))) {
        let mut out = Command::None;
        let sleep = |ms| std::thread::sleep(Duration::from_millis(ms));
        let n = steps.len();
        let mut done = 0;
        // nach dem letzten Schritt nicht mehr warten
        run_steps(&steps, |(i, j)| g[i][j].execute_nested(env), |ms| {
            done += 1;
            if done < n {
                sleep(ms)
            }
        }, |c| out = std::mem::replace(&mut out, Command::None).then(c));
        return out;
    }
    drop(g);
    let owned = env.snapshot();
    // Fortschritt auf der Taste (Einstellungen › Stil › Multi-Aktion), nur bei mehreren Schritten
    let n = steps.len().min(u16::MAX as usize) as u16;
    let show = n > 1 && env.cfg.get("multi_progress").and_then(Value::as_bool).unwrap_or(true);
    std::thread::spawn(move || {
        let Ok(mut g) = groups.try_lock() else { return };
        let env = owned.env();
        let progress = |done: usize| {
            if show {
                env.outbox.send(Command::Progress(Some((done.min(n as usize) as u16, n))));
            }
        };
        progress(0);
        for (i, st) in steps.iter().enumerate() {
            run_steps(std::slice::from_ref(st), |(i, j)| g[i][j].execute_nested(&env),
                      |ms| std::thread::sleep(Duration::from_millis(ms)), |c| env.outbox.send(c));
            progress(i + 1);
        }
        if show {
            env.outbox.send(Command::Progress(None));
        }
    });
    Command::None
}

impl Behavior for MultiAction {
    fn key_down(&mut self, a: &ActionData, env: &Env) -> Command {
        let groups = self.groups(a);
        match self.kind {
            MultiKind::Routine => launch(groups, |g| {
                g.iter().enumerate().flat_map(|(i, grp)| (0..grp.len()).map(move |j| (i, j))).collect()
            }, env),
            MultiKind::Switch => {
                let state = a.state;
                let c = launch(groups, |g| {
                    if g.is_empty() {
                        return Vec::new();
                    }
                    let i = state.min(g.len() - 1);
                    (0..g[i].len()).map(|j| (i, j)).collect()
                }, env);
                c.then(Command::SetState(a.state + 1))
            }
            MultiKind::Random => launch(groups, |g| {
                let pool: Vec<(usize, usize)> = g.iter().enumerate()
                    .flat_map(|(i, grp)| grp.iter().enumerate().filter(|(_, x)| delay_ms(&x.data).is_none()).map(move |(j, _)| (i, j)))
                    .collect();
                if pool.is_empty() { Vec::new() } else { vec![pool[fastrand::usize(..pool.len())]] }
            }, env),
        }
    }
    fn execute(&mut self, a: &ActionData, env: &Env) -> Command {
        self.key_down(a, env)
    }
}

/// Wartezeit – allein auf einer Taste tut sie nichts.
pub struct Delay;
impl Behavior for Delay {}

// ---------------------------------------------------------------------------------- Key Logic
/// Was Key Logic beim Loslassen tut.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LogicStep {
    /// Platz sofort auslösen (0 = Tippen, 1 = Doppeltippen, 2 = Halten)
    Fire(usize),
    /// auf ein zweites Tippen warten
    Wait,
}

/// Entscheidung nach dem Loslassen: langer Druck, gibt es Halten/Doppeltippen, wartet schon ein Tippen?
pub fn logic_step(long: bool, has_hold: bool, has_double: bool, pending: bool) -> LogicStep {
    if long && has_hold {
        LogicStep::Fire(2)
    } else if !has_double {
        LogicStep::Fire(0)
    } else if pending {
        LogicStep::Fire(1)
    } else {
        LogicStep::Wait
    }
}

type Slots = Arc<Mutex<Vec<Option<Action>>>>;

/// com.elgato.streamdeck.keys.logic: [Tippen, Doppeltippen, Halten] -> je eine Aktion.
#[derive(Default)]
pub struct KeyLogic {
    slots: Option<Slots>,
    /// Nummer des wartenden Tippens (None = keins)
    pending: Arc<Mutex<Option<u64>>>,
    gen: u64,
}

fn slots_of(slots: &mut Option<Slots>, a: &ActionData) -> Slots {
    slots.get_or_insert_with(|| Arc::new(Mutex::new(child_slots(&a.data).iter().map(|d| d.as_ref().map(|d| make(d, a))).collect()))).clone()
}

fn fire_slot(slots: &Slots, i: usize, env: &Env) -> Command {
    let Ok(mut s) = slots.lock() else { return Command::None };
    s.get_mut(i).and_then(Option::as_mut).map(|x| x.execute_nested(env)).unwrap_or(Command::None)
}

impl Behavior for KeyLogic {
    fn key_up(&mut self, a: &ActionData, env: &Env, held_ms: u64) -> Command {
        let slots = slots_of(&mut self.slots, a);
        let (has_double, has_hold) = {
            let s = slots.lock().unwrap();
            (s[1].is_some(), s[2].is_some())
        };
        let pending = self.pending.lock().unwrap().take().is_some();
        match logic_step(env.is_long(held_ms), has_hold, has_double, pending) {
            LogicStep::Fire(i) => fire_slot(&slots, i, env),
            LogicStep::Wait => {
                self.gen += 1;
                let gen = self.gen;
                *self.pending.lock().unwrap() = Some(gen);
                let wait = env.cfg_i64("double_press_ms", 300).max(0) as u64;
                let (pending, owned) = (self.pending.clone(), env.snapshot());
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_millis(wait));
                    let mut p = pending.lock().unwrap();
                    if *p != Some(gen) {
                        return; // inzwischen doppelt getippt
                    }
                    *p = None;
                    drop(p);
                    let env = owned.env();
                    let c = fire_slot(&slots, 0, &env);
                    env.outbox.send(c);
                });
                Command::None
            }
        }
    }
}

// ----------------------------------------------------------------- Action Wheel / Dial Stack
fn items_of(a: &ActionData) -> Vec<Action> {
    child_groups(&a.data).iter().flatten().map(|d| make(d, a)).collect()
}

fn current_idx(a: &ActionData, n: usize) -> usize {
    let i = a.data.get("Settings").and_then(|s| s.get("CurrentIdx")).and_then(Value::as_i64).unwrap_or(0);
    if n == 0 { 0 } else { i.rem_euclid(n as i64) as usize }
}

/// Unteraktionen eines Rads bzw. Stapels: beim ersten Gebrauch angelegt (auch zum Zeichnen, daher
/// OnceCell), die Position kommt bis zum ersten Drehen aus Settings.CurrentIdx.
#[derive(Default)]
struct Stack {
    items: OnceCell<Vec<Action>>,
    idx: Option<usize>,
}

impl Stack {
    fn items(&self, a: &ActionData) -> &Vec<Action> {
        self.items.get_or_init(|| items_of(a))
    }
    fn idx(&self, a: &ActionData) -> usize {
        let n = self.items(a).len();
        self.idx.unwrap_or_else(|| current_idx(a, n))
    }
    fn len(&self, a: &ActionData) -> usize {
        self.items(a).len()
    }
    fn get(&self, a: &ActionData) -> Option<&Action> {
        self.items(a).get(self.idx(a))
    }
    fn get_mut(&mut self, a: &ActionData) -> Option<&mut Action> {
        let i = self.idx(a);
        self.idx = Some(i);
        self.items.get_mut()?.get_mut(i)
    }
    fn set(&mut self, a: &ActionData, i: usize) {
        self.items(a);
        self.idx = Some(i);
    }
}

/// com.elgato.streamdeck.keys.stack (Action Wheel): Drehen wählt, Drücken führt aus.
#[derive(Default)]
pub struct ActionWheel {
    stack: Stack,
}

/// Neue Position nach `ticks` Rasten (umlaufend).
pub fn wheel_pos(idx: usize, ticks: i32, n: usize) -> usize {
    if n == 0 { 0 } else { (idx as i64 + ticks as i64).rem_euclid(n as i64) as usize }
}

impl Behavior for ActionWheel {
    fn dial_turn(&mut self, a: &ActionData, _env: &Env, ticks: i8) -> Command {
        let inv = a.data.get("Settings").and_then(|s| s.get("InvertRotation")).and_then(Value::as_bool).unwrap_or(false);
        let n = self.stack.len(a);
        if n == 0 {
            return Command::None;
        }
        let i = wheel_pos(self.stack.idx(a), ticks as i32 * if inv { -1 } else { 1 }, n);
        self.stack.set(a, i);
        Command::Redraw
    }
    fn dial_down(&mut self, a: &ActionData, env: &Env) -> Command {
        self.stack.get_mut(a).map(|c| c.execute_nested(env)).unwrap_or(Command::None)
    }
    fn dial_up(&mut self, _a: &ActionData, _env: &Env, _held_ms: u64) -> Command {
        Command::None
    }
    fn title(&self, a: &ActionData, _env: &Env) -> Option<String> {
        let c = self.stack.get(a)?;
        let t = c.data.title();
        Some(if t.is_empty() { c.data.name() } else { t })
    }
    fn feedback(&self, a: &ActionData, env: &Env) -> Feedback {
        let mut fb = Feedback::of(a);
        if let Some(t) = self.title(a, env) {
            fb.title = t;
            fb.value = format!("{}/{}", self.stack.idx(a) + 1, self.stack.len(a));
        }
        fb
    }
    fn encoder_icon_path(&self, a: &ActionData) -> Option<PathBuf> {
        match self.stack.get(a) {
            Some(c) => c.data.image_path(),
            None => a.encoder_icon_path(),
        }
    }
}

/// com.elgato.streamdeck.dial.stack: mehrere Drehregler-Aktionen; Drücken wechselt zur nächsten.
#[derive(Default)]
pub struct DialStack {
    stack: Stack,
}

impl DialStack {
    fn current(&mut self, a: &ActionData) -> Option<&mut Action> {
        self.stack.get_mut(a)
    }
}

impl Behavior for DialStack {
    fn dial_turn(&mut self, a: &ActionData, env: &Env, ticks: i8) -> Command {
        match self.current(a) {
            Some(c) => {
                let cmd = c.behavior.dial_turn(&c.data, env, ticks);
                c.absorb(cmd).then(Command::Redraw)
            }
            None => Command::None,
        }
    }
    fn dial_down(&mut self, a: &ActionData, _env: &Env) -> Command {
        let n = self.stack.len(a);
        if n == 0 {
            return Command::None;
        }
        let i = (self.stack.idx(a) + 1) % n;
        self.stack.set(a, i);
        Command::Redraw
    }
    fn dial_up(&mut self, _a: &ActionData, _env: &Env, _held_ms: u64) -> Command {
        Command::None
    }
    fn touch(&mut self, a: &ActionData, env: &Env, long: bool) -> Command {
        match self.current(a) {
            Some(c) => {
                let cmd = c.behavior.touch(&c.data, env, long);
                c.absorb(cmd)
            }
            None => Command::None,
        }
    }
    fn needs_tick(&self) -> bool {
        true
    }
    fn tick(&mut self, a: &ActionData) -> bool {
        match self.current(a) {
            Some(c) => c.behavior.needs_tick() && c.behavior.tick(&c.data),
            None => false,
        }
    }
    fn feedback(&self, a: &ActionData, env: &Env) -> Feedback {
        let Some(c) = self.stack.get(a) else { return Feedback::of(a) };
        let mut fb = c.behavior.feedback(&c.data, env);
        let n = self.stack.len(a);
        let show = a.data.get("Settings").and_then(|s| s.get("ShowStackIcon")).and_then(Value::as_bool).unwrap_or(true);
        if show && n > 1 {
            fb.title = format!("{}  {}/{}", fb.title, self.stack.idx(a) + 1, n);
        }
        fb
    }
    fn encoder_icon_path(&self, a: &ActionData) -> Option<PathBuf> {
        let c = self.stack.get(a)?;
        c.behavior.encoder_icon_path(&c.data)
    }
    fn encoder_icon(&self, a: &ActionData, env: &Env, size: (u32, u32)) -> Option<image::RgbaImage> {
        let c = self.stack.get(a)?;
        c.behavior.encoder_icon(&c.data, env, size)
    }
}

/// com.elgato.streamdeck.keys.adaptor (Action Trigger): [Links drehen, Drücken, Rechts drehen].
#[derive(Default)]
pub struct ActionTrigger {
    slots: Option<Slots>,
}

impl Behavior for ActionTrigger {
    fn dial_turn(&mut self, a: &ActionData, env: &Env, ticks: i8) -> Command {
        let slots = slots_of(&mut self.slots, a);
        let i = if ticks > 0 { 2 } else { 0 };
        let mut out = Command::None;
        for _ in 0..ticks.unsigned_abs().min(10) {
            out = out.then(fire_slot(&slots, i, env));
        }
        out
    }
    fn dial_down(&mut self, a: &ActionData, env: &Env) -> Command {
        let slots = slots_of(&mut self.slots, a);
        fire_slot(&slots, 1, env)
    }
    fn dial_up(&mut self, _a: &ActionData, _env: &Env, _held_ms: u64) -> Command {
        Command::None
    }
}

pub fn behavior_for(uuid: &str) -> Option<Box<dyn Behavior>> {
    Some(match uuid {
        "com.elgato.streamdeck.multiactions.routine" => Box::new(MultiAction::new(MultiKind::Routine)),
        "com.elgato.streamdeck.multiactions.routine2" => Box::new(MultiAction::new(MultiKind::Switch)),
        "com.elgato.streamdeck.multiactions.random" => Box::new(MultiAction::new(MultiKind::Random)),
        DELAY => Box::new(Delay),
        "com.elgato.streamdeck.keys.logic" => Box::<KeyLogic>::default(),
        "com.elgato.streamdeck.keys.stack" => Box::<ActionWheel>::default(),
        "com.elgato.streamdeck.dial.stack" => Box::<DialStack>::default(),
        "com.elgato.streamdeck.keys.adaptor" => Box::<ActionTrigger>::default(),
        _ => return None,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::path::PathBuf;

    fn obj(v: Value) -> Map<String, Value> {
        v.as_object().cloned().unwrap()
    }

    fn data(v: Value) -> ActionData {
        ActionData::new(obj(v), PathBuf::new())
    }

    #[test]
    fn sequencing_with_fake_runner() {
        let steps = [Step::Exec("a"), Step::Wait(500), Step::Exec("b"), Step::Exec("c")];
        let mut trace = Vec::new();
        let log = std::cell::RefCell::new(&mut trace);
        let mut outs = Vec::new();
        run_steps(&steps, |t| {
            log.borrow_mut().push(format!("run {t}"));
            if t == "b" { Command::NextPage } else { Command::None }
        }, |ms| log.borrow_mut().push(format!("wait {ms}")), |c| outs.push(c));
        assert_eq!(trace, vec!["run a", "wait 30", "wait 500", "run b", "wait 30", "run c", "wait 30"]);
        assert_eq!(outs, vec![Command::NextPage]);
    }

    #[test]
    fn children_and_groups() {
        let d = obj(json!({"UUID": "com.elgato.streamdeck.multiactions.routine2", "Actions": [
            {"Actions": [{"UUID": "com.elgato.streamdeck.page.next"}, {"UUID": ""}]},
            {"Actions": [{"UUID": DELAY, "Settings": {"delay": 250}}, {"UUID": "com.elgato.streamdeck.page.previous"}]},
        ]}));
        let g = child_groups(&d);
        assert_eq!(g.len(), 2);
        assert_eq!(g[0].len(), 1);
        assert_eq!(g[1].len(), 2);
        let flat = obj(json!({"Actions": [{"UUID": "a"}, {"UUID": "b"}]}));
        assert_eq!(child_groups(&flat).len(), 2);
        let slots = child_slots(&obj(json!({"Actions": [{"UUID": "a"}, {}]})));
        assert_eq!(slots.len(), 3);
        assert!(slots[0].is_some() && slots[1].is_none() && slots[2].is_none());
        assert_eq!(delay_ms(&data(json!({"UUID": DELAY, "Settings": {"delay": "250"}}))), Some(250));
        assert_eq!(delay_ms(&data(json!({"UUID": "x"}))), None);
    }

    #[test]
    fn routine_runs_navigation_in_order() {
        let a = data(json!({"UUID": "com.elgato.streamdeck.multiactions.routine", "Actions": [
            {"Actions": [{"UUID": "com.elgato.streamdeck.page.next"}, {"UUID": "com.elgato.streamdeck.page.goto", "Settings": {"PageIndex": 3}}]},
        ]}));
        let cfg = Map::new();
        let env = Env::new(&cfg, 0, 3);
        let mut m = MultiAction::new(MultiKind::Routine);
        assert_eq!(m.key_down(&a, &env), Command::Run(vec![Command::NextPage, Command::GotoPage(2)]));
    }

    #[test]
    fn switch_runs_group_of_state_and_advances() {
        let mut a = data(json!({"UUID": "com.elgato.streamdeck.multiactions.routine2", "States": [{}, {}], "Actions": [
            {"Actions": [{"UUID": "com.elgato.streamdeck.page.next"}]},
            {"Actions": [{"UUID": "com.elgato.streamdeck.page.previous"}]},
        ]}));
        let cfg = Map::new();
        let env = Env::new(&cfg, 0, 3);
        let mut m = MultiAction::new(MultiKind::Switch);
        assert_eq!(m.key_down(&a, &env), Command::Run(vec![Command::NextPage, Command::SetState(1)]));
        a.state = 1;
        assert_eq!(m.key_down(&a, &env), Command::Run(vec![Command::PreviousPage, Command::SetState(2)]));
    }

    #[test]
    fn delayed_routine_sends_through_outbox() {
        let a = data(json!({"UUID": "com.elgato.streamdeck.multiactions.routine", "Actions": [
            {"UUID": DELAY, "Settings": {"delay": 10}}, {"UUID": "com.elgato.streamdeck.page.next"},
        ]}));
        let cfg = Map::new();
        let env = Env::new(&cfg, 0, 3);
        let mut m = MultiAction::new(MultiKind::Routine);
        assert_eq!(m.key_down(&a, &env), Command::None);
        // bis zum Ende des Ablaufs (Fortschritt None) einsammeln
        let mut got = Vec::new();
        for _ in 0..100 {
            got.extend(env.outbox.drain());
            if got.last() == Some(&(None, Command::Progress(None))) {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        let (progress, cmds): (Vec<_>, Vec<_>) = got.into_iter().partition(|(_, c)| matches!(c, Command::Progress(_)));
        assert_eq!(cmds, vec![(None, Command::NextPage)]);
        let progress: Vec<Command> = progress.into_iter().map(|(_, c)| c).collect();
        assert_eq!(progress, vec![Command::Progress(Some((0, 2))), Command::Progress(Some((1, 2))),
                                  Command::Progress(Some((2, 2))), Command::Progress(None)]);
        // ohne Fortschrittsanzeige (Einstellung multi_progress) nur der Befehl
        let cfg = serde_json::from_value::<Map<String, Value>>(json!({"multi_progress": false})).unwrap();
        let env = Env::new(&cfg, 0, 3);
        let mut m = MultiAction::new(MultiKind::Routine);
        m.key_down(&a, &env);
        let mut got = Vec::new();
        for _ in 0..100 {
            got.extend(env.outbox.drain());
            if !got.is_empty() {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(got, vec![(None, Command::NextPage)]);
    }

    #[test]
    fn key_logic_decisions() {
        assert_eq!(logic_step(true, true, true, false), LogicStep::Fire(2));
        assert_eq!(logic_step(true, false, false, false), LogicStep::Fire(0));
        assert_eq!(logic_step(false, true, false, false), LogicStep::Fire(0));
        assert_eq!(logic_step(false, false, true, false), LogicStep::Wait);
        assert_eq!(logic_step(false, false, true, true), LogicStep::Fire(1));
    }

    #[test]
    fn key_logic_double_press() {
        let a = data(json!({"UUID": "com.elgato.streamdeck.keys.logic", "Actions": [
            {"UUID": "com.elgato.streamdeck.page.next"}, {"UUID": "com.elgato.streamdeck.page.previous"},
            {"UUID": "com.elgato.streamdeck.system.sleep"}]}));
        let cfg = obj(json!({"double_press_ms": 300, "long_press_ms": 500}));
        let env = Env::new(&cfg, 0, 3);
        let mut k = KeyLogic::default();
        assert_eq!(k.key_up(&a, &env, 900), Command::Sleep);
        assert_eq!(k.key_up(&a, &env, 0), Command::None);
        assert_eq!(k.key_up(&a, &env, 0), Command::PreviousPage);
        // einzelnes Tippen kommt nach der Wartezeit über die Outbox
        let cfg = obj(json!({"double_press_ms": 10}));
        let env = Env::new(&cfg, 0, 3);
        assert_eq!(k.key_up(&a, &env, 0), Command::None);
        let mut got = Vec::new();
        for _ in 0..100 {
            got.extend(env.outbox.drain());
            if !got.is_empty() {
                break;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(got, vec![(None, Command::NextPage)]);
    }

    #[test]
    fn wheel_and_stacks() {
        assert_eq!(wheel_pos(0, -1, 3), 2);
        assert_eq!(wheel_pos(2, 2, 3), 1);
        assert_eq!(wheel_pos(0, 5, 0), 0);
        let mut a = data(json!({"UUID": "com.elgato.streamdeck.keys.stack", "Settings": {"CurrentIdx": 1}, "Actions": [
            {"UUID": "com.elgato.streamdeck.page.next"}, {"UUID": "com.elgato.streamdeck.page.previous"}]}));
        a.dial = true;
        let cfg = Map::new();
        let env = Env::new(&cfg, 0, 3);
        let mut w = ActionWheel::default();
        assert_eq!(w.dial_down(&a, &env), Command::PreviousPage);
        w.dial_turn(&a, &env, 1);
        assert_eq!(w.dial_down(&a, &env), Command::NextPage);

        let t = data(json!({"UUID": "com.elgato.streamdeck.keys.adaptor", "Actions": [
            {"UUID": "com.elgato.streamdeck.page.previous"}, {"UUID": "com.elgato.streamdeck.system.sleep"}, {"UUID": "com.elgato.streamdeck.page.next"}]}));
        let mut tr = ActionTrigger::default();
        assert_eq!(tr.dial_turn(&t, &env, 2), Command::Run(vec![Command::NextPage, Command::NextPage]));
        assert_eq!(tr.dial_turn(&t, &env, -1), Command::PreviousPage);
        assert_eq!(tr.dial_down(&t, &env), Command::Sleep);

        let s = data(json!({"UUID": "com.elgato.streamdeck.dial.stack", "Actions": [
            {"UUID": "com.elgato.streamdeck.system.keybrightness"}, {"UUID": "com.elgato.streamdeck.page.next"}]}));
        let mut ds = DialStack::default();
        assert_eq!(ds.feedback(&s, &env).title, "keybrightness  1/2");
        assert_eq!(ds.dial_down(&s, &env), Command::Redraw);
        assert_eq!(ds.stack.idx, Some(1));
        assert_eq!(ds.feedback(&s, &env).title, "next  2/2");
        assert_eq!(ds.touch(&s, &env, false), Command::NextPage);
    }
}
