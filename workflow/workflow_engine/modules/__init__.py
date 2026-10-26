"""Independent module registry. Each contract is contained in its own file."""
from .discovery import SPEC as discovery
from .evals import SPEC as evals
from .planning import SPEC as planning
from .system_design import SPEC as system_design
from .tasks import SPEC as tasks
from .development import SPEC as development
from .debugging import SPEC as debugging
from .quality import SPEC as quality
from .documentation import SPEC as documentation
from .understanding import SPEC as understanding
from .routing import SPEC as routing

CATALOG = {spec.name: spec for spec in (
    discovery, evals, planning, system_design, tasks, development,
    debugging, quality, documentation,
)}
INTERNAL = {spec.name: spec for spec in (understanding, routing)}
