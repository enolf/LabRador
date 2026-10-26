import re
from dataclasses import dataclass

_PATTERN_ITEM = re.compile(r"\[([^\]]+)\]|:?([^:\[\]]+)")
_TOKEN = re.compile(r"([A-Z]+)(\d*)")


@dataclass(frozen=True)
class ScpiCommand:
    text: str
    nodes: tuple[str, ...]
    is_query: bool
    is_common: bool
    arguments: str

    @property
    def header(self) -> str:
        if self.is_common:
            return self.nodes[0] + ("?" if self.is_query else "")
        return ":" + ":".join(self.nodes) + ("?" if self.is_query else "")


@dataclass(frozen=True)
class _Mnemonic:
    long_form: str
    short_form: str
    takes_suffix: bool

    def accepts(self, token: str) -> tuple[bool, int | None]:
        found = _TOKEN.fullmatch(token)
        if found is None:
            return False, None
        name, digits = found.groups()
        if digits and not self.takes_suffix:
            return False, None
        if name not in (self.long_form, self.short_form):
            return False, None
        return True, int(digits) if digits else None


@dataclass(frozen=True)
class _Node:
    alternatives: tuple[_Mnemonic, ...]
    optional: bool

    @property
    def takes_suffix(self) -> bool:
        return any(alternative.takes_suffix for alternative in self.alternatives)

    def accepts(self, token: str) -> tuple[bool, int | None]:
        for alternative in self.alternatives:
            accepted, suffix = alternative.accepts(token)
            if accepted:
                return True, suffix
        return False, None


def _mnemonic(spelling: str) -> _Mnemonic:
    takes_suffix = spelling.endswith("#")
    name = spelling.rstrip("#").lstrip(":")
    short_form = "".join(character for character in name if not character.islower())
    return _Mnemonic(name.upper(), short_form.upper(), takes_suffix)


class ScpiPattern:
    def __init__(self, notation: str) -> None:
        self.notation = notation
        body = notation.strip()
        self.is_query = body.endswith("?")
        body = body.removesuffix("?")
        self.is_common = body.startswith("*")
        if self.is_common:
            self._common_name = body.upper()
            self._nodes: tuple[_Node, ...] = ()
            return
        self._common_name = ""
        nodes = []
        for optional_part, required_part in _PATTERN_ITEM.findall(body):
            part = optional_part or required_part
            alternatives = tuple(_mnemonic(spelling) for spelling in part.split("|"))
            nodes.append(_Node(alternatives, optional=bool(optional_part)))
        if not nodes:
            raise ValueError(f"Empty SCPI pattern: {notation!r}")
        self._nodes = tuple(nodes)

    def __repr__(self) -> str:
        return f"ScpiPattern({self.notation!r})"

    def matches(self, command: ScpiCommand) -> bool:
        return self.suffixes(command) is not None

    def suffixes(self, command: ScpiCommand) -> list[int | None] | None:
        if command.is_query != self.is_query or command.is_common != self.is_common:
            return None
        if self.is_common:
            return [] if command.nodes[0] == self._common_name else None
        return _match_nodes(self._nodes, command.nodes)


def _match_nodes(nodes: tuple[_Node, ...], tokens: tuple[str, ...]) -> list[int | None] | None:
    if not nodes:
        return [] if not tokens else None
    node, remaining_nodes = nodes[0], nodes[1:]
    if tokens:
        accepted, suffix = node.accepts(tokens[0])
        if accepted:
            rest = _match_nodes(remaining_nodes, tokens[1:])
            if rest is not None:
                return [suffix, *rest] if node.takes_suffix else rest
    if node.optional:
        rest = _match_nodes(remaining_nodes, tokens)
        if rest is not None:
            return [None, *rest] if node.takes_suffix else rest
    return None


def _split_outside_quotes(message: str) -> list[str]:
    parts = []
    current = []
    quote = ""
    for character in message:
        if quote:
            if character == quote:
                quote = ""
        elif character in "\"'":
            quote = character
        elif character == ";":
            parts.append("".join(current))
            current = []
            continue
        current.append(character)
    parts.append("".join(current))
    return parts


def parse_message(message: str) -> list[ScpiCommand]:
    commands = []
    path: tuple[str, ...] = ()
    for raw_part in _split_outside_quotes(message):
        part = raw_part.strip()
        if not part:
            raise ValueError(f"Empty command in SCPI message {message!r}")
        header, _, arguments = part.partition(" ")
        header = header.strip()
        arguments = arguments.strip()
        is_query = header.endswith("?")
        header_body = header.removesuffix("?").upper()
        if header_body.startswith("*"):
            commands.append(ScpiCommand(part, (header_body,), is_query, True, arguments))
            continue
        is_absolute = header_body.startswith(":") or not commands
        tokens = tuple(header_body.lstrip(":").split(":"))
        if any(not token for token in tokens):
            raise ValueError(f"Malformed SCPI header {header!r}")
        nodes = tokens if is_absolute else path + tokens
        path = nodes[:-1]
        commands.append(ScpiCommand(part, nodes, is_query, False, arguments))
    return commands


ALWAYS_DENIED_REASON = "front-panel lock commands are never sent"
OBSERVE_REASON = "observe mode never changes instrument settings; switch to control mode to send it"


@dataclass(frozen=True)
class CommandPolicy:
    observe_allowed: tuple[ScpiPattern, ...]
    always_denied: tuple[ScpiPattern, ...] = ()

    @classmethod
    def from_notation(
        cls, observe_allowed: list[str], always_denied: list[str] | None = None
    ) -> "CommandPolicy":
        return cls(
            tuple(ScpiPattern(notation) for notation in observe_allowed),
            tuple(ScpiPattern(notation) for notation in always_denied or []),
        )

    def refusal(self, message: str, *, observe: bool) -> str | None:
        try:
            commands = parse_message(message)
        except ValueError as error:
            return str(error)
        for command in commands:
            if any(pattern.matches(command) for pattern in self.always_denied):
                return ALWAYS_DENIED_REASON
            if observe and not any(pattern.matches(command) for pattern in self.observe_allowed):
                return OBSERVE_REASON
        return None
