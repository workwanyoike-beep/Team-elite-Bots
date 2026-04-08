# AUTO-SUPERVISOR WORKFORCE ECOSYSTEM
## Complete Setup & Deployment Guide

---

## ARCHITECTURE OVERVIEW

```
┌─────────────────────────────────────────────────────────────┐
│                     SUPABASE (PostgreSQL)                   │
│  workers · pcs · shifts · performance_logs · unlock_signals  │
└──────────┬───────────────────────────┬──────────────────────┘
           │                           │
    ┌──────▼──────┐              ┌─────▼──────┐
    │  Telegram   │              │  Next.js   │
    │  Bot        │◄────────────►│  Portal    │
    │  (Python)   │  Realtime    │  (Worker   │
    │  + HTTP :8765│             │   Dashboard│
    └──────┬──────┘              └────────────┘
           │ WebSocket/Poll
    ┌──────▼──────┐
    │  Desktop    │
    │  Client     │
    │  (PyQt6 EXE)│
    │  12x PCs    │
    └─────────────┘
```

---

## STEP 1 — SUPABASE DATABASE

1. Create a free project at https://supabase.com
2. Go to **SQL Editor** → paste the full contents of `database/schema.sql`
3. Click **Run**
4. Save your credentials:
   - `SUPABASE_URL` = Project URL (Settings → API)
   - `SUPABASE_ANON_KEY` = anon/public key
   - `SUPABASE_SERVICE_KEY` = service_role key (keep secret!)

---

## STEP 2 — TELEGRAM BOT

