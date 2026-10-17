#!/usr/bin/env python3
"""Translation helper for FreeDeck/FreeWave.

    tools/i18n.py extract            write freestream/locale/freestream.pot from the code
    tools/i18n.py update             add new strings to every .po (empty), drop unused ones
    tools/i18n.py check [LANG ...]   missing translations and placeholder mismatches
    tools/i18n.py merge LANG FILE..  copy translations from .po fragments into LANG.po
    tools/i18n.py german [FILE ...]  string literals that still look German
    tools/i18n.py check-fragment FRAG FILE..  FRAG (.po/.json) covers all strings of FILE..

New language: ``cp freestream/locale/freestream.pot freestream/locale/<code>.po``,
fill in the msgstr lines, run ``tools/i18n.py check <code>``.
"""
import ast
import re
import string
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from freestream.i18n import LOCALE_DIR, parse_po  # noqa: E402

SRC = ROOT / "freestream"
POT = LOCALE_DIR / "freestream.pot"
FUNCS = {"_": ("msgid",), "N_": ("msgid",), "pgettext": ("msgctxt", "msgid"),
         "ngettext": ("msgid", "msgid_plural")}


def _quote(s: str) -> str:
    s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t").replace("\r", "\\r")
    if "\n" in s and s != "\n":
        parts = s.split("\n")
        lines = [p + "\\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
        return '""\n' + "\n".join(f'"{x}"' for x in lines)
    return f'"{s.replace(chr(10), chr(92) + "n")}"'


def extract() -> list:
    """All translatable strings as entries (in order of first use)."""
    found = {}
    for f in sorted(SRC.rglob("*.py")):
        rel = f.relative_to(ROOT).as_posix()
        tree = ast.parse(f.read_text(encoding="utf-8"), rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func.id if isinstance(node.func, ast.Name) else (
                node.func.attr if isinstance(node.func, ast.Attribute) else None)
            if fn not in FUNCS:
                continue
            names = FUNCS[fn]
            args = node.args[:len(names)]
            if len(args) < len(names) or not all(
                    isinstance(a, ast.Constant) and isinstance(a.value, str) for a in args):
                if fn != "N_" or args:
                    print(f"{rel}:{node.lineno}: {fn}() needs literal strings", file=sys.stderr)
                continue
            e = dict(zip(names, (a.value for a in args)))
            key = (e.get("msgctxt"), e["msgid"])
            ent = found.setdefault(key, {**e, "refs": []})
            ent["refs"].append(f"{rel}:{node.lineno}")
    return list(found.values())


def _fields(e: dict) -> list:
    """Placeholders like {name} / {0} in a string."""
    out = []
    for s in [e["msgid"]] + ([e["msgid_plural"]] if e.get("msgid_plural") else []):
        try:
            out += [f for _, f, _, _ in string.Formatter().parse(s) if f is not None]
        except ValueError:
            pass
    return sorted(set(out))


def write_po(path: Path, entries: list, header: str, trans=None) -> None:
    trans = trans or {}
    out = [header.rstrip() + "\n"]
    for e in entries:
        lines = []
        for c in e.get("comments", []):
            if c.startswith("#.") or c.startswith("# "):
                lines.append(c)
        if e.get("refs"):
            lines.append("#: " + " ".join(e["refs"][:4]))
        flags = set(e.get("flags", ())) - {""}
        if any(f for f in _fields(e)):
            flags.add("python-brace-format")
        if flags:
            lines.append("#, " + ", ".join(sorted(flags)))
        if e.get("msgctxt"):
            lines.append("msgctxt " + _quote(e["msgctxt"]))
        lines.append("msgid " + _quote(e["msgid"]))
        key = (e.get("msgctxt"), e["msgid"])
        strs = trans.get(key) or []
        if e.get("msgid_plural"):
            lines.append("msgid_plural " + _quote(e["msgid_plural"]))
            for i in range(max(2, len(strs))):
                lines.append(f"msgstr[{i}] " + _quote(strs[i] if i < len(strs) else ""))
        else:
            lines.append("msgstr " + _quote(strs[0] if strs else ""))
        out.append("\n".join(lines) + "\n")
    path.write_text("\n".join(out), encoding="utf-8")


POT_HEADER = '''# FreeDeck / FreeWave translation template.
# Copy to <language>.po (e.g. it.po, pt_BR.po) and fill in the msgstr lines.
msgid ""
msgstr ""
"Project-Id-Version: freestream\\n"
"Content-Type: text/plain; charset=UTF-8\\n"
"Content-Transfer-Encoding: 8bit\\n"
"Language: \\n"
'''


def _header(lang: str) -> str:
    return POT_HEADER.replace("# Copy to <language>.po (e.g. it.po, pt_BR.po) and fill in the msgstr lines.\n",
                              "").replace('"Language: \\n"', f'"Language: {lang}\\n"')


def _load(path: Path) -> dict:
    out = {}
    if path.suffix == ".json":
        import json
        for k, v in json.loads(path.read_text(encoding="utf-8")).items():
            ctx, _, mid = k.rpartition("\x04")
            out[(ctx or None, mid)] = {"msgid": mid, "msgstr": v if isinstance(v, list) else [v], "flags": set()}
        return out
    if path.exists():
        for e in parse_po(path.read_text(encoding="utf-8")):
            if e["msgid"]:
                out[(e.get("msgctxt"), e["msgid"])] = e
    return out


def cmd_extract() -> list:
    ents = extract()
    LOCALE_DIR.mkdir(exist_ok=True)
    write_po(POT, ents, POT_HEADER)
    print(f"{POT.relative_to(ROOT)}: {len(ents)} strings")
    return ents


def cmd_update() -> None:
    ents = cmd_extract()
    for po in sorted(LOCALE_DIR.glob("*.po")):
        old = _load(po)
        trans = {k: [s for s in e["msgstr"]] for k, e in old.items() if "fuzzy" not in e["flags"]}
        ents_po = []
        for e in ents:  # keep translator comments
            o = old.get((e.get("msgctxt"), e["msgid"]))
            ents_po.append({**e, "comments": [c for c in o["comments"] if c.startswith("# ")] if o else []})
        write_po(po, ents_po, _header(po.stem), trans)
        gone = len(set(old) - {(e.get("msgctxt"), e["msgid"]) for e in ents})
        print(f"{po.relative_to(ROOT)} updated" + (f", {gone} unused dropped" if gone else ""))


def check(langs=None, verbose=True) -> int:
    ents = extract()
    problems = 0
    for lang in langs or sorted(p.stem for p in LOCALE_DIR.glob("*.po")):
        po = _load(LOCALE_DIR / f"{lang}.po")
        missing = []
        for e in ents:
            t = po.get((e.get("msgctxt"), e["msgid"]))
            if not t or not t["msgstr"] or not all(t["msgstr"]) or "fuzzy" in t["flags"]:
                missing.append(e)
                continue
            want = _fields(e)
            for s in t["msgstr"]:
                got = sorted(set(f for _, f, _, _ in string.Formatter().parse(s) if f is not None)) \
                    if _safe_fmt(s) else None
                if got is None or (want and set(got) - set(want)) or (not e.get("msgid_plural") and got != want):
                    problems += 1
                    if verbose:
                        print(f"{lang}: placeholders differ: {e['msgid']!r} -> {s!r}")
        problems += len(missing)
        if verbose:
            for e in missing:
                print(f"{lang}: missing: {e['msgid']!r}  ({e['refs'][0]})")
            print(f"{lang}: {len(ents) - len(missing)}/{len(ents)} translated")
    return problems


def cmd_check_fragment(frag: str, files: list) -> int:
    """Check that a fragment (.po or .json) translates every string used in FILES."""
    files = {Path(f).resolve() for f in files}
    tr = _load(Path(frag))
    bad = 0
    for e in extract():
        if not any((ROOT / r.rsplit(":", 1)[0]).resolve() in files for r in e["refs"]):
            continue
        t = tr.get((e.get("msgctxt"), e["msgid"]))
        if not t or not all(t["msgstr"]):
            bad += 1
            print(f"missing: {e['msgid']!r}  ({e['refs'][0]})")
            continue
        want = set(_fields(e))
        for s in t["msgstr"]:
            got = set(f for _, f, _, _ in string.Formatter().parse(s) if f is not None) if _safe_fmt(s) else None
            if got is None or got - want or (not e.get("msgid_plural") and got != want):
                bad += 1
                print(f"placeholders differ: {e['msgid']!r} -> {s!r}")
    print(f"{bad} problems")
    return bad


def _safe_fmt(s: str) -> bool:
    try:
        list(string.Formatter().parse(s))
        return True
    except ValueError:
        return False


def cmd_merge(lang: str, files: list) -> None:
    target = LOCALE_DIR / f"{lang}.po"
    have = {k: e["msgstr"] for k, e in _load(target).items() if all(e["msgstr"])}
    conflicts = 0
    for f in files:
        for k, e in _load(Path(f)).items():
            if not e["msgstr"] or not all(e["msgstr"]):
                continue
            if k in have and have[k] != e["msgstr"]:
                conflicts += 1
                print(f"conflict {k[1]!r}: {have[k]!r} (kept) vs {e['msgstr']!r} ({f})")
                continue
            have[k] = e["msgstr"]
    write_po(target, extract(), _header(lang), have)
    print(f"{target.relative_to(ROOT)}: {len(have)} translations, {conflicts} conflicts")


GERMAN = re.compile(r"[äöüÄÖÜß]|\b(?:und|nicht|oder|für|mit|eine?[nmr]?|wird|wurde|kein|keine|Datei|"
                    r"Fehler|Gerät|Taste|Seite|Profil|Einstellungen|Abbrechen|Schließen|Speichern|"
                    r"Hinzufügen|Löschen|Bitte|Kanal|Lautstärke|läuft|fehlt|gefunden|Ordner)\b")


def cmd_german(files: list) -> int:
    """Report string literals that look German and are not docstrings."""
    hits = 0
    for f in files or sorted(SRC.rglob("*.py")):
        f = Path(f)
        tree = ast.parse(f.read_text(encoding="utf-8"))
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                b = node.body
                if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant):
                    docs.add(id(b[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                if GERMAN.search(node.value):
                    hits += 1
                    print(f"{f}:{node.lineno}: {node.value[:90]!r}")
    return hits


def main(argv) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "extract":
        cmd_extract()
    elif cmd == "update":
        cmd_update()
    elif cmd == "check":
        return 1 if check(rest or None) else 0
    elif cmd == "merge":
        cmd_merge(rest[0], rest[1:])
    elif cmd == "check-fragment":
        return 1 if cmd_check_fragment(rest[0], rest[1:]) else 0
    elif cmd == "german":
        return 1 if cmd_german(rest) else 0
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
