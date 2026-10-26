"""System Design: separately testable completeness criteria."""
from .base import ModuleSpec


class SystemDesignSpec(ModuleSpec):
    def validate(self, results: dict, *, design_mode: str = "none") -> list[str]:
        from ..system_design import validate_architect_result
        result = results.get("architect") or {}
        return validate_architect_result(result, design_mode)


SPEC = SystemDesignSpec("system_design", ("architect",), "System Design", "design")
