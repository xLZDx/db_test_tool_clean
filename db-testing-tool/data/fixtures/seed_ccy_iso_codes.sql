-- Fixture seed: clean the FREEPDB1 mirror's REFERENCE_REPL_OWNER.CCY reference data.
--
-- WHY: the mirror's CCY_ISO_NUM_CODE was 1000 rows of non-numeric junk ('L011'...),
-- which made the AVY CCY join (APA.ORIG_CCY_ID = CCY.CCY_ISO_NUM_CODE) throw
-- ORA-01722 (implicit string->number on 'L011'), and prevented V13 from deriving the
-- LPAD pad width (no numeric values to measure). The mirror's CCY_CODE is synthetic
-- numeric ('0'..'999'), not real ISO alpha codes, so we seed clean 3-digit numeric
-- ISO-format codes from CCY_CODE. After this, V13 derives width 3 -> emits
-- LPAD(TO_CHAR(ORIG_CCY_ID), 3, '0') = CCY_ISO_NUM_CODE (string compare, no ORA-01722),
-- and ORIG_CCY_ID <= 999 rows actually join.
--
-- Run as a user with UPDATE on REFERENCE_REPL_OWNER.CCY (e.g. IKOROSTELEV / DBA) on ds 2.
UPDATE REFERENCE_REPL_OWNER.CCY
   SET CCY_ISO_NUM_CODE = LPAD(CCY_CODE, 3, '0')
 WHERE REGEXP_LIKE(CCY_CODE, '^[0-9]+$');
COMMIT;

-- verify: expect MAX over numeric values = 3
-- SELECT MAX(LENGTH(CCY_ISO_NUM_CODE)) FROM REFERENCE_REPL_OWNER.CCY
--  WHERE REGEXP_LIKE(CCY_ISO_NUM_CODE, '^[0-9]+$');
