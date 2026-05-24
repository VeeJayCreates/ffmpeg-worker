"""
GRWM FFmpeg Worker — RunPod Serverless
Builds a 9:16 Instagram Reel from VTON try-on images.

Input payload:
{
  "image_urls":   ["https://...png", "https://...png", ...],  # VTON images in order
  "title":        "Dresses Haul",                             # video title overlay
  "music_url":    "https://...mp3",                           # background music
  "dress_type":   "dresses",                                  # for motion preset
  "marketplace":  "meesho",                                   # for badge color
  "session_id":   123,                                        # for R2 key
  "creator_name": "kanishkarao25",                            # for R2 path

  # R2 credentials (passed as env or in payload)
  "r2_endpoint":  "https://<account>.r2.cloudflarestorage.com",
  "r2_bucket":    "grwm-haul",
  "r2_access_key": "...",
  "r2_secret_key": "..."
}

Output:
{
  "video_url": "https://pub-xxx.r2.dev/videos/kanishkarao25/session_123.mp4"
}
"""

import runpod
import os
import sys
import json
import time
import random
import shutil
import subprocess
import tempfile
import requests
import boto3
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

# ── Constants ────────────────────────────────────────────────────────────────
OUTPUT_W      = 1080
OUTPUT_H      = 1920
FPS           = 30
FONT_PATH     = "/fonts/PlayfairDisplay-Bold.ttf"
FONT_REG_PATH = "/fonts/PlayfairDisplay-Regular.ttf"
TITLE_DURATION = 2.5   # seconds title overlay is shown
FADE_DURATION  = 0.6   # seconds for fade in/out

# Ken burns motion presets per dress type
MOTION_PRESETS = {
    "dresses":    [("zoom_in", 4), ("pan_right", 3), ("zoom_out", 3)],
    "tops":       [("zoom_in", 4), ("pan_left",  3), ("zoom_out", 3)],
    "co-ord sets":[("pan_right",4), ("zoom_in",  3), ("pan_left", 3)],
    "jeans":      [("zoom_in", 4), ("zoom_out", 3), ("pan_right", 3)],
    "skirts":     [("pan_left", 4), ("zoom_in",  3), ("pan_right", 3)],
    "trousers":   [("zoom_in", 4), ("pan_left",  3), ("zoom_out", 3)],
    "palazzos":   [("pan_right",4), ("zoom_out", 3), ("pan_left", 3)],
    "shorts":     [("zoom_in", 4), ("zoom_out", 3), ("pan_right", 3)],
    "jumpsuit":   [("zoom_in", 4), ("pan_right", 3), ("zoom_out", 3)],
    "western":    [("pan_left", 4), ("zoom_in",  3), ("pan_right", 3)],
    "jacket":     [("zoom_in", 4), ("pan_left",  3), ("zoom_out", 3)],
    "other":      [("zoom_in", 4), ("zoom_out",  3), ("pan_right", 3)],
}

# Marketplace badge colors (hex)
MP_COLORS = {
    "amazon": {"bg": "FF9900", "fg": "000000"},
    "meesho": {"bg": "9B2C8E", "fg": "FFFFFF"},
    "myntra": {"bg": "FF3F6C", "fg": "FFFFFF"},
    "nykaa":  {"bg": "FC338B", "fg": "FFFFFF"},
    "other":  {"bg": "444444", "fg": "FFFFFF"},
}

# ── Helpers ──────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[GRWM-FFmpeg] {msg}", flush=True)


def download_file(url, dest_path, label="file"):
    log(f"Downloading {label}: {url}")
    r = requests.get(url, stream=True, timeout=60)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            f.write(chunk)
    log(f"Downloaded {label} → {dest_path} ({os.path.getsize(dest_path)//1024}KB)")


