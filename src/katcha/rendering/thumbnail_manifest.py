from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from katcha.rendering.manifest import ShortBrandSpec


class ThumbnailSourceSpec(BaseModel):
    storage_key: str = Field(min_length=1)
    fit: str = "cover"


class ThumbnailTextSpec(BaseModel):
    text: str | None = Field(default=None, max_length=40)
    position: str = "bottom"
    max_lines: int = Field(default=2, ge=1, le=3)


class ThumbnailRenderManifest(BaseModel):
    version: str = "thumbnail-render-v1"
    publication_id: str = Field(min_length=1)
    parent_variant_id: str = Field(min_length=1)
    variant_key: str = Field(min_length=1)
    variant_version: int = Field(ge=1)
    channel_profile_id: str = Field(min_length=1)
    brand_key: str = Field(min_length=1)
    brand_version: int = Field(ge=1)
    source: ThumbnailSourceSpec
    brief: dict[str, object]
    text: ThumbnailTextSpec
    brand: ShortBrandSpec
    width: int = Field(default=1280, ge=640, le=3840)
    height: int = Field(default=720, ge=360, le=2160)
    output_key: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_lineage(self) -> ThumbnailRenderManifest:
        if self.brand.brand_key != self.brand_key:
            raise ValueError("thumbnail brand key does not match frozen brand")
        if self.brand.version != self.brand_version:
            raise ValueError("thumbnail brand version does not match frozen brand")
        expected = (
            f"packaging/{self.publication_id}/{self.variant_key}/"
            f"v{self.variant_version}/thumbnail.png"
        )
        if self.output_key != expected:
            raise ValueError("thumbnail output key does not match immutable variant namespace")
        return self


def build_thumbnail_manifest(
    *,
    publication_id: str,
    parent_variant_id: str,
    variant_key: str,
    variant_version: int,
    channel_profile_id: str,
    brand: ShortBrandSpec,
    source_storage_key: str,
    brief: dict[str, object],
) -> ThumbnailRenderManifest:
    on_image_text = str(brief.get("on_image_text") or "").strip()
    if len(on_image_text) > 40:
        raise ValueError("thumbnail on-image text cannot exceed 40 characters")
    return ThumbnailRenderManifest(
        publication_id=publication_id,
        parent_variant_id=parent_variant_id,
        variant_key=variant_key,
        variant_version=variant_version,
        channel_profile_id=channel_profile_id,
        brand_key=brand.brand_key,
        brand_version=brand.version,
        source=ThumbnailSourceSpec(storage_key=source_storage_key),
        brief=dict(brief),
        text=ThumbnailTextSpec(text=on_image_text or None),
        brand=brand,
        output_key=(
            f"packaging/{publication_id}/{variant_key}/"
            f"v{variant_version}/thumbnail.png"
        ),
    )
