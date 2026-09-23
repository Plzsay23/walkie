# 무전기 노드 컨테이너 (라즈베리파이 64-bit / arm64, PC 리눅스에서도 동작)
#
# 파이 카메라는 rpicam-vid 가 필요해서 라즈베리파이 저장소를 붙여 설치한다.
# 컨테이너 안의 libcamera 가 커널과 맞아야 하므로 베이스는 파이 OS 와 같은 Debian trixie 로 맞춘다.
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:/root/.local/bin:$PATH

# 1) 시스템 패키지
#    libportaudio2  : 마이크·스피커(sounddevice)
#    rpicam-apps-lite: 파이 카메라(rpicam-vid). USB 웹캠만 쓸 거면 없어도 된다
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg alsa-utils libportaudio2 \
    && curl -fsSL https://archive.raspberrypi.com/debian/raspberrypi.gpg.key \
        | gpg --dearmor -o /usr/share/keyrings/raspberrypi.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/raspberrypi.gpg] http://archive.raspberrypi.com/debian trixie main" \
        > /etc/apt/sources.list.d/raspberrypi.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends rpicam-apps-lite \
    && rm -rf /var/lib/apt/lists/*

# 2) 파이썬 의존성 (소스보다 먼저 넣어 캐시가 살아 있게 한다)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# 3) 코드
COPY walkie ./walkie

# 인자는 compose 의 WALKIE_ARGS 로 넘긴다
ENTRYPOINT ["/bin/sh", "-c", "exec python -m walkie $WALKIE_ARGS"]
