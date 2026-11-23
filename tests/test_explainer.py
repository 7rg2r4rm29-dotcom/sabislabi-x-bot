"""Vidéos explicatives animées (bot/explainer.py) : aucun réseau ; le vrai ffmpeg assemble de
petites vidéos fabriquées sur place (sauté s'il est absent)."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest

from bot import explainer as ex
from bot import visuals
from bot.models import Draft, Visual

PLAN = {
    "title": "How does a car know you're not watching the road?",
    "scenes": [
        {"kind": "fact", "text": "A new update can tell when your eyes leave the road, even behind sunglasses.",
         "big": "SUNGLASSES", "items": [], "left": "", "right": "", "who": ""},
        {"kind": "steps", "text": "How it works", "items": ["A small camera watches the driver",
                                                            "It checks where your eyes point",
                                                            "Look away too long: the car warns you"],
         "big": "", "left": "", "right": "", "who": ""},
        {"kind": "compare", "text": "What changes", "left": "Before: dark glasses could fool it",
         "right": "Now: it still sees where you look", "big": "", "items": [], "who": ""},
        {"kind": "quote", "text": "It works even when the driver wears sunglasses.", "who": "Tesla",
         "big": "", "items": [], "left": "", "right": ""},
    ],
    "ending": "Your car is learning to watch you. Would you trust it?",
}

SMALL = (270, 338)          # même mise en page, en petit : les tests restent rapides
needs_ffmpeg = pytest.mark.skipif(visuals.ffmpeg_exe() is None, reason="ffmpeg absent")


def plan(**over) -> dict:
    p = copy.deepcopy(PLAN)
    p.update(over)
    return p


# ---------------------------------------------------------------------------
# Le script → les scènes
# ---------------------------------------------------------------------------
def test_scenes_open_with_the_title_and_close_with_the_ending():
    scenes = ex.scenes_of(PLAN)
    assert [s.kind for s in scenes] == ["title", "fact", "steps", "compare", "quote", "end"]
    assert scenes[2].items == PLAN["scenes"][1]["items"]
    assert all(ex.SCENE_MIN_S * 0.8 <= s.seconds <= ex.SCENE_MAX_S for s in scenes)
    assert 25 <= ex.total_seconds(scenes) <= ex.TOTAL_MAX_S
    assert scenes[2].seconds > scenes[1].seconds - 1      # les étapes prennent le temps d'apparaître


def test_a_long_script_is_kept_under_a_minute():
    long_fact = {"kind": "fact", "text": " ".join(["word"] * 30), "big": "", "items": [], "left": "",
                 "right": "", "who": ""}
    scenes = ex.scenes_of(plan(scenes=[long_fact] * 5))
    assert ex.total_seconds(scenes) <= ex.TOTAL_MAX_S + 0.01


@pytest.mark.parametrize("bad,why", [
    ({"title": ""}, "incomplet"),
    ({"ending": ""}, "incomplet"),
    ({"scenes": []}, "incomplet"),
    ({"scenes": [{"kind": "dance", "text": "x"}]}, "inconnue"),
    ({"scenes": [{"kind": "steps", "text": "How", "items": ["only one"]}]}, "2 à 4"),
    ({"scenes": [{"kind": "compare", "text": "x", "left": "a", "right": ""}]}, "deux côtés"),
    ({"scenes": [{"kind": "quote", "text": "words", "who": ""}]}, "auteur"),
    ({"scenes": [{"kind": "fact", "text": ""}]}, "sans texte"),
])
def test_incomplete_scripts_are_refused(bad, why):
    with pytest.raises(ex.LayoutError, match=why):
        ex.scenes_of(plan(**bad))


def test_layout_is_checked_before_anything_is_drawn():
    assert ex.layout_problem(PLAN) is None
    too_wide = plan(scenes=[{"kind": "fact", "text": "ok", "big": "Pneumonoultramicroscopicsilicovolcanoconiosis",
                             "items": [], "left": "", "right": "", "who": ""}])
    assert "trop long" in ex.layout_problem(too_wide)
    too_long = plan(title=" ".join(["Incredible"] * 60))
    assert ex.layout_problem(too_long)


def test_text_shrinks_to_fit_then_gives_up():
    fnt, lines = ex.fit("A short line", 900, 2, 80, 40)
    assert lines == ["A short line"] and fnt.size == 80
    fnt, lines = ex.fit(" ".join(["word"] * 40), 900, 6, 80, 30)
    assert len(lines) <= 6 and fnt.size < 80
    with pytest.raises(ex.LayoutError):
        ex.fit(" ".join(["word"] * 400), 900, 3, 60, 40)


def test_frames_have_the_progress_bar_and_the_label():
    scenes = ex.scenes_of(PLAN)
    first = next(ex.frames(scenes, size=SMALL, fps=2))
    assert first.size == SMALL and first.mode == "RGB"
    r, g, b = first.getpixel((1, 1))                     # la barre de progression, en jaune
    assert r > 200 and g > 150 and b < 120


# ---------------------------------------------------------------------------
# La vidéo
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_render_makes_a_playable_video_with_a_silent_soundtrack(tmp_path):
    out = tmp_path / "v.mp4"
    assert ex.render(PLAN, out, size=SMALL, fps=4) is None
    info = visuals.probe_video(visuals.ffmpeg_exe(), out)
    assert info is not None and (info.width, info.height) == SMALL
    assert abs(info.duration - ex.total_seconds(ex.scenes_of(PLAN))) < 1.5
    assert info.audio                                     # piste son muette : X lit tout


@needs_ffmpeg
def test_render_with_the_story_photo_as_background(tmp_path):
    from PIL import Image
    photo = tmp_path / "photo.jpg"
    Image.new("RGB", (1600, 900), (200, 60, 40)).save(photo)
    out = tmp_path / "v.mp4"
    assert ex.render(PLAN, out, photo, size=SMALL, fps=2) is None
    frame = next(ex.frames(ex.scenes_of(PLAN), photo, size=SMALL, fps=2))
    r, g, b = frame.getpixel((SMALL[0] // 2, SMALL[1] // 3))
    assert r > b                                          # le fond vient bien de la photo (assombrie)


@needs_ffmpeg
def test_unreadable_photo_falls_back_to_the_animated_background(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not a picture")
    assert ex.render(PLAN, tmp_path / "v.mp4", bad, size=SMALL, fps=2) is None


def test_render_explains_why_it_could_not(tmp_path, monkeypatch):
    assert ex.render({"title": "x"}, tmp_path / "v.mp4", size=SMALL, fps=2).startswith("mise en page impossible")
    monkeypatch.setattr(visuals, "ffmpeg_exe", lambda: None)
    assert ex.render(PLAN, tmp_path / "v.mp4", size=SMALL, fps=2) == "ffmpeg absent"
    assert not (tmp_path / "v.mp4").exists()


# ---------------------------------------------------------------------------
# Point d'entrée du planificateur
# ---------------------------------------------------------------------------
def _draft(**kw) -> Draft:
    kw.setdefault("visual", Visual("photo", "https://www.tesla.com/blog/update"))
    return Draft(kind="explainer", texts=["Your car can now tell you're not watching the road."],
                 video_plan=copy.deepcopy(PLAN), **kw)


class FakeRender:
    def __init__(self, result=None) -> None:
        self.result = result
        self.calls: list[tuple] = []

    def __call__(self, plan, out, photo=None, **kw):
        self.calls.append((plan, out, photo))
        if self.result is None:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"mp4")
        return self.result


def test_prepare_makes_the_video_with_the_story_photo(settings, tmp_path, monkeypatch):
    render = FakeRender()
    monkeypatch.setattr(ex, "render", render)
    seen = []

    def find_photo(s, draft, writer, tag):
        seen.append((s.videos_enabled, s.second_image_enabled, tag))
        return str(tmp_path / "photo.jpg"), "photo", "logo.jpg"

    settings.photos_enabled = settings.videos_enabled = settings.second_image_enabled = True
    path, kind, second = ex.prepare(settings, _draft(), writer=None, tag="explainer_2026-11-25",
                                    find_photo=find_photo)
    assert kind == "video" and second is None and path.endswith(".mp4") and Path(path).is_file()
    assert "explainers" in path
    assert seen == [(False, False, "explainer_2026-11-25")]   # une photo seulement : ni extrait, ni logo
    assert render.calls[0][2] == tmp_path / "photo.jpg"
    assert settings.videos_enabled is True                      # le réglage du bot n'est pas touché


def test_prepare_without_photo_uses_the_animated_background(settings, monkeypatch):
    render = FakeRender()
    monkeypatch.setattr(ex, "render", render)
    settings.photos_enabled = True
    path, kind, _ = ex.prepare(settings, _draft(), None, find_photo=lambda *a: (None, None, None))
    assert kind == "video" and render.calls[0][2] is None
    path, kind, _ = ex.prepare(settings, _draft(visual=None), None,
                               find_photo=lambda *a: pytest.fail("aucune photo à chercher"))
    assert kind == "video"


def test_prepare_falls_back_to_the_photo_then_to_text(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(ex, "render", FakeRender("assemblage impossible"))
    settings.photos_enabled = True
    photo = str(tmp_path / "photo.jpg")
    assert ex.prepare(settings, _draft(), None, find_photo=lambda *a: (photo, "photo", None)) == (
        photo, "photo", None)
    assert ex.prepare(settings, _draft(), None, find_photo=lambda *a: (None, None, None)) == (None, None, None)


def test_prepare_never_raises(settings, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ex, "render", boom)
    assert ex.prepare(settings, _draft(visual=None), None) == (None, None, None)
    assert ex.prepare(settings, Draft(kind="explainer", texts=["x"]), None) == (None, None, None)
