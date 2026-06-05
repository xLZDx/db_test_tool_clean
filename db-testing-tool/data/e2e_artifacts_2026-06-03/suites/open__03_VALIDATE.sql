-- VALIDATE suite -- IKOROSTELEV.OPN_TAX_LOTS_NON_BKR_FACT  (scenario: open)
-- ODI-vs-DRD: matched=65/66 real_mismatch=0 unresolvable=1 source_missing=0 odi_extra=0 audit_skipped=5
-- reviewable targets (editable in GUI, NOT auto-fixed per operator addendum): WASH_SALE_TP
-- INSERT vs FREEPDB1 (commit=false): success=True ora=0 resolver_changes=3
SELECT COUNT(*) AS row_count FROM IKOROSTELEV.OPN_TAX_LOTS_NON_BKR_FACT;