"""Lazy public exports to avoid runtime/evaluation import cycles."""

__all__ = ["EvaluationResult", "EvaluationRunner", "EvaluationStatus", "EvaluationVerifier", "ExecutionBoundary", "Outcome", "SemanticOutcome", "classify_outcome", "load_task"]


def __getattr__(name):
    if name in __all__:
        from agentic_debugger.evaluation import evaluator
        return getattr(evaluator, name)
    raise AttributeError(name)
