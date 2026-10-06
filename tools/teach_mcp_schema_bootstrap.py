"""Run the installed teach-mcp server with Firefly's schema adapter attached."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

from authoring_question_schema_adapter import install_package_adapter
from authoring_output_path_resolver import install_output_path_resolver
from fullbook_planner_adapter import install_fullbook_planner_adapter


def main(server_path: str) -> None:
    server = Path(server_path).resolve(strict=True)
    sys.path.insert(0, str(server.parent))
    install_package_adapter()
    install_output_path_resolver()
    install_fullbook_planner_adapter()
    runpy.run_path(str(server), run_name="__main__")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: teach_mcp_schema_bootstrap.py <server.py>")
    main(sys.argv[1])
