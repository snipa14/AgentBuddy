"""Shared pytest configuration for plugin tests."""

import sys
import importlib.util
from pathlib import Path


def load_capabilities_directly():
    """Load the Capability enum directly without triggering model_router's __init__.py.

    This is needed because model_router/__init__.py eagerly imports config.py which
    requires yaml, but we want to test plugins.manifest which only depends on pydantic.
    """
    spec = importlib.util.spec_from_file_location(
        "services.orchestrator.model_router.capabilities",
        Path(__file__).parent.parent.parent / "model_router" / "capabilities.py"
    )
    capabilities_module = importlib.util.module_from_spec(spec)
    sys.modules["services.orchestrator.model_router.capabilities"] = capabilities_module
    spec.loader.exec_module(capabilities_module)


load_capabilities_directly()
