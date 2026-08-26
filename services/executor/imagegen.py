"""Renders per-stack workspace Dockerfiles from the shared template (ADR-045).

Operator-side tooling, invoked via scripts/gen_workspace_images.py -- the
Executor never calls this at runtime, and nothing agent-reachable can
trigger an image build (ADR-028: docker-socket-proxy keeps BUILD=0). It
lives in the executor package because the Executor owns workspace images,
and because that makes the rendering logic unit-testable under the normal
services/ coverage run.
"""

import string
from pathlib import Path

from services.executor.stacks import StackConfig, StacksConfig

_APT_INDENT = "        "
_CREATE_SANDBOX_BLOCK = (
    "RUN groupadd --gid 1000 sandbox \\\n"
    "    && useradd --uid 1000 --gid sandbox --create-home "
    "--shell /usr/sbin/nologin sandbox\n"
)
_RENAME_SANDBOX_BLOCK = (
    "RUN groupmod --new-name sandbox {rename_from} \\\n"
    "    && usermod --login sandbox --home /home/sandbox --move-home \\\n"
    "       --shell /usr/sbin/nologin {rename_from}\n"
)


def _cache_dir_setup(cache_dir: str | None) -> str:
    """Renders the build-time cache-directory ownership fix, or nothing.

    A freshly created Docker named volume mounted over a path with no
    existing content in the image is created root:root by the daemon,
    regardless of the container's runtime --user. Pre-creating the
    directory in the image with the sandbox UID/GID lets Docker seed the
    volume's initial content (and ownership) from the image layer on first
    mount instead, so the non-root sandbox user can write into its
    package-manager cache from the very first container run. The chown is
    recursive over the whole sandbox home directory, not just cache_dir
    itself, so any parent directories mkdir -p creates along the way (e.g.
    the go stack's GOPATH at /home/sandbox/go, above /home/sandbox/go/pkg/mod)
    are owned by the sandbox user too, not left root-owned. This is cheap
    and safe to run unconditionally here: at this point in the Dockerfile,
    right after the sandbox user block creates/renames the home directory,
    it's small and either already correctly owned or about to be created.

    Args:
        cache_dir: The stack's cache directory, or None if it has no
            per-project cache volume.

    Returns:
        A RUN instruction pre-creating cache_dir and recursively chowning
        /home/sandbox, or "" if cache_dir is None.
    """
    if cache_dir is None:
        return ""
    return f"RUN mkdir -p {cache_dir} && chown -R 1000:1000 /home/sandbox\n"


def render_dockerfile(stack_name: str, stack: StackConfig, template_text: str) -> str:
    """Renders one stack's Dockerfile from the shared template.

    Args:
        stack_name: The stack's key in stacks.yaml (used in the generated
            header comment).
        stack: The stack's validated configuration.
        template_text: The contents of images/workspace.template.Dockerfile.

    Returns:
        The complete Dockerfile text, with runs of blank lines collapsed so
        stacks without extra env lines don't render double blanks.

    Raises:
        ValueError: If stack.apt_packages is empty or if stack.pip_packages
            is empty. Workspace images require both to build correctly and
            every image needs the shared tool layer (Playwright, etc.).
    """
    if not stack.apt_packages:
        raise ValueError(
            f"stack {stack_name!r} declares no apt_packages; the workspace "
            "template's apt-get block requires at least one"
        )
    if not stack.pip_packages:
        raise ValueError(
            f"stack {stack_name!r} declares no pip_packages; every workspace "
            "image needs at least Playwright (ADR-028)"
        )
    apt_package_lines = "\n".join(f"{_APT_INDENT}{package} \\" for package in stack.apt_packages)
    env_lines = "".join(f"\nENV {key}={value}" for key, value in sorted(stack.env.items()))
    if env_lines:
        env_lines += "\n"
    if stack.sandbox_user_mode == "rename":
        sandbox_user_block = _RENAME_SANDBOX_BLOCK.format(rename_from=stack.rename_from)
    else:
        sandbox_user_block = _CREATE_SANDBOX_BLOCK
    rendered = string.Template(template_text).substitute(
        stack_name=stack_name,
        base_image=stack.base_image,
        apt_package_lines=apt_package_lines,
        pip_flags=" --break-system-packages" if stack.pip_break_system_packages else "",
        pip_packages=" ".join(stack.pip_packages),
        env_lines=env_lines,
        sandbox_user_block=sandbox_user_block,
        cache_dir_setup=_cache_dir_setup(stack.cache_dir),
    )
    return _collapse_blank_runs(rendered)


def instruction_lines(dockerfile_text: str) -> list[str]:
    """Reduces a Dockerfile to its logical instruction lines.

    Drops comments and blank lines and joins backslash continuations, so
    two Dockerfiles can be compared on what they actually instruct the
    daemon to do rather than on prose.

    Args:
        dockerfile_text: Full Dockerfile contents.

    Returns:
        One string per logical instruction.
    """
    logical: list[str] = []
    pending = ""
    for raw_line in dockerfile_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1].rstrip() + " "
            continue
        logical.append((pending + line).strip())
        pending = ""
    if pending:
        logical.append(pending.strip())
    return logical


def generate_all(stacks: StacksConfig, template_path: Path, images_root: Path) -> dict[str, Path]:
    """Renders and writes every stack's Dockerfile under images_root.

    Args:
        stacks: The loaded stack registry.
        template_path: Path to images/workspace.template.Dockerfile.
        images_root: The images/ directory to write
            workspace-<stack>/Dockerfile files into.

    Returns:
        A mapping of stack name to the Dockerfile path written.
    """
    template_text = template_path.read_text()
    written: dict[str, Path] = {}
    for stack_name, stack in stacks.stacks.items():
        target = images_root / f"workspace-{stack_name}" / "Dockerfile"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_dockerfile(stack_name, stack, template_text))
        written[stack_name] = target
    return written


def _collapse_blank_runs(text: str) -> str:
    """Collapses runs of two or more blank lines into a single blank line."""
    lines = text.splitlines()
    collapsed: list[str] = []
    for line in lines:
        if line.strip() == "" and collapsed and collapsed[-1].strip() == "":
            continue
        collapsed.append(line)
    return "\n".join(collapsed) + "\n"
