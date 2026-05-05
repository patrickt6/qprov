"""qprov - Provenance tracker for math research computations.

Public API:
    @tracked              decorate a function to record every call
    claim(...)            register a numerical claim linked to a computation
    find(...)             query computations by tags / function / dates
    get(id)               fetch a single computation by id
    export_latex(...)     render claims to a .tex file
    register_external(...) retroactively register pre-existing JSON outputs
    set_store_root(path)  override the default .qprov store location
"""
from .tracking import (
    tracked,
    QprovHashWarning,
    QprovHashError,
    QprovFileMissingError,
    QprovTraversalError,
    QprovPropertyWarning,
)
from .store import (
    Store,
    Computation,
    Claim,
    get_store,
    set_store_root,
    QprovCollisionError,
    PayloadTamperedError,
)
from .claims import claim, export_latex, UnbackedPaperClaimError
from .inputs import canonical_file, hash_file, path_of
from .query import find, get
from .external import register_external
from .audit_paper import (
    audit_paper,
    AuditEntry,
    AuditReport,
    ExtractedNumber,
    Mismatch,
)
from .properties import Property, PropertyResult, QprovPropertyError

__version__ = "0.4.0"

__all__ = [
    "tracked",
    "claim",
    "find",
    "get",
    "export_latex",
    "register_external",
    "canonical_file",
    "hash_file",
    "path_of",
    "UnbackedPaperClaimError",
    "QprovHashWarning",
    "QprovHashError",
    "QprovFileMissingError",
    "QprovTraversalError",
    "QprovCollisionError",
    "QprovPropertyError",
    "QprovPropertyWarning",
    "PayloadTamperedError",
    "Property",
    "PropertyResult",
    "Store",
    "Computation",
    "Claim",
    "get_store",
    "set_store_root",
    "audit_paper",
    "AuditEntry",
    "AuditReport",
    "ExtractedNumber",
    "Mismatch",
    "__version__",
]
