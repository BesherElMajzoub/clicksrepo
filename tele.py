#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram bot to query daily clicks for multiple sources and treat them as one.

Commands:
  /start
  /list               -> list all sites (name | domain | type | total)
  /klik site [date]   -> get clicks for a site on a date (YYYY-MM-DD). Date optional; defaults to today (Europe/Berlin).

Requirements:
  pip install "python-telegram-bot>=21.6" requests beautifulsoup4
"""

import re
import json
import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# -------- Sources configuration --------
SOURCES = [
    {"kind": "master", "url": "https://khadimat.com/administrator/api.php"},
    {"kind": "master", "url": "https://sdadi-qarde.com/administrator/api.php"},
    # Single clicks page treated as its own site
    {
        "kind": "clicks",
        "url": "https://tasdedqard.com/view_clicks.php",
        "name": "tasdedqard.com",
        "domain": "tasdedqard.com",
        "type": "single",
    },
]

DEFAULT_TZ = "Europe/Berlin"


def fetch_html(url: str, timeout: int = 20) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    try:
        if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
            r.encoding = r.apparent_encoding or "utf-8"
    except Exception:
        r.encoding = r.apparent_encoding or "utf-8"
    return r.text


def parse_master_sites(html: str, source_url: str) -> List[Dict]:
    """Parse a master table page with id=data-table and return site dicts."""
    soup = BeautifulSoup(html, "html.parser", from_encoding="utf-8")
    table = soup.find(id="data-table")
    sites = []
    if not table:
        return sites

    rows = table.find_all("tr")
    for tr in rows[1:]:
        tds = tr.find_all("td")
        if len(tds) < 9:
            continue

        name = tds[0].get_text(strip=True)
        domain = tds[1].get_text(strip=True)
        site_type = tds[2].get_text(strip=True)
        total_clicks = tds[3].get_text(strip=True)

        def _btn_href(td):
            a = td.find("a")
            return a["href"] if a and a.has_attr("href") else ""

        clear_url = ""
        btn = tds[4].find("button")
        if btn and btn.has_attr("onclick"):
            m = re.search(r"reloadThePage\('([^']+)'\)", btn["onclick"])
            if m:
                clear_url = m.group(1)

        view_clicks_url = _btn_href(tds[5])
        visits_url = _btn_href(tds[6])
        combined_url = _btn_href(tds[7])
        chart_url = _btn_href(tds[8])

        if domain and view_clicks_url:
            # extract int from total_clicks
            try:
                total = int(re.sub(r"[^\d]", "", total_clicks) or 0)
            except Exception:
                total = 0
            sites.append({
                "name": name,
                "domain": domain,
                "type": site_type,
                "total_clicks": total,
                "clear_url": clear_url,
                "view_clicks_url": view_clicks_url,
                "visits_url": visits_url,
                "combined_url": combined_url,
                "chart_url": chart_url,
                "_source": source_url,
            })
    return sites


def parse_daily_clicks_page(html: str) -> List[Dict]:
    soup = BeautifulSoup(html, "html.parser", from_encoding="utf-8")
    rows = []
    for tr in soup.select("table tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        btn = tds[0].get_text(strip=True)
        date_text = tds[1].get_text(strip=True)
        m = re.search(r"\d{4}-\d{2}-\d{2}", date_text)
        date_str = m.group(0) if m else date_text
        try:
            dt.datetime.strptime(date_str, "%Y-%m-%d")
        except Exception:
            continue
        count_str = tds[2].get_text(strip=True)
        try:
            count = int(re.sub(r"[^\d]", "", count_str) or 0)
        except Exception:
            count = 0
        rows.append({"button_type": btn, "date": date_str, "count": count})
    return rows


def summarize_for_date(records: List[Dict], target_date: str) -> Dict:
    by_button = {}
    total = 0
    for r in records:
        if r["date"] == target_date:
            by_button[r["button_type"]] = by_button.get(r["button_type"], 0) + r["count"]
            total += r["count"]
    return {"by_button": by_button, "total": total}


def hostname_of(url_or_domain: str) -> str:
    try:
        netloc = urlparse(url_or_domain).netloc or url_or_domain
    except Exception:
        netloc = url_or_domain
    host = netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def pick_site(sites: List[Dict], needle: str) -> Optional[Dict]:
    if not needle:
        return None
    n = needle.strip().lower()
    n_host = hostname_of(n)

    # exact hostname match first
    exact = [s for s in sites if hostname_of(s.get("domain", "")) == n_host]
    if exact:
        return sorted(exact, key=lambda s: len(s.get("domain","")))[0]

    # partial domain contains
    domain_hits = [s for s in sites if n in s.get("domain","").lower()]
    if domain_hits:
        return sorted(domain_hits, key=lambda s: len(s.get("domain","")))[0]

    # name contains
    name_hits = [s for s in sites if n in s.get("name","").lower()]
    if name_hits:
        return name_hits[0]
    return None


def build_single_clicks_site(entry: Dict) -> Dict:
    """Create a 'site' dict for a standalone clicks page (e.g., tasdedqard.com)."""
    name = entry.get("name") or entry.get("domain") or hostname_of(entry["url"])
    domain = entry.get("domain") or hostname_of(entry["url"])
    site = {
        "name": name,
        "domain": domain,
        "type": entry.get("type") or "single",
        "total_clicks": 0,  # may update below
        "clear_url": "",
        "view_clicks_url": entry["url"],
        "visits_url": "",
        "combined_url": "",
        "chart_url": "",
        "_source": entry["url"],
    }
    # Try to compute an overall total for listing (optional)
    try:
        html = fetch_html(entry["url"])
        recs = parse_daily_clicks_page(html)
        site["total_clicks"] = sum(r["count"] for r in recs)
    except Exception:
        # leave total_clicks as 0 if we can't parse
        pass
    return site


def fetch_all_sites() -> List[Dict]:
    """Fetch and merge sites from all configured sources."""
    all_sites: List[Dict] = []
    for src in SOURCES:
        kind = src.get("kind")
        url = src.get("url")
        try:
            if kind == "master":
                html = fetch_html(url)
                sites = parse_master_sites(html, url)
                all_sites.extend(sites)
            elif kind == "clicks":
                site = build_single_clicks_site(src)
                all_sites.append(site)
        except Exception:
            # Skip failing source but continue with others
            continue

    # Deduplicate by (domain, view_clicks_url)
    dedup: Dict[Tuple[str, str], Dict] = {}
    for s in all_sites:
        key = (hostname_of(s.get("domain","")), s.get("view_clicks_url",""))
        if key not in dedup:
            dedup[key] = s
        else:
            # Prefer one with a non-zero total_clicks
            if dedup[key].get("total_clicks", 0) == 0 and s.get("total_clicks", 0) > 0:
                dedup[key] = s
    return list(dedup.values())


# ------------------ Telegram Handlers ------------------

BOT_TOKEN = "8368293478:AAE4duF3MkcQzbmi86UcFJvuBH9TDTKeFd4"  # <<< ضع التوكن هنا


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "أهلًا 👋\n"
        "الأوامر المتاحة:\n"
        "• /list  — عرض جميع المواقع من كل المصادر\n"
        "• /klik <site> [YYYY-MM-DD] — نقرات موقع في تاريخ محدد (بدون التاريخ = اليوم)\n\n"
        "مثال:\n"
        "/klik khadimati.com 2025-09-13"
    )
    await update.message.reply_text(msg)


async def list_sites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        sites = fetch_all_sites()
        if not sites:
            await update.message.reply_text("لا توجد مواقع في المصادر حاليًا.")
            return
        lines = []
        for i, s in enumerate(sites, start=1):
            total = s.get("total_clicks", 0)
            stype = s.get("type") or "-"
            lines.append(
                f"{i:2d}. {s['name']} | {s['domain']} | نوع: {stype} | إجمالي: {total}"
            )
        text = "\n".join(lines)
        # Telegram limits ~4096 chars per message
        if len(text) <= 3900:
            await update.message.reply_text(text)
        else:
            # Split into chunks
            chunk = []
            size = 0
            for line in lines:
                if size + len(line) + 1 > 3900:
                    await update.message.reply_text("\n".join(chunk))
                    chunk = [line]
                    size = len(line) + 1
                else:
                    chunk.append(line)
                    size += len(line) + 1
            if chunk:
                await update.message.reply_text("\n".join(chunk))
    except Exception as e:
        await update.message.reply_text(f"🚨 خطأ أثناء جلب القائمة: {e}")


async def klik(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        if not context.args:
            await update.message.reply_text("استخدم: /klik <site> [YYYY-MM-DD]")
            return
        site_arg = context.args[0]
        if len(context.args) >= 2:
            target_date = context.args[1]
            try:
                dt.datetime.strptime(target_date, "%Y-%m-%d")
            except Exception:
                await update.message.reply_text("صيغة التاريخ يجب أن تكون YYYY-MM-DD")
                return
        else:
            target_date = dt.datetime.now(ZoneInfo(DEFAULT_TZ)).date().strftime("%Y-%m-%d")

        sites = fetch_all_sites()
        site = pick_site(sites, site_arg)
        if not site:
            await update.message.reply_text(f"لم أجد موقعاً يطابق: {site_arg}")
            return

        page_html = fetch_html(site["view_clicks_url"])
        records = parse_daily_clicks_page(page_html)
        summary = summarize_for_date(records, target_date)

        # Build response
        header = (
            f"الموقع: {site['name']} ({site['domain']})\n"
            f"المصدر: {site.get('_source','-')}\n"
            f"التاريخ: {target_date}\n"
            + "-" * 35
        )
        if not summary["by_button"]:
            body = "\nلا توجد نقرات مسجلة لهذا التاريخ."
        else:
            # ثبّت ترتيب العرض (اختياري)
            parts = []
            for btn, cnt in sorted(summary["by_button"].items()):
                parts.append(f"\n{btn}: {cnt}")
            parts.append(f"\n{'-'*15}\nالإجمالي: {summary['total']}")
            body = "".join(parts)

        await update.message.reply_text(header + body)
    except Exception as e:
        await update.message.reply_text(f"🚨 خطأ أثناء الاستعلام: {e}")


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", list_sites))
    app.add_handler(CommandHandler("klik", klik))
    print("Bot is running. Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
