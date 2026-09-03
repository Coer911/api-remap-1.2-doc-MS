#!/usr/bin/env bash
# Cloud Agent dev-environment lifecycle for the MoySklad API documentation site.
# The docs run through the repo's Ruby 2.5.1 + Middleman image, so we drive it with Docker.
set -euo pipefail

IMAGE=apidoc:local
CONTAINER=apidoc
PORT=4567

ensure_docker() {
  sudo mkdir -p /etc/docker
  # fuse-overlayfs is required for a nested Docker daemon inside the Cloud Agent VM.
  echo '{"storage-driver":"fuse-overlayfs"}' | sudo tee /etc/docker/daemon.json >/dev/null

  if ! command -v docker >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io fuse-overlayfs
  fi

  if ! sudo docker info >/dev/null 2>&1; then
    sudo sh -c 'nohup dockerd >/tmp/dockerd.log 2>&1 &'
    for _ in $(seq 1 60); do
      sudo docker info >/dev/null 2>&1 && break
      sleep 1
    done
  fi
  sudo docker info >/dev/null 2>&1
}

build_image() {
  sudo docker build -t "$IMAGE" .
}

run_container() {
  sudo docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  sudo docker run -d --name "$CONTAINER" -p "$PORT:$PORT" -v "$PWD":/usr/src/app "$IMAGE"
}

case "${1:-}" in
  install)
    ensure_docker
    build_image
    ;;
  start)
    ensure_docker
    sudo docker image inspect "$IMAGE" >/dev/null 2>&1 || build_image
    run_container
    ;;
  *)
    echo "usage: $0 {install|start}" >&2
    exit 1
    ;;
esac
