"""Local development server for the Astris web build: the static files with the headers
WebAssembly threads need (COOP/COEP), launch logs posted to /log, and an in-process
WebSocket↔TCP bridge at /minecraft-tcp so the browser can join a local dedicated server.

The game's resources do not come from here: the player imports their Horizon-prepared
folder in the launcher (horizon-import.js), and the game reads it from OPFS."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
import base64
import hashlib
import json
import os
import selectors
import socket
import struct
import threading

LOG_KEEP = 50
LOG_MAX_POST = 4 << 20

WEB_ROOT = Path(__file__).resolve().parent
LOG_ROOT = WEB_ROOT / 'logs'
LOG_LOCK = threading.Lock()
# Never served: the logs (paths and names of the player's folders), local caches, and
# the server's own files. Decided on the resolved file, so no spelling of the URL
# (%6Cogs, LOGS on Windows, //logs) gets around it.
PRIVATE_DIRS = (LOG_ROOT, WEB_ROOT / '.cache')
PRIVATE_SUFFIXES = ('.log', '.py', '.ps1')
# The address to listen on: this computer only, unless ASTRIS_HOST says otherwise
# (0.0.0.0 shares it on the local network, for trying it from other devices).
BIND = os.environ.get('ASTRIS_HOST', '127.0.0.1')

WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
WS_PATH = '/minecraft-tcp'
DEFAULT_MC = ('127.0.0.1', 25565)


def own_names():
    names = {'127.0.0.1', 'localhost', '[::1]'}
    # Names a proxy in front of it answers to, e.g. tailscale serve's https name.
    names.update(n.lower() for n in os.environ.get('ASTRIS_NAMES', '').split())
    if BIND != '127.0.0.1':
        host = socket.gethostname()
        names.add(host.lower())
        for info in socket.getaddrinfo(host, None):
            address = info[4][0]
            names.add(f'[{address}]' if ':' in address else address)
    return tuple(names)


# Only these names reach the server, this computer's own: a page on another domain that
# points its name at this computer (DNS rebinding) is turned away.
LOCAL_HOSTS = own_names()


def _allowed_mc_host(host: str) -> bool:
    h = host.lower().strip('[]')
    if h in ('localhost', '127.0.0.1', '::1'):
        return True
    if h.startswith('192.168.') or h.startswith('10.'):
        return True
    if h.startswith('172.'):
        try:
            second = int(h.split('.')[1])
            return 16 <= second <= 31
        except (IndexError, ValueError):
            return False
    return False


def _parse_mc_target(raw: str | None) -> tuple[str, int]:
    if not raw:
        return DEFAULT_MC
    raw = raw.strip()
    if raw.startswith('[') and ']' in raw:
        host, _, rest = raw[1:].partition(']')
        port = int(rest[1:]) if rest.startswith(':') and rest[1:] else 25565
        return host, port
    if raw.count(':') == 1:
        host, port_s = raw.split(':')
        return host, int(port_s)
    return raw, 25565


def _ws_accept(key: str) -> str:
    digest = hashlib.sha1((key + WS_GUID).encode('ascii')).digest()
    return base64.b64encode(digest).decode('ascii')


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError('closed')
        buf.extend(chunk)
    return bytes(buf)


def _read_ws_frame(sock: socket.socket) -> tuple[int, bytes]:
    b1, b2 = _recv_exact(sock, 2)
    opcode = b1 & 0x0F
    masked = (b2 & 0x80) != 0
    length = b2 & 0x7F
    if length == 126:
        (length,) = struct.unpack('!H', _recv_exact(sock, 2))
    elif length == 127:
        (length,) = struct.unpack('!Q', _recv_exact(sock, 8))
    mask = _recv_exact(sock, 4) if masked else b''
    payload = _recv_exact(sock, length) if length else b''
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _send_ws_frame(sock: socket.socket, opcode: int, payload: bytes) -> None:
    header = bytearray()
    header.append(0x80 | (opcode & 0x0F))
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < 65536:
        header.append(126)
        header.extend(struct.pack('!H', n))
    else:
        header.append(127)
        header.extend(struct.pack('!Q', n))
    sock.sendall(header + payload)


def _bridge_minecraft(client: socket.socket, target: tuple[str, int]) -> None:
    host, port = target
    if not _allowed_mc_host(host):
        try:
            _send_ws_frame(client, 0x8, b'\x03\xe8denied')
        except OSError:
            pass
        client.close()
        return
    try:
        remote = socket.create_connection((host, port), timeout=10)
    except OSError as exc:
        try:
            _send_ws_frame(client, 0x8, f'tcp:{exc}'.encode()[:120])
        except OSError:
            pass
        client.close()
        return
    remote.setblocking(False)
    client.setblocking(False)
    sel = selectors.DefaultSelector()
    sel.register(client, selectors.EVENT_READ)
    sel.register(remote, selectors.EVENT_READ)
    try:
        while True:
            for key, _ in sel.select(timeout=60):
                if key.fileobj is client:
                    try:
                        opcode, payload = _read_ws_frame(client)
                    except (ConnectionError, OSError, struct.error):
                        return
                    if opcode == 0x8:
                        return
                    if opcode in (0x1, 0x2) and payload:
                        remote.sendall(payload)
                    elif opcode == 0x9:
                        _send_ws_frame(client, 0xA, payload)
                else:
                    try:
                        data = remote.recv(65536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        return
                    if not data:
                        return
                    _send_ws_frame(client, 0x2, data)
    finally:
        for sock in (remote, client):
            try:
                sock.close()
            except OSError:
                pass


class Handler(SimpleHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, '.js': 'text/javascript', '.wasm': 'application/wasm'}

    def end_headers(self):
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('Cross-Origin-Embedder-Policy', 'require-corp')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        # Not to be framed by another site, nor read as another content type.
        self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        # Revalidate instead of re-downloading: the 30 MB wasm answers 304 when unchanged.
        self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def _refused(self):
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].lower()
        if host not in LOCAL_HOSTS:
            self.send_error(421, 'Unknown host')
            return True
        target = Path(self.translate_path(self.path))
        if any(target == d or target.is_relative_to(d) for d in PRIVATE_DIRS) or target.suffix.lower() in PRIVATE_SUFFIXES:
            self.send_error(404)
            return True
        return False

    def _same_site(self):
        # The launcher's own requests only: a page on another site could otherwise wipe
        # this site's storage (Clear-Site-Data) or fill the logs with a plain form post.
        fetch_site = self.headers.get('Sec-Fetch-Site')
        origin = self.headers.get('Origin')
        if fetch_site not in (None, 'same-origin') or (origin and urlsplit(origin).netloc.rsplit(':', 1)[0].lower() not in LOCAL_HOSTS):
            self.send_error(403, 'Cross-site request refused')
            return False
        return True

    def _minecraft_tcp_upgrade(self) -> bool:
        """Same-origin WS↔TCP for dedicated joins. Returns True if handled."""
        path = urlsplit(self.path).path
        if path != WS_PATH:
            return False
        if self.headers.get('Upgrade', '').lower() != 'websocket':
            self.send_error(426, 'Upgrade Required')
            return True
        key = self.headers.get('Sec-WebSocket-Key')
        if not key:
            self.send_error(400, 'Missing Sec-WebSocket-Key')
            return True
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].lower()
        if host not in LOCAL_HOSTS:
            self.send_error(421, 'Unknown host')
            return True
        query = parse_qs(urlsplit(self.path).query)
        target = _parse_mc_target((query.get('target') or [None])[0])
        self.close_connection = True
        self.send_response(101, 'Switching Protocols')
        self.send_header('Upgrade', 'websocket')
        self.send_header('Connection', 'Upgrade')
        self.send_header('Sec-WebSocket-Accept', _ws_accept(key))
        # No COEP on the upgrade response — this is a raw socket after 101.
        SimpleHTTPRequestHandler.end_headers(self)
        print(f'Astris: WS↔TCP bridge → {target[0]}:{target[1]}', flush=True)
        _bridge_minecraft(self.connection, target)
        return True

    def _json(self, code: int, payload: dict | list | str):
        if isinstance(payload, str):
            body = payload.encode()
        else:
            body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fetch_proxy(self) -> bool:
        """Same-origin GET proxy for skin/cape/profile URLs (wasm has no ureq)."""
        path = urlsplit(self.path).path
        if path != '/fetch':
            return False
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].lower()
        if host not in LOCAL_HOSTS:
            self.send_error(421, 'Unknown host')
            return True
        if not self._same_site():
            return True
        raw = (parse_qs(urlsplit(self.path).query).get('url') or [''])[0]
        try:
            target = urlsplit(raw)
        except ValueError:
            self.send_error(400, 'Bad url')
            return True
        if target.scheme not in ('http', 'https') or not target.hostname:
            self.send_error(400, 'Bad url')
            return True
        allowed = (
            'textures.minecraft.net',
            'sessionserver.mojang.com',
            'api.minecraftservices.com',
            'launchercontent.mojang.com',
            'minecraft.wiki',
            'crafatar.com',
            'minotar.net',
            'mc-heads.net',
            'visage.surgeplay.com',
        )
        hostname = target.hostname.lower()
        if hostname not in allowed and not hostname.endswith('.minecraft.net'):
            self.send_error(403, 'Host not allowed')
            return True
        try:
            import urllib.request
            req = urllib.request.Request(
                raw,
                headers={'User-Agent': 'Java', 'Accept': '*/*'},
                method='GET',
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read(16 << 20)
                ctype = resp.headers.get('Content-Type', 'application/octet-stream')
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                self.wfile.write(body)
        except Exception as e:  # noqa: BLE001
            self.send_error(502, f'fetch failed: {e}')
        return True

    def _auth_get(self) -> bool:
        """MSA session mint / device-code for the browser client. Returns True if handled."""
        path = urlsplit(self.path).path
        if path not in ('/auth/session', '/auth/device/start', '/auth/device/poll'):
            return False
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].lower()
        if host not in LOCAL_HOSTS:
            self.send_error(421, 'Unknown host')
            return True
        if not self._same_site():
            return True
        try:
            import msa_session
        except ImportError as e:
            self._json(500, {'error': f'msa_session missing: {e}'})
            return True
        try:
            if path == '/auth/session':
                force = parse_qs(urlsplit(self.path).query).get('force', ['0'])[0] in ('1', 'true')
                session = msa_session.mint_session(force=force)
                # Never put the access token in the access log.
                print(f"Astris: MSA session for {session.get('name')} via {session.get('source')}", flush=True)
                self._json(200, session)
            elif path == '/auth/device/start':
                self._json(200, msa_session.device_start())
            else:
                self._json(200, msa_session.device_poll())
        except Exception as e:  # noqa: BLE001
            self._json(401, {'error': str(e), 'need_sign_in': True})
        return True

    def do_GET(self):
        if self._minecraft_tcp_upgrade():
            return
        if self._fetch_proxy():
            return
        if self._auth_get():
            return
        if not self._refused():
            super().do_GET()

    def do_HEAD(self):
        if urlsplit(self.path).path == WS_PATH:
            self.send_error(426, 'Upgrade Required')
            return
        if not self._refused():
            super().do_HEAD()

    def translate_path(self, path):
        target = (WEB_ROOT / unquote(urlsplit(path).path).lstrip('/')).resolve()
        if not target.is_relative_to(WEB_ROOT):
            return str(WEB_ROOT / '.missing')
        return str(target)

    def list_directory(self, path):
        self.send_error(403, 'Directory listing disabled')

    def do_POST(self):
        host = (self.headers.get('Host') or '').rsplit(':', 1)[0].lower()
        if host not in LOCAL_HOSTS:
            return self.send_error(421, 'Unknown host')
        if not self._same_site():
            return
        path = urlsplit(self.path).path
        if path == '/clear-site-data':
            # The launcher's "delete all local data": the browser drops all of the site's
            # storage, the space it still counts for unfinished writes included.
            self.send_response(204)
            self.send_header('Clear-Site-Data', '"storage"')
            self.end_headers()
            return
        if path == '/auth/join':
            length = min(int(self.headers.get('Content-Length', '0')), 1 << 20)
            raw = self.rfile.read(length) if length > 0 else b'{}'
            try:
                import msa_session
                body = json.loads(raw.decode() or '{}')
                status, text = msa_session.join_forward(body)
            except Exception as e:  # noqa: BLE001
                return self._json(500, {'error': str(e)})
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            out = text.encode()
            self.send_header('Content-Length', str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if path != '/log':
            return self.send_error(404)
        length = min(int(self.headers.get('Content-Length', '0')), LOG_MAX_POST)
        body = self.rfile.read(length) if length > 0 else b''
        # Launch logs: one file per launch in logs/, the newest LOG_KEEP kept.
        session = parse_qs(urlsplit(self.path).query).get('session', ['unknown'])[0]
        session = ''.join(c for c in session if c.isalnum() or c == '-')[:64] or 'unknown'
        with LOG_LOCK:
            LOG_ROOT.mkdir(exist_ok=True)
            target = LOG_ROOT / f'{session}.log'
            fresh = not target.exists()
            with target.open('ab') as f:
                f.write(body)
                if not body.endswith(b'\n'):
                    f.write(b'\n')
            if fresh:
                logs = sorted(LOG_ROOT.glob('*.log'), key=lambda p: p.stat().st_mtime)
                for stale in logs[:-LOG_KEEP]:
                    stale.unlink(missing_ok=True)
        self.send_response(204)
        self.end_headers()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', '8766'))
    # Allow `python3 dev_server.py 8766` as used by existing launches.
    import sys
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])
    server = ThreadingHTTPServer((BIND, port), partial(Handler, directory=str(WEB_ROOT)))
    for name in sorted(LOCAL_HOSTS):
        print(f'Astris: http://{name}:{port}/', flush=True)
    print(f'Astris: Minecraft WS↔TCP ws://127.0.0.1:{port}{WS_PATH}?target=127.0.0.1:25565', flush=True)
    server.serve_forever()
