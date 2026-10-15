"""Translations (gettext-style .po files in lindeck/locale, no build step).

The source language in the code is English. A translation is a plain
``<code>.po`` file (e.g. ``de.po``, ``pt_BR.po``) next to ``lindeck.pot``;
it can be edited with Poedit, Weblate or any text editor.

Usage::

    from .i18n import _
    label = _("Add channel")
    text = _("{n} plugins found").format(n=count)

The language is chosen once at start:
``LINDECK_LANG`` > ``"language"`` in config.json > system locale
(``LANGUAGE``, ``LC_ALL``, ``LC_MESSAGES``, ``LANG``) > English.
"""
import json
import os
import re
from pathlib import Path

from . import paths

LOCALE_DIR = Path(__file__).resolve().parent / "locale"
SOURCE = "en"

# Shown in the language menus (always in the language itself)
NAMES = {
    "en": "English",
    "de": "Deutsch",
    "es": "Español",
    "fr": "Français",
    "pt_BR": "Português (Brasil)",
    "it": "Italiano",
    "pl": "Polski",
    "ru": "Русский",
    "uk": "Українська",
    "nl": "Nederlands",
    "ja": "日本語",
    "zh_CN": "简体中文",
}

_ESC = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "r": "\r"}

_catalog: dict = {}
_lang = SOURCE


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) < 2 or s[0] != '"' or s[-1] != '"':
        raise ValueError(f"not a quoted string: {s!r}")
    return re.sub(r'\\(.)', lambda m: _ESC.get(m.group(1), m.group(1)), s[1:-1])


def parse_po(text: str) -> list:
    """Return the entries of a .po file as dicts.

    Keys: ``msgctxt``, ``msgid``, ``msgid_plural``, ``msgstr`` (list),
    ``flags`` (set), ``comments`` (list of raw comment lines).
    """
    entries, cur, field = [], None, None

    def start_new():
        # a comment, msgctxt or msgid after a msgstr begins the next entry
        nonlocal cur
        if cur is None or cur["msgstr"]:
            if cur is not None and "msgid" in cur:
                entries.append(cur)
            cur = {"msgstr": [], "flags": set(), "comments": []}

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            start_new()
            if line.startswith("#,"):
                cur["flags"].update(f.strip() for f in line[2:].split(","))
            else:
                cur["comments"].append(raw)
            field = None
            continue
        m = re.match(r'(msgctxt|msgid_plural|msgid|msgstr(?:\[(\d+)\])?)\s+(".*")$', line)
        if m:
            key, idx, val = m.group(1), m.group(2), _unquote(m.group(3))
            if key in ("msgctxt", "msgid"):
                start_new()
            if cur is None:
                raise ValueError(f"msgstr without msgid: {raw!r}")
            if key.startswith("msgstr"):
                i = int(idx) if idx is not None else 0
                while len(cur["msgstr"]) <= i:
                    cur["msgstr"].append("")
                cur["msgstr"][i] = val
                field = ("msgstr", i)
            else:
                cur[key] = val
                field = key
            continue
        if line.startswith('"') and cur is not None and field:
            val = _unquote(line)
            if isinstance(field, tuple):
                cur["msgstr"][field[1]] += val
            else:
                cur[field] += val
            continue
        raise ValueError(f"cannot parse .po line: {raw!r}")
    if cur is not None and "msgid" in cur:
        entries.append(cur)
    return entries


def _key(msgid: str, ctx=None) -> str:
    return f"{ctx}\x04{msgid}" if ctx else msgid


def load_catalog(lang: str) -> dict:
    """Translations of one language as {msgid (or ctx\\x04msgid): msgstr list}."""
    f = LOCALE_DIR / f"{lang}.po"
    if lang == SOURCE:
        return {}
    out = {}
    for e in parse_po(f.read_text(encoding="utf-8")) if f.exists() else []:
        if not e["msgid"] or "fuzzy" in e["flags"]:
            continue
        strs = [s for s in e["msgstr"]]
        if strs and all(strs):
            out[_key(e["msgid"], e.get("msgctxt"))] = strs
    # Development: extra JSON fragments {"msgid" or "ctx\x04msgid": "translation"}
    for extra in filter(None, os.environ.get("LINDECK_I18N_EXTRA", "").split(os.pathsep)):
        try:
            for k, v in json.loads(Path(extra).read_text(encoding="utf-8")).items():
                out[k] = v if isinstance(v, list) else [v]
        except (OSError, ValueError):
            pass
    return out


def available() -> list:
    """Language codes with a translation (plus English), English first."""
    langs = {SOURCE} | {p.stem for p in LOCALE_DIR.glob("*.po")}
    rest = sorted(langs - {SOURCE}, key=lambda c: NAMES.get(c, c).lower())
    return [SOURCE] + rest


def name(lang: str) -> str:
    return NAMES.get(lang, lang)


def _match(code: str):
    """Map a locale like 'pt_BR.UTF-8' or 'de-AT' to an available language."""
    code = (code or "").split(".")[0].split("@")[0].replace("-", "_")
    if not code or code in ("C", "POSIX"):
        return None
    have = available()
    if code in have:
        return code
    base = code.split("_")[0]
    if base in have:
        return base
    for c in have:  # 'pt' -> 'pt_BR'
        if c.split("_")[0] == base:
            return c
    return None


def configured() -> str:
    """The "language" setting from config.json ("auto" if unset)."""
    try:
        data = json.loads(paths.CONFIG_FILE.read_text(encoding="utf-8"))
        return str(data.get("language") or "auto")
    except (OSError, ValueError):
        return "auto"


def detect() -> str:
    env = os.environ.get("LINDECK_LANG")
    if env:
        return _match(env) or SOURCE
    cfg = configured()
    if cfg != "auto":
        found = _match(cfg)
        if found:
            return found
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var)
        if not val:
            continue
        for part in val.split(":"):
            found = _match(part)
            if found:
                return found
        if var != "LANGUAGE":
            break
    return SOURCE


def set_language(lang: str) -> str:
    """Switch the active language (for new strings only); returns it."""
    global _catalog, _lang
    _lang = _match(lang) or SOURCE
    _catalog = load_catalog(_lang)
    return _lang


def current() -> str:
    return _lang


def _(msgid: str) -> str:
    t = _catalog.get(msgid)
    return t[0] if t else msgid


def pgettext(ctx: str, msgid: str) -> str:
    """Like _() with a context, for words that translate differently by place."""
    t = _catalog.get(_key(msgid, ctx))
    return t[0] if t else msgid


def ngettext(singular: str, plural: str, n: int) -> str:
    t = _catalog.get(singular)
    if t and len(t) > 1:
        return t[_plural_index(n, len(t))]
    return singular if n == 1 else plural


def _plural_index(n: int, forms: int) -> int:
    if _lang in ("fr", "pt_BR"):
        i = 0 if n in (0, 1) else 1
    elif _lang in ("ru", "uk"):
        i = 0 if n % 10 == 1 and n % 100 != 11 else (
            1 if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else 2)
    elif _lang == "pl":
        i = 0 if n == 1 else (1 if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else 2)
    elif _lang in ("ja", "zh_CN"):
        i = 0
    else:
        i = 0 if n == 1 else 1
    return min(i, forms - 1)


def N_(msgid: str) -> str:
    """Mark a string for translation without translating it yet."""
    return msgid


set_language(detect())
