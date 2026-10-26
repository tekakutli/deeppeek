# File emission rules for LLM-generated content

When an AI writes source files in a conversation, it must emit
them in a form that survives the GUI's markdown renderer, reaches
disk intact, and is easy for the human to copy-paste when the
AI's output is the deliverable. This document records the rules,
the reasons, and the failure modes they prevent.

**Standalone.** This file is self-contained. If you are an AI
reading it without any other context, rules 1-6 below are
everything you need to comply. There is no prerequisite prompt,
no project-specific context, no other file to read.

## How to use this document

Read rules 1-6 once. Apply them to every source file you emit,
every file you edit, every file you reprint, for the rest of this
conversation. The rules are stated in imperative form because
they are constraints, not suggestions.

**Changelog.** An earlier version of this file said never wrap
the emitted bash in a markdown fence. That rule was too
conservative. The underlying danger was always *body*
contamination, not the existence of an outer fence. Once the
body is chr(96)-clean, the outer fence is safe and gives the
human a one-click copy button. The revised rules below make the
fence the default and explain exactly why it works. Rules 1, 4,
5 and 6 are unchanged. Rule 2 is unchanged in wording but now
carries the load. Rule 3 flipped.

## The rules

1. **Use a quoted bash heredoc.** `python3 - <<'PYEOF' … PYEOF`

   The single quotes around PYEOF disable bash's parameter
   expansion, command substitution, and backslash processing.

2. **Never put a literal backtick in the heredoc body.** Build
   every backtick at Python runtime with `chr(96)`.

   - `chr(96)`        — one backtick
   - `chr(96) * 3`    — the three-backtick fence marker
   - `chr(96) * 4`    — a four-backtick fence, if needed

   This is the load-bearing rule. A raw three-backtick run
   inside the body is treated by the markdown renderer as a
   fence boundary, closing the outer fence early. chr(96)
   delays construction past the renderer's parser.

3. **Wrap the whole bash in a markdown code fence.** Open with a
   line that is ```bash, close with ``` alone. This gives
   the human a one-click copy button. If the body itself must
   display three-backtick fences as examples (a document about
   markdown, a test fixture containing a fence, a README showing
   code-fenced code), use a four-backtick opening line
   (````bash) instead. Rule: outer fence width must exceed
   the widest inner fence.

4. **Batch files into one heredoc block.** One block, many
   `w(...)` calls, one renderer round-trip. Ten `w()`
   calls in one block is strictly safer than ten blocks with one
   call each.

5. **Never nest the same triple-quote style in Python strings.**
   No triple-single-quote inside a triple-single-quoted string,
   and no triple-double-quote inside a triple-double-quoted
   string. Use `def`-based examples (no docstring), flip one
   side, or construct via `chr(34) * 3` / `chr(39) * 3`.

6. **Verify before write.** Any string manipulation that
   produces a `.py` file: `ast.parse()` the result before
   writing it. If parsing fails, do not write. Read-only probe
   first, verified write second, idempotent patches so a re-run
   is a no-op. See *Canonical patterns → patching an existing
   file*.

## Why each rule exists

**Quoted delimiter.** Without single quotes, a body containing
`$var` or a backtick-command would be expanded at
execution time, and the file on disk would differ from what the
AI intended.

**`chr(96)`.** A raw run of three backticks is a fence
boundary to the markdown renderer. When the AI's message
contains a raw fence *inside the body it is transmitting*, the
renderer closes the outer fence early and the body arrives
fragmented. Constructing the backtick at Python runtime delays
it past the renderer's parser. This is what makes rule 3 safe.

**The outer markdown fence.** Two reasons:

- **Copy-paste.** Most renderers give a one-click copy button
  to fenced blocks. Without a fence the human copies by hand
  and loses trailing whitespace. The AI's output is often the
  deliverable; losing it to a selection artifact is unnecessary.
- **Visual isolation.** A fenced block is visually distinct from
  prose. The boundary between explanation and bash is
  unambiguous.

