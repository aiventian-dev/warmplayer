#!/bin/sh
# 代码与数据分离：镜像里带代码，/data 是持久卷（cache + folders.json + settings.json）。
# 每次启动把代码刷新进 /data —— 升级镜像即升级代码，缓存与配置不丢。
set -e
mkdir -p /data
cp -f /opt/warmplayer/vinyl_server.py /opt/warmplayer/唱片机.html /opt/warmplayer/磁带机.html /data/
cd /data
exec python -u vinyl_server.py --no-open --host 0.0.0.0