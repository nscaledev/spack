# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import argparse
import difflib
import importlib
import os
import re
import sys
from collections import Counter
from typing import Generator, List, Optional, Sequence, Union

import llnl.string
import llnl.util.tty as tty
from llnl.util.filesystem import join_path
from llnl.util.lang import attr_setdefault, index_by
from llnl.util.tty.colify import colify
from llnl.util.tty.color import colorize

import spack.concretize
import spack.config  # breaks a cycle.
import spack.environment as ev
import spack.error
import spack.extensions
import spack.paths
import spack.repo
import spack.spec
import spack.spec_parser
import spack.store
import spack.traverse as traverse
import spack.user_environment as uenv
import spack.util.spack_json as sjson
import spack.util.spack_yaml as syaml

from ..enums import InstallRecordStatus

# cmd has a submodule called "list" so preserve the python list module
python_list = list

# Patterns to ignore in the commands directory when looking for commands.
ignore_files = r"^\.|^__init__.py$|^#"

SETUP_PARSER = "setup_parser"
DESCRIPTION = "description"


def python_name(cmd_name):
    """Convert ``-`` to ``_`` in command name, to make a valid identifier."""
    return cmd_name.replace("-", "_")


def require_python_name(pname):
    """Require that the provided name is a valid python name (per
    python_name()). Useful for checking parameters for function
    prerequisites."""
    if python_name(pname) != pname:
        raise PythonNameError(pname)


def cmd_name(python_name):
    """Convert module name (with ``_``) to command name (with ``-``)."""
    return python_name.replace("_", "-")


def require_cmd_name(cname):
    """Require that the provided name is a valid command name (per
    cmd_name()). Useful for checking parameters for function
    prerequisites.
    """
    if cmd_name(cname) != cname:
        raise CommandNameError(cname)


#: global, cached list of all commands -- access through all_commands()
_all_commands = None


def all_commands():
    """Get a sorted list of all spack commands.

    This will list the lib/spack/spack/cmd directory and find the
    commands there to construct the list.  It does not actually import
    the python files -- just gets the names.
    """
    global _all_commands
    if _all_commands is None:
        _all_commands = []
        command_paths = [spack.paths.command_path]  # Built-in commands
        command_paths += spack.extensions.get_command_paths()  # Extensions
        for path in command_paths:
            for file in os.listdir(path):
                if file.endswith(".py") and not re.search(ignore_files, file):
                    cmd = re.sub(r".py$", "", file)
                    _all_commands.append(cmd_name(cmd))

        _all_commands.sort()

    return _all_commands


def remove_options(parser, *options):
    """Remove some options from a parser."""
    for option in options:
        for action in parser._actions:
            if vars(action)["option_strings"][0] == option:
                parser._handle_conflict_resolve(None, [(option, action)])
                break


def get_module(cmd_name):
    """Imports the module for a particular command name and returns it.

    Args:
        cmd_name (str): name of the command for which to get a module
            (contains ``-``, not ``_``).
    """
    require_cmd_name(cmd_name)
    pname = python_name(cmd_name)

    try:
        # Try to import the command from the built-in directory
        module_name = f"{__name__}.{pname}"
        module = importlib.import_module(module_name)
        tty.debug("Imported {0} from built-in commands".format(pname))
    except ImportError:
        module = spack.extensions.get_module(cmd_name)
        if not module:
            raise CommandNotFoundError(cmd_name)

    attr_setdefault(module, SETUP_PARSER, lambda *args: None)  # null-op
    attr_setdefault(module, DESCRIPTION, "")

    if not hasattr(module, pname):
        tty.die(
            "Command module %s (%s) must define function '%s'."
            % (module.__name__, module.__file__, pname)
        )

    return module


def get_command(cmd_name):
    """Imports the command function associated with cmd_name.

    The function's name is derived from cmd_name using python_name().

    Args:
        cmd_name (str): name of the command (contains ``-``, not ``_``).
    """
    require_cmd_name(cmd_name)
    pname = python_name(cmd_name)
    return getattr(get_module(cmd_name), pname)


