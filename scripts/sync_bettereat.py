#!/usr/bin/env python3
# BETTEREAT sold-list sync v6 — 取代 repo 內 scripts/sync_bettereat.py（v5）。
# *** 未推上 GitHub：呢份只係放喺 ~/workspace/bettereat-order-page/ 等 parent 處理 ***
# v6 唯一改動（優惠系統，PROMO-DESIGN.md §6）：加讀「銷售紀錄」表，
# 按（日期 ← 單號入面嘅 YYYYMMDD、門店、產品）計每條記錄嘅實際金額（優惠後），
# 寫入 sold JSON 每條 record 嘅 "amount"；冇網站銷售行嗰啲 record 保持 amount: null，
# 報表跌返標準價。滿減金額按該單各行金額比例攤分（最大餘數法），確保每日總數啱。
# v5 改動（P0 第 2 項）：fetch_products() 加 "cost": num(f.get("成本"))，
# 等 T1_產品主檔嘅「成本」欄同步入 bettereat_products.json，供報表計毛利。
# T1 未有「成本」欄／未填數嗰陣 cost = null，網站顯示「待填成本」。
# 其餘邏輯同 v4 一字不改（SNAPSHOT archive 規則、V1 Base、history 保留）。
# 讀銷售紀錄表失敗唔會阻住同步（amount 全部留 null）。
# Reads the BETTEREAT HONG KONG LIMITED org's "BETTEREAT - Dessert V1" base.
# V1 differences vs the old base: remaining is split per store, and
# Total 剩餘量 equals the row's store remaining on every row (verified
# 2026-10-28: 174/174 MJF/MAP/MKH rows match), so Total 剩餘量 is the
# remaining source, with fallbacks to 剩餘量 and 剩餘量 {store}.
# The Feishu C table's date field is a TODAY() formula (always "today"), so the
# website keeps its own frozen record instead: every run preserves the existing
# history in bettereat_sold_data.json and appends today's snapshot keyed by the
# RUN date (HKT). A snapshot, once written, is never changed by later runs —
# except that a re-run on the SAME day refreshes that day's snapshot (end-of-day
# state wins). Rule change 2026-10-25 (Yan's decision): EVERY run date opens
# its own snapshot, even if the data is identical to the previous day —
# the archive is a daily record, not a change log.
# Credentials from env: FEISHU_APP_ID / FEISHU_APP_SECRET (GitHub Secrets).
# 2026-11-05: also dumps T1_產品主檔 to bettereat_products.json so the
# order page product list tracks the Feishu master (a T1 fetch failure
# never blocks the sold-data sync).
import json, os, re, urllib.request
from datetime import datetime, timezone, timedelta

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU = "https://open.feishu.cn"
APP_TOKEN = "YvpCbgjTia7wqWsMjNhcPwL7nih"      # BETTEREAT - Dessert V1 base (BETTEREAT HONG KONG LIMITED)
C_TABLE = "tblJuBUdRyqbL8Pq"                    # V1 C_每日分配及出貨記錄 (main table, 232 rows = 58 products x 4 stores)
T1_TABLE = "tblueJrNjwgQk8nN"                   # V1 T1_產品主檔 (59 products, verified 2026-11-05)
SALES_TABLE = "tblecgnQ7n8Q0r7X"                # V1 銷售紀錄表（網站訂單；v6 讀佢計實際金額）
DISCOUNT_PRODUCT = "滿減優惠"                   # 滿減行喺銷售紀錄表嘅產品名（Worker 內部構造，負金額，唔入按產品檢視）
PRODUCTS_OUT = os.environ.get("PRODUCTS_PATH", "bettereat_products.json")
OUT = os.environ.get("OUT_PATH", "bettereat_sold_data.json")
HKT = timezone(timedelta(hours=8))
BOGUS_KEYS = {"1970-01-01", ""}

def call(method, url, token=None, payload=None):
    import time, http.client, urllib.error
    data = json.dumps(payload).encode() if payload is not None else None
    last = None
    for attempt in range(3):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept-Encoding", "gzip")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, http.client.HTTPException, ConnectionError) as e:
            last = e
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"request failed after 3 attempts: {last}")

def get_token():
    r = call("POST", f"{FEISHU}/open-apis/auth/v3/tenant_access_token/internal",
             payload={"app_id": APP_ID, "app_secret": APP_SECRET})
    if r.get("code") != 0:
        raise RuntimeError(f"token failed: {r.get('code')} {r.get('msg')}")
    return r["tenant_access_token"]

def list_records(token, table_id):
    # small pages + gzip + retries: long responses get cut by the egress proxy
    items, page = [], None
    while True:
        url = f"{FEISHU}/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}/records?page_size=50"
        if page:
            url += f"&page_token={page}"
        r = call("GET", url, token=token)
        if r.get("code") != 0:
            raise RuntimeError(f"list records failed: {r.get('code')} {r.get('msg')}")
        d = r.get("data") or {}
        items.extend(d.get("items") or [])
        if not d.get("has_more"):
            return items
        page = d.get("page_token")

