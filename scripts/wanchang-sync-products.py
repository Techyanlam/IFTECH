#!/usr/bin/env python3
"""Nightly sync: Feishu 產品 table -> mancheong.html product seed.

Feishu is the source of truth for the product master. The script lists every
row of 產品 (tblJfZH58x8L8OMo), then rebuilds the `products` array in
the <script id="seed"> JSON of mancheong.html:

- A Feishu row matching a local product by 貨品編號 (or by 商品名稱 when the
  part number is empty) keeps the local id and unit, so in-session records
  that reference productId keep working.
- A Feishu row with no local match is appended with a fresh id (maxId + 1).
- A local product with no Feishu match is dropped, EXCEPT when a seed
  receipt / transfer / shipment still references its productId (dropping it
  would break the record) — those are kept and warned about.
- Feishu has no 單位 field, so new products get unit "件".

Only the products array is touched; locations / receipts / allocations /
transfers / shipments in the seed are left exactly as they are. The file is
only rewritten when the products array actually changed.

Env: WANCHANG_APP_ID, WANCHANG_APP_SECRET (same repo secrets as
split_wanchang.yml). Usage: python3 scripts/wanchang-sync-products.py
[--dry-run] — dry-run prints the summary without writing the file.
"""

import json
import os
import re
import sys
import urllib.request

APP_TOKEN = "Wftab3L6AaSQG9s0zaIcKMf9n0g"
PRODUCT_TABLE = "tblJfZH58x8L8OMo"  # 產品 (new clean product master; was tblFqzqfaRbXh5qq 商品信息表 until 2026-11-09)
BASE = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{PRODUCT_TABLE}/records"
TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
SEED_RE = re.compile(r'(<script id="seed" type="application/json">)(.*?)(\s*</script>)', re.S)


def get_token(app_id, app_secret):
    req = urllib.request.Request(
        TOKEN_URL,
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"tenant token failed: {data}")
    return data["tenant_access_token"]


def list_all_products(token):
    """Return all 產品 rows as (part, name, brand, alias, unit) tuples."""
    rows = []
    page_token = None
    while True:
        url = BASE + "?page_size=100"
        if page_token:
            url += "&page_token=" + page_token
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") != 0:
            raise RuntimeError(f"list records failed: {data}")
        for rec in data.get("data", {}).get("items", []):
            f = rec.get("fields", {})
            rows.append((
                (f.get("貨品編號") or "").strip() if isinstance(f.get("貨品編號"), str) else "",
                (f.get("商品名稱") or "").strip() if isinstance(f.get("商品名稱"), str) else "",
                (f.get("品牌") or "").strip() if isinstance(f.get("品牌"), str) else "",
                (f.get("產品花名／英文名") or "").strip() if isinstance(f.get("產品花名／英文名"), str) else "",
                (f.get("單位") or "").strip() if isinstance(f.get("單位"), str) else "",
            ))
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data["data"].get("page_token")
        if not page_token:
            break
    return rows


def key_of(part, name):
    """Stable merge key: part number first, else the product name."""
    return ("part", part) if part else ("name", name)


