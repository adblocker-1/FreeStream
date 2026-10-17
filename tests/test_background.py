"""Hintergrundbilder je Seite (Tasten und Touchstreifen)."""
import os
from types import SimpleNamespace

import pytest
from PIL import Image

from freestream import editdoc as E
from freestream import profiles as P
from freestream import render
from freestream.engine import DeckController
from freestream.models import Geometry


@pytest.fixture
def picture(tmp_path):
    """Links rot, rechts blau – so lässt sich prüfen, welche Kachel wohin gehört."""
    im = Image.new("RGB", (400, 200), (255, 0, 0))
    im.paste((0, 0, 255), (200, 0, 400, 200))
    p = tmp_path / "bild.png"
    im.save(p)
    return p


def test_tiles_cover_keys_and_dim(picture):
    tiles = render.background_tiles(picture, 4, 2, (72, 72))
    assert set(tiles) == {(c, r) for c in range(4) for r in range(2)}
    assert tiles[(0, 0)].size == (72, 72)
    assert tiles[(0, 1)].getpixel((10, 10))[:3] == (255, 0, 0)    # linke Hälfte
    assert tiles[(3, 0)].getpixel((60, 10))[:3] == (0, 0, 255)    # rechte Hälfte
    dark = render.background_tiles(picture, 4, 2, (72, 72), dim=50)
    assert dark[(0, 0)].getpixel((10, 10))[0] < 140
    assert render.background_tiles(picture.parent / "fehlt.png", 4, 2, (72, 72)) == {}


def test_key_shows_tile_under_title(picture):
    base = render.background_tiles(picture, 2, 1, (72, 72))[(0, 0)]
    img = render.compose_key((72, 72), None, "Hallo", {"TitleAlignment": "top"}, base=base)
    assert img.getpixel((36, 66))[:3] == (255, 0, 0)  # Hintergrund statt Schwarz


def test_edit_and_load_background(tmp_path, picture):
    root = tmp_path / "profiles"
    root.mkdir()
    doc = E.ProfileDoc(E.create_profile("Bild", "20GBD9901", root))  # Stream Deck +
    pid = doc.page_ids[0]
    doc.set_background(pid, picture, dim=20)
    doc.set_strip_background(pid, picture)
    doc.save()
    path, dim = doc.background(pid)
    assert path.exists() and dim == 20 and doc.strip_background(pid).exists()
    doc.set_background(pid, None, dim=40, keep_image=True)
    assert doc.background(pid) == (path, 40)
    doc.cleanup()  # Bilder bleiben, weil die Seite sie benutzt
    assert path.exists()
    pg = P.load_page(pid, doc.page(pid).dir)
    assert pg.background.startswith("Images/Background") and pg.background_dim == 40
    assert pg.encoder_background.startswith("Images/Strip")
    # Vorschau im Editor nutzt die Kachel
    tile = render.background_tiles(pg.image_path(pg.background), 4, 2, (72, 72), 40)[(1, 1)]
    img = E.preview_action(doc, pid, {"UUID": "com.elgato.streamdeck.system.text", "States": [{"Title": "x"}]},
                           base=tile)
    assert img.size == (72, 72)
    doc.set_background(pid, None)
    assert doc.background(pid) == (None, 0)


def test_engine_tiles_for_empty_and_used_keys(tmp_path, picture):
    root = tmp_path / "profiles"
    root.mkdir()
    doc = E.ProfileDoc(E.create_profile("Bild", "20GBA9901", root))  # 5 x 3
    pid = doc.page_ids[0]
    doc.set_background(pid, picture, dim=0)
    doc.save()
    pg = P.load_page(pid, doc.page(pid).dir)
    fake = SimpleNamespace(current_page=lambda: pg, geometry=Geometry(5, 3), visual=True, key_size=(72, 72),
                           keys={}, _overlay_for=lambda a: None)
    fake._bg_tile = lambda k: DeckController._bg_tile(fake, k)
    img, _ = DeckController.key_image(fake, 0)
    assert img.getpixel((5, 5))[:3] == (255, 0, 0)
    img, _ = DeckController.key_image(fake, 4)   # rechts oben
    assert img.getpixel((60, 5))[:3] == (0, 0, 255)
    assert os.path.exists(pg.image_path(pg.background))
