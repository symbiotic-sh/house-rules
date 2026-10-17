---
name: change-review
description: Review code, a commit, a diff, a branch or a pull request for defects with the House Rules reviewer pack (review bar, report format, code canon). Use whenever asked to review, check or look over a change, including a single commit or a small diff. A plain request gets one reviewer; a panel starts only when asked. Dispatching review rounds, fixing findings, pushing and merging use pr-ready.
license: MIT
---

# Change Review

Review the change yourself against the shared lane and Warden reviewer criteria.
This skill delivers the reviewer pack and adapts the pack to an interactive session.

## 1. Compile the reviewer pack

Find the House Rules root from the session's pointer to `<HOUSE_RULES_ROOT>/INDEX.md`.
Select a named House Rules commit for the review pack.
Use the dispatcher's commit when the dispatcher supplies one.
Otherwise, resolve the House Rules clone's HEAD to a commit ID before compilation:

```sh
git -C "<HOUSE_RULES_ROOT>" rev-parse HEAD
```

Keep that resolved ID as `<HOUSE_RULES_COMMIT>` for this review, including after compaction.
Compile the reviewer role with the session option for already-loaded shared rules:

```sh
python3 "<HOUSE_RULES_ROOT>/skills/pr-ready/scripts/prompt.py" --rev "<HOUSE_RULES_COMMIT>" --role reviewer --no-target --session
```

The [pack-assembly design](../../docs/design/77-one-step-pack-assembly.md) owns named-commit compilation.

- Read the whole compiler output before reviewing.
- Select any requested review lens from the same named commit.
  Compile the reviewer role and the selected `prompts/lenses/<lens>.md` together
  with the same `--rev` and `--session` options.
  Add `--lens "<lens>"` to the pack command, using the lens name without its path or extension.
- If compilation fails, report the error and stop the review.
  Do not substitute live checkout files or an incomplete pack.
- After compaction, compile the pack again from the same named commit.

## 2. Apply the pack in this session

- The session's already-loaded rules stay in force.
- Perform a read-only review and run at most one targeted test to resolve a specific suspected finding.
- The reviewer role's dispatched-agent loading instructions do not replace the normal session's loading rules.
- External writes require the operator's authorization and the session's external-write rules.
  A review request alone does not authorize a GitHub post.
- Use the fixed report format unless the operator asks for something shorter.
- Questions to the operator use
  [rules/session-writing.md §Questions to the operator](../../rules/session-writing.md#questions-to-the-operator)
  in every review.
- A plain review request gets one reviewer.
  Start a panel only when the operator asks for a panel, through pr-ready.

## 3. After the review

Fixing findings, pushing, further dispatched review rounds and merging continue in skill pr-ready.
