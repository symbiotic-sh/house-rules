# Writing rules

The reader must understand the text without any other document. They should know what
happened, why it matters and what to do, in that order.

## Self-contained text

- YOU MUST make every message stand alone. Never send the reader to find text elsewhere.
  Examples: "my last message", "the questions above", "as discussed earlier" or "see the PR".
- To mention an earlier question, decision or finding, restate it in full in this message.
  Otherwise, leave it out.
- A count or label ("the 11 questions", "question 4", "9a-1") never replaces the items it
  names.

<a id="communication-rules"></a>
## Communication rules

- Support claims with evidence.
- Distinguish observed causes from hypotheses.
- Distinguish completed fixes from plans and deployments awaiting verification.

## Language

- One fact per sentence. Prefer short sentences; split one that carries more than one fact.
  Length is never a reason to drop content.
- Each paragraph develops one point. Connect its sentences through cause and effect,
  condition and consequence, or claim and example. Put unrelated assertions in a list,
  not a paragraph. Short related sentences stay in one paragraph.
- Active voice, simple tense. Name who does what.
- One term per concept and one meaning per term. Define it once at first use, then never
  use a synonym or reuse its accepted form for another concept.
- The same concept keeps the same name in documents, code and user interfaces.
- Every specification and every codebase declares one naming convention for each kind of
  public name it defines: types, fields, error types and error reasons, settings, events
  and commands.
- Each convention states casing, required prefixes or suffixes, and word order.
  Example: "error reasons are lower snake case, subject first: `quota_exceeded`, never
  `exceeded_quota` or `QuotaExceeded`."
- Declare the conventions when a specification or codebase starts, and in an existing one
  before work adds or renames a public name.
- A codebase records its conventions in its bible. A specification records them as the
  specification rules require.
- A codebase whose bible adopts the House Rules naming defaults declares only its overrides
  there. A specification still records its complete conventions as the specification
  rules require.
- A name spelled two ways is a defect. Rename every use to the declared convention; never
  keep both spellings as accepted names. When consumers depend on the old spelling, the
  project's compatibility rules govern its removal.
- A name carries meaning without filler words such as `Info`, `Manager`, `Helper` or `Util`.
- No unexplained acronyms, internal labels or IDs as a replacement for meaning. An ID may
  follow the plain description, never replace it.
- In newly designed or redesigned internal storage, schemas and protocols, use readable
  words for collection, column, field, row-kind and key-prefix names. A glossary does not
  make an invented abbreviation a readable name. Name a shared slot by its value kind,
  indexing role and position; state a field's mapping to that slot for its record type.
- Preserve exact external API names and identifiers when citing existing code. Explain
  them where needed. Standard terms and symbols defined beside a formula are allowed;
  their use does not permit invented abbreviations in stored names.
- Concrete over abstract: a real name, number, example or before/after, not "the
  mechanism" or "the invariant".
- Put the concrete case an abstract rule governs beside that rule.
- Keep the source's uncertainty and qualifiers. Never add a cause, frequency or number the
  evidence does not show. Never drop a hedge, exception or consequence that changes what a
  statement means for the reader.
- Required output formats, needed context, tool announcements and approval explanations come
  first. Brevity never removes evidence or content the task needs.
- State what the evidence shows and what it does not show.
- Keep these pairs apart:
  - an observed cause and a hypothesis;
  - a failure in the running system and a risk of a proposed change;
  - a completed fix and a plan or an unverified deployment.
- Report an error as the observed failure, its known cause or "cause unknown", and the next
  diagnostic or fix. Use no alarmist words.
- No filler, no marketing adjectives, no ceremonial openings or closings.

## Format

- Never put long text in a table cell. Use a table only when every cell is a few words.
  Give each option or item its own short section instead.
- Keep a lookup table when every cell is a few words.
- Diagrams are Mermaid in files and chat, never ASCII art or indented text trees.
  State the single question each diagram answers before the diagram. Give adjacent term-home
  links and explain its answer. Separate diagrams with explanatory text.
  Orient them vertically, with groups and comparisons stacked rather than side by side.
  Draw unconnected groups as separate diagrams, with text between them; one diagram would
  place them side by side.
- Make every file reference a link that works where the text is read.
- In a file, link another local file with a path relative to the linking file.
- In other text read on the web (issues, PRs, comments, commits, web pages), link files only
  with web links.
- Pin a web link to a commit when it cites evidence.
- Write a PR or issue reference as a Markdown link: `[#52](https://github.com/owner/repo/pull/52)`.
- Prefer lists of five or fewer items.
- Group longer lists only when helpful.
- Preserve sequence, identifiers, and coverage when grouping.

## House technical English

Use this versioned profile of selected controlled-language practices. It does not claim
ASD-STE100 conformance. The checker reads this block; do not maintain a second limits file.

<!-- spec-docs:profile -->
```json
{
  "version": 1,
  "name": "house-technical-english",
  "procedure_sentence_words": 20,
  "descriptive_sentence_words": 25,
  "descriptive_paragraph_sentences": 6,
  "severity": "warning"
}
```
<!-- /spec-docs:profile -->

- Give each instruction one independent action. Put a condition before its action.
  Use active imperative voice in procedures. Simultaneous actions may share an instruction
  when separating them changes behavior.
- Treat sentence and paragraph targets as warnings. Never fail continuous integration or
  pack generation because of prose heuristics or warning counts.
- Preserve conditions, exceptions, uncertainty and consequences when shortening text.
  A warning alone does not demonstrate a reader problem or require a rewrite.
- Exclude the full STE dictionary, universal part-of-speech checks, full verb restrictions
  and aerospace safety-notice conventions from this profile.

### Counting visible prose

