"""Execute the ODI full insert SQL against Oracle and report results."""
import sys
import asyncio
from pathlib import Path

sys.path.insert(0, ".")


async def run():
    from app.database import async_session
    from app.models.datasource import DataSource
    from app.connectors.factory import get_connector
    from sqlalchemy import select

    sql = Path("data/odi_full_insert.sql").read_text(encoding="utf-8")

    async with async_session() as session:
        result = await session.execute(select(DataSource).where(DataSource.id == 3))
        ds = result.scalar_one_or_none()
        conn = get_connector(ds)

        try:
            conn.execute_query(sql, [])
            print("INSERT executed OK")
        except Exception as e:
            msg = str(e)
            print("ERROR:", msg[:800])


asyncio.run(run())
