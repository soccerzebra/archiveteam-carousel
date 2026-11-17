# Carousel — a single image: the ArchiveTeam crawl toolchain + Python 3.11 + the worker
# runtime + the control plane (web UI / REST / WebSocket / supervisor). One container runs the
# control plane and spawns a worker subprocess per active project, so the worker runtime and
# toolchain must be present here too.
#
# Multi-arch (linux/amd64, linux/arm64). wget-at is compiled from ArchiveTeam's wget-lua source
# in the first stage, following that repo's own Dockerfile recipe (NSS TLS build, same library
# versions, same configure flags) so the binary matches the one ArchiveTeam ships; only the
# architecture differs. Build for the host:
#   docker build -f docker/Dockerfile -t carousel .
#
# Every input is pinned (base images by digest, wget-lua + gnulib by commit, tarballs by
# SHA-256). To move to a new wget-at release, bump WGET_AT_COMMIT + WGET_AT_VERSION together;
# catalog-compat.yml flags when a project needs a newer wget-at than we build.

ARG WGET_AT_COMMIT=c6f35eeffd372579f8b3a6e042c836f58f38de7f
ARG WGET_AT_VERSION=1.21.3-at.20261026.01

# ---------------------------------------------------------------------------------------------
# wget-at build
# ---------------------------------------------------------------------------------------------
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc AS wget-at
ARG WGET_AT_COMMIT
ARG WGET_AT_VERSION
ENV LC_ALL=C DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential autoconf autoconf-archive automake autopoint gettext texinfo gperf flex \
      bzip2 cmake pkg-config ca-certificates wget git rsync \
      gyp ninja-build libnspr4-dev libsqlite3-dev nss-plugin-pem \
      zlib1g-dev libidn-dev libidn2-dev libpsl-dev libpcre2-dev libgpgme-dev libluajit-5.1-dev \
 && rm -rf /var/lib/apt/lists/* \
 # The libraries built below (brotli, zstd, c-ares, NSS) must shadow Debian's older copies at
 # link and run time. ld.so.conf.d is read alphabetically, so /usr/local/lib already precedes the
 # multiarch dir on amd64 (libc.conf < x86_64-linux-gnu.conf) but not on arm64
 # (aarch64-linux-gnu.conf < libc.conf); this makes every architecture resolve like amd64.
 && echo /usr/local/lib > /etc/ld.so.conf.d/00-usr-local.conf

WORKDIR /tmp

# NSS (the TLS stack behind --impersonate) plus the system NSPR/PEM libs it runs with.
RUN set -eux \
 && multiarch="$(gcc -print-multiarch)" \
 && wget -q https://github.com/nss-dev/nss/archive/refs/tags/NSS_3_121_RTM.tar.gz \
 && echo 'b88d4af0f00186d2542c86cf17a2031f6cca6b9fb31cb64a305589ee1ca4fe8b  NSS_3_121_RTM.tar.gz' | sha256sum -c - \
 && tar xf NSS_3_121_RTM.tar.gz \
 && cd nss-NSS_3_121_RTM \
 && ./build.sh -o --disable-tests --system-sqlite --system-nspr \
 && mkdir -p /usr/local/include/nss /usr/local/include/nspr \
 && cp -a ../dist/public/nss/. /usr/local/include/nss/ \
 && cp -a /usr/include/nspr/. /usr/local/include/nspr/ \
 && rm -f ../dist/Release/lib/*.TOC \
 && cp -a ../dist/Release/lib/* /usr/local/lib/ \
 && cp -a /usr/lib/"$multiarch"/nss/libnsspem.so* /usr/local/lib/ \
 && for l in libnspr4 libplc4 libplds4; do cp -a /lib/"$multiarch"/$l.so* /usr/local/lib/; done \
 && cd /tmp && rm -rf nss-* NSS_* dist

RUN set -eux \
 && wget -q https://github.com/google/brotli/archive/refs/tags/v1.2.1.tar.gz -O brotli-1.2.1.tar.gz \
 && echo '98d35d11a78dc453aebb7d10c3748f3f73ff78c8267c0dc697b8631f67bf7e8f  brotli-1.2.1.tar.gz' | sha256sum -c - \
 && tar xf brotli-1.2.1.tar.gz \
 && cmake -S brotli-1.2.1 -B brotli-build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local \
      -DCMAKE_INSTALL_LIBDIR=lib -DBUILD_SHARED_LIBS=ON -DBROTLI_BUILD_TOOLS=OFF -DBROTLI_DISABLE_TESTS=ON \
 && cmake --build brotli-build --parallel "$(nproc)" \
 && cmake --install brotli-build \
 && rm -rf brotli-*

RUN set -eux \
 && wget -q https://github.com/vapier/ncompress/archive/c576364d691df490ec1841d028c7bece2b523c58.tar.gz -O ncompress.tar.gz \
 && echo '4ffc3de25ac0ba2e76684e0b300502db8bba931de99a8a0d9dc0533d1efd4f2a  ncompress.tar.gz' | sha256sum -c - \
 && tar xf ncompress.tar.gz \
 && cd ncompress-c576364d691df490ec1841d028c7bece2b523c58 \
 && echo 'WGET_AT_NCOMPRESS { global: ncompress_compress_fd; ncompress_uncompress_fd; local: *; };' > ncompress.map \
 && gcc -fPIC -shared -Dcompress=ncompress_compress_fd -Ddecompress=ncompress_uncompress_fd -Dstatic= \
      -Wl,--version-script=ncompress.map -o /usr/local/lib/libncompress.so compress.c \
 && cd /tmp && rm -rf ncompress*

RUN set -eux \
 && wget -q https://github.com/facebook/zstd/releases/download/v1.4.4/zstd-1.4.4.tar.gz \
 && echo '59ef70ebb757ffe74a7b3fe9c305e2ba3350021a918d168a046c6300aeea9315  zstd-1.4.4.tar.gz' | sha256sum -c - \
 && tar xf zstd-1.4.4.tar.gz \
 && make -C zstd-1.4.4 -j "$(nproc)" \
 && make -C zstd-1.4.4 install \
 && rm -rf zstd-*

RUN set -eux \
 && wget -q https://github.com/c-ares/c-ares/releases/download/cares-1_23_0/c-ares-1.23.0.tar.gz \
 && echo 'cb614ecf78b477d35963ebffcf486fc9d55cc3d3216f00700e71b7d4868f79f5  c-ares-1.23.0.tar.gz' | sha256sum -c - \
 && tar xf c-ares-1.23.0.tar.gz \
 && cd c-ares-1.23.0 \
 && ./configure --disable-tests \
 && make -j "$(nproc)" \
 && make install \
 && cd /tmp && rm -rf c-ares-* \
 && ldconfig

# wget-lua at a pinned commit; its gnulib submodule is fetched at the commit wget-lua records
# (from the GitHub mirror of gnulib, which serves single commits shallowly).
RUN set -eux \
 && git init -q wget && cd wget \
 && git remote add origin https://github.com/ArchiveTeam/wget-lua.git \
 && git fetch -q --depth 1 origin "$WGET_AT_COMMIT" \
 && git checkout -q FETCH_HEAD \
 && test "$(git rev-parse HEAD)" = "$WGET_AT_COMMIT" \
 && git config submodule.gnulib.url https://github.com/coreutils/gnulib.git \
 && git submodule update -q --init --depth 1 gnulib \
 && ./bootstrap --skip-po \
 && ./configure --with-ssl=nss --with-libnss-prefix=/usr/local --with-cares --disable-nls \
 && make -j "$(nproc)" \
 && src/wget --version | head -1 | grep -qF "GNU Wget $WGET_AT_VERSION " \
 && mkdir -p /out/bin /out/lib \
 && cp src/wget /out/bin/wget-lua \
 && cp -a /usr/local/lib/*.so* /usr/local/lib/*.chk /out/lib/ \
 && cd /tmp && rm -rf wget

# ---------------------------------------------------------------------------------------------
# CA bundle wget-at is configured with (newer than bookworm's, as in ArchiveTeam's image)
# ---------------------------------------------------------------------------------------------
FROM debian:trixie-slim@sha256:a72b4433de1cf7d6bae0c4cee74d0547f58601a643b3375f070c51600d63b6b2 AS certs
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------------------------
FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc
ARG WGET_AT_COMMIT
ARG WGET_AT_VERSION
LABEL io.archiveteam.wget-at.version="${WGET_AT_VERSION}" \
      io.archiveteam.wget-at.commit="${WGET_AT_COMMIT}"
ENV LC_ALL=C.UTF-8 DEBIAN_FRONTEND=noninteractive

# Python 3.11 (+ distutils, which Debian unbundles) — the newest Python that still ships stdlib
# distutils, which the project pipelines we run unchanged import.
# The rest is what project pipelines and their install hooks expect on the box: wget-at's
# runtime libraries, rsync/curl for uploads, sudo (install hooks call it), LuaJIT + luarocks
# and the headers to build the Lua rocks below, gcc/make/git, and poppler-utils.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3.11 python3.11-venv python3.11-dev python3-distutils python3-pip \
      ca-certificates curl gzip rsync sudo bash git gcc make \
      libpcre2-8-0 libidn2-0 libpsl5 libsqlite3-0 libunistring2 zlib1g \
      libluajit-5.1-dev luarocks \
      libidn2-dev libssl-dev libzzip-dev zlib1g-dev \
      poppler-utils \
 && rm -rf /var/lib/apt/lists/* \
 && ln -sf /usr/bin/python3.11 /usr/local/bin/python3 \
 && ln -sf /usr/bin/python3.11 /usr/local/bin/python \
 # Debian marks its Python externally-managed (PEP 668), which blocks pip — including a
 # project's warrior-install.sh. Remove the marker so pip works in this container.
 && rm -f /usr/lib/python3.11/EXTERNALLY-MANAGED

# wget-at, its libraries, and its config (points the NSS build at the CA bundle).
COPY --from=wget-at /out/lib/ /usr/local/lib/
COPY --from=wget-at /out/bin/wget-lua /usr/local/bin/wget-lua
COPY --from=certs /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
RUN mkdir -p /usr/local/etc \
 && echo 'ca_certificate = /etc/ssl/certs/ca-certificates.crt' > /usr/local/etc/wgetrc \
 # /usr/local/lib first on every architecture, so wget-at loads the libraries it was built with
 # rather than Debian's older brotli/zstd (see the wget-at stage).
 && echo /usr/local/lib > /etc/ld.so.conf.d/00-usr-local.conf \
 && ldconfig \
 && wget-lua --version | head -1 | grep -qF "GNU Wget $WGET_AT_VERSION " \
 && for l in libbrotlidec libbrotlicommon libzstd libcares libnss3 libssl3 libnspr4; do \
      ldd /usr/local/bin/wget-lua | grep -q "$l\.so.* => /usr/local/lib/" \
        || { echo "wget-lua resolves $l outside /usr/local/lib"; ldd /usr/local/bin/wget-lua; exit 1; }; \
    done

# Lua libraries project scripts `require`, at pinned versions. miniblooms isn't on the luarocks
# server, so it's built from a checksummed source archive with our own rockspec.
COPY docker/rocks/ /tmp/rocks/
RUN set -eux \
 && curl -fsSL -o /tmp/miniblooms.tar.gz \
      "https://web.archive.org/web/20260720090735id_/https://codeload.github.com/bitdivine/miniblooms/tar.gz/6b5f98e7e22348b00a0e71a122a79ebe35899727" \
 && echo '55522d196858c251ae6128c5ed8f1dcba22b9df339addd68c9ee547379b7648e  /tmp/miniblooms.tar.gz' | sha256sum -c - \
 && tar -xzf /tmp/miniblooms.tar.gz -C /tmp \
 && (cd /tmp/miniblooms-6b5f98e7e22348b00a0e71a122a79ebe35899727 && luarocks make /tmp/rocks/miniblooms-0.5-2.rockspec) \
 && while read -r rock version; do \
      case "$rock" in ''|'#'*) continue ;; esac; \
      luarocks install "$rock" "$version"; \
    done < /tmp/rocks/requirements.txt \
 && rm -rf /tmp/miniblooms* /tmp/rocks /root/.cache

# Python dependencies, from the hash-locked requirements-image.txt: carousel's own (fastapi,
# uvicorn, httpx, pydantic) plus the libraries project pipelines import directly
# (tornado/requests/zstandard). --require-hashes refuses anything not pinned in the lock.
COPY requirements-image.txt /tmp/requirements-image.txt
RUN python3 -m pip install --no-cache-dir --require-hashes -r /tmp/requirements-image.txt \
 && rm /tmp/requirements-image.txt

# crane (google/go-containerregistry): a static, daemon-free client that reads image
# registries directly. The installer uses it to export a docker-only project's /grab dir.
# The release tarball is verified against its published SHA-256 before unpacking.
ARG CRANE_VERSION=0.20.2
RUN set -eux \
 && case "$(dpkg --print-architecture)" in \
      amd64) arch=x86_64; sum=c14340087103ba9dadf61d45acd20675490fd0ccbd56ac7901fc1b502137f44b ;; \
      arm64) arch=arm64;  sum=aff0db48825124c9331ea310057214bd4e92c01aa2e414d539e9659841d9422a ;; \
      *) echo "no crane build pinned for $(dpkg --print-architecture)"; exit 1 ;; \
    esac \
 && curl -fsSL -o /tmp/crane.tar.gz \
      "https://github.com/google/go-containerregistry/releases/download/v${CRANE_VERSION}/go-containerregistry_Linux_${arch}.tar.gz" \
 && echo "${sum}  /tmp/crane.tar.gz" | sha256sum -c - \
 && tar -xz -C /usr/local/bin -f /tmp/crane.tar.gz crane \
 && rm /tmp/crane.tar.gz \
 && crane version

# Make wget-at discoverable at the paths project pipelines probe, and create data dirs.
# Projects ask for a specific TLS variant (nss/openssl/gnutls); point them all at wget-lua.
RUN mkdir -p /home/warrior/data /home/warrior/projects \
 && for v in wget-at wget-at-gnutls wget-at-openssl wget-at-nss; do \
      ln -sf /usr/local/bin/wget-lua /home/warrior/data/$v; \
    done

ENV PYTHONPATH=/opt/carousel:/opt/carousel/worker

# Worker runtime: importable as `worker`, and `seesaw` via PYTHONPATH. Copied before the
# control-plane code so app edits don't invalidate this (less-volatile) layer.
COPY worker/ /opt/carousel/worker/

# Control plane + dashboard (the volatile layer). --no-deps: every dependency is already
# installed from the lock above. --no-build-isolation: use the setuptools already in the
# image instead of fetching the build backend from PyPI at build time.
WORKDIR /opt/carousel
COPY pyproject.toml ./
COPY controlplane/ ./controlplane/
COPY web/ ./web/
RUN python3 -m pip install --no-cache-dir --no-deps --no-build-isolation -e .

WORKDIR /home/warrior
EXPOSE 8001
STOPSIGNAL SIGINT
# Probes whatever port the app is actually listening on (PORT, default 8001).
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python3 -c "import os, sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8001'), timeout=5).status == 200 else 1)"
ENTRYPOINT ["carousel"]
