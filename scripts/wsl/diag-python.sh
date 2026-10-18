#!/bin/bash
# 定位 bin/python 间歇性 ENOENT 的原因
V=/home/qusong/autoalpha-venv
echo "== ls -l =="
ls -l "$V/bin/python" "$V/bin/python3" 2>&1
echo "== file =="
file "$V/bin/python" 2>&1 | head -n 2
echo "== stat =="
stat -c '%n type=%F mode=%a size=%s' "$V/bin/python" "$V/bin/python3" 2>&1
echo "== readelf interp =="
readelf -l "$V/bin/python" 2>/dev/null | grep -A0 interp || echo "(readelf unavailable or not ELF)"
echo "== mount of /home =="
findmnt -no TARGET,SOURCE,OPTIONS /home 2>&1 || mount | grep ' /home'
echo "== exec attempts (10x) =="
for i in $(seq 1 10); do
  out=$("$V/bin/python" -c 'print("ok")' 2>&1)
  echo "  #$i rc=$? out=$out"
done
echo "== exec via relative path =="
( cd "$V/bin" && ./python -c 'print("rel ok")' 2>&1 )
echo "== env =="
echo "PATH=$PATH"
