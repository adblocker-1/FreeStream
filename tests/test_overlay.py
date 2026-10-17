"""Anzeigen auf den Tasten (erledigt, Fehler, Fortschritt, kein Profil) und Geräte-Vorschau im Fenster."""
from PIL import Image

from streamlin import render
from streamlin.models import Geometry


def _differs(a, b):
    return any(p != q for p, q in zip(a.getdata(), b.getdata()))


def test_overlay_hides_title():
    base = Image.new("RGBA", (72, 72), (200, 40, 40, 255))
    plain = render.compose_key((72, 72), None, "", {}, overlay="ok", base=base.copy())
    titled = render.compose_key((72, 72), None, "LongTitle", {}, overlay="ok", base=base.copy())
    assert not _differs(plain, titled)  # der Titel steht während der Anzeige nicht auf der Taste
    assert _differs(plain, render.compose_key((72, 72), None, "", {}, overlay="alert", base=base.copy()))


def test_overlay_colors():
    ok = render.compose_key((96, 96), None, "", {}, overlay="ok")
    alert = render.compose_key((96, 96), None, "", {}, overlay="alert")
    # Kachel oben mittig: grün bzw. rot
    r, g, b, _a = ok.getpixel((48, 22))
    assert g > r and g > b
    r, g, b, _a = alert.getpixel((48, 22))
    assert r > g and r > b


def test_alert_on_wide_segment():
    seg = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    render._alert(seg)
    assert seg.size == (200, 100) and seg.getpixel((100, 22))[0] > 200


def test_no_profile_key():
    img = render.no_profile_key((72, 72))
    assert img.size == (72, 72) and img.getpixel((36, 20))[2] > 200  # blaue Kachel


def test_stage_layout_kinds():
    from streamlin.editor import _layout, _stage_kind
    assert _stage_kind("20GBF9901", Geometry(3, 1)) == "pedal"
    assert _stage_kind("20GBO9901", Geometry(16, 2, dials=2)) == "studio"
    assert _stage_kind("20GBJ9901", Geometry(4, 2, touch_keys=2)) == "neo"
    assert _stage_kind("20GBD9901", Geometry(4, 2, dials=4)) == "plus"
    assert _stage_kind("20GBA9901", Geometry(5, 3)) == "plain"
    for code, g in (("20GBD9901", Geometry(4, 2, dials=4)), ("20GBX9901", Geometry(9, 4, dials=6)),
                    ("20GBJ9901", Geometry(4, 2, touch_keys=2)), ("20GBO9901", Geometry(16, 2, dials=2)),
                    ("20GBF9901", Geometry(3, 1)), ("20GAT9901", Geometry(8, 4))):
        L = _layout(g, 660, 360, 18, _stage_kind(code, g))
        assert L["w"] + 36 <= 660 and L["h"] + 36 <= 360, code
