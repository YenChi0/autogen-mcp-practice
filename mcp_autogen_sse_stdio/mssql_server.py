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
import sys, traceback, re
import aioodbc
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
import sys, traceback, re

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

# top_metric_mssql : Get top-N metrics for a stock within a date range
@mcp.tool()
async def top_metric_mssql(
    stock_id: int,
    metric: str,
    start_date: str,
    end_date: str,
    top_n: int = 1,
    order_dir: str = "DESC",
    latest_only: bool = False
) -> list[dict]:
    """
    Return top-N rows for a given metric within a date window for one stock.
    If latest_only=True, first lock to the latest trading day within the window,
    then rank within that day; else rank across the whole window.

    description :
    Return the top-N rows for a given stock and metric over a date window.
    Optionally set latest_only=true to first lock to the latest trading day inside the window, then rank on that day.

    Dates: YYYY-MM-DD (half-open interval [start_date, end_date); end is exclusive).

    Metrics:

    txnValue = 成交金額

    txnShares = 成交量

    salePrice = 收盤價

    Ordering: DESC (最大在前) or ASC (最小在前).

    Read-only: Internally builds a safe SELECT against dbo.stTseStkPrcD and dbo.stScuSecuBasC.

    Returns an array of rows with: trading_date, metric_value, stock_name, listCode.

    Parameters :
    {
        "type": "object",
        "properties": {
            "stock_id": {
            "type": "integer",
            "minimum": 1,
            "description": "Internal stock ID (stScuSecuBasC.id / p.stScuSecuBasC_id). Example: 9722."
            },
            "metric": {
            "type": "string",
            "enum": ["txnValue", "txnShares", "salePrice"],
            "description": "Column to rank by: txnValue=成交金額, txnShares=成交量, salePrice=收盤價."
            },
            "start_date": {
            "type": "string",
            "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
            "description": "Inclusive start date (YYYY-MM-DD)."
            },
            "end_date": {
            "type": "string",
            "pattern": "^\\d{4}-\\d{2}-\\d{2}$",
            "description": "Exclusive end date (YYYY-MM-DD). Must be later than start_date."
            },
            "top_n": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
            "default": 1,
            "description": "Number of rows to return after sorting."
            },
            "order_dir": {
            "type": "string",
            "enum": ["ASC", "DESC"],
            "default": "DESC",
            "description": "Sort direction for the metric (DESC = largest first)."
            },
            "latest_only": {
            "type": "boolean",
            "default": false,
            "description": "If true, first lock to the latest trading date within [start_date, end_date) for this stock, then rank on that day."
            }
        },
        "required": ["stock_id", "metric", "start_date", "end_date"]
    }

    example :
    {
        "stock_id": 9722,
        "metric": "txnShares",
        "start_date": "2025-07-01",
        "end_date": "2025-08-01",
        "top_n": 3,
        "order_dir": "DESC",
        "latest_only": false
    }
    
    """
    # ---- whitelist ----
    METRIC_MAP = {
        "txnValue": "txnValue",
        "txnShares": "txnShares",
        "salePrice": "salePrice",
        # add others you actually store
    }
    if metric not in METRIC_MAP:
        raise ValueError("Unsupported metric")
    col = METRIC_MAP[metric]

    order = "DESC" if str(order_dir).upper() == "DESC" else "ASC"
    n = int(top_n)
    sid = int(stock_id)

    if latest_only:
        sql = f"""
            WITH D AS (
            SELECT MAX(ymdOn) AS d
            FROM dbo.stTseStkPrcD
            WHERE stScuSecuBasC_id = {sid}
                AND ymdOn >= '{start_date}' AND ymdOn < '{end_date}'
            )
            SELECT TOP ({n})
            p.ymdOn AS trading_date,
            p.{col} AS metric_value,
            s.name  AS stock_name,
            s.listCode
            FROM dbo.stTseStkPrcD p
            JOIN dbo.stScuSecuBasC s ON p.stScuSecuBasC_id = s.id
            WHERE p.stScuSecuBasC_id = {sid}
            AND p.ymdOn = (SELECT d FROM D)
            ORDER BY p.{col} {order}, p.ymdOn DESC
            """.strip()
    else:
        sql = f"""
            SELECT TOP ({n})
            p.ymdOn AS trading_date,
            p.{col} AS metric_value,
            s.name  AS stock_name,
            s.listCode
            FROM dbo.stTseStkPrcD p
            JOIN dbo.stScuSecuBasC s ON p.stScuSecuBasC_id = s.id
            WHERE p.stScuSecuBasC_id = {sid}
            AND p.ymdOn >= '{start_date}' AND p.ymdOn < '{end_date}'
            ORDER BY p.{col} {order}, p.ymdOn DESC
            """.strip()

    # Reuse your pool + execution path (same as query_sql_mssql)
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(sql)
        cols = [c[0] for c in cur.description]
        rows = await cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]


