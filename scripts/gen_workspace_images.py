"""Operator CLI: regenerate images/workspace-<stack>/Dockerfile from config/stacks.yaml.

Run from the repo root after editing config/stacks.yaml, then build the
image(s) with docker build (CLAUDE.md's Commands section). This is the only
path that produces workspace Dockerfiles -- they are generated files, and
services/executor/tests/unit/test_imagegen.py's golden test fails CI if a
committed Dockerfile drifts from its rendered form.
"""

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services.executor.imagegen import generate_all
from services.executor.stacks import load_stacks_config


def main(argv: list[str] | None = None) -> int:
    """Parses arguments and regenerates every stack's Dockerfile.

    Args:
        argv: Command-line arguments, excluding the program name. Defaults
            to sys.argv[1:] when None.

    Returns:
        0 on success.
    """
    parser = argparse.ArgumentParser(prog="gen_workspace_images")
    parser.add_argument("--stacks-config", type=Path, default=REPO_ROOT / "config" / "stacks.yaml")
    parser.add_argument(
        "--template",
        type=Path,
        default=REPO_ROOT / "images" / "workspace.template.Dockerfile",
    )
    parser.add_argument("--images-root", type=Path, default=REPO_ROOT / "images")
    args = parser.parse_args(argv)

    stacks = load_stacks_config(args.stacks_config)
    written = generate_all(stacks, args.template, args.images_root)
    for stack_name, path in sorted(written.items()):
        print(f"{stack_name}: {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
