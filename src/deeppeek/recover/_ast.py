"""_ast.py — the single point where deeppeek parses Python source.

Every `ast.parse` call in `deeppeek.recover` routes through
`parse_source`, which:

  1. Caches results by source string.
  2. Silences `SyntaxWarning` — invalid escape sequences like `\\w`
     in a non-raw string, and deprecated syntax.

The warnings are *data about the source under analysis*, not
failures of it.  A source file whose docstring mentions `[\\w$]*`
is not malformed; it just has an unrecognized escape sequence in a
string literal.  Emitting the warning once per parse per pass —
deeppeek parses its own ~50 modules on every pipeline run — turns
the log unreadable for a check that, at runtime, changes nothing.

`parse_source` is a drop-in for `ast.parse`: same signature, same
return type, same `SyntaxError` on failure.  The differences are
the absence of warnings, the memoization, and the exception's
`filename` being the caller's filename (see MEMOIZATION below).

MEMOIZATION (2026-11-05)
========================
`parse_source` memoizes on the source string.  Motivation: multiple
passes re-parse the same content (base, result, patch bodies)
repeatedly.  Measured at 6+ parses per `.py` record across passes
6, 8, 8.4b, 8.5, 8.7d, and 8.10.  On a 500-record `--apply-to` run
that's ~3000 parses of content that hasn't changed, and `ast.parse`
is the dominant cost of those passes.

Cache key: the source string only.  `filename` is used *only* for
error attribution: on a cache miss that raises `SyntaxError`, we
re-raise with the caller's filename so warnings.txt and the
per-file manifest carry the right path.  On a cache hit, the
cached tree is returned as-is (`ast.AST` nodes carry no filename —
only line/col offsets, which are filename-agnostic).

Cache size: 2048 entries.  A mid-sized `--apply-to` run touches
~500 records × 2 sources (content + base) = ~1000 distinct strings;
2048 is generous for the common case, bounded for pathological
ones.  Memory: ~50–100 MB of AST nodes for a 500-record run.

Thread safety: `lru_cache` is thread-safe; the pipeline is
single-threaded.  The returned `ast.AST` is treated as immutable by
every caller — no mutation anywhere in the codebase (audited:
`ast.walk`, `ast.iter_child_nodes`, `ast.unparse`, `.body`,
`.lineno`, `.end_lineno`, `.func`, `.args`, `.keywords` are the
only attributes touched, all reads).

Exception caching: NOT DONE.  `lru_cache` does not cache raised
exceptions.  A source that raises `SyntaxError` re-parses on
every call, matching pre-cache behaviour.  Syntax errors in the
pipeline are one-shot (bad input, caught immediately); the
re-parse cost is negligible.  If a future run shows pathological
re-raise volume, wrap the raise site with a sentinel cache — but
measure first.

DIAGNOSTIC HOOK
===============
`parse_cache_stats()` returns `{hits, misses, size, maxsize}`.
The `--profile` block in `pipeline.run()` reports it after the
per-pass timing table.  A run where `misses >> hits` means an
upstream pass is passing fresh strings per call — dedent-of-dedent,
whitespace normalisation, or a string rebuilt from line lists.
The fix is at that call site, not here.

`clear_parse_cache()` exists for tests that patch `ast.parse` or
need fresh-cache semantics.  The pipeline itself never calls it.
"""

from __future__ import annotations

import ast
import warnings
from functools import lru_cache


_PARSE_CACHE_SIZE = 2048


@lru_cache(maxsize=_PARSE_CACHE_SIZE)
def _parse_cached(source: str) -> ast.AST:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(source)


def parse_source(source: str,
                 filename: str = "<unknown>") -> ast.AST:
    """Parse `source` as Python, memoized, silencing `SyntaxWarning`.

    Returns the `ast.Module` on success; raises `SyntaxError` on
    failure, exactly as `ast.parse` does.  On a cache miss that
    raises, the exception is re-raised with the caller's `filename`
    so error attribution matches the caller's expectation.

    Thread `filename` whenever it is available.  The resulting
    `SyntaxError.filename` is what callers report to the operator;
    the default `<unknown>` is a last resort for call sites where
    no filename is in scope.  On a cache HIT, the filename is
    ignored — the cached tree carries no filename.
    """
    try:
        return _parse_cached(source)
    except SyntaxError as e:
        # Preserve the original exception class (IndentationError,
        # TabError) so callers that specifically catch those still
        # work.  All three accept `(msg, (filename, lineno,
        # offset, text))`.  `from None` collapses the exception
        # chain so tracebacks don't show both the cached parse and
        # the re-raise.
        raise type(e)(
            e.msg,
            (filename, e.lineno, e.offset, e.text),
        ) from None


def parse_cache_stats() -> dict:
    """Diagnostic: `{hits, misses, size, maxsize}` for `parse_source`.

    Consumed by the `--profile` block in `pipeline.run()`.  A run
    where `misses >> hits` means an upstream pass is passing fresh
    strings per call — dedent-of-dedent, whitespace normalisation,
    or a string rebuilt from line lists.  The fix is at that call
    site, not here.
    """
    info = _parse_cached.cache_info()
    return {
        "hits": info.hits,
        "misses": info.misses,
        "size": info.currsize,
        "maxsize": info.maxsize,
    }


def clear_parse_cache() -> None:
    """Drop every cached parse.  For tests, not for pipeline code.

    Tests that patch `ast.parse` (to inject a fake, count calls,
    etc.) must call this before and after their exercise, or they
    see stale results from earlier runs in the same process.  The
    pipeline itself never calls this — the cache is per-process
    and the process exits when the run finishes.
    """
    _parse_cached.cache_clear()