# query_sql_mssql : input is a raw SQL string, output is a list of dicts from the result set.
@mcp.tool()
async def query_sql_mssql(sql: str, limit: int = 500) -> List[Dict[str, Any]] | Dict[str, str]:
    """
    Run a **read-only** T-SQL statement (SELECT / WITH).
    Auto-limit: if the query has neither TOP nor FETCH, add TOP(limit).
    If it has ORDER BY but no TOP/FETCH, append OFFSET/FETCH.
    Never mix TOP and OFFSET/FETCH in the same query.

    "description": "Run a **read-only** T-SQL query and return rows as JSON."
    "**STRICT RULES (MSSQL dialect only):**\n• **Allowed statements:** SELECT / WITH only. (No INSERT/UPDATE/DELETE/MERGE/ALTER/DROP/EXEC/sp_*)"
    "• **No `LIMIT`.** Use **`TOP (N)`** or **`ORDER BY ... OFFSET 0 ROWS FETCH NEXT N ROWS ONLY`**."
    "• If you use `FETCH NEXT`, you **must** include an `ORDER BY` clause.• **Fully qualify tables** with schema: e.g. `dbo.stTseStkPrcD`, `dbo.stScuSecuBasC`, `misc.dbo.mtIndustryC`."
    "• Use **'stScuSecuBasC_id'** for filtering by internal stock id."
    "• Keep result sets small: include `TOP (N)` or `OFFSET/FETCH` (the tool will auto-limit if missing).",
    "parameters": {
        "type": "object",
        "properties": {
        "sql": {
            "type": "string",
            "description": "A single **T-SQL** SELECT/WITH statement that obeys the rules above."
        },
        "limit": {
            "type": "integer",
            "default": 500,
            "description": "Soft cap. If your query has no TOP/FETCH, the tool will auto-limit to this."
        }
        },
        "required": ["sql"]
    }

    """
    
    low_src = sql.strip()
    sql = re.sub(r";+\s*$", "", low_src, flags=re.S)
    low = sql.lower()
    
    if not low.startswith(("select", "with")):
        raise ValueError("Only SELECT / WITH statements are allowed")

    # Detect presence of limiting clauses
    has_top   = bool(re.search(r"\bselect\s+top\s*\(", low, flags=re.I))
    has_fetch = " fetch next " in low
    has_order = " order by " in low

    # Auto-limit rules:
    # - If TOP present: leave as-is (do NOT append FETCH).
    # - Else if FETCH present: leave as-is.
    # - Else if ORDER BY present: append OFFSET/FETCH.
    # - Else: inject TOP (limit).
    if not has_top and not has_fetch:
        if has_order:
            sql = f"{sql} OFFSET 0 ROWS FETCH NEXT {limit} ROWS ONLY"
        else:
            sql = re.sub(r"(?i)^\s*select\s+", f"SELECT TOP ({limit}) ", sql, count=1)

    try:
        # Optional: log exactly what we execute
        print("\nDEBUG Executed SQL (server) →\n" + sql, file=sys.stderr)

        pool = await get_pool()
        async with pool.acquire() as conn, conn.cursor() as cur:
            # set per-statement timeout (seconds) BEFORE running the query
            try:
                conn.timeout = 30
            except Exception:
                pass

            await cur.execute(sql)
            cols = [c[0] for c in cur.description]
            rows = await cur.fetchall()
            return [dict(zip(cols, r)) for r in rows]

    except Exception as e:
        print("\n=== SQL ERROR in query_sql_mssql ===", file=sys.stderr)
        print(sql, file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
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
# Maps a user keyword (公司名 / 簡稱 / listCode) to the internal stock id in stScuSecuBasC.
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
# 🔁  Resolve stock name from internal id  -----------------------------------
# ---------------------------------------------------------------------------
# resolve_stock_name_mssql : Given an internal `id`, return the primary company name from stock.dbo.stScuSecuBasC (`name` column). Also returns listCode.
@mcp.tool()
async def resolve_stock_name_mssql(stock_id: int | str) -> Dict[str, Any]:
    """
    Given an internal `id`, return the primary company name from
    stock.dbo.stScuSecuBasC (`name` column). Also returns listCode.

    Returns {"id": 2330, "name": "台積電", "listCode": "2330"} or {} if not found.

    "description": "Return the primary company name ('name') for a given internal stock id from stScuSecuBasC.",
    "parameters": {
        "type": "object",
        "properties": {
            "stock_id": {
                "type": ["integer", "string"],
                "description": "Internal id from stScuSecuBasC (e.g., 2330)"
            }
        },
        "required": ["stock_id"]
    }

    """
    pool = await get_pool()
    try:
        sid = int(stock_id)
    except (TypeError, ValueError):
        raise ValueError("stock_id must be an integer (or numeric string)")

    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, listCode, name
            FROM   stScuSecuBasC
            WHERE  id = ?
            """,
            (sid,),
        )
        row = await cur.fetchone()
        if not row:
            return {}

        _id, list_code, name = row

        # normalize whitespace: strip, collapse, convert full-width spaces
        def normalize(s: str | None) -> str:
            if s is None:
                return ""
            s = s.replace("\u3000", " ")  # full-width space → normal space
            return " ".join(s.strip().split())

        return {
            "id": _id,
            "name": normalize(name),
            "listCode": normalize(list_code),
        }

# ---------------------------------------------------------------------------
# 🔎  Resolve industry id from name / Eng / Abbr  (returns market 1 and 2)
# ---------------------------------------------------------------------------
# resolve_stock_industry : Translate an industry keyword to its internal `id` from misc.dbo.mtIndustryC.
# given industry keyword, return its internal id for both markets.
@mcp.tool()
async def resolve_stock_industry(keyword: str) -> dict[str, Any]:
    """
    Translate an industry keyword to its internal `id` from misc.dbo.mtIndustryC,
    checking BOTH mtMarketC_id = 1 and = 2. For each market, try:
      1) exact match on any alias column (name / nameEng / nameAbbr)
      2) fallback: LIKE '%keyword%' ordered by shortest alias, then alphabetic

    Returns:
      {
        "market_1": {"id": ..., "matched_column": "...", "matched_value": "..."} | None,
        "market_2": {"id": ..., "matched_column": "...", "matched_value": "..."} | None
      }

    "description": "Resolve an industry keyword to internal industry ids from misc.dbo.mtIndustryC for two markets. After calling this tool, ALWAYS inspect both keys: for each non-null `id`, call `list_stocks_by_industry` (once per id), merge the resulting stock ids, and use the COMBINED set in subsequent SQL filters. If only one market is present, just use that one.",
    "parameters": {
        "type": "object",
        "properties": {
        "keyword": { "type": "string", "description": "Industry keyword, e.g. '半導體' or '水泥'." }
        },
        "required": ["keyword"]
    }

    """
    pool = await get_pool()
    kw = keyword.strip()

    async def _resolve_for_market(cur, market_id: int) -> dict[str, Any] | None:
        base = """
        WITH U AS (
            SELECT id, 'name'     AS colname, name     AS alias
            FROM   misc.dbo.mtIndustryC
            WHERE  mtMarketC_id = ? AND name     IS NOT NULL
            UNION ALL
            SELECT id, 'nameEng'  AS colname, nameEng  AS alias
            FROM   misc.dbo.mtIndustryC
            WHERE  mtMarketC_id = ? AND nameEng  IS NOT NULL
            UNION ALL
            SELECT id, 'nameAbbr' AS colname, nameAbbr AS alias
            FROM   misc.dbo.mtIndustryC
            WHERE  mtMarketC_id = ? AND nameAbbr IS NOT NULL
        )
        """

        # 1) exact
        exact_sql = base + " SELECT TOP (1) id, colname, alias FROM U WHERE alias = ?;"
        await cur.execute(exact_sql, (market_id, market_id, market_id, kw))
        row = await cur.fetchone()
        if row:
            return {"id": row[0], "matched_column": row[1], "matched_value": (row[2] or "").strip()}

        # 2) LIKE fallback
        like_sql = base + """
            SELECT TOP (1) id, colname, alias
            FROM U
            WHERE alias LIKE ?
            ORDER BY LEN(alias), alias;
        """
        await cur.execute(like_sql, (market_id, market_id, market_id, f"%{kw}%"))
        row = await cur.fetchone()
        if row:
            return {"id": row[0], "matched_column": row[1], "matched_value": (row[2] or "").strip()}

        return None

    async with pool.acquire() as conn, conn.cursor() as cur:
        res1 = await _resolve_for_market(cur, 1)
        res2 = await _resolve_for_market(cur, 2)

    return {"market_1": res1, "market_2": res2}



# ---------------------------------------------------------------------------
# 🗂️  List all stocks in an industry  ----------------------------------------
# ---------------------------------------------------------------------------
# list_stocks_by_industry : query company stocks by industry_id
# given an 上市產業ID or 上櫃產業ID, return all matching stocks from **stock.dbo.stScuSecuBasC**.
@mcp.tool()
async def list_stocks_by_industry(industry_id: int) -> list[int]:
    """
    Given an `industry_id` (= mtIndustryC_id), return a list of internal stock IDs.

    "description": "Return all stocks id that belong to a given industry_id using stock.dbo.stScuSecuBasC.",
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
            SELECT id
            FROM   stock.dbo.stScuSecuBasC
            WHERE  mtIndustryC_id = ?
            ORDER  BY id
            """,
            (industry_id,),
        )
        rows = await cur.fetchall()
        return [int(r[0]) for r in rows]


# ── BOOT ──────────────────────────────────────────────────────────────
async def run() -> None:
    """Entry-point used by  python -m mssqlserver  (stdio)"""
    await mcp.run_stdio_async()          # FastMCP handles JSON-RPC loop


async def main():
    # single line — FastMCP handles stdio internally
    await mcp.run_stdio_async()

if __name__ == "__main__":
    mcp.run(transport="stdio")


