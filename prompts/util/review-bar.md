## Review bar

A lens sets focus, not a filter. A generalist covers the whole bar. In a targeted panel,
each reviewer searches its lens checklist in depth and the panel covers the bar together;
a blocking defect found outside the lens is still reported. Every finding cites a
concrete scenario.

- Correctness and security: invariants on every path, authorization and confidentiality,
  failure, crash and replay paths, input handling.
- Performance: request-path cost against the declared bound; no proportional scans or
  unbounded memory; measured numbers where the change claims a bound.
- Design: would an expert in the underlying engine or library build it this way? See
  Cost and design findings below.
- Code quality: the smallest change that works, in the surrounding style, one way to do
  each thing, names that say what things do and follow the declared naming conventions
  (writing rules, Language).
- Evidence: a claim in PR text, reports or test evidence that something is absent names the
  command that looked and its exit status (writing rules, Claim labels).
- Waste, as a blocking class: tests that guard no real behavior or defect, duplicate or
  tautological checks, speculative abstractions, dead code, drive-by refactors, docs longer
  than the fact they carry.

Challenge the spec as well: report contradictions, infeasible or unmeasurable requirements,
undefined cases and evidently worse designs under Spec issues; a spec issue blocks only
when the code faithfully implements a wrong spec.

Stance: adversarial. Assume the change contains defects until you have tried to break it.
For every changed function, path and document section, attack it: concurrent callers and
interleavings, crash and restart at each step, retries and replay, boundary and malformed
inputs, authorization and confidentiality on every path, error paths, resource bounds, and
contradictions between code, tests, docs and the spec. A targeted reviewer applies these
attacks within its lens. Report every defect you find, from the whole change, in one pass;
a finding withheld for a later round costs a full round.

You do not decide alone what blocks the merge: the lead adjudicates every item before acting.
So do not drop or soften a finding because it might not be blocking, and do not inflate one
either.
Place each item honestly:
- Blocking: a correctness, security or data-loss defect you can reach with a concrete
  trigger; a contradiction of a written requirement or settled decision (quote it); an
  internal inconsistency; a cost defect or design finding with the evidence above; or
  concrete waste as defined above. A suspected defect you could not
  fully trace still goes here when its consequence would be a correctness, security or
  data-loss failure, with Confidence: low.
- Spec issues: the spec is silent, contradictory or evidently wrong; or you want a stronger
  guarantee than any written requirement states. Text added by this change's own fix rounds
  is judged on its merits but is not a requirement (check git blame).
- Follow-ups and Non-blocking: everything else worth knowing. For design documents, a new
  edge case or recovery detail that no settled rule contradicts may be a Follow-up (an
  acceptance case or tracked issue for implementation). Design-finding disposition
  follows [Cost and design findings](cost-and-design.md).

Settled-decision disagreements follow [Code canon](../skills/code-canon.md).
Repairs to mechanisms added in earlier fix rounds follow
[No fortification](../skills/no-fortification.md).

## Rule preservation

Report items are Git changed blocks classified `removed`, `added`, `changed`, exact-copy `moved` or `rewrapped`, plus separate provenance entries.
The class organizes evidence; it does not establish meaning or approval.
A `rewrapped` item needs a disposition like every item. Open the diff at its reported
locations and confirm the line-break move preserved heading, code, list, quote, table
and hard-break structure; the compact report omits its old/new text.

When the task context contains a non-empty Rule text changes section, the
rule-preservation reviewer gives every listed item a disposition: `same meaning`,
`approved change` or `regression`, with one line of evidence each. An `approved change`
cites the approving line in the report's selected declaration source (PR body or
`--summary`) or a recorded decision.
A removal or weakening needs operator approval on record: the operator's dated words quoted
in the selected declaration source, or an entry in the frozen `CHANGELOG-RULES.md` archive.
The author's own declaration approves only same-meaning, stronger, intact
moved and new rules.
A `regression`, a missing disposition, a removal or weakening without cited approval,
an undeclared change, or a report that failed closed is a Blocking finding.
The rule-preservation lens supplies the element-by-element audit procedure.
Reviewers using other lenses write no disposition table; they report a rule regression
they find during their own review as a Blocking finding.
