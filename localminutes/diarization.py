"""端末内で音色・基本周波数を比較し、会議内の発言に匿名話者ラベルを付ける。"""

import argparse
import json
import sys
from pathlib import Path

from .transcription import RATE, audio_blocks, deny_network, save_result


STEP = RATE * 3 // 4
FRAME = 640
FFT = 1024
MELS = 30


def filter_banks(np):
    edges = 700 * np.expm1(np.linspace(np.log1p(60 / 700), np.log1p(7500 / 700), MELS + 2))
    hz = np.fft.rfftfreq(FFT, 1 / RATE)
    left = (hz - edges[:-2, None]) / (edges[1:-1] - edges[:-2])[:, None]
    right = (edges[2:, None] - hz) / (edges[2:] - edges[1:-1])[:, None]
    filters = np.maximum(0, np.minimum(left, right))
    dct = np.cos(np.pi / MELS * np.arange(1, 13)[:, None] * (np.arange(MELS) + 0.5))
    return filters.T, dct.T / MELS**0.5


def voice_feature(samples, filters, dct, np):
    if len(samples) < FRAME:
        return None
    frames = np.lib.stride_tricks.sliding_window_view(samples, FRAME)[::320]
    frames = frames - frames.mean(axis=1, keepdims=True)
    level = np.mean(frames * frames, axis=1)
    threshold = max(1e-8, np.quantile(level, 0.9) * 0.035)
    frames = frames[level >= threshold]
    if len(frames) < 3:
        return None
    spectrum = np.abs(np.fft.rfft(frames * np.hamming(FRAME), n=FFT)) ** 2
    bands = np.maximum(spectrum @ filters, 1e-10)
    cepstra = np.log(bands) @ dct

    # 自己相関を重なり区間のエネルギーで規格化し、声帯振動の第1周期を探す。
    lags = np.arange(RATE // 440, RATE // 65 + 1)
    power = np.abs(np.fft.rfft(frames, n=2048)) ** 2
    covariance = np.fft.irfft(power, n=2048)[:, lags]
    energy = np.pad(np.cumsum(frames * frames, axis=1), ((0, 0), (1, 0)))
    norm = energy[:, FRAME - lags] + energy[:, -1:] - energy[:, lags]
    corr = 2 * covariance / np.maximum(norm, 1e-12)
    peak = (corr[:, 1:-1] >= corr[:, :-2]) & (corr[:, 1:-1] >= corr[:, 2:])
    peaks = np.where(peak, corr[:, 1:-1], -1)
    matches = peaks >= np.maximum(0.70, 0.90 * peaks.max(axis=1, keepdims=True))
    voiced = matches.any(axis=1)
    # 無声音を含む窓では、母音など周期性のあるフレームを優先して音色を集計する。
    timbre = np.mean(cepstra[voiced] if voiced.sum() >= 5 else cepstra, axis=0)
    pitch = np.nan
    if voiced.sum() >= 3:
        periods = lags[1:-1][np.argmax(matches[voiced], axis=1)]
        pitch = np.median(np.log2(RATE / periods))
    return np.append(timbre, pitch)


def observations(audio, segments, np):
    filters, dct = filter_banks(np)
    times, features = [], []
    spans = sorted((float(s["start"]), float(s["end"])) for s in segments)
    segment = 0
    pending = np.empty(0, dtype=np.float32)
    position = 0

    def observe(window):
        nonlocal segment
        center = (position + len(window) / 2) / RATE
        while segment < len(spans) and spans[segment][1] < center - 0.35:
            segment += 1
        if segment < len(spans) and spans[segment][0] <= center + 0.35:
            feature = voice_feature(window, filters, dct, np)
            if feature is not None:
                times.append(center)
                features.append(feature)

    for block in audio_blocks(audio, np):
        pending = np.concatenate((pending, block))
        while len(pending) >= 2 * STEP:
            observe(pending[:2 * STEP])
            pending = pending[STEP:]
            position += STEP
    if len(pending):
        observe(pending)
    return np.asarray(times), np.asarray(features)


def scaled(features, np):
    values = features.copy()
    pitch = values[:, -1]
    valid = np.isfinite(pitch)
    pitch[~valid] = np.median(pitch[valid]) if np.any(valid) else 0.0
    center = np.median(values, axis=0)
    spread = np.quantile(values, 0.85, axis=0) - np.quantile(values, 0.15, axis=0)
    # 発話の抑揚や母音の違いを話者の違いと取り違えないよう、弱い変動は増幅しない。
    spread[:-1] = np.maximum(spread[:-1], 0.9)
    spread[-1] = max(spread[-1], 0.28)
    values = np.clip((values - center) / spread, -3, 3)
    values[:, -1] *= 3.2
    return values


def distances(values, centers, np):
    return np.maximum((values * values).sum(1)[:, None] + (centers * centers).sum(1) - 2 * values @ centers.T, 0)


def fit_clusters(samples, count, np):
    best_score, best_centers = float("inf"), None
    for seed in (0, len(samples) // 3, 2 * len(samples) // 3):
        centers = [samples[seed]]
        nearest = distances(samples, np.asarray(centers), np).ravel()
        for _ in range(1, count):
            center = samples[int(nearest.argmax())]
            centers.append(center)
            nearest = np.minimum(nearest, ((samples - center) ** 2).sum(1))
        centers = np.asarray(centers)
        for _ in range(35):
            labels = distances(samples, centers, np).argmin(1)
            updated = centers.copy()
            for index in range(count):
                members = samples[labels == index]
                if len(members):
                    updated[index] = members.mean(axis=0)
            if np.allclose(centers, updated):
                break
            centers = updated
        score = distances(samples, centers, np).min(1).sum()
        if score < best_score:
            best_score, best_centers = score, centers
    return best_centers


def choose_clusters(values, count, np):
    # 長時間の会議でも比較用行列は一定サイズに収める。
    samples = values[np.linspace(0, len(values) - 1, min(650, len(values)), dtype=int)]
    if count:
        return fit_clusters(samples, min(count, len(samples)), np)
    best = samples.mean(0, keepdims=True)
    best_score = 0.50 if len(samples) < 16 else 0.42
    pair = np.sqrt(distances(samples, samples, np))
    for n in range(2, min(12, len(samples) // 3) + 1):
        centers = fit_clusters(samples, n, np)
        labels = distances(samples, centers, np).argmin(1)
        counts = np.bincount(labels, minlength=n)
        if counts.min() < 3:
            continue
        means = np.stack([pair[:, labels == j].mean(1) for j in range(n)], axis=1)
        rows = np.arange(len(samples))
        within = means[rows, labels] * counts[labels] / (counts[labels] - 1)
        means[rows, labels] = np.inf
        between = means.min(1)
        score = np.mean((between - within) / np.maximum(np.maximum(between, within), 1e-10))
        score -= 0.025 * (n - 2)
        if score > best_score:
            best_score, best = score, centers
    return best


def smooth_labels(times, values, centers, np):
    costs = distances(values, centers, np)
    back = np.zeros(costs.shape, dtype=np.int16)
    total = costs[0]
    for i in range(1, len(times)):
        transitions = np.broadcast_to(total[:, None], (len(total), len(total))).copy()
        if times[i] - times[i - 1] < 2:
            transitions += 2.0
            np.fill_diagonal(transitions, total)
        back[i] = transitions.argmin(0)
        total = transitions.min(0) + costs[i]
    labels = np.zeros(len(times), dtype=int)
    labels[-1] = total.argmin()
    for i in range(len(times) - 1, 0, -1):
        labels[i - 1] = back[i, labels[i]]
    return labels


def interval_label(start, end, times, labels, np):
    left, right = times.searchsorted([start, end], side="left")
    if left < right:
        return int(np.bincount(labels[left:right]).argmax())
    pos = min(int(left), len(times) - 1)
    mid = (start + end) / 2
    if pos > 0 and abs(times[pos - 1] - mid) <= abs(times[pos] - mid):
        pos -= 1
    return int(labels[pos])


def label_segment(row, times, labels, np):
    def label(start, end):
        return interval_label(start, end, times, labels, np)

    default = [{**row, "speaker": label(row["start"], row["end"])}]
    words = row.get("words") or []
    if len(words) < 2:
        return default
    tokens = [w.get("word", "") for w in words]
    if "".join(tokens).strip() != row["text"].strip() or any(
        "start" not in w or "end" not in w for w in words
    ):
        return default
    speakers = [label(w["start"], w["end"]) for w in words]
    cuts = [0] + [i for i in range(1, len(words)) if speakers[i] != speakers[i - 1]] + [len(words)]
    if len(cuts) == 2:
        return default
    bounds = [row["start"]]
    for i in cuts[1:-1]:
        midpoint = 0.5 * (words[i - 1]["end"] + words[i]["start"])
        bounds.append(max(bounds[-1], min(midpoint, row["end"])))
    bounds.append(row["end"])
    if min(b - a for a, b in zip(bounds, bounds[1:])) < 0.55:
        return default
    sections = []
    for pos, (a, b) in enumerate(zip(cuts, cuts[1:])):
        text = "".join(tokens[a:b])
        sections.append({**row, "text": text, "start": bounds[pos], "end": bounds[pos + 1],
                         "words": words[a:b], "speaker": speakers[a]})
    original = row["text"]
    sections[0]["text"] = original[:len(original) - len(original.lstrip())] + sections[0]["text"].lstrip()
    sections[-1]["text"] = sections[-1]["text"].rstrip() + original[len(original.rstrip()):]
    if "".join(s["text"] for s in sections) != original:
        return default
    return sections


def diarize(audio, rows, count):
    if not 0 <= count <= 12:
        raise ValueError("話者数は自動（0）または1〜12人で指定してください。")
    if not rows or count == 1:
        return [{**r, "speaker": "S1"} for r in rows]
    import numpy as np

    times, features = observations(audio, rows, np)
    if not len(times):
        return [{**r, "speaker": ""} for r in rows]
    values = scaled(features, np)
    centers = choose_clusters(values, count, np)
    labels = smooth_labels(times, values, centers, np)
    result = [s for r in rows for s in label_segment(r, times, labels, np)]
    names = {}
    for row in result:
        cluster = row["speaker"]
        row["speaker"] = names.setdefault(cluster, f"S{len(names) + 1}")
    return result


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
        print("声の特徴を比較しています…", flush=True)
        result = diarize(args.audio, rows, args.count)
        save_result(args.output, result)
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
