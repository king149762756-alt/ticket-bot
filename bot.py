import asyncio
import re
import sys
from datetime import datetime

import httpx
from playwright.async_api import async_playwright

# 讓 Windows 終端機比較正常顯示中文
sys.stdout.reconfigure(encoding="utf-8")

# ====== 改成你的三天資料 ======
TARGETS = [
    {
        "name": "9/11",
        "url": "https://tixcraft.com/ticket/area/26_ive/22286",
        "event_name": "IVE WORLD TOUR ＜SHOW WHAT I AM＞ IN TAIPEI",
        "event_location": "臺北大巨蛋",
        "event_time": "2026/09/11（五）19:00",
    },
    {
        "name": "9/12",
        "url": "https://tixcraft.com/ticket/area/26_ive/22287",
        "event_name": "IVE WORLD TOUR ＜SHOW WHAT I AM＞ IN TAIPEI",
        "event_location": "臺北大巨蛋",
        "event_time": "2026/09/12（六）18:00",
    },
    {
        "name": "9/13",
        "url": "https://tixcraft.com/ticket/area/26_ive/22288",
        "event_name": "IVE WORLD TOUR ＜SHOW WHAT I AM＞ IN TAIPEI",
        "event_location": "臺北大巨蛋",
        "event_time": "2026/09/13（日）18:00",
    },
]

# ====== 改成你的 webhook ======
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1495419030511812789/olLe6g6sG6NXbpbSIGJvVacX4ypGYMBS-H4rHM-R7885Ed-Xl7zI410I-_SxqxV0PatS"

# 想 tag 就填，不想 tag 就留空 ""
# 身分組：<@&123456789012345678>
# 個人：<@123456789012345678>
DISCORD_MENTION = "<@everyone>"

# 幾秒檢查一次
CHECK_INTERVAL = 10

# Embed 顏色
EMBED_COLOR = 0x0099FF  # 綠色


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def has_ticket(text: str) -> bool:
    return "remaining" in text.lower()


def extract_sections(text: str) -> list[str]:
    results = []
    lines = text.split("\n")

    for raw_line in lines:
        line = normalize_line(raw_line)
        lower_line = line.lower()

        if "remaining" in lower_line and "sold out" not in lower_line:
            results.append(line)

    unique_results = []
    seen = set()

    for item in results:
        if item not in seen:
            seen.add(item)
            unique_results.append(item)

    return unique_results[:50]


def convert_line_to_chinese_style(line: str) -> str:
    """
    例如：
    紅1B區 (best available) 4 seat(s) remaining
    -> 紅1B區 剩餘 4
    """
    line = normalize_line(line)
    line = re.sub(r"\s*\(best available\)", "", line, flags=re.IGNORECASE)
    line = re.sub(r"(\d+)\s*seat\(s\)\s*remaining", r"剩餘 \1", line, flags=re.IGNORECASE)
    return line.strip()


def build_state_key(results_by_day: dict[str, list[str]]) -> str:
    parts = []
    for day_name in sorted(results_by_day.keys()):
        parts.append(day_name)
        parts.extend(results_by_day[day_name])
    return "\n".join(parts)


def build_embed_description(target: dict, sections: list[str]) -> str:
    formatted_sections = [convert_line_to_chinese_style(s) for s in sections]

    lines = [
        "🔥 開始放票啦 (Tixcraft)",
        f"🎤 場次名稱：{target['event_name']}",
        f"⏰ 更新時間：{now()}",
        f"🔗 網站連結：{target['url']}",
        "",
        "✅ 可購買票區：",
    ]

    lines.extend(formatted_sections)

    lines.extend([
        "",
        "📍 活動地點",
        target["event_location"],
        "🕒 活動時間",
        target["event_time"],
        "✨ 活動網址",
        f"[點我前往]({target['url']})",
    ])

    return "\n".join(lines)


def split_long_description(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks = []
    current = []

    for line in text.split("\n"):
        candidate = "\n".join(current + [line])
        if len(candidate) <= limit:
            current.append(line)
        else:
            if current:
                chunks.append("\n".join(current))
            current = [line]

    if current:
        chunks.append("\n".join(current))

    return chunks


async def send_discord_embed(title: str, description: str, url: str) -> None:
    chunks = split_long_description(description)

    async with httpx.AsyncClient(timeout=20) as client:
        for index, chunk in enumerate(chunks, start=1):
            embed_title = title if len(chunks) == 1 else f"{title} ({index}/{len(chunks)})"

            payload = {
                "content": DISCORD_MENTION if DISCORD_MENTION and index == 1 else None,
                "embeds": [
                    {
                        "title": embed_title,
                        "description": chunk,
                        "url": url,
                        "color": EMBED_COLOR,
                    }
                ]
            }

            resp = await client.post(DISCORD_WEBHOOK_URL, json=payload)
            print(f"Discord 狀態碼: {resp.status_code}")


async def fetch_text(page, url: str) -> str:
    await page.goto(url, wait_until="domcontentloaded", timeout=20000)

    loaded = False
    for keyword in ["remaining", "Sold out", "sold out"]:
        try:
            await page.wait_for_selector(f"text={keyword}", timeout=5000)
            loaded = True
            break
        except:
            pass

    if not loaded:
        print("⚠️ 沒等到 remaining / Sold out，先直接抓整頁文字")

    await page.wait_for_timeout(1500)
    return await page.locator("body").inner_text()


async def main() -> None:
    print("🚀 三天監票 Bot 啟動")

    last_sent_state = ""

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(locale="en-US")

        while True:
            try:
                results_by_day: dict[str, list[str]] = {}
                target_map: dict[str, dict] = {}

                for target in TARGETS:
                    day_name = target["name"]

                    print(f"[{now()}] 檢查 {day_name}")

                    text = await fetch_text(page, target["url"])

                    if has_ticket(text):
                        sections = extract_sections(text)

                        if sections:
                            results_by_day[day_name] = sections
                            target_map[day_name] = target

                            print(f"🎯 {day_name} 有票：")
                            for s in sections:
                                print("  ", convert_line_to_chinese_style(s))
                        else:
                            print(f"⚠️ {day_name} 有 remaining，但沒解析到明細")
                    else:
                        print(f"❌ {day_name} 尚無票")

                current_state = build_state_key(results_by_day) if results_by_day else ""

                if results_by_day and current_state != last_sent_state:
                    print("✅ 票況有變化，準備發送 Discord")

                    for day_name, sections in results_by_day.items():
                        target = target_map[day_name]
                        description = build_embed_description(target, sections)

                        await send_discord_embed(
                            title=f"🎟️ {day_name} 放票通知",
                            description=description,
                            url=target["url"]
                        )

                    last_sent_state = current_state
                    print("✅ Discord 已送出通知")
                elif results_by_day:
                    print("⚪ 有票，但票況和上次相同，不重複通知")
                else:
                    print("🕳️ 三天目前都沒有票")

            except Exception as e:
                print("❌ 發生錯誤：", e)

            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bot 已手動停止")