# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""
Spack allows very fine-grained control over how packages are installed and
over how they are built and configured.  To make this easy, it has its own
syntax for declaring a dependence.  We call a descriptor of a particular
package configuration a "spec".

The syntax looks like this:

.. code-block:: sh

    $ spack install mpileaks ^openmpi @1.2:1.4 +debug %intel @12.1 target=zen
                    0        1        2        3      4      5     6

The first part of this is the command, 'spack install'.  The rest of the
line is a spec for a particular installation of the mpileaks package.

0. The package to install

1. A dependency of the package, prefixed by ^

2. A version descriptor for the package.  This can either be a specific
   version, like "1.2", or it can be a range of versions, e.g. "1.2:1.4".
   If multiple specific versions or multiple ranges are acceptable, they
   can be separated by commas, e.g. if a package will only build with
   versions 1.0, 1.2-1.4, and 1.6-1.8 of mvapich, you could say:

       depends_on("mvapich@1.0,1.2:1.4,1.6:1.8")

3. A compile-time variant of the package.  If you need openmpi to be
   built in debug mode for your package to work, you can require it by
   adding +debug to the openmpi spec when you depend on it.  If you do
   NOT want the debug option to be enabled, then replace this with -debug.
   If you would like for the variant to be propagated through all your
   package's dependencies use "++" for enabling and "--" or "~~" for disabling.

4. The name of the compiler to build with.

5. The versions of the compiler to build with.  Note that the identifier
   for a compiler version is the same '@' that is used for a package version.
   A version list denoted by '@' is associated with the compiler only if
   if it comes immediately after the compiler name.  Otherwise it will be
   associated with the current package spec.

6. The architecture to build with.  This is needed on machines where
   cross-compilation is required
