"""日本語音声をローカルの Whisper モデルで時刻付きテキストにする子プロセス。"""

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path


RATE = 16_000
CHUNK_SECONDS = 90


class TranscriptionError(Exception):
    pass


def deny_network(event, args):
    if event in {
        "socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg",
        "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
    }:
        raise PermissionError("文字起こし中のネットワーク接続は禁止されています。")


def pcm_to_mono(data, width, channels, np):
    if width == 1:
        samples = (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - 128) / 128
    elif width == 3:
        octets = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        packed = octets[:, 0] | (octets[:, 1] << 8) | (octets[:, 2] << 16)
        signed = (packed ^ 0x800000) - 0x800000
        samples = signed.astype(np.float32) / 8388608.0
    else:
        values = np.frombuffer(data, dtype=f"<i{width}")
        samples = values.astype(np.float32) / float(1 << (width * 8 - 1))
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples


def resample(samples, source_rate, np):
    if source_rate == RATE:
        return samples
    if source_rate > RATE:
        # 間引きで高域の雑音が音声帯域に折り返さないよう低域フィルターをかける。
        half_width = math.ceil(8 * source_rate / RATE)
        indices = np.arange(-half_width, half_width + 1)
        cutoff = 0.94 * RATE / source_rate
        kernel = np.sinc(indices * cutoff) * np.hamming(len(indices))
        kernel /= kernel.sum()
        padded = np.pad(samples, half_width, mode="edge")
        samples = np.convolve(padded, kernel, mode="valid")
    count = round(len(samples) * RATE / source_rate)
    positions = np.arange(count) * (source_rate / RATE)
    return np.interp(positions, np.arange(len(samples)), samples).astype(np.float32)


def pcm_blocks(source, np):
    with source:
        channels = source.getnchannels()
        rate = source.getframerate()
        width = source.getsampwidth()
        if not 1 <= channels <= 32 or not 1000 <= rate <= 384000 or width not in (1, 2, 3, 4):
            raise TranscriptionError("このWAVのチャンネル数・サンプルレート・ビット深度には未対応です。")
        while data := source.readframes(rate):
            yield resample(pcm_to_mono(data, width, channels, np), rate, np)


def find_ffmpeg():
    filename = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    for directory in (Path(sys.executable).parent, Path(sys.prefix) / "Scripts", Path(sys.prefix) / "bin"):
        candidate = directory / filename
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which(filename)


def ffmpeg_blocks(audio, np):
    executable = find_ffmpeg()
    if not executable:
        raise TranscriptionError("この音声形式にはFFmpegが必要です。PCM WAV形式でも読み込めます。")
    command = [
        executable, "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
        "-format_whitelist", "wav,mp3,mov,flac,ogg,matroska,webm,aac,aiff",
        "-i", str(audio.resolve()), "-map", "0:a:0", "-ac", "1", "-ar", str(RATE),
        "-f", "s16le", "pipe:1",
    ]
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        while data := process.stdout.read(RATE * 2):
            yield pcm_to_mono(data, 2, 1, np)
        if process.wait() != 0:
            raise TranscriptionError("FFmpegで音声をデコードできませんでした。音声形式やファイルを確認してください。")
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
        process.wait()


def audio_blocks(audio, np):
    try:
        source = wave.open(str(audio), "rb")
    except (wave.Error, EOFError):
        yield from ffmpeg_blocks(audio, np)
    else:
        yield from pcm_blocks(source, np)


def chunks(blocks, np):
    # 長い会議も一定量ずつ処理する。区切りは末尾の静かな箇所を優先する。
    parts = []
    size = 0
    for block in blocks:
        parts.append(block)
        size += len(block)
        if size >= CHUNK_SECONDS * RATE:
            merged = np.concatenate(parts)
            window = RATE // 10
            tail = merged[-2 * RATE:].reshape(-1, window)
            quiet = int(np.argmin(np.mean(tail * tail, axis=1)))
            boundary = len(merged) - len(tail) * window + quiet * window + window // 2
            yield merged[:boundary]
            parts = [merged[boundary:]]
            size = len(parts[0])
    if size:
        yield np.concatenate(parts)


def transcribe(audio, model_path):
    try:
        import numpy as np
        import torch
        from whisper import load_model
    except (ImportError, OSError) as exc:
        raise TranscriptionError("openai-whisper とその依存関係をこのPython環境にインストールしてください。") from exc

    if not audio.is_file():
        raise TranscriptionError("音声ファイルが見つかりません。")
    if not model_path.is_file():
        raise TranscriptionError("ローカルのWhisperモデル（.pt）が見つかりません。")

    print("音声認識モデルを読み込み中…", flush=True)
    # 絶対パスでのみロードする。モデル名指定時のWhisperの自動取得を使わない。
    torch.set_num_threads(min(6, os.cpu_count() or 1))
    device = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        model = load_model(str(model_path.resolve(strict=True)), device=device)
    except (OSError, RuntimeError, ValueError, KeyError, EOFError) as exc:
        raise TranscriptionError("モデルの読み込みに失敗しました。Whisper形式の有効な.ptファイルを確認してください。") from exc
    if not model.is_multilingual:
        raise TranscriptionError("日本語に対応した多言語Whisperモデルを選択してください。")
    print("音声を解析中…", flush=True)
    results = []
    offset = 0
    for samples in chunks(audio_blocks(audio, np), np):
        begin = offset / RATE
        offset += len(samples)
        duration = len(samples) / RATE
        if np.max(np.abs(samples)) > 0.0001:
            result = model.transcribe(
                samples, language="ja", task="transcribe", fp16=device == "cuda",
                verbose=None, temperature=0.0, condition_on_previous_text=False, word_timestamps=True,
            )
            for segment in result["segments"]:
                text = segment["text"].strip()
                start = min(duration, max(0.0, segment["start"]))
                end = min(duration, max(start, segment["end"]))
                if text and end > start:
                    words = [{"word": w["word"],
                              "start": round(begin + min(duration, max(0, w["start"])), 3),
                              "end": round(begin + min(duration, max(0, w["end"])), 3)}
                             for w in segment.get("words", [])]
                    results.append({"start": round(begin + start, 3), "end": round(begin + end, 3),
                                    "text": text, "words": words})
        elapsed = int(offset / RATE)
        print(f"{elapsed // 60}分{elapsed % 60:02}秒まで解析済み", flush=True)
    if not offset:
        raise TranscriptionError("音声データが空です。")
    return results


def save_result(path, segments):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            json.dump(segments, file, ensure_ascii=False, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("model", type=Path)
    parser.add_argument("result", type=Path)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.addaudithook(deny_network)
    os.environ["TORCH_FORCE_WEIGHTS_ONLY_LOAD"] = "1"
    try:
        if args.result.resolve() in (args.audio.resolve(), args.model.resolve()):
            raise TranscriptionError("結果の保存先が入力ファイルと同じです。")
        segments = transcribe(args.audio, args.model)
        save_result(args.result, segments)
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
