"""Voix off des vidéos explicatives (bot/voice.py) : aucun vrai modèle ; Kokoro est simulé."""
from __future__ import annotations

import json
import subprocess
import sys
import types
import wave
from pathlib import Path

import pytest

from bot import voice as vmod


@pytest.fixture
def ready(settings, tmp_path, monkeypatch):
    """Voix allumée, paquet « présent », fichiers du modèle en place (vides : jamais lus ici)."""
    settings.explainer_voice_enabled = True
    settings.voice_model_dir = str(tmp_path / "kokoro")
    (tmp_path / "kokoro").mkdir()
    for name in (vmod.MODEL_FILE, vmod.VOICES_FILE):
        (tmp_path / "kokoro" / name).write_bytes(b"x")
    real = vmod.importlib.util.find_spec
    monkeypatch.setattr(vmod.importlib.util, "find_spec",
                        lambda name, *a: object() if name == "kokoro_onnx" else real(name, *a))
    return settings


def test_voice_is_off_by_default_and_says_why(settings, tmp_path, monkeypatch):
    assert vmod.problem(settings).startswith("voix off coupée")
    settings.explainer_voice_enabled = True
    monkeypatch.setattr(vmod.importlib.util, "find_spec", lambda name, *a: None)
    assert vmod.problem(settings) == "paquet kokoro-onnx absent"
    monkeypatch.setattr(vmod.importlib.util, "find_spec", lambda name, *a: object())
    settings.voice_model_dir = str(tmp_path / "nothing")
    assert "fichier du modèle absent" in vmod.problem(settings) and "install_voice.sh" in vmod.problem(settings)


def test_ready_voice(ready):
    assert vmod.problem(ready) is None
    assert vmod.model_dir(ready).name == "kokoro"


def test_default_model_folder_is_outside_the_bot(settings):
    assert vmod.model_dir(settings) == Path.home() / "xbot-models" / "kokoro"


@pytest.mark.parametrize("raw,name", [("", "af_heart"), ("am_michael", "am_michael"), (" BM_George ", "bm_george"),
                                      ("../etc/passwd", "af_heart"), ("michael", "af_heart")])
def test_voice_name(settings, raw, name):
    settings.explainer_voice = raw
    assert vmod.voice_name(settings) == name


class FakeRun:
    """subprocess.run du processus à part : écrit les WAV et result.json comme le vrai."""

    def __init__(self, returncode: int = 0, seconds: float = 1.5, files: int | None = None, stderr: str = "") -> None:
        self.returncode, self.seconds, self.files, self.stderr = returncode, seconds, files, stderr
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        job = json.loads(Path(cmd[-1]).read_text())
        out = Path(job["out"])
        files = []
        for i, _text in enumerate(job["texts"][: self.files if self.files is not None else None]):
            path = out / f"beat_{i:03d}.wav"
            vmod.write_wav(path, b"\0\0" * 2400, 24000)
            files.append({"path": str(path), "seconds": self.seconds, "rate": 24000})
        if self.returncode == 0:
            (out / "result.json").write_text(json.dumps({"files": files}))
        return subprocess.CompletedProcess(cmd, self.returncode, "", self.stderr)


def test_synthesize_runs_apart_and_returns_each_sentence(ready, tmp_path, monkeypatch):
    run = FakeRun(seconds=2.0)
    monkeypatch.setattr(vmod.subprocess, "run", run)
    out = vmod.synthesize(ready, ["How does it work?", "  It  sees you. "], tmp_path / "job")
    assert [round(s, 1) for _p, s in out] == [2.0, 2.0] and all(p.is_file() for p, _s in out)
    cmd = run.calls[0]
    assert cmd[:3] == [sys.executable, "-m", "bot.voice"]
    job = json.loads(Path(cmd[3]).read_text())
    assert job["texts"] == ["How does it work?", "It sees you."]
    assert job["voice"] == "af_heart" and job["lang"] == "en-us" and job["model"].endswith(vmod.MODEL_FILE)


@pytest.mark.parametrize("run", [FakeRun(returncode=1, stderr="Traceback…\nRuntimeError: boom"),
                                 FakeRun(files=1), FakeRun(seconds=0)])
def test_any_problem_means_no_voice(ready, tmp_path, monkeypatch, run):
    monkeypatch.setattr(vmod.subprocess, "run", run)
    assert vmod.synthesize(ready, ["One.", "Two."], tmp_path / "job") is None


def test_too_slow_or_impossible(ready, tmp_path, monkeypatch):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(vmod.subprocess, "run", slow)
    assert vmod.synthesize(ready, ["One."], tmp_path / "a") is None

    def broken(cmd, **kw):
        raise OSError("no python")

    monkeypatch.setattr(vmod.subprocess, "run", broken)
    assert vmod.synthesize(ready, ["One."], tmp_path / "b") is None


def test_nothing_to_say_or_voice_off(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(vmod.subprocess, "run", lambda *a, **k: pytest.fail("rien ne doit être lancé"))
    assert vmod.synthesize(settings, ["Hello."], tmp_path) is None             # voix coupée
    settings.explainer_voice_enabled = True
    assert vmod.synthesize(settings, [], tmp_path) is None
    assert vmod.synthesize(settings, ["Hello.", "  "], tmp_path) is None


def test_the_separate_process_writes_one_wav_per_sentence(tmp_path, monkeypatch):
    made = []

    class FakeKokoro:
        def __init__(self, model, voices):
            made.append((model, voices))

        def create(self, text, voice, speed, lang):
            assert (voice, speed, lang) == ("am_michael", 1.0, "en-us")
            return [0.0, 0.5, -0.5, 2.0] * 6000, 24000          # 1 s de « voix »

    monkeypatch.setitem(sys.modules, "kokoro_onnx", types.SimpleNamespace(Kokoro=FakeKokoro))
    job = {"texts": ["One.", "Two."], "out": str(tmp_path), "voice": "am_michael", "speed": 1.0, "lang": "en-us",
           "model": "m.onnx", "voices": "v.bin"}
    (tmp_path / "job.json").write_text(json.dumps(job))
    assert vmod.main([str(tmp_path / "job.json")]) == 0
    result = json.loads((tmp_path / "result.json").read_text())
    assert made == [("m.onnx", "v.bin")]
    assert [f["seconds"] for f in result["files"]] == [1.0, 1.0]
    with wave.open(result["files"][0]["path"]) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 24000, 24000)
    assert vmod.main([]) == 2


def test_pcm_conversion_clips_loud_samples():
    pcm = vmod._pcm16([0.0, 1.0, -1.0, 3.0])
    import array
    values = array.array("h")
    values.frombytes(pcm)
    if sys.byteorder != "little":
        values.byteswap()
    assert values.tolist() == [0, 32767, -32767, 32767]


def test_self_test(ready, monkeypatch):
    monkeypatch.setattr(vmod.subprocess, "run", FakeRun(seconds=1.2))
    assert vmod.self_test(ready) == (True, "af_heart, 1.2 s de voix")
    ready.explainer_voice_enabled = False
    ok, why = vmod.self_test(ready)
    assert ok is False and "coupée" in why


def test_install_script_matches_the_code():
    script = (Path(__file__).resolve().parent.parent / "deploy" / "install_voice.sh").read_text()
    for name, sha in vmod.MODEL_SHA256.items():
        assert f"fetch {name} {sha}" in script
    assert vmod.MODEL_URL.rstrip("/") in script and "kokoro-onnx==0.6.1" in script
    req = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
    assert "kokoro-onnx==0.6.1" in req
