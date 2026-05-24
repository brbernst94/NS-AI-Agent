"""Click CLI for NS-AI-Agent: chat, ingest, validate, transform, projects."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# CLI Group
# ---------------------------------------------------------------------------

@click.group()
@click.option("--data-dir", default=None, envvar="DATA_DIR", help="Data directory path.")
@click.pass_context
def cli(ctx: click.Context, data_dir: str | None) -> None:
    """NS-AI-Agent: NetSuite Data Migration AI Agent CLI."""
    ctx.ensure_object(dict)
    ctx.obj["data_dir"] = data_dir or os.getenv("DATA_DIR", "./data")


# ---------------------------------------------------------------------------
# Chat command
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--project-id", "-p", default=None, help="Project ID for context.")
@click.option("--session-id", "-s", default=None, help="Existing session ID to continue.")
@click.pass_context
def chat(ctx: click.Context, project_id: str | None, session_id: str | None) -> None:
    """Interactive REPL chat with the NetSuite Migration AI Agent."""
    from agent.core import NSMigrationAgent

    data_dir = ctx.obj["data_dir"]
    agent = NSMigrationAgent(project_id=project_id, data_dir=data_dir)

    click.echo(click.style("NS-AI-Agent: NetSuite Migration Assistant", fg="cyan", bold=True))
    click.echo(click.style("Type 'exit' or 'quit' to end the session.", fg="white"))
    if project_id:
        click.echo(click.style(f"Project: {project_id}", fg="green"))
    click.echo("")

    current_session = session_id

    while True:
        try:
            user_input = click.prompt(click.style("You", fg="blue", bold=True))
        except (EOFError, KeyboardInterrupt):
            click.echo("\nGoodbye!")
            break

        if user_input.strip().lower() in ("exit", "quit", "q"):
            click.echo("Goodbye!")
            break

        if not user_input.strip():
            continue

        click.echo(click.style("Agent: ", fg="green", bold=True), nl=False)

        try:
            result = agent.chat(
                user_message=user_input,
                session_id=current_session,
                project_id=project_id,
            )
            current_session = result["session_id"]

            click.echo(result["response"])
            click.echo("")

        except Exception as exc:
            click.echo(click.style(f"Error: {exc}", fg="red"))
            click.echo("")


# ---------------------------------------------------------------------------
# Ingest commands
# ---------------------------------------------------------------------------

@cli.group()
def ingest() -> None:
    """Ingest documents into the knowledge base."""


@ingest.command("file")
@click.argument("path", type=click.Path(exists=True))
@click.pass_context
def ingest_file(ctx: click.Context, path: str) -> None:
    """Ingest a file (PDF, Word, CSV, Excel, or text) into the knowledge base."""
    from agent.core import NSMigrationAgent

    data_dir = ctx.obj["data_dir"]
    agent = NSMigrationAgent(data_dir=data_dir)

    click.echo(f"Ingesting file: {path}")
    with click.progressbar(length=1, label="Processing") as bar:
        chunks = agent.ingest_document(path)
        bar.update(1)

    click.echo(click.style(f"Done! {chunks} chunks added to knowledge base.", fg="green"))


@ingest.command("url")
@click.argument("url")
@click.pass_context
def ingest_url(ctx: click.Context, url: str) -> None:
    """Fetch a URL and ingest its content into the knowledge base."""
    from agent.core import NSMigrationAgent

    data_dir = ctx.obj["data_dir"]
    agent = NSMigrationAgent(data_dir=data_dir)

    click.echo(f"Fetching and ingesting: {url}")
    chunks = agent.ingest_url(url)
    click.echo(click.style(f"Done! {chunks} chunks added to knowledge base.", fg="green"))


@ingest.command("text")
@click.argument("text")
@click.option("--source", "-s", required=True, help="Source name/label for this content.")
@click.pass_context
def ingest_text(ctx: click.Context, text: str, source: str) -> None:
    """Ingest plain text into the knowledge base."""
    from agent.core import NSMigrationAgent

    data_dir = ctx.obj["data_dir"]
    agent = NSMigrationAgent(data_dir=data_dir)

    chunks = agent.ingest_text(text, source)
    click.echo(click.style(f"Done! {chunks} chunks added from '{source}'.", fg="green"))


# ---------------------------------------------------------------------------
# Validate command
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("csv_path", type=click.Path(exists=True))
@click.option("--record-type", "-r", required=True, help="NetSuite record type (customer, vendor, inventoryitem, etc.)")
@click.option("--output", "-o", default=None, help="Write validation report to this file.")
def validate(csv_path: str, record_type: str, output: str | None) -> None:
    """Validate a migration CSV file against a NetSuite record type."""
    from data_tools.validator import CSVValidator

    click.echo(f"Validating '{csv_path}' as {record_type}...")
    validator = CSVValidator()
    result = validator.validate(csv_path, record_type)
    summary = result.summary()

    click.echo("")
    click.echo(summary)

    # Colour coding
    if result.errors:
        click.echo(click.style(f"\n{len(result.errors)} validation error(s) found.", fg="red", bold=True))
    else:
        click.echo(click.style("\nNo validation errors.", fg="green", bold=True))
    if result.warnings:
        click.echo(click.style(f"{len(result.warnings)} warning(s) to review.", fg="yellow"))

    if output:
        result_dict = result.to_dict()
        Path(output).write_text(json.dumps(result_dict, indent=2))
        click.echo(f"\nValidation report written to: {output}")


# ---------------------------------------------------------------------------
# Transform command
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("csv_path", type=click.Path(exists=True))
@click.option("--record-type", "-r", required=True, help="Target NetSuite record type.")
@click.option("--output", "-o", default=None, help="Output file path.")
@click.option("--source-system", "-s", default="generic", help="Source system (salesforce, quickbooks, sap, dynamics, generic).")
@click.option("--mapping-file", "-m", default=None, type=click.Path(exists=True), help="JSON file with field mapping {source_col: ns_field}.")
def transform(
    csv_path: str,
    record_type: str,
    output: str | None,
    source_system: str,
    mapping_file: str | None,
) -> None:
    """Transform a source CSV into a NetSuite Import-ready file."""
    import pandas as pd
    from data_tools.transformer import DataTransformer
    from data_tools.mapper import FieldMapper

    src = Path(csv_path)
    out_path = output or str(src.parent / f"{src.stem}_netsuite_import.csv")

    click.echo(f"Loading '{csv_path}'...")
    df = pd.read_csv(csv_path, dtype=str).fillna("")

    if mapping_file:
        with open(mapping_file) as f:
            mapping = json.load(f)
        click.echo(f"Using mapping file: {mapping_file}")
    else:
        click.echo(f"Auto-detecting field mappings (source system: {source_system})...")
        mapper = FieldMapper()
        suggestions = mapper.suggest_mapping(list(df.columns), source_system, record_type)
        mapping = {src_col: info["netsuite_field"] for src_col, info in suggestions.items()}

        click.echo("\nSuggested mappings:")
        click.echo(mapper.format_mapping_table(suggestions))
        click.echo("")

    transformer = DataTransformer()
    transformed = transformer.transform(df, mapping, record_type)
    out = transformer.generate_netsuite_csv(transformed, record_type, out_path)

    click.echo(click.style(f"\nTransform complete!", fg="green", bold=True))
    click.echo(f"  Input rows  : {len(df)}")
    click.echo(f"  Output rows : {len(transformed)}")
    click.echo(f"  Columns     : {', '.join(transformed.columns)}")
    click.echo(f"  Output file : {out}")


# ---------------------------------------------------------------------------
# Projects commands
# ---------------------------------------------------------------------------

@cli.group()
def projects() -> None:
    """Manage migration projects."""


@projects.command("list")
@click.pass_context
def projects_list(ctx: click.Context) -> None:
    """List all migration projects."""
    from agent.memory import AgentMemory

    data_dir = ctx.obj["data_dir"]
    memory = AgentMemory(db_path=Path(data_dir) / "agent_memory.db")
    project_list = memory.list_projects()

    if not project_list:
        click.echo("No projects found. Create one with: ns-agent projects create NAME")
        return

    click.echo(click.style(f"{'ID':<38} {'Name':<30} {'Source System':<20} {'Created'}", bold=True))
    click.echo("-" * 100)
    for p in project_list:
        click.echo(
            f"{p['id']:<38} {p['name']:<30} {p.get('source_system', 'N/A'):<20} {p.get('created_at', '')[:10]}"
        )


@projects.command("create")
@click.argument("name")
@click.option("--description", "-d", default=None, help="Project description.")
@click.option("--source-system", "-s", default=None, help="Source system name.")
@click.pass_context
def projects_create(
    ctx: click.Context,
    name: str,
    description: str | None,
    source_system: str | None,
) -> None:
    """Create a new migration project."""
    from agent.memory import AgentMemory

    data_dir = ctx.obj["data_dir"]
    memory = AgentMemory(db_path=Path(data_dir) / "agent_memory.db")
    project_id = memory.create_project(name, description)

    if source_system:
        memory.update_project(project_id, source_system=source_system)

    click.echo(click.style(f"Project created!", fg="green", bold=True))
    click.echo(f"  Name   : {name}")
    click.echo(f"  ID     : {project_id}")
    if description:
        click.echo(f"  Desc   : {description}")
    if source_system:
        click.echo(f"  Source : {source_system}")
    click.echo(f"\nUse --project-id {project_id} in the chat command to load this project.")


@projects.command("show")
@click.argument("project_id")
@click.pass_context
def projects_show(ctx: click.Context, project_id: str) -> None:
    """Show project details, notes, and field mappings."""
    from agent.memory import AgentMemory

    data_dir = ctx.obj["data_dir"]
    memory = AgentMemory(db_path=Path(data_dir) / "agent_memory.db")

    project = memory.get_project(project_id)
    if project is None:
        click.echo(click.style(f"Project '{project_id}' not found.", fg="red"))
        sys.exit(1)

    summary = memory.get_project_summary(project_id)
    click.echo(summary)


# ---------------------------------------------------------------------------
# Knowledge stats command
# ---------------------------------------------------------------------------

@cli.command()
@click.pass_context
def kb_stats(ctx: click.Context) -> None:
    """Show knowledge base statistics."""
    from knowledge_base.index import KnowledgeIndex

    data_dir = ctx.obj["data_dir"]
    index = KnowledgeIndex(data_dir=data_dir)
    stats = index.get_stats()

    click.echo(click.style("Knowledge Base Statistics", bold=True))
    click.echo(f"  Total documents : {stats['total_documents']}")
    if stats.get("sources"):
        click.echo("\n  Documents per source:")
        for src, count in sorted(stats["sources"].items(), key=lambda x: -x[1]):
            click.echo(f"    {count:>6}  {src}")
    else:
        click.echo("  No documents ingested yet.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cli(obj={})


if __name__ == "__main__":
    main()
