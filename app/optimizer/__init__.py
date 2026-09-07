from app.optimizer import greedy, milp
from app.optimizer.constraints import (
    ROLE_POLICY,
    EmployeeSpec,
    PlannedShift,
    SolveParams,
    SolveResult,
    TemplateSpec,
    demand_to_requirements,
    required_headcount,
)

SOLVER_REGISTRY = {"milp": milp.solve, "greedy": greedy.solve}


def solve(name: str, *args, **kwargs) -> SolveResult:
    """Dispatch to a solver; degrade to greedy if the MILP backend is missing."""
    if name not in SOLVER_REGISTRY:
        raise KeyError(f"unknown solver '{name}'; available {sorted(SOLVER_REGISTRY)}")
    if name == "milp":
        try:
            return milp.solve(*args, **kwargs)
        except milp.SolverUnavailable:
            result = greedy.solve(*args, **kwargs)
            result.meta["degraded_from"] = "milp"
            return result
    return SOLVER_REGISTRY[name](*args, **kwargs)


__all__ = [
    "solve",
    "SOLVER_REGISTRY",
    "EmployeeSpec",
    "TemplateSpec",
    "SolveParams",
    "SolveResult",
    "PlannedShift",
    "demand_to_requirements",
    "required_headcount",
    "ROLE_POLICY",
]
