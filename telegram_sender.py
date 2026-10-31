"""Send the most recent picture of each satellite in output/ to the Telegram channel.

    .venv/bin/python telegram_sender.py [output folder]   # default: ./output

TG_BOT_TOKEN and TG_CHANNEL_ID come from .env next to this script. The bot must be an
administrator of the channel with the right to post messages.
"""

import io
import os
import re
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
from PIL import Image

HERE = Path(__file__).resolve().parent
# satview names pictures <source>[_<satellite>]_<YYYYMMDD>[T<HHMM>].png; daily ones have no time
STAMP = re.compile(r"_(\d{8})(?:T(\d{4}))?\.png$")
# EUMETSAT and Copernicus require this credit on shared images
CREDITS = {
    "meteosat12_fci": "Contains modified EUMETSAT Meteosat data",
    "meteosat9_iodc": "Contains modified EUMETSAT Meteosat data",
    "sentinel3_olci": "Contains modified Copernicus Sentinel data",
    "sentinel2_msi": "Contains modified Copernicus Sentinel data",
}


def newest_picture(source_dir: Path) -> Path | None:
    """The picture with the latest observation time in its name."""
    pictures = [p for p in source_dir.glob(f"*/{source_dir.name}_*.png") if STAMP.search(p.name)]
    return max(pictures, key=lambda p: (STAMP.search(p.name).groups(""), p.name), default=None)


def caption(source: str, picture: Path) -> str:
    day, hhmm = STAMP.search(picture.name).groups("")
    text = f"{source} · {day[:4]}-{day[4:6]}-{day[6:]}" + (f" {hhmm[:2]}:{hhmm[2:]} UTC" if hhmm else "")
    if source in CREDITS:
        text += f"\n{CREDITS[source]} {day[:4]}"
    return text


def as_jpeg(picture: Path) -> bytes:
    """Telegram turns photos into JPEGs anyway; sending one makes the upload ~5x smaller."""
    out = io.BytesIO()
    with Image.open(picture) as im:
        im.convert("RGB").save(out, "JPEG", quality=92)
    return out.getvalue()


def send_photo(token: str, chat: str, picture: Path, text: str) -> None:
    r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                      data={"chat_id": chat, "caption": text},
                      files={"photo": (f"{picture.stem}.jpg", as_jpeg(picture), "image/jpeg")}, timeout=120)
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(body.get("description", f"HTTP {r.status_code}"))


def main() -> int:
    load_dotenv(HERE / ".env")
    token, chat = os.environ["TG_BOT_TOKEN"], os.environ["TG_CHANNEL_ID"]
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "output"
    found = failed = 0
    for source_dir in sorted(p for p in output.iterdir() if p.is_dir()):
        picture = newest_picture(source_dir)
        if picture is None:
            continue
        found += 1
        print(f"{picture.name}: ", end="", flush=True)  # uploads take a while
        try:
            send_photo(token, chat, picture, caption(source_dir.name, picture))
            print("sent")
        except Exception as e:
            # requests puts the URL, and with it the token, into its error messages
            print(str(e).replace(token, "<token>"))
            failed += 1
    if not found:
        print(f"no pictures in {output}")
    return 1 if failed or not found else 0


if __name__ == "__main__":
    sys.exit(main())
