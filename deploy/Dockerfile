# WarmPlayer · Synology / any Docker host
# 构建上下文 = 仓库根目录：docker build -f deploy/Dockerfile -t warmplayer .
FROM python:3.11-slim

# ffmpeg：转码 / 杜比下混 / 服务端频谱 / 封面取色（Linux 无 sips，取色走 ffmpeg）
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# 可选：PyPI 镜像（国内网络受限时构建加 --build-arg PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple）
ARG PIP_INDEX=https://pypi.org/simple
RUN pip install --no-cache-dir --index-url ${PIP_INDEX} numpy

WORKDIR /opt/warmplayer
COPY vinyl_server.py 唱片机.html 磁带机.html ./
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8900
VOLUME /data
ENTRYPOINT ["/entrypoint.sh"]