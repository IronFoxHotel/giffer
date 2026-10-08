import http.server
import json
import math
import os
import pathlib
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import webbrowser

import imageio_ffmpeg

ROOT = pathlib.Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(32)
JOBS, SOURCES = {}, {}
LOCK = threading.Lock()
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
TEMP = tempfile.TemporaryDirectory(prefix='link-to-gif-')
CHILD_OPTIONS = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
# Video-only streams are valid input: YouTube commonly has no combined format.
VIDEO_FORMAT = 'bv*[height<=720]/bv*'


def node_path():
    try:
        import nodejs_wheel
        root = pathlib.Path(nodejs_wheel.__file__).resolve().parent
        return str(root / ('node.exe' if os.name == 'nt' else 'bin/node'))
    except ImportError:
        return shutil.which('node') or str(pathlib.Path(sys.base_prefix).parent / 'node/bin/node')


def run(command, timeout):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, **CHILD_OPTIONS)


def download_command(url, folder):
    args = [sys.executable, str(ROOT / 'download.py'), '--ignore-config', '--no-playlist',
            '--socket-timeout', '25', '--retries', '2', '--max-filesize', '100M', '--downloader', 'm3u8:native', '--match-filters', '!is_live',
            '--no-progress', '--write-info-json', '--ffmpeg-location', FFMPEG,
            '-f', VIDEO_FORMAT, '-S', 'res:720,vcodec:h264,proto:https',
            '--restrict-filenames', '-o', str(folder / 'source.%(ext)s')]
    node = node_path()
    if pathlib.Path(node).is_file():
        args += ['--js-runtimes', 'node:' + node]
    return args + ['--', url]


def friendly_error(stderr):
    lines = [line for line in stderr.splitlines() if line.startswith('ERROR:')]
    message = lines[-1] if lines else 'The website did not provide a downloadable video.'
    if 'Requested format' in message:
        return 'The website did not offer a usable video stream. Try again or use a different public link.'
    if 'Sign in' in message or 'not a bot' in message:
        return 'YouTube is asking for sign-in or verification for this video. Loop cannot download it anonymously right now.'
    return message.removeprefix('ERROR: ').strip()[:500]


def load_video(key, url):
    job = JOBS[key]
    folder = pathlib.Path(TEMP.name) / key
    folder.mkdir()
    try:
        job.update(stage='Downloading video for preview…', progress=15)
        result = run(download_command(url, folder), 600)
        if result.returncode:
            raise ValueError(friendly_error(result.stderr))
        candidates = [p for p in folder.glob('source.*') if p.suffix not in ('.json', '.part', '.ytdl')]
        if not candidates:
            raise ValueError('No video was downloaded. The source may exceed the 100 MB limit.')
        source = candidates[0]
        if source.stat().st_size > 100 * 1024**2:
            raise ValueError('The source exceeds the 100 MB limit. Try a shorter video.')
        info_file = folder / 'source.info.json'
        info = json.loads(info_file.read_text()) if info_file.exists() else {}
        if info.get('is_live'):
            raise ValueError('Live streams cannot be trimmed yet. Use a finished video.')
        job.update(stage='Preparing a seekable preview…', progress=70)
        preview = folder / 'preview.mp4'
        # Normalise codec and put the MP4 index first for reliable browser seeking.
        result = run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-threads', '1', '-protocol_whitelist', 'file,pipe,crypto,data', '-i', str(source), '-threads', '1', '-filter_complex_threads', '1',
                      '-map', '0:v:0', '-an', '-vf',
                      "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
                      '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                      '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-y', str(preview)], 600)
        if result.returncode:
            raise ValueError('The downloaded video could not be prepared for preview.')
        reader = imageio_ffmpeg.read_frames(str(preview))
        try:
            metadata = next(reader)
        finally:
            reader.close()
        duration = float(metadata['duration'])
        if not math.isfinite(duration) or duration < 0.5:
            raise ValueError('This video is too short or its length could not be read.')
        SOURCES[key] = {'path': preview, 'duration': duration, 'title': info.get('title') or 'Video'}
        job.update(state='done', stage='Video ready. Scrub to find your clip.', progress=100,
                   source_id=key, duration=duration, title=SOURCES[key]['title'])
    except subprocess.TimeoutExpired:
        job.update(state='error', stage='This video took too long to load. Try a shorter source.')
    except Exception as error:
        job.update(state='error', stage=str(error))
    finally:
        for path in folder.glob('source.*'):
            path.unlink(missing_ok=True)
        if job['state'] == 'error':
            shutil.rmtree(folder, ignore_errors=True)


