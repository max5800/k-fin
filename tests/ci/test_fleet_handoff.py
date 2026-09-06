import base64
import copy
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location("fleet_handoff", ROOT / "scripts/fleet_handoff.py")
handoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(handoff)
BASE, HEAD, BLOB = "a" * 40, "b" * 40, "c" * 40
ORIGINAL = 'defaultNamespace: example\nhelm:\n  chart: k-fin\n  version: "1.2.2" # pin\n'
EXPECTED = ORIGINAL.replace('"1.2.2"', '"1.2.3"')
PR = {
    "number": 1,
    "state": "open",
    "draft": True,
    "base": {"repo": {"full_name": handoff.TARGET}, "ref": "main", "sha": BASE},
    "head": {
        "repo": {"full_name": handoff.TARGET},
        "ref": "release/k-fin-v1.2.3",
        "sha": HEAD,
    },
}


def file_response(text):
    return {
        "type": "file",
        "encoding": "base64",
        "sha": BLOB,
        "content": base64.b64encode(text.encode()).decode(),
    }


class FakeAPI:
    def __init__(self, *, existing=False, prs=None, unrelated=False, fail_pull=False):
        self.calls = []
        self.last_operation = None
        self.branch_sha = HEAD if existing else None
        self.content = EXPECTED if existing else ORIGINAL
        self.original = ORIGINAL
        self.prs = prs or []
        self.unrelated = unrelated
        self.fail_pull = fail_pull
        self.confirmed_pr = copy.deepcopy(PR)
        if self.prs:
            self.confirmed_pr["number"] = self.prs[0]["number"]
        self.after_pr_read = None
        self.base_sha = BASE

    def request(self, method, suffix, data=None, *, missing_ok=False):
        self.calls.append((method, suffix, data))
        self.last_operation = f"{method} {suffix.split('?')[0]}"
        if suffix == "git/ref/heads/main":
            self.assert_read(method)
            return {"object": {"sha": self.base_sha}}
        if suffix.startswith("git/ref/heads/release/k-fin-v"):
            self.assert_read(method)
            return {"object": {"sha": self.branch_sha}} if self.branch_sha else None
        if method == "POST" and suffix == "git/refs":
            assert data == {"ref": "refs/heads/release/k-fin-v1.2.3", "sha": BASE}
            self.branch_sha = BASE
            return {}
        if suffix.startswith("contents/AGENTS.md?"):
            self.assert_read(method)
            return file_response("# Target delivery policy\n")
        if suffix.startswith(f"contents/{handoff.PIN_PATH}"):
            if method == "PUT":
                assert data["branch"] == "release/k-fin-v1.2.3"
                assert data["sha"] == BLOB
                self.content = base64.b64decode(data["content"]).decode()
                self.branch_sha = HEAD
                return {"commit": {"sha": HEAD}}
            self.assert_read(method)
            return file_response(self.original if suffix.endswith(BASE) else self.content)
        if suffix.startswith("compare/"):
            self.assert_read(method)
            return {
                "status": "ahead",
                "files": [
                    {
                        "filename": "unrelated.yaml" if self.unrelated else handoff.PIN_PATH,
                        "status": "modified",
                    }
                ],
            }
        if suffix.startswith("pulls?"):
            self.assert_read(method)
            return self.prs
        if method == "POST" and suffix == "pulls":
            if self.fail_pull:
                raise handoff.HandoffError("github_http_403")
            return {"number": 1}
        if suffix.startswith("pulls/"):
            self.assert_read(method)
            assert suffix == f"pulls/{self.confirmed_pr['number']}"
            if self.after_pr_read:
                self.after_pr_read(self)
            return self.confirmed_pr
        raise AssertionError(f"Unexpected API operation: {method} {suffix}")

    @staticmethod
    def assert_read(method):
        assert method == "GET"

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


