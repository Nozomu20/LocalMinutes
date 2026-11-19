"""既定のWASAPI再生機器に送られるPCMを、会議の時間軸を保って保存する。"""

import json
import os
from pathlib import Path
from queue import Empty, Full, Queue
import sys
from threading import Event, Thread
import time
import wave

RATE = 16000


def notify(event, **fields):
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


class Timeline:
    def __init__(self, output, sample_rate, channels):
        self.output = output
        self.sample_rate = sample_rate
        self.channels = channels
        self.frames = 0
        self.rate_state = None
        output.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        output.writeframes(b"")

    def silence_until(self, frame):
        while self.frames < frame:
            count = min(RATE, frame - self.frames)
            self.output.writeframes(bytes(2 * count))
            self.frames += count

    def packet(self, pcm, seconds):
        import audioop  # 対応Python 3.10〜3.12の標準ライブラリ。

        if self.channels == 2:
            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
        elif self.channels > 2:
            import numpy as np
            samples = np.frombuffer(pcm, dtype="<i2").reshape(-1, self.channels)
            pcm = samples.mean(axis=1).round().astype("<i2").tobytes()

        # WASAPIは再生が途切れた間、パケットを生成しないことがある。
        # 通常のコールバック時刻の揺れではPCMを分断しない。
        target = max(0, round(seconds * RATE))
        jump = target - self.frames
        if jump > RATE * 0.06:
            self.silence_until(target)
            self.rate_state = None
        data, self.rate_state = audioop.ratecv(
            pcm, 2, 1, self.sample_rate, RATE, self.rate_state, 1,
            1 if self.sample_rate > RATE else 0,
        )
        # 機器クロックが進んだ場合は1パケットで1サンプルだけ補正する。
        # コールバックがまとめて到着したときも、発言を含むパケット全体を失わない。
        if jump < -RATE * 0.06 and len(data) >= 4:
            data = data[2:]
        self.output.writeframes(data)
        self.frames += len(data) // 2
        return audioop.max(data, 2) / 32768 if data else 0.0


def wait_for_stop(stop):
    # OSのパイプを直接待ち、EOFや親プロセスの終了でも停止する。
    try:
        while os.read(0, 1) not in (b"", b"\n"):
            pass
    finally:
        stop.set()


def capture(output, audio, stop):
    packets = Queue(maxsize=250)
    overrun = Event()
    with audio.PyAudio() as device_manager:
        try:
            endpoint = device_manager.get_default_wasapi_loopback()
            if not endpoint.get("isLoopbackDevice"):
                raise ValueError("ループバック入力を確認できません。")
            rate = int(endpoint["defaultSampleRate"])
            channels = int(endpoint["maxInputChannels"])
            if rate <= 0 or channels <= 0:
                raise ValueError("出力形式を取得できません。")
        except (LookupError, ValueError, OSError) as exc:
            raise RuntimeError("Windowsの既定の出力が見つかりません。イヤホンを接続し、サウンド設定で既定の出力にしてください。") from exc

        def incoming(data, frames, timing, flags):
            # PortAudioのスレッドは待機やディスク書き込みを行わない。
            if flags & audio.paInputOverflow:
                overrun.set()
                return None, audio.paAbort
            arrived = time.monotonic()
            latency = timing.get("current_time", 0) - timing.get("input_buffer_adc_time", 0)
            packet_time = arrived - (latency if 0 < latency < 1 else frames / rate)
            try:
                packets.put_nowait((packet_time, data))
            except Full:
                overrun.set()
                return None, audio.paAbort
            return None, audio.paContinue

        with device_manager.open(
            input=True, format=audio.paInt16, input_device_index=endpoint["index"],
            channels=channels, rate=rate, frames_per_buffer=max(1, rate // 50),
            stream_callback=incoming, start=False,
        ) as stream, output.open("xb") as raw, wave.open(raw, "wb") as wav:
            timeline = Timeline(wav, rate, channels)
            if stop.is_set():
                return
            start = time.monotonic()
            stream.start_stream()
            notify("ready", device=endpoint["name"], startMonotonic=start)
            reported = start
            peak = 0.0
            while not stop.is_set():
                try:
                    packet_time, data = packets.get(timeout=0.05)
                except Empty:
                    # 遅延して到着した音声を待つ猶予を残す。完全無音でもファイルを伸ばす。
                    timeline.silence_until(round((time.monotonic() - start - 0.5) * RATE))
                else:
                    peak = max(peak, timeline.packet(data, packet_time - start))
                if overrun.is_set():
                    raise RuntimeError("PC再生音の取り込みが追いつかず、音声の一部が欠落しました。")
                if not stream.is_active():
                    raise RuntimeError("録音中に再生機器が切断されました。Windowsと会議アプリで出力先を確認してください。")
                now = time.monotonic()
                if now - reported >= 0.2:
                    notify("level", peak=round(peak, 4))
                    reported = now
                    peak = 0.0
            elapsed = time.monotonic() - start
            stream.stop_stream()
            while not packets.empty():
                packet_time, data = packets.get_nowait()
                timeline.packet(data, packet_time - start)
            timeline.silence_until(round(elapsed * RATE))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) != 2:
        notify("error", message="保存先のWAVファイルを指定してください。")
        return 2
    if sys.platform != "win32":
        notify("error", message="WASAPIの再生音録音にはWindowsが必要です。")
        return 1
    try:
        import pyaudiowpatch as audio
    except (OSError, ImportError):
        notify("error", message="Windows用の音声入力を準備してください。アプリのフォルダで py -3.12 setup_models.py を実行し、Start-Windows.cmd で再起動してください。")
        return 1
    stop = Event()
    Thread(target=wait_for_stop, args=(stop,), daemon=True).start()
    try:
        capture(Path(sys.argv[1]), audio, stop)
    except Exception as exc:
        notify("error", message=f"PC再生音を録音できません。{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
