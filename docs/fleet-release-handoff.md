# Fleet release handoff

The release workflow still publishes k-fin images and its Helm chart. Its final
job proposes the chart pin in `max5800/home-lab` through a release branch and a
draft PR. It never commits to the default branch, merges a PR, dispatches an
infrastructure workflow, or claims that a deployment succeeded.

The existing `HOME_LAB_PAT` CI credential must allow Contents read/write and
Pull requests read/write in the target repository. A local assistant GitHub App
login does not configure Actions authentication. Read access cannot prove write
permission; a rejected write is reported with its HTTP status and operation,
without logging the raw API response or credential. No new secret is assumed.

The helper reads the target's current `main`, its `AGENTS.md`, and the exact
Fleet file at that immutable base. It changes only an explicit `helm.version`
scalar on a fixed `release/k-fin-v<version>` branch. It refuses downgrades,
unrecognized YAML, changed branches, and candidates with unrelated changes.
An unchanged candidate with an existing open PR is reused; a closed PR is not
reopened automatically. A branch created before an API failure is retained for
the protected delivery owner to inspect, never force-pushed or silently deleted.

Every normal helper result produces `fleet-handoff.json`, retained as an Actions
artifact, and a short job summary. Missing credentials, denied API operations,
or an unexpected target state keep the handoff job failed with `status=blocked`;
the already published release is not described as deployed. `pr_open` means
only that a draft PR exists. `pin_already_current` means only that Git already
contains the requested version. Neither establishes application health.

The delivery owner consumes the exact artifact and current target policy,
obtains the required independent review and checks, and uses the authorized
protected HomeLab PR/merge/deployment route. The artifact alone grants no new
authority. A generic HTTP 403 is not permission to change credentials or bypass
policy. If CI cannot open a PR, the existing authorized repository broker path
can continue from the recorded base/branch; reconcile partial effects first.

Offline tests cover mutation scope, candidate reuse, downgrade refusal,
conflicting branches and PRs, and sanitized failure output. They do not prove
that the current CI credential can create a PR or that Fleet can deploy it.
