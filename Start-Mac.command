#!/bin/zsh
# Finderから起動した場合も、シェル設定を読み込まずに既存環境を探す。
setopt null_glob
cd "${0:A:h}" || exit 1
export PYTHONDONTWRITEBYTECODE=1
candidates=(
  "$PWD/.venv/bin/python"
  "${VIRTUAL_ENV:-/nonexistent}/bin/python"
  "${PYENV_ROOT:-$HOME/.pyenv}"/versions/3.1[012]*/bin/python3
  /Library/Frameworks/Python.framework/Versions/3.1[012]/bin/python3
  /opt/homebrew/bin/python3.1[012] /usr/local/bin/python3.1[012]
  "${commands[python3.12]}" "${commands[python3.11]}" "${commands[python3.10]}" "${commands[python3]}"
)
for candidate in "${candidates[@]}"; do
  [[ -x "$candidate" && ! -d "$candidate" && "$candidate" != /usr/bin/python3 ]] || continue
  if "$candidate" -c 'import sys; assert (3,10) <= sys.version_info[:2] <= (3,12); from whisper import load_model' 2>/dev/null; then
    exec "$candidate" app.py "$@"
  fi
done
print -u2 'Python 3.10〜3.12 と openai-whisper が必要です。初回のみ、このフォルダで python3.12 setup_models.py を実行してください。'
exit 1
