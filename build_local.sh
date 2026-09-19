#!/bin/bash
# 一键本地构建 + 起一个测试实例（和正式部署错开，互不影响）
#
#   正式部署   tg-forwarder        :9020   数据 ./data
#   本地构建   tg-forwarder-local  :9025   数据 ./data-local
#
# 用法：bash build_local.sh
# 国内网络慢时现在同目录建 .env 写：PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
set -e
cd "$(dirname "$0")"

docker compose -f docker-compose.build.yml up -d --build
sleep 6

echo
docker ps --filter name=tg-forwarder-local --format '  {{.Names}} | {{.Status}} | {{.Ports}}'
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "  面板: http://${IP:-<主机IP>}:9025"
echo "  数据: ./data-local（测试用，与正式部署的 ./data 完全隔离）"
echo "  看日志: docker logs -f tg-forwarder-local"
echo "  停掉:   docker compose -f docker-compose.build.yml down"
