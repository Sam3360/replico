"""Node ecosystem (planned for v0.3).

v0.1 deliberately ships Python only. This adapter still *detects* Node-based
jobs so Replico can say "unsupported" precisely instead of guessing, and it
doubles as the template future ecosystem adapters copy.
"""

from __future__ import annotations

from replico.environments.base import EcosystemAdapter, EcosystemDetection
from replico.workflow.detector import JobAnalysis

INSTALL_KINDS = ("npm", "yarn", "pnpm")


class NodeAdapter(EcosystemAdapter):
    name = "node"

    def detect(self, analysis: JobAnalysis) -> EcosystemDetection:
        is_node = "node" in analysis.ecosystems or any(
            cmd.kind in INSTALL_KINDS for cmd in analysis.install_commands
        )
        if not is_node:
            return EcosystemDetection(
                ecosystem="node", supported=False, reason="not the ecosystem for this job"
            )
        return EcosystemDetection(
            ecosystem="node",
            supported=False,
            reason=(
                "Node.js workflows are not supported in replico v0.1 "
                "(detected actions/setup-node / npm / yarn / pnpm); "
                "Node support is planned for v0.3"
            ),
        )
