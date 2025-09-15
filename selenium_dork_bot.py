import os
import re
import random
import sqlite3
from typing import Dict, List, Tuple

import requests
from urllib.parse import urlparse, urlunparse
from bs4 import BeautifulSoup
import tldextract

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

# ───────────────────────────────────────────────────────────────────────────────
# Config / Env
# ───────────────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN") or "YOUR_TELEGRAM_BOT_TOKEN"
DB_PATH = os.getenv("YUVRAJ_DB_PATH", "scan_cache.sqlite3")

# ───────────────────────────────────────────────────────────────────────────────
# Proxy settings (single or pool)
# ───────────────────────────────────────────────────────────────────────────────
PROXIES = None                  # single proxy URL string or None
PROXY_POOL: list[str] = []      # managed via /setproxylist
PROXY_MODE = "single"           # single | rr | random
_rr_index = 0
_last_proxy_url: str | None = None  # last proxy actually used

def _proxies_dict_from_url(proxy_url: str | None) -> dict | None:
    if not proxy_url:
        return None
    scheme = urlparse(proxy_url).scheme.lower()
    if scheme in ("http", "https", "socks5", "socks5h"):
        return {"http": proxy_url, "https": proxy_url}
    return None

def _pick_proxy() -> str | None:
    global _rr_index, _last_proxy_url
    if PROXY_POOL:
        if PROXY_MODE == "rr":
            proxy_url = PROXY_POOL[_rr_index % len(PROXY_POOL)]
            _rr_index += 1
        elif PROXY_MODE == "random":
            proxy_url = random.choice(PROXY_POOL)
        else:
            proxy_url = PROXY_POOL[0]
        _last_proxy_url = proxy_url
        return proxy_url
    _last_proxy_url = PROXIES
    return PROXIES

def _proxy_ok(proxy_url: str, test_url: str = "https://example.com", timeout: int = 6):
    try:
        requests.get(test_url, timeout=timeout, proxies=_proxies_dict_from_url(proxy_url))
        return True, None
    except Exception as e:
        return False, str(e)

