"""
AUTO-SUPERVISOR WORKFORCE ECOSYSTEM
Desktop Client — Windows Lock Screen & Monitor

Compile to EXE:
    pip install pyqt6 mss pynput requests supabase nuitka aiohttp bcrypt

    nuitka --onefile --windowed \
           --include-qt-plugins=sensible \
           --windows-uac-uiaccess=true \
           --windows-product-name="WorkAgent" \
           --output-filename="WorkAgent.exe" \
           client.py

Set to run at startup:
    Python code below auto-registers itself in HKCU\\...\\Run on first launch.

Environment (edit CONFIG section below before compiling):
    BOT_AUTH_URL   — http://your-server:8765/auth
    SUPABASE_URL   — https://xxx.supabase.co
    SUPABASE_ANON  — anon/public key
    WORK_URL       — the website workers use (e.g. https://app.example.com)
"""

import sys
import os
import json
import uuid
import hashlib
import threading
import time
import logging
import subprocess
import winreg
from datetime import datetime, timezone
from pathlib import Path

import requests
import mss
import mss.tools
from pynput import mouse, keyboard
from PyQt6.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QHBoxLayout,
    QFrame, QSystemTrayIcon, QMenu, QMessageBox
)
from PyQt6.QtCore import (
    Qt, QTimer, pyqtSignal, QObject, QThread,
    QPropertyAnimation, QEasingCurve, QRect
)
from PyQt6.QtGui import (
    QFont, QColor, QPalette, QIcon, QPixmap,
    QFontDatabase, QPainter, QBrush
)

# ── CONFIG — edit before compiling ───────────────────────────────────────────
BOT_AUTH_URL  = os.environ.get("BOT_AUTH_URL",  "http://localhost:8765/auth")
SUPABASE_URL  = os.environ.get("SUPABASE_URL",  "https://your-project.supabase.co")
SUPABASE_ANON = os.environ.get("SUPABASE_ANON", "your-anon-key")
WORK_URL      = os.environ.get("WORK_URL",      "https://app.example.com")

SCREENSHOT_DIR    = Path(os.environ.get("APPDATA", ".")) / "WorkAgent" / "screenshots"
NUDGE_THRESHOLD   = 80.0    # rolling 15-min score below this triggers nudge
NUDGE_WINDOW_MIN  = 15      # minutes for rolling window
CLICK_SCORE_DECAY = 0.97    # decay per minute for rolling score
MIN_CLICKS_WINDOW = 10      # minimum clicks in window for score to be meaningful
APP_TITLE         = "Work Agent"

# ── Hardware ID ───────────────────────────────────────────────────────────────
def get_hwid() -> str:
    """Derive a stable hardware ID from machine identifiers."""
    try:
        result = subprocess.check_output(
            ["wmic", "csproduct", "get", "uuid"],
            stderr=subprocess.DEVNULL,
            timeout=5
        ).decode().strip().split("\n")
        raw = result[-1].strip()
    except Exception:
        import socket
        raw = socket.gethostname()
    return hashlib.sha256(raw.encode()).hexdigest()[:32]

HWID = get_hwid()

# ── Startup registration ──────────────────────────────────────────────────────
def register_startup():
    """Add this EXE to Windows startup registry."""
    try:
        exe_path = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE
        )
        winreg.SetValueEx(key, "WorkAgent", 0, winreg.REG_SZ, exe_path)
        winreg.CloseKey(key)
    except Exception as e:
        logging.warning(f"Could not register startup: {e}")

# ── Screenshot engine ─────────────────────────────────────────────────────────
class ScreenshotEngine:
    def __init__(self):
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        self.sct = mss.mss()

    def capture(self, trigger: str) -> str:
        """Capture full screen; return saved filepath."""
        ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = SCREENSHOT_DIR / f"{ts}_{trigger}.png"
        monitor = self.sct.monitors[1]  # primary monitor
        shot    = self.sct.grab(monitor)
        mss.tools.to_png(shot.rgb, shot.size, output=str(path))
        return str(path)

