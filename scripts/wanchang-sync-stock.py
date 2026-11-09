#!/usr/bin/env python3
"""Nightly rebuild: all movement records -> Feishu 庫存 table.

Stock is DERIVED from movements, never edited by hand in the 庫存 table
(the rebuild overwrites it). One row per (product, warehouse):

- 入庫數量: Feishu 收貨 rows with 入倉 set + webpage manual receipts
  (網頁收貨記錄 rows with an empty 來源飛書單號; GR-card receipts are
  counted via their GR row only, never double).
- 出庫數量: Feishu 出貨 rows with 出倉 set + 網頁出貨記錄 rows.
- 轉入數量 / 轉出數量: 網頁轉位記錄 rows (to / from).
- 調整數量: 調整庫存 rows (positive or negative).
- 現有庫存 is a Formula in Feishu, so the script never writes it.

Each run recomputes from scratch and SETS the five quantity fields, so
re-runs never double-count. Rows with no movements left are zeroed, not
deleted. Unreadable quantities, unknown parts and unknown locations are
skipped and reported. Negative totals are written and reported, never hidden.

The 庫存 table's 貨品編號 links to the NEW 產品 table (tblJfZH58x8L8OMo)
and its 倉庫 links to 倉庫主檔; the old 分倉庫存 table is untouched.

Env: WANCHANG_APP_ID, WANCHANG_APP_SECRET (same repo secrets as
split_wanchang.yml). Usage: python3 scripts/wanchang-sync-stock.py
[--dry-run] — dry-run prints the summary without writing anything.
"""

import json
import os
import sys
import urllib.request

APP_TOKEN = "Wftab3L6AaSQG9s0zaIcKMf9n0g"
STOCK_TABLE = "tblUFOFGtsc4GJ9v"        # 庫存 (new; created 2026-11-09)
PRODUCT_TABLE = "tblJfZH58x8L8OMo"      # 產品 (new clean product master)
WAREHOUSE_TABLE = "tblHiQYnU8QjmizF"    # 倉庫主檔
GR_TABLE = "tblyUYU8s2Novavk"           # 收貨
OUTBOUND_TABLE = "tblrYciGocE5Z545"     # 出貨
SYNC_RECEIPT = "tbl6mt1l4y5zkQG7"       # 網頁收貨記錄
SYNC_TRANSFER = "tbl4SHGRBiYBPbMr"      # 網頁轉位記錄
SYNC_SHIPMENT = "tblPnUMIh4pguG2H"      # 網頁出貨記錄
ADJUST_TABLE = "tblWSPMVOKxczwjC"       # 調整庫存

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
API_BASE = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables"

QTY_FIELDS = ["入庫數量", "出庫數量", "轉入數量", "轉出數量", "調整數量"]


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


def list_records(token, table_id):
    """Return all rows of a table as [(record_id, fields)] (max 5 pages)."""
    rows = []
    page_token = None
    for _ in range(5):
        url = f"{API_BASE}/{table_id}/records?page_size=100"
        if page_token:
            url += "&page_token=" + page_token
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") != 0:
            raise RuntimeError(f"list records failed for {table_id}: {data}")
        for rec in data.get("data", {}).get("items", []):
            rows.append((rec.get("record_id") or "", rec.get("fields", {}) or {}))
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data["data"].get("page_token")
        if not page_token:
            break
    return rows


def create_record(token, table_id, fields):
    url = f"{API_BASE}/{table_id}/records"
    req = urllib.request.Request(
        url,
        data=json.dumps({"fields": fields}).encode("utf-8"),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"create record failed in {table_id}: {data}")
    return data["data"]["record"]["record_id"]


