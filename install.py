"""
install.py — put Sid on your Desktop and Start Menu. Run once.

    py install.py              add Desktop + Start Menu shortcuts
    py install.py --startup    also launch Sid when Windows starts
    py install.py --listener   run the "Hey Jarvis" listener at startup
    py install.py --remove     take all of it back off

A Windows shortcut (.lnk) is a small binary file, so you can't just write one
with open(). We ask Windows itself to make it, through the same COM object
that Explorer uses. That's what the PowerShell block below does.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "Axon.pyw"
ICON = ROOT / "frontend" / "icons" / "axon.ico"

DESKTOP = Path.home() / "Desktop"
START_MENU = (
    Path.home() / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs"
)
STARTUP = START_MENU / "Startup"


def make_icon() -> None:
    """
    Build a .ico from the PNG we already have.

    Windows shortcuts want .ico specifically, and a proper one contains
    SEVERAL sizes — Windows picks whichever fits the context (16px in the
    taskbar, 256px on the desktop at large-icon setting). Pillow does this
    for us if we hand it a sizes list.
    """
    if ICON.exists():
        return
    try:
        from PIL import Image

        source = Image.open(ROOT / "frontend/icons/icon-512.png")
        source.save(
            ICON,
            sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )
        print(f"  made {ICON.name}")
    except Exception as exc:
        print(f"  (no icon: {exc}) - shortcuts will use the default")


def pythonw() -> str:
    """The no-console Python. This is what makes it feel like an app."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate if candidate.exists() else sys.executable)


def make_shortcut(folder: Path, name: str = "Sid", script: Path | None = None) -> None:
    """Ask Windows to create a .lnk, via PowerShell + the WScript.Shell COM object."""
    folder.mkdir(parents=True, exist_ok=True)
    link = folder / f"{name}.lnk"
    script = script or TARGET

    icon_line = f'$s.IconLocation = "{ICON}"' if ICON.exists() else ""

    # Named `ps`, not `script` - reusing the name would shadow the Path we
    # were handed. It happens to work (the f-string is evaluated before the
    # assignment lands) but it is exactly the kind of accident that breaks
    # the day someone reorders two lines.
    ps = f'''
$w = New-Object -ComObject WScript.Shell
$s = $w.CreateShortcut("{link}")
$s.TargetPath = "{pythonw()}"
$s.Arguments = '"{script}"'
$s.WorkingDirectory = "{ROOT}"
$s.Description = "Sid - personal AI assistant"
{icon_line}
$s.Save()
'''
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"  created {link}")
    else:
        print(f"  FAILED {link}: {result.stderr.strip()[:200]}")


def listener_running() -> bool:
    """Is a wake-word listener already running? Avoids starting a second one."""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-CimInstance Win32_Process -Filter \"Name='python.exe' or "
         "Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like "
         "'*listener.py*' }).ProcessId"],
        capture_output=True, text=True,
    )
    return bool((result.stdout or "").strip())


def start_listener() -> None:
    """Launch the wake-word listener in the background, with no console."""
    if listener_running():
        print("  listener is already running")
        return

    subprocess.Popen(
        [pythonw(), str(ROOT / "listener.py")],
        cwd=str(ROOT),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    print("  listener started - say your wake word now, no reboot needed")


def remove() -> None:
    for folder in (DESKTOP, START_MENU, STARTUP):
        # Old names are included deliberately. When the app was renamed from
        # Axon to Sid, the code started creating Sid.lnk but nothing deleted
        # Axon.lnk - so both sat on the Desktop and the stale one still got
        # clicked. Cleaning up previous names is part of renaming anything
        # that leaves files behind.
        for name in ("Sid.lnk", "Sid Listener.lnk",
                     "Axon.lnk", "Axon Listener.lnk"):
            link = folder / name
            if link.exists():
                link.unlink()
                print(f"  removed {link}")
    print("\nDone. (The project folder itself is untouched.)")


def main() -> None:
    if "--remove" in sys.argv:
        remove()
        return

    print(f"Installing Sid shortcuts from {ROOT}\n")
    make_icon()

    # Sweep away shortcuts from earlier names first, so you never end up
    # with two icons and no idea which one is live.
    for folder in (DESKTOP, START_MENU, STARTUP):
        for stale in ("Axon.lnk", "Axon Listener.lnk"):
            if (folder / stale).exists():
                (folder / stale).unlink()
                print(f"  removed old {stale}")

    make_shortcut(DESKTOP)
    make_shortcut(START_MENU)

    if "--startup" in sys.argv:
        make_shortcut(STARTUP)
        print("  Sid will now start with Windows")

    if "--listener" in sys.argv:
        make_shortcut(STARTUP, "Sid Listener", ROOT / "listener.py")
        print("  listener will start with Windows")

        # ...and start it RIGHT NOW. A Startup shortcut only fires at the
        # next login, so without this the wake word appears broken until you
        # reboot - which is exactly what happened. Installing something and
        # having it not run is a bad default.
        start_listener()

    print("\nDone. Double-click Sid on your Desktop.")
    print("The server starts itself and Sid opens in its own window.")
    print("\nTo undo:  py install.py --remove")


if __name__ == "__main__":
    main()
