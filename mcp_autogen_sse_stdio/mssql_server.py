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

# --- helpers for dynamic metric whitelist ---
_NUMERIC_TYPE_TOKENS = {"int", "bigint", "decimal", "numeric", "float", "real", "smallint", "tinyint", "money", "smallmoney"}

async def _load_rankable_metrics_from_csv() -> set[str]:
    """
    Read CSV via read_schema_csv() and return a set of numeric, available column names.
    Assumes CSV has columns: Column_name, Datatype, Availability (Y/N or blank).
    """
    rows = await read_schema_csv()
    allow: set[str] = set()
    for r in rows:
        col = (r.get("Column_name") or "").strip()
        dtype = (r.get("Datatype") or "").strip().lower()
        avail = (r.get("Availability") or "Y").strip().upper()
        if not col or avail.startswith("N"):
            continue
        # crude numeric check: column's datatype string contains any numeric token
        if any(tok in dtype for tok in _NUMERIC_TYPE_TOKENS):
            allow.add(col)
    return allow

def _is_safe_identifier(s: str) -> bool:
    # Defensive: ensure metric looks like a normal SQL identifier
    # (letters/underscore start; then letters/digits/underscore)
    import re
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", s))


from typing import Any, Dict, List

@mcp.tool()
async def price_change_mssql(
    stock_id: int,
    start_date: str,
    end_date: str,
    include_series: bool = False,
    baseline: str = "prev_close",         # "in_window" or "prev_close"
    price_basis: str = "close"           # close|open|high|low|hl2|hlc3|ohlc4|wclose
) -> Dict[str, Any]:
    """
    Compute price change for a given stock over [start_date, end_date) (absolute and percent),
    supporting different price bases and baseline choices.

    Price basis (price_basis) mapping:
      close  -> [salePrice]
      open   -> [openPrice]
      high   -> [highPrice]
      low    -> [lowPrice]
      hl2    -> ([highPrice]+[lowPrice]) / 2.0
      hlc3   -> ([highPrice]+[lowPrice]+[salePrice]) / 3.0
      ohlc4  -> ([openPrice]+[highPrice]+[lowPrice]+[salePrice]) / 4.0
      wclose -> ([highPrice]+[lowPrice]+2.0*[salePrice]) / 4.0

    Baseline (baseline):
      - "in_window": use the first available trading day inside the window as the start,
                     and the last available trading day inside the window as the end.
      - "prev_close": use the most recent trading day BEFORE start_date as the start,
                      and the last available trading day inside the window as the end.
                      (e.g., "July return = July month-end vs June month-end")

    Return example:
      {
        "stock_id": <int>,
        "price_basis": "<str>",
        "baseline": "<str>",
        "start_date": "YYYY-MM-DD",
        "end_date": "YYYY-MM-DD",
        "first_date": "YYYY-MM-DD" | None,
        "first_price": <float> | None,
        "last_date": "YYYY-MM-DD" | None,
        "last_price": <float> | None,
        "abs_change": <float> | None,      # last_price - first_price
        "pct_change": <float> | None,      # (last - first) / first
        "series": [{"date":"YYYY-MM-DD","price":<float>}, ...] | None  # only when include_series=True
      }

    Notes:
      - end_date is exclusive; e.g., for July use start_date='YYYY-07-01', end_date='YYYY-08-01'.
      - If either endpoint is missing (no data), abs_change/pct_change will be None.
    """

    # Allowed baselines and basis expressions
    BASELINES = {"in_window", "prev_close"}
    BASIS_SQL = {
        "close":  "[closePrice]",
        "open":   "[openPrice]",
        "high":   "[highPrice]",
        "low":    "[lowPrice]",
        "hl2":    "([highPrice]+[lowPrice]) / 2.0",   # HL2（中間價）
        "hlc3":   "([highPrice]+[lowPrice]+[colsePrice]) / 3.0",   # HLC3（典型價）
        "ohlc4":  "([openPrice]+[highPrice]+[lowPrice]+[colsePrice]) / 4.0", # OHLC4（均價）
        "wclose": "([highPrice]+[lowPrice]+2.0*[colsePrice]) / 4.0", # 加權收盤（WCL）
    }

    b = (baseline or "in_window").lower()
    if b not in BASELINES:
        raise ValueError(f"Unsupported baseline '{baseline}'. Use one of: {', '.join(sorted(BASELINES))}")

    pb = (price_basis or "close").lower()
    if pb not in BASIS_SQL:
        raise ValueError(f"Unsupported price_basis '{price_basis}'. Use one of: {', '.join(BASIS_SQL.keys())}")

    expr = BASIS_SQL[pb]
    sid = int(stock_id)

    # Build SQL based on baseline
    if b == "prev_close":
        # Start: the most recent trading day before start_date
        # End:   the last trading day inside [start_date, end_date)
        summary_sql = f"""
            WITH W AS (
                SELECT ymdOn, {expr} AS price
                FROM dbo.stTseStkPrcD
                WHERE stScuSecuBasC_id = {sid}
                  AND ymdOn >= '{start_date}' AND ymdOn < '{end_date}'
                  AND {expr} IS NOT NULL
            ),
            P0 AS (
                SELECT TOP (1) ymdOn AS first_date, {expr} AS first_price
                FROM dbo.stTseStkPrcD
                WHERE stScuSecuBasC_id = {sid}
                  AND ymdOn < '{start_date}'
                  AND {expr} IS NOT NULL
                ORDER BY ymdOn DESC
            ),
            L AS (
                SELECT TOP (1) ymdOn AS last_date, price AS last_price
                FROM W ORDER BY ymdOn DESC
            )
            SELECT
                P0.first_date,
                P0.first_price,
                L.last_date,
                L.last_price,
                CASE WHEN P0.first_price IS NULL OR L.last_price IS NULL THEN NULL
                     ELSE L.last_price - P0.first_price END AS abs_change,
                CASE WHEN P0.first_price IS NULL OR P0.first_price = 0 OR L.last_price IS NULL THEN NULL
                     ELSE (L.last_price - P0.first_price) / P0.first_price END AS pct_change
            FROM P0 CROSS JOIN L;
        """.strip()
    else:
        # in_window: first/last trading day inside the window
        summary_sql = f"""
            WITH W AS (
                SELECT ymdOn, {expr} AS price
                FROM dbo.stTseStkPrcD
                WHERE stScuSecuBasC_id = {sid}
                  AND ymdOn >= '{start_date}' AND ymdOn < '{end_date}'
                  AND {expr} IS NOT NULL
            ),
            F AS (
                SELECT TOP (1) ymdOn AS first_date, price AS first_price
                FROM W ORDER BY ymdOn ASC
            ),
            L AS (
                SELECT TOP (1) ymdOn AS last_date, price AS last_price
                FROM W ORDER BY ymdOn DESC
            )
            SELECT
                F.first_date,
                F.first_price,
                L.last_date,
                L.last_price,
                CASE WHEN F.first_price IS NULL OR L.last_price IS NULL THEN NULL
                     ELSE L.last_price - F.first_price END AS abs_change,
                CASE WHEN F.first_price IS NULL OR F.first_price = 0 OR L.last_price IS NULL THEN NULL
                     ELSE (L.last_price - F.first_price) / F.first_price END AS pct_change
            FROM F CROSS JOIN L;
        """.strip()

    # Execute summary query
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(summary_sql)
        row = await cur.fetchone()
        if not row:
            result: Dict[str, Any] = {
                "stock_id": sid,
                "price_basis": pb,
                "baseline": b,
                "start_date": start_date,
                "end_date": end_date,
                "first_date": None,
                "first_price": None,
                "last_date": None,
                "last_price": None,
                "abs_change": None,
                "pct_change": None,
                "series": [] if include_series else None
            }
            return result

        cols_summary = [c[0] for c in cur.description]
        summary = dict(zip(cols_summary, row))

    # Assemble result
    result: Dict[str, Any] = {
        "stock_id": sid,
        "price_basis": pb,
        "baseline": b,
        "start_date": start_date,
        "end_date": end_date,
        **summary
    }

    # Optional daily series inside the window (same price_basis)
    if include_series:
        series_sql = f"""
            SELECT ymdOn AS [date], {expr} AS price
            FROM dbo.stTseStkPrcD
            WHERE stScuSecuBasC_id = {sid}
              AND ymdOn >= '{start_date}' AND ymdOn < '{end_date}'
              AND {expr} IS NOT NULL
            ORDER BY ymdOn ASC;
        """.strip()
        async with pool.acquire() as conn2, conn2.cursor() as cur2:
            await cur2.execute(series_sql)
            rows = await cur2.fetchall()
            cols_series = [c[0] for c in cur2.description]
            result["series"] = [dict(zip(cols_series, r)) for r in rows]
    else:
        result["series"] = None

    return result


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
    Return the top-N rows for a given stock and metric within a date window.
        If latest_only = true, first lock to the latest trading day inside the window,
        then rank on that day; otherwise rank across the whole window.

        Dates: YYYY-MM-DD (half-open interval [start_date, end_date); end is exclusive).

        Workflow requirement:
        1) Call read_schema_csv() first and obtain the 'Column_name' list.
        2) Choose `metric` ONLY from those column names (prefer numeric columns).
        3) Then call this tool.

        Ordering: DESC (最大在前 / largest first) or ASC (最小在前 / smallest first).

        Read-only: Builds a safe SELECT against dbo.stTseStkPrcD and dbo.stScuSecuBasC.

        Returns: array of rows with fields → trading_date, metric_value, stock_name, listCode.

    parameters:
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
                "pattern": "^[A-Za-z_][A-Za-z0-9_]*$",
                "description": "Column name to rank by. MUST be chosen from read_schema_csv().Column_name (prefer numeric columns). Do NOT invent or translate names."
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

    examples:
    [
        {
            "stock_id": 9722,
            "metric": "txnShares",
            "start_date": "2025-07-01",
            "end_date": "2025-08-01",
            "top_n": 3,
            "order_dir": "DESC",
            "latest_only": false
        },
        {
            "stock_id": 2330,
            "metric": "salePrice",
            "start_date": "2025-07-15",
            "end_date": "2025-08-01",
            "top_n": 5,
            "order_dir": "ASC",
            "latest_only": true
        }
    ]

    """
    # --- dynamic whitelist from CSV ---
    allowed_cols = await _load_rankable_metrics_from_csv()

    if not _is_safe_identifier(metric):
        raise ValueError("Unsupported metric: invalid identifier")

    if metric not in allowed_cols:
        # small helpful error listing the first few allowed metrics
        sample = ", ".join(sorted(list(allowed_cols))[:12])
        raise ValueError(f"Unsupported metric '{metric}'. Choose a numeric column from schema. e.g.: {sample} …")

    # use bracket quoting to be safe with identifiers
    col_sql = f"[{metric}]"

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
                p.{col_sql} AS metric_value,
                s.name  AS stock_name,
                s.listCode
            FROM dbo.stTseStkPrcD AS p
            JOIN dbo.stScuSecuBasC AS s ON p.stScuSecuBasC_id = s.id
            WHERE p.stScuSecuBasC_id = {sid}
              AND p.ymdOn = (SELECT d FROM D)
            ORDER BY p.{col_sql} {order}, p.ymdOn DESC
        """.strip()
    else:
        sql = f"""
            SELECT TOP ({n})
                p.ymdOn AS trading_date,
                p.{col_sql} AS metric_value,
                s.name  AS stock_name,
                s.listCode
            FROM dbo.stTseStkPrcD AS p
            JOIN dbo.stScuSecuBasC AS s ON p.stScuSecuBasC_id = s.id
            WHERE p.stScuSecuBasC_id = {sid}
              AND p.ymdOn >= '{start_date}' AND p.ymdOn < '{end_date}'
            ORDER BY p.{col_sql} {order}, p.ymdOn DESC
        """.strip()

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
async def main():
    # single line — FastMCP handles stdio internally
    await mcp.run_stdio_async()

if __name__ == "__main__":
    mcp.run(transport="stdio")