# ───────────────────────────────────────────────────────────────────────────────
# HTTP + Parsing helpers
# ───────────────────────────────────────────────────────────────────────────────
def safe_get(url: str) -> requests.Response:
    headers = {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    return requests.get(url, headers=headers, timeout=12, allow_redirects=True,
                        proxies=_proxies_dict_from_url(_pick_proxy()))

def join_assets_and_html(resp: requests.Response) -> str:
    if "html" not in (resp.headers.get("Content-Type","" ).lower()):
        return ""
    html = resp.text[:1_200_000]
    soup = BeautifulSoup(html, "html.parser")
    srcs = []
    for tag in soup.find_all(["script","link","img","iframe","form","a","button"]):
        for attr in ("src","href","data-src","action"):
            v = tag.get(attr)
            if isinstance(v, str):
                srcs.append(v)
        if tag.name in {"a","button"} and tag.text:
            srcs.append(tag.text.strip())
    return "\n".join(srcs) + "\n" + html

def any_match(text: str, pats: List[str]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in pats)

def map_hits(text: str, sigs: Dict[str, List[str]]) -> Dict[str, bool]:
    return {name: any_match(text, pats) for name, pats in sigs.items()}

def summarize(d: Dict[str,bool]) -> List[str]:
    return [k for k,v in d.items() if v]

# ───────────────────────────────────────────────────────────────────────────────
# Signatures
# ───────────────────────────────────────────────────────────────────────────────
CAPTCHA_SIGNATURES = {
    "Google reCAPTCHA":[r"www\.google\.com/recaptcha/", r"www\.recaptcha\.net/recaptcha/", r"grecaptcha\.render"],
    "hCaptcha":[r"hcaptcha\.com/1/api\.js", r"new hcaptcha", r"data-sitekey=.*?hcaptcha"],
    "Cloudflare Turnstile":[r"challenges\.cloudflare\.com/turnstile", r"data-sitekey=.*?turnstile"],
}

PAYMENT_SIGNATURES = {
    "Stripe":[r"js\.stripe\.com", r"stripe\.elements", r"stripe\.checkout", r"stripe\.payment"],
    "PayPal":[r"www\.paypal\.com/sdk/js", r"www\.paypalobjects\.com"],
    "Razorpay":[r"checkout\.razorpay\.com"],
    "Braintree":[r"js\.braintreegateway\.com", r"braintreeweb"],
    "Adyen":[r"checkoutshopper(-\\w+)?\.adyen\.com", r"adyencomponent", r"adyen\.encrypt"],
    "Checkout.com":[r"pay\.checkout\.com", r"frames\.js"],
    "Square":[r"js\.squareup\.com", r"js\.squareupsandbox\.com", r"web-payments-sdk"],
    "PayU":[r"secure\.payu\.", r"api\.payu\.", r"payumoney", r"payumin"],
    "CCAvenue":[r"secure\.ccavenue\.com", r"\\bccavenue\\b"],
    "Paystack":[r"js\.paystack\.co"],
    "Flutterwave":[r"checkout\.flutterwave\.com", r"ravepay"],
    "Authorize.Net":[r"authorize\.net", r"acceptjs"],
}

PLATFORM_PATTERNS = {
    "WooCommerce":[r"\\bwoocommerce\\b", r"wp-content/plugins/woocommerce"],
    "WordPress":[r"content=\"WordPress", r"wp-content/", r"wp-includes/"],
    "Magento":[r"\\bMagento\\b", r"/static/frontend/|/skin/frontend/"],
}

# ───────────────────────────────────────────────────────────────────────────────
# UI formatting
# ───────────────────────────────────────────────────────────────────────────────
def tick(v: bool) -> str:
    return "✅" if v else "✖️"

def flame(v: bool) -> str:
    return "🔥" if v else "✖️"

def ui_block(payload: dict) -> str:
    url          = payload["url"]
    gateways     = payload["gateways"]
    captcha      = payload["captcha"]
    graphql      = payload["graphql"]
    add_to_cart  = payload["add_to_cart"]
    my_account   = payload["my_account"]
    platform     = payload["platform"]
    status       = payload["status"]
    error        = payload["error"]
    lines = []
    lines.append("• Gateway Analysis ── Selenium Bot")
    lines.append(f"[★] URL: {url}")
    lines.append(f"[★] Gateways: {', '.join(gateways) if gateways else 'none'}")
    lines.append(f"[★] Captcha: {tick(captcha)}")
    lines.append(f"[★] GraphQL: {flame(graphql)}")
    lines.append(f"[★] Add to Cart: {tick(add_to_cart)}")
    lines.append(f"[★] My Account: {flame(my_account)}")
    lines.append(f"[★] Platform: {platform}")
    lines.append(f"[⌁] Status: {status}")
    lines.append(f"[C] Error: {error or 'None'}")
    return "```\n" + "\n".join(lines) + "\n```"

# ───────────────────────────────────────────────────────────────────────────────
# SQLite cache for /dork seen URLs
# ───────────────────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS dork_seen (
            dork TEXT NOT NULL,
            url  TEXT NOT NULL,
            PRIMARY KEY (dork, url)
        )
    """)
    con.commit()
    con.close()

def get_seen_urls_for_dork(dork: str) -> set[str]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT url FROM dork_seen WHERE dork = ?", (dork,))
    rows = cur.fetchall()
    con.close()
    return {r[0] for r in rows}

def mark_urls_seen(dork: str, urls: List[str]):
    if not urls:
        return
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executemany("INSERT OR IGNORE INTO dork_seen (dork, url) VALUES (?, ?)",
                    [(dork, u) for u in urls])
    con.commit()
    con.close()

# ───────────────────────────────────────────────────────────────────────────────
# Selenium Search (DuckDuckGo) with Proxy + WebDriver Manager
# ───────────────────────────────────────────────────────────────────────────────
def search_selenium(dork_query: str, count: int = 15) -> List[str]:
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")

    proxy_url = _pick_proxy()
    if proxy_url:
        options.add_argument(f"--proxy-server={proxy_url}")

    driver = webdriver.Chrome(ChromeDriverManager().install(), options=options)
    driver.get(f"https://duckduckgo.com/?q={dork_query}")

    urls = []
    elements = driver.find_elements(By.CSS_SELECTOR, "a.result__a")
    for e in elements:
        href = e.get_attribute("href")
        if href and href.startswith("http"):
            urls.append(href)
        if len(urls) >= count:
            break

    driver.quit()
    return urls

# ───────────────────────────────────────────────────────────────────────────────
# Telegram handlers (simplified)
# ───────────────────────────────────────────────────────────────────────────────
async def dork(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /dork <query>\\nExample: /dork inurl:checkout Stripe")
        return
    dork_query = " ".join(context.args).strip()
    try:
        results = search_selenium(dork_query, count=20)
    except Exception as e:
        await update.message.reply_text(f"Search error: {e}")
        return

    seen = get_seen_urls_for_dork(dork_query)
    new_urls = [u for u in results if u not in seen][:5]
    if not new_urls:
        await update.message.reply_text("No new sites found for this dork (already scanned recent hits).")
        return

    mark_urls_seen(dork_query, new_urls)
    await update.message.reply_text(f"Scanning {len(new_urls)} new site(s):\\n" + "\\n".join(new_urls))

# ───────────────────────────────────────────────────────────────────────────────
# App bootstrap
# ───────────────────────────────────────────────────────────────────────────────
def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        raise RuntimeError("Set BOT_TOKEN env var to your Telegram bot token.")
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("dork", dork))
    app.run_polling()

if __name__ == "__main__":
    main()