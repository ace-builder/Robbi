"""Clip lifecycle, paid-request admission, persistence and HTTP media boundaries."""
import json
from pathlib import Path
import threading
import time

import httpx
import pytest

from robbi.audio_studio import AudioStudio
from robbi.audio_studio.provider import DEFAULT_VOICE, MODEL, ElevenLabs, ProviderError
from robbi.config import load, validate
from robbi.errors import BusyError, ValidationError
from robbi.runtime import Runtime
from robbi.web import create_app

TEST_VOICE = "TestVoice123"

ROOT = Path(__file__).resolve().parents[1]
MP3 = b"ID3" + b"test-audio" * 40


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.release = threading.Event()
        self.release.set()
        self.started = threading.Event()
        self.error = None

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        assert self.release.wait(3)
        kwargs["destination"].write_bytes(MP3)
        if self.error:
            raise self.error

    def voices(self, key, page_token):
        return {"voices": [{"voice_id": TEST_VOICE, "name": "Robbi"}], "next_page_token": ""}


def finished(studio, clip_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not studio.history()["active_id"]:
            # The worker may finish between two reads. Fetch the final clip
            # only after observing completion, never return an earlier snapshot.
            return studio.get(clip_id)
        time.sleep(.01)
    pytest.fail("generation did not finish")


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-secret")
    provider = FakeProvider()
    result = AudioStudio(tmp_path, provider=provider)
    yield result
    provider.release.set()
    result.stop()


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    source = tmp_path / "robot.yaml"
    source.write_text("camera: {enabled: false}\n")
    runtime = Runtime(load(ROOT, source, tmp_path / "var"))
    runtime.audio_studio.provider = FakeProvider()
    yield runtime, create_app(runtime).test_client()
    runtime.audio_studio.provider.release.set()
    runtime.stop()


def test_keep_regenerate_discard_delete_and_restart(studio):
    text = "  [sarcastic] Great.\n[laughs] Another update!  "
    clip = studio.generate(text, TEST_VOICE)
    clip = finished(studio, clip["id"])
    assert clip["status"] == "draft" and clip["bytes"] == len(MP3)
    assert studio.provider.calls[0]["text"] == text
    studio.keep(clip["id"], "  Grumpy startup  ")
    new = studio.regenerate(clip["id"])
    new = finished(studio, new["id"])
    assert new["id"] != clip["id"] and new["parent_id"] == clip["id"]
    assert studio.get(clip["id"])["name"] == "Grumpy startup"
    studio.discard(new["id"])
    assert studio.get(new["id"])["status"] == "discarded"
    assert studio.audio(new["id"]) is None
    studio.configure({"voice_id": "OtherVoice123", "api_key": "new-secret"})
    studio.stop()
    restarted = AudioStudio(studio.directory.parent, provider=studio.provider)
    try:
        assert restarted.get(clip["id"])["status"] == "kept"
        assert restarted.settings()["voice_id"] == "OtherVoice123"
        assert "new-secret" not in json.dumps(restarted.settings())
        assert restarted._key() == "new-secret"
        assert restarted.history(status="kept")["total"] == 1
        restarted.discard(clip["id"], delete=True)
        assert restarted.get(clip["id"]) is None
        assert restarted.audio(clip["id"]) is None
    finally:
        restarted.stop()


def test_delete_during_request_cannot_resurrect_audio(studio):
    studio.provider.release.clear()
    clip = studio.generate("[whispers] Hello", TEST_VOICE)
    assert studio.provider.started.wait(1)
    with pytest.raises(BusyError):
        studio.generate("second", TEST_VOICE)
    studio.discard(clip["id"], delete=True)
    assert studio.history()["active_id"] == clip["id"]
    studio.provider.release.set()
    assert finished(studio, clip["id"]) is None
    assert not list(studio.directory.glob("*.mp3"))
    assert not list(studio.directory.glob("*.part"))


def test_failure_does_not_retry_or_leak_credentials(studio):
    studio.provider.error = RuntimeError("Authorization: test-secret")
    clip = finished(studio, studio.generate("hello", TEST_VOICE)["id"])
    assert clip["status"] == "failed" and not clip["audio_url"]
    assert "test-secret" not in str(clip)
    assert len(studio.provider.calls) == 1
    assert not list(studio.directory.glob("*.part"))
    studio.provider.error = ProviderError("Voice is unavailable")
    assert finished(studio, studio.regenerate(clip["id"])["id"])["error"] == "Voice is unavailable"


def test_recovery_marks_interrupted_and_missing_files_without_replaying(studio):
    clip = finished(studio, studio.generate("hello", TEST_VOICE)["id"])
    studio._path(clip["id"]).unlink()
    with studio._db() as db:
        db.execute("UPDATE clips SET status='generating' WHERE id=?", (clip["id"],))
    studio._path(clip["id"], ".part").write_bytes(b"partial")
    orphan = studio.directory / ("a" * 32 + ".mp3")
    orphan.write_bytes(MP3)
    studio.stop()
    restarted = AudioStudio(studio.directory.parent, provider=studio.provider)
    try:
        recovered = restarted.get(clip["id"])
        assert recovered["status"] == "failed" and "not retried" in recovered["error"]
        assert len(studio.provider.calls) == 1
        assert not orphan.exists()
        assert not list(studio.directory.glob("*.part"))
    finally:
        restarted.stop()


def test_missing_kept_audio_and_corrupt_database_are_recoverable(studio):
    clip = finished(studio, studio.generate("hello", TEST_VOICE)["id"])
    studio.keep(clip["id"], "Missing")
    studio._path(clip["id"]).unlink()
    studio.stop()
    restarted = AudioStudio(studio.directory.parent)
    try:
        assert restarted.get(clip["id"])["status"] == "failed"
        assert restarted.history()["storage_bytes"] == 0
    finally:
        restarted.stop()
    database = studio.directory / "library.sqlite3"
    database.write_bytes(b"corrupt database")
    restarted = AudioStudio(studio.directory.parent)
    try:
        with pytest.raises(RuntimeError, match="audio library"):
            restarted.history()
        assert database.read_bytes() == b"corrupt database"
    finally:
        restarted.stop()


def test_shutdown_during_download_never_publishes_partial_clip(studio):
    studio.provider.release.clear()
    clip = studio.generate("hello", TEST_VOICE)
    assert studio.provider.started.wait(1)
    before = time.monotonic()
    studio.stop()
    assert time.monotonic() - before < .2
    studio.provider.release.set()
    finished(studio, clip["id"])
    assert studio.audio(clip["id"]) is None
    assert not list(studio.directory.glob("*.part"))


@pytest.mark.parametrize("text,voice", [("", TEST_VOICE), (" " * 5, TEST_VOICE),
    ("x" * 10001, TEST_VOICE), (123, TEST_VOICE), ("Hi", "../../secret"), ("Hi", None)])
def test_invalid_inputs_never_reach_provider(studio, text, voice):
    with pytest.raises(ValidationError):
        studio.generate(text, voice)
    assert not studio.provider.calls


def test_limits_reject_before_paid_request_and_do_not_evict_kept(studio):
    studio.history_limit = 1
    clip = finished(studio, studio.generate("hello", TEST_VOICE)["id"])
    studio.keep(clip["id"], "Keep me")
    with pytest.raises(ValidationError, match="History is full"):
        studio.generate("second", TEST_VOICE)
    studio.history_limit = 10
    studio.max_storage = studio.max_clip
    with pytest.raises(ValidationError, match="storage"):
        studio.generate("second", TEST_VOICE)
    assert studio.get(clip["id"])["status"] == "kept"
    assert len(studio.provider.calls) == 1


def test_hardware_independent_http_workflow_and_audio_ranges(dashboard):
    runtime, client = dashboard
    studio = runtime.audio_studio
    assert not runtime._ready.is_set()
    settings = client.get("/api/audio-studio/settings").get_json()
    assert settings["voice_id"] == DEFAULT_VOICE and settings["model"] == "eleven_v4"
    assert not settings["api_key_configured"]
    assert client.post("/api/audio-studio/clips", json={"text": "Hi", "voice_id": TEST_VOICE}).status_code == 400
    response = client.post("/api/audio-studio/settings", json={"api_key": "server-secret"})
    assert response.status_code == 200 and "server-secret" not in response.text
    assert client.get("/api/audio-studio/voices").get_json()["voices"][0]["name"] == "Robbi"
    response = client.post("/api/audio-studio/clips", json={"text": "[sarcastic] Hi!", "voice_id": TEST_VOICE})
    assert response.status_code == 202
    clip = finished(studio, response.get_json()["id"])
    response = client.get(clip["audio_url"], headers={"Range": "bytes=0-15"})
    assert response.status_code == 206 and response.data == MP3[:16]
    assert response.headers["Content-Range"] == f"bytes 0-15/{len(MP3)}"
    response.close()
    response = client.get(clip["audio_url"])
    assert response.mimetype == "audio/mpeg" and response.data == MP3
    response.close()
    assert client.post(f"/api/audio-studio/clips/{clip['id']}/keep", json={"name": "Greeting / startup"}).status_code == 200
    response = client.get(clip["audio_url"] + "?download=1")
    assert "attachment" in response.headers["Content-Disposition"]
    assert "Greeting _ startup.mp3" in response.headers["Content-Disposition"]
    response.close()
    assert client.get("/api/audio-studio/clips?status=kept").get_json()["total"] == 1
    assert client.post(f"/api/audio-studio/clips/{clip['id']}/discard", json={}).status_code == 200
    assert client.get(clip["audio_url"]).status_code == 404
    assert client.delete(f"/api/audio-studio/clips/{clip['id']}").status_code == 200
    assert client.get(f"/api/audio-studio/clips/{clip['id']}").status_code == 404


def test_http_boundary_secret_and_path_validation(dashboard, monkeypatch):
    runtime, client = dashboard
    for path in ("/api/audio-studio/settings", "/api/audio-studio/clips"):
        assert client.post(path, json={}, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post(path, json=[]).status_code == 400
    assert client.get("/api/audio-studio/clips/invalid/audio").status_code == 400
    assert client.get("/api/audio-studio/clips?offset=NaN").status_code == 400
    assert client.get("/api/audio-studio/clips?status=nope").status_code == 400
    assert client.post("/api/audio-studio/settings", json={"api_key": "secret\r\nheader"}).status_code == 400
    assert client.post("/api/audio-studio/settings", json={"voice_id": "../../foo"}).status_code == 400
    monkeypatch.setenv("ROBBI_WEB_TOKEN", "password")
    runtime.config.values["server"]["auth_enabled"] = True
    assert client.get("/api/audio-studio/settings").status_code == 401
    assert client.get("/api/audio-studio/settings", headers={"Authorization": "Bearer password"}).status_code == 200


def mock_provider(handler):
    return ElevenLabs(lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handler), **kwargs))


