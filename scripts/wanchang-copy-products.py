#!/usr/bin/env python3
"""One-off copy: old 商品信息表 (tblFqzqfaRbXh5qq) -> new 產品 table (tblJfZH58x8L8OMo).

Yan (2026-11-09) decided the new 產品 table is the product master and the old
table must no longer be synced. This script copies the old table's distinct
products into the new table once, then the nightly sync takes over from the
new table.

- Source rows are deduplicated by key (貨品編號, else 商品名稱), first wins;
  rows with neither part nor name are skipped.
- 單位 is "件" for every copied row (the old table has no unit field; the
  webpage seed is all 件）.
- The script REFUSES to run if the target table already has rows (to avoid
  duplicates); pass --force to override.
- Creates in batches of 500 via records/batch_create.

Env: WANCHANG_APP_ID, WANCHANG_APP_SECRET (same repo secrets as
split_wanchang.yml). Usage: python3 scripts/wanchang-copy-products.py [--force]
"""

import json
import os
import sys
import urllib.request

APP_TOKEN = "Wftab3L6AaSQG9s0zaIcKMf9n0g"
OLD_TABLE = "tblFqzqfaRbXh5qq"
NEW_TABLE = "tblJfZH58x8L8OMo"
TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"


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


def api(token, method, url, body=None):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json; charset=utf-8"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"{method} {url} failed: {json.dumps(data)[:400]}")
    return data.get("data", {})


def list_all(token, table_id):
    rows = []
    page_token = None
    total = None
    while True:
        url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}"
               f"/tables/{table_id}/records?page_size=100")
        if page_token:
            url += "&page_token=" + page_token
        data = api(token, "GET", url)
        if total is None:
            total = data.get("total")
        for rec in data.get("items", []):
            rows.append(rec.get("fields", {}))
        if not data.get("has_more"):
            break
        page_token = data.get("page_token")
        if not page_token:
            break
    return rows, total


def key_of(part, name):
    return ("part", part) if part else ("name", name)


def main():
    force = "--force" in sys.argv
    app_id = os.environ.get("WANCHANG_APP_ID")
    app_secret = os.environ.get("WANCHANG_APP_SECRET")
    if not app_id or not app_secret:
        print("WANCHANG_APP_ID / WANCHANG_APP_SECRET not set", file=sys.stderr)
        return 2
    token = get_token(app_id, app_secret)
    target_rows, target_total = list_all(token, NEW_TABLE)
    print(f"Target 產品 table rows: {target_total} (listed {len(target_rows)})")
    if target_total and target_total > 0 and not force:
        print("Target table is not empty — refusing to copy (would duplicate). "
              "Pass --force to override.", file=sys.stderr)
        return 2
    source_rows, source_total = list_all(token, OLD_TABLE)
    print(f"Source 商品信息表 rows: {source_total} (listed {len(source_rows)})")
    seen = set()
    records = []
    skipped = 0
    for f in source_rows:
        part = (f.get("貨品編號") or "").strip() if isinstance(f.get("貨品編號"), str) else ""
        name = (f.get("商品名稱") or "").strip() if isinstance(f.get("商品名稱"), str) else ""
        brand = (f.get("品牌") or "").strip() if isinstance(f.get("品牌"), str) else ""
        alias = (f.get("產品名稱(英文)") or "").strip() if isinstance(f.get("產品名稱(英文)"), str) else ""
        if not part and not name:
            skipped += 1
            continue
        k = key_of(part, name)
        if k in seen:
            skipped += 1
            continue
        seen.add(k)
        fields = {"單位": "件"}
        if part:
            fields["貨品編號"] = part
        if name:
            fields["商品名稱"] = name
        if brand:
            fields["品牌"] = brand
        if alias:
            fields["產品名稱(英文)"] = alias
        records.append({"fields": fields})
    print(f"Distinct products to copy: {len(records)}, skipped (dup/empty): {skipped}")
    base = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}"
            f"/tables/{NEW_TABLE}/records/batch_create")
    created = 0
    for i in range(0, len(records), 500):
        chunk = records[i:i + 500]
        data = api(token, "POST", base, {"records": chunk})
        created += len(data.get("records", []))
        print(f"  batch {i // 500 + 1}: created {len(data.get('records', []))} (total {created})")
    print(f"Done. Created {created} rows in 產品 table.")
    return 0 if created == len(records) else 1


if __name__ == "__main__":
    sys.exit(main())
