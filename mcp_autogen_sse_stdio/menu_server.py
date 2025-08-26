# menu_server.py
from mcp.server.fastmcp import FastMCP
import csv, re, pathlib
from typing import List, TypedDict, Dict, Any
import json

MENU_CSV = pathlib.Path(__file__).parent / "menu.csv"
MENU = {}  # {"魯肉便當": 80, ...}

# 讀取菜單一次即可
with open(MENU_CSV, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        MENU[row["品項"].strip()] = float(row["價格(元)"])

mcp = FastMCP("Menu")

class MenuRow(TypedDict):
    item: str
    price: float

@mcp.tool()
def show_menu_list() -> str:
    """
    Return the menu as ONE raw JSON array string (not a Python list),
    so the framework won't split it into multiple chunks.
    Example: [{"item":"魯肉便當","price":80.0}, ...]
    """
    arr = [{"item": name, "price": float(price)} for name, price in MENU.items()]
    return json.dumps(arr, ensure_ascii=False)

class OrderItem(TypedDict):
    item: str
    price: float
    qty: int

def _ch_to_int(s: str) -> int:
    # 支援 一二兩三四五六七八九十、十、二十、二十三…（簡單 1~99）
    map_ = {"零":0,"〇":0,"一":1,"二":2,"兩":2,"三":3,"四":4,"五":5,"六":6,"七":7,"八":8,"九":9}
    s = s.strip()
    if not s:
        return 0
    if s == "十":
        return 10
    if "十" in s:
        a, b = s.split("十", 1)
        tens = map_.get(a, 1) if a else 1
        ones = map_.get(b, 0) if b else 0
        return tens*10 + ones
    # 逐字拼
    v = 0
    for ch in s:
        if ch in map_:
            v = v*10 + map_[ch]
        else:
            return 0
    return v

def _parse_qty(q: str) -> int:
    q = q.strip()
    if q.isdigit():
        return int(q)
    v = _ch_to_int(q)
    return v if v > 0 else 0

def _build_item_regex() -> str:
    # 用 CSV 的鍵名逐字匹配（避免被正規化/改名）
    names = sorted(MENU.keys(), key=len, reverse=True)
    parts = [re.escape(n) for n in names]
    return "(" + "|".join(parts) + ")"

_ITEM_RE = _build_item_regex()

@mcp.tool()
def parse_order(text: str) -> List[OrderItem]:
    """
    Parse flexible order text. Supports:
      - '魯肉便當 x2', '魯肉便當×2', '魯肉便當*2', '魯肉便當 2 份', '魯肉便當2份'
      - '2份 魯肉便當', '二 魯肉便當', '二份魯肉便當'
    Returns: [{"item": <name>, "price": <float>, "qty": <int>}, ...]
    """
    t = text.strip()
    items: List[OrderItem] = []

    # 1) 形如：<item> [x|×|*]? <qty> [份|個]?
    pat_item_first = re.compile(
        rf"(?P<item>{_ITEM_RE})\s*(?:[\*xX×]?\s*(?P<q1>\d+|[零〇一二兩三四五六七八九十]+)\s*(?:份|個)?)"
    )

    # 2) 形如：<qty> [份|個]? <item>
    pat_qty_first = re.compile(
        rf"(?P<q2>\d+|[零〇一二兩三四五六七八九十]+)\s*(?:份|個)?\s*(?P<item>{_ITEM_RE})"
    )

    used: List[tuple[int,int]] = []

    def _add(item_name: str, qty: int):
        if qty <= 0:
            return
        price = float(MENU[item_name])
        items.append({"item": item_name, "price": price, "qty": qty})

    # 掃描兩種樣式（避免相互重疊：用 span 去重）
    for m in pat_item_first.finditer(t):
        s, e = m.span()
        if any(not (e <= s2 or s >= e2) for s2, e2 in used):
            continue
        qty = _parse_qty(m.group("q1") or "")
        _add(m.group("item"), qty)
        used.append((s, e))

    for m in pat_qty_first.finditer(t):
        s, e = m.span()
        if any(not (e <= s2 or s >= e2) for s2, e2 in used):
            continue
        qty = _parse_qty(m.group("q2") or "")
        _add(m.group("item"), qty)
        used.append((s, e))

    # 合併同品項、保持首次出現順序
    agg: Dict[str, OrderItem] = {}
    order: List[OrderItem] = []
    for it in items:
        k = it["item"]
        if k in agg:
            agg[k]["qty"] += it["qty"]
        else:
            agg[k] = {"item": k, "price": it["price"], "qty": it["qty"]}
    for m in pat_item_first.finditer(t):
        k = m.group("item")
        if k in agg and all(o["item"] != k for o in order):
            order.append(agg[k])
    for m in pat_qty_first.finditer(t):
        k = m.group("item")
        if k in agg and all(o["item"] != k for o in order):
            order.append(agg[k])

    return order

@mcp.tool()
def parse_and_bind(text: str) -> Dict[str, Any]:
    """
    One-shot: parse + attach prices + build arrays for Math_Agent.
    Returns:
      {
        "items": [{"item":..., "price":..., "qty":...}, ...],
        "prices": [ ... ],
        "quantities": [ ... ]
      }
    """
    order = parse_order(text)
    prices = [row["price"] for row in order]
    quantities = [row["qty"] for row in order]
    return {"items": order, "prices": prices, "quantities": quantities}

# @mcp.tool()
# def order_subtotals(text: str) -> list[float]:
#     """
#     Given the user's natural-language order, return a list of subtotals
#     (price * qty) as floats.  DO NOT sum them here – Math_Agent will.
#     """
#     subtotals = []
#     for item, qty in parse_order(text):
#         subtotals.append(MENU[item] * qty)
#     return subtotals

if __name__ == "__main__":
    mcp.run(transport="stdio")
