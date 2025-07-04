# SPDX-License-Identifier: MIT

from _vendoring.attr import (
    NOTHING,
    Attribute,
    Factory,
    __author__,
    __copyright__,
    __description__,
    __doc__,
    __email__,
    __license__,
    __title__,
    __url__,
    __version__,
    __version_info__,
    assoc,
    cmp_using,
    define,
    evolve,
    field,
    fields,
    fields_dict,
    frozen,
    has,
    make_class,
    mutable,
    resolve_types,
    validate,
)
from _vendoring.attr._next_gen import asdict, astuple

from . import converters, exceptions, filters, setters, validators


__all__ = [
    "__author__",
    "__copyright__",
    "__description__",
    "__doc__",
    "__email__",
    "__license__",
    "__title__",
    "__url__",
    "__version__",
    "__version_info__",
    "asdict",
    "assoc",
    "astuple",
    "Attribute",
    "cmp_using",
    "converters",
    "define",
    "evolve",
    "exceptions",
    "Factory",
    "field",
    "fields_dict",
    "fields",
    "filters",
    "frozen",
    "has",
    "make_class",
    "mutable",
    "NOTHING",
    "resolve_types",
    "setters",
    "validate",
    "validators",
]