def quote_kvp(string: str) -> str:
    """For strings like ``name=value`` or ``name==value``, quote and escape the value if needed.

    This is a compromise to respect quoting of key-value pairs on the CLI. The shell
    strips quotes from quoted arguments, so we cannot know *exactly* how CLI arguments
    were quoted. To compensate, we re-add quotes around anything staritng with ``name=``
    or ``name==``, and we assume the rest of the argument is the value. This covers the
    common cases of passign flags, e.g., ``cflags="-O2 -g"`` on the command line.
    """
    match = spack.spec_parser.SPLIT_KVP.match(string)
    if not match:
        return string

    key, delim, value = match.groups()
    return f"{key}{delim}{spack.spec_parser.quote_if_needed(value)}"


def parse_specs(
    args: Union[str, List[str]],
    concretize: bool = False,
    tests: spack.concretize.TestsType = False,
) -> List[spack.spec.Spec]:
    """Convenience function for parsing arguments from specs.  Handles common
    exceptions and dies if there are errors.
    """
    args = [args] if isinstance(args, str) else args
    arg_string = " ".join([quote_kvp(arg) for arg in args])

    specs = spack.spec_parser.parse(arg_string)
    if not concretize:
        return specs

    to_concretize: List[spack.concretize.SpecPairInput] = [(s, None) for s in specs]
    return _concretize_spec_pairs(to_concretize, tests=tests)


def _concretize_spec_pairs(
    to_concretize: List[spack.concretize.SpecPairInput], tests: spack.concretize.TestsType = False
) -> List[spack.spec.Spec]:
    """Helper method that concretizes abstract specs from a list of abstract,concrete pairs.

    Any spec with a concrete spec associated with it will concretize to that spec. Any spec
    with ``None`` for its concrete spec will be newly concretized. This method respects unification
    rules from config."""
    unify = spack.config.get("concretizer:unify", False)

    # Special case for concretizing a single spec
    if len(to_concretize) == 1:
        abstract, concrete = to_concretize[0]
        return [concrete or spack.concretize.concretize_one(abstract, tests=tests)]

    # Special case if every spec is either concrete or has an abstract hash
    if all(
        concrete or abstract.concrete or abstract.abstract_hash
        for abstract, concrete in to_concretize
    ):
        # Get all the concrete specs
        ret = [
            concrete or (abstract if abstract.concrete else abstract.lookup_hash())
            for abstract, concrete in to_concretize
        ]

        # If unify: true, check that specs don't conflict
        # Since all concrete, "when_possible" is not relevant
        if unify is True:  # True, "when_possible", False are possible values
            runtimes = spack.repo.PATH.packages_with_tags("runtime")
            specs_per_name = Counter(
                spec.name
                for spec in traverse.traverse_nodes(
                    ret, deptype=("link", "run"), key=traverse.by_dag_hash
                )
                if spec.name not in runtimes  # runtimes are allowed multiple times
            )

            conflicts = sorted(name for name, count in specs_per_name.items() if count > 1)
            if conflicts:
                raise spack.error.SpecError(
                    "Specs conflict and `concretizer:unify` is configured true.",
                    f"    specs depend on multiple versions of {', '.join(conflicts)}",
                )
        return ret

    # Standard case
    concretize_method = spack.concretize.concretize_separately  # unify: false
    if unify is True:
        concretize_method = spack.concretize.concretize_together
    elif unify == "when_possible":
        concretize_method = spack.concretize.concretize_together_when_possible

    concretized = concretize_method(to_concretize, tests=tests)
    return [concrete for _, concrete in concretized]


def matching_spec_from_env(spec):
    """
    Returns a concrete spec, matching what is available in the environment.
    If no matching spec is found in the environment (or if no environment is
    active), this will return the given spec but concretized.
    """
    env = ev.active_environment()
    if env:
        return env.matching_spec(spec) or spack.concretize.concretize_one(spec)
    else:
        return spack.concretize.concretize_one(spec)


