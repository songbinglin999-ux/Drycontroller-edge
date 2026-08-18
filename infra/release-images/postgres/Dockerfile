# syntax=docker/dockerfile:1.7

FROM golang:1.25.10-alpine@sha256:c4ea15b4a7912716eb362a022e2b12317762eca387423760bc59c0f9ae69423c AS gosu-builder

WORKDIR /src
ADD --checksum=sha256:33d7537d588ea49458b9509bcf4554bdf5ceacc66da71e5caa1058ea3b689c3b \
    https://codeload.github.com/tianon/gosu/tar.gz/6456aaa0f3c854d199d0f037f068eb97515b7513 \
    /tmp/gosu.tar.gz
RUN tar -xzf /tmp/gosu.tar.gz --strip-components=1 \
    && test "$(go env GOVERSION)" = "go1.25.10" \
    && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOTOOLCHAIN=local go build \
        -trimpath -buildvcs=false -ldflags "-s -w" -o /out/gosu .

FROM postgres:15.18-alpine3.24@sha256:cae15a3b718f23497a60b7cafdcf205216d7949680972da0584db00fb68bf3e6

COPY --from=gosu-builder /out/gosu /usr/local/bin/gosu

RUN gosu --version \
    && gosu postgres true
