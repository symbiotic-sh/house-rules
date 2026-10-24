# Delivery rules

## Verification

- Add behavior-named red-to-green regression tests for product defects and documented invariants.
- Prove harness repairs with only the smallest evidence that restores trust.
- Prefer direct product-behavior evidence.
- Simplify or delete nonessential harnesses that cost more than the invariants they protect.
- Re-run reproductions to confirm fixes.
- Include test output with results.
- Reserve end-to-end claims for the real stack.
- Classify mocked-stack tests as integration tests.
- During iteration, run the smallest gate that proves the current change.
- Run the complete required gate on the resulting candidate or whenever changes invalidate prior full-gate evidence.
- Where CI owns the full suite, use CI’s run on the pushed head.
- Do not repeat the full matrix after changes that cannot affect it.
- Let CI own the full suite.
- Follow `../prompts/roles/implementer.md` and `../prompts/roles/reviewer.md` for local scopes.
- Follow `pr-ready` §4 for the no-PR-CI exception.
- Fail long-running checks on lack of progress, rather than elapsed time.
- Allow generous overall test ceilings only as runaway guards.
- Never use overall ceilings as the primary failure mode.
- Define a standard CI time for each repository.
- Investigate runs more than 20% over that time.
- Allow an expected long run once, including a rebuilt dependency cache.
- Have Warden review CI runs.
- Send failing jobs to a CI-repair investigator.
- Require a specialized agent to verify UI work visually with screenshots.
- Do not rely only on programmatic assertions.
- Follow `../prompts/skills/test-discipline.md` §Test discipline for what each test must prove, which browser automated tests and UI checks may start, tests for systems that keep durable state, duration, hanging-test removal, build-queue hold limits, and scale-test replacement.
- Follow `../skills/pr-ready/references/guards.md` for guard/test upkeep and daily whole-system audits.
- Make the cost, token and runtime ceilings of external or paid runs configurable.
- Leave those ceilings off while testing or debugging inside an approved task; the run's progress decides when it stops.
- In all other cases, set the ceilings the approved envelope names before launch.
- Apply these ceiling requirements only to external or paid work.
- Exempt routine short, cheap, reproducible commands from these expensive-work requirements.
- Keep paid, long-running, or non-reproducible external runs primary or proportionally supporting within approved envelopes.
- Examples include agents, model command-line interfaces, remote jobs, benchmarks, and crawls.
- Require a durable transcript or checkpoint before the first substantive call for those runs.
- Require a recorded session ID before that call.
- Record spend at milestones.
- Report each paid run's start, failure and finish to the operator at once.
- Include the run's identifier, its target and its spend so far in each report.
- Report retries and test runs the same way; no failed or new paid run goes unreported.
- Inspect process state before stopping runs because of silence or wrapper timeouts.
- Do not stop a paid run on your own while it progresses at a reasonable pace, unless a configured ceiling is reached or a safety requirement applies.
- Investigate a paid run that stops progressing, stop it early when it has stalled, and report the stop at once.
- Obtain operator approval before stopping progressing, materially paid work, except when a configured ceiling is reached or under rules/core.md §Operator correction or safety requirements.
- Follow `handoff-continuity` §Authorized, bounded, and durable external runs for procedure.

<a id="git"></a>
## Git

- Follow [Git rules](git.md).

## Parallel work

- Parallelize independent primary and supporting work when delegation is available and useful.
- Parallelize autonomously inside the resource envelope.
- Never broaden the campaign through parallelism.
- Keep lane ownership disjoint.
- Keep rules and policy in their home file.
- Use messages between sessions for tasks, status, or questions.
- To change a rule, edit its home file and tell running sessions which section to re-read.
- Before sending a correction, read what the receiver already did.
- Never send a second instruction on a subject while the first is still queued.
- Assign one lead to decide for each work item.
- Verify lane diffs stay within ownership.
- Keep shared spine files append-only.
- Run a rerunnable benchmark in the project's benchmark environment: by default a Linux container from Apple's `container` tool, with a fixed core count, memory size and recorded image.
- Use a macOS virtual machine with the same fixed setup only for a benchmark that measures macOS-specific behaviour.
- Run a benchmark's arms interleaved, one run of each arm per round in rotating order, and compare them by round.
  A valid stored baseline is reused instead of rerun, as `bench-discipline` allows; interleave only the arms that run.
