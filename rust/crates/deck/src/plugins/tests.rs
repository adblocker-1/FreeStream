//! Tests für Plugins: Manifest, Laufzeit, Installation, Bilder und der WebSocket-Host von Anfang bis Ende.

use super::*;
use crate::actions::{ActionData, Behavior, Command, Env};
use serde_json::json;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::{Duration, Instant};
use tungstenite::client::IntoClientRequest;

fn write_plugin(root: &Path, dir: &str, manifest: Value) -> PathBuf {
    let d = root.join(dir);
    std::fs::create_dir_all(&d).unwrap();
    std::fs::write(d.join("manifest.json"), manifest.to_string()).unwrap();
    d
}

fn red_png() -> Vec<u8> {
    let img = RgbaImage::from_pixel(10, 10, image::Rgba([255, 0, 0, 255]));
    let mut out = std::io::Cursor::new(Vec::new());
    img.write_to(&mut out, image::ImageFormat::Png).unwrap();
    out.into_inner()
}

#[test]
fn parses_manifest_and_decides_runtime() {
    let tmp = tempfile::tempdir().unwrap();
    let root = tmp.path();
    write_plugin(root, "com.example.node.sdPlugin", json!({
        "UUID": "com.example.node", "Name": "Node Plugin", "Version": "1.2.0", "CodePath": "bin/plugin.js",
        "PropertyInspectorPath": "ui/pi.html", "Icon": "imgs/plugin", "Author": "Someone",
        "Actions": [
            {"UUID": "com.example.node.toggle", "Name": "Toggle", "States": [{"Image": "imgs/a"}, {"Image": "imgs/b"}],
             "Tooltip": "Switches", "Controllers": ["Keypad", "Encoder"], "PropertyInspectorPath": "ui/toggle.html"},
            {"UUID": "com.example.node.plain"},
            {"Name": "ohne UUID"}
        ]
    }));
    std::fs::create_dir_all(root.join("com.example.node.sdPlugin/imgs")).unwrap();
    std::fs::write(root.join("com.example.node.sdPlugin/imgs/b.png"), red_png()).unwrap();
    // Linux-Programm über CodePathLin
    write_plugin(root, "com.example.lin.sdPlugin", json!({"UUID": "com.example.lin", "CodePath": "x.exe", "CodePathLin": "linux/run", "Actions": []}));
    // „Nodejs“ im Manifest, CodePath ohne .js
    write_plugin(root, "com.example.nodejs.sdPlugin", json!({"UUID": "com.example.nodejs", "CodePath": "main", "Nodejs": {"Version": "20"}}));
    // HTML-Plugin
    write_plugin(root, "com.example.html.sdPlugin", json!({"UUID": "com.example.html", "CodePath": "index.html"}));
    // nur Windows
    write_plugin(root, "com.example.win.sdPlugin", json!({"UUID": "com.example.win", "CodePath": "plugin.exe", "Nodejs": {"Version": "20"}}));
    // ELF-Programm im CodePath; UUID aus dem Ordnernamen
    let elf = write_plugin(root, "com.example.elf.sdPlugin", json!({"CodePath": "run"}));
    std::fs::write(elf.join("run"), b"\x7fELF\x02\x01\x01").unwrap();
    // Programm ohne ELF-Kopf
    let sh = write_plugin(root, "com.example.sh.sdPlugin", json!({"UUID": "com.example.sh", "CodePath": "run.sh"}));
    std::fs::write(sh.join("run.sh"), b"#!/bin/sh\n").unwrap();
    // kaputtes Manifest wird übergangen
    std::fs::create_dir_all(root.join("broken.sdPlugin")).unwrap();
    std::fs::write(root.join("broken.sdPlugin/manifest.json"), "{nope").unwrap();

    let all = installed(Some(root));
    assert_eq!(all.len(), 7);
    let n = &all["com.example.node"];
    assert_eq!((n.name.as_str(), n.version.as_str(), n.code_path.as_str()), ("Node Plugin", "1.2.0", "bin/plugin.js"));
    assert_eq!(n.runtime, Some(Runtime::Node));
    assert!(n.runnable() && n.reason.is_empty());
    assert_eq!(n.actions.keys().cloned().collect::<Vec<_>>(), vec!["com.example.node.toggle", "com.example.node.plain"]);
    assert_eq!(n.property_inspector_path("com.example.node.toggle").as_deref(), Some("ui/toggle.html"));
    assert_eq!(n.property_inspector_path("com.example.node.plain").as_deref(), Some("ui/pi.html"));
    assert_eq!(n.action_icon("com.example.node.toggle", 1), Some(n.dir.join("imgs/b.png")));
    assert_eq!(n.action_icon("com.example.node.toggle", 0), None);
    assert!(meta_line(n).contains("Someone"));

    let lin = &all["com.example.lin"];
    assert_eq!((lin.runtime, lin.code_path.as_str()), (Some(Runtime::Exec), "linux/run"));
    assert_eq!(all["com.example.nodejs"].runtime, Some(Runtime::Node));
    let html = &all["com.example.html"];
    assert_eq!(html.runtime, None);
    assert_eq!(html.reason, freestream_core::tr!("HTML plugin (runs only in the Elgato app)"));
    let win = &all["com.example.win"];
    assert_eq!(win.runtime, None);
    assert_eq!(win.reason, freestream_core::tr!("built for Windows/macOS only"));
    assert_eq!(all["com.example.elf"].runtime, Some(Runtime::Exec));
    assert_eq!(all["com.example.sh"].runtime, None);

    // Aktion -> Plugin (im Manifest oder über das Präfix)
    assert_eq!(plugin_for_action("com.example.node.toggle", Some(root)).unwrap().uuid, "com.example.node");
    assert_eq!(plugin_for_action("com.example.lin.other", Some(root)).unwrap().uuid, "com.example.lin");
    assert!(plugin_for_action("com.unknown.x", Some(root)).is_none());

    // Aktionsarten für den Editor
    let types = plugin_types(Some(root));
    let t = types.iter().find(|t| t.uuid == "com.example.node.toggle").unwrap();
    assert_eq!(t.name(), "Toggle");
    assert_eq!(t.category.id(), "Plugin: Node Plugin");
    assert_eq!(t.states, 2);
    assert_eq!(t.controllers, vec![KEY, DIAL]);
    assert_eq!(t.description(), "Switches");
    assert_eq!((t.plugin.as_str(), t.plugin_version.as_str()), ("com.example.node", "1.2.0"));
    let p = types.iter().find(|t| t.uuid == "com.example.node.plain").unwrap();
    assert_eq!((p.name(), p.states, p.controllers.clone()), ("com.example.node.plain".to_string(), 1, vec![KEY]));

    // Befehlszeile: Liste
    let list = cli_list(Some(root));
    assert!(list.lines.iter().any(|l| l.starts_with("com.example.node") && l.contains("[node]")));
}

