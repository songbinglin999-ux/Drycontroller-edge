from __future__ import annotations

from typing import Mapping, Sequence


class ServiceInfo:
    addresses: Sequence[bytes]
    properties: Mapping[bytes, bytes]

    def __init__(
        self,
        type_: str,
        name: str,
        port: int,
        addresses: Sequence[bytes],
        *,
        server: str | None = ...,
        properties: Mapping[bytes, bytes] | None = ...,
    ) -> None: ...

