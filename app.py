"""AI Radio Video Studio — cloud edition."""
from pathlib import Path
import base64
import hashlib
import hmac
import json
import math
import random
import re
import shutil
import subprocess
import tempfile
import time
import os

import requests
import streamlit as st

st.set_page_config(page_title="AI Radio Video Studio", page_icon="🎬", layout="centered")

# Fail closed: a public URL must never expose paid API features without a password.
try:
    studio_password = str(st.secrets["STUDIO_PASSWORD"])
except (KeyError, FileNotFoundError):
    st.error("Studio is locked. Configure STUDIO_PASSWORD in Streamlit Cloud Secrets.")
    st.stop()
if len(studio_password) < 12 or studio_password == "CHANGE_ME":
    st.error("Studio is locked. Set a strong STUDIO_PASSWORD (at least 12 characters).")
    st.stop()

if not st.session_state.get("authenticated", False):
    st.title("🔒 AI Radio Video Studio")
    with st.form("login"):
        entered = st.text_input("Studio password", type="password")
        submitted = st.form_submit_button("Unlock studio")
    if submitted:
        if hmac.compare_digest(entered.encode(), studio_password.encode()):
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password")
    st.stop()

ROOT = Path(__file__).resolve().parent
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm"}
IMAGE_EXT = {".jpg", ".jpeg", ".png"}


def run(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Video rendering timed out.") from exc
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-2500:])
    return result.stdout


def duration(path):
    info = json.loads(run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)]))
    return float(info["format"]["duration"])


def story_segments(lyrics, total, count):
    lines = [x.strip() for x in re.split(r"[\n.!?]+", lyrics) if len(x.strip()) > 8]
    if not lines:
        lines = ["A nation in need", "People praying together", "Hope and restoration", "Peace and a brighter future"]
    return [{"start": round(total*i/count, 3), "end": round(total*(i+1)/count, 3), "theme": lines[min(len(lines)-1, i*len(lines)//count)]} for i in range(count)]


def choose_media(segments, assets):
    used = {}
    selected = []
    for segment in segments:
        words = set(re.findall(r"[a-z]{3,}", segment["theme"].lower()))
        def score(p):
            name = set(re.findall(r"[a-z]{3,}", p.stem.lower()))
            return len(words & name)*10 - used.get(p, 0)*5
        best = max(assets, key=lambda p: (score(p), random.random()))
        selected.append(best)
        used[best] = used.get(best, 0) + 1
    return selected


def generate_ai_images(lyrics, total, key, scene_count, progress, workdir):
    if not key:
        raise RuntimeError("OpenAI API key is not configured in Streamlit Secrets.")
    segments = story_segments(lyrics, total, scene_count)
    generated = []
    for i, segment in enumerate(segments):
        prompt = (
            "Photorealistic cinematic widescreen 16:9 film still, South African setting when relevant. "
            "A visually compelling scene illustrating: " + segment["theme"] + ". "
            "Represent South Africa inclusively across the video: Black, White, Coloured and Indian South Africans, "
            "diverse cultures, ages and communities, shown with dignity, unity and hope. "
            "Natural lighting, realistic human anatomy, sophisticated cinematography, hopeful atmosphere. "
            "No text, no subtitles, no letters, no logos, no watermark."
        )
        try:
            response = requests.post(
                "https://api.openai.com/v1/images/generations",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": "gpt-image-1", "quality": "high", "prompt": prompt, "size": "1536x1024", "n": 1},
                timeout=240,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"Image generation connection failed: {exc}") from exc
        if not response.ok:
            # Do not expose provider response bodies that could contain sensitive context.
            raise RuntimeError(f"Image generation failed (HTTP {response.status_code}). Check API billing, access and logs.")
        data = response.json()["data"][0]
        if "b64_json" not in data:
            raise RuntimeError("The image provider did not return image data.")
        out = workdir / f"ai_scene_{i+1:03d}.png"
        out.write_bytes(base64.b64decode(data["b64_json"]))
        generated.append(out)
        progress.progress((i + 1) / (scene_count + 1), text=f"Generating original scene {i+1}/{scene_count}")
    return generated


def create_video(audio, lyrics, resolution, progress, assets, workdir):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg / FFprobe unavailable on server. Verify packages.txt.")
    if not assets:
        raise RuntimeError("Upload at least one MP4 or JPG/PNG for footage mode.")
    total = duration(audio)
    if total < 5:
        raise RuntimeError("The uploaded audio is too short.")
    count = max(1, math.ceil(total / 12))
    segments = story_segments(lyrics, total, count)
    picks = choose_media(segments, assets)
    w, h = (1920, 1080) if resolution == "1080p" else (1280, 720)
    target = workdir / f"AI_Radio_{int(time.time())}_{hashlib.sha256(audio.read_bytes()).hexdigest()[:8]}.mp4"
    logo = ROOT / "ai_radio_logo.png"
    clips = []
    for i, (seg, src) in enumerate(zip(segments, picks)):
        seconds = seg["end"] - seg["start"]
        out = workdir / f"scene_{i:04d}.mp4"
        filters = f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps=24,format=yuv420p"
        if src.suffix.lower() in IMAGE_EXT:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-framerate", "24", "-i", str(src), "-t", f"{seconds:.3f}", "-vf", filters, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", str(out)]
        else:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-stream_loop", "-1", "-i", str(src), "-t", f"{seconds:.3f}", "-vf", filters, "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", str(out)]
        run(cmd)
        clips.append(out)
        progress.progress((i+1)/(count+1), text=f"Rendering scene {i+1} of {count}")
    listing = workdir / "concat.txt"
    listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in clips), encoding="utf-8")
    assembled = workdir / "assembled.mp4"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(assembled)])
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(assembled), "-i", str(audio)]
    if logo.exists():
        cmd += ["-i", str(logo), "-filter_complex", f"[2:v]scale={int(w*0.13)}:-1[logo];[0:v][logo]overlay=W-w-28:28[v]", "-map", "[v]"]
    else:
        cmd += ["-map", "0:v:0"]
    cmd += ["-map", "1:a:0", "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", str(target)]
    run(cmd)
    progress.progress(1.0, text="Video ready")
    return target

