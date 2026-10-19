"""Monitor the real Group 5.2 schedule and send compact Telegram images."""

import base64
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
OUT = ROOT / "docs"
OUT.mkdir(exist_ok=True)

STATE = OUT / "data.json"
BASE = "https://bezsvitla.com.ua/kyivska-oblast/cherha-5-2"
TZ = ZoneInfo("Europe/Kyiv")

HEADING = re.compile(r"Графік\s+черги\s+5\.2", re.I)
INTERVAL = re.compile(r"\d\d:\d\d\s*[-–]\s*\d\d:\d\d")


def capture(page, url, filename, title):
    response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    if not response or response.status >= 400:
        raise RuntimeError("Schedule page did not load")

    page.wait_for_timeout(2200)
    body = page.locator("body").inner_text(timeout=12000)
    if not HEADING.search(body):
        raise RuntimeError("Schedule heading not found; page may have changed")

    heading = page.get_by_text(HEADING).first
    if heading.count() == 0:
        raise RuntimeError("Schedule heading not found")

    card = None
    for depth in range(1, 7):
        candidate = heading.locator("xpath=" + "/".join([".."] * depth))
        try:
            content = candidate.inner_text(timeout=2000)
            rect = candidate.bounding_box()
            if (
                rect
                and 260 <= rect["width"] <= 1300
                and 90 <= rect["height"] <= 850
                and INTERVAL.search(content)
                and len(content) < 1400
            ):
                card = candidate
                break
        except Exception:
            pass

    if card is None:
        raise RuntimeError("Could not isolate the real schedule safely")

    visible_text = card.inner_text(timeout=5000)
    if not INTERVAL.search(visible_text):
        raise RuntimeError("Schedule intervals missing")

    raw_image = card.screenshot(animations="disabled", timeout=15000)
    schedule_hash = hashlib.sha256(raw_image).hexdigest()
    raw_b64 = base64.b64encode(raw_image).decode("ascii")

    decorated_b64 = page.evaluate(
        """
        async ({ imageData, title }) => {
            const original = new Image();
            original.src = "data:image/png;base64," + imageData;
            await original.decode();

            const source = document.createElement("canvas");
            source.width = original.width;
            source.height = original.height;
            const ctx = source.getContext("2d", { willReadFrequently: true });
            ctx.drawImage(original, 0, 0);
            const pixels = ctx.getImageData(0, 0, source.width, source.height).data;

            let minX = source.width, minY = source.height;
            let maxX = -1, maxY = -1;
            for (let y = 0; y < source.height; y += 2) {
                for (let x = 0; x < source.width; x += 2) {
                    const i = (y * source.width + x) * 4;
                    const r = pixels[i], g = pixels[i + 1], b = pixels[i + 2];
                    const a = pixels[i + 3];
                    const visible = a > 50 && (r < 238 || g < 238 || b < 238);
                    if (visible) {
                        minX = Math.min(minX, x);
                        minY = Math.min(minY, y);
                        maxX = Math.max(maxX, x);
                        maxY = Math.max(maxY, y);
                    }
                }
            }

            if (maxX < 0 || maxY < 0) {
                minX = 0;
                minY = 0;
                maxX = source.width - 1;
                maxY = source.height - 1;
            }
            const margin = 14;
            minX = Math.max(0, minX - margin);
            minY = Math.max(0, minY - margin);
            maxX = Math.min(source.width, maxX + margin);
            maxY = Math.min(source.height, maxY + margin);

            const cropWidth = Math.max(1, maxX - minX);
            const cropHeight = Math.max(1, maxY - minY);
            const padding = 24, headerHeight = 116, footerHeight = 48;
            const width = Math.max(520, cropWidth + padding * 2);
            const height = headerHeight + cropHeight + padding * 2 + footerHeight;
            const canvas = document.createElement("canvas");
            canvas.width = width;
            canvas.height = height;
            const out = canvas.getContext("2d");

            out.fillStyle = "#f1f5f9";
            out.fillRect(0, 0, width, height);
            out.fillStyle = "#172b49";
            out.fillRect(0, 0, width, headerHeight);
            out.fillStyle = "#ffffff";
            out.font = "bold 27px Arial, sans-serif";
            out.fillText("Графік відключень світла", 24, 44);
            out.fillStyle = "#cbd5e1";
            out.font = "20px Arial, sans-serif";
            out.fillText("Група 5.2 · Київська область", 24, 79);
            out.font = "17px Arial, sans-serif";
            out.fillText(title, 24, 105);

            out.fillStyle = "#ffffff";
            out.fillRect(
                padding - 6,
                headerHeight + padding - 6,
                width - (padding - 6) * 2,
                cropHeight + 12
            );
            out.drawImage(
                source, minX, minY, cropWidth, cropHeight,
                Math.floor((width - cropWidth) / 2),
                headerHeight + padding, cropWidth, cropHeight
            );
            out.fillStyle = "#64748b";
            out.font = "15px Arial, sans-serif";
            out.fillText("Джерело: bezsvitla.com.ua", 24, height - 17);
            return canvas.toDataURL("image/png").split(",")[1];
        }
        """,
        {"imageData": raw_b64, "title": title},
    )

    (OUT / filename).write_bytes(base64.b64decode(decorated_b64))
    return {
        "url": url,
        "text": visible_text[:1200],
        "image": filename,
        "hash": schedule_hash,
    }


