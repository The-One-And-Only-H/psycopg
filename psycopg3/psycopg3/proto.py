"""
Protocol objects representing different implementations of the same classes.
"""

# Copyright (C) 2020 The Psycopg Team

from typing import Any, Callable, Dict, Generator, Mapping
from typing import Optional, Sequence, Tuple, Type, TypeVar, Union
from typing import TYPE_CHECKING
from typing_extensions import Protocol

from . import pq
from .pq import Format

if TYPE_CHECKING:
    from .connection import BaseConnection
    from .cursor import BaseCursor
    from .adapt import Dumper, Loader
    from .waiting import Wait, Ready
    from .sql import Composable

Query = Union[str, bytes, "Composable"]
Params = Union[Sequence[Any], Mapping[str, Any]]


# Waiting protocol types

RV = TypeVar("RV")
PQGen = Generator[Tuple[int, "Wait"], "Ready", RV]


# Adaptation types

AdaptContext = Union[None, "BaseConnection", "BaseCursor", "Transformer"]

DumpFunc = Callable[[Any], bytes]
DumperType = Type["Dumper"]
DumpersMap = Dict[Tuple[Union[type, str], Format], DumperType]

LoadFunc = Callable[[bytes], Any]
LoaderType = Type["Loader"]
LoadersMap = Dict[Tuple[int, Format], LoaderType]


class Transformer(Protocol):
    def __init__(self, context: AdaptContext = None):
        ...

    @property
    def connection(self) -> Optional["BaseConnection"]:
        ...

    @property
    def encoding(self) -> str:
        ...

    @property
    def pgresult(self) -> Optional[pq.proto.PGresult]:
        ...

    @pgresult.setter
    def pgresult(self, result: Optional[pq.proto.PGresult]) -> None:
        ...

    @property
    def dumpers(self) -> DumpersMap:
        ...

    @property
    def loaders(self) -> LoadersMap:
        ...

    def set_row_types(
        self,
        types: Sequence[int],
        formats: Sequence[Format],
        fmods: Sequence[int] = (),
    ) -> None:
        ...

    def get_dumper(self, obj: Any, format: Format) -> "Dumper":
        ...

    def load_row(self, row: int) -> Optional[Tuple[Any, ...]]:
        ...

    def load_sequence(
        self, record: Sequence[Optional[bytes]]
    ) -> Tuple[Any, ...]:
        ...

    def get_loader(self, oid: int, format: Format) -> "Loader":
        ...
