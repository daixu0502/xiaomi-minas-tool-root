# NAS container network migration

Openlist-aio and Syncthing now use `bridge-net`. UpSnap remains on `host` so
LAN broadcast wake and discovery retain their original network behavior.

## Re-create commands

`create-containers.sh` contains the individual `docker run` commands. Run it
with one container name only when that container does not already exist. It
does not remove existing containers or data. It requires the current named
volumes, storage-pool bind paths, existing images, and `bridge-net` network.

The migration itself used the exact installed image IDs, not an image upgrade.
The commands use the original local image tags; pulling a newer tag separately
may upgrade the application. Original restart policies are preserved: Openlist
and Syncthing are `no`, UpSnap is `always`. Syncthing's original PUID/PGID image
environment is retained; its effective identity is controlled by `--user`.

Published ports: Openlist 5244/TCP and LAN-only `10.31.0.66:6800/TCP`;
Syncthing 8384/TCP, 22000/TCP+UDP,
21027/UDP. UpSnap remains at host port 8090. Publishing 21027/UDP does not make
LAN broadcast discovery equivalent to host mode. Configure peers with the
NAS LAN address and port 22000 if automatic discovery no longer finds them.
The current bridge is IPv4; this change does not preserve direct host IPv6
connectivity inside these containers.

Openlist's embedded aria2 configuration, DHT data and session now persist in
the `openlist_aria2` volume mounted at `/opt/aria2/.aria2`. Its configuration
is owned by the container's UID 19718 and the secret-bearing `aria2.conf` is
mode 0600. The requested RPC secret is stored only in that configuration,
not in these scripts. Anonymous RPC requests are rejected. Re-creation commands
require this existing volume so they do not silently create an unprotected or
unwritable default configuration. Openlist's existing client RPC secret already
matches; it was not changed. Never delete this volume during container cleanup.

## Openlist qBittorrent compatibility

Download center itself listens on both `127.0.0.1:19700` and
`10.31.0.66:19700`. Openlist uses the latter, preserving its credentials,
temporary directory and other settings. Login and task/path restrictions remain.
The plugin's `var/openlist-lan.json` allows sources from `10.31.0.0/24` and
`172.18.0.0/16`. Do not forward this plaintext HTTP API to the public internet.

The temporary `openlist-bridge.service` and `/data/openlist-bridge/` were removed
after validating direct LAN access. No separate proxy is needed. Download
center was gracefully restarted to activate its LAN listener. If the NAS IP or
Docker subnet changes, update the plugin's LAN configuration as appropriate.

## Default bridge NAT service removed at the administrator's request

`um-react`, `ariang`, and `portainer` still use Docker's default `bridge`.
The administrator explicitly requested removal after being informed of that
dependency. `/data/docker-bridge-nat/apply.sh`, its empty parent directory, and
`docker-bridge-nat.service` have now been deleted and the old unit disabled.
No containers were migrated as part of this removal. `bridge-net.service`
remains active and does not provide NAT for default `bridge`.
