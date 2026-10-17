"""Deckkraft der Tasten: Hintergrundbild der Seite scheint durch – im Editor und beim FreeWave-Export."""
import pytest
from PIL import Image

from freestream import deckexport, mixkeys, render
from freestream import editdoc as E


def _bg(path, color=(200, 40, 40)):
    Image.new("RGB", (400, 200), color).save(path)
    return path


def test_icon_opacity_lets_background_through():
    base = Image.new("RGBA", (72, 72), (200, 40, 40, 255))
    icon = Image.new("RGBA", (72, 72), (0, 0, 255, 255))
    full = render.compose_key((72, 72), icon, "", {}, base=base)
    assert full.getpixel((5, 5))[:3] == (0, 0, 255)
    clear = render.compose_key((72, 72), icon, "", {"IconOpacity": 0}, base=base)
    assert clear.getpixel((5, 5))[:3] == (200, 40, 40)
    half = render.compose_key((72, 72), icon, "", {"IconOpacity": 50}, base=base)
    r, g, b = half.getpixel((5, 5))[:3]
    assert 90 < r < 110 and 135 < b < 160  # halb Blau, halb Rot (40 Blau im Hintergrund)
    assert render.with_opacity(icon, "kaputt") is icon


def test_freewave_keys_keep_symbol_only_background_clears():
    """FreeWave-Tasten: nur ihr dunkler Hintergrund wird durchsichtig, Symbol und Text bleiben."""
    img = mixkeys.tile((72, 72), "music", "#22c55e", 70, False, "both", bg_alpha=mixkeys.bg_alpha({"IconOpacity": 0}))
    assert img.getpixel((1, 1))[3] == 0                 # Ecke: durchsichtig
    assert img.getpixel((36, 22))[3] == 255             # Kachel mit Symbol: deckend
    assert img.info.get("own_opacity")
    base = Image.new("RGBA", (72, 72), (200, 40, 40, 255))
    out = render.compose_key((72, 72), img, "", {"IconOpacity": 0}, base=base)
    assert out.getpixel((1, 1))[:3] == (200, 40, 40) and out.getpixel((36, 22))[:3] != (200, 40, 40)
    assert mixkeys.bg_alpha({}) == 255 and mixkeys.bg_alpha({"IconOpacity": "x"}) == 255


def test_export_transparent_with_background_survives_reexport(tmp_path, monkeypatch):
    monkeypatch.setattr(deckexport, "notify_freedeck", lambda: False)
    store = tmp_path / "profiles"
    store.mkdir()
    chans = ["system", "music", "browser", "voice", "game", "sfx", "mic", "aux1", "aux2"]
    deckexport.export("20GBA9901", chans, store_root=store, opacity=0, background=_bg(tmp_path / "bg.png"), dim=10)

    def check(opacity=0):
        p = deckexport.managed_profile("20GBA9901", store)
        doc = E.ProfileDoc(p.root)
        assert len(doc.page_ids) > 1
        for page in doc.page_ids:
            path, dim = doc.background(page)
            assert path is not None and dim == 10
            for slot, data in doc.page(page).items():
                assert all(st.get("IconOpacity", 100) == opacity for st in data.get("States") or [{}])
        return p

    check()
    lk = deckexport.look("20GBA9901", store)
    assert lk["opacity"] == 0 and lk["background"] is not None and lk["dim"] == 10
    # neuer Kanal: FreeWave belegt neu – Aussehen bleibt
    assert deckexport.sync(chans + ["aux3"], store_root=store)
    check()
    # Hintergrund entfernen, Tasten wieder deckend
    deckexport.export("20GBA9901", chans, store_root=store, opacity=100, background=None)
    doc = E.ProfileDoc(deckexport.managed_profile("20GBA9901", store).root)
    assert all(doc.background(pg)[0] is None for pg in doc.page_ids)
    assert deckexport.look("20GBA9901", store)["background"] is None


