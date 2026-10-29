"""PyYAML: the installed package when there is one, else the copy vendored in _vendor/ (no pip needed)."""
try:
    import yaml
except ImportError:  # pragma: no cover - exercised by the `python -S` behaviour test
    from ._vendor import yaml  # type: ignore[no-redef]

__all__ = ["yaml"]
