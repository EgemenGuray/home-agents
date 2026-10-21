# Agent code changes by pull request (E1.9, S2.2.3)

1. The coding subagent works in a CLI workspace on a branch named `agent/<job-id>`, writes tests, and opens a pull request with a plain-language summary.
2. CI runs the unit tests, `protected-paths` (fails if the branch touches anything in `PROTECTED_PATHS`), gitleaks, and for `home-assistant-config` changes, HA's `check_config`. A replay of recent requests against a staging copy is added in Phase 2 on the Studio.
3. The app shows the summary and diff; you approve with Face ID. The approval is signed by the phone (see `approvals.py`); the merge bot merges only with a valid signature.
4. `deploy/deploy.sh <tag>` deploys, runs health checks, and rolls back to the previous tag by itself if they fail.

The agent's GitHub credential is a fine-grained token limited to opening pull requests on these repos. It cannot merge, change branch protection, or push to `main`. Branch protection on `main`: pull request required, CODEOWNERS review required for protected paths, status checks required.
