"""Monitor the visible Group 5.2 schedule and notify Telegram chats."""
import hashlib
import base64
import html
import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent
OUT = ROOT / 'docs'
OUT.mkdir(exist_ok=True)
STATE = OUT / 'data.json'
BASE = 'https://bezsvitla.com.ua/kyivska-oblast/cherha-5-2'
TZ = ZoneInfo('Europe/Kyiv')
# The site currently says "Графік Черга 5.2", not "Графік черги 5.2".
HEADING = re.compile(r'Графік\s+черг[аиі]\s+5[.,]2', re.I)
INTERVAL = re.compile(r'\d{2}:\d{2}\s*[-–—]\s*\d{2}:\d{2}')


def decorate(page, raw, label):
    """Render a compact branded image using the real, unmodified schedule screenshot."""
    encoded = base64.b64encode(raw).decode('ascii')
    title = html.escape(label)
    preview = page.context.browser.new_page()
    try:
        preview.set_content(f"""<!doctype html><html lang="uk"><head><meta charset="utf-8">
<style>
* {{ box-sizing:border-box; }}
html,body {{ margin:0; padding:0; background:#eef2f7; }}
.panel {{ width:760px; padding:20px; background:#eef2f7; font-family:Arial,sans-serif; }}
.header {{ background:#14243b; color:white; padding:22px 26px; border-radius:18px 18px 0 0; }}
h1 {{ margin:0 0 8px; font-size:27px; line-height:1.2; }}
.sub {{ font-size:17px; color:#c8d8f1; }}
.content {{ background:white; padding:12px; text-align:center; }}
.content img {{ display:block; width:100%; height:auto; }}
.footer {{ background:#14243b; color:#c8d8f1; padding:13px 26px; border-radius:0 0 18px 18px; font-size:14px; }}
</style></head><body><div class="panel">
<div class="header"><h1>⚡ Графік відключень світла</h1>
<div class="sub">Група 5.2 · Київська область · {title}</div></div>
<div class="content"><img src="data:image/png;base64,{encoded}" alt="Графік"></div>
<div class="footer">Джерело: bezsvitla.com.ua</div>
</div></body></html>""", wait_until='load')
        return preview.locator('.panel').screenshot(animations='disabled', timeout=15000)
    finally:
        preview.close()


def capture(page, url, filename, label):
    response = page.goto(url, wait_until='domcontentloaded', timeout=45000)
    if not response or response.status >= 400:
        raise RuntimeError(f'HTTP {response.status if response else "no response"}')
    page.wait_for_timeout(2200)
    body = page.locator('body').inner_text(timeout=12000)
    if not HEADING.search(body):
        raise RuntimeError('Schedule heading not found; page may have changed')
    heading = page.get_by_text(HEADING).first
    if heading.count() == 0:
        raise RuntimeError('Schedule heading element not found')
    card = None
    for depth in range(1, 8):
        candidate = heading.locator('xpath=' + '/'.join(['..'] * depth))
        try:
            content = candidate.inner_text(timeout=2000)
            rect = candidate.bounding_box()
            if (rect and 260 <= rect['width'] <= 1400
                    and 90 <= rect['height'] <= 950
                    and INTERVAL.search(content) and len(content) < 1800):
                card = candidate
                break
        except Exception:
            continue
    if card is None:
        raise RuntimeError('Could not isolate schedule card; no message sent')
    text = card.inner_text(timeout=5000)
    if not INTERVAL.search(text):
        raise RuntimeError('Schedule time intervals missing')
    raw = card.screenshot(animations='disabled', timeout=15000)
    # Detect changes using the original card, independent of our image decoration.
    digest = hashlib.sha256(raw).hexdigest()
    # Clip only whitespace outside the original card; keep every original color/time.
    # The screenshot is already a tight element screenshot, not a full-page screenshot.
    (OUT / filename).write_bytes(decorate(page, raw, label))
    return {'url': url, 'text': text[:1200], 'image': filename, 'hash': digest}


def send_photo(token, chat, photo, caption):
    with open(photo, 'rb') as fp:
        response = requests.post(
            f'https://api.telegram.org/bot{token}/sendPhoto',
            data={'chat_id': chat, 'caption': caption[:1024]},
            files={'photo': fp}, timeout=40)
    if not response.ok:
        try:
            reason = response.json().get('description', 'Telegram request failed')
        except ValueError:
            reason = f'HTTP {response.status_code}'
        raise RuntimeError(f'Telegram sendPhoto failed: {reason}')
    if not response.json().get('ok'):
        raise RuntimeError('Telegram rejected photo')


def main():
    previous = json.loads(STATE.read_text('utf-8')) if STATE.exists() else {}
    now = datetime.now(TZ).strftime('%d.%m.%Y %H:%M')
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
            page = browser.new_page(viewport={'width': 1200, 'height': 900},
                                    device_scale_factor=2, locale='uk-UA',
                                    timezone_id='Europe/Kyiv')
            today = capture(page, BASE, 'today.png', 'Сьогодні')
            tomorrow = None
            try:
                page.goto(BASE, wait_until='domcontentloaded', timeout=45000)
                link = page.get_by_role('link', name=re.compile('Графік на завтра', re.I)).first
                if link.count():
                    href = link.get_attribute('href')
                    if href:
                        tomorrow = capture(page, urljoin(BASE, href), 'tomorrow.png', 'Завтра')
            except Exception as exc:
                print('Tomorrow unavailable:', exc)
            browser.close()
        current = {'checked_at': now, 'status': 'ok', 'today': today,
                   'tomorrow': tomorrow, 'source': BASE}
        changed = []
        for key, label in [('today', 'сьогодні'), ('tomorrow', 'завтра')]:
            old, new = previous.get(key), current.get(key)
            if new and (not old or old.get('hash') != new['hash']):
                changed.append((label, new))
        token = os.getenv('TELEGRAM_TOKEN')
        chat = os.getenv('TELEGRAM_CHAT_ID')
        family_chat = os.getenv('TELEGRAM_FAMILY_CHAT_ID')
        send_photo(token, family_chat, OUT / "today.png", "🧪 Тест сімейної групи")
        if token and chat:
            for label, data in changed:
                caption = (f'⚡ Графік 5.2 оновився ({label})\n'
                           f'Перевірено: {now}\nДжерело: {data["url"]}')
                send_photo(token, chat, OUT / data['image'], caption)
                if family_chat:
                    try:
                        send_photo(token, family_chat, OUT / data['image'], caption)
                    except Exception as exc:
                        print('Family group delivery failed:', exc)
        elif changed:
            print('Telegram secrets missing; saved graph without sending notifications')
        STATE.write_text(json.dumps(current, ensure_ascii=False, indent=2), 'utf-8')
        print('Checked:', now, 'changed:', [x[0] for x in changed])
    except Exception as exc:
        previous['checked_at'] = now
        previous['status'] = 'error'
        previous['error'] = str(exc)[:200]
        STATE.write_text(json.dumps(previous, ensure_ascii=False, indent=2), 'utf-8')
        raise


if __name__ == '__main__':
    main()
