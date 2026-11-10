#!/usr/bin/env python3
"""Rebuild: all movement records -> Feishu 庫存總表 (pivoted master table).

Stock is DERIVED from movements, never edited by hand in the 庫存總表
(the rebuild overwrites it). One row per PRODUCT, copied from the 產品
table, with one Number column per warehouse (倉1–倉7, 收貨區, 出貨區)
plus 收貨總數, 出貨總數 and 總庫存. Only products with at least one
movement get a row; a product with no movements has no row (zero everywhere).

Movement sources (same as the old per-(product, warehouse) rebuild):
- 入庫: Feishu 收貨 rows with 入倉 set + webpage manual receipts
  (網頁收貨記錄 rows with an empty 來源飛書單號). For a GR-card receipt
  (a 網頁收貨記錄 row whose 來源飛書單號 matches the GR number), the
  webpage-confirmed quantity and warehouse WIN over the GR row's AI-filled
  values — Yan corrects quantities at receipt time, and the corrected
  number is what must land in stock (2026-11-09: GR rows said 1, Yan
  received 20, and the old GR-row-only count showed 2 instead of 40).
  A GR row with no matching sync record still counts via the GR row itself.
  Each GR is counted once, never double.
- 出庫: Feishu 出貨 rows with 出倉 set + 網頁出貨記錄 rows.
- 轉入 / 轉出: 網頁轉位記錄 rows (to / from).
- 調整: 調整庫存 rows (positive or negative).

Each run recomputes from scratch and SETS the twelve Number columns, so
re-runs never double-count. Rows with no movements left are zeroed, not
deleted. Unreadable quantities, unknown parts and unknown locations are
skipped and reported. Negative totals are written and reported, never hidden.

The old per-(product, warehouse) 庫存 table (tblUFOFGtsc4GJ9v) is NOT
written by this script; it is superseded by the 庫存總表.

Env: WANCHANG_APP_ID, WANCHANG_APP_SECRET (same repo secrets as
split_wanchang.yml). Usage: python3 scripts/wanchang-sync-stock-summary.py
[--dry-run] — dry-run prints the summary without writing anything.
"""

import json
import os
import sys
import time
import urllib.request

APP_TOKEN = "Wftab3L6AaSQG9s0zaIcKMf9n0g"
SUMMARY_TABLE = "tblsZfbJ1mj19zk5"      # 庫存總表 (pivoted; created 2026-11-09)
WAREHOUSE_COLUMNS = ["倉1", "倉2", "倉3", "倉4", "倉5", "倉6", "倉7",
                     "收貨區", "出貨區", "暫存待確認"]
SUMMARY_NUMBER_COLUMNS = WAREHOUSE_COLUMNS + ["收貨總數", "出貨總數", "總庫存"]
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
    """Return all rows of a table as [(record_id, fields)].

    Pages through all results (up to 200 pages x 100 rows — the 產品 table
    alone has 5,121 rows, so a 5-page cap silently dropped most products
    and made them "unknown part" in run #3, 2026-11-09).

    Retries a page up to 3 times when the API returns a null items with a
    non-zero total. A table with genuinely 0 rows returns items null +
    total 0 (verified in the Feishu UI for the 出貨 table, 2026-11-09:
    0 records, no filter) — that is treated as empty, not an error.
    If it still fails, raise with the raw response so the log shows the
    real cause instead of a bare TypeError.
    """
    rows = []
    page_token = None
    for _ in range(200):  # 200 pages x 100 = 20,000 rows; 產品表有 5,121 行
        url = f"{API_BASE}/{table_id}/records?page_size=100"
        if page_token:
            url += "&page_token=" + page_token
        data = None
        for attempt in range(3):
            req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("code") != 0:
                break
            d = data.get("data") or {}
            if d.get("items") is not None or d.get("total") == 0:
                break
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
        if data is None or data.get("code") != 0:
            raise RuntimeError(f"list records failed for {table_id}: {data}")
        items = (data.get("data") or {}).get("items")
        if items is None:
            # Genuinely empty table (items null + total 0, verified in the
            # Feishu UI) — treat as no rows. If total > 0 the rows exist
            # but were not returned, so still fail loudly.
            if (data.get("data") or {}).get("total") == 0:
                return rows
            raise RuntimeError(f"list records returned no items for {table_id}: {data}")
        for rec in items:
            rows.append((rec.get("record_id") or "", rec.get("fields", {}) or {}))
        if not (data.get("data") or {}).get("has_more"):
            break
        page_token = (data.get("data") or {}).get("page_token")
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


