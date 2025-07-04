# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
"""High-level functions to concretize list of specs"""
import importlib
import sys
import time
from typing import Iterable, List, Optional, Sequence, Tuple, Union

import llnl.util.tty as tty

import spack.compilers
import spack.compilers.config
import spack.config
import spack.error
import spack.repo
import spack.util.parallel
from spack.spec import ArchSpec, CompilerSpec, Spec

SpecPairInput = Tuple[Spec, Optional[Spec]]
SpecPair = Tuple[Spec, Spec]
TestsType = Union[bool, Iterable[str]]


def _concretize_specs_together(
    abstract_specs: Sequence[Spec], tests: TestsType = False
) -> List[Spec]:
    """Given a number of specs as input, tries to concretize them together.

    Args:
        abstract_specs: abstract specs to be concretized
        tests: list of package names for which to consider tests dependencies. If True, all nodes
            will have test dependencies. If False, test dependencies will be disregarded.
    """
    from spack.solver.asp import Solver

    allow_deprecated = spack.config.get("config:deprecated", False)
    result = Solver().solve(abstract_specs, tests=tests, allow_deprecated=allow_deprecated)
    return [s.copy() for s in result.specs]


def concretize_together(
    spec_list: Sequence[SpecPairInput], tests: TestsType = False
) -> List[SpecPair]:
    """Given a number of specs as input, tries to concretize them together.

    Args:
        spec_list: list of tuples to concretize. First entry is abstract spec, second entry is
            already concrete spec or None if not yet concretized
        tests: list of package names for which to consider tests dependencies. If True, all nodes
            will have test dependencies. If False, test dependencies will be disregarded.
    """
    to_concretize = [concrete if concrete else abstract for abstract, concrete in spec_list]
    abstract_specs = [abstract for abstract, _ in spec_list]
    concrete_specs = _concretize_specs_together(to_concretize, tests=tests)
    return list(zip(abstract_specs, concrete_specs))


def concretize_together_when_possible(
    spec_list: Sequence[SpecPairInput], tests: TestsType = False
) -> List[SpecPair]:
    """Given a number of specs as input, tries to concretize them together to the extent possible.

    See documentation for ``unify: when_possible`` concretization for the precise definition of
    "to the extent possible".

    Args:
        spec_list: list of tuples to concretize. First entry is abstract spec, second entry is
            already concrete spec or None if not yet concretized
        tests: list of package names for which to consider tests dependencies. If True, all nodes
            will have test dependencies. If False, test dependencies will be disregarded.
    """
    from spack.solver.asp import Solver

    to_concretize = [concrete if concrete else abstract for abstract, concrete in spec_list]
    old_concrete_to_abstract = {
        concrete: abstract for (abstract, concrete) in spec_list if concrete
    }

    result_by_user_spec = {}
    allow_deprecated = spack.config.get("config:deprecated", False)
    for result in Solver().solve_in_rounds(
        to_concretize, tests=tests, allow_deprecated=allow_deprecated
    ):
        result_by_user_spec.update(result.specs_by_input)

    # If the "abstract" spec is a concrete spec from the previous concretization
    # translate it back to an abstract spec. Otherwise, keep the abstract spec
    return [
        (old_concrete_to_abstract.get(abstract, abstract), concrete)
        for abstract, concrete in sorted(result_by_user_spec.items())
    ]


