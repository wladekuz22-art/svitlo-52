"""Monitor the visible Group 5.2 schedule and notify Telegram chats."""
import hashlib
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


def capture(page, url, filename):
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
    (OUT / filename).write_bytes(raw)
    return {'url': url, 'text': text[:1200], 'image': filename, 'hash': digest}


def send_photo(token, chat, photo, caption):
    with open(photo, 'rb') as fp:
        response = requests.post(
            f'https://api.telegram.org/bot{token}/sendPhoto',
            data={'chat_id': chat, 'caption': caption[:1024]},
            files={'photo': fp}, timeout=40)
    response.raise_for_status()
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
            today = capture(page, BASE, 'today.png')
            tomorrow = None
            try:
                page.goto(BASE, wait_until='domcontentloaded', timeout=45000)
                link = page.get_by_role('link', name=re.compile('Графік на завтра', re.I)).first
                if link.count():
                    href = link.get_attribute('href')
                    if href:
                        tomorrow = capture(page, urljoin(BASE, href), 'tomorrow.png')
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
        if token and chat:
            for label, data in changed:
                caption = (f'⚡ Графік 5.2 оновився ({label})\n'
                           f'Перевірено: {now}\nДжерело: {data["url"]}')
                send_photo(token, chat, OUT / data['image'], caption)
                if family_chat:
                    send_photo(token, family_chat, OUT / data['image'], caption)
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