def matching_specs_from_env(specs):
    """
    Same as ``matching_spec_from_env`` but respects spec unification rules.

    For each spec, if there is a matching spec in the environment it is used. If no
    matching spec is found, this will return the given spec but concretized in the
    context of the active environment and other given specs, with unification rules applied.
    """
    env = ev.active_environment()
    spec_pairs = [(spec, env.matching_spec(spec) if env else None) for spec in specs]
    additional_concrete_specs = (
        [(concrete, concrete) for _, concrete in env.concretized_specs()] if env else []
    )
    return _concretize_spec_pairs(spec_pairs + additional_concrete_specs)[: len(spec_pairs)]


def disambiguate_spec(
    spec: spack.spec.Spec,
    env: Optional[ev.Environment],
    local: bool = False,
    installed: Union[bool, InstallRecordStatus] = True,
    first: bool = False,
) -> spack.spec.Spec:
    """Given a spec, figure out which installed package it refers to.

    Args:
        spec: a spec to disambiguate
        env: a spack environment, if one is active, or None if no environment is active
        local: do not search chained spack instances
        installed: install status argument passed to database query.
        first: returns the first matching spec, even if more than one match is found
    """
    hashes = env.all_hashes() if env else None
    return disambiguate_spec_from_hashes(spec, hashes, local, installed, first)


def disambiguate_spec_from_hashes(
    spec: spack.spec.Spec,
    hashes: Optional[List[str]],
    local: bool = False,
    installed: Union[bool, InstallRecordStatus] = True,
    first: bool = False,
) -> spack.spec.Spec:
    """Given a spec and a list of hashes, get concrete spec the spec refers to.

    Arguments:
        spec: a spec to disambiguate
        hashes: a set of hashes of specs among which to disambiguate
        local: if True, do not search chained spack instances
        installed: install status argument passed to database query.
        first: returns the first matching spec, even if more than one match is found
    """
    if local:
        matching_specs = spack.store.STORE.db.query_local(spec, hashes=hashes, installed=installed)
    else:
        matching_specs = spack.store.STORE.db.query(spec, hashes=hashes, installed=installed)
    if not matching_specs:
        tty.die(f"Spec '{spec}' matches no installed packages.")

    elif first:
        return matching_specs[0]

    ensure_single_spec_or_die(spec, matching_specs)

    return matching_specs[0]


def ensure_single_spec_or_die(spec, matching_specs):
    if len(matching_specs) <= 1:
        return

    format_string = "{name}{@version}{ arch=architecture} {%compiler.name}{@compiler.version}"
    args = ["%s matches multiple packages." % spec, "Matching packages:"]
    args += [
        colorize("  @K{%s} " % s.dag_hash(7)) + s.cformat(format_string) for s in matching_specs
    ]
    args += ["Use a more specific spec (e.g., prepend '/' to the hash)."]
    tty.die(*args)


def gray_hash(spec, length):
    if not length:
        # default to maximum hash length
        length = 32
    h = spec.dag_hash(length) if spec.concrete else "-" * length
    return colorize("@K{%s}" % h)


def display_specs_as_json(specs, deps=False):
    """Convert specs to a list of json records."""
    seen = set()
    records = []
    for spec in specs:
        dag_hash = spec.dag_hash()
        if dag_hash in seen:
            continue
        records.append(spec.node_dict_with_hashes())
        seen.add(dag_hash)

        if deps:
            for dep in spec.traverse():
                dep_dag_hash = dep.dag_hash()
                if dep_dag_hash in seen:
                    continue
                records.append(dep.node_dict_with_hashes())
                seen.add(dep_dag_hash)

    sjson.dump(records, sys.stdout)


