from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from katcha.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    text: str
    language: str | None
    confidence: float | None
    segment_count: int


class FasterWhisperTranscriber:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper is not installed; run the analysis worker image or install katcha[media]"
            ) from exc
        self.model = WhisperModel(
            self.settings.whisper_model,
            device=self.settings.whisper_device,
            compute_type=self.settings.whisper_compute_type,
        )

    def transcribe(self, media_path: Path) -> TranscriptResult:
        segments, info = self.model.transcribe(
            str(media_path),
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        text_parts: list[str] = []
        probabilities: list[float] = []
        count = 0
        for segment in segments:
            cleaned = segment.text.strip()
            if cleaned:
                text_parts.append(cleaned)
            avg_logprob = getattr(segment, "avg_logprob", None)
            if avg_logprob is not None:
                probabilities.append(max(0.0, min(1.0, math.exp(float(avg_logprob)))))
            count += 1

        confidence = None
        if probabilities:
            confidence = round(sum(probabilities) / len(probabilities), 6)
        language = getattr(info, "language", None)
        return TranscriptResult(
            text=" ".join(text_parts).strip(),
            language=str(language) if language else None,
            confidence=confidence,
            segment_count=count,
        )


@lru_cache(maxsize=1)
def get_transcriber() -> FasterWhisperTranscriber:
    return FasterWhisperTranscriber()