def merge_products(local_products, feishu_rows, referenced_ids):
    """Merge Feishu rows into the local seed products.

    local_products: list of [id, name, part, brand, alias, unit].
    feishu_rows: list of (part, name, brand, alias, unit).
    referenced_ids: set of product ids still referenced by seed records.
    Returns (new_products, stats dict).
    """
    by_key = {}
    for p in local_products:
        k = key_of(p[2] or "", p[1] or "")
        by_key.setdefault(k, p)
    max_id = max((p[0] for p in local_products), default=0)
    seen_keys = set()
    matched_ids = set()
    new_products = []
    stats = {"feishu_rows": len(feishu_rows), "matched": 0, "added": 0,
             "dropped": 0, "kept_referenced": 0, "dup_feishu_keys": 0,
             "dup_local_keys": 0, "skipped": 0}
    for part, name, brand, alias, unit in feishu_rows:
        if not part and not name:
            stats["skipped"] += 1
            continue
        k = key_of(part, name)
        if k in seen_keys:
            # Duplicate part number / name in Feishu — keep the first row.
            stats["dup_feishu_keys"] += 1
            continue
        seen_keys.add(k)
        old = by_key.get(k)
        if old is not None:
            stats["matched"] += 1
            matched_ids.add(old[0])
            # Keep the local id; take name/brand/alias from Feishu when it has
            # one, else keep the local value (the new table was copied by hand
            # and has gaps — an empty Feishu field must not blank the seed),
            # and the unit from Feishu when it has one, else keep the local unit.
            new_products.append([old[0], name or old[1], part or None, brand or old[3] or None,
                                 alias or old[4] or None, unit or old[5] or "件"])
        else:
            stats["added"] += 1
            max_id += 1
            new_products.append([max_id, name, part or None, brand or None,
                                 alias or None, unit or "件"])
    # Local products with no Feishu match: drop, unless a seed record still
    # references them (dropping would break that record). A local product
    # sharing its key with an already-matched product (e.g. two identical
    # PC30 rows) is a duplicate-key twin — same treatment, counted separately.
    kept = []
    for p in local_products:
        if p[0] in matched_ids:
            continue
        k = key_of(p[2] or "", p[1] or "")
        if k in seen_keys:
            stats["dup_local_keys"] += 1
        if p[0] in referenced_ids:
            stats["kept_referenced"] += 1
            kept.append(p)
        else:
            stats["dropped"] += 1
    new_products.extend(kept)
    new_products.sort(key=lambda p: p[0])
    return new_products, stats


def referenced_product_ids(seed):
    ids = set()
    for r in seed.get("receipts", []) or []:
        if r.get("productId") is not None:
            ids.add(r["productId"])
    for t in seed.get("transfers", []) or []:
        if t.get("productId") is not None:
            ids.add(t["productId"])
    for s in seed.get("shipments", []) or []:
        if s.get("productId") is not None:
            ids.add(s["productId"])
    return ids


def main():
    dry_run = "--dry-run" in sys.argv
    app_id = os.environ.get("WANCHANG_APP_ID")
    app_secret = os.environ.get("WANCHANG_APP_SECRET")
    if not app_id or not app_secret:
        print("WANCHANG_APP_ID / WANCHANG_APP_SECRET not set", file=sys.stderr)
        return 2
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "mancheong.html")
    html_path = os.path.normpath(html_path)
    token = get_token(app_id, app_secret)
    feishu_rows = list_all_products(token)
    html = open(html_path, encoding="utf-8").read()
    m = SEED_RE.search(html)
    if not m:
        print("seed script tag not found in mancheong.html", file=sys.stderr)
        return 2
    seed = json.loads(m.group(2))
    old_products = seed.get("products", [])
    new_products, stats = merge_products(
        old_products, feishu_rows, referenced_product_ids(seed))
    print(f"Feishu rows: {stats['feishu_rows']}, local before: {len(old_products)}")
    print(f"matched: {stats['matched']}, added: {stats['added']}, "
          f"dropped: {stats['dropped']}, kept (referenced): {stats['kept_referenced']}, "
          f"dup Feishu keys: {stats['dup_feishu_keys']}, "
          f"dup local keys: {stats['dup_local_keys']}, skipped: {stats['skipped']}")
    print(f"local after: {len(new_products)}")
    if new_products == old_products:
        print("No change — mancheong.html untouched.")
        return 0
    if dry_run:
        print("Dry run — not writing.")
        return 0
    seed["products"] = new_products
    new_seed = json.dumps(seed, ensure_ascii=False, separators=(",", ":"))
    new_html = html[: m.start(2)] + new_seed + html[m.end(2):]
    open(html_path, "w", encoding="utf-8").write(new_html)
    print("mancheong.html updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