#[test]
fn installs_zip_and_removes() {
    let tmp = tempfile::tempdir().unwrap();
    let file = tmp.path().join("x.streamDeckPlugin");
    {
        let mut z = zip::ZipWriter::new(std::fs::File::create(&file).unwrap());
        let o = zip::write::SimpleFileOptions::default();
        z.start_file("com.example.zip.sdPlugin/manifest.json", o).unwrap();
        z.write_all(json!({"UUID": "com.example.zip", "Name": "Zip", "CodePath": "bin/app.js",
                           "Actions": [{"UUID": "com.example.zip.a"}]}).to_string().as_bytes()).unwrap();
        z.start_file("com.example.zip.sdPlugin/bin/app.js", o).unwrap();
        z.write_all(b"console.log(1)").unwrap();
        z.start_file("com.example.zip.sdPlugin/bin/helper", o).unwrap();
        z.write_all(b"#!/bin/sh").unwrap();
        z.finish().unwrap();
    }
    let root = tmp.path().join("plugins");
    let ids = install(&file, Some(&root)).unwrap();
    assert_eq!(ids, vec!["com.example.zip"]);
    let d = root.join("com.example.zip.sdPlugin");
    assert!(d.join("bin/app.js").is_file());
    use std::os::unix::fs::PermissionsExt;
    assert_ne!(std::fs::metadata(d.join("bin/helper")).unwrap().permissions().mode() & 0o111, 0);
    assert!(installed(Some(&root)).contains_key("com.example.zip"));
    // noch einmal: ersetzt
    assert_eq!(install(&file, Some(&root)).unwrap(), vec!["com.example.zip"]);
    let r = cli_install(std::slice::from_ref(&file), Some(&root));
    assert_eq!(r.code, 0);
    assert_eq!(r.lines, vec![freestream_core::tr!("Installed: {name} ({uuid})", name = "Zip", uuid = "com.example.zip")]);
    assert_eq!(install_report(&[file], Some(&root)), vec![freestream_core::tr!("“{name}” installed", name = "Zip")]);
    assert!(remove("com.example.zip", Some(&root)));
    assert!(!remove("com.example.zip", Some(&root)));
    assert!(installed(Some(&root)).is_empty());
    assert_eq!(cli_remove(&["nope".into()], Some(&root)).code, 1);

    // Pfade aus dem Archiv heraus werden abgelehnt
    let evil = tmp.path().join("evil.zip");
    {
        let mut z = zip::ZipWriter::new(std::fs::File::create(&evil).unwrap());
        z.start_file("../escape.txt", zip::write::SimpleFileOptions::default()).unwrap();
        z.write_all(b"x").unwrap();
        z.finish().unwrap();
    }
    assert!(install(&evil, Some(&root)).is_err());
    assert!(!tmp.path().join("escape.txt").exists());
}

