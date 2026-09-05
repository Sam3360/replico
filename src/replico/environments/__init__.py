"""Ecosystem adapters + environment fingerprinting."""

from replico.environments.base import EcosystemAdapter, EcosystemDetection, choose_adapter
from replico.environments.node import NodeAdapter
from replico.environments.python import (
    PythonAdapter,
    ensure_venv,
    find_python,
    venv_python,
)

# Order matters: first *supported* match wins in choose_adapter.
ADAPTERS: list[EcosystemAdapter] = [PythonAdapter(), NodeAdapter()]

__all__ = [
    "ADAPTERS",
    "EcosystemAdapter",
    "EcosystemDetection",
    "NodeAdapter",
    "PythonAdapter",
    "choose_adapter",
    "ensure_venv",
    "find_python",
    "venv_python",
]
