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
    If the caller forgets to limit rows, we auto-limit safely (see below).
    On ANY exception, write the SQL + traceback to stderr and
    return {"error": "...", "sql": "<the-sql>"} instead of null.

    "description": (
        "Run a read-only T-SQL statement (SELECT / WITH) against the "
        "Microsoft SQL Server warehouse and return rows as JSON. "
        "Always use 'internal stock id' in queries, with the column name stScuSecuBasC_id"
        "When querying with date, always type it out explicitly."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sql":   {"type": "string"},
            "limit": {"type": "integer", "default": 500},
        },
        "required": ["sql"]
    }

    """
    import sys, traceback, re

    # Basic guard
    low = sql.strip().lower()
    if not low.startswith(("select", "with")):
        raise ValueError("Only SELECT / WITH statements are allowed")

    # --- Auto-limit (robust) -------------------------------------------------
    # 1) We never create illegal TOP + OFFSET/FETCH combos.
    # 2) If the query starts with SELECT:
    #      - If it has no ORDER BY, TOP(limit) is injected.
    #      - If it has ORDER BY but no OFFSET/FETCH and no TOP, append OFFSET/FETCH.
    # 3) If the query starts with WITH (CTE), we avoid injecting TOP (complex to place);
    #    we only append OFFSET/FETCH if ORDER BY exists and no TOP is present.
    has_top   = re.search(r"\btop\b", low) is not None
    has_order = re.search(r"\border\s+by\b", low) is not None
    has_fetch = re.search(r"\boffset\s+\d+\s+rows\b|\bfetch\s+next\b", low) is not None

    # Normalize trailing semicolon before appending OFFSET/FETCH
    def strip_trailing_semicolon(s: str) -> tuple[str, bool]:
        s_stripped = s.rstrip()
        had = s_stripped.endswith(";")
        return (s_stripped[:-1].rstrip() if had else s_stripped, had)

    begins_with_select = low.startswith("select")
    begins_with_with   = low.startswith("with")

    if begins_with_select:
        if not has_order and not has_fetch and not has_top:
            # Case A: no ORDER BY → inject TOP(limit) right after SELECT
            sql = re.sub(r"^\s*select\b", f"SELECT TOP ({limit})", sql, count=1, flags=re.I)
        elif has_order and not has_fetch and not has_top:
            # Case B: ORDER BY but no FETCH and no TOP → append OFFSET/FETCH
            sql_no_semi, had_semi = strip_trailing_semicolon(sql)
            sql = f"{sql_no_semi} OFFSET 0 ROWS FETCH NEXT {limit} ROWS ONLY"
            if had_semi:
                sql += ";"

    elif begins_with_with:
        # For CTEs, avoid injecting TOP (would need to locate the OUTER SELECT).
        # Safe: only append OFFSET/FETCH when ORDER BY present and no TOP/FETCH yet.
        if has_order and not has_fetch and not has_top:
            sql_no_semi, had_semi = strip_trailing_semicolon(sql)
            sql = f"{sql_no_semi} OFFSET 0 ROWS FETCH NEXT {limit} ROWS ONLY"
            if had_semi:
                sql += ";"

    # ------------------------------------------------------------------------

    try:
        pool = await get_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql)
            cols = [c[0] for c in cur.description]
            rows = await cur.fetchall()
            return [dict(zip(cols, r)) for r in rows]

    except Exception as e:
        # --- dump details to server stderr (shows in runner log) ---
        print("\n=== SQL ERROR in query_sql_mssql ===", file=sys.stderr)
        print(sql, file=sys.stderr)
        traceback.print_exc(file=sys.stderr)

        # --- bubble a JSON error payload back to the host -----------
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
        Optional custom path.  If omitted, uses 'stTseStkPrcD_schema.csv'
        located in the same directory as server.py.

    Returns
    -------
    list[dict]
        Each row is a dict with keys:
        'Column_name', 'Explanation', 'Datatype', 'Availability'

    "description": "Return the stTseStkPrcD schema as JSON rows. DO NOT input file paths, use the default.",
    "parameters": {
        "type": "object",
        "properties": {
        "file": { "type": "string", "description": "Optional custom CSV path" }
        }
    }

    """
    path = _SCHEMA_CSV
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
    Resolve the internal `id` from stScuSecuBasC with this priority:

    1. Exact match on listCode             (e.g. '2330')
    2. Exact match on any alias column     – but ignoring all spaces
    3. Fallback: LIKE '%keyword%'          – also ignoring spaces

    "description": "Translate a company name / abbreviation / listCode to its internal id using stScuSecuBasC.",
    "parameters": {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "e.g. '台積電' or '2330'"}
        },
        "required": ["keyword"]
    }

    """
    pool = await get_pool()
    kw_raw = keyword.strip()

    # Strip ASCII space, full-width space (U+3000) and tabs
    kw_ns = kw_raw.replace(" ", "").replace("\u3000", "").replace("\t", "")

    async with pool.acquire() as conn, conn.cursor() as cur:

        # 1️⃣ exact listCode --------------------------------------------------
        await cur.execute(
            """SELECT id, 'listCode' AS matched_column, listCode AS matched_value
               FROM   stScuSecuBasC
               WHERE  listCode = ?""",
            (kw_raw,),
        )
        if (row := await cur.fetchone()):
            return dict(zip([c[0] for c in cur.description], row))

        # Helper expression to strip spaces in SQL
        def nosp(col: str) -> str:
            return (
                "REPLACE(REPLACE(REPLACE(" + col +
                ", N' ', N''), NCHAR(12288), N''), CHAR(9), N'')"
            )

        # 2️⃣ exact alias (space-insensitive) ----------------------------------
        exact_sql = f"""
        DECLARE @kw NVARCHAR(100) = ?;

        SELECT TOP (1) id, colname, alias
        FROM (
            SELECT id, 'name'        AS colname, name        AS alias, {nosp('name')}        AS alias_ns FROM stScuSecuBasC
            UNION ALL
            SELECT id, 'name3',      name3      AS alias, {nosp('name3')}      FROM stScuSecuBasC
            UNION ALL
            SELECT id, 'name4',      name4      AS alias, {nosp('name4')}      FROM stScuSecuBasC
            UNION ALL
            SELECT id, 'nameV2',     nameV2     AS alias, {nosp('nameV2')}     FROM stScuSecuBasC
            UNION ALL
            SELECT id, 'nameAbbrV2', nameAbbrV2 AS alias, {nosp('nameAbbrV2')} FROM stScuSecuBasC
        ) AS u
        WHERE alias_ns = @kw;
        """
        await cur.execute(exact_sql, (kw_ns,))
        if (row := await cur.fetchone()):
            return {
                "id":             row[0],
                "matched_column": row[1],
                "matched_value":  row[2].strip(),
            }

        # 3️⃣ LIKE '%kw%' fallback (space-insensitive) ------------------------
        like_sql = exact_sql.replace("= @kw", "LIKE '%' + @kw + '%'")
        await cur.execute(like_sql, (kw_ns,))
        if (row := await cur.fetchone()):
            return {
                "id":             row[0],
                "matched_column": row[1],
                "matched_value":  row[2].strip(),
            }

    # nothing found -----------------------------------------------------------
    return {}

# ---------------------------------------------------------------------------
# 🔎  Resolve industry id from name / Eng / Abbr  ----------------------------
# ---------------------------------------------------------------------------
@mcp.tool()
async def resolve_stock_industry(keyword: str) -> dict[str, Any]:
    """
    Translate an industry keyword to its internal `id` (misc.dbo.mtIndustryC).

    Priority
    --------
    1. Exact match on any alias column (name / nameEng / nameAbbr)
    2. Fallback: LIKE '%keyword%' ordered by shortest alias

    Returns {"id": 42, "matched_column": "name", "matched_value": "水泥"} or {}

    "description": "Translate an industry name / alias to its internal id via misc.dbo.mtIndustryC.",
    "parameters": {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "e.g. '半導體' or 'Electronics'"}
        },
        "required": ["keyword"]
    """
    pool = await get_pool()                   # same DSN, same login
    kw = keyword.strip()

    async with pool.acquire() as conn, conn.cursor() as cur:

        # 1️⃣  exact match ----------------------------------------------------
        exact_sql = """
        SELECT TOP (1) id, colname, alias
        FROM (
            SELECT id, 'name'      AS colname, name      AS alias FROM misc.dbo.mtIndustryC WHERE name      IS NOT NULL
            UNION ALL
            SELECT id, 'nameEng'   AS colname, nameEng   AS alias FROM misc.dbo.mtIndustryC WHERE nameEng   IS NOT NULL
            UNION ALL
            SELECT id, 'nameAbbr'  AS colname, nameAbbr  AS alias FROM misc.dbo.mtIndustryC WHERE nameAbbr  IS NOT NULL
        ) AS u
        WHERE alias = ?
        """
        await cur.execute(exact_sql, (kw,))
        row = await cur.fetchone()
        if row:
            return {"id": row[0], "matched_column": row[1], "matched_value": row[2].strip()}

        # 2️⃣  LIKE '%kw%' fallback ------------------------------------------
        like_sql = exact_sql + """
        ORDER BY LEN(alias), alias   -- shortest alias first
        """
        await cur.execute(like_sql.replace("alias = ?", "alias LIKE ?"), (f"%{kw}%",))
        row = await cur.fetchone()
        if row:
            return {"id": row[0], "matched_column": row[1], "matched_value": row[2].strip()}

    # nothing found
    return {}

# ---------------------------------------------------------------------------
# 🗂️  List all stocks in an industry  ----------------------------------------
# ---------------------------------------------------------------------------
@mcp.tool()
async def list_stocks_by_industry(industry_id: int) -> list[dict[str, Any]]:
    """
    Given an `industry_id` ( = mtIndustryC_id ),
    return all matching stocks from **stock.dbo.stScuSecuBasC**.

    Each row -> {"id": 748, "listCode": "2330", "nameAbbrV2": "台積電"}

    "description": "Return all stocks (id, listCode, nameAbbrV2) that belong to a given industry_id using stock.dbo.stScuSecuBasC.",
    "parameters": {
        "type": "object",
        "properties": {
            "industry_id": {
                "type": "integer",
                "description": "The numeric id from mtIndustryC.id"
            }
        },
        "required": ["industry_id"]
    }
    """
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, listCode, nameAbbrV2
            FROM   stock.dbo.stScuSecuBasC
            WHERE  mtIndustryC_id = ?
            ORDER  BY id

            """,
            (industry_id,),
        )
        cols = [c[0] for c in cur.description]      # ["id", "listCode", …]
        rows = await cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]

# ── BOOT ──────────────────────────────────────────────────────────────
async def run() -> None:
    """Entry-point used by  python -m mssqlserver  (stdio)"""
    await mcp.run_stdio_async()          # FastMCP handles JSON-RPC loop


async def main():
    # single line — FastMCP handles stdio internally
    await mcp.run_stdio_async()

if __name__ == "__main__":
    mcp.run(transport="stdio")