#[test]
fn decodes_images() {
    let png = red_png();
    let b64 = data_encoding::BASE64.encode(&png);
    let img = decode_image(&format!("data:image/png;base64,{b64}"), None, 144).unwrap();
    assert_eq!((img.width(), img.get_pixel(5, 5).0), (10, [255, 0, 0, 255]));
    // reines Base64 (ohne data:)
    assert_eq!(decode_image(&b64, None, 144).unwrap().get_pixel(1, 1).0, [255, 0, 0, 255]);
    // Base64 mit Zeilenumbrüchen in einer data-URL
    let wrapped: String = b64.chars().collect::<Vec<_>>().chunks(20).map(|c| c.iter().collect::<String>() + "\n").collect();
    assert!(decode_image(&format!("data:image/png;base64,{wrapped}"), None, 144).is_some());
    // SVG als Base64, als Text in der data-URL und roh
    let svg = r##"<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10" fill="#00ff00"/></svg>"##;
    let s1 = decode_image(&format!("data:image/svg+xml;base64,{}", data_encoding::BASE64.encode(svg.as_bytes())), None, 144).unwrap();
    assert_eq!((s1.width(), s1.height(), s1.get_pixel(70, 70).0), (144, 144, [0, 255, 0, 255]));
    let s2 = decode_image(&format!("data:image/svg+xml;charset=utf8,{}", svg.replace('#', "%23")), None, 96).unwrap();
    assert_eq!((s2.width(), s2.get_pixel(50, 50).0), (96, [0, 255, 0, 255]));
    assert!(decode_image(svg, None, 72).is_some());
    // Pfad im Plugin-Ordner
    let tmp = tempfile::tempdir().unwrap();
    std::fs::write(tmp.path().join("icon.png"), &png).unwrap();
    assert!(decode_image("icon", Some(tmp.path()), 144).is_some());
    // Unsinn
    assert!(decode_image("", None, 144).is_none());
    assert!(decode_image("not base64 at all!", None, 144).is_none());
    assert!(decode_image("data:image/png;base64,AAAA", None, 144).is_none());
}

#[test]
fn slots_and_urls() {
    assert_eq!(PiSlot::parse("key:2,1"), Some(PiSlot::Key(2, 1)));
    assert_eq!(PiSlot::parse("dial:3"), Some(PiSlot::Dial(3)));
    assert_eq!(PiSlot::parse("x"), None);
    assert_eq!(PiSlot::Key(2, 1).query(), "key:2,1");
    assert_eq!(pi_url_for(1234, "tok", "A B", "p", PiSlot::Key(0, 1)), "http://127.0.0.1:1234/pi/tok?profile=A%20B&page=p&slot=key%3A0%2C1");
    assert!(!browser_command("http://x").is_empty());
}

