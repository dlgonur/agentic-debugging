"""Agentic Debugger's Textual terminal UI (optional application surface).

This package is never imported by the scientific core or the existing CLI
paths; it requires the optional ``app`` extra (``textual>=8,<9``).  It is
presentation-only over the accepted application/session layer
(``agentic_debugger.application``) and never executes controller, PDB,
patch, verifier, or model work itself.
"""

__all__ = []