def text_of(v):
    if isinstance(v, list):
        return "".join(str(x.get("text", "")) for x in v if isinstance(x, dict))
    return "" if v is None else str(v)

def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _remaining(f):
    return num(f.get("Total 剩餘量"))

def _short_name(name):
    return re.sub(r"\s*[\(（][^\)）]*[\)）]\s*$", "", name or "").strip()

def _per_pan_map(products):
    # T1「No of pcs」＝每盤件數；以產品名（全名＋去括號短名）做對照，
    # 因為 C 表嘅產品編號同 T1 唔完全對得上（有改號／已刪產品），名先對得齊。
    m = {}
    for p in products or []:
        pcs = p.get("no_of_pcs")
        name = p.get("product_name") or ""
        if pcs is None or not name:
            continue
        if name not in m:
            m[name] = pcs
        short = _short_name(name)
        if short and short not in m:
            m[short] = pcs
    return m

def _alloc_pcs(c_name, allocated, existing_pcs, per_pan):
    # C 表「分配量」係盤數，要轉返件數：分配（件）＝ 分配量（盤）× T1 每盤件數。
    # C 自己有「No of pcs」就用返 C 嗰個；對唔到產品名就留空，唔好估。
    if existing_pcs is not None:
        return existing_pcs
    if allocated is None or not per_pan:
        return None
    per = per_pan.get(c_name)
    if per is None:
        per = per_pan.get(_short_name(c_name))
    if per is None:
        return None
    v = allocated * per
    return int(v) if v == int(v) else v

def fetch_current(token, per_pan=None):
    rows = []
    for it in list_records(token, C_TABLE):
        f = it.get("fields") or {}
        sold = num(f.get("已銷售"))
        if sold is None or sold <= 0:
            continue
        if sold == int(sold):
            sold = int(sold)
        allocated = num(f.get("分配量"))
        rows.append({
            "store": text_of(f.get("門店代號")),
            "product_code": text_of(f.get("產品編號")),
            "product_name": text_of(f.get("產品名稱")),
            "unit": text_of(f.get("計量單位")),
            "allocated_pans": allocated,
            "no_of_pcs": _alloc_pcs(text_of(f.get("產品名稱")), allocated, num(f.get("No of pcs")), per_pan or {}),
            "remaining_pcs": _remaining(f),
            "sold_pcs": sold,
        })
    rows.sort(key=lambda r: (r["store"], r["product_code"]))
    return rows

def fetch_products(token):
    rows = []
    for it in list_records(token, T1_TABLE):
        f = it.get("fields") or {}
        code = text_of(f.get("產品編號"))
        if not code:
            continue
        pcs = num(f.get("No of pcs"))
        if pcs is not None and pcs == int(pcs):
            pcs = int(pcs)
        price = num(f.get("標準售價"))
        if price is not None and price == int(price):
            price = int(price)
        cost = num(f.get("成本"))
        if cost is not None and cost == int(cost):
            cost = int(cost)
        rows.append({
            "product_code": code,
            "product_name": text_of(f.get("產品名稱")),
            "category": text_of(f.get("大類")),
            "subcategory": text_of(f.get("類別")),
            "cabinet": text_of(f.get("存放櫃")),
            "unit": text_of(f.get("計量單位")),
            "no_of_pcs": pcs,
            "standard_price": price,
            "cost": cost,
            "sku": text_of(f.get("E365商品編號SKU")),
            "status": text_of(f.get("狀態")),
            "remarks": text_of(f.get("備註")),
        })
    rows.sort(key=lambda r: r["product_code"])
    return rows

# ===== v6：實際金額（優惠後）=====
def _round2(x):
    import math
    return math.floor(x * 100 + 0.5) / 100

def _order_date(order_no):
    # 單號入面嘅 YYYYMMDD（B + 店代碼 + YYYYMMDD + 序號；舊格式 B20261106-002 冇店代碼）
    m = re.match(r"^B([A-Z]{3})?(\d{8})-\d{3}$", order_no or "")
    if not m:
        return None
    d = m.group(2)
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"

def _store_key(store):
    # 門店全名「謝斐 (MJF)」→「謝斐」（同 C 表門店代號／銷售紀錄取貨門店對返）
    return (store or "").split("(")[0].strip()

def sales_lines_from_items(items):
    # 銷售紀錄表原始行 → 已確認行（待確認／已取消唔計；滿減行另外標記）
    lines = []
    for it in items:
        f = it.get("fields") or {}
        status = text_of(f.get("狀態")) or "已確認"
        if status in ("待確認", "已取消"):
            continue
        order = text_of(f.get("訂單編號"))
        date = _order_date(order)
        if not date:
            continue
        product = text_of(f.get("產品")).strip()
        if not product:
            continue
        qty = num(f.get("數量")) or 0
        subtotal = num(f.get("總金額")) or 0
        lines.append({
            "order": order, "date": date, "store": _store_key(text_of(f.get("取貨門店"))),
            "product": product, "qty": qty, "subtotal": subtotal,
            "discount": product == DISCOUNT_PRODUCT,
        })
    return lines

