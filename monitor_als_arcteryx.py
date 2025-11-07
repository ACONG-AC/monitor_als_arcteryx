#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ALS.com Arc'teryx 监控
- 上新（新商品/新变体）
- 价格变化
- 仅提醒“缺货→到货”
- 库存数量增加（按尺码对比数量；若无精确数量则用0/1近似）

通知格式：
• 名称：{title}
• 货号：{sku}
• 颜色：{color}
• 价格：{currency}{price}
🧾 库存信息：{size1:qty1, size2:qty2, ...}
{url}

Env:
  DISCORD_WEBHOOK_URL   必填：Discord Webhook
  ALWAYS_NOTIFY=1       可选：即使无变化也发一条（连通性测试）
  HEADLESS=0            可选：本地调试设为0，CI默认1
  KEYWORD_FILTER        可选：仅监控包含该关键词的标题
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


def norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def normalize_key(title: str, sku: str, color: str, url: str) -> str:
    """优先用 sku+color，其次 title+color，最后回退 url 段"""
    if sku and color:
        return f"{sku.lower()}::{color.lower()}"
    if title and color:
        return f"{title.lower()}::{color.lower()}"
    m = re.search(r"/([^/]+)/p(?:$|\?)", url)
    slug = m.group(1).lower() if m else re.sub(r"[^a-z0-9]+", "-", (title or url).lower())
    return f"{slug}::{color.lower() if color else 'na'}"


def money_from_text(txt: str):
    """
    抽取货币符号与金额，例如 '$ 360.00' 或 'CA$ 360'。
    返回 (currency_symbol, price_float)；若失败 price=nan, symbol=''
    """
    if not txt:
        return "", math.nan
    # 常见：'$360.00' 'CA$ 360' 'US$ 200'
    m = re.search(r"([A-Z]{2}\$|\$|C\$|CA\$|US\$|€|£|¥)\s*([0-9]+(?:\.[0-9]{2})?)", txt.replace(",", ""))
    if m:
        return m.group(1), float(m.group(2))
    # 退路：只找金额
    m = re.search(r"([0-9]+(?:\.[0-9]{2})?)", txt.replace(",", ""))
    if m:
        return "", float(m.group(1))
    return "", math.nan


# --------------------------
# Scraper
# --------------------------

def extract_collection_links(page) -> List[str]:
    """收集集合页上的 PDP 链接"""
    anchors = page.locator("a[href*='/arcteryx-'][href*='/p']")
    hrefs = anchors.evaluate_all("els => els.map(e => e.href)")
    uniq = []
    for h in hrefs:
        if "als.com" in h:
            h = h.split("#")[0]
            if h not in uniq:
                uniq.append(h)
    return uniq


def extract_sku(page) -> str:
    """
    解析货号（SKU）。常见位置：
    - 明文 'SKU:'、'Style #'、'Model #'
    - meta/ld+json 中的 'sku'
    - 以 X0000... 形式
    """
    # 1) DOM 文本
    try:
        txt = page.locator("body").inner_text()
        # 优先 X000... 样式
        m = re.search(r"(X\d{9,12})", txt)
        if m:
            return m.group(1).strip()
        # 通用 SKU/Style/Model
        m = re.search(r"(?:SKU|Style|Model)\s*[:#]\s*([A-Za-z0-9\-]+)", txt, re.I)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    # 2) 元数据
    try:
        metas = page.locator("script[type='application/ld+json']")
        for i in range(metas.count()):
            raw = metas.nth(i).inner_text()
            for obj in json.loads(raw if raw.strip().startswith("{") else "{}"),:
                if isinstance(obj, dict):
                    sku = obj.get("sku") or ""
                    if sku:
                        return str(sku).strip()
    except Exception:
        pass
    return ""


def extract_color(page) -> str:
    """
    解析当前选中颜色。常见：
    - 'Color: Trail Magic'
    - 颜色选择器的 aria-pressed / selected 文本
    """
    # 1) Label 形式
    try:
        # 查带 "Color" 的文本
        matches = page.locator("text=/Color\\s*:/i")
        if matches.count():
            # 取包含冒号的这一行
            line = matches.first.evaluate("el => el.parentElement ? el.parentElement.innerText : el.innerText")
            if line:
                m = re.search(r"Color\s*:\s*(.+)", line, re.I)
                if m:
                    return norm_spaces(m.group(1))
    except Exception:
        pass
    # 2) 颜色按钮（选中项）
    try:
        selected = page.locator("[aria-pressed='true'], [aria-selected='true']")
        for i in range(min(selected.count(), 10)):
            t = norm_spaces(selected.nth(i).inner_text())
            if t and len(t) <= 40 and not re.search(r"(Add to cart|Add to bag)", t, re.I):
                return t
    except Exception:
        pass
    # 3) 标题中带颜色
    try:
        title = page.locator("h1").first.inner_text() if page.locator("h1").count() else ""
        # 经验：颜色有时在标题末尾括号里
        m = re.search(r"\(([^()]+)\)$", title)
        if m:
            return norm_spaces(m.group(1))
    except Exception:
        pass
    return ""


