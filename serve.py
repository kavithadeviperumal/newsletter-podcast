"""
serve.py — Lightweight HTTP server for the podcast feed and audio files.
Run this once; it stays alive in the background serving your iPhone.

Usage:
    python serve.py            # serves on port 8080
    python serve.py --port 9090
"""

import argparse
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

BASE_DIR = Path(__file__).parent


class PodcastHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def end_headers(self):
        # Allow iPhone podcast apps to access the feed
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, format, *args):
        # Clean up log output
        print(f"[server] {self.address_string()} - {format % args}")


def get_local_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    parser = argparse.ArgumentParser(description="Newsletter Podcast Server")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    local_ip = get_local_ip()
    feed_url = f"http://{local_ip}:{args.port}/feed/podcast.xml"

    print("=" * 60)
    print("  Newsletter Podcast Server")
    print("=" * 60)
    print(f"  Serving on : http://{local_ip}:{args.port}")
    print(f"  Podcast feed URL (add this to your iPhone):")
    print(f"  → {feed_url}")
    print("=" * 60)
    print("  Press Ctrl+C to stop\n")

    server = HTTPServer(("0.0.0.0", args.port), PodcastHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
