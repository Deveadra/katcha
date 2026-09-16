from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class EpisodeItemScript(BaseModel):
    position: int = Field(ge=1)
    clip_id: str = Field(min_length=1)
    reveal_line: str | None = Field(default=None, max_length=180)
    pre_commentary: str | None = Field(default=None, max_length=260)
    post_commentary: str | None = Field(default=None, max_length=260)
    transition_to_next: str | None = Field(default=None, max_length=220)
    editorial_purpose: str = Field(default="", max_length=180)

    @model_validator(mode="after")
    def require_meaningful_host_layer(self) -> EpisodeItemScript:
        if not any(
            value and value.strip()
            for value in (
                self.reveal_line,
                self.pre_commentary,
                self.post_commentary,
                self.transition_to_next,
            )
        ):
            raise ValueError("episode item must contain at least one host beat")
        return self


class RankedEpisodeScript(BaseModel):
    style: Literal["observational", "sarcastic", "interactive"]
    title_angle: str = Field(default="", max_length=180)
    use_native_cold_open: bool = False
    opening_line: str | None = Field(default=None, max_length=280)
    items: list[EpisodeItemScript] = Field(min_length=3, max_length=7)
    closing_line: str | None = Field(default=None, max_length=260)
    interaction_prompt: str | None = Field(default=None, max_length=240)
    rationale: str = Field(default="", max_length=700)

    @model_validator(mode="after")
    def validate_episode_shape(self) -> RankedEpisodeScript:
        positions = [item.position for item in self.items]
        clip_ids = [item.clip_id for item in self.items]
        if len(positions) != len(set(positions)):
            raise ValueError("episode script positions must be unique")
        if len(clip_ids) != len(set(clip_ids)):
            raise ValueError("episode script clip IDs must be unique")
        if not self.use_native_cold_open and not (self.opening_line or "").strip():
            raise ValueError("episode requires an opening line unless using a native cold open")
        return self

    def narration_beats(self) -> list[dict[str, object]]:
        beats: list[dict[str, object]] = []
        sequence = 0

        def add(
            text: str | None,
            *,
            placement: str,
            position: int | None = None,
            clip_id: str | None = None,
        ) -> None:
            nonlocal sequence
            normalized = (text or "").strip()
            if not normalized:
                return
            beats.append(
                {
                    "sequence": sequence,
                    "placement": placement,
                    "position": position,
                    "clip_id": clip_id,
                    "text": normalized,
                }
            )
            sequence += 1

        add(self.opening_line, placement="opening")
        for item in sorted(self.items, key=lambda value: value.position, reverse=True):
            add(
                item.reveal_line,
                placement="reveal",
                position=item.position,
                clip_id=item.clip_id,
            )
            add(
                item.pre_commentary,
                placement="pre_clip",
                position=item.position,
                clip_id=item.clip_id,
            )
            add(
                item.post_commentary,
                placement="post_clip",
                position=item.position,
                clip_id=item.clip_id,
            )
            add(
                item.transition_to_next,
                placement="transition",
                position=item.position,
                clip_id=item.clip_id,
            )
        add(self.closing_line, placement="closing")
        add(self.interaction_prompt, placement="interaction")
        return beats


class RankedEpisodeScriptSet(BaseModel):
    candidates: list[RankedEpisodeScript] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def require_distinct_styles(self) -> RankedEpisodeScriptSet:
        styles = {candidate.style for candidate in self.candidates}
        if styles != {"observational", "sarcastic", "interactive"}:
            raise ValueError("episode script set must contain one treatment per style")
        return self
