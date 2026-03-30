# Newsletter Podcast Agent

Fetches your Hotmail newsletters every morning, converts them to audio using Google TTS, and serves them as a private podcast you can play on your iPhone — completely hands-free while driving.

---

## How it works

```
Hotmail → agent.py → Google TTS → MP3 files + RSS feed → serve.py → iPhone Podcasts app
```

---

## One-time setup

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Set up Microsoft Azure (to read your Hotmail)

1. Go to [portal.azure.com](https://portal.azure.com) and sign in with your Microsoft account
2. Search for **"App registrations"** → click **New registration**
3. Name it anything (e.g. `newsletter-agent`), leave defaults, click **Register**
4. Copy the **Application (client) ID** → this is your `ms_client_id`
5. Copy the **Directory (tenant) ID** → this is your `ms_tenant_id`
6. Go to **Certificates & secrets** → **New client secret** → copy the value → `ms_client_secret`
7. Go to **API permissions** → **Add a permission** → **Microsoft Graph** → **Application permissions**
8. Search and add: `Mail.Read`
9. Click **Grant admin consent**

### 3. Set up Google Cloud TTS (to convert text to audio)

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (e.g. `newsletter-podcast`)
3. Search for **"Text-to-Speech API"** → Enable it
4. Go to **APIs & Services** → **Credentials** → **Create credentials** → **Service account**
5. Name it anything, click **Done**
6. Click the service account → **Keys** → **Add key** → **JSON** → download the file
7. Set the environment variable pointing to it:

```bash
# Mac/Linux — add this to your ~/.zshrc or ~/.bashrc
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/your/keyfile.json"

# Windows (PowerShell)
$env:GOOGLE_APPLICATION_CREDENTIALS="C:\path\to\your\keyfile.json"
```

### 4. Configure the agent

```bash
cp config.example.json config.json
```

Edit `config.json`:
- Fill in your `hotmail_address`, Azure credentials
- Add any newsletter sender domains you subscribe from
- Update `base_url` with your computer's local IP (see below)

**Finding your local IP:**
```bash
# Mac
ipconfig getifaddr en0

# Windows
ipconfig  # look for IPv4 Address under your WiFi adapter
```

Set `base_url` to e.g. `http://192.168.1.42:8080`

---

## Running the agent

### Start the podcast server (keep this running)

```bash
python serve.py
```

It will print out your podcast feed URL. Add this URL to your iPhone once (see below).

### Run the agent manually (first test)

```bash
python agent.py
```

You should see it fetch newsletters, convert them to MP3, and write the RSS feed.

### Schedule it to run every morning automatically

**Mac (cron):**
```bash
crontab -e
```
Add this line to run at 6 AM daily:
```
0 6 * * * cd /path/to/newsletter-podcast && /usr/bin/python3 agent.py >> logs/cron.log 2>&1
```

**Windows (Task Scheduler):**
1. Open Task Scheduler → Create Basic Task
2. Name: `Newsletter Podcast Agent`
3. Trigger: Daily at 6:00 AM
4. Action: Start a program → `python.exe`
5. Arguments: `C:\path\to\newsletter-podcast\agent.py`

---

## Add to iPhone Podcasts app

1. Open the **Podcasts** app on your iPhone
2. Tap **Search** → then tap the search bar
3. Scroll down to find **"Add a Show by URL"** (or in Library → tap `...` → Add a Show by URL)
4. Paste your feed URL: `http://YOUR_LOCAL_IP:8080/feed/podcast.xml`
5. Tap **Subscribe**

> ⚠️ Your iPhone must be on the same WiFi network as your computer to access the feed. This is perfect for home — just make sure the server is running before you leave.

---

## Daily workflow

1. Wake up — newsletters have already been fetched and converted at 6 AM
2. Open Podcasts app → your feed is ready
3. Get in the car → press play
4. Put the phone down — it plays through all newsletters hands-free

---

## Folder structure

```
newsletter-podcast/
├── agent.py          # Main agent — fetches emails and generates audio
├── serve.py          # HTTP server — serves feed and audio to iPhone
├── config.json       # Your settings (gitignored)
├── config.example.json
├── requirements.txt
├── processed.json    # Tracks which emails have been processed
├── audio/            # Generated MP3 files
├── feed/
│   └── podcast.xml   # RSS feed your iPhone subscribes to
└── logs/
    └── agent.log
```

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `No new newsletters found` | Check your `newsletter_domains` / `newsletter_keywords` in config.json |
| Azure auth error | Double-check client ID/secret/tenant, and that Mail.Read permission has admin consent |
| Google TTS error | Make sure `GOOGLE_APPLICATION_CREDENTIALS` env var is set correctly |
| iPhone can't reach feed | Ensure your computer and iPhone are on the same WiFi; check your `base_url` IP |
| Audio sounds cut off | Newsletter was too long — chunking handles this automatically |

---

## Portfolio notes

This project demonstrates:
- **OAuth2 / MSAL** — authenticating with Microsoft identity platform
- **Microsoft Graph API** — reading mailbox data via REST
- **Google Cloud TTS API** — converting text to natural-sounding audio
- **RSS/Podcast feed generation** — producing a standards-compliant podcast feed
- **HTML parsing & cleaning** — extracting readable content from email HTML
- **Task scheduling** — cron / Windows Task Scheduler automation
- **Python HTTP server** — serving static files locally

---

## Free tier limits

| Service | Free allowance | Typical usage |
|---|---|---|
| Microsoft Graph | Unlimited for personal use | — |
| Google Cloud TTS | 1,000,000 characters/month | ~5 newsletters/day ≈ 150,000 chars/month |

You are very unlikely to exceed the Google TTS free tier with personal newsletter usage.