st.title("🎬 AI Radio Video Studio")
st.caption("Cloud Edition • SUNO music videos • AI scene artwork • AI Radio watermark")
if st.button("Lock studio"):
    st.session_state.authenticated = False
    st.rerun()
st.info("Files are temporary on the cloud server. Download completed videos immediately. Long 1080p renders may exceed free hosting limits.")
audio_upload = st.file_uploader("1. Upload SUNO song (MP3 or WAV)", type=["mp3", "wav", "m4a"])
lyrics = st.text_area("2. Paste lyrics or describe the message (optional)", height=180, placeholder="Heal and restore our land...")
resolution = st.selectbox("3. Video quality", ["720p", "1080p"], index=0)
mode = st.radio("4. Visual source", ["Use my footage (no AI charges)", "Generate original AI scene artwork (paid API)"])
footage_uploads = []
scene_count = 8
if mode.startswith("Use my footage"):
    footage_uploads = st.file_uploader("5. Upload licensed footage or images", type=["mp4", "mov", "mkv", "webm", "jpg", "jpeg", "png"], accept_multiple_files=True)
else:
    st.warning("Generates paid AI still images, then animates them into video. It does not generate native AI video clips. API charges apply.")
    scene_count = st.slider("Number of AI scenes (each may incur a charge)", 4, 20, 8)
    st.caption("OpenAI API usage is billed separately from ChatGPT. Check spending limits before proceeding.")

if st.button("🎬 Create full music video", type="primary", use_container_width=True):
    if audio_upload is None:
        st.error("Upload a song first.")
    elif mode.startswith("Use my footage") and not footage_uploads:
        st.error("Upload at least one footage clip or image.")
    else:
        try:
            with tempfile.TemporaryDirectory(prefix="ai_radio_cloud_") as tmp:
                workdir = Path(tmp)
                audio = workdir / ("song" + Path(audio_upload.name).suffix.lower())
                audio.write_bytes(audio_upload.getvalue())
                assets = []
                for i, upload in enumerate(footage_uploads or []):
                    suffix = Path(upload.name).suffix.lower()
                    if suffix not in VIDEO_EXT | IMAGE_EXT:
                        continue
                    dest = workdir / f"asset_{i:03d}_{Path(upload.name).stem[:60]}{suffix}"
                    dest.write_bytes(upload.getvalue())
                    assets.append(dest)
                p = st.progress(0.0, text="Starting...")
                with st.spinner("Rendering video. Please keep this tab open."):
                    if mode.startswith("Generate"):
                        key = str(st.secrets.get("OPENAI_API_KEY", ""))
                        assets = generate_ai_images(lyrics, duration(audio), key, scene_count, p, workdir)
                    output = create_video(audio, lyrics, resolution, p, assets, workdir)
                st.success(f"Video created: {output.name}")
                st.video(output.read_bytes())
                st.download_button("Download MP4", data=output.read_bytes(), file_name=output.name, mime="video/mp4")
        except Exception as exc:
            st.error(str(exc))
