"""MySQL support for the Keira deployment (one rds01 database `keira`).

The models were written for SQLite, which accepts indexes on unbounded TEXT columns. MySQL needs a key prefix
length for those, so before create_all() every TEXT column inside an index gets a 191-character prefix
(utf8mb4 limit-safe). Nothing changes for SQLite.
"""
from sqlalchemy import Text
from sqlalchemy.ext.compiler import compiles


@compiles(Text, "mysql")
def _mysql_unbounded_text(type_, compiler, **kw):
    """SQLite TEXT is unbounded; MySQL TEXT stops at 64 KB (parsed documents exceed it). Use LONGTEXT."""
    return "LONGTEXT" if type_.length is None else compiler.visit_TEXT(type_, **kw)


def _is_unbounded_text(col_type) -> bool:
    impl = getattr(col_type, "impl", col_type)
    impl = impl() if isinstance(impl, type) else impl
    return isinstance(impl, Text) or (getattr(impl, "length", 1) is None and impl.__class__.__name__ in ("String", "Unicode"))


def adapt_metadata_for_mysql(metadata, prefix: int = 191) -> int:
    changed = 0
    for table in metadata.tables.values():
        for index in table.indexes:
            lengths = {col.name: prefix for col in index.columns if _is_unbounded_text(col.type)}
            if lengths:
                index.dialect_options["mysql"]["length"] = lengths
                changed += 1
    return changed
