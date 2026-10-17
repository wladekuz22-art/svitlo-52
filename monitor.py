"""Capture the visible schedule card. Never infer on/off status from plain intervals."""
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent
OUT = ROOT / 'docs'
OUT.mkdir(exist_ok=True)
STATE = OUT / 'data.json'
BASE = 'https://bezsvitla.com.ua/kyivska-oblast/cherha-5-2'
TZ = ZoneInfo('Europe/Kyiv')


def capture(page, url, filename):
    response = page.goto(url, wait_until='domcontentloaded', timeout=45000)
    if not response or response.status >= 400:
        raise RuntimeError(f'HTTP {response.status if response else "no response"}')
    page.wait_for_timeout(2200)
    body = page.locator('body').inner_text(timeout=12000)
    if not re.search(r'Графік\s+Черга\s+5\.2', body, re.I):
        raise RuntimeError('Schedule heading not found; page may have changed')
    # Find a compact visible schedule container, avoiding unrelated news and navigation.
    heading = page.get_by_text(re.compile(r'^Графік\s+Черга\s+5\.2$')).first
    if heading.count() == 0:
        raise RuntimeError('Schedule card not found')
    card = None
    for depth in range(1, 7):
        candidate = heading.locator('xpath=' + '/'.join(['..'] * depth))
        try:
            content = candidate.inner_text(timeout=2000)
            rect = candidate.bounding_box()
            if (rect and 260 <= rect['width'] <= 1300 and 90 <= rect['height'] <= 850
                and re.search(r'\d\d:\d\d\s*[–-]\s*\d\d:\d\d', content)
                and len(content) < 1400):
                card = candidate
                break
        except Exception:
            pass
    if card is None:
        raise RuntimeError('Could not isolate graph safely; no notification sent')
    card.screenshot(path=str(OUT / filename), animations='disabled', timeout=15000)
    visible_text = card.inner_text(timeout=5000)
    if not re.search(r'\d\d:\d\d\s*[–-]\s*\d\d:\d\d', visible_text):
        raise RuntimeError('Graph intervals missing')
    return {'url': url, 'text': visible_text[:1200], 'image': filename,
            'hash': hashlib.sha256((OUT / filename).read_bytes()).hexdigest()}


def send_photo(token, chat, photo, caption):
    with open(photo, 'rb') as fp:
        response = requests.post(f'https://api.telegram.org/bot{token}/sendPhoto',
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
            page = browser.new_page(viewport={'width': 1200, 'height': 900}, device_scale_factor=1,
                                    locale='uk-UA', timezone_id='Europe/Kyiv')
            today = capture(page, BASE, 'today.png')
            # Tomorrow is linked from the page, but its URL can change; follow actual link.
            tomorrow = None
            try:
                page.goto(BASE, wait_until='domcontentloaded', timeout=45000)
                link = page.get_by_role('link', name=re.compile('Графік на завтра', re.I)).first
                if link.count():
                    url = link.get_attribute('href')
                    from urllib.parse import urljoin
                    tomorrow = capture(page, urljoin(BASE, url), 'tomorrow.png')
            except Exception as ex:
                print('Tomorrow unavailable:', ex)
            browser.close()
        current = {'checked_at': now, 'status': 'ok', 'today': today, 'tomorrow': tomorrow,
                   'source': BASE}
        changed = []
        for key, label in [('today', 'сьогодні'), ('tomorrow', 'завтра')]:
            old = previous.get(key)
            new = current.get(key)
            if new and (not old or old.get('hash') != new['hash']):
                changed.append((label, new))
        token, chat = os.getenv('TELEGRAM_TOKEN'), os.getenv('TELEGRAM_CHAT_ID')
        # Only persist the new state once messages were successfully delivered.
        if token and chat:
            for label, data in changed:
                if data is None:
                    continue
                send_photo(token, chat, OUT / data['image'],
                           f'⚡ Графік 5.2 оновився ({label})\nПеревірено: {now}\nДжерело: {data["url"]}')
        elif changed:
            print('Telegram secrets missing; saved graph without sending notifications')
        STATE.write_text(json.dumps(current, ensure_ascii=False, indent=2), 'utf-8')
        print('Checked:', now, 'changed:', [x[0] for x in changed])
    except Exception as ex:
        # Preserve last good data, but mark that a check failed. No fabricated schedule.
        previous['checked_at'] = now
        previous['status'] = 'error'
        previous['error'] = str(ex)[:200]
        STATE.write_text(json.dumps(previous, ensure_ascii=False, indent=2), 'utf-8')
        raise


if __name__ == '__main__':
    main()