def extract_price(page) -> Tuple[str, float]:
    """解析货币与价格"""
    # 尝试多个选择器
    candidates = [
        "[class*='price']",
        "[data-test*='price']",
        "div:has-text('$')",
        "div:has-text('US$'), div:has-text('CA$'), div:has-text('C$'), div:has-text('¥'), div:has-text('€'), div:has-text('£')",
        "body",
    ]
    for sel in candidates:
        try:
            if page.locator(sel).count():
                txt = page.locator(sel).first.inner_text()
                cur, pr = money_from_text(txt)
                if not math.isnan(pr):
                    return cur, pr
        except Exception:
            continue
    return "", math.nan


def extract_sizes_with_qty(page) -> Dict[str, int]:
    """
    返回 dict: {size_text: qty_int}
    解析顺序：
    1) 带数量的数据属性：data-available-qty / data-inventory / data-qty / data-stock
    2) 内嵌 JSON（variants / options）
    3) 回退：按钮可点=1，不可点=0
    """
    sizes: Dict[str, int] = {}

    # 1) 按钮/选项带数据属性
    try:
        btns = page.locator("button, [role='option'], [data-size]")
        for i in range(min(200, btns.count())):
            el = btns.nth(i)
            label = norm_spaces(el.inner_text())
            if not label or len(label) > 10:  # 过滤非尺码
                continue
            if not re.fullmatch(r"(XXS|XS|S|M|L|XL|XXL|XXXL|[\d]{1,2})", label, re.I):
                continue
            qty_attr = None
            for attr in ("data-available-qty", "data-inventory", "data-qty", "data-stock", "data-quantity"):
                v = el.get_attribute(attr)
                if v and re.fullmatch(r"\d+", v.strip()):
                    qty_attr = int(v.strip())
                    break
            if qty_attr is not None:
                sizes[label.upper()] = max(0, qty_attr)
    except Exception:
        pass

    # 2) 内嵌 JSON（有时页面会有 variants 列表）
    if not sizes:
        try:
            scripts = page.locator("script")
            for i in range(min(20, scripts.count())):
                raw = scripts.nth(i).inner_text()
                if not raw or ("variant" not in raw.lower() and "inventory" not in raw.lower()):
                    continue
                # 粗暴找出类似 ... "size":"XL","inventory_quantity":3 ...
                for m in re.finditer(r'"size"\s*:\s*"(?P<size>[^"]+?)"[^}]*?"inventory[^"]*?"\s*:\s*(?P<qty>-?\d+)', raw, re.I | re.S):
                    size = m.group("size").strip().upper()
                    qty = int(m.group("qty"))
                    sizes[size] = max(0, qty)
        except Exception:
            pass

    # 3) 回退：可点=1，不可点=0（保证能做“缺货→到货/数量增加”的判断）
    if not sizes:
        try:
            candidates = page.locator(
                "button:has-text('XXS'), button:has-text('XS'), button:has-text('S'), "
                "button:has-text('M'), button:has-text('L'), button:has-text('XL'), "
                "button:has-text('XXL'), button:has-text('XXXL')"
            )
            for i in range(candidates.count()):
                el = candidates.nth(i)
                label = norm_spaces(el.inner_text()).upper()
                if not label:
                    continue
                disabled = el.get_attribute("disabled")
                aria = el.get_attribute("aria-disabled")
                cls = (el.get_attribute("class") or "")
                sizes[label] = 0 if (disabled is not None or aria in ("true", "disabled") or "disabled" in cls) else 1
        except Exception:
            pass

    return sizes


