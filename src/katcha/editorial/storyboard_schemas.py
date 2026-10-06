"""Durable, partial Storyboard workspace contracts.

These are editing-state contracts, not render authorization. A workspace may contain
unassigned beats; strict StoryboardPlan validation remains the render/preflight gate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from katcha.editorial.project_schemas import Contract, Identity, RequestKey
from katcha.editorial.visual_schemas import VisualBeat, VisualMediaUse, VisualOverlay


class StoryboardWorkspaceBeat(Contract):
    beat_id: Identity
    layout: Literal[
        "unassigned",
        "single",
        "quote",
        "image",
        "image_comparison",
    ] = "unassigned"
    media: list[VisualMediaUse] = Field(default_factory=list, max_length=2)
    quote_source_id: Identity | None = None
    image_id: UUID | None = None
    image_ids: list[UUID] = Field(default_factory=list, max_length=2)
    image_push_in: float = Field(default=1, ge=1, le=1.15)
    overlays: list[VisualOverlay] = Field(default_factory=list, max_length=1)

    @model_validator(mode="after")
    def coherent_partial_visual(self) -> Self:
        if self.layout == "unassigned":
            if (
                self.media
                or self.quote_source_id is not None
                or self.image_id is not None
                or self.image_ids
                or self.image_push_in != 1
                or self.overlays
            ):
                raise ValueError("Unassigned beats cannot carry visual selections")
            return self
        VisualBeat.model_validate(self.model_dump())
        return self


class StoryboardWorkspace(Contract):
    version: Literal["editorial-storyboard-workspace-v1"] = (
        "editorial-storyboard-workspace-v1"
    )
    presentation_mode: Literal["captioned_silent", "narrated"] = "captioned_silent"
    asset_run_id: UUID | None = None
    beats: list[StoryboardWorkspaceBeat] = Field(min_length=1, max_length=100)
    narration_ids: dict[Identity, UUID] = Field(default_factory=dict, max_length=100)

    @model_validator(mode="after")
    def coherent_workspace(self) -> Self:
        beat_ids = [beat.beat_id for beat in self.beats]
        if len(set(beat_ids)) != len(beat_ids):
            raise ValueError("Storyboard workspace beat IDs must be unique")
        has_footage = any(beat.media for beat in self.beats)
        if has_footage and self.asset_run_id is None:
            raise ValueError("Footage selections require their acquisition run")
        if not has_footage and self.asset_run_id is not None:
            raise ValueError("An acquisition run is only valid with selected footage")
        if not set(self.narration_ids) <= set(beat_ids):
            raise ValueError("Narration selections must reference workspace beats")
        if self.presentation_mode == "captioned_silent" and self.narration_ids:
            raise ValueError("Silent workspaces cannot select narration")
        if len(self.model_dump_json().encode("utf-8")) > 500_000:
            raise ValueError("Storyboard workspace exceeds the 500 KB snapshot limit")
        return self


class SaveStoryboardWorkspace(Contract):
    script_revision: int = Field(gt=0, strict=True)
    expected_version: int = Field(ge=0, strict=True)
    workspace: StoryboardWorkspace
    idempotency_key: RequestKey


class UndoStoryboardWorkspace(Contract):
    script_revision: int = Field(gt=0, strict=True)
    expected_version: int = Field(gt=0, strict=True)
    idempotency_key: RequestKey


class StoryboardWorkspaceResponse(Contract):
    script_revision: int
    version: int
    parent_version: int | None = None
    digest: str
    workspace: StoryboardWorkspace
    origin: Literal["operator", "ai_apply", "undo"]
    actor: str
    created_at: datetime
