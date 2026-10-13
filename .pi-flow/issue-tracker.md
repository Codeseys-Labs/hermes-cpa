# Issue tracker: GitHub

Repository: https://github.com/baladithyab/hermes-cpa. Visibility must remain private.

GitHub Issues own the new project's Wayfinder decisions. The existing Hermes Kanban cards remain historical evidence, not a second active decision tracker. Use linked issue titles when referring to maps and decisions.

## Operations

Use `gh --repo baladithyab/hermes-cpa` for issue commands, or `gh api repos/baladithyab/hermes-cpa/...` for REST operations. Create with `gh issue create`; read with `gh issue view --comments`; list with `gh issue list --json number,title,body,labels,assignees`; comment with `gh issue comment`; edit with `gh issue edit`; close with `gh issue close --comment`. Assign the acting owner before investigating a decision.

## Wayfinding operations

Keep one map issue labelled `wayfinder:map`. Decision issues have only an exact `## Question` in their initial body and one type label: `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling`, or `wayfinder:task`.

Create every currently precise decision first. Retrieve each issue's database id with `gh api repos/baladithyab/hermes-cpa/issues/<number> --jq .id`. Attach children through `POST repos/baladithyab/hermes-cpa/issues/<map>/sub_issues` with `sub_issue_id=<database-id>`.

Use native dependencies through `POST repos/baladithyab/hermes-cpa/issues/<blocked>/dependencies/blocked_by` with `issue_id=<blocker-database-id>`. If the API explicitly lacks those features, record child links in the map and use the documented `Part of` / `Blocked by` body convention. Do not create an unrelated local ticket convention.

The frontier is an open, unblocked, unassigned child in tracker order. Claim with `gh issue edit --add-assignee @me` before work. Record a resolution comment with evidence and tradeoffs, close the decision, and append its linked one-line result to the map's Decisions so far.

Implementation is allowed only where the map Notes explicitly authorize it. Decision tickets are questions, not build slices. Track unresolved product work without treating completed implementation subtasks as product acceptance.

## Publication boundary

Never push credentials, local homes, environment files, runtime databases, sockets, private logs, or validation archives. Source publication is not live deployment or release acceptance. Never force-push.
