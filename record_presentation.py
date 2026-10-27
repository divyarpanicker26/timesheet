"""Record a short presentation walkthrough of the timesheet site.

The site must already be running at http://127.0.0.1:8766.
Writes presentation/nycha-timesheet-demo.mp4.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg
from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "presentation"
RAW_DIR = OUT_DIR / "raw"
BASE = "http://127.0.0.1:8766"
VIDEO_SIZE = {"width": 1600, "height": 900}


def caption(page: Page, text: str) -> None:
    page.evaluate(
        """(text) => {
          document.body.style.paddingBottom = '76px';
          let bar = document.getElementById('demo-caption');
          if (!bar) {
            bar = document.createElement('div');
            bar.id = 'demo-caption';
            bar.style.cssText = [
              'position:fixed', 'left:0', 'right:0', 'bottom:0', 'z-index:99999',
              'background:#17375e', 'color:#ffffff',
              'font:600 24px Segoe UI, Calibri, sans-serif',
              'padding:20px 36px'
            ].join(';');
            document.documentElement.appendChild(bar);
          }
          bar.textContent = text;
        }""",
        text,
    )


def pause(page: Page, milliseconds: int) -> None:
    page.wait_for_timeout(milliseconds)


def show_title(page: Page, heading: str, detail: str, milliseconds: int = 4000) -> None:
    page.set_content(
        f"""<!DOCTYPE html>
        <html><head><meta charset="utf-8"></head>
        <body style="margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
          background:#17375e;color:#fff;font-family:Segoe UI,Calibri,sans-serif;text-align:center;">
          <div style="max-width:1100px;padding:40px;">
            <div style="font-size:22px;letter-spacing:.14em;text-transform:uppercase;opacity:.85;">NYCHA</div>
            <h1 style="font-size:64px;margin:18px 0 12px;font-weight:700;">{heading}</h1>
            <p style="font-size:28px;margin:0;line-height:1.4;">{detail}</p>
          </div>
        </body></html>"""
    )
    pause(page, milliseconds)


def login(page: Page, username: str, password: str, line: str) -> None:
    page.goto(BASE, wait_until="networkidle")
    caption(page, line)
    pause(page, 1600)
    page.locator("#username").click()
    page.locator("#username").type(username, delay=80)
    page.locator("#password").click()
    page.locator("#password").type(password, delay=70)
    pause(page, 700)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_load_state("networkidle")


def sign_out(page: Page) -> None:
    page.get_by_role("button", name="Sign out").click()
    page.wait_for_load_state("networkidle")
    pause(page, 800)


def scroll_by(page: Page, total: int) -> None:
    steps = 8
    for _ in range(steps):
        page.evaluate("(distance) => window.scrollBy(0, distance)", total // steps)
        pause(page, 280)


def record() -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    RAW_DIR.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(
            viewport=VIDEO_SIZE,
            record_video_dir=str(RAW_DIR),
            record_video_size=VIDEO_SIZE,
        )
        page = context.new_page()
        show_title(
            page,
            "Time Sheet",
            "Employees submit hours. Supervisors approve. Admins set permissions.",
        )

        login(page, "divya", "timesheet", "An employee signs in to see only their own time sheets.")
        caption(page, "Each row shows the pay period, the weekday pattern, and whether it is approved.")
        pause(page, 3200)
        page.get_by_role("link", name="Open").first.click()
        page.wait_for_load_state("networkidle")
        caption(page, "Download PDF saves the filled form, including the drawn signatures.")
        pause(page, 2800)
        caption(page, "Weekdays on or before the form date are 9.00AM to 5.00PM. Weekends stay blank.")
        scroll_by(page, 720)
        pause(page, 2600)
        caption(page, "The employee and supervisor each draw their own signature. Nothing is signed for them.")
        scroll_by(page, 520)
        pause(page, 2800)
        sign_out(page)

        login(page, "supervisor", "approve", "A supervisor signs in to review every submitted time sheet.")
        caption(page, "The queue lists every employee. Open a sheet to approve it or send it back.")
        pause(page, 3800)
        sign_out(page)

        login(page, "admin", "manage", "An admin signs in to create user ids and give permissions.")
        caption(page, "Permissions are Submit time sheets, Review and approve, and Manage users.")
        pause(page, 2800)
        scroll_by(page, 640)
        caption(page, "A person can have more than one permission. The last admin account cannot be removed.")
        pause(page, 3400)
        sign_out(page)

        show_title(
            page,
            "Ready to try",
            "python timesheet_app.py &nbsp;&nbsp;·&nbsp;&nbsp; http://127.0.0.1:8766",
            4200,
        )
        video = page.video
        context.close()
        browser.close()
        if video is None:
            raise RuntimeError("No recording was created.")
        return Path(video.path())


def to_mp4(source: Path) -> Path:
    destination = OUT_DIR / "nycha-timesheet-demo.mp4"
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(source),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(destination),
        ],
        check=True,
        capture_output=True,
    )
    return destination


if __name__ == "__main__":
    webm = record()
    mp4 = to_mp4(webm)
    print(mp4)
