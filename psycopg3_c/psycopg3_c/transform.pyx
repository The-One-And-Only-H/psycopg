"""
Helper object to transform values between Python and PostgreSQL

Cython implementation: can access to lower level C features without creating
too many temporary Python objects and performing less memory copying.

"""

# Copyright (C) 2020 The Psycopg Team

from cpython.ref cimport Py_INCREF
from cpython.tuple cimport PyTuple_New, PyTuple_SET_ITEM

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from psycopg3_c cimport libpq
from psycopg3_c.pq_cython cimport PGresult

from psycopg3 import errors as e
from psycopg3.pq.enums import Format

TEXT_OID = 25


cdef class RowLoader:
    cdef object pyloader
    cdef CLoader cloader


cdef class Transformer:
    """
    An object that can adapt efficiently between Python and PostgreSQL.

    The life cycle of the object is the query, so it is assumed that stuff like
    the server version or connection encoding will not change. It can have its
    state so adapting several values of the same type can use optimisations.
    """

    cdef list _dumpers_maps, _loaders_maps
    cdef dict _dumpers, _loaders, _dumpers_cache, _loaders_cache, _load_funcs
    cdef object _connection
    cdef PGresult _pgresult
    cdef int _nfields, _ntuples
    cdef str _encoding
    cdef list _row_loaders
    cdef dict _row_loaders_cache

    def __cinit__(self, context: "AdaptContext" = None):
        self._dumpers_maps: List["DumpersMap"] = []
        self._loaders_maps: List["LoadersMap"] = []
        self._setup_context(context)

        # mapping class, fmt -> Dumper instance
        self._dumpers_cache: Dict[Tuple[type, Format], "Dumper"] = {}

        # mapping oid, fmt, fmod -> Loader instance
        self._loaders_cache: Dict[Tuple[int, Format, int], "Loader"] = {}

        # mapping oid, fmt, fmod -> RowLoader instance
        self._row_loaders_cache: Dict[Tuple[int, Format, int], "RowLoader"] = {}

        # mapping oid, fmt -> load function
        self._load_funcs: Dict[Tuple[int, Format], "LoadFunc"] = {}

        self.pgresult = None
        self._row_loaders = []

    def _setup_context(self, context: "AdaptContext") -> None:
        from psycopg3.adapt import Dumper, Loader
        from psycopg3.cursor import BaseCursor
        from psycopg3.connection import BaseConnection

        cdef Transformer ctx
        if context is None:
            self._connection = None
            self._encoding = "utf-8"
            self._dumpers = {}
            self._loaders = {}
            self._dumpers_maps = [self._dumpers]
            self._loaders_maps = [self._loaders]

        elif isinstance(context, Transformer):
            # A transformer created from a transformers: usually it happens
            # for nested types: share the entire state of the parent
            ctx = context
            self._connection = ctx._connection
            self._encoding = ctx.encoding
            self._dumpers = ctx._dumpers
            self._loaders = ctx._loaders
            self._dumpers_maps.extend(ctx._dumpers_maps)
            self._loaders_maps.extend(ctx._loaders_maps)
            # the global maps are already in the lists
            return

        elif isinstance(context, BaseCursor):
            self._connection = context.connection
            self._encoding = context.connection.pyenc
            self._dumpers = {}
            self._dumpers_maps.extend(
                (self._dumpers, context.dumpers, self.connection.dumpers)
            )
            self._loaders = {}
            self._loaders_maps.extend(
                (self._loaders, context.loaders, self.connection.loaders)
            )

        elif isinstance(context, BaseConnection):
            self._connection = context
            self._encoding = context.pyenc
            self._dumpers = {}
            self._dumpers_maps.extend((self._dumpers, context.dumpers))
            self._loaders = {}
            self._loaders_maps.extend((self._loaders, context.loaders))

        self._dumpers_maps.append(Dumper.globals)
        self._loaders_maps.append(Loader.globals)

    @property
    def connection(self):
        return self._connection

    @property
    def encoding(self):
        return self._encoding

    @property
    def dumpers(self):
        return self._dumpers

    @property
    def loaders(self):
        return self._loaders

    @property
    def pgresult(self) -> Optional[PGresult]:
        return self._pgresult

    @pgresult.setter
    def pgresult(self, result: Optional[PGresult]) -> None:
        self._pgresult = result

        if result is None:
            self._nfields = self._ntuples = 0
            return

        cdef libpq.PGresult *res = self._pgresult.pgresult_ptr
        self._nfields = libpq.PQnfields(res)
        self._ntuples = libpq.PQntuples(res)

        cdef int i
        cdef list types = []
        cdef list formats = []
        cdef list fmods = []
        for i in range(self._nfields):
            types.append(libpq.PQftype(res, i))
            formats.append(libpq.PQfformat(res, i))
            fmods.append(libpq.PQfmod(res, i))

        self.set_row_types(types, formats, fmods)

    def set_row_types(
        self,
        types: Sequence[int],
        formats: Sequence[Format],
        fmods: Sequence[int] = (),
    ) -> None:
        del self._row_loaders[:]

        cdef int i
        for i in range(len(types)):
            self._row_loaders.append(self._get_row_loader(
                types[i], formats[i], fmods[i] if fmods else -1))

    cdef RowLoader _get_row_loader(self, oid: int, format: Format, fmod: int):
        key = (oid, format, fmod)
        if key in self._row_loaders_cache:
            return self._row_loaders_cache[key]

        cdef RowLoader row_loader = RowLoader()
        loader = self.get_loader(oid, format, fmod)
        row_loader.pyloader = loader.load

        if isinstance(loader, CLoader):
            row_loader.cloader = loader
        else:
            row_loader.cloader = None

        self._row_loaders_cache[key] = row_loader
        return row_loader

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
        if self._pgresult is None:
            return None

        cdef int crow = row
        if crow >= self._ntuples:
            return None

        cdef libpq.PGresult *res = self._pgresult.pgresult_ptr

        cdef RowLoader loader
        cdef int col
        cdef int length
        cdef const char *val
        rv = PyTuple_New(self._nfields)
        for col in range(self._nfields):
            length = libpq.PQgetlength(res, crow, col)
            if length == 0:
                if libpq.PQgetisnull(res, crow, col):
                    Py_INCREF(None)
                    PyTuple_SET_ITEM(rv, col, None)
                    continue

            val = libpq.PQgetvalue(res, crow, col)
            loader = self._row_loaders[col]
            if loader.cloader is not None:
                pyval = loader.cloader.cload(val, length)
            else:
                # TODO: no copy
                pyval = loader.pyloader(val[:length])

            Py_INCREF(pyval)
            PyTuple_SET_ITEM(rv, col, pyval)

        return rv

    def load_sequence(
        self, record: Sequence[Optional[bytes]]
    ) -> Tuple[Any, ...]:
        cdef list rv = []
        cdef int i
        cdef RowLoader loader
        for i in range(len(record)):
            item = record[i]
            if item is None:
                rv.append(None)
            else:
                loader = self._row_loaders[i]
                rv.append(loader.pyloader(item))

        return tuple(rv)

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
            from psycopg3.adapt import Loader
            loader_cls = Loader.globals[0, format]    # INVALID_OID

        self._loaders_cache[key] = loader = loader_cls(oid, fmod, self)
        return loader
