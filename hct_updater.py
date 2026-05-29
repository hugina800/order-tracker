#!/usr/bin/env python3
"""
自動查詢 HCT 宅配到貨狀態，更新 hct_status.json
給 GitHub Actions 排程使用
"""

import os
import re
import json
import requests
import warnings
from datetime import datetime, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

API_BASE = "https://gateway.globemerce.com"
PLATFORM = "FIV5S Web"

HCT_STATUS_MAP = {
    "順利送達": ("✅ 已送達", "done"),
    "已送達":   ("✅ 已送達", "done"),
    "配送中":   ("🚚 配送中", "transit"),
    "派件中":   ("🚚 派件中", "transit"),
    "已集貨":   ("📦 已集貨", "collected"),
    "攬收":     ("📦 攬收",   "collected"),
    "退件":     ("↩️ 退件",   "returned"),
    "客不在":   ("⚠️ 客不在", "failed"),
    "無法投遞": ("⚠️ 無法投遞", "failed"),
}


def gm_login(username: str, password: str) -> str:
    r = requests.post(f"{API_BASE}/user/login",
        json={"username": username, "password": password, "platform": PLATFORM},
        timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("status") is True and data.get("data", {}).get("token"):
        return data["data"]["token"]
    raise Exception(f"登入失敗：{data.get('message', '帳號或密碼錯誤')}")


def gm_get_orders(token: str) -> list:
    headers = {"Authorization": f"Bearer {token}"}
    date_to   = datetime.now().strftime("%Y-%m-%d")
    date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    all_orders, page, total = [], 1, 999
    while page <= total:
        r = requests.get(f"{API_BASE}/myorder/all/listing",
            params={"periodFrom": date_from, "periodTo": date_to,
                    "country_code": "TWTW", "client_code": "All",
                    "page_size": 50, "page": page},
            headers=headers, timeout=15)
        r.raise_for_status()
        data = r.json()
        orders = data.get("data", [])
        total  = int(data.get("pagination", {}).get("total_page", 1))
        if not orders:
            break
        all_orders.extend(orders)
        print(f"  第 {page}/{total} 頁，{len(all_orders)} 筆")
        page += 1
    return all_orders


def classify(o: dict):
    st = o.get("shop_type_name", "")
    c  = o.get("courier_name", "")
    if "7-Eleven" in st or "7eleven" in st.lower(): return "7-11"
    if "Family Mart" in st or "全家" in st:         return "全家"
    if st:                                           return "超商"
    if "HCT" in c or "新竹" in c:                   return "宅配(HCT)"
    if "Post" in c or "郵局" in c:                  return "郵局"
    return "其他"


def read_captcha(session_cookie: str) -> str:
    try:
        import ddddocr
    except ImportError:
        import subprocess, sys
        subprocess.run([sys.executable, "-m", "pip", "install", "ddddocr", "-q"],
                       capture_output=True)
        import ddddocr

    r = requests.get(
        "https://www.hct.com.tw/Search/BuildCaptchaN.aspx",
        cookies={"ASP.NET_SessionId": session_cookie},
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
                 "Referer": "https://www.hct.com.tw/Search/SearchGoods_n.aspx"},
        timeout=10)
    r.raise_for_status()
    ocr = ddddocr.DdddOcr(show_ad=False)
    return re.sub(r"[^0-9a-zA-Z]", "", ocr.classification(r.content))


def check_hct_batch(tracking_numbers: list) -> dict:
    if not tracking_numbers:
        return {}

    from playwright.sync_api import sync_playwright

    results = {}
    batches = [tracking_numbers[i:i+10] for i in range(0, len(tracking_numbers), 10)]
    total   = len(tracking_numbers)
    done    = 0

    print(f"\n🔍 查詢 HCT（共 {total} 筆，{len(batches)} 批）...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx  = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        for bi, batch in enumerate(batches):
            success = False
            for attempt in range(4):
                try:
                    page.goto("https://www.hct.com.tw/Search/SearchGoods_n.aspx", timeout=20000)
                    page.wait_for_timeout(1500)

                    session_id = next(
                        (c["value"] for c in ctx.cookies() if c["name"] == "ASP.NET_SessionId"),
                        ""
                    )
                    captcha = read_captcha(session_id)
                    if not captcha or len(captcha) < 4:
                        print(f"  驗證碼讀取失敗，重試({attempt+1}/4)")
                        continue

                    print(f"  批次 {bi+1}/{len(batches)}：{captcha}", end=" ... ")

                    page.evaluate("""(data) => {
                        const inputs = document.querySelectorAll('input[placeholder="10碼貨號/提單號"]');
                        data.numbers.forEach((n, i) => { if (inputs[i]) inputs[i].value = n; });
                        const chk = document.querySelector('input[placeholder=" 驗證碼"]');
                        if (chk) chk.value = data.captcha;
                        const btn = document.querySelector('input[type="submit"]');
                        if (btn) btn.click();
                    }""", {"numbers": batch, "captcha": captcha})

                    page.wait_for_timeout(3000)

                    lines = [l.strip() for l in page.evaluate("() => document.body.innerText").split('\n') if l.strip()]
                    batch_results = {}
                    for i, line in enumerate(lines):
                        if re.match(r"^\d{10}$", line):
                            raw_date   = lines[i+1] if i+1 < len(lines) else ""
                            raw_status = lines[i+2] if i+2 < len(lines) else ""
                            label, cls = HCT_STATUS_MAP.get(raw_status, (raw_status or "查無資料", "other"))
                            dt_str = None
                            if re.match(r"\d{4}[-/]\d{2}[-/]\d{2}", raw_date):
                                dt_str = raw_date[:10]
                            batch_results[line] = {"status": label, "status_cls": cls, "date": dt_str}

                    if any(n in batch_results for n in batch):
                        results.update(batch_results)
                        done += len(batch)
                        print(f"✅ {done}/{total}")
                        success = True
                        break
                    else:
                        print(f"驗證碼錯誤，重試({attempt+1}/4)")

                except Exception as e:
                    print(f"\n  錯誤：{e}，重試({attempt+1}/4)")

            if not success:
                for n in batch:
                    results[n] = {"status": "查詢失敗", "status_cls": "error", "date": None}
                print(f"  ❌ 批次 {bi+1} 失敗")

        browser.close()
    return results


def main():
    username = os.environ.get("GM_USERNAME") or input("帳號：").strip()
    password = os.environ.get("GM_PASSWORD") or input("密碼：").strip()

    print("🔑 登入 Globemerce...")
    token = gm_login(username, password)
    print("✅ 登入成功")

    print("\n📋 取得訂單...")
    orders = gm_get_orders(token)
    print(f"✅ {len(orders)} 筆")

    hct_numbers = list({
        o["tracking_code"]
        for o in orders
        if classify(o) == "宅配(HCT)"
        and int(o.get("is_payment_link", 0)) == 1
        and o.get("tracking_code")
    })

    print(f"\n需查 HCT：{len(hct_numbers)} 筆")

    if not hct_numbers:
        print("（無需查詢）")
        return

    hct_results = check_hct_batch(hct_numbers)

    # 讀取現有 hct_status.json 合併
    out_path = Path(__file__).parent / "hct_status.json"
    existing = {}
    if out_path.exists():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8")).get("tracking", {})
        except Exception:
            pass

    existing.update(hct_results)
    output = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "tracking": existing
    }
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n✅ 已更新 hct_status.json（{len(hct_results)} 筆）")


if __name__ == "__main__":
    main()
