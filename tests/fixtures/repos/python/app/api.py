"""HTTP routes."""
from fastapi import APIRouter, HTTPException

from . import service

router = APIRouter(prefix="/v1")


@router.post("/sessions", status_code=202)
def create_session(tenant_id: int, repo: str) -> dict:
    """Submit a repository for documentation."""
    try:
        session = service.submit(tenant_id, repo)
    except service.QuotaExceeded:
        raise HTTPException(status_code=402, detail="no credit")
    return {"id": session.id, "state": session.state.value}


@router.get("/sessions/{session_id}")
def get_session(session_id: int) -> dict:
    """Read a session's state."""
    return {"id": session_id}
