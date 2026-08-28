from pathlib import Path


WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "security.yml"
AUDIT_STEP = "      - name: Export locked requirements and run pip-audit\n"
EXPECTED_SCRIPT = """\
set -euo pipefail
uv export --locked --format requirements-txt --no-emit-project --output-file ${RUNNER_TEMP}/requirements.txt
uvx --from pip-audit==2.10.1 pip-audit --no-deps --disable-pip -r ${RUNNER_TEMP}/requirements.txt"""


def _dependency_audit_job() -> str:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index("  dependency-audit:\n")
    return workflow[start:]


def _audit_script(job: str) -> str:
    step = job[job.index(AUDIT_STEP) :]
    lines = step.splitlines()
    run_index = lines.index("        run: |")
    command_lines: list[str] = []
    for line in lines[run_index + 1 :]:
        if not line.startswith("          "):
            break
        command_lines.append(line[10:])
    return "\n".join(command_lines)


def test_dependency_audit_uses_the_complete_locked_environment() -> None:
    job = _dependency_audit_job()

    assert "run: uv sync --locked --dev" in job
    assert _audit_script(job) == EXPECTED_SCRIPT
    assert "--no-dev" not in job
    assert "--no-group" not in job


def test_dependency_audit_has_no_bypass_ignore_or_masking_semantics() -> None:
    job = _dependency_audit_job()
    script = _audit_script(job)

    forbidden = (
        "continue-on-error:",
        "if:",
        "--ignore-vuln",
        "PIP_AUDIT_IGNORE_VULNS",
        " /dev/stdin",
        "||",
        "; true",
    )
    assert all(token not in job for token in forbidden)
    assert "|" not in "\n".join(script.splitlines()[1:])
