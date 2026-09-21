# Base image for the sandbox containers. Everything the model is likely to need
# is baked in, because dnf inside a container is lost when it gets recycled.
FROM docker.io/library/fedora:44

# bash, coreutils and curl are named explicitly to replace the base image's
# coreutils-single and curl-minimal, which conflict with the full packages;
# --allowerasing is what lets dnf swap them.
RUN dnf install -y --setopt=install_weak_deps=False --allowerasing \
      bash coreutils curl ca-certificates \
      git jq ripgrep fd-find less procps-ng hostname \
      file tar gzip unzip xz which findutils diffutils \
      iproute bind-utils openssl \
      python3 python3-pip \
      nodejs24-bin nodejs24-npm-bin \
      gcc gcc-c++ make binutils pkgconf-pkg-config openssl-devel lld \
    && dnf clean all && rm -rf /var/cache/dnf

ENV LANG=C.UTF-8 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_ROOT_USER_ACTION=ignore

# Rust comes from rustup, not dnf: no distribution ships a current rustc.
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
      | sh -s -- -y --no-modify-path --profile minimal --default-toolchain stable \
    && rustup component add clippy rustfmt \
    && rm -rf /usr/local/rustup/downloads /usr/local/rustup/tmp /usr/local/cargo/registry

# run_command uses `bash -lc`, so cargo must be on PATH for a login shell too.
RUN printf 'export PATH=/usr/local/cargo/bin:$PATH\n' > /etc/profile.d/rust.sh

# lld links far faster than the default, and Rust only picks it up by default on
# x86_64, not on aarch64.
RUN printf '[build]\nrustflags = ["-C", "link-arg=-fuse-ld=lld"]\njobs = 2\n' \
      > /usr/local/cargo/config.toml

# Fail the build, not the model's first command, when a tool is missing or the
# toolchain does not actually work end to end.
RUN set -eu; \
    for c in timeout sleep hostname nproc bash sh git curl rg fd jq ip dig \
             python3 pip3 node npm npx make gcc g++ cc cargo rustc ld.lld; do \
      command -v "$c" >/dev/null || { echo "missing: $c" >&2; exit 1; }; \
    done; \
    cd /tmp && cargo new -q t && cd t && cargo build -q && ./target/debug/t >/dev/null; \
    cd / && rm -rf /tmp/t

WORKDIR /workspace
