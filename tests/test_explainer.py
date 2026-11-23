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


# ---------------------------------------------------------------------------
# Voix off (accord du propriétaire, 23/11/2026)
# ---------------------------------------------------------------------------
import array  # noqa: E402
import math  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import wave  # noqa: E402

from bot import voice as vmod  # noqa: E402


def tone_voice(seconds_per_word: float = 0.3, calls: list | None = None):
    """Voix simulée : un vrai son (pas du silence), 0,3 s par mot, comme une voix posée."""
    def speak(texts, folder):
        if calls is not None:
            calls.append(list(texts))
        out = []
        for i, text in enumerate(texts):
            secs = max(0.5, seconds_per_word * len(text.split()))
            path = folder / f"beat_{i:03d}.wav"
            pcm = array.array("h", [int(8000 * math.sin(k / 3)) for k in range(int(secs * 8000))])
            vmod.write_wav(path, pcm.tobytes(), 8000)
            out.append((path, secs))
        return out
    return speak


def mean_volume(path) -> float:
    res = subprocess.run([visuals.ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", str(path), "-af", "volumedetect",
                          "-f", "null", "-"], capture_output=True, text=True, timeout=60)
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", res.stderr or "")
    return float(m.group(1)) if m else -999.0


def test_what_the_voice_says():
    said = [ex.narration(sc) for sc in ex.scenes_of(PLAN)]
    assert said[0] == [PLAN["title"]]
    assert said[1] == [PLAN["scenes"][0]["text"]]                       # le mot en grand n'est pas lu
    assert said[2] == ["How it works"] + PLAN["scenes"][1]["items"]
    assert said[3] == ["What changes", PLAN["scenes"][2]["left"], PLAN["scenes"][2]["right"]]
    assert said[4] == ["As Tesla put it: It works even when the driver wears sunglasses."]
    assert said[5] == [PLAN["ending"], ex.FOLLOW]


def test_scenes_follow_the_voice():
    scenes = ex.scenes_of(PLAN)
    spoken = [[2.0], [4.0], [1.0, 2.0, 2.0, 2.0], [1.0, 2.0, 2.0], [3.0], [2.0, 1.5]]
    timed, placed = ex.voice_timing(scenes, spoken)
    lead, gap, tail = ex.VOICE_LEAD_S, ex.VOICE_GAP_S, ex.VOICE_TAIL_S
    assert timed[0].seconds == max(ex.SCENE_MIN_S, lead + 2.0 + tail)
    assert timed[2].seconds == pytest.approx(lead + 7.0 + 3 * gap + tail)
    # chaque étape apparaît juste avant d'être dite
    first_item = lead + 1.0 + gap
    assert timed[2].starts == pytest.approx([first_item - ex.VOICE_EARLY_S, first_item + 2 + gap - ex.VOICE_EARLY_S,
                                             first_item + 4 + 2 * gap - ex.VOICE_EARLY_S])
    assert len(timed[3].starts) == 2 and timed[3].starts[1] > timed[3].starts[0]
    assert timed[5].starts == pytest.approx([lead + 2.0 + gap - ex.VOICE_EARLY_S])     # la signature, au « Follow »
    assert placed[0] == (pytest.approx(lead), 0, 0)
    assert placed[1][0] == pytest.approx(timed[0].seconds + lead)
    assert scenes[2].starts == []                                                    # les scènes d'origine intactes


def test_the_layout_uses_the_voice_starts():
    sc = ex.Scene("steps", text="How it works", items=["One thing", "Another thing"], starts=[1.0, 3.5])
    els = ex.Layout(*SMALL).elements(sc)
    assert [e.start for e in els] == [0.1, 1.0, 3.5]


def test_no_voice_beyond_two_minutes():
    scenes = ex.scenes_of(PLAN)
    assert ex.voice_timing(scenes, [[30.0]] * len(scenes)) is None


def test_narration_track_places_each_sentence(tmp_path):
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    vmod.write_wav(a, array.array("h", [1000] * 800).tobytes(), 8000)          # 0,1 s
    vmod.write_wav(b, array.array("h", [-2000] * 800).tobytes(), 8000)
    out = tmp_path / "track.wav"
    assert ex.narration_track([(0.5, a), (2.0, b)], 3.0, out) == 8000
    with wave.open(str(out)) as w:
        frames = array.array("h")
        frames.frombytes(w.readframes(w.getnframes()))
    assert len(frames) == 24000
    assert frames[3999] == 0 and frames[4000] == 1000 and frames[4799] == 1000 and frames[4800] == 0
    assert frames[16000] == -2000 and frames[-1] == 0


@needs_ffmpeg
def test_render_with_the_voice(tmp_path):
    calls: list = []
    out = tmp_path / "v.mp4"
    assert ex.render(PLAN, out, size=SMALL, fps=4, voice=tone_voice(calls=calls)) is None
    assert calls == [[text for sc in ex.scenes_of(PLAN) for text in ex.narration(sc)]]   # une seule synthèse
    expected = ex.total_seconds(ex._with_voice(ex.scenes_of(PLAN), tone_voice(), tmp_path)[0])
    info = visuals.probe_video(visuals.ffmpeg_exe(), out)
    assert info.audio and abs(info.duration - expected) < 1.0
    assert mean_volume(out) > -60                                          # la voix est bien dans la vidéo


@needs_ffmpeg
def test_a_failed_voice_never_blocks_the_video(tmp_path):
    out = tmp_path / "v.mp4"
    assert ex.render(PLAN, out, size=SMALL, fps=2, voice=lambda texts, folder: None) is None
    assert visuals.probe_video(visuals.ffmpeg_exe(), out).audio and mean_volume(out) < -80     # piste muette

    def broken(texts, folder):
        raise RuntimeError("onnxruntime crashed")

    assert ex.render(PLAN, tmp_path / "w.mp4", size=SMALL, fps=2, voice=broken) is None


def test_prepare_speaks_only_when_the_voice_is_ready(settings, monkeypatch):
    seen = []

    def fake_render(plan, out, photo=None, **kw):
        seen.append(kw.get("voice"))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"mp4")
        return None

    monkeypatch.setattr(ex, "render", fake_render)
    ex.prepare(settings, _draft(visual=None), None)
    assert seen[-1] is None                                                 # voix coupée : vidéo muette
    settings.explainer_voice_enabled = True
    monkeypatch.setattr(vmod, "problem", lambda s: None)
    spoken = []
    monkeypatch.setattr(vmod, "synthesize", lambda s, texts, folder: spoken.append((s, texts)) or [])
    ex.prepare(settings, _draft(visual=None), None)
    assert callable(seen[-1])
    seen[-1](["Hello."], Path("/tmp"))
    assert spoken == [(settings, ["Hello."])]