def delete_record(token, table_id, record_id):
    url = f"{API_BASE}/{table_id}/records/{record_id}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + token},
        method="DELETE",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("code") != 0:
        raise RuntimeError(f"delete record failed in {table_id}: {data}")


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

    # Webpage-confirmed receipts, indexed by (GR number, part). A GR-card
    # receipt's sync record carries the quantity and warehouse Yan actually
    # confirmed at receipt time; it overrides the GR row's AI-filled values.
    # (A GR received in several batches has several sync records — they sum.)
    sync_by_gr = {}
    for rec_id, f in sync_receipt_rows:
        src = text(f.get("來源飛書單號"))
        if not src:
            continue
        part = text(f.get("貨品編號"))
        if not part:
            continue
        sync_by_gr.setdefault((src, part), []).append(
            (parse_qty(f.get("數量")), text(f.get("轉入位置")),
             text(f.get("記錄編號")) or rec_id))

    for rec_id, f in gr_rows:
        part = text(f.get("貨品編號"))
        gr_no = text(f.get("收貨單 編號")) or rec_id
        synced = sync_by_gr.get((gr_no, part)) if part else None
        if synced:
            # Webpage-confirmed values win; the GR row itself is not counted,
            # so this receipt is counted once, never double.
            for qty, loc, rec_no in synced:
                wh_id = warehouse_by_name.get(loc)
                if qty is None:
                    problems["unreadable_qty"].append(f"{rec_no} part={part}")
                    continue
                if wh_id is None:
                    problems["unknown_location"].append(
                        f"{rec_no} part={part} loc={loc or '?'}")
                    continue
                prod_id = product_by_part.get(part)
                if prod_id is None:
                    problems["unknown_part"].append(f"{rec_no} part={part}")
                    continue
                accumulate(stock, problems, prod_id, wh_id, part, loc,
                           "in", qty)
            continue
        wh_id = link_ids(f.get("入倉"))
        if not part or not wh_id:
            problems["missing_warehouse"].append(f"{gr_no} part={part or '?'}")
            continue
        prod_id = product_by_part.get(part)
        if prod_id is None:
            problems["unknown_part"].append(f"{gr_no} part={part}")
            continue
        qty = parse_qty(f.get("數量 (Number)"))
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


def pivot_stock(stock):
    """Pivot per-(product, warehouse) stock into per-product summary rows.

    Returns {prod_id: {"part":, "wh": {warehouse_name: stock},
    "in_total":, "out_total":, "total":}}. Only products with at least one
    movement appear; a product with no movements has no row (zero everywhere).
    """
    summary = {}
    for (prod_id, wh_id), row in stock.items():
        entry = summary.setdefault(prod_id, {"part": row["part"], "wh": {},
                                             "in_total": 0.0, "out_total": 0.0})
        wh_stock = row["in"] + row["tin"] - row["out"] - row["tout"] + row["adj"]
        entry["wh"][row["wh_name"]] = entry["wh"].get(row["wh_name"], 0.0) + wh_stock
        entry["in_total"] += row["in"]
        entry["out_total"] += row["out"]
    for entry in summary.values():
        entry["total"] = sum(entry["wh"].values())
    return summary