def parse_product_detail(page) -> Dict[str, Any]:
    """解析 PDP 所需字段"""
    data = {
        "title": "",
        "sku": "",
        "color": "",
        "currency": "",
        "price": math.nan,
        "sizes": {},       # {size: qty_int}
        "in_stock": False, # 是否整体可买（任一尺码 qty>0 即 True）
    }

    # 标题
    try:
        if page.locator("h1").count():
            data["title"] = norm_spaces(page.locator("h1").first.inner_text())
        elif page.locator("title").count():
            data["title"] = norm_spaces(page.locator("title").first.inner_text())
    except Exception:
        pass

    # 货号
    try:
        data["sku"] = extract_sku(page)
    except Exception:
        pass

    # 颜色
    try:
        data["color"] = extract_color(page)
    except Exception:
        pass

    # 价格
    try:
        cur, pr = extract_price(page)
        data["currency"] = cur
        data["price"] = pr
    except Exception:
        pass

    # 尺码与数量
    try:
        sizes = extract_sizes_with_qty(page)
        data["sizes"] = sizes
        data["in_stock"] = any(qty > 0 for qty in sizes.values()) if sizes else False
    except Exception:
        pass

    return data


def scrape_all_products(headless=True, timeout_ms=15000) -> Dict[str, Any]:
    """遍历集合页 → 逐个 PDP 解析 → 返回以 variant key 为键的 dict"""
    result: Dict[str, Any] = {}
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
                safe_sleep(0.4, 1.0)

                ok = False
                for attempt in range(3):
                    try:
                        page.goto(href, timeout=timeout_ms)
                        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
                        safe_sleep(0.2, 0.6)
                        pdata = parse_product_detail(page)
                        title = pdata.get("title", "")
                        color = pdata.get("color", "")
                        sku = pdata.get("sku", "")
                        if keyword and keyword not in (title or "").lower():
                            ok = True
                            break
                        if title:
                            key = normalize_key(title, sku, color, href)
                            pdata.update({"url": href, "last_seen": now_iso(), "key": key})
                            result[key] = pdata
                            ok = True
                            break
                    except Exception as e:
                        print(f"[detail] error {href}: {e}")
                        safe_sleep(0.7, 1.5)
                if not ok:
                    # 记录最少信息以免丢失
                    key = normalize_key("", "", "", href)
                    result[key] = {
                        "title": "",
                        "sku": "",
                        "color": "",
                        "currency": "",
                        "price": math.nan,
                        "sizes": {},
                        "in_stock": False,
                        "url": href,
                        "last_seen": now_iso(),
                        "key": key,
                        "note": "parse_failed",
                    }
            page_idx += 1

        ctx.close()
        browser.close()

    return result


# --------------------------
# Diff & Notification
# --------------------------

def compute_diff(old: Dict[str, Any], new: Dict[str, Any]):
    """
    返回：
      new_items:        新商品/新变体
      price_changes:    价格变化
      restocks:         缺货→到货（old.in_stock=False & new.in_stock=True）
      stock_increases:  库存数量增加（按尺码对比；若解析不到数量，用0/1）
                         元素结构：[(key, old, new, increased_sizes_dict)]
    """
    new_items = []
    price_changes = []
    restocks = []
    stock_increases = []

    old_keys = set(old.keys())
    new_keys = set(new.keys())

    # 上新（含新变体）
    for k in sorted(new_keys - old_keys):
        new_items.append((k, None, new[k]))

    # 交集对比
    for k in sorted(new_keys & old_keys):
        o = old[k] or {}
        n = new[k] or {}

        # 价格变化
        op, np = o.get("price"), n.get("price")
        if (isinstance(op, (int, float)) and isinstance(np, (int, float))
                and not math.isnan(op) and not math.isnan(np) and abs(op - np) >= 0.01):
            price_changes.append((k, o, n))

        # 缺货→到货（仅提醒这一方向）
        if (not o.get("in_stock", False)) and n.get("in_stock", False):
            restocks.append((k, o, n))

        # 库存数量增加（逐尺码）
        increased: Dict[str, int] = {}
        osizes: Dict[str, int] = o.get("sizes") or {}
        nsizes: Dict[str, int] = n.get("sizes") or {}
        for size, nqty in nsizes.items():
            oqty = osizes.get(size, 0)
            try:
                if int(nqty) > int(oqty):
                    increased[size] = int(nqty)
            except Exception:
                # 非法值按0/1逻辑
                if (nqty and not oqty):
                    increased[size] = 1
        if increased:
            stock_increases.append((k, o, n, increased))

    return {
        "new_items": new_items,
        "price_changes": price_changes,
        "restocks": restocks,
        "stock_increases": stock_increases,
    }


def _fmt_currency_price(currency: str, price: float) -> str:
    if isinstance(price, (int, float)) and not math.isnan(price):
        cur = (currency or "").strip()
        # 统一去掉多余空格：'CA$ ' → 'CA$ '
        return f"{cur} {price:.2f}".strip()
    return "N/A"


