#!/bin/bash
# VM 资源监控：./vm_monitor.sh <输出CSV> <命令...>
# 每 2 秒采样：epoch秒, 总内存MB, 已用MB, 可用MB, swap已用MB, java_rss_mb, python_rss_mb, CPU空闲%
set -u
OUT="$1"; shift
echo "ts,mem_total_mb,mem_used_mb,mem_avail_mb,swap_used_mb,java_rss_mb,python_rss_mb,cpu_idle_pct" > "$OUT"
"$@" &
CMD_PID=$!
while kill -0 "$CMD_PID" 2>/dev/null; do
  TS=$(date +%s)
  read -r T U A S < <(free -m | awk '/^Mem:/{t=$2;u=$3;a=$7} /^Swap:/{s=$3} END{print t,u,a,s}')
  read -r JRSS < <(ps -u "$(whoami)" -o rss=,comm= | awk '/java|SparkSubmit/{j+=$1} END{print j+0}')
  read -r PRSS < <(ps -u "$(whoami)" -o rss=,comm= | awk '/python/{p+=$1} END{print p+0}')
  IDLE=$(top -bn1 | awk '/Cpu(s)/{print $8}' | head -1)
  echo "$TS,$T,$U,$A,$S,$((JRSS/1024)),$((PRSS/1024)),${IDLE:-0}" >> "$OUT"
  sleep 2
done
wait "$CMD_PID"
RC=$?
echo "# exit=$RC ts=$(date +%s)" >> "$OUT"
exit $RC
