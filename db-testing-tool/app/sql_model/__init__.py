"""Typed semantic model for the SQL test-generation pipeline.

Package layout:
  types.py           -- IR data classes (TableRef, AliasBinding, ODIModel, …)
  odi_template_resolver.py -- resolve ODI XML template tags to SQL-ready strings
  odi_parser.py      -- ODI XML -> ODIModel parser (multi-step staging lineage)
  sql_emitter.py     -- ODIModel -> Oracle INSERT SQL (fail-loud on UnresolvedExpr)
"""
