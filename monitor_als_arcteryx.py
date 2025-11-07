#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor Arc'teryx products on als.com:
- Track price changes, new arrivals, and stock increases
- Send Discord notifications
- Persist snapshot.json (atomic write)

Env vars:
  DISCORD_WEBHOOK_URL   Discord webhook（必填）
  ALWAYS_NOTIFY=1       即使无变化也发一条（验证连通性时开启）
  HEADLESS=0            本地调试可设为 0，Actions 默认 1
  KEYWORD_FILTER        仅监控标题包含该关键词的商品（可选）

Author: Rolland Yip helper
"""

import json
import os
import re
import sys
import time
import math
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Dict, Any, List, Tuple

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

COLLECTION_URL = "https://www.als.com/arc-teryx"
SNAPSHOT_PATH = Path("snapshot.json")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# --------------------------
# Utilities
# --------------------------

def jdump(obj: Any, path: Path):
    """Atomic write to avoid half-written or empty JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile('w', delete=False, encoding='utf-8', dir=str(path.parent)) as tmp:
        json.dump(obj, tmp, ensure_ascii=False, indent=2)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_name = tmp.name
    try:
        shutil.move(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except Exception:
            pass


def jload(path: Path) -> Dict[str, Any]:
    if not path.exists():
        print(f"[snapshot] {path} not found.")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        print(f"[snapshot] loaded {len(data)} items from {path}.")
        return data
    except Exception as e:
        print(f"[snapshot] failed to parse {path}: {e}")
        return {}


def safe_sleep(a=0.3, b=0.9):
    time.sleep(random.uniform(a, b))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_key(url: str, title: str) -> str:
    """Stable key per product; prefer PDP slug /.../<slug>/p ."""
    m = re.search(r"/([^/]+)/p(?:$|\?)", url)
    if m:
        return m.group(1).lower()
    slug = re.sub(r"[^a-z0-9]+", "-", (title or url).lower()).strip("-")
    return slug[:80]


# --------------------------
# Scraper
# --------------------------

def extract_collection_links(page) -> List[str]:
    """Collect all Arc'teryx product links from a listing page."""
    anchors = page.locator("a[href*='/arcteryx-'][href*='/p']")
    hrefs = anchors.evaluate_all("els => els.map(e => e.href)")
    uniq = []
    for h in hrefs:
        if "als.com" in h:
            h = h.split("#")[0]
            if h not in uniq:
                uniq.append(h)
    return uniq


def parse_product_detail(page) -> Dict[str, Any]:
    """Extract title, price, stock, and size info."""
    data = {
        "title": "",
        "price": math.nan,
        "orig_price": math.nan,
        "currency": "USD",
        "in_stock": False,
        "sizes_total": 0,
        "sizes_available": 0,
    }

    try:
        if page.locator("h1").count():
            data["title"] = page.locator("h1").first.inner_text().strip()
        elif page.locator("title").count():
            data["title"] = page.locator("title").first.inner_text().strip()
    except Exception:
        pass

    try:
        price_text = ""
        for sel in ["[class*='price']", "[data-test*='price']", "div:has-text('$')", "body"]:
            if page.locator(sel).count():
                txt = page.locator(sel).first.inner_text()
                if "$" in txt:
                    price_text = txt
                    break
        if price_text:
            prices = re.findall(r"\$\s*[0-9]+(?:\.[0-9]{2})?", price_text.replace(",", ""))
            if prices:
                data["price"] = float(prices[0].replace("$", "").strip())
                if len(prices) >= 2:
                    data["orig_price"] = float(prices[1].replace("$", "").strip())
    except Exception:
        pass

    try:
        in_stock = False
        for t in ("Add to bag", "Add to cart", "Add To Bag", "Add To Cart"):
            btn = page.get_by_role("button", name=re.compile(t, re.I))
            if btn.count():
                disabled = btn.first.get_attribute("disabled")
                aria = btn.first.get_attribute("aria-disabled")
                cls = (btn.first.get_attribute("class") or "")
                if disabled is None and (aria not in ("true", "disabled")) and ("disabled" not in cls):
                    in_stock = True
                    break
        if re.search(r"out of stock", page.content(), re.I):
            in_stock = False
        data["in_stock"] = in_stock
    except Exception:
        pass

    try:
        size_btns = page.locator(
            "button:has-text('XS'), button:has-text('S'), button:has-text('M'), "
            "button:has-text('L'), button:has-text('XL'), button:has-text('XXL')"
        )
        total = size_btns.count()
        available = 0
        for i in range(total):
            el = size_btns.nth(i)
            cls = (el.get_attribute("class") or "")
            if "disabled" not in cls and el.get_attribute("disabled") is None:
                available += 1
        data["sizes_total"] = total
        data["sizes_available"] = available
    except Exception:
        pass

    return data


def scrape_all_products(headless=True, timeout_ms=15000) -> Dict[str, Any]:
    result = {}
    keyword = os.environ.get("KEYWORD_FILTER", "").strip().lower()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ctx = browser.new_context(user_agent=USER_AGENT, locale="en-US")
        page = ctx.new_page()

        page_idx = 1
        empty_hits = 0
        seen_urls = set()

        while True:
            url = COLLECTION_URL if page_idx == 1 else f"{COLLECTION_URL}?page={page_idx}"
            try:
                page.goto(url, timeout=timeout_ms)
                page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except PWTimeout:
                print(f"[page] timeout loading {url}")
                empty_hits += 1
                if empty_hits >= 2:
                    break
                page_idx += 1
                continue

            links = extract_collection_links(page)
            print(f"[collection] page {page_idx} links: {len(links)}")

            if not links:
                empty_hits += 1
                if empty_hits >= 2:
                    break
                page_idx += 1
                continue

            empty_hits = 0
            for href in links:
                if href in seen_urls:
                    continue
                seen_urls.add(href)
                safe_sleep(0.5, 1.2)

                ok = False
                for attempt in range(3):
                    try:
                        page.goto(href, timeout=timeout_ms)
                        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                        safe_sleep(0.3, 0.8)
                        pdata = parse_product_detail(page)
                        title = pdata.get("title", "")
                        if keyword and keyword not in title.lower():
                            ok = True
                            break
                        if title:
                            key = normalize_key(href, title)
                            pdata.update({"url": href, "last_seen": now_iso()})
                            result[key] = pdata
                            ok = True
                            break
                    except Exception as e:
                        print(f"[detail] error {href}: {e}")
                        safe_sleep(0.8, 1.5)
                if not ok:
                    key = normalize_key(href, href)
                    result[key] = {
                        "title": "",
                        "price": math.nan,
                        "orig_price": math.nan,
                        "currency": "USD",
                        "in_stock": False,
                        "sizes_total": 0,
                        "sizes_available": 0,
                        "url": href,
                        "last_seen": now_iso(),
                        "note": "parse_failed",
                    }

            page_idx += 1

        ctx.close()
        browser.close()

    return result


# --------------------------
# Diff & Notification
# --------------------------

def compute_diff(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, List[Tuple[str, Dict[str, Any], Dict[str, Any]]]]:
    diffs = {"new": [], "price_change": [], "stock_increase": []}
    old_keys = set(old.keys())
    new_keys = set(new.keys())

    for k in sorted(new_keys - old_keys):
        diffs["new"].append((k, None, new[k]))

    for k in sorted(new_keys & old_keys):
        o, n = old[k], new[k]
        op, np = o.get("price"), n.get("price"）
        if (isinstance(op, (int, float)) and isinstance(np, (int, float))
                and not math.isnan(op) and not math.isnan(np) and abs(op - np) >= 0.01):
            diffs["price_change"].append((k, o, n))
        oa = o.get("sizes_available", 0) or 0
        na = n.get("sizes_available", 0) or 0
        if na > oa:
            diffs["stock_increase"].append((k, o, n))
    return diffs


def format_discord_message(diffs: Dict[str, List[Tuple[str, Dict[str, Any], Dict[str, Any]]]]) -> Dict[str, Any]:
    def line_for_new(item):
        _, _, n = item
        p = n.get("price")
        price_str = f"${p:.2f}" if isinstance(p, (int, float)) and not math.isnan(p) else "N/A"
        return f"🆕 上新 | {n.get('title')} | {price_str}\n{n.get('url')}"

    def line_for_price(item):
        _, o, n = item
        op, np = o.get("price"), n.get("price")
        if all(isinstance(x, (int, float)) and not math.isnan(x) for x in [op, np]):
            arrow = "⬇️" if np < op else "⬆️"
            return f"💲 价格变化 | {n.get('title')} | {arrow} {op:.2f} → {np:.2f}\n{n.get('url')}"
        return f"💲 价格变化 | {n.get('title')}\n{n.get('url')}"

    def line_for_stock(item):
        _, o, n = item
        oa, na = o.get("sizes_available", 0), n.get("sizes_available", 0)
        return f"📦 库存增加 | {n.get('title')} | 可售尺码 {oa} → {na}\n{n.get('url')}"

    sections = []
    if diffs["new"]:
        sections.append("**上新**\n" + "\n\n".join(line_for_new(x) for x in diffs["new"][:15]))
    if diffs["price_change"]:
        sections.append("**价格变化**\n" + "\n\n".join(line_for_price(x) for x in diffs["price_change"][:15]))
    if diffs["stock_increase"]:
        sections.append("**库存增加**\n" + "\n\n".join(line_for_stock(x) for x in diffs["stock_increase"][:15]))

    content = "\n\n".join(sections) if sections else "本次扫描未发现变化。"
    return {
        "content": None,
        "embeds": [{
            "title": "Al's | Arc'teryx 监控结果",
            "description": content,
            "timestamp": datetime.utcnow().isoformat(),
            "color": 0x00AAFF,
            "footer": {"text": "als.com 价格/上新/库存监控"},
        }]
    }


def send_discord(payload: dict) -> None:
    """
    Send Discord webhook notification with safe headers and retries.
    (Final fixed version — no Origin/Referer)
    """
    import urllib.request, urllib.error

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print("WARN: DISCORD_WEBHOOK_URL 未配置，跳过通知。")
        return

    webhook = webhook.replace("discordapp.com", "discord.com")
    if "?" not in webhook:
        webhook += "?wait=true"

    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0 Safari/537.36"
        ),
    }

    for attempt in range(4):
        req = urllib.request.Request(webhook, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8", "ignore")
                print(f"Discord sent OK: {resp.status} {body[:200]}")
                return
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            print(f"Discord HTTPError: {e.code} {body[:300]}")
            if e.code in (429, 403, 502, 503) and attempt < 3:
                wait = max(2 ** attempt, float(e.headers.get("Retry-After", "0") or 0))
                print(f"等待 {wait} 秒后重试...")
                time.sleep(wait)
                continue
            print("放弃重试。")
            return
        except Exception as ex:
            print(f"Discord error: {repr(ex)}")
            if attempt < 3:
                wait = 2 ** attempt
                print(f"等待 {wait} 秒后重试...")
                time.sleep(wait)
                continue
            return


def main():
    print(f"CWD={os.getcwd()}  SNAPSHOT_PATH={SNAPSHOT_PATH.resolve()}")
    headless = os.environ.get("HEADLESS", "1") != "0"

    old = jload(SNAPSHOT_PATH)
    print(f"Loaded {len(old)} items from snapshot.")

    new = scrape_all_products(headless=headless)
    print(f"Scraped {len(new)} items from website.")

    diffs = compute_diff(old, new)
    total_changes = sum(len(v) for v in diffs.values())
    print(f"Found changes: {total_changes} "
          f"(new={len(diffs['new'])}, price={len(diffs['price_change'])}, stock={len(diffs['stock_increase'])})")

    jdump(new, SNAPSHOT_PATH)

    if total_changes > 0 or os.environ.get("ALWAYS_NOTIFY", "0") == "1":
        payload = format_discord_message(diffs)
        send_discord(payload)
    else:
        print("No diff; not notifying.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
