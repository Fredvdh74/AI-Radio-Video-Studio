"""AI Radio Video Studio: local automated music video assembler."""
from pathlib import Path
import hashlib
import json
import random
import re
import shutil
import subprocess
import tempfile
import time
import streamlit as st
import os
import base64
import requests

ROOT = Path(__file__).resolve().parent
FOOTAGE = ROOT / 'footage'
OUTPUTS = ROOT / 'outputs'
FOOTAGE.mkdir(exist_ok=True)
OUTPUTS.mkdir(exist_ok=True)
VIDEO_EXT = {'.mp4', '.mov', '.mkv', '.webm'}
IMAGE_EXT = {'.jpg', '.jpeg', '.png'}
GENERATED = ROOT / 'generated_scenes'
GENERATED.mkdir(exist_ok=True)


def run(args):
    result = subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout)[-2500:])
    return result.stdout


def duration(path):
    info = json.loads(run(['ffprobe','-v','error','-show_entries','format=duration','-of','json',str(path)]))
    return float(info['format']['duration'])


def story_segments(lyrics, total, count):
    lines = [x.strip() for x in re.split(r'[\n.!?]+', lyrics) if len(x.strip()) > 8]
    if not lines:
        lines = ['A nation in need', 'People praying together', 'Hope and restoration', 'Peace and a brighter future']
    return [{'start': round(total*i/count, 2),
             'end': round(total*(i+1)/count, 2),
             'theme': lines[min(len(lines)-1, i*len(lines)//count)]}
            for i in range(count)]


def choose_media(segments, assets):
    used = {}
    selected = []
    for segment in segments:
        words = set(re.findall(r'[a-z]{3,}', segment['theme'].lower()))
        def score(p):
            name = set(re.findall(r'[a-z]{3,}', p.stem.lower()))
            return len(words & name)*10 - used.get(p, 0)*5
        best = max(assets, key=lambda p: (score(p), random.random()))
        selected.append(best)
        used[best] = used.get(best, 0)+1
    return selected




def generate_ai_images(lyrics, total, key, scene_count, progress):
    """Generate original scene artwork; animate stills during FFmpeg assembly."""
    if not key.strip():
        raise RuntimeError('Enter an OpenAI API key to generate new scenes. API usage is billed separately.')
    segments = story_segments(lyrics, total, scene_count)
    generated = []
    for i, segment in enumerate(segments):
        prompt = (
            'Photorealistic cinematic widescreen 16:9 film still, South African setting when relevant. '
            'A visually compelling scene illustrating: ' + segment['theme'] + '. '
		'Represent South Africa inclusively across the video: Black, White, Coloured and Indian South Africans, diverse cultures, ages and communities, shown with dignity, unity and hope. '
            'Natural lighting, realistic human anatomy, sophisticated cinematography, hopeful atmosphere. '
            'No text, no subtitles, no letters, no logos, no watermark.'
        )
        try:
            response = requests.post(
                'https://api.openai.com/v1/images/generations',
                headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
                json={'model': 'gpt-image-1', 'quality': 'high', 'prompt': prompt, 'size': '1536x1024', 'n': 1},
                timeout=240,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f'Image generation connection failed: {exc}') from exc
        if not response.ok:
            raise RuntimeError(f'Image generation failed (HTTP {response.status_code}): {response.text[:500]}')
        data = response.json()['data'][0]
        if 'b64_json' not in data:
            raise RuntimeError('The image provider did not return image data.')
        out = GENERATED / f'ai_scene_{int(time.time())}_{i+1:03d}.png'
        out.write_bytes(base64.b64decode(data['b64_json']))
        generated.append(out)
        progress.progress((i + 1) / (scene_count + 1), text=f'Generating original scene {i+1}/{scene_count}')
    return generated

def create_video(audio, lyrics, resolution, progress, override_assets=None):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise RuntimeError('FFmpeg / FFprobe not found. Install FFmpeg and add its bin folder to PATH.')
    assets = override_assets if override_assets is not None else sorted(p for p in FOOTAGE.iterdir() if p.suffix.lower() in VIDEO_EXT | IMAGE_EXT)
    if not assets:
        raise RuntimeError('Add at least one licensed MP4 or JPG/PNG to the footage folder before creating a video.')
    total = duration(audio)
    if total < 5:
        raise RuntimeError('The uploaded audio is too short.')
    segment_length = 12
    count = max(1, int(total / segment_length + 0.999))
    segments = story_segments(lyrics, total, count)
    picks = choose_media(segments, assets)
    w,h = (1920,1080) if resolution == '1080p' else (1280,720)
    token = hashlib.sha256(audio.read_bytes()).hexdigest()[:8]
    target = OUTPUTS / f'AI_Radio_{int(time.time())}_{token}.mp4'
    logo = ROOT / 'ai_radio_logo.png'
    with tempfile.TemporaryDirectory(prefix='airadio_') as temp:
        td = Path(temp)
        clips = []
        for i,(seg,src) in enumerate(zip(segments,picks)):
            seconds = seg['end'] - seg['start']
            out = td / f'scene_{i:04d}.mp4'
            filters = f'scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,fps=24,format=yuv420p'
            if src.suffix.lower() in IMAGE_EXT:
                cmd = ['ffmpeg','-hide_banner','-loglevel','error','-y','-loop','1','-framerate','24','-i',str(src),'-t',f'{seconds:.3f}', '-vf',filters,'-an','-c:v','libx264','-preset','veryfast','-crf','24',str(out)]
            else:
                cmd = ['ffmpeg','-hide_banner','-loglevel','error','-y','-stream_loop','-1','-i',str(src),'-t',f'{seconds:.3f}', '-vf',filters,'-an','-c:v','libx264','-preset','veryfast','-crf','24',str(out)]
            run(cmd)
            clips.append(out)
            progress.progress((i+1)/(count+1), text=f'Rendering scene {i+1} of {count}')
        listing = td / 'concat.txt'
        listing.write_text(''.join(f"file '{p.as_posix()}'\n" for p in clips), encoding='utf-8')
        assembled = td / 'assembled.mp4'
        run(['ffmpeg','-hide_banner','-loglevel','error','-y','-f','concat','-safe','0','-i',str(listing),'-c','copy',str(assembled)])
        cmd = ['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(assembled),'-i',str(audio)]
        if logo.exists():
            cmd += ['-i',str(logo),'-filter_complex',f'[2:v]scale={int(w*0.13)}:-1[logo];[0:v][logo]overlay=W-w-28:28[v]', '-map','[v]']
        else:
            cmd += ['-map','0:v:0']
        cmd += ['-map','1:a:0','-t',f'{total:.3f}','-c:v','libx264','-preset','veryfast','-crf','22','-c:a','aac','-b:a','256k','-movflags','+faststart',str(target)]
        run(cmd)
    progress.progress(1.0, text='Video ready')
    (target.with_suffix('.storyboard.json')).write_text(json.dumps([{'scene':i+1, 'theme':s['theme'], 'footage':p.name} for i,(s,p) in enumerate(zip(segments,picks))],indent=2),encoding='utf-8')
    return target

st.set_page_config(page_title='AI Radio Video Studio', page_icon='🎬', layout='centered')
st.title('🎬 AI Radio Video Studio')
st.caption('Create a full-length music video using your footage or newly generated AI scene artwork.')
with st.expander('How to supply footage and your watermark'):
    st.write(f'Place licensed MP4 clips or JPG/PNG images in: `{FOOTAGE}`')
    st.write(f'For the watermark, save a transparent PNG as `{ROOT / "ai_radio_logo.png"}`.')
    st.write('Name clips by topic (e.g. `prayer_church.mp4`, `south_africa_sunrise.mp4`). Matching is keyword-based in this first version.')
audio_upload = st.file_uploader('1. Upload SUNO song (MP3 or WAV)', type=['mp3','wav','m4a'])
lyrics = st.text_area('2. Paste lyrics or describe the message (optional)', height=180, placeholder='Heal and restore our land...')
resolution = st.selectbox('3. Video quality', ['720p','1080p'], index=1)
mode = st.radio('4. Visual source', ['Use my footage (no AI charges)', 'Generate original AI scene artwork (paid API)'])
key = ''
scene_count = 8
if mode.startswith('Generate'):
    st.warning('This mode generates new AI **images**, then animates them as video scenes. It does NOT yet generate native moving AI video clips. Image generation incurs API charges.')
    key = st.text_input('OpenAI API key (kept in this running session)', type='password')
    scene_count = st.slider('Number of AI scenes (each image may incur a charge)', 4, 20, 8)
    st.caption('Check your API pricing and spending limits before proceeding. Your ChatGPT subscription does not include API usage.')

assets = [p for p in FOOTAGE.iterdir() if p.suffix.lower() in VIDEO_EXT | IMAGE_EXT]
st.info(f'{len(assets)} footage file(s) found in your local library.')
if st.button('🎬 Create full music video', type='primary', use_container_width=True):
    if audio_upload is None:
        st.error('Upload a song first.')
    else:
        try:
            with tempfile.TemporaryDirectory(prefix='airadio_audio_') as tmp:
                path = Path(tmp) / ('song'+Path(audio_upload.name).suffix.lower())
                path.write_bytes(audio_upload.getvalue())
                p = st.progress(0.0, text='Starting...')
                with st.spinner('Automatically rendering your complete video. Please keep this window open.'):
                    override = None
                    if mode.startswith('Generate'):
                        if not shutil.which('ffprobe'):
                            raise RuntimeError('FFprobe is required to measure the audio duration.')
                        override = generate_ai_images(lyrics, duration(path), key, scene_count, p)
                    output = create_video(path, lyrics, resolution, p, override_assets=override)
                st.success(f'Video created: {output.name}')
                st.video(str(output))
                with open(output,'rb') as f:
                    st.download_button('Download MP4', data=f, file_name=output.name, mime='video/mp4')
        except Exception as exc:
            st.error(str(exc))
