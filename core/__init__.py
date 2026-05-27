"""
core — Workspace execution layer.

The Workspace is the central registry for everything the AI can manipulate:
spreadsheets, documents, extracted tables, and the memory of prior results.
LLM-generated code executes against workspace objects rather than against
a single bare DataFrame.

Public surface
==============
    Workspace, WorkspaceMemory
    SpreadsheetObject, DocumentObject, TableObject
    MutationRecord
    compile_context, select_relevant
    ReferenceResolver, ResolvedReference
"""

from .context_compiler import compile_context, select_relevant
from .reference_resolver import ReferenceResolver, ResolvedReference
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
    "compile_context",
    "select_relevant",
]
