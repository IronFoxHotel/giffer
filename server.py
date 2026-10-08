"""Small shared Giffer service. One conversion worker fits the free instance."""
import concurrent.futures
import hmac
import ipaddress
import os
import pathlib
import secrets
import shutil
import socket
import threading
import time
import urllib.parse

from flask import Flask, jsonify, request, send_file, session
import engine

app = Flask(__name__)
app.secret_key = os.environ.get('APP_SECRET') or secrets.token_hex(32)
app.config.update(MAX_CONTENT_LENGTH=10000, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=bool(os.environ.get('RENDER')))
executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
lock = threading.Lock()
last_requests = {}


def identity():
    if 'owner' not in session:
        session['owner'] = secrets.token_urlsafe(24)
        session['csrf'] = secrets.token_urlsafe(32)
    return session['owner']


def cleanup():
    # Do not remove source media while the single worker is using it.
    if any(job['state'] in ('working', 'queued') for job in engine.JOBS.values()):
        return
    cutoff = time.time() - 1800
    for key, job in list(engine.JOBS.items()):
        if job['created'] < cutoff:
            engine.SOURCES.pop(key, None)
            engine.JOBS.pop(key, None)
            shutil.rmtree(pathlib.Path(engine.TEMP.name) / key, ignore_errors=True)
    for owner, times in list(last_requests.items()):
        if not times or times[-1] < cutoff:
            last_requests.pop(owner, None)


def owns(key):
    job = engine.JOBS.get(key)
    return job and job.get('owner') == session.get('owner')


@app.after_request
def headers(response):
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    return response


@app.get('/health')
def health():
    return {'status': 'ok'}


@app.get('/')
def home():
    identity()
    return (engine.ROOT / 'index.html').read_text().replace('__TOKEN__', session['csrf'])


@app.get('/status/<key>')
def status(key):
    if not owns(key):
        return {'error': 'This preview expired. Load the video again.'}, 404
    return {k: v for k, v in engine.JOBS[key].copy().items() if k not in ('owner', 'created')}


@app.get('/media/<key>')
def media(key):
    if not owns(key) or key not in engine.SOURCES:
        return {'error': 'Preview expired. Load it again.'}, 404
    return send_file(engine.SOURCES[key]['path'], mimetype='video/mp4', conditional=True)


@app.get('/gif/<key>')
def gif(key):
    path = pathlib.Path(engine.TEMP.name) / key / 'clip.gif'
    if not owns(key) or not path.is_file():
        return {'error': 'GIF not found. Please create it again.'}, 404
    return send_file(path, mimetype='image/gif', download_name='giffer.gif', conditional=True)


def public_url(value):
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError('Use a public HTTPS video link.')
    addresses = socket.getaddrinfo(parsed.hostname, 443)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError('Only public internet video links are supported.')
    return value


def work(key, target, arguments):
    engine.JOBS[key].update(state='working', stage='Starting…')
    target(key, *arguments)


@app.post('/load')
@app.post('/convert')
def create():
    owner = identity()
    if not hmac.compare_digest(request.headers.get('X-App-Token', ''), session['csrf']):
        return {'error': 'Refresh Giffer and try again.'}, 403
    try:
        body = request.get_json() or {}
        if request.path == '/load':
            url = public_url(str(body.get('url', '')).strip())
            target, arguments = engine.load_video, (url,)
        else:
            key = body['source_id']
            if not owns(key) or key not in engine.SOURCES:
                raise ValueError('Load your video again; its preview may have expired.')
            start, end = float(body['start']), float(body['end'])
            width, fps = int(body['width']), int(body['fps'])
            if not (0 <= start < end <= engine.SOURCES[key]['duration'] + .05 and .5 <= end-start <= 30.001
                    and width in (320, 480, 640) and fps in (10, 15, 20)):
                raise ValueError('Choose a clip of 0.5–30 seconds within the video.')
            target = engine.convert_gif
            arguments = ({'source_id': key, 'start': start, 'end': end, 'width': width, 'fps': fps},)
        with lock:
            if request.path == '/convert':
                engine.JOBS[body['source_id']]['created'] = time.time()
            cleanup()
            pending = [job for job in engine.JOBS.values() if job['state'] in ('working', 'queued')]
            if any(job['owner'] == owner for job in pending):
                return {'error': 'Your video is still processing. Please wait.'}, 409
            if len(pending) >= 3:
                return {'error': 'Giffer is busy. Please try again in a minute.'}, 429
            now = time.time()
            recent = [t for t in last_requests.get(owner, []) if now-t < 300]
            if len(recent) >= 8:
                return {'error': 'Please wait a few minutes before making more requests.'}, 429
            used = sum(p.stat().st_size for p in pathlib.Path(engine.TEMP.name).rglob('*') if p.is_file())
            if used > 400 * 1024**2:
                return {'error': 'Temporary storage is full. Try again in 30 minutes.'}, 503
            key = secrets.token_urlsafe(24)
            engine.JOBS[key] = {'state': 'queued', 'stage': 'Waiting for the current conversion…' if pending else 'Starting…',
                                'progress': 0, 'owner': owner, 'created': now}
            last_requests[owner] = recent + [now]
            executor.submit(work, key, target, arguments)
        return {'id': key}, 202
    except (ValueError, KeyError, TypeError, OSError) as error:
        return {'error': str(error) if isinstance(error, ValueError) else 'Check your link and clip settings.'}, 400
