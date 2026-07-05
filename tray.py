#!/usr/bin/env python3
"""tray.py — Windows 시스템 트레이 아이콘 (pystray + Pillow 필요, 둘 다 선택적 의존성)."""

import os
import webbrowser


def _make_icon_image():
    """트레이 아이콘 이미지 생성 (녹색 원 + HL)."""
    from PIL import Image, ImageDraw, ImageFont
    sz = 64
    img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 3
    d.ellipse([pad, pad, sz - pad, sz - pad], fill=(40, 167, 69, 255))
    font_size = int(sz * 0.40)
    try:
        font = ImageFont.truetype("C:\\Windows\\Fonts\\arialbd.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()
    text = "HL"
    try:
        bbox = d.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    except AttributeError:
        tw, th = d.textsize(text, font=font)
    tx = (sz - tw) // 2
    ty = (sz - th) // 2 - 2
    d.text((tx, ty), text, fill=(255, 255, 255, 255), font=font)
    return img


def run(url: str) -> None:
    """시스템 트레이 아이콘 실행. 메인 스레드에서 호출해야 한다 (Windows 요구사항)."""
    import pystray

    def open_browser(icon, item):
        webbrowser.open(url)

    def quit_app(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("브라우저 열기", open_browser, default=True),
        pystray.MenuItem("종료", quit_app),
    )
    icon = pystray.Icon("HLEditor", _make_icon_image(), "HLEditor", menu)
    icon.run()
