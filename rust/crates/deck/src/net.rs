//! Gemeinsame Netzwerk-Helfer der Verbindungs-Aktionen (Home Assistant, Discord): HTTP(S) mit den
//! Zertifikaten des Systems, URL- und Formular-Kodierung, gemeinsamer Zähler für das Neuzeichnen.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, OnceLock};
use std::time::Duration;

/// Zertifikate des Systems (einmal geladen; ohne lesbare Systemzertifikate die von Mozilla).
fn root_certs() -> ureq::tls::RootCerts {
    static ROOTS: OnceLock<Option<Arc<Vec<ureq::tls::Certificate<'static>>>>> = OnceLock::new();
    let certs = ROOTS.get_or_init(|| {
        let res = rustls_native_certs::load_native_certs();
        for e in &res.errors {
            log::debug!("Systemzertifikate: {e}");
        }
        let certs: Vec<_> = res.certs.iter().map(|c| ureq::tls::Certificate::from_der(c.as_ref()).to_owned()).collect();
        (!certs.is_empty()).then(|| Arc::new(certs))
    });
    match certs {
        Some(c) => ureq::tls::RootCerts::Specific(c.clone()),
        None => ureq::tls::RootCerts::WebPki,
    }
}

/// HTTP-Client: Zeitlimit für die ganze Anfrage, Fehlercodes als Antwort (nicht als Fehler);
/// `verify = false` prüft das Zertifikat nicht (selbst signiert im Heimnetz, wie `verify_ssl` in Python).
pub fn agent(timeout: Duration, verify: bool) -> ureq::Agent {
    let tls = ureq::tls::TlsConfig::builder().root_certs(root_certs()).disable_verification(!verify).build();
    ureq::Agent::config_builder()
        .timeout_global(Some(timeout))
        .http_status_as_error(false)
        .user_agent("FreeDeck")
        .tls_config(tls)
        .build()
        .into()
}

/// Wie `urllib.parse.quote` (sicher: Buchstaben, Ziffern, `_.-~` und `/`).
pub fn quote(s: &str) -> String {
    quote_with(s, b"/")
}

/// Wie `urllib.parse.quote_plus` für Formulardaten (Leerzeichen als `+`).
pub fn quote_plus(s: &str) -> String {
    s.split(' ').map(|p| quote_with(p, b"")).collect::<Vec<_>>().join("+")
}

fn quote_with(s: &str, safe: &[u8]) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || b"_.-~".contains(&b) || safe.contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// Formulardaten (application/x-www-form-urlencoded) wie `urllib.parse.urlencode`.
pub fn urlencode(fields: &[(&str, &str)]) -> String {
    fields.iter().map(|(k, v)| format!("{}={}", quote_plus(k), quote_plus(v))).collect::<Vec<_>>().join("&")
}

/// Versionszähler: steigt bei jeder Änderung, die Tasten vergleichen ihn in `tick` (Neuzeichnen).
#[derive(Default, Debug)]
pub struct Version(AtomicU64);

impl Version {
    pub fn bump(&self) {
        self.0.fetch_add(1, Ordering::Relaxed);
    }
    pub fn get(&self) -> u64 {
        self.0.load(Ordering::Relaxed)
    }
}

/// Text auf `n` Zeichen kürzen („Szene 12…“) wie `_fit` in `freestream/native.py`.
pub fn fit(text: &str, n: usize) -> String {
    if text.chars().count() <= n {
        return text.to_string();
    }
    let mut s: String = text.chars().take(n.saturating_sub(1)).collect();
    s.push('…');
    s
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn quoting_like_urllib() {
        assert_eq!(quote("light.wohnzimmer"), "light.wohnzimmer");
        assert_eq!(quote("a b/ä"), "a%20b/%C3%A4");
        assert_eq!(urlencode(&[("grant_type", "authorization_code"), ("redirect_uri", "http://localhost")]),
                   "grant_type=authorization_code&redirect_uri=http%3A%2F%2Flocalhost");
        assert_eq!(quote_plus("a b+c"), "a+b%2Bc");
    }

    #[test]
    fn fit_shortens() {
        assert_eq!(fit("Szene", 9), "Szene");
        assert_eq!(fit("Hauptszene mit Kamera", 9), "Hauptsze…");
    }
}
