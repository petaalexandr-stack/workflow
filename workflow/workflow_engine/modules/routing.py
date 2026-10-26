"""Contract for independent routing module."""
from .base import ModuleSpec

SPEC = ModuleSpec('routing', ('orchestrator',), 'Динамический выбор необходимых модулей', 'intake')
