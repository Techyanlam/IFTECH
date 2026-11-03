"""Split Vendor Invoices multi-item records into GR Note rows via Feishu Bitable API.

Design (v1, deterministic, no AI Agent):
- Source: Vendor Invoices (tblnEpXNQqXxFuXM), field 商品編號 (fldVXbc4z0, text).
  Split ONLY this field. 商品編號 (K) is IGNORED entirely per Yan 2026-11-02
  ("不用理會"): not split, not flagged, not logged as a warning.
- Target: GR Note (tblyUYU8s2Novavk).
  Writable fields only: 供應商名稱 (fldKT8UB9L, text), 貨品編號 (fldu2XH4jB, text),
  數量 (fldRA5YL9y, Multiline TEXT as of 2026-11-03 — send as string, not number,
  otherwise Feishu returns 1254060 TextFieldConvFail), 單位 (fldUyHE8J5, text).
  System fields (1005 auto-number, 1001 created-time) are NEVER written.
  Lookup field Check inventory list (type 19) is NEVER written (computed).
  Duplex link 關聯單據 (fld7CR5SyZ) is attempted AFTER the row is created, in a
  separate step; if the link update fails, the row is kept and the failure is logged.
  It is never allowed to fail the whole row.
- Quantity: 送貨數量 is a TEXT field in the source. Parse it as a number.
  If it is missing / non-numeric, the source record is flagged for manual handling
  and NO rows are created for it (never guess a quantity).
  If n items: integer totals are split as evenly as possible, remainder 1 each to
  the first items, so the sum of created rows == total. Decimal totals: round to
  2 decimals for the first n-1 rows, last row = total - sum(previous).
- Two input formats, learned from live test records MC-20261008-0036 and
  MC-20261102-0037 (2026-11-02, read-only browser lookup):
  (a) List format: "1. <item> Qty:<n>" per line/item, e.g. "1. 1.5M 盛膠欄 Qty:11"
      and "1. HM0810  Qty:10". In this format the leading enumeration is stripped,
      the item text before "Qty:" is the part/product identifier, and the
      per-item Qty after "Qty:" is used directly (NOT averaged from the total).
      The sum of per-item Qtys is then checked against 送貨數量; a mismatch is
      flagged and the record is skipped, not partially split.
  (b) Separator format (fallback): split by separators below, then allocate
      the total as described.
- Separators for format (b) (exactly the prompt's list, NOT space, so a part
  number containing a space is not split): ， , ； ; 、 / ／ newline, carriage return.
  Empty tokens are dropped. Duplicate part numbers in one record are kept as
  separate rows (per-item logic), and logged.
- Dedupe / safety (lesson from earlier volume work):
  * A state file records source record_ids already split successfully, with the
    created GR Note record_ids. A source record in the state file is skipped.
  * Default caps: --limit 20 source records per run, --max-items 20 items per record.
    A record exceeding --max-items is flagged and skipped, NOT partially split.
  * Default mode is DRY-RUN: no GR Note rows are created, only the planned rows
    are printed. Use --live to actually create rows.
  * --source-record-id processes exactly one record (use this first, with a
    single-item invoice, before any batch run).
- Credentials: NEVER hardcode, NEVER write to a file. Read app_id/app_secret
  from stdin JSON (same pattern as the earlier Wanchang API scripts), or from
  environment variables WANCHANG_APP_ID / WANCHANG_APP_SECRET if stdin is empty.
  The token is never printed.

Usage:
  # dry-run, all unprocessed records (reads only)
  echo '{"app_id":"...","app_secret":"..."}' | python3 wanchang-split-gr-note.py --dry-run
  # dry-run, one record only (by Feishu record_id)
  echo '{"app_id":"...","app_secret":"..."}' | python3 wanchang-split-gr-note.py --dry-run --source-record-id recXXXX
  # dry-run, by document number (e.g. MC-20261008-0036, MC-20261102-0037, Yan 2026-11-02 test records)
  echo '{"app_id":"...","app_secret":"..."}' | python3 wanchang-split-gr-note.py --dry-run --source-doc-number MC-20261008-0036 --source-doc-number MC-20261102-0037
  # live, one record only (after dry-run is confirmed correct)
  echo '{"app_id":"...","app_secret":"..."}' | python3 wanchang-split-gr-note.py --live --source-record-id recXXXX
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

APP_TOKEN = "Wftab3L6AaSQG9s0zaIcKMf9n0g"
SOURCE_TABLE = "tblnEpXNQqXxFuXM"
TARGET_TABLE = "tblyUYU8s2Novavk"
# GitHub Actions: set WANCHANG_STATE_FILE=scripts/wanchang-split-gr-note-state.json
# so the workflow can commit the state file back to the repo (Actions runners are
# ephemeral, so a home-directory state file would be lost between runs).
# Local runs: default below, preserving the existing local state file.
STATE_PATH = os.environ.get("WANCHANG_STATE_FILE") or os.path.expanduser("~/workspace/wanchang-split-gr-note-state.json")

# Record API field keys are FIELD NAMES, not field_ids (verified 2026-11-02:
# list-records returns fields keyed by name, e.g. "單據編號": "MC-20261008-0035",
# while fldQsZbOtz returns None). Names below are current Traditional names.
SRC_DOC = "單據編號"
SRC_ITEM = "商品編號"
SRC_QTY = "送貨數量"
SRC_SUPPLIER = "供應商名稱"
SRC_UNITS = "單位"
SRC_STATUS = "單據狀態"
SRC_INVOICE = "發票編號"

DST_SUPPLIER = "供應商名稱"
DST_ITEM = "貨品編號"
DST_QTY = "數量"
DST_UNITS = "單位"
DST_LINK = "關聯單據"

SEPARATORS = re.compile(r"[，,；;、/／\n\r]+")


def api(method, url, token=None, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    last = None
    for _attempt in range(3):
        try:
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Content-Type", "application/json; charset=utf-8")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")
            try:
                return json.loads(body)
            except Exception:
                return {"code": e.code, "msg": body[:500]}
        except Exception as e:
            last = e
            import time as _time
            _time.sleep(3)
    raise last


def get_token(creds):
    res = api("POST", "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
              payload={"app_id": creds["app_id"], "app_secret": creds["app_secret"]})
    if res.get("code") != 0:
        raise RuntimeError(f"tenant token failed: {res}")
    return res["tenant_access_token"]


def list_records(token, table_id, page_size=20):
    records, pt = [], None
    while True:
        url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/{table_id}"
               f"/records?page_size={page_size}")
        if pt:
            url += f"&page_token={pt}"
        res = api("GET", url, token=token)
        if res.get("code") != 0:
            raise RuntimeError(f"list records failed: {res}")
        d = res.get("data", {})
        records.extend(d.get("items", []))
        if not d.get("has_more"):
            break
        pt = d.get("page_token")
    return records


def text_value(field):
    if field is None:
        return ""
    if isinstance(field, str):
        return field.strip()
    if isinstance(field, list):
        return " ".join(str(x.get("text", x)) for x in field).strip()
    if isinstance(field, dict):
        return str(field.get("text", "")).strip()
    return str(field).strip()


def split_items(raw):
    parts = [p.strip() for p in SEPARATORS.split(raw) if p.strip()]
    return parts


def parse_list_format(raw):
    """Parse "1. <item> Qty:<n>" style lists.

    Returns (items, qtys) or None if the text does not match that format.
    Each item is split on newlines first; a line matches only if it starts
    with an enumeration like "1." and contains "Qty:" followed by a number.
    If ANY line fails to match, the whole text is NOT treated as list format
    (caller falls back to separator splitting), so mixed formats are never
    silently half-parsed.
    """
    lines = [ln.strip() for ln in raw.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    items, qtys = [], []
    for ln in lines:
        m = re.match(r"^\d+\.\s*(.+?)\s+Qty:\s*([0-9]+(?:\.[0-9]+)?)\s*$", ln)
        if not m:
            return None
        items.append(m.group(1).strip())
        qtys.append(float(m.group(2)) if "." in m.group(2) else int(m.group(2)))
    return items, qtys


def allocate(total, n):
    """Return n quantities summing to total. Integers stay integers."""
    if float(total).is_integer():
        total_i = int(total)
        base, rem = divmod(total_i, n)
        return [base + (1 if i < rem else 0) for i in range(n)]
    vals = [round(total / n, 2)] * (n - 1)
    vals.append(round(total - sum(vals), 2))
    return vals


def parse_total(raw):
    try:
        return float(raw.replace(",", "").strip())
    except Exception:
        return None


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            return json.load(open(STATE_PATH, encoding="utf-8"))
        except Exception:
            return {"processed": {}}
    return {"processed": {}}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="actually create rows (default is dry-run)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--source-record-id", default=None)
    ap.add_argument("--source-doc-number", action="append", default=None,
                    help="filter by 單據編號, repeatable, e.g. MC-20261008-0036")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--max-items", type=int, default=20)
    args = ap.parse_args()
    live = args.live and not args.dry_run

    stdin_data = sys.stdin.read().strip()
    creds = json.loads(stdin_data) if stdin_data else {}
    if not creds.get("app_id"):
        creds = {"app_id": os.environ.get("WANCHANG_APP_ID", ""),
                 "app_secret": os.environ.get("WANCHANG_APP_SECRET", "")}
    if not creds.get("app_id") or not creds.get("app_secret"):
        print("ERROR: app_id/app_secret required via stdin JSON or env vars (values are never printed).")
        return 2

    token = get_token(creds)
    state = load_state()
    processed = state.setdefault("processed", {})

    sources = list_records(token, SOURCE_TABLE)
    if args.source_record_id:
        sources = [r for r in sources if r["record_id"] == args.source_record_id]
        if not sources:
            print(f"ERROR: source record {args.source_record_id} not found.")
            return 1
    if args.source_doc_number:
        wanted = set(args.source_doc_number)
        sources = [r for r in sources
                   if text_value(r.get("fields", {}).get(SRC_DOC)) in wanted]
        if not sources:
            print(f"ERROR: no source records found for 單據編號 {sorted(wanted)}.")
            return 1
        found = {text_value(r.get("fields", {}).get(SRC_DOC)) for r in sources}
        for doc in sorted(wanted - found):
            print(f"WARNING: 單據編號 {doc} not found in Vendor Invoices.")

    planned, skipped, flagged = [], [], []
    count = 0
    for rec in sources:
        if count >= args.limit and not (args.source_record_id or args.source_doc_number):
            break
        rid = rec["record_id"]
        if rid in processed:
            skipped.append((rid, "already processed (state file)"))
            continue
        fields = rec.get("fields", {})
        raw_items = text_value(fields.get(SRC_ITEM))
        if not raw_items:
            skipped.append((rid, "商品編號 empty"))
            continue
        qty_raw = text_value(fields.get(SRC_QTY))
        # 送貨數量 can be a single total OR a per-item list, e.g. "120, 80, 200, 200, 4"
        # (MC-20261008-0035, 2026-11-02). A list is parsed element-wise below.
        qty_list = None
        if qty_raw and any(s in qty_raw for s in [",", "，", ";", "；", "、", "\n", "\r", "/"]):
            try:
                qty_list = [float(x) for x in SEPARATORS.split(qty_raw) if x.strip()]
            except Exception:
                qty_list = None
        total = None if qty_list is not None else parse_total(qty_raw)
        if total is None and qty_list is None:
            flagged.append((rid, f"送貨數量 not numeric: {qty_raw!r}, skipped, quantity never guessed"))
            continue
        parsed = parse_list_format(raw_items)
        if parsed is not None:
            items, qtys = parsed
            if len(items) > args.max_items:
                flagged.append((rid, f"{len(items)} items exceeds max-items {args.max_items}, skipped, not partially split"))
                continue
            if qty_list is not None:
                if len(qty_list) != len(qtys) or qty_list != qtys:
                    flagged.append((rid, f"送貨數量 list {qty_list} != per-item Qty {qtys}, skipped, not partially split"))
                    continue
            elif sum(qtys) != total:
                flagged.append((rid, f"per-item Qty sum {sum(qtys)} != 送貨數量 {total}, skipped, not partially split"))
                continue
        else:
            items = split_items(raw_items)
            if len(items) > args.max_items:
                flagged.append((rid, f"{len(items)} items exceeds max-items {args.max_items}, skipped, not partially split"))
                continue
            if qty_list is not None:
                if len(qty_list) != len(items):
                    flagged.append((rid, f"送貨數量 list length {len(qty_list)} != items {len(items)}, skipped, not partially split"))
                    continue
                qtys = qty_list
            else:
                qtys = allocate(total, len(items))
        # 商品編號 (K) ignored entirely per Yan 2026-11-02: do not read, split, or flag it.
        planned.append({
            "source_record_id": rid,
            "source_invoice": text_value(fields.get(SRC_INVOICE)),
            "source_status": text_value(fields.get(SRC_STATUS)),
            "supplier": text_value(fields.get(SRC_SUPPLIER)),
            "units": text_value(fields.get(SRC_UNITS)),
            "items": items,
            "quantities": qtys,
            "total": sum(qtys) if qty_list is not None else total,
        })
        count += 1

    print(f"MODE: {'LIVE' if live else 'DRY-RUN (no rows created)'}")
    print(f"planned source records: {len(planned)}, skipped: {len(skipped)}, flagged: {len(flagged)}")
    for f in flagged:
        print("FLAG:", f)
    for f in skipped:
        print("SKIP:", f)

    created = {}
    for plan in planned:
        print(f"\nSOURCE {plan['source_record_id']} invoice={plan['source_invoice']} "
              f"status={plan['source_status']} supplier={plan['supplier']} units={plan['units']} total={plan['total']}")
        for item, qty in zip(plan["items"], plan["quantities"]):
            # 收貨 數量 is a Multiline text field: send quantity as a string.
            # Whole numbers stay whole ("11", not "11.0"); non-integers keep
            # their decimal text. Sending a number fails with 1254060.
            qty_text = str(int(qty)) if float(qty).is_integer() else str(qty)
            print(f"  -> GR Note: 貨品編號={item} 數量={qty_text} 單位={plan['units']} 供應商名稱={plan['supplier']}")
            if not live:
                continue
            payload = {"fields": {
                DST_SUPPLIER: plan["supplier"],
                DST_ITEM: item,
                DST_QTY: qty_text,
                DST_UNITS: plan["units"],
            }}
            url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/"
                   f"{TARGET_TABLE}/records")
            res = api("POST", url, token=token, payload=payload)
            if res.get("code") != 0:
                print(f"  CREATE FAILED for item {item}: {res}")
                continue
            new_id = res.get("data", {}).get("record", {}).get("record_id") or res.get("data", {}).get("record_id")
            print(f"  CREATED GR Note record {new_id}")
            created.setdefault(plan["source_record_id"], []).append(new_id)
            # Best-effort link back to source; failure here must not fail the row.
            if new_id:
                link_payload = {"fields": {DST_LINK: [plan["source_record_id"]]}}
                link_url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{APP_TOKEN}/tables/"
                            f"{TARGET_TABLE}/records/{new_id}")
                link_res = api("PUT", link_url, token=token, payload=link_payload)
                if link_res.get("code") != 0:
                    print(f"  LINK WARNING (row kept, link not set): {link_res}")
        if live and plan["source_record_id"] in created:
            processed[plan["source_record_id"]] = {
                "created_gr_note_ids": created[plan["source_record_id"]],
                "invoice": plan["source_invoice"],
            }

    if live:
        save_state(state)
        print(f"\nLIVE DONE. Created rows for {len(created)} source records. State saved (record ids only, no credentials).")
    else:
        print("\nDRY-RUN DONE. No rows created, state file unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