def run_cmd(cmd, label=""):
    log(f"Running{' '+label if label else ''}: {' '.join(cmd[:6])}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"STDERR: {result.stderr[-500:]}")
        raise RuntimeError(f"FFmpeg failed ({label}): {result.stderr[-300:]}")
    return result


def prepare_image(src_path, dest_path):
    """Resize and pad image to 1080x1920 (9:16), keeping aspect ratio."""
    img = Image.open(src_path).convert("RGB")
    iw, ih = img.size
    target_ratio = OUTPUT_W / OUTPUT_H
    img_ratio = iw / ih

    if img_ratio > target_ratio:
        # Image is wider — fit height, pad sides
        new_h = OUTPUT_H
        new_w = int(iw * OUTPUT_H / ih)
    else:
        # Image is taller — fit width, pad top/bottom
        new_w = OUTPUT_W
        new_h = int(ih * OUTPUT_W / iw)

    img = img.resize((new_w, new_h), Image.LANCZOS)

    # Create dark background and paste centered
    bg = Image.new("RGB", (OUTPUT_W, OUTPUT_H), (15, 15, 15))
    x = (OUTPUT_W - new_w) // 2
    y = (OUTPUT_H - new_h) // 2
    bg.paste(img, (x, y))
    bg.save(dest_path, "JPEG", quality=95)
    log(f"Prepared image: {new_w}x{new_h} → {dest_path}")


def build_title_card(title, marketplace, dest_path):
    """Build a 1080x1920 title card PNG with Playfair Display text."""
    img = Image.new("RGBA", (OUTPUT_W, OUTPUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Semi-transparent dark gradient at bottom
    overlay = Image.new("RGBA", (OUTPUT_W, 400), (0, 0, 0, 180))
    img.paste(overlay, (0, OUTPUT_H - 400), overlay)

    # Title text
    font_size = 72
    try:
        font = ImageFont.truetype(FONT_PATH, font_size)
    except Exception:
        font = ImageFont.load_default()

    # Word wrap title if long
    words = title.split()
    lines = []
    current = ""
    for word in words:
        test = (current + " " + word).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] > OUTPUT_W - 80:
            if current:
                lines.append(current)
            current = word
        else:
            current = test
    if current:
        lines.append(current)

    # Draw title lines centered
    total_h = len(lines) * (font_size + 12)
    y_start = OUTPUT_H - 280 - total_h
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        x = (OUTPUT_W - (bbox[2] - bbox[0])) // 2
        # Shadow
        draw.text((x + 2, y_start + 2), line, font=font, fill=(0, 0, 0, 180))
        # Main text
        draw.text((x, y_start), line, font=font, fill=(255, 255, 255, 255))
        y_start += font_size + 12

    # Marketplace badge
    mp = marketplace.lower() if marketplace else "other"
    colors = MP_COLORS.get(mp, MP_COLORS["other"])
    badge_text = mp.upper()
    badge_font_size = 32
    try:
        badge_font = ImageFont.truetype(FONT_PATH, badge_font_size)
    except Exception:
        badge_font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
    bw = bbox[2] - bbox[0] + 32
    bh = bbox[3] - bbox[1] + 16
    bx = (OUTPUT_W - bw) // 2
    by = y_start + 16

    bg_color = tuple(int(colors["bg"][i:i+2], 16) for i in (0, 2, 4)) + (230,)
    fg_color = tuple(int(colors["fg"][i:i+2], 16) for i in (0, 2, 4)) + (255,)

    draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=8, fill=bg_color)
    draw.text((bx + 16, by + 8), badge_text, font=badge_font, fill=fg_color)

    img.save(dest_path, "PNG")
    log(f"Title card created: {dest_path}")


def get_zoompan_filter(motion, duration, idx):
    """Build FFmpeg zoompan filter string for ken burns effect."""
    frames = int(duration * FPS)
    w, h = OUTPUT_W, OUTPUT_H

    if motion == "zoom_in":
        z = f"'zoom+0.0015'"
        x = f"'iw/2-(iw/zoom/2)'"
        y = f"'ih/2-(ih/zoom/2)'"
    elif motion == "zoom_out":
        z = f"'if(eq(on,1),1.12,max(zoom-0.0015,1))'"
        x = f"'iw/2-(iw/zoom/2)'"
        y = f"'ih/2-(ih/zoom/2)'"
    elif motion == "pan_right":
        z = f"'1.08'"
        x = f"'iw/2-(iw/zoom/2)+on*{int(w*0.0015)}'"
        y = f"'ih/2-(ih/zoom/2)'"
    elif motion == "pan_left":
        z = f"'1.08'"
        x = f"'iw/2-(iw/zoom/2)-on*{int(w*0.0015)}'"
        y = f"'ih/2-(ih/zoom/2)'"
    else:
        z, x, y = "'1'", "'0'", "'0'"

    return (
        f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},"
        f"zoompan=z={z}:x={x}:y={y}:d={frames}:s={w}x{h}:fps={FPS},"
        f"setsar=1[v{idx}]"
    )


