"""Ecosystem adapter protocol.

Ecosystems (Python, Node, Go, Rust, ...) plug in behind a small interface.
v0.1 ships a full Python adapter, a generic adapter for plain ``run:`` jobs,
and honest "not yet supported" stubs for the other ecosystems.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from replico.workflow.detector import InstallCommand, JobAnalysis


@dataclass
class EcosystemDetection:
    """What an adapter understood about the CI job."""

    ecosystem: str
    supported: bool
    reason: str = ""
    python_request: str | None = None
    installs: list[InstallCommand] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class EcosystemAdapter(ABC):
    """Interface implemented by every ecosystem adapter."""

    name: str = ""

    @abstractmethod
    def detect(self, analysis: JobAnalysis) -> EcosystemDetection:
        """Decide whether this adapter owns the job (and is supported)."""

    def inspect(self, repo_root: Path, detection: EcosystemDetection) -> None:
        """Adapter hook to inspect the local repository (default: nothing)."""
        return None


def choose_adapter(
    analysis: JobAnalysis,
    registry: list[EcosystemAdapter],
) -> EcosystemDetection:
    """Pick the strongest adapter for a job.

    Order: python → node → go → rust → generic. The chosen adapter is
    returned with its full detection; unsupported ecosystems produce an
    ``unsupported`` detection carrying an explanation.
    """
    best: EcosystemDetection | None = None
    for adapter in registry:
        detection = adapter.detect(analysis)
        if detection.supported:
            return detection
        if best is None and not detection.reason.startswith("not the ecosystem for"):
            best = detection
    if best is not None:
        return best
    return EcosystemDetection(
        ecosystem="unknown", supported=False, reason="no ecosystem adapter matched this job"
    )
