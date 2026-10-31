"""OpenAI-compatible local model server backed by the DeepSeek browser extension.

The daemon implements the subset of the OpenAI HTTP API that aider
(and similar clients) dial into:

    POST /v1/chat/completions
    GET  /v1/models

Requests are serialized into a single prompt string, typed into the
DeepSeek web composer via the existing deeppeek extension, submitted,
and the assistant's response is streamed back as OpenAI-compatible
SSE chunks.

See ``deeppeek daemon --help`` for usage.
"""