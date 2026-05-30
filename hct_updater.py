#!/usr/bin/env python3
"""
自動查詢 HCT / 7-11 / 全家 到貨狀態，更新 hct_status.json
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

API_BASE    = "https://gateway.globemerce.com"
PLATFORM    = "FIV5S Web"
RETURN_DAYS = 7
RELEVANT    = {"7-11", "全家", "超商", "宅配(HCT)", "郵局"}

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

SEVEN_STATUS_MAP = {
    "已到達取件門市": ("🏪 到店待取", "arrived"),
    "門市通知取件":   ("🏪 到店待取", "arrived"),
    "已通知取件人":   ("🏪 到店待取", "arrived"),
    "取件完成":       ("✅ 已取件",   "done"),
    "取件人已取件":   ("✅ 已取件",   "done"),
    "出貨中":         ("🚚 配送中",   "transit"),
    "配送中":         ("🚚 配送中",   "transit"),
    "逾期未取退回":   ("↩️ 退回",     "returned"),
}

FAMILY_STATUS_MAP = {
    "到店通知":   ("🏪 到店待取", "arrived"),
    "已到達門市": ("🏪 到店待取", "arrived"),
    "已取件":     ("✅ 已取件",   "done"),
    "取件完成":   ("✅ 已取件",   "done"),
    "配送中":     ("🚚 配送中",   "transit"),
    "出貨中":     ("🚚 配送中",   "transit"),
    "逾期退回":   ("↩️ 退回",     "returned"),
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
    sn = o.get("shop_name", "")
    c  = o.get("courier_name", "")
    if "7-Eleven" in st or "7eleven" in st.lower(): return "7-11", sn
    if "Family Mart" in st or "全家" in st:         return "全家", sn
    if st:                                           return "超商", sn
    if "HCT" in c or "新竹" in c:                   return "宅配(HCT)", ""
    if "Post" in c or "郵局" in c:                  return "郵局", ""
    if "QSY" in c or "Self Collect" in c:           return "超商", sn
    return "其他", ""


def is_hct_payment_link(o: dict) -> bool:
    dtype, _ = classify(o)
    return dtype == "宅配(HCT)" and int(o.get("is_payment_link", 0)) == 1


def parse_dt(s: str):
    if not s or s.startswith("0000"):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:len(fmt)], fmt)
        except ValueError:
            pass
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return None


def build_snapshot(orders: list, hct_results: dict) -> dict:
    date_to   = datetime.now().strftime("%Y-%m-%d")
    date_from = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    def products(o):
        return ", ".join(
            d.get("product_name", "") for d in (o.get("order_detail") or [])[:3]
            if d.get("product_name", "") and not d.get("product_name", "").startswith("TW-DISCOUNT")
        )

    def fmt(d):
        if d is None:
            return None
        if isinstance(d, datetime):
            return d.strftime("%Y-%m-%d")
        s = str(d)[:10]
        return s if s and not s.startswith("0000") else None

    snapshot_orders = []
    for o in orders:
        dtype, shop_name = classify(o)
        if dtype not in RELEVANT:
            continue

        tracking   = o.get("tracking_code") or ""
        arrived    = None
        deadline   = None
        status     = "🚚 運送中"
        status_cls = "transit"

        if is_hct_payment_link(o):
            info       = hct_results.get(tracking, {})
            status     = info.get("status", "—")
            status_cls = info.get("status_cls", "other")
            arrived    = fmt(info.get("date"))
        elif (o.get("courier_type") == "cod"
              and o.get("status_word") == "Completed"
              and o.get("donedate") and not o["donedate"].startswith("0000")):
            dt         = parse_dt(o["donedate"])
            status     = "✅ 已取件"
            status_cls = "done"
            arrived    = fmt(dt)
        elif tracking and tracking in hct_results:
            info       = hct_results[tracking]
            status     = info.get("status", "🚚 運送中")
            status_cls = info.get("status_cls", "transit")
            arrived    = fmt(info.get("date"))

        if arrived:
            try:
                deadline = (datetime.strptime(arrived, "%Y-%m-%d") + timedelta(days=RETURN_DAYS)).strftime("%Y-%m-%d")
            except ValueError:
                pass

        entry = {
            "id":         str(o.get("o_id", "")),
            "type":       dtype,
            "shop":       shop_name,
            "tracking":   tracking,
            "buyer":      o.get("receiver_name", "—"),
            "product":    products(o),
            "order_date": (o.get("created") or "")[:10],
            "status":     status,
            "status_cls": status_cls,
        }
        if arrived:
            entry["arrived_date"]    = arrived
        if deadline:
            entry["return_deadline"] = deadline

        snapshot_orders.append(entry)

    priority = {"arrived": 0, "failed": 1, "transit": 2,
                "collected": 3, "done": 4, "returned": 5, "other": 6, "error": 7}
    snapshot_orders.sort(key=lambda x: (
        priority.get(x["status_cls"], 99),
        x.get("return_deadline", "9999")
    ))

    return {
        "updated":     datetime.now().strftime("%Y-%m-%d %H:%M"),
        "period_from": date_from,
        "period_to":   date_to,
        "orders":      snapshot_orders
    }


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


def ocr_captcha_url(url: str, cookies: dict, referer: str) -> str:
    try:
        import ddddocr
    except ImportError:
        import subprocess, sys
        subprocess.run([sys.executable, "-m", "pip", "install", "ddddocr", "-q"],
                       capture_output=True)
        import ddddocr

    r = requests.get(url, cookies=cookies,
                     headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
                              "Referer": referer},
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
                            if re.match(r"^\d{10}$", raw_status):
                                continue
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


def check_711_batch(tracking_numbers: list) -> dict:
    if not tracking_numbers:
        return {}

    from playwright.sync_api import sync_playwright
    import time as _time

    results = {}
    total = len(tracking_numbers)
    done  = 0
    url   = "https://eservice.7-11.com.tw/e-tracking/search.aspx"

    print(f"\n🔍 查詢 7-11 物流（共 {total} 筆）...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx  = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        for no in tracking_numbers:
            success = False
            for attempt in range(4):
                try:
                    page.goto(url, timeout=20000)
                    page.wait_for_timeout(1500)

                    cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                    captcha = ocr_captcha_url(
                        f"https://eservice.7-11.com.tw/e-tracking/ValidateImage.aspx?ts={int(_time.time())}",
                        cookies, url)
                    if not captcha or len(captcha) < 4:
                        print(f"  [{no}] 驗證碼讀取失敗，重試({attempt+1}/4)")
                        continue

                    print(f"  [{no}] 驗證碼 {captcha}", end=" ... ")

                    page.evaluate("""(d) => {
                        document.getElementById('txtProductNum').value = d.no;
                        document.getElementById('tbChkCode').value = d.cap;
                        document.querySelector('input[name="aaa"]').click();
                    }""", {"no": no, "cap": captcha})
                    page.wait_for_timeout(3000)

                    text  = page.evaluate("() => document.body.innerText")
                    lines = [l.strip() for l in text.split('\n') if l.strip()]

                    arrived_date = None
                    status_label, status_cls = "查無資料", "other"
                    for line in lines:
                        m = re.search(r"(\d{4}[/-]\d{2}[/-]\d{2})", line)
                        if m and not arrived_date:
                            arrived_date = m.group(1).replace("/", "-")
                        for kw, (lbl, cls) in SEVEN_STATUS_MAP.items():
                            if kw in line:
                                status_label, status_cls = lbl, cls
                                if not arrived_date:
                                    m2 = re.search(r"(\d{4}[/-]\d{2}[/-]\d{2})", line)
                                    if m2:
                                        arrived_date = m2.group(1).replace("/", "-")

                    if "驗證碼" in text and status_label == "查無資料":
                        print(f"驗證碼錯誤，重試({attempt+1}/4)")
                        continue

                    results[no] = {"status": status_label, "status_cls": status_cls, "date": arrived_date}
                    done += 1
                    print(f"✅ {status_label} {arrived_date or ''}")
                    success = True
                    break

                except Exception as e:
                    print(f"\n  [{no}] 錯誤：{e}，重試({attempt+1}/4)")

            if not success:
                results[no] = {"status": "查詢失敗", "status_cls": "error", "date": None}

        browser.close()
    return results


def check_family_batch(tracking_numbers: list) -> dict:
    if not tracking_numbers:
        return {}

    from playwright.sync_api import sync_playwright

    results = {}
    total = len(tracking_numbers)
    done  = 0
    url   = "https://fmec.famiport.com.tw/FP_Entrance/QueryBox"

    print(f"\n🔍 查詢全家物流（共 {total} 筆）...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx  = browser.new_context(viewport={"width": 1280, "height": 900})
        page = ctx.new_page()

        for no in tracking_numbers:
            success = False
            for attempt in range(4):
                try:
                    page.goto(url, timeout=20000)
                    page.wait_for_timeout(2000)

                    frame = page.frame_locator("iframe").first

                    captcha_img_url = page.evaluate("""() => {
                        const iframes = document.querySelectorAll('iframe');
                        for (const f of iframes) {
                            try {
                                const img = f.contentDocument?.querySelector('img[src*="captcha"], img[src*="Captcha"], img[src*="code"], img[src*="Code"], img[src*="verify"]');
                                if (img) return img.src;
                            } catch(e) {}
                        }
                        return null;
                    }""")

                    if not captcha_img_url:
                        captcha_img_url = frame.locator("img").first.get_attribute("src") if frame else None

                    captcha = ""
                    if captcha_img_url:
                        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                        captcha = ocr_captcha_url(captcha_img_url, cookies, url)

                    try:
                        frame.locator("input[type='text']").first.fill(no)
                        if captcha:
                            frame.locator("input[placeholder*='驗證'], input[placeholder*='code']").fill(captcha)
                        frame.locator("button[type='submit'], input[type='submit'], a.submit, button").first.click()
                    except Exception:
                        page.fill("input[name='orderno']", no)
                        page.press("input[name='orderno']", "Enter")

                    page.wait_for_timeout(3000)

                    text  = page.evaluate("() => document.body.innerText")
                    lines = [l.strip() for l in text.split('\n') if l.strip()]

                    arrived_date = None
                    status_label, status_cls = "查無資料", "other"
                    for line in lines:
                        m = re.search(r"(\d{4}[/-]\d{2}[/-]\d{2})", line)
                        if m and not arrived_date:
                            arrived_date = m.group(1).replace("/", "-")
                        for kw, (lbl, cls) in FAMILY_STATUS_MAP.items():
                            if kw in line:
                                status_label, status_cls = lbl, cls
                                if not arrived_date:
                                    m2 = re.search(r"(\d{4}[/-]\d{2}[/-]\d{2})", line)
                                    if m2:
                                        arrived_date = m2.group(1).replace("/", "-")

                    results[no] = {"status": status_label, "status_cls": status_cls, "date": arrived_date}
                    done += 1
                    print(f"  [{no}] ✅ {status_label} {arrived_date or ''}")
                    success = True
                    break

                except Exception as e:
                    print(f"\n  [{no}] 錯誤：{e}，重試({attempt+1}/4)")

            if not success:
                results[no] = {"status": "查詢失敗", "status_cls": "error", "date": None}

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
        if classify(o)[0] == "宅配(HCT)"
        and int(o.get("is_payment_link", 0)) == 1
        and o.get("tracking_code")
    })
    seven_numbers = list({
        o["tracking_code"]
        for o in orders
        if classify(o)[0] == "7-11"
        and o.get("status_word") == "Delivering"
        and o.get("tracking_code")
    })
    family_numbers = list({
        o["tracking_code"]
        for o in orders
        if classify(o)[0] == "全家"
        and o.get("status_word") == "Delivering"
        and o.get("tracking_code")
    })

    print(f"\n需查 HCT：{len(hct_numbers)} 筆，7-11：{len(seven_numbers)} 筆，全家：{len(family_numbers)} 筆")

    all_results = {}
    if hct_numbers:
        all_results.update(check_hct_batch(hct_numbers))
    else:
        print("（無 HCT 信用卡宅配訂單）")

    if seven_numbers:
        all_results.update(check_711_batch(seven_numbers))
    else:
        print("（無 7-11 運送中訂單）")

    if family_numbers:
        all_results.update(check_family_batch(family_numbers))
    else:
        print("（無全家運送中訂單）")

    repo_dir = Path(__file__).parent

    # 更新 hct_status.json
    if all_results:
        out_path = repo_dir / "hct_status.json"
        existing = {}
        if out_path.exists():
            try:
                existing = json.loads(out_path.read_text(encoding="utf-8")).get("tracking", {})
            except Exception:
                pass
        existing.update(all_results)
        output = {
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "tracking": {
                no: {"status": info.get("status","—"),
                     "status_cls": info.get("status_cls","other"),
                     "date": str(info.get("date","") or "")[:10] or None}
                for no, info in existing.items()
            }
        }
        out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n✅ 已更新 hct_status.json（{len(all_results)} 筆新結果）")
    else:
        print("\n（無需查詢任何物流）")

    # 產生助理看板 snapshot
    snapshot = build_snapshot(orders, all_results)
    snap_path = repo_dir / "orders_snapshot.json"
    snap_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 已產生 orders_snapshot.json（{len(snapshot['orders'])} 筆訂單）")


if __name__ == "__main__":
    main()
