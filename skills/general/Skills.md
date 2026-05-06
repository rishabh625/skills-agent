---
name: create-pull-request
description: Create a standards-compliant pull request for GitOps repository changes with traceable evidence and rollback notes. Use when repository modifications are ready for review in Helm or Kustomize based deployments.
license: Apache-2.0
compatibility: Requires MCP connectivity to Git, Argo, and Kubernetes servers. No CLI tools required.
metadata:
  owner: platform-engineering
  category: change-control
  version: "1.0.0"
allowed-tools: mcp__git__* mcp__argo__* mcp__k8s__*
---

# Create Pull Request

## Inputs

- `repo`: Git repository identifier.
- `base_branch`: target branch for merge.
- `change_branch`: source branch with commits.
- `change_type`: `rollout-migration`, `image-update`, `policy-fix`, or `config-fix`.
- `application`: Argo CD application name.
- `manifest_mode`: `helm` or `kustomize`.
- `risk_level`: `low`, `medium`, or `high`.

## Outputs

- `pull_request`: PR URL or identifier.
- `pr_body`: generated summary, validation evidence, rollback plan.
- `checks`: list of required validation gates captured in PR.

## Tool Bindings

- **Git MCP**: read diff, create PR, assign reviewers/labels as available.
- **Argo MCP**: provide app health/sync context for PR narrative.
- **Kubernetes MCP**: provide runtime evidence relevant to risk/impact.

## Steps

1. Gather change context
   - Read branch diff and commit metadata with Git MCP.
   - Collect current app health and sync with Argo MCP.
2. Build PR narrative
   - Summarize why change is needed and expected behavior.
   - Include Helm/Kustomize specific impact statement.
3. Attach operational evidence
   - Add pre-change app status and relevant Kubernetes state.
   - Define explicit rollback conditions and rollback target.
4. Create PR using Git MCP
   - Use descriptive title and structured body.
   - Ensure reviewers and labels follow repository conventions.
5. Return PR artifacts
   - Emit PR URL, body, and checklist status.

## Guardrails

- Do not bypass PR creation with direct branch merges.
- Ensure rollback guidance is mandatory for production-bound changes.
- Reject PR generation if source branch has unrelated changes.

## Example Input

```json
{
  "repo": "acme/platform-apps",
  "base_branch": "main",
  "change_branch": "release/checkout-v2.14.3",
  "change_type": "image-update",
  "application": "checkout-prod",
  "manifest_mode": "helm",
  "risk_level": "medium"
}
```

## Example Output

```json
{
  "pull_request": "https://git.example.com/acme/platform-apps/pulls/130",
  "pr_body": "Updates checkout image tag to v2.14.3 with rollback to v2.14.2. Includes Argo health and rollout verification plan.",
  "checks": [
    "Argo app pre-change status captured",
    "Manifest mode impact documented",
    "Rollback path documented"
  ]
}
```
