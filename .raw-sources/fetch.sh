#!/bin/bash
UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
get() { # url out
  for i in 1 2 3 4 5; do
    curl -sL --max-time 90 --retry 3 --retry-delay 2 -A "$UA" "$1" -o "$2" && [ -s "$2" ] && { echo "OK  $2  $(wc -c <"$2") bytes"; return 0; }
    echo "  retry $i for $2"; sleep 3
  done
  echo "FAIL $2"; return 1
}
mkdir -p jingui lib
get "https://raw.githubusercontent.com/hh-in-zhuzhou/ShangHanLun/master/app/src/main/assets/shangHan_data.json" shangHan_data.json
get "https://raw.githubusercontent.com/hh-in-zhuzhou/ShangHanLun/master/app/src/main/assets/jinKui_data.json" jinKui_data.json
for i in 000 001 002 003; do
  get "https://raw.githubusercontent.com/kanripo/KR3e0007/master/KR3e0007_$i.txt" "jingui/KR3e0007_$i.txt"
done
for c in 001 002 003 004 005 006 007 008 009 010 011 012 013 014 015 016 017 018 019 020 021 022 023 024 025; do
  get "https://raw.githubusercontent.com/the-quizzman/jinguiyaolue/main/chapters/$c.md" "jingui/qj_$c.md"
done
get "https://raw.githubusercontent.com/the-quizzman/jinguiyaolue/main/book.yaml" jingui/book.yaml
echo "=== DONE ==="
ls -la
