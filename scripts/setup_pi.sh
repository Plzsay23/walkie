#!/usr/bin/env bash
# 파이 한 대를 무전기로 세팅한다. 일반 사용자로 실행한다(필요한 곳에서만 sudo 를 부른다).
#
#   bash scripts/setup_pi.sh <이 무전기 이름> <상대 주소>
#   예) 파이 A:  bash scripts/setup_pi.sh A walkie-b
#       파이 B:  bash scripts/setup_pi.sh B walkie-a
#
# 상대 주소는 상대 파이의 Tailscale 이름(MagicDNS)이나 100.x.x.x IP.
# 마이크/스피커 카드를 자동으로 못 찾으면 MIC_CARD=... SPK_CARD=... 를 앞에 붙여 지정한다
# (이름은 `cat /proc/asound/cards` 의 대괄호 안 값).
set -euo pipefail

NAME=${1:?"이 무전기 이름이 필요합니다 (예: A)"}
PEER=${2:?"상대 주소가 필요합니다 (예: walkie-b)"}
PORT=${PORT:-7700}
HTTP=${HTTP:-8080}
REPO=$(cd "$(dirname "$0")/.." && pwd)
USER_NAME=$(id -un)

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "1. 패키지"
sudo apt-get update -qq
sudo apt-get install -y -qq git curl alsa-utils libportaudio2

step "2. uv, 파이썬 의존성"
if ! command -v uv >/dev/null && [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
UV="$HOME/.local/bin/uv"
command -v uv >/dev/null && UV=$(command -v uv)
(cd "$REPO" && "$UV" sync --frozen)

step "3. Tailscale"
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale status >/dev/null 2>&1; then
  echo "Tailscale 에 아직 로그인하지 않았습니다. 아래 명령을 실행하고 뜨는 주소로 로그인하십시오:"
  echo "    sudo tailscale up --hostname=walkie-$(echo "$NAME" | tr 'A-Z' 'a-z')"
fi

step "4. 오디오 장치 (ALSA 기본 장치로 고정)"
cat /proc/asound/cards
card_id() {  # /proc/asound/cards 에서 설명이 $1 에 맞는 카드의 id
  sed -n 's/^ *[0-9]* \[\([^ ]*\) *\]: \(.*\)$/\1\t\2/p' /proc/asound/cards | grep -i -- "$1" | head -n1 | cut -f1 || true
}
MIC_CARD=${MIC_CARD:-$(card_id respeaker)}
if [ -z "${SPK_CARD:-}" ]; then
  # ReSpeaker 가 아닌 USB 오디오 중 재생 장치가 있는 첫 카드
  for c in /proc/asound/card[0-9]*; do
    id=$(cat "$c/id")
    [ "$id" = "$MIC_CARD" ] && continue
    [ -e "$c/usbid" ] && [ -e "$c/pcm0p" ] && { SPK_CARD=$id; break; }
  done
fi
if [ -z "$MIC_CARD" ] || [ -z "${SPK_CARD:-}" ]; then
  echo "마이크(${MIC_CARD:-없음}) 또는 스피커(${SPK_CARD:-없음})를 못 찾았습니다."
  echo "USB 를 꽂고 다시 실행하거나 MIC_CARD=... SPK_CARD=... 로 지정하십시오."
  exit 1
fi
echo "마이크 카드: $MIC_CARD / 스피커 카드: $SPK_CARD"
# 카드 번호 대신 id 로 묶어서 USB 꽂는 순서가 바뀌어도 그대로 간다.
# plug 가 16kHz mono 와 장치 고유 형식 사이를 변환해 준다.
sudo tee /etc/asound.conf >/dev/null <<EOF
# walkie setup_pi.sh 가 만듦
pcm.!default {
    type asym
    playback.pcm { type plug slave.pcm "hw:CARD=$SPK_CARD,DEV=0" }
    capture.pcm  { type plug slave.pcm "hw:CARD=$MIC_CARD,DEV=0" }
}
ctl.!default { type hw card $SPK_CARD }
EOF
if pgrep -x pipewire >/dev/null || pgrep -x pulseaudio >/dev/null; then
  echo "주의: PipeWire/PulseAudio 가 돌고 있습니다. 데스크톱 OS 면 장치를 잡고 있을 수 있습니다."
  echo "      Raspberry Pi OS Lite 를 권합니다."
fi
sudo usermod -aG audio "$USER_NAME"
amixer -q -c "$SPK_CARD" sset PCM 80% unmute 2>/dev/null || amixer -q -c "$SPK_CARD" sset Speaker 80% unmute 2>/dev/null || true

step "5. 부팅 시 자동 실행 (systemd)"
sudo tee /etc/default/walkie >/dev/null <<EOF
WALKIE_ARGS="--name $NAME --peer $PEER:$PORT --port $PORT --http $HTTP"
EOF
sudo tee /etc/systemd/system/walkie.service >/dev/null <<EOF
[Unit]
Description=walkie 무전기 노드
After=network-online.target tailscaled.service sound.target
Wants=network-online.target

[Service]
User=$USER_NAME
SupplementaryGroups=audio
WorkingDirectory=$REPO
EnvironmentFile=/etc/default/walkie
Environment=PYTHONUNBUFFERED=1
ExecStart=$UV run --frozen python -m walkie \$WALKIE_ARGS
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now walkie
sudo systemctl restart walkie

step "끝"
IP=$(hostname -I | awk '{print $1}')
echo "로그:      journalctl -u walkie -f"
echo "탭 접속:   http://$IP:$HTTP   (또는 http://$(hostname).local:$HTTP)"
echo "설정 변경: sudo nano /etc/default/walkie && sudo systemctl restart walkie"
