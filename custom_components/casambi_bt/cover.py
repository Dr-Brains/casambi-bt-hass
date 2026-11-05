"""Support for Casambi compatible covers (blinds, shutters, venetian blinds)."""

from __future__ import annotations

import logging
from typing import Any
from typing import cast

from CasambiBt import Unit, UnitControlType

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CasambiApi
from .const import DOMAIN
from .entities import CasambiUnitEntity, TypedEntityDescription

_LOGGER = logging.getLogger(__name__)

_ILLUMY_FOLD_BITS_OFFSET = 32
_ILLUMY_FOLD_BITS_LENGTH = 10
_ILLUMY_FOLD_MAX_RAW = (1 << _ILLUMY_FOLD_BITS_LENGTH) - 1


def _read_bits(raw: bytes, offset: int, length: int) -> int:
    """Read `length` bits at `offset` from little-endian raw state bytes."""
    byte_offset = offset // 8
    bit_offset = offset % 8
    num_bytes = (length + bit_offset + 7) // 8
    val = int.from_bytes(raw[byte_offset : byte_offset + num_bytes], "little")
    return (val >> bit_offset) & ((1 << length) - 1)


def _write_bits(raw: bytearray, offset: int, length: int, value: int) -> None:
    """Set `length` bits at `offset` in little-endian raw state bytes."""
    val_shifted = value << (offset % 8)
    byte_len = (length + offset % 8 + 7) // 8
    val_bytes = val_shifted.to_bytes(byte_len, byteorder="little", signed=False)
    clear_mask = ((1 << length) - 1) << (offset % 8)
    for i in range(byte_len):
        byte_idx = offset // 8 + i
        mask_byte = (clear_mask >> (i * 8)) & 0xFF
        raw[byte_idx] = (raw[byte_idx] & ~mask_byte) | (val_bytes[i] & mask_byte)


async def _send_raw_state(api, unit: Unit, raw: bytearray) -> None:
    """Send a full raw-state packet to the unit."""
    from CasambiBt._operation import OpCode

    await api.ensure_connected()
    await api.casa._send(unit, bytes(raw), OpCode.SetState)


def _is_legacy_cover_unit(unit: Unit) -> bool:
    """Return True for DIMMER-based EXT covers (non-hybrid)."""
    controls = {c.type for c in unit.unitType.controls}
    return (
        unit.unitType.mode.startswith("EXT/")
        and UnitControlType.DIMMER in controls
        and UnitControlType.TEMPERATURE not in controls
    )


def _is_illumy_hybrid_cover_unit(unit: Unit) -> bool:
    """Return True for Illumy-style hybrid units with folded-blinds raw control."""
    controls = {c.type for c in unit.unitType.controls}
    return (
        unit.unitType.mode.startswith("EXT/")
        and UnitControlType.DIMMER in controls
        and UnitControlType.TEMPERATURE in controls
    )


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the Casambi cover entities.

    All covers (SO! shutters and Lamel venetian blinds) use CasambiCover.
    Tilt (slat angle) for Lamel is exposed as a NumberEntity in lamel_controls.py.
    """
    casa_api: CasambiApi = hass.data[DOMAIN][config_entry.entry_id]

    cover_entities: list[CoverEntity] = []
    for unit in casa_api.get_units():
        if _is_legacy_cover_unit(unit):
            cover_entities.append(CasambiCover(casa_api, unit))
        elif _is_illumy_hybrid_cover_unit(unit):
            cover_entities.append(CasambiIllumyFoldCover(casa_api, unit))

    _LOGGER.info("Creating %d cover entities", len(cover_entities))
    async_add_entities(cover_entities)


class CasambiCover(CasambiUnitEntity, CoverEntity):
    """Defines a Casambi cover entity for blinds and shutters."""

    def __init__(self, api: CasambiApi, unit: Unit) -> None:
        """Initialize a Casambi cover entity."""
        desc = TypedEntityDescription(key=unit.uuid, name=None, entity_type="cover")
        super().__init__(api, desc, unit)

        self._attr_device_class = CoverDeviceClass.BLIND
        self._attr_supported_features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.SET_POSITION
        )

    @property
    def current_cover_position(self) -> int | None:
        """Return the current position (0=closed, 100=open).

        Winsol/Casambi convention is inverted: dimmer=0 means fully open,
        dimmer=255 means fully closed. We invert to match HA convention.
        """
        unit = cast("Unit", self._obj)
        if unit.state is not None and unit.state.dimmer is not None:
            return 100 - (unit.state.dimmer * 100 // 255)
        return None

    @property
    def is_closed(self) -> bool | None:
        """Return True if the cover is fully closed."""
        pos = self.current_cover_position
        if pos is None:
            return None
        return pos == 0

    async def async_open_cover(self, **kwargs) -> None:
        """Open the cover (dimmer=0 in Winsol convention)."""
        await self._api.ensure_connected()
        unit = cast("Unit", self._obj)
        await self._api.casa.setLevel(unit, 0)

    async def async_close_cover(self, **kwargs) -> None:
        """Close the cover (dimmer=255 in Winsol convention)."""
        await self._api.ensure_connected()
        unit = cast("Unit", self._obj)
        await self._api.casa.setLevel(unit, 255)

    async def async_set_cover_position(self, **kwargs) -> None:
        """Move the cover to a specific position (0=closed, 100=open)."""
        await self._api.ensure_connected()
        unit = cast("Unit", self._obj)
        position = kwargs[ATTR_POSITION]
        # Invert: HA 100%=open → Winsol dimmer=0; HA 0%=closed → Winsol dimmer=255
        await self._api.casa.setLevel(unit, (100 - position) * 255 // 100)


class CasambiIllumyFoldCover(CasambiUnitEntity, CoverEntity):
    """Cover entity for Illumy folded-blinds control encoded in raw unit state."""

    def __init__(self, api, unit: Unit) -> None:
        desc = TypedEntityDescription(key=unit.uuid, name=None, entity_type="cover")
        super().__init__(api, desc, unit)
        self._attr_device_class = CoverDeviceClass.BLIND
        self._attr_supported_features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.SET_POSITION
        )

    def _raw_position(self) -> int | None:
        unit = cast("Unit", self._obj)
        if unit.state is None or unit.state.raw_state is None:
            return None
        return _read_bits(
            unit.state.raw_state,
            _ILLUMY_FOLD_BITS_OFFSET,
            _ILLUMY_FOLD_BITS_LENGTH,
        )

    @property
    def current_cover_position(self) -> int | None:
        raw_pos = self._raw_position()
        if raw_pos is None:
            return None
        return round(raw_pos * 100 / _ILLUMY_FOLD_MAX_RAW)

    @property
    def is_closed(self) -> bool | None:
        pos = self.current_cover_position
        if pos is None:
            return None
        return pos == 0

    async def _async_set_raw_position_percent(self, position: int) -> None:
        unit = cast("Unit", self._obj)
        if unit.state is None or unit.state.raw_state is None:
            _LOGGER.warning(
                "Cannot set folded-blinds position for unit %s: missing raw state",
                unit.deviceId,
            )
            return

        clamped = max(0, min(100, position))
        raw_target = round(clamped * _ILLUMY_FOLD_MAX_RAW / 100)
        raw = bytearray(unit.state.raw_state)
        _write_bits(raw, _ILLUMY_FOLD_BITS_OFFSET, _ILLUMY_FOLD_BITS_LENGTH, raw_target)
        await _send_raw_state(self._api, unit, raw)

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._async_set_raw_position_percent(100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._async_set_raw_position_percent(0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        await self._async_set_raw_position_percent(kwargs[ATTR_POSITION])
