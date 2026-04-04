#!/bin/bash
# Auto-restart clone until all 281 articles are done
cd "$(dirname "$0")"

while true; do
    DONE=$(python3 -c "
import json
with open('clone_state.json','r') as f: s = json.load(f)
print(len(s.get('completed',[])))
" 2>/dev/null || echo "0")

    echo "$(date): Completed $DONE/281"

    if [ "$DONE" -ge 281 ]; then
        echo "ALL DONE!"
        break
    fi

    python3 -u -X utf8 clone.py full 2>&1 | tee -a clone_full.log

    echo "$(date): Process exited, restarting in 10s..."
    sleep 10
done
