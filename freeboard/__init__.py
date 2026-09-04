"""freeboard -- compose free OpenRouter models into a board that votes.

    from freeboard import board
    session = board.ask("Should we rewrite the parser this quarter?")
    print(session.report())
"""
from . import board, budget, catalogue, config, redact, seats, transport, usage  # noqa: F401

__version__ = "0.1.0"
__all__ = ["board", "budget", "catalogue", "config", "redact", "seats", "transport", "usage"]