def test_provider_preserves_tags_and_explicit_v4_mp3_contract(tmp_path):
    seen = []
    def respond(request):
        seen.append(request)
        return httpx.Response(200, content=MP3, headers={"content-type": "audio/mpeg"})
    provider = mock_provider(respond)
    output = tmp_path / "clip.part"
    provider.generate(key="private", text=" [sarcastic] Hi! ", voice_id=TEST_VOICE,
                      destination=output, max_bytes=1024, cancelled=threading.Event())
    request = seen[0]
    assert json.loads(request.content) == {"text": " [sarcastic] Hi! ", "model_id": MODEL}
    assert request.headers["xi-api-key"] == "private"
    assert request.url.params["output_format"] == "mp3_44100_128"
    assert request.url.path == f"/v1/text-to-speech/{TEST_VOICE}"
    assert output.read_bytes() == MP3


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422, 429, 500, 302])
def test_provider_errors_are_actionable_and_never_echo_raw_body(tmp_path, status):
    provider = mock_provider(lambda _: httpx.Response(status, text="private-api-key"))
    with pytest.raises(ProviderError) as error:
        provider.generate(key="private-api-key", text="Hi", voice_id=TEST_VOICE,
                          destination=tmp_path / "audio", max_bytes=1000, cancelled=threading.Event())
    assert "private-api-key" not in str(error.value)
    assert not (tmp_path / "audio").exists()