### 2a. Create the bot
1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow prompts
3. Copy the **BOT_TOKEN**
4. Get your own Telegram ID: message [@userinfobot](https://t.me/userinfobot)

### 2b. Deploy the bot (Replit / VPS / Railway)

**Option A — Replit (easiest):**
1. Create a new Replit (Python template)
2. Upload `bot/bot.py` and `bot/requirements.txt`
3. Add Secrets:
   ```
   TELEGRAM_BOT_TOKEN = your-bot-token
   MANAGER_CHAT_ID    = your-telegram-id
   SUPABASE_URL       = https://xxx.supabase.co
   SUPABASE_SERVICE_KEY = your-service-key
   ```
4. Click Run. The bot starts polling + HTTP server on :8765.

**Option B — VPS (DigitalOcean / Hetzner):**
```bash
git clone your-repo && cd workforce-system/bot
pip install -r requirements.txt
cp .env.example .env  # fill in values

# Run with systemd or screen:
screen -S workerbot
python bot.py
# Ctrl+A, D to detach
```

**Configure firewall — CRITICAL:**
```bash
# Allow port 8765 ONLY from your office IP range
# (Desktop clients call this port for authentication)
ufw allow from YOUR_OFFICE_IP to any port 8765
ufw deny 8765
```

### 2c. Register your PCs
Once bot is running, message it:
```
/addpc ABC123XYZ456 PC-01
/addpc DEF456ABC789 PC-02
# ... repeat for all 12 PCs
```

---

## STEP 3 — DESKTOP CLIENT (Windows EXE)

### 3a. Configure before compiling
Edit these lines in `desktop-client/client.py`:
```python
BOT_AUTH_URL  = "http://YOUR-BOT-SERVER:8765/auth"
SUPABASE_URL  = "https://your-project.supabase.co"
SUPABASE_ANON = "your-anon-key"
WORK_URL      = "https://your-work-website.com"
```

### 3b. Find each PC's HWID
Run this on each PC in PowerShell:
```powershell
wmic csproduct get uuid
# Copy the UUID — this is the HWID for /addpc
```

### 3c. Compile to EXE
On a Windows machine with Python 3.11+:
```cmd
pip install pyqt6 mss pynput requests supabase nuitka aiohttp bcrypt

nuitka --onefile --windowed ^
  --include-qt-plugins=sensible ^
  --windows-uac-uiaccess=true ^
  --windows-product-name="WorkAgent" ^
  --output-filename="WorkAgent.exe" ^
  client.py
```
Expected size: 80–140 MB.

### 3d. Deploy to all 12 PCs
1. Copy `WorkAgent.exe` to each PC
2. Place in `C:\WorkAgent\WorkAgent.exe`
3. The app registers itself in Windows startup automatically on first run
4. Alternatively, add to startup manually:
   ```
   Windows key → "Run" → shell:startup
   Create shortcut to WorkAgent.exe
   ```

### 3e. Button coordinate calibration
The click monitor uses screen coordinates to detect Login/Send button clicks.
Edit `BUTTON_ZONES` in `client.py` to match your work website:
```python
BUTTON_ZONES = {
    "login": (860, 450, 200, 50),   # x, y, width, height at 1920×1080
    "send":  (1700, 980, 180, 40),
}
```
To find coordinates: use the Windows Snipping Tool or run this in Python:
```python
import pyautogui; print(pyautogui.position())
# Move mouse to button, read coordinates
```

---

## STEP 4 — WORKER PORTAL (Next.js)

### 4a. Quick deploy with the HTML file
The `portal/index.html` is a self-contained demo. To serve it:
```bash
# Python (quick test)
cd portal && python -m http.server 3000

# Or upload to Netlify/Vercel for free hosting
```

### 4b. Full Next.js setup (production)
```bash
npx create-next-app@latest worker-portal
cd worker-portal
npm install @supabase/supabase-js next-auth

# Add Supabase + Telegram OAuth
# Protect routes with RLS (already configured in schema.sql)
```

Key Next.js pages to build:
- `/` → redirect to `/dashboard` if logged in, else login
- `/api/auth/[...nextauth]` → NextAuth with Telegram provider
- `/dashboard` → the HTML portal logic, server-rendered
- `/api/worker/payment` → PATCH endpoint for M-Pesa details

### Telegram OAuth for Next.js
```javascript
// pages/api/auth/[...nextauth].js
import NextAuth from "next-auth"
export default NextAuth({
  providers: [{
    id: "telegram",
    name: "Telegram",
    type: "oauth",
    // Use telegram-passport or bot-initiated auth flow
    // See: https://core.telegram.org/widgets/login
  }]
})
```

---

## WORKER DAILY WORKFLOW

```
1. Worker sits at PC
   → WorkAgent lock screen is showing

2. Worker enters:
   - Telegram username: @alice
   - 6-digit PIN (assigned by manager per shift, or auto-generated)

3. Bot checks:
   ✓ Is @alice registered?
   ✓ Is this PC registered and vacant?
   ✓ Was @alice's last shift score ≥ 85%?

4. If all pass:
   → Unlock signal sent to PC via Supabase Realtime
   → Work website opens automatically
   → Shift starts in database

5. During shift:
   → Worker sends /stats 120 98 in Telegram group at intervals
   → Bot calculates rolling score

6. End of shift:
   → Worker sends /endshift
   → Bot marks PC as vacant
   → Desktop client re-locks the screen
   → Final score saved to performance_logs
```

---

## MANAGER COMMANDS

| Command | Description |
|---------|-------------|
| `/addpc HWID Label` | Register a new PC |
| `/grant @worker HWID` | Manually unlock a PC |
| `/status` | Full system overview (all PCs, active shifts) |

---

## ENVIRONMENT VARIABLES REFERENCE

### Bot (.env)
```env
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHI...
MANAGER_CHAT_ID=987654321
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_SERVICE_KEY=eyJhbGc...  # service_role key — KEEP SECRET
```

### Desktop Client (compiled in, or via env)
```env
BOT_AUTH_URL=http://your-server:8765/auth
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_ANON=eyJhbGc...  # anon/public key — safe to embed
WORK_URL=https://your-work-site.com
```

### Portal (Next.js .env.local)
```env
NEXT_PUBLIC_SUPABASE_URL=https://xxxxx.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=eyJhbGc...
NEXTAUTH_SECRET=random-secret-string
TELEGRAM_BOT_TOKEN=1234567890:ABCdefGHI...
```

---

## SECURITY NOTES

1. **Service key** — never expose `SUPABASE_SERVICE_KEY` in the browser or desktop client.
   Only the bot server uses it. The desktop client uses the anon key.

2. **Port 8765** — the bot's auth endpoint. Firewall it to only your office IPs.
   Workers' PCs must be able to reach the bot server.

3. **RLS** — Row Level Security is enabled on all tables. Workers can only
   read their own data. The service key bypasses RLS (used only by the bot).

4. **PIN security** — PINs are bcrypt-hashed before storage. The 6-digit PIN
   is a convenience auth layer — the real security is the Telegram identity check.

5. **Screenshot storage** — screenshots are saved locally on each PC in
   `%APPDATA%\WorkAgent\screenshots\`. Consider adding a periodic upload to
   Supabase Storage if you need remote access.

---

## TROUBLESHOOTING

**Lock screen can be bypassed with Task Manager**
→ Use Group Policy to disable Task Manager for standard users:
  `gpedit.msc → User Config → Admin Templates → System → Ctrl+Alt+Del Options`

**Desktop client won't stay on top**
→ Ensure compiled with `--windows-uac-uiaccess=true`
→ Run WorkAgent.exe as Administrator

**Bot not responding**
→ Check `TELEGRAM_BOT_TOKEN` is correct
→ Ensure bot is added to the worker group with admin permissions

**PC not unlocking**
→ Check `BOT_AUTH_URL` is reachable from the PC
→ Verify the HWID matches what was registered with `/addpc`
→ Check Supabase `unlock_signals` table for the signal

**Score not calculating**
→ Worker must send `/stats` TWICE: once at shift start, once at shift end
→ The first report sets baseline; the second calculates the delta
