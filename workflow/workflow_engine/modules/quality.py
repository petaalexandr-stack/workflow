"""Contract for independent quality module."""
from .base import ModuleSpec

SPEC = ModuleSpec('quality', ('code-reviewer', 'tester', 'security'), 'Проверка качества, тестов и безопасности', 'quality')
