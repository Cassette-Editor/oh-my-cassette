"""Cassette core: transports, job state, media manifest, and the tool handlers.

Imported as ``cassette.core`` — Hermes clones this repository into
``~/.hermes/plugins/cassette`` and loads the repository root as the package
``cassette``, so this directory is a subpackage of it.

``runtime_config`` deliberately stays at the repository root: every caller
reaches it through a bare ``import runtime_config`` resolved on ``sys.path``,
and moving it here would let ``cassette.core.runtime_config`` and
``core.runtime_config`` load as two modules with separate ``contextvars``
state.
"""
