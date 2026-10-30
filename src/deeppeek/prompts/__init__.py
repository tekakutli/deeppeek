"""AI prompt pipelines.  Each prompt lives in its own module.

Convention: a module is a prompt if it (a) lives directly in this
package, (b) does not start with `_`, and (c) defines a callable
`register(subparsers)`.  `deeppeek.commands.prompt` auto-discovers
every such module — adding a prompt is one new file, no edit to
the dispatcher.

Helper modules shared between prompts should start with `_`
(e.g. `_common.py`) to exclude them from discovery.
"""