def concretize_separately(
    spec_list: Sequence[SpecPairInput], tests: TestsType = False
) -> List[SpecPair]:
    """Concretizes the input specs separately from each other.

    Args:
        spec_list: list of tuples to concretize. First entry is abstract spec, second entry is
            already concrete spec or None if not yet concretized
        tests: list of package names for which to consider tests dependencies. If True, all nodes
            will have test dependencies. If False, test dependencies will be disregarded.
    """
    from spack.bootstrap import ensure_bootstrap_configuration, ensure_clingo_importable_or_raise

    to_concretize = [abstract for abstract, concrete in spec_list if not concrete]
    args = [
        (i, str(abstract), tests)
        for i, abstract in enumerate(to_concretize)
        if not abstract.concrete
    ]
    ret = [(i, abstract) for i, abstract in enumerate(to_concretize) if abstract.concrete]
    try:
        # Ensure we don't try to bootstrap clingo in parallel
        importlib.import_module("clingo")
    except ImportError:
        with ensure_bootstrap_configuration():
            ensure_clingo_importable_or_raise()

    # Ensure all the indexes have been built or updated, since
    # otherwise the processes in the pool may timeout on waiting
    # for a write lock. We do this indirectly by retrieving the
    # provider index, which should in turn trigger the update of
    # all the indexes if there's any need for that.
    _ = spack.repo.PATH.provider_index

    # Ensure we have compilers in packages.yaml to avoid that
    # processes try to write the config file in parallel
    _ = spack.compilers.config.all_compilers()

    # Early return if there is nothing to do
    if len(args) == 0:
        # Still have to combine the things that were passed in as abstract with the things
        # that were passed in as pairs
        return [(abstract, concrete) for abstract, (_, concrete) in zip(to_concretize, ret)] + [
            (abstract, concrete) for abstract, concrete in spec_list if concrete
        ]

    # Solve the environment in parallel on Linux
    # TODO: support parallel concretization on macOS and Windows
    num_procs = min(len(args), spack.config.determine_number_of_jobs(parallel=True))

    msg = "Starting concretization"
    if sys.platform not in ("darwin", "win32") and num_procs > 1:
        msg += f" pool with {num_procs} processes"
    tty.msg(msg)

    for j, (i, concrete, duration) in enumerate(
        spack.util.parallel.imap_unordered(
            _concretize_task, args, processes=num_procs, debug=tty.is_debug(), maxtaskperchild=1
        )
    ):
        ret.append((i, concrete))
        percentage = (j + 1) / len(args) * 100
        tty.verbose(
            f"{duration:6.1f}s [{percentage:3.0f}%] {concrete.cformat('{hash:7}')} "
            f"{to_concretize[i].colored_str}"
        )
        sys.stdout.flush()

    # Add specs in original order
    ret.sort(key=lambda x: x[0])

    return [(abstract, concrete) for abstract, (_, concrete) in zip(to_concretize, ret)] + [
        (abstract, concrete) for abstract, concrete in spec_list if concrete
    ]


def _concretize_task(packed_arguments: Tuple[int, str, TestsType]) -> Tuple[int, Spec, float]:
    index, spec_str, tests = packed_arguments
    with tty.SuppressOutput(msg_enabled=False):
        start = time.time()
        spec = concretize_one(Spec(spec_str), tests=tests)
        return index, spec, time.time() - start


def concretize_one(spec: Union[str, Spec], tests: TestsType = False) -> Spec:
    """Return a concretized copy of the given spec.

    Args:
        tests: if False disregard 'test' dependencies, if a list of names activate them for
            the packages in the list, if True activate 'test' dependencies for all packages.
    """
    from spack.solver.asp import Solver, SpecBuilder

    if isinstance(spec, str):
        spec = Spec(spec)
    spec = spec.lookup_hash()

    if spec.concrete:
        return spec.copy()

    for node in spec.traverse():
        if not node.name:
            raise spack.error.SpecError(
                f"Spec {node} has no name; cannot concretize an anonymous spec"
            )

    allow_deprecated = spack.config.get("config:deprecated", False)
    result = Solver().solve([spec], tests=tests, allow_deprecated=allow_deprecated)

    # take the best answer
    opt, i, answer = min(result.answers)
    name = spec.name
    # TODO: Consolidate this code with similar code in solve.py
    if spack.repo.PATH.is_virtual(spec.name):
        providers = [s.name for s in answer.values() if s.package.provides(name)]
        name = providers[0]

    node = SpecBuilder.make_node(pkg=name)
    assert (
        node in answer
    ), f"cannot find {name} in the list of specs {','.join([n.pkg for n in answer.keys()])}"

    concretized = answer[node]
    return concretized


class UnavailableCompilerVersionError(spack.error.SpackError):
    """Raised when there is no available compiler that satisfies a
    compiler spec."""

    def __init__(self, compiler_spec: CompilerSpec, arch: Optional[ArchSpec] = None) -> None:
        err_msg = f"No compilers with spec {compiler_spec} found"
        if arch:
            err_msg += f" for operating system {arch.os} and target {arch.target}."

        super().__init__(
            err_msg,
            "Run 'spack compiler find' to add compilers or "
            "'spack compilers' to see which compilers are already recognized"
            " by spack.",
        )
