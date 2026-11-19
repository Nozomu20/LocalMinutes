"""Local Minutes のループバック専用サーバー。"""
import argparse
import hmac
import json
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import sys
import threading
from urllib.parse import unquote, urlsplit
import webbrowser

from localminutes.store import Library, ROOT, LIMIT

ASSETS = {"index.html":"text/html; charset=utf-8", "style.css":"text/css",
          "app.js":"text/javascript", "recorder.js":"text/javascript", "pcm-worklet.js":"text/javascript",
          "speakers.js":"text/javascript", "speakers.css":"text/css"}


class Handler(BaseHTTPRequestHandler):
    # 会議名や本文を HTTP ログに残さない。
    def log_message(self, *args):
        pass

    def reply(self, payload, status=200, mime="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "microphone=(self), camera=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; worker-src 'self'; media-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def body(self, limit):
        n = int(self.headers.get("Content-Length", 0))
        if not 0 <= n <= limit:
            raise ValueError("データが大きすぎます。")
        data = self.rfile.read(n)
        if len(data) != n:
            raise ValueError("受信が中断されました。")
        return data

    def audio_ticket(self, key):
        return hmac.digest(self.server.secret.encode(), key.encode(), "sha256").hex()

    def stream_audio(self, path):
        size = path.stat().st_size
        interval = self.headers.get("Range")
        start, end = 0, size - 1
        if interval:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", interval)
            if not match or not any(match.groups()):
                return self.reply({"error":"再生範囲が不正です。"},416)
            left, right = match.groups()
            start = int(left) if left else max(0, size-int(right))
            end = min(end,int(right)) if left and right else end
            if start > end:
                return self.reply({"error":"再生範囲が音声を超えています。"},416)
        with path.open("rb") as f:
            f.seek(start)
            self.send_response(206 if interval else 200)
            if interval:
                self.send_header("Content-Range",f"bytes {start}-{end}/{size}")
            self.send_header("Content-Type",mimetypes.guess_type(path.name)[0] or "application/octet-stream")
            self.send_header("Cache-Control","no-store")
            self.send_header("Cross-Origin-Resource-Policy","same-origin")
            self.send_header("X-Content-Type-Options","nosniff")
            self.send_header("Accept-Ranges","bytes")
            self.send_header("Content-Length",str(end-start+1))
            self.end_headers()
            remaining = end-start+1
            while remaining:
                chunk = f.read(min(65536,remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def dispatch(self):
        self.connection.settimeout(90)
        server = self.server
        host = f"127.0.0.1:{server.server_port}"
        if self.headers.get("Host") != host or self.headers.get("Origin", "http://"+host) != "http://"+host:
            return self.reply({"error":"接続元を確認してください。"},403)
        path = urlsplit(self.path).path
        file = "index.html" if path == "/" else path.removeprefix("/static/")
        if self.command == "GET" and file in ASSETS and (path == "/" or path.startswith("/static/")):
            return self.reply((ROOT/"static"/file).read_bytes(), mime=ASSETS[file])
        media = re.fullmatch(r"/audio/([a-f0-9]{32})/([a-f0-9]{64})", path)
        if self.command == "GET" and media and hmac.compare_digest(media[2], self.audio_ticket(media[1])):
            return self.stream_audio(server.library.audio_file(media[1]))
        if not hmac.compare_digest(self.headers.get("Authorization",""), "Bearer "+server.secret):
            return self.reply({"error":"起動時に表示されたURLから開いてください。"},403)
        lib = server.library
        if self.command == "GET":
            if path == "/api/state":
                state = lib.state()
                for m in state["meetings"]:
                    if m["status"] not in ("importing","recording"):
                        try:
                            lib.audio_file(m["id"])
                        except ValueError:
                            continue
                        m["playback"] = "/audio/"+m["id"]+"/"+self.audio_ticket(m["id"])
                return self.reply(state)
            if path.startswith("/api/result/"):
                return self.reply(lib.result(path.split("/")[-1]))
        if self.command == "POST":
            if path == "/api/import":
                size = int(self.headers.get("Content-Length",0))
                if not 0 < size <= LIMIT:
                    raise ValueError("空のファイル、または2 GiBを超えるファイルです。")
                key = lib.import_audio(unquote(self.headers.get("X-Title","")), self.headers.get("X-Extension",""),
                                       self.rfile, size, int(self.headers.get("X-Speaker-Count","0")))
                return self.reply({"id":key})
            if path.startswith("/api/chunk/"):
                lib.chunk(path.split("/")[-1], self.body(2*1024**2))
                return self.reply({})
            data = json.loads(self.body(2*1024**2 if path == "/api/speakers" else 16384) or b"{}")
            if path == "/api/start":
                return self.reply({"id":lib.start(data.get("title"),data.get("rate"),data.get("count",0),
                                                  data.get("mode","microphone"))})
            if path == "/api/stop":
                lib.stop(data["id"],bool(data.get("interrupted")))
            elif path == "/api/process":
                lib.process(data["id"])
            elif path == "/api/settings":
                lib.configure(data)
            elif path == "/api/speakers":
                lib.edit_speakers(data["id"],data["speakers"],data["names"],data["revision"])
            else:
                return self.reply({"error":"見つかりません。"},404)
            return self.reply({})
        self.reply({"error":"見つかりません。"},404)

    def handle_request(self):
        try:
            self.dispatch()
        except (ConnectionError, TimeoutError):
            pass
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            self.reply({"error":str(e)},400)
    do_GET = do_POST = handle_request


class Server(ThreadingHTTPServer):
    def service_actions(self):
        self.library.tick()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser",action="store_true")
    parser.add_argument("--data-dir",type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    base = Path(os.environ.get("LOCALAPPDATA", Path.home()/"AppData/Local")) if os.name=="nt" else Path.home()/"Library/Application Support"
    lib = Library(args.data_dir or base/"LocalMinutes")
    with Server(("127.0.0.1",0),Handler) as server:
        server.library, server.secret = lib, secrets.token_urlsafe(32)
        url = f"http://127.0.0.1:{server.server_port}/#{server.secret}"
        print("Local Minutes を開くには、この端末のブラウザで次のURLを開いてください。",flush=True)
        print(url,flush=True)
        print("終了するには、録音・処理が終わってからこのウィンドウで Ctrl+C を押してください。",flush=True)
        if not args.no_browser:
            threading.Timer(0.25,webbrowser.open,args=(url,)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            lib.close()


if __name__ == "__main__":
    main()
