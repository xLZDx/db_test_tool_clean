"""Generate data/test_suites/avyfactside_odi/03_validate.sql"""
import sys, asyncio, pathlib
sys.path.insert(0, '.')

CT  = 'IKOROSTELEV.AVY_FACT_SIDE'
PRD = 'TRANSACTIONS_OWNER.AVY_FACT_SIDE'
JOIN_ON = 'CT.TXN_ID = T.TXN_ID'


async def run():
    from app.database import async_session
    from app.models.datasource import DataSource
    from app.connectors.factory import get_connector
    from sqlalchemy import select

    async with async_session() as session:
        result = await session.execute(select(DataSource).where(DataSource.id == 3))
        ds = result.scalar_one_or_none()
        conn = get_connector(ds)

        rows = conn.execute_query(
            "SELECT COLUMN_NAME, DATA_TYPE FROM ALL_TAB_COLUMNS"
            " WHERE OWNER='IKOROSTELEV' AND TABLE_NAME='AVY_FACT_SIDE'"
            " ORDER BY COLUMN_ID",
            []
        )
        cols = [(r['COLUMN_NAME'], r['DATA_TYPE']) for r in rows]

    lines = [
        '-- ============================================================================',
        '-- AVY_FACT_SIDE Validation: CT (' + CT + ') vs Production (' + PRD + ')',
        '-- Each query returns rows where CT value differs from production value.',
        '-- JOIN: ' + JOIN_ON,
        '-- Generated from: _gen_validate_sql.py  (source: _build_odi_insert.py)',
        '-- ============================================================================',
        '',
    ]

    text_cols = []

    for col, dtype in cols:
        null_safe_lhs = 'CT.' + col
        null_safe_rhs = 'T.'  + col

        if 'CHAR' in dtype or dtype == 'CLOB':
            text_cols.append(col)
            null_val = "'~NULL~'"
            where = 'NVL(' + null_safe_lhs + ', ' + null_val + ') <> NVL(' + null_safe_rhs + ', ' + null_val + ')'
        elif 'DATE' in dtype or 'TIMESTAMP' in dtype:
            null_val = "DATE '1900-01-01'"
            where = 'NVL(' + null_safe_lhs + ', ' + null_val + ') <> NVL(' + null_safe_rhs + ', ' + null_val + ')'
        else:
            # NUMBER: DECODE treats NULL=NULL as equal
            where = 'DECODE(' + null_safe_lhs + ', ' + null_safe_rhs + ', 0, 1) = 1'

        lines += [
            '-- Check: ' + col + '  (' + dtype + ')',
            'SELECT CT.TXN_ID',
            '     , CT.' + col + ' AS CT_VAL',
            '     , T.'  + col + '  AS PROD_VAL',
            'FROM ' + CT  + ' CT',
            'LEFT JOIN ' + PRD + ' T ON ' + JOIN_ON,
            'WHERE ' + where,
            ';',
            '',
        ]

    lines += [
        '-- ============================================================================',
        '-- Overflow / Truncation Audit (text columns)',
        '-- Flags rows where CT value is a strict prefix of PROD value (possible truncation)',
        '-- Condition: LENGTH(T.col) > LENGTH(CT.col) and T.col LIKE CT.col || ''%''',
        '-- ============================================================================',
        '',
    ]

    for col in text_cols:
        lines += [
            '-- Overflow audit: ' + col,
            'SELECT CT.TXN_ID',
            '     , LENGTH(CT.' + col + ') AS CT_LEN',
            '     , LENGTH(T.' + col + ') AS PROD_LEN',
            '     , CT.' + col + ' AS CT_VAL',
            '     , T.' + col + ' AS PROD_VAL',
            'FROM ' + CT + ' CT',
            'LEFT JOIN ' + PRD + ' T ON ' + JOIN_ON,
            'WHERE CT.' + col + ' IS NOT NULL',
            '  AND T.' + col + ' IS NOT NULL',
            '  AND LENGTH(T.' + col + ') > LENGTH(CT.' + col + ')',
            '  AND T.' + col + ' LIKE CT.' + col + " || '%'",
            ';',
            '',
        ]

    sql = '\n'.join(lines)
    out_dir = pathlib.Path('data/test_suites/avyfactside_odi')
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / '03_validate.sql').write_text(sql, encoding='utf-8')
    print(f'Written {len(cols)} column checks -> {out_dir / "03_validate.sql"}')
    print(f'File size: {len(sql):,} chars')


asyncio.run(run())