def convert_gif(key, options):
    job = JOBS[key]
    folder = pathlib.Path(TEMP.name) / key
    folder.mkdir()
    try:
        job.update(stage='Making your GIF…', progress=40)
        source = SOURCES[options['source_id']]['path']
        duration = options['end'] - options['start']
        filters = f"fps={options['fps']},scale={options['width']}:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a"
        out = folder / 'clip.gif'
        result = run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-ss', str(options['start']),
                      '-threads', '1', '-protocol_whitelist', 'file,pipe,crypto,data', '-i', str(source), '-threads', '1', '-filter_complex_threads', '1', '-t', str(duration), '-filter_complex', filters,
                      '-loop', '0', '-y', str(out)], 240)
        if result.returncode or not out.exists() or out.stat().st_size < 50:
            raise ValueError('Could not convert that selection. Adjust the start and end, then try again.')
        job.update(state='done', stage='Your GIF is ready.', progress=100, size=out.stat().st_size)
    except subprocess.TimeoutExpired:
        job.update(state='error', stage='Conversion timed out. Try a shorter clip or smaller size.')
    except Exception as error:
        job.update(state='error', stage=str(error))


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, body, kind='application/json'):
        data = json.dumps(body).encode() if kind == 'application/json' else body
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def authorized(self):
        return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}')

    def serve_file(self, path, kind, head=False):
        total = path.stat().st_size
        start, end, status = 0, total - 1, 200
        requested = self.headers.get('Range')
        if requested:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
            try:
                if not match or not any(match.groups()):
                    raise ValueError()
                left, right = match.groups()
                if left:
                    start = int(left)
                    end = min(int(right), total - 1) if right else total - 1
                else:
                    start = max(0, total - int(right))
                if start > end or start >= total:
                    raise ValueError()
                status = 206
            except ValueError:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{total}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Cache-Control', 'private, no-store')
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{total}')
        self.end_headers()
        if head:
            return
        try:
            with path.open('rb') as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = stream.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        if not self.authorized():
            return self.reply(403, {'error': 'Local access only'})
        path = urllib.parse.urlparse(self.path).path
        if path == '/':
            return self.reply(200, (ROOT / 'index.html').read_text().replace('__TOKEN__', TOKEN).encode(), 'text/html; charset=utf-8')
        parts = path.strip('/').split('/')
        if len(parts) == 2:
            kind, key = parts
            if kind == 'status' and key in JOBS:
                return self.reply(200, JOBS[key].copy())
            if kind == 'media' and key in SOURCES:
                return self.serve_file(SOURCES[key]['path'], 'video/mp4', head)
            if kind == 'gif' and key in JOBS and JOBS[key]['state'] == 'done':
                file = pathlib.Path(TEMP.name) / key / 'clip.gif'
                if file.exists():
                    return self.serve_file(file, 'image/gif', head)
        self.reply(404, {'error': 'Not found. Reload the video if Loop has restarted.'})

    def do_POST(self):
        if not self.authorized() or self.headers.get('X-App-Token') != TOKEN:
            return self.reply(403, {'error': 'Refresh Loop and try again.'})
        if self.path not in ('/load', '/convert'):
            return self.reply(404, {'error': 'Not found'})
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length < 10000:
                raise ValueError('Invalid request.')
            options = json.loads(self.rfile.read(length))
            if self.path == '/load':
                url = options.get('url', '').strip()
                parsed = urllib.parse.urlparse(url)
                if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
                    raise ValueError('Paste a valid public http or https video link.')
                target, args = load_video, (url,)
            else:
                source_id = options['source_id']
                if source_id not in SOURCES:
                    raise ValueError('Load your video first.')
                options = {'source_id': source_id, 'start': float(options['start']), 'end': float(options['end']),
                           'width': int(options['width']), 'fps': int(options['fps'])}
                start, end = options['start'], options['end']
                if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= SOURCES[source_id]['duration'] + 0.05
                        and 0.5 <= end - start <= 30.001 and options['width'] in (320, 480, 640) and options['fps'] in (10, 15, 20)):
                    raise ValueError('Choose a clip of 0.5–30 seconds within the video, with one of the available quality settings.')
                target, args = convert_gif, (options,)
            with LOCK:
                if any(job['state'] == 'working' for job in JOBS.values()):
                    return self.reply(409, {'error': 'Please wait for the current video operation to finish.'})
                key = secrets.token_urlsafe(16)
                JOBS[key] = {'state': 'working', 'stage': 'Starting…', 'progress': 0}
            threading.Thread(target=target, args=(key, *args), daemon=True).start()
            self.reply(202, {'id': key})
        except (ValueError, KeyError, TypeError) as error:
            self.reply(400, {'error': str(error) if isinstance(error, ValueError) else 'Check your video and clip settings.'})


if __name__ == '__main__':
    server = http.server.ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('GIF_PORT', '0'))), Handler)
    address = f'http://127.0.0.1:{server.server_port}'
    print(address, flush=True)
    if '--no-browser' not in sys.argv:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        TEMP.cleanup()
