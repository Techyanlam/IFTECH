#!/usr/bin/env python3
"""One-off GitHub Actions test: write one clearly-marked test order to the
BETTEREAT V1 Base '網上訂單' table via Lark API, then verify by reading it back.

Mode create: create + verify, write test-order-result.json (does NOT delete,
so staff can see the test record in Feishu before cleanup).
Mode delete: delete the record_id from the result file (or --record-id),
verify it is gone, update test-order-result.json.

Secrets come only from env (GitHub repo Secrets). Nothing secret is printed.
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_TOKEN = "YvpCbgjTia7wqWsMjNhcPwL7nih"
TABLE_ID = "tblh6rwkuOnNNSjN"
API = "https://open.larksuite.com/open-apis"
RESULT_FILE = "test-order-result.json"


def api_post(path, token, body):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(API + path, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def api_get(path, token):
    req = urllib.request.Request(API + path,
                                 headers={"Authorization": "Bearer " + token},
                                 method="GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def hkt_today_ms():
    d = datetime.now(timezone(timedelta(hours=8))).date()
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["create", "delete"], required=True)
    ap.add_argument("--record-id", default="")
    args = ap.parse_args()
    result = {"mode": args.mode}
    try:
        app_id = os.environ["FEISHU_APP_ID"]
        app_secret = os.environ["FEISHU_APP_SECRET"]
        tok = api_post("/auth/v3/tenant_access_token/internal", None,
                       {"app_id": app_id, "app_secret": app_secret})
        if tok.get("code") != 0 or not tok.get("tenant_access_token"):
            raise RuntimeError("token failed, code=%s" % tok.get("code"))
        token = tok["tenant_access_token"]
        if args.mode == "create":
            fields = {
                "取貨門店": "謝斐 (MJF)",
                "顧客姓名": "GitHub測試單",
                "聯絡電話": "00000000",
                "取貨日期": hkt_today_ms(),
                "取貨時間": "測試",
                "產品1": "測試產品",
                "數量1": 1,
                "訂單狀態": "待確認",
                "備註": "GitHub Actions 寫入測試，核實後刪除",
            }
            created = api_post(
                "/bitable/v1/apps/%s/tables/%s/records/batch_create" % (BASE_TOKEN, TABLE_ID),
                token, {"records": [{"fields": fields}]})
            if created.get("code") != 0:
                raise RuntimeError("create failed, code=%s msg=%s"
                                   % (created.get("code"), created.get("msg")))
            record_id = created["data"]["records"][0]["record_id"]
            result["record_id"] = record_id
            listed = api_get(
                "/bitable/v1/apps/%s/tables/%s/records?page_size=50" % (BASE_TOKEN, TABLE_ID),
                token)
            found = None
            for it in listed.get("data", {}).get("items", []):
                if it["record_id"] == record_id:
                    found = it["fields"]
                    break
            result["verified"] = found is not None
            result["found_fields"] = found
            print("created record:", record_id, "verified:", result["verified"])
        else:
            record_id = args.record_id
            if not record_id and os.path.exists(RESULT_FILE):
                with open(RESULT_FILE) as f:
                    record_id = json.load(f).get("record_id", "")
            if not record_id:
                raise RuntimeError("no record_id: pass --record-id or run create first")
            deleted = api_post(
                "/bitable/v1/apps/%s/tables/%s/records/batch_delete" % (BASE_TOKEN, TABLE_ID),
                token, {"records": [record_id]})
            if deleted.get("code") != 0:
                raise RuntimeError("delete failed, code=%s msg=%s"
                                   % (deleted.get("code"), deleted.get("msg")))
            listed = api_get(
                "/bitable/v1/apps/%s/tables/%s/records?page_size=50" % (BASE_TOKEN, TABLE_ID),
                token)
            still = [it for it in listed.get("data", {}).get("items", [])
                     if it["record_id"] == record_id]
            result["record_id"] = record_id
            result["verified_gone"] = len(still) == 0
            print("deleted record:", record_id, "gone:", result["verified_gone"])
    except Exception as e:
        result["error"] = str(e)
        print("FAILED:", e, file=sys.stderr)
        with open(RESULT_FILE, "w") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        return 1
    with open(RESULT_FILE, "w") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
