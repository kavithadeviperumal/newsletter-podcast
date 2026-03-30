"""
Newsletter Podcast Agent
Fetches newsletters from any configured email provider and converts them
to a podcast RSS feed you can listen to on your iPhone.
"""

import re
import json
import hashlib
import logging
import html2text
from pathlib import Path
from datetime import datetime, timezone
from email.utils import formatdate
from google.cloud import texttospeech
from fetchers import get_fetcher

# ── Config ─────────────────────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).parent
AUDIO_DIR       = BASE_DIR / "audio"
FEED_DIR        = BASE_DIR / "feed"
LOG_DIR         = BASE_DIR / "logs"
CONFIG_FILE     = BASE_DIR / "config.json"
FEED_FILE       = FEED_DIR / "podcast.xml"
PROCESSED_FILE  = BASE_DIR / "processed.json"

AUDIO_DIR.mkdir(exist_ok=True)
FEED_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "agent.log"),
        logging.StreamHandler(),
    ],
)
# Suppress noisy debug output from third-party libraries
logging.getLogger("msal").setLevel(logging.WARNING)
logging.getLogger("google").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


# ── Load config ────────────────────────────────────────────────────────────────
def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(
            f"config.json not found. Copy config.example.json to config.json and fill in your values."
        )
    with open(CONFIG_FILE) as f:
        return json.load(f)


# ── Email fetching is handled by fetchers.py ──────────────────────────────────
# Use get_fetcher(cfg) to get the right provider based on config.json
# Supported: "outlook", "gmail"


# ── Clean email HTML → plain text ─────────────────────────────────────────────
def html_to_text(html: str) -> str:
    converter = html2text.HTML2Text()
    converter.ignore_links  = True
    converter.ignore_images = True
    converter.ignore_tables = True   # tables sound awful when read aloud
    converter.body_width    = 0      # don't wrap lines
    text = converter.handle(html)

    # Remove markdown table remnants (lines starting with | )
    text = re.sub(r"^\|.*", "", text, flags=re.MULTILINE)

    # Remove markdown bold/italic markers
    text = re.sub(r"\*{1,3}(.*?)\*{1,3}", r"\1", text)

    # Remove markdown headings (#, ##, ###)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)

    # Remove excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Remove common newsletter footer noise
    noise_patterns = [
        r"unsubscribe.*",
        r"view in browser.*",
        r"you('re| are) receiving this.*",
        r"copyright ©.*",
        r"all rights reserved.*",
        r"\*\*\*.*\*\*\*",
    ]
    for pattern in noise_patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.MULTILINE)

    # Add period after each line that doesn't already end with punctuation
    # This prevents Google TTS from treating entire paragraphs as one sentence
    lines = text.split("\n")
    fixed = []
    for line in lines:
        line = line.strip()
        if not line:
            fixed.append("")
            continue
        if line and line[-1] not in ".!?,;:":
            line = line + "."
        fixed.append(line)
    text = "\n".join(fixed)

    return text.strip()


