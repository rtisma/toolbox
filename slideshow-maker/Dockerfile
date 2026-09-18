FROM python:3.13-slim

# ffmpeg/exiftool for generate-list and render; docker-cli (client only,
# not docker.io -- that pulls in the full daemon/containerd/runc) gives
# us the `docker` binary so `annotate` can talk out to the *host* Docker
# daemon (see Makefile) to run its own ffmpeg-drawtext container.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       libimage-exiftool-perl \
       docker-cli \
    && rm -rf /var/lib/apt/lists/*

COPY slideshow-maker.py /usr/local/bin/slideshow-maker.py
RUN chmod +x /usr/local/bin/slideshow-maker.py

ENTRYPOINT ["python3", "/usr/local/bin/slideshow-maker.py"]
CMD ["--help"]
