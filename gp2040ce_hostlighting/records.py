"""The records and replies that gp2040ce_hostlighting.protocol decodes.

Fields are named after the specification's. A page record has its ordinal and one attribute per field group, in
mask bit order, each a frozen dataclass. Reading a group the reply did not return raises
HostLightingGroupNotReadError, which is an AttributeError too, so hasattr() says whether it was read, as do
`has()` and `groups`. Inside a group, None is the value the specification marks none, such as an unlit control's
first LED. A record's `merged()` takes the groups a later read returned, as a host does after reading some
groups again. Records are immutable, compare equal when their page, ordinal and groups are, and copy and pickle.

SPDX-FileCopyrightText: © 2026 Jacob Simpson
SPDX-License-Identifier: GPL-3.0-or-later
"""
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, ClassVar, Generic, Optional, TypeVar, overload

__all__ = ['AnimationRecord', 'BoardListing', 'Changes', 'ControlInput', 'ControlInstances', 'ControlLights',
           'ControlRecord', 'Controller', 'Effects', 'EnumValue', 'GridCell', 'Hello', 'Identity', 'InputModeFlags',
           'InputModeRecord', 'Instances', 'LedRange', 'LightKind', 'LightRecord', 'NameRecord', 'Owner', 'PageHeader',
           'PageRead',
           'PaletteSlot', 'Position', 'Profile', 'ProfileRecord', 'Requirements', 'Settings', 'Speeds',
           'StageOutcome', 'State', 'StateChanged', 'StateRecord', 'Summary', 'SummaryRecord', 'jsonable']

# the configuration pages, with change counters in the Summary's Changes group in page order
_FIRST_CONFIGURATION_PAGE = 8
_CONFIGURATION_PAGES = 8

GroupType = TypeVar('GroupType')
RecordType = TypeVar('RecordType', bound='_Record')


class _Group(Generic[GroupType]):
    """One field group of a page record: the group's dataclass, or an error when the read did not return it."""

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name
        self.slot = '_' + name

    @overload
    def __get__(self, record: None, owner: type) -> '_Group[GroupType]':
        ...

    @overload
    def __get__(self, record: '_Record', owner: type) -> GroupType:
        ...

    def __get__(self, record: Optional['_Record'], owner: type) -> Any:
        if record is None:
            return self
        value = getattr(record, self.slot)
        if value is None:
            # imported here, since records imports nothing from the package when it loads
            from gp2040ce_hostlighting.protocol import HostLightingGroupNotReadError
            raise HostLightingGroupNotReadError(record.PAGE, self.name, record.ordinal)
        return value


class _Record:
    """A page record: an ordinal, then one checked attribute per field group."""

    __slots__ = ('ordinal',)
    ordinal: int
    PAGE: ClassVar[int]
    GROUPS: ClassVar[tuple[str, ...]]

    def __init__(self, ordinal: int, groups: dict[str, Any]) -> None:
        unknown = set(groups) - set(self.GROUPS)
        if unknown:
            raise TypeError(f"page {self.PAGE} has no field group {sorted(unknown)[0]!r}")
        object.__setattr__(self, 'ordinal', ordinal)
        for name in self.GROUPS:
            object.__setattr__(self, '_' + name, groups.get(name))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def _values(self) -> tuple[Any, ...]:
        return (self.ordinal,) + tuple(getattr(self, '_' + name) for name in self.GROUPS)

    def __eq__(self, other: object) -> bool:
        return type(other) is type(self) and other._values() == self._values()

    def __hash__(self) -> int:
        return hash((type(self),) + self._values())

    def __repr__(self) -> str:
        groups = ', '.join(f"{name}={getattr(self, '_' + name)!r}" for name in self.GROUPS)
        return f"{type(self).__name__}(ordinal={self.ordinal}, {groups})"

    def __reduce__(self) -> tuple[Any, ...]:
        """Rebuild the record through its constructor, for copy and pickle."""
        return _rebuild, (type(self), self.ordinal, {name: getattr(self, '_' + name) for name in self.GROUPS})

    @property
    def groups(self) -> frozenset[str]:
        """The names of the field groups the read returned."""
        return frozenset(name for name in self.GROUPS if getattr(self, '_' + name) is not None)

    def has(self, group: str) -> bool:
        """Say whether the read returned this field group.

        :raises ValueError: for a name that is not one of the page's groups
        """
        if group not in self.GROUPS:
            raise ValueError(f"page {self.PAGE} has no field group {group!r}")
        return getattr(self, '_' + group) is not None

    def merged(self: RecordType, newer: RecordType) -> RecordType:
        """Return this record with each group a later read of the same record returned.

        :param newer: the same page's record at the same ordinal, read with some groups
        :return: a new record, its groups from `newer` where that read returned them and from this one otherwise
        :raises ValueError: for a record of another page or ordinal
        """
        if type(newer) is not type(self) or newer.ordinal != self.ordinal:
            raise ValueError("merged() takes the same page's record at the same ordinal")
        record = object.__new__(type(self))
        object.__setattr__(record, 'ordinal', self.ordinal)
        for name in self.GROUPS:
            newest = getattr(newer, '_' + name)
            object.__setattr__(record, '_' + name, newest if newest is not None else getattr(self, '_' + name))
        return record


