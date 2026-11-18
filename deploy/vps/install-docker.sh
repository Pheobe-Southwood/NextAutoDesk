#!/usr/bin/env bash
# One-time Docker installation for an Ubuntu 24.04 host.
#
# This is the only system-level change NextAutoDesk needs. It installs the
# distribution packages (docker.io, docker-compose-v2), enables the daemon and
# configures size-limited JSON logs. No Docker repository, no PPA, no reboot.
#
#   sudo ./deploy/vps/install-docker.sh
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "install-docker: run as root (sudo)" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive

echo "== installing docker.io + docker-compose-v2 =="
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2

echo "== configuring /etc/docker/daemon.json =="
mkdir -p /etc/docker
if [[ ! -f /etc/docker/daemon.json ]]; then
    cat >/etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": {"max-size": "10m", "max-file": "3"},
  "live-restore": true
}
JSON
else
    echo "keeping existing /etc/docker/daemon.json"
fi

systemctl enable --now docker
echo "== installed =="
docker --version
docker compose version
systemctl is-enabled docker
systemctl is-active docker
