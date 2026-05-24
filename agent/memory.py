"""SQLite-based conversation and project memory for the NS-AI-Agent."""

import json
import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AgentMemory:
    """Manages conversation history and project metadata in SQLite."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            data_dir = Path("./data")
            data_dir.mkdir(parents=True, exist_ok=True)
            db_path = data_dir / "agent_memory.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def _get_connection(self) -> sqlite3.Connection:
        """Get a SQLite connection with row factory."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init_db(self) -> None:
        """Create SQLite tables if they don't exist."""
        schema_path = Path(__file__).parent.parent / "db" / "schema.sql"
        if schema_path.exists():
            with open(schema_path) as f:
                schema_sql = f.read()
        else:
            schema_sql = self._inline_schema()

        with self._get_connection() as conn:
            conn.executescript(schema_sql)
        logger.info("Database initialized at %s", self.db_path)

    def _inline_schema(self) -> str:
        """Inline schema as fallback if schema.sql not found."""
        return """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            source_system TEXT,
            target_record_types TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT REFERENCES projects(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            project_id TEXT REFERENCES projects(id),
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            tool_name TEXT,
            tool_input TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS project_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id),
            note_type TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS field_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id),
            source_system TEXT,
            source_field TEXT NOT NULL,
            netsuite_record_type TEXT NOT NULL,
            netsuite_field TEXT NOT NULL,
            transformation_notes TEXT,
            confidence REAL DEFAULT 1.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
        CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project_id);
        CREATE INDEX IF NOT EXISTS idx_project_notes_project ON project_notes(project_id);
        CREATE INDEX IF NOT EXISTS idx_field_mappings_project ON field_mappings(project_id);
        """

    # -------------------------------------------------------------------------
    # Session management
    # -------------------------------------------------------------------------

    def create_session(self, project_id: str | None = None) -> str:
        """Create a new session and return its ID."""
        session_id = str(uuid.uuid4())
        with self._get_connection() as conn:
            conn.execute(
                "INSERT INTO sessions (id, project_id) VALUES (?, ?)",
                (session_id, project_id),
            )
        logger.debug("Created session %s", session_id)
        return session_id

    def get_or_create_session(self, session_id: str | None, project_id: str | None = None) -> str:
        """Return existing session_id or create a new one."""
        if session_id is None:
            return self.create_session(project_id)

        with self._get_connection() as conn:
            row = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO sessions (id, project_id) VALUES (?, ?)",
                    (session_id, project_id),
                )
                logger.debug("Created session with provided id %s", session_id)
            else:
                conn.execute(
                    "UPDATE sessions SET last_active = CURRENT_TIMESTAMP WHERE id = ?",
                    (session_id,),
                )
        return session_id

    # -------------------------------------------------------------------------
    # Message management
    # -------------------------------------------------------------------------

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        project_id: str | None = None,
        tool_name: str | None = None,
        tool_input: str | None = None,
    ) -> int:
        """Save a message and return its row ID."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO messages (session_id, project_id, role, content, tool_name, tool_input)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, project_id, role, content, tool_name, tool_input),
            )
            return cursor.lastrowid

    def get_history(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return the last `limit` messages for a session as list of {role, content}."""
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT role, content, tool_name, created_at
                FROM messages
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()

        messages = []
        for row in reversed(rows):
            messages.append(
                {
                    "role": row["role"],
                    "content": row["content"],
                    "tool_name": row["tool_name"],
                    "created_at": row["created_at"],
                }
            )
        return messages

    def get_anthropic_messages(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return messages formatted for the Anthropic API messages list."""
        history = self.get_history(session_id, limit)
        anthropic_messages = []
        for msg in history:
            if msg["role"] in ("user", "assistant"):
                anthropic_messages.append({"role": msg["role"], "content": msg["content"]})
        return anthropic_messages

    # -------------------------------------------------------------------------
    # Project management
    # -------------------------------------------------------------------------

    def create_project(self, name: str, description: str | None = None) -> str:
        """Create a new project and return its ID."""
        project_id = str(uuid.uuid4())
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO projects (id, name, description)
                VALUES (?, ?, ?)
                """,
                (project_id, name, description),
            )
        logger.info("Created project '%s' with id %s", name, project_id)
        return project_id

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        """Return project metadata as a dict, or None if not found."""
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if row is None:
                return None
            return dict(row)

    def list_projects(self) -> list[dict[str, Any]]:
        """Return all projects as a list of dicts."""
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def update_project(
        self,
        project_id: str,
        name: str | None = None,
        description: str | None = None,
        source_system: str | None = None,
        target_record_types: list[str] | None = None,
    ) -> bool:
        """Update project fields. Returns True if project was found and updated."""
        updates = []
        params: list[Any] = []

        if name is not None:
            updates.append("name = ?")
            params.append(name)
        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if source_system is not None:
            updates.append("source_system = ?")
            params.append(source_system)
        if target_record_types is not None:
            updates.append("target_record_types = ?")
            params.append(json.dumps(target_record_types))

        if not updates:
            return False

        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(project_id)

        with self._get_connection() as conn:
            cursor = conn.execute(
                f"UPDATE projects SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            return cursor.rowcount > 0

    # -------------------------------------------------------------------------
    # Project notes
    # -------------------------------------------------------------------------

    def save_project_note(
        self, project_id: str, note_type: str, content: str
    ) -> int:
        """Save a note to a project and return its row ID."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO project_notes (project_id, note_type, content)
                VALUES (?, ?, ?)
                """,
                (project_id, note_type, content),
            )
            return cursor.lastrowid

    def get_project_notes(
        self, project_id: str, note_type: str | None = None
    ) -> list[dict[str, Any]]:
        """Return all notes for a project, optionally filtered by type."""
        with self._get_connection() as conn:
            if note_type:
                rows = conn.execute(
                    """
                    SELECT * FROM project_notes
                    WHERE project_id = ? AND note_type = ?
                    ORDER BY created_at DESC
                    """,
                    (project_id, note_type),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM project_notes
                    WHERE project_id = ?
                    ORDER BY created_at DESC
                    """,
                    (project_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    # -------------------------------------------------------------------------
    # Field mappings
    # -------------------------------------------------------------------------

    def save_field_mapping(
        self,
        project_id: str,
        source_field: str,
        netsuite_record_type: str,
        netsuite_field: str,
        source_system: str | None = None,
        transformation_notes: str | None = None,
        confidence: float = 1.0,
    ) -> int:
        """Save a field mapping decision and return its row ID."""
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO field_mappings
                    (project_id, source_system, source_field, netsuite_record_type,
                     netsuite_field, transformation_notes, confidence)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    source_system,
                    source_field,
                    netsuite_record_type,
                    netsuite_field,
                    transformation_notes,
                    confidence,
                ),
            )
            return cursor.lastrowid

    def get_field_mappings(
        self, project_id: str, record_type: str | None = None
    ) -> list[dict[str, Any]]:
        """Return field mappings for a project."""
        with self._get_connection() as conn:
            if record_type:
                rows = conn.execute(
                    """
                    SELECT * FROM field_mappings
                    WHERE project_id = ? AND netsuite_record_type = ?
                    ORDER BY created_at DESC
                    """,
                    (project_id, record_type),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM field_mappings
                    WHERE project_id = ?
                    ORDER BY netsuite_record_type, source_field
                    """,
                    (project_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def get_project_summary(self, project_id: str) -> str:
        """Return a formatted text summary of a project for injection into system prompt."""
        project = self.get_project(project_id)
        if not project:
            return f"Project {project_id} not found."

        notes = self.get_project_notes(project_id)
        mappings = self.get_field_mappings(project_id)

        lines = [
            f"Project: {project['name']}",
            f"Description: {project.get('description', 'N/A')}",
            f"Source System: {project.get('source_system', 'Not specified')}",
        ]

        if project.get("target_record_types"):
            try:
                record_types = json.loads(project["target_record_types"])
                lines.append(f"Target Record Types: {', '.join(record_types)}")
            except (json.JSONDecodeError, TypeError):
                pass

        if notes:
            lines.append(f"\nProject Notes ({len(notes)} total):")
            for note in notes[:10]:  # Show most recent 10
                lines.append(f"  [{note['note_type']}] {note['content'][:200]}")

        if mappings:
            lines.append(f"\nSaved Field Mappings ({len(mappings)} total):")
            for mapping in mappings[:20]:  # Show up to 20
                lines.append(
                    f"  {mapping['source_field']} → {mapping['netsuite_record_type']}.{mapping['netsuite_field']}"
                )

        return "\n".join(lines)
