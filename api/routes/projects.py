"""Projects API routes: CRUD for migration projects."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/projects", tags=["projects"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    source_system: str | None = Field(None, max_length=100)
    target_record_types: list[str] | None = Field(None)


class UpdateProjectRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=2000)
    source_system: str | None = Field(None, max_length=100)
    target_record_types: list[str] | None = Field(None)


class AddNoteRequest(BaseModel):
    note_type: str = Field("general", description="field_mapping, validation_rule, general, decision, issue")
    content: str = Field(..., min_length=1, max_length=10000)


class ProjectResponse(BaseModel):
    id: str
    name: str
    description: str | None
    source_system: str | None
    target_record_types: list[str] | None
    created_at: str
    updated_at: str


class ProjectNote(BaseModel):
    id: int
    project_id: str
    note_type: str
    content: str
    created_at: str


class ProjectDetailResponse(BaseModel):
    id: str
    name: str
    description: str | None
    source_system: str | None
    target_record_types: list[str] | None
    created_at: str
    updated_at: str
    notes: list[ProjectNote]
    field_mappings: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------

def get_agent(request: Request) -> Any:
    agent = getattr(request.app.state, "agent", None)
    if agent is None:
        startup_error = getattr(request.app.state, "startup_error", "unknown error")
        raise HTTPException(
            status_code=503,
            detail=f"Agent not initialized: {startup_error}",
        )
    return agent


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("", response_model=ProjectResponse, status_code=201)
async def create_project(
    body: CreateProjectRequest,
    agent: Any = Depends(get_agent),
) -> ProjectResponse:
    """Create a new migration project."""
    try:
        project_id = agent.memory.create_project(body.name, body.description)
        if body.source_system or body.target_record_types:
            agent.memory.update_project(
                project_id,
                source_system=body.source_system,
                target_record_types=body.target_record_types,
            )
        project = agent.memory.get_project(project_id)
        return _to_project_response(project)
    except Exception as exc:
        logger.exception("Create project error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("", response_model=list[ProjectResponse])
async def list_projects(agent: Any = Depends(get_agent)) -> list[ProjectResponse]:
    """List all migration projects."""
    try:
        projects = agent.memory.list_projects()
        return [_to_project_response(p) for p in projects]
    except Exception as exc:
        logger.exception("List projects error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/{project_id}", response_model=ProjectDetailResponse)
async def get_project(
    project_id: str,
    agent: Any = Depends(get_agent),
) -> ProjectDetailResponse:
    """Get a project with its notes and field mappings."""
    try:
        project = agent.memory.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found.")

        notes = agent.memory.get_project_notes(project_id)
        mappings = agent.memory.get_field_mappings(project_id)

        return ProjectDetailResponse(
            id=project["id"],
            name=project["name"],
            description=project.get("description"),
            source_system=project.get("source_system"),
            target_record_types=_parse_record_types(project.get("target_record_types")),
            created_at=str(project.get("created_at", "")),
            updated_at=str(project.get("updated_at", "")),
            notes=[
                ProjectNote(
                    id=n["id"],
                    project_id=n["project_id"],
                    note_type=n["note_type"],
                    content=n["content"],
                    created_at=str(n.get("created_at", "")),
                )
                for n in notes
            ],
            field_mappings=mappings,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Get project error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: str,
    body: UpdateProjectRequest,
    agent: Any = Depends(get_agent),
) -> ProjectResponse:
    """Update project metadata."""
    try:
        project = agent.memory.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found.")

        agent.memory.update_project(
            project_id,
            name=body.name,
            description=body.description,
            source_system=body.source_system,
            target_record_types=body.target_record_types,
        )
        updated = agent.memory.get_project(project_id)
        return _to_project_response(updated)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Update project error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/{project_id}/notes", response_model=ProjectNote, status_code=201)
async def add_note(
    project_id: str,
    body: AddNoteRequest,
    agent: Any = Depends(get_agent),
) -> ProjectNote:
    """Add a note to a project."""
    try:
        project = agent.memory.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found.")

        note_id = agent.memory.save_project_note(project_id, body.note_type, body.content)
        notes = agent.memory.get_project_notes(project_id)
        note = next((n for n in notes if n["id"] == note_id), None)
        if note is None:
            raise HTTPException(status_code=500, detail="Note saved but could not be retrieved.")

        return ProjectNote(
            id=note["id"],
            project_id=note["project_id"],
            note_type=note["note_type"],
            content=note["content"],
            created_at=str(note.get("created_at", "")),
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Add note error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_project_response(project: dict[str, Any]) -> ProjectResponse:
    import json as _json

    record_types = _parse_record_types(project.get("target_record_types"))
    return ProjectResponse(
        id=project["id"],
        name=project["name"],
        description=project.get("description"),
        source_system=project.get("source_system"),
        target_record_types=record_types,
        created_at=str(project.get("created_at", "")),
        updated_at=str(project.get("updated_at", "")),
    )


def _parse_record_types(value: Any) -> list[str] | None:
    import json as _json

    if value is None:
        return None
    if isinstance(value, list):
        return value
    try:
        parsed = _json.loads(value)
        return parsed if isinstance(parsed, list) else None
    except Exception:
        return None
