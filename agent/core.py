"""Agentic loop for the NetSuite Migration AI Agent using the Anthropic SDK."""

from __future__ import annotations

import logging
import os
from typing import Any, Generator

import anthropic
from dotenv import load_dotenv

from agent.memory import AgentMemory
from agent.prompts import get_system_prompt
from agent.tools import TOOL_DEFINITIONS, run_tool
from knowledge_base.index import KnowledgeIndex

load_dotenv()

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
_MAX_TOOL_ITERATIONS = 10  # Safety limit on agentic loop iterations


class NSMigrationAgent:
    """
    NetSuite Data Migration AI Agent.

    Uses the Anthropic SDK with tool use to answer questions, validate data,
    map fields, and generate NetSuite import files.
    """

    def __init__(
        self,
        project_id: str | None = None,
        model: str | None = None,
        data_dir: str | None = None,
    ) -> None:
        self.project_id = project_id
        self.model = model or _DEFAULT_MODEL
        self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self.memory = AgentMemory(
            db_path=os.path.join(data_dir or os.getenv("DATA_DIR", "./data"), "agent_memory.db")
        )
        self.knowledge_index = KnowledgeIndex(data_dir=data_dir or os.getenv("DATA_DIR", "./data"))

        logger.info(
            "NSMigrationAgent initialized. Model: %s, Project: %s",
            self.model,
            self.project_id or "none",
        )

    # ------------------------------------------------------------------
    # Public chat interface
    # ------------------------------------------------------------------

    def chat(
        self,
        user_message: str,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Process a user message through the agentic loop.

        Returns:
            {
                "response": str,              # Final assistant text
                "session_id": str,            # Session ID used
                "tool_calls_made": list[str], # Names of tools called
                "iterations": int,            # Number of loop iterations
            }
        """
        effective_project_id = project_id or self.project_id
        session_id = self.memory.get_or_create_session(session_id, effective_project_id)

        # Build conversation history for Anthropic API
        history = self.memory.get_anthropic_messages(session_id, limit=20)

        # Persist user message
        self.memory.save_message(
            session_id=session_id,
            role="user",
            content=user_message,
            project_id=effective_project_id,
        )

        # Build messages list
        messages: list[dict[str, Any]] = history + [{"role": "user", "content": user_message}]

        # Build system prompt with optional project context
        project_context = None
        if effective_project_id:
            try:
                project_context = self.memory.get_project_summary(effective_project_id)
            except Exception:
                pass
        system_prompt = get_system_prompt(project_context)

        # Run agentic loop
        tool_calls_made: list[str] = []
        final_response = ""
        iterations = 0

        while iterations < _MAX_TOOL_ITERATIONS:
            iterations += 1
            logger.debug("Agentic loop iteration %d", iterations)

            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=system_prompt,
                    tools=TOOL_DEFINITIONS,
                    messages=messages,
                )
            except anthropic.APIError as exc:
                logger.error("Anthropic API error: %s", exc)
                final_response = f"I encountered an API error: {exc}. Please try again."
                break

            # Collect text from response content blocks
            text_parts: list[str] = []
            tool_use_blocks: list[Any] = []

            for block in response.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_use_blocks.append(block)

            # If no tool calls, we're done
            if response.stop_reason == "end_turn" or not tool_use_blocks:
                final_response = "\n".join(text_parts)
                break

            # Append assistant message to conversation
            messages.append({"role": "assistant", "content": response.content})

            # Execute tool calls and build tool_result blocks
            tool_results: list[dict[str, Any]] = []
            for tool_block in tool_use_blocks:
                tool_name = tool_block.name
                tool_input = tool_block.input
                tool_use_id = tool_block.id

                logger.info("Calling tool: %s", tool_name)
                tool_calls_made.append(tool_name)

                tool_result = run_tool(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    memory=self.memory,
                    knowledge_index=self.knowledge_index,
                )

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": tool_result,
                    }
                )

            # Append tool results as a user message
            messages.append({"role": "user", "content": tool_results})

        else:
            # Loop limit exceeded
            logger.warning("Agentic loop hit max iterations (%d).", _MAX_TOOL_ITERATIONS)
            final_response = (
                final_response
                or "I reached the maximum number of reasoning steps. Please try a more specific question."
            )

        # Persist assistant response
        self.memory.save_message(
            session_id=session_id,
            role="assistant",
            content=final_response,
            project_id=effective_project_id,
        )

        return {
            "response": final_response,
            "session_id": session_id,
            "tool_calls_made": tool_calls_made,
            "iterations": iterations,
        }

    # ------------------------------------------------------------------
    # Streaming support
    # ------------------------------------------------------------------

    def chat_stream(
        self,
        user_message: str,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> Generator[dict[str, Any], None, None]:
        """
        Streaming version of chat(). Yields event dicts:
            {"type": "text", "text": "..."}
            {"type": "tool_start", "tool_name": "..."}
            {"type": "tool_end", "tool_name": "...", "result": "..."}
            {"type": "done", "session_id": "...", "tool_calls_made": [...]}

        Note: Tool loop is still synchronous internally; only final text is streamed.
        """
        effective_project_id = project_id or self.project_id
        session_id = self.memory.get_or_create_session(session_id, effective_project_id)

        history = self.memory.get_anthropic_messages(session_id, limit=20)
        self.memory.save_message(
            session_id=session_id,
            role="user",
            content=user_message,
            project_id=effective_project_id,
        )

        messages: list[dict[str, Any]] = history + [{"role": "user", "content": user_message}]

        project_context = None
        if effective_project_id:
            try:
                project_context = self.memory.get_project_summary(effective_project_id)
            except Exception:
                pass
        system_prompt = get_system_prompt(project_context)

        tool_calls_made: list[str] = []
        iterations = 0
        full_response_parts: list[str] = []

        while iterations < _MAX_TOOL_ITERATIONS:
            iterations += 1

            # Use streaming for the final text turn
            tool_use_blocks: list[Any] = []
            text_parts: list[str] = []

            with self.client.messages.stream(
                model=self.model,
                max_tokens=4096,
                system=system_prompt,
                tools=TOOL_DEFINITIONS,
                messages=messages,
            ) as stream:
                # Collect the full response for tool processing
                final_message = stream.get_final_message()

            for block in final_message.content:
                if block.type == "text":
                    text_parts.append(block.text)
                elif block.type == "tool_use":
                    tool_use_blocks.append(block)

            if final_message.stop_reason == "end_turn" or not tool_use_blocks:
                # Stream the final text
                response_text = "\n".join(text_parts)
                full_response_parts.append(response_text)
                # Yield text chunks (simulate streaming in word chunks)
                words = response_text.split(" ")
                chunk = ""
                for word in words:
                    chunk += word + " "
                    if len(chunk) >= 50:
                        yield {"type": "text", "text": chunk}
                        chunk = ""
                if chunk:
                    yield {"type": "text", "text": chunk}
                break

            # Handle tool calls (not yet streamed to client, run synchronously)
            messages.append({"role": "assistant", "content": final_message.content})

            tool_results: list[dict[str, Any]] = []
            for tool_block in tool_use_blocks:
                tool_name = tool_block.name
                yield {"type": "tool_start", "tool_name": tool_name}
                tool_calls_made.append(tool_name)

                result = run_tool(
                    tool_name=tool_name,
                    tool_input=tool_block.input,
                    memory=self.memory,
                    knowledge_index=self.knowledge_index,
                )

                yield {"type": "tool_end", "tool_name": tool_name, "result": result[:500]}
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_block.id,
                        "content": result,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

        full_response = " ".join(full_response_parts)
        self.memory.save_message(
            session_id=session_id,
            role="assistant",
            content=full_response,
            project_id=effective_project_id,
        )

        yield {
            "type": "done",
            "session_id": session_id,
            "tool_calls_made": tool_calls_made,
        }

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def ingest_document(self, file_path: str, content: bytes | None = None) -> int:
        """Ingest a document into the knowledge base. Returns number of chunks added."""
        from knowledge_base.ingest import DocumentIngester

        ingester = DocumentIngester(self.knowledge_index)
        return ingester.ingest_file(file_path, content)

    def ingest_url(self, url: str) -> int:
        """Ingest a URL into the knowledge base."""
        from knowledge_base.ingest import DocumentIngester

        ingester = DocumentIngester(self.knowledge_index)
        return ingester.ingest_url(url)

    def ingest_text(self, text: str, source_name: str) -> int:
        """Ingest plain text into the knowledge base."""
        from knowledge_base.ingest import DocumentIngester

        ingester = DocumentIngester(self.knowledge_index)
        return ingester.ingest_text(text, source_name)

    def get_knowledge_stats(self) -> dict[str, Any]:
        """Return knowledge base statistics."""
        return self.knowledge_index.get_stats()

    def create_project(self, name: str, description: str | None = None) -> str:
        """Create a new project and return its ID."""
        return self.memory.create_project(name, description)

    def list_projects(self) -> list[dict[str, Any]]:
        """Return all projects."""
        return self.memory.list_projects()
