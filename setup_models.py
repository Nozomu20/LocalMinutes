"""明示的な初回準備。会議の保存先は読み込まない。Python 3.10〜3.12 対応。"""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import http.client
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import venv


ROOT = Path(__file__).resolve().parent
CACHE = Path.home() / ".cache" / "whisper"
SPEAKER_CACHE = Path.home() / ".cache" / "localminutes"
SHERPA_VERSION = "1.12.28"
RELEASE = "https://github.com/k2-fsa/sherpa-onnx/releases/download"


@dataclass(frozen=True)
class Asset:
    filename: str
    size: int
    sha256: str
    url: str = ""


def whisper_file(name, size, sha256):
    return Asset(name, size, sha256, f"https://openaipublic.azureedge.net/main/whisper/models/{sha256}/{name}")


# openai-whisper 20250625 の公式配布URL内の SHA-256 と配布ファイルのサイズ。
ASR = {
    "small": whisper_file("small.pt", 483_617_219, "9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794"),
    "turbo": whisper_file("large-v3-turbo.pt", 1_617_941_637, "aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a"),
}
SEGMENTATION_DIR = "sherpa-onnx-pyannote-segmentation-3-0"
ARCHIVE = Asset(
    SEGMENTATION_DIR + ".tar.bz2", 6_958_444, "24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488",
    f"{RELEASE}/speaker-segmentation-models/{SEGMENTATION_DIR}.tar.bz2",
)
# 指定した通常ファイルのみを取り出す。アーカイブ同梱のPythonやシェルは実行しない。
MEMBERS = {
    "model.onnx": Asset("segmentation.onnx", 5_992_913, "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079"),
    "LICENSE": Asset("segmentation-LICENSE.txt", 1_061, "14d7016ad68e7394d6e6b78d96cc2ae431c905287b89674cfdf021e79e62b8ba"),
}
EMBEDDING = Asset(
    # GitHubの配布タグでは同名ONNXが再生成される。公式checksum.txtと一致する
    # sherpaメンテナーの固定コミットを使い、匿名の初回取得を再現可能にする。
    "speaker-embedding.onnx", 39_593_761, "1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b",
    "https://huggingface.co/csukuangfj/speaker-embedding-models/resolve/"
    "8be2a75c9ed7a590538b268e46fbb65e1aa9d208/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
)
EMBEDDING_LICENSE = Asset(
    "speaker-embedding-LICENSE.txt", 11_357, "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4",
    "https://raw.githubusercontent.com/modelscope/3D-Speaker/065629c313eaf1a01c65c640c46d77e61e9607b4/LICENSE",
)


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            checksum.update(block)
    return checksum.hexdigest()


def installed(path, asset):
    if not path.exists():
        return False
    if path.stat().st_size != asset.size or digest(path) != asset.sha256:
        raise RuntimeError(f"既存ファイルが配布元のサイズまたはSHA-256と一致しません。別の場所に移してから再実行してください。\n{path}")
    return True


@contextmanager
def setup_lock(folder):
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (folder / ".localminutes-setup.lock").open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("別のセットアップが実行中です。完了後に再実行してください。") from exc
        yield


def install_package(python, spec, check, binary=False):
    package, version = spec.split("==")
    probe = [str(python), "-c",
             "import sys; from importlib.metadata import version; "
             "assert (3,10) <= sys.version_info[:2] <= (3,12); "
             "assert version(sys.argv[1]) == sys.argv[2]; " + check, package, version]
    if subprocess.run(probe, stderr=subprocess.DEVNULL).returncode:
        print(f"専用のPython環境に {spec} をインストールします。", flush=True)
        args = ["--only-binary=:all:"] if binary else []
        subprocess.run([str(python), "-m", "pip", "--disable-pip-version-check", "install", *args, spec], check=True)
        subprocess.run(probe, check=True)


def install_runtime():
    folder = ROOT / ".venv"
    python = folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        print("専用のPython環境を作成しています。", flush=True)
        venv.EnvBuilder(with_pip=True).create(folder)
    whisper = (ROOT / "requirements.txt").read_text(encoding="utf-8").strip()
    install_package(python, whisper, "from whisper import load_model")
    if os.name == "nt":
        install_package(python, "PyAudioWPatch==0.2.12.9", "import pyaudiowpatch", binary=True)
    install_package(python, f"sherpa-onnx=={SHERPA_VERSION}", "from sherpa_onnx import OfflineSpeakerDiarization", binary=True)


