"""Compatibility imports for provider-neutral :mod:`yaya_agent_sandbox` adapters.

New code should import from ``yaya_agent_sandbox``.  No Sandbox implementation
is retained in the backend package.
"""

from yaya_agent_sandbox import ArgumentBuilder, ProductionCppSandbox

__all__ = ["ArgumentBuilder", "ProductionCppSandbox"]
