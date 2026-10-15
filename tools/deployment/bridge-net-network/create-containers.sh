#!/bin/sh
# Re-create a missing container; existing containers are NEVER deleted here.
# Usage: sh create-containers.sh Openlist-aio|syncthing|upsnap
set -eu
PATH=/data/docker:/usr/sbin:/usr/bin:/sbin:/bin
case "${1:-}" in
  Openlist-aio)
    docker network inspect bridge-net >/dev/null
    # This existing volume holds the RPC secret and session; do not recreate it empty.
    docker volume inspect openlist_aria2 >/dev/null
    docker run -d --name Openlist-aio --hostname minasc73208 \
      --user "$(id -u u103868178):$(id -g u103868178)" \
      --restart no --network bridge-net -p 5244:5244 -p 10.31.0.66:6800:6800 \
      --log-opt max-size=50m -e UMASK=022 -e RUN_ARIA2=true \
      -v openlist:/opt/openlist/data \
      -v openlist_aria2:/opt/aria2/.aria2 \
      --mount 'type=bind,source=/nas/pool0/u103868178/data,target=/nas/pool0/u103868178/data' \
      --mount 'type=bind,source=/nas/pool0/u103868178/data/Download,target=/opt/aria2/Download' \
      --mount 'type=bind,source=/nas/pool0/u103868178/data/Download,target=/opt/openlist/data/temp' \
      openlistteam/openlist:latest-aio
    ;;
  syncthing)
    docker network inspect bridge-net >/dev/null
    docker run -d --name syncthing --hostname minasc73208 \
      --user "$(id -u u103868178):$(id -g u103868178)" \
      --restart no --network bridge-net --log-opt max-size=50m \
      -p 8384:8384 -p 22000:22000/tcp -p 22000:22000/udp -p 21027:21027/udp \
      -e STGUIADDRESS=0.0.0.0:8384 -e PUID=1000 -e PGID=1000 \
      -v syncthing:/var/syncthing \
      --mount 'type=bind,source=/nas/pool0/u103868178/data/Jaco/公司文档/3.新华三,target=/data/公司文档/3.新华三' \
      syncthing/syncthing:latest
    ;;
  upsnap)
    docker run -d --name upsnap --hostname minasc73208 \
      --network host --restart always --log-opt max-size=50m \
      -e UPSNAP_HTTP_LISTEN=0.0.0.0:8090 \
      -v upsnap:/app/pb_data seriousm4x/upsnap
    ;;
  *) printf '%s\n' 'Usage: sh create-containers.sh Openlist-aio|syncthing|upsnap' >&2; exit 2 ;;
esac
