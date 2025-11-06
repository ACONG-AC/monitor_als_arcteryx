#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Monitor Arc'teryx products on als.com:
- Track price changes, new arrivals, and stock increases
- Send Discord notifications
- Persist snapshot.json and auto-commit in CI

Author: Rolland Yip helper
"""

import json
import os
import re
import sys
import time
import math
import random
from datetime import datetime, timezone
from pathlib import Path
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
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def jload(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def money_to_float(txt: str) -> float:
    """Extract the first money-like value like $799.99 from text to float."""
    m = re.search(r"\$?\s*([0-9]{1,4}(?:[,][0-9]{3})*(?:\.[0-9]{2})?)", txt.replace(",", ""))
    return float(m.group(1)) if m else math.nan


def safe_sleep(a=0.3, b=0.9):
    time.sleep(random.uniform(a, b))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_key(url: str, title: str) -> str:
    """
    Try making a stable key per product to match across runs.
    Prefer product path last segment if present, else title slug.
    """
    # product pages like: https://www.als.com/arcteryx-beta-jacket-mens-10575692/p
    m = re.search(r"/([^/]+)/p(?:$|\?)", url)
    if m:
        return m.group(1).lower()
    # fallback to title slug
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:80]


# --------------------------
# Scraper
# --------------------------

def extract_collection_links(page) -> List[str]:
    """
    On a collection page, collect links to product detail pages.
    We broadly match '/arcteryx-*-*/p' patterns to be robust.
    """
    anchors = page.locator("a[href*='/arcteryx-'][href*='/p']")
    hrefs = anchors.evaluate_all("els => els.map(e => e.href)")
    # De-dup & keep only als.com domain
    uniq = []
    for h in hrefs:
        if "als.com" in h and h not in uniq:
            uniq.append(h.split("#")[0])
    return uniq


def detect_has_next(page) -> bool:
    # Try to find pagination 'Next' button/link; fallback: try probing next page until 404
    # On ALS it often works with ?page=2,3...
    # We'll not rely on DOM 'Next', the caller will paginate numerically.
    return True


def get_text_or_empty(el):
    try:
        return el.inner_text().strip()
    except PWTimeout:
        return ""
    except Exception:
        return ""


def parse_product_detail(page) -> Dict[str, Any]:
    """
    Extract title, price (current + original if any), simple stock signal, size availability count.
    Heuristics-based selectors to increase robustness.
    """
    data = {
        "title": "",
        "price": math.nan,
        "orig_price": math.nan,
        "currency": "USD",
        "in_stock": False,
        "sizes_total": 0,
        "sizes_available": 0,
    }

    # Title: try <h1>, fallback to <title>
    try:
        if page.locator("h1").count():
            data["title"] = page.locator("h1").first.inner_text().strip()
        elif page.locator("title").count():
            data["title"] = page.locator("title").first.inner_text().strip()
    except Exception:
        pass

    # Price area: grab first $ number on page main content
    try:
        # Some sites render price in a specific container; try a few common selectors
        candidates = [
            "[class*='price']",
            "[data-test*='price']",
            "div:has-text('$')",
            "body",
        ]
        price_text = ""
        for sel in candidates:
            if page.locator(sel).count():
                txt = page.locator(sel).first.inner_text()
                if "$" in txt:
                    price_text = txt
                    break
        if price_text:
            # If both sale & original appear, usually the first is current (sale) and later is crossed-out
            prices = re.findall(r"\$\s*[0-9]+(?:\.[0-9]{2})?", price_text.replace(",", ""))
            if prices:
                data["price"] = float(prices[0].replace("$", "").strip())
                if len(prices) >= 2:
                    data["orig_price"] = float(prices[1].replace("$", "").strip())
    except Exception:
        pass

    # Stock: heuristic — if "Add to bag" / "Add to cart" clickable, treat as in stock
    try:
        # Try a few possible CTA texts
        ctas = ["Add to bag", "Add to cart", "Add To Bag", "Add To Cart"]
        in_stock = False
        for t in ctas:
            btn = page.get_by_role("button", name=re.compile(t, re.I))
            if btn.count():
                # if disabled attribute or has 'disabled' in class?
                try:
                    disabled = btn.first.get_attribute("disabled")
                    if disabled is None:
                        in_stock = True
                        break
                except Exception:
                    in_stock = True
                    break
        # Also consider explicit "Out of Stock"
        if re.search(r"out of stock", page.content(), re.I):
            in_stock = False
        data["in_stock"] = in_stock
    except Exception:
        pass

    # Sizes: count available size options (buttons/selects not disabled)
    sizes_total = 0
    sizes_available = 0
    try:
        # Common patterns: buttons with size text, or option elements
        # Buttons
        size_buttons = page.locator("button:has-text('XS'), button:has-text('S'), button:has-text('M'), button:has-text('L'), button:has-text('XL'), button:has-text('XXL')")
        sizes_total += size_buttons.count()
        for i in range(size_buttons.count()):
            el = size_buttons.nth(i)
            disabled = el.get_attribute("disabled")
            aria = el.get_attribute("aria-disabled")
            cls = (el.get_attribute("class") or "")
            if not disabled and aria not in ("true", "disabled") and "disabled" not in cls:
                sizes_available += 1

        # Select dropdown
        if page.locator("select").count():
            opts = page.locator("select option")
            sizes_total += opts.count()
            for i in range(opts.count()):
                opt = opts.nth(i)
                valtxt = (opt.inner_text() or "").strip()
                if not valtxt or valtxt.lower() in ("select", "choose"):
                    continue
                disabled = opt.get_attribute("disabled")
                if not disabled:
                    sizes_available += 1
    except Exception:
        pass

    data["sizes_total"] = sizes_total
    data["sizes_available"] = sizes_available

    return data


def scrape_all_products(headless=True, timeout_ms=15000) -> Dict[str, Any]:
    """
    Crawl collection pagination and each product detail.
    Return dict keyed by stable product key.
    """
    result = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ctx = browser.new_context(user_agent=USER_AGENT, locale="en-US")
        page = ctx.new_page()

        # Paginate ?page=1..N until a page yields no product links twice
        page_idx = 1
        empty_hits = 0
        seen_urls = set()

        while True:
            url = COLLECTION_URL if page_idx == 1 else f"{COLLECTION_URL}?page={page_idx}"
            try:
                page.goto(url, timeout=timeout_ms)
                page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            except PWTimeout:
                empty_hits += 1
                if empty_hits >= 2:
                    break
                page_idx += 1
                continue

            links = extract_collection_links(page)
            # Stop condition: two consecutive empty pages
            if not links:
                empty_hits += 1
                if empty_hits >= 2:
                    break
                page_idx += 1
                continue

            empty_hits = 0
            # Dedup
            links = [u for u in links if u not in seen_urls]
            for href in links:
                seen_urls.add(href)

            # Crawl product details
            for href in links:
                safe_sleep(0.4, 1.0)
                # Retry per PDP
                detail_ok = False
                for attempt in range(3):
                    try:
                        page.goto(href, timeout=timeout_ms)
                        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                        # Small wait for price DOM to settle
                        safe_sleep(0.2, 0.6)
                        pdata = parse_product_detail(page)
                        if pdata["title"]:
                            key = normalize_key(href, pdata["title"])
                            pdata["url"] = href
                            pdata["last_seen"] = now_iso()
                            result[key] = pdata
                            detail_ok = True
                            break
                    except PWTimeout:
                        safe_sleep(0.6, 1.2)
                    except Exception:
                        safe_sleep(0.6, 1.2)
                if not detail_ok:
                    # record minimal info to not loop forever
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
    """
    Returns dict with keys: 'new', 'price_change', 'stock_increase'
    Each item is a tuple (product_key, old_data_or_None, new_data)
    """
    diffs = {"new": [], "price_change": [], "stock_increase": []}

    old_keys = set(old.keys())
    new_keys = set(new.keys())

    # New arrivals
    for k in sorted(new_keys - old_keys):
        diffs["new"].append((k, None, new[k]))

    # Price changes, stock increase
    for k in sorted(new_keys & old_keys):
        o = old[k]
        n = new[k]
        # Price change
        op = o.get("price")
        np = n.get("price")
        if (isinstance(op, (int, float)) and isinstance(np, (int, float)) and
                not math.isnan(op) and not math.isnan(np) and abs(op - np) >= 0.01):
            diffs["price_change"].append((k, o, n))
        # Stock increase — compare available sizes count
        oa = o.get("sizes_available", 0) or 0
        na = n.get("sizes_available", 0) or 0
        if na > oa:
            diffs["stock_increase"].append((k, o, n))

    return diffs


def format_discord_message(diffs: Dict[str, List[Tuple[str, Dict[str, Any], Dict[str, Any]]]]) -> Dict[str, Any]:
    """
    Build a Discord webhook payload.
    """
    def line_for_new(item):
        k, _, n = item
        p = n.get("price")
        price_str = f"${p:.2f}" if isinstance(p, (int, float)) and not math.isnan(p) else "N/A"
        return f"🆕 上新 | {n.get('title') or k} | {price_str}\n{n.get('url')}"

    def line_for_price(item):
        k, o, n = item
        op = o.get("price")
        np = n.get("price")
        delta = ""
        if all(isinstance(x, (int, float)) and not math.isnan(x) for x in [op, np]):
            arrow = "⬇️" if np < op else "⬆️"
            delta = f"{arrow} {op:.2f} → {np:.2f}"
        return f"💲 价格变化 | {n.get('title') or k} | {delta}\n{n.get('url')}"

    def line_for_stock(item):
        k, o, n = item
        oa = o.get("sizes_available", 0) or 0
        na = n.get("sizes_available", 0) or 0
        return f"📦 库存增加 | {n.get('title') or k} | 可售尺码 {oa} → {na}\n{n.get('url')}"

    sections = []
    if diffs["new"]:
        sections.append("**上新**\n" + "\n\n".join(line_for_new(x) for x in diffs["new"][:15]))
    if diffs["price_change"]:
        sections.append("**价格变化**\n" + "\n\n".join(line_for_price(x) for x in diffs["price_change"][:15]))
    if diffs["stock_increase"]:
        sections.append("**库存增加**\n" + "\n\n".join(line_for_stock(x) for x in diffs["stock_increase"][:15]))

    content = "\n\n".join(sections)
    if not content:
        content = "本次扫描未发现变化。"

    payload = {
        "content": None,
        "embeds": [{
            "title": "Al's | Arc'teryx 监控结果",
            "description": content,
            "timestamp": datetime.utcnow().isoformat(),
            "color": 0x00AAFF,
            "footer": {"text": "als.com 价格/上新/库存监控"},
        }]
    }
    return payload


def send_discord(payload: Dict[str, Any]) -> None:
    import urllib.request
    import urllib.error

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print("WARN: DISCORD_WEBHOOK_URL 未配置，跳过通知。")
        return
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print("Discord sent:", resp.status)
    except urllib.error.HTTPError as e:
        print("Discord HTTPError:", e.code, e.read())
    except Exception as e:
        print("Discord error:", e)


# --------------------------
# Main
# --------------------------

def main():
    headless = os.environ.get("HEADLESS", "1") != "0"
    old = jload(SNAPSHOT_PATH)
    print(f"Loaded {len(old)} items from snapshot.")

    new = scrape_all_products(headless=headless)
    print(f"Scraped {len(new)} items from website.")

    diffs = compute_diff(old, new)
    total_changes = sum(len(v) for v in diffs.values())
    print(f"Found changes: {total_changes} "
          f"(new={len(diffs['new'])}, price={len(diffs['price_change'])}, stock={len(diffs['stock_increase'])})")

    # Write snapshot eagerly
    jdump(new, SNAPSHOT_PATH)

    # Only notify when there is at least one change
    if total_changes > 0 or os.environ.get("ALWAYS_NOTIFY", "0") == "1":
        payload = format_discord_message(diffs)
        send_discord(payload)
    else:
        print("No diff; not notifying.")

    # Exit code for CI visibility (0 always OK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