def jsonable(value: Any) -> Any:
    """Turn a record, field group, HELLO or listing, and what they hold, into values json.dumps() takes.

    A record becomes its ordinal and the groups read, a field group or other dataclass a dict of its fields, a
    set a sorted list, a tuple a list and bytes hex. Anything else is returned as it is.
    """
    if isinstance(value, _Record):
        return {'ordinal': value.ordinal, **{name: jsonable(getattr(value, name)) for name in value.GROUPS
                                             if value.has(name)}}
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, (set, frozenset)):
        return sorted(jsonable(member) for member in value)
    if isinstance(value, (list, tuple)):
        return [jsonable(member) for member in value]
    if isinstance(value, dict):
        return {str(key): jsonable(member) for key, member in value.items()}
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return value


def _rebuild(record_type: type, ordinal: int, groups: dict[str, Any]) -> Any:
    """Make a record again from its ordinal and groups, as copy and pickle do through __reduce__."""
    return record_type(ordinal=ordinal, **groups)


# page 1, State

@dataclass(frozen=True, slots=True, kw_only=True)
class State:
    """Page 1's State group: what the board shows now."""

    takeover_live: bool
    brightness_unsaved: bool
    animations_idle: bool
    input_mode: int
    profile: int
    player: int
    animation: Optional[int]                # None while the animations are off
    brightness_step_shown: int
    takeover_number: int
    saved_brightness_step: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Controller:
    """Page 1's Controller group, which the CLAIM reply and a status 4 reply carry too: the claim's holder."""

    holder_token: int                       # 0 while unclaimed, as the specification gives it
    application_name: str
    exclusive: bool
    claim_generation: int

    def held_by(self, token: int) -> bool:
        """Say whether this token holds the claim."""
        return bool(self.holder_token) and self.holder_token == token


@dataclass(frozen=True, slots=True, kw_only=True)
class Settings:
    """Page 1's Settings group and CONFIGURE's settings, with the specification's defaults."""

    takeover: int = 0                       # TAKEOVER_WHOLE_FRAME
    keepalive_ms: int = 2000
    brightness_applied: bool = True


class StateRecord(_Record):
    """A page 1 record."""

    PAGE = 1
    GROUPS = ('state', 'controller', 'settings')
    __slots__ = ('_state', '_controller', '_settings')
    state = _Group[State]()
    controller = _Group[Controller]()
    settings = _Group[Settings]()

    def __init__(self, *, ordinal: int, state: Optional[State] = None, controller: Optional[Controller] = None,
                 settings: Optional[Settings] = None) -> None:
        """Hold the groups a read returned, None for the others."""
        super().__init__(ordinal, {'state': state, 'controller': controller, 'settings': settings})


# page 2, Input modes