def allocate_discount(total_discount, weights):
    # 將滿減總額按各行總金額比例攤分（2 位小數，最大餘數法），攤分總和一定等於 total_discount
    total_w = sum(weights)
    if total_w <= 0 or total_discount <= 0:
        return [0.0] * len(weights)
    import math
    raw = [total_discount * w / total_w for w in weights]
    floors = [math.floor(r * 100) / 100 for r in raw]
    remainder_cents = int(round((total_discount - sum(floors)) * 100))
    order = sorted(range(len(weights)),
                   key=lambda i: (raw[i] * 100 - math.floor(raw[i] * 100), weights[i]),
                   reverse=True)
    alloc = floors[:]
    for k in range(remainder_cents):
        idx = order[k % len(order)]
        alloc[idx] = _round2(alloc[idx] + 0.01)
    return alloc

def compute_sales_amounts(lines):
    # 按單分組：每單嘅滿減行（產品「滿減優惠」）按該單各實付行總金額比例攤分；
    # 回 {(date, store_key, product): {"amount": 實際金額, "qty": 網站件數（含免費行）}}
    orders = {}
    for ln in lines:
        orders.setdefault(ln["order"], []).append(ln)
    out = {}
    for order, olines in orders.items():
        paid = [l for l in olines if not l["discount"]]
        discount_total = -sum(l["subtotal"] for l in olines if l["discount"])
        weights = [max(0.0, l["subtotal"]) for l in paid]
        allocs = allocate_discount(discount_total, weights) if discount_total > 0 else [0.0] * len(paid)
        for l, a in zip(paid, allocs):
            key = (l["date"], l["store"], l["product"])
            e = out.setdefault(key, {"amount": 0.0, "qty": 0})
            e["amount"] += l["subtotal"] - a
            e["qty"] += l["qty"]
    for e in out.values():
        e["amount"] = _round2(e["amount"])
    return out

def attach_amounts(rows, products, sales_amounts, run_date):
    # 每條 C 表記錄加 "amount"（實際金額，優惠後）：
    # 網站行實際金額 +（紙單件數 × 標準價）；冇網站行嗰陣 amount = null（報表跌返標準價）。
    # 紙單件數 = 已銷售 − 網站件數；冇標準價又有多出紙單件數嗰陣，金額計唔準，amount = null。
    price_by_code = {}
    for p in products or []:
        price_by_code[p.get("product_code")] = p.get("standard_price")
    for r in rows:
        key = (run_date, _store_key(r.get("store")), (r.get("product_name") or "").strip())
        e = sales_amounts.get(key)
        if e is None:
            r["amount"] = None
            continue
        sold = r.get("sold_pcs") or 0
        paper_qty = max(0, sold - e["qty"])
        price = price_by_code.get(r.get("product_code"))
        if paper_qty > 0 and price is None:
            r["amount"] = None
            continue
        r["amount"] = _round2(e["amount"] + paper_qty * (price or 0))

def main():
    run_date = datetime.now(HKT).strftime("%Y-%m-%d")
    history = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8"))
            history = {k: v for k, v in (prev.get("history") or {}).items()
                       if k not in BOGUS_KEYS}
        except Exception:
            history = {}
    token = get_token()
    try:
        products = fetch_products(token)
    except Exception as e:
        print(f"T1 products fetch failed (sold sync continues): {e}")
        products = []
    if products:
        pout = {
            "source": "BETTEREAT-Feishu-Base-T1",
            "generated_at": datetime.now(HKT).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            "count": len(products),
            "products": products,
        }
        with open(PRODUCTS_OUT, "w", encoding="utf-8") as fh:
            json.dump(pout, fh, ensure_ascii=False, indent=2)
        print(f"wrote {PRODUCTS_OUT}: count={len(products)}")
    else:
        print("no T1 products found; leaving existing products JSON untouched")
    rows = fetch_current(token, _per_pan_map(products))
    if not rows:
        print("no sold records found; leaving existing JSON untouched")
        return
    # v6：讀「銷售紀錄」表計每條記錄嘅實際金額（優惠後）；讀唔到就全部 amount = null，同步照繼續。
    try:
        sales_amounts = compute_sales_amounts(sales_lines_from_items(list_records(token, SALES_TABLE)))
    except Exception as e:
        print(f"sales-record fetch failed (amounts left null): {e}")
        sales_amounts = {}
    attach_amounts(rows, products, sales_amounts, run_date)
    # 2026-10-25 rule: every run date gets its own snapshot, even when the
    # data is identical to the previous day. Same-day re-runs still refresh
    # that day's snapshot (end-of-day state wins).
    history[run_date] = rows
    out_date = run_date
    out = {
        "source": "BETTEREAT-Feishu-Base",
        "date": out_date,
        "generated_at": datetime.now(HKT).strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "records": history[out_date],
        "history": {d: history[d] for d in sorted(history)},
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print(f"wrote {OUT}: date={out_date} run_date={run_date} "
          f"history_days={len(history)} records={len(rows)}")

if __name__ == "__main__":
    main()
