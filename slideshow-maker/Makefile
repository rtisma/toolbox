# Build and run slideshow-maker in a container.
#
# `annotate` shells out to Docker itself, to run a second, throwaway
# ffmpeg-drawtext container for the actual text burn-in (Homebrew/Debian's
# ffmpeg lacks the drawtext filter, see slideshow-maker.py). That means this
# container needs the *host* Docker socket, not a Docker-in-Docker setup --
# and any path it hands to that nested `docker run -v ...` must resolve on
# the host, not inside this container. So DATA_DIR is mounted at the same
# absolute path here as on the host, rather than remapped to something like
# /work: whatever path you pass slideshow-maker.py in ARGS must live under
# DATA_DIR.
#
# Usage:
#   make run ARGS="check"
#   make run ARGS="generate-list -i $HOME/Pictures/trip -o $HOME/Pictures/trip/list.csv"
#   make run ARGS="render -l $HOME/Pictures/trip/list.csv -o $HOME/Pictures/trip/out.mp4 -d 2"
#   make run DATA_DIR=/Volumes/Photos ARGS="generate-list -i /Volumes/Photos/trip -o /Volumes/Photos/trip/list.csv"

IMAGE    := slideshow-maker:local
ROOT_DIR := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
DATA_DIR ?= $(HOME)

.PHONY: build run shell check clean

build:
	docker build -t $(IMAGE) "$(ROOT_DIR)"

run: build
	docker run --rm -i \
		-v /var/run/docker.sock:/var/run/docker.sock \
		-v "$(DATA_DIR):$(DATA_DIR)" \
		-w "$(DATA_DIR)" \
		$(IMAGE) $(ARGS)

# Quick dependency check; doesn't need DATA_DIR mounted.
check: build
	docker run --rm \
		-v /var/run/docker.sock:/var/run/docker.sock \
		$(IMAGE) check

shell: build
	docker run --rm -it \
		-v /var/run/docker.sock:/var/run/docker.sock \
		-v "$(DATA_DIR):$(DATA_DIR)" \
		-w "$(DATA_DIR)" \
		--entrypoint /bin/bash \
		$(IMAGE)

clean:
	docker rmi $(IMAGE) 2>/dev/null || true
