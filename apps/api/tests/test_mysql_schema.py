"""Keira MySQL support: every model table compiles for MySQL with LONGTEXT for unbounded text and prefix lengths
on TEXT index columns (prevents: 'BLOB/TEXT column used in key specification without a key length' and
'Data too long for column extracted_markdown' when OpenTutor moves to the Keira rds01 database)."""
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

import models  # noqa: F401 - registers every table
from database import Base
from services.mysql_schema import adapt_metadata_for_mysql


def test_all_tables_compile_for_mysql():
    adapt_metadata_for_mysql(Base.metadata)
    dialect = mysql.dialect()
    ddl = []
    for table in Base.metadata.sorted_tables:
        ddl.append(str(CreateTable(table).compile(dialect=dialect)))
        for index in table.indexes:
            ddl.append(str(CreateIndex(index).compile(dialect=dialect)))
    text = "\n".join(ddl)
    assert " TEXT" not in text.replace("LONGTEXT", "")          # every unbounded text is LONGTEXT
    assert "practice_problems" in text and len(Base.metadata.sorted_tables) >= 30
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            for col in index.columns:
                compiled = str(col.type.compile(dialect=dialect))
                if compiled == "LONGTEXT":
                    assert col.name in index.dialect_options["mysql"]["length"], (table.name, index.name, col.name)
