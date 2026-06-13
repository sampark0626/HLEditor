"""
setup_shortcut.py — HLEditor 바탕화면 바로가기 생성 (Windows, 최초 1회)

실행:
    python setup_shortcut.py
"""
import os
import sys
import subprocess
from pathlib import Path


def make_icon(out_path: Path) -> bool:
    """녹색 원 + HL 텍스트 아이콘을 icon.ico로 저장."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("[경고] Pillow 없음 — pip install Pillow 후 재실행하세요.")
        return False

    sizes = [256, 128, 64, 48, 32, 16]
    imgs = []
    for sz in sizes:
        img = Image.new("RGBA", (sz, sz), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        pad = max(1, sz // 16)
        # 녹색 원 배경
        d.ellipse([pad, pad, sz - pad, sz - pad], fill=(40, 167, 69, 255))
        # "HL" 텍스트
        font_size = max(6, int(sz * 0.40))
        try:
            font = ImageFont.truetype("C:\\Windows\\Fonts\\arialbd.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()
        text = "HL"
        # PIL 버전 호환
        try:
            bbox = d.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except AttributeError:
            tw, th = d.textsize(text, font=font)
        tx = (sz - tw) // 2
        ty = (sz - th) // 2 - max(1, sz // 20)
        d.text((tx, ty), text, fill=(255, 255, 255, 255), font=font)
        imgs.append(img)

    imgs[0].save(str(out_path), format="ICO", sizes=[(s, s) for s in sizes])
    print(f"아이콘 생성: {out_path}")
    return True


def create_shortcut(shortcut_path: Path, target: Path, icon: Path, workdir: Path):
    """PowerShell로 Windows .lnk 바로가기 생성."""
    ps = f"""
$WshShell  = New-Object -comObject WScript.Shell
$Shortcut  = $WshShell.CreateShortcut("{shortcut_path}")
$Shortcut.TargetPath        = "{target}"
$Shortcut.IconLocation      = "{icon},0"
$Shortcut.WorkingDirectory  = "{workdir}"
$Shortcut.Description       = "HLEditor — 축구 하이라이트 추출기"
$Shortcut.Save()
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"[오류] 바로가기 생성 실패:\n{result.stderr}")
        return False
    return True


def main():
    base = Path(__file__).parent.resolve()
    vbs  = base / "start.vbs"
    ico  = base / "icon.ico"

    # 1) 아이콘 생성 (없으면)
    if not ico.exists():
        if not make_icon(ico):
            print("[경고] 아이콘 없이 바로가기 생성을 시도합니다.")

    # 2) start.vbs 확인
    if not vbs.exists():
        print(f"[오류] {vbs} 가 없습니다. start.vbs 파일을 확인하세요.")
        sys.exit(1)

    # 3) 바탕화면 경로
    desktop = Path.home() / "Desktop"
    if not desktop.exists():
        # 한국어 Windows
        desktop = Path.home() / "바탕 화면"
    if not desktop.exists():
        desktop = Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"

    shortcut = desktop / "HLEditor.lnk"
    icon_loc = ico if ico.exists() else vbs  # 아이콘 없으면 기본

    if create_shortcut(shortcut, vbs, icon_loc, base):
        print(f"\n바탕화면에 바로가기가 생성되었습니다: {shortcut}")
        print("HLEditor.lnk 를 더블클릭하면 앱이 시작됩니다.\n")
    else:
        print(f"\n수동으로 바로가기를 만들려면:")
        print(f"  대상: {vbs}")
        print(f"  아이콘: {ico}\n")


if __name__ == "__main__":
    main()
