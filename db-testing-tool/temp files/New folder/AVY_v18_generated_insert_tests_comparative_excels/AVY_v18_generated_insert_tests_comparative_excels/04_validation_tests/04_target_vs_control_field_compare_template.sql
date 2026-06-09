-- AVY field-level target-vs-control compare template
-- Fill the grain columns first. Example pattern:
--
-- SELECT COUNT(*) AS mismatch_count
-- FROM TRANSACTIONS_OWNER.AVY_FACT T
-- JOIN IKOROSTELEV.AVY_FACT_CTL CTL
--   ON T.<GRAIN_COL_1> = CTL.<GRAIN_COL_1>
--  AND T.<GRAIN_COL_2> = CTL.<GRAIN_COL_2>
-- WHERE NVL(TO_CHAR(T.<COMPARE_COLUMN>), '-999') <> NVL(TO_CHAR(CTL.<COMPARE_COLUMN>), '-999');
--
-- The generated INSERT and tri_compare_report prove DRD alignment at tool-contract level.
-- This SQL is for database row-level validation after CTL table is built.

PROMPT TODO: Replace <GRAIN_COL_N> and <COMPARE_COLUMN> placeholders before running.

SELECT 'CONFIGURE_GRAIN_AND_COLUMN_FIRST' AS status FROM dual;
