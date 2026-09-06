"""Open a bounded Fleet pin PR; never merge it or update the default branch."""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

TARGET = "max5800/home-lab"
PIN_PATH = "kubernetes/apps/k-fin/app/fleet.yaml"
API = "https://api.github.com"
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SHA = re.compile(r"[0-9a-f]{40}")


class HandoffError(Exception):
    """A sanitized, actionable reason suitable for the handoff artifact."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHub:
    def __init__(self, token: str):
        self.token = token
        self.opener = build_opener(NoRedirect())
        self.last_operation = None

    def request(self, method: str, suffix: str, data=None, *, missing_ok=False):
        self.last_operation = f"{method} {suffix.split('?')[0]}"
        request = Request(
            f"{API}/repos/{TARGET}/{suffix}",
            data=json.dumps(data).encode() if data is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            if missing_ok and error.code == 404:
                return None
            # Never include token-bearing requests, private file contents or raw API errors.
            raise HandoffError(f"github_http_{error.code}") from None
        except (URLError, TimeoutError, ValueError):
            raise HandoffError("github_transport_or_response_error") from None


def checked_sha(value: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise HandoffError("invalid_git_identity")
    return value


def replace_pin(text: str, version: str) -> tuple[str, str]:
    """Surgically change one explicit helm.version; unfamiliar YAML needs review."""
    if not VERSION.fullmatch(version):
        raise HandoffError("invalid_stable_release_version")
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if re.fullmatch(r"helm:\s*(?:#[^\n]*)?\n?", line)]
    if len(starts) != 1:
        raise HandoffError("ambiguous_helm_block")
    start = starts[0] + 1
    end = next(
        (i for i in range(start, len(lines)) if re.match(r"[^\s#]", lines[i])),
        len(lines),
    )
    children = [
        line for line in lines[start:end] if line.strip() and not line.lstrip().startswith("#")
    ]
    if not children or any("\t" in line[: len(line) - len(line.lstrip())] for line in children):
        raise HandoffError("unsupported_helm_indentation")
    indent = min(len(line) - len(line.lstrip()) for line in children)
    indices = [i for i in range(start, end) if re.match(rf" {{{indent}}}version\s*:", lines[i])]
    if len(indices) != 1:
        raise HandoffError("ambiguous_helm_version")
    index = indices[0]
    match = re.fullmatch(
        r"( +version: *)([\"']?)([0-9]+\.[0-9]+\.[0-9]+)\2([ \t]*(?:#[^\n]*)?)(\r?\n)?",
        lines[index],
    )
    if not match or not VERSION.fullmatch(match[3]):
        raise HandoffError("unsupported_existing_version")
    old = match[3]
    if tuple(map(int, old.split("."))) > tuple(map(int, version.split("."))):
        raise HandoffError("release_superseded_by_current_pin")
    lines[index] = f"{match[1]}{match[2]}{version}{match[2]}{match[4]}{match[5] or ''}"
    return "".join(lines), old


def read_file(api, path: str, ref: str):
    item = api.request("GET", f"contents/{path}?ref={ref}")
    if item.get("type") != "file" or item.get("encoding") != "base64":
        raise HandoffError("unexpected_target_file")
    try:
        text = base64.b64decode(item["content"]).decode("utf-8")
    except (ValueError, UnicodeError, KeyError):
        raise HandoffError("invalid_target_content") from None
    return text, checked_sha(item.get("sha"))


def verify_candidate(api, base: str, head: str, expected: str):
    content, _ = read_file(api, PIN_PATH, head)
    comparison = api.request("GET", f"compare/{base}...{head}")
    files = comparison.get("files", [])
    if (
        content != expected
        or comparison.get("status") != "ahead"
        or len(files) != 1
        or files[0].get("filename") != PIN_PATH
        or files[0].get("status") != "modified"
    ):
        raise HandoffError("existing_branch_or_candidate_requires_review")


def create_handoff(api, version: str, receipt: dict):
    if not VERSION.fullmatch(version):
        raise HandoffError("invalid_stable_release_version")
    branch = f"release/k-fin-v{version}"
    receipt["branch"] = branch
    base = checked_sha(api.request("GET", "git/ref/heads/main")["object"]["sha"])
    receipt["base_sha"] = base
    _, policy_sha = read_file(api, "AGENTS.md", base)
    receipt["policy_blob_sha"] = policy_sha
    content, blob = read_file(api, PIN_PATH, base)
    expected, old = replace_pin(content, version)
    receipt["previous_version"] = old
    if expected == content:
        receipt["status"] = "pin_already_current"
        receipt["review_required"] = False
        return
    existing = api.request("GET", f"git/ref/heads/{branch}", missing_ok=True)
    if existing is None:
        api.request("POST", "git/refs", {"ref": f"refs/heads/{branch}", "sha": base})
        receipt["branch_created"] = True
        if checked_sha(api.request("GET", f"git/ref/heads/{branch}")["object"]["sha"]) != base:
            raise HandoffError("branch_changed_before_update")
        update = api.request(
            "PUT",
            f"contents/{PIN_PATH}",
            {
                "branch": branch,
                "sha": blob,
                "content": base64.b64encode(expected.encode()).decode(),
                "message": f"chore(release): pin k-fin to v{version}",
            },
        )
        head = checked_sha(update["commit"]["sha"])
    else:
        head = checked_sha(existing["object"]["sha"])
    receipt["head_sha"] = head
    verify_candidate(api, base, head, expected)
    prs = api.request("GET", f"pulls?state=all&head=max5800:{branch}&base=main&per_page=100")
    if len(prs) > 1 or (prs and prs[0].get("state") != "open"):
        raise HandoffError("existing_pull_request_requires_review")
    if checked_sha(api.request("GET", f"git/ref/heads/{branch}")["object"]["sha"]) != head:
        raise HandoffError("branch_changed_before_handoff")
    pr = (
        prs[0]
        if prs
        else api.request(
            "POST",
            "pulls",
            {
                "head": branch,
                "base": "main",
                "draft": True,
                "title": f"chore(release): pin k-fin v{version}",
                "body": (
                    f"Proposes only the Fleet chart pin for k-fin v{version}.\n\n"
                    f"Source commit: `{receipt['source_sha']}`. Candidate: `{head}`.\n\n"
                    "Independent review of the exact candidate, required checks and the target "
                    "repository's protected merge/deployment process are still required. "
                    "This workflow does not merge or verify deployment."
                ),
            },
        )
    )
    number = pr["number"]
    if type(number) is not int or number < 1:
        raise HandoffError("invalid_pull_request_identity")
    receipt["pr_number"] = number
    # Bind the final receipt to the actual PR, including reuse and races after POST.
    confirmed = api.request("GET", f"pulls/{number}")
    pr_base, pr_head = confirmed.get("base") or {}, confirmed.get("head") or {}
    if (
        confirmed.get("number") != number
        or confirmed.get("state") != "open"
        or confirmed.get("draft") is not True
        or (pr_base.get("repo") or {}).get("full_name") != TARGET
        or pr_base.get("ref") != "main"
        or pr_base.get("sha") != base
        or (pr_head.get("repo") or {}).get("full_name") != TARGET
        or pr_head.get("ref") != branch
        or pr_head.get("sha") != head
    ):
        raise HandoffError("pull_request_changed_or_requires_review")
    if (
        checked_sha(api.request("GET", f"git/ref/heads/{branch}")["object"]["sha"]) != head
        or checked_sha(api.request("GET", "git/ref/heads/main")["object"]["sha"]) != base
    ):
        raise HandoffError("base_or_branch_changed_after_handoff")
    receipt["status"] = "pr_open"


def main() -> int:
    receipt = {
        "schema": "k-fin/fleet-handoff/v1",
        "target_repository": TARGET,
        "path": PIN_PATH,
        "status": "blocked",
        "deployment_status": "not_verified",
        "review_required": True,
    }
    result = 1
    api = None
    try:
        version = os.environ.get("RELEASE_VERSION", "")
        if not VERSION.fullmatch(version):
            raise HandoffError("invalid_stable_release_version")
        receipt["version"] = version
        receipt["source_sha"] = checked_sha(os.environ.get("GITHUB_SHA", ""))
        token = os.environ.get("HOME_LAB_PAT", "")
        if not token:
            raise HandoffError("missing_HOME_LAB_PAT")
        api = GitHub(token)
        create_handoff(api, version, receipt)
        result = 0
    except HandoffError as error:
        receipt["reason"] = str(error)
        if api is not None:
            receipt["blocked_operation"] = api.last_operation
    except (KeyError, TypeError, IndexError, AttributeError):
        receipt["reason"] = "unexpected_github_response"
    output = Path("fleet-handoff.json")
    output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    summary = (
        f"Fleet handoff: **{receipt['status']}**. Deployment has not been verified.\n\n"
        "See the fleet-handoff artifact for the exact proposed change. "
        "If blocked, use the authorized HomeLab PR/review delivery path; "
        "do not retry with a direct push to main or bypass protections.\n"
    )
    if receipt.get("reason"):
        summary += f"\nReason: `{receipt['reason']}`.\n"
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(summary)
    print(summary)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
