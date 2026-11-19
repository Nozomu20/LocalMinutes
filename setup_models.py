"""明示的に起動したときだけ実行する初回セットアップ。会議データは読み込まない。"""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request
import venv


ROOT = Path(__file__).resolve().parent
CACHE = Path.home() / ".cache" / "whisper"
# openai-whisper 20250625 が small モデルに使用する公式URLとSHA-256。
DIGEST = "9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794"
URL = f"https://openaipublic.azureedge.net/main/whisper/models/{DIGEST}/small.pt"
MODEL_BYTES = 483_617_219


def digest(path):
    checksum = hashlib.sha256()
    with path.open("rb") as model:
        while block := model.read(1024 * 1024):
            checksum.update(block)
    return checksum.hexdigest()


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


def install_runtime():
    folder = ROOT / ".venv"
    python = folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        print("専用のPython環境を作成しています。", flush=True)
        venv.EnvBuilder(with_pip=True).create(folder)
    requirement = (ROOT / "requirements.txt").read_text(encoding="utf-8").strip()
    package, version = requirement.split("==")
    check = ("import sys; from importlib.metadata import version; "
             "assert (3,10) <= sys.version_info[:2] <= (3,12); "
             "assert version(sys.argv[1]) == sys.argv[2]; from whisper import load_model")
    probe = [str(python), "-c", check, package, version]
    if subprocess.run(probe, stderr=subprocess.DEVNULL).returncode == 0:
        print("文字起こし用ライブラリを確認しました。", flush=True)
        return
    print("専用のPython環境に文字起こし用ライブラリをインストールします。", flush=True)
    subprocess.run([str(python), "-m", "pip", "--disable-pip-version-check", "install",
                    "-r", str(ROOT / "requirements.txt")], check=True)
    subprocess.run(probe, check=True)


def download_partial(part):
    offset = part.stat().st_size if part.exists() else 0
    request = urllib.request.Request(URL, headers={"Range": f"bytes={offset}-"} if offset else {})
    with urllib.request.urlopen(request, timeout=90) as response:
        if response.status == 206:
            header = response.headers.get("Content-Range", "")
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", header)
            if not match or int(match[1]) != offset or int(match[3]) != MODEL_BYTES:
                raise RuntimeError("モデルのダウンロード再開位置を確認できませんでした。")
        elif response.status == 200:
            offset = 0
        else:
            raise RuntimeError("モデルを取得できませんでした。")
        last = -1
        with part.open("ab" if offset else "wb") as output:
            while block := response.read(1024 * 1024):
                offset += len(block)
                if offset > MODEL_BYTES:
                    raise RuntimeError("配布モデルのサイズが想定と異なります。")
                output.write(block)
                percent = int(offset * 100 / MODEL_BYTES)
                if percent // 10 != last:
                    print(f"モデル取得 {percent}%", flush=True)
                    last = percent // 10


def install_windows_capture():
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    version = "0.2.12.9"
    probe = [str(python), "-c",
             "import sys, pyaudiowpatch; from importlib.metadata import version; "
             "assert version('PyAudioWPatch') == sys.argv[1]", version]
    if subprocess.run(probe, stderr=subprocess.DEVNULL).returncode == 0:
        print("Windowsの再生音録音ライブラリを確認しました。", flush=True)
        return
    print("専用のPython環境にWindowsの再生音録音ライブラリをインストールします。", flush=True)
    subprocess.run([str(python), "-m", "pip", "--disable-pip-version-check", "install",
                    "--only-binary=:all:", f"PyAudioWPatch=={version}"], check=True)
    subprocess.run(probe, check=True)


def ensure_model(folder):
    target = folder / "small.pt"
    if target.exists():
        if digest(target) != DIGEST:
            raise RuntimeError(f"既存モデルのSHA-256が公式値と一致しません。別の場所に移してから再実行してください。\n{target}")
        print("既存のsmall.ptのSHA-256を確認しました。", flush=True)
        return
    part = folder / ".localminutes-small.pt.part"
    if not part.exists() or part.stat().st_size < MODEL_BYTES:
        print("公式配布元から日本語対応モデルを取得しています。", flush=True)
        download_partial(part)
    if part.stat().st_size < MODEL_BYTES:
        raise RuntimeError("モデルの取得が中断されました。再実行すると続きから取得します。")
    if digest(part) != DIGEST:
        part.unlink()
        raise RuntimeError("取得したモデルのSHA-256が一致しません。セットアップを再実行してください。")
    # 完成したファイルだけを配置する。同時に別のツールが用意したモデルを上書きしない。
    try:
        os.link(part, target)
    except FileExistsError:
        if digest(target) != DIGEST:
            raise RuntimeError(f"別のモデルが配置されています。確認してください。\n{target}")
    part.unlink()
    print("モデルのSHA-256を確認し、保存しました。", flush=True)


def main():
    if sys.argv[1:]:
        print("このフォルダで Python 3.10〜3.12 を使い、引数なしで setup_models.py を実行してください。")
        return 0 if sys.argv[1:] in (["--help"], ["-h"]) else 2
    if not (3, 10) <= sys.version_info[:2] <= (3, 12):
        print("Python 3.10〜3.12 で実行してください。", file=sys.stderr)
        return 2
    print("初回準備を開始します。必要なパッケージとモデルの取得にインターネットを使用します。", flush=True)
    try:
        with setup_lock(CACHE):
            install_runtime()
            if os.name == "nt":
                install_windows_capture()
            ensure_model(CACHE)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"準備を完了できませんでした。\n{exc}", file=sys.stderr)
        return 1
    print("準備が完了しました。Start-Mac.command または Start-Windows.cmd から起動してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
