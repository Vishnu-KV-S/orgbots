"""Graphs.

`runtime.graphs.department` imports every M1 actor, which is how they come to be in
the registry. Importing the package itself does not — a process that only needs one
graph should not pay to build four.
"""
