Build image
```bash
docker build -t mx3-docker .
```
Run it with SPEC and traces mounted from the host:
```
docker run --rm -it \
  -v /path/to/spec2017:/mnt/spec2017 \
  -v /path/to/traces:/mnt/traces \
  -v "$PWD":/work \
  -w /work \
  mx3-docker bash
```

The image includes mx3 and clones/builds sniper-hybrid inside the container.