@dataclass(frozen=True, slots=True, kw_only=True)
class InputModeFlags:
    """Page 2's first group: the InputMode value and its flags."""

    value: int
    lighting: bool
    detected: bool


class InputModeRecord(_Record):
    """A page 2 record."""

    PAGE = 2
    GROUPS = ('mode', 'name')
    __slots__ = ('_mode', '_name')
    mode = _Group[InputModeFlags]()
    name = _Group[str]()

    def __init__(self, *, ordinal: int, mode: Optional[InputModeFlags] = None, name: Optional[str] = None) -> None:
        """Hold the groups a read returned, None for the others."""
        super().__init__(ordinal, {'mode': mode, 'name': name})


# page 3, Names

@dataclass(frozen=True, slots=True, kw_only=True)
class EnumValue:
    """Page 3's first group: which GP2040-CE enum, and the value."""

    enum: int                               # NAMES_GPIO_ACTION or NAMES_LIGHT_TYPE
    value: int


class NameRecord(_Record):
    """A page 3 record: an enum value and its name in GP2040-CE's enums.proto."""

    PAGE = 3
    GROUPS = ('entry', 'name')
    __slots__ = ('_entry', '_name')
    entry = _Group[EnumValue]()
    name = _Group[str]()

    def __init__(self, *, ordinal: int, entry: Optional[EnumValue] = None, name: Optional[str] = None) -> None:
        """Hold the groups a read returned, None for the others."""
        super().__init__(ordinal, {'entry': entry, 'name': name})


# page 8, Summary