def iter_groups(specs, indent, all_headers):
    """Break a list of specs into groups indexed by arch/compiler."""
    # Make a dict with specs keyed by architecture and compiler.
    index = index_by(specs, ("architecture", "compiler"))
    ispace = indent * " "

    def _key(item):
        if item is None:
            return ""
        return str(item)

    # Traverse the index and print out each package
    for i, (architecture, compiler) in enumerate(sorted(index, key=_key)):
        if i > 0:
            print()

        header = "%s{%s} / %s{%s}" % (
            spack.spec.ARCHITECTURE_COLOR,
            architecture if architecture else "no arch",
            spack.spec.COMPILER_COLOR,
            f"{compiler.display_str}" if compiler else "no compiler",
        )

        # Sometimes we want to display specs that are not yet concretized.
        # If they don't have a compiler / architecture attached to them,
        # then skip the header
        if all_headers or (architecture is not None or compiler is not None):
            sys.stdout.write(ispace)
            tty.hline(colorize(header), char="-")

        specs = index[(architecture, compiler)]
        specs.sort()
        yield specs


def display_specs(specs, args=None, **kwargs):
    """Display human readable specs with customizable formatting.

    Prints the supplied specs to the screen, formatted according to the
    arguments provided.

    Specs are grouped by architecture and compiler, and columnized if
    possible.

    Options can add more information to the default display. Options can
    be provided either as keyword arguments or as an argparse namespace.
    Keyword arguments take precedence over settings in the argparse
    namespace.

    Args:
        specs (list): the specs to display
        args (argparse.Namespace or None): namespace containing formatting arguments

    Keyword Args:
        paths (bool): Show paths with each displayed spec
        deps (bool): Display dependencies with specs
        long (bool): Display short hashes with specs
        very_long (bool): Display full hashes with specs (supersedes ``long``)
        namespaces (bool): Print namespaces along with names
        show_flags (bool): Show compiler flags with specs
        variants (bool): Show variants with specs
        indent (int): indent each line this much
        groups (bool): display specs grouped by arch/compiler (default True)
        decorator (typing.Callable): function to call to decorate specs
        all_headers (bool): show headers even when arch/compiler aren't defined
        status_fn (typing.Callable): if provided, prepend install-status info
        output (typing.IO): A file object to write to. Default is ``sys.stdout``
        specfile_format (bool): specfile format of the current spec
    """

    def get_arg(name, default=None):
        """Prefer kwargs, then args, then default."""
        if name in kwargs:
            return kwargs.get(name)
        elif args is not None:
            return getattr(args, name, default)
        else:
            return default

    paths = get_arg("paths", False)
    deps = get_arg("deps", False)
    hashes = get_arg("long", False)
    namespaces = get_arg("namespaces", False)
    flags = get_arg("show_flags", False)
    variants = get_arg("variants", False)
    groups = get_arg("groups", True)
    all_headers = get_arg("all_headers", False)
    output = get_arg("output", sys.stdout)
    status_fn = get_arg("status_fn", None)
    specfile_format = get_arg("specfile_format", False)

    decorator = get_arg("decorator", None)
    if decorator is None:
        decorator = lambda s, f: f

    indent = get_arg("indent", 0)

    hlen = 7
    if get_arg("very_long", False):
        hashes = True
        hlen = None

    format_string = get_arg("format", None)
    if format_string is None:
        nfmt = "{fullname}" if namespaces else "{name}"
        ffmt = ""
        if flags:
            ffmt += " {compiler_flags}"
        vfmt = "{variants}" if variants else ""
        format_string = nfmt + "{@version}" + vfmt + ffmt

    if specfile_format:
        format_string = "[{specfile_version}] " + format_string

    def fmt(s, depth=0):
        """Formatter function for all output specs"""
        string = ""

        if status_fn:
            # This was copied from spec.tree's colorization logic
            # then shortened because it seems like status_fn should
            # always return an InstallStatus
            string += colorize(status_fn(s).value)

        if hashes:
            string += gray_hash(s, hlen) + " "
        string += depth * "    "
        string += decorator(s, s.cformat(format_string))
        return string

    def format_list(specs):
        """Display a single list of specs, with no groups"""
        # create the final, formatted versions of all specs
        formatted = []
        for spec in specs:
            if deps:
                for depth, dep in traverse.traverse_tree([spec], depth_first=False):
                    formatted.append((fmt(dep.spec, depth), dep.spec))
                formatted.append(("", None))  # mark newlines
            else:
                formatted.append((fmt(spec), spec))

        # unless any of these are set, we can just colify and be done.
        if not any((deps, paths)):
            colify((f[0] for f in formatted), indent=indent, output=output)
            return ""

        # otherwise, we'll print specs one by one
        max_width = max(len(f[0]) for f in formatted)
        path_fmt = "%%-%ds%%s" % (max_width + 2)

        out = ""
        # getting lots of prefixes requires DB lookups. Ensure
        # all spec.prefix calls are in one transaction.
        with spack.store.STORE.db.read_transaction():
            for string, spec in formatted:
                if not string:
                    # print newline from above
                    out += "\n"
                    continue

                if paths:
                    out += path_fmt % (string, spec.prefix) + "\n"
                else:
                    out += string + "\n"

        return out

    out = ""
    if groups:
        for specs in iter_groups(specs, indent, all_headers):
            output.write(format_list(specs))
    else:
        out = format_list(sorted(specs))

    output.write(out)
    output.flush()


