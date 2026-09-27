"""把 app/icon/icon.jpg 转成打包要用的图标：icon.icns（macOS）+ icon.ico（Windows）。

    python scripts/make_icons.py

产物已提交进仓库，所以 CI 和普通用户都不用跑这个脚本；只有换图标时才需要。
只用 PyQt6（项目本来就依赖），不额外装 Pillow / ImageMagick。
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication, QImage

ROOT = Path(__file__).resolve().parent.parent
ICON_DIR = ROOT / "app" / "icon"
SOURCE = ICON_DIR / "icon.jpg"
#: .icns 内嵌的这些尺寸；.ico 少一个 512（Windows 用不到那么大）
ICNS_SIZES = (16, 32, 64, 128, 256, 512, 1024)
ICO_SIZES = (16, 32, 48, 64, 128, 256)


def square_source() -> QImage:
    """把竖长了一点点的原图补成正方形。

    原图 1090×1137，直接拉成正方形会把人脸压扁；这里按边缘色补边，
    因为差值只有 4%，肉眼看不出接缝。
    """
    image = QImage(str(SOURCE))
    if image.isNull():
        raise SystemExit(f"读不到图标原图：{SOURCE}")
    side = max(image.width(), image.height())
    edge = image.pixelColor(0, 0)
    canvas = QImage(side, side, QImage.Format.Format_RGB32)
    canvas.fill(edge)
    canvas.setDevicePixelRatio(1.0)
    from PyQt6.QtGui import QPainter

    painter = QPainter(canvas)
    painter.drawImage((side - image.width()) // 2, (side - image.height()) // 2, image)
    painter.end()
    return canvas


def scaled(source: QImage, size: int) -> QImage:
    return source.scaled(
        size,
        size,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def write_icns(source: QImage) -> Path:
    """macOS：凑一个 .iconset 目录再交给系统自带的 iconutil。"""
    if sys.platform != "darwin":
        raise SystemExit("iconutil 只在 macOS 上有")
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"
        iconset.mkdir()
        for size in ICNS_SIZES:
            scaled(source, size).save(str(iconset / f"icon_{size}x{size}.png"))
            if size <= 512:
                scaled(source, size * 2).save(str(iconset / f"icon_{size}x{size}@2x.png"))
        target = ICON_DIR / "icon.icns"
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(target)], check=True
        )
        return target


def write_ico(source: QImage) -> Path:
    """Windows：手写 ICO 容器。

    Vista 之后的 ICO 允许直接嵌 PNG，所以不需要 BMP 那套调色板逻辑，
    也就不必为了生成一个图标去装 Pillow。目录项 16 字节，见 ICO 规范。
    """
    blobs: list[tuple[int, bytes]] = []
    with tempfile.TemporaryDirectory() as tmp:
        for size in ICO_SIZES:
            path = Path(tmp) / f"{size}.png"
            scaled(source, size).save(str(path), "PNG")
            blobs.append((size, path.read_bytes()))

    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = len(header) + 16 * len(blobs)
    entries = b""
    for size, blob in blobs:
        # 256 在目录项里用 0 表示：一个字节放不下 256
        dimension = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(blob), offset
        )
        offset += len(blob)

    target = ICON_DIR / "icon.ico"
    target.write_bytes(header + entries + b"".join(blob for _, blob in blobs))
    return target


def main() -> int:
    QGuiApplication([])
    source = square_source()
    ico = write_ico(source)
    print(f"OK  {ico.relative_to(ROOT)}  ({ico.stat().st_size} 字节)")
    if sys.platform == "darwin":
        icns = write_icns(source)
        print(f"OK  {icns.relative_to(ROOT)}  ({icns.stat().st_size} 字节)")
    else:
        print("跳过 .icns：iconutil 只在 macOS 上可用（仓库里已有现成的 icon.icns）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
