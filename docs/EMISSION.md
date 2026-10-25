# File emission rules for LLM-generated content

When an AI writes source files in this conversation, it must emit
them in a form that survives the GUI's markdown renderer. This
document records the rules, the reasons, and the failure modes they
prevent. `deeppeek prompt heredoc` types the operative subset into
the DeepSeek composer.

This document uses backticks and triple-quotes freely — it is for
humans. The embedded prompt (`HEREDOC_PROMPT`) is backtick-free
because it contains examples the AI is meant to copy.

## The rules

1. **Use a quoted bash heredoc.** `python3 - <<'PYEOF' … PYEOF`
2. **Never put a literal backtick in the emitted bash.** Use
   `chr(96) * 3` for fences.
3. **Never wrap the emitted bash in a markdown code fence.** Present
   it as a plain paragraph.
4. **Batch files into one heredoc block.** One block, many `w(...)`
   calls — one renderer round-trip.
5. **Never nest the same triple-quote style.** No `"""` inside `"""`
   examples, no `'''` inside `'''`. Use `def` examples (no
   docstring), or flip one side.

## Why each rule exists

**Quoted delimiter.** The single quotes around `PYEOF` disable bash's
parameter expansion, command substitution, and backslash processing.
Without them, a body containing `$var` or `` `cmd` `` would be
expanded at execution time, and the file that lands on disk would
differ from what the AI intended.

**`chr(96)`.** A run of three literal backticks is treated as a
code-fence boundary by the renderer. When the AI's message contains
```python *inside a body it is transmitting*, the renderer
closes the *outer* fence early and the body arrives fragmented.
Constructing the backtick at Python runtime (`chr(96)`) delays it
past the renderer's parser.

**No fence around the bash.** Same failure: an outer fence closes at
the first inner fence. The emitted bash must be plain prose.

**Batching.** Every bash message is one round-trip through the
renderer, one chance for something to go wrong. One block with ten
`w(...)` calls is strictly safer than ten blocks with one call each.

**Triple-quote nesting.** An AI writing a Python file often includes
a docstring example. If the enclosing string is the same quote style
— `"""` outer, `"""` inner — the inner sequence closes the outer
string early. The remainder of the example is parsed as code and
fails with `SyntaxError: invalid syntax` at the first identifier
after the close. Use `def`-based examples, or flip one side
(`'''` inside `"""`). The safest move is to avoid nested
triple-quotes entirely.

This is the bug that broke the first version of `heredoc.py` itself:
`''''docstring'''',` was written inside a `'''…'''`
string, and Python reported `SyntaxError` at the token `docstring`.

## The delimiter-collision class

Both of the failures above are instances of one general rule:

    When embedding string A inside string B, A must not contain
    B's terminator sequence. Escapes do not help inside raw strings;
    they help only inside cooked strings. Prefer to avoid the
    nesting entirely.

Four concrete collisions have occurred in this project. The markdown
fence is the special case: it cannot be written literally into a
markdown document without breaking it. The row below describes the
collision rather than displaying it; the other three rows show the
offending sequence directly because their terminators are
renderable in Markdown:

| Outer layer | Terminator | Content that broke it |
|---|---|---|
| Markdown fence (three backticks) | three backticks | a raw fence in prose or a table cell |
| bash heredoc `<<'PYEOF'` | line `PYEOF` alone | `python3 - <<'PYEOF'` in a copyable example |
| Python `r'''…'''` | `'''` | ``No `'''` inside `'''` `` in prose |
| Python `"""…"""` | `"""` | `'"""docstring"""'` in a docstring example |

**The fix pattern** for each is identical: replace the offending
sequence with a unique sentinel during the write, then reconstruct
it after the outer layer has parsed. Sentinels are chosen to be
impossible in real content (`'''`, `````, `@@TD@@`) so a
`grep` after emission catches any leak.

**The outer delimiter itself** also needs to be chosen per-context.
`PYEOF` works when the content has no shell heredoc examples; it
breaks when it does. A unique string (`EMIT_END_9f3c`) or a hash of
the content's length and first line works everywhere.

## Canonical patterns

### Single file

```bash
python3 - <<'PYEOF'
from pathlib import Path
B = chr(96) * 3   # backtick triple
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
```

### Multiple files

```bash
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
```

## Anti-patterns

- **Raw backtick fence** in the emitted content — use `chr(96) * 3`.
- **Markdown fence wrapping the bash** — present as plain prose.
- **Docstring inside a triple-double example** — use `def` instead,
  or flip quote style.
- **Unquoted heredoc delimiter** — quote it: `<<'PYEOF'` not
  `<<PYEOF`.
- **One file per bash message** — batch with `w(...)`.
- **Outer delimiter that appears in content** — pick a unique one,
  or sentinelize the inner occurrence.
- **Assuming a delimiter is "just rendering"** — a `'''`
  inside a Markdown code cell, a table cell, or a backtick span is
  still a `'''` to the parser. Sentinelize every literal
  occurrence, not just the ones that look like content the reader
  will copy.

## The prompt

`deeppeek prompt heredoc` ships one prompt (`HEREDOC_PROMPT`) with
the operative rules and the two canonical patterns. It is
backtick-free by construction, because it contains examples the AI
is meant to copy.

It is not piped by default. This document is for humans; the
prompt is what gets typed. If you want to send this document to
the AI anyway, `--text "$(cat docs/EMISSION.md)"` works —
user-to-AI direction is not mangled — but it dilutes the AI's
attention and is not the recommended path.

## The meta-lessons

These lessons came out of building this tool:

- **Test semantics, not formatting.** When a test asserts "the
  prompt mentions X", normalise whitespace first. A wrapped line is
  a formatting choice, not a semantic one. `re.sub(r"\s+", " ", t)`
  is a few characters of code that eliminates a whole class of false
  failures.
- **Test the test-prompt itself.** If the prompt is supposed to be
  backtick-free, assert it. The discipline applies recursively.
- **Two documents serve two audiences.** Humans want rationale and
  history; AIs want rules and examples. Do not merge them.
- **Standing orders.** "From now on" beats "for this task" when the
  constraint should apply across turns. Idempotent, so sending twice
  is harmless.
- **Delimiter collisions are a class of bug, not an accident.** See
  the table above. The escape-hatch is sentinel + reconstruct; the
  discipline is to grep for the sentinel after emission.
- **The rule applies to its own documentation.** This file has now
  been broken three times during its own construction: once by a
  raw fence in prose, once by a `PYEOF` inside a shell-example
  block, once by literal `'''` typed into a table cell. Each
  time the fix was the discipline the file teaches. The document is
  a proof of its own necessity — and the count grows. (The markdown
  fence type, listed first in the collisions table above, is the
  fourth entry: it appears in the table to complete the set, but no
  amount of sentinel discipline can prevent it from appearing in
  prose that discusses it. It can only be detected by the reader.)
- **The parser does not know what "rendering" is.** Every break
  during this document's construction was the same mistake in
  different clothes: I classified a delimiter as "formatting" or
  "the reader will see this as a code cell" and skipped the
  sentinel. The parser sees characters, not intent. Sentinelize
  every literal occurrence, no exceptions.
