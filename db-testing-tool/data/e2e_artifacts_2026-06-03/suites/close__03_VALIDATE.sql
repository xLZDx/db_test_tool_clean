-- VALIDATE suite -- IKOROSTELEV.CLS_TAX_LOTS_NON_BKR_FACT  (scenario: close)
-- ODI-vs-DRD: matched=75/84 real_mismatch=0 unresolvable=7 source_missing=1 odi_extra=1 audit_skipped=5
-- reviewable targets (editable in GUI, NOT auto-fixed per operator addendum): LOSS_NOT_ALWD_F, WASH_SALE_TP, SESN_NUM, CRT_DTM, CRT_USR_NM, LAST_UDT_DTM, LAST_UDT_USR_NM
-- INSERT vs FREEPDB1 (commit=false): success=True ora=0 resolver_changes=5
SELECT COUNT(*) AS row_count FROM IKOROSTELEV.CLS_TAX_LOTS_NON_BKR_FACT;