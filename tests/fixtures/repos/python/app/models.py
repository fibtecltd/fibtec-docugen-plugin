"""ORM models for tenants, documentation sessions and their results."""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base."""


class SessionState(enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    sessions: Mapped[list[Session]] = relationship(back_populates="tenant")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    state: Mapped[SessionState] = mapped_column(default=SessionState.QUEUED)
    created_at: Mapped[datetime]
    tenant: Mapped[Tenant] = relationship(back_populates="sessions")
    results: Mapped[list[Result]] = relationship(back_populates="session")


class Result(Base):
    __tablename__ = "results"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id"))
    path: Mapped[str]
    session: Mapped[Session] = relationship(back_populates="results")
