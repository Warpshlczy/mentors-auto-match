"""本地打包：python scripts/build.py

PyInstaller 不支持交叉编译，所以这个脚本只能在目标系统上跑：
在 Windows 上打出 zip，在 macOS 上打出 .app + dmg。
想一次拿到三个平台，用 .github/workflows/build.yml（三个 runner 各跑一遍本脚本）。

首次使用需要先装打包工具：pip install pyinstaller
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
APP_NAME = "MentorMatch"

#: platform.machine() → 产物名里的架构后缀，方便用户一眼看出下的是哪个
ARCH_LABELS = {
    "x86_64": "intel",
    "amd64": "x64",
    "arm64": "arm64",
    "aarch64": "arm64",
}


def arch_label() -> str:
    machine = platform.machine().lower()
    label = ARCH_LABELS.get(machine)
    if not label:
        raise SystemExit(f"认不出的架构：{machine}，请手动指定产物名")
    return label


def icon_path() -> Path:
    name = "icon.icns" if sys.platform == "darwin" else "icon.ico"
    path = ROOT / "app" / "icon" / name
    if not path.exists():
        # 图标没生成过就先补上（Windows 也能生成 .ico，.icns 用仓库里已有的）
        subprocess.run([sys.executable, str(ROOT / "scripts" / "make_icons.py")], check=True)
    return path


def run_pyinstaller(icon: Path) -> None:
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--windowed",                      # GUI 程序，不要黑色控制台窗口
        "--name", APP_NAME,
        "--icon", str(icon),
        "--distpath", str(DIST),
        "--workpath", str(ROOT / "build"),
        str(ROOT / "run.py"),
    ]
    if sys.platform == "darwin":
        args.insert(-1, "--osx-bundle-identifier")
        args.insert(-1, "com.mentormatch.app")
    print("$", " ".join(args), flush=True)
    subprocess.run(args, check=True, cwd=ROOT)


def make_dmg() -> Path:
    app = DIST / f"{APP_NAME}.app"
    if not app.exists():
        raise SystemExit(f"没找到 {app}，打包这一步应该失败了")
    # 本地没有 Apple 开发者证书，只做 ad-hoc 签名，让包内部自洽一点；
    # 用户首次打开仍会被 Gatekeeper 拦一次（见 README）。
    subprocess.run(["codesign", "--force", "--deep", "-s", "-", str(app)], check=False)
    dmg = DIST / f"{APP_NAME}-macos-{arch_label()}.dmg"
    dmg.unlink(missing_ok=True)
    subprocess.run(
        ["hdiutil", "create", "-volname", APP_NAME, "-srcfolder", str(app),
         "-ov", "-format", "UDZO", str(dmg)],
        check=True,
    )
    return dmg


def make_zip() -> Path:
    folder = DIST / APP_NAME
    if not folder.exists():
        raise SystemExit(f"没找到 {folder}，打包这一步应该失败了")
    archive = shutil.make_archive(str(DIST / f"{APP_NAME}-windows-{arch_label()}"), "zip", DIST, APP_NAME)
    return Path(archive)


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit("缺少 pyinstaller，先执行：pip install pyinstaller")

    print(f"平台：{sys.platform} / {platform.machine()} / Python {platform.python_version()}")
    run_pyinstaller(icon_path())

    if sys.platform == "darwin":
        print(f"\n产物：{make_dmg()}")
        print(f"（{DIST / (APP_NAME + '.app')} 是解压后的 app，dmg 里装的就是它）")
    else:
        print(f"\n产物：{make_zip()}（解压后运行 {APP_NAME}/{APP_NAME}.exe）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
