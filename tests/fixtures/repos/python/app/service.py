"""Business logic: submit a session, run it, read its result."""
from __future__ import annotations

from .models import Session, SessionState


class QuotaExceeded(Exception):
    """The tenant has no credit left."""


def reserve_credit(tenant_id: int, amount: int) -> None:
    if amount > 100:
        raise QuotaExceeded(tenant_id)


def submit(tenant_id: int, repo: str) -> Session:
    reserve_credit(tenant_id, amount=4)
    session = Session(tenant_id=tenant_id, state=SessionState.QUEUED)
    enqueue(session, repo)
    return session


def enqueue(session: Session, repo: str) -> None:
    session.state = SessionState.RUNNING


def finish(session: Session, ok: bool) -> None:
    session.state = SessionState.DONE if ok else SessionState.FAILED