# ── Click monitor ─────────────────────────────────────────────────────────────
class ClickMonitor(QObject):
    """
    Global mouse listener.
    Captures screenshot when 'Login' or 'Send' button regions are clicked.
    Maintains rolling 15-min score based on click frequency.
    """
    nudge_triggered = pyqtSignal(float)
    screenshot_taken = pyqtSignal(str, str)  # (filepath, trigger_name)

    # Approximate screen coordinates for monitored buttons
    # Adjust these to match the work website layout at 1920x1080
    BUTTON_ZONES = {
        "login":  (860, 450, 200, 50),   # (x, y, width, height)
        "send":   (1700, 980, 180, 40),
    }

    def __init__(self):
        super().__init__()
        self.screenshot_engine = ScreenshotEngine()
        self.click_log: list[float] = []    # timestamps of productive clicks
        self.all_clicks: list[float] = []   # all click timestamps
        self._active = False
        self._listener = None

    def start(self):
        self._active = True
        self._listener = mouse.Listener(on_click=self._on_click)
        self._listener.start()

    def stop(self):
        self._active = False
        if self._listener:
            self._listener.stop()

    def _on_click(self, x, y, button, pressed):
        if not pressed or not self._active:
            return
        now = time.time()
        self.all_clicks.append(now)
        self._prune_old(now)

        zone_hit = self._check_zones(x, y)
        if zone_hit:
            self.click_log.append(now)
            path = self.screenshot_engine.capture(zone_hit)
            self.screenshot_taken.emit(path, zone_hit)

        score = self._rolling_score(now)
        if score is not None and score < NUDGE_THRESHOLD:
            self.nudge_triggered.emit(score)

    def _check_zones(self, x, y) -> str | None:
        for name, (zx, zy, zw, zh) in self.BUTTON_ZONES.items():
            if zx <= x <= zx + zw and zy <= y <= zy + zh:
                return name
        return None

    def _prune_old(self, now: float):
        cutoff = now - NUDGE_WINDOW_MIN * 60
        self.click_log  = [t for t in self.click_log  if t > cutoff]
        self.all_clicks = [t for t in self.all_clicks if t > cutoff]

    def _rolling_score(self, now: float) -> float | None:
        if len(self.all_clicks) < MIN_CLICKS_WINDOW:
            return None
        productive = len(self.click_log)
        total      = len(self.all_clicks)
        if total == 0:
            return None
        return round((productive / total) * 100, 1)


