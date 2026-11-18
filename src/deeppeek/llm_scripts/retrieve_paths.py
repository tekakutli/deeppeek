"""llm_scripts.retrieve_paths — extract file paths + a one-liner command.

Like `passthrough`, but the input text is wrapped in a delimited
envelope and prefixed with a fixed instruction asking the model to:

  · list every file path mentioned in the text, and
  · emit a single-line command with no commas.

Keeping instruction and payload in separate markers means a
conversation turn that itself contains the words "give me all the
paths..." (or fenced blocks, or colons) cannot be mistaken for the
directive.

No `on_response` hook — the framework prints the raw LLM reply, so
multi-line path lists survive intact.
"""
NAME = "retrieve_paths"
DESCRIPTION = "List file paths mentioned in the text + a comma-free one-liner command"

# Fixed instruction prepended to every call.
_INSTRUCTION = (
    "give me all the paths of files mentioned in this text, "
    "give me a oneliner for a command, no commas:"
)

# Delimiters — ASCII sentinels chosen so they survive Markdown
# fences, triple backticks, and YAML frontmatter in the payload.
_OPEN = "<<<BEGIN_TEXT>>>"
_CLOSE = "<<<END_TEXT>>>"


def build_prompt(input_text, ctx):
    return (
        f"{_INSTRUCTION}\n"
        f"\n"
        f"{_OPEN}\n"
        f"{input_text}\n"
        f"{_CLOSE}\n"
    )