def filter_loaded_specs(specs):
    """Filter a list of specs returning only those that are
    currently loaded."""
    hashes = os.environ.get(uenv.spack_loaded_hashes_var, "").split(os.pathsep)
    return [x for x in specs if x.dag_hash() in hashes]


def print_how_many_pkgs(specs, pkg_type="", suffix=""):
    """Given a list of specs, this will print a message about how many
    specs are in that list.

    Args:
        specs (list): depending on how many items are in this list, choose
            the plural or singular form of the word "package"
        pkg_type (str): the output string will mention this provided
            category, e.g. if pkg_type is "installed" then the message
            would be "3 installed packages"
    """
    tty.msg("%s" % llnl.string.plural(len(specs), pkg_type + " package") + suffix)


def spack_is_git_repo():
    """Ensure that this instance of Spack is a git clone."""
    return is_git_repo(spack.paths.prefix)


def is_git_repo(path):
    dotgit_path = join_path(path, ".git")
    if os.path.isdir(dotgit_path):
        # we are in a regular git repo
        return True
    if os.path.isfile(dotgit_path):
        # we might be in a git worktree
        try:
            with open(dotgit_path, "rb") as f:
                dotgit_content = syaml.load(f)
            return os.path.isdir(dotgit_content.get("gitdir", dotgit_path))
        except syaml.SpackYAMLError:
            pass
    return False


class PythonNameError(spack.error.SpackError):
    """Exception class thrown for impermissible python names"""

    def __init__(self, name):
        self.name = name
        super().__init__("{0} is not a permissible Python name.".format(name))


class CommandNameError(spack.error.SpackError):
    """Exception class thrown for impermissible command names"""

    def __init__(self, name):
        self.name = name
        super().__init__("{0} is not a permissible Spack command name.".format(name))


class MultipleSpecsMatch(Exception):
    """Raised when multiple specs match a constraint, in a context where
    this is not allowed.
    """


class NoSpecMatches(Exception):
    """Raised when no spec matches a constraint, in a context where
    this is not allowed.
    """


########################################
# argparse types for argument validation
########################################
def extant_file(f):
    """
    Argparse type for files that exist.
    """
    if not os.path.isfile(f):
        raise argparse.ArgumentTypeError("%s does not exist" % f)
    return f


def require_active_env(cmd_name):
    """Used by commands to get the active environment

    If an environment is not found, print an error message that says the calling
    command *needs* an active environment.

    Arguments:
        cmd_name (str): name of calling command

    Returns:
        (spack.environment.Environment): the active environment
    """
    env = ev.active_environment()

    if env:
        return env

    tty.die(
        "`spack %s` requires an environment" % cmd_name,
        "activate an environment first:",
        "    spack env activate ENV",
        "or use:",
        "    spack -e ENV %s ..." % cmd_name,
    )