# ── Supabase Realtime listener ────────────────────────────────────────────────
class RealtimeListener(QThread):
    """
    Polls unlock_signals table every 2 seconds as a lightweight alternative
    to a full WebSocket realtime subscription.
    For production, replace with supabase-py realtime client.
    """
    unlock_received = pyqtSignal(str, str)  # (action, reason)

    def __init__(self, hwid: str):
        super().__init__()
        self.hwid    = hwid
        self.running = True
        self.headers = {
            "apikey":        SUPABASE_ANON,
            "Authorization": f"Bearer {SUPABASE_ANON}",
        }

    def run(self):
        url = (
            f"{SUPABASE_URL}/rest/v1/unlock_signals"
            f"?pc_hwid=eq.{self.hwid}&consumed=eq.false"
            f"&order=created_at.desc&limit=1"
        )
        while self.running:
            try:
                resp = requests.get(url, headers=self.headers, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    if data:
                        signal = data[0]
                        self.unlock_received.emit(
                            signal["action"],
                            signal.get("reason", "")
                        )
                        # Mark consumed
                        requests.patch(
                            f"{SUPABASE_URL}/rest/v1/unlock_signals?id=eq.{signal['id']}",
                            headers={**self.headers, "Content-Type": "application/json"},
                            json={"consumed": True},
                            timeout=5
                        )
            except Exception as e:
                logging.warning(f"Realtime poll error: {e}")
            time.sleep(2)

    def stop(self):
        self.running = False


# ══════════════════════════════════════════════════════════════════════════════
# LOCK SCREEN WIDGET
# ══════════════════════════════════════════════════════════════════════════════

class LockScreen(QWidget):
    auth_success = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._setup_window()
        self._build_ui()
        self._apply_style()

    def _setup_window(self):
        self.setWindowTitle(APP_TITLE)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.setContentsMargins(0, 0, 0, 0)

        card = QFrame()
        card.setObjectName("card")
        card.setFixedSize(420, 480)
        card_layout = QVBoxLayout(card)
        card_layout.setSpacing(18)
        card_layout.setContentsMargins(48, 48, 48, 48)

        # Icon / logo
        icon_lbl = QLabel("🔒")
        icon_lbl.setObjectName("icon")
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(icon_lbl)

        # Title
        title = QLabel("Work Agent")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(title)

        subtitle = QLabel("Enter your Telegram username & PIN to unlock")
        subtitle.setObjectName("subtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setWordWrap(True)
        card_layout.addWidget(subtitle)

        card_layout.addSpacing(8)

        # Username field
        self.username_field = QLineEdit()
        self.username_field.setPlaceholderText("@your_telegram_username")
        self.username_field.setObjectName("field")
        card_layout.addWidget(self.username_field)

        # PIN field
        self.pin_field = QLineEdit()
        self.pin_field.setPlaceholderText("6-digit PIN")
        self.pin_field.setEchoMode(QLineEdit.EchoMode.Password)
        self.pin_field.setObjectName("field")
        self.pin_field.setMaxLength(6)
        self.pin_field.returnPressed.connect(self._attempt_auth)
        card_layout.addWidget(self.pin_field)

        # Status label
        self.status_lbl = QLabel("")
        self.status_lbl.setObjectName("status")
        self.status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_lbl.setWordWrap(True)
        card_layout.addWidget(self.status_lbl)

        # Unlock button
        self.unlock_btn = QPushButton("Unlock")
        self.unlock_btn.setObjectName("unlock_btn")
        self.unlock_btn.clicked.connect(self._attempt_auth)
        card_layout.addWidget(self.unlock_btn)

        # HWID info (small)
        hwid_lbl = QLabel(f"PC: {HWID[:12]}…")
        hwid_lbl.setObjectName("hwid")
        hwid_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(hwid_lbl)

        root.addWidget(card)

    def _apply_style(self):
        self.setStyleSheet("""
            LockScreen {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #0f0f1a, stop:1 #1a1a2e
                );
            }
            QFrame#card {
                background: rgba(255,255,255,0.05);
                border: 1px solid rgba(255,255,255,0.12);
                border-radius: 20px;
            }
            QLabel#icon {
                font-size: 48px;
            }
            QLabel#title {
                font-size: 26px;
                font-weight: 700;
                color: #e8e8f0;
                font-family: 'Segoe UI', sans-serif;
            }
            QLabel#subtitle {
                font-size: 13px;
                color: rgba(255,255,255,0.5);
                font-family: 'Segoe UI', sans-serif;
            }
            QLineEdit#field {
                background: rgba(255,255,255,0.08);
                border: 1px solid rgba(255,255,255,0.15);
                border-radius: 10px;
                color: #e8e8f0;
                font-size: 15px;
                padding: 12px 16px;
                font-family: 'Segoe UI', sans-serif;
            }
            QLineEdit#field:focus {
                border: 1px solid rgba(120,180,255,0.6);
                background: rgba(255,255,255,0.12);
            }
            QPushButton#unlock_btn {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #4f8ef7, stop:1 #7b5ea7
                );
                color: white;
                border: none;
                border-radius: 10px;
                font-size: 15px;
                font-weight: 600;
                padding: 13px;
                font-family: 'Segoe UI', sans-serif;
            }
            QPushButton#unlock_btn:hover {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:0,
                    stop:0 #5f9ef7, stop:1 #8b6eb7
                );
            }
            QPushButton#unlock_btn:disabled {
                background: rgba(255,255,255,0.1);
                color: rgba(255,255,255,0.3);
            }
            QLabel#status {
                font-size: 13px;
                color: #f09050;
                font-family: 'Segoe UI', sans-serif;
                min-height: 36px;
            }
            QLabel#hwid {
                font-size: 11px;
                color: rgba(255,255,255,0.2);
                font-family: 'Consolas', monospace;
            }
        """)

    def _attempt_auth(self):
        username = self.username_field.text().strip()
        pin      = self.pin_field.text().strip()

        if not username:
            self.show_status("Please enter your Telegram username.", error=True)
            return
        if len(pin) != 6 or not pin.isdigit():
            self.show_status("PIN must be exactly 6 digits.", error=True)
            return

        self.unlock_btn.setEnabled(False)
        self.show_status("⏳ Verifying with supervisor…")

        # Run auth in background thread
        t = threading.Thread(target=self._do_auth, args=(username, pin), daemon=True)
        t.start()

    def _do_auth(self, username: str, pin: str):
        try:
            resp = requests.post(
                BOT_AUTH_URL,
                json={"username": username, "hwid": HWID, "pin": pin},
                timeout=15
            )
            data = resp.json()
            granted = data.get("granted", False)
            reason  = data.get("reason", "Unknown error")

            # Marshal back to main thread
            QTimer.singleShot(0, lambda: self._on_auth_result(granted, reason))
        except requests.exceptions.ConnectionError:
            QTimer.singleShot(0, lambda: self._on_auth_result(False, "Cannot reach server. Check your connection."))
        except Exception as e:
            QTimer.singleShot(0, lambda: self._on_auth_result(False, f"Error: {e}"))

    def _on_auth_result(self, granted: bool, reason: str):
        self.unlock_btn.setEnabled(True)
        if granted:
            self.show_status("✅ Access granted! Loading workspace…", error=False)
            QTimer.singleShot(1500, self.auth_success.emit)
        else:
            self.show_status(f"❌ {reason}", error=True)
            self.pin_field.clear()
            self.pin_field.setFocus()

    def show_status(self, msg: str, error: bool = False):
        color = "#f09050" if error else "#60c060"
        self.status_lbl.setStyleSheet(f"color: {color}; font-size: 13px; font-family: 'Segoe UI';")
        self.status_lbl.setText(msg)

    def keyPressEvent(self, event):
        # Block Alt+F4, Alt+Tab etc at widget level
        blocked = [
            Qt.Key.Key_Escape,
            Qt.Key.Key_F4,
            Qt.Key.Key_Tab,
        ]
        if event.key() in blocked:
            return
        super().keyPressEvent(event)


# ══════════════════════════════════════════════════════════════════════════════
# NUDGE OVERLAY — shown when rolling score drops below threshold
# ══════════════════════════════════════════════════════════════════════════════

class NudgeOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool |
            Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(380, 100)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)

        self.msg = QLabel("⚠️  Your activity score is low. Keep up the pace!")
        self.msg.setStyleSheet("""
            color: #fff3cd;
            font-size: 14px;
            font-weight: 600;
            font-family: 'Segoe UI', sans-serif;
        """)
        self.msg.setWordWrap(True)
        layout.addWidget(self.msg)

        self.setStyleSheet("""
            NudgeOverlay {
                background: rgba(180, 100, 20, 0.92);
                border-radius: 12px;
                border: 1px solid rgba(255,200,80,0.5);
            }
        """)

        # Auto-hide after 8 seconds
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_nudge(self, score: float):
        self.msg.setText(f"⚠️  Activity score: {score:.0f}% — below the 80% target. Pick up the pace!")
        screen = QApplication.primaryScreen().geometry()
        self.move(screen.width() - 400, screen.height() - 130)
        self.show()
        self._timer.start(8000)

        # Play system sound
        try:
            import winsound
            winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS | winsound.SND_ASYNC)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════════
