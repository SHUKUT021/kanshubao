#!/bin/zsh
# 看书宝启动脚本：双击运行或 ./run.sh
cd "$(dirname "$0")" || exit 1
PORT=$(python3 -c "import json,os;print(json.load(open('config.json')).get('port',8770) if os.path.exists('config.json') else 8770)")
echo "看书宝 → http://127.0.0.1:$PORT"
open "http://127.0.0.1:$PORT" 2>/dev/null &
exec python3 app/server.py