def find_environment(args):
    """Find active environment from args or environment variable.

    Check for an environment in this order:
        1. via ``spack -e ENV`` or ``spack -D DIR`` (arguments)
        2. via a path in the spack.environment.spack_env_var environment variable.

    If an environment is found, read it in.  If not, return None.

    Arguments:
        args (argparse.Namespace): argparse namespace with command arguments

    Returns:
        (spack.environment.Environment): a found environment, or ``None``
    """

    # treat env as a name
    env = args.env
    if env:
        if ev.exists(env):
            return ev.read(env)

    else:
        # if env was specified, see if it is a directory otherwise, look
        # at env_dir (env and env_dir are mutually exclusive)
        env = args.env_dir

        # if no argument, look for the environment variable
        if not env:
            env = os.environ.get(ev.spack_env_var)

            # nothing was set; there's no active environment
            if not env:
                return None

    # if we get here, env isn't the name of a spack environment; it has
    # to be a path to an environment, or there is something wrong.
    if ev.is_env_dir(env):
        return ev.Environment(env)

    raise ev.SpackEnvironmentError("no environment in %s" % env)


def first_line(docstring):
    """Return the first line of the docstring."""
    return docstring.split("\n")[0]


def group_arguments(
    args: Sequence[str],
    *,
    max_group_size: int = 500,
    prefix_length: int = 0,
    max_group_length: Optional[int] = None,
) -> Generator[List[str], None, None]:
    """Splits the supplied list of arguments into groups for passing to CLI tools.

    When passing CLI arguments, we need to ensure that argument lists are no longer than
    the system command line size limit, and we may also need to ensure that groups are
    no more than some number of arguments long.

    This returns an iterator over lists of arguments that meet these constraints.
    Arguments are in the same order they appeared in the original argument list.

    If any argument's length is greater than the max_group_length, this will raise a
    ``ValueError``.

    Arguments:
        args: list of arguments to split into groups
        max_group_size: max number of elements in any group (default 500)
        prefix_length: length of any additional arguments (including spaces) to be passed before
            the groups from args; default is 0 characters
        max_group_length: max length of characters that if a group of args is joined by " "
            On unix, ths defaults to SC_ARG_MAX from sysconf. On Windows the default is
            the max usable for CreateProcess (32,768 chars)

    """
    if max_group_length is None:
        max_group_length = 32768  # default to the Windows limit
        if hasattr(os, "sysconf"):  # sysconf is only on unix
            try:
                # returns -1 if an option isn't present (soem older POSIXes)
                sysconf_max = os.sysconf("SC_ARG_MAX")
                max_group_length = sysconf_max if sysconf_max != -1 else max_group_length
            except (ValueError, OSError):
                pass  # keep windows default if SC_ARG_MAX isn't in sysconf_names

    group: List[str] = []
    grouplen, space = prefix_length, 0
    for arg in args:
        arglen = len(arg)
        if arglen > max_group_length:
            raise ValueError(f"Argument is longer than max command line size: '{arg}'")
        if arglen + prefix_length > max_group_length:
            raise ValueError(f"Argument with prefix is longer than max command line size: '{arg}'")

        next_grouplen = grouplen + arglen + space
        if len(group) == max_group_size or next_grouplen > max_group_length:
            yield group
            group, grouplen, space = [], prefix_length, 0

        group.append(arg)
        grouplen += arglen + space
        space = 1  # add a space for elements 1, 2, etc. but not 0

    if group:
        yield group


class CommandNotFoundError(spack.error.SpackError):
    """Exception class thrown when a requested command is not recognized as
    such.
    """

    def __init__(self, cmd_name):
        msg = (
            f"{cmd_name} is not a recognized Spack command or extension command; "
            "check with `spack commands`."
        )
        long_msg = None

        similar = difflib.get_close_matches(cmd_name, all_commands())

        if 1 <= len(similar) <= 5:
            long_msg = "\nDid you mean one of the following commands?\n  "
            long_msg += "\n  ".join(similar)

        super().__init__(msg, long_msg)
