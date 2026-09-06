"""PostgreSQL-based conversation and project memory for the NS-AI-Agent."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from knowledge_base import db

logger = logging.getLogger(__name__)


class AgentMemory:
    """Manages conversation history and project metadata in PostgreSQL."""

    def __init__(self, db_path: str | None = None) -> None:
        # db_path kept for API compatibility; DATABASE_URL via the shared pool is used.
        self.init_db()

    def _get_conn(self):
        return db.get_conn(register_vector_type=False)

    def _put_conn(self, conn):
        db.put_conn(conn)

    def _row_to_dict(self, cur, row) -> dict[str, Any]:
        cols = [desc[0] for desc in cur.description]
        return dict(zip(cols, row))

    def _rows_to_dicts(self, cur, rows) -> list[dict[str, Any]]:
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row)) for row in rows]

    def init_db(self) -> None:
        """Create tables if they don't exist."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS projects (
                        id                  TEXT        PRIMARY KEY,
                        name                TEXT        NOT NULL,
                        description         TEXT,
                        source_system       TEXT,
                        target_record_types TEXT,
                        created_at          TIMESTAMPTZ DEFAULT NOW(),
                        updated_at          TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        id          TEXT        PRIMARY KEY,
                        project_id  TEXT        REFERENCES projects(id),
                        created_at  TIMESTAMPTZ DEFAULT NOW(),
                        last_active TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id          SERIAL      PRIMARY KEY,
                        session_id  TEXT        NOT NULL,
                        project_id  TEXT        REFERENCES projects(id),
                        role        TEXT        NOT NULL,
                        content     TEXT        NOT NULL,
                        tool_name   TEXT,
                        tool_input  TEXT,
                        created_at  TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS project_notes (
                        id          SERIAL      PRIMARY KEY,
                        project_id  TEXT        NOT NULL REFERENCES projects(id),
                        note_type   TEXT        NOT NULL,
                        content     TEXT        NOT NULL,
                        created_at  TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS field_mappings (
                        id                    SERIAL  PRIMARY KEY,
                        project_id            TEXT    NOT NULL REFERENCES projects(id),
                        source_system         TEXT,
                        source_field          TEXT    NOT NULL,
                        netsuite_record_type  TEXT    NOT NULL,
                        netsuite_field        TEXT    NOT NULL,
                        transformation_notes  TEXT,
                        confidence            REAL    DEFAULT 1.0,
                        created_at            TIMESTAMPTZ DEFAULT NOW()
                    )
                """)
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project_id)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_project_notes_project ON project_notes(project_id)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_field_mappings_project ON field_mappings(project_id)"
                )
            conn.commit()
            logger.info("AgentMemory initialized (PostgreSQL)")
        finally:
            self._put_conn(conn)

    # -------------------------------------------------------------------------
    # Session management
    # -------------------------------------------------------------------------

    def create_session(self, project_id: str | None = None) -> str:
        session_id = str(uuid.uuid4())
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO sessions (id, project_id) VALUES (%s, %s)",
                    (session_id, project_id),
                )
            conn.commit()
        finally:
            self._put_conn(conn)
        logger.debug("Created session %s", session_id)
        return session_id

    def get_or_create_session(self, session_id: str | None, project_id: str | None = None) -> str:
        if session_id is None:
            return self.create_session(project_id)

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM sessions WHERE id = %s", (session_id,))
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        "INSERT INTO sessions (id, project_id) VALUES (%s, %s)",
                        (session_id, project_id),
                    )
                    logger.debug("Created session with provided id %s", session_id)
                else:
                    cur.execute(
                        "UPDATE sessions SET last_active = NOW() WHERE id = %s",
                        (session_id,),
                    )
            conn.commit()
        finally:
            self._put_conn(conn)
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
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO messages
                        (session_id, project_id, role, content, tool_name, tool_input)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (session_id, project_id, role, content, tool_name, tool_input),
                )
                row_id = cur.fetchone()[0]
            conn.commit()
        finally:
            self._put_conn(conn)
        return row_id

    def get_history(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return the last `limit` messages for a session in chronological order."""
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT role, content, tool_name, created_at
                    FROM (
                        SELECT role, content, tool_name, created_at, id
                        FROM messages
                        WHERE session_id = %s
                        ORDER BY id DESC
                        LIMIT %s
                    ) sub
                    ORDER BY sub.id ASC
                    """,
                    (session_id, limit),
                )
                rows = cur.fetchall()
        finally:
            self._put_conn(conn)
        return [
            {"role": r[0], "content": r[1], "tool_name": r[2], "created_at": r[3]}
            for r in rows
        ]

    def get_anthropic_messages(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return messages formatted for the Anthropic API messages list."""
        history = self.get_history(session_id, limit)
        return [
            {"role": msg["role"], "content": msg["content"]}
            for msg in history
            if msg["role"] in ("user", "assistant")
        ]

    # -------------------------------------------------------------------------
    # Project management
    # -------------------------------------------------------------------------

    def create_project(self, name: str, description: str | None = None) -> str:
        project_id = str(uuid.uuid4())
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO projects (id, name, description) VALUES (%s, %s, %s)",
                    (project_id, name, description),
                )
            conn.commit()
        finally:
            self._put_conn(conn)
        logger.info("Created project '%s' with id %s", name, project_id)
        return project_id

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM projects WHERE id = %s", (project_id,))
                row = cur.fetchone()
                if row is None:
                    return None
                return self._row_to_dict(cur, row)
        finally:
            self._put_conn(conn)

    def list_projects(self) -> list[dict[str, Any]]:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM projects ORDER BY created_at DESC")
                rows = cur.fetchall()
                return self._rows_to_dicts(cur, rows)
        finally:
            self._put_conn(conn)

    def update_project(
        self,
        project_id: str,
        name: str | None = None,
        description: str | None = None,
        source_system: str | None = None,
        target_record_types: list[str] | None = None,
    ) -> bool:
        updates = []
        params: list[Any] = []

        if name is not None:
            updates.append("name = %s")
            params.append(name)
        if description is not None:
            updates.append("description = %s")
            params.append(description)
        if source_system is not None:
            updates.append("source_system = %s")
            params.append(source_system)
        if target_record_types is not None:
            updates.append("target_record_types = %s")
            params.append(json.dumps(target_record_types))

        if not updates:
            return False

        updates.append("updated_at = NOW()")
        params.append(project_id)

        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE projects SET {', '.join(updates)} WHERE id = %s",
                    params,
                )
                updated = cur.rowcount > 0
            conn.commit()
        finally:
            self._put_conn(conn)
        return updated

    # -------------------------------------------------------------------------
    # Project notes
    # -------------------------------------------------------------------------

    def save_project_note(self, project_id: str, note_type: str, content: str) -> int:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO project_notes (project_id, note_type, content)
                    VALUES (%s, %s, %s)
                    RETURNING id
                    """,
                    (project_id, note_type, content),
                )
                row_id = cur.fetchone()[0]
            conn.commit()
        finally:
            self._put_conn(conn)
        return row_id

    def get_project_notes(
        self, project_id: str, note_type: str | None = None
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                if note_type:
                    cur.execute(
                        """
                        SELECT * FROM project_notes
                        WHERE project_id = %s AND note_type = %s
                        ORDER BY created_at DESC
                        """,
                        (project_id, note_type),
                    )
                else:
                    cur.execute(
                        """
                        SELECT * FROM project_notes
                        WHERE project_id = %s
                        ORDER BY created_at DESC
                        """,
                        (project_id,),
                    )
                rows = cur.fetchall()
                return self._rows_to_dicts(cur, rows)
        finally:
            self._put_conn(conn)

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
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO field_mappings
                        (project_id, source_system, source_field, netsuite_record_type,
                         netsuite_field, transformation_notes, confidence)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
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
                row_id = cur.fetchone()[0]
            conn.commit()
        finally:
            self._put_conn(conn)
        return row_id

    def get_field_mappings(
        self, project_id: str, record_type: str | None = None
    ) -> list[dict[str, Any]]:
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                if record_type:
                    cur.execute(
                        """
                        SELECT * FROM field_mappings
                        WHERE project_id = %s AND netsuite_record_type = %s
                        ORDER BY created_at DESC
                        """,
                        (project_id, record_type),
                    )
                else:
                    cur.execute(
                        """
                        SELECT * FROM field_mappings
                        WHERE project_id = %s
                        ORDER BY netsuite_record_type, source_field
                        """,
                        (project_id,),
                    )
                rows = cur.fetchall()
                return self._rows_to_dicts(cur, rows)
        finally:
            self._put_conn(conn)

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
            for note in notes[:10]:
                lines.append(f"  [{note['note_type']}] {note['content'][:200]}")

        if mappings:
            lines.append(f"\nSaved Field Mappings ({len(mappings)} total):")
            for mapping in mappings[:20]:
                lines.append(
                    f"  {mapping['source_field']} → "
                    f"{mapping['netsuite_record_type']}.{mapping['netsuite_field']}"
                )

        return "\n".join(lines)
