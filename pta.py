"""PTA active problem sets exporter. Credentials are never written by this script."""
import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ACTIVE = "https://pintia.cn/problem-sets/active"
LOGIN = "https://pintia.cn/auth/login"
CARD = 'a[href*="/problem-sets/"][href$="/overview"]'


def parse_card(text, url):
    match = re.search(r"关闭时间\s*[:：]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)", text)
    if not match:
        raise ValueError(f"未找到关闭时间：{text[:120]}")
    name = text[:match.start()].strip()
    if not name:
        raise ValueError("题目集名称为空")
    return {"name": name, "close_time": match.group(1), "url": url}


def ready(page):
    # Wait for either actual cards or a completed, explicitly empty list.
    page.wait_for_function("""() =>
      document.querySelector('a[href*="/problem-sets/"][href$="/overview"]') ||
      /共\\s*0\\s*条数据/.test(document.body.innerText) ||
      document.body.innerText.includes('用户不存在') ||
      location.pathname.includes('/auth/login')
    """, timeout=30000)
    body = page.locator("body").inner_text()
    return ("/auth/login" not in page.url and "用户不存在" not in body
            and (page.locator(CARD).count() > 0 or re.search(r"共\s*0\s*条数据", body)))


def extract(page):
    rows, seen = [], set()
    while True:
        page.wait_for_function("""() =>
          document.querySelector('a[href*="/problem-sets/"][href$="/overview"]') ||
          /共\\s*0\\s*条数据/.test(document.body.innerText)
        """, timeout=30000)
        cards = page.locator(CARD)
        for card in cards.all():
            url = card.evaluate("el => el.href")
            if url not in seen:
                rows.append(parse_card(card.inner_text(), url))
                seen.add(url)
        body = page.locator("body").inner_text()
        total_match = re.search(r"共\s*(\d+)\s*条数据", body)
        total = int(total_match.group(1)) if total_match else None
        if total is not None and len(rows) == total:
            break
        # Fail closed if pagination is not recognizable, instead of exporting a partial list.
        next_button = page.get_by_role("button", name=re.compile(r"^(下一页|Next page|Next)$"))
        if next_button.count() != 1 or not next_button.is_enabled():
            raise RuntimeError(f"未确认完整列表：已读 {len(rows)}，页面总数 {total}；请检查分页控件。")
        old_urls = cards.evaluate_all("els => els.map(el => el.href).join('|')")
        next_button.click()
        page.wait_for_function("""old => Array.from(document.querySelectorAll(
          'a[href*="/problem-sets/"][href$="/overview"]')).map(el => el.href).join('|') !== old
        """, arg=old_urls, timeout=30000)
    return sorted(rows, key=lambda row: row["close_time"])


def main():
    parser = argparse.ArgumentParser(description="登录 PTA 并导出活跃题目集及关闭时间")
    parser.add_argument("--headless", action="store_true", help="无窗口运行；必须已有有效登录状态")
    parser.add_argument("--channel", choices=["msedge", "chrome", "chromium"], default="msedge")
    args = parser.parse_args()
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

    state = ROOT / ".auth" / "state.json"
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=args.channel, headless=args.headless)
        context = browser.new_context(storage_state=str(state) if state.exists() else None,
                                      locale="zh-CN", timezone_id="Asia/Shanghai")
        try:
            page = context.new_page()
            page.goto(ACTIVE, wait_until="domcontentloaded")
            try:
                authenticated = ready(page)
            except PlaywrightTimeout:
                # A network/render failure must not be mistaken for an expired login.
                raise RuntimeError("列表加载超时，请检查网络或 PTA 页面；本次未输出结果。")
            if not authenticated:
                if args.headless:
                    raise RuntimeError("登录状态不存在或已失效，请先不带 --headless 运行并登录。")
                page.goto(LOGIN, wait_until="domcontentloaded")
                username, password = os.environ.get("PTA_USERNAME"), os.environ.get("PTA_PASSWORD")
                if username and password:
                    password_input = page.locator('input[type="password"]')
                    password_input.wait_for()
                    page.locator('input:not([type="password"]):not([type="checkbox"]):not([type="hidden"])').filter(visible=True).first.fill(username)
                    password_input.fill(password)
                    page.get_by_role("button", name="登录", exact=True).click()
                    print("已提交登录。若出现验证码，请在浏览器中完成。")
                else:
                    print("请在新打开的浏览器中登录 PTA（登录状态将保存在本机 .auth 中）。")
                input("确认浏览器已登录后，按回车继续：")
                page.goto(ACTIVE, wait_until="domcontentloaded")
                if not ready(page):
                    raise RuntimeError("未确认登录成功，本次未输出结果。")
            state.parent.mkdir(exist_ok=True)
            context.storage_state(path=str(state), indexed_db=True)
            rows = extract(page)
            output = ROOT / "output"
            output.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            json_path = output / f"pta-{stamp}.json"
            csv_path = output / f"pta-{stamp}.csv"
            json_path.write_text(json.dumps({"source": ACTIVE, "time_zone": "Asia/Shanghai",
                                           "fetched_at": datetime.now().astimezone().isoformat(),
                                           "count": len(rows), "problem_sets": rows},
                                          ensure_ascii=False, indent=2), encoding="utf-8")
            with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["题目集名称", "关闭时间（北京时间）", "链接"])
                for row in rows:
                    # Prevent spreadsheet formula interpretation of untrusted titles.
                    name = row["name"]
                    if name.lstrip().startswith(("=", "+", "-", "@")):
                        name = "'" + name
                    writer.writerow([name, row["close_time"], row["url"]])
            for row in rows:
                print(f'{row["close_time"]}  {row["name"]}')
            print(f"共 {len(rows)} 个题目集\nCSV: {csv_path}\nJSON: {json_path}")
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print(f"提取未完成：{exc}", file=sys.stderr)
        sys.exit(1)
