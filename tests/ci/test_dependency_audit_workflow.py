import os
from pathlib import Path
import subprocess

import pytest


WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "security.yml"


def _dependency_audit_job() -> str:
    workflow = WORKFLOW.read_text()
    start = workflow.index("  dependency-audit:\n")
    return workflow[start:]


def _audit_command() -> str:
    job = _dependency_audit_job()
    step = job[job.index("      - name: Export locked requirements and run pip-audit\n") :]
    lines = step.splitlines()
    run_index = lines.index("        run: |")
    command_lines = []
    for line in lines[run_index + 1 :]:
        if not line.startswith("          "):
            break
        command_lines.append(line[10:])
    return "\n".join(command_lines)


def _write_executable(directory: Path, name: str, body: str) -> None:
    executable = directory / name
    executable.write_text("#!/bin/sh\n" + body)
    executable.chmod(0o755)


def _run_audit(
    tmp_path: Path,
    *,
    export_exit: int = 0,
    audit_exit: int = 0,
    install_auditor: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "pip-audit-invoked"
    _write_executable(
        bin_dir,
        "uv",
        f'printf "locked-dependency==1.0\\n"\nexit {export_exit}\n',
    )
    if install_auditor:
        _write_executable(
            bin_dir,
            "uvx",
            f'printf "invoked\\n" > "$AUDIT_MARKER"\nexit {audit_exit}\n',
        )

    env = os.environ.copy()
    env.update(
        {
            "AUDIT_MARKER": str(marker),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "RUNNER_TEMP": str(tmp_path),
        }
    )
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", _audit_command()],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, marker


def test_dependency_audit_succeeds_only_after_auditor_runs(tmp_path: Path) -> None:
    result, marker = _run_audit(tmp_path)

    assert result.returncode == 0
    assert marker.read_text() == "invoked\n"


def test_dependency_audit_stops_when_export_setup_fails(tmp_path: Path) -> None:
    result, marker = _run_audit(tmp_path, export_exit=23)

    assert result.returncode == 23
    assert not marker.exists()


@pytest.mark.parametrize("audit_exit", [1, 127])
def test_dependency_audit_fails_on_findings_or_tool_crash(
    tmp_path: Path, audit_exit: int
) -> None:
    result, marker = _run_audit(tmp_path, audit_exit=audit_exit)

    assert result.returncode == audit_exit
    assert marker.exists()


def test_dependency_audit_fails_when_auditor_is_absent(tmp_path: Path) -> None:
    result, marker = _run_audit(tmp_path, install_auditor=False)

    assert result.returncode == 127
    assert not marker.exists()


def test_dependency_audit_job_has_no_masking_or_skip_conditions() -> None:
    job = _dependency_audit_job()

    assert "continue-on-error:" not in job
    assert "if:" not in job
    assert "uv python install 3.13" in job
    assert "uv sync --dev" in job
    assert 'uvx pip-audit -r "${RUNNER_TEMP}/requirements.txt"' in job
