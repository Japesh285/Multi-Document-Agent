"""
core — Workspace execution layer.

Single-LLM-call architecture:

  File loads     → Workspace.register_*  → snapshot.refresh_snapshot()  (Python)
  User query     → prompts.build_workspace_system_prompt(ws)            (Python)
                 → llm.call_chat()                                       (one LLM call)
                 → executor.safe_execute(code, workspace=ws)             (Python)
                 → return result string

Public surface:
    Workspace, WorkspaceMemory
    SpreadsheetObject, DocumentObject, TableObject
    MutationRecord
    compile_context, select_relevant   — small workspace summary (no LLM)
    build_snapshot, refresh_snapshot   — static, pure-Python snapshots
    ReferenceResolver, ResolvedReference
"""

from .context_compiler import compile_context, select_relevant
from .reference_resolver import ReferenceResolver, ResolvedReference
from .snapshot import build_snapshot, refresh_snapshot
from .workspace_manager import Workspace
from .workspace_memory import MutationRecord, WorkspaceMemory
from .workspace_objects import (
    DocumentObject,
    SpreadsheetObject,
    TableObject,
    WorkspaceObject,
)

__all__ = [
    "DocumentObject",
    "MutationRecord",
    "ReferenceResolver",
    "ResolvedReference",
    "SpreadsheetObject",
    "TableObject",
    "Workspace",
    "WorkspaceMemory",
    "WorkspaceObject",
    "build_snapshot",
    "compile_context",
    "refresh_snapshot",
    "select_relevant",
]
