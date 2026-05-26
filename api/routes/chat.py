"""Chat API routes: POST /chat, GET /history/{session_id}."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=10000, description="User message")
    session_id: str | None = Field(None, description="Session ID (created if not provided)")
    project_id: str | None = Field(None, description="Project ID for context")
    stream: bool = Field(False, description="Enable streaming response")


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls_made: list[str]
    iterations: int


class HistoryMessage(BaseModel):
    role: str
    content: str
    tool_name: str | None
    created_at: str


# ---------------------------------------------------------------------------
# Dependency: get agent from app state
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

@router.post("", response_model=ChatResponse)
async def chat(body: ChatRequest, agent: Any = Depends(get_agent)) -> ChatResponse:
    """
    Send a message to the NetSuite Migration AI Agent.

    Returns the agent's response along with metadata about tool calls made.
    """
    try:
        result = agent.chat(
            user_message=body.message,
            session_id=body.session_id,
            project_id=body.project_id,
        )
        return ChatResponse(
            response=result["response"],
            session_id=result["session_id"],
            tool_calls_made=result["tool_calls_made"],
            iterations=result["iterations"],
        )
    except Exception as exc:
        logger.exception("Chat endpoint error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/stream")
async def chat_stream(body: ChatRequest, agent: Any = Depends(get_agent)) -> StreamingResponse:
    """
    Streaming chat endpoint. Returns server-sent events.
    """
    import json

    def event_generator():
        try:
            for event in agent.chat_stream(
                user_message=body.message,
                session_id=body.session_id,
                project_id=body.project_id,
            ):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as exc:
            logger.exception("Stream error")
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/history/{session_id}", response_model=list[HistoryMessage])
async def get_history(
    session_id: str,
    limit: int = 20,
    agent: Any = Depends(get_agent),
) -> list[HistoryMessage]:
    """Retrieve conversation history for a session."""
    try:
        history = agent.memory.get_history(session_id, limit=limit)
        return [
            HistoryMessage(
                role=msg["role"],
                content=msg["content"],
                tool_name=msg.get("tool_name"),
                created_at=msg.get("created_at", ""),
            )
            for msg in history
        ]
    except Exception as exc:
        logger.exception("History endpoint error")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
