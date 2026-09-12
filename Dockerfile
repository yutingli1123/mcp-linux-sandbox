# Base image for the sandbox containers. Everything the model is likely to need
# is baked in, because apt inside a container is lost when it gets recycled.
FROM docker.io/library/debian:trixie-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash coreutils curl wget git ca-certificates \
      jq ripgrep fd-find less procps \
      file tar gzip unzip xz-utils \
      netcat-openbsd iproute2 dnsutils \
      vim-tiny nano \
      python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

# Notes:
#   - `fd` is `fdfind` on Debian; run_command's description says so, so the model
#     does not go looking for the wrong name.
#   - `vim-tiny` provides `vim.tiny`, not `vim`.
WORKDIR /workspace
