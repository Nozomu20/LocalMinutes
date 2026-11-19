"""会議ごとの保存と、同時に一件だけ行う録音・推論の管理。"""
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid
import wave
from .system_audio import SystemAudio

ROOT = Path(__file__).resolve().parents[1]
FORMATS = {".wav", ".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".webm", ".aac", ".aiff"}
LIMIT = 2 * 1024**3


def save_json(path, value):
    stage = path.with_suffix(".tmp")
    stage.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    stage.replace(path)


def local_path(value, suffix):
    if not isinstance(value, str) or value.startswith(("//", "\\\\")):
        raise ValueError("端末内のモデルのパスを指定してください。")
    path = Path(value.strip()).expanduser()
    if not path.is_absolute() or path.suffix.lower() != suffix or not path.is_file():
        raise ValueError(f"保存済みの {suffix} ファイルの絶対パスを指定してください。")
    return str(path)


def speaker_count(value):
    if type(value) is not int or not 0 <= value <= 12:
        raise ValueError("話者数は自動または1〜12人を選択してください。")
    return value


def named_summary(text, segments, names):
    source = re.compile(r"^(- \([^\n)]*\))(?:【話者\d+】)?<!--lm:(\d+)-->", re.M)
    def attribution(match):
        index = int(match[2])
        who = segments[index].get("speaker", "") if index < len(segments) else ""
        name = names.get(who) or ("話者"+who[1:] if who else "話者未設定")
        name = re.sub(r"([\\`*_\[\]~|])",r"\\\1",
                      name.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;"))
        return f"{match[1]}【{name}】"
    return source.sub(attribution, text)


class Library:
    def __init__(self, root):
        self.root = root = root.expanduser().resolve()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        # 同じ保存先を二つのプロセスが同時に変更するのを防ぐ。
        self.lockfile = (root / ".lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.lockfile.seek(0)
                msvcrt.locking(self.lockfile.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise RuntimeError("この保存先を使用中の Local Minutes を先に終了してください。") from e
        self.mu = threading.RLock()
        self.active = None
        self.wav = None
        self.capture = None
        self.last_chunk = 0
        self.frames = 0
        self.worker = None
        self.closed = False
        self.rows = {}
        self.settings = {"model": str(Path.home() / ".cache/whisper/small.pt"), "gguf": ""}
        if (root / "settings.json").exists():
            self.settings.update(self.read(root / "settings.json"))
        for path in root.glob("*/meeting.json"):
            m = self.read(path)
            if m["status"] in ("preparing", "recording", "processing", "importing"):
                m.update(status="error", detail="前回の処理が中断されました。保存済みの音声から再実行できます。")
                if not (path.parent / m["audio"]).exists() and not (path.parent/"mic.wav").exists():
                    m["detail"] = "音声の保存前に中断されました。新しい会議で取り込み直してください。"
                save_json(path, m)
            self.rows[m["id"]] = m

    @staticmethod
    def read(path):
        return json.loads(path.read_text(encoding="utf-8"))

    def settings_view(self):
        return {**self.settings, "ready": Path(self.settings["model"]).is_file(),
                "storage": str(self.root)}

    def configure(self, data):
        with self.mu:
            config = {"model": local_path(data.get("model"), ".pt"),
                      "gguf": local_path(data["gguf"], ".gguf") if data.get("gguf") else ""}
            save_json(self.root / "settings.json", config)
            self.settings = config

    def state(self):
        with self.mu:
            return {"meetings": [{k:v for k,v in m.items() if k not in ("assignments","names")}
                                 for m in sorted(self.rows.values(), key=lambda r:r["date"], reverse=True)],
                    "active": self.active, "settings": self.settings_view(),
                    "capture": self.capture.status() if self.capture else None}

    def new(self, title, suffix, status, count):
        if self.active:
            raise ValueError("処理中の会議が完了してから開始してください。")
        key = uuid.uuid4().hex
        path = self.root / key
        path.mkdir(mode=0o700)
        now = dt.datetime.now().astimezone()
        m = {"id":key, "title":str(title or "").strip()[:120] or now.strftime("%m月%d日の会議"),
             "date":now.isoformat(), "status":status, "detail":"", "audio":"audio"+suffix,
             "duration":0, "hasTranscript":False, "hasSummary":False, "speakerCount":count, "revision":0}
        save_json(path / "meeting.json", m)
        self.rows[key] = m
        self.active = key
        return m

    def mark(self, m, **changes):
        save_json(self.root / m["id"] / "meeting.json", {**m, **changes})
        m.update(changes)

    def start(self, title, rate, count=0, mode="microphone"):
        if type(rate) is not int or not 8000 <= rate <= 192000:
            raise ValueError("録音のサンプルレートを確認できません。")
        if mode not in ("microphone","online"):
            raise ValueError("録音モードを選択してください。")
        online = mode == "online"
        capture = None
        with self.mu:
            m = self.new(title, ".wav", "preparing", speaker_count(count))
        folder = self.root / m["id"]
        try:
            if online:
                with self.mu:
                    if self.closed:
                        raise ValueError("アプリを終了しました。")
                    self.mark(m, mode=mode)
                    capture = self.capture = SystemAudio(folder/"remote.wav")
                # OSの許可ダイアログ待ちの間も他のHTTPリクエストを処理する。
                remote_start = capture.wait_ready()
            with self.mu:
                if self.closed:
                    raise ValueError("アプリを終了しました。")
                self.wav = wave.open(str(folder / ("mic.wav" if online else m["audio"])), "wb")
                self.wav.setparams((1, 2, rate, 0, "NONE", ""))
                self.wav.writeframes(b"")
                self.frames, self.rate, self.last_chunk = 0, rate, time.monotonic()
                offset = remote_start-self.last_chunk if online else 0
                self.mark(m, status="recording", mode=mode, remoteOffset=offset)
                return m["id"]
        except Exception as exc:
            with self.mu:
                if capture:
                    capture.stop()
                if self.wav:
                    self.wav.close()
                self.wav = self.capture = self.active = None
                self.mark(m, status="error", captureFailed=True, detail=str(exc))
            raise

    def chunk(self, key, data):
        with self.mu:
            if key != self.active or self.wav is None or not data or len(data)%2:
                raise ValueError("録音データを追加できません。")
            if self.frames * 2 + len(data) > LIMIT:
                raise ValueError("録音が2 GiBに達しました。別の会議で続けてください。")
            if self.capture and not self.frames:
                # 先頭PCMの受信時刻と長さから、ブラウザでのマイク開始時刻を合わせる。
                first_sample = max(self.last_chunk,time.monotonic()-len(data)/(2*self.rate))
                self.mark(self.rows[key], remoteOffset=self.capture.start_time-first_sample)
            self.wav.writeframes(data)
            self.frames += len(data)//2
            self.last_chunk = time.monotonic()
            if self.capture and self.capture.status()["error"]:
                raise ValueError(self.capture.status()["error"])

    def stop(self, key, interrupted=False):
        with self.mu:
            if key != self.active or self.wav is None:
                raise ValueError("録音はすでに終了しています。")
            m = self.rows[key]
            warning = ""
            try:
                if self.capture:
                    self.capture.stop()
                    status = self.capture.status()
                    warning = status["error"]
                    if not warning and not status["heard"]:
                        warning = "録音中、PC音声の入力を確認できませんでした。会議アプリとイヤホンの出力先を確認してください。"
            finally:
                try:
                    self.wav.close()
                finally:
                    self.wav = self.active = self.capture = None
            self.mark(m, duration=self.frames/self.rate, status="ready" if self.frames else "error", captureWarning=warning,
                      detail="録音が中断されました。保存できた音声を確認してください。" if interrupted else "")
            if (self.frames or m.get("mode")=="online") and not interrupted:
                self.process(key)
            elif not self.frames:
                self.mark(m, detail="音声を取得できませんでした。マイクを確認してください。")

    def import_audio(self, title, suffix, stream, size, count=0):
        if suffix not in FORMATS or not 0 < size <= LIMIT:
            raise ValueError("対応する音声ファイルを選択してください（最大2 GiB）。")
        with self.mu:
            m = self.new(title, suffix, "importing", speaker_count(count))
        key = m["id"]
        path = self.root / key / m["audio"]
        try:
            tmp = path.with_suffix(".part")
            with tmp.open("xb") as f:
                left = size
                while left:
                    data = stream.read(min(left, 1024*1024))
                    if not data:
                        raise ValueError("取り込みが中断されました。再度ファイルを選んでください。")
                    f.write(data)
                    left -= len(data)
            tmp.replace(path)
            with self.mu:
                self.active = None
                self.mark(m, status="ready")
                self.process(key)
            return key
        except Exception:
            with self.mu:
                self.active = None
                self.mark(m, status="error", detail="取り込みに失敗しました。ファイルを選び直してください。")
            raise

    def process(self, key):
        with self.mu:
            if self.active:
                raise ValueError("別の処理が進行中です。")
            m = self.rows[key]
            folder = self.root / key
            online = m.get("mode")=="online"
            if online and not all((folder / f).is_file() for f in ("mic.wav","remote.wav")):
                raise ValueError("マイクまたはPC音声のファイルがありません。新しい会議で録音してください。")
            if not online and not (folder / m["audio"]).is_file():
                raise ValueError("音声がありません。新しい会議で取り込み直してください。")
            if not online and not any((folder/f).is_file() for f in ("transcript.json","asr.json")) and not Path(self.settings["model"]).is_file():
                self.mark(m, status="ready", detail="音声を保存しました。設定でWhisperモデルを指定し、議事録を作成してください。")
                return
            self.active = key
            self.mark(m, status="processing", detail="処理を準備しています")
            threading.Thread(target=self.pipeline, args=(m, self.settings.copy()), daemon=True).start()

    def command(self, m, module, args, label):
        env = {**os.environ, "PYTHONIOENCODING":"utf-8", "PYTHONUNBUFFERED":"1",
               "HF_HUB_OFFLINE":"1", "TRANSFORMERS_OFFLINE":"1", "PYTHONDONTWRITEBYTECODE":"1"}
        with self.mu:
            if self.closed:
                raise ValueError("処理を中断しました。")
            self.mark(m, detail=label)
            proc = subprocess.Popen([sys.executable,"-m","localminutes."+module,*map(str,args)],
                                    cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    encoding="utf-8", errors="replace")
            self.worker = proc
        last = ""
        for line in proc.stdout:
            if line.strip():
                last = line.strip()[-500:]
                with self.mu:
                    m["detail"] = last
        proc.stdout.close()
        if proc.wait():
            raise ValueError(last or label+"に失敗しました。")

    def pipeline(self, m, settings):
        key = m["id"]
        folder = self.root / key
        transcript, summary = folder / "transcript.json", folder / "minutes.md"
        try:
            if m.get("mode")=="online":
                tracks = [folder/"mic.wav",folder/"remote.wav",m.get("remoteOffset",0)]
                playback = folder/m["audio"]
                if not playback.is_file():
                    self.command(m,"online_meeting",["mix",*tracks,playback],"自分と相手の録音を同期中")
                with wave.open(str(playback),"rb") as f:
                    duration = f.getnframes()/f.getframerate()
                with self.mu:
                    self.mark(m,duration=duration)
                if not transcript.exists():
                    if not Path(settings["model"]).is_file():
                        with self.mu:
                            self.mark(m,status="ready",detail="音声を保存しました。設定でWhisperモデルを指定してください。")
                        return
                    self.command(m,"online_meeting",
                                 ["transcribe",*tracks,settings["model"],m.get("speakerCount",0),transcript],
                                 "マイクとPC音声を個別に文字起こし中")
                if "names" not in m:
                    names = {"S1":"自分"}
                    for s in self.read(transcript):
                        identifier = s.get("speaker","")
                        if re.fullmatch(r"S(?:[2-9]|1[0-2])",identifier):
                            names[identifier] = f"相手{int(identifier[1:])-1}"
                    with self.mu:
                        self.mark(m,names=names)
            elif not transcript.exists():
                raw = folder / "asr.json"
                if not raw.exists():
                    self.command(m,"transcription",[folder/m["audio"],settings["model"],raw],"文字起こし中")
                try:
                    self.command(m,"diarization",[folder/m["audio"],raw,transcript,m.get("speakerCount",0)],"話者を推定中")
                except ValueError as exc:
                    if self.closed:
                        raise
                    # 音声認識済みの発言は、話者推定の失敗後も編集できる。
                    save_json(transcript,self.read(raw))
                    with self.mu:
                        self.mark(m,speakerWarning=f"話者を自動推定できませんでした。「話者を編集」で設定できます。{exc}")
            segments = self.read(transcript)
            with self.mu:
                self.mark(m, hasTranscript=True, duration=m["duration"] or max((s["end"] for s in segments), default=0))
            args = [transcript,summary] + ([settings["gguf"]] if settings["gguf"] else [])
            self.command(m,"summary",args,"要約中")
            with self.mu:
                self.mark(m, status="done", detail="", hasSummary=True)
        except Exception as e:
            with self.mu:
                self.mark(m, status="error", detail=str(e))
        finally:
            with self.mu:
                self.active, self.worker = None, None

    def result(self, key):
        with self.mu:
            m = self.rows[key]
            path = self.root / key
            segments = self.read(path/"transcript.json") if m["hasTranscript"] else []
            for i, s in enumerate(segments):
                s.pop("words",None)
                if "assignments" in m:
                    s["speaker"] = m["assignments"][i]
            names = m.get("names", {})
            summary = (path/"minutes.md").read_text(encoding="utf-8") if m["hasSummary"] else ""
            return {"segments":segments, "names":names, "revision":m.get("revision",0),
                    "summary":named_summary(summary,segments,names)}

    def edit_speakers(self, key, speakers, names, revision):
        with self.mu:
            m = self.rows[key]
            if not m["hasTranscript"] or self.active == key:
                raise ValueError("議事録の作成が完了してから編集してください。")
            if m.get("revision",0) != revision:
                raise ValueError("他の画面で更新されました。一度閉じて最新の内容から編集してください。")
            ids = {f"S{i}" for i in range(1,13)}
            if not isinstance(names,dict) or not names.keys() <= ids:
                raise ValueError("話者名の形式が不正です。")
            if not all(isinstance(n,str) and len(n)<=60 and all(c.isprintable() or c=="　" for c in n)
                       for n in names.values()):
                raise ValueError("話者名は改行を含まない60文字以内で指定してください。")
            segments = self.read(self.root/key/"transcript.json")
            if not isinstance(speakers,list) or len(speakers)!=len(segments) or any(s not in ids and s!="" for s in speakers):
                raise ValueError("発言の数または話者の指定が一致しません。")
            self.mark(m, names={k:v.strip() for k,v in names.items() if v.strip()},
                      assignments=speakers,revision=revision+1)

    def audio_file(self, key):
        with self.mu:
            m = self.rows[key]
            if m["status"] in ("recording", "importing"):
                raise ValueError("音声を保存中です。")
            path = self.root / key / m["audio"]
            if not path.is_file():
                raise ValueError("保存済みの音声が見つかりません。")
            return path

    def close(self):
        with self.mu:
            self.closed = True
            if self.wav:
                self.stop(self.active, True)
            elif self.capture:
                self.capture.stop()
            if self.worker and self.worker.poll() is None:
                self.worker.terminate()

    def tick(self):
        with self.mu:
            if self.wav and time.monotonic()-self.last_chunk > (15 if self.capture else 90):
                self.stop(self.active, True)
