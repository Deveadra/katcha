from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, JSON, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from katcha.db import Base


class ChatGPTConnection(Base):
    __tablename__ = "chatgpt_connections"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    issued_client_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    issuer: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255), index=True)
    email: Mapped[str | None] = mapped_column(Text, nullable=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    encrypted_access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    encrypted_id_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    selected_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    connection_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ChatGPTOAuthState(Base):
    __tablename__ = "chatgpt_oauth_states"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    nonce: Mapped[str] = mapped_column(String(255))
    encrypted_code_verifier: Mapped[str] = mapped_column(Text)
    redirect_uri: Mapped[str] = mapped_column(Text)
    expected_client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
