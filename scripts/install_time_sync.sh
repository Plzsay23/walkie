#!/usr/bin/env bash
# NTP(udp 123)를 막아 둔 망에서 파이 시계를 맞춘다.
#
# 파이에는 시계 배터리가 없어서 껐다 켜면 시각을 잃는다. 보통은 NTP 로 맞추는데
# 그게 막혀 있으면 시계가 한참 틀어지고, 그러면 TLS 인증서가 "아직 유효하지 않다"며
# 거부돼 git·apt 가 조용히 실패한다. 실제로 겪었다.
# 대신 HTTP 응답의 Date 헤더로 맞춘다. 정확도는 1초 안쪽이라 이 용도로 충분하다.
#
#   sudo bash scripts/install_time_sync.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "sudo 로 실행하십시오"; exit 1; }

cat > /usr/local/bin/http-time-sync <<'EOF'
#!/bin/bash
# HTTP Date 헤더로 시계를 맞춘다. 부팅 직후엔 와이파이가 아직 안 붙어 있을 수 있어 재시도한다.
set -u
for attempt in $(seq 1 20); do
  for url in http://connectivitycheck.gstatic.com/generate_204 \
             http://www.msftconnecttest.com/connecttest.txt \
             http://detectportal.firefox.com/success.txt; do
    d=$(curl -sI --max-time 8 "$url" | awk 'tolower($1) == "date:" { $1=""; sub(/^ /, ""); sub(/\r$/, ""); print; exit }')
    [ -n "${d:-}" ] || continue
    if date -s "$d" > /dev/null 2>&1; then
      logger -t http-time-sync "시계를 맞췄습니다: $d (시도 $attempt)"
      exit 0
    fi
  done
  sleep 10
done
logger -t http-time-sync "시각을 가져오지 못했습니다 (20번 시도)"
exit 1
EOF
chmod +x /usr/local/bin/http-time-sync

cat > /etc/systemd/system/http-time-sync.service <<'EOF'
[Unit]
Description=HTTP Date 헤더로 시계 맞추기 (NTP 차단 망 대체)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/http-time-sync
EOF

cat > /etc/systemd/system/http-time-sync.timer <<'EOF'
[Unit]
Description=부팅 직후와 한 시간마다 시계 맞추기

[Timer]
OnBootSec=20s
OnUnitActiveSec=1h
AccuracySec=10s

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now http-time-sync.timer
/usr/local/bin/http-time-sync || true

echo
echo "지금 시각: $(date)"
echo "확인:  journalctl -t http-time-sync | tail -3"
