from __future__ import annotations

from .. import ServiceInfo


class AsyncZeroconf:
    async def async_register_service(self, info: ServiceInfo) -> None: ...

    async def async_update_service(self, info: ServiceInfo) -> None: ...

    async def async_unregister_service(self, info: ServiceInfo) -> None: ...

    async def async_close(self) -> None: ...

