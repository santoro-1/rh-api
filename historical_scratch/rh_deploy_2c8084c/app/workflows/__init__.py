"""Workflow adapters registered by the application.

Generic web and worker code imports only the registry from this package.  A
new RunningHub workflow belongs in its own adapter module plus one registry
entry; its node IDs never leak into generic task handling.
"""

from app.workflows.registry import get_workflow, list_workflows

__all__ = ["get_workflow", "list_workflows"]
