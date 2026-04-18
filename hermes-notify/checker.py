"""
エルメス 在庫監視 & メール通知スクリプト
"""
import asyncio
import json
import logging
import os
import smtplib
import socket
import sys
import time
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import yaml
from bs4 import BeautifulSoup
from curl_cffi.requests import Session as CurlSession
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

STATE_FILE = Path("state.json")

_BASE_URL = "https://www.hermes.com/jp/ja/"


def load_config() -> dict:
    with open("config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def send_email(cfg: dict, subject: str, body_html: str):
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not password:
        raise RuntimeError(".env に GMAIL_APP_PASSWORD が設定されていません")

    msg = MIMEMultipart("alternative")
    recipients = cfg["gmail"]["recipients"]
    msg["Subject"] = subject
    msg["From"] = cfg["gmail"]["sender"]
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    # Force IPv4 — container has no IPv6 route
    smtp_ip = socket.getaddrinfo("smtp.gmail.com", 587, socket.AF_INET)[0][4][0]
    with smtplib.SMTP(smtp_ip, 587) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(cfg["gmail"]["sender"], password)
        smtp.sendmail(cfg["gmail"]["sender"], recipients, msg.as_string())
    log.info("メール送信完了: %s", subject)


def scrape_products_for_target(
    session: CurlSession, hermes_cfg: dict, target: dict, debug: bool = False
) -> list[dict]:
    """1つのターゲットをスクレイピングして商品リストを返す"""
    products = []
    label = target["label"]

    log.info("[%s] アクセス中: %s", label, target["search_url"])
    try:
        # Strip hash fragment — it's client-side only and can trigger bot detection
        clean_url = target["search_url"].split("#")[0]
        resp = session.get(clean_url, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        log.warning("[%s] リクエストエラー: %s", label, e)
        return []

    html = resp.text

    if "captcha-delivery.com" in html or "Please enable JS" in html:
        log.warning("[%s] ボット検知ページが返されました。接続を確認してください。", label)
        return []

    if debug:
        safe_label = label.replace(" ", "_")
        Path(f"page_{safe_label}.html").write_text(html, encoding="utf-8")
        log.info("[%s] デバッグ: HTML を保存しました", label)

    soup = BeautifulSoup(html, "html.parser")

    selectors = [s.strip() for s in hermes_cfg["product_selector"].split(",")]
    product_elements = []
    for selector in selectors:
        found = soup.select(selector)
        if found:
            product_elements = found
            log.info("[%s] セレクタ '%s' で %d 件の商品を検出", label, selector, len(found))
            break

    if not product_elements:
        log.warning("[%s] 商品が見つかりませんでした。デバッグモードを有効にして確認してください。", label)
        return []

    name_selectors = [s.strip() for s in hermes_cfg["name_selector"].split(",")]
    unavail_selectors = [s.strip() for s in hermes_cfg["unavailable_selector"].split(",")]

    for elem in product_elements:
        try:
            name = "不明"
            for ns in name_selectors:
                name_el = elem.select_one(ns)
                if name_el:
                    name = name_el.get_text(strip=True)
                    break

            link_el = elem.select_one("a[href]")
            href = link_el["href"] if link_el else ""
            url = (
                f"https://www.hermes.com{href}"
                if href and href.startswith("/")
                else href or ""
            )

            is_unavailable = any(elem.select_one(us) for us in unavail_selectors)
            if not is_unavailable:
                elem_text = elem.get_text(" ", strip=True).lower()
                if any(kw in elem_text for kw in ["在庫なし", "sold out", "unavailable", "入荷待ち"]):
                    is_unavailable = True

            products.append(
                {
                    "label": label,
                    "name": name,
                    "url": url,
                    "available": not is_unavailable,
                }
            )
        except Exception as e:
            log.debug("[%s] 商品パースエラー（スキップ）: %s", label, e)

    return products


def scrape_products(cfg: dict) -> list[dict]:
    """全ターゲットをスクレイピングして商品リストを返す"""
    hermes_cfg = cfg["hermes"]
    targets = hermes_cfg.get("targets", [])
    all_products = []

    with CurlSession(impersonate="chrome131") as session:
        # Warm up session with homepage to obtain cookies before scraping
        try:
            session.get(_BASE_URL, timeout=15)
            time.sleep(2)
        except Exception:
            pass
        for i, target in enumerate(targets):
            if i > 0:
                time.sleep(10)
            products = scrape_products_for_target(
                session, hermes_cfg, target, debug=cfg.get("debug", False)
            )
            all_products.extend(products)

    return all_products


async def check_once(cfg: dict, state: dict) -> tuple[list[dict], dict]:
    """1回チェックして新規入荷リストと更新済みstateを返す"""
    products = await asyncio.get_event_loop().run_in_executor(
        None, scrape_products, cfg
    )
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    newly_available = []

    for product in products:
        key = product["url"] or product["name"]
        prev = state.get(key, {})
        was_available = prev.get("available", False)

        if product["available"] and not was_available:
            newly_available.append(product)
            log.info("★ 新規入荷: %s", product["name"])
        elif product["available"]:
            log.info("  在庫あり（継続）: %s", product["name"])
        else:
            log.info("  在庫なし: %s", product["name"])

        state[key] = {
            "name": product["name"],
            "url": product["url"],
            "available": product["available"],
            "last_checked": now,
        }

    return newly_available, state


def build_email_body(newly_available: list[dict]) -> str:
    now = datetime.now(JST).strftime("%Y年%m月%d日 %H:%M:%S")
    rows = "".join(
        f'<tr>'
        f'<td style="padding:8px;border:1px solid #ddd;font-weight:bold;color:#b1935a">{p["label"]}</td>'
        f'<td style="padding:8px;border:1px solid #ddd">{p["name"]}</td>'
        f'<td style="padding:8px;border:1px solid #ddd">'
        f'<a href="{p["url"]}" style="color:#b1935a">購入ページを開く</a>'
        f'</td>'
        f'</tr>'
        for p in newly_available
    )
    return f"""
    <html>
    <body style="font-family:sans-serif;color:#333">
      <h2 style="color:#b1935a">&#x1f6cd;&#xfe0f; エルメス 入荷のお知らせ</h2>
      <p>以下の商品が入荷しました。</p>
      <p style="color:#666;font-size:12px">{now}</p>
      <table style="border-collapse:collapse;width:100%">
        <tr style="background:#f5f0e8">
          <th style="padding:8px;border:1px solid #ddd;text-align:left">種別</th>
          <th style="padding:8px;border:1px solid #ddd;text-align:left">商品名</th>
          <th style="padding:8px;border:1px solid #ddd;text-align:left">リンク</th>
        </tr>
        {rows}
      </table>
      <p style="margin-top:20px;font-size:11px;color:#999">
        このメールはエルメス在庫監視スクリプトが自動送信しました。
      </p>
    </body>
    </html>
    """


async def main():
    cfg = load_config()
    state = load_state()
    interval = cfg.get("interval_minutes", 5) * 60

    scan_only = "--scan" in sys.argv
    if scan_only:
        cfg["debug"] = True
        log.info("スキャンモード: 1回だけ実行してデバッグ情報を保存します")

    targets = cfg["hermes"].get("targets", [])
    target_names = "・".join(t["label"] for t in targets)
    log.info(
        "エルメス在庫監視を開始します（対象: %s / %d分間隔）",
        target_names,
        cfg.get("interval_minutes", 5),
    )

    is_first_run = not STATE_FILE.exists()

    while True:
        try:
            newly_available, state = await check_once(cfg, state)
            save_state(state)

            if newly_available:
                labels = sorted({p["label"] for p in newly_available})
                subject = f"【入荷通知】エルメス {'・'.join(labels)} {len(newly_available)}件"
                body = build_email_body(newly_available)
                send_email(cfg, subject, body)
            elif is_first_run:
                targets = cfg["hermes"].get("targets", [])
                target_names = "・".join(t["label"] for t in targets)
                available_count = sum(1 for v in state.values() if v.get("available"))
                total_count = len(state)
                now = datetime.now(JST).strftime("%Y年%m月%d日 %H:%M:%S")
                body = f"""
                <html>
                <body style="font-family:sans-serif;color:#333">
                  <h2 style="color:#b1935a">&#x1f6cd;&#xfe0f; エルメス 監視開始</h2>
                  <p>監視対象: {target_names}</p>
                  <p>在庫監視を開始しました。</p>
                  <p>（確認した商品数: {total_count}件 / 在庫あり: {available_count}件）</p>
                  <p style="color:#666;font-size:12px">{now}</p>
                  <p style="font-size:11px;color:#999">入荷次第お知らせします。</p>
                </body>
                </html>
                """
                send_email(cfg, f"【監視開始】エルメス {target_names} {available_count}件在庫あり", body)
                log.info("初回通知送信完了")
            else:
                log.info("新規入荷なし")

            is_first_run = False

        except Exception as e:
            log.error("エラーが発生しました: %s", e, exc_info=True)

        if scan_only:
            break

        log.info("%d秒後に再チェックします...", interval)
        await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
