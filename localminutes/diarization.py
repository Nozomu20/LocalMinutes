"""端末内の発話区間検出・話者埋め込み・クラスタリングで、発言を話者に対応付ける。"""

import argparse
from bisect import bisect_left, bisect_right
from collections import defaultdict
from itertools import accumulate, groupby
import json
import math
from pathlib import Path
import sys
import tempfile

from .transcription import RATE, audio_blocks, deny_network, save_result


def make_engine(count, model_dir):
    segmentation = model_dir / "segmentation.onnx"
    embedding = model_dir / "speaker-embedding.onnx"
    if not segmentation.is_file() or not embedding.is_file():
        raise ValueError("話者判定用の学習済みモデルが不足しています。初回セットアップを実行してください。")
    try:
        import sherpa_onnx as sherpa
    except (ImportError, OSError) as exc:
        raise ValueError("このPython環境に sherpa-onnx が必要です。初回セットアップを実行してください。") from exc
    config = sherpa.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(segmentation)),
            num_threads=2, provider="cpu"),
        embedding=sherpa.SpeakerEmbeddingExtractorConfig(model=str(embedding), num_threads=2, provider="cpu"),
        clustering=sherpa.FastClusteringConfig(num_clusters=count or -1, threshold=0.5),
        min_duration_on=0.3, min_duration_off=0.35,
    )
    if not config.validate():
        raise ValueError("話者判定用モデルの構成を確認できません。")
    engine = sherpa.OfflineSpeakerDiarization(config)
    if engine.sample_rate != RATE:
        raise ValueError("16 kHz入力に対応する話者判定用モデルを指定してください。")
    return engine


def detect_turns(audio, count=0, model_dir=None):
    """話者ごとの発話区間を返す。同時に話した部分は複数の区間が重なり得る。"""
    if not 0 <= count <= 12:
        raise ValueError("話者数は自動（0）または1〜12人で指定してください。")
    import numpy as np

    engine = make_engine(count, Path(model_dir) if model_dir is not None else Path.home()/".cache"/"localminutes")
    # 会議全体でクラスタリングし、離れた時点に現れる同じ声にも同じIDを割り当てる。
    # デコード済み全ブロックをPython側のメモリに同時に保持しない。
    with tempfile.TemporaryFile(dir=audio.parent) as pcm:
        n = 0
        audible = False
        for block in audio_blocks(audio, np):
            if not len(block):
                continue
            pcm.write(np.ascontiguousarray(block, dtype=np.float32).tobytes())
            n += len(block)
            audible = audible or np.max(np.abs(block)) > 0.0001
        if not n or not audible:
            return []
        pcm.flush()
        samples = np.memmap(pcm, dtype=np.float32, mode="r", shape=(n,))
        last_bucket = -1

        def progress(done, total):
            nonlocal last_bucket
            bucket = int(10 * done / max(1, total))
            if bucket != last_bucket:
                print(f"発話区間・声の特徴を解析中… {bucket * 10}%", flush=True)
                last_bucket = bucket
            return 0

        try:
            result = engine.process(samples, callback=progress)
        finally:
            del samples
    turns = []
    names = {}
    for segment in result.sort_by_start_time():
        if segment.speaker < 0 or not (math.isfinite(segment.start) and math.isfinite(segment.end)):
            continue
        start = max(0.0, float(segment.start))
        end = min(n / RATE, float(segment.end))
        if end <= start:
            continue
        label = names.setdefault(segment.speaker, f"S{len(names) + 1}" if len(names) < 12 else "")
        turns.append({"start": start, "end": end, "speaker": label})
    return turns


class TurnIndex:
    """同時発話も含め、ASRの時刻と重なるダイアライゼーション区間を探す。"""

    def __init__(self, turns):
        self.turns = sorted(turns, key=lambda turn: turn["start"])
        self.starts = [t["start"] for t in self.turns]
        self.ends = list(accumulate((t["end"] for t in self.turns), max))

    def speaker(self, start, end):
        # 長さゼロの句読点にも、この時刻を含む発話があれば割り当てる。
        if start == end:
            start, end = max(0.0, start - 0.02), end + 0.02
        first = bisect_right(self.ends, start)
        last = bisect_left(self.starts, end)
        coverage = defaultdict(float)
        for turn in self.turns[first:last]:
            overlap = min(end, turn["end"]) - max(start, turn["start"])
            if overlap > 0:
                coverage[turn["speaker"]] += overlap
        if not coverage:
            return ""
        ranked = sorted(coverage, key=coverage.get, reverse=True)
        if len(ranked) > 1 and coverage[ranked[1]] >= 0.9 * coverage[ranked[0]]:
            # 一つのASR単語が二人にほぼ同じだけ重なる場合、発言者を決め打ちしない。
            return ""
        return ranked[0]


def label_segment(row, index):
    text = row["text"]
    words = row.get("words") or []
    default = {**row, "speaker": index.speaker(row["start"], row["end"])}
    if not words or any(not isinstance(w.get("word"), str) for w in words):
        return [default]
    tokens = "".join(w["word"] for w in words)
    if tokens.strip() != text.strip():
        return [default]
    labels = []
    for w in words:
        start, end = w.get("start"), w.get("end")
        if not isinstance(start, (float, int)) or not isinstance(end, (float, int)):
            return [default]
        if not (math.isfinite(start) and math.isfinite(end) and end >= start):
            return [default]
        labels.append(index.speaker(start, end))

    # 句読点単独の認識トークンは隣の発言に含め、同時発話中の不明な語は未設定で残す。
    for i, w in enumerate(words):
        if not any(ch.isalnum() for ch in w["word"]):
            labels[i] = labels[i - 1] if i else labels[min(1, len(labels) - 1)]
    groups = [(speaker, list(items)) for speaker, items in groupby(zip(labels, words), key=lambda pair: pair[0])]
    if len(groups) == 1:
        return [{**row, "speaker": groups[0][0]}]
    sections = []
    for speaker, pairs in groups:
        group = [word for _, word in pairs]
        sections.append({**row, "start": group[0]["start"], "end": group[-1]["end"], "speaker": speaker,
                         "text": "".join(w["word"] for w in group), "words": group})
    sections[0]["start"] = row["start"]
    sections[-1]["end"] = row["end"]
    # Whisperは発言全体をstripするが、単語の先頭スペースは保持することがある。
    sections[0]["text"] = text[:len(text) - len(text.lstrip())] + sections[0]["text"].lstrip()
    sections[-1]["text"] = sections[-1]["text"].rstrip() + text[len(text.rstrip()):]
    if "".join(s["text"] for s in sections) != text:
        return [default]
    return sections


def diarize(audio, rows, count, model_dir=None):
    if not rows:
        return []
    turns = detect_turns(audio, count, model_dir)
    if not turns:
        raise ValueError("この音声から話者を特定できる発話区間を検出できませんでした。")
    index = TurnIndex(turns)
    return [part for row in rows for part in label_segment(row, index)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("transcript", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("count", type=int)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.addaudithook(deny_network)
    try:
        if args.output.resolve() in (args.audio.resolve(), args.transcript.resolve()):
            raise ValueError("結果は別のファイルに保存してください。")
        rows = json.loads(args.transcript.read_text(encoding="utf-8"))
        print("学習済みモデルで話者の発話区間を検出しています…", flush=True)
        result = diarize(args.audio, rows, args.count)
        save_result(args.output, result)
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
