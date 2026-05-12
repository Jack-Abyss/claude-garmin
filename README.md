# claude-garmin

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-lightgrey.svg)](#)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io/)

> **Garmin Connect for Claude Desktop** — an MCP connector that brings your Garmin data into Claude, so you can chat about your training and get personalized plans, recovery checks, and session analyses grounded in your **real** numbers.

Once installed, you can ask things like:

> *"How's my recovery today? Looking at my HRV and training readiness, should I push hard?"*
>
> *"Plan a 4-week build for a Sprint triathlon 6 weeks out, using my current swim/bike/run volume as the baseline."*
>
> *"I've got an Achilles flare-up and only my stationary bike. Based on my recent load, structure a recovery week that doesn't kill my fitness."*

Claude reaches for the 8 tools the connector exposes (recovery, fitness, load, activities, running dynamics, training load, stress, personal records) and answers using your actual numbers.

> 💡 **First-time setup tip** — after install, paste the [Custom Instructions snippet](#getting-the-most-out-of-the-connector) into Claude Desktop **once**. With it set, even casual questions like *"how do I recover this week?"* will automatically pull your real data instead of giving generic advice. Without it, you need to anchor each question to your data (e.g. *"based on my..."*, *"looking at my..."*).

---

## Lifecycle — it does NOT run 24/7

The connector is **not a Windows service**, **doesn't start at boot**, **doesn't stay resident**:

- Claude Desktop spawns it as a **child process** only when the app is open
- While idle (no tool calls), footprint is ~10 MB RAM, 0% CPU — heavy imports (`garminconnect`, `curl_cffi`) are lazy and only load on the first tool call
- When you close Claude Desktop, the server process exits automatically
- No network ports opened: it speaks stdio with Claude Desktop only

In short: if you don't open Claude Desktop, nothing is running.

---

## Quick install (automated script)

1. Download the zip from the **[Releases](https://github.com/Jack-Abyss/claude-garmin/releases)** page (or `git clone`) and extract it wherever you like.
2. **Double-click `install.bat`**.
3. The script will:
   - Install Python 3.12 if missing (via winget)
   - Install Claude Desktop if missing (via winget)
   - Create the `.venv` virtualenv and install dependencies
   - Ask for your **Garmin email and password** (stored only in the local `.env`, never sent anywhere else)
   - Ask whether **MFA (2FA)** is enabled: if yes, it warns you to have your authenticator app ready before triggering the login prompt
   - Run a one-time Garmin login (caches the OAuth token in `~/.garminconnect/`, auto-refreshed for months)
   - Detect whether you have Claude Desktop **Microsoft Store** or **classic** and write the config to the correct path (smart merge if you already have other MCP servers configured)
4. Open Claude Desktop and try: *"How's my recovery today?"*

To re-configure later (change password, re-login):
```powershell
.\install.bat -Reconfigure
```

To **uninstall** cleanly (removes Claude entry, tokens, .venv, .env — leaves Python and Claude Desktop in place):
```powershell
.\uninstall.bat
```

If something fails during install, take a screenshot of the output. Prefer to do every step by hand? Follow the manual guide below.

---

## Manual install (Windows)

> ⏱️ Total time: ~10 minutes the first time.
>
> All commands run in **PowerShell**. Open it via Start menu → type "PowerShell" → Enter.

### 1. Install Python 3.12

Check if it's already installed:

```powershell
python --version
```

If you see `Python 3.12.x` or `3.13.x`, jump to step 2. If you see an error or it redirects you to the Microsoft Store, install via winget:

```powershell
winget install --id Python.Python.3.12 --silent --accept-source-agreements --accept-package-agreements
```

Then **close and reopen PowerShell** (to reload the PATH) and verify:

```powershell
python --version
```

### 2. Install Claude Desktop

```powershell
winget install --id Anthropic.Claude --accept-source-agreements --accept-package-agreements
```

Open Claude Desktop at least once (search "Claude" in the Start menu), sign in with your Anthropic account, then **close it completely** (also from the tray icon, bottom right). You'll reopen it after configuring the connector.

### 3. Get the connector

If you have `git`:

```powershell
cd $HOME\Desktop
git clone https://github.com/Jack-Abyss/claude-garmin.git
cd claude-garmin
```

Otherwise download the zip from GitHub, extract it wherever you like, and `cd` into it in PowerShell:

```powershell
cd "C:\path\to\claude-garmin"
```

### 4. Install dependencies

Create the virtualenv and install the libraries:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> If PowerShell blocks venv activation with an execution policy error, run once:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```
> Then try activating again.

### 5. Set Garmin credentials

```powershell
Copy-Item .env.example .env
notepad .env
```

Replace the placeholder lines with your real Garmin Connect email and password:

```
GARMIN_EMAIL=you@email.com
GARMIN_PASSWORD=yourpassword
```

Save and close notepad.

> Your credentials stay **on your machine**. The `.env` file is git-ignored. The library authenticates directly with `sso.garmin.com` and saves an OAuth token under `~/.garminconnect/` that auto-refreshes. The password is only needed for the first login.

### 6. One-time Garmin login

```powershell
python garmin_mcp.py login
```

Expected output:

```
Garmin Connect login...
OK. Tokens saved in C:\Users\<you>\.garminconnect
```

If you have **MFA enabled** on your Garmin account, you'll be asked for the code here — only this once. From then on tokens refresh automatically and you won't need to log in again (typically for months).

### 7. (Optional but recommended) Verify the data flow

Before wiring up Claude Desktop, run a direct test that calls all 8 tools and prints the JSON they return:

```powershell
python test_tools.py
```

If you see reasonable numbers (training readiness, HRV, recent activities, etc.) you're good. If you see lots of `null` values for HRV / training readiness / body battery, that's normal for older devices that don't support those metrics.

Test a single tool:

```powershell
python test_tools.py recovery
python test_tools.py fitness
python test_tools.py activities
```

### 8. Wire it to Claude Desktop

Create (or update) the Claude Desktop config file:

```powershell
$cfg = "$env:APPDATA\Claude\claude_desktop_config.json"
New-Item -ItemType Directory -Force -Path (Split-Path $cfg) | Out-Null

$projDir = (Get-Location).Path -replace '\\','/'
$pythonExe = "$projDir/.venv/Scripts/python.exe"
$mcpScript = "$projDir/garmin_mcp.py"

@"
{
  "mcpServers": {
    "garmin": {
      "command": "$pythonExe",
      "args": ["$mcpScript"]
    }
  }
}
"@ | Set-Content -Path $cfg -Encoding utf8

Write-Host "Config written to: $cfg"
Get-Content $cfg
```

> If you already had a `claude_desktop_config.json` with other MCP servers, **don't** run the block above (it would overwrite the file). Open it manually with `notepad $env:APPDATA\Claude\claude_desktop_config.json` and add only the `"garmin"` key under `mcpServers`.

### 9. Restart Claude Desktop and try it

1. Open Claude Desktop from the Start menu.
2. Start a new chat.
3. Look under **Connectors** (or **Tools**, depending on UI version) — you should see **8 tools** under **garmin**.
4. Type a test question:

   > How's my recovery today?

5. Claude will ask permission to use the `get_recovery` tool the first time — approve it. You'll get a reply grounded in your real numbers.

---

## Available tools

Claude calls them automatically as needed — you don't have to memorize them, but it's useful to know what's available:

| Tool | Returns |
|---|---|
| `get_recovery` | training readiness, HRV (7 days), sleep (7 days), body battery, resting HR, training status |
| `get_fitness` | VO2max running/cycling, cycling FTP, race predictions (5K/10K/HM/marathon) |
| `get_recent_load(days=28)` | per-sport aggregates: sessions, km, minutes, average HR |
| `get_activities(days=14)` | multisport activity list with details (pace, power, HR, duration) |
| `get_running_dynamics(activity_id)` | cadence, ground contact, vertical oscillation, stride for one run |
| `get_training_load` | acute / chronic load, ACWR ratio, base/tempo/threshold/anaerobic breakdown |
| `get_stress_data(days=7)` | daily stress (rest/low/medium/high minutes) |
| `get_personal_records` | PRs by sport (best times across distances, longest, power records) |

---

## Getting the most out of the connector

Claude decides on its own when to call MCP tools based on the question. If a question can be answered with general knowledge, it skips the tool call — which is usually a sensible default but means you can miss the point of *this* connector. A question like *"How should I recover this week?"* sounds personal but doesn't explicitly reference your data, so Claude often replies with generic recovery advice without ever opening Garmin.

Two ways to fix that.

### Option A — Phrase questions so Claude pulls your real data

Anchor the question to **your** data. The magic words: *"my recent / my current / based on my / looking at my / from my data"*.

| Won't trigger the connector | Will trigger the connector |
|---|---|
| *"How should I recover this week?"* | *"Based on my current training load and recovery, how should I structure this week?"* |
| *"I've got an Achilles flare-up, what should I do?"* | *"Given my recent activity pattern and recovery status, plan a week assuming I can only ride indoor (Achilles flare-up)."* |
| *"What's a good HR zone for easy runs?"* | *"Looking at the HR data from my recent runs, am I running my easy days too hard?"* |
| *"Should I do a long workout today?"* | *"Based on my training readiness and HRV today, what should I do?"* |
| *"Plan me a 4-week build for an Olympic triathlon"* | *"Plan me a 4-week build for an Olympic triathlon, using my current volume across swim/bike/run and my VO2max as the baseline."* |

You can also force it directly: *"Use the garmin tools to check my recovery and recent load, then ..."*. Less elegant but 100% deterministic.

### Option B — Tell Claude once and forget about it (recommended)

In Claude Desktop, open **Settings → Profile → Custom instructions** (or create a [Project](https://support.anthropic.com/en/articles/9519177) and put it in the project's custom instructions if you want it scoped) and paste something like:

```
I'm a multisport athlete (triathlon / running / cycling). The "garmin" MCP
connector is installed and gives you access to my real Garmin Connect data
(recovery, fitness markers, training load, recent activities, running
dynamics, stress, personal records).

Whenever I ask about training, recovery, workouts, planning, performance,
heart rate, pace, intensity zones, or anything related to my fitness,
ALWAYS use the garmin tools first to pull my actual current data before
giving advice. Do not default to generic advice when my real numbers are
one tool call away.

When suggesting workouts or recovery plans, ground them in my recent load,
HRV trend, and training readiness — not in textbook averages.
```

Adapt the persona and constraints (current injuries, target races, time available per week, equipment access) to your situation. From that point on, Claude will reach for the connector automatically and you can ask questions in their natural form.

### Why this matters

The connector exposes 8 tools that are individually cheap to call (~0.5-3s each). The cost of *not* using them is generic advice that ignores your fitness reality — exactly what you wanted to escape by installing this in the first place.

---

## Troubleshooting

**Claude doesn't show the "garmin" connector**
- Did you **fully restart** Claude Desktop? (also close it from the tray icon, or run `Get-Process claude | Stop-Process -Force`)
- Verify the config was written in the correct location. **Store version**:
  ```powershell
  Get-ChildItem "$env:LOCALAPPDATA\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json" | Get-Content
  ```
  **Classic version**:
  ```powershell
  Get-Content "$env:APPDATA\Claude\claude_desktop_config.json"
  ```
- Check the Claude logs (Store version):
  ```powershell
  Get-ChildItem "$env:LOCALAPPDATA\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\logs"
  ```
- If something is wrong: `.\install.bat -Reconfigure` redoes the setup.

**"Garmin login failed... cached tokens are missing or expired"**
- Run `python garmin_mcp.py login` again from PowerShell. Tokens expire every few months or if Garmin invalidates the session.

**Lots of `null` values in `get_recovery` / `get_training_load`**
- Advanced metrics (training readiness, HRV, training load, body battery) require compatible devices: Forerunner 255 / 265 / 955 / 965, Fenix 6 Pro+, Venu 2+, Epix, etc. Older devices don't compute them, so Garmin simply doesn't have the data — the connector returns `null` faithfully.

**`GarminConnectTooManyRequestsError` (429)**
- Garmin rate-limits. Wait 5-10 minutes before retrying. Tools fetch in parallel but moderately — this usually only happens during heavy testing.

**`pip install` fails on `curl_cffi`**
- Make sure you have Python 3.11+ (not 3.9 or earlier). `python --version` should say 3.11.x, 3.12.x, or 3.13.x.

---

## Updating

When new versions are released:

```powershell
cd "C:\path\to\claude-garmin"
git pull
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt --upgrade
```

Then restart Claude Desktop.

---

## Uninstall

Double-click **`uninstall.bat`** (or run `.\uninstall.ps1` in PowerShell).

The script asks for confirmation and then removes:
- the `garmin` entry from `claude_desktop_config.json` (both Store and classic)
- Garmin tokens in `~/.garminconnect/`
- the `.venv` virtualenv
- the `.env` file with your credentials

It **does NOT touch** Python, Claude Desktop, or the project source files.

Optional flags:
```powershell
.\uninstall.bat -KeepTokens   # keep the Garmin tokens
.\uninstall.bat -KeepEnv      # keep the .env credentials
.\uninstall.bat -KeepVenv     # keep the virtualenv
.\uninstall.bat -Force        # skip the confirmation prompt
```

To also remove Python or Claude Desktop:
```powershell
winget uninstall Python.Python.3.12
winget uninstall Anthropic.Claude
```

---

## Tech stack

- Python 3.11+
- [`mcp`](https://github.com/modelcontextprotocol/python-sdk) — official MCP SDK
- [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) — Garmin Connect wrapper (built on `garth`)
- [`curl_cffi`](https://github.com/lexiforest/curl_cffi) — required for TLS fingerprinting against Garmin
- [`python-dotenv`](https://pypi.org/project/python-dotenv/) — `.env` loading

The server runs **only locally** on your PC, over stdio. No Garmin data leaves your machine outside the regular conversation between Claude Desktop (local) and Anthropic's API (when you chat).

---

## Contributing

Issues and pull requests welcome. Areas of interest:
- macOS / Linux support (the installer is Windows-only for now; the Python server itself is cross-platform)
- Additional tools (sleep details, hydration, women's health metrics, swim-specific dynamics)
- Pinning more activity type keys in the swim/bike/run buckets

---

## License

[MIT](LICENSE) — do whatever you want, just don't blame me if Garmin changes their auth flow.

## Acknowledgements

- [cyberjunky/python-garminconnect](https://github.com/cyberjunky/python-garminconnect) for the Garmin Connect wrapper
- [matin/garth](https://github.com/matin/garth) for the underlying SSO + auth implementation
- [Anthropic](https://www.anthropic.com/) for Claude Desktop and the MCP protocol
