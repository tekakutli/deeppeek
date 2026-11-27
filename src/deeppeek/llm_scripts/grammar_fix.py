"""llm_scripts.grammar_fix — correct grammar and orthography.

Wraps the input in a prompt that asks the LLM to fix grammar,
spelling, and orthography without altering meaning, tone, structure,
or formatting (markdown, code blocks, URLs, proper nouns).  The
framework prints the LLM's raw reply automatically since there is no
`on_response` hook.

Use it via:
    deeppeek editor --script grammar_fix --file draft.md
    deeppeek editor --script grammar_fix "their going too the store"
"""
NAME = "grammar_fix"
DESCRIPTION = "Correct grammar, spelling, and orthography; keep meaning"


PROMPT_TEMPLATE = """\
You are a meticulous copy editor.  Correct all grammar, spelling,
punctuation, and orthography errors in the text below.

Rules:
  · Preserve the author's meaning, tone, register, and intent.
  · Preserve structure and formatting exactly: paragraph breaks,
    list markers, headings, blank lines, indentation.
  · Leave code blocks, inline code, URLs, file paths, and other
    technical literals untouched.
  · Leave proper nouns, product names, and quoted material alone
    unless they are clearly misspelled.
  · Do not rephrase, rewrite, shorten, or expand.  Fix only what is
    actually wrong.
  · Do not add commentary, explanations, or a changelog.
  · Output ONLY the corrected text — nothing before or after it.
    If the input is already correct, return it unchanged.

Text to correct:
<text>
{input_text}
</text>
"""


def build_prompt(input_text, ctx):
    return PROMPT_TEMPLATE.format(input_text=input_text)
