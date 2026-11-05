#!/usr/bin/env python3
# BETTEREAT sold-list sync v4 — SNAPSHOT archive (V1 Base migration 2026-10-28).
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
import json, os, urllib.request
from datetime import datetime, timezone, timedelta

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU = "https://open.feishu.cn"
APP_TOKEN = "YvpCbgjTia7wqWsMjNhcPwL7nih"      # BETTEREAT - Dessert V1 base (BETTEREAT HONG KONG LIMITED)
C_TABLE = "tblJuBUdRyqbL8Pq"                    # V1 C_每日分配及出貨記錄 (main table, 232 rows = 58 products x 4 stores)
T1_TABLE = "tblueJrNjwgQk8nN"                   # V1 T1_產品主檔 (59 products, verified 2026-11-05)
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

def fetch_current(token):
    rows = []
    for it in list_records(token, C_TABLE):
        f = it.get("fields") or {}
        sold = num(f.get("已銷售"))
        if sold is None or sold <= 0:
            continue
        if sold == int(sold):
            sold = int(sold)
        rows.append({
            "store": text_of(f.get("門店代號")),
            "product_code": text_of(f.get("產品編號")),
            "product_name": text_of(f.get("產品名稱")),
            "unit": text_of(f.get("計量單位")),
            "allocated_pans": num(f.get("分配量")),
            "no_of_pcs": num(f.get("No of pcs")),
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
        rows.append({
            "product_code": code,
            "product_name": text_of(f.get("產品名稱")),
            "category": text_of(f.get("大類")),
            "subcategory": text_of(f.get("類別")),
            "cabinet": text_of(f.get("存放櫃")),
            "unit": text_of(f.get("計量單位")),
            "no_of_pcs": pcs,
            "standard_price": price,
            "sku": text_of(f.get("E365商品編號SKU")),
            "status": text_of(f.get("狀態")),
            "remarks": text_of(f.get("備註")),
        })
    rows.sort(key=lambda r: r["product_code"])
    return rows

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
    rows = fetch_current(token)
    if not rows:
        print("no sold records found; leaving existing JSON untouched")
        return
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