def send_photo(token, chat, photo, caption):
    with open(photo, "rb") as fp:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": chat, "caption": caption[:1024]},
            files={"photo": fp},
            timeout=40,
        )
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError("Telegram rejected photo")


def main():
    previous = json.loads(STATE.read_text("utf-8")) if STATE.exists() else {}
    now = datetime.now(TZ).strftime("%d.%m.%Y %H:%M")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page(
                viewport={"width": 1200, "height": 900},
                device_scale_factor=2,
                locale="uk-UA",
                timezone_id="Europe/Kyiv",
            )
            today = capture(page, BASE, "today.png", "Сьогодні")
            tomorrow = None
            try:
                page.goto(BASE, wait_until="domcontentloaded", timeout=45000)
                link = page.get_by_role(
                    "link", name=re.compile("Графік на завтра", re.I)
                ).first
                if link.count():
                    href = link.get_attribute("href")
                    if href:
                        tomorrow = capture(
                            page, urljoin(BASE, href), "tomorrow.png", "Завтра"
                        )
            except Exception as exc:
                print("Tomorrow unavailable:", exc)
            browser.close()

        current = {
            "checked_at": now,
            "status": "ok",
            "today": today,
            "tomorrow": tomorrow,
            "source": BASE,
        }
        changed = []
        for key, label in [("today", "сьогодні"), ("tomorrow", "завтра")]:
            old = previous.get(key)
            new = current.get(key)
            if new and (not old or old.get("hash") != new["hash"]):
                changed.append((label, new))

        token = os.getenv("TELEGRAM_TOKEN")
        chat = os.getenv("TELEGRAM_CHAT_ID")
        family_chat = os.getenv("TELEGRAM_FAMILY_CHAT_ID")
        if token and chat:
            for label, data in changed:
                caption = (
                    f"⚡ Графік 5.2 оновився ({label})\n"
                    f"Перевірено: {now}\n"
                    f"Київська область\n"
                    f"Джерело: {data['url']}"
                )
                send_photo(token, chat, OUT / data["image"], caption)
                if family_chat:
                    send_photo(token, family_chat, OUT / data["image"], caption)
        elif changed:
            print("Telegram secrets missing; notifications not sent")

        STATE.write_text(
            json.dumps(current, ensure_ascii=False, indent=2), "utf-8"
        )
        print("Checked:", now, "changed:", [item[0] for item in changed])
    except Exception as exc:
        previous["checked_at"] = now
        previous["status"] = "error"
        previous["error"] = str(exc)[:200]
        STATE.write_text(
            json.dumps(previous, ensure_ascii=False, indent=2), "utf-8"
        )
        raise


if __name__ == "__main__":
    main()
