"""
GRWM FFmpeg Worker — RunPod Serverless
Builds a 9:16 Instagram Reel from VTON try-on images.
"""

import runpod
import os
import time
import shutil
import subprocess
import tempfile
import requests
import boto3
from PIL import Image, ImageDraw, ImageFont

# ── Constants ────────────────────────────────────────────────────────────────
OUTPUT_W       = 1080
OUTPUT_H       = 1920
FPS            = 30
FONT_BOLD      = "/fonts/PlayfairDisplay-Bold.ttf"
FONT_REG       = "/fonts/PlayfairDisplay-Regular.ttf"
FONT_EMOJI     = "/fonts/NotoColorEmoji.ttf"
IMG_DURATION   = 2.5    # seconds per image
TITLE_DURATION = 3.5    # seconds title is visible
FADE_DURATION  = 0.5    # fade in/out duration

# Title colors — rotate per session_id
TITLE_COLORS = ["#FFD700", "#FF69B4", "#FF4444", "#C41E3A", "#FF6B35", "#FF1493"]

# Ken burns motion presets per dress type — all 2.5s
MOTION_PRESETS = {
    "dresses":    [("zoom_in", IMG_DURATION), ("pan_right", IMG_DURATION), ("zoom_out", IMG_DURATION), ("pan_left", IMG_DURATION)],
    "tops":       [("zoom_in", IMG_DURATION), ("pan_left",  IMG_DURATION), ("zoom_out", IMG_DURATION), ("pan_right", IMG_DURATION)],
    "co-ord sets":[("pan_right",IMG_DURATION), ("zoom_in",  IMG_DURATION), ("pan_left", IMG_DURATION), ("zoom_out", IMG_DURATION)],
    "jeans":      [("zoom_in", IMG_DURATION), ("zoom_out", IMG_DURATION), ("pan_right", IMG_DURATION), ("pan_left", IMG_DURATION)],
    "skirts":     [("pan_left", IMG_DURATION), ("zoom_in",  IMG_DURATION), ("pan_right", IMG_DURATION), ("zoom_out", IMG_DURATION)],
    "trousers":   [("zoom_in", IMG_DURATION), ("pan_left",  IMG_DURATION), ("zoom_out", IMG_DURATION), ("pan_right", IMG_DURATION)],
    "palazzos":   [("pan_right",IMG_DURATION), ("zoom_out", IMG_DURATION), ("pan_left", IMG_DURATION), ("zoom_in",  IMG_DURATION)],
    "shorts":     [("zoom_in", IMG_DURATION), ("zoom_out", IMG_DURATION), ("pan_right", IMG_DURATION), ("pan_left", IMG_DURATION)],
    "jumpsuit":   [("zoom_in", IMG_DURATION), ("pan_right", IMG_DURATION), ("zoom_out", IMG_DURATION), ("pan_left", IMG_DURATION)],
    "western":    [("pan_left", IMG_DURATION), ("zoom_in",  IMG_DURATION), ("pan_right", IMG_DURATION), ("zoom_out", IMG_DURATION)],
    "jacket":     [("zoom_in", IMG_DURATION), ("pan_left",  IMG_DURATION), ("zoom_out", IMG_DURATION), ("pan_right", IMG_DURATION)],
    "other":      [("zoom_in", IMG_DURATION), ("zoom_out",  IMG_DURATION), ("pan_right", IMG_DURATION), ("pan_left", IMG_DURATION)],
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
    log(f"Running {label}: {' '.join(str(x) for x in cmd[:5])}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log(f"STDERR: {result.stderr[-800:]}")
        raise RuntimeError(f"FFmpeg failed ({label}): {result.stderr[-400:]}")
    return result


def prepare_image(src_path, dest_path):
    """Resize and pad image to 1080x1920 keeping aspect ratio."""
    img = Image.open(src_path).convert("RGB")
    iw, ih = img.size
    ratio = iw / ih
    target = OUTPUT_W / OUTPUT_H

    if ratio > target:
        new_h = OUTPUT_H
        new_w = int(iw * OUTPUT_H / ih)
    else:
        new_w = OUTPUT_W
        new_h = int(ih * OUTPUT_W / iw)

    img = img.resize((new_w, new_h), Image.LANCZOS)
    bg = Image.new("RGB", (OUTPUT_W, OUTPUT_H), (10, 10, 10))
    x = (OUTPUT_W - new_w) // 2
    y = (OUTPUT_H - new_h) // 2
    bg.paste(img, (x, y))
    bg.save(dest_path, "JPEG", quality=95)
    log(f"Prepared: {iw}x{ih} → {new_w}x{new_h} padded to {OUTPUT_W}x{OUTPUT_H}")


def hex_to_rgb(hex_color):
    h = hex_color.lstrip('#')
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def build_title_overlay_image(title, color_hex, workdir):
    """Build a transparent PNG with just the title text — overlaid on video."""
    img = Image.new("RGBA", (OUTPUT_W, OUTPUT_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # No background bar — clean overlay

    # Title text
    font_size = 80
    try:
        font = ImageFont.truetype(FONT_BOLD, font_size)
    except Exception:
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()

    color_rgb = hex_to_rgb(color_hex) + (255,)
    shadow_rgb = (0, 0, 0, 200)

    # Word wrap
    words = title.split()
    lines, current = [], ""
    for word in words:
        test = (current + " " + word).strip()
        try:
            bbox = draw.textbbox((0, 0), test, font=font)
            if bbox[2] > OUTPUT_W - 60:
                if current:
                    lines.append(current)
                current = word
            else:
                current = test
        except Exception:
            current = test
    if current:
        lines.append(current)

    # Draw title near top with padding
    line_h = font_size + 8
    total_h = len(lines) * line_h
    y_start = 40  # fixed top padding

    for line in lines:
        try:
            bbox = draw.textbbox((0, 0), line, font=font)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw = len(line) * (font_size // 2)
        x = (OUTPUT_W - tw) // 2
        # Shadow
        draw.text((x + 2, y_start + 2), line, font=font, fill=shadow_rgb)
        # Colored text
        draw.text((x, y_start), line, font=font, fill=color_rgb)
        y_start += line_h

    # "Comment for link" text at bottom — yellow with black stroke
    cfl_text = "Comment for link"
    cfl_font_size = 88
    try:
        cfl_font = ImageFont.truetype(FONT_BOLD, cfl_font_size)
    except Exception:
        try:
            cfl_font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf", cfl_font_size)
        except Exception:
            cfl_font = ImageFont.load_default()

    try:
        cfl_bbox = draw.textbbox((0, 0), cfl_text, font=cfl_font)
        cfl_w = cfl_bbox[2] - cfl_bbox[0]
    except Exception:
        cfl_w = len(cfl_text) * (cfl_font_size // 2)

    cfl_x = (OUTPUT_W - cfl_w) // 2
    cfl_y = OUTPUT_H - 460  # slightly down from 3x

    # Black stroke (draw text offset in 8 directions)
    stroke = 3
    for dx in range(-stroke, stroke + 1):
        for dy in range(-stroke, stroke + 1):
            if dx == 0 and dy == 0:
                continue
            draw.text((cfl_x + dx, cfl_y + dy), cfl_text, font=cfl_font, fill=(0, 0, 0, 255))

    # Yellow fill
    draw.text((cfl_x, cfl_y), cfl_text, font=cfl_font, fill=(255, 215, 0, 255))

    # Add emoji using Noto Color Emoji font if available
    emoji_text = "🛍️"
    try:
        emoji_font = ImageFont.truetype(FONT_EMOJI, 80)
        try:
            e_bbox = draw.textbbox((0, 0), emoji_text, font=emoji_font)
            e_w = e_bbox[2] - e_bbox[0]
        except Exception:
            e_w = 80
        e_x = (OUTPUT_W - e_w) // 2
        e_y = y_start + 10  # just below title text
        draw.text((e_x, e_y), emoji_text, font=emoji_font, fill=(255, 255, 255, 255), embedded_color=True)
        log("Emoji drawn successfully")
    except Exception as e:
        log(f"Emoji skipped: {e}")

    path = os.path.join(workdir, "title_overlay.png")
    img.save(path, "PNG")
    log(f"Title overlay created: {path}")
    return path


def get_zoompan_filter(motion, duration, idx):
    """FFmpeg zoompan filter for ken burns."""
    frames = int(duration * FPS)
    w, h = OUTPUT_W, OUTPUT_H

    if motion == "zoom_in":
        z = "'zoom+0.002'"
        x = "'iw/2-(iw/zoom/2)'"
        y = "'ih/2-(ih/zoom/2)'"
    elif motion == "zoom_out":
        z = "'if(eq(on,1),1.15,max(zoom-0.002,1))'"
        x = "'iw/2-(iw/zoom/2)'"
        y = "'ih/2-(ih/zoom/2)'"
    elif motion == "pan_right":
        z = "'1.1'"
        x = f"'iw/2-(iw/zoom/2)+on*{int(w*0.002)}'"
        y = "'ih/2-(ih/zoom/2)'"
    elif motion == "pan_left":
        z = "'1.1'"
        x = f"'iw/2-(iw/zoom/2)-on*{int(w*0.002)}'"
        y = "'ih/2-(ih/zoom/2)'"
    else:
        z, x, y = "'1'", "'0'", "'0'"

    return (
        f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h},"
        f"zoompan=z={z}:x={x}:y={y}:d={frames}:s={w}x{h}:fps={FPS},"
        f"setsar=1[v{idx}]"
    )


def upload_to_r2(local_path, r2_key, job_input):
    endpoint = os.environ.get("R2_ENDPOINT")
    bucket   = os.environ.get("R2_BUCKET", "grwm-haul")
    access   = os.environ.get("R2_ACCESS_KEY")
    secret   = os.environ.get("R2_SECRET_KEY")
    pub_url  = os.environ.get("R2_PUBLIC_URL", "https://pub-e8495394a16e4722827186cdcf97b931.r2.dev")

    log(f"R2 endpoint: {endpoint}")
    log(f"R2 access key prefix: {access[:8] if access else 'MISSING'}")

    if not endpoint or not access or not secret:
        raise ValueError(f"Missing R2 credentials. endpoint={endpoint}, access={bool(access)}, secret={bool(secret)}")

    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name="auto"
    )

    log(f"Uploading {local_path} → {bucket}/{r2_key}")
    s3.upload_file(local_path, bucket, r2_key, ExtraArgs={"ContentType": "video/mp4"})
    public_url = f"{pub_url}/{r2_key}"
    log(f"Uploaded → {public_url}")
    return public_url


# ── Main handler ─────────────────────────────────────────────────────────────
def handler(job):
    job_input    = job["input"]
    image_urls   = job_input.get("image_urls", [])
    marketplace  = (job_input.get("marketplace") or "").lower() or "other"
    dress_type   = job_input.get("dress_type", "other")
    session_id   = job_input.get("session_id", int(time.time()))
    creator_name = job_input.get("creator_name", "creator")
    music_url    = job_input.get("music_url", "")

    # Smart title generation
    mp_display = marketplace.capitalize() if marketplace and marketplace != "other" else ""
    dt_map = {
        "dresses":    "Dress Collection",
        "tops":       "Top Picks",
        "co-ord sets":"Co-ord Sets",
        "jeans":      "Jeans Edit",
        "skirts":     "Skirt Collection",
        "trousers":   "Trouser Edit",
        "palazzos":   "Palazzo Collection",
        "shorts":     "Shorts Edit",
        "jumpsuit":   "Jumpsuit Collection",
        "western":    "Western Wear",
        "jacket":     "Jacket Collection",
        "other":      "Fashion Haul",
    }
    dt_display = dt_map.get(dress_type, dress_type.capitalize())
    # Smart title formats
    smart_titles = [
        f"{mp_display} {dt_display} Under 500 🌸",
        f"Best {dt_display} on {mp_display} 🌸",
        f"{mp_display} {dt_display} Worth Buying 🌸",
        f"Affordable {dt_display} Haul 🌸",
    ]
    auto_title = smart_titles[int(session_id) % len(smart_titles)].strip()
    # Use provided title if it's meaningful, else use smart auto title
    provided = job_input.get("title", "")
    title = provided if provided and provided not in ["Dresses Haul", "Tops Haul", "other Haul"] else auto_title
    title = title.replace("'", "").replace('"', "")

    # Pick title color based on session_id
    color_hex = TITLE_COLORS[int(session_id) % len(TITLE_COLORS)]

    if not image_urls:
        return {"error": "No image_urls provided"}

    log(f"Job start — {len(image_urls)} images, title: '{title}', color: {color_hex}")

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
            try:
                download_file(music_url, music_path, "music")
            except Exception as e:
                log(f"Music download failed: {e} — continuing without music")
                music_path = None

        # ── 4. Build title overlay image ─────────────────────────────────
        title_overlay_path = build_title_overlay_image(title, color_hex, workdir)

        # ── 5. Get motion presets ────────────────────────────────────────
        presets = MOTION_PRESETS.get(dress_type, MOTION_PRESETS["other"])
        motions = []
        for i in range(len(prep_paths)):
            motion, duration = presets[i % len(presets)]
            # First image gets 3.5s, rest get 2.5s
            dur = 3.5 if i == 0 else IMG_DURATION
            motions.append((motion, dur))

        n = len(prep_paths)
        total_dur = sum(d for _, d in motions)
        log(f"Total video duration: {total_dur}s ({n} images × {IMG_DURATION}s)")

        # ── 6. Render each image as individual clip then concat ──────────
        clip_paths = []
        for i, (prep, (motion, dur)) in enumerate(zip(prep_paths, motions)):
            clip_path = os.path.join(workdir, f"clip_{i}.mp4")
            zp = get_zoompan_filter(motion, dur, 0)  # always index 0 for single input
            clip_cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "warning",
                "-loop", "1", "-t", str(dur + 0.5), "-i", prep,
                "-filter_complex", zp.replace("[0:v]", "[0:v]").replace(f"[v0]", "[vout]"),
                "-map", "[vout]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "22",
                "-pix_fmt", "yuv420p", "-r", str(FPS),
                "-t", str(dur), "-y", clip_path
            ]
            run_cmd(clip_cmd, f"clip {i+1}/{n}")
            clip_paths.append(clip_path)
            log(f"Clip {i+1} rendered: {clip_path}")

        # Concat all clips using concat demuxer
        concat_list = os.path.join(workdir, "concat.txt")
        with open(concat_list, "w") as f:
            for cp in clip_paths:
                f.write(f"file '{cp}'\n")

        concat_path = os.path.join(workdir, "concat.mp4")
        concat_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "concat", "-safe", "0", "-i", concat_list,
            "-c", "copy", "-y", concat_path
        ]
        run_cmd(concat_cmd, "concat clips")
        log(f"Concat done: {concat_path}")

        # Overlay title PNG on concat video — simple fade in/out
        title_filter = (
            f"[1:v]"
            f"fade=t=in:st=0:d={FADE_DURATION}:alpha=1,"
            f"fade=t=out:st={TITLE_DURATION - FADE_DURATION}:d={FADE_DURATION}:alpha=1"
            f"[title_fade];"
            f"[0:v][title_fade]overlay=0:0[vfinal]"
        )

        output_path = os.path.join(workdir, "output.mp4")

        # Build FFmpeg overlay command — all inputs MUST come before output options
        overlay_cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
        # Input 0: concat video
        overlay_cmd += ["-i", concat_path]
        # Input 1: title overlay PNG
        overlay_cmd += ["-loop", "1", "-t", str(TITLE_DURATION), "-i", title_overlay_path]
        # Input 2: music — -stream_loop BEFORE -i
        if music_path:
            overlay_cmd += ["-stream_loop", "-1", "-i", music_path]

        # Filter complex
        overlay_cmd += ["-filter_complex", title_filter]

        # Video output
        overlay_cmd += ["-map", "[vfinal]"]
        overlay_cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "22"]
        overlay_cmd += ["-pix_fmt", "yuv420p", "-r", str(FPS)]

        # Audio output
        if music_path:
            overlay_cmd += ["-map", "2:a"]
            overlay_cmd += ["-c:a", "aac", "-b:a", "128k"]
            overlay_cmd += ["-af", f"afade=t=in:st=0:d=0.5,afade=t=out:st={total_dur-1}:d=1"]

        overlay_cmd += ["-t", str(total_dur), "-y", output_path]

        # ── 9. Run FFmpeg overlay ─────────────────────────────────────────
        log(f"Running FFmpeg overlay + audio...")
        log(f"CMD: {' '.join(str(x) for x in overlay_cmd)}")
        run_cmd(overlay_cmd, "overlay render")

        file_size = os.path.getsize(output_path)
        log(f"Output: {output_path} ({file_size // 1024 // 1024}MB, {total_dur}s)")

        # ── 10. Upload to R2 ──────────────────────────────────────────────
        r2_key = f"videos/{creator_name}/session_{session_id}_{int(time.time())}.mp4"
        video_url = upload_to_r2(output_path, r2_key, job_input)

        return {
            "success": True,
            "video_url": video_url,
            "session_id": session_id,
            "duration_seconds": round(total_dur, 1),
            "image_count": n,
            "title": title,
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
