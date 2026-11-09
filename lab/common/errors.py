"""Base error type for failures the CLI reports without a traceback."""

from __future__ import annotations


class LabError(Exception):
    """An expected failure with a message written for the operator."""