def sync_summary(token, summary, product_name_by_id, dry_run=False):
    """Write the pivoted stock into the 庫存總表 (one row per product).

    Columns: 貨品編號 (primary), 商品名稱, 倉1–倉7, 收貨區, 出貨區,
    收貨總數, 出貨總數, 總庫存. Creates missing rows, updates changed rows,
    zeroes rows whose movements are gone (never deletes them).
    One row per PART NUMBER: several product records sharing a part are
    merged into a single row (their quantities summed). Duplicate rows for
    the same part (e.g. created by two near-simultaneous fast-path writes)
    are deleted, keeping one.
    Returns a stats dict.
    """
    # Merge summary entries by part number (several product records may
    # share one part number; their quantities add up).
    by_part = {}
    for prod_id, entry in summary.items():
        part = entry["part"]
        merged = by_part.get(part)
        if merged is None:
            merged = by_part[part] = {
                "part": part,
                "name": product_name_by_id.get(prod_id, ""),
                "wh": dict(entry["wh"]),
                "in_total": entry["in_total"],
                "out_total": entry["out_total"],
                "total": entry["total"],
            }
        else:
            for wh, v in entry["wh"].items():
                merged["wh"][wh] = merged["wh"].get(wh, 0.0) + v
            merged["in_total"] += entry["in_total"]
            merged["out_total"] += entry["out_total"]
            merged["total"] += entry["total"]
            if not merged["name"]:
                merged["name"] = product_name_by_id.get(prod_id, "")
    # Existing rows, keyed by part. Duplicates (same part, several rows)
    # are collected so the extras can be deleted, keeping one.
    existing = {}
    duplicates = []
    for rec_id, f in list_records(token, SUMMARY_TABLE):
        part = text(f.get("貨品編號"))
        if not part:
            continue
        row = {
            "record_id": rec_id,
            "values": [parse_qty(f.get(name)) or 0.0
                       for name in SUMMARY_NUMBER_COLUMNS],
        }
        if part in existing:
            duplicates.append(row)
        else:
            existing[part] = row
    stats = {"created": 0, "updated": 0, "unchanged": 0, "zeroed": 0,
             "deduped": 0, "negative": []}
    for part, entry in sorted(by_part.items()):
        values = [entry["wh"].get(name, 0.0) for name in WAREHOUSE_COLUMNS]
        values += [entry["in_total"], entry["out_total"], entry["total"]]
        if entry["total"] < 0:
            stats["negative"].append(f"{part} = {num(entry['total'])}")
        fields = {"貨品編號": part, "商品名稱": entry["name"]}
        for name, value in zip(SUMMARY_NUMBER_COLUMNS, values):
            fields[name] = num(value)
        old = existing.get(part)
        if old is None:
            stats["created"] += 1
            if not dry_run:
                new_id = create_record(token, SUMMARY_TABLE, fields)
                existing[part] = {"record_id": new_id, "values": values}
        elif old["values"] != values:
            stats["updated"] += 1
            if not dry_run:
                update_record(token, SUMMARY_TABLE, old["record_id"], fields)
                old["values"] = values
        else:
            stats["unchanged"] += 1
    for part, old in existing.items():
        if part not in by_part:
            stats["zeroed"] += 1
            if not dry_run:
                update_record(token, SUMMARY_TABLE, old["record_id"],
                              {name: 0 for name in SUMMARY_NUMBER_COLUMNS})
    for dup in duplicates:
        # A duplicate row for a part that already has a kept row: delete it.
        # The kept row was just updated to the merged totals above, so
        # nothing is lost.
        stats["deduped"] += 1
        if not dry_run:
            delete_record(token, SUMMARY_TABLE, dup["record_id"])
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
    product_name_by_id = {}
    for rec_id, f in product_rows:
        part = text(f.get("貨品編號"))
        if part and part not in product_by_part:
            product_by_part[part] = rec_id
        if rec_id:
            product_part_by_id[rec_id] = part
            product_name_by_id[rec_id] = text(f.get("商品名稱"))
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
    summary = pivot_stock(stock)
    print(f"summary products: {len(summary)}")

    stats = sync_summary(token, summary, product_name_by_id, dry_run=dry_run)
    print(f"created: {stats['created']}, updated: {stats['updated']}, "
          f"unchanged: {stats['unchanged']}, zeroed: {stats['zeroed']}, "
          f"deduped: {stats['deduped']}")
    print(f"negative rows: {len(stats['negative'])}" +
          (f" — {'; '.join(stats['negative'][:5])}" if stats['negative'] else ""))
    if dry_run:
        print("Dry run — nothing written.")
    else:
        print("庫存總表 updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