def test_export_keeps_background_set_in_freedeck(tmp_path, monkeypatch):
    """Hintergrund im FreeDeck-Fenster auf das FreeWave-Profil gelegt: das nächste Neu-Belegen behält ihn."""
    monkeypatch.setattr(deckexport, "notify_freedeck", lambda: False)
    store = tmp_path / "profiles"
    store.mkdir()
    deckexport.export("20GBA9901", ["system", "music"], store_root=store)
    p = deckexport.managed_profile("20GBA9901", store)
    doc = E.ProfileDoc(p.root)
    doc.set_background(doc.page_ids[0], _bg(tmp_path / "mine.png", (10, 200, 10)), dim=40)
    doc.save()
    deckexport.export("20GBA9901", ["system", "music", "browser"], store_root=store)
    doc = E.ProfileDoc(deckexport.managed_profile("20GBA9901", store).root)
    path, dim = doc.background(doc.page_ids[0])
    assert path is not None and dim == 40
    with Image.open(path) as im:
        assert im.convert("RGB").getpixel((5, 5)) == (10, 200, 10)


@pytest.mark.parametrize("deck_type", ["Stream Deck XL", "Stream Deck +"])
def test_idle_image_instead_of_logo(tmp_path, monkeypatch, deck_type):
    """Beim Beenden: gewähltes Bild über alle Tasten (und den Touchstreifen) statt Zurücksetzen (Elgato-Logo)."""
    pytest.importorskip("StreamDeck")
    import StreamDeck.Transport.Dummy as dummy
    from StreamDeck.DeviceManager import DeviceManager

    from freestream import paths
    from freestream.config import Config, State
    from freestream.engine import DeckController
    from freestream.profiles import ProfileStore
    monkeypatch.setattr(dummy.Dummy.Device, "read", lambda self, length: None)
    monkeypatch.setattr(paths, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(paths, "STATE_FILE", tmp_path / "state.json")
    (tmp_path / "p").mkdir()
    deck = next(d for d in DeviceManager(transport="dummy").enumerate() if d.deck_type() == deck_type)
    deck.open()
    st = State()
    st.save = lambda: None
    ctrl = DeckController(deck, ProfileStore(tmp_path / "p"), Config(), st, None, None)
    calls = []
    real_close = deck.close
    deck.reset = lambda: calls.append("reset")
    deck.set_key_image = lambda k, img: calls.append(("key", k))
    deck.set_touchscreen_image = lambda *a: calls.append("touch")
    deck.close = lambda: calls.append("close")
    assert not ctrl.show_idle_image()  # nichts gewählt
    ctrl.cfg.device(ctrl.serial)["idle_image"] = str(_bg(tmp_path / "idle.png"))
    ctrl.stop()
    assert "reset" not in calls and calls[-1] == "close"
    assert {c[1] for c in calls if isinstance(c, tuple)} == set(range(ctrl.key_count))
    assert ("touch" in calls) == ctrl.touch
    # Bild weg: wie bisher zurücksetzen
    calls.clear()
    ctrl.cfg.device(ctrl.serial)["idle_image"] = str(tmp_path / "gibtsnicht.png")
    ctrl.stop()
    assert calls == ["reset", "close"]
    real_close()  # Lese-Thread der Bibliothek beenden


import test_editor  # noqa: E402
import test_settings_tabs  # noqa: E402

store, doc, app = test_editor.store, test_editor.doc, test_editor.app  # Fixtures
settings = test_settings_tabs.settings


@pytest.fixture
def own_paths(tmp_path, monkeypatch):
    from freestream import paths
    monkeypatch.setattr(paths, "CONFIG_FILE", tmp_path / "config.json")


@test_settings_tabs.needs_x
def test_settings_idle_image(own_paths, settings, tmp_path, monkeypatch):
    from pathlib import Path

    from freestream import paths
    monkeypatch.setattr(paths, "CONFIG_DIR", tmp_path / "cfg")  # Bild nicht ins echte ~/.config
    dlg, ctrl, cfg = settings
    ctrl.idle_image_path = lambda: (Path(cfg.device(ctrl.serial)["idle_image"])
                                    if cfg.device(ctrl.serial).get("idle_image") else None)
    dlg.show_tab("devices")
    page = dlg.pages["devices"]
    assert page.idle_preview.cget("text") == "Geräte-Logo" and not page.idle_remove.enabled
    page._set_idle(str(_bg(tmp_path / "mein.jpg")))
    stored = Path(cfg.device(ctrl.serial)["idle_image"])
    assert stored == paths.CONFIG_DIR / "idle" / f"{ctrl.serial}.png" and stored.exists()
    assert page.idle_remove.enabled and page.idle_preview.cget("image")
    test_settings_tabs._shot(dlg, "settings-idle")
    page._set_idle(None)
    assert "idle_image" not in cfg.device(ctrl.serial) and not stored.exists()
