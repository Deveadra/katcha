"""Decode a real media file through the same dependency path used by Whisper."""

import wave

import pytest


def test_faster_whisper_decodes_media_without_metadata_argument_error(tmp_path):
    pytest.importorskip("av")
    audio = pytest.importorskip("faster_whisper.audio")
    media = tmp_path / "sample with spaces.wav"
    with wave.open(str(media), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\x00\x00" * 16000)
    samples = audio.decode_audio(str(media))
    assert samples.shape == (16000,)
    assert samples.dtype.name == "float32"
