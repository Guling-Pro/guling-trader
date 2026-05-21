"""brand mark 渲染回归测试（托盘/窗口/exe 图标统一来源）。"""
import os

from PIL import Image

from trader import brand


def test_render_logo_rgba_nonempty():
    img = brand.render_logo(64)
    assert img.size == (64, 64)
    assert img.mode == "RGBA"
    # 不是全透明（确实画了东西）
    assert img.getbbox() is not None


def test_render_tray_icon_has_state_dot():
    plain = brand.render_logo(64)
    connected = brand.render_tray_icon(64, "connected")
    # 右下角圆点中心（约 47,47）让该像素和纯 logo 不同
    assert connected.getpixel((47, 47)) != plain.getpixel((47, 47))


def test_render_tray_icon_unknown_state_no_dot():
    # 未知状态不画圆点，应与纯 logo 一致
    assert brand.render_tray_icon(64, None).tobytes() == brand.render_logo(64).tobytes()


def test_save_ico(tmp_path):
    p = tmp_path / "icon.ico"
    brand.save_ico(str(p))
    assert p.exists() and p.stat().st_size > 0
    with Image.open(p) as im:
        assert im.format == "ICO"