#[test]
fn saves_settings_in_multi_actions() {
    let tmp = tempfile::tempdir().unwrap();
    let inner = json!({"UUID": "com.example.p.a", "Settings": {"v": 1}});
    std::fs::write(tmp.path().join("manifest.json"), json!({"Controllers": [{"Type": "Keypad", "Actions": {
        "0,0": {"UUID": "com.elgato.streamdeck.multiactions.routine", "Settings": {}, "Actions": [{"Actions": [inner.clone()]}]}
    }}]}).to_string()).unwrap();
    let old = inner.as_object().unwrap().clone();
    let new = json!({"v": 2}).as_object().unwrap().clone();
    assert!(host::save_action_settings(tmp.path(), &old, &new).unwrap());
    let m = freestream_core::profiles::read_json(&tmp.path().join("manifest.json")).unwrap();
    assert_eq!(m["Controllers"][0]["Actions"]["0,0"]["Actions"][0]["Actions"][0]["Settings"]["v"], 2);
    assert!(!host::save_action_settings(tmp.path(), &old, &new).unwrap());
}

// ======================================================================= WebSocket von Anfang bis Ende
type Ws = tungstenite::WebSocket<TcpStream>;

fn connect(port: u16, origin: Option<&str>) -> Result<Ws, tungstenite::Error> {
    let stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
    let mut req = format!("ws://127.0.0.1:{port}/").into_client_request().unwrap();
    if let Some(o) = origin {
        req.headers_mut().insert("Origin", o.parse().unwrap());
    }
    let (ws, _) = tungstenite::client::client(req, stream).map_err(|e| match e {
        tungstenite::HandshakeError::Failure(e) => e,
        tungstenite::HandshakeError::Interrupted(_) => tungstenite::Error::ConnectionClosed,
    })?;
    ws.get_ref().set_read_timeout(Some(Duration::from_millis(50))).unwrap();
    Ok(ws)
}

fn send(ws: &mut Ws, v: Value) {
    ws.send(tungstenite::Message::text(v.to_string())).unwrap();
}

/// Nächste Nachricht mit diesem Ereignis (andere werden übergangen).
fn expect(ws: &mut Ws, event: &str) -> Value {
    let end = Instant::now() + Duration::from_secs(5);
    while Instant::now() < end {
        match ws.read() {
            Ok(tungstenite::Message::Text(t)) => {
                let v: Value = serde_json::from_str(t.as_str()).unwrap();
                if v["event"] == event {
                    return v;
                }
            }
            Ok(_) => {}
            Err(tungstenite::Error::Io(e)) if matches!(e.kind(), std::io::ErrorKind::WouldBlock | std::io::ErrorKind::TimedOut) => {}
            Err(e) => panic!("{event}: {e}"),
        }
    }
    panic!("kein {event}");
}

