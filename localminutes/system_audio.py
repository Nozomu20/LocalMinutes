"""OSの出力音声を録音する子プロセスの起動・音量表示・終了処理。"""
from collections import deque
import json
import math
import os
import platform
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def helper_command(path):
    if sys.platform == "darwin":
        if int(platform.mac_ver()[0].split(".")[0]) < 13:
            raise ValueError("PC音声の録音にはmacOS 13以降が必要です。")
        helper = ROOT / "native" / "macos-system-audio"
        if not os.access(helper, os.X_OK):
            raise ValueError("システム音声用の実行ファイルが見つかりません。最新版のZIPを展開してください。")
        return [str(helper), str(path)]
    if sys.platform == "win32":
        return [sys.executable, "-m", "localminutes.windows_loopback", str(path)]
    raise ValueError("PC音声の録音はWindowsとmacOSで利用できます。")


class SystemAudio:
    def __init__(self, path):
        env = {**os.environ, "PYTHONUNBUFFERED":"1", "PYTHONIOENCODING":"utf-8",
               "PYTHONDONTWRITEBYTECODE":"1"}
        self.proc = subprocess.Popen(helper_command(path), cwd=ROOT, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, encoding="utf-8", errors="replace",
                                     start_new_session=os.name!="nt",
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.ready = threading.Event()
        self.lock = threading.Lock()
        self.start_time = None
        self.device = ""
        self.error = ""
        self.stopping = False
        self.levels = deque(maxlen=64)
        self.heard = False
        self.reader = threading.Thread(target=self._listen, daemon=True)
        self.reader.start()

    def _listen(self):
        try:
            for line in self.proc.stdout:
                event = json.loads(line)
                kind = event.get("event")
                with self.lock:
                    if kind == "ready":
                        stamp = float(event["startMonotonic"])
                        if not math.isfinite(stamp) or abs(time.monotonic()-stamp) > 90:
                            raise ValueError("システム音声の開始時刻を確認できません。")
                        self.start_time = stamp
                        self.device = str(event.get("device") or "システム出力")
                        self.ready.set()
                    elif kind == "error":
                        self.error = str(event.get("message") or "システム音声を取得できません。")
                        self.ready.set()
                    elif kind == "level":
                        peak = min(1.0,max(0.0,float(event["peak"])))
                        self.levels.append((time.monotonic(),peak))
                        self.heard |= peak >= 0.001
        except (OSError,ValueError,KeyError,TypeError):
            with self.lock:
                self.error = "システム音声の録音状態を読み取れませんでした。"
        finally:
            self.proc.stdout.close()
            with self.lock:
                if not self.stopping and not self.error:
                    self.error = "システム音声の録音が中断されました。出力先やOSの許可を確認してください。"
            self.ready.set()

    def wait_ready(self):
        if not self.ready.wait(65):
            raise ValueError("システム音声の開始がタイムアウトしました。OSの録音許可と出力先を確認してください。")
        with self.lock:
            if self.error or self.start_time is None:
                raise ValueError(self.error or "システム音声の開始前に停止しました。")
            return self.start_time

    def status(self):
        with self.lock:
            cutoff = time.monotonic()-2
            peak = max((p for t,p in self.levels if t>=cutoff), default=0)
            return {"peak":peak, "heard":self.heard, "device":self.device, "error":self.error}

    def stop(self):
        with self.lock:
            if self.stopping:
                return
            self.stopping = True
        try:
            # EOFは親プロセスの異常終了時にも子へ届く。
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=7)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.reader.join(timeout=2)
        with self.lock:
            if self.proc.returncode and not self.error:
                self.error = "システム音声の保存が中断されました。保存済みの範囲を確認してください。"