def download_partial(part, asset):
    offset = part.stat().st_size if part.exists() else 0
    request = urllib.request.Request(asset.url, headers={"Range": f"bytes={offset}-"} if offset else {})
    with urllib.request.urlopen(request, timeout=90) as response:
        if response.status == 206:
            header = response.headers.get("Content-Range", "")
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", header)
            if not match or int(match[1]) != offset or int(match[3]) != asset.size:
                raise RuntimeError(f"{asset.filename} の再開位置を確認できません。")
        elif response.status == 200:
            offset = 0
        else:
            raise RuntimeError(f"{asset.filename} を取得できません。")
        last = -1
        with part.open("ab" if offset else "wb") as output:
            while block := response.read(1024 * 1024):
                offset += len(block)
                if offset > asset.size:
                    raise RuntimeError(f"{asset.filename} のサイズが想定と異なります。")
                output.write(block)
                percent = int(offset * 100 / asset.size)
                if percent // 10 != last:
                    print(f"{asset.filename} {percent}%", flush=True)
                    last = percent // 10


def publish(part, target, asset):
    if part.stat().st_size != asset.size or digest(part) != asset.sha256:
        part.unlink()
        raise RuntimeError(f"取得した {asset.filename} のSHA-256またはサイズが一致しません。再実行してください。")
    # 同じファイルを並行して配置した他のセットアップの出力を上書きしない。
    try:
        os.link(part, target)
    except FileExistsError:
        installed(target, asset)
    part.unlink()


def download(folder, asset):
    target = folder / asset.filename
    if installed(target, asset):
        print(f"{asset.filename} を確認しました。", flush=True)
        return target
    part = folder / (".localminutes-" + asset.filename + ".part")
    if not part.exists() or part.stat().st_size < asset.size:
        download_partial(part, asset)
    if part.stat().st_size < asset.size:
        raise RuntimeError(f"{asset.filename} の取得が中断されました。再実行で再開できます。")
    publish(part, target, asset)
    return target


def ensure_speakers(folder):
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    missing = [(member, asset) for member, asset in MEMBERS.items() if not installed(folder / asset.filename, asset)]
    if missing:
        # 小さな圧縮ファイルのみ一時領域に置き、検証済みの対象ファイルを公開する。
        with tempfile.TemporaryDirectory(dir=folder) as tmp:
            staged = Path(tmp)
            archive = download(staged, ARCHIVE)
            with tarfile.open(archive, "r:bz2") as tar:
                for name, asset in missing:
                    entry = tar.getmember(f"{SEGMENTATION_DIR}/{name}")
                    if not entry.isfile() or entry.size != asset.size:
                        raise RuntimeError(f"アーカイブ内の {name} の形式またはサイズを確認できません。")
                    part = staged / asset.filename
                    with tar.extractfile(entry) as src, part.open("xb") as dst:
                        dst.write(src.read(asset.size))
                    publish(part, folder / asset.filename, asset)
    download(folder, EMBEDDING_LICENSE)
    download(folder, EMBEDDING)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--asr", choices=ASR, default="small", help="準備するWhisperモデル（既定 small）")
    group.add_argument("--speakers-only", action="store_true", help="Whisperモデルを取得せず話者判定を準備")
    args = parser.parse_args()
    if not (3, 10) <= sys.version_info[:2] <= (3, 12):
        print("Python 3.10〜3.12 で実行してください。", file=sys.stderr)
        return 2
    print("初回準備を開始します。必要なパッケージとモデルの取得にインターネットを使用します。", flush=True)
    try:
        with setup_lock(CACHE):
            install_runtime()
            ensure_speakers(SPEAKER_CACHE)
            if not args.speakers_only:
                model = download(CACHE, ASR[args.asr])
                print(f"文字起こしモデルの保存先　{model}", flush=True)
    except (OSError, RuntimeError, ValueError, KeyError, http.client.HTTPException, tarfile.TarError, subprocess.CalledProcessError) as exc:
        print(f"準備を完了できませんでした。\n{exc}", file=sys.stderr)
        return 1
    print("準備が完了しました。Start-Mac.command または Start-Windows.cmd から起動してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
