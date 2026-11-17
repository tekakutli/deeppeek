"""transport — the local HTTP/SSE server that the extension talks to.

Every CLI→extension interaction (`pipe`, `type`, `enter`, `clear`,
`hijack`, `prompt *`, `daemon`, `extension watchdog`) goes through
`ExportServer` from this module.
"""
from .server import ExportServer

__all__ = ["ExportServer"]