- Run one benchmark or measured performance run at a time per machine, across all agents, lanes and sessions.
- Start no build or test suite of your own while your measured run is active.
- Record the machine load with each run.
- Mark a run taken under overlap with another benchmark, measured run, build or test suite invalid, and rerun it.
- The one exception: contention is the declared study and resource policy permits it.
- The load gate below, and holding the machine's build queue during a measured run, apply only to a measurement taken on a shared host, outside the benchmark environment.
- Before a benchmark or measured performance run on a shared host starts, check the machine load.
- When the one-minute load average is above the online core count, do not start the run.
  Send the operator a message at once that starts with this, in bold: the run you were about to start, the load, the core count and the heaviest processes with their working directories.
  Also send it as a notification when the tool has one.
- Start the run when the load falls below the core count, or when the operator says to start.
- Build and test commands do not check the load. The build queue limits them.
- On a shared machine, every build and test command goes through the machine's one build queue, shared by cargo and every other build tool: one such command at a time.
  A shared machine runs several agents at once, or a person uses it while agents work.
  The queue covers cargo, cmake builds, make, ninja, ctest, benchmarks, and test binaries or scripts run on their own.
- The queue gives each command the build budget as its thread limit: total cores minus efficiency cores minus 2, unless the operator sets another budget.
- Builds and agents run at normal priority. Never use nice, renice or background quality-of-service classes.
- Queue one command at a time. Never hold the queue while you edit, read output or wait for CI, a review or the operator.
- Review panels and other read-only agents do not take the build queue.
- Start reviewer command-line tools without the user's own tool configuration, so a reviewer starts no MCP server, plugin or hook. Reviewers need only read access.
- Point reviewers at a checkout without build output. A reviewer that searches a build tree loads the machine and finds nothing.
- Wait for a job your shell did not start by its process ID or by a file the job writes when it ends. Give that wait a deadline, and fail visibly when it passes.
- Never wait with `pgrep -f` on a pattern that your own command line contains. The pattern matches the waiting shell itself, so the wait never ends.
- Bound memory-heavy concurrency by verified host capacity and the approved resource envelope.
- Declare every touched repository in the work item.
- Flag shared kit or application programming interface impacts on other consumers in the handoff.
- Require one adversarial verification pass per deliverable.
- Count CI review, including Warden, as that pass when it covers the deliverable.
- Do not add a separate local pass when CI covers the deliverable.
- Follow `agent-lanes` for mechanics, and `agent-lanes` §Shared machine for the build queue commands.

## Work tracking & continuity

- Apply these invariants with any tracker.
- Follow `work-tracking` for default mechanics.
- Assign one owner per work item.
- Claim work before touching it.
- Treat a claim conflict as a signal to stop and coordinate.
- Assign one writer for state.
- Allow everyone else only to read state.
- Continue through immutable handoff records.
- Follow `handoff-continuity` for contents, triggers, and resuming.
- Derive boards and status views from tracker items.
- Never maintain status files by hand.
- The workspace tracker file under "Track every deferred item" is the one exception.

**Track every deferred item.**

- Make a work item in the same turn when you defer a requested outcome, an accepted finding or a promise to the operator.
- Use the workspace's tracker. By default, that is an issue in the GitHub repository that owns the change.
- If no repository exists for the item, add one entry to the workspace tracker file. The entry holds one item and its status.
- That file is the one exception to the status-file and two-tracker rules.
- Link the work item where you defer the work.
- Do not write "later", "I will file" or "a follow-up covers" without that link.
- Plans, documents, chat, reports, logs and unmerged branches record intent. They do not track work.
- When the operator repeats a request, search the tracker first. State whether the request was tracked.
- Ideas the operator did not request are proposals, not work items (rules/outcome.md §Outcome and resource contract).

- A requested outcome or accepted finding that the session does not finish becomes a work item in the tracker chosen above, before the session ends, linked from wherever it was set aside.
- That covers work that is deferred, "saved as a task", scoped out of another item, left as an audit gap, a plan or migration step, or said to "belong to the other repository's side".
- Accepted findings are findings the operator or a review accepted.

## Layout

- Use repository-relative paths in documentation and instructions.
- Derive script repository roots from script locations.
- Never use private paths in documentation or instructions.
- Follow the installed House Rules `STRUCTURE.md` for canonical paths, naming, and temp-storage classes.
- Default to regenerating derived views from source.
- Never hand-edit derived views.
- Keep generated files out of the repository's top-level directory.
- Default to keeping large artifacts untracked.
- Default to working from the repository root.
- Prefer path arguments over directory changes.
- Follow [rules/writing.md §Format](writing.md#format) for diagrams.
- Never change directories inside compound commands.
- Use `git -C` and absolute arguments.
- Re-anchor before relative-path writes.
- Verify no nested duplicate directories after bulk file creation.
- Always use absolute paths for file-tool reads, edits, writes, and searches.
