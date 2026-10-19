# Apollonia PBX: Asterisk 22 LTS built from source (verified tarball).
# Adapted from the call assistant's /opt/pbx/Dockerfile (used as a read-only template, 2026-10-19):
#   + func_curl (dialplan -> agent), explicit AudioSocket checks
#   - recording notice, sox/espeak, python3, PBX scripts and entrypoint (config is mounted, runbook §5)
ARG DEBIAN_IMAGE=debian:trixie-20260918-slim

FROM ${DEBIAN_IMAGE} AS build
ARG ASTERISK_VERSION=22.11.0
ARG ASTERISK_SHA256=3bd5ee040509a3d3cd9b1ba9520c18e6ec0a7e7981ca68c457dcd36ba3c54d94
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential pkg-config curl ca-certificates bzip2 patch python3 file \
        libedit-dev libjansson-dev libxml2-dev uuid-dev libsqlite3-dev libssl-dev libsrtp2-dev \
        libunbound-dev libcurl4-openssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /usr/src
RUN curl -fsSLo asterisk.tar.gz \
        "https://downloads.asterisk.org/pub/telephony/asterisk/releases/asterisk-${ASTERISK_VERSION}.tar.gz" \
    && echo "${ASTERISK_SHA256}  asterisk.tar.gz" | sha256sum -c - \
    && tar xzf asterisk.tar.gz \
    && mv "asterisk-${ASTERISK_VERSION}" asterisk

WORKDIR /usr/src/asterisk
# Portable binary; no sound packs. Fail the build if any module we depend on is missing.
RUN ./configure --with-pjproject-bundled --without-x11 --without-gtk2 > /dev/null \
    && make menuselect.makeopts \
    && menuselect/menuselect \
        --disable BUILD_NATIVE \
        --enable res_srtp --enable res_resolver_unbound --enable func_curl \
        --enable res_audiosocket --enable chan_audiosocket \
        --disable-category MENUSELECT_CORE_SOUNDS \
        --disable-category MENUSELECT_MOH \
        --disable-category MENUSELECT_EXTRA_SOUNDS \
        menuselect.makeopts \
    && make -j"$(nproc)" > /dev/null \
    && make install DESTDIR=/stage > /dev/null \
    && for m in res_pjsip res_srtp res_resolver_unbound func_curl res_audiosocket chan_audiosocket; do \
         test -f /stage/usr/lib/asterisk/modules/$m.so || { echo "MISSING MODULE: $m"; exit 1; }; \
       done \
    && rm -rf /stage/var/run

# Runtime packages = whatever the built binaries link against.
RUN find /stage -type f \( -name '*.so*' -o -path '*/sbin/*' \) -exec file {} + \
        | awk -F: '/ELF/ {print $1}' \
        | xargs ldd 2>/dev/null \
        | awk '/=> \// {print $3}' | sort -u | xargs -r realpath | sort -u \
        | xargs -r dpkg -S 2>/dev/null | cut -d: -f1 | sort -u > /stage/runtime-packages.txt \
    && cat /stage/runtime-packages.txt

FROM ${DEBIAN_IMAGE}
COPY --from=build /stage/runtime-packages.txt /tmp/runtime-packages.txt
RUN apt-get update \
    && xargs -a /tmp/runtime-packages.txt apt-get install -y --no-install-recommends \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* /tmp/runtime-packages.txt \
    && useradd --uid 10002 --user-group --no-create-home --shell /usr/sbin/nologin asterisk

COPY --from=build /stage/usr/ /usr/
COPY --from=build /stage/var/ /var/
RUN ldconfig && asterisk -V \
    && mkdir -p /etc/asterisk /var/spool/asterisk /var/log/asterisk /var/run/asterisk \
    && chown -R asterisk:asterisk /etc/asterisk /var/lib/asterisk /var/spool/asterisk \
        /var/log/asterisk /var/run/asterisk

USER asterisk
CMD ["asterisk", "-f", "-vvv"]