class FleetHandoffTests(unittest.TestCase):
    def test_fresh_candidate_writes_only_release_branch_and_opens_draft(self):
        api = FakeAPI()
        receipt = {"source_sha": BASE}
        handoff.create_handoff(api, "1.2.3", receipt)
        self.assertEqual(api.content, EXPECTED)
        self.assertEqual(receipt["status"], "pr_open")
        self.assertEqual(receipt["head_sha"], HEAD)
        self.assertEqual([call[0] for call in api.writes()], ["POST", "PUT", "POST"])
        self.assertEqual(api.writes()[-1][2]["base"], "main")
        self.assertTrue(api.writes()[-1][2]["draft"])
        self.assertIn(("GET", "pulls/1", None), api.calls)
        self.assertNotIn("merge", [call[1] for call in api.calls])

    def test_rerun_reuses_unchanged_candidate_without_new_writes(self):
        api = FakeAPI(existing=True, prs=[{"state": "open", "number": 42}])
        receipt = {"source_sha": BASE}
        handoff.create_handoff(api, "1.2.3", receipt)
        self.assertEqual(receipt["pr_number"], 42)
        self.assertIn(("GET", "pulls/42", None), api.calls)
        self.assertEqual(api.writes(), [])

    def test_final_pr_readback_rejects_changed_identity_or_review_state(self):
        changes = (
            ("state", "closed"),
            ("draft", False),
            ("base.repo.full_name", "other/repository"),
            ("base.ref", "other-base"),
            ("base.sha", "d" * 40),
            ("head.repo.full_name", "other/repository"),
            ("head.ref", "other-branch"),
            ("head.sha", "d" * 40),
        )
        for existing in (False, True):
            for field, value in changes:
                with self.subTest(existing=existing, field=field):
                    api = FakeAPI(
                        existing=existing,
                        prs=[{"state": "open", "number": 42}] if existing else None,
                    )
                    target = api.confirmed_pr
                    keys = field.split(".")
                    for key in keys[:-1]:
                        target = target[key]
                    target[keys[-1]] = value
                    receipt = {"source_sha": BASE, "status": "blocked"}
                    with self.assertRaisesRegex(handoff.HandoffError, "pull_request_changed"):
                        handoff.create_handoff(api, "1.2.3", receipt)
                    self.assertEqual(receipt["status"], "blocked")
                    self.assertEqual(receipt["pr_number"], 42 if existing else 1)
                    if existing:
                        self.assertEqual(api.writes(), [])

    def test_branch_or_base_race_after_post_fails_with_partial_artifact(self):
        for attribute in ("branch_sha", "base_sha"):
            with self.subTest(attribute=attribute):
                api = FakeAPI()
                api.after_pr_read = lambda current: setattr(current, attribute, "d" * 40)
                result, artifact = self.run_main(
                    api,
                    {
                        "RELEASE_VERSION": "1.2.3",
                        "GITHUB_SHA": BASE,
                        "HOME_LAB_PAT": "dummy-secret-never-print",
                    },
                )
                self.assertEqual(result, 1)
                self.assertEqual(artifact["status"], "blocked")
                self.assertEqual(artifact["reason"], "base_or_branch_changed_after_handoff")
                self.assertEqual(artifact["head_sha"], HEAD)
                self.assertEqual(artifact["pr_number"], 1)
                self.assertEqual(artifact["deployment_status"], "not_verified")

    def test_existing_unrelated_branch_changes_are_preserved_and_blocked(self):
        api = FakeAPI(existing=True, unrelated=True)
        with self.assertRaisesRegex(handoff.HandoffError, "requires_review"):
            handoff.create_handoff(api, "1.2.3", {"source_sha": BASE})
        self.assertEqual(api.writes(), [])

    def test_closed_pr_is_not_reopened(self):
        api = FakeAPI(existing=True, prs=[{"state": "closed", "number": 42}])
        with self.assertRaisesRegex(handoff.HandoffError, "requires_review"):
            handoff.create_handoff(api, "1.2.3", {"source_sha": BASE})
        self.assertEqual(api.writes(), [])

    def test_current_pin_is_not_a_deployment_claim(self):
        api = FakeAPI()
        api.original = EXPECTED
        receipt = {"deployment_status": "not_verified", "review_required": True}
        handoff.create_handoff(api, "1.2.3", receipt)
        self.assertEqual(receipt["status"], "pin_already_current")
        self.assertEqual(receipt["deployment_status"], "not_verified")
        self.assertFalse(receipt["review_required"])
        self.assertEqual(api.writes(), [])

    def test_stale_release_cannot_downgrade_current_main(self):
        api = FakeAPI()
        api.original = ORIGINAL.replace("1.2.2", "1.2.4")
        with self.assertRaisesRegex(handoff.HandoffError, "superseded"):
            handoff.create_handoff(api, "1.2.3", {})
        self.assertEqual(api.writes(), [])

    def test_pin_replacement_preserves_unrelated_content_and_comments(self):
        changed, old = handoff.replace_pin(ORIGINAL, "1.2.3")
        self.assertEqual(changed, EXPECTED)
        self.assertEqual(old, "1.2.2")

    def test_ambiguous_yaml_and_untrusted_versions_are_rejected(self):
        for text in (
            ORIGINAL + "helm:\n  version: 1.2.2\n",
            ORIGINAL + "  version: 1.2.2\n",
            "helm: {version: 1.2.2}\n",
        ):
            with self.subTest(text=text), self.assertRaises(handoff.HandoffError):
                handoff.replace_pin(text, "1.2.3")
        for version in ("../main", "1.2.3\nmain", "1.2.3; echo unsafe", "01.2.3", "1.2.3-rc.1"):
            with self.subTest(version=version), self.assertRaises(handoff.HandoffError):
                handoff.replace_pin(ORIGINAL, version)

    def run_main(self, api, env):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                with (
                    patch.dict(os.environ, env, clear=True),
                    patch.object(handoff, "GitHub", return_value=api),
                ):
                    result = handoff.main()
                artifact = json.loads(Path("fleet-handoff.json").read_text())
            finally:
                os.chdir(previous)
        return result, artifact

    def test_missing_credential_produces_actionable_artifact_without_api(self):
        api = FakeAPI()
        result, artifact = self.run_main(api, {"RELEASE_VERSION": "1.2.3", "GITHUB_SHA": BASE})
        self.assertEqual(result, 1)
        self.assertEqual(artifact["reason"], "missing_HOME_LAB_PAT")
        self.assertEqual(api.calls, [])

    def test_denied_pr_creation_preserves_partial_handoff_and_fails_job(self):
        api = FakeAPI(fail_pull=True)
        result, artifact = self.run_main(
            api,
            {
                "RELEASE_VERSION": "1.2.3",
                "GITHUB_SHA": BASE,
                "HOME_LAB_PAT": "dummy-secret-never-print",
            },
        )
        self.assertEqual(result, 1)
        self.assertEqual(artifact["status"], "blocked")
        self.assertEqual(artifact["deployment_status"], "not_verified")
        self.assertEqual(artifact["blocked_operation"], "POST pulls")
        self.assertEqual(artifact["head_sha"], HEAD)
        self.assertTrue(artifact["branch_created"])
        self.assertNotIn("dummy-secret", json.dumps(artifact))

    def test_transport_errors_do_not_expose_raw_details(self):
        api = handoff.GitHub("dummy-secret-never-print")
        error = HTTPError("https://api.github.com", 403, "private response", {}, None)
        with patch.object(api.opener, "open", side_effect=error):
            with self.assertRaisesRegex(handoff.HandoffError, "^github_http_403$"):
                api.request("GET", "git/ref/heads/main")

    def test_redirects_cannot_forward_the_credential(self):
        self.assertIsNone(
            handoff.NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://example.invalid"
            )
        )


if __name__ == "__main__":
    unittest.main()