# ── Google TTS: convert text → MP3 ────────────────────────────────────────────
def text_to_audio(text: str, output_path: Path, cfg: dict) -> bool:
    """Convert text to MP3 using Google Cloud TTS. Returns True on success."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    client        = texttospeech.TextToSpeechClient()
    voice_name    = cfg.get("tts_voice", "en-US-Journey-F")
    language_code = voice_name[:5]

    chunks = chunk_text(text, max_bytes=MAX_CHUNK_BYTES)
    log.info(f"    Synthesizing {len(chunks)} chunks in parallel...")

    def synthesize_chunk(args):
        import time
        index, chunk = args
        synthesis_input = texttospeech.SynthesisInput(text=chunk)
        voice = texttospeech.VoiceSelectionParams(
            language_code=language_code,
            name=voice_name,
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=cfg.get("speaking_rate", 1.1),
        )
        # Retry up to 3 times on transient server errors
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = client.synthesize_speech(
                    input=synthesis_input, voice=voice, audio_config=audio_config
                )
                log.info(f"    Chunk {index + 1}/{len(chunks)} done ({blen(chunk)} bytes)")
                return index, response.audio_content
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = 2 ** attempt  # 1s, 2s, 4s exponential backoff
                    log.warning(f"    Chunk {index + 1} failed (attempt {attempt + 1}), retrying in {wait}s... {e}")
                    time.sleep(wait)
                else:
                    log.error(f"    Chunk {index + 1} failed after {max_retries} attempts.")
                    raise

    # Submit all chunks in parallel — preserve order using index
    audio_chunks = [None] * len(chunks)
    with ThreadPoolExecutor(max_workers=min(len(chunks), 5)) as executor:
        futures = {executor.submit(synthesize_chunk, (i, c)): i for i, c in enumerate(chunks)}
        for future in as_completed(futures):
            index, audio = future.result()
            audio_chunks[index] = audio

    # Concatenate MP3 chunks in correct order
    with open(output_path, "wb") as f:
        for chunk in audio_chunks:
            f.write(chunk)

    return True


# Google TTS limits — measured in BYTES not characters (emojis = multi-byte)
MAX_CHUNK_BYTES    = 4500   # per request (hard limit is 5000)
MAX_SENTENCE_BYTES = 200    # per sentence inside a chunk


def blen(s: str) -> int:
    """Return byte length of a string when encoded as UTF-8."""
    return len(s.encode("utf-8"))


def chunk_text(text: str, max_bytes: int = MAX_CHUNK_BYTES) -> list[str]:
    """
    Split text into chunks for Google TTS.
    Two constraints must both be satisfied — measured in BYTES:
      1. Each chunk must be under max_bytes (4500) total
      2. Each individual sentence must be under MAX_SENTENCE_BYTES (200)
    """
    # Step 1 — break into sentences at punctuation boundaries
    sentences = re.split(r"(?<=[.!?,;:\n])\s*", text)

    # Step 2 — hard-split any sentence that is still too long in bytes
    safe_sentences = []
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if blen(s) <= MAX_SENTENCE_BYTES:
            safe_sentences.append(s)
        else:
            # Hard split at word boundaries near MAX_SENTENCE_BYTES
            words = s.split()
            current = ""
            for word in words:
                if blen(current) + blen(word) + 1 > MAX_SENTENCE_BYTES:
                    if current:
                        safe_sentences.append(current.strip() + ".")
                    current = word
                else:
                    current += " " + word
            if current.strip():
                safe_sentences.append(current.strip() + ".")

    # Step 3 — combine safe sentences into chunks under max_bytes
    chunks, current = [], ""
    for s in safe_sentences:
        if blen(current) + blen(s) + 1 > max_bytes:
            if current:
                chunks.append(current.strip())
            current = s
        else:
            current += " " + s
    if current.strip():
        chunks.append(current.strip())
    return chunks or [text]


# ── RSS feed generation ────────────────────────────────────────────────────────
def build_item_xml(ep: dict, cfg: dict) -> str:
    """Build a single <item> block for one episode."""
    base_url = cfg["base_url"].rstrip("/")
    audio_url = f"{base_url}/audio/{ep['filename']}"
    file_size = Path(AUDIO_DIR / ep["filename"]).stat().st_size
    pub_date = formatdate(datetime.fromisoformat(
        ep["received"].replace("Z", "+00:00")
    ).timestamp())

    return f"""
    <item>
      <title>{escape_xml(ep['subject'])}</title>
      <description>{escape_xml(ep['sender'])}</description>
      <pubDate>{pub_date}</pubDate>
      <enclosure url="{audio_url}" length="{file_size}" type="audio/mpeg"/>
      <guid isPermaLink="false">{ep['id']}</guid>
      <itunes:duration>{ep.get('duration', '00:00')}</itunes:duration>
      <itunes:author>{escape_xml(ep['sender'])}</itunes:author>
    </item>"""


def generate_feed(episodes: list[dict], cfg: dict) -> str:
    """Generate a full RSS feed from scratch — only used on the very first run."""
    base_url = cfg["base_url"].rstrip("/")
    podcast_title = cfg.get("podcast_title", "My Morning Newsletters")
    podcast_desc = cfg.get("podcast_description", "Your daily newsletter digest, read aloud.")

    items_xml = "".join(build_item_xml(ep, cfg) for ep in episodes)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"
  xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>{escape_xml(podcast_title)}</title>
    <description>{escape_xml(podcast_desc)}</description>
    <link>{base_url}</link>
    <language>en-us</language>
    <itunes:category text="News"/>
    <lastBuildDate>{formatdate()}</lastBuildDate>
    {items_xml}
  </channel>
</rss>"""