def _fmt_sizes_line(sizes: Dict[str, int], only_keys: List[str] = None, limit: int = 8) -> str:
    items = []
    if only_keys:
        for k in only_keys:
            if k in sizes:
                items.append(f"{k}:{sizes[k]}")
    else:
        # 仅展示有库存（>0）的尺码，最多 limit 个
        for k, v in sizes.items():
            if v and v > 0:
                items.append(f"{k}:{v}")
                if len(items) >= limit:
                    break
    return "，".join(items) if items else "无"


def format_discord_message(diffs) -> Dict[str, Any]:
    """按指定格式组织为 Discord 嵌入消息"""
    lines: List[str] = []

    def block(n: Dict[str, Any], title: str):
        nm = n.get("title") or "-"
        sku = n.get("sku") or "-"
        color = n.get("color") or "-"
        price = _fmt_currency_price(n.get("currency", ""), n.get("price"))
        sizes = n.get("sizes") or {}
        # 按你的示例格式输出
        lines.append(f"• 名称：{nm}")
        lines.append(f"• 货号：{sku}")
        lines.append(f"• 颜色：{color}")
        lines.append(f"• 价格：{price}")
        lines.append(f"🧾 库存信息：{_fmt_sizes_line(sizes)}")
        lines.append(f"{n.get('url')}")
        lines.append("")  # 空行分隔

    # 上新
    if diffs["new_items"]:
        lines.append("**上新（新商品/新变体）**")
        for k, _, n in diffs["new_items"][:20]:
            block(n, "上新")

    # 价格变化
    if diffs["price_changes"]:
        lines.append("**价格变化**")
        for k, o, n in diffs["price_changes"][:20]:
            block(n, "价格变化")

    # 缺货→到货
    if diffs["restocks"]:
        lines.append("**缺货 → 到货**")
        for k, o, n in diffs["restocks"][:20]:
            block(n, "到货")

    # 库存数量增加（仅展示增加的尺码）
    if diffs["stock_increases"]:
        lines.append("**库存数量增加**")
        for k, o, n, inc in diffs["stock_increases"][:20]:
            nm = n.get("title") or "-"
            sku = n.get("sku") or "-"
            color = n.get("color") or "-"
            price = _fmt_currency_price(n.get("currency", ""), n.get("price"))
            sizes = n.get("sizes") or {}
            inc_keys = list(inc.keys())
            lines.append(f"• 名称：{nm}")
            lines.append(f"• 货号：{sku}")
            lines.append(f"• 颜色：{color}")
            lines.append(f"• 价格：{price}")
            lines.append(f"🧾 库存信息：{_fmt_sizes_line(sizes, only_keys=inc_keys)}")
            lines.append(f"{n.get('url')}")
            lines.append("")

    content = "\n".join(lines) if lines else "本次扫描未发现变化。"

    payload = {
        "content": None,
        "embeds": [{
            "title": "Al's | Arc'teryx 监控结果",
            "description": content[:4000],  # 保险起见限制描述长度
            "timestamp": datetime.utcnow().isoformat(),
            "color": 0x00AAFF,
            "footer": {"text": "als.com 价格/上新/库存监控"},
        }]
    }
    return payload


def send_discord(payload: dict) -> None:
    """
    Discord Webhook 通知：仅必要请求头 + 重试
    （去掉 Origin/Referer，避免 50067 Invalid request origin）
    """
    import urllib.request
    import urllib.error

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print("WARN: DISCORD_WEBHOOK_URL 未配置，跳过通知。")
        return

    webhook = webhook.replace("discordapp.com", "discord.com")
    if "?" not in webhook:
        webhook = webhook + "?wait=true"

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


# --------------------------
# Main
# --------------------------

def main():
    print(f"CWD={os.getcwd()}  SNAPSHOT_PATH={SNAPSHOT_PATH.resolve()}")
    headless = os.environ.get("HEADLESS", "1") != "0"

    old = jload(SNAPSHOT_PATH)
    print(f"Loaded {len(old)} items from snapshot.")

    new = scrape_all_products(headless=headless)
    print(f"Scraped {len(new)} items from website.")

    diffs = compute_diff(old, new)
    print("Diff summary:",
          f"new={len(diffs['new_items'])},",
          f"price={len(diffs['price_changes'])},",
          f"restock={len(diffs['restocks'])},",
          f"stock_inc={len(diffs['stock_increases'])}")

    jdump(new, SNAPSHOT_PATH)

    if (sum(len(v) for v in diffs.values()) > 0) or os.environ.get("ALWAYS_NOTIFY", "0") == "1":
        payload = format_discord_message(diffs)
        send_discord(payload)
    else:
        print("No diff; not notifying.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
