"""オンライン会議の独立した2本の録音を、同じ時計で再生・文字起こしする。"""

import argparse
import math
import os
from pathlib import Path
import sys
import tempfile
import wave
from itertools import zip_longest

from .diarization import diarize
from .transcription import RATE, deny_network, pcm_blocks, save_result, transcribe


def track_starts(offset):
    if not math.isfinite(offset):
        raise ValueError("録音の開始時刻差を確認できません。")
    gap = round(offset * RATE)
    # 最初に始まった録音の先頭を、再生・文字起こしとも0秒とする。
    return max(0, -gap), max(0, gap)


def pcm_seconds(path, delay, np):
    """元の録音を16 kHzに変換し、開始のずれを含めて1秒ずつ返す。"""
    with wave.open(str(path), "rb") as source:
        if not source.getnframes():
            return
        silence = np.zeros(RATE, dtype=np.float32)
        seconds, remainder = divmod(delay, RATE)
        for _ in range(seconds):
            yield silence
        pending = silence[:remainder]
        for block in pcm_blocks(source, np):
            if pending.size:
                block = np.concatenate((pending, block))
            if len(block) >= RATE:
                yield block[:RATE]
                block = block[RATE:]
            pending = block
        if pending.size:
            yield pending


def mix(mic, remote, offset, output):
    import numpy as np

    mic_start, remote_start = track_starts(offset)
    mic_blocks = pcm_seconds(mic, mic_start, np)
    remote_blocks = pcm_seconds(remote, remote_start, np)
    empty = np.empty(0, dtype=np.float32)
    scratch = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as file:
            scratch = Path(file.name)
            with wave.open(file, "wb") as audio:
                audio.setparams((1, 2, RATE, 0, "NONE", ""))
                for own, others in zip_longest(mic_blocks, remote_blocks, fillvalue=empty):
                    data = np.zeros(max(len(own), len(others)), dtype=np.float32)
                    data[:len(own)] += own
                    data[:len(others)] += others
                    # 同時に大きな声が入っても、どちらの声もクリップさせない。
                    audio.writeframesraw(np.rint(data * 16384).clip(-32768, 32767).astype("<i2").tobytes())
                if not audio.getnframes():
                    raise ValueError("録音に再生できる音声データがありません。")
        os.replace(scratch, output)
    finally:
        mic_blocks.close()
        remote_blocks.close()
        if scratch is not None:
            scratch.unlink(missing_ok=True)


def recognize(path, model):
    with wave.open(str(path), "rb") as stream:
        if not stream.getnframes():
            return []
    return transcribe(path, model)


def timed_segment(row, source, speaker, start):
    offset = start / RATE

    def on_timeline(interval):
        return {**interval, "start": round(interval["start"] + offset, 3),
                "end": round(interval["end"] + offset, 3)}

    timed = on_timeline(row)
    if "words" in row:
        timed["words"] = [on_timeline(word) for word in row["words"]]
    return {**timed, "speaker": speaker, "source": source}


def transcribe_meeting(mic, remote, offset, model, count, output):
    if not 0 <= count <= 12:
        raise ValueError("話者数は自動（0）または1〜12人で指定してください。")
    mic_start, remote_start = track_starts(offset)

    print("自分のマイクを文字起こししています…", flush=True)
    rows = [timed_segment(row, "microphone", "S1", mic_start) for row in recognize(mic, model)]
    print("会議相手の音声を文字起こししています…", flush=True)
    heard = recognize(remote, model)
    warning_path = output.parent / "speaker-warning.json"
    warning = ""
    if heard:
        # ネイティブ推論が異常終了しても、認識済みの両トラックを利用できるよう先に確定する。
        fallback = rows + [timed_segment(row, "remote", "", remote_start) for row in heard]
        save_result(warning_path, {"message": "相手の話者判定が中断されました。発言は文字起こしに保存されています。"})
        save_result(output, sorted(fallback, key=lambda row: row["start"]))
        print("相手側の話者を推定しています…", flush=True)
        remote_count = max(1, count - 1) if count else 0
        try:
            labeled = diarize(remote, heard, remote_count)
        except Exception as exc:
            warning = f"相手の話者判定に失敗しました。発言は「話者を編集」で割り当てられます。{exc}"
            labeled = heard
        for row in labeled:
            cluster = row.get("speaker", "")
            # 自分のIDを予約する。特徴不足や自動推定が上限を超えた発言は手動で割り当てられる。
            speaker = ""
            if cluster.startswith("S") and cluster[1:].isdigit():
                index = int(cluster[1:])
                if 1 <= index < 12:
                    speaker = f"S{index + 1}"
            rows.append(timed_segment(row, "remote", speaker, remote_start))
    # 同時に話した発言も各トラックのまま残す。
    save_result(output, sorted(rows, key=lambda row: row["start"]))
    save_result(warning_path, {"message": warning})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("mix", "transcribe"):
        command = actions.add_parser(action)
        command.add_argument("mic", type=Path)
        command.add_argument("remote", type=Path)
        command.add_argument("offset", type=float)
        if action == "transcribe":
            command.add_argument("model", type=Path)
            command.add_argument("count", type=int)
        command.add_argument("output", type=Path)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.addaudithook(deny_network)
    os.environ["TORCH_FORCE_WEIGHTS_ONLY_LOAD"] = "1"
    try:
        inputs = [args.mic, args.remote]
        if args.action == "transcribe":
            inputs.append(args.model)
        if args.output.resolve() in [p.resolve() for p in inputs]:
            raise ValueError("結果の保存先が入力ファイルと同じです。")
        if args.action == "mix":
            mix(args.mic, args.remote, args.offset, args.output)
        else:
            if args.output.resolve() == (args.output.parent / "speaker-warning.json").resolve():
                raise ValueError("文字起こしの保存先が話者判定の状態ファイルと同じです。")
            transcribe_meeting(args.mic, args.remote, args.offset, args.model, args.count, args.output)
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