def append_to_feed(new_episodes: list[dict], cfg: dict):
    """
    Append new episodes to the existing feed file by injecting <item> blocks
    just before </channel>. Creates the feed from scratch on first run.
    """
    if not FEED_FILE.exists():
        # First run — write the full feed structure including items
        log.info("  No existing feed found, creating new feed file...")
        with open(FEED_FILE, "w", encoding="utf-8") as f:
            f.write(generate_feed(new_episodes, cfg))
        return

    # Build only the new <item> blocks
    new_items_xml = "".join(build_item_xml(ep, cfg) for ep in new_episodes)

    # Inject new items just before </channel> — no parsing needed
    content = FEED_FILE.read_text(encoding="utf-8")
    updated = content.replace(
        "</channel>",
        f"{new_items_xml}\n  </channel>",
        1  # replace only the first occurrence
    )

    FEED_FILE.write_text(updated, encoding="utf-8")


def escape_xml(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
    )


# ── Processed email tracking ───────────────────────────────────────────────────
def load_processed() -> set:
    if PROCESSED_FILE.exists():
        with open(PROCESSED_FILE) as f:
            return set(json.load(f))
    return set()


def save_processed(processed: set):
    with open(PROCESSED_FILE, "w") as f:
        json.dump(list(processed), f)


# ── Main agent loop ────────────────────────────────────────────────────────────
def run():
    log.info("=" * 60)
    log.info("Newsletter Podcast Agent starting...")
    log.info("=" * 60)

    cfg = load_config()
    processed = load_processed()

    # 1. Fetch newsletters using the configured provider
    fetcher = get_fetcher(cfg)
    log.info(f"Fetching newsletters from {cfg.get('email_provider', 'unknown')}...")
    newsletters = fetcher.fetch_newsletters()

    if not newsletters:
        log.info("No new newsletters found. Nothing to do.")
        return

    # 3. Convert each newsletter to audio
    episodes = []
    for nl in newsletters:
        msg_id = nl["id"]
        if msg_id in processed:
            log.info(f"Skipping already processed: {nl['subject']}")
            continue

        log.info(f"Processing: {nl['subject']}")

        # ✅ Clean text FIRST — before generating filename or any file I/O
        text = html_to_text(nl["body_html"])
        if not text:
            log.warning(f"  Empty body after cleaning, skipping.")
            processed.add(msg_id)  # mark as processed so we don't retry it
            continue

        # Prepend subject as intro
        full_text = f"{nl['subject']}. From {nl['sender']}. \n\n{text}"

        # Only generate filename once we know there is actual content
        file_hash = hashlib.md5(msg_id.encode()).hexdigest()[:10]
        filename = f"{file_hash}.mp3"
        audio_path = AUDIO_DIR / filename

        if not audio_path.exists():
            log.info(f"  Converting to audio ({len(full_text)} chars)...")
            success = text_to_audio(full_text, audio_path, cfg)
            if not success:
                log.error(f"  TTS failed for: {nl['subject']}")
                continue

        episodes.append({**nl, "filename": filename})
        processed.add(msg_id)
        log.info(f"  ✓ Audio ready: {filename}")

    if not episodes:
        log.info("No new episodes generated.")
        return

    # 4. Append new episodes to the feed — no reparsing of existing feed
    log.info("Updating RSS feed...")
    append_to_feed(episodes, cfg)
    log.info(f"  ✓ Feed updated: {FEED_FILE}")

    # 5. Save processed IDs
    save_processed(processed)

    log.info(f"Done! {len(episodes)} new episode(s) added to your podcast feed.")
    log.info(f"Feed URL: {cfg['base_url']}/feed/podcast.xml")


if __name__ == "__main__":
    run()