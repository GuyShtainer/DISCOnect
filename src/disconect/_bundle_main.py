"""Entry point of the PyInstaller sidecar (`hearthbeat-core`).

    hearthbeat-core [--db PATH]        -> the serve protocol (what the desktop app spawns)
    hearthbeat-core mcp [...]          -> the read-only MCP server for Claude Desktop
    hearthbeat-core cli <args...>      -> the ordinary CLI, for diagnostics from a terminal
"""

import sys


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["mcp"]:
        from disconect.mcp_server import main as mcp_main

        sys.argv = [sys.argv[0], *argv[1:]]
        return mcp_main() or 0
    if argv[:1] == ["cli"]:
        from disconect.cli import main as cli_main

        return cli_main(argv[1:]) or 0
    from disconect.serve import main as serve_main

    return serve_main(argv) or 0


if __name__ == "__main__":
    sys.exit(main())