def update_record(token, table_id, record_id, fields):
    url = f"{API_BASE}/{table_id}/records/{record_id}"
    req = urllib.request.Request(
        url,
        data=json.dumps({"fields": fields}).encode("utf-8"),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json; charset=utf-8"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"update record failed in {table_id}: {data}")


def link_ids(value):
    """First record_id out of a Feishu link field value, or None.

    A populated link is a list of objects with a "record_ids" array; an
    empty link is a single object with record_ids null.
    """
    if isinstance(value, list):
        for item in value:
            ids = (item or {}).get("record_ids") or []
            if ids:
                return ids[0]
        return None
    if isinstance(value, dict):
        ids = value.get("record_ids") or []
        return ids[0] if ids else None
    return None


def parse_qty(value):
    """A quantity as a float, or None when it is missing / unreadable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (ValueError, AttributeError):
            return None
    return None


def num(value):
    """Write whole quantities as ints so the Number fields stay clean."""
    return int(value) if float(value).is_integer() else float(value)


def text(value):
    return (value or "").strip() if isinstance(value, str) else ""


def accumulate(stock, log, prod_id, wh_id, part, wh_name, field, qty):
    """Add qty to one of the five quantity buckets of a (product, warehouse)."""
    if prod_id is None or wh_id is None or qty is None:
        return
    key = (prod_id, wh_id)
    row = stock.setdefault(key, {"in": 0.0, "out": 0.0, "tin": 0.0,
                                 "tout": 0.0, "adj": 0.0,
                                 "part": part, "wh_name": wh_name})
    row[field] += qty


def build_stock(gr_rows, outbound_rows, sync_receipt_rows, sync_transfer_rows,
                sync_shipment_rows, adjust_rows,
                product_by_part, product_part_by_id,
                warehouse_by_name, warehouse_name_by_id):
    """Recompute stock from every movement source.

    Returns (stock dict, problems dict). stock is keyed by
    (product_record_id, warehouse_record_id) and holds the five quantities
    plus the part and warehouse name for the 庫存項目 text.
    """
    stock = {}
    problems = {"unreadable_qty": [], "unknown_part": [],
                "unknown_location": [], "missing_warehouse": []}

    for rec_id, f in gr_rows:
        part = text(f.get("貨品編號"))
        wh_id = link_ids(f.get("入倉"))
        gr_no = text(f.get("收貨單 編號")) or rec_id
        if not part or not wh_id:
            problems["missing_warehouse"].append(f"{gr_no} part={part or '?'}")
            continue
        prod_id = product_by_part.get(part)
        if prod_id is None:
            problems["unknown_part"].append(f"{gr_no} part={part}")
            continue
        qty = f.get("數量 (Number)")
        if qty is None:
            qty = parse_qty(f.get("數量"))
        if qty is None:
            problems["unreadable_qty"].append(f"{gr_no} part={part}")
            continue
        accumulate(stock, problems, prod_id, wh_id, part,
                   warehouse_name_by_id.get(wh_id, ""), "in", qty)

    for rec_id, f in outbound_rows:
        part = text(f.get("貨品編號"))
        wh_id = link_ids(f.get("出倉"))
        dn_no = text(f.get("出貨單 編號")) or rec_id
        if not part or not wh_id:
            problems["missing_warehouse"].append(f"{dn_no} part={part or '?'}")
            continue
        prod_id = product_by_part.get(part)
        if prod_id is None:
            problems["unknown_part"].append(f"{dn_no} part={part}")
            continue
        qty = parse_qty(f.get("數量"))
        if qty is None:
            problems["unreadable_qty"].append(f"{dn_no} part={part}")
            continue
        accumulate(stock, problems, prod_id, wh_id, part,
                   warehouse_name_by_id.get(wh_id, ""), "out", qty)

    for rec_id, f in sync_receipt_rows:
        if text(f.get("來源飛書單號")):
            continue  # GR-card receipt: already counted via its GR row.
        part = text(f.get("貨品編號"))
        loc = text(f.get("轉入位置"))
        rec_no = text(f.get("記錄編號")) or rec_id
        wh_id = warehouse_by_name.get(loc)
        if not part or not wh_id:
            problems["unknown_location"].append(f"{rec_no} part={part or '?'} loc={loc or '?'}")
            continue
        prod_id = product_by_part.get(part)
        if prod_id is None:
            problems["unknown_part"].append(f"{rec_no} part={part}")
            continue
        qty = parse_qty(f.get("數量"))
        if qty is None:
            problems["unreadable_qty"].append(f"{rec_no} part={part}")
            continue
        accumulate(stock, problems, prod_id, wh_id, part, loc, "in", qty)

    for rec_id, f in sync_transfer_rows:
        part = text(f.get("貨品編號"))
        from_loc = text(f.get("轉出位置"))
        to_loc = text(f.get("轉入位置"))
        rec_no = text(f.get("記錄編號")) or rec_id
        prod_id = product_by_part.get(part)
        if prod_id is None:
            problems["unknown_part"].append(f"{rec_no} part={part or '?'}")
            continue
        qty = parse_qty(f.get("數量"))
        if qty is None:
            problems["unreadable_qty"].append(f"{rec_no} part={part}")
            continue
        wh_from = warehouse_by_name.get(from_loc)
        wh_to = warehouse_by_name.get(to_loc)
        if wh_from is None or wh_to is None:
            problems["unknown_location"].append(
                f"{rec_no} part={part} from={from_loc or '?'} to={to_loc or '?'}")
            continue
        accumulate(stock, problems, prod_id, wh_from, part, from_loc, "tout", qty)
        accumulate(stock, problems, prod_id, wh_to, part, to_loc, "tin", qty)

    for rec_id, f in sync_shipment_rows:
        part = text(f.get("貨品編號"))
        loc = text(f.get("轉出位置"))
        rec_no = text(f.get("記錄編號")) or rec_id
        wh_id = warehouse_by_name.get(loc)
        if not part or not wh_id:
            problems["unknown_location"].append(f"{rec_no} part={part or '?'} loc={loc or '?'}")
            continue
        prod_id = product_by_part.get(part)
        if prod_id is None:
            problems["unknown_part"].append(f"{rec_no} part={part}")
            continue
        qty = parse_qty(f.get("數量"))
        if qty is None:
            problems["unreadable_qty"].append(f"{rec_no} part={part}")
            continue
        accumulate(stock, problems, prod_id, wh_id, part, loc, "out", qty)

    for rec_id, f in adjust_rows:
        prod_id = link_ids(f.get("貨品編號"))
        wh_id = link_ids(f.get("倉庫"))
        adj_no = text(f.get("調整編號")) or rec_id
        if prod_id is None or wh_id is None:
            problems["missing_warehouse"].append(f"{adj_no}")
            continue
        part = product_part_by_id.get(prod_id)
        if part is None:
            problems["unknown_part"].append(f"{adj_no} product={prod_id}")
            continue
        qty = parse_qty(f.get("調整數量"))
        if qty is None:
            problems["unreadable_qty"].append(f"{adj_no} part={part}")
            continue
        accumulate(stock, problems, prod_id, wh_id, part,
                   warehouse_name_by_id.get(wh_id, ""), "adj", qty)

    return stock, problems


def sync_stock(token, stock, dry_run=False):
    """Write the computed stock into the 庫存 table.

    Creates missing rows, updates changed rows, zeroes rows whose movements
    are gone (never deletes them). Returns a stats dict.
    """
    existing = {}
    for rec_id, f in list_records(token, STOCK_TABLE):
        prod_id = link_ids(f.get("貨品編號"))
        wh_id = link_ids(f.get("倉庫"))
        if prod_id is None or wh_id is None:
            continue
        existing[(prod_id, wh_id)] = {
            "record_id": rec_id,
            "values": [parse_qty(f.get(name)) or 0.0 for name in QTY_FIELDS],
        }
    stats = {"created": 0, "updated": 0, "unchanged": 0, "zeroed": 0,
             "negative": []}
    for (prod_id, wh_id), row in sorted(stock.items(), key=lambda kv: kv[1]["part"]):
        values = [row["in"], row["out"], row["tin"], row["tout"], row["adj"]]
        total = row["in"] + row["tin"] - row["out"] - row["tout"] + row["adj"]
        if total < 0:
            stats["negative"].append(
                f"{row['part']} / {row['wh_name']} = {num(total)}")
        item = f"{row['part']} / {row['wh_name']}"
        fields = {"庫存項目": item, "貨品編號": [prod_id], "倉庫": [wh_id]}
        for name, value in zip(QTY_FIELDS, values):
            fields[name] = num(value)
        old = existing.get((prod_id, wh_id))
        if old is None:
            stats["created"] += 1
            if not dry_run:
                create_record(token, STOCK_TABLE, fields)
        elif old["values"] != values:
            stats["updated"] += 1
            if not dry_run:
                update_record(token, STOCK_TABLE, old["record_id"], fields)
        else:
            stats["unchanged"] += 1
    for key, old in existing.items():
        if key not in stock:
            stats["zeroed"] += 1
            if not dry_run:
                update_record(token, STOCK_TABLE, old["record_id"],
                              {name: 0 for name in QTY_FIELDS})
    return stats


def main():
    dry_run = "--dry-run" in sys.argv
    app_id = os.environ.get("WANCHANG_APP_ID")
    app_secret = os.environ.get("WANCHANG_APP_SECRET")
    if not app_id or not app_secret:
        print("WANCHANG_APP_ID / WANCHANG_APP_SECRET not set", file=sys.stderr)
        return 2
    token = get_token(app_id, app_secret)

    product_rows = list_records(token, PRODUCT_TABLE)
    product_by_part = {}
    product_part_by_id = {}
    for rec_id, f in product_rows:
        part = text(f.get("貨品編號"))
        if part and part not in product_by_part:
            product_by_part[part] = rec_id
        if rec_id:
            product_part_by_id[rec_id] = part
    warehouse_rows = list_records(token, WAREHOUSE_TABLE)
    warehouse_by_name = {}
    warehouse_name_by_id = {}
    for rec_id, f in warehouse_rows:
        name = text(f.get("倉庫名稱"))
        if name:
            warehouse_by_name[name] = rec_id
            warehouse_name_by_id[rec_id] = name

    gr_rows = list_records(token, GR_TABLE)
    outbound_rows = list_records(token, OUTBOUND_TABLE)
    sync_receipt_rows = list_records(token, SYNC_RECEIPT)
    sync_transfer_rows = list_records(token, SYNC_TRANSFER)
    sync_shipment_rows = list_records(token, SYNC_SHIPMENT)
    adjust_rows = list_records(token, ADJUST_TABLE)

    stock, problems = build_stock(
        gr_rows, outbound_rows, sync_receipt_rows, sync_transfer_rows,
        sync_shipment_rows, adjust_rows,
        product_by_part, product_part_by_id,
        warehouse_by_name, warehouse_name_by_id)

    print(f"GR rows: {len(gr_rows)}, 出貨 rows: {len(outbound_rows)}, "
          f"sync receipt: {len(sync_receipt_rows)}, "
          f"sync transfer: {len(sync_transfer_rows)}, "
          f"sync shipment: {len(sync_shipment_rows)}, "
          f"adjust rows: {len(adjust_rows)}")
    for name, items in problems.items():
        print(f"{name}: {len(items)}" + (f" — {', '.join(items[:5])}" if items else ""))
    print(f"computed stock rows: {len(stock)}")

    stats = sync_stock(token, stock, dry_run=dry_run)
    print(f"created: {stats['created']}, updated: {stats['updated']}, "
          f"unchanged: {stats['unchanged']}, zeroed: {stats['zeroed']}")
    print(f"negative rows: {len(stats['negative'])}" +
          (f" — {'; '.join(stats['negative'][:5])}" if stats['negative'] else ""))
    if dry_run:
        print("Dry run — nothing written.")
    else:
        print("庫存 table updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
