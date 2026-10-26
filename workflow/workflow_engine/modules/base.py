"""One module = one independently reviewable contract, not an orchestrator."""
from dataclasses import dataclass


@dataclass(frozen=True)
class ModuleSpec:
    name: str
    agents: tuple[str, ...]
    description: str
    category: str

    def validate(self, results: dict, *, design_mode: str = "none") -> list[str]:
        """Override only for module-specific requirements; generic checks live in controller."""
        return []
