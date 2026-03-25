from __future__ import annotations

from importlib.metadata import version as package_version
from pathlib import Path
import tomllib

import typer

from .server import main as run_server

app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["--help", "-h"]},
    pretty_exceptions_enable=False,
)


def _print_version() -> None:
    typer.echo(f"jira-mcp {package_version('jira-mcp')} ({_load_release_date()})")


def _load_release_date() -> str:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject_path.exists():
        raise RuntimeError(f"pyproject.toml not found at {pyproject_path}")

    with pyproject_path.open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    try:
        return pyproject["tool"]["jira_mcp"]["release_date"]
    except KeyError as exc:
        raise RuntimeError(
            "Missing tool.jira_mcp.release_date in pyproject.toml",
        ) from exc


@app.callback(invoke_without_command=True)
def cli(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        help="Show the application version and release date.",
        is_eager=True,
    ),
) -> None:
    if version:
        _print_version()
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        run_server()


def main() -> None:
    app()

if __name__ == "__main__":
    main()
