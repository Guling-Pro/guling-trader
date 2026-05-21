"""股灵 brand mark 渲染（纯 PIL，无 SVG 运行时依赖）。

对应官网 favicon（web/public/favicon.svg）：阴阳双眼机器人头，主色金 #E0B656，
viewBox 0 0 100 100。这里用 PIL 在任意尺寸重绘，统一托盘 / 窗口 / exe 图标，
避免 cairo/librsvg 之类的运行时依赖。设计源 SVG 见 assets/favicon.svg。
"""
from __future__ import annotations

from typing import Optional, Tuple

from PIL import Image, ImageDraw

GOLD: Tuple[int, int, int, int] = (224, 182, 86, 255)  # #E0B656

# 连接状态 → 角标小圆点颜色（保留旧版"颜色表状态"的功能，不丢信息）。
STATE_DOT = {
    "unpaired": (128, 128, 128, 255),
    "dialing": (255, 200, 0, 255),
    "awaiting_bind": (255, 200, 0, 255),
    "connected": (0, 200, 0, 255),
    "disconnected": (128, 128, 128, 255),
    "fatal": (255, 0, 0, 255),
    "installing": (255, 165, 0, 255),
}


def render_logo(size: int = 64, color: Tuple[int, int, int, int] = GOLD) -> Image.Image:
    """渲染透明背景的 brand mark，size×size 的 RGBA Image。"""
    ss = 4  # 超采样，下采样后边缘平滑
    S = size * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    k = S / 100.0  # viewBox 100 → S 像素

    def p(x, y):
        return (x * k, y * k)

    def w(stroke):
        return max(1, int(round(stroke * k)))

    def dot(cx, cy, r):
        d.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=color)

    # 天线
    d.line([p(50, 8), p(50, 18)], fill=color, width=w(4))
    dot(50, 6, 3)
    # 头部圆角矩形（描边）
    d.rounded_rectangle([p(20, 20), p(80, 68)], radius=10 * k, outline=color, width=w(4.5))
    # 左眼（实心圆）
    dot(36, 42, 5.5)
    # 右眼（> 形眨眼）
    d.line([p(65, 39), p(58, 42), p(65, 45)], fill=color, width=w(4), joint="curve")
    # 身体/嘴部横线
    for x1, y1, x2, y2 in [
        (30, 80, 38, 80), (62, 80, 70, 80), (30, 86, 70, 86),
        (30, 92, 38, 92), (62, 92, 70, 92),
    ]:
        d.line([p(x1, y1), p(x2, y2)], fill=color, width=w(4))

    return img.resize((size, size), Image.LANCZOS)


def render_tray_icon(size: int, state: Optional[str] = None) -> Image.Image:
    """托盘图标：brand mark + 右下角状态色圆点（保留连接状态指示）。"""
    img = render_logo(size)
    dot_color = STATE_DOT.get(state or "")
    if dot_color:
        d = ImageDraw.Draw(img)
        r = max(2, size // 4)
        x2, y2 = size - 1, size - 1
        x1, y1 = x2 - 2 * r, y2 - 2 * r
        # 白描边让圆点在任意底色上都清晰
        d.ellipse([x1, y1, x2, y2], fill=dot_color, outline=(255, 255, 255, 255))
    return img


def save_ico(path: str, sizes=(16, 32, 48, 64, 128, 256)) -> None:
    """生成多尺寸 .ico（PyInstaller --icon 用）。"""
    base = render_logo(max(sizes))
    base.save(path, format="ICO", sizes=[(s, s) for s in sizes])