- Count whitespace-separated words. Identifiers and hyphenated tokens count as one word;
  separate numbers and measurement words count separately. Count parenthetical prose.
  Do not count markup or link destinations.
- End a counting sentence at `.`, `?` or `!` followed by whitespace or block end.
  Decimal points and code-span punctuation do not split sentences. A colon introducing
  a vertical list ends its introduction.
- Exclude code blocks, exact signatures, declaration encodings and attributed quotations.
  Their surrounding explanations remain in scope.

## Numbers, dates and times

- Use ISO 80000-1 number formatting in prose: a plain space separates groups of three
  digits, and a dot is the decimal marker. Examples: 2 048; 4 194 304; 0.1; 1 234.5.
- Use ISO 8601 date-only values such as 2026-10-16 without a time zone.
- Use a 24-hour time with a zone, such as 14:05 UTC.
  A timestamp combines date, time and zone: 2026-10-16 14:05 UTC.
- Put a space before unit symbols: 10 min, 1 s, 4 MiB.
- Code, identifiers, configuration, data and literal examples retain their required format.
  A renderer may replace grouping spaces with narrow no-break spaces.
- Format deviations produce writing warnings, never blocking failures.

## Sequences and structured detail

- In walkthroughs, procedures, worked examples and test scenarios, put each ordered action
  or call in its own numbered item under **Procedure: descriptive label**. Use at least
  two ordered actions. Start each item with a short bold action. Put the exact call beside
  it when one is shown. A single action is a sentence.
- Keep each step's action and caller-visible result in one or two plain lines.
  Put exact rows, values, counts and encodings in a labelled detail block beneath its step.
  Preserve exact names, values, order and exceptions there.
- A heading or outer list item does not make several actions one step. Writers and
  reviewers check the action boundaries, not only sentence length.
- Number only steps someone performs in order. A rule, a set of conditions, properties,
  alternatives or independent checks is not a sequence, even when it names several actions;
  use bullets or a paragraph.
- This format does not apply to a single action, an exact signature or schema, or a lookup
  table. Descriptive enumerations use bullets or compact tables. Operator decision options
  are exempt.
- Keep required instructions beside the action that needs them. Put informational detail
  in an ordinary blockquote labelled **Detail — descriptive label**. Every line of a
  detail block carries the `>` prefix.

Before:

`files.create { name: "draft" }` writes the file row and returns its id.
`files.publish { id }` changes its state and returns a publication receipt.

After:

**Procedure: create and publish a file**

1. **Create the file:** `files.create { name: "draft" }`.
   - Writes the file row.
   - Returns its id.
2. **Publish the file:** `files.publish { id }`.
   - Changes the file's state to published.
   - Returns a publication receipt.

## Claim labels

- Render claim labels as inline code: `FACT`, `ASSUMPTION`, `ESTIMATE`, `ASSESSMENT` or
  `DECISION`. Put punctuation outside the code span, for example `FACT`: the test passed.
- Label claims `FACT`, `ASSUMPTION`, `ESTIMATE`, `ASSESSMENT` or `DECISION`. Quote an
  existing decision; never present a settled operator decision as open, or your own choice
  as settled.
- Never ask the operator to decide from a brief that fails the checks below or leaves out
  a real alternative with its side effects. The brief's author runs those checks before
  sending, also on questions a subagent drafted (skill `decision-brief` §Before sending).
- A settled decision reopens only on new evidence, meaning evidence its brief did not
  show: a benchmark, an agent finding confirmed
  by a check, demonstrated overcomplication, a simpler mechanism that meets the same
  needs, or a change to the constraint the decision names as its reason. The reopened
  decision goes back to the operator as a brief that passes those checks.
- In evidence narratives, label a paragraph or finding once when its claims have
  the same authority. Keep evidence beside the supported claims. Split the passage or
  mark the changed authority when fact, assessment or uncertainty changes.
- In specifications, state normative behavior directly. Do not prefix ordinary rules,
  definitions or walkthrough steps with claim labels. Mark measurements, designed costs,
  assumptions, estimates and open choices in the block where they occur. Keep their
  sources and limits. Follow a user's requested claim format when one is supplied.
- The sentence-label format for operator question context applies to that context only;
  it is not the format for a specification or an evidence narrative.
- Label a claim `FACT` only after you verified it yourself, in this session, against its primary source.
  The source is code at a named commit, a command you ran, or a log or record you read.
- Put the evidence for a `FACT` inside its sentence.
  Link the claim's key words to the source.
  Or name the file and line, the command, or the quoted output in a few words.
- A claim that something is absent, such as "none found", "no matches" or "nothing failed",
  names the command that looked and its exit status, for example: `find ~ -name '*.ipsw'`
  exited 0 with no output.
- A claim you remember, infer or read in a summary is never a `FACT`.
  A claim you receive from another agent or an earlier session is never a `FACT`.
  Verify it first, or label it `ASSUMPTION` and name its source.
- A web page or file read through a tool that summarises it with a model is a summary, even
  when the tool's output shows quotes; the quotes may be paraphrased.
  Verify a quote or claim from it against the raw page or file, for example the page
  fetched with `curl`, before you label it `FACT`.

## Checks before sending

- **Reader test:** read only this text as if no other document exists. The reader can tell
  why it matters, what to choose or do, and what happens next.
  Find every reference to an earlier message, question or document, and restate its content.
- **Language test:** no unexplained acronym or label; every sentence states one fact; no
  option or decision appears before its explanation.
- **Detail test:** every fact the decision or action depends on is present.
  For each limit, stop, failure or change, state what it means for the reader.
  Use the same or the next sentence.
  Include what is changed or unchanged, who acts, and what happens next.
  Cut only what the reader does not need.
