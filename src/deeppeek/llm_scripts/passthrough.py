"""llm_scripts.passthrough — send input verbatim to the LLM.

Matches the behavior of a bare `deeppeek answer "<text>"`: the input
text is passed through as the prompt, and the LLM's raw reply is
printed to stdout.  The framework prints the raw response
automatically when there is no on_response hook.
"""
NAME = "passthrough"
DESCRIPTION = "Send input verbatim, print LLM reply as-is"


def build_prompt(input_text, ctx):
    return input_text