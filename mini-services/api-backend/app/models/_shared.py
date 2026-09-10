"""Shared prelude for the app.models package - split from the original
single app/models.py (task #3: file-splitting) purely by moving code, no
behavior change. Every submodule imports its columns/helpers from here so
Base.metadata stays the SAME single object regardless of which module a
model class lives in (that is what makes the split safe: SQLAlchemy
relationship() strings and ForeignKey table names resolve against the
metadata, not against Python import structure).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


JSONVariant = JSON().with_variant(JSONB(), "postgresql")
