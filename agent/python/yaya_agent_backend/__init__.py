"""Production adapters and application services for the Agent Turn vertical slice.

The package intentionally sits outside :mod:`yaya_agent_runtime`.  Runtime code
continues to depend only on immutable contracts and Protocol ports; concrete
PostgreSQL, HTTP, model-provider and native-process concerns are composed here.

Exports are resolved lazily so importing the learner-only configuration does
not import the Agent Runtime, HTTP, model-provider or sandbox dependency graph.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .composition import LearnerWorkerComposition, create_learner_worker_composition
    from .config import LearnerWorkerSettings
    from .learner_projection import (
        FencedLearnerProjectionPort,
        LearnerProjectionFence,
        LearnerProjectionFenceLost,
        LearnerProjectionLease,
        LearnerProjectionWorker,
        LearnerProjectionWorkerError,
    )
    from .stores import PostgresLearnerStore

__all__ = [
    "FencedLearnerProjectionPort",
    "LearnerProjectionFence",
    "LearnerProjectionFenceLost",
    "LearnerProjectionLease",
    "LearnerProjectionWorker",
    "LearnerProjectionWorkerError",
    "LearnerWorkerComposition",
    "LearnerWorkerSettings",
    "PostgresLearnerStore",
    "create_learner_worker_composition",
]


def __getattr__(name: str) -> object:
    if name == "LearnerWorkerSettings":
        from .config import LearnerWorkerSettings

        return LearnerWorkerSettings
    if name in {"LearnerWorkerComposition", "create_learner_worker_composition"}:
        from .composition import (
            LearnerWorkerComposition,
            create_learner_worker_composition,
        )

        exports: dict[str, object] = {
            "LearnerWorkerComposition": LearnerWorkerComposition,
            "create_learner_worker_composition": create_learner_worker_composition,
        }
        return exports[name]
    if name == "PostgresLearnerStore":
        from .stores import PostgresLearnerStore

        return PostgresLearnerStore
    if name in {
        "FencedLearnerProjectionPort",
        "LearnerProjectionFence",
        "LearnerProjectionFenceLost",
        "LearnerProjectionLease",
        "LearnerProjectionWorker",
        "LearnerProjectionWorkerError",
    }:
        from .learner_projection import (
            FencedLearnerProjectionPort,
            LearnerProjectionFence,
            LearnerProjectionFenceLost,
            LearnerProjectionLease,
            LearnerProjectionWorker,
            LearnerProjectionWorkerError,
        )

        exports = {
            "FencedLearnerProjectionPort": FencedLearnerProjectionPort,
            "LearnerProjectionFence": LearnerProjectionFence,
            "LearnerProjectionFenceLost": LearnerProjectionFenceLost,
            "LearnerProjectionLease": LearnerProjectionLease,
            "LearnerProjectionWorker": LearnerProjectionWorker,
            "LearnerProjectionWorkerError": LearnerProjectionWorkerError,
        }
        return exports[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
