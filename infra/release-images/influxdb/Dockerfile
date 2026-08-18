# syntax=docker/dockerfile:1.7

FROM quay.io/influxdb/cross-builder@sha256:af550a559d42095b3cec8ccfdea614ac2364d924fc8e16813fc2a2e693477630 AS influxd-builder

WORKDIR /src
ADD --checksum=sha256:0d6d47516cd385285e878dac92e04b534667788449cf42e7bf6a96bb050c368f \
    https://codeload.github.com/influxdata/influxdb/tar.gz/d4fa1941fd4adb29a556e774de426dfbab72f346 \
    /tmp/influxdb.tar.gz
ADD --checksum=sha256:9cb2df818f1f5badc23e051f51eef7e8b143540233947606eadad309f395bb0e \
    https://github.com/influxdata/ui/releases/download/OSS-v2.9.0/build.tar.gz \
    /tmp/influxdb-ui.tar.gz
ADD --checksum=sha256:ffdfb10ab2fc4cbceb10af8d274e1fe88bc6bef5434f7d913dd1b7601f02c9ee \
    https://codeload.github.com/influxdata/openapi/tar.gz/8b5f1bbb2cd388eb454dc9da19e3d2c4061cdf5f \
    /tmp/influxdb-openapi.tar.gz

RUN tar -xzf /tmp/influxdb.tar.gz --strip-components=1 \
    && mkdir -p static/data /tmp/influxdb-openapi \
    && tar -xzf /tmp/influxdb-ui.tar.gz -C static/data \
    && tar -xzf /tmp/influxdb-openapi.tar.gz -C /tmp/influxdb-openapi --strip-components=1 \
    && cp /tmp/influxdb-openapi/contracts/oss.json static/data/swagger.json \
    && test "$(go env GOVERSION)" = "go1.25.10" \
    && GOTOOLCHAIN=local go get \
        github.com/buger/jsonparser@v1.1.2 \
        github.com/go-jose/go-jose/v4@v4.1.4 \
        go.opentelemetry.io/otel@v1.43.0 \
        go.opentelemetry.io/otel/metric@v1.43.0 \
        go.opentelemetry.io/otel/sdk@v1.43.0 \
        go.opentelemetry.io/otel/sdk/metric@v1.43.0 \
        go.opentelemetry.io/otel/trace@v1.43.0 \
        golang.org/x/sys@v0.42.0 \
    && GOTOOLCHAIN=local go generate ./static

RUN export CC="$(xcc linux x86_64)" \
    && export LINUX_EXTLD="-fno-PIC -static-pie -Wl,-z,stack-size=8388608" \
    && CGO_ENABLED=1 PKG_CONFIG="$(command -v pkg-config)" GOTOOLCHAIN=local go build \
        -tags assets,sqlite_foreign_keys,sqlite_json,static_build,noasm \
        -buildmode=pie \
        -trimpath \
        -buildvcs=false \
        -ldflags "-s -w -X main.version=2.9.1 -X main.commit=d4fa1941fd4adb29a556e774de426dfbab72f346 -X main.date=2026-05-11T18:30:49Z -linkmode=external -extld=${CC} -extldflags '${LINUX_EXTLD} -L$(find_rustlib_path x86_64-unknown-linux-musl) -Wl,-lunwind'" \
        -o /out/influxd \
        ./cmd/influxd

FROM golang:1.25.10-alpine@sha256:c4ea15b4a7912716eb362a022e2b12317762eca387423760bc59c0f9ae69423c AS tools-builder

ADD --checksum=sha256:d4a2c499041e45f192223d5c1d483c0d619fd97cbddd57c2e2beecdd57b3be5e \
    https://codeload.github.com/influxdata/influx-cli/tar.gz/8cdf40161d7662e942582086853fca948fc1a842 \
    /tmp/influx-cli.tar.gz
ADD --checksum=sha256:e1bd68bcc5c2d356aea7893d7089a6cac5adb6ca4e49474dcf3af8e6133fa730 \
    https://codeload.github.com/TomWright/dasel/tar.gz/e3c6663d78c62f69579d8f9afd00453dc3d01c29 \
    /tmp/dasel.tar.gz

RUN mkdir -p /src/influx-cli /src/dasel /out \
    && tar -xzf /tmp/influx-cli.tar.gz -C /src/influx-cli --strip-components=1 \
    && tar -xzf /tmp/dasel.tar.gz -C /src/dasel --strip-components=1 \
    && test "$(go env GOVERSION)" = "go1.25.10" \
    && cd /src/influx-cli \
    && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOTOOLCHAIN=local go build \
        -trimpath -buildvcs=false \
        -ldflags "-s -w -X main.version=2.8.0 -X main.commit=8cdf40161d7662e942582086853fca948fc1a842 -X main.date=2023-08-14T00:00:00Z" \
        -o /out/influx ./cmd/influx \
    && cd /src/dasel \
    && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOTOOLCHAIN=local go build \
        -trimpath -buildvcs=false \
        -ldflags "-s -w -X github.com/tomwright/dasel/v3/internal.Version=v3.10.1" \
        -o /out/dasel ./cmd/dasel

FROM influxdb:2.9.1-alpine@sha256:818eb972f70c4b6b04b8b952b9ef54480221d92f2ee9cadf8f73c180efa69ac7

COPY --from=influxd-builder /out/influxd /usr/local/bin/influxd
COPY --from=tools-builder /out/influx /usr/local/bin/influx
COPY --from=tools-builder /out/dasel /usr/local/bin/dasel

RUN influxd version \
    && influx version \
    && dasel --version
