# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""This is where most of the action happens in Spack.

The spack package class structure is based strongly on Homebrew
(http://brew.sh/), mainly because Homebrew makes it very easy to create
packages.
"""

import base64
import collections
import copy
import functools
import glob
import hashlib
import io
import os
import re
import sys
import textwrap
import time
import traceback
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple, Type, TypeVar, Union

from _vendoring.typing_extensions import Literal

import llnl.util.filesystem as fsys
import llnl.util.tty as tty
from llnl.util.lang import ClassProperty, classproperty, memoized

import spack.config
import spack.dependency
import spack.deptypes as dt
import spack.directives_meta
import spack.error
import spack.fetch_strategy as fs
import spack.hooks
import spack.mirrors.layout
import spack.mirrors.mirror
import spack.multimethod
import spack.patch
import spack.phase_callbacks
import spack.repo
import spack.spec
import spack.stage as stg
import spack.store
import spack.url
import spack.util.archive
import spack.util.environment
import spack.util.executable
import spack.util.git
import spack.util.naming
import spack.util.path
import spack.util.web
import spack.variant
from spack.compilers.adaptor import DeprecatedCompiler
from spack.error import InstallError, NoURLError, PackageError
from spack.filesystem_view import YamlFilesystemView
from spack.resource import Resource
from spack.solver.version_order import concretization_version_order
from spack.util.package_hash import package_hash
from spack.util.typing import SupportsRichComparison
from spack.version import GitVersion, StandardVersion, VersionError, is_git_version

FLAG_HANDLER_RETURN_TYPE = Tuple[
    Optional[Iterable[str]], Optional[Iterable[str]], Optional[Iterable[str]]
]
FLAG_HANDLER_TYPE = Callable[[str, Iterable[str]], FLAG_HANDLER_RETURN_TYPE]

#: Filename for the Spack build/install log.
_spack_build_logfile = "spack-build-out.txt"

#: Filename for the Spack build/install environment file.
_spack_build_envfile = "spack-build-env.txt"

#: Filename for the Spack build/install environment modifications file.
_spack_build_envmodsfile = "spack-build-env-mods.txt"

#: Filename for the Spack configure args file.
_spack_configure_argsfile = "spack-configure-args.txt"

#: Filename of json with total build and phase times (seconds)
spack_times_log = "install_times.json"

NO_DEFAULT = object()


class WindowsRPath:
    """Collection of functionality surrounding Windows RPATH specific features

    This is essentially meaningless for all other platforms
    due to their use of RPATH. All methods within this class are no-ops on
    non Windows. Packages can customize and manipulate this class as
    they would a genuine RPATH, i.e. adding directories that contain
    runtime library dependencies"""

    def win_add_library_dependent(self):
        """Return extra set of directories that require linking for package

        This method should be overridden by packages that produce
        binaries/libraries/python extension modules/etc that are installed into
        directories outside a package's `bin`, `lib`, and `lib64` directories,
        but still require linking against one of the packages dependencies, or
        other components of the package itself. No-op otherwise.

        Returns:
            List of additional directories that require linking
        """
        return []

    def win_add_rpath(self):
        """Return extra set of rpaths for package

        This method should be overridden by packages needing to
        include additional paths to be searched by rpath. No-op otherwise

        Returns:
            List of additional rpaths
        """
        return []

    def windows_establish_runtime_linkage(self):
        """Establish RPATH on Windows

        Performs symlinking to incorporate rpath dependencies to Windows runtime search paths
        """
        # If spec is an external, we should not be modifying its bin directory, as we would
        # be doing in this method
        # Spack should in general not modify things it has not installed
        # we can reasonably expect externals to have their link interface properly established
        if sys.platform == "win32" and not self.spec.external:
            win_rpath = fsys.WindowsSimulatedRPath(self)
            win_rpath.add_library_dependent(*self.win_add_library_dependent())
            win_rpath.add_rpath(*self.win_add_rpath())
            win_rpath.establish_link()


#: Registers which are the detectable packages, by repo and package name
#: Need a pass of package repositories to be filled.
detectable_packages = collections.defaultdict(list)


class DetectablePackageMeta(type):
    """Check if a package is detectable and add default implementations
    for the detection function.
    """

    TAG = "detectable"

    def __init__(cls, name, bases, attr_dict):
        if hasattr(cls, "executables") and hasattr(cls, "libraries"):
            msg = "a package can have either an 'executables' or 'libraries' attribute"
            raise spack.error.SpackError(f"{msg} [package '{name}' defines both]")

        # On windows, extend the list of regular expressions to look for
        # filenames ending with ".exe"
        # (in some cases these regular expressions include "$" to avoid
        # pulling in filenames with unexpected suffixes, but this allows
        # for example detecting "foo.exe" when the package writer specified
        # that "foo" was a possible executable.

        # If a package has the executables or libraries  attribute then it's
        # assumed to be detectable. Add a tag, so finding them is faster
        if hasattr(cls, "executables") or hasattr(cls, "libraries"):
            # To add the tag, we need to copy the tags attribute, and attach it to
            # the current class. We don't use append, since it might modify base classes,
            # if "tags" is retrieved following the MRO.
            cls.tags = getattr(cls, "tags", []) + [DetectablePackageMeta.TAG]

            @classmethod
            def platform_executables(cls):
                def to_windows_exe(exe):
                    if exe.endswith("$"):
                        exe = exe.replace("$", "%s$" % spack.util.path.win_exe_ext())
                    else:
                        exe += spack.util.path.win_exe_ext()
                    return exe

                plat_exe = []
                if hasattr(cls, "executables"):
                    for exe in cls.executables:
                        if sys.platform == "win32":
                            exe = to_windows_exe(exe)
                        plat_exe.append(exe)
                return plat_exe

            @classmethod
            def determine_spec_details(cls, prefix, objs_in_prefix):
                """Allow ``spack external find ...`` to locate installations.

                Args:
                    prefix (str): the directory containing the executables
                                  or libraries
                    objs_in_prefix (set): the executables or libraries that
                                          match the regex

                Returns:
                    The list of detected specs for this package
                """
                objs_by_version = collections.defaultdict(list)
                # The default filter function is the identity function for the
                # list of executables
                filter_fn = getattr(cls, "filter_detected_exes", lambda x, exes: exes)
                objs_in_prefix = filter_fn(prefix, objs_in_prefix)
                for obj in objs_in_prefix:
                    try:
                        version_str = cls.determine_version(obj)
                        if version_str:
                            objs_by_version[version_str].append(obj)
                    except Exception as e:
                        tty.debug(f"Cannot detect the version of '{obj}' [{str(e)}]")

                specs = []
                for version_str, objs in objs_by_version.items():
                    variants = cls.determine_variants(objs, version_str)
                    # Normalize output to list
                    if not isinstance(variants, list):
                        variants = [variants]

                    for variant in variants:
                        if isinstance(variant, str):
                            variant = (variant, {})
                        variant_str, extra_attributes = variant
                        spec_str = f"{cls.name}@{version_str} {variant_str}"

                        # Pop a few reserved keys from extra attributes, since
                        # they have a different semantics
                        external_path = extra_attributes.pop("prefix", None)
                        external_modules = extra_attributes.pop("modules", None)
                        try:
                            spec = spack.spec.Spec.from_detection(
                                spec_str,
                                external_path=external_path,
                                external_modules=external_modules,
                                extra_attributes=extra_attributes,
                            )
                        except Exception as e:
                            tty.debug(f'Parsing failed [spec_str="{spec_str}", error={str(e)}]')
                        else:
                            specs.append(spec)

                return sorted(specs)

            @classmethod
            def determine_variants(cls, objs, version_str):
                return ""

            # Register the class as a detectable package
            detectable_packages[cls.namespace].append(cls.name)

            # Attach function implementations to the detectable class
            default = False
            if not hasattr(cls, "determine_spec_details"):
                default = True
                cls.determine_spec_details = determine_spec_details

            if default and not hasattr(cls, "determine_version"):
                msg = (
                    'the package "{0}" in the "{1}" repo needs to define'
                    ' the "determine_version" method to be detectable'
                )
                NotImplementedError(msg.format(cls.name, cls.namespace))

            if default and not hasattr(cls, "determine_variants"):
                cls.determine_variants = determine_variants

            # This function should not be overridden by subclasses,
            # as it is not designed for bespoke pkg detection but rather
            # on a per-platform basis
            if "platform_executables" in cls.__dict__.keys():
                raise PackageError("Packages should not override platform_executables")
            cls.platform_executables = platform_executables

        super(DetectablePackageMeta, cls).__init__(name, bases, attr_dict)


class PackageMeta(
    spack.phase_callbacks.PhaseCallbacksMeta,
    DetectablePackageMeta,
    spack.directives_meta.DirectiveMeta,
    spack.multimethod.MultiMethodMeta,
):
    """
    Package metaclass for supporting directives (e.g., depends_on) and phases
    """

    def __new__(cls, name, bases, attr_dict):
        """
        FIXME: REWRITE
        Instance creation is preceded by phase attribute transformations.

        Conveniently transforms attributes to permit extensible phases by
        iterating over the attribute 'phases' and creating / updating private
        InstallPhase attributes in the class that will be initialized in
        __init__.
        """
        attr_dict["_name"] = None

        return super(PackageMeta, cls).__new__(cls, name, bases, attr_dict)


def on_package_attributes(**attr_dict):
    """Decorator: executes instance function only if object has attr valuses.

    Executes the decorated method only if at the moment of calling the
    instance has attributes that are equal to certain values.

    Args:
        attr_dict (dict): dictionary mapping attribute names to their
            required values
    """

    def _execute_under_condition(func):
        @functools.wraps(func)
        def _wrapper(instance, *args, **kwargs):
            # If all the attributes have the value we require, then execute
            has_all_attributes = all([hasattr(instance, key) for key in attr_dict])
            if has_all_attributes:
                has_the_right_values = all(
                    [
                        getattr(instance, key) == value for key, value in attr_dict.items()
                    ]  # NOQA: ignore=E501
                )
                if has_the_right_values:
                    func(instance, *args, **kwargs)

        return _wrapper

    return _execute_under_condition


class PackageViewMixin:
    """This collects all functionality related to adding installed Spack
    package to views. Packages can customize how they are added to views by
    overriding these functions.
    """

    def view_source(self):
        """The source root directory that will be added to the view: files are
        added such that their path relative to the view destination matches
        their path relative to the view source.
        """
        return self.spec.prefix

    def view_destination(self, view):
        """The target root directory: each file is added relative to this
        directory.
        """
        return view.get_projection_for_spec(self.spec)

    def view_file_conflicts(self, view, merge_map):
        """Report any files which prevent adding this package to the view. The
        default implementation looks for any files which already exist.
        Alternative implementations may allow some of the files to exist in
        the view (in this case they would be omitted from the results).
        """
        return set(dst for dst in merge_map.values() if os.path.lexists(dst))

    def add_files_to_view(self, view, merge_map, skip_if_exists=True):
        """Given a map of package files to destination paths in the view, add
        the files to the view. By default this adds all files. Alternative
        implementations may skip some files, for example if other packages
        linked into the view already include the file.

        Args:
            view (spack.filesystem_view.FilesystemView): the view that's updated
            merge_map (dict): maps absolute source paths to absolute dest paths for
                all files in from this package.
            skip_if_exists (bool): when True, don't link files in view when they
                already exist. When False, always link files, without checking
                if they already exist.
        """
        if skip_if_exists:
            for src, dst in merge_map.items():
                if not os.path.lexists(dst):
                    view.link(src, dst, spec=self.spec)
        else:
            for src, dst in merge_map.items():
                view.link(src, dst, spec=self.spec)

    def remove_files_from_view(self, view, merge_map):
        """Given a map of package files to files currently linked in the view,
        remove the files from the view. The default implementation removes all
        files. Alternative implementations may not remove all files. For
        example if two packages include the same file, it should only be
        removed when both packages are removed.
        """
        view.remove_files(merge_map.values())


Pb = TypeVar("Pb", bound="PackageBase")

# Some typedefs for dealing with when-indexed dictionaries
#
# Many of the dictionaries on PackageBase are of the form:
# { Spec: { K: V } }
#
# K might be a variant name, a version, etc. V is a definition of some Spack object.
# The methods below transform these types of dictionaries.
K = TypeVar("K", bound=SupportsRichComparison)
V = TypeVar("V")


def _by_subkey(
    when_indexed_dictionary: Dict[spack.spec.Spec, Dict[K, V]], when: bool = False
) -> Dict[K, Union[List[V], Dict[spack.spec.Spec, List[V]]]]:
    """Convert a dict of dicts keyed by when/subkey into a dict of lists keyed by subkey.

    Optional Arguments:
        when: if ``True``, don't discared the ``when`` specs; return a 2-level dictionary
            keyed by subkey and when spec.
    """
    # very hard to define this type to be conditional on `when`
    all_by_subkey: Dict[K, Any] = {}

    for when_spec, by_key in when_indexed_dictionary.items():
        for key, value in by_key.items():
            if when:
                when_dict = all_by_subkey.setdefault(key, {})
                when_dict.setdefault(when_spec, []).append(value)
            else:
                all_by_subkey.setdefault(key, []).append(value)

    # this needs to preserve the insertion order of whens
    return dict(sorted(all_by_subkey.items()))


def _subkeys(when_indexed_dictionary: Dict[spack.spec.Spec, Dict[K, V]]) -> List[K]:
    """Get sorted names from dicts keyed by when/name."""
    all_keys = set()
    for when, by_key in when_indexed_dictionary.items():
        for key in by_key:
            all_keys.add(key)

    return sorted(all_keys)


def _has_subkey(when_indexed_dictionary: Dict[spack.spec.Spec, Dict[K, V]], key: K) -> bool:
    return any(key in dictionary for dictionary in when_indexed_dictionary.values())


def _num_definitions(when_indexed_dictionary: Dict[spack.spec.Spec, Dict[K, V]]) -> int:
    return sum(len(dictionary) for dictionary in when_indexed_dictionary.values())


def _precedence(obj) -> int:
    """Get either a 'precedence' attribute or item from an object."""
    precedence = getattr(obj, "precedence", None)
    if precedence is None:
        raise KeyError(f"Couldn't get precedence from {type(obj)}")
    return precedence


def _remove_overridden_defs(defs: List[Tuple[spack.spec.Spec, Any]]) -> None:
    """Remove definitions from the list if their when specs are satisfied by later ones.

    Any such definitions are *always* overridden by their successor, as they will
    match everything the predecessor matches, and the solver will prefer them because of
    their higher precedence.

    We can just remove these defs and avoid putting them in the solver. This is also
    useful for, e.g., `spack info`, where we don't want to show a variant from a
    superclass if it is always overridden by a variant defined in a subclass.

    Example::

        class ROCmPackage:
            variant("amdgpu_target", ..., when="+rocm")

        class Hipblas:
            variant("amdgpu_target", ...)

    The subclass definition *always* overrides the superclass definition here, but they
    have different when specs and the subclass def won't just replace the one in the
    superclass. In this situation, the subclass should *probably* also have
    ``when="+rocm"``, but we can't guarantee that will always happen when a vdef is
    overridden. So we use this method to remove any overrides we can know statically.

    """
    i = 0
    while i < len(defs):
        when, _ = defs[i]
        if any(when.satisfies(successor) for successor, _ in defs[i + 1 :]):
            del defs[i]
        else:
            i += 1


def _definitions(
    when_indexed_dictionary: Dict[spack.spec.Spec, Dict[K, V]], key: K
) -> List[Tuple[spack.spec.Spec, V]]:
    """Iterator over (when_spec, Value) for all values with a particular Key."""
    # construct a list of defs sorted by precedence
    defs: List[Tuple[spack.spec.Spec, V]] = []
    for when, values_by_key in when_indexed_dictionary.items():
        value_def = values_by_key.get(key)
        if value_def:
            defs.append((when, value_def))

    # With multiple definitions, ensure precedence order and simplify overrides
    if len(defs) > 1:
        defs.sort(key=lambda v: _precedence(v[1]))
        _remove_overridden_defs(defs)

    return defs


#: Store whether a given Spec source/binary should not be redistributed.
class DisableRedistribute:
    def __init__(self, source, binary):
        self.source = source
        self.binary = binary


class PackageBase(WindowsRPath, PackageViewMixin, metaclass=PackageMeta):
    """This is the superclass for all spack packages.

    ***The Package class***

    At its core, a package consists of a set of software to be installed.
    A package may focus on a piece of software and its associated software
    dependencies or it may simply be a set, or bundle, of software.  The
    former requires defining how to fetch, verify (via, e.g., sha256), build,
    and install that software and the packages it depends on, so that
    dependencies can be installed along with the package itself.   The latter,
    sometimes referred to as a ``no-source`` package, requires only defining
    the packages to be built.

    Packages are written in pure Python.

    There are two main parts of a Spack package:

      1. **The package class**.  Classes contain ``directives``, which are special functions, that
         add metadata (versions, patches, dependencies, and other information) to packages (see
         ``directives.py``). Directives provide the constraints that are used as input to the
         concretizer.

      2. **Package instances**. Once instantiated, a package can be passed to the PackageInstaller.
         It calls methods like ``do_stage()`` on the ``Package`` object, and it uses those to drive
         user-implemented methods like ``patch()``, ``install()``, and other build steps. To
         install software, an instantiated package needs a *concrete* spec, which guides the
         behavior of the various install methods.

    Packages are imported from repos (see ``repo.py``).

    **Package DSL**

    Look in ``lib/spack/docs`` or check https://spack.readthedocs.io for
    the full documentation of the package domain-specific language.  That
    used to be partially documented here, but as it grew, the docs here
    became increasingly out of date.

    **Package Lifecycle**

    A package's lifecycle over a run of Spack looks something like this:

    .. code-block:: python

       p = Package()             # Done for you by spack

       p.do_fetch()              # downloads tarball from a URL (or VCS)
       p.do_stage()              # expands tarball in a temp directory
       p.do_patch()              # applies patches to expanded source
       p.do_uninstall()          # removes install directory

    although packages that do not have code have nothing to fetch so omit
    ``p.do_fetch()``.

    There are also some other commands that clean the build area:

    .. code-block:: python

       p.do_clean()              # removes the stage directory entirely
       p.do_restage()            # removes the build directory and
                                 # re-expands the archive.

    The convention used here is that a ``do_*`` function is intended to be
    called internally by Spack commands (in ``spack.cmd``).  These aren't for
    package writers to override, and doing so may break the functionality
    of the Package class.

    Package creators have a lot of freedom, and they could technically
    override anything in this class.  That is not usually required.

    For most use cases.  Package creators typically just add attributes
    like ``homepage`` and, for a code-based package, ``url``, or functions
    such as ``install()``.
    There are many custom ``Package`` subclasses in the
    ``spack_repo.builtin.build_systems`` package that make things even easier for
    specific build systems.

    """

    compiler = DeprecatedCompiler()

    #
    # These are default values for instance variables.
    #

    # Declare versions dictionary as placeholder for values.
    # This allows analysis tools to correctly interpret the class attributes.
    versions: dict
    resources: Dict[spack.spec.Spec, List[Resource]]
    dependencies: Dict[spack.spec.Spec, Dict[str, spack.dependency.Dependency]]
    conflicts: Dict[spack.spec.Spec, List[Tuple[spack.spec.Spec, Optional[str]]]]
    requirements: Dict[
        spack.spec.Spec, List[Tuple[Tuple[spack.spec.Spec, ...], str, Optional[str]]]
    ]
    provided: Dict[spack.spec.Spec, Set[spack.spec.Spec]]
    provided_together: Dict[spack.spec.Spec, List[Set[str]]]
    patches: Dict[spack.spec.Spec, List[spack.patch.Patch]]
    variants: Dict[spack.spec.Spec, Dict[str, spack.variant.Variant]]
    languages: Dict[spack.spec.Spec, Set[str]]
    licenses: Dict[spack.spec.Spec, str]
    splice_specs: Dict[spack.spec.Spec, Tuple[spack.spec.Spec, Union[None, str, List[str]]]]

    #: Store whether a given Spec source/binary should not be redistributed.
    disable_redistribute: Dict[spack.spec.Spec, DisableRedistribute]

    #: By default, packages are not virtual
    #: Virtual packages override this attribute
    virtual = False

    #: Most Spack packages are used to install source or binary code while
    #: those that do not can be used to install a set of other Spack packages.
    has_code = True

    #: By default we build in parallel.  Subclasses can override this.
    parallel = True

    #: By default do not run tests within package's install()
    run_tests = False

    #: Most packages are NOT extendable. Set to True if you want extensions.
    extendable = False

    #: When True, add RPATHs for the entire DAG. When False, add RPATHs only
    #: for immediate dependencies.
    transitive_rpaths = True

    #: List of shared objects that should be replaced with a different library at
    #: runtime. Typically includes stub libraries like libcuda.so. When linking
    #: against a library listed here, the dependent will only record its soname
    #: or filename, not its absolute path, so that the dynamic linker will search
    #: for it. Note: accepts both file names and directory names, for example
    #: ``["libcuda.so", "stubs"]`` will ensure libcuda.so and all libraries in the
    #: stubs directory are not bound by path."""
    non_bindable_shared_objects: List[str] = []

    #: List of fnmatch patterns of library file names (specifically DT_NEEDED entries) that are not
    #: expected to be locatable in RPATHs. Generally this is a problem, and Spack install with
    #: config:shared_linking:strict will cause install failures if such libraries are found.
    #: However, in certain cases it can be hard if not impossible to avoid accidental linking
    #: against system libraries; until that is resolved, this attribute can be used to suppress
    #: errors.
    unresolved_libraries: List[str] = []

    #: List of prefix-relative file paths (or a single path). If these do
    #: not exist after install, or if they exist but are not files,
    #: sanity checks fail.
    sanity_check_is_file: List[str] = []

    #: List of prefix-relative directory paths (or a single path). If
    #: these do not exist after install, or if they exist but are not
    #: directories, sanity checks will fail.
    sanity_check_is_dir: List[str] = []

    #: Boolean. Set to ``True`` for packages that require a manual download.
    #: This is currently used by package sanity tests and generation of a
    #: more meaningful fetch failure error.
    manual_download = False

    #: Set of additional options used when fetching package versions.
    fetch_options: Dict[str, Any] = {}

    #
    # Set default licensing information
    #
    #: Boolean. If set to ``True``, this software requires a license.
    #: If set to ``False``, all of the ``license_*`` attributes will
    #: be ignored. Defaults to ``False``.
    license_required = False

    #: String. Contains the symbol used by the license manager to denote
    #: a comment. Defaults to ``#``.
    license_comment = "#"

    #: List of strings. These are files that the software searches for when
    #: looking for a license. All file paths must be relative to the
    #: installation directory. More complex packages like Intel may require
    #: multiple licenses for individual components. Defaults to the empty list.
    license_files: List[str] = []

    #: List of strings. Environment variables that can be set to tell the
    #: software where to look for a license if it is not in the usual location.
    #: Defaults to the empty list.
    license_vars: List[str] = []

    #: String. A URL pointing to license setup instructions for the software.
    #: Defaults to the empty string.
    license_url = ""

    #: Verbosity level, preserved across installs.
    _verbose = None

    #: Package homepage where users can find more information about the package
    homepage: ClassProperty[Optional[str]] = None

    #: Default list URL (place to find available versions)
    list_url: ClassProperty[Optional[str]] = None

    #: Link depth to which list_url should be searched for new versions
    list_depth = 0

    #: List of strings which contains GitHub usernames of package maintainers.
    #: Do not include @ here in order not to unnecessarily ping the users.
    maintainers: List[str] = []

    #: Set to ``True`` to indicate the stand-alone test requires a compiler.
    #: It is used to ensure a compiler and build dependencies like 'cmake'
    #: are available to build a custom test code.
    test_requires_compiler: bool = False

    #: TestSuite instance used to manage stand-alone tests for 1+ specs.
    test_suite: Optional[Any] = None

    def __init__(self, spec):
        # this determines how the package should be built.
        self.spec: spack.spec.Spec = spec

        # Allow custom staging paths for packages
        self.path = None

        # Keep track of whether or not this package was installed from
        # a binary cache.
        self.installed_from_binary_cache = False

        # Ensure that only one of these two attributes are present
        if getattr(self, "url", None) and getattr(self, "urls", None):
            msg = "a package can have either a 'url' or a 'urls' attribute"
            msg += " [package '{0.name}' defines both]"
            raise ValueError(msg.format(self))

        # init internal variables
        self._stage: Optional[stg.StageComposite] = None
        self._patch_stages = []  # need to track patch stages separately, in order to apply them
        self._fetcher = None
        self._tester: Optional[Any] = None

        # Set up timing variables
        self._fetch_time = 0.0

        super().__init__()

    def __getitem__(self, key: str) -> "PackageBase":
        return self.spec[key].package

    @classmethod
    def dependency_names(cls):
        return _subkeys(cls.dependencies)

    @classmethod
    def dependencies_by_name(cls, when: bool = False):
        return _by_subkey(cls.dependencies, when=when)

    # Accessors for variants
    # External code working with Variants should go through the methods below

    @classmethod
    def variant_names(cls) -> List[str]:
        return _subkeys(cls.variants)

    @classmethod
    def has_variant(cls, name) -> bool:
        return _has_subkey(cls.variants, name)

    @classmethod
    def num_variant_definitions(cls) -> int:
        """Total number of variant definitions in this class so far."""
        return _num_definitions(cls.variants)

    @classmethod
    def variant_definitions(cls, name: str) -> List[Tuple[spack.spec.Spec, spack.variant.Variant]]:
        """Iterator over (when_spec, Variant) for all variant definitions for a particular name."""
        return _definitions(cls.variants, name)

    @classmethod
    def variant_items(cls) -> Iterable[Tuple[spack.spec.Spec, Dict[str, spack.variant.Variant]]]:
        """Iterate over ``cls.variants.items()`` with overridden definitions removed."""
        # Note: This is quadratic in the average number of variant definitions per name.
        # That is likely close to linear in practice, as there are few variants with
        # multiple definitions (but it matters when they are there).
        exclude = {
            name: [id(vdef) for _, vdef in cls.variant_definitions(name)]
            for name in cls.variant_names()
        }

        for when, variants_by_name in cls.variants.items():
            filtered_variants_by_name = {
                name: vdef for name, vdef in variants_by_name.items() if id(vdef) in exclude[name]
            }

            if filtered_variants_by_name:
                yield when, filtered_variants_by_name

    def get_variant(self, name: str) -> spack.variant.Variant:
        """Get the highest precedence variant definition matching this package's spec.

        Arguments:
            name: name of the variant definition to get
        """
        try:
            highest_to_lowest = reversed(self.variant_definitions(name))
            return next(vdef for when, vdef in highest_to_lowest if self.spec.satisfies(when))
        except StopIteration:
            raise ValueError(f"No variant '{name}' on spec: {self.spec}")

    @classproperty
    def package_dir(cls):
        """Directory where the package.py file lives."""
        return os.path.abspath(os.path.dirname(cls.module.__file__))

    @classproperty
    def module(cls):
        """Module instance that this package class is defined in.

        We use this to add variables to package modules.  This makes
        install() methods easier to write (e.g., can call configure())
        """
        return sys.modules[cls.__module__]

    @classproperty
    def namespace(cls):
        """Spack namespace for the package, which identifies its repo."""
        return spack.repo.namespace_from_fullname(cls.__module__)

    @classproperty
    def fullname(cls):
        """Name of this package, including the namespace"""
        return "%s.%s" % (cls.namespace, cls.name)

    @classproperty
    def fullnames(cls):
        """Fullnames for this package and any packages from which it inherits."""
        fullnames = []
        for base in cls.__mro__:
            if not spack.repo.is_package_module(base.__module__):
                break
            fullnames.append(base.fullname)
        return fullnames

    @classproperty
    def name(cls):
        """The name of this package."""
        if cls._name is None:
            # We cannot know the exact package API version, but we can distinguish between v1
            # v2 based on the module. We don't want to figure out the exact package API version
            # since it requires parsing the repo.yaml.
            module = cls.__module__

            if module.startswith(spack.repo.PKG_MODULE_PREFIX_V1):
                version = (1, 0)
            elif module.startswith(spack.repo.PKG_MODULE_PREFIX_V2):
                version = (2, 0)
            else:
                raise ValueError(f"Package {cls.__qualname__} is not a known Spack package")

            if version < (2, 0):
                # spack.pkg.builtin.package_name.
                _, _, pkg_module = module.rpartition(".")
            else:
                # spack_repo.builtin.packages.package_name.package
                pkg_module = module.rsplit(".", 2)[-2]

            cls._name = spack.util.naming.pkg_dir_to_pkg_name(pkg_module, version)
        return cls._name

    @classproperty
    def global_license_dir(cls):
        """Returns the directory where license files for all packages are stored."""
        return spack.util.path.canonicalize_path(spack.config.get("config:license_dir"))

    @property
    def global_license_file(self):
        """Returns the path where a global license file for this
        particular package should be stored."""
        if not self.license_files:
            return
        return os.path.join(
            self.global_license_dir, self.name, os.path.basename(self.license_files[0])
        )

    # Source redistribution must be determined before concretization (because source mirrors work
    # with abstract specs).
    @classmethod
    def redistribute_source(cls, spec):
        """Whether it should be possible to add the source of this
        package to a Spack mirror."""
        for when_spec, disable_redistribute in cls.disable_redistribute.items():
            if disable_redistribute.source and spec.satisfies(when_spec):
                return False
        return True

    @property
    def redistribute_binary(self):
        """Whether it should be possible to create a binary out of an installed instance of this
        package."""
        for when_spec, disable_redistribute in self.disable_redistribute.items():
            if disable_redistribute.binary and self.spec.satisfies(when_spec):
                return False
        return True

    @property
    def keep_werror(self) -> Optional[Literal["all", "specific", "none"]]:
        """Keep ``-Werror`` flags, matches ``config:flags:keep_werror`` to override config.

        Valid return values are:
        * ``"all"``: keep all ``-Werror`` flags.
        * ``"specific"``: keep only ``-Werror=specific-warning`` flags.
        * ``"none"``: filter out all ``-Werror*`` flags.
        * ``None``: respect the user's configuration (``"none"`` by default).
        """
        if self.spec.satisfies("%nvhpc@:23.3"):
            # Filtering works by replacing -Werror with -Wno-error, but older nvhpc and
            # PGI do not understand -Wno-error, so we disable filtering.
            return "all"

        elif self.spec.satisfies("%nvhpc@23.4:"):
            # newer nvhpc supports -Wno-error but can't disable specific warnings with
            # -Wno-error=warning. Skip -Werror=warning, but still filter -Werror.
            return "specific"

        else:
            # use -Werror disablement by default for other compilers
            return None

    @property
    def version(self):
        if not self.spec.versions.concrete:
            raise ValueError(
                "Version requested for a package that" " does not have a concrete version."
            )
        return self.spec.versions[0]

    @classmethod
    @memoized
    def version_urls(cls) -> Dict[StandardVersion, str]:
        """Dict of explicitly defined URLs for versions of this package.

        Return:
           An dict mapping version to url, ordered by version.

        A version's URL only appears in the result if it has an an explicitly defined ``url``
        argument. So, this list may be empty if a package only defines ``url`` at the top level.
        """
        return {v: args["url"] for v, args in sorted(cls.versions.items()) if "url" in args}

    def nearest_url(self, version):
        """Finds the URL with the "closest" version to ``version``.

        This uses the following precedence order:

          1. Find the next lowest or equal version with a URL.
          2. If no lower URL, return the next *higher* URL.
          3. If no higher URL, return None.

        """
        version_urls = self.version_urls()

        if version in version_urls:
            return version_urls[version]

        last_url = None
        for v, u in self.version_urls().items():
            if v > version:
                if last_url:
                    return last_url
            last_url = u

        return last_url

    def url_for_version(self, version):
        """Returns a URL from which the specified version of this package
        may be downloaded.

        version: class Version
            The version for which a URL is sought.

        See Class Version (version.py)
        """
        return self._implement_all_urls_for_version(version)[0]

    def _update_external_dependencies(
        self, extendee_spec: Optional[spack.spec.Spec] = None
    ) -> None:
        """
        Method to override in package classes to handle external dependencies
        """
        pass

    def detect_dev_src_change(self) -> bool:
        """
        Method for checking for source code changes to trigger rebuild/reinstall
        """
        dev_path_var = self.spec.variants.get("dev_path", None)
        _, record = spack.store.STORE.db.query_by_spec_hash(self.spec.dag_hash())
        assert dev_path_var and record, "dev_path variant and record must be present"
        return fsys.recursive_mtime_greater_than(dev_path_var.value, record.installation_time)

    @classmethod
    def version_or_package_attr(cls, attr, version, default=NO_DEFAULT):
        """
        Get an attribute that could be on the version or package with preference to the version
        """
        version_attrs = cls.versions.get(version)
        if version_attrs and attr in version_attrs:
            return version_attrs.get(attr)
        if default is NO_DEFAULT and not hasattr(cls, attr):
            raise PackageError(f"{attr} attribute not defined on {cls.name}")
        return getattr(cls, attr, default)

    @classmethod
    def needs_commit(cls, version) -> bool:
        """
        Method for checking if the package instance needs a commit sha to be found
        """
        if isinstance(version, GitVersion):
            return True

        ver_attrs = cls.versions.get(version)
        if ver_attrs:
            return bool(ver_attrs.get("commit") or ver_attrs.get("tag") or ver_attrs.get("branch"))

        return False

    def resolve_binary_provenance(self) -> None:
        """
        Method to ensure concrete spec has binary provenance.
        Base implementation will look up git commits when appropriate.
        Packages may override this implementation for custom implementations
        """
        # early return cases, don't overwrite user intention
        # commit pre-assigned or develop specs don't need commits changed
        # since this would create un-necessary churn
        if "commit" in self.spec.variants or self.spec.is_develop:
            return

        if is_git_version(str(self.spec.version)):
            ref = self.spec.version.ref
        else:
            v_attrs = self.versions.get(self.spec.version, {})
            if "commit" in v_attrs:
                self.spec.variants["commit"] = spack.variant.SingleValuedVariant(
                    "commit", v_attrs["commit"]
                )
                return
            ref = v_attrs.get("tag") or v_attrs.get("branch")

        if not ref:
            raise VersionError(
                f"{self.name}'s version {str(self.spec.version)} "
                "is missing a git ref (commit, tag or branch)"
            )

        # Look for commits in the following places:
        # 1) stage,                (cheap, local, static)
        # 2) mirror archive file,  (cheapish, local, staticish)
        # 3) URL                   (cheap, remote, dynamic)
        # If users pre-stage, or use a mirror they can expect consistent commit resolution
        sha = None
        if self.stage.expanded:
            sha = spack.util.git.get_commit_sha(self.stage.source_path, ref)

        if not sha:
            try:
                self.do_fetch(mirror_only=True)
            except spack.error.FetchError:
                pass
            if self.stage.archive_file:
                sha = spack.util.archive.retrieve_commit_from_archive(self.stage.archive_file, ref)

        if not sha:
            url = self.version_or_package_attr("git", self.spec.version)
            sha = spack.util.git.get_commit_sha(url, ref)

        if sha:
            self.spec.variants["commit"] = spack.variant.SingleValuedVariant("commit", sha)

    def all_urls_for_version(self, version: StandardVersion) -> List[str]:
        """Return all URLs derived from version_urls(), url, urls, and
        list_url (if it contains a version) in a package in that order.

        Args:
            version: the version for which a URL is sought
        """
        uf = None
        if type(self).url_for_version != PackageBase.url_for_version:
            uf = self.url_for_version
        return self._implement_all_urls_for_version(version, uf)

    def _implement_all_urls_for_version(
        self,
        version: Union[str, StandardVersion],
        custom_url_for_version: Optional[Callable[[StandardVersion], Optional[str]]] = None,
    ) -> List[str]:
        version = StandardVersion.from_string(version) if isinstance(version, str) else version

        urls: List[str] = []

        # If we have a specific URL for this version, don't extrapolate.
        url = self.version_urls().get(version)
        if url:
            urls.append(url)

        # if there is a custom url_for_version, use it
        if custom_url_for_version is not None:
            u = custom_url_for_version(version)
            if u is not None and u not in urls:
                urls.append(u)

        def sub_and_add(u: Optional[str]) -> None:
            if u is None:
                return
            # skip the url if there is no version to replace
            try:
                spack.url.parse_version(u)
            except spack.url.UndetectableVersionError:
                return
            urls.append(spack.url.substitute_version(u, self.url_version(version)))

        # If no specific URL, use the default, class-level URL
        sub_and_add(getattr(self, "url", None))
        for u in getattr(self, "urls", []):
            sub_and_add(u)

        sub_and_add(getattr(self, "list_url", None))

        # if no version-bearing URLs can be found, try them raw
        if not urls:
            default_url = getattr(self, "url", getattr(self, "urls", [None])[0])

            # if no exact match AND no class-level default, use the nearest URL
            if not default_url:
                default_url = self.nearest_url(version)

                # if there are NO URLs to go by, then we can't do anything
                if not default_url:
                    raise NoURLError(self.__class__)
            urls.append(spack.url.substitute_version(default_url, self.url_version(version)))

        return urls

    def find_valid_url_for_version(self, version):
        """Returns a URL from which the specified version of this package
        may be downloaded after testing whether the url is valid. Will try
        url, urls, and list_url before failing.

        version: class Version
            The version for which a URL is sought.

        See Class Version (version.py)
        """
        urls = self.all_urls_for_version(version)

        for u in urls:
            if spack.util.web.url_exists(u):
                return u

        return None

    def _make_resource_stage(self, root_stage, resource):
        pretty_resource_name = fsys.polite_filename(f"{resource.name}-{self.version}")
        return stg.ResourceStage(
            resource.fetcher,
            root=root_stage,
            resource=resource,
            name=self._resource_stage(resource),
            mirror_paths=spack.mirrors.layout.default_mirror_layout(
                resource.fetcher, os.path.join(self.name, pretty_resource_name)
            ),
            mirrors=spack.mirrors.mirror.MirrorCollection(source=True).values(),
            path=self.path,
        )

    def _download_search(self):
        dynamic_fetcher = fs.from_list_url(self)
        return [dynamic_fetcher] if dynamic_fetcher else []

    def _make_root_stage(self, fetcher):
        # Construct a mirror path (TODO: get this out of package.py)
        format_string = "{name}-{version}"
        pretty_name = self.spec.format_path(format_string)
        mirror_paths = spack.mirrors.layout.default_mirror_layout(
            fetcher, os.path.join(self.name, pretty_name), self.spec
        )
        # Construct a path where the stage should build..
        s = self.spec
        stage_name = stg.compute_stage_name(s)
        stage = stg.Stage(
            fetcher,
            mirror_paths=mirror_paths,
            mirrors=spack.mirrors.mirror.MirrorCollection(source=True).values(),
            name=stage_name,
            path=self.path,
            search_fn=self._download_search,
        )
        return stage

    def _make_stages(self) -> Tuple[stg.StageComposite, List[stg.Stage]]:
        """Create stages for this package, its resources, and any patches to be applied.

        Returns:
            A StageComposite containing all stages created, as well as a list of patch stages for
            any patches that need to be fetched remotely.

        The StageComposite is used to manage (create destroy, etc.) the stages.

        The list of patch stages will be in the same order that patches are to be applied
        to the package's staged source code. This is needed in order to apply the patches later.

        """
        # If it's a dev package (not transitively), use a DIY stage object
        dev_path_var = self.spec.variants.get("dev_path", None)
        if dev_path_var:
            dev_path = dev_path_var.value
            link_format = spack.config.get("config:develop_stage_link")
            if not link_format:
                link_format = "build-{arch}-{hash:7}"
            stage_link = self.spec.format_path(link_format)
            source_stage = stg.DevelopStage(
                stg.compute_stage_name(self.spec), dev_path, stage_link
            )
        else:
            source_stage = self._make_root_stage(self.fetcher)

        # all_stages is source + resources + patches
        all_stages = stg.StageComposite()
        all_stages.append(source_stage)
        all_stages.extend(
            self._make_resource_stage(source_stage, r) for r in self._get_needed_resources()
        )

        def make_patch_stage(patch: spack.patch.UrlPatch, uniqe_part: str):
            # UrlPatches can make their own fetchers
            fetcher = patch.fetcher()

            # The same package can have multiple patches with the same name but
            # with different contents, therefore apply a subset of the hash.
            fetch_digest = patch.archive_sha256 or patch.sha256

            name = f"{os.path.basename(patch.url)}-{fetch_digest[:7]}"
            per_package_ref = os.path.join(patch.owner.split(".")[-1], name)
            mirror_ref = spack.mirrors.layout.default_mirror_layout(fetcher, per_package_ref)

            return stg.Stage(
                fetcher,
                name=f"{stg.stage_prefix}-{uniqe_part}-patch-{fetch_digest}",
                mirror_paths=mirror_ref,
                mirrors=spack.mirrors.mirror.MirrorCollection(source=True).values(),
            )

        if self.spec.concrete:
            patches = self.spec.patches
            uniqe_part = self.spec.dag_hash(7)
        else:
            # The only code path that gets here is `spack mirror create --all`,
            # which needs all matching patches.
            patch_lists = [
                plist for when, plist in self.patches.items() if self.spec.intersects(when)
            ]
            patches = sum(patch_lists, [])
            uniqe_part = self.name

        patch_stages = [
            make_patch_stage(p, uniqe_part) for p in patches if isinstance(p, spack.patch.UrlPatch)
        ]
        all_stages.extend(patch_stages)

        return all_stages, patch_stages

    @property
    def stage(self):
        """Get the build staging area for this package.

        This automatically instantiates a ``Stage`` object if the package
        doesn't have one yet, but it does not create the Stage directory
        on the filesystem.
        """
        if not self.spec.versions.concrete:
            raise ValueError("Cannot retrieve stage for package without concrete version.")
        if self._stage is None:
            self._stage, self._patch_stages = self._make_stages()
        return self._stage

    @stage.setter
    def stage(self, stage: stg.StageComposite):
        """Allow a stage object to be set to override the default."""
        self._stage = stage

    @property
    def env_path(self):
        """Return the build environment file path associated with staging."""
        return os.path.join(self.stage.path, _spack_build_envfile)

    @property
    def env_mods_path(self):
        """
        Return the build environment modifications file path associated with
        staging.
        """
        return os.path.join(self.stage.path, _spack_build_envmodsfile)

    @property
    def metadata_dir(self):
        """Return the install metadata directory."""
        return spack.store.STORE.layout.metadata_path(self.spec)

    @property
    def install_env_path(self):
        """
        Return the build environment file path on successful installation.
        """
        # Backward compatibility: Return the name of an existing log path;
        # otherwise, return the current install env path name.
        old_filename = os.path.join(self.metadata_dir, "build.env")
        if os.path.exists(old_filename):
            return old_filename
        else:
            return os.path.join(self.metadata_dir, _spack_build_envfile)

    @property
    def log_path(self):
        """Return the build log file path associated with staging."""
        return os.path.join(self.stage.path, _spack_build_logfile)

    @property
    def phase_log_files(self):
        """Find sorted phase log files written to the staging directory"""
        logs_dir = os.path.join(self.stage.path, "spack-build-*-out.txt")
        log_files = glob.glob(logs_dir)
        log_files.sort()
        return log_files

    @property
    def install_log_path(self):
        """Return the (compressed) build log file path on successful installation"""
        # Backward compatibility: Return the name of an existing install log.
        for filename in [_spack_build_logfile, "build.out", "build.txt"]:
            old_log = os.path.join(self.metadata_dir, filename)
            if os.path.exists(old_log):
                return old_log

        # Otherwise, return the current install log path name.
        return os.path.join(self.metadata_dir, _spack_build_logfile + ".gz")

    @property
    def configure_args_path(self):
        """Return the configure args file path associated with staging."""
        return os.path.join(self.stage.path, _spack_configure_argsfile)

    @property
    def times_log_path(self):
        """Return the times log json file."""
        return os.path.join(self.metadata_dir, spack_times_log)

    @property
    def install_configure_args_path(self):
        """Return the configure args file path on successful installation."""
        return os.path.join(self.metadata_dir, _spack_configure_argsfile)

    def archive_install_test_log(self):
        """Archive the install-phase test log, if present."""
        if getattr(self, "tester", None):
            self.tester.archive_install_test_log(self.metadata_dir)

    @property
    def tester(self):
        import spack.install_test

        if not self.spec.versions.concrete:
            raise ValueError("Cannot retrieve tester for package without concrete version.")

        if not self._tester:
            self._tester = spack.install_test.PackageTest(self)
        return self._tester

    @property
    def fetcher(self):
        if not self.spec.versions.concrete:
            raise ValueError("Cannot retrieve fetcher for package without concrete version.")
        if not self._fetcher:
            self._fetcher = fs.for_package_version(self)
        return self._fetcher

    @fetcher.setter
    def fetcher(self, f):
        self._fetcher = f
        self._fetcher.set_package(self)

    @classmethod
    def dependencies_of_type(cls, deptypes: dt.DepFlag):
        """Get names of dependencies that can possibly have these deptypes.

        This analyzes the package and determines which dependencies *can*
        be a certain kind of dependency. Note that they may not *always*
        be this kind of dependency, since dependencies can be optional,
        so something may be a build dependency in one configuration and a
        run dependency in another.
        """
        return {
            name
            for name, dependencies in cls.dependencies_by_name().items()
            if any(deptypes & dep.depflag for dep in dependencies)
        }

    # TODO: allow more than one active extendee.
    @property
    def extendee_spec(self):
        """
        Spec of the extendee of this package, or None if it is not an extension
        """
        if not self.extendees:
            return None

        # If the extendee is in the spec's deps already, return that.
        deps = [
            dep
            for dep in self.spec.dependencies(deptype=("link", "run"))
            for d, when in self.extendees.values()
            if dep.satisfies(d) and self.spec.satisfies(when)
        ]

        if deps:
            assert len(deps) == 1
            return deps[0]

        # if the spec is concrete already, then it extends something
        # that is an *optional* dependency, and the dep isn't there.
        if self.spec._concrete:
            return None
        else:
            # If it's not concrete, then return the spec from the
            # extends() directive since that is all we know so far.
            spec_str = next(iter(self.extendees))
            return spack.spec.Spec(spec_str)

    @property
    def is_extension(self):
        # if it is concrete, it's only an extension if it actually
        # dependes on the extendee.
        if self.spec._concrete:
            return self.extendee_spec is not None
        else:
            # If not, then it's an extension if it *could* be an extension
            return bool(self.extendees)

    def extends(self, spec):
        """
        Returns True if this package extends the given spec.

        If ``self.spec`` is concrete, this returns whether this package extends
        the given spec.

        If ``self.spec`` is not concrete, this returns whether this package may
        extend the given spec.
        """
        if spec.name not in self.extendees:
            return False
        s = self.extendee_spec
        return s and spec.satisfies(s)

    def provides(self, vpkg_name):
        """
        True if this package provides a virtual package with the specified name
        """
        return any(
            any(spec.name == vpkg_name for spec in provided)
            for when_spec, provided in self.provided.items()
            if self.spec.intersects(when_spec)
        )

    @property
    def virtuals_provided(self):
        """
        virtual packages provided by this package with its spec
        """
        return [
            vspec
            for when_spec, provided in self.provided.items()
            for vspec in sorted(provided)
            if self.spec.satisfies(when_spec)
        ]

    @classmethod
    def provided_virtual_names(cls):
        """Return sorted list of names of virtuals that can be provided by this package."""
        return sorted(
            set(vpkg.name for virtuals in cls.provided.values() for vpkg in sorted(virtuals))
        )

    @property
    def prefix(self):
        """Get the prefix into which this package should be installed."""
        return self.spec.prefix

    @property
    def home(self):
        return self.prefix

    @property
    def command(self) -> spack.util.executable.Executable:
        """Returns the main executable for this package."""
        path = os.path.join(self.home.bin, self.spec.name)
        if fsys.is_exe(path):
            return spack.util.executable.Executable(path)
        raise RuntimeError(f"Unable to locate {self.spec.name} command in {self.home.bin}")

    def url_version(self, version):
        """
        Given a version, this returns a string that should be substituted
        into the package's URL to download that version.

        By default, this just returns the version string. Subclasses may need
        to override this, e.g. for boost versions where you need to ensure that
        there are _'s in the download URL.
        """
        return str(version)

    def remove_prefix(self):
        """
        Removes the prefix for a package along with any empty parent
        directories
        """
        spack.store.STORE.layout.remove_install_directory(self.spec)

    @property
    def download_instr(self):
        """
        Defines the default manual download instructions.  Packages can
        override the property to provide more information.

        Returns:
            (str):  default manual download instructions
        """
        required = (
            f"Manual download is required for {self.spec.name}. " if self.manual_download else ""
        )
        return f"{required}Refer to {self.homepage} for download instructions."

    def do_fetch(self, mirror_only=False):
        """
        Creates a stage directory and downloads the tarball for this package.
        Working directory will be set to the stage directory.
        """
        if not self.has_code or self.spec.external:
            tty.debug("No fetch required for {0}".format(self.name))
            return

        checksum = spack.config.get("config:checksum")
        if (
            checksum
            and (self.version not in self.versions)
            and (not isinstance(self.version, GitVersion))
            and ("dev_path" not in self.spec.variants)
        ):
            tty.warn(
                "There is no checksum on file to fetch %s safely."
                % self.spec.cformat("{name}{@version}")
            )

            # Ask the user whether to skip the checksum if we're
            # interactive, but just fail if non-interactive.
            ck_msg = "Add a checksum or use --no-checksum to skip this check."
            ignore_checksum = False
            if sys.stdout.isatty():
                ignore_checksum = tty.get_yes_or_no("  Fetch anyway?", default=False)
                if ignore_checksum:
                    tty.debug("Fetching with no checksum. {0}".format(ck_msg))

            if not ignore_checksum:
                raise spack.error.FetchError(
                    "Will not fetch %s" % self.spec.format("{name}{@version}"), ck_msg
                )

        deprecated = spack.config.get("config:deprecated")
        if not deprecated and self.versions.get(self.version, {}).get("deprecated", False):
            tty.warn(
                "{0} is deprecated and may be removed in a future Spack "
                "release.".format(self.spec.format("{name}{@version}"))
            )

            # Ask the user whether to install deprecated version if we're
            # interactive, but just fail if non-interactive.
            dp_msg = (
                "If you are willing to be a maintainer for this version "
                "of the package, submit a PR to remove `deprecated=False"
                "`, or use `--deprecated` to skip this check."
            )
            ignore_deprecation = False
            if sys.stdout.isatty():
                ignore_deprecation = tty.get_yes_or_no("  Fetch anyway?", default=False)

                if ignore_deprecation:
                    tty.debug("Fetching deprecated version. {0}".format(dp_msg))

            if not ignore_deprecation:
                raise spack.error.FetchError(
                    "Will not fetch {0}".format(self.spec.format("{name}{@version}")), dp_msg
                )

        self.stage.create()
        err_msg = None if not self.manual_download else self.download_instr
        start_time = time.time()
        self.stage.fetch(mirror_only, err_msg=err_msg)
        self._fetch_time = time.time() - start_time

        if checksum and self.version in self.versions:
            self.stage.check()

        self.stage.cache_local()

    def do_stage(self, mirror_only=False):
        """Unpacks and expands the fetched tarball."""
        # Always create the stage directory at this point.  Why?  A no-code
        # package may want to use the installation process to install metadata.
        self.stage.create()

        # Fetch/expand any associated code.
        if self.has_code and not self.spec.external:
            self.do_fetch(mirror_only)
            self.stage.expand_archive()
        else:
            # Support for post-install hooks requires a stage.source_path
            fsys.mkdirp(self.stage.source_path)

    def do_patch(self):
        """Applies patches if they haven't been applied already."""
        if not self.spec.concrete:
            raise ValueError("Can only patch concrete packages.")

        # Kick off the stage first.  This creates the stage.
        self.do_stage()

        # Package can add its own patch function.
        has_patch_fun = hasattr(self, "patch") and callable(self.patch)

        # Get the patches from the spec (this is a shortcut for the MV-variant)
        patches = self.spec.patches

        # If there are no patches, note it.
        if not patches and not has_patch_fun:
            tty.msg("No patches needed for {0}".format(self.name))
            return

        # Construct paths to special files in the archive dir used to
        # keep track of whether patches were successfully applied.
        archive_dir = self.stage.source_path
        good_file = os.path.join(archive_dir, ".spack_patched")
        no_patches_file = os.path.join(archive_dir, ".spack_no_patches")
        bad_file = os.path.join(archive_dir, ".spack_patch_failed")

        # If we encounter an archive that failed to patch, restage it
        # so that we can apply all the patches again.
        if os.path.isfile(bad_file):
            if self.stage.requires_patch_success:
                tty.debug("Patching failed last time. Restaging.")
                self.stage.restage()
            else:
                # develop specs may have patch failures but should never be restaged
                tty.warn(
                    f"A patch failure was detected in {self.name}."
                    " Build errors may occur due to this."
                )
                return

        # If this file exists, then we already applied all the patches.
        if os.path.isfile(good_file):
            tty.msg("Already patched {0}".format(self.name))
            return
        elif os.path.isfile(no_patches_file):
            tty.msg("No patches needed for {0}".format(self.name))
            return

        errors = []
        # Apply all the patches for specs that match this one
        patched = False
        patch_stages = iter(self._patch_stages)

        for patch in patches:
            try:
                with fsys.working_dir(self.stage.source_path):
                    # get the path either from the stage where it was fetched, or from the Patch
                    if isinstance(patch, spack.patch.UrlPatch):
                        patch_stage = next(patch_stages)
                        patch_path = patch_stage.single_file
                    else:
                        patch_path = patch.path

                    spack.patch.apply_patch(
                        self.stage, patch_path, patch.level, patch.working_dir, patch.reverse
                    )

                tty.msg(f"Applied patch {patch.path_or_url}")
                patched = True

            except spack.error.SpackError as e:
                # Touch bad file if anything goes wrong.
                fsys.touch(bad_file)
                error_msg = f"Patch {patch.path_or_url} failed."
                if self.stage.requires_patch_success:
                    tty.msg(error_msg)
                    raise
                else:
                    tty.debug(error_msg)
                    tty.debug(e)
                    errors.append(e)

        if has_patch_fun:
            try:
                with fsys.working_dir(self.stage.source_path):
                    self.patch()
                tty.msg("Ran patch() for {0}".format(self.name))
                patched = True
            except spack.multimethod.NoSuchMethodError:
                # We are running a multimethod without a default case.
                # If there's no default it means we don't need to patch.
                if not patched:
                    # if we didn't apply a patch from a patch()
                    # directive, AND the patch function didn't apply, say
                    # no patches are needed.  Otherwise, we already
                    # printed a message for each patch.
                    tty.msg("No patches needed for {0}".format(self.name))
            except spack.error.SpackError as e:
                # Touch bad file if anything goes wrong.
                fsys.touch(bad_file)
                error_msg = f"patch() function failed for {self.name}"
                if self.stage.requires_patch_success:
                    tty.msg(error_msg)
                    raise
                else:
                    tty.debug(error_msg)
                    tty.debug(e)
                    errors.append(e)

        if not errors:
            # Get rid of any old failed file -- patches have either succeeded
            # or are not needed.  This is mostly defensive -- it's needed
            # if we didn't restage
            if os.path.isfile(bad_file):
                os.remove(bad_file)

            # touch good or no patches file so that we skip next time.
            if patched:
                fsys.touch(good_file)
            else:
                fsys.touch(no_patches_file)

    @classmethod
    def all_patches(cls):
        """Retrieve all patches associated with the package.

        Retrieves patches on the package itself as well as patches on the
        dependencies of the package."""
        patches = []
        for _, patch_list in cls.patches.items():
            for patch in patch_list:
                patches.append(patch)

        pkg_deps = cls.dependencies
        for dep_name in pkg_deps:
            for _, dependency in pkg_deps[dep_name].items():
                for _, patch_list in dependency.patches.items():
                    for patch in patch_list:
                        patches.append(patch)

        return patches

    def content_hash(self, content: Optional[bytes] = None) -> str:
        """Create a hash based on the artifacts and patches used to build this package.

        This includes:
            * source artifacts (tarballs, repositories) used to build;
            * content hashes (``sha256``'s) of all patches applied by Spack; and
            * canonicalized contents the ``package.py`` recipe used to build.

        This hash is only included in Spack's DAG hash for concrete specs, but if it
        happens to be called on a package with an abstract spec, only applicable (i.e.,
        determinable) portions of the hash will be included.

        """
        # list of components to make up the hash
        hash_content = []

        # source artifacts/repositories
        # TODO: resources
        if self.spec.versions.concrete:
            try:
                source_id = fs.for_package_version(self).source_id()
            except (fs.ExtrapolationError, fs.InvalidArgsError, spack.error.NoURLError):
                # ExtrapolationError happens if the package has no fetchers defined.
                # InvalidArgsError happens when there are version directives with args,
                #     but none of them identifies an actual fetcher.
                # NoURLError happens if the package is external-only with no url
                source_id = None

            if not source_id:
                # TODO? in cases where a digest or source_id isn't available,
                # should this attempt to download the source and set one? This
                # probably only happens for source repositories which are
                # referenced by branch name rather than tag or commit ID.
                from_local_sources = "dev_path" in self.spec.variants
                if self.has_code and not self.spec.external and not from_local_sources:
                    message = "Missing a source id for {s.name}@{s.version}"
                    tty.debug(message.format(s=self))
                hash_content.append("".encode("utf-8"))
            else:
                hash_content.append(source_id.encode("utf-8"))

        # patch sha256's
        # Only include these if they've been assigned by the concretizer.
        # We check spec._patches_assigned instead of spec.concrete because
        # we have to call package_hash *before* marking specs concrete
        if self.spec._patches_assigned():
            hash_content.extend(
                ":".join((p.sha256, str(p.level))).encode("utf-8") for p in self.spec.patches
            )

        # package.py contents
        hash_content.append(package_hash(self.spec, source=content).encode("utf-8"))

        # put it all together and encode as base32
        b32_hash = base64.b32encode(
            hashlib.sha256(bytes().join(sorted(hash_content))).digest()
        ).lower()
        b32_hash = b32_hash.decode("utf-8")

        return b32_hash

    @property
    def cmake_prefix_paths(self):
        return [self.prefix]

    def _has_make_target(self, target):
        """Checks to see if 'target' is a valid target in a Makefile.

        Parameters:
            target (str): the target to check for

        Returns:
            bool: True if 'target' is found, else False
        """
        # Check if we have a Makefile
        for makefile in ["GNUmakefile", "Makefile", "makefile"]:
            if os.path.exists(makefile):
                break
        else:
            tty.debug("No Makefile found in the build directory")
            return False

        # Prevent altering LC_ALL for 'make' outside this function
        make = copy.deepcopy(self.module.make)

        # Use English locale for missing target message comparison
        make.add_default_env("LC_ALL", "C")

        # Check if 'target' is a valid target.
        #
        # `make -n target` performs a "dry run". It prints the commands that
        # would be run but doesn't actually run them. If the target does not
        # exist, you will see one of the following error messages:
        #
        # GNU Make:
        #     make: *** No rule to make target `test'.  Stop.
        #           *** No rule to make target 'test'.  Stop.
        #
        # BSD Make:
        #     make: don't know how to make test. Stop
        #
        # Note: "Stop." is not printed when running a Make jobserver (spack env depfile) that runs
        # with `make -k/--keep-going`
        missing_target_msgs = [
            "No rule to make target `{0}'.",
            "No rule to make target '{0}'.",
            "don't know how to make {0}.",
        ]

        kwargs = {
            "fail_on_error": False,
            "output": os.devnull,
            "error": str,
            # Remove MAKEFLAGS to avoid inherited flags from Make jobserver (spack env depfile)
            "extra_env": {"MAKEFLAGS": ""},
        }

        stderr = make("-n", target, **kwargs)

        for missing_target_msg in missing_target_msgs:
            if missing_target_msg.format(target) in stderr:
                tty.debug("Target '{0}' not found in {1}".format(target, makefile))
                return False

        return True

    def _if_make_target_execute(self, target, *args, **kwargs):
        """Runs ``make target`` if 'target' is a valid target in the Makefile.

        Parameters:
            target (str): the target to potentially execute
        """
        if self._has_make_target(target):
            # Execute target
            self.module.make(target, *args, **kwargs)

    def _has_ninja_target(self, target):
        """Checks to see if 'target' is a valid target in a Ninja build script.

        Parameters:
            target (str): the target to check for

        Returns:
            bool: True if 'target' is found, else False
        """
        ninja = self.module.ninja

        # Check if we have a Ninja build script
        if not os.path.exists("build.ninja"):
            tty.debug("No Ninja build script found in the build directory")
            return False

        # Get a list of all targets in the Ninja build script
        # https://ninja-build.org/manual.html#_extra_tools
        all_targets = ninja("-t", "targets", "all", output=str).split("\n")

        # Check if 'target' is a valid target
        matches = [line for line in all_targets if line.startswith(target + ":")]

        if not matches:
            tty.debug("Target '{0}' not found in build.ninja".format(target))
            return False

        return True

    def _if_ninja_target_execute(self, target, *args, **kwargs):
        """Runs ``ninja target`` if 'target' is a valid target in the Ninja
        build script.

        Parameters:
            target (str): the target to potentially execute
        """
        if self._has_ninja_target(target):
            # Execute target
            self.module.ninja(target, *args, **kwargs)

    def _get_needed_resources(self):
        # We use intersects here cause it would also work if self.spec is abstract
        resources = [
            resource
            for when_spec, resource_list in self.resources.items()
            if self.spec.intersects(when_spec)
            for resource in resource_list
        ]
        # Sorts the resources by the length of the string representing their destination. Since any
        # nested resource must contain another resource's path, that should work
        return sorted(resources, key=lambda res: len(res.destination))

    def _resource_stage(self, resource):
        pieces = ["resource", resource.name, self.spec.dag_hash()]
        resource_stage_folder = "-".join(pieces)
        return resource_stage_folder

    def do_test(self, *, dirty=False, externals=False, timeout: Optional[int] = None):
        if self.test_requires_compiler and not any(
            lang in self.spec for lang in ("c", "cxx", "fortran")
        ):
            tty.error(
                f"Skipping tests for package {self.spec}, since a compiler is required, "
                f"but not available"
            )
            return

        kwargs = {
            "dirty": dirty,
            "fake": False,
            "context": "test",
            "externals": externals,
            "verbose": tty.is_verbose(),
        }

        self.tester.stand_alone_tests(kwargs, timeout=timeout)

    def unit_test_check(self):
        """Hook for unit tests to assert things about package internals.

        Unit tests can override this function to perform checks after
        ``Package.install`` and all post-install hooks run, but before
        the database is updated.

        The overridden function may indicate that the install procedure
        should terminate early (before updating the database) by
        returning ``False`` (or any value such that ``bool(result)`` is
        ``False``).

        Return:
            (bool): ``True`` to continue, ``False`` to skip ``install()``
        """
        return True

    @classmethod
    def inject_flags(cls: Type[Pb], name: str, flags: Iterable[str]) -> FLAG_HANDLER_RETURN_TYPE:
        """
        flag_handler that injects all flags through the compiler wrapper.
        """
        return flags, None, None

    @classmethod
    def env_flags(cls: Type[Pb], name: str, flags: Iterable[str]) -> FLAG_HANDLER_RETURN_TYPE:
        """
        flag_handler that adds all flags to canonical environment variables.
        """
        return None, flags, None

    @classmethod
    def build_system_flags(
        cls: Type[Pb], name: str, flags: Iterable[str]
    ) -> FLAG_HANDLER_RETURN_TYPE:
        """
        flag_handler that passes flags to the build system arguments.  Any
        package using `build_system_flags` must also implement
        `flags_to_build_system_args`, or derive from a class that
        implements it.  Currently, AutotoolsPackage and CMakePackage
        implement it.
        """
        return None, None, flags

    def setup_run_environment(self, env: spack.util.environment.EnvironmentModifications) -> None:
        """Sets up the run environment for a package.

        Args:
            env: environment modifications to be applied when the package is run. Package authors
                can call methods on it to alter the run environment.
        """
        pass

    def setup_dependent_run_environment(
        self, env: spack.util.environment.EnvironmentModifications, dependent_spec: spack.spec.Spec
    ) -> None:
        """Sets up the run environment of packages that depend on this one.

        This is similar to ``setup_run_environment``, but it is used to modify the run environment
        of a package that *depends* on this one.

        This gives packages like Python and others that follow the extension model a way to
        implement common environment or run-time settings for dependencies.

        Args:
            env: environment modifications to be applied when the dependent package is run. Package
                authors can call methods on it to alter the build environment.

            dependent_spec: The spec of the dependent package about to be run. This allows the
                extendee (self) to query the dependent's state. Note that *this* package's spec is
                available as ``self.spec``
        """
        pass

    def setup_dependent_package(self, module, dependent_spec: spack.spec.Spec) -> None:
        """Set up module-scope global variables for dependent packages.

        This function is called when setting up the build and run environments of a DAG.

        Examples:

        1. Extensions often need to invoke the ``python`` interpreter from the Python installation
           being extended. This routine can put a ``python`` Executable as a global in the module
           scope for the extension package to simplify extension installs.

        2. MPI compilers could set some variables in the dependent's scope that point to ``mpicc``,
           ``mpicxx``, etc., allowing them to be called by common name regardless of which MPI is
           used.

        Args:
            module: The Python ``module`` object of the dependent package. Packages can use this to
                set module-scope variables for the dependent to use.

            dependent_spec: The spec of the dependent package about to be built. This allows the
                extendee (self) to query the dependent's state.  Note that *this* package's spec is
                available as ``self.spec``.
        """
        pass

    _flag_handler: Optional[FLAG_HANDLER_TYPE] = None

    @property
    def flag_handler(self) -> FLAG_HANDLER_TYPE:
        if self._flag_handler is None:
            self._flag_handler = PackageBase.inject_flags
        return self._flag_handler

    @flag_handler.setter
    def flag_handler(self, var: FLAG_HANDLER_TYPE) -> None:
        self._flag_handler = var

    # The flag handler method is called for each of the allowed compiler flags.
    # It returns a triple of inject_flags, env_flags, build_system_flags.
    # The flags returned as inject_flags are injected through the spack
    #  compiler wrappers.
    # The flags returned as env_flags are passed to the build system through
    #  the environment variables of the same name.
    # The flags returned as build_system_flags are passed to the build system
    #  package subclass to be turned into the appropriate part of the standard
    #  arguments. This is implemented for build system classes where
    #  appropriate and will otherwise raise a NotImplementedError.

    def flags_to_build_system_args(self, flags: Dict[str, List[str]]) -> None:
        # Takes flags as a dict name: list of values
        if any(v for v in flags.values()):
            msg = "The {0} build system".format(self.__class__.__name__)
            msg += " cannot take command line arguments for compiler flags"
            raise NotImplementedError(msg)

    @staticmethod
    def uninstall_by_spec(spec, force=False, deprecator=None):
        if not os.path.isdir(spec.prefix):
            # prefix may not exist, but DB may be inconsistent. Try to fix by
            # removing, but omit hooks.
            specs = spack.store.STORE.db.query(spec, installed=True)
            if specs:
                if deprecator:
                    spack.store.STORE.db.deprecate(specs[0], deprecator)
                    tty.debug("Deprecating stale DB entry for {0}".format(spec.short_spec))
                else:
                    spack.store.STORE.db.remove(specs[0])
                    tty.debug("Removed stale DB entry for {0}".format(spec.short_spec))
                return
            else:
                raise InstallError(str(spec) + " is not installed.")

        if not force:
            dependents = spack.store.STORE.db.installed_relatives(
                spec, direction="parents", transitive=True, deptype=("link", "run")
            )
            if dependents:
                raise PackageStillNeededError(spec, dependents)

        # Try to get the package for the spec
        try:
            pkg = spec.package
        except spack.repo.UnknownEntityError:
            pkg = None

        # Pre-uninstall hook runs first.
        with spack.store.STORE.prefix_locker.write_lock(spec):
            if pkg is not None:
                try:
                    spack.hooks.pre_uninstall(spec)
                except Exception as error:
                    if force:
                        error_msg = (
                            "One or more pre_uninstall hooks have failed"
                            " for {0}, but Spack is continuing with the"
                            " uninstall".format(str(spec))
                        )
                        if isinstance(error, spack.error.SpackError):
                            error_msg += "\n\nError message: {0}".format(str(error))
                        tty.warn(error_msg)
                        # Note that if the uninstall succeeds then we won't be
                        # seeing this error again and won't have another chance
                        # to run the hook.
                    else:
                        raise

            # Uninstalling in Spack only requires removing the prefix.
            if not spec.external:
                msg = "Deleting package prefix [{0}]"
                tty.debug(msg.format(spec.short_spec))
                # test if spec is already deprecated, not whether we want to
                # deprecate it now
                deprecated = bool(spack.store.STORE.db.deprecator(spec))
                spack.store.STORE.layout.remove_install_directory(spec, deprecated)
            # Delete DB entry
            if deprecator:
                msg = "deprecating DB entry [{0}] in favor of [{1}]"
                tty.debug(msg.format(spec.short_spec, deprecator.short_spec))
                spack.store.STORE.db.deprecate(spec, deprecator)
            else:
                msg = "Deleting DB entry [{0}]"
                tty.debug(msg.format(spec.short_spec))
                spack.store.STORE.db.remove(spec)

        if pkg is not None:
            try:
                spack.hooks.post_uninstall(spec)
            except Exception:
                # If there is a failure here, this is our only chance to do
                # something about it: at this point the Spec has been removed
                # from the DB and prefix, so the post-uninstallation hooks
                # will not have another chance to run.
                error_msg = (
                    "One or more post-uninstallation hooks failed for"
                    " {0}, but the prefix has been removed (if it is not"
                    " external).".format(str(spec))
                )
                tb_msg = traceback.format_exc()
                error_msg += "\n\nThe error:\n\n{0}".format(tb_msg)
                tty.warn(error_msg)

        tty.msg("Successfully uninstalled {0}".format(spec.short_spec))

    def do_uninstall(self, force=False):
        """Uninstall this package by spec."""
        # delegate to instance-less method.
        PackageBase.uninstall_by_spec(self.spec, force)

    def view(self):
        """Create a view with the prefix of this package as the root.
        Extensions added to this view will modify the installation prefix of
        this package.
        """
        return YamlFilesystemView(self.prefix, spack.store.STORE.layout)

    def do_restage(self):
        """Reverts expanded/checked out source to a pristine state."""
        self.stage.restage()

    def do_clean(self):
        """Removes the package's build stage and source tarball."""
        self.stage.destroy()

    @classmethod
    def format_doc(cls, **kwargs):
        """Wrap doc string at 72 characters and format nicely"""
        indent = kwargs.get("indent", 0)

        if not cls.__doc__:
            return ""

        doc = re.sub(r"\s+", " ", cls.__doc__)
        lines = textwrap.wrap(doc, 72)
        results = io.StringIO()
        for line in lines:
            results.write((" " * indent) + line + "\n")
        return results.getvalue()

    @property
    def all_urls(self) -> List[str]:
        """A list of all URLs in a package.

        Check both class-level and version-specific URLs.

        Returns a list of URLs
        """
        urls: List[str] = []
        if hasattr(self, "url") and self.url:
            urls.append(self.url)

        # fetch from first entry in urls to save time
        if hasattr(self, "urls") and self.urls:
            urls.append(self.urls[0])

        for args in self.versions.values():
            if "url" in args:
                urls.append(args["url"])
        return urls

    def fetch_remote_versions(
        self, concurrency: Optional[int] = None
    ) -> Dict[StandardVersion, str]:
        """Find remote versions of this package.

        Uses ``list_url`` and any other URLs listed in the package file.

        Returns:
            dict: a dictionary mapping versions to URLs
        """
        if not self.all_urls:
            return {}

        try:
            return spack.url.find_versions_of_archive(
                self.all_urls, self.list_url, self.list_depth, concurrency, reference_package=self
            )
        except spack.util.web.NoNetworkConnectionError as e:
            tty.die("Package.fetch_versions couldn't connect to:", e.url, e.message)

    @property
    def rpath(self):
        """Get the rpath this package links with, as a list of paths."""
        deps = self.spec.dependencies(deptype="link")

        # on Windows, libraries of runtime interest are typically
        # stored in the bin directory
        # Do not include Windows system libraries in the rpath interface
        # these libraries are handled automatically by VS/VCVARS and adding
        # Spack derived system libs into the link path or address space of a program
        # can result in conflicting versions, which makes Spack packages less useable
        if sys.platform == "win32":
            rpaths = [self.prefix.bin]
            rpaths.extend(
                d.prefix.bin
                for d in deps
                if os.path.isdir(d.prefix.bin)
                and "windows-system" not in getattr(d.package, "tags", [])
            )
        else:
            rpaths = [self.prefix.lib, self.prefix.lib64]
            rpaths.extend(d.prefix.lib for d in deps if os.path.isdir(d.prefix.lib))
            rpaths.extend(d.prefix.lib64 for d in deps if os.path.isdir(d.prefix.lib64))
        return rpaths

    @property
    def rpath_args(self):
        """
        Get the rpath args as a string, with -Wl,-rpath, for each element
        """
        return " ".join("-Wl,-rpath,%s" % p for p in self.rpath)


inject_flags = PackageBase.inject_flags
env_flags = PackageBase.env_flags
build_system_flags = PackageBase.build_system_flags


def deprecated_version(pkg: PackageBase, version: Union[str, StandardVersion]) -> bool:
    """Return True iff the version is deprecated.

    Arguments:
        pkg: The package whose version is to be checked.
        version: The version being checked
    """
    if not isinstance(version, StandardVersion):
        version = StandardVersion.from_string(version)

    details = pkg.versions.get(version)
    return details is not None and details.get("deprecated", False)


def preferred_version(pkg: PackageBase):
    """
    Returns a sorted list of the preferred versions of the package.

    Arguments:
        pkg: The package whose versions are to be assessed.
    """

    version, _ = max(pkg.versions.items(), key=concretization_version_order)
    return version


class PackageStillNeededError(InstallError):
    """Raised when package is still needed by another on uninstall."""

    def __init__(self, spec, dependents):
        spec_fmt = spack.spec.DEFAULT_FORMAT + " /{hash:7}"
        dep_fmt = "{name}{@versions} /{hash:7}"
        super().__init__(
            f"Cannot uninstall {spec.format(spec_fmt)}, "
            f"needed by {[dep.format(dep_fmt) for dep in dependents]}"
        )
        self.spec = spec
        self.dependents = dependents


class InvalidPackageOpError(PackageError):
    """Raised when someone tries perform an invalid operation on a package."""


class ExtensionError(PackageError):
    """Superclass for all errors having to do with extension packages."""


class ActivationError(ExtensionError):
    """Raised when there are problems activating an extension."""

    def __init__(self, msg, long_msg=None):
        super().__init__(msg, long_msg)


class DependencyConflictError(spack.error.SpackError):
    """Raised when the dependencies cannot be flattened as asked for."""

    def __init__(self, conflict):
        super().__init__("%s conflicts with another file in the flattened directory." % (conflict))


class ManualDownloadRequiredError(InvalidPackageOpError):
    """Raised when attempting an invalid operation on a package that requires a manual download."""
