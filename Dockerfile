FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    git \
    make \
    gcc \
    g++ \
    gfortran \
    python3 \
    python3-pip \
    python3-venv \
    python3-yaml \
    sqlite3 \
    libsqlite3-dev \
    libbz2-dev \
    zlib1g-dev \
    libzstd-dev \
    liblzma-dev \
    file \
    grep \
    sed \
    gawk \
    coreutils \
    findutils \
    procps \
    time \
    wget \
    curl \
    tar \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/project

# Copy mx3 repo into the layout expected by the scripts: /opt/project/mx3
COPY mx3 /opt/project/mx3

# Clone Sniper fork
RUN git clone --branch dvfs --single-branch https://github.com/shinsakukataoka/sniper-hybrid.git /opt/project/sniper

# Python deps commonly used by mx3
RUN python3 -m pip install --break-system-packages --no-cache-dir \
    pyyaml \
    notebook \
    jupyter

# Build Sniper with sqlite headers/libs available from system packages
WORKDIR /opt/project/sniper
ENV CPATH=/usr/include
ENV LIBRARY_PATH=/usr/lib/x86_64-linux-gnu
ENV CFLAGS=-I/usr/include
ENV CXXFLAGS=-I/usr/include
ENV LDFLAGS=-L/usr/lib/x86_64-linux-gnu
ENV LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu
RUN make -j"$(nproc)"

WORKDIR /opt/project
ENV PYTHONUNBUFFERED=1
ENV REPO_ROOT=/opt/project

ENTRYPOINT ["/opt/project/mx3/docker/entrypoint.sh"]
CMD ["bash"]