@dataclass(frozen=True, slots=True, kw_only=True)
class Summary:
    """Page 8's Summary group: the sizes a host needs."""

    led_extent: int
    led_limit: int
    render_hz: int
    colour_format: int
    white_channel: bool
    brightness_maximum: int
    brightness_steps: int
    animation_speed_steps: int
    button_layout: int
    button_layout_right: int
    button_layout_orientation: int
    grid_width: int
    grid_height: int
    profiles: int
    controls: int
    lit_controls: int
    unlit_controls: int
    lights: int
    animations: int

    def brightness_scale(self, step: int) -> float:
        """Give the scale the board applies at a brightness step: step x (maximum / steps, rounded down) / 255.

        It is the scale for host pixels while CONFIGURE applies the board's brightness, and 0 for a board with
        no brightness steps.
        """
        if not self.brightness_steps:
            return 0.0
        return step * (self.brightness_maximum // self.brightness_steps) / 255

    def stream_rate(self, cap: Optional[float] = None) -> Optional[float]:
        """Give the frame rate a host need not stream faster than: the render rate, held to a cap if given.

        A frame the board does not render before the next arrives is never shown. None when the board states
        no render rate.
        """
        if not self.render_hz:
            return None
        return float(min(self.render_hz, cap)) if cap else float(self.render_hz)


@dataclass(frozen=True, slots=True, kw_only=True)
class Changes:
    """Page 8's Changes group: one change counter for each configuration page, 8 to 15."""

    counters: tuple[int, ...]

    def counter(self, page: int) -> int:
        """Give one configuration page's change counter.

        :raises ValueError: for a page outside 8 to 15
        """
        if not _FIRST_CONFIGURATION_PAGE <= page < _FIRST_CONFIGURATION_PAGE + _CONFIGURATION_PAGES:
            raise ValueError(f"page {page} is not a configuration page, 8 to 15")
        return self.counters[page - _FIRST_CONFIGURATION_PAGE]


class SummaryRecord(_Record):
    """A page 8 record."""

    PAGE = 8
    GROUPS = ('summary', 'changes')
    __slots__ = ('_summary', '_changes')
    summary = _Group[Summary]()
    changes = _Group[Changes]()

    def __init__(self, *, ordinal: int, summary: Optional[Summary] = None, changes: Optional[Changes] = None) -> None:
        """Hold the groups a read returned, None for the others."""
        super().__init__(ordinal, {'summary': summary, 'changes': changes})


# page 9, Profiles

@dataclass(frozen=True, slots=True, kw_only=True)
class Profile:
    """Page 9's one group: an input profile."""

    number: int
    enabled: bool
    label: str


class ProfileRecord(_Record):
    """A page 9 record."""

    PAGE = 9
    GROUPS = ('profile',)
    __slots__ = ('_profile',)
    profile = _Group[Profile]()

    def __init__(self, *, ordinal: int, profile: Optional[Profile] = None) -> None:
        """Hold the groups a read returned, None for the others."""
        super().__init__(ordinal, {'profile': profile})


# page 10, Controls

@dataclass(frozen=True, slots=True, kw_only=True)
class ControlInput:
    """Page 10's first group: the input, its action and its flags."""

    key: int
    source: str                             # 'gpio', 'hall_effect', 'expander', or 'unknown' for a later key
    index: int
    action: int
    duplicated: bool
    lit: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ControlInstances:
    """Page 10's second group: the control's place among the controls with its action, from 1."""

    instance: int
    instances: int                          # 1 when the action is not duplicated


@dataclass(frozen=True, slots=True, kw_only=True)
class Instances:
    """Page 11's owner instances: the owning control's place among the controls with its action.

    Both are None for a light with an owner pin that is none or not a control, which the board sends as 0.
    """

    instance: Optional[int]
    instances: Optional[int]


@dataclass(frozen=True, slots=True, kw_only=True)
class Position:
    """Page 10's Position group, both None for an unlit control."""

    first_led: Optional[int]
    grid_rectangle: Optional[tuple[int, int, int, int]]         # (min x, min y, max x, max y)


@dataclass(frozen=True, slots=True, kw_only=True)
class ControlLights:
    """Page 10's last group: the control's lights."""

    first_light: Optional[int]              # None for an unlit control
    light_count: int


class ControlRecord(_Record):
    """A page 10 record."""

    PAGE = 10
    GROUPS = ('input', 'instances', 'position', 'lights')
    __slots__ = ('_input', '_instances', '_position', '_lights')
    input = _Group[ControlInput]()             # noqa: A003 the specification's word for a control's input
    instances = _Group[ControlInstances]()
    position = _Group[Position]()
    lights = _Group[ControlLights]()

    def __init__(self, *, ordinal: int, input: Optional[ControlInput] = None,       # noqa: A002 the group's name
                 instances: Optional[ControlInstances] = None, position: Optional[Position] = None,
                 lights: Optional[ControlLights] = None) -> None:
        """Hold the groups a read returned, None for the others."""
        super().__init__(ordinal, {'input': input, 'instances': instances, 'position': position, 'lights': lights})


# page 11, Lights

@dataclass(frozen=True, slots=True, kw_only=True)
class LedRange:
    """Page 11's first group: the light's LEDs."""

    first_led: int
    led_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class LightKind:
    """Page 11's second group: the light's LightType and its flag."""

    value: int
    duplicated: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class Owner:
    """Page 11's owner group, each None for none."""

    pin: Optional[int]
    action: Optional[int]


@dataclass(frozen=True, slots=True, kw_only=True)
class GridCell:
    """A light's cell of the LED layout grid."""

    x: int
    y: int


@dataclass(frozen=True, slots=True, kw_only=True)
class PaletteSlot:
    """Page 11's last group: the light's palette slot, None for none."""

    slot: Optional[int]


class LightRecord(_Record):
    """A page 11 record."""

    PAGE = 11
    GROUPS = ('leds', 'kind', 'owner', 'owner_instances', 'grid', 'palette')
    __slots__ = ('_leds', '_kind', '_owner', '_owner_instances', '_grid', '_palette')
    leds = _Group[LedRange]()
    kind = _Group[LightKind]()
    owner = _Group[Owner]()
    owner_instances = _Group[Instances]()
    grid = _Group[GridCell]()
    palette = _Group[PaletteSlot]()

    def __init__(self, *, ordinal: int, leds: Optional[LedRange] = None, kind: Optional[LightKind] = None,
                 owner: Optional[Owner] = None, owner_instances: Optional[Instances] = None,
                 grid: Optional[GridCell] = None, palette: Optional[PaletteSlot] = None) -> None:
        """Hold the groups a read returned, None for the others."""
        super().__init__(ordinal, {'leds': leds, 'kind': kind, 'owner': owner, 'owner_instances': owner_instances,
                                   'grid': grid, 'palette': palette})


# page 12, Animations

@dataclass(frozen=True, slots=True, kw_only=True)
class Effects:
    """Page 12's first group: whether the animation is enabled, and its idle, pressed and case effects."""

    enabled: bool
    idle: int
    pressed: int
    case: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Speeds:
    """Page 12's second group: the idle, pressed and case speeds."""

    idle: int
    pressed: int
    case: int


class AnimationRecord(_Record):
    """A page 12 record."""

    PAGE = 12
    GROUPS = ('effects', 'speeds')
    __slots__ = ('_effects', '_speeds')
    effects = _Group[Effects]()
    speeds = _Group[Speeds]()

    def __init__(self, *, ordinal: int, effects: Optional[Effects] = None, speeds: Optional[Speeds] = None) -> None:
        """Hold the groups a read returned, None for the others."""
        super().__init__(ordinal, {'effects': effects, 'speeds': speeds})


# the other replies

@dataclass(frozen=True, slots=True, kw_only=True)
class Hello:
    """A HELLO reply. The capability bitmaps are frozensets of the members a board lists."""

    magic: bytes
    major: int
    minor: int
    board_id: str
    reply_queue_limit: int
    request_flags: frozenset[int]
    commands: frozenset[int]
    pages: frozenset[int]
    addressing_modes: frozenset[int]
    pixel_formats: frozenset[int]
    events: frozenset[int]
    events_enabled: frozenset[int]


@dataclass(frozen=True, slots=True, kw_only=True)
class Identity:
    """Page 0's strings, in group bit order, each None when not read."""

    board_label: Optional[str] = None
    board_config: Optional[str] = None
    platform: Optional[str] = None
    firmware_version: Optional[str] = None
    version_id: Optional[str] = None
    build_id: Optional[str] = None
    build_type: Optional[str] = None
    firmware_file_name: Optional[str] = None


@dataclass(frozen=True, slots=True, kw_only=True)
class PageHeader:
    """The header of a GET_PAGE reply."""

    status: int
    page: int
    start: int
    total: int
    count: int
    stride: int
    groups: int
    token: int


@dataclass(frozen=True, slots=True, kw_only=True)
class PageRead(Generic[RecordType]):
    """The records a read returned, of the page's record type, and the state token they carry."""

    records: tuple[RecordType, ...]
    token: int


@dataclass(frozen=True, slots=True, kw_only=True)
class StateChanged:
    """A STATE_CHANGED event: the page 1 record it carries and the state token."""

    record: StateRecord
    token: int


@dataclass(frozen=True, slots=True, kw_only=True)
class StageOutcome:
    """A STAGE or UNSTAGE reply: the entries applied and skipped, and the outcome mask."""

    applied: int
    skipped: int
    outcome: int


@dataclass(frozen=True, slots=True, kw_only=True)
class BoardListing:
    """One connected board, as list_boards() finds it without claiming it."""

    board_id: str
    board_label: Optional[str]
    firmware_version: Optional[str]
    major: int
    minor: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Requirements:
    """The capabilities a host uses, named by the HELLO bitmap each belongs to."""

    commands: frozenset[int] = field(default_factory=frozenset)
    request_flags: frozenset[int] = field(default_factory=frozenset)
    pages: frozenset[int] = field(default_factory=frozenset)
    addressing_modes: frozenset[int] = field(default_factory=frozenset)
    pixel_formats: frozenset[int] = field(default_factory=frozenset)
    events: frozenset[int] = field(default_factory=frozenset)

    def union(self, other: 'Requirements') -> 'Requirements':
        """Combine two hosts' or layers' requirements."""
        return Requirements(**{item.name: getattr(self, item.name) | getattr(other, item.name)
                               for item in fields(self)})
