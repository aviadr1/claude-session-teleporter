"""
Packaging invariants - see INVARIANTS.md, section "Packaging".

"Single file, standard library only" is the promise that lets someone curl this
onto a machine and run it. Until there was a pyproject.toml nothing could break
that by accident; now something can, so it is pinned here.
"""

from __future__ import annotations

import ast
import sys

from conftest import ROOT, cs

TOOL = ROOT / "claude_sessions.py"

try:
    import tomllib
except ModuleNotFoundError:  # 3.10, where tomli backfills it for the dev group
    import tomli as tomllib


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _imported_modules() -> set[str]:
    """Every top-level module name the tool imports, wherever the import sits."""
    tree = ast.parse(TOOL.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


# --------------------------------------------------------------------------
# P1 - standard library only
# --------------------------------------------------------------------------


def test_tool_imports_only_the_standard_library():
    """
    The whole install story is `curl` and run. A third-party import would break
    that for every user who is not installing from PyPI.
    """
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    foreign = {m for m in _imported_modules() if m not in stdlib}
    assert not foreign, (
        f"claude_sessions.py imports non-stdlib modules: {sorted(foreign)}. "
        "The tool must stay runnable as a bare file."
    )


def test_tool_is_a_single_file():
    """No sibling modules to forget when someone copies the script."""
    own = {m for m in _imported_modules() if (ROOT / f"{m}.py").exists()}
    assert not own, f"the tool imports sibling modules, so it is no longer one file: {sorted(own)}"


def test_tool_runs_from_a_bare_interpreter(tmp_path):
    """
    The end-to-end version of P1: copy the file somewhere with nothing else
    around it and run it with no package installed.
    """
    import shutil
    import subprocess

    lonely = tmp_path / "claude_sessions.py"
    shutil.copy(TOOL, lonely)
    out = subprocess.run(
        [sys.executable, str(lonely), "--version"],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert out.returncode == 0, out.stderr
    assert cs.__version__ in out.stdout


# --------------------------------------------------------------------------
# P2 - the package declares no runtime dependencies
# --------------------------------------------------------------------------


def test_no_runtime_dependencies():
    proj = _pyproject()["project"]
    assert proj.get("dependencies") == [], (
        "the published package must have no runtime dependencies; "
        f"found {proj.get('dependencies')}"
    )
    assert not proj.get("optional-dependencies"), (
        "optional runtime extras would make `pip install claude-session-teleporter` "
        "ambiguous about what you get"
    )


def test_test_dependencies_are_a_dev_group_not_runtime():
    """pytest is for us, not for users. It belongs in a dependency-group."""
    doc = _pyproject()
    assert "pytest" in str(doc.get("dependency-groups", {}).get("dev", []))
    assert "pytest" not in str(doc["project"].get("dependencies", []))


# --------------------------------------------------------------------------
# P3 - one version, in two places, kept equal
# --------------------------------------------------------------------------


def test_version_matches_the_module():
    """
    A release publishes the pyproject version but users see __version__. Letting
    them drift means bug reports quoting a version that was never released.
    """
    assert _pyproject()["project"]["version"] == cs.__version__


# --------------------------------------------------------------------------
# P4 - the console script actually resolves
# --------------------------------------------------------------------------


def test_console_script_entry_point_resolves():
    scripts = _pyproject()["project"]["scripts"]
    assert scripts == {"claude-sessions": "claude_sessions:main"}
    module, _, attr = scripts["claude-sessions"].partition(":")
    assert callable(getattr(cs, attr)), f"{module}:{attr} is not callable"


def _skill_command(skill: str, subcommand: str) -> str:
    """The command line the installed skill tells Claude to type for `subcommand`."""
    line = next(ln for ln in skill.splitlines() if ln.split("#")[0].rstrip().endswith(f" {subcommand}"))
    return line.split("#")[0].strip()


def test_skill_commands_run_from_any_directory(tmp_path):
    """
    P5, curl install: the skill is installed once and then used from whatever
    project Claude happens to be in. Run the tool by a relative path, as someone
    does right after `curl -o`, and the command it writes into the skill must
    still work from a different directory.
    """
    import shlex
    import shutil
    import subprocess

    tools = tmp_path / "tools"
    project = tmp_path / "some project"
    tools.mkdir()
    project.mkdir()
    shutil.copy(TOOL, tools / "claude_sessions.py")

    skill = subprocess.run(
        [sys.executable, "claude_sessions.py", "skill"],
        capture_output=True, text=True, encoding="utf-8", cwd=tools, check=True,
    ).stdout
    argv = shlex.split(_skill_command(skill, "partitions"))
    assert argv[0] == "python", argv
    # `python` may not be on PATH in CI; what matters is the script argument
    out = subprocess.run(
        [sys.executable, *argv[1:-1], "--version"], capture_output=True, text=True, cwd=project,
    )
    assert out.returncode == 0, f"skill command {argv} fails outside the tool's directory: {out.stderr}"
    assert "Bash(python *)" in skill


def test_skill_calls_the_console_script_by_name(monkeypatch, capsys, tmp_path):
    """
    P5, package install: `claude-sessions` is on PATH, so the skill calls it by
    name - not `python claude-sessions`, which looks for a file of that name in
    the current directory - and allows exactly that command.
    """
    for argv0 in ("claude-sessions", "claude-sessions.exe"):
        monkeypatch.setattr(sys, "argv", [str(tmp_path / "bin" / argv0), "skill"])
        assert cs.main(["skill"]) == 0
        skill = capsys.readouterr().out
        assert _skill_command(skill, "partitions") == "claude-sessions partitions"
        assert "Bash(claude-sessions *)" in skill
        assert "python claude-sessions" not in skill


def test_wheel_ships_the_tool_and_nothing_else():
    """A single-module wheel: the tests and docs are not part of the install."""
    included = _pyproject()["tool"]["hatch"]["build"]["targets"]["wheel"]["only-include"]
    assert included == ["claude_sessions.py"]
