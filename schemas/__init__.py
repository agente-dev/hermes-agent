"""Pydantic request/response schemas for Hermes API surfaces.

Schemas live in their own package so non-API callers (CLI, tests,
sub-packages) can validate payloads without dragging the whole
gateway server import graph.
"""
