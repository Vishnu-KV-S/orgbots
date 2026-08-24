"""Operator commands.

The top layer: it may import anything, and nothing may import it. That placement is
what lets the sampling harness reach the org services and the run service at once
without either of them acquiring a dependency on a CLI.
"""