def upload_to_r2(local_path, r2_key, job_input):
    """Upload file to Cloudflare R2."""
    # Always use env vars for credentials (never from job input for security)
    endpoint = os.environ.get("R2_ENDPOINT")
    bucket   = os.environ.get("R2_BUCKET", "grwm-haul")
    access   = os.environ.get("R2_ACCESS_KEY")
    secret   = os.environ.get("R2_SECRET_KEY")
    pub_url  = os.environ.get("R2_PUBLIC_URL", "https://pub-e8495394a16e4722827186cdcf97b931.r2.dev")
    
    log(f"R2 endpoint: {endpoint}")
    log(f"R2 access key (first 8 chars): {access[:8] if access else 'MISSING'}")

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name="auto"
    )

    log(f"Uploading to R2: {r2_key}")
    s3.upload_file(
        local_path, bucket, r2_key,
        ExtraArgs={"ContentType": "video/mp4"}
    )
    public_url = f"{pub_url}/{r2_key}"
    log(f"Uploaded → {public_url}")
    return public_url


# ── Main handler ─────────────────────────────────────────────────────────────
def handler(job):
    job_input    = job["input"]
    image_urls   = job_input.get("image_urls", [])
    title        = job_input.get("title", "Fashion Haul")
    music_url    = job_input.get("music_url", "")
    dress_type   = job_input.get("dress_type", "other")
    marketplace  = job_input.get("marketplace", "other")
    session_id   = job_input.get("session_id", int(time.time()))
    creator_name = job_input.get("creator_name", "creator")

    if not image_urls:
        return {"error": "No image_urls provided"}

    log(f"Starting FFmpeg job — {len(image_urls)} images, title: '{title}'")

    workdir = tempfile.mkdtemp(prefix="grwm_")
    try:
        # ── 1. Download all images ──────────────────────────────────────
        raw_paths = []
        for i, url in enumerate(image_urls):
            raw = os.path.join(workdir, f"raw_{i}.jpg")
            download_file(url, raw, f"image {i+1}/{len(image_urls)}")
            raw_paths.append(raw)

        # ── 2. Prepare (resize/pad) all images ──────────────────────────
        prep_paths = []
        for i, raw in enumerate(raw_paths):
            prep = os.path.join(workdir, f"prep_{i}.jpg")
            prepare_image(raw, prep)
            prep_paths.append(prep)

        # ── 3. Download music ────────────────────────────────────────────
        music_path = None
        if music_url:
            music_path = os.path.join(workdir, "music.mp3")
            download_file(music_url, music_path, "music")

        # ── 4. Build title card PNG ──────────────────────────────────────
        title_card_path = os.path.join(workdir, "title_card.png")
        build_title_card(title, marketplace, title_card_path)

        # ── 5. Get motion preset for dress type ──────────────────────────
        presets = MOTION_PRESETS.get(dress_type, MOTION_PRESETS["other"])
        # Cycle presets if more images than presets
        motions = []
        for i in range(len(prep_paths)):
            motion, duration = presets[i % len(presets)]
            motions.append((motion, duration))

        # ── 6. Build FFmpeg filter graph ─────────────────────────────────
        n = len(prep_paths)
        total_dur = sum(d for _, d in motions)

        # Input args: one -loop -t -i per image
        input_args = []
        for i, (path, (motion, dur)) in enumerate(zip(prep_paths, motions)):
            input_args += ["-loop", "1", "-t", str(dur + 0.5), "-i", path]

        # Music input
        music_idx = n
        if music_path:
            input_args += ["-i", music_path]

        # Filter complex
        filter_parts = []

        # Ken burns per image
        for i, (motion, dur) in enumerate(motions):
            filter_parts.append(get_zoompan_filter(motion, dur, i))

        # Concat all video streams
        concat_inputs = "".join(f"[v{i}]" for i in range(n))
        filter_parts.append(f"{concat_inputs}concat=n={n}:v=1:a=0[vconcat]")

        # Title overlay — fade in for 0.6s, hold, fade out at TITLE_DURATION
        title_overlay = (
            f"[vconcat]"
            f"drawtext="
            f"fontfile={FONT_PATH}:"
            f"text='{title.replace(chr(39), '')}':  "
            f"fontcolor=white:"
            f"fontsize=72:"
            f"x=(w-text_w)/2:"
            f"y=h-350:"
            f"alpha='if(lt(t,{FADE_DURATION}),t/{FADE_DURATION},"
            f"if(lt(t,{TITLE_DURATION-FADE_DURATION}),1,"
            f"if(lt(t,{TITLE_DURATION}),({TITLE_DURATION}-t)/{FADE_DURATION},0)))':"
            f"shadowx=2:shadowy=2:shadowcolor=black@0.8"
            f"[vtext]"
        )
        filter_parts.append(title_overlay)

        # Marketplace badge text overlay (same timing as title)
        mp_label = marketplace.upper() if marketplace else "OTHER"
        badge_overlay = (
            f"[vtext]"
            f"drawtext="
            f"fontfile={FONT_PATH}:"
            f"text='{mp_label}':"
            f"fontcolor=white:"
            f"fontsize=34:"
            f"x=(w-text_w)/2:"
            f"y=h-260:"
            f"alpha='if(lt(t,{FADE_DURATION}),t/{FADE_DURATION},"
            f"if(lt(t,{TITLE_DURATION-FADE_DURATION}),1,"
            f"if(lt(t,{TITLE_DURATION}),({TITLE_DURATION}-t)/{FADE_DURATION},0)))':"
            f"shadowx=1:shadowy=1:shadowcolor=black@0.9"
            f"[vfinal]"
        )
        filter_parts.append(badge_overlay)

        filter_complex = "; ".join(filter_parts)

        # ── 7. Build output args ─────────────────────────────────────────
        output_path = os.path.join(workdir, "output.mp4")
        output_args = [
            "-map", "[vfinal]",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-r", str(FPS),
        ]

        if music_path:
            output_args += [
                "-map", f"{music_idx}:a",
                "-c:a", "aac",
                "-b:a", "128k",
                "-shortest",
                "-af", f"afade=t=out:st={total_dur-1}:d=1",
            ]

        output_args += ["-y", output_path]

        # ── 8. Run FFmpeg ────────────────────────────────────────────────
        cmd = (
            ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
            + input_args
            + ["-filter_complex", filter_complex]
            + output_args
        )

        log(f"Running FFmpeg — total duration: {total_dur:.1f}s")
        run_cmd(cmd, "main render")

        file_size = os.path.getsize(output_path)
        log(f"Output: {output_path} ({file_size//1024//1024}MB)")

        # ── 9. Upload to R2 ──────────────────────────────────────────────
        r2_key = f"videos/{creator_name}/session_{session_id}_{int(time.time())}.mp4"
        video_url = upload_to_r2(output_path, r2_key, job_input)

        return {
            "success": True,
            "video_url": video_url,
            "session_id": session_id,
            "duration_seconds": round(total_dur, 1),
            "image_count": n,
            "file_size_mb": round(file_size / 1024 / 1024, 2)
        }

    except Exception as e:
        log(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}

    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        log("Cleaned up workdir")


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