fn wait_for(mut f: impl FnMut() -> bool) {
    let end = Instant::now() + Duration::from_secs(5);
    while Instant::now() < end {
        if f() {
            return;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    panic!("Bedingung nicht erfüllt");
}

fn http_get(port: u16, path: &str) -> (String, String) {
    let mut s = TcpStream::connect(("127.0.0.1", port)).unwrap();
    write!(s, "GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n").unwrap();
    let mut out = Vec::new();
    s.read_to_end(&mut out).unwrap();
    let text = String::from_utf8_lossy(&out).to_string();
    let (head, body) = text.split_once("\r\n\r\n").unwrap();
    (head.to_string(), body.to_string())
}

#[test]
fn host_end_to_end() {
    let tmp = tempfile::tempdir().unwrap();
    let plugins = tmp.path().join("plugins");
    let pd = write_plugin(&plugins, "com.example.e2e.sdPlugin", json!({
        "UUID": "com.example.e2e", "Name": "E2E", "CodePath": "plugin.js", "PropertyInspectorPath": "pi.html",
        "Actions": [{"UUID": "com.example.e2e.act", "Name": "Act", "States": [{}, {}]}]
    }));
    std::fs::write(pd.join("pi.html"), "<html><head></head><body>PI</body></html>").unwrap();
    // Profil mit einer Seite, auf der die Aktion bei Spalte 1, Zeile 0 liegt
    let profiles = tmp.path().join("profiles");
    let proot = profiles.join("TESTPROFILE.sdProfile");
    let page_dir = proot.join("Profiles").join("PAGE1");
    std::fs::create_dir_all(&page_dir).unwrap();
    std::fs::write(proot.join("manifest.json"), json!({"Name": "Test", "Device": {"Model": "20GBA9901", "UUID": ""},
        "Pages": {"Pages": ["PAGE1"], "Default": "PAGE1", "Current": "PAGE1"}, "Version": "2.0"}).to_string()).unwrap();
    let data = json!({"UUID": "com.example.e2e.act", "ActionID": "abc", "Name": "Act", "Settings": {"a": 1},
                      "State": 0, "States": [{}, {}]});
    std::fs::write(page_dir.join("manifest.json"), json!({"Controllers": [{"Type": "Keypad", "Actions": {"1,0": data}}]}).to_string()).unwrap();

    let host = PluginHost::start(HostOptions {
        plugins_dir: plugins.clone(),
        plugin_data_dir: tmp.path().join("plugin-data"),
        log_dir: tmp.path().join("logs"),
        profiles_dir: profiles.clone(),
        node: None, // kein Prozess: das Plugin spielt der Test
        publish: false,
        autostart: true,
    }).unwrap();
    host.device_connected(DeviceInfo { serial: "SER1".into(), model: "20GBA9901".into(), cols: 5, rows: 3 });
    let port = host.port();
    assert_eq!(host.status(&installed(Some(&plugins))["com.example.e2e"]), PluginStatus::NodeMissing);

    // Aktion auf der Taste; erstes Zeichnen meldet sie an (willAppear)
    let a = ActionData::new(data.as_object().unwrap().clone(), page_dir.clone());
    let mut b = PluginBehavior::with_host("com.example.e2e.act", host.clone());
    let cfg = Map::new();
    let env = Env::new(&cfg, 0, 1);
    let outbox = env.outbox.clone();
    assert!(b.image(&a, &env, (72, 72)).is_some());

    // Plugin verbindet sich und meldet sich an
    let mut plugin = connect(port, None).unwrap();
    send(&mut plugin, json!({"event": "registerPlugin", "uuid": "com.example.e2e"}));
    let dev = expect(&mut plugin, "deviceDidConnect");
    assert_eq!(dev["deviceInfo"]["size"]["columns"], 5);
    let appear = expect(&mut plugin, "willAppear");
    let id = appear["context"].as_str().unwrap().to_string();
    assert_eq!(appear["action"], "com.example.e2e.act");
    assert_eq!(appear["device"], "SER1");
    assert_eq!(appear["payload"]["coordinates"], json!({"column": 1, "row": 0}));
    assert_eq!(appear["payload"]["settings"], json!({"a": 1}));
    assert_eq!(appear["payload"]["isInMultiAction"], false);

    // Titel, Bild, Zustand und „Fertig“ vom Plugin
    let png = data_encoding::BASE64.encode(&red_png());
    send(&mut plugin, json!({"event": "setTitle", "context": id, "payload": {"title": "Hi"}}));
    send(&mut plugin, json!({"event": "setImage", "context": id, "payload": {"image": format!("data:image/png;base64,{png}")}}));
    send(&mut plugin, json!({"event": "setState", "context": id, "payload": {"state": 1}}));
    send(&mut plugin, json!({"event": "showOk", "context": id}));
    let mut posted = Vec::new();
    wait_for(|| {
        posted.extend(outbox.drain().into_iter().map(|(_, c)| c));
        posted.contains(&Command::SetState(1)) && posted.contains(&Command::Ok)
    });
    assert_eq!(b.title(&a, &env).as_deref(), Some("Hi"));
    assert!(b.tick(&a));
    assert!(!b.tick(&a));
    let img = b.image(&a, &env, (72, 72)).unwrap();
    assert_eq!(img.get_pixel(2, 2).0, [255, 0, 0, 255]);

    // Taste gedrückt -> keyDown beim Plugin; Loslassen wechselt den Zustand
    assert_eq!(b.key_down(&a, &env), Command::None);
    let kd = expect(&mut plugin, "keyDown");
    assert_eq!(kd["context"], id.as_str());
    assert_eq!(b.key_up(&a, &env, 10), Command::SetState(1));
    expect(&mut plugin, "keyUp");

    // Settings vom Plugin landen im Profil; getSettings liefert sie
    send(&mut plugin, json!({"event": "setSettings", "context": id, "payload": {"a": 2}}));
    wait_for(|| {
        let m = freestream_core::profiles::read_json(&page_dir.join("manifest.json")).unwrap();
        m["Controllers"][0]["Actions"]["1,0"]["Settings"]["a"] == 2
    });
    send(&mut plugin, json!({"event": "getSettings", "context": id}));
    assert_eq!(expect(&mut plugin, "didReceiveSettings")["payload"]["settings"], json!({"a": 2}));
    send(&mut plugin, json!({"event": "setGlobalSettings", "context": "com.example.e2e", "payload": {"g": true}}));
    send(&mut plugin, json!({"event": "getGlobalSettings", "context": "com.example.e2e"}));
    assert_eq!(expect(&mut plugin, "didReceiveGlobalSettings")["payload"]["settings"], json!({"g": true}));
    assert_eq!(host.status(&installed(Some(&plugins))["com.example.e2e"]), PluginStatus::NodeMissing);

    // Einstellungsseite: Weiterleitung auf die Seite des Plugins mit demselben Kontext
    let (head, _) = http_get(port, &format!("/pi/{}?profile=TESTPROFILE&page=PAGE1&slot=key%3A1%2C0", host.token()));
    assert!(head.starts_with("HTTP/1.1 302"), "{head}");
    let loc = head.lines().find_map(|l| l.strip_prefix("Location: ")).unwrap().to_string();
    assert!(loc.ends_with(&format!("pi.html?ctx={id}")), "{loc}");
    let (head, body) = http_get(port, &loc);
    assert!(head.starts_with("HTTP/1.1 200"));
    assert!(body.contains("connectElgatoStreamDeckSocket(port,uuid,'registerPropertyInspector'"));
    assert!(body.contains(&format!("uuid=\"{id}\"")));
    assert!(body.contains("<title>E2E – FreeDeck</title>"));
    assert!(http_get(port, "/pi/wrong?profile=x").0.starts_with("HTTP/1.1 403"));
    assert!(http_get(port, &format!("/plugin/{}/com.example.e2e/..%2F..%2Fprofiles", host.token())).0.starts_with("HTTP/1.1 404"));

    // fremde Seiten dürfen sich nicht verbinden
    assert!(connect(port, Some("http://evil.example")).is_err());
    let mut pi = connect(port, Some(&format!("http://127.0.0.1:{port}"))).unwrap();
    send(&mut pi, json!({"event": "registerPropertyInspector", "uuid": id}));
    expect(&mut plugin, "propertyInspectorDidAppear");
    send(&mut pi, json!({"event": "sendToPlugin", "action": "com.example.e2e.act", "context": id, "payload": {"x": 1}}));
    assert_eq!(expect(&mut plugin, "sendToPlugin")["payload"], json!({"x": 1}));
    send(&mut plugin, json!({"event": "sendToPropertyInspector", "context": id, "payload": {"y": 2}}));
    assert_eq!(expect(&mut pi, "sendToPropertyInspector")["payload"], json!({"y": 2}));
    send(&mut pi, json!({"event": "setSettings", "context": id, "payload": {"a": 3}}));
    assert_eq!(expect(&mut plugin, "didReceiveSettings")["payload"]["settings"], json!({"a": 3}));
    send(&mut pi, json!({"event": "getGlobalSettings", "context": id}));
    assert_eq!(expect(&mut pi, "didReceiveGlobalSettings")["payload"]["settings"], json!({"g": true}));
    pi.close(None).unwrap();
    expect(&mut plugin, "propertyInspectorDidDisappear");

    // Seite gewechselt: die Aktion wird verworfen -> willDisappear
    drop(b);
    assert_eq!(expect(&mut plugin, "willDisappear")["context"], id.as_str());
    host.stop();
}

#[test]
fn behavior_without_host_alerts() {
    let tmp = tempfile::tempdir().unwrap();
    let a = ActionData::new(json!({"UUID": "com.example.none.x"}).as_object().unwrap().clone(), tmp.path().to_path_buf());
    let mut b = PluginBehavior::new("com.example.none.x");
    let cfg = Map::new();
    let env = Env::new(&cfg, 0, 1);
    // Host des Prozesses ist in Tests nicht gesetzt
    if host().is_none() {
        assert_eq!(b.key_down(&a, &env), Command::Alert);
    }
    assert!(b.supported());
}
