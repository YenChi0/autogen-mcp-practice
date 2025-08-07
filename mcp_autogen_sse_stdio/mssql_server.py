# src/mssqlserver/server.py
"""
Minimal MCP server for Microsoft SQL Server.
• One tool  : query_sql(sql, limit=500)
• Transport : stdio (FastMCP)
Env vars required (.env or shell):
    MSSQL_DSN  = "Driver={ODBC Driver 18 for SQL Server};Server=tcp:host,1433;\
                  Database=mydb;UID=user;PWD=pw;Encrypt=yes;TrustServerCertificate=yes"
"""
import csv
from pathlib import Path
import asyncio, os
from typing import Any, Dict, List

import aioodbc
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()
# environ : can catch KeyError if missing
DSN = os.environ["MSSQL_DSN"]
POOL_MIN, POOL_MAX = 1, 5

mcp  = FastMCP("mssql-demo")
# aioodbc is a Python library that allows asynchronous interaction with ODBC-compatible databases using asyncio.
# _pool will be either a Pool object from aioodbc.pool, or None.
_pool: aioodbc.pool.Pool | None = None


async def get_pool() -> aioodbc.pool.Pool:
    """Singleton aioodbc pool (lazy init)."""
    global _pool
    if _pool is None:
        _pool = await aioodbc.create_pool(
            dsn=DSN, minsize=POOL_MIN, maxsize=POOL_MAX, autocommit=True
        )
    return _pool

# query_sql_mssql : input is a raw SQL string, output is a list of dicts from the result set.
@mcp.tool()
async def query_sql_mssql(sql: str, limit: int = 500) -> List[Dict[str, Any]] | Dict[str, str]:
    """
    Run a **read-only** T-SQL statement (SELECT / WITH).
    If the caller forgets to limit rows, append
        OFFSET 0 ROWS FETCH NEXT <limit> ROWS ONLY
    so we never return an unbounded result.
    On ANY exception, write the SQL + traceback to stderr and
    return {"error": "...", "sql": "<the-sql>"} instead of null.
    """
    import sys, traceback
    # clean sql string and only SELECT / WITH statements allowed
    low = sql.strip().lower()
    if not low.startswith(("select", "with")):
        raise ValueError("Only SELECT / WITH statements are allowed")

    # auto-limit rows if caller didn't
    import re
    if (" top " not in low
            and " fetch next " not in low
            and " order by " not in low):
        # add TOP when there's no existing limit and no ORDER BY
        sql = re.sub(r"^\s*select\b", f"SELECT TOP ({limit})", sql, count=1, flags=re.I)
    elif " fetch next " not in low and " order by " in low:
        # has ORDER BY but no FETCH ⇒ append fetch
        sql += f" OFFSET 0 ROWS FETCH NEXT {limit} ROWS ONLY"

    try:
        pool = await get_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql)
            cols = [c[0] for c in cur.description]
            rows = await cur.fetchall()
            return [dict(zip(cols, r)) for r in rows]

    except Exception as e:
        # --- dump details to server stderr (shows in Claude / runner log) ---
        print("\n=== SQL ERROR in query_sql_mssql ===", file=sys.stderr)
        print(sql, file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

        # --- bubble a JSON error payload back to the host ---------------
        return {"error": str(e), "sql": sql}

_SCHEMA_CSV = Path(__file__).with_name("stTseStkPrcD_schema.csv")

# read_schema_csv : input is an optional file path which defaults to _SCHEMA_CSV, output is a list of dicts from the CSV.
@mcp.tool()
async def read_schema_csv(file: str | None = None) -> List[Dict[str, str]]:
    """
    Load the column-definition CSV and return it as a JSON array.

    Parameters
    ----------
    file : str | None
        Optional custom path.  If omitted, uses 'stScuSecuBasC_schema.csv'
        located in the same directory as server.py.

    Returns
    -------
    list[dict]
        Each row is a dict with keys:
        'Column_name', 'Explanation', 'Datatype', 'Availability'
    """
    path = Path(file) if file else _SCHEMA_CSV
    if not path.exists():
        raise FileNotFoundError(f"CSV not found → {path}")

    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)

# ---------------------------------------------------------------------------
# 🔎  Resolve stock ID from common name / alias  -----------------------------
# ---------------------------------------------------------------------------
# resolve_stock_id_mssql : input is a keyword string, output is a dict with the stock ID and matched column/value.
# it compares stock id first by exact listCode match, then by LIKE search across multiple alias columns.
@mcp.tool()
async def resolve_stock_id_mssql(keyword: str) -> Dict[str, Any]:
    """
    Find the internal `id` in `stScuSecuBasC` that matches a company name,
    abbreviation or listCode.
    
    Strategy
    --------
    * Case-insensitive LIKE across all columns.
    * Prefer **exact listCode** first (fast path).
    * Otherwise return TOP 1 shortest alias, alphabetically first.

    Returns
    -------
    {"id": 748, "matched_column": "nameAbbrV2", "matched_value": "台積電"}
    or {} if no match.
    """
    pool = await get_pool()
    kw = keyword.strip()

    async with pool.acquire() as conn, conn.cursor() as cur:

        # 1️⃣  Fast path: exact listCode match (e.g. '2330')
        await cur.execute(
            "SELECT id, 'listCode' AS matched_column, listCode AS matched_value "
            "FROM stScuSecuBasC WHERE listCode = ?", (kw,)
        )
        if (row := await cur.fetchone()):
            return dict(zip([c[0] for c in cur.description], row))

        # 2️⃣  Search every alias column with LIKE '%kw%'
        like_sql = """
        SELECT TOP (1)
               id,
               colname   AS matched_column,
               alias     AS matched_value
        FROM (
            SELECT id, 'name'        AS colname, name        AS alias FROM stScuSecuBasC
            UNION ALL
            SELECT id, 'name3',      name3      FROM stScuSecuBasC
            UNION ALL
            SELECT id, 'name4',      name4      FROM stScuSecuBasC
            UNION ALL
            SELECT id, 'nameV2',     nameV2     FROM stScuSecuBasC
            UNION ALL
            SELECT id, 'nameAbbrV2', nameAbbrV2 FROM stScuSecuBasC
        ) AS u
        WHERE alias LIKE ?
        ORDER BY LEN(alias), alias
        """
        await cur.execute(like_sql, (f"%{kw}%",))
        row = await cur.fetchone()
        if row:
            return dict(zip([c[0] for c in cur.description], row))

        # nothing found
        return {}

# ── BOOT ──────────────────────────────────────────────────────────────
async def run() -> None:
    """Entry-point used by  python -m mssqlserver  (stdio)"""
    await mcp.run_stdio_async()          # FastMCP handles JSON-RPC loop


async def main():
    # single line — FastMCP handles stdio internally
    await mcp.run_stdio_async()

if __name__ == "__main__":
    mcp.run(transport="stdio")