# MAIN APPLICATION CONTROLLER
# ══════════════════════════════════════════════════════════════════════════════

class WorkAgent(QObject):
    def __init__(self, app: QApplication):
        super().__init__()
        self.app           = app
        self.lock_screen   = LockScreen()
        self.nudge_overlay = NudgeOverlay()
        self.click_monitor = ClickMonitor()
        self.rt_listener   = RealtimeListener(HWID)
        self._shift_active = False

        # Connect signals
        self.lock_screen.auth_success.connect(self._on_auth_success)
        self.click_monitor.nudge_triggered.connect(self._on_nudge)
        self.click_monitor.screenshot_taken.connect(self._on_screenshot)
        self.rt_listener.unlock_received.connect(self._on_realtime_signal)

        # Start realtime listener
        self.rt_listener.start()

        # Show lock screen
        self.lock_screen.showFullScreen()

    def _on_auth_success(self):
        """Worker authenticated — hide lock screen, start monitoring."""
        self._shift_active = True
        self.lock_screen.hide()
        self.click_monitor.start()

        # Open work URL in default browser
        import webbrowser
        webbrowser.open(WORK_URL)

        logging.info("Shift started — monitoring active")

    def _on_nudge(self, score: float):
        if self._shift_active:
            self.nudge_overlay.show_nudge(score)

    def _on_screenshot(self, path: str, trigger: str):
        logging.info(f"Screenshot saved [{trigger}]: {path}")

    def _on_realtime_signal(self, action: str, reason: str):
        if action == "unlock" and not self._shift_active:
            # Bot-triggered unlock (e.g. manager override)
            self.lock_screen.show_status("✅ Unlocked by supervisor", error=False)
            QTimer.singleShot(1000, self._on_auth_success)

        elif action in ("lock", "deny"):
            # Shift ended or denied — show lock screen
            self._shift_active = False
            self.click_monitor.stop()
            self.lock_screen.show_status(
                f"🔒 {reason or 'Session ended by supervisor'}",
                error=True
            )
            self.lock_screen.showFullScreen()
            logging.info(f"Lock signal received: {action} — {reason}")

    def cleanup(self):
        self.click_monitor.stop()
        self.rt_listener.stop()
        self.rt_listener.wait(3000)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(
                Path(os.environ.get("APPDATA", ".")) / "WorkAgent" / "client.log"
            ),
            logging.StreamHandler()
        ]
    )

    # Create log directory
    (Path(os.environ.get("APPDATA", ".")) / "WorkAgent").mkdir(parents=True, exist_ok=True)

    # Register startup
    register_startup()

    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setQuitOnLastWindowClosed(False)

    agent = WorkAgent(app)

    # Ensure cleanup on exit
    app.aboutToQuit.connect(agent.cleanup)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
