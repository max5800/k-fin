import json
from pathlib import Path


ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
RELEASE_CONFIG = ROOT / ".releaserc.json"
PACKAGE_MANIFEST = ROOT / "package.json"
PACKAGE_LOCK = ROOT / "package-lock.json"
REMOVED_PLUGINS = ("@semantic-release/changelog", "@semantic-release/git")
EXPECTED_PLUGINS = [
    "@semantic-release/commit-analyzer",
    "@semantic-release/release-notes-generator",
    "@semantic-release/github",
]


def _workflow_job(workflow: str, name: str, next_name: str | None = None) -> str:
    start = workflow.index(f"  {name}:\n")
    if next_name is None:
        return workflow[start:]
    end = workflow.index(f"\n  {next_name}:\n", start)
    return workflow[start:end]


def test_release_uses_only_non_committing_semantic_release_plugins() -> None:
    config = json.loads(RELEASE_CONFIG.read_text(encoding="utf-8"))

    assert config["branches"] == ["main", {"name": "develop", "prerelease": True}]
    assert config["plugins"] == EXPECTED_PLUGINS


def test_release_job_cannot_commit_or_push_to_k_fin() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    release_job = _workflow_job(workflow, "release", "publish")

    assert "persist-credentials: false" in release_job
    assert "extra_plugins:" not in release_job
    assert "git commit" not in release_job
    assert "git push" not in release_job


def test_fleet_handoff_has_no_direct_git_push_and_preserves_failure_artifact() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    fleet_job = _workflow_job(workflow, "update-fleet")

    assert "git commit " not in workflow
    assert "git push" not in workflow
    assert "repository: max5800/home-lab" not in fleet_job
    assert "persist-credentials: false" in fleet_job
    assert "contents: read" in fleet_job
    assert "python3 scripts/fleet_handoff.py" in fleet_job
    assert "if: always()" in fleet_job
    assert "path: fleet-handoff.json" in fleet_job
    assert "if-no-files-found: error" in fleet_job
    assert "continue-on-error" not in fleet_job


def test_removed_plugins_are_absent_from_manifest_and_lock() -> None:
    manifest = json.loads(PACKAGE_MANIFEST.read_text(encoding="utf-8"))
    lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
    manifest_dependencies = manifest["devDependencies"]
    lock_dependencies = lock["packages"][""]["devDependencies"]

    assert lock_dependencies == manifest_dependencies
    for plugin in REMOVED_PLUGINS:
        assert plugin not in manifest_dependencies
        assert f"node_modules/{plugin}" not in lock["packages"]
