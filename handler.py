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
R2_MUSIC_PREFIX = "music/modern/"  # folder in R2 bucket to scan for music
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
TITLE_COLORS = ["#FFD700", "#FFFFFF", "#FF69B4", "#FF4444", "#00E5FF", "#C41E3A", "#FF6B35", "#FF1493"]

# Motion preset — slow subtle zoom only for all dress types
MOTION_PRESETS = {
    "dresses":    [("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION)],
    "tops":       [("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION)],
    "co-ord sets":[("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION)],
    "jeans":      [("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION)],
    "skirts":     [("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION)],
    "trousers":   [("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION)],
    "palazzos":   [("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION)],
    "shorts":     [("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION)],
    "jumpsuit":   [("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION)],
    "western":    [("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION)],
    "jacket":     [("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION)],
    "other":      [("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION), ("zoom_in", IMG_DURATION)],
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
    font_size = 68
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

    # No decorative elements above title

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

    # Comment for link is now in a separate PNG (see build_title_overlay_image return)

    # Emoji rendering skipped — not reliably supported by system fonts

    title_path = os.path.join(workdir, "title_overlay.png")
    img.save(title_path, "PNG")
    log(f"Title overlay created: {title_path}")

    # Build separate "Comment for link" PNG — full video height, text at bottom
    comment_img = Image.new("RGBA", (OUTPUT_W, OUTPUT_H), (0, 0, 0, 0))
    comment_draw = ImageDraw.Draw(comment_img)

    cfl_text = "Comment for links" if platform == "instagram" else "Links in description"
    cfl_font_size = 88
    try:
        cfl_font = ImageFont.truetype(FONT_BOLD, cfl_font_size)
    except Exception:
        try:
            cfl_font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf", cfl_font_size)
        except Exception:
            cfl_font = ImageFont.load_default()

    try:
        cfl_bbox = comment_draw.textbbox((0, 0), cfl_text, font=cfl_font)
        cfl_w = cfl_bbox[2] - cfl_bbox[0]
    except Exception:
        cfl_w = len(cfl_text) * (cfl_font_size // 2)

    cfl_x = (OUTPUT_W - cfl_w) // 2
    cfl_y = OUTPUT_H - 460

    # Black stroke
    stroke = 3
    for dx in range(-stroke, stroke + 1):
        for dy in range(-stroke, stroke + 1):
            if dx == 0 and dy == 0:
                continue
            comment_draw.text((cfl_x + dx, cfl_y + dy), cfl_text, font=cfl_font, fill=(0, 0, 0, 255))
    # Yellow fill
    comment_draw.text((cfl_x, cfl_y), cfl_text, font=cfl_font, fill=(255, 215, 0, 255))

    comment_path = os.path.join(workdir, "comment_overlay.png")
    comment_img.save(comment_path, "PNG")
    log(f"Comment overlay created: {comment_path}")

    return title_path, comment_path


def get_motion_filter(motion, duration, idx):
    """Ken burns using scale+crop with frame counter — no zoompan, no frame stalls.
    Images are already 1080x1920. We scale UP slightly then animate crop position.
    """
    w, h = OUTPUT_W, OUTPUT_H
    # Scale only 4% larger — very subtle zoom
    sw = int(w * 1.04)  # 1123
    sh = int(h * 1.04)  # 1996
    frames = int(duration * FPS)
    max_x = sw - w  # 43px
    max_y = sh - h  # 76px

    if motion == "zoom_in":
        # Gradually crop from outer edge inward (simulates slow zoom in)
        vf = (
            f"scale={sw}:{sh},"
            f"crop={w}:{h}:"
            f"x='({max_x}-n*{max(1, max_x//frames)})':"
            f"y='({max_y}-n*{max(1, max_y//frames)})',"
            f"setsar=1"
        )
    elif motion == "zoom_out":
        vf = (
            f"scale={sw}:{sh},"
            f"crop={w}:{h}:"
            f"x='min(n*{max(1, max_x//frames)},{max_x})':"
            f"y='min(n*{max(1, max_y//frames)},{max_y})',"
            f"setsar=1"
        )
    elif motion == "pan_right":
        # Pan from left to right: x goes from 0 to max_x
        vf = (
            f"scale={sw}:{sh},"
            f"crop={w}:{h}:"
            f"x='min(n*{max_x//frames},{max_x})':"
            f"y='{max_y//2}',"
            f"setsar=1"
        )
    elif motion == "pan_left":
        # Pan from right to left: x goes from max_x to 0
        vf = (
            f"scale={sw}:{sh},"
            f"crop={w}:{h}:"
            f"x='max({max_x}-n*{max_x//frames},0)':"
            f"y='{max_y//2}',"
            f"setsar=1"
        )
    else:
        vf = f"scale={w}:{h},setsar=1"

    return vf


def list_music_from_r2():
    """List all mp3 files from R2 music folder dynamically."""
    try:
        endpoint = os.environ.get("R2_ENDPOINT")
        bucket   = os.environ.get("R2_BUCKET", "grwm-haul")
        access   = os.environ.get("R2_ACCESS_KEY")
        secret   = os.environ.get("R2_SECRET_KEY")
        pub_url  = os.environ.get("R2_PUBLIC_URL", "https://pub-e8495394a16e4722827186cdcf97b931.r2.dev")

        s3 = boto3.client("s3", endpoint_url=endpoint,
            aws_access_key_id=access, aws_secret_access_key=secret, region_name="auto")

        response = s3.list_objects_v2(Bucket=bucket, Prefix=R2_MUSIC_PREFIX)
        tracks = []
        for obj in response.get("Contents", []):
            key = obj["Key"]
            if key.endswith(".mp3"):
                tracks.append(f"{pub_url}/{key}")

        if tracks:
            log(f"Found {len(tracks)} music tracks in R2")
            return tracks
        else:
            log("No music tracks found in R2, using fallback")
            return [
                f"{pub_url}/music/modern/audio-1.mp3",
                f"{pub_url}/music/modern/audio-2.mp3",
            ]
    except Exception as e:
        log(f"Music listing failed: {e}, using fallback")
        return [
            "https://pub-e8495394a16e4722827186cdcf97b931.r2.dev/music/modern/audio-1.mp3",
            "https://pub-e8495394a16e4722827186cdcf97b931.r2.dev/music/modern/audio-2.mp3",
        ]


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
    content_type = "image/jpeg" if r2_key.endswith(".jpg") else "video/mp4"
    s3.upload_file(local_path, bucket, r2_key, ExtraArgs={"ContentType": content_type})
    public_url = f"{pub_url}/{r2_key}"
    log(f"Uploaded → {public_url}")
    return public_url


def generate_thumbnail(first_image_path, title_overlay_path, workdir, platform="instagram"):
    """Generate thumbnail based on platform:
    - instagram: first image + title overlay only (no comment text)
    - youtube: first image only, completely clean
    """
    try:
        bg = Image.open(first_image_path).convert("RGBA")
        bg = bg.resize((OUTPUT_W, OUTPUT_H), Image.LANCZOS)

        if platform == "instagram":
            # Instagram: title overlay only
            title_img = Image.open(title_overlay_path).convert("RGBA")
            bg = Image.alpha_composite(bg, title_img)
            log("Instagram thumbnail: title overlay applied")
        else:
            # YouTube: completely clean, no overlays
            log("YouTube thumbnail: clean image, no overlays")

        thumb_path = os.path.join(workdir, f"thumbnail_{platform}.jpg")
        bg.convert("RGB").save(thumb_path, "JPEG", quality=92)
        log(f"Thumbnail created: {thumb_path}")
        return thumb_path
    except Exception as e:
        log(f"Thumbnail generation failed: {e}")
        return None


# ── Main handler ─────────────────────────────────────────────────────────────
def handler(job):
    job_input    = job["input"]
    image_urls   = job_input.get("image_urls", [])
    marketplace  = (job_input.get("marketplace") or "").lower() or "other"
    dress_type   = job_input.get("dress_type", "other")
    session_id   = job_input.get("session_id", int(time.time()))
    creator_name = job_input.get("creator_name", "creator")
    # Dynamic music: pick random track from R2 folder
    music_tracks = list_music_from_r2()
    import random
    music_url = random.choice(music_tracks)
    log(f"Selected music: {music_url}")

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
        f"{mp_display} {dt_display} Under 500",
        f"Best {dt_display} on {mp_display}",
        f"{mp_display} {dt_display} Worth Buying",
        f"Affordable {dt_display} Haul",
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
        platform = job_input.get("platform", "instagram")
        title_overlay_path, comment_overlay_path = build_title_overlay_image(title, color_hex, workdir, platform)

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
            vf = get_motion_filter(motion, dur, 0)
            clip_cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "warning",
                "-loop", "1", "-t", str(dur), "-i", prep,
                "-vf", vf,
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
        # Force keyframe at every clip boundary to prevent browser stalls
        clip_durations = [m[1] for m in motions]
        keyframe_times = []
        t = 0
        for dur in clip_durations[:-1]:  # all except last
            t += dur
            keyframe_times.append(str(round(t, 3)))
        kf_arg = ",".join(keyframe_times) if keyframe_times else "0"

        concat_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-f", "concat", "-safe", "0", "-i", concat_list,
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-vf", f"scale={OUTPUT_W}:{OUTPUT_H},setsar=1",
            "-force_key_frames", kf_arg,
            "-y", concat_path
        ]
        run_cmd(concat_cmd, "concat clips")
        log(f"Keyframes forced at: {kf_arg}")
        log(f"Concat done: {concat_path}")

        # Two overlays:
        # [1:v] = comment PNG — shown throughout entire video
        # [2:v] = title PNG — fades in then hard cut off at TITLE_DURATION
        title_filter = (
            f"[0:v][1:v]overlay=0:0[with_comment];"
            f"[2:v]fade=t=in:st=0:d={FADE_DURATION}:alpha=1,"
            f"fade=t=out:st={TITLE_DURATION - FADE_DURATION}:d={FADE_DURATION}:alpha=1"
            f"[title_fade];"
            f"[with_comment][title_fade]overlay=0:0:enable='lte(t,{TITLE_DURATION})'[vfinal]"
        )

        output_path = os.path.join(workdir, "output.mp4")

        # Build FFmpeg overlay command — all inputs MUST come before output options
        overlay_cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning"]
        # Input 0: concat video
        overlay_cmd += ["-i", concat_path]
        # Input 1: comment overlay PNG (shown throughout)
        overlay_cmd += ["-loop", "1", "-i", comment_overlay_path]
        # Input 2: title overlay PNG (shown for TITLE_DURATION only)
        overlay_cmd += ["-loop", "1", "-t", str(TITLE_DURATION), "-i", title_overlay_path]
        # Input 3: music — -stream_loop BEFORE -i
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
            overlay_cmd += ["-map", "3:a"]
            overlay_cmd += ["-c:a", "aac", "-b:a", "128k"]
            overlay_cmd += ["-af", f"afade=t=in:st=0:d=0.5,afade=t=out:st={total_dur-1}:d=1"]

        overlay_cmd += ["-t", str(total_dur), "-y", output_path]

        # ── 9. Run FFmpeg overlay ─────────────────────────────────────────
        log(f"Running FFmpeg overlay + audio...")
        log(f"CMD: {' '.join(str(x) for x in overlay_cmd)}")
        run_cmd(overlay_cmd, "overlay render")

        file_size = os.path.getsize(output_path)
        log(f"Output: {output_path} ({file_size // 1024 // 1024}MB, {total_dur}s)")

        # ── 10. Generate thumbnail ────────────────────────────────────────
        thumb_path = generate_thumbnail(prep_paths[0], title_overlay_path, workdir, platform)

        # ── 11. Upload to R2 ──────────────────────────────────────────────
        ts = int(time.time())
        plat = platform  # "instagram" or "youtube"
        video_key = f"videos/{creator_name}/{plat}/session_{session_id}_{ts}.mp4"
        video_url = upload_to_r2(output_path, video_key, job_input)

        # Upload thumbnail
        thumb_url = None
        if thumb_path:
            try:
                thumb_key = f"thumbnails/{creator_name}/{plat}/session_{session_id}_{ts}.jpg"
                thumb_url = upload_to_r2(thumb_path, thumb_key, job_input)
                log(f"Thumbnail uploaded: {thumb_url}")
            except Exception as e:
                log(f"Thumbnail upload failed: {e}")

        return {
            "success": True,
            "platform": plat,
            "video_url": video_url,
            "thumbnail_url": thumb_url,
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