The historical argument against the outer fence — one slip in
the body contaminates the outer fence — is precisely the
argument for enforcing rule 2. If the body is chr(96)-clean, the
fence is safe. If the body is not clean, no rule saves the
message. Rule 2 is the load-bearing constraint. The outer fence
is the reward.

**Rule 3 failure is silent to the emitter.** Rules 1, 5, and
6 all fail in ways the emitter can observe: rule 1 produces
a file whose bytes differ from intent (visible in the file);
rule 5 raises a SyntaxError from the target's parser
(visible in a traceback the reader can show the AI); rule 6
refuses to write (visible in the run's stderr). Rule 2
(literal backticks) and rule 3 (outer width) are different
— both fail in the markdown parse, which happens after the
AI's response is finalized and before the reader sees it.
The AI's view of its own output is the byte stream it
emitted; that stream is contiguous. The renderer's view is
a parsed tree; that tree may split at a point the stream
does not. The break happens in the parse, not in the stream.

The diagnostic when the reader reports "everything after
point X is broken": find the first fenced block in the
message whose body contains a backtick run of width at
least the outer fence's width. That is where the outer fence
closed. The fix is at that boundary, not at "the part that
looks broken after it" — the reader saw a different parse
of the same bytes.

**Batching.** Every bash message is one round-trip through the
renderer, one chance for something to go wrong.

**Triple-quote nesting.** An AI writing a Python file often
includes a docstring example. If the enclosing string is the
same quote style — """ outer, """ inner — the inner sequence
closes the outer string early. The remainder of the example is
parsed as code and fails with `SyntaxError: invalid syntax`
at the first identifier after the close. Use `def`-based
examples, flip one side (''' inside """), or build with
`chr(34) * 3` / `chr(39) * 3`.

**Verify before write.** A patch applied by string substitution
can produce source that looks like Python but does not parse.
`ast.parse` the result before writing. If the write fails,
nothing changes; if it does not fail, the file is guaranteed
loadable. This is cheap insurance against a whole class of
half-applied failures.

## The fence-width ladder

A markdown fence is a run of three or more backticks. It opens
at a line that is *only* backticks (optionally followed by a
language tag) and closes at the next line that is *only* three-
or-more backticks, of the same width or wider.

**Three backticks — the default.** Correct when the body has no
literal three-backtick run. Since rule 2 makes the body
chr(96)-clean, three backticks is safe by construction. Use this
width 95% of the time.

**Four or more backticks — the escape hatch.** When the body
MUST display three-backtick examples (a doc about markdown
syntax, a test fixture that itself contains a fence, a README
documenting code-fenced code), the outer fence must be wider
than any inner fence. Four-backtick outer with three-backtick
inner works; four-backtick outer with four-backtick inner
breaks. Rule: outer width must exceed maximum inner width.

**The self-referential trap.** A file whose *purpose* is to
display three-backtick fences — a markdown-parser fixture,
a README documenting code fences, this document, a dump of
markdown files — is where rule 3 matters most and where it
is most tempting to skip. The failure is not ignorance of
the rule; it is the "this is obviously code"
rationalization: the file's content is known, the fences
inside it are expected, and the width check feels redundant.
That rationalization is the failure. Rule 3 has no exception
for "but the file is supposed to contain fences" — the
required outer width is determined by the bytes inside the
fence, not by the file's intent.

Tells that a file is in this class: the path or purpose
mentions "fixture", "example", "doc", "README",
"prompt", "dump", or "EMISSION"; the file is a markdown
file whose content is a markdown sample. For such a file,
either widen the outer fence (four-backtick outer around
three-backtick inner, the common shape), or apply rule 2 to
the *inner* content (construct the inner three-backtick runs
via chr(96) * 3 in the emission script; the emitted file
still contains raw fences). Both are correct. Rule 3 is
cheaper; rule 2 produces a cleaner emission script. Choose
one — do not choose "neither, because it's obvious."

**No fence — the piping case.** When the AI's output goes
directly into another process (a text-injection command, a
clipboard pipe, a program that parses stdout), a markdown fence
would be interpreted by the receiver as literal content. In
that case — and only that case — skip the fence and emit plain
prose. Rule 2 still applies to the body.

## The four escaping layers

Every byte of an emitted file passes through four parsers in
sequence. Naming them helps track which escape layer applies.

- **Layer 1 — the markdown renderer.** Sees the whole AI
  message. Interprets three-backtick runs as fence boundaries.
  Solved by rules 2 and 3.
- **Layer 2 — the bash heredoc.** Sees the body between
  `<<'PYEOF'` and `PYEOF`. Quoted delimiter makes bash
  pass through byte-for-byte. No escape layer here.
- **Layer 3 — the Python string literal.** A pattern like
  `"\n".join([...])` inside the heredoc is Python source.
  `"\n"` inside a Python string literal becomes a real
  newline in the resulting string. `"\\n"` becomes
  backslash-n (two characters). Getting this wrong is the most
  common bug in this pattern.
- **Layer 4 — the target file's own parser.** When the target
  is Python, a backslash-n in the file content is parsed by
  Python's tokenizer as a newline-in-a-string. When the target
  is JS, the same two characters mean a newline-in-a-string to
  JS's tokenizer. Same characters, different meaning.

**Named lesson — `\n` vs `\\n`.** When constructing
content that will land in a target file:

- A *search pattern* in the outer Python (looking for a
  substring in an existing file) uses `"\n"` — single
  backslash in the Python source, real newline at runtime,
  matches a real newline in the file.
- A *content string* destined for the target file uses
  `"\\n"` — double backslash in the outer Python,
  backslash-n at runtime, which the target's parser then
  interprets per its own Layer-4 rules.

Mixing the two in one emission is a known bug shape. Fix: name
each as a separate constant (`marker` vs `replacement`) so
the names disambiguate.

**Named lesson — three layers at once is the error surface.**
When the outer Python builds a Python file that itself contains
Python strings that themselves contain escape sequences, every
backslash is ambiguous. If you cannot tell at a glance whether
a given `\n` becomes one byte or two, split the emission:
build the innermost content first, verify it with `ast.parse`,
then wrap it. Failing slow beats shipping corrupted source.

## The delimiter-collision class

All the failures above are instances of one rule:

> When embedding string A inside string B, A must not contain
> B's terminator sequence. Escapes do not help inside raw
> strings; they help only inside cooked strings. Prefer to
> avoid the nesting entirely.

Concrete collisions observed in this project:

| Outer layer | Terminator | Content that broke it |
|---|---|---|
| Markdown fence (three backticks) | three backticks | a raw fence in prose or a table cell |
| bash heredoc `<<'PYEOF'` | line `PYEOF` alone | `python3 - <<'PYEOF'` in a copyable example |
| Python `r'''…'''` | `'''` | `'''` appearing in prose |
| Python `"""…"""` | `"""` | a docstring example inside a `"""`-quoted string |
| Four-backtick fence | four backticks | a four-backtick example in prose (use five-backtick outer) |

**The fix pattern** for each is identical: replace the offending
sequence with a unique sentinel during the write, then
reconstruct it after the outer layer has parsed. Sentinels are
chosen to be impossible in real content (`@@TD@@`, `@@PYEOF@@`)
so a grep after emission catches any leak. For markdown-fence
cases, reconstruct via `chr(96) * N`.

**The outer delimiter itself** also needs to be chosen per
context. `PYEOF` works when the content has no shell heredoc
examples; it breaks when it does. A unique string
(`EMIT_END_9f3c`) or a hash of the content's length and first
line works everywhere.

## Canonical patterns

Each pattern below is shown inside a four-backtick outer fence
so the three-backtick fence markers it contains are preserved
verbatim. See *The fence-width ladder*.

````bash
python3 - <<'PYEOF'
from pathlib import Path
B = chr(96) * 3   # three backticks; chr(96) is the backtick
content = "\n".join([
    "# heading",
    "",
    B + "python",
    "x = 1",
    B,
])
Path("path/to/file.md").write_text(content)
print("wrote path/to/file.md")
PYEOF
````

### Multiple files

````bash
python3 - <<'PYEOF'
from pathlib import Path
B = chr(96) * 3

def w(path, lines):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines) + "\n")
    print("wrote", path)

w("a/b.py", [
    "def f():",
    "    return 1",
])
w("a/c.md", [
    "# heading",
    B + "python",
    "x = 1",
    B,
])
PYEOF
````

### Patching an existing file (two-pass, verify-before-write)

````bash
python3 - <<'PYEOF'
from pathlib import Path
import ast

path = Path("target.py")
src = path.read_text()

# Pass 1 — verify preconditions. Do not touch anything if they fail.
try:
    ast.parse(src)
except SyntaxError as e:
    raise SystemExit(
        f"target.py has a SyntaxError at line {e.lineno}: {e.msg}"
    )

# Search pattern (single \n = real newline in the source)
marker = "def old_func():\n    pass\n"
if marker not in src:
    raise SystemExit("marker not found; nothing to patch")

# Replacement content (double \\n = backslash-n, which the
# target's parser will interpret as a newline when loaded)
replacement = "def new_func():\n    return 42\n"
patched = src.replace(marker, replacement, 1)

# Verify before write.
try:
    ast.parse(patched)
except SyntaxError as e:
    raise SystemExit(
        f"patched source has a SyntaxError at line {e.lineno}: "
        f"{e.msg}. File NOT written."
    )

path.write_text(patched)
print("patched", path)
PYEOF
````

Three properties of the patching pattern that make it robust:

- **Read-only probe first.** If the target is already broken,
  the script exits before touching it.
- **Verified write.** If the substitution produces something
  that does not parse, nothing is written.
- **Search pattern vs. content separated by name.** `marker`
  (a pattern) and `replacement` (content) are separate
  variables so the `\n` vs `\\n` distinction is visible.

For a structural change (not a string substitution), the
pattern generalizes: `ast.parse`, mutate the tree,
`ast.unparse` back, `ast.parse` the result to verify, then
write. `ast.unparse` is not a nice-formatting tool, but for
verified programmatic edits it is safe by construction.

## Anti-patterns

- **Raw three-backtick fence in the emitted body** — use
  `chr(96) * 3`.
- **Three-backtick outer fence around a body that also contains
  three-backtick fences** — use four-backtick outer.
- **Emitting a self-referential file inside a narrow outer
  fence** — a markdown fixture, a doc-about-markdown,
  this document, a dump of markdown files. The body
  necessarily contains fences the same width as the outer,
  so the outer closes early and everything after it in the
  message renders as prose. See *The fence-width ladder →
  The self-referential trap*. The failure is silent to the
  emitter and catastrophic to the reader.
- **Fencing the bash when the output will be machine-parsed** —
  the receiver treats the fence as content; emit plain prose.
- **Docstring inside a triple-double example** — use `def`
  instead, or flip quote style, or `chr(34) * 3`.
- **Unquoted heredoc delimiter** — quote it: `<<'PYEOF'`,
  not `<<PYEOF`.
- **One file per bash message** — batch with `w(...)`.
- **Outer delimiter that appears in content** — pick a unique
  one, or sentinelize the inner occurrence.
- **Assuming a delimiter is just formatting** — `'''` inside
  a markdown code cell is still `'''` to the parser.
  Sentinelize every literal occurrence, not just the ones that
  look like content the reader will copy.
- **Writing without verifying** — `ast.parse` the result of
  any `.py` string manipulation before writing it. Same
  for `.js`: `node --check` if available, or read it
  back and diff.
- **Re-running a non-idempotent patch** — check
  `if marker not in src: exit()` first, so a re-run is a
  no-op rather than a double application.
- **Mixing search patterns with content-destined strings in
  the same block** — name them separately (`marker`,
  `replacement`), so the `\n` vs `\\n` distinction
  stays visible in review.

## Relationship to `deeppeek prompt heredoc`

The operative subset of these rules also ships as a short prompt
in `src/deeppeek/prompts/heredoc.py`, typed into the composer
by `deeppeek prompt heredoc`. That prompt is a convenience
— a standing order the AI can be given at the start of a
conversation.

**This document is standalone.** You do not need the prompt to
follow the rules. If you are reading only this file, you have
everything you need. If the prompt and this document ever
disagree, this document wins — the prompt is a summary, this
document is the source of truth.

## The meta-lessons

These lessons came out of building this tool, running it on real
conversations, and debugging the recovery pipeline when it
failed.

- **The outer fence and the body discipline are complementary,
  not redundant.** The fence gives copy-paste convenience. The
  `chr(96)` discipline makes the fence safe. Enforce both.
  Removing either makes the other worse.

- **A rule that protects against a failure you cannot observe
  is a rule you should re-examine.** The old no-fence rule
  protected against the renderer breaking the outer fence. But
  that is exactly the same failure as the renderer breaking a
  body backtick. Banning the outer fence removed the
  deliverable's usability while doing nothing to prevent body
  contamination. The fix was to enforce the body discipline
  harder.

- **Name your layers of escaping.** When bytes will be parsed
  by four different parsers in sequence (markdown, bash,
  Python, target-file parser), track which byte goes through
  which layer. Use named constants (`B = chr(96) * 3`,
  `marker`, `replacement`) so the layers stay visible.

- **Verify before write.** Convert a whole class of half-
  applied failures into *nothing happened* with a one-line
  parser call before the write is attempted. Failures that
  produce no side effects are strictly better than failures
  that produce partial side effects.

- **Probe before patch.** Read-only precondition checks catch
  the case where the source was already broken. If you patch a
  broken file, the result is a differently-broken file. Verify
  first, write second.

- **Idempotent patches.** A patch that starts with *is the
  marker already present? if so, no-op* can be safely re-run.
  The re-run case is not hypothetical: the human will re-run
  when they are not sure whether the first run succeeded. Make
  the re-run safe.

- **Fail-closed is only as good as the check that feeds it.** A
  protective policy (demote the record if it references an
  undefined symbol) is only as good as its detector (what
  counts as a symbol reference?). This project has twice had
  the policy correct and the detector wrong. When the policy
  fails closed on content that is actually fine, fix the
  detector, not the policy.

- **Test semantics, not formatting.** Normalise whitespace
  before matching text. A wrapped line is a formatting choice,
  not a semantic one. `re.sub(r"\s+", " ", t)` is a few
  characters of code that eliminates a whole class of false
  failures.

- **When you change the rules, update every artefact that
  enforces them.** Rule changes touch the whole chain: this
  document, the prompt constant, the test suite that asserts
  the prompt's shape. When one says never-wrap and another
  says always-wrap, the artefact that is wrong silently
  produces wrong behaviour.

- **Two documents serve two audiences.** Humans want rationale
  and history; AIs want rules and examples. Do not merge them.

- **Standing orders.** *From now on* beats *for this task* when
  the constraint should apply across turns. Idempotent, so
  sending twice is harmless.

- **Delimiter collisions are a class of bug, not an accident.**
  See the table above. The escape hatch is sentinel +
  reconstruct; the discipline is to grep for the sentinel after
  emission.

- **The rule applies to its own documentation.** This file has
  been broken several times during its own construction: once
  by a raw fence in prose, once by a bare `PYEOF` inside a
  shell-example block, once by literal `'''` typed into a
  table cell. Each time the fix was the discipline the file
  teaches. The document is a proof of its own necessity — and
  the count grows. The markdown fence is now on the desired
  list, and its width rule is the newest addition.

- **The parser does not know what rendering is.** Every break
  during this document's construction was the same mistake in
  different clothes: classifying a delimiter as *formatting*
  or *the reader will see this as a code cell* and skipping the
  sentinel. The parser sees characters, not intent. Sentinelize
  every literal occurrence, no exceptions.

- **"This is obviously code" is not an exemption.** Every
  rule in this document has a rationalization that skips it
  in a specific case. Rule 3's rationalization is "but the
  file is supposed to contain fences" — a markdown
  fixture, a dump of markdown files, a doc-about-markdown.
  This is the case rule 3 exists for, and it is the case
  most likely to be skipped. The failure has now happened
  once inside this very document's history: a fixture
  emission was written with a three-backtick outer fence
  because "of course it's a fixture", and the message
  broke at the first inner fence. The rule was in view; the
  rationalization still fired. When the check is trivial, do
  the check.
