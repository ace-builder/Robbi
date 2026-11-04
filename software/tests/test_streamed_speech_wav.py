"""Provider WAV streams must preserve the chosen volume and bounded memory."""
import io
import logging
import struct
import wave

import pytest

from robot import media


def wav_bytes(pcm, *, channels=1, width=2, rate=24000, placeholder=None):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(pcm)
    data = bytearray(buffer.getvalue())
    if placeholder is not None:
        struct.pack_into("<I", data, 4, placeholder)
        struct.pack_into("<I", data, 40, placeholder)
    return bytes(data)


@pytest.fixture
def converter():
    # Conversion itself has no worker, hardware or persisted-state dependency.
    player = object.__new__(media.AudioPlayer)
    player._log = logging.getLogger(__name__)
    return player


@pytest.mark.parametrize("placeholder", [None, 0x7FFFFFFF, 0xFFFFFFFF])
@pytest.mark.parametrize("channels", [1, 2])
def test_provider_placeholder_scales_actual_pcm_and_repairs_header(tmp_path, converter, placeholder, channels):
    samples = (12000, -12000, 0, 4000) * 100
    source = tmp_path / "provider.wav"
    source.write_bytes(wav_bytes(struct.pack("<400h", *samples), channels=channels,
                                placeholder=placeholder))
    assert converter.duration_s(source) == pytest.approx(400 / channels / 24000)

    scaled = converter._scale_wav_python(source, 0.25)
    assert scaled is not None
    try:
        with wave.open(str(scaled), "rb") as output:
            assert output.getnchannels() == channels
            assert output.getnframes() == 400 // channels
            assert output.getframerate() == 24000
            assert struct.unpack("<400h", output.readframes(400)) == tuple(s // 4 for s in samples)
        assert converter.duration_s(scaled) == converter.duration_s(source)
    finally:
        scaled.unlink()


def test_pcm_conversion_streams_and_enforces_actual_size_limit(tmp_path, converter, monkeypatch):
    monkeypatch.setattr(media, "MAX_PCM_BYTES", 128)
    monkeypatch.setattr(media, "PCM_CHUNK_BYTES", 32)
    monkeypatch.setattr(media.tempfile, "tempdir", str(tmp_path))
    read_counts = []
    original_read = wave.Wave_read.readframes

    def readframes(reader, count):
        read_counts.append(count)
        return original_read(reader, count)

    monkeypatch.setattr(wave.Wave_read, "readframes", readframes)
    source = tmp_path / "provider.wav"
    source.write_bytes(wav_bytes(b"\0" * 128, placeholder=0xFFFFFFFF))
    scaled = converter._scale_wav_python(source, 0.25)
    assert scaled is not None
    scaled.unlink()
    source.write_bytes(wav_bytes(b"\0" * 130, placeholder=0xFFFFFFFF))
    assert converter._scale_wav_python(source, 0.25) is None
    assert max(read_counts) <= 16
    assert len(read_counts) > 4
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.parametrize("pcm", [b"", b"\0\0\0"])
def test_empty_or_incomplete_pcm_is_rejected_and_tempfile_removed(tmp_path, converter, monkeypatch, pcm):
    monkeypatch.setattr(media.tempfile, "tempdir", str(tmp_path))
    source = tmp_path / "provider.wav"
    source.write_bytes(wav_bytes(pcm, placeholder=0xFFFFFFFF))
    assert converter._scale_wav_python(source, 0.25) is None
    assert list(tmp_path.iterdir()) == [source]


def test_unsigned_eight_bit_pcm_preserves_silence_when_scaled(tmp_path, converter):
    source = tmp_path / "unsigned.wav"
    source.write_bytes(wav_bytes(bytes([0, 128, 252]), width=1, placeholder=0xFFFFFFFF))
    scaled = converter._scale_wav_python(source, 0.5)
    assert scaled is not None
    try:
        with wave.open(str(scaled), "rb") as output:
            assert output.readframes(3) == bytes([64, 128, 190])
    finally:
        scaled.unlink()


def test_duration_excludes_trailing_non_audio_chunks(tmp_path, converter):
    source = tmp_path / "with-metadata.wav"
    data = bytearray(wav_bytes(b"\0" * 48000))
    data.extend(b"JUNK" + struct.pack("<I", 100) + b"x" * 100)
    struct.pack_into("<I", data, 4, len(data) - 8)
    source.write_bytes(data)
    assert converter.duration_s(source) == 1.0


def test_streamed_speech_plays_at_requested_volume_without_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(media.AudioPlayer, "_load_volume", lambda self: 25)
    monkeypatch.setattr(media.shutil, "which", lambda command: "/fake/aplay")
    monkeypatch.setattr(media.tempfile, "tempdir", str(tmp_path))
    played = []

    def no_ffmpeg(*args):
        pytest.fail("Speech must not need ffmpeg volume conversion")

    def play(player, path, *args):
        with wave.open(str(path), "rb") as output:
            played.append(struct.unpack("<4h", output.readframes(4)))
            assert output.getnframes() == 4
        return 0

    monkeypatch.setattr(media.AudioPlayer, "_scale_wav_ffmpeg", no_ffmpeg)
    monkeypatch.setattr(media.AudioPlayer, "_play_with_fallbacks", play)
    manager = media.MediaManager()
    try:
        result = manager.speak_wav_bytes(wav_bytes(struct.pack("<4h", 12000, -12000, 0, 4000),
                                                 placeholder=0xFFFFFFFF))
        assert result.returncode == 0
        assert played == [(3000, -3000, 0, 1000)]
        assert manager.audio.health()["last_error"] == ""
        assert list(tmp_path.iterdir()) == []
    finally:
        manager.shutdown()