@pytest.mark.parametrize("content,ctype,limit", [(b"", "audio/mpeg", 100), (b"not mp3", "audio/mpeg", 100),
    (b"{}", "application/json", 100), (MP3, "audio/mpeg", 16)])
def test_provider_rejects_invalid_or_oversized_audio(tmp_path, content, ctype, limit):
    provider = mock_provider(lambda _: httpx.Response(200, content=content, headers={"content-type": ctype}))
    with pytest.raises(ProviderError):
        provider.generate(key="key", text="Hi", voice_id=TEST_VOICE,
                          destination=tmp_path / "audio", max_bytes=limit, cancelled=threading.Event())


def test_voice_pagination_and_only_public_fields():
    def respond(request):
        assert request.url.path == "/v2/voices"
        assert request.url.params["next_page_token"] == "page-two"
        return httpx.Response(200, json={"voices": [{"voice_id": TEST_VOICE, "name": "My voice", "secret": "hidden"}],
                                         "has_more": True, "next_page_token": "page-three"})
    assert mock_provider(respond).voices("key", "page-two") == {
        "voices": [{"voice_id": TEST_VOICE, "name": "My voice"}], "next_page_token": "page-three"}


def test_network_timeout_and_missing_dependency_have_safe_errors(tmp_path, monkeypatch):
    def timeout(request):
        raise httpx.ReadTimeout("private-api-key", request=request)
    provider = mock_provider(timeout)
    with pytest.raises(ProviderError, match="may have used credits") as error:
        provider.generate(key="private-api-key", text="Hi", voice_id=TEST_VOICE,
                          destination=tmp_path / "audio", max_bytes=1000, cancelled=threading.Event())
    assert "private-api-key" not in str(error.value)
    import sys
    monkeypatch.setitem(sys.modules, "httpx", None)
    with pytest.raises(ProviderError, match="providers extra"):
        ElevenLabs().voices("key")


@pytest.mark.parametrize("values", [{"history_limit": 1.5}, {"max_storage_mb": 0}, {"max_clip_mb": 32,"max_storage_mb": 16}])
def test_audio_studio_config_limits(values):
    with pytest.raises(ValidationError):
        validate({"audio_studio": values})
