"""
Helper object to transform values between Python and PostgreSQL
"""

# Copyright (C) 2020 The Psycopg Team

from typing import Any, Dict, List, Optional, Sequence, Tuple
from typing import TYPE_CHECKING

from . import errors as e
from . import pq
from .oids import builtins, INVALID_OID
from .proto import AdaptContext, DumpersMap
from .proto import LoadFunc, LoadersMap
from .cursor import BaseCursor
from .connection import BaseConnection

if TYPE_CHECKING:
    from .adapt import Dumper, Loader

Format = pq.Format
TEXT_OID = builtins["text"].oid


class Transformer:
    """
    An object that can adapt efficiently between Python and PostgreSQL.

    The life cycle of the object is the query, so it is assumed that stuff like
    the server version or connection encoding will not change. It can have its
    state so adapting several values of the same type can use optimisations.
    """

    def __init__(self, context: AdaptContext = None):
        self._dumpers: DumpersMap
        self._loaders: LoadersMap
        self._dumpers_maps: List[DumpersMap] = []
        self._loaders_maps: List[LoadersMap] = []
        self._setup_context(context)
        self.pgresult = None

        # mapping class, fmt -> Dumper instance
        self._dumpers_cache: Dict[Tuple[type, Format], "Dumper"] = {}

        # mapping oid, format, fmod -> Loader instance
        self._loaders_cache: Dict[Tuple[int, Format, int], "Loader"] = {}

        # mapping oid, fmt -> load function
        self._load_funcs: Dict[Tuple[int, Format], LoadFunc] = {}

        # sequence of load functions from value to python
        # the length of the result columns
        self._row_loaders: List[LoadFunc] = []

    def _setup_context(self, context: AdaptContext) -> None:
        if not context:
            self._connection = None
            self._encoding = "utf-8"
            self._dumpers = {}
            self._loaders = {}
            self._dumpers_maps = [self._dumpers]
            self._loaders_maps = [self._loaders]

        elif isinstance(context, Transformer):
            # A transformer created from a transformers: usually it happens
            # for nested types: share the entire state of the parent
            self._connection = context.connection
            self._encoding = context.encoding
            self._dumpers = context.dumpers
            self._loaders = context.loaders
            self._dumpers_maps.extend(context._dumpers_maps)
            self._loaders_maps.extend(context._loaders_maps)
            # the global maps are already in the lists
            return

        elif isinstance(context, BaseCursor):
            self._connection = context.connection
            self._encoding = context.connection.pyenc
            self._dumpers = {}
            self._dumpers_maps.extend(
                (self._dumpers, context.dumpers, context.connection.dumpers)
            )
            self._loaders = {}
            self._loaders_maps.extend(
                (self._loaders, context.loaders, context.connection.loaders)
            )

        elif isinstance(context, BaseConnection):
            self._connection = context
            self._encoding = context.pyenc
            self._dumpers = {}
            self._dumpers_maps.extend((self._dumpers, context.dumpers))
            self._loaders = {}
            self._loaders_maps.extend((self._loaders, context.loaders))

        from .adapt import Dumper, Loader

        self._dumpers_maps.append(Dumper.globals)
        self._loaders_maps.append(Loader.globals)

    @property
    def connection(self) -> Optional["BaseConnection"]:
        return self._connection

    @property
    def encoding(self) -> str:
        return self._encoding

    @property
    def pgresult(self) -> Optional[pq.proto.PGresult]:
        return self._pgresult

    @pgresult.setter
    def pgresult(self, result: Optional[pq.proto.PGresult]) -> None:
        self._pgresult = result

        self._ntuples: int
        self._nfields: int
        if not result:
            self._nfields = self._ntuples = 0
            return

        nf = self._nfields = result.nfields
        self._ntuples = result.ntuples

        types = []
        formats = []
        fmods = []
        for i in range(nf):
            types.append(result.ftype(i))
            formats.append(result.fformat(i))
            fmods.append(result.fmod(i))

        self.set_row_types(types, formats, fmods)

    @property
    def dumpers(self) -> DumpersMap:
        return self._dumpers

    @property
    def loaders(self) -> LoadersMap:
        return self._loaders

    def set_row_types(
        self,
        types: Sequence[int],
        formats: Sequence[Format],
        fmods: Sequence[int] = (),
    ) -> None:
        self._row_loaders = [
            self.get_loader(
                types[i], formats[i], fmods[i] if fmods else -1
            ).load
            for i in range(len(types))
        ]

    def get_dumper(self, obj: Any, format: Format) -> "Dumper":
        # Fast path: return a Dumper class already instantiated from the same type
        cls = type(obj)
        try:
            return self._dumpers_cache[cls, format]
        except KeyError:
            pass

        # We haven't seen this type in this query yet. Look for an adapter
        # in contexts from the most specific to the most generic.
        # Also look for superclasses: if you can adapt a type you should be
        # able to adapt its subtypes, otherwise Liskov is sad.
        for dmap in self._dumpers_maps:
            for scls in cls.__mro__:
                key = (scls, format)
                dumper_class = dmap.get(key)
                if not dumper_class:
                    continue

                self._dumpers_cache[key] = dumper = dumper_class(scls, self)
                return dumper

        # If the adapter is not found, look for its name as a string
        for dmap in self._dumpers_maps:
            for scls in cls.__mro__:
                fqn = f"{cls.__module__}.{scls.__qualname__}"
                dumper_class = dmap.get((fqn, format))
                if dumper_class is None:
                    continue

                key = (scls, format)
                dmap[key] = dumper_class
                self._dumpers_cache[key] = dumper = dumper_class(scls, self)
                return dumper

        raise e.ProgrammingError(
            f"cannot adapt type {type(obj).__name__}"
            f" to format {Format(format).name}"
        )

    def load_row(self, row: int) -> Optional[Tuple[Any, ...]]:
        res = self.pgresult
        if not res:
            return None

        if row >= self._ntuples:
            return None

        rv: List[Any] = []
        for col in range(self._nfields):
            val = res.get_value(row, col)
            if val is not None:
                val = self._row_loaders[col](val)
            rv.append(val)

        return tuple(rv)

    def load_sequence(
        self, record: Sequence[Optional[bytes]]
    ) -> Tuple[Any, ...]:
        return tuple(
            (self._row_loaders[i](val) if val is not None else None)
            for i, val in enumerate(record)
        )

    def get_loader(self, oid: int, format: Format, fmod: int = -1) -> "Loader":
        key = (oid, format, fmod)
        try:
            return self._loaders_cache[key]
        except KeyError:
            pass

        ckey = (oid, format)
        for tcmap in self._loaders_maps:
            if ckey in tcmap:
                loader_cls = tcmap[ckey]
                break
        else:
            from .adapt import Loader  # noqa

            loader_cls = Loader.globals[INVALID_OID, format]

        self._loaders_cache[key] = loader = loader_cls(oid, fmod, self)
        return loader
