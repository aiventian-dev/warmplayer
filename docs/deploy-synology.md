# Deploy on Synology (Docker) / 群晖容器部署

Run WarmPlayer where the music lives. The container pins python + ffmpeg + numpy together,
survives DSM upgrades, and restarts on boot — the same stability contract as the macOS launchd setup.
把服务放到音乐所在的机器上。容器把 python + ffmpeg + numpy 锁在一起，DSM 升级碰不到它，开机自启——与 macOS launchd 方案同一套稳定契约。

macOS-only pieces (caffeinate guards, sips) auto-skip on Linux; cover color falls back to ffmpeg.
macOS 专属部件（caffeinate 守卫、sips）在 Linux 自动跳过；封面取色回退 ffmpeg。代码零改动。

## 0. Prerequisites / 前提

- Container Manager (DSM 7.2+) or any Docker host. / 已装 Container Manager 或任意 Docker 主机。
- Music share on a NAS volume. Confirm the real path: `ls -d /volume*/Music`
  音乐共享在 NAS 卷上，先确认真实路径（下文以 `/volume1/Music` 为例）。
- SSH optional but easiest. / SSH 可选但最省事。

## 1. Get the code / 取代码

```bash
mkdir -p /volume1/docker/warmplayer/data
cd /volume1/docker/warmplayer
git clone https://github.com/aiventian-dev/warmplayer.git repo   # or upload the release zip
cd repo
```

Docker Hub 直连受限时，先经加速站拉基础镜像并打回原名（构建即走本地）：
If Docker Hub is unreachable, pre-pull the base image via a mirror and re-tag:

```bash
docker pull docker.1panel.live/library/python:3.11-slim
docker tag docker.1panel.live/library/python:3.11-slim python:3.11-slim
```

## 2. Build & run / 构建并运行

```bash
MUSIC_ROOT=/volume1/Music docker compose -f deploy/compose.yaml up -d --build
```

PyPI 受限时加镜像：`docker compose build --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple`
（或 `docker build -f deploy/Dockerfile --build-arg PIP_INDEX=… -t warmplayer .` 后 `docker run`。）

不用 compose 的等价命令 / equivalent without compose:

```bash
docker build -f deploy/Dockerfile -t warmplayer .
docker run -d --name warmplayer --restart unless-stopped \
  -p 8900:8900 \
  -v /volume1/docker/warmplayer/data:/data \
  -v /volume1/Music:/music:ro \
  -e TZ=Asia/Shanghai warmplayer
```

数据与代码分离：`/data` 持久卷存 cache / folders.json / settings.json；每次启动从镜像刷新代码。
升级 = `git pull && docker compose up -d --build`，缓存与配置不丢。
Data/code split: `/data` holds cache and config; code is refreshed from the image at each start,
so upgrading never loses your library cache or settings.

## 3. First run / 首次运行

Open `http://<NAS_IP>:8900/` (turntable) or `/tape` (deck).
Settings → add folders using **container paths**: `/music/Dolby`, `/music/AppleMusic - ALAC` …
设置里添加目录时用**容器内路径** `/music/…`，不是 NAS 宿主路径。

Verify / 验证：

```bash
curl -s http://127.0.0.1:8900/api/health
# {"ok":true,"py":"3.11.x","ffmpeg":"/usr/bin/ffmpeg","numpy":"2.x",...}
```

## 4. HTTPS & public access (recommended) / 公网与 HTTPS（推荐）

Plain HTTP has no TLS and no accounts — keep it LAN/VPN, or front it with DSM's reverse proxy:
明文 HTTP 无 TLS 无账号体系——留在局域网/VPN，或用 DSM 反代套 HTTPS：

1. Control Panel → Login Portal → Advanced → Reverse Proxy:
   source `https://langba.synology.me:443` → destination `http://127.0.0.1:8900`.
   控制面板 → 登录门户 → 高级 → 反向代理：源 https 443 → 目标 http://127.0.0.1:8900。
2. Attach a Let's Encrypt certificate (Control Panel → Security → Certificate).
   挂 Let's Encrypt 证书。
3. Router: forward 443 → NAS. The old 8900 → Mac mapping can then be removed.
   路由器 443 映射到 NAS；旧的 8900→Mac 映射可撤。

`/api/meter` is chunked ndjson over plain HTTP — the reverse proxy passes it through unchanged.
频谱流是普通 HTTP 分块推流，反代原样透传，无需 WebSocket 配置。

## 5. Retire the Mac instance / 退役 Mac 实例

Only after the NAS serves all your devices: 确认 NAS 已接管所有设备后再执行：

```bash
launchctl bootout gui/$(id -u)/com.lldaddy.vinylserver
```

The Mac becomes a pure client. Do not run both as libraries — pick one home for the cache.
Mac 退化为纯客户端。不要两边同时当曲库主机，缓存只认一个家。

## Notes / 说明

- Transcode (ALAC→mp3 for non-Safari) and spectrum run on NAS CPU; transcodes are cached per track.
  转码与频谱吃 NAS CPU；转码按曲缓存，每首只算一次。弱 ARM 机型多流并发时留意负载。
- Streaming keeps disks awake; HDD hibernation may not trigger while playing.
  播放期间磁盘保持唤醒，硬盘休眠可能不触发。
- One instance per host (flock lock inside the container). / 每主机单实例（容器内 flock 锁）。
- Container Manager UI works too: create the project from `deploy/compose.yaml`, same volumes/ports.
  也可以在 Container Manager 界面用该 compose 建项目，卷与端口相同。