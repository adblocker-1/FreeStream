//! Zahlen wie Python formatieren (`{x:g}`), damit erzeugte PipeWire-Konfigurationen gleich aussehen.

/// Wie Pythons `format(x, "g")`: 6 gültige Stellen, ohne überflüssige Nullen.
pub fn g(x: f64) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf".into() } else { "-inf".into() };
    }
    if x == 0.0 {
        return if x.is_sign_negative() { "-0".into() } else { "0".into() };
    }
    const P: i32 = 6;
    // auf P gültige Stellen runden und den Exponenten danach bestimmen
    let sci = format!("{:.*e}", (P - 1) as usize, x);
    let (mant, exp) = sci.split_once('e').unwrap_or((&sci, "0"));
    let exp: i32 = exp.parse().unwrap_or(0);
    if (-4..P).contains(&exp) {
        let decimals = (P - 1 - exp).max(0) as usize;
        strip(&format!("{x:.decimals$}"))
    } else {
        let m = strip(mant);
        format!("{m}e{}{:02}", if exp < 0 { '-' } else { '+' }, exp.abs())
    }
}

fn strip(s: &str) -> String {
    if s.contains('.') {
        s.trim_end_matches('0').trim_end_matches('.').to_string()
    } else {
        s.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::g;

    #[test]
    fn like_python() {
        for (x, want) in [(90.0, "90"), (0.707, "0.707"), (-45.0, "-45"), (12000.0, "12000"), (1e6, "1e+06"),
                          (0.0001, "0.0001"), (0.00001, "1e-05"), (123456789.0, "1.23457e+08"), (0.1 + 0.2, "0.3"),
                          (999999.5, "1e+06"), (2.5, "2.5"), (-1.0, "-1"), (0.08, "0.08")] {
            assert_eq!(g(x), want, "{x}");
        }
    }
}
