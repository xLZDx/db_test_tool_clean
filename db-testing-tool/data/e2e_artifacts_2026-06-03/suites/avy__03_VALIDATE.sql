-- VALIDATE suite -- IKOROSTELEV.AVY_FACT_SIDE  (scenario: avy)
-- ODI-vs-DRD: matched=365/373 real_mismatch=0 unresolvable=2 source_missing=4 odi_extra=2 audit_skipped=5
-- reviewable targets (editable in GUI, NOT auto-fixed per operator addendum): LAST_UDT_USR_NM, SESS_NO
-- INSERT vs FREEPDB1 (commit=false): success=True ora=0 resolver_changes=1
SELECT COUNT(*) AS row_count FROM IKOROSTELEV.AVY_FACT_SIDE;