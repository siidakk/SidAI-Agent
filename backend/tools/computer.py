"""
tools/computer.py — the tools that make "do anything on my PC" possible.

THE DESIGN INSIGHT
------------------
You cannot write a tool for every possible action. "Everything you can do on
a PC" is not a list — it's millions of things, and it grows every time you
install software.

So you don't enumerate. You give the agent a few **general-purpose** tools
that compose, and let it combine them:

    run_command    anything with a command line  (which on Windows is
                   almost everything: apps, files, processes, services,
                   network, printers, installed programs, settings)
    open_app       launch things by name
    press_keys     drive apps that only have a GUI
    type_text      write into whatever is focused
    clipboard      move data between apps
    list_windows   see what's actually open

Six tools, effectively unlimited reach. That's the same reason Unix ships
`sh` rather than ten thousand commands.

⚠️ WHY EVERY DANGEROUS ONE ASKS FIRST
-------------------------------------
`run_command` is arbitrary code execution with your full user rights. Sid
also reads your Gmail and web search results — text written by strangers.
Untrusted input plus arbitrary execution is the pairing that turns a hidden
instruction in an email into a real command on your machine.

Phase 3 was safe because the dangerous capability did not exist. Now it does,
so `approvals.py` replaces that defence with a human tap. Every `danger` tool
shows you the exact command and waits.

The blocklist below is a second layer, for things nobody should be one
mis-tap away from.
"""

import ctypes
import subprocess
import time

from . import tool

# Commands that are refused outright, approval or not. This is not a security
# boundary - anything here can be trivially reworded, and a determined
# attacker who owns the model owns the machine anyway. It exists to stop a
# CONFUSED model, and to stop you approving something at 2am while half
# asleep. Treat it as a guardrail, not a wall.
NEVER = [
    "format ",           # format c:
    "rm -rf /",
    "del /f /s /q c:\\",
    "remove-item -recurse -force c:\\",
    "diskpart",
    "bcdedit",
    "vssadmin delete",   # deletes shadow copies - ransomware's first move
    "cipher /w",
    "mkfs",
    ":(){",              # fork bomb
]

TIMEOUT_SECONDS = 60


def _blocked(command: str) -> str | None:
    lowered = " ".join(command.lower().split())
    for pattern in NEVER:
        if pattern in lowered:
            return pattern
    return None


@tool(tier="danger")
def run_command(command: str) -> str:
    """Run a PowerShell command on the user's Windows PC and return its output.

    This is the most capable tool you have. Use it for anything there isn't a
    specific tool for: managing files and folders, checking or stopping
    processes, network and wifi info, installed programs, system settings,
    starting programs with arguments, reading logs.

    Prefer a specific tool when one exists (set_volume, play_on_youtube,
    list_events). Use this when nothing else fits.

    The user must approve every command before it runs, so write commands
    that are easy to read and do one clear thing.

    Args:
        command: The PowerShell command, e.g. "Get-Process | Sort-Object CPU -Descending | Select-Object -First 5"
    """
    hit = _blocked(command)
    if hit:
        return (
            f"Refused: that command contains '{hit}', which Sid will never "
            f"run. If you genuinely need it, do it yourself in a terminal."
        )

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            # Never inherit a console window - Sid runs headless.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return (
            f"The command was still running after {TIMEOUT_SECONDS}s and was "
            f"stopped. It may have been waiting for input - Sid can't type "
            f"into a prompt, so avoid interactive commands."
        )
    except Exception as exc:
        return f"Could not run it: {exc}"

    output = (result.stdout or "").strip()
    errors = (result.returncode != 0 and (result.stderr or "").strip()) or ""

    if errors:
        # Return the error AS the result, not as an exception. The model reads
        # this and corrects itself on the next loop - see tools/__init__.py.
        return f"Command failed (exit {result.returncode}):\n{errors[:1500]}"

    if not output:
        return "Done. (The command produced no output, which usually means it worked.)"

    if len(output) > 4000:
        return output[:4000] + f"\n\n[... {len(output) - 4000} more characters]"
    return output


@tool(tier="act")
def open_app(name: str) -> str:
    """Open an application, folder, file or website on the user's PC.

    Works with app names Windows knows (notepad, calc, mspaint, explorer,
    chrome, spotify), full file paths, and web addresses.

    Args:
        name: What to open, e.g. "notepad", "calculator", "C:/Users/Malika/Downloads"
    """
    # Friendly names -> what Windows actually calls them.
    ALIASES = {
        "calculator": "calc", "notepad": "notepad", "paint": "mspaint",
        "files": "explorer", "file explorer": "explorer", "explorer": "explorer",
        "settings": "ms-settings:", "control panel": "control",
        "task manager": "taskmgr", "cmd": "cmd", "terminal": "wt",
        "camera": "microsoft.windows.camera:", "browser": "chrome",
    }
    target = ALIASES.get(name.lower().strip(), name.strip())

    try:
        # `start` resolves app names, file associations, URLs and protocol
        # handlers - the same thing the Run box does. "" is the window title
        # argument, which start requires before a quoted path.
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", f'Start-Process "{target}"'],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        return f"Could not open '{name}': {exc}"

    return f"Opened {name}."


@tool(tier="read")
def list_windows() -> str:
    """List the windows currently open on the user's screen.

    Use this to find out what is running before focusing or closing something.
    """
    script = (
        "Get-Process | Where-Object { $_.MainWindowTitle -ne '' } | "
        "Select-Object -First 25 Id, ProcessName, MainWindowTitle | "
        "ForEach-Object { \"$($_.Id)`t$($_.ProcessName)`t$($_.MainWindowTitle)\" }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return f"Could not list windows: {exc}"

    lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return "No windows with visible titles are open."

    out = ["Open windows (pid, app, title):"]
    out += [f"  {ln}" for ln in lines]
    return "\n".join(out)


@tool(tier="act")
def focus_window(title: str) -> str:
    """Bring a window to the front of the screen.

    Args:
        title: Part of the window title or the app name, e.g. "chrome", "notepad"
    """
    script = f"""
$p = Get-Process | Where-Object {{ $_.MainWindowTitle -like '*{title}*' -or $_.ProcessName -like '*{title}*' }} |
     Where-Object {{ $_.MainWindowTitle -ne '' }} | Select-Object -First 1
if ($p) {{
  $sig = '[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
          [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);'
  $t = Add-Type -MemberDefinition $sig -Name W -Namespace F -PassThru
  $t::ShowWindow($p.MainWindowHandle, 9) | Out-Null
  $t::SetForegroundWindow($p.MainWindowHandle) | Out-Null
  Write-Output $p.MainWindowTitle
}}
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return f"Could not focus that window: {exc}"

    found = (result.stdout or "").strip()
    if not found:
        return f"No open window matching '{title}'. Use list_windows to see what's open."
    return f"Brought '{found}' to the front."


@tool(tier="act")
def type_text(text: str) -> str:
    """Type text into whatever window is currently focused, as if on the keyboard.

    Focus the right window FIRST with focus_window or open_app - this types
    wherever the cursor happens to be.

    Args:
        text: The text to type
    """
    # SendKeys treats these as control characters, so they must be escaped
    # or "100% + tax" starts pressing modifier keys.
    escaped = text
    for ch in "+^%~(){}[]":
        escaped = escaped.replace(ch, "{" + ch + "}")

    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        f"[System.Windows.Forms.SendKeys]::SendWait('{escaped.replace(chr(39), chr(39)*2)}')"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return f"Could not type: {exc}"

    preview = text if len(text) < 60 else text[:57] + "..."
    return f"Typed: {preview}"


# Keys we can press by name. Windows virtual key codes.
KEYS = {
    "enter": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "up": 0x26, "down": 0x28,
    "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "printscreen": 0x2C,
    "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f11": 0x7A, "f12": 0x7B,
}
KEYEVENTF_KEYUP = 0x0002


@tool(tier="act")
def press_keys(keys: str) -> str:
    """Press a keyboard shortcut in the focused window.

    Use this to drive apps that have no command line: copy, paste, save,
    close a tab, switch windows, take a screenshot.

    Args:
        keys: The combination, e.g. "ctrl+c", "alt+tab", "ctrl+shift+t", "win+d", "enter"
    """
    names = [k.strip().lower() for k in keys.replace(" ", "").split("+") if k.strip()]
    if not names:
        return "No keys given, e.g. 'ctrl+c'."

    codes = []
    for name in names:
        if name in KEYS:
            codes.append(KEYS[name])
        elif len(name) == 1:
            codes.append(ord(name.upper()))    # letters and digits
        else:
            return f"Don't know the key '{name}'. Known: {', '.join(sorted(KEYS))}"

    user32 = ctypes.windll.user32
    # Press every key down in order, then release in REVERSE order. That's
    # what makes ctrl+shift+t work: the modifiers must still be held when
    # the last key goes down.
    for code in codes:
        user32.keybd_event(code, 0, 0, 0)
        time.sleep(0.01)
    for code in reversed(codes):
        user32.keybd_event(code, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.01)

    return f"Pressed {'+'.join(names)}."


@tool(tier="read")
def read_clipboard() -> str:
    """Read whatever text is currently on the user's clipboard."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return f"Could not read the clipboard: {exc}"

    text = (result.stdout or "").strip()
    if not text:
        return "The clipboard is empty (or holds something that isn't text)."
    return f"Clipboard:\n{text[:3000]}"


@tool(tier="act")
def write_clipboard(text: str) -> str:
    """Put text on the user's clipboard so they can paste it anywhere.

    Args:
        text: The text to copy
    """
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"],
            input=text, capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return f"Could not set the clipboard: {exc}"

    return f"Copied {len(text)} characters to the clipboard."


@tool(tier="act")
def lock_screen() -> str:
    """Lock the computer straight away. The user will need to sign in again.

    Use this for "lock my pc", "lock it", "I'm heading out".
    """
    # `act`, not `danger`, so it never stops to ask. Locking is the safest
    # possible thing to do to a computer - it destroys nothing, interrupts
    # nothing, and the worst case of an accidental lock is typing your PIN.
    # Requiring approval to lock would be like asking "are you sure?" before
    # closing a door.
    try:
        subprocess.Popen(
            ["rundll32.exe", "user32.dll,LockWorkStation"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return f"Could not lock: {exc}"
    return "Locked."


@tool(tier="act")
def close_app(name: str) -> str:
    """Close an application gracefully, as if you clicked its X button.

    If the app has unsaved work it will show its own "save changes?" prompt -
    this does not force-kill anything, so nothing is lost without the user
    being asked by the app itself.

    Args:
        name: The app or window to close, e.g. "notepad", "chrome", "spotify"
    """
    # CloseMainWindow() sends WM_CLOSE - the same message the X button sends.
    # The app decides what to do, including prompting to save.
    #
    # Stop-Process would be the other option and is NOT used here: it kills
    # the process outright and silently loses unsaved work. That difference
    # is exactly why this can be `act` while force-killing stays behind an
    # approval via run_command.
    safe = str(name).replace("'", "''")
    script = f"""
$hit = Get-Process | Where-Object {{
    ($_.MainWindowTitle -like '*{safe}*' -or $_.ProcessName -like '*{safe}*') -and
    $_.MainWindowTitle -ne ''
}}
if (-not $hit) {{ Write-Output 'NONE'; exit }}
foreach ($p in $hit) {{
    Write-Output ("CLOSED " + $p.ProcessName + " :: " + $p.MainWindowTitle)
    $p.CloseMainWindow() | Out-Null
}}
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return f"Could not close '{name}': {exc}"

    output = (result.stdout or "").strip()
    if not output or output == "NONE":
        return f"Nothing open matching '{name}'. Use list_windows to see what is."

    closed = [ln.replace("CLOSED ", "") for ln in output.splitlines()
              if ln.startswith("CLOSED")]
    return f"Closed {len(closed)}: " + "; ".join(closed[:5])


@tool(tier="danger")
def power_action(action: str) -> str:
    """Put the computer to sleep, restart it, or shut it down.

    For locking use lock_screen instead - that one does not need approval.

    Args:
        action: One of "sleep", "restart", "shutdown"
    """
    commands = {
        "sleep": "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
        "restart": "shutdown /r /t 5",
        "shutdown": "shutdown /s /t 5",
    }
    key = str(action).lower().strip()

    # Send "lock" to the tool that doesn't need approval, rather than making
    # the user approve something harmless because the model picked the more
    # general tool name.
    if key == "lock":
        return lock_screen()

    if key not in commands:
        return f"'{action}' isn't valid. Use: {', '.join(commands)}"

    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", commands[key]],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        return f"Could not {key}: {exc}"

    if key in ("restart", "shutdown"):
        return f"{key.title()} in 5 seconds. Run 'shutdown /a' to cancel."
    return f"{key.title()}ing now."