"""
import collections
import collections.abc
import enum
import io
import itertools
import json
import os
import pathlib
import platform
import re
import socket
import warnings
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Match,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    overload,
)

import _vendoring.archspec.cpu
from _vendoring.typing_extensions import Literal

import llnl.path
import llnl.string
import llnl.util.filesystem as fs
import llnl.util.lang as lang
import llnl.util.tty as tty
import llnl.util.tty.color as clr

import spack
import spack.aliases
import spack.compilers.flags
import spack.deptypes as dt
import spack.error
import spack.hash_types as ht
import spack.paths
import spack.platforms
import spack.provider_index
import spack.repo
import spack.spec_parser
import spack.store
import spack.traverse
import spack.util.hash
import spack.util.prefix
import spack.util.spack_json as sjson
import spack.util.spack_yaml as syaml
import spack.variant as vt
import spack.version as vn
import spack.version.git_ref_lookup

from .enums import InstallRecordStatus

__all__ = [
    "CompilerSpec",
    "Spec",
    "UnsupportedPropagationError",
    "DuplicateDependencyError",
    "UnsupportedCompilerError",
    "DuplicateArchitectureError",
    "InvalidDependencyError",
    "UnsatisfiableSpecNameError",
    "UnsatisfiableVersionSpecError",
    "UnsatisfiableArchitectureSpecError",
    "UnsatisfiableDependencySpecError",
    "AmbiguousHashError",
    "InvalidHashError",
    "SpecDeprecatedError",
]


SPEC_FORMAT_RE = re.compile(
    r"(?:"  # this is one big or, with matches ordered by priority
    # OPTION 1: escaped character (needs to be first to catch opening \{)
    # Note that an unterminated \ at the end of a string is left untouched
    r"(?:\\(.))"
    r"|"  # or
    # OPTION 2: an actual format string
    r"{"  # non-escaped open brace {
    r"( ?[%@/]|[\w ][\w -]*=)?"  # optional sigil (or identifier or space) to print sigil in color
    r"(?:\^([^}\.]+)\.)?"  # optional ^depname. (to get attr from dependency)
    # after the sigil or depname, we can have a hash expression or another attribute
    r"(?:"  # one of
    r"(hash\b)(?:\:(\d+))?"  # hash followed by :<optional length>
    r"|"  # or
    r"([^}]*)"  # another attribute to format
    r")"  # end one of
    r"(})?"  # finish format string with non-escaped close brace }, or missing if not present
    r"|"
    # OPTION 3: mismatched close brace (option 2 would consume a matched open brace)
    r"(})"  # brace
    r")",
    re.IGNORECASE,
)

#: Valid pattern for an identifier in Spack

IDENTIFIER_RE = r"\w[\w-]*"

# Coloring of specs when using color output. Fields are printed with
# different colors to enhance readability.
# See llnl.util.tty.color for descriptions of the color codes.
COMPILER_COLOR = "@g"  #: color for highlighting compilers
VERSION_COLOR = "@c"  #: color for highlighting versions
ARCHITECTURE_COLOR = "@m"  #: color for highlighting architectures
VARIANT_COLOR = "@B"  #: color for highlighting variants
HASH_COLOR = "@K"  #: color for highlighting package hashes

#: Default format for Spec.format(). This format can be round-tripped, so that:
#:     Spec(Spec("string").format()) == Spec("string)"
DEFAULT_FORMAT = (
    "{name}{@versions}{compiler_flags}"
    "{variants}{ namespace=namespace_if_anonymous}{ arch=architecture}{/abstract_hash}"
)

#: Display format, which eliminates extra `@=` in the output, for readability.
DISPLAY_FORMAT = (
    "{name}{@version}{compiler_flags}"
    "{variants}{ namespace=namespace_if_anonymous}{ arch=architecture}{/abstract_hash}"
    "{compilers}"
)

#: Regular expression to pull spec contents out of clearsigned signature
#: file.
CLEARSIGN_FILE_REGEX = re.compile(
    (
        r"^-----BEGIN PGP SIGNED MESSAGE-----"
        r"\s+Hash:\s+[^\s]+\s+(.+)-----BEGIN PGP SIGNATURE-----"
    ),
    re.MULTILINE | re.DOTALL,
)

#: specfile format version. Must increase monotonically
SPECFILE_FORMAT_VERSION = 5


class InstallStatus(enum.Enum):
    """Maps install statuses to symbols for display.

    Options are artificially disjoint for display purposes
    """

    installed = "@g{[+]}  "
    upstream = "@g{[^]}  "
    external = "@M{[e]}  "
    absent = "@K{ - }  "
    missing = "@r{[-]}  "


# regexes used in spec formatting
OLD_STYLE_FMT_RE = re.compile(r"\${[A-Z]+}")


def ensure_modern_format_string(fmt: str) -> None:
    """Ensure that the format string does not contain old ${...} syntax."""
    result = OLD_STYLE_FMT_RE.search(fmt)
    if result:
        raise SpecFormatStringError(
            f"Format string `{fmt}` contains old syntax `{result.group(0)}`. "
            "This is no longer supported."
        )


def _make_microarchitecture(name: str) -> _vendoring.archspec.cpu.Microarchitecture:
    if isinstance(name, _vendoring.archspec.cpu.Microarchitecture):
        return name
    return _vendoring.archspec.cpu.TARGETS.get(
        name, _vendoring.archspec.cpu.generic_microarchitecture(name)
    )


@lang.lazy_lexicographic_ordering
class ArchSpec:
    """Aggregate the target platform, the operating system and the target microarchitecture."""

    @staticmethod
    def default_arch():
        """Return the default architecture"""
        platform = spack.platforms.host()
        default_os = platform.default_operating_system()
        default_target = platform.default_target()
        arch_tuple = str(platform), str(default_os), str(default_target)
        return ArchSpec(arch_tuple)

    __slots__ = "_platform", "_os", "_target"

    def __init__(self, spec_or_platform_tuple=(None, None, None)):
        """Architecture specification a package should be built with.

        Each ArchSpec is comprised of three elements: a platform (e.g. Linux),
        an OS (e.g. RHEL6), and a target (e.g. x86_64).

        Args:
            spec_or_platform_tuple (ArchSpec or str or tuple): if an ArchSpec
                is passed it will be duplicated into the new instance.
                Otherwise information on platform, OS and target should be
                passed in either as a spec string or as a tuple.
        """

        # If the argument to __init__ is a spec string, parse it
        # and construct an ArchSpec
        def _string_or_none(s):
            if s and s != "None":
                return str(s)
            return None

        # If another instance of ArchSpec was passed, duplicate it
        if isinstance(spec_or_platform_tuple, ArchSpec):
            other = spec_or_platform_tuple
            platform_tuple = other.platform, other.os, other.target

        elif isinstance(spec_or_platform_tuple, (str, tuple)):
            spec_fields = spec_or_platform_tuple

            # Normalize the string to a tuple
            if isinstance(spec_or_platform_tuple, str):
                spec_fields = spec_or_platform_tuple.split("-")
                if len(spec_fields) != 3:
                    msg = "cannot construct an ArchSpec from {0!s}"
                    raise ValueError(msg.format(spec_or_platform_tuple))

            platform, operating_system, target = spec_fields
            platform_tuple = (_string_or_none(platform), _string_or_none(operating_system), target)

        self.platform, self.os, self.target = platform_tuple

    @staticmethod
    def override(init_spec, change_spec):
        if init_spec:
            new_spec = init_spec.copy()
        else:
            new_spec = ArchSpec()
        if change_spec.platform:
            new_spec.platform = change_spec.platform
            # TODO: if the platform is changed to something that is incompatible
            # with the current os, we should implicitly remove it
        if change_spec.os:
            new_spec.os = change_spec.os
        if change_spec.target:
            new_spec.target = change_spec.target
        return new_spec

    def _autospec(self, spec_like):
        if isinstance(spec_like, ArchSpec):
            return spec_like
        return ArchSpec(spec_like)

    def _cmp_iter(self):
        yield self.platform
        yield self.os
        if self.target is None:
            yield self.target
        else:
            yield self.target.name

    @property
    def platform(self):
        """The platform of the architecture."""
        return self._platform

    @platform.setter
    def platform(self, value):
        # The platform of the architecture spec will be verified as a
        # supported Spack platform before it's set to ensure all specs
        # refer to valid platforms.
        value = str(value) if value is not None else None
        self._platform = value

    @property
    def os(self):
        """The OS of this ArchSpec."""
        return self._os

    @os.setter
    def os(self, value):
        # The OS of the architecture spec will update the platform field
        # if the OS is set to one of the reserved OS types so that the
        # default OS type can be resolved.  Since the reserved OS
        # information is only available for the host machine, the platform
        # will assumed to be the host machine's platform.
        value = str(value) if value is not None else None

        if value in spack.platforms.Platform.reserved_oss:
            curr_platform = str(spack.platforms.host())
            self.platform = self.platform or curr_platform

            if self.platform != curr_platform:
                raise ValueError(
                    "Can't set arch spec OS to reserved value '%s' when the "
                    "arch platform (%s) isn't the current platform (%s)"
                    % (value, self.platform, curr_platform)
                )

            spec_platform = spack.platforms.by_name(self.platform)
            value = str(spec_platform.operating_system(value))

        self._os = value

    @property
    def target(self):
        """The target of the architecture."""
        return self._target

    @target.setter
    def target(self, value):
        # The target of the architecture spec will update the platform field
        # if the target is set to one of the reserved target types so that
        # the default target type can be resolved.  Since the reserved target
        # information is only available for the host machine, the platform
        # will assumed to be the host machine's platform.

        def target_or_none(t):
            if isinstance(t, _vendoring.archspec.cpu.Microarchitecture):
                return t
            if t and t != "None":
                return _make_microarchitecture(t)
            return None

        value = target_or_none(value)

        if str(value) in spack.platforms.Platform.reserved_targets:
            curr_platform = str(spack.platforms.host())
            self.platform = self.platform or curr_platform

            if self.platform != curr_platform:
                raise ValueError(
                    "Can't set arch spec target to reserved value '%s' when "
                    "the arch platform (%s) isn't the current platform (%s)"
                    % (value, self.platform, curr_platform)
                )

            spec_platform = spack.platforms.by_name(self.platform)
            value = spec_platform.target(value)

        self._target = value

    def satisfies(self, other: "ArchSpec") -> bool:
        """Return True if all concrete specs matching self also match other, otherwise False.

        Args:
            other: spec to be satisfied
        """
        other = self._autospec(other)

        # Check platform and os
        for attribute in ("platform", "os"):
            other_attribute = getattr(other, attribute)
            self_attribute = getattr(self, attribute)
            if other_attribute and self_attribute != other_attribute:
                return False

        return self._target_satisfies(other, strict=True)

    def intersects(self, other: "ArchSpec") -> bool:
        """Return True if there exists at least one concrete spec that matches both
        self and other, otherwise False.

        This operation is commutative, and if two specs intersect it means that one
        can constrain the other.

        Args:
            other: spec to be checked for compatibility
        """
        other = self._autospec(other)

        # Check platform and os
        for attribute in ("platform", "os"):
            other_attribute = getattr(other, attribute)
            self_attribute = getattr(self, attribute)
            if other_attribute and self_attribute and self_attribute != other_attribute:
                return False

        return self._target_satisfies(other, strict=False)

    def _target_satisfies(self, other: "ArchSpec", strict: bool) -> bool:
        if strict is True:
            need_to_check = bool(other.target)
        else:
            need_to_check = bool(other.target and self.target)

        if not need_to_check:
            return True

        # other_target is there and strict=True
        if self.target is None:
            return False

        return bool(self._target_intersection(other))

    def _target_constrain(self, other: "ArchSpec") -> bool:
        if self.target is None and other.target is None:
            return False

        if not other._target_satisfies(self, strict=False):
            raise UnsatisfiableArchitectureSpecError(self, other)

        if self.target_concrete:
            return False

        elif other.target_concrete:
            self.target = other.target
            return True

        # Compute the intersection of every combination of ranges in the lists
        results = self._target_intersection(other)
        attribute_str = ",".join(results)

        intersection_target = _make_microarchitecture(attribute_str)
        if self.target == intersection_target:
            return False

        self.target = intersection_target
        return True

    def _target_intersection(self, other):
        results = []

        if not self.target or not other.target:
            return results

        for s_target_range in str(self.target).split(","):
            s_min, s_sep, s_max = s_target_range.partition(":")
            for o_target_range in str(other.target).split(","):
                o_min, o_sep, o_max = o_target_range.partition(":")

                if not s_sep:
                    # s_target_range is a concrete target
                    # get a microarchitecture reference for at least one side
                    # of each comparison so we can use archspec comparators
                    s_comp = _make_microarchitecture(s_min)
                    if not o_sep:
                        if s_min == o_min:
                            results.append(s_min)
                    elif (not o_min or s_comp >= o_min) and (not o_max or s_comp <= o_max):
                        results.append(s_min)
                elif not o_sep:
                    # "cast" to microarchitecture
                    o_comp = _make_microarchitecture(o_min)
                    if (not s_min or o_comp >= s_min) and (not s_max or o_comp <= s_max):
                        results.append(o_min)
                else:
                    # Take the "min" of the two max, if there is a partial ordering.
                    n_max = ""
                    if s_max and o_max:
                        _s_max = _make_microarchitecture(s_max)
                        _o_max = _make_microarchitecture(o_max)
                        if _s_max.family != _o_max.family:
                            continue
                        if _s_max <= _o_max:
                            n_max = s_max
                        elif _o_max < _s_max:
                            n_max = o_max
                        else:
                            continue
                    elif s_max:
                        n_max = s_max
                    elif o_max:
                        n_max = o_max

                    # Take the "max" of the two min.
                    n_min = ""
                    if s_min and o_min:
                        _s_min = _make_microarchitecture(s_min)
                        _o_min = _make_microarchitecture(o_min)
                        if _s_min.family != _o_min.family:
                            continue
                        if _s_min >= _o_min:
                            n_min = s_min
                        elif _o_min > _s_min:
                            n_min = o_min
                        else:
                            continue
                    elif s_min:
                        n_min = s_min
                    elif o_min:
                        n_min = o_min

                    if n_min and n_max:
                        _n_min = _make_microarchitecture(n_min)
                        _n_max = _make_microarchitecture(n_max)
                        if _n_min.family != _n_max.family or not _n_min <= _n_max:
                            continue
                        if n_min == n_max:
                            results.append(n_min)
                        else:
                            results.append(f"{n_min}:{n_max}")
                    elif n_min:
                        results.append(f"{n_min}:")
                    elif n_max:
                        results.append(f":{n_max}")

        return results

    def constrain(self, other: "ArchSpec") -> bool:
        """Projects all architecture fields that are specified in the given
        spec onto the instance spec if they're missing from the instance
        spec.

        This will only work if the two specs are compatible.

        Args:
            other (ArchSpec or str): constraints to be added

        Returns:
            True if the current instance was constrained, False otherwise.
        """
        other = self._autospec(other)

        if not other.intersects(self):
            raise UnsatisfiableArchitectureSpecError(other, self)

        constrained = False
        for attr in ("platform", "os"):
            svalue, ovalue = getattr(self, attr), getattr(other, attr)
            if svalue is None and ovalue is not None:
                setattr(self, attr, ovalue)
                constrained = True

        constrained |= self._target_constrain(other)

        return constrained

    def copy(self):
        """Copy the current instance and returns the clone."""
        return ArchSpec(self)

    @property
    def concrete(self):
        """True if the spec is concrete, False otherwise"""
        return self.platform and self.os and self.target and self.target_concrete

    @property
    def target_concrete(self):
        """True if the target is not a range or list."""
        return (
            self.target is not None and ":" not in str(self.target) and "," not in str(self.target)
        )

    def to_dict(self):
        # Generic targets represent either an architecture family (like x86_64)
        # or a custom micro-architecture
        if self.target.vendor == "generic":
            target_data = str(self.target)
        else:
            # Get rid of compiler flag information before turning the uarch into a dict
            target_data = self.target.to_dict()
            target_data.pop("compilers", None)
        return {"arch": {"platform": self.platform, "platform_os": self.os, "target": target_data}}

    @staticmethod
    def from_dict(d):
        """Import an ArchSpec from raw YAML/JSON data"""
        arch = d["arch"]
        target_name = arch["target"]
        if not isinstance(target_name, str):
            target_name = target_name["name"]
        target = _make_microarchitecture(target_name)
        return ArchSpec((arch["platform"], arch["platform_os"], target))

    def __str__(self):
        return "%s-%s-%s" % (self.platform, self.os, self.target)

    def __repr__(self):
        fmt = "ArchSpec(({0.platform!r}, {0.os!r}, {1!r}))"
        return fmt.format(self, str(self.target))

    def __contains__(self, string):
        return string in str(self) or string in self.target

    def complete_with_defaults(self) -> None:
        default_architecture = ArchSpec.default_arch()
        if not self.platform:
            self.platform = default_architecture.platform

        if not self.os:
            self.os = default_architecture.os

        if not self.target:
            self.target = default_architecture.target


class CompilerSpec:
    """Adaptor to the old compiler spec interface. Exposes just a few attributes"""

    def __init__(self, spec):
        self.spec = spec

    @property
    def name(self):
        return self.spec.name

    @property
    def version(self):
        return self.spec.version

    @property
    def versions(self):
        return self.spec.versions

    @property
    def display_str(self):
        """Equivalent to {compiler.name}{@compiler.version} for Specs, without extra
        @= for readability."""
        if self.versions != vn.any_version:
            return self.spec.format("{name}{@version}")
        return self.spec.format("{name}")

    def __lt__(self, other):
        if not isinstance(other, CompilerSpec):
            return self.spec < other
        return self.spec < other.spec

    def __eq__(self, other):
        if not isinstance(other, CompilerSpec):
            return self.spec == other
        return self.spec == other.spec

    def __hash__(self):
        return hash(self.spec)

    def __str__(self):
        return str(self.spec)

    def _cmp_iter(self):
        return self.spec._cmp_iter()

    def __bool__(self):
        if self.spec == Spec():
            return False
        return bool(self.spec)


class DeprecatedCompilerSpec(lang.DeprecatedProperty):
    def __init__(self):
        super().__init__(name="compiler")

    def factory(self, instance, owner):
        if instance.original_spec_format() < 5:
            compiler = instance.annotations.compiler_node_attribute
            assert compiler is not None, "a compiler spec is expected"
            return CompilerSpec(compiler)

        for language in ("c", "cxx", "fortran"):
            deps = instance.dependencies(virtuals=language)
            if deps:
                return CompilerSpec(deps[0])

        raise AttributeError(f"{instance} has no C, C++, or Fortran compiler")


@lang.lazy_lexicographic_ordering
class DependencySpec:
    """DependencySpecs represent an edge in the DAG, and contain dependency types
    and information on the virtuals being provided.

    Dependencies can be one (or more) of several types:

    - build: needs to be in the PATH at build time.
    - link: is linked to and added to compiler flags.
    - run: needs to be in the PATH for the package to run.

    Args:
        parent: starting node of the edge
        spec: ending node of the edge.
        depflag: represents dependency relationships.
        virtuals: virtual packages provided from child to parent node.
    """

    __slots__ = "parent", "spec", "depflag", "virtuals", "direct", "when"

    def __init__(
        self,
        parent: "Spec",
        spec: "Spec",
        *,
        depflag: dt.DepFlag,
        virtuals: Tuple[str, ...],
        direct: bool = False,
        when: Optional["Spec"] = None,
    ):
        self.parent = parent
        self.spec = spec
        self.depflag = depflag
        self.virtuals = tuple(sorted(set(virtuals)))
        self.direct = direct
        self.when = when or Spec()

    def update_deptypes(self, depflag: dt.DepFlag) -> bool:
        """Update the current dependency types"""
        old = self.depflag
        new = depflag | old
        if new == old:
            return False
        self.depflag = new
        return True

    def update_virtuals(self, virtuals: Union[str, Iterable[str]]) -> bool:
        """Update the list of provided virtuals"""
        old = self.virtuals
        if isinstance(virtuals, str):
            union = {virtuals, *self.virtuals}
        else:
            union = {*virtuals, *self.virtuals}
        if len(union) == len(old):
            return False
        self.virtuals = tuple(sorted(union))
        return True

    def copy(self) -> "DependencySpec":
        """Return a copy of this edge"""
        return DependencySpec(
            self.parent,
            self.spec,
            depflag=self.depflag,
            virtuals=self.virtuals,
            direct=self.direct,
            when=self.when,
        )

    def _cmp_iter(self):
        yield self.parent.name if self.parent else None
        yield self.spec.name if self.spec else None
        yield self.depflag
        yield self.virtuals
        yield self.direct
        yield self.when

    def __str__(self) -> str:
        parent = self.parent.name if self.parent else None
        child = self.spec.name if self.spec else None
        virtuals_string = f"virtuals={','.join(self.virtuals)}" if self.virtuals else ""
        when_string = f"when='{self.when}'" if self.when != Spec() else ""
        edge_attrs = filter(lambda x: bool(x), (virtuals_string, when_string))
        return f"{parent} {self.depflag}[{' '.join(edge_attrs)}] --> {child}"

    def format(self, *, unconditional: bool = False) -> str:
        """Returns a string, using the spec syntax, representing this edge

        Args:
            unconditional: if True, removes any condition statement from the representation
        """

        parent = self.parent.name if self.parent.name else ""
        child = self.spec if self.spec else ""
        virtuals_str = f"virtuals={','.join(self.virtuals)}" if self.virtuals else ""

        when_str = ""
        if not unconditional and self.when != Spec():
            when_str = f"when='{self.when}'"

        dep_sigil = "%" if self.direct else "^"
        edge_attrs = filter(lambda x: bool(x), (virtuals_str, when_str))

        if edge_attrs:
            return f"{parent} {dep_sigil}[{' '.join(edge_attrs)}] {child}"
        return f"{parent} {dep_sigil}{child}"

    def flip(self) -> "DependencySpec":
        """Flip the dependency, and drop virtual and conditional information"""
        return DependencySpec(
            parent=self.spec, spec=self.parent, depflag=self.depflag, virtuals=()
        )


class CompilerFlag(str):
    """Will store a flag value and it's propagation value

    Args:
        value (str): the flag's value
        propagate (bool): if ``True`` the flag value will
            be passed to the package's dependencies. If
            ``False`` it will not
        flag_group (str): if this flag was introduced along
            with several flags via a single source, then
            this will store all such flags
        source (str): identifies the type of constraint that
            introduced this flag (e.g. if a package has
            ``depends_on(... cflags=-g)``, then the ``source``
            for "-g" would indicate ``depends_on``.
    """

    def __new__(cls, value, **kwargs):
        obj = str.__new__(cls, value)
        obj.propagate = kwargs.pop("propagate", False)
        obj.flag_group = kwargs.pop("flag_group", value)
        obj.source = kwargs.pop("source", None)
        return obj


_valid_compiler_flags = ["cflags", "cxxflags", "fflags", "ldflags", "ldlibs", "cppflags"]


def _shared_subset_pair_iterate(container1, container2):
    """
    [0, a, c, d, f]
    [a, d, e, f]

    yields [(a, a), (d, d), (f, f)]

    no repeated elements
    """
    a_idx, b_idx = 0, 0
    max_a, max_b = len(container1), len(container2)
    while a_idx < max_a and b_idx < max_b:
        if container1[a_idx] == container2[b_idx]:
            yield (container1[a_idx], container2[b_idx])
            a_idx += 1
            b_idx += 1
        else:
            while container1[a_idx] < container2[b_idx]:
                a_idx += 1
            while container1[a_idx] > container2[b_idx]:
                b_idx += 1


class FlagMap(lang.HashableMap[str, List[CompilerFlag]]):
    __slots__ = ("spec",)

    def __init__(self, spec):
        super().__init__()
        self.spec = spec

    def satisfies(self, other):
        return all(f in self and set(self[f]) >= set(other[f]) for f in other)

    def intersects(self, other):
        return True

    def constrain(self, other):
        """Add all flags in other that aren't in self to self.

        Return whether the spec changed.
        """
        changed = False
        for flag_type in other:
            if flag_type not in self:
                self[flag_type] = other[flag_type]
                changed = True
            else:
                extra_other = set(other[flag_type]) - set(self[flag_type])
                if extra_other:
                    self[flag_type] = list(self[flag_type]) + list(
                        x for x in other[flag_type] if x in extra_other
                    )
                    changed = True

                # Next, if any flags in other propagate, we force them to propagate in our case
                shared = list(sorted(set(other[flag_type]) - extra_other))
                for x, y in _shared_subset_pair_iterate(shared, sorted(self[flag_type])):
                    if y.propagate is True and x.propagate is False:
                        changed = True
                        y.propagate = False

        # TODO: what happens if flag groups with a partial (but not complete)
        # intersection specify different behaviors for flag propagation?

        return changed

    @staticmethod
    def valid_compiler_flags():
        return _valid_compiler_flags

    def copy(self):
        clone = FlagMap(self.spec)
        for name, compiler_flag in self.items():
            clone[name] = compiler_flag
        return clone

    def add_flag(self, flag_type, value, propagation, flag_group=None, source=None):
        """Stores the flag's value in CompilerFlag and adds it
        to the FlagMap

        Args:
            flag_type (str): the type of flag
            value (str): the flag's value that will be added to the flag_type's
                corresponding list
            propagation (bool): if ``True`` the flag value will be passed to
                the packages' dependencies. If``False`` it will not be passed
        """
        flag_group = flag_group or value
        flag = CompilerFlag(value, propagate=propagation, flag_group=flag_group, source=source)

        if flag_type not in self:
            self[flag_type] = [flag]
        else:
            self[flag_type].append(flag)

    def yaml_entry(self, flag_type):
        """Returns the flag type and a list of the flag values since the
        propagation values aren't needed when writing to yaml

        Args:
            flag_type (str): the type of flag to get values from

        Returns the flag_type and a list of the corresponding flags in
            string format
        """
        return flag_type, [str(flag) for flag in self[flag_type]]

    def _cmp_iter(self):
        for k, v in sorted(self.items()):
            yield k

            def flags():
                for flag in v:
                    yield flag
                    yield flag.propagate

            yield flags

    def __str__(self):
        if not self:
            return ""

        sorted_items = sorted((k, v) for k, v in self.items() if v)

        result = ""
        for flag_type, flags in sorted_items:
            normal = [f for f in flags if not f.propagate]
            if normal:
                value = spack.spec_parser.quote_if_needed(" ".join(normal))
                result += f" {flag_type}={value}"

            propagated = [f for f in flags if f.propagate]
            if propagated:
                value = spack.spec_parser.quote_if_needed(" ".join(propagated))
                result += f" {flag_type}=={value}"

        # TODO: somehow add this space only if something follows in Spec.format()
        if sorted_items:
            result += " "

        return result


def _sort_by_dep_types(dspec: DependencySpec):
    return dspec.depflag


class _EdgeMap(collections.abc.Mapping):
    """Represent a collection of edges (DependencySpec objects) in the DAG.

    Objects of this class are used in Specs to track edges that are
    outgoing towards direct dependencies, or edges that are incoming
    from direct dependents.

    Edges are stored in a dictionary and keyed by package name.
    """

    __slots__ = "edges", "store_by_child"

    def __init__(self, store_by_child: bool = True) -> None:
        self.edges: Dict[str, List[DependencySpec]] = {}
        self.store_by_child = store_by_child

    def __getitem__(self, key: str) -> List[DependencySpec]:
        return self.edges[key]

    def __iter__(self):
        return iter(self.edges)

    def __len__(self) -> int:
        return len(self.edges)

    def add(self, edge: DependencySpec) -> None:
        key = edge.spec.name if self.store_by_child else edge.parent.name
        if key in self.edges:
            lst = self.edges[key]
            lst.append(edge)
            lst.sort(key=_sort_by_dep_types)
        else:
            self.edges[key] = [edge]

    def __str__(self) -> str:
        return f"{{deps: {', '.join(str(d) for d in sorted(self.values()))}}}"

    def select(
        self,
        *,
        parent: Optional[str] = None,
        child: Optional[str] = None,
        depflag: dt.DepFlag = dt.ALL,
        virtuals: Optional[Union[str, Sequence[str]]] = None,
    ) -> List[DependencySpec]:
        """Selects a list of edges and returns them.

        If an edge:

        - Has *any* of the dependency types passed as argument,
        - Matches the parent and/or child name
        - Provides *any* of the virtuals passed as argument

        then it is selected.

        The deptypes argument needs to be a flag, since the method won't
        convert it for performance reason.

        Args:
            parent: name of the parent package
            child: name of the child package
            depflag: allowed dependency types in flag form
            virtuals: list of virtuals or specific virtual on the edge
        """
        if not depflag:
            return []

        # Start from all the edges we store
        selected = (d for d in itertools.chain.from_iterable(self.values()))

        # Filter by parent name
        if parent:
            selected = (d for d in selected if d.parent.name == parent)

        # Filter by child name
        if child:
            selected = (d for d in selected if d.spec.name == child)

        # Filter by allowed dependency types
        selected = (dep for dep in selected if not dep.depflag or (depflag & dep.depflag))

        # Filter by virtuals
        if virtuals is not None:
            if isinstance(virtuals, str):
                selected = (dep for dep in selected if virtuals in dep.virtuals)
            else:
                selected = (dep for dep in selected if any(v in dep.virtuals for v in virtuals))

        return list(selected)

    def clear(self):
        self.edges.clear()


def _headers_default_handler(spec: "Spec"):
    """Default handler when looking for the 'headers' attribute.

    Tries to search for ``*.h`` files recursively starting from
    ``spec.package.home.include``.

    Parameters:
        spec: spec that is being queried

    Returns:
        HeaderList: The headers in ``prefix.include``

    Raises:
        NoHeadersError: If no headers are found
    """
    home = getattr(spec.package, "home")
    headers = fs.find_headers("*", root=home.include, recursive=True)

    if headers:
        return headers
    raise spack.error.NoHeadersError(f"Unable to locate {spec.name} headers in {home}")


def _libs_default_handler(spec: "Spec"):
    """Default handler when looking for the 'libs' attribute.

    Tries to search for ``lib{spec.name}`` recursively starting from
    ``spec.package.home``. If ``spec.name`` starts with ``lib``, searches for
    ``{spec.name}`` instead.

    Parameters:
        spec: spec that is being queried

    Returns:
        LibraryList: The libraries found

    Raises:
        NoLibrariesError: If no libraries are found
    """

    # Variable 'name' is passed to function 'find_libraries', which supports
    # glob characters. For example, we have a package with a name 'abc-abc'.
    # Now, we don't know if the original name of the package is 'abc_abc'
    # (and it generates a library 'libabc_abc.so') or 'abc-abc' (and it
    # generates a library 'libabc-abc.so'). So, we tell the function
    # 'find_libraries' to give us anything that matches 'libabc?abc' and it
    # gives us either 'libabc-abc.so' or 'libabc_abc.so' (or an error)
    # depending on which one exists (there is a possibility, of course, to
    # get something like 'libabcXabc.so, but for now we consider this
    # unlikely).
    name = spec.name.replace("-", "?")
    home = getattr(spec.package, "home")

    # Avoid double 'lib' for packages whose names already start with lib
    if not name.startswith("lib") and not spec.satisfies("platform=windows"):
        name = "lib" + name

    # If '+shared' search only for shared library; if '~shared' search only for
    # static library; otherwise, first search for shared and then for static.
    search_shared = (
        [True] if ("+shared" in spec) else ([False] if ("~shared" in spec) else [True, False])
    )

    for shared in search_shared:
        # Since we are searching for link libraries, on Windows search only for
        # ".Lib" extensions by default as those represent import libraries for implicit links.
        libs = fs.find_libraries(name, home, shared=shared, recursive=True, runtime=False)
        if libs:
            return libs

    raise spack.error.NoLibrariesError(
        f"Unable to recursively locate {spec.name} libraries in {home}"
    )


class ForwardQueryToPackage:
    """Descriptor used to forward queries from Spec to Package"""

    def __init__(
        self,
        attribute_name: str,
        default_handler: Optional[Callable[["Spec"], Any]] = None,
        _indirect: bool = False,
    ) -> None:
        """Create a new descriptor.

        Parameters:
            attribute_name: name of the attribute to be searched for in the Package instance
            default_handler: default function to be called if the attribute was not found in the
                Package instance
            _indirect: temporarily added to redirect a query to another package.
        """
        self.attribute_name = attribute_name
        self.default = default_handler
        self.indirect = _indirect

    def __get__(self, instance: "SpecBuildInterface", cls):
        """Retrieves the property from Package using a well defined chain
        of responsibility.

        The order of call is:

        1. if the query was through the name of a virtual package try to
            search for the attribute `{virtual_name}_{attribute_name}`
            in Package

        2. try to search for attribute `{attribute_name}` in Package

        3. try to call the default handler

        The first call that produces a value will stop the chain.

        If no call can handle the request then AttributeError is raised with a
        message indicating that no relevant attribute exists.
        If a call returns None, an AttributeError is raised with a message
        indicating a query failure, e.g. that library files were not found in a
        'libs' query.
        """
        # TODO: this indirection exist solely for `spec["python"].command` to actually return
        # spec["python-venv"].command. It should be removed when `python` is a virtual.
        if self.indirect and instance.indirect_spec:
            pkg = instance.indirect_spec.package
        else:
            pkg = instance.wrapped_obj.package
        try:
            query = instance.last_query
        except AttributeError:
            # There has been no query yet: this means
            # a spec is trying to access its own attributes
            _ = instance.wrapped_obj[instance.wrapped_obj.name]  # NOQA: ignore=F841
            query = instance.last_query

        callbacks_chain = []
        # First in the chain : specialized attribute for virtual packages
        if query.isvirtual:
            specialized_name = "{0}_{1}".format(query.name, self.attribute_name)
            callbacks_chain.append(lambda: getattr(pkg, specialized_name))
        # Try to get the generic method from Package
        callbacks_chain.append(lambda: getattr(pkg, self.attribute_name))
        # Final resort : default callback
        if self.default is not None:
            _default = self.default  # make mypy happy
            callbacks_chain.append(lambda: _default(instance.wrapped_obj))

        # Trigger the callbacks in order, the first one producing a
        # value wins
        value = None
        message = None
        for f in callbacks_chain:
            try:
                value = f()
                # A callback can return None to trigger an error indicating
                # that the query failed.
                if value is None:
                    msg = "Query of package '{name}' for '{attrib}' failed\n"
                    msg += "\tprefix : {spec.prefix}\n"
                    msg += "\tspec : {spec}\n"
                    msg += "\tqueried as : {query.name}\n"
                    msg += "\textra parameters : {query.extra_parameters}"
                    message = msg.format(
                        name=pkg.name,
                        attrib=self.attribute_name,
                        spec=instance,
                        query=instance.last_query,
                    )
                else:
                    return value
                break
            except AttributeError:
                pass
        # value is 'None'
        if message is not None:
            # Here we can use another type of exception. If we do that, the
            # unit test 'test_getitem_exceptional_paths' in the file
            # lib/spack/spack/test/spec_dag.py will need to be updated to match
            # the type.
            raise AttributeError(message)
        # 'None' value at this point means that there are no appropriate
        # properties defined and no default handler, or that all callbacks
        # raised AttributeError. In this case, we raise AttributeError with an
        # appropriate message.
        fmt = "'{name}' package has no relevant attribute '{query}'\n"
        fmt += "\tspec : '{spec}'\n"
        fmt += "\tqueried as : '{spec.last_query.name}'\n"
        fmt += "\textra parameters : '{spec.last_query.extra_parameters}'\n"
        message = fmt.format(name=pkg.name, query=self.attribute_name, spec=instance)
        raise AttributeError(message)

    def __set__(self, instance, value):
        cls_name = type(instance).__name__
        msg = "'{0}' object attribute '{1}' is read-only"
        raise AttributeError(msg.format(cls_name, self.attribute_name))


# Represents a query state in a BuildInterface object
QueryState = collections.namedtuple("QueryState", ["name", "extra_parameters", "isvirtual"])


class SpecBuildInterface(lang.ObjectWrapper):
    # home is available in the base Package so no default is needed
    home = ForwardQueryToPackage("home", default_handler=None)
    headers = ForwardQueryToPackage("headers", default_handler=_headers_default_handler)
    libs = ForwardQueryToPackage("libs", default_handler=_libs_default_handler)
    command = ForwardQueryToPackage("command", default_handler=None, _indirect=True)

    def __init__(
        self,
        spec: "Spec",
        name: str,
        query_parameters: List[str],
        _parent: "Spec",
        is_virtual: bool,
    ):
        super().__init__(spec)
        # Adding new attributes goes after super() call since the ObjectWrapper
        # resets __dict__ to behave like the passed object
        original_spec = getattr(spec, "wrapped_obj", spec)
        self.wrapped_obj = original_spec
        self.token = original_spec, name, query_parameters, _parent, is_virtual
        self.last_query = QueryState(
            name=name, extra_parameters=query_parameters, isvirtual=is_virtual
        )

        # TODO: this ad-hoc logic makes `spec["python"].command` return
        # `spec["python-venv"].command` and should be removed when `python` is a virtual.
        self.indirect_spec = None
        if spec.name == "python":
            python_venvs = _parent.dependencies("python-venv")
            if not python_venvs:
                return
            self.indirect_spec = python_venvs[0]

    def __reduce__(self):
        return SpecBuildInterface, self.token

    def copy(self, *args, **kwargs):
        return self.wrapped_obj.copy(*args, **kwargs)


def tree(
    specs: List["Spec"],
    *,
    color: Optional[bool] = None,
    depth: bool = False,
    hashes: bool = False,
    hashlen: Optional[int] = None,
    cover: spack.traverse.CoverType = "nodes",
    indent: int = 0,
    format: str = DEFAULT_FORMAT,
    deptypes: Union[dt.DepFlag, dt.DepTypes] = dt.ALL,
    show_types: bool = False,
    depth_first: bool = False,
    recurse_dependencies: bool = True,
    status_fn: Optional[Callable[["Spec"], InstallStatus]] = None,
    prefix: Optional[Callable[["Spec"], str]] = None,
    key: Callable[["Spec"], Any] = id,
) -> str:
    """Prints out specs and their dependencies, tree-formatted with indentation.

    Status function may either output a boolean or an InstallStatus

    Args:
        color: if True, always colorize the tree. If False, don't colorize the tree. If None,
            use the default from llnl.tty.color
        depth: print the depth from the root
        hashes: if True, print the hash of each node
        hashlen: length of the hash to be printed
        cover: either "nodes" or "edges"
        indent: extra indentation for the tree being printed
        format: format to be used to print each node
        deptypes: dependency types to be represented in the tree
        show_types: if True, show the (merged) dependency type of a node
        depth_first: if True, traverse the DAG depth first when representing it as a tree
        recurse_dependencies: if True, recurse on dependencies
        status_fn: optional callable that takes a node as an argument and return its
            installation status
        prefix: optional callable that takes a node as an argument and return its
            installation prefix
    """
    out = ""

    if color is None:
        color = clr.get_color_when()

    # reduce deptypes over all in-edges when covering nodes
    if show_types and cover == "nodes":
        deptype_lookup: Dict[str, dt.DepFlag] = collections.defaultdict(dt.DepFlag)
        for edge in spack.traverse.traverse_edges(
            specs, cover="edges", deptype=deptypes, root=False
        ):
            deptype_lookup[edge.spec.dag_hash()] |= edge.depflag

    # SupportsRichComparisonT issue with List[Spec]
    sorted_specs: List["Spec"] = sorted(specs)  # type: ignore[type-var]

    for d, dep_spec in spack.traverse.traverse_tree(
        sorted_specs, cover=cover, deptype=deptypes, depth_first=depth_first, key=key
    ):
        node = dep_spec.spec

        if prefix is not None:
            out += prefix(node)
        out += " " * indent

        if depth:
            out += "%-4d" % d

        if status_fn:
            status = status_fn(node)
            if status in list(InstallStatus):
                out += clr.colorize(status.value, color=color)
            elif status:
                out += clr.colorize("@g{[+]}  ", color=color)
            else:
                out += clr.colorize("@r{[-]}  ", color=color)

        if hashes:
            out += clr.colorize("@K{%s}  ", color=color) % node.dag_hash(hashlen)

        if show_types:
            if cover == "nodes":
                depflag = deptype_lookup[dep_spec.spec.dag_hash()]
            else:
                # when covering edges or paths, we show dependency
                # types only for the edge through which we visited
                depflag = dep_spec.depflag

            type_chars = dt.flag_to_chars(depflag)
            out += "[%s]  " % type_chars

        out += "    " * d
        if d > 0:
            out += "^"
        out += node.format(format, color=color) + "\n"

        # Check if we wanted just the first line
        if not recurse_dependencies:
            break

    return out


class SpecAnnotations:
    def __init__(self) -> None:
        self.original_spec_format = SPECFILE_FORMAT_VERSION
        self.compiler_node_attribute: Optional["Spec"] = None

    def with_spec_format(self, spec_format: int) -> "SpecAnnotations":
        self.original_spec_format = spec_format
        return self

    def with_compiler(self, compiler: "Spec") -> "SpecAnnotations":
        self.compiler_node_attribute = compiler
        return self

    def __repr__(self) -> str:
        result = f"SpecAnnotations().with_spec_format({self.original_spec_format})"
        if self.compiler_node_attribute:
            result += f".with_compiler({str(self.compiler_node_attribute)})"
        return result


def _anonymous_star(dep, dep_format):
    """Determine if a spec needs a star to disambiguate it from an anonymous spec w/variants.

    Returns:
        "*" if a star is needed, "" otherwise
    """
    # named spec never needs star
    if dep.spec.name:
        return ""

    # virtuals without a name always need *: %c=* @4.0 foo=bar
    if dep.virtuals:
        return "*"

    # versions are first so checking for @ is faster than != VersionList(':')
    if dep_format.startswith("@"):
        return ""

    # compiler flags are key-value pairs and can be ambiguous with virtual assignment
    if dep.spec.compiler_flags:
        return "*"

    # booleans come first, and they don't need a star. key-value pairs do. If there are
    # no key value pairs, we're left with either an empty spec, which needs * as in
    # '^*', or we're left with arch, which is a key value pair, and needs a star.
    if not any(v.type == spack.variant.VariantType.BOOL for v in dep.spec.variants.values()):
        return "*"

    return "*" if dep.spec.architecture else ""


@lang.lazy_lexicographic_ordering(set_hash=False)
class Spec:
    compiler = DeprecatedCompilerSpec()

    @staticmethod
    def default_arch():
        """Return an anonymous spec for the default architecture"""
        s = Spec()
        s.architecture = ArchSpec.default_arch()
        return s

    def __init__(self, spec_like=None, *, external_path=None, external_modules=None):
        """Create a new Spec.

        Arguments:
            spec_like: if not provided, we initialize an anonymous Spec that matches any Spec;
                if provided we parse this as a Spec string, or we copy the provided Spec.

        Keyword arguments:
            external_path: prefix, if this is a spec for an external package
            external_modules: list of external modules, if this is an external package
                using modules.
        """
        # Copy if spec_like is a Spec.
        if isinstance(spec_like, Spec):
            self._dup(spec_like)
            return

        # init an empty spec that matches anything.
        self.name = None
        self.versions = vn.VersionList(":")
        self.variants = VariantMap(self)
        self.architecture = None
        self.compiler_flags = FlagMap(self)
        self._dependents = _EdgeMap(store_by_child=False)
        self._dependencies = _EdgeMap(store_by_child=True)
        self.namespace = None
        self.abstract_hash = None

        # initial values for all spec hash types
        for h in ht.HASHES:
            setattr(self, h.attr, None)

        # cache for spec's prefix, computed lazily by prefix property
        self._prefix = None

        # Python __hash__ is handled separately from the cached spec hashes
        self._dunder_hash = None

        # cache of package for this spec
        self._package = None

        # whether the spec is concrete or not; set at the end of concretization
        self._concrete = False

        # External detection details that can be set by internal Spack calls
        # in the constructor.
        self._external_path = external_path
        self.external_modules = Spec._format_module_list(external_modules)

        # This attribute is used to store custom information for external specs.
        self.extra_attributes: dict = {}

        # This attribute holds the original build copy of the spec if it is
        # deployed differently than it was built. None signals that the spec
        # is deployed "as built."
        # Build spec should be the actual build spec unless marked dirty.
        self._build_spec = None
        self.annotations = SpecAnnotations()

        if isinstance(spec_like, str):
            spack.spec_parser.parse_one_or_raise(spec_like, self)

        elif spec_like is not None:
            raise TypeError(f"Can't make spec out of {type(spec_like)}")

    @staticmethod
    def _format_module_list(modules):
        """Return a module list that is suitable for YAML serialization
        and hash computation.

        Given a module list, possibly read from a configuration file,
        return an object that serializes to a consistent YAML string
        before/after round-trip serialization to/from a Spec dictionary
        (stored in JSON format): when read in, the module list may
        contain YAML formatting that is discarded (non-essential)
        when stored as a Spec dictionary; we take care in this function
        to discard such formatting such that the Spec hash does not
        change before/after storage in JSON.
        """
        if modules:
            modules = list(modules)
        return modules

    @property
    def external_path(self):
        return llnl.path.path_to_os_path(self._external_path)[0]

    @external_path.setter
    def external_path(self, ext_path):
        self._external_path = ext_path

    @property
    def external(self):
        return bool(self.external_path) or bool(self.external_modules)

    @property
    def is_develop(self):
        """Return whether the Spec represents a user-developed package
        in a Spack ``Environment`` (i.e. using `spack develop`).
        """
        return bool(self.variants.get("dev_path", False))

    def clear_dependencies(self):
        """Trim the dependencies of this spec."""
        self._dependencies.clear()

    def clear_edges(self):
        """Trim the dependencies and dependents of this spec."""
        self._dependencies.clear()
        self._dependents.clear()

    def detach(self, deptype="all"):
        """Remove any reference that dependencies have of this node.

        Args:
            deptype (str or tuple): dependency types tracked by the
                current spec
        """
        key = self.dag_hash()
        # Go through the dependencies
        for dep in self.dependencies(deptype=deptype):
            # Remove the spec from dependents
            if self.name in dep._dependents:
                dependents_copy = dep._dependents.edges[self.name]
                del dep._dependents.edges[self.name]
                for edge in dependents_copy:
                    if edge.parent.dag_hash() == key:
                        continue
                    dep._dependents.add(edge)

    def _get_dependency(self, name):
        # WARNING: This function is an implementation detail of the
        # WARNING: original concretizer. Since with that greedy
        # WARNING: algorithm we don't allow multiple nodes from
        # WARNING: the same package in a DAG, here we hard-code
        # WARNING: using index 0 i.e. we assume that we have only
        # WARNING: one edge from package "name"
        deps = self.edges_to_dependencies(name=name)
        if len(deps) != 1:
            err_msg = 'expected only 1 "{0}" dependency, but got {1}'
            raise spack.error.SpecError(err_msg.format(name, len(deps)))
        return deps[0]

    def edges_from_dependents(
        self,
        name=None,
        depflag: dt.DepFlag = dt.ALL,
        *,
        virtuals: Optional[Union[str, Sequence[str]]] = None,
    ) -> List[DependencySpec]:
        """Return a list of edges connecting this node in the DAG
        to parents.

        Args:
            name (str): filter dependents by package name
            depflag: allowed dependency types
            virtuals: allowed virtuals
        """
        return [
            d for d in self._dependents.select(parent=name, depflag=depflag, virtuals=virtuals)
        ]

    def edges_to_dependencies(
        self,
        name=None,
        depflag: dt.DepFlag = dt.ALL,
        *,
        virtuals: Optional[Union[str, Sequence[str]]] = None,
    ) -> List[DependencySpec]:
        """Returns a list of edges connecting this node in the DAG to children.

        Args:
            name: filter dependencies by package name
            depflag: allowed dependency types
            virtuals: allowed virtuals
        """
        return [
            d for d in self._dependencies.select(child=name, depflag=depflag, virtuals=virtuals)
        ]

    @property
    def edge_attributes(self) -> str:
        """Helper method to print edge attributes in spec strings."""
        edges = self.edges_from_dependents()
        if not edges:
            return ""

        union = DependencySpec(parent=Spec(), spec=self, depflag=0, virtuals=())
        all_direct_edges = all(x.direct for x in edges)
        dep_conditions = set()

        for edge in edges:
            union.update_deptypes(edge.depflag)
            union.update_virtuals(edge.virtuals)
            dep_conditions.add(edge.when)

        deptypes_str = ""
        if not all_direct_edges and union.depflag:
            deptypes_str = f"deptypes={','.join(dt.flag_to_tuple(union.depflag))}"

        virtuals_str = f"virtuals={','.join(union.virtuals)}" if union.virtuals else ""

        conditions = [str(c) for c in dep_conditions if c != Spec()]
        when_str = f"when='{','.join(conditions)}'" if conditions else ""

        result = " ".join(filter(lambda x: bool(x), (when_str, deptypes_str, virtuals_str)))
        if result:
            result = f"[{result}]"
        return result

    def dependencies(
        self,
        name=None,
        deptype: Union[dt.DepTypes, dt.DepFlag] = dt.ALL,
        *,
        virtuals: Optional[Union[str, Sequence[str]]] = None,
    ) -> List["Spec"]:
        """Returns a list of direct dependencies (nodes in the DAG)

        Args:
            name: filter dependencies by package name
            deptype: allowed dependency types
            virtuals: allowed virtuals
        """
        if not isinstance(deptype, dt.DepFlag):
            deptype = dt.canonicalize(deptype)
        return [
            d.spec for d in self.edges_to_dependencies(name, depflag=deptype, virtuals=virtuals)
        ]

    def dependents(
        self, name=None, deptype: Union[dt.DepTypes, dt.DepFlag] = dt.ALL
    ) -> List["Spec"]:
        """Return a list of direct dependents (nodes in the DAG).

        Args:
            name (str): filter dependents by package name
            deptype: allowed dependency types
        """
        if not isinstance(deptype, dt.DepFlag):
            deptype = dt.canonicalize(deptype)
        return [d.parent for d in self.edges_from_dependents(name, depflag=deptype)]

    def _dependencies_dict(self, depflag: dt.DepFlag = dt.ALL):
        """Return a dictionary, keyed by package name, of the direct
        dependencies.

        Each value in the dictionary is a list of edges.

        Args:
            deptype: allowed dependency types
        """
        _sort_fn = lambda x: (x.spec.name, _sort_by_dep_types(x))
        _group_fn = lambda x: x.spec.name
        selected_edges = self._dependencies.select(depflag=depflag)
        result = {}
        for key, group in itertools.groupby(sorted(selected_edges, key=_sort_fn), key=_group_fn):
            result[key] = list(group)
        return result

    def _add_flag(
        self, name: str, value: Union[str, bool], propagate: bool, concrete: bool
    ) -> None:
        """Called by the parser to add a known flag"""

        if propagate and name in vt.RESERVED_NAMES:
            raise UnsupportedPropagationError(
                f"Propagation with '==' is not supported for '{name}'."
            )

        valid_flags = FlagMap.valid_compiler_flags()
        if name == "arch" or name == "architecture":
            assert type(value) is str, "architecture have a string value"
            parts = tuple(value.split("-"))
            plat, os, tgt = parts if len(parts) == 3 else (None, None, value)
            self._set_architecture(platform=plat, os=os, target=tgt)
        elif name == "platform":
            self._set_architecture(platform=value)
        elif name == "os" or name == "operating_system":
            self._set_architecture(os=value)
        elif name == "target":
            self._set_architecture(target=value)
        elif name == "namespace":
            self.namespace = value
        elif name in valid_flags:
            assert self.compiler_flags is not None
            assert type(value) is str, f"{name} must have a string value"
            flags_and_propagation = spack.compilers.flags.tokenize_flags(value, propagate)
            flag_group = " ".join(x for (x, y) in flags_and_propagation)
            for flag, propagation in flags_and_propagation:
                self.compiler_flags.add_flag(name, flag, propagation, flag_group)
        else:
            self.variants[name] = vt.VariantValue.from_string_or_bool(
                name, value, propagate=propagate, concrete=concrete
            )

    def _set_architecture(self, **kwargs):
        """Called by the parser to set the architecture."""
        arch_attrs = ["platform", "os", "target"]
        if self.architecture and self.architecture.concrete:
            raise DuplicateArchitectureError("Spec cannot have two architectures.")

        if not self.architecture:
            new_vals = tuple(kwargs.get(arg, None) for arg in arch_attrs)
            self.architecture = ArchSpec(new_vals)
        else:
            new_attrvals = [(a, v) for a, v in kwargs.items() if a in arch_attrs]
            for new_attr, new_value in new_attrvals:
                if getattr(self.architecture, new_attr):
                    raise DuplicateArchitectureError(f"Cannot specify '{new_attr}' twice")
                else:
                    setattr(self.architecture, new_attr, new_value)

    def _add_dependency(
        self,
        spec: "Spec",
        *,
        depflag: dt.DepFlag,
        virtuals: Tuple[str, ...],
        direct: bool = False,
        when: Optional["Spec"] = None,
    ):
        """Called by the parser to add another spec as a dependency.

        Args:
            depflag: dependency type for this edge
            virtuals: virtuals on this edge
            direct: if True denotes a direct dependency (associated with the % sigil)
            when: optional condition under which dependency holds
        """
        if when is None:
            when = Spec()

        if spec.name not in self._dependencies or not spec.name:
            self.add_dependency_edge(
                spec, depflag=depflag, virtuals=virtuals, direct=direct, when=when
            )
            return

        # Keep the intersection of constraints when a dependency is added multiple times with
        # the same deptype. Add a new dependency if it is added with a compatible deptype
        # (for example, a build-only dependency is compatible with a link-only dependency).
        # The only restrictions, currently, are that we cannot add edges with overlapping
        # dependency types and we cannot add multiple edges that have link/run dependency types.
        # See ``spack.deptypes.compatible``.
        orig = self._dependencies[spec.name]
        try:
            dspec = next(
                dspec for dspec in orig if depflag == dspec.depflag and when == dspec.when
            )
        except StopIteration:
            # Error if we have overlapping or incompatible deptypes
            if any(not dt.compatible(dspec.depflag, depflag) for dspec in orig) and all(
                dspec.when == when for dspec in orig
            ):
                edge_attrs = f"deptypes={dt.flag_to_chars(depflag).strip()}"
                required_dep_str = f"^[{edge_attrs}] {str(spec)}"

                raise DuplicateDependencyError(
                    f"{spec.name} is a duplicate dependency, with conflicting dependency types\n"
                    f"\t'{str(self)}' cannot depend on '{required_dep_str}'"
                )

            self.add_dependency_edge(
                spec, depflag=depflag, virtuals=virtuals, direct=direct, when=when
            )
            return

        try:
            dspec.spec.constrain(spec)
            dspec.update_virtuals(virtuals=virtuals)
        except spack.error.UnsatisfiableSpecError:
            raise DuplicateDependencyError(
                f"Cannot depend on incompatible specs '{dspec.spec}' and '{spec}'"
            )

    def add_dependency_edge(
        self,
        dependency_spec: "Spec",
        *,
        depflag: dt.DepFlag,
        virtuals: Tuple[str, ...],
        direct: bool = False,
        when: Optional["Spec"] = None,
    ):
        """Add a dependency edge to this spec.

        Args:
            dependency_spec: spec of the dependency
            deptypes: dependency types for this edge
            virtuals: virtuals provided by this edge
            direct: if True denotes a direct dependency
            when: if non-None, condition under which dependency holds
        """
        if when is None:
            when = Spec()

        # Check if we need to update edges that are already present
        selected = self._dependencies.select(child=dependency_spec.name)
        for edge in selected:
            has_errors, details = False, []
            msg = f"cannot update the edge from {edge.parent.name} to {edge.spec.name}"

            if edge.when != when:
                continue

            # If the dependency is to an existing spec, we can update dependency
            # types. If it is to a new object, check deptype compatibility.
            if id(edge.spec) != id(dependency_spec) and not dt.compatible(edge.depflag, depflag):
                has_errors = True
                details.append(
                    (
                        f"{edge.parent.name} has already an edge matching any"
                        f" of these types {depflag}"
                    )
                )

                if any(v in edge.virtuals for v in virtuals):
                    details.append(
                        (
                            f"{edge.parent.name} has already an edge matching any"
                            f" of these virtuals {virtuals}"
                        )
                    )

            if has_errors:
                raise spack.error.SpecError(msg, "\n".join(details))

        for edge in selected:
            if id(dependency_spec) == id(edge.spec) and edge.when == when:
                # If we are here, it means the edge object was previously added to
                # both the parent and the child. When we update this object they'll
                # both see the deptype modification.
                edge.update_deptypes(depflag=depflag)
                edge.update_virtuals(virtuals=virtuals)
                return

        edge = DependencySpec(
            self, dependency_spec, depflag=depflag, virtuals=virtuals, direct=direct, when=when
        )
        self._dependencies.add(edge)
        dependency_spec._dependents.add(edge)

    #
    # Public interface
    #
    @property
    def fullname(self):
        return (
            f"{self.namespace}.{self.name}" if self.namespace else (self.name if self.name else "")
        )

    @property
    def anonymous(self):
        return not self.name and not self.abstract_hash

    @property
    def root(self):
        """Follow dependent links and find the root of this spec's DAG.

        Spack specs have a single root (the package being installed).
        """
        # FIXME: In the case of multiple parents this property does not
        # FIXME: make sense. Should we revisit the semantics?
        if not self._dependents:
            return self
        edges_by_package = next(iter(self._dependents.values()))
        return edges_by_package[0].parent.root

    @property
    def package(self):
        assert self.concrete, "{0}: Spec.package can only be called on concrete specs".format(
            self.name
        )
        if not self._package:
            self._package = spack.repo.PATH.get(self)
        return self._package

    @property
    def concrete(self):
        """A spec is concrete if it describes a single build of a package.

        More formally, a spec is concrete if concretize() has been called
        on it and it has been marked `_concrete`.

        Concrete specs either can be or have been built. All constraints
        have been resolved, optional dependencies have been added or
        removed, a compiler has been chosen, and all variants have
        values.
        """
        return self._concrete

    @property
    def spliced(self):
        """Returns whether or not this Spec is being deployed as built i.e.
        whether or not this Spec has ever been spliced.
        """
        return any(s.build_spec is not s for s in self.traverse(root=True))

    @property
    def installed(self):
        """Installation status of a package.

        Returns:
            True if the package has been installed, False otherwise.
        """
        if not self.concrete:
            return False

        try:
            # If the spec is in the DB, check the installed
            # attribute of the record
            return spack.store.STORE.db.get_record(self).installed
        except KeyError:
            # If the spec is not in the DB, the method
            #  above raises a Key error
            return False

    @property
    def installed_upstream(self):
        """Whether the spec is installed in an upstream repository.

        Returns:
            True if the package is installed in an upstream, False otherwise.
        """
        if not self.concrete:
            return False

        upstream, _ = spack.store.STORE.db.query_by_spec_hash(self.dag_hash())
        return upstream

    @overload
    def traverse(
        self,
        *,
        root: bool = ...,
        order: spack.traverse.OrderType = ...,
        cover: spack.traverse.CoverType = ...,
        direction: spack.traverse.DirectionType = ...,
        deptype: Union[dt.DepFlag, dt.DepTypes] = ...,
        depth: Literal[False] = False,
        key: Callable[["Spec"], Any] = ...,
        visited: Optional[Set[Any]] = ...,
    ) -> Iterable["Spec"]: ...

    @overload
    def traverse(
        self,
        *,
        root: bool = ...,
        order: spack.traverse.OrderType = ...,
        cover: spack.traverse.CoverType = ...,
        direction: spack.traverse.DirectionType = ...,
        deptype: Union[dt.DepFlag, dt.DepTypes] = ...,
        depth: Literal[True],
        key: Callable[["Spec"], Any] = ...,
        visited: Optional[Set[Any]] = ...,
    ) -> Iterable[Tuple[int, "Spec"]]: ...

    def traverse(
        self,
        *,
        root: bool = True,
        order: spack.traverse.OrderType = "pre",
        cover: spack.traverse.CoverType = "nodes",
        direction: spack.traverse.DirectionType = "children",
        deptype: Union[dt.DepFlag, dt.DepTypes] = "all",
        depth: bool = False,
        key: Callable[["Spec"], Any] = id,
        visited: Optional[Set[Any]] = None,
    ) -> Iterable[Union["Spec", Tuple[int, "Spec"]]]:
        """Shorthand for :meth:`~spack.traverse.traverse_nodes`"""
        return spack.traverse.traverse_nodes(
            [self],
            root=root,
            order=order,
            cover=cover,
            direction=direction,
            deptype=deptype,
            depth=depth,
            key=key,
            visited=visited,
        )

    @overload
    def traverse_edges(
        self,
        *,
        root: bool = ...,
        order: spack.traverse.OrderType = ...,
        cover: spack.traverse.CoverType = ...,
        direction: spack.traverse.DirectionType = ...,
        deptype: Union[dt.DepFlag, dt.DepTypes] = ...,
        depth: Literal[False] = False,
        key: Callable[["Spec"], Any] = ...,
        visited: Optional[Set[Any]] = ...,
    ) -> Iterable[DependencySpec]: ...

    @overload
    def traverse_edges(
        self,
        *,
        root: bool = ...,
        order: spack.traverse.OrderType = ...,
        cover: spack.traverse.CoverType = ...,
        direction: spack.traverse.DirectionType = ...,
        deptype: Union[dt.DepFlag, dt.DepTypes] = ...,
        depth: Literal[True],
        key: Callable[["Spec"], Any] = ...,
        visited: Optional[Set[Any]] = ...,
    ) -> Iterable[Tuple[int, DependencySpec]]: ...

    def traverse_edges(
        self,
        *,
        root: bool = True,
        order: spack.traverse.OrderType = "pre",
        cover: spack.traverse.CoverType = "nodes",
        direction: spack.traverse.DirectionType = "children",
        deptype: Union[dt.DepFlag, dt.DepTypes] = "all",
        depth: bool = False,
        key: Callable[["Spec"], Any] = id,
        visited: Optional[Set[Any]] = None,
    ) -> Iterable[Union[DependencySpec, Tuple[int, DependencySpec]]]:
        """Shorthand for :meth:`~spack.traverse.traverse_edges`"""
        return spack.traverse.traverse_edges(
            [self],
            root=root,
            order=order,
            cover=cover,
            direction=direction,
            deptype=deptype,
            depth=depth,
            key=key,
            visited=visited,
        )

    def _format_edge_attributes(self, dep: DependencySpec, deptypes=True, virtuals=True):
        deptypes_str = (
            f"deptypes={','.join(dt.flag_to_tuple(dep.depflag))}"
            if deptypes and dep.depflag
            else ""
        )
        when_str = f"when='{(dep.when)}'" if dep.when != Spec() else ""
        virtuals_str = f"virtuals={','.join(dep.virtuals)}" if virtuals and dep.virtuals else ""

        attrs = " ".join(s for s in (when_str, deptypes_str, virtuals_str) if s)
        if attrs:
            attrs = f"[{attrs}] "

        return attrs

    def _format_dependencies(
        self,
        format_string: str = DEFAULT_FORMAT,
        include: Optional[Callable[[DependencySpec], bool]] = None,
        deptypes=True,
        _force_direct=False,
    ):
        """Helper for formatting dependencies on specs.

        Arguments:
            format_string: format string to use for each dependency
            include: predicate to select which dependencies to include
            deptypes: whether to format deptypes
            _force_direct: if True, print all dependencies as direct dependencies
                (to be removed when we have this metadata on concrete edges)
        """
        include = include or (lambda dep: True)
        parts = []
        if self.concrete:
            direct = self.edges_to_dependencies()
            transitive: List[DependencySpec] = []
        else:
            direct, transitive = lang.stable_partition(
                self.edges_to_dependencies(), predicate_fn=lambda x: x.direct
            )

        # helper for direct and transitive loops below
        def format_edge(edge, sigil, dep_spec=None):
            dep_spec = dep_spec or edge.spec
            dep_format = dep_spec.format(format_string)

            edge_attributes = (
                self._format_edge_attributes(edge, deptypes=deptypes, virtuals=False)
                if edge.depflag or edge.when != Spec()
                else ""
            )
            virtuals = f"{','.join(edge.virtuals)}=" if edge.virtuals else ""
            star = _anonymous_star(edge, dep_format)

            return f"{sigil}{edge_attributes}{star}{virtuals}{dep_format}"

        # direct dependencies
        for edge in sorted(direct, key=lambda x: x.spec.name):
            if not include(edge):
                continue

            # replace legacy compiler names
            old_name = edge.spec.name
            new_name = spack.aliases.BUILTIN_TO_LEGACY_COMPILER.get(old_name)
            try:
                # this is ugly but copies can be expensive
                if new_name:
                    edge.spec.name = new_name
                parts.append(format_edge(edge, "%", edge.spec))
            finally:
                edge.spec.name = old_name

        if self.concrete:
            # Concrete specs should go no further, as the complexity
            # below is O(paths)
            return " ".join(parts).strip()

        # transitive dependencies (with any direct dependencies)
        for edge in sorted(transitive, key=lambda x: x.spec.name):
            if not include(edge):
                continue
            sigil = "%" if _force_direct else "^"  # hack til direct deps represented better
            parts.append(format_edge(edge, sigil, edge.spec))

            # also recursively add any direct dependencies of transitive dependencies
            if edge.spec._dependencies:
                parts.append(
                    edge.spec._format_dependencies(
                        format_string=format_string,
                        include=include,
                        deptypes=deptypes,
                        _force_direct=_force_direct,
                    )
                )

        return " ".join(parts).strip()

    @property
    def compilers(self):
        # TODO: get rid of the space here and make formatting smarter
        return " " + self._format_dependencies(
            "{name}{@version}",
            include=lambda dep: any(lang in dep.virtuals for lang in ("c", "cxx", "fortran")),
            deptypes=False,
            _force_direct=True,
        )

    @property
    def long_spec(self):
        """Returns a string of the spec with the dependencies completely enumerated."""
        if self.concrete:
            return self.tree(format=DISPLAY_FORMAT)
        return f"{self.format()} {self._format_dependencies()}".strip()

    @property
    def short_spec(self):
        """Returns a version of the spec with the dependencies hashed
        instead of completely enumerated."""
        return self.format("{name}{@version}{variants}{ arch=architecture}{/hash:7}")

    @property
    def cshort_spec(self):
        """Returns an auto-colorized version of ``self.short_spec``."""
        return self.cformat("{name}{@version}{variants}{ arch=architecture}{/hash:7}")

    @property
    def prefix(self) -> spack.util.prefix.Prefix:
        if not self._concrete:
            raise spack.error.SpecError(f"Spec is not concrete: {self}")

        if self._prefix is None:
            _, record = spack.store.STORE.db.query_by_spec_hash(self.dag_hash())
            if record and record.path:
                self.set_prefix(record.path)
            else:
                self.set_prefix(spack.store.STORE.layout.path_for_spec(self))
        assert self._prefix is not None
        return self._prefix

    def set_prefix(self, value: str) -> None:
        self._prefix = spack.util.prefix.Prefix(llnl.path.convert_to_platform_path(value))

    def spec_hash(self, hash):
        """Utility method for computing different types of Spec hashes.

        Arguments:
            hash (spack.hash_types.SpecHashDescriptor): type of hash to generate.
        """
        # TODO: currently we strip build dependencies by default.  Rethink
        # this when we move to using package hashing on all specs.
        if hash.override is not None:
            return hash.override(self)
        node_dict = self.to_node_dict(hash=hash)
        json_text = json.dumps(
            node_dict, ensure_ascii=True, indent=None, separators=(",", ":"), sort_keys=False
        )
        # This implements "frankenhashes", preserving the last 7 characters of the
        # original hash when splicing so that we can avoid relocation issues
        out = spack.util.hash.b32_hash(json_text)
        if self.build_spec is not self:
            return out[:-7] + self.build_spec.spec_hash(hash)[-7:]
        return out

    def _cached_hash(self, hash, length=None, force=False):
        """Helper function for storing a cached hash on the spec.

        This will run spec_hash() with the deptype and package_hash
        parameters, and if this spec is concrete, it will store the value
        in the supplied attribute on this spec.

        Arguments:
            hash (spack.hash_types.SpecHashDescriptor): type of hash to generate.
            length (int): length of hash prefix to return (default is full hash string)
            force (bool): cache the hash even if spec is not concrete (default False)
        """
        if not hash.attr:
            return self.spec_hash(hash)[:length]

        hash_string = getattr(self, hash.attr, None)
        if hash_string:
            return hash_string[:length]
        else:
            hash_string = self.spec_hash(hash)
            if force or self.concrete:
                setattr(self, hash.attr, hash_string)

            return hash_string[:length]

    def package_hash(self):
        """Compute the hash of the contents of the package for this node"""
        # Concrete specs with the old DAG hash did not have the package hash, so we do
        # not know what the package looked like at concretization time
        if self.concrete and not self._package_hash:
            raise ValueError(
                "Cannot call package_hash() on concrete specs with the old dag_hash()"
            )

        return self._cached_hash(ht.package_hash)

    def dag_hash(self, length=None):
        """This is Spack's default hash, used to identify installations.

        NOTE: Versions of Spack prior to 0.18 only included link and run deps.
        NOTE: Versions of Spack prior to 1.0 only did not include test deps.

        """
        return self._cached_hash(ht.dag_hash, length)

    def dag_hash_bit_prefix(self, bits):
        """Get the first <bits> bits of the DAG hash as an integer type."""
        return spack.util.hash.base32_prefix_bits(self.dag_hash(), bits)

    def _lookup_hash(self):
        """Lookup just one spec with an abstract hash, returning a spec from the the environment,
        store, or finally, binary caches."""
        import spack.binary_distribution
        import spack.environment

        active_env = spack.environment.active_environment()

        # First env, then store, then binary cache
        matches = (
            (active_env.all_matching_specs(self) if active_env else [])
            or spack.store.STORE.db.query(self, installed=InstallRecordStatus.ANY)
            or spack.binary_distribution.BinaryCacheQuery(True)(self)
        )

        if not matches:
            raise InvalidHashError(self, self.abstract_hash)

        if len(matches) != 1:
            raise AmbiguousHashError(
                f"Multiple packages specify hash beginning '{self.abstract_hash}'.", *matches
            )

        return matches[0]

    def lookup_hash(self):
        """Given a spec with an abstract hash, return a copy of the spec with all properties and
        dependencies by looking up the hash in the environment, store, or finally, binary caches.
        This is non-destructive."""
        if self.concrete or not any(node.abstract_hash for node in self.traverse()):
            return self

        spec = self.copy(deps=False)
        # root spec is replaced
        if spec.abstract_hash:
            spec._dup(self._lookup_hash())
            return spec

        # Map the dependencies that need to be replaced
        node_lookup = {
            id(node): node._lookup_hash()
            for node in self.traverse(root=False)
            if node.abstract_hash
        }

        # Reconstruct dependencies
        for edge in self.traverse_edges(root=False):
            key = edge.parent.name
            current_node = spec if key == spec.name else spec[key]
            child_node = node_lookup.get(id(edge.spec), edge.spec.copy())
            current_node._add_dependency(
                child_node, depflag=edge.depflag, virtuals=edge.virtuals, direct=edge.direct
            )

        return spec

    def replace_hash(self):
        """Given a spec with an abstract hash, attempt to populate all properties and dependencies
        by looking up the hash in the environment, store, or finally, binary caches.
        This is destructive."""

        if not any(node for node in self.traverse(order="post") if node.abstract_hash):
            return

        self._dup(self.lookup_hash())

    def to_node_dict(self, hash=ht.dag_hash):
        """Create a dictionary representing the state of this Spec.

        ``to_node_dict`` creates the content that is eventually hashed by
        Spack to create identifiers like the DAG hash (see
        ``dag_hash()``).  Example result of ``to_node_dict`` for the
        ``sqlite`` package::

            {
                'sqlite': {
                    'version': '3.28.0',
                    'arch': {
                        'platform': 'darwin',
                        'platform_os': 'mojave',
                        'target': 'x86_64',
                    },
                    'namespace': 'builtin',
                    'parameters': {
                        'fts': 'true',
                        'functions': 'false',
                        'cflags': [],
                        'cppflags': [],
                        'cxxflags': [],
                        'fflags': [],
                        'ldflags': [],
                        'ldlibs': [],
                    },
                    'dependencies': {
                        'readline': {
                            'hash': 'zvaa4lhlhilypw5quj3akyd3apbq5gap',
                            'type': ['build', 'link'],
                        }
                    },
                }
            }

        Note that the dictionary returned does *not* include the hash of
        the *root* of the spec, though it does include hashes for each
        dependency, and (optionally) the package file corresponding to
        each node.

        See ``to_dict()`` for a "complete" spec hash, with hashes for
        each node and nodes for each dependency (instead of just their
        hashes).

        Arguments:
            hash (spack.hash_types.SpecHashDescriptor) type of hash to generate.
        """
        d = {"name": self.name}

        if self.versions:
            d.update(self.versions.to_dict())

        if self.architecture:
            d.update(self.architecture.to_dict())

        if self.namespace:
            d["namespace"] = self.namespace

        params = dict(sorted(v.yaml_entry() for v in self.variants.values()))

        # Only need the string compiler flag for yaml file
        params.update(
            sorted(
                self.compiler_flags.yaml_entry(flag_type)
                for flag_type in self.compiler_flags.keys()
            )
        )

        if params:
            d["parameters"] = params

        if params and not self.concrete:
            flag_names = [
                name
                for name, flags in self.compiler_flags.items()
                if any(x.propagate for x in flags)
            ]
            d["propagate"] = sorted(
                itertools.chain(
                    [v.name for v in self.variants.values() if v.propagate], flag_names
                )
            )
            d["abstract"] = sorted(v.name for v in self.variants.values() if not v.concrete)

        if self.external:
            d["external"] = {
                "path": self.external_path,
                "module": self.external_modules or None,
                "extra_attributes": syaml.sorted_dict(self.extra_attributes),
            }

        if not self._concrete:
            d["concrete"] = False

        if "patches" in self.variants:
            variant = self.variants["patches"]
            if hasattr(variant, "_patches_in_order_of_appearance"):
                d["patches"] = variant._patches_in_order_of_appearance

        if (
            self._concrete
            and hash.package_hash
            and hasattr(self, "_package_hash")
            and self._package_hash
        ):
            # We use the attribute here instead of `self.package_hash()` because this
            # should *always* be assignhed at concretization time. We don't want to try
            # to compute a package hash for concrete spec where a) the package might not
            # exist, or b) the `dag_hash` didn't include the package hash when the spec
            # was concretized.
            package_hash = self._package_hash

            # Full hashes are in bytes
            if not isinstance(package_hash, str) and isinstance(package_hash, bytes):
                package_hash = package_hash.decode("utf-8")
            d["package_hash"] = package_hash

        # Note: Relies on sorting dict by keys later in algorithm.
        deps = self._dependencies_dict(depflag=hash.depflag)
        if deps:
            dependencies = []
            for name, edges_for_name in sorted(deps.items()):
                for dspec in edges_for_name:
                    dep_attrs = {
                        "name": name,
                        hash.name: dspec.spec._cached_hash(hash),
                        "parameters": {
                            "deptypes": dt.flag_to_tuple(dspec.depflag),
                            "virtuals": dspec.virtuals,
                        },
                    }
                    if dspec.direct:
                        dep_attrs["parameters"]["direct"] = True
                    dependencies.append(dep_attrs)

            d["dependencies"] = dependencies

        # Name is included in case this is replacing a virtual.
        if self._build_spec:
            d["build_spec"] = {
                "name": self.build_spec.name,
                hash.name: self.build_spec._cached_hash(hash),
            }

        # Annotations
        d["annotations"] = {"original_specfile_version": self.annotations.original_spec_format}
        if self.annotations.original_spec_format < 5:
            d["annotations"]["compiler"] = str(self.annotations.compiler_node_attribute)

        return d

    def to_dict(self, hash=ht.dag_hash):
        """Create a dictionary suitable for writing this spec to YAML or JSON.

        This dictionaries like the one that is ultimately written to a
        ``spec.json`` file in each Spack installation directory.  For
        example, for sqlite::

            {
            "spec": {
                "_meta": {
                "version": 2
                },
                "nodes": [
                {
                    "name": "sqlite",
                    "version": "3.34.0",
                    "arch": {
                    "platform": "darwin",
                    "platform_os": "catalina",
                    "target": "x86_64"
                    },
                    "compiler": {
                    "name": "apple-clang",
                    "version": "11.0.0"
                    },
                    "namespace": "builtin",
                    "parameters": {
                    "column_metadata": true,
                    "fts": true,
                    "functions": false,
                    "rtree": false,
                    "cflags": [],
                    "cppflags": [],
                    "cxxflags": [],
                    "fflags": [],
                    "ldflags": [],
                    "ldlibs": []
                    },
                    "dependencies": [
                    {
                        "name": "readline",
                        "hash": "4f47cggum7p4qmp3xna4hi547o66unva",
                        "type": [
                        "build",
                        "link"
                        ]
                    },
                    {
                        "name": "zlib",
                        "hash": "uvgh6p7rhll4kexqnr47bvqxb3t33jtq",
                        "type": [
                        "build",
                        "link"
                        ]
                    }
                    ],
                    "hash": "tve45xfqkfgmzwcyfetze2z6syrg7eaf",
                },
                    # ... more node dicts for readline and its dependencies ...
                ]
            }

        Note that this dictionary starts with the 'spec' key, and what
        follows is a list starting with the root spec, followed by its
        dependencies in preorder.  Each node in the list also has a
        'hash' key that contains the hash of the node *without* the hash
        field included.

        In the example, the package content hash is not included in the
        spec, but if ``package_hash`` were true there would be an
        additional field on each node called ``package_hash``.

        ``from_dict()`` can be used to read back in a spec that has been
        converted to a dictionary, serialized, and read back in.

        Arguments:
            deptype (tuple or str): dependency types to include when
                traversing the spec.
            package_hash (bool): whether to include package content
                hashes in the dictionary.

        """
        node_list = []  # Using a list to preserve preorder traversal for hash.
        hash_set = set()
        for s in self.traverse(order="pre", deptype=hash.depflag):
            spec_hash = s._cached_hash(hash)

            if spec_hash not in hash_set:
                node_list.append(s.node_dict_with_hashes(hash))
                hash_set.add(spec_hash)

            if s.build_spec is not s:
                build_spec_list = s.build_spec.to_dict(hash)["spec"]["nodes"]
                for node in build_spec_list:
                    node_hash = node[hash.name]
                    if node_hash not in hash_set:
                        node_list.append(node)
                        hash_set.add(node_hash)

        return {"spec": {"_meta": {"version": SPECFILE_FORMAT_VERSION}, "nodes": node_list}}

    def node_dict_with_hashes(self, hash=ht.dag_hash):
        """Returns a node_dict of this spec with the dag hash added.  If this
        spec is concrete, the full hash is added as well.  If 'build' is in
        the hash_type, the build hash is also added."""
        node = self.to_node_dict(hash)
        # All specs have at least a DAG hash
        node[ht.dag_hash.name] = self.dag_hash()

        if not self.concrete:
            node["concrete"] = False

        # we can also give them other hash types if we want
        if hash.name != ht.dag_hash.name:
            node[hash.name] = self._cached_hash(hash)

        return node

    def to_yaml(self, stream=None, hash=ht.dag_hash):
        return syaml.dump(self.to_dict(hash), stream=stream, default_flow_style=False)

    def to_json(self, stream=None, hash=ht.dag_hash):
        return sjson.dump(self.to_dict(hash), stream)

    @staticmethod
    def from_specfile(path):
        """Construct a spec from a JSON or YAML spec file path"""
        with open(path, "r", encoding="utf-8") as fd:
            file_content = fd.read()
            if path.endswith(".json"):
                return Spec.from_json(file_content)
            return Spec.from_yaml(file_content)

    @staticmethod
    def override(init_spec, change_spec):
        # TODO: this doesn't account for the case where the changed spec
        # (and the user spec) have dependencies
        new_spec = init_spec.copy()
        package_cls = spack.repo.PATH.get_pkg_class(new_spec.name)
        if change_spec.versions and not change_spec.versions == vn.any_version:
            new_spec.versions = change_spec.versions

        for vname, value in change_spec.variants.items():
            if vname in package_cls.variant_names():
                if vname in new_spec.variants:
                    new_spec.variants.substitute(value)
                else:
                    new_spec.variants[vname] = value
            else:
                raise ValueError("{0} is not a variant of {1}".format(vname, new_spec.name))

        if change_spec.compiler_flags:
            for flagname, flagvals in change_spec.compiler_flags.items():
                new_spec.compiler_flags[flagname] = flagvals
        if change_spec.architecture:
            new_spec.architecture = ArchSpec.override(
                new_spec.architecture, change_spec.architecture
            )
        return new_spec

    @staticmethod
    def from_literal(spec_dict, normal=True):
        """Builds a Spec from a dictionary containing the spec literal.

        The dictionary must have a single top level key, representing the root,
        and as many secondary level keys as needed in the spec.

        The keys can be either a string or a Spec or a tuple containing the
        Spec and the dependency types.

        Args:
            spec_dict (dict): the dictionary containing the spec literal
            normal (bool): if True the same key appearing at different levels
                of the ``spec_dict`` will map to the same object in memory.

        Examples:
            A simple spec ``foo`` with no dependencies:

            .. code-block:: python

                {'foo': None}

            A spec ``foo`` with a ``(build, link)`` dependency ``bar``:

            .. code-block:: python

                {'foo':
                    {'bar:build,link': None}}

            A spec with a diamond dependency and various build types:

            .. code-block:: python

                {'dt-diamond': {
                    'dt-diamond-left:build,link': {
                        'dt-diamond-bottom:build': None
                    },
                    'dt-diamond-right:build,link': {
                        'dt-diamond-bottom:build,link,run': None
                    }
                }}

            The same spec with a double copy of ``dt-diamond-bottom`` and
            no diamond structure:

            .. code-block:: python

                {'dt-diamond': {
                    'dt-diamond-left:build,link': {
                        'dt-diamond-bottom:build': None
                    },
                    'dt-diamond-right:build,link': {
                        'dt-diamond-bottom:build,link,run': None
                    }
                }, normal=False}

            Constructing a spec using a Spec object as key:

            .. code-block:: python

                mpich = Spec('mpich')
                libelf = Spec('libelf@1.8.11')
                expected_normalized = Spec.from_literal({
                    'mpileaks': {
                        'callpath': {
                            'dyninst': {
                                'libdwarf': {libelf: None},
                                libelf: None
                            },
                            mpich: None
                        },
                        mpich: None
                    },
                })

        """

        # Maps a literal to a Spec, to be sure we are reusing the same object
        spec_cache = LazySpecCache()

        def spec_builder(d):
            # The invariant is that the top level dictionary must have
            # only one key
            assert len(d) == 1

            # Construct the top-level spec
            spec_like, dep_like = next(iter(d.items()))

            # If the requirements was for unique nodes (default)
            # then reuse keys from the local cache. Otherwise build
            # a new node every time.
            if not isinstance(spec_like, Spec):
                spec = spec_cache[spec_like] if normal else Spec(spec_like)
            else:
                spec = spec_like

            if dep_like is None:
                return spec

            def name_and_dependency_types(s: str) -> Tuple[str, dt.DepFlag]:
                """Given a key in the dictionary containing the literal,
                extracts the name of the spec and its dependency types.

                Args:
                    s: key in the dictionary containing the literal
                """
                t = s.split(":")

                if len(t) > 2:
                    msg = 'more than one ":" separator in key "{0}"'
                    raise KeyError(msg.format(s))

                name = t[0]
                if len(t) == 2:
                    depflag = dt.flag_from_strings(dep_str.strip() for dep_str in t[1].split(","))
                else:
                    depflag = 0
                return name, depflag

            def spec_and_dependency_types(
                s: Union[Spec, Tuple[Spec, str]],
            ) -> Tuple[Spec, dt.DepFlag]:
                """Given a non-string key in the literal, extracts the spec
                and its dependency types.

                Args:
                    s: either a Spec object, or a tuple of Spec and string of dependency types
                """
                if isinstance(s, Spec):
                    return s, 0

                spec_obj, dtypes = s
                return spec_obj, dt.flag_from_strings(dt.strip() for dt in dtypes.split(","))

            # Recurse on dependencies
            for s, s_dependencies in dep_like.items():
                if isinstance(s, str):
                    dag_node, dep_flag = name_and_dependency_types(s)
                else:
                    dag_node, dep_flag = spec_and_dependency_types(s)

                dependency_spec = spec_builder({dag_node: s_dependencies})
                spec._add_dependency(dependency_spec, depflag=dep_flag, virtuals=())

            return spec

        return spec_builder(spec_dict)

    @staticmethod
    def from_dict(data) -> "Spec":
        """Construct a spec from JSON/YAML.

        Args:
            data: a nested dict/list data structure read from YAML or JSON.
        """
        # Legacy specfile format
        if isinstance(data["spec"], list):
            spec = SpecfileV1.load(data)
        elif int(data["spec"]["_meta"]["version"]) == 2:
            spec = SpecfileV2.load(data)
        elif int(data["spec"]["_meta"]["version"]) == 3:
            spec = SpecfileV3.load(data)
        elif int(data["spec"]["_meta"]["version"]) == 4:
            spec = SpecfileV4.load(data)
        else:
            spec = SpecfileV5.load(data)

        # Any git version should
        for s in spec.traverse():
            s.attach_git_version_lookup()

        return spec

    @staticmethod
    def from_yaml(stream) -> "Spec":
        """Construct a spec from YAML.

        Args:
            stream: string or file object to read from.
        """
        data = syaml.load(stream)
        return Spec.from_dict(data)

    @staticmethod
    def from_json(stream) -> "Spec":
        """Construct a spec from JSON.

        Args:
            stream: string or file object to read from.
        """
        try:
            data = sjson.load(stream)
            return Spec.from_dict(data)
        except Exception as e:
            raise sjson.SpackJSONError("error parsing JSON spec:", e) from e

    @staticmethod
    def extract_json_from_clearsig(data):
        m = CLEARSIGN_FILE_REGEX.search(data)
        if m:
            return sjson.load(m.group(1))
        return sjson.load(data)

    @staticmethod
    def from_signed_json(stream):
        """Construct a spec from clearsigned json spec file.

        Args:
            stream: string or file object to read from.
        """
        data = stream
        if hasattr(stream, "read"):
            data = stream.read()

        extracted_json = Spec.extract_json_from_clearsig(data)
        return Spec.from_dict(extracted_json)

    @staticmethod
    def from_detection(
        spec_str: str,
        *,
        external_path: str,
        external_modules: Optional[List[str]] = None,
        extra_attributes: Optional[Dict] = None,
    ) -> "Spec":
        """Construct a spec from a spec string determined during external
        detection and attach extra attributes to it.

        Args:
            spec_str: spec string
            external_path: prefix of the external spec
            external_modules: optional module files to be loaded when the external spec is used
            extra_attributes: dictionary containing extra attributes
        """
        s = Spec(spec_str, external_path=external_path, external_modules=external_modules)
        extra_attributes = syaml.sorted_dict(extra_attributes or {})
        # This is needed to be able to validate multi-valued variants,
        # otherwise they'll still be abstract in the context of detection.
        substitute_abstract_variants(s)
        s.extra_attributes = extra_attributes
        return s

    def _patches_assigned(self):
        """Whether patches have been assigned to this spec by the concretizer."""
        # FIXME: _patches_in_order_of_appearance is attached after concretization
        # FIXME: to store the order of patches.
        # FIXME: Probably needs to be refactored in a cleaner way.
        if "patches" not in self.variants:
            return False

        # ensure that patch state is consistent
        patch_variant = self.variants["patches"]
        assert hasattr(
            patch_variant, "_patches_in_order_of_appearance"
        ), "patches should always be assigned with a patch variant."

        return True

    @staticmethod
    def ensure_no_deprecated(root):
        """Raise if a deprecated spec is in the dag.

        Args:
            root (Spec): root spec to be analyzed

        Raises:
            SpecDeprecatedError: if any deprecated spec is found
        """
        deprecated = []
        with spack.store.STORE.db.read_transaction():
            for x in root.traverse():
                _, rec = spack.store.STORE.db.query_by_spec_hash(x.dag_hash())
                if rec and rec.deprecated_for:
                    deprecated.append(rec)
        if deprecated:
            msg = "\n    The following specs have been deprecated"
            msg += " in favor of specs with the hashes shown:\n"
            for rec in deprecated:
                msg += "        %s  --> %s\n" % (rec.spec, rec.deprecated_for)
            msg += "\n"
            msg += "    For each package listed, choose another spec\n"
            raise SpecDeprecatedError(msg)

    def _mark_root_concrete(self, value=True):
        """Mark just this spec (not dependencies) concrete."""
        if (not value) and self.concrete and self.installed:
            return
        self._concrete = value
        self._validate_version()

    def _validate_version(self):
        # Specs that were concretized with just a git sha as version, without associated
        # Spack version, get their Spack version mapped to develop. This should only apply
        # when reading specs concretized with Spack 0.19 or earlier. Currently Spack always
        # ensures that GitVersion specs have an associated Spack version.
        v = self.versions.concrete
        if not isinstance(v, vn.GitVersion):
            return

        try:
            v.ref_version
        except vn.VersionLookupError:
            before = self.cformat("{name}{@version}{/hash:7}")
            v.std_version = vn.StandardVersion.from_string("develop")
            tty.debug(
                f"the git sha of {before} could not be resolved to spack version; "
                f"it has been replaced by {self.cformat('{name}{@version}{/hash:7}')}."
            )

    def _mark_concrete(self, value=True):
        """Mark this spec and its dependencies as concrete.

        Only for internal use -- client code should use "concretize"
        unless there is a need to force a spec to be concrete.
        """
        # if set to false, clear out all hashes (set to None or remove attr)
        # may need to change references to respect None
        for s in self.traverse():
            if (not value) and s.concrete and s.installed:
                continue
            elif not value:
                s.clear_caches()
            s._mark_root_concrete(value)

    def _finalize_concretization(self):
        """Assign hashes to this spec, and mark it concrete.

        There are special semantics to consider for `package_hash`, because we can't
        call it on *already* concrete specs, but we need to assign it *at concretization
        time* to just-concretized specs. So, the concretizer must assign the package
        hash *before* marking their specs concrete (so that we know which specs were
        already concrete before this latest concretization).

        `dag_hash` is also tricky, since it cannot compute `package_hash()` lazily.
        Because `package_hash` needs to be assigned *at concretization time*,
        `to_node_dict()` can't just assume that it can compute `package_hash` itself
        -- it needs to either see or not see a `_package_hash` attribute.

        Rules of thumb for `package_hash`:
          1. Old-style concrete specs from *before* `dag_hash` included `package_hash`
             will not have a `_package_hash` attribute at all.
          2. New-style concrete specs will have a `_package_hash` assigned at
             concretization time.
          3. Abstract specs will not have a `_package_hash` attribute at all.

        """
        for spec in self.traverse():
            # Already concrete specs either already have a package hash (new dag_hash())
            # or they never will b/c we can't know it (old dag_hash()). Skip them.
            #
            # We only assign package hash to not-yet-concrete specs, for which we know
            # we can compute the hash.
            if not spec.concrete:
                # we need force=True here because package hash assignment has to happen
                # before we mark concrete, so that we know what was *already* concrete.
                spec._cached_hash(ht.package_hash, force=True)

                # keep this check here to ensure package hash is saved
                assert getattr(spec, ht.package_hash.attr)

        # Mark everything in the spec as concrete
        self._mark_concrete()

        # Assign dag_hash (this *could* be done lazily, but it's assigned anyway in
        # ensure_no_deprecated, and it's clearer to see explicitly where it happens).
        # Any specs that were concrete before finalization will already have a cached
        # DAG hash.
        for spec in self.traverse():
            spec._cached_hash(ht.dag_hash)

    def index(self, deptype="all"):
        """Return a dictionary that points to all the dependencies in this
        spec.
        """
        dm = collections.defaultdict(list)
        for spec in self.traverse(deptype=deptype):
            dm[spec.name].append(spec)
        return dm

    def validate_or_raise(self):
        """Checks that names and values in this spec are real. If they're not,
        it will raise an appropriate exception.
        """
        # FIXME: this function should be lazy, and collect all the errors
        # FIXME: before raising the exceptions, instead of being greedy and
        # FIXME: raise just the first one encountered
        for spec in self.traverse():
            # raise an UnknownPackageError if the spec's package isn't real.
            if spec.name and not spack.repo.PATH.is_virtual(spec.name):
                spack.repo.PATH.get_pkg_class(spec.fullname)

            # FIXME: atm allow '%' on abstract specs only if they depend on C, C++, or Fortran
            if spec.dependencies(deptype="build"):
                pkg_cls = spack.repo.PATH.get_pkg_class(spec.fullname)
                pkg_dependencies = pkg_cls.dependency_names()
                if not any(x in pkg_dependencies for x in ("c", "cxx", "fortran")):
                    raise UnsupportedCompilerError(
                        f"{spec.fullname} does not depend on 'c', 'cxx, or 'fortran'"
                    )

            # Ensure correctness of variants (if the spec is not virtual)
            if not spack.repo.PATH.is_virtual(spec.name):
                Spec.ensure_valid_variants(spec)
                substitute_abstract_variants(spec)

    @staticmethod
    def ensure_valid_variants(spec):
        """Ensures that the variant attached to a spec are valid.

        Args:
            spec (Spec): spec to be analyzed

        Raises:
            spack.variant.UnknownVariantError: on the first unknown variant found
        """
        # concrete variants are always valid
        if spec.concrete:
            return

        pkg_cls = spack.repo.PATH.get_pkg_class(spec.fullname)
        pkg_variants = pkg_cls.variant_names()
        # reserved names are variants that may be set on any package
        # but are not necessarily recorded by the package's class
        propagate_variants = [name for name, variant in spec.variants.items() if variant.propagate]

        not_existing = set(spec.variants)
        not_existing.difference_update(pkg_variants, vt.RESERVED_NAMES, propagate_variants)

        if not_existing:
            raise vt.UnknownVariantError(
                f"No such variant {not_existing} for spec: '{spec}'", list(not_existing)
            )

    def constrain(self, other, deps=True):
        """Intersect self with other in-place. Return True if self changed, False otherwise.

        Args:
            other: constraint to be added to self
            deps: if False, constrain only the root node, otherwise constrain dependencies
                as well.

        Raises:
             spack.error.UnsatisfiableSpecError: when self cannot be constrained
        """
        # If we are trying to constrain a concrete spec, either the spec
        # already satisfies the constraint (and the method returns False)
        # or it raises an exception
        if self.concrete:
            if self.satisfies(other):
                return False
            else:
                raise spack.error.UnsatisfiableSpecError(self, other, "constrain a concrete spec")

        other = self._autospec(other)
        if other.concrete and other.satisfies(self):
            self._dup(other)
            return True

        if other.abstract_hash:
            if not self.abstract_hash or other.abstract_hash.startswith(self.abstract_hash):
                self.abstract_hash = other.abstract_hash
            elif not self.abstract_hash.startswith(other.abstract_hash):
                raise InvalidHashError(self, other.abstract_hash)

        if not (self.name == other.name or (not self.name) or (not other.name)):
            raise UnsatisfiableSpecNameError(self.name, other.name)

        if (
            other.namespace is not None
            and self.namespace is not None
            and other.namespace != self.namespace
        ):
            raise UnsatisfiableSpecNameError(self.fullname, other.fullname)

        if not self.versions.overlaps(other.versions):
            raise UnsatisfiableVersionSpecError(self.versions, other.versions)

        for v in [x for x in other.variants if x in self.variants]:
            if not self.variants[v].intersects(other.variants[v]):
                raise vt.UnsatisfiableVariantSpecError(self.variants[v], other.variants[v])

        sarch, oarch = self.architecture, other.architecture
        if (
            sarch is not None
            and oarch is not None
            and not self.architecture.intersects(other.architecture)
        ):
            raise UnsatisfiableArchitectureSpecError(sarch, oarch)

        changed = False

        if not self.name and other.name:
            self.name = other.name
            changed = True

        if not self.namespace and other.namespace:
            self.namespace = other.namespace
            changed = True

        changed |= self.versions.intersect(other.versions)
        changed |= self.variants.constrain(other.variants)

        changed |= self.compiler_flags.constrain(other.compiler_flags)

        sarch, oarch = self.architecture, other.architecture
        if sarch is not None and oarch is not None:
            changed |= self.architecture.constrain(other.architecture)
        elif oarch is not None:
            self.architecture = oarch
            changed = True

        if deps:
            changed |= self._constrain_dependencies(other)

        if other.concrete and not self.concrete and other.satisfies(self):
            self._finalize_concretization()

        return changed

    def _constrain_dependencies(self, other: "Spec") -> bool:
        """Apply constraints of other spec's dependencies to this spec."""
        if not other._dependencies:
            return False

        # TODO: might want more detail than this, e.g. specific deps
        # in violation. if this becomes a priority get rid of this
        # check and be more specific about what's wrong.
        if not other._intersects_dependencies(self):
            raise UnsatisfiableDependencySpecError(other, self)

        if any(not d.name for d in other.traverse(root=False)):
            raise UnconstrainableDependencySpecError(other)

        reference_spec = self.copy(deps=True)
        for edge in other.edges_to_dependencies():
            existing = [
                e for e in self.edges_to_dependencies(edge.spec.name) if e.when == edge.when
            ]
            if existing:
                existing[0].spec.constrain(edge.spec)
                existing[0].update_deptypes(edge.depflag)
                existing[0].update_virtuals(edge.virtuals)
            else:
                self.add_dependency_edge(
                    edge.spec,
                    depflag=edge.depflag,
                    virtuals=edge.virtuals,
                    direct=edge.direct,
                    when=edge.when,
                )
        return self != reference_spec

    def common_dependencies(self, other):
        """Return names of dependencies that self and other have in common."""
        common = set(s.name for s in self.traverse(root=False))
        common.intersection_update(s.name for s in other.traverse(root=False))
        return common

    def constrained(self, other, deps=True):
        """Return a constrained copy without modifying this spec."""
        clone = self.copy(deps=deps)
        clone.constrain(other, deps)
        return clone

    def direct_dep_difference(self, other):
        """Returns dependencies in self that are not in other."""
        mine = set(dname for dname in self._dependencies)
        mine.difference_update(dname for dname in other._dependencies)
        return mine

    def _autospec(self, spec_like):
        """
        Used to convert arguments to specs.  If spec_like is a spec, returns
        it.  If it's a string, tries to parse a string.  If that fails, tries
        to parse a local spec from it (i.e. name is assumed to be self's name).
        """
        if isinstance(spec_like, Spec):
            return spec_like
        return Spec(spec_like)

    def intersects(self, other: Union[str, "Spec"], deps: bool = True) -> bool:
        """Return True if there exists at least one concrete spec that matches both
        self and other, otherwise False.

        This operation is commutative, and if two specs intersect it means that one
        can constrain the other.

        Args:
            other: spec to be checked for compatibility
            deps: if True check compatibility of dependency nodes too, if False only check root
        """
        other = self._autospec(other)

        if other.concrete and self.concrete:
            return self.dag_hash() == other.dag_hash()

        elif self.concrete:
            return self.satisfies(other)

        elif other.concrete:
            return other.satisfies(self)

        # From here we know both self and other are not concrete
        self_hash = self.abstract_hash
        other_hash = other.abstract_hash

        if (
            self_hash
            and other_hash
            and not (self_hash.startswith(other_hash) or other_hash.startswith(self_hash))
        ):
            return False

        # If the names are different, we need to consider virtuals
        if self.name != other.name and self.name and other.name:
            self_virtual = spack.repo.PATH.is_virtual(self.name)
            other_virtual = spack.repo.PATH.is_virtual(other.name)
            if self_virtual and other_virtual:
                # Two virtual specs intersect only if there are providers for both
                lhs = spack.repo.PATH.providers_for(str(self))
                rhs = spack.repo.PATH.providers_for(str(other))
                intersection = [s for s in lhs if any(s.intersects(z) for z in rhs)]
                return bool(intersection)

            # A provider can satisfy a virtual dependency.
            elif self_virtual or other_virtual:
                virtual_spec, non_virtual_spec = (self, other) if self_virtual else (other, self)
                try:
                    # Here we might get an abstract spec
                    pkg_cls = spack.repo.PATH.get_pkg_class(non_virtual_spec.fullname)
                    pkg = pkg_cls(non_virtual_spec)
                except spack.repo.UnknownEntityError:
                    # If we can't get package info on this spec, don't treat
                    # it as a provider of this vdep.
                    return False

                if pkg.provides(virtual_spec.name):
                    for when_spec, provided in pkg.provided.items():
                        if non_virtual_spec.intersects(when_spec, deps=False):
                            if any(vpkg.intersects(virtual_spec) for vpkg in provided):
                                return True
            return False

        # namespaces either match, or other doesn't require one.
        if (
            other.namespace is not None
            and self.namespace is not None
            and self.namespace != other.namespace
        ):
            return False

        if self.versions and other.versions:
            if not self.versions.intersects(other.versions):
                return False

        if not self.variants.intersects(other.variants):
            return False

        if self.architecture and other.architecture:
            if not self.architecture.intersects(other.architecture):
                return False

        if not self.compiler_flags.intersects(other.compiler_flags):
            return False

        # If we need to descend into dependencies, do it, otherwise we're done.
        if deps:
            return self._intersects_dependencies(other)

        return True

    def _intersects_dependencies(self, other):
        if not other._dependencies or not self._dependencies:
            # one spec *could* eventually satisfy the other
            return True

        # Handle first-order constraints directly
        common_dependencies = {x.name for x in self.dependencies()}
        common_dependencies &= {x.name for x in other.dependencies()}
        for name in common_dependencies:
            if not self[name].intersects(other[name], deps=True):
                return False

        # For virtual dependencies, we need to dig a little deeper.
        self_index = spack.provider_index.ProviderIndex(
            repository=spack.repo.PATH, specs=self.traverse(), restrict=True
        )
        other_index = spack.provider_index.ProviderIndex(
            repository=spack.repo.PATH, specs=other.traverse(), restrict=True
        )

        # These two loops handle cases where there is an overly restrictive
        # vpkg in one spec for a provider in the other (e.g., mpi@3: is not
        # compatible with mpich2)
        for spec in self.traverse():
            if (
                spack.repo.PATH.is_virtual(spec.name)
                and spec.name in other_index
                and not other_index.providers_for(spec)
            ):
                return False

        for spec in other.traverse():
            if (
                spack.repo.PATH.is_virtual(spec.name)
                and spec.name in self_index
                and not self_index.providers_for(spec)
            ):
                return False

        return True

    def satisfies(self, other: Union[str, "Spec"], deps: bool = True) -> bool:
        """Return True if all concrete specs matching self also match other, otherwise False.

        Args:
            other: spec to be satisfied
            deps: if True descend to dependencies, otherwise only check root node
        """
        other = self._autospec(other)

        if other.concrete:
            # The left-hand side must be the same singleton with identical hash. Notice that
            # package hashes can be different for otherwise indistinguishable concrete Spec
            # objects.
            return self.concrete and self.dag_hash() == other.dag_hash()

        # If the right-hand side has an abstract hash, make sure it's a prefix of the
        # left-hand side's (abstract) hash.
        if other.abstract_hash:
            compare_hash = self.dag_hash() if self.concrete else self.abstract_hash
            if not compare_hash or not compare_hash.startswith(other.abstract_hash):
                return False

        # If the names are different, we need to consider virtuals
        if self.name != other.name and self.name and other.name:
            # A concrete provider can satisfy a virtual dependency.
            if not spack.repo.PATH.is_virtual(self.name) and spack.repo.PATH.is_virtual(
                other.name
            ):
                try:
                    # Here we might get an abstract spec
                    pkg_cls = spack.repo.PATH.get_pkg_class(self.fullname)
                    pkg = pkg_cls(self)
                except spack.repo.UnknownEntityError:
                    # If we can't get package info on this spec, don't treat
                    # it as a provider of this vdep.
                    return False

                if pkg.provides(other.name):
                    for when_spec, provided in pkg.provided.items():
                        if self.satisfies(when_spec, deps=False):
                            if any(vpkg.intersects(other) for vpkg in provided):
                                return True
            return False

        # namespaces either match, or other doesn't require one.
        if (
            other.namespace is not None
            and self.namespace is not None
            and self.namespace != other.namespace
        ):
            return False

        if not self.versions.satisfies(other.versions):
            return False

        if not self.variants.satisfies(other.variants):
            return False

        if self.architecture and other.architecture:
            if not self.architecture.satisfies(other.architecture):
                return False
        elif other.architecture and not self.architecture:
            return False

        if not self.compiler_flags.satisfies(other.compiler_flags):
            return False

        # If we need to descend into dependencies, do it, otherwise we're done.
        if not deps:
            return True

        # If there are no constraints to satisfy, we're done.
        if not other._dependencies:
            return True

        # If we arrived here, the lhs root node satisfies the rhs root node. Now we need to check
        # all the edges that have an abstract parent, and verify that they match some edge in the
        # lhs.
        #
        # It might happen that the rhs brings in concrete sub-DAGs. For those we don't need to
        # verify the edge properties, cause everything is encoded in the hash of the nodes that
        # will be verified later.
        lhs_edges: Dict[str, Set[DependencySpec]] = collections.defaultdict(set)
        mock_nodes_from_old_specfiles = set()
        for rhs_edge in other.traverse_edges(root=False, cover="edges"):
            # The condition cannot be applied in any case, skip the edge
            test_root = rhs_edge.parent.name in (None, self.name)
            if test_root and not self.intersects(rhs_edge.when):
                continue

            if (
                not test_root
                and rhs_edge.parent.name in self
                and not self[rhs_edge.parent.name].intersects(rhs_edge.when)
            ):
                continue

            # If we are checking for ^mpi we need to verify if there is any edge
            is_virtual_node = spack.repo.PATH.is_virtual(rhs_edge.spec.name)
            if is_virtual_node:
                # Don't mutate objects in memory that may be referred elsewhere
                rhs_edge = rhs_edge.copy()
                rhs_edge.update_virtuals(virtuals=(rhs_edge.spec.name,))

            if rhs_edge.direct:
                # Note: this relies on abstract specs from string not being deeper than 2 levels
                # e.g. in foo %fee ^bar %baz we cannot go deeper than "baz" and e.g. specify its
                # dependencies too.
                #
                # We also need to account for cases like gcc@<new> %gcc@<old> where the parent
                # name is the same as the child name
                #
                # The same assumptions hold on Spec.constrain, and Spec.intersect
                current_node = self
                if rhs_edge.parent.name is not None and rhs_edge.parent.name != rhs_edge.spec.name:
                    try:
                        current_node = self[rhs_edge.parent.name]
                    except KeyError:
                        return False

                if current_node.original_spec_format() < 5 or (
                    current_node.original_spec_format() >= 5 and current_node.external
                ):
                    compiler_spec = current_node.annotations.compiler_node_attribute
                    if compiler_spec is None:
                        return False

                    mock_nodes_from_old_specfiles.add(compiler_spec)
                    # This checks that the single node compiler spec satisfies the request
                    # of a direct dependency. The check is not perfect, but based on heuristic.
                    if not compiler_spec.satisfies(rhs_edge.spec):
                        return False

                else:
                    name = rhs_edge.spec.name if not is_virtual_node else None
                    candidate_edges = current_node.edges_to_dependencies(
                        name=name, virtuals=rhs_edge.virtuals or None
                    )
                    # Select at least the deptypes on the rhs_edge, and conditional edges that
                    # constrain a bigger portion of the search space (so it's rhs.when <= lhs.when)
                    candidates = [
                        lhs_edge.spec
                        for lhs_edge in candidate_edges
                        if ((lhs_edge.depflag & rhs_edge.depflag) ^ rhs_edge.depflag) == 0
                        and rhs_edge.when.satisfies(lhs_edge.when)
                    ]
                    if not candidates or not any(x.satisfies(rhs_edge.spec) for x in candidates):
                        return False

                continue

            # Skip edges from a concrete sub-DAG
            if rhs_edge.parent.concrete:
                continue

            if not lhs_edges:
                # Construct a map of the link/run subDAG + direct "build" edges,
                # keyed by dependency name
                for lhs_edge in self.traverse_edges(
                    root=False, cover="edges", deptype=("link", "run")
                ):
                    lhs_edges[lhs_edge.spec.name].add(lhs_edge)
                    for virtual_name in lhs_edge.virtuals:
                        lhs_edges[virtual_name].add(lhs_edge)

                build_edges = self.edges_to_dependencies(depflag=dt.BUILD)
                for lhs_edge in build_edges:
                    lhs_edges[lhs_edge.spec.name].add(lhs_edge)
                    for virtual_name in lhs_edge.virtuals:
                        lhs_edges[virtual_name].add(lhs_edge)

            # We don't have edges to this dependency
            current_dependency_name = rhs_edge.spec.name
            if current_dependency_name is not None and current_dependency_name not in lhs_edges:
                return False

            if current_dependency_name is None:
                # Here we have an anonymous spec e.g. ^ dev_path=*
                candidate_edges = list(itertools.chain(*lhs_edges.values()))

            else:
                candidate_edges = [
                    lhs_edge
                    for lhs_edge in lhs_edges[current_dependency_name]
                    if rhs_edge.when.satisfies(lhs_edge.when)
                ]

            if not candidate_edges:
                return False

            for virtual in rhs_edge.virtuals:
                has_virtual = any(virtual in edge.virtuals for edge in candidate_edges)
                if not has_virtual:
                    return False

            for lhs_edge in candidate_edges:
                if lhs_edge.spec.satisfies(rhs_edge.spec, deps=False):
                    break
            else:
                return False

        return True

    @property  # type: ignore[misc] # decorated prop not supported in mypy
    def patches(self):
        """Return patch objects for any patch sha256 sums on this Spec.

        This is for use after concretization to iterate over any patches
        associated with this spec.

        TODO: this only checks in the package; it doesn't resurrect old
        patches from install directories, but it probably should.
        """
        if not hasattr(self, "_patches"):
            self._patches = []

            # translate patch sha256sums to patch objects by consulting the index
            if self._patches_assigned():
                for sha256 in self.variants["patches"]._patches_in_order_of_appearance:
                    index = spack.repo.PATH.patch_index
                    pkg_cls = spack.repo.PATH.get_pkg_class(self.name)
                    try:
                        patch = index.patch_for_package(sha256, pkg_cls)
                    except spack.error.PatchLookupError as e:
                        raise spack.error.SpecError(
                            f"{e}. This usually means the patch was modified or removed. "
                            "To fix this, either reconcretize or use the original package "
                            "repository"
                        ) from e

                    self._patches.append(patch)

        return self._patches

    def _dup(self, other: "Spec", deps: Union[bool, dt.DepTypes, dt.DepFlag] = True) -> bool:
        """Copies "other" into self, by overwriting all attributes.

        Args:
            other: spec to be copied onto ``self``
            deps: if True copies all the dependencies. If False copies None.
                If deptype, or depflag, copy matching types.

        Returns:
            True if ``self`` changed because of the copy operation, False otherwise.
        """
        # We don't count dependencies as changes here
        changed = True
        if hasattr(self, "name"):
            changed = (
                self.name != other.name
                and self.versions != other.versions
                and self.architecture != other.architecture
                and self.variants != other.variants
                and self.concrete != other.concrete
                and self.external_path != other.external_path
                and self.external_modules != other.external_modules
                and self.compiler_flags != other.compiler_flags
                and self.abstract_hash != other.abstract_hash
            )

        self._package = None

        # Local node attributes get copied first.
        self.name = other.name
        self.versions = other.versions.copy()
        self.architecture = other.architecture.copy() if other.architecture else None
        self.compiler_flags = other.compiler_flags.copy()
        self.compiler_flags.spec = self
        self.variants = other.variants.copy()
        self._build_spec = other._build_spec

        # Clear dependencies
        self._dependents = _EdgeMap(store_by_child=False)
        self._dependencies = _EdgeMap(store_by_child=True)

        # FIXME: we manage _patches_in_order_of_appearance specially here
        # to keep it from leaking out of spec.py, but we should figure
        # out how to handle it more elegantly in the Variant classes.
        for k, v in other.variants.items():
            patches = getattr(v, "_patches_in_order_of_appearance", None)
            if patches:
                self.variants[k]._patches_in_order_of_appearance = patches

        self.variants.spec = self
        self.external_path = other.external_path
        self.external_modules = other.external_modules
        self.extra_attributes = other.extra_attributes
        self.namespace = other.namespace
        self.annotations = other.annotations

        # If we copy dependencies, preserve DAG structure in the new spec
        if deps:
            # If caller restricted deptypes to be copied, adjust that here.
            # By default, just copy all deptypes
            depflag = dt.ALL
            if isinstance(deps, (tuple, list, str)):
                depflag = dt.canonicalize(deps)
            self._dup_deps(other, depflag)

        self._prefix = other._prefix
        self._concrete = other._concrete

        self.abstract_hash = other.abstract_hash

        if self._concrete:
            self._dunder_hash = other._dunder_hash
            for h in ht.HASHES:
                setattr(self, h.attr, getattr(other, h.attr, None))
        else:
            self._dunder_hash = None
            for h in ht.HASHES:
                setattr(self, h.attr, None)

        return changed

    def _dup_deps(self, other, depflag: dt.DepFlag):
        def spid(spec):
            return id(spec)

        new_specs = {spid(other): self}
        for edge in other.traverse_edges(cover="edges", root=False):
            if edge.depflag and not depflag & edge.depflag:
                continue

            if spid(edge.parent) not in new_specs:
                new_specs[spid(edge.parent)] = edge.parent.copy(deps=False)

            if spid(edge.spec) not in new_specs:
                new_specs[spid(edge.spec)] = edge.spec.copy(deps=False)

            new_specs[spid(edge.parent)].add_dependency_edge(
                new_specs[spid(edge.spec)],
                depflag=edge.depflag,
                virtuals=edge.virtuals,
                direct=edge.direct,
                when=edge.when,
            )

    def copy(self, deps: Union[bool, dt.DepTypes, dt.DepFlag] = True, **kwargs):
        """Make a copy of this spec.

        Args:
            deps: Defaults to True. If boolean, controls
                whether dependencies are copied (copied if True). If a
                DepTypes or DepFlag is provided, *only* matching dependencies are copied.
            kwargs: additional arguments for internal use (passed to ``_dup``).

        Returns:
            A copy of this spec.

        Examples:
            Deep copy with dependencies::

                spec.copy()
                spec.copy(deps=True)

            Shallow copy (no dependencies)::

                spec.copy(deps=False)

            Only build and run dependencies::

                deps=('build', 'run'):

        """
        clone = Spec.__new__(Spec)
        clone._dup(self, deps=deps, **kwargs)
        return clone

    @property
    def version(self):
        if not self.versions.concrete:
            raise spack.error.SpecError("Spec version is not concrete: " + str(self))
        return self.versions[0]

    def __getitem__(self, name: str):
        """Get a dependency from the spec by its name. This call implicitly
        sets a query state in the package being retrieved. The behavior of
        packages may be influenced by additional query parameters that are
        passed after a colon symbol.

        Note that if a virtual package is queried a copy of the Spec is
        returned while for non-virtual a reference is returned.
        """
        query_parameters: List[str] = name.split(":")
        if len(query_parameters) > 2:
            raise KeyError("key has more than one ':' symbol. At most one is admitted.")

        name, query_parameters = query_parameters[0], query_parameters[1:]
        if query_parameters:
            # We have extra query parameters, which are comma separated
            # values
            csv = query_parameters.pop().strip()
            query_parameters = re.split(r"\s*,\s*", csv)

        # Consider all direct dependencies and transitive runtime dependencies
        order = itertools.chain(
            self.edges_to_dependencies(depflag=dt.BUILD | dt.TEST),
            self.traverse_edges(deptype=dt.LINK | dt.RUN, order="breadth", cover="edges"),
        )

        try:
            edge = next((e for e in order if e.spec.name == name or name in e.virtuals))
        except StopIteration as e:
            raise KeyError(f"No spec with name {name} in {self}") from e

        if self._concrete:
            return SpecBuildInterface(
                edge.spec, name, query_parameters, _parent=self, is_virtual=name in edge.virtuals
            )

        return edge.spec

    def __contains__(self, spec):
        """True if this spec or some dependency satisfies the spec.

        Note: If ``spec`` is anonymous, we ONLY check whether the root
        satisfies it, NOT dependencies.  This is because most anonymous
        specs (e.g., ``@1.2``) don't make sense when applied across an
        entire DAG -- we limit them to the root.

        """
        spec = self._autospec(spec)

        # if anonymous or same name, we only have to look at the root
        if not spec.name or spec.name == self.name:
            return self.satisfies(spec)
        try:
            dep = self[spec.name]
        except KeyError:
            return False
        return dep.satisfies(spec)

    def eq_dag(self, other, deptypes=True, vs=None, vo=None):
        """True if the full dependency DAGs of specs are equal."""
        if vs is None:
            vs = set()
        if vo is None:
            vo = set()

        vs.add(id(self))
        vo.add(id(other))

        if not self.eq_node(other):
            return False

        if len(self._dependencies) != len(other._dependencies):
            return False

        ssorted = [self._dependencies[name] for name in sorted(self._dependencies)]
        osorted = [other._dependencies[name] for name in sorted(other._dependencies)]
        for s_dspec, o_dspec in zip(
            itertools.chain.from_iterable(ssorted), itertools.chain.from_iterable(osorted)
        ):
            if deptypes and s_dspec.depflag != o_dspec.depflag:
                return False

            s, o = s_dspec.spec, o_dspec.spec
            visited_s = id(s) in vs
            visited_o = id(o) in vo

            # Check for duplicate or non-equal dependencies
            if visited_s != visited_o:
                return False

            # Skip visited nodes
            if visited_s or visited_o:
                continue

            # Recursive check for equality
            if not s.eq_dag(o, deptypes, vs, vo):
                return False

        return True

    def _cmp_node(self):
        """Yield comparable elements of just *this node* and not its deps."""
        yield self.name
        yield self.namespace
        yield self.versions
        yield self.variants
        yield self.compiler_flags
        yield self.architecture
        yield self.abstract_hash

        # this is not present on older specs
        yield getattr(self, "_package_hash", None)

    def eq_node(self, other):
        """Equality with another spec, not including dependencies."""
        return (other is not None) and lang.lazy_eq(self._cmp_node, other._cmp_node)

    def _cmp_fast_eq(self, other) -> Optional[bool]:
        """Short-circuit compare with other for equality, for lazy_lexicographic_ordering."""
        # If there is ever a breaking change to hash computation, whether accidental or purposeful,
        # two specs can be identical modulo DAG hash, depending on what time they were concretized
        # From the perspective of many operation in Spack (database, build cache, etc) a different
        # DAG hash means a different spec. Here we ensure that two otherwise identical specs, one
        # serialized before the hash change and one after, are considered different.
        if self is other:
            return True

        if self.concrete and other and other.concrete:
            return self.dag_hash() == other.dag_hash()

        return None

    def _cmp_iter(self):
        """Lazily yield components of self for comparison."""

        # Spec comparison in Spack needs to be fast, so there are several cases here for
        # performance. The main places we care about this are:
        #
        #   * Abstract specs: there are lots of abstract specs in package.py files,
        #     which are put into metadata dictionaries and sorted during concretization
        #     setup. We want comparing abstract specs to be fast.
        #
        #   * Concrete specs: concrete specs are bigger and have lots of nodes and
        #     edges. Because of the graph complexity, we need a full, linear time
        #     traversal to compare them -- that's pretty much is unavoidable. But they
        #     also have precoputed cryptographic hashes (dag_hash()), which we can use
        #     to do fast equality comparison. See _cmp_fast_eq() above for the
        #     short-circuit logic for hashes.
        #
        # A full traversal involves constructing data structurs, visitor objects, etc.,
        # and it can be expensive if we have to do it to compare a bunch of tiny
        # abstract specs. Therefore, there are 3 cases below, which avoid calling
        # `spack.traverse.traverse_edges()` unless necessary.
        #
        # WARNING: the cases below need to be consistent, so don't mess with this code
        # unless you really know what you're doing. Be sure to keep all three consistent.
        #
        # All cases lazily yield:
        #
        #   1. A generator over nodes
        #   2. A generator over canonical edges
        #
        # Canonical edges have consistent ids defined by breadth-first traversal order. That is,
        # the root is always 0, dependencies of the root are 1, 2, 3, etc., and so on.
        #
        # The three cases are:
        #
        #   1. Spec has no dependencies
        #      * We can avoid any traversal logic and just yield this node's _cmp_node generator.
        #
        #   2. Spec has dependencies, but dependencies have no dependencies.
        #      * We need to sort edges, but we don't need to track visited nodes, which
        #        can save us the cost of setting up all the tracking data structures
        #        `spack.traverse` uses.
        #
        #   3. Spec has dependencies that have dependencies.
        #      * In this case, the spec is *probably* concrete. Equality comparisons
        #        will be short-circuited by dag_hash(), but other comparisons will need
        #        to lazily enumerate components of the spec. The traversal logic is
        #        unavoidable.
        #
        # TODO: consider reworking `spack.traverse` to construct fewer data structures
        # and objects, as this would make all traversals faster and could eliminate the
        # need for the complexity here. It was not clear at the time of writing that how
        # much optimization was possible in `spack.traverse`.

        sorted_l1_edges = None
        edge_list = None
        node_ids = None

        def nodes():
            nonlocal sorted_l1_edges
            nonlocal edge_list
            nonlocal node_ids

            # Level 0: root node
            yield self._cmp_node  # always yield the root (this node)
            if not self._dependencies:  # done if there are no dependencies
                return

            # Level 1: direct dependencies
            # we can yield these in sorted order without tracking visited nodes
            deps_have_deps = False
            sorted_l1_edges = self.edges_to_dependencies(depflag=dt.ALL)
            if len(sorted_l1_edges) > 1:
                sorted_l1_edges = spack.traverse.sort_edges(sorted_l1_edges)

            for edge in sorted_l1_edges:
                yield edge.spec._cmp_node
                if edge.spec._dependencies:
                    deps_have_deps = True

            if not deps_have_deps:  # done if level 1 specs have no dependencies
                return

            # Level 2: dependencies of direct dependencies
            # now it's general; we need full traverse() to track visited nodes
            l1_specs = [edge.spec for edge in sorted_l1_edges]

            # the node_ids dict generates consistent ids based on BFS traversal order
            # these are used to identify edges later
            node_ids = collections.defaultdict(lambda: len(node_ids))
            node_ids[id(self)]  # self is 0
            for spec in l1_specs:
                node_ids[id(spec)]  # l1 starts at 1

            edge_list = []
            for edge in spack.traverse.traverse_edges(
                l1_specs, order="breadth", cover="edges", root=False, visited=set([0])
            ):
                # yield each node only once, and generate a consistent id for it the
                # first time it's encountered.
                if id(edge.spec) not in node_ids:
                    yield edge.spec._cmp_node
                    node_ids[id(edge.spec)]

                if edge.parent is None:  # skip fake edge to root
                    continue

                edge_list.append(
                    (
                        node_ids[id(edge.parent)],
                        node_ids[id(edge.spec)],
                        edge.depflag,
                        edge.virtuals,
                        edge.direct,
                        edge.when,
                    )
                )

        def edges():
            # no edges in single-node graph
            if not self._dependencies:
                return

            # level 1 edges all start with zero
            for i, edge in enumerate(sorted_l1_edges, start=1):
                yield (0, i, edge.depflag, edge.virtuals, edge.direct, edge.when)

            # yield remaining edges in the order they were encountered during traversal
            if edge_list:
                yield from edge_list

        yield nodes
        yield edges

    @property
    def namespace_if_anonymous(self):
        return self.namespace if not self.name else None

    def format(self, format_string: str = DEFAULT_FORMAT, color: Optional[bool] = False) -> str:
        r"""Prints out attributes of a spec according to a format string.

        Using an ``{attribute}`` format specifier, any field of the spec can be
        selected. Those attributes can be recursive. For example,
        ``s.format({compiler.version})`` will print the version of the compiler.

        If the attribute in a format specifier evaluates to ``None``, then the format
        specifier will evaluate to the empty string, ``""``.

        Commonly used attributes of the Spec for format strings include::

            name
            version
            compiler_flags
            compilers
            variants
            architecture
            architecture.platform
            architecture.os
            architecture.target
            prefix
            namespace

        Some additional special-case properties can be added::

            hash[:len]    The DAG hash with optional length argument
            spack_root    The spack root directory
            spack_install The spack install directory

        The ``^`` sigil can be used to access dependencies by name.
        ``s.format({^mpi.name})`` will print the name of the MPI implementation in the
        spec.

        The ``@``, ``%``, and ``/`` sigils can be used to include the sigil with the
        printed string. These sigils may only be used with the appropriate attributes,
        listed below::

            @        ``{@version}``, ``{@compiler.version}``
            %        ``{%compiler}``, ``{%compiler.name}``
            /        ``{/hash}``, ``{/hash:7}``, etc

        The ``@`` sigil may also be used for any other property named ``version``.
        Sigils printed with the attribute string are only printed if the attribute
        string is non-empty, and are colored according to the color of the attribute.

        Variants listed by name naturally print with their sigil. For example,
        ``spec.format('{variants.debug}')`` prints either ``+debug`` or ``~debug``
        depending on the name of the variant. Non-boolean variants print as
        ``name=value``. To print variant names or values independently, use
        ``spec.format('{variants.<name>.name}')`` or
        ``spec.format('{variants.<name>.value}')``.

        There are a few attributes on specs that can be specified as key-value pairs
        that are *not* variants, e.g.: ``os``, ``arch``, ``architecture``, ``target``,
        ``namespace``, etc. You can format these with an optional ``key=`` prefix, e.g.
        ``{namespace=namespace}`` or ``{arch=architecture}``, etc. The ``key=`` prefix
        will be colorized along with the value.

        When formatting specs, key-value pairs are separated from preceding parts of the
        spec by whitespace. To avoid printing extra whitespace when the formatted
        attribute is not set, you can add whitespace to the key *inside* the braces of
        the format string, e.g.:

            { namespace=namespace}

        This evaluates to `` namespace=builtin`` if ``namespace`` is set to ``builtin``,
        and to ``""`` if ``namespace`` is ``None``.

        Spec format strings use ``\`` as the escape character. Use ``\{`` and ``\}`` for
        literal braces, and ``\\`` for the literal ``\`` character.

        Args:
            format_string: string containing the format to be expanded
            color: True for colorized result; False for no color; None for auto color.

        """
        ensure_modern_format_string(format_string)

        def safe_color(sigil: str, string: str, color_fmt: Optional[str]) -> str:
            # avoid colorizing if there is no color or the string is empty
            if (color is False) or not color_fmt or not string:
                return sigil + string
            # escape and add the sigil here to avoid multiple concatenations
            if sigil == "@":
                sigil = "@@"
            return clr.colorize(f"{color_fmt}{sigil}{clr.cescape(string)}@.", color=color)

        def format_attribute(match_object: Match) -> str:
            (esc, sig, dep, hash, hash_len, attribute, close_brace, unmatched_close_brace) = (
                match_object.groups()
            )
            if esc:
                return esc
            elif unmatched_close_brace:
                raise SpecFormatStringError(f"Unmatched close brace: '{format_string}'")
            elif not close_brace:
                raise SpecFormatStringError(f"Missing close brace: '{format_string}'")

            current = self if dep is None else self[dep]

            # Hash attributes can return early.
            # NOTE: we currently treat abstract_hash like an attribute and ignore
            # any length associated with it. We may want to change that.
            if hash:
                if sig and sig != "/":
                    raise SpecFormatSigilError(sig, "DAG hashes", hash)
                try:
                    length = int(hash_len) if hash_len else None
                except ValueError:
                    raise SpecFormatStringError(f"Invalid hash length: '{hash_len}'")
                return safe_color(sig or "", current.dag_hash(length), HASH_COLOR)

            if attribute == "":
                raise SpecFormatStringError("Format string attributes must be non-empty")

            attribute = attribute.lower()
            parts = attribute.split(".")
            assert parts

            # check that the sigil is valid for the attribute.
            if not sig:
                sig = ""
            elif sig == "@" and parts[-1] not in ("versions", "version"):
                raise SpecFormatSigilError(sig, "versions", attribute)
            elif sig == "%" and attribute not in ("compiler", "compiler.name"):
                raise SpecFormatSigilError(sig, "compilers", attribute)
            elif sig == "/" and attribute != "abstract_hash":
                raise SpecFormatSigilError(sig, "DAG hashes", attribute)

            # Iterate over components using getattr to get next element
            for idx, part in enumerate(parts):
                if not part:
                    raise SpecFormatStringError("Format string attributes must be non-empty")
                elif part.startswith("_"):
                    raise SpecFormatStringError("Attempted to format private attribute")
                elif isinstance(current, VariantMap):
                    # subscript instead of getattr for variant names
                    try:
                        current = current[part]
                    except KeyError:
                        raise SpecFormatStringError(f"Variant '{part}' does not exist")
                else:
                    # aliases
                    if part == "arch":
                        part = "architecture"
                    elif part == "version" and not current.versions.concrete:
                        # version (singular) requires a concrete versions list. Avoid
                        # pedantic errors by using versions (plural) when not concrete.
                        # These two are not entirely equivalent for pkg@=1.2.3:
                        # - version prints '1.2.3'
                        # - versions prints '=1.2.3'
                        part = "versions"
                    try:
                        current = getattr(current, part)
                    except AttributeError:
                        if part == "compiler":
                            return "none"
                        elif part == "specfile_version":
                            return f"v{current.original_spec_format()}"

                        raise SpecFormatStringError(
                            f"Attempted to format attribute {attribute}. "
                            f"Spec {'.'.join(parts[:idx])} has no attribute {part}"
                        )
                    if isinstance(current, vn.VersionList) and current == vn.any_version:
                        # don't print empty version lists
                        return ""

                if callable(current):
                    raise SpecFormatStringError("Attempted to format callable object")

                if current is None:
                    # not printing anything
                    return ""

            # Set color codes for various attributes
            color = None
            if "architecture" in parts:
                color = ARCHITECTURE_COLOR
            elif "variants" in parts or sig.endswith("="):
                color = VARIANT_COLOR
            elif any(c in parts for c in ("compiler", "compilers", "compiler_flags")):
                color = COMPILER_COLOR
            elif "version" in parts or "versions" in parts:
                color = VERSION_COLOR

            # return empty string if the value of the attribute is None.
            if current is None:
                return ""

            # return colored output
            return safe_color(sig, str(current), color)

        return SPEC_FORMAT_RE.sub(format_attribute, format_string).strip()

    def cformat(self, *args, **kwargs):
        """Same as format, but color defaults to auto instead of False."""
        kwargs = kwargs.copy()
        kwargs.setdefault("color", None)
        return self.format(*args, **kwargs)

    @property
    def spack_root(self):
        """Special field for using ``{spack_root}`` in Spec.format()."""
        return spack.paths.spack_root

    @property
    def spack_install(self):
        """Special field for using ``{spack_install}`` in Spec.format()."""
        return spack.store.STORE.layout.root

    def format_path(
        # self, format_string: str, _path_ctor: Optional[pathlib.PurePath] = None
        self,
        format_string: str,
        _path_ctor: Optional[Callable[[Any], pathlib.PurePath]] = None,
    ) -> str:
        """Given a `format_string` that is intended as a path, generate a string
        like from `Spec.format`, but eliminate extra path separators introduced by
        formatting of Spec properties.

        Path separators explicitly added to the string are preserved, so for example
        "{name}/{version}" would generate a directory based on the Spec's name, and
        a subdirectory based on its version; this function guarantees though that
        the resulting string would only have two directories (i.e. that if under
        normal circumstances that `str(Spec.version)` would contain a path
        separator, it would not in this case).
        """
        format_component_with_sep = r"\{[^}]*[/\\][^}]*}"
        if re.search(format_component_with_sep, format_string):
            raise SpecFormatPathError(
                f"Invalid path format string: cannot contain {{/...}}\n\t{format_string}"
            )

        path_ctor = _path_ctor or pathlib.PurePath
        format_string_as_path = path_ctor(format_string)
        if format_string_as_path.is_absolute() or (
            # Paths that begin with a single "\" on windows are relative, but we still
            # want to preserve the initial "\\" to be consistent with PureWindowsPath.
            # Ensure that this '\' is not passed to polite_filename() so it's not converted to '_'
            (os.name == "nt" or path_ctor == pathlib.PureWindowsPath)
            and format_string_as_path.parts[0] == "\\"
        ):
            output_path_components = [format_string_as_path.parts[0]]
            input_path_components = list(format_string_as_path.parts[1:])
        else:
            output_path_components = []
            input_path_components = list(format_string_as_path.parts)

        output_path_components += [
            fs.polite_filename(self.format(part)) for part in input_path_components
        ]
        return str(path_ctor(*output_path_components))

    def __str__(self):
        if self._concrete:
            return self.format("{name}{@version}{/hash}")

        if not self._dependencies:
            return self.format()

        return self.long_spec

    @property
    def colored_str(self):
        root_str = [self.cformat()]
        sorted_dependencies = sorted(
            self.traverse(root=False), key=lambda x: (x.name, x.abstract_hash)
        )
        sorted_dependencies = [
            d.cformat("{edge_attributes} " + DISPLAY_FORMAT) for d in sorted_dependencies
        ]
        spec_str = " ^".join(root_str + sorted_dependencies)
        return spec_str.strip()

    def install_status(self) -> InstallStatus:
        """Helper for tree to print DB install status."""
        if not self.concrete:
            return InstallStatus.absent

        if self.external:
            return InstallStatus.external

        upstream, record = spack.store.STORE.db.query_by_spec_hash(self.dag_hash())
        if not record:
            return InstallStatus.absent
        elif upstream and record.installed:
            return InstallStatus.upstream
        elif record.installed:
            return InstallStatus.installed
        else:
            return InstallStatus.missing

    def _installed_explicitly(self):
        """Helper for tree to print DB install status."""
        if not self.concrete:
            return None
        try:
            record = spack.store.STORE.db.get_record(self)
            return record.explicit
        except KeyError:
            return None

    def tree(
        self,
        *,
        color: Optional[bool] = None,
        depth: bool = False,
        hashes: bool = False,
        hashlen: Optional[int] = None,
        cover: spack.traverse.CoverType = "nodes",
        indent: int = 0,
        format: str = DEFAULT_FORMAT,
        deptypes: Union[dt.DepTypes, dt.DepFlag] = dt.ALL,
        show_types: bool = False,
        depth_first: bool = False,
        recurse_dependencies: bool = True,
        status_fn: Optional[Callable[["Spec"], InstallStatus]] = None,
        prefix: Optional[Callable[["Spec"], str]] = None,
        key=id,
    ) -> str:
        """Prints out this spec and its dependencies, tree-formatted with indentation.

        See multi-spec ``spack.spec.tree()`` function for details.

        Args:
            specs: List of specs to format.
            color: if True, always colorize the tree. If False, don't colorize the tree. If None,
                use the default from llnl.tty.color
            depth: print the depth from the root
            hashes: if True, print the hash of each node
            hashlen: length of the hash to be printed
            cover: either "nodes" or "edges"
            indent: extra indentation for the tree being printed
            format: format to be used to print each node
            deptypes: dependency types to be represented in the tree
            show_types: if True, show the (merged) dependency type of a node
            depth_first: if True, traverse the DAG depth first when representing it as a tree
            recurse_dependencies: if True, recurse on dependencies
            status_fn: optional callable that takes a node as an argument and return its
                installation status
            prefix: optional callable that takes a node as an argument and return its
                installation prefix
        """
        return tree(
            [self],
            color=color,
            depth=depth,
            hashes=hashes,
            hashlen=hashlen,
            cover=cover,
            indent=indent,
            format=format,
            deptypes=deptypes,
            show_types=show_types,
            depth_first=depth_first,
            recurse_dependencies=recurse_dependencies,
            status_fn=status_fn,
            prefix=prefix,
            key=key,
        )

    def __repr__(self):
        return str(self)

    @property
    def platform(self):
        return self.architecture.platform

    @property
    def os(self):
        return self.architecture.os

    @property
    def target(self):
        return self.architecture.target

    @property
    def build_spec(self):
        return self._build_spec or self

    @build_spec.setter
    def build_spec(self, value):
        self._build_spec = value

    def trim(self, dep_name):
        """
        Remove any package that is or provides `dep_name` transitively
        from this tree. This can also remove other dependencies if
        they are only present because of `dep_name`.
        """
        for spec in list(self.traverse()):
            new_dependencies = _EdgeMap()  # A new _EdgeMap
            for pkg_name, edge_list in spec._dependencies.items():
                for edge in edge_list:
                    if (dep_name not in edge.virtuals) and (not dep_name == edge.spec.name):
                        new_dependencies.add(edge)
            spec._dependencies = new_dependencies

    def _virtuals_provided(self, root):
        """Return set of virtuals provided by self in the context of root"""
        if root is self:
            # Could be using any virtual the package can provide
            return set(v.name for v in self.package.virtuals_provided)

        hashes = [s.dag_hash() for s in root.traverse()]
        in_edges = set(
            [edge for edge in self.edges_from_dependents() if edge.parent.dag_hash() in hashes]
        )
        return set().union(*[edge.virtuals for edge in in_edges])

    def _splice_match(self, other, self_root, other_root):
        """Return True if other is a match for self in a splice of other_root into self_root

        Other is a splice match for self if it shares a name, or if self is a virtual provider
        and other provides a superset of the virtuals provided by self. Virtuals provided are
        evaluated in the context of a root spec (self_root for self, other_root for other).

        This is a slight oversimplification. Other could be a match for self in the context of
        one edge in self_root and not in the context of another edge. This method could be
        expanded in the future to account for these cases.
        """
        if other.name == self.name:
            return True

        return bool(
            bool(self._virtuals_provided(self_root))
            and self._virtuals_provided(self_root) <= other._virtuals_provided(other_root)
        )

    def _splice_detach_and_add_dependents(self, replacement, context):
        """Helper method for Spec._splice_helper.

        replacement is a node to splice in, context is the scope of dependents to consider relevant
        to this splice."""
        # Update build_spec attributes for all transitive dependents
        # before we start changing their dependencies
        ancestors_in_context = [
            a
            for a in self.traverse(root=False, direction="parents")
            if a in context.traverse(deptype=dt.LINK | dt.RUN)
        ]
        for ancestor in ancestors_in_context:
            # Only set it if it hasn't been spliced before
            ancestor._build_spec = ancestor._build_spec or ancestor.copy()
            ancestor.clear_caches(ignore=(ht.package_hash.attr,))
            for edge in ancestor.edges_to_dependencies(depflag=dt.BUILD):
                if edge.depflag & ~dt.BUILD:
                    edge.depflag &= ~dt.BUILD
                else:
                    ancestor._dependencies[edge.spec.name].remove(edge)
                    edge.spec._dependents[ancestor.name].remove(edge)

        # For each direct dependent in the link/run graph, replace the dependency on
        # node with one on replacement
        for edge in self.edges_from_dependents():
            if edge.parent not in ancestors_in_context:
                continue

            edge.parent._dependencies.edges[self.name].remove(edge)
            self._dependents.edges[edge.parent.name].remove(edge)
            edge.parent._add_dependency(replacement, depflag=edge.depflag, virtuals=edge.virtuals)

    def _splice_helper(self, replacement):
        """Main loop of a transitive splice.

        The while loop around a traversal of self ensures that changes to self from previous
        iterations are reflected in the traversal. This avoids evaluating irrelevant nodes
        using topological traversal (all incoming edges traversed before any outgoing edge).
        If any node will not be in the end result, its parent will be spliced and it will not
        ever be considered.
        For each node in self, find any analogous node in replacement and swap it in.
        We assume all build deps are handled outside of this method

        Arguments:
            replacement: The node that will replace any equivalent node in self
            self_root: The root of the spec that self comes from. This provides the context for
                evaluating whether ``replacement`` is a match for each node of ``self``. See
                ``Spec._splice_match`` and ``Spec._virtuals_provided`` for details.
            other_root: The root of the spec that replacement comes from. This provides the context
                for evaluating whether ``replacement`` is a match for each node of ``self``. See
                ``Spec._splice_match`` and ``Spec._virtuals_provided`` for details.
        """
        ids = set(id(s) for s in replacement.traverse())

        # Sort all possible replacements by name and virtual for easy access later
        replacements_by_name = collections.defaultdict(list)
        for node in replacement.traverse():
            replacements_by_name[node.name].append(node)
            virtuals = node._virtuals_provided(root=replacement)
            for virtual in virtuals:
                replacements_by_name[virtual].append(node)

        changed = True
        while changed:
            changed = False

            # Intentionally allowing traversal to change on each iteration
            # using breadth-first traversal to ensure we only reach nodes that will
            # be in final result
            for node in self.traverse(root=False, order="topo", deptype=dt.ALL & ~dt.BUILD):
                # If this node has already been swapped in, don't consider it again
                if id(node) in ids:
                    continue

                analogs = replacements_by_name[node.name]
                if not analogs:
                    # If we have to check for matching virtuals, then we need to check that it
                    # matches all virtuals. Use `_splice_match` to validate possible matches
                    for virtual in node._virtuals_provided(root=self):
                        analogs += [
                            r
                            for r in replacements_by_name[virtual]
                            if node._splice_match(r, self_root=self, other_root=replacement)
                        ]

                    # No match, keep iterating over self
                    if not analogs:
                        continue

                # If there are multiple analogs, this package must satisfy the constraint
                # that a newer version can always replace a lesser version.
                analog = max(analogs, key=lambda s: s.version)

                # No splice needed here, keep checking
                if analog == node:
                    continue

                node._splice_detach_and_add_dependents(analog, context=self)
                changed = True
                break

    def splice(self, other: "Spec", transitive: bool = True) -> "Spec":
        """Returns a new, spliced concrete Spec with the "other" dependency and,
        optionally, its dependencies.

        Args:
            other: alternate dependency
            transitive: include other's dependencies

        Returns: a concrete, spliced version of the current Spec

        When transitive is "True", use the dependencies from "other" to reconcile
        conflicting dependencies. When transitive is "False", use dependencies from self.

        For example, suppose we have the following dependency graph:

            T
            | \
            Z<-H

        Spec T depends on H and Z, and H also depends on Z. Now we want to use
        a different H, called H'. This function can be used to splice in H' to
        create a new spec, called T*. If H' was built with Z', then transitive
        "True" will ensure H' and T* both depend on Z':

            T*
            | \
            Z'<-H'

        If transitive is "False", then H' and T* will both depend on
        the original Z, resulting in a new H'*

            T*
            | \
            Z<-H'*

        Provenance of the build is tracked through the "build_spec" property
        of the spliced spec and any correspondingly modified dependency specs.
        The build specs are set to that of the original spec, so the original
        spec's provenance is preserved unchanged."""
        assert self.concrete
        assert other.concrete

        if self._splice_match(other, self_root=self, other_root=other):
            return other.copy()

        if not any(
            node._splice_match(other, self_root=self, other_root=other)
            for node in self.traverse(root=False, deptype=dt.LINK | dt.RUN)
        ):
            other_str = other.format("{name}/{hash:7}")
            self_str = self.format("{name}/{hash:7}")
            msg = f"Cannot splice {other_str} into {self_str}."
            msg += f" Either {self_str} cannot depend on {other_str},"
            msg += f" or {other_str} fails to provide a virtual used in {self_str}"
            raise SpliceError(msg)

        # Copies of all non-build deps, build deps will get added at the end
        spec = self.copy(deps=dt.ALL & ~dt.BUILD)
        replacement = other.copy(deps=dt.ALL & ~dt.BUILD)

        def make_node_pairs(orig_spec, copied_spec):
            return list(
                zip(
                    orig_spec.traverse(deptype=dt.ALL & ~dt.BUILD),
                    copied_spec.traverse(deptype=dt.ALL & ~dt.BUILD),
                )
            )

        def mask_build_deps(in_spec):
            for edge in in_spec.traverse_edges(cover="edges"):
                edge.depflag &= ~dt.BUILD

        if transitive:
            # These pairs will allow us to reattach all direct build deps
            # We need the list of pairs while the two specs still match
            node_pairs = make_node_pairs(self, spec)

            # Ignore build deps in the modified spec while doing the splice
            # They will be added back in at the end
            mask_build_deps(spec)

            # Transitively splice any relevant nodes from new into base
            # This handles all shared dependencies between self and other
            spec._splice_helper(replacement)
        else:
            # Do the same thing as the transitive splice, but reversed
            node_pairs = make_node_pairs(other, replacement)
            mask_build_deps(replacement)
            replacement._splice_helper(spec)

            # Intransitively splice replacement into spec
            # This is very simple now that all shared dependencies have been handled
            for node in spec.traverse(order="topo", deptype=dt.LINK | dt.RUN):
                if node._splice_match(other, self_root=spec, other_root=other):
                    node._splice_detach_and_add_dependents(replacement, context=spec)

        # For nodes that were spliced, modify the build spec to ensure build deps are preserved
        # For nodes that were not spliced, replace the build deps on the spec itself
        for orig, copy in node_pairs:
            if copy._build_spec:
                copy._build_spec = orig.build_spec.copy()
            else:
                for edge in orig.edges_to_dependencies(depflag=dt.BUILD):
                    copy._add_dependency(edge.spec, depflag=dt.BUILD, virtuals=edge.virtuals)

        return spec

    def clear_caches(self, ignore: Tuple[str, ...] = ()) -> None:
        """
        Clears all cached hashes in a Spec, while preserving other properties.
        """
        for h in ht.HASHES:
            if h.attr not in ignore:
                if hasattr(self, h.attr):
                    setattr(self, h.attr, None)
        for attr in ("_dunder_hash", "_prefix"):
            if attr not in ignore:
                setattr(self, attr, None)

    def __hash__(self):
        # If the spec is concrete, we leverage the dag hash and just use a 64-bit prefix of it.
        # The dag hash has the advantage that it's computed once per concrete spec, and it's saved
        # -- so if we read concrete specs we don't need to recompute the whole hash.
        if self.concrete:
            if not self._dunder_hash:
                self._dunder_hash = self.dag_hash_bit_prefix(64)
            return self._dunder_hash

        # This is the normal hash for lazy_lexicographic_ordering. It's
        # slow for large specs because it traverses the whole spec graph,
        # so we hope it only runs on abstract specs, which are small.
        return hash(lang.tuplify(self._cmp_iter))

    def __reduce__(self):
        return Spec.from_dict, (self.to_dict(hash=ht.dag_hash),)

    def attach_git_version_lookup(self):
        # Add a git lookup method for GitVersions
        if not self.name:
            return
        for v in self.versions:
            if isinstance(v, vn.GitVersion) and v.std_version is None:
                v.attach_lookup(spack.version.git_ref_lookup.GitRefLookup(self.fullname))

    def original_spec_format(self) -> int:
        """Returns the spec format originally used for this spec."""
        return self.annotations.original_spec_format

    def has_virtual_dependency(self, virtual: str) -> bool:
        return bool(self.dependencies(virtuals=(virtual,)))


class VariantMap(lang.HashableMap[str, vt.VariantValue]):
    """Map containing variant instances. New values can be added only
    if the key is not already present."""

    def __init__(self, spec: Spec):
        super().__init__()
        self.spec = spec

    def __setitem__(self, name, vspec):
        # Raise a TypeError if vspec is not of the right type
        if not isinstance(vspec, vt.VariantValue):
            raise TypeError(
                "VariantMap accepts only values of variant types "
                f"[got {type(vspec).__name__} instead]"
            )

        # Raise an error if the variant was already in this map
        if name in self.dict:
            msg = 'Cannot specify variant "{0}" twice'.format(name)
            raise vt.DuplicateVariantError(msg)

        # Raise an error if name and vspec.name don't match
        if name != vspec.name:
            raise KeyError(
                f'Inconsistent key "{name}", must be "{vspec.name}" to ' "match VariantSpec"
            )

        # Set the item
        super().__setitem__(name, vspec)

    def substitute(self, vspec):
        """Substitutes the entry under ``vspec.name`` with ``vspec``.

        Args:
            vspec: variant spec to be substituted
        """
        if vspec.name not in self:
            raise KeyError(f"cannot substitute a key that does not exist [{vspec.name}]")

        # Set the item
        super().__setitem__(vspec.name, vspec)

    def partition_variants(self):
        non_prop, prop = lang.stable_partition(self.values(), lambda x: not x.propagate)
        # Just return the names
        non_prop = [x.name for x in non_prop]
        prop = [x.name for x in prop]
        return non_prop, prop

    def satisfies(self, other: "VariantMap") -> bool:
        if self.spec.concrete:
            return self._satisfies_when_self_concrete(other)
        return self._satisfies_when_self_abstract(other)

    def _satisfies_when_self_concrete(self, other: "VariantMap") -> bool:
        non_propagating, propagating = other.partition_variants()
        result = all(
            name in self and self[name].satisfies(other[name]) for name in non_propagating
        )
        if not propagating:
            return result

        for node in self.spec.traverse():
            if not all(
                node.variants[name].satisfies(other[name])
                for name in propagating
                if name in node.variants
            ):
                return False
        return result

    def _satisfies_when_self_abstract(self, other: "VariantMap") -> bool:
        other_non_propagating, other_propagating = other.partition_variants()
        self_non_propagating, self_propagating = self.partition_variants()

        # First check variants without propagation set
        result = all(
            name in self_non_propagating
            and (self[name].propagate or self[name].satisfies(other[name]))
            for name in other_non_propagating
        )
        if result is False or (not other_propagating and not self_propagating):
            return result

        # Check that self doesn't contradict variants propagated by other
        if other_propagating:
            for node in self.spec.traverse():
                if not all(
                    node.variants[name].satisfies(other[name])
                    for name in other_propagating
                    if name in node.variants
                ):
                    return False

        # Check that other doesn't contradict variants propagated by self
        if self_propagating:
            for node in other.spec.traverse():
                if not all(
                    node.variants[name].satisfies(self[name])
                    for name in self_propagating
                    if name in node.variants
                ):
                    return False

        return result

    def intersects(self, other):
        return all(self[k].intersects(other[k]) for k in other if k in self)

    def constrain(self, other: "VariantMap") -> bool:
        """Add all variants in other that aren't in self to self. Also constrain all multi-valued
        variants that are already present. Return True iff self changed"""
        if other.spec is not None and other.spec._concrete:
            for k in self:
                if k not in other:
                    raise vt.UnsatisfiableVariantSpecError(self[k], "<absent>")

        changed = False
        for k in other:
            if k in self:
                if not self[k].intersects(other[k]):
                    raise vt.UnsatisfiableVariantSpecError(self[k], other[k])
                # If they are compatible merge them
                changed |= self[k].constrain(other[k])
            else:
                # If it is not present copy it straight away
                self[k] = other[k].copy()
                changed = True

        return changed

    def copy(self) -> "VariantMap":
        clone = VariantMap(self.spec)
        for name, variant in self.items():
            clone[name] = variant.copy()
        return clone

    def __str__(self):
        if not self:
            return ""

        # print keys in order
        sorted_keys = sorted(self.keys())

        # Separate boolean variants from key-value pairs as they print
        # differently. All booleans go first to avoid ' ~foo' strings that
        # break spec reuse in zsh.
        bool_keys = []
        kv_keys = []
        for key in sorted_keys:
            if self[key].type == vt.VariantType.BOOL:
                bool_keys.append(key)
            else:
                kv_keys.append(key)

        # add spaces before and after key/value variants.
        string = io.StringIO()

        for key in bool_keys:
            string.write(str(self[key]))

        for key in kv_keys:
            string.write(" ")
            string.write(str(self[key]))

        return string.getvalue()


def substitute_abstract_variants(spec: Spec):
    """Uses the information in `spec.package` to turn any variant that needs
    it into a SingleValuedVariant or BoolValuedVariant.

    This method is best effort. All variants that can be substituted will be
    substituted before any error is raised.

    Args:
        spec: spec on which to operate the substitution
    """
    # This method needs to be best effort so that it works in matrix exclusion
    # in $spack/lib/spack/spack/spec_list.py
    unknown = []
    for name, v in spec.variants.items():
        if v.concrete and v.type == vt.VariantType.MULTI:
            continue

        if name in ("dev_path", "commit"):
            v.type = vt.VariantType.SINGLE
            v.concrete = True
            continue
        elif name in vt.RESERVED_NAMES:
            continue

        variant_defs = spack.repo.PATH.get_pkg_class(spec.fullname).variant_definitions(name)
        valid_defs = []
        for when, vdef in variant_defs:
            if when.intersects(spec):
                valid_defs.append(vdef)

        if not valid_defs:
            if name not in spack.repo.PATH.get_pkg_class(spec.fullname).variant_names():
                unknown.append(name)
            else:
                whens = [str(when) for when, _ in variant_defs]
                raise InvalidVariantForSpecError(v.name, f"({', '.join(whens)})", spec)
            continue

        pkg_variant, *rest = valid_defs
        if rest:
            continue

        new_variant = pkg_variant.make_variant(*v.values)
        pkg_variant.validate_or_raise(new_variant, spec.name)
        spec.variants.substitute(new_variant)

    if unknown:
        variants = llnl.string.plural(len(unknown), "variant")
        raise vt.UnknownVariantError(
            f"Tried to set {variants} {llnl.string.comma_and(unknown)}. "
            f"{spec.name} has no such {variants}",
            unknown_variants=unknown,
        )


def parse_with_version_concrete(spec_like: Union[str, Spec]):
    """Same as Spec(string), but interprets @x as @=x"""
    s = Spec(spec_like)
    interpreted_version = s.versions.concrete_range_as_version
    if interpreted_version:
        s.versions = vn.VersionList([interpreted_version])
    return s


def merge_abstract_anonymous_specs(*abstract_specs: Spec):
    """Merge the abstracts specs passed as input and return the result.

    The root specs must be anonymous, and it's duty of the caller to ensure that.

    This function merge the abstract specs based on package names. In particular
    it doesn't try to resolve virtual dependencies.

    Args:
        *abstract_specs: abstract specs to be merged
    """
    merged_spec = Spec()
    for current_spec_constraint in abstract_specs:
        merged_spec.constrain(current_spec_constraint, deps=False)

        for name in merged_spec.common_dependencies(current_spec_constraint):
            merged_spec[name].constrain(current_spec_constraint[name], deps=False)

        # Update with additional constraints from other spec
        for name in current_spec_constraint.direct_dep_difference(merged_spec):
            edge = next(iter(current_spec_constraint.edges_to_dependencies(name)))

            merged_spec._add_dependency(
                edge.spec.copy(), depflag=edge.depflag, virtuals=edge.virtuals
            )

    return merged_spec


def reconstruct_virtuals_on_edges(spec: Spec) -> None:
    """Reconstruct virtuals on edges. Used to read from old DB and reindex."""
    virtuals_needed: Dict[str, Set[str]] = {}
    virtuals_provided: Dict[str, Set[str]] = {}
    for edge in spec.traverse_edges(cover="edges", root=False):
        parent_key = edge.parent.dag_hash()
        if parent_key not in virtuals_needed:
            # Construct which virtuals are needed by parent
            virtuals_needed[parent_key] = set()
            try:
                parent_pkg = edge.parent.package
            except Exception as e:
                warnings.warn(
                    f"cannot reconstruct virtual dependencies on {edge.parent.name}: {e}"
                )
                continue

            virtuals_needed[parent_key].update(
                name
                for name, when_deps in parent_pkg.dependencies_by_name(when=True).items()
                if spack.repo.PATH.is_virtual(name)
                and any(edge.parent.satisfies(x) for x in when_deps)
            )

        if not virtuals_needed[parent_key]:
            continue

        child_key = edge.spec.dag_hash()
        if child_key not in virtuals_provided:
            virtuals_provided[child_key] = set()
            try:
                child_pkg = edge.spec.package
            except Exception as e:
                warnings.warn(
                    f"cannot reconstruct virtual dependencies on {edge.parent.name}: {e}"
                )
                continue
            virtuals_provided[child_key].update(x.name for x in child_pkg.virtuals_provided)

        if not virtuals_provided[child_key]:
            continue

        virtuals_to_add = virtuals_needed[parent_key] & virtuals_provided[child_key]
        if virtuals_to_add:
            edge.update_virtuals(virtuals_to_add)


class SpecfileReaderBase:
    @classmethod
    def from_node_dict(cls, node):
        spec = Spec()

        name, node = cls.name_and_data(node)
        for h in ht.HASHES:
            setattr(spec, h.attr, node.get(h.name, None))

        spec.name = name
        spec.namespace = node.get("namespace", None)

        if "version" in node or "versions" in node:
            spec.versions = vn.VersionList.from_dict(node)
            spec.attach_git_version_lookup()

        if "arch" in node:
            spec.architecture = ArchSpec.from_dict(node)

        propagated_names = node.get("propagate", [])
        abstract_variants = set(node.get("abstract", ()))
        for name, values in node.get("parameters", {}).items():
            propagate = name in propagated_names
            if name in _valid_compiler_flags:
                spec.compiler_flags[name] = []
                for val in values:
                    spec.compiler_flags.add_flag(name, val, propagate)
            else:
                spec.variants[name] = vt.VariantValue.from_node_dict(
                    name, values, propagate=propagate, abstract=name in abstract_variants
                )

        spec.external_path = None
        spec.external_modules = None
        if "external" in node:
            # This conditional is needed because sometimes this function is
            # called with a node already constructed that contains a 'versions'
            # and 'external' field. Related to virtual packages provider
            # indexes.
            if node["external"]:
                spec.external_path = node["external"]["path"]
                spec.external_modules = node["external"]["module"]
                if spec.external_modules is False:
                    spec.external_modules = None
                spec.extra_attributes = node["external"].get("extra_attributes") or {}

        # specs read in are concrete unless marked abstract
        if node.get("concrete", True):
            spec._mark_root_concrete()

        if "patches" in node:
            patches = node["patches"]
            if len(patches) > 0:
                mvar = spec.variants.setdefault("patches", vt.MultiValuedVariant("patches", ()))
                mvar.set(*patches)
                # FIXME: Monkey patches mvar to store patches order
                mvar._patches_in_order_of_appearance = patches

        # Annotate the compiler spec, might be used later
        if "annotations" not in node:
            # Specfile v4 and earlier
            spec.annotations.with_spec_format(cls.SPEC_VERSION)
            if "compiler" in node:
                spec.annotations.with_compiler(cls.legacy_compiler(node))
        else:
            spec.annotations.with_spec_format(node["annotations"]["original_specfile_version"])
            if "compiler" in node["annotations"]:
                spec.annotations.with_compiler(Spec(f"{node['annotations']['compiler']}"))

        # Don't read dependencies here; from_dict() is used by
        # from_yaml() and from_json() to read the root *and* each dependency
        # spec.

        return spec

    @classmethod
    def legacy_compiler(cls, node):
        d = node["compiler"]
        return Spec(f"{d['name']}@{vn.VersionList.from_dict(d)}")

    @classmethod
    def _load(cls, data):
        """Construct a spec from JSON/YAML using the format version 2.

        This format is used in Spack v0.17, was introduced in
        https://github.com/spack/spack/pull/22845

        Args:
            data: a nested dict/list data structure read from YAML or JSON.
        """
        # Current specfile format
        nodes = data["spec"]["nodes"]
        hash_type = None
        any_deps = False

        # Pass 0: Determine hash type
        for node in nodes:
            for _, _, _, dhash_type, _, _ in cls.dependencies_from_node_dict(node):
                any_deps = True
                if dhash_type:
                    hash_type = dhash_type
                    break

        if not any_deps:  # If we never see a dependency...
            hash_type = ht.dag_hash.name
        elif not hash_type:  # Seen a dependency, still don't know hash_type
            raise spack.error.SpecError(
                "Spec dictionary contains malformed dependencies. Old format?"
            )

        hash_dict = {}
        root_spec_hash = None

        # Pass 1: Create a single lookup dictionary by hash
        for i, node in enumerate(nodes):
            node_hash = node[hash_type]
            node_spec = cls.from_node_dict(node)
            hash_dict[node_hash] = node
            hash_dict[node_hash]["node_spec"] = node_spec
            if i == 0:
                root_spec_hash = node_hash

        if not root_spec_hash:
            raise spack.error.SpecError("Spec dictionary contains no nodes.")

        # Pass 2: Finish construction of all DAG edges (including build specs)
        for node_hash, node in hash_dict.items():
            node_spec = node["node_spec"]
            for _, dhash, dtype, _, virtuals, direct in cls.dependencies_from_node_dict(node):
                node_spec._add_dependency(
                    hash_dict[dhash]["node_spec"],
                    depflag=dt.canonicalize(dtype),
                    virtuals=virtuals,
                    direct=direct,
                )
            if "build_spec" in node.keys():
                _, bhash, _ = cls.extract_build_spec_info_from_node_dict(node, hash_type=hash_type)
                node_spec._build_spec = hash_dict[bhash]["node_spec"]

        return hash_dict[root_spec_hash]["node_spec"]

    @classmethod
    def read_specfile_dep_specs(cls, deps, hash_type=ht.dag_hash.name):
        raise NotImplementedError("Subclasses must implement this method.")


class SpecfileV1(SpecfileReaderBase):
    SPEC_VERSION = 1

    @classmethod
    def load(cls, data):
        """Construct a spec from JSON/YAML using the format version 1.

        Note: Version 1 format has no notion of a build_spec, and names are
        guaranteed to be unique. This function is guaranteed to read specs as
        old as v0.10 - while it was not checked for older formats.

        Args:
            data: a nested dict/list data structure read from YAML or JSON.
        """
        nodes = data["spec"]

        # Read nodes out of list.  Root spec is the first element;
        # dependencies are the following elements.
        dep_list = [cls.from_node_dict(node) for node in nodes]
        if not dep_list:
            raise spack.error.SpecError("specfile contains no nodes.")

        deps = {spec.name: spec for spec in dep_list}
        result = dep_list[0]

        for node in nodes:
            # get dependency dict from the node.
            name, data = cls.name_and_data(node)
            for dname, _, dtypes, _, virtuals, direct in cls.dependencies_from_node_dict(data):
                deps[name]._add_dependency(
                    deps[dname], depflag=dt.canonicalize(dtypes), virtuals=virtuals, direct=direct
                )

        reconstruct_virtuals_on_edges(result)
        return result

    @classmethod
    def name_and_data(cls, node):
        name = next(iter(node))
        node = node[name]
        return name, node

    @classmethod
    def dependencies_from_node_dict(cls, node):
        if "dependencies" not in node:
            return []

        for t in cls.read_specfile_dep_specs(node["dependencies"]):
            yield t

    @classmethod
    def read_specfile_dep_specs(cls, deps, hash_type=ht.dag_hash.name):
        """Read the DependencySpec portion of a YAML-formatted Spec.
        This needs to be backward-compatible with older spack spec
        formats so that reindex will work on old specs/databases.
        """
        for dep_name, elt in deps.items():
            if isinstance(elt, dict):
                for h in ht.HASHES:
                    if h.name in elt:
                        dep_hash, deptypes = elt[h.name], elt["type"]
                        hash_type = h.name
                        virtuals = []
                        break
                else:  # We never determined a hash type...
                    raise spack.error.SpecError("Couldn't parse dependency spec.")
            else:
                raise spack.error.SpecError("Couldn't parse dependency types in spec.")
            yield dep_name, dep_hash, list(deptypes), hash_type, list(virtuals), True


class SpecfileV2(SpecfileReaderBase):
    SPEC_VERSION = 2

    @classmethod
    def load(cls, data):
        result = cls._load(data)
        reconstruct_virtuals_on_edges(result)
        return result

    @classmethod
    def name_and_data(cls, node):
        return node["name"], node

    @classmethod
    def dependencies_from_node_dict(cls, node):
        return cls.read_specfile_dep_specs(node.get("dependencies", []))

    @classmethod
    def read_specfile_dep_specs(cls, deps, hash_type=ht.dag_hash.name):
        """Read the DependencySpec portion of a YAML-formatted Spec.
        This needs to be backward-compatible with older spack spec
        formats so that reindex will work on old specs/databases.
        """
        if not isinstance(deps, list):
            raise spack.error.SpecError("Spec dictionary contains malformed dependencies")

        result = []
        for dep in deps:
            elt = dep
            dep_name = dep["name"]
            if isinstance(elt, dict):
                # new format: elements of dependency spec are keyed.
                for h in ht.HASHES:
                    if h.name in elt:
                        dep_hash, deptypes, hash_type, virtuals, direct = (
                            cls.extract_info_from_dep(elt, h)
                        )
                        break
                else:  # We never determined a hash type...
                    raise spack.error.SpecError("Couldn't parse dependency spec.")
            else:
                raise spack.error.SpecError("Couldn't parse dependency types in spec.")
            result.append((dep_name, dep_hash, list(deptypes), hash_type, list(virtuals), direct))
        return result

    @classmethod
    def extract_info_from_dep(cls, elt, hash):
        dep_hash, deptypes = elt[hash.name], elt["type"]
        hash_type = hash.name
        virtuals = []
        direct = True
        return dep_hash, deptypes, hash_type, virtuals, direct

    @classmethod
    def extract_build_spec_info_from_node_dict(cls, node, hash_type=ht.dag_hash.name):
        build_spec_dict = node["build_spec"]
        return build_spec_dict["name"], build_spec_dict[hash_type], hash_type


class SpecfileV3(SpecfileV2):
    SPEC_VERSION = 3


class SpecfileV4(SpecfileV2):
    SPEC_VERSION = 4

    @classmethod
    def extract_info_from_dep(cls, elt, hash):
        dep_hash = elt[hash.name]
        deptypes = elt["parameters"]["deptypes"]
        hash_type = hash.name
        virtuals = elt["parameters"]["virtuals"]
        direct = True
        return dep_hash, deptypes, hash_type, virtuals, direct

    @classmethod
    def load(cls, data):
        return cls._load(data)


class SpecfileV5(SpecfileV4):
    SPEC_VERSION = 5

    @classmethod
    def legacy_compiler(cls, node):
        raise RuntimeError("The 'compiler' option is unexpected in specfiles at v5 or greater")

    @classmethod
    def extract_info_from_dep(cls, elt, hash):
        dep_hash = elt[hash.name]
        deptypes = elt["parameters"]["deptypes"]
        hash_type = hash.name
        virtuals = elt["parameters"]["virtuals"]
        direct = elt["parameters"].get("direct", False)
        return dep_hash, deptypes, hash_type, virtuals, direct


#: Alias to the latest version of specfiles
SpecfileLatest = SpecfileV5


class LazySpecCache(collections.defaultdict):
    """Cache for Specs that uses a spec_like as key, and computes lazily
    the corresponding value ``Spec(spec_like``.
    """

    def __init__(self):
        super().__init__(Spec)

    def __missing__(self, key):
        value = self.default_factory(key)
        self[key] = value
        return value


def save_dependency_specfiles(root: Spec, output_directory: str, dependencies: List[Spec]):
    """Given a root spec (represented as a yaml object), index it with a subset
    of its dependencies, and write each dependency to a separate yaml file
    in the output directory.  By default, all dependencies will be written
    out.  To choose a smaller subset of dependencies to be written, pass a
    list of package names in the dependencies parameter. If the format of the
    incoming spec is not json, that can be specified with the spec_format
    parameter. This can be used to convert from yaml specfiles to the
    json format."""

    for spec in root.traverse():
        if not any(spec.satisfies(dep) for dep in dependencies):
            continue

        json_path = os.path.join(output_directory, f"{spec.name}.json")

        with open(json_path, "w", encoding="utf-8") as fd:
            fd.write(spec.to_json(hash=ht.dag_hash))


def get_host_environment_metadata() -> Dict[str, str]:
    """Get the host environment, reduce to a subset that we can store in
    the install directory, and add the spack version.
    """

    environ = get_host_environment()
    return {
        "host_os": environ["os"],
        "platform": environ["platform"],
        "host_target": environ["target"],
        "hostname": environ["hostname"],
        "spack_version": spack.get_version(),
        "kernel_version": platform.version(),
    }


def get_host_environment() -> Dict[str, Any]:
    """Returns a dictionary with host information (not including the os.environ)."""
    host_platform = spack.platforms.host()
    host_target = host_platform.default_target()
    host_os = host_platform.default_operating_system()
    arch_fmt = "platform={0} os={1} target={2}"
    arch_spec = Spec(arch_fmt.format(host_platform, host_os, host_target))
    return {
        "target": str(host_target),
        "os": str(host_os),
        "platform": str(host_platform),
        "arch": arch_spec,
        "architecture": arch_spec,
        "arch_str": str(arch_spec),
        "hostname": socket.gethostname(),
    }


def eval_conditional(string):
    """Evaluate conditional definitions using restricted variable scope."""
    valid_variables = get_host_environment()
    valid_variables.update({"re": re, "env": os.environ})
    return eval(string, valid_variables)


class InvalidVariantForSpecError(spack.error.SpecError):
    """Raised when an invalid conditional variant is specified."""

    def __init__(self, variant, when, spec):
        msg = f"Invalid variant {variant} for spec {spec}.\n"
        msg += f"{variant} is only available for {spec.name} when satisfying one of {when}."
        super().__init__(msg)


class UnsupportedPropagationError(spack.error.SpecError):
    """Raised when propagation (==) is used with reserved variant names."""


class DuplicateDependencyError(spack.error.SpecError):
    """Raised when the same dependency occurs in a spec twice."""


class UnsupportedCompilerError(spack.error.SpecError):
    """Raised when the user asks for a compiler spack doesn't know about."""


class DuplicateArchitectureError(spack.error.SpecError):
    """Raised when the same architecture occurs in a spec twice."""


class InvalidDependencyError(spack.error.SpecError):
    """Raised when a dependency in a spec is not actually a dependency
    of the package."""

    def __init__(self, pkg, deps):
        self.invalid_deps = deps
        super().__init__(
            "Package {0} does not depend on {1}".format(pkg, llnl.string.comma_or(deps))
        )


class UnsatisfiableSpecNameError(spack.error.UnsatisfiableSpecError):
    """Raised when two specs aren't even for the same package."""

    def __init__(self, provided, required):
        super().__init__(provided, required, "name")


class UnsatisfiableVersionSpecError(spack.error.UnsatisfiableSpecError):
    """Raised when a spec version conflicts with package constraints."""

    def __init__(self, provided, required):
        super().__init__(provided, required, "version")


class UnsatisfiableArchitectureSpecError(spack.error.UnsatisfiableSpecError):
    """Raised when a spec architecture conflicts with package constraints."""

    def __init__(self, provided, required):
        super().__init__(provided, required, "architecture")


# TODO: get rid of this and be more specific about particular incompatible
# dep constraints
class UnsatisfiableDependencySpecError(spack.error.UnsatisfiableSpecError):
    """Raised when some dependency of constrained specs are incompatible"""

    def __init__(self, provided, required):
        super().__init__(provided, required, "dependency")


class UnconstrainableDependencySpecError(spack.error.SpecError):
    """Raised when attempting to constrain by an anonymous dependency spec"""

    def __init__(self, spec):
        msg = "Cannot constrain by spec '%s'. Cannot constrain by a" % spec
        msg += " spec containing anonymous dependencies"
        super().__init__(msg)


class AmbiguousHashError(spack.error.SpecError):
    def __init__(self, msg, *specs):
        spec_fmt = "{namespace}.{name}{@version}{variants}{ arch=architecture}{/hash:7}"
        specs_str = "\n  " + "\n  ".join(spec.format(spec_fmt) for spec in specs)
        super().__init__(msg + specs_str)


class InvalidHashError(spack.error.SpecError):
    def __init__(self, spec, hash):
        msg = f"No spec with hash {hash} could be found to match {spec}."
        msg += " Either the hash does not exist, or it does not match other spec constraints."
        super().__init__(msg)


class SpecFilenameError(spack.error.SpecError):
    """Raised when a spec file name is invalid."""


class NoSuchSpecFileError(SpecFilenameError):
    """Raised when a spec file doesn't exist."""


class SpecFormatStringError(spack.error.SpecError):
    """Called for errors in Spec format strings."""


class SpecFormatPathError(spack.error.SpecError):
    """Called for errors in Spec path-format strings."""


class SpecFormatSigilError(SpecFormatStringError):
    """Called for mismatched sigils and attributes in format strings"""

    def __init__(self, sigil, requirement, used):
        msg = "The sigil %s may only be used for %s." % (sigil, requirement)
        msg += " It was used with the attribute %s." % used
        super().__init__(msg)


class ConflictsInSpecError(spack.error.SpecError, RuntimeError):
    def __init__(self, spec, matches):
        message = 'Conflicts in concretized spec "{0}"\n'.format(spec.short_spec)

        visited = set()

        long_message = ""

        match_fmt_default = '{0}. "{1}" conflicts with "{2}"\n'
        match_fmt_custom = '{0}. "{1}" conflicts with "{2}" [{3}]\n'

        for idx, (s, c, w, msg) in enumerate(matches):
            if s not in visited:
                visited.add(s)
                long_message += "List of matching conflicts for spec:\n\n"
                long_message += s.tree(indent=4) + "\n"

            if msg is None:
                long_message += match_fmt_default.format(idx + 1, c, w)
            else:
                long_message += match_fmt_custom.format(idx + 1, c, w, msg)

        super().__init__(message, long_message)


class SpecDeprecatedError(spack.error.SpecError):
    """Raised when a spec concretizes to a deprecated spec or dependency."""


class InvalidSpecDetected(spack.error.SpecError):
    """Raised when a detected spec doesn't pass validation checks."""


class SpliceError(spack.error.SpecError):
    """Raised when a splice is not possible due to dependency or provider
    satisfaction mismatch. The resulting splice would be unusable."""
