# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)
import os
import pathlib
import platform
import sys

import _vendoring.archspec.cpu
import _vendoring.jinja2
import pytest

import llnl.util.lang

import spack.binary_distribution
import spack.cmd
import spack.compilers.config
import spack.concretize
import spack.config
import spack.deptypes as dt
import spack.detection
import spack.error
import spack.hash_types as ht
import spack.package_base
import spack.paths
import spack.platforms
import spack.platforms.test
import spack.repo
import spack.solver.asp
import spack.solver.version_order
import spack.spec
import spack.store
import spack.util.file_cache
import spack.util.spack_yaml as syaml
import spack.variant as vt
from spack.installer import PackageInstaller
from spack.spec import Spec
from spack.version import Version, VersionList, ver


def check_spec(abstract, concrete):
    if abstract.versions.concrete:
        assert abstract.versions == concrete.versions

    if abstract.variants:
        for name in abstract.variants:
            avariant = abstract.variants[name]
            cvariant = concrete.variants[name]
            assert avariant.value == cvariant.value

    if abstract.compiler_flags:
        for flag in abstract.compiler_flags:
            aflag = abstract.compiler_flags[flag]
            cflag = concrete.compiler_flags[flag]
            assert set(aflag) <= set(cflag)

    for name in spack.repo.PATH.get_pkg_class(abstract.name).variant_names():
        assert name in concrete.variants

    for flag in concrete.compiler_flags.valid_compiler_flags():
        assert flag in concrete.compiler_flags

    if abstract.architecture and abstract.architecture.concrete:
        assert abstract.architecture == concrete.architecture


def check_concretize(abstract_spec):
    abstract = Spec(abstract_spec)
    concrete = spack.concretize.concretize_one(abstract)
    assert not abstract.concrete
    assert concrete.concrete
    check_spec(abstract, concrete)
    return concrete


@pytest.fixture(scope="function", autouse=True)
def binary_compatibility(monkeypatch, request):
    """Selects whether we use OS compatibility for binaries, or libc compatibility."""
    if spack.platforms.real_host().name != "linux":
        return

    if "mock_packages" not in request.fixturenames:
        # Only builtin_mock has a mock glibc package
        return

    if "database" in request.fixturenames or "mutable_database" in request.fixturenames:
        # Databases have been created without glibc support
        return

    monkeypatch.setattr(spack.solver.asp, "using_libc_compatibility", lambda: True)


@pytest.fixture(
    params=[
        # no_deps
        "libelf",
        "libelf@0.8.13",
        # dag
        "callpath",
        "mpileaks",
        "libelf",
        # variant
        "mpich+debug",
        "mpich~debug",
        "mpich debug=True",
        "mpich",
        # compiler flags
        'mpich cppflags="-O3"',
        'mpich cppflags=="-O3"',
        # with virtual
        "mpileaks ^mpi",
        "mpileaks ^mpi@:1.1",
        "mpileaks ^mpi@2:",
        "mpileaks ^mpi@2.1",
        "mpileaks ^mpi@2.2",
        "mpileaks ^mpi@2.2",
        "mpileaks ^mpi@:1",
        "mpileaks ^mpi@1.2:2",
        # conflict not triggered
        "conflict",
        "conflict~foo%clang",
        "conflict-parent%gcc",
        # Direct dependency with different deptypes
        "mpileaks %[deptypes=link] mpich",
    ]
)
def spec(request):
    """Spec to be concretized"""
    return request.param


@pytest.fixture(
    params=[
        # Mocking the host detection
        "haswell",
        "broadwell",
        "skylake",
        "icelake",
        # Using preferred targets from packages.yaml
        "icelake-preference",
        "cannonlake-preference",
    ]
)
def current_host(request, monkeypatch):
    # is_preference is not empty if we want to supply the
    # preferred target via packages.yaml
    cpu, _, is_preference = request.param.partition("-")

    monkeypatch.setattr(spack.platforms.Test, "default", cpu)
    if not is_preference:
        target = _vendoring.archspec.cpu.TARGETS[cpu]
        monkeypatch.setattr(_vendoring.archspec.cpu, "host", lambda: target)
        yield target
    else:
        target = _vendoring.archspec.cpu.TARGETS["sapphirerapids"]
        monkeypatch.setattr(_vendoring.archspec.cpu, "host", lambda: target)
        with spack.config.override("packages:all", {"target": [cpu]}):
            yield target


@pytest.fixture(scope="function", params=[True, False])
def fuzz_dep_order(request, monkeypatch):
    """Meta-function that tweaks the order of iteration over dependencies in a package."""

    def reverser(pkg_name):
        if request.param:
            pkg_cls = spack.repo.PATH.get_pkg_class(pkg_name)
            reversed_dict = dict(reversed(list(pkg_cls.dependencies.items())))
            monkeypatch.setattr(pkg_cls, "dependencies", reversed_dict)

    return reverser


@pytest.fixture()
def repo_with_changing_recipe(tmp_path_factory, mutable_mock_repo):
    repos_dir: pathlib.Path = tmp_path_factory.mktemp("repos_dir")
    root, _ = spack.repo.create_repo(str(repos_dir), "changing")
    packages_dir = pathlib.Path(root, "packages")

    root_pkg_str = """
from spack_repo.builtin_mock.build_systems.generic import Package
from spack.package import *

class Root(Package):
    homepage = "http://www.example.com"
    url      = "http://www.example.com/root-1.0.tar.gz"

    version("1.0", sha256="abcde")
    depends_on("middle")
    depends_on("changing")

    conflicts("^changing~foo")
"""
    package_py = packages_dir / "root" / "package.py"
    package_py.parent.mkdir(parents=True)
    package_py.write_text(root_pkg_str)

    middle_pkg_str = """
from spack_repo.builtin_mock.build_systems.generic import Package
from spack.package import *

class Middle(Package):
    homepage = "http://www.example.com"
    url      = "http://www.example.com/root-1.0.tar.gz"

    version("1.0", sha256="abcde")
    depends_on("changing")
"""
    package_py = packages_dir / "middle" / "package.py"
    package_py.parent.mkdir(parents=True)
    package_py.write_text(middle_pkg_str)

    changing_template = """
from spack_repo.builtin_mock.build_systems.generic import Package
from spack.package import *

class Changing(Package):
    homepage = "http://www.example.com"
    url      = "http://www.example.com/changing-1.0.tar.gz"


{% if not delete_version %}
    version("1.0", sha256="abcde")
{% endif %}
    version("0.9", sha256="abcde")

{% if not delete_variant %}
    variant("fee", default=True, description="nope")
{% endif %}
    variant("foo", default=True, description="nope")
{% if add_variant %}
    variant("fum", default=True, description="nope")
    variant("fum2", default=True, description="nope")
{% endif %}
"""

    with spack.repo.use_repositories(root, override=False) as repos:

        class _ChangingPackage:
            default_context = [
                ("delete_version", True),
                ("delete_variant", False),
                ("add_variant", False),
            ]

            def __init__(self):
                cache_dir = tmp_path_factory.mktemp("cache")
                self.repo_cache = spack.util.file_cache.FileCache(str(cache_dir))
                self.repo = spack.repo.Repo(root, cache=self.repo_cache)

            def change(self, changes=None):
                changes = changes or {}
                context = dict(self.default_context)
                context.update(changes)
                # Remove the repo object and delete Python modules
                repos.remove(self.repo)
                # TODO: this mocks a change in the recipe that should happen in a
                # TODO: different process space. Leaving this comment as a hint
                # TODO: in case tests using this fixture start failing.
                for module in [x for x in sys.modules if x.startswith("spack_repo.changing")]:
                    del sys.modules[module]

                # Change the recipe
                t = _vendoring.jinja2.Template(changing_template)
                changing_pkg_str = t.render(**context)
                package_py = packages_dir / "changing" / "package.py"
                package_py.parent.mkdir(parents=True, exist_ok=True)
                package_py.write_text(changing_pkg_str)

                # Re-add the repository
                self.repo = spack.repo.Repo(root, cache=self.repo_cache)
                repos.put_first(self.repo)

        _changing_pkg = _ChangingPackage()
        _changing_pkg.change(
            {"delete_version": False, "delete_variant": False, "add_variant": False}
        )
        yield _changing_pkg


@pytest.fixture()
def clang12_with_flags(compiler_factory):
    c = compiler_factory(spec="llvm@12.2.0 os=redhat6")
    c["extra_attributes"]["flags"] = {"cflags": "-O3", "cxxflags": "-O3"}
    return c


@pytest.fixture()
def gcc11_with_flags(compiler_factory):
    c = compiler_factory(spec="gcc@11.1.0 os=redhat6")
    c["extra_attributes"]["flags"] = {"cflags": "-O0 -g", "cxxflags": "-O0 -g", "fflags": "-O0 -g"}
    return c


# This must use the mutable_config fixture because the test
# adjusting_default_target_based_on_compiler uses the current_host fixture,
# which changes the config.
@pytest.mark.usefixtures("mutable_config", "mock_packages", "do_not_check_runtimes_on_reuse")
class TestConcretize:
    def test_concretize(self, spec):
        check_concretize(spec)

    def test_concretize_mention_build_dep(self):
        spec = check_concretize("cmake-client ^cmake@=3.21.3")

        # Check parent's perspective of child
        to_dependencies = spec.edges_to_dependencies(name="cmake")
        assert len(to_dependencies) == 1
        assert to_dependencies[0].depflag == dt.BUILD

        # Check child's perspective of parent
        cmake = spec["cmake"]
        from_dependents = cmake.edges_from_dependents(name="cmake-client")
        assert len(from_dependents) == 1
        assert from_dependents[0].depflag == dt.BUILD

    def test_concretize_preferred_version(self):
        spec = check_concretize("python")
        assert spec.version == ver("=2.7.11")
        spec = check_concretize("python@3.5.1")
        assert spec.version == ver("=3.5.1")

    def test_concretize_with_restricted_virtual(self):
        check_concretize("mpileaks ^mpich2")

        concrete = check_concretize("mpileaks   ^mpich2@1.1")
        assert concrete["mpich2"].satisfies("mpich2@1.1")

        concrete = check_concretize("mpileaks   ^mpich2@1.2")
        assert concrete["mpich2"].satisfies("mpich2@1.2")

        concrete = check_concretize("mpileaks   ^mpich2@:1.5")
        assert concrete["mpich2"].satisfies("mpich2@:1.5")

        concrete = check_concretize("mpileaks   ^mpich2@:1.3")
        assert concrete["mpich2"].satisfies("mpich2@:1.3")

        concrete = check_concretize("mpileaks   ^mpich2@:1.2")
        assert concrete["mpich2"].satisfies("mpich2@:1.2")

        concrete = check_concretize("mpileaks   ^mpich2@:1.1")
        assert concrete["mpich2"].satisfies("mpich2@:1.1")

        concrete = check_concretize("mpileaks   ^mpich2@1.1:")
        assert concrete["mpich2"].satisfies("mpich2@1.1:")

        concrete = check_concretize("mpileaks   ^mpich2@1.5:")
        assert concrete["mpich2"].satisfies("mpich2@1.5:")

        concrete = check_concretize("mpileaks   ^mpich2@1.3.1:1.4")
        assert concrete["mpich2"].satisfies("mpich2@1.3.1:1.4")

    def test_concretize_with_provides_when(self):
        """Make sure insufficient versions of MPI are not in providers list when
        we ask for some advanced version.
        """
        repo = spack.repo.PATH
        assert not any(s.intersects("mpich2@:1.0") for s in repo.providers_for("mpi@2.1"))
        assert not any(s.intersects("mpich2@:1.1") for s in repo.providers_for("mpi@2.2"))
        assert not any(s.intersects("mpich@:1") for s in repo.providers_for("mpi@2"))
        assert not any(s.intersects("mpich@:1") for s in repo.providers_for("mpi@3"))
        assert not any(s.intersects("mpich2") for s in repo.providers_for("mpi@3"))

    def test_provides_handles_multiple_providers_of_same_version(self):
        """ """
        providers = spack.repo.PATH.providers_for("mpi@3.0")

        # Note that providers are repo-specific, so we don't misinterpret
        # providers, but vdeps are not namespace-specific, so we can
        # associate vdeps across repos.
        assert Spec("builtin_mock.multi-provider-mpi@1.10.3") in providers
        assert Spec("builtin_mock.multi-provider-mpi@1.10.2") in providers
        assert Spec("builtin_mock.multi-provider-mpi@1.10.1") in providers
        assert Spec("builtin_mock.multi-provider-mpi@1.10.0") in providers
        assert Spec("builtin_mock.multi-provider-mpi@1.8.8") in providers

    def test_different_compilers_get_different_flags(
        self, mutable_config, clang12_with_flags, gcc11_with_flags
    ):
        """Tests that nodes get the flags of the associated compiler."""
        mutable_config.set(
            "packages",
            {
                "llvm": {"externals": [clang12_with_flags]},
                "gcc": {"externals": [gcc11_with_flags]},
            },
        )
        t = _vendoring.archspec.cpu.host().family
        client = spack.concretize.concretize_one(
            Spec(
                f"cmake-client platform=test os=redhat6 target={t} %gcc@11.1.0"
                f" ^cmake platform=test os=redhat6 target={t} %clang@12.2.0"
            )
        )
        cmake = client["cmake"]
        assert set(client.compiler_flags["cflags"]) == {"-O0", "-g"}
        assert set(cmake.compiler_flags["cflags"]) == {"-O3"}
        assert set(client.compiler_flags["fflags"]) == {"-O0", "-g"}
        assert not set(cmake.compiler_flags["fflags"])

    @pytest.mark.regression("9908")
    def test_spec_flags_maintain_order(self, mutable_config, gcc11_with_flags):
        """Tests that Spack assembles flags in a consistent way (i.e. with the same ordering),
        for successive concretizations.
        """
        mutable_config.set("packages", {"gcc": {"externals": [gcc11_with_flags]}})
        spec_str = "libelf os=redhat6 %gcc@11.1.0"
        for _ in range(3):
            s = spack.concretize.concretize_one(spec_str)
            assert all(
                s.compiler_flags[x] == ["-O0", "-g"] for x in ("cflags", "cxxflags", "fflags")
            )

    @pytest.mark.parametrize(
        "spec_str,expected,not_expected",
        [
            # Simple flag propagation from the root
            ("hypre cflags=='-g' ^openblas", ["hypre cflags='-g'", "^openblas cflags='-g'"], []),
            (
                "hypre cflags='-g' ^openblas",
                ["hypre cflags='-g'", "^openblas"],
                ["^openblas cflags='-g'"],
            ),
            # Setting a flag overrides propagation
            (
                "hypre cflags=='-g' ^openblas cflags='-O3'",
                ["hypre cflags='-g'", "^openblas cflags='-O3'"],
                ["^openblas cflags='-g'"],
            ),
            # Propagation doesn't go across build dependencies
            (
                "cmake-client cflags=='-O2 -g'",
                ["cmake-client cflags=='-O2 -g'", "^cmake"],
                ["cmake cflags=='-O2 -g'"],
            ),
        ],
    )
    def test_compiler_flag_propagation(self, spec_str, expected, not_expected):
        root = spack.concretize.concretize_one(spec_str)

        for constraint in expected:
            assert root.satisfies(constraint)

        for constraint in not_expected:
            assert not root.satisfies(constraint)

    def test_mixing_compilers_only_affects_subdag(self):
        """Tests that, when we mix compilers, the one with lower penalty is used for nodes
        where the compiler is not forced.
        """
        spec = spack.concretize.concretize_one("dt-diamond%clang ^dt-diamond-bottom%gcc")
        # This is intended to traverse the "root" unification set, and check compilers
        # on the nodes in the set
        for x in spec.traverse(deptype=("link", "run")):
            if "c" not in x or not x.name.startswith("dt-diamond"):
                continue
            expected_gcc = x.name != "dt-diamond"
            assert bool(x.dependencies(name="llvm", deptype="build")) is not expected_gcc, x.tree()
            assert bool(x.dependencies(name="gcc", deptype="build")) is expected_gcc
            assert x.satisfies("%clang") is not expected_gcc
            assert x.satisfies("%gcc") is expected_gcc

    def test_compiler_inherited_upwards(self):
        spec = spack.concretize.concretize_one("dt-diamond ^dt-diamond-bottom%clang")
        for x in spec.traverse(deptype=("link", "run")):
            if "c" not in x:
                continue
            assert x.satisfies("%clang")

    def test_architecture_deep_inheritance(self, mock_targets, compiler_factory):
        """Make sure that indirect dependencies receive architecture
        information from the root even when partial architecture information
        is provided by an intermediate dependency.
        """
        cnl_compiler = compiler_factory(spec="gcc@4.5.0 os=CNL target=nocona")
        with spack.config.override("packages", {"gcc": {"externals": [cnl_compiler]}}):
            spec_str = "mpileaks os=CNL target=nocona %gcc@4.5.0 ^dyninst os=CNL ^callpath os=CNL"
            spec = spack.concretize.concretize_one(spec_str)
            for s in spec.traverse(root=False, deptype=("link", "run")):
                assert s.architecture.target == spec.architecture.target

    def test_compiler_flags_from_user_are_grouped(self):
        spec = Spec('pkg-a cflags="-O -foo-flag foo-val" platform=test %gcc')
        spec = spack.concretize.concretize_one(spec)
        cflags = spec.compiler_flags["cflags"]
        assert any(x == "-foo-flag foo-val" for x in cflags)

    def concretize_multi_provider(self):
        s = Spec("mpileaks ^multi-provider-mpi@3.0")
        s = spack.concretize.concretize_one(s)
        assert s["mpi"].version == ver("1.10.3")

    def test_concretize_dependent_with_singlevalued_variant_type(self):
        s = Spec("singlevalue-variant-dependent-type")
        s = spack.concretize.concretize_one(s)

    @pytest.mark.parametrize("spec,version", [("dealii", "develop"), ("xsdk", "0.4.0")])
    def concretize_difficult_packages(self, a, b):
        """Test a couple of large packages that are often broken due
        to current limitations in the concretizer"""
        s = Spec(a + "@" + b)
        s = spack.concretize.concretize_one(s)
        assert s[a].version == ver(b)

    def test_concretize_two_virtuals(self):
        """Test a package with multiple virtual dependencies."""
        spack.concretize.concretize_one("hypre")

    def test_concretize_two_virtuals_with_one_bound(self, mutable_mock_repo):
        """Test a package with multiple virtual dependencies and one preset."""
        spack.concretize.concretize_one("hypre ^openblas")

    def test_concretize_two_virtuals_with_two_bound(self):
        """Test a package with multiple virtual deps and two of them preset."""
        spack.concretize.concretize_one("hypre ^netlib-lapack")

    def test_concretize_two_virtuals_with_dual_provider(self):
        """Test a package with multiple virtual dependencies and force a provider
        that provides both.
        """
        spack.concretize.concretize_one("hypre ^openblas-with-lapack")

    def test_concretize_two_virtuals_with_dual_provider_and_a_conflict(self):
        """Test a package with multiple virtual dependencies and force a
        provider that provides both, and another conflicting package that
        provides one.
        """
        s = Spec("hypre ^openblas-with-lapack ^netlib-lapack")
        with pytest.raises(spack.error.SpackError):
            spack.concretize.concretize_one(s)

    @pytest.mark.parametrize(
        "spec_str,expected_propagation",
        [
            # Propagates past a node that doesn't have the variant
            ("hypre~~shared ^openblas", [("hypre", "~shared"), ("openblas", "~shared")]),
            # Propagates from root node to all nodes
            (
                "ascent~~shared +adios2",
                [("ascent", "~shared"), ("adios2", "~shared"), ("bzip2", "~shared")],
            ),
            # Propagate from a node that is not the root node
            (
                "ascent +adios2 ^adios2~~shared",
                [("ascent", "+shared"), ("adios2", "~shared"), ("bzip2", "~shared")],
            ),
        ],
    )
    def test_concretize_propagate_disabled_variant(self, spec_str, expected_propagation):
        """Tests various patterns of boolean variant propagation"""
        spec = spack.concretize.concretize_one(spec_str)
        for key, expected_satisfies in expected_propagation:
            spec[key].satisfies(expected_satisfies)

    def test_concretize_propagate_variant_not_dependencies(self):
        """Test that when propagating a variant it is not propagated to dependencies that
        do not have that variant"""
        spec = Spec("quantum-espresso~~invino")
        spec = spack.concretize.concretize_one(spec)

        for dep in spec.traverse(root=False):
            assert "invino" not in dep.variants.keys()

    def test_concretize_propagate_variant_exclude_dependency_fail(self):
        """Tests that a propagating variant cannot be allowed to be excluded by any of
        the source package's dependencies"""
        spec = Spec("hypre ~~shared ^openblas +shared")
        with pytest.raises(spack.error.UnsatisfiableSpecError):
            spec = spack.concretize.concretize_one(spec)

    def test_concretize_propagate_same_variant_from_direct_dep_fail(self):
        """Test that when propagating a variant from the source package and a direct
        dependency also propagates the same variant with a different value. Raises error"""
        spec = Spec("ascent +adios2 ++shared ^adios2 ~~shared")
        with pytest.raises(spack.error.UnsatisfiableSpecError):
            spec = spack.concretize.concretize_one(spec)

    def test_concretize_propagate_same_variant_in_dependency_fail(self):
        """Test that when propagating a variant from the source package, none of it's
        dependencies can propagate that variant with a different value. Raises error."""
        spec = Spec("ascent +adios2 ++shared ^bzip2 ~~shared")
        with pytest.raises(spack.error.UnsatisfiableSpecError):
            spec = spack.concretize.concretize_one(spec)

    def test_concretize_propagate_same_variant_virtual_dependency_fail(self):
        """Test that when propagating a variant from the source package and a direct
        dependency (that is a virtual pkg) also propagates the same variant with a
        different value. Raises error"""
        spec = Spec("hypre ++shared ^openblas ~~shared")
        with pytest.raises(spack.error.UnsatisfiableSpecError):
            spec = spack.concretize.concretize_one(spec)

    def test_concretize_propagate_same_variant_multiple_sources_diamond_dep_fail(self):
        """Test that fails when propagating the same variant with different values from multiple
        sources that share a dependency"""
        spec = Spec("parent-foo-bar ^dependency-foo-bar++bar ^direct-dep-foo-bar~~bar")
        with pytest.raises(spack.error.UnsatisfiableSpecError):
            spec = spack.concretize.concretize_one(spec)

    def test_concretize_propagate_specified_variant(self):
        """Test that only the specified variant is propagated to the dependencies"""
        spec = Spec("parent-foo-bar ~~foo")
        spec = spack.concretize.concretize_one(spec)

        assert spec.satisfies("^dependency-foo-bar~foo")
        assert spec.satisfies("^second-dependency-foo-bar-fee~foo")
        assert spec.satisfies("^direct-dep-foo-bar~foo")

        assert not spec.satisfies("^dependency-foo-bar+bar")
        assert not spec.satisfies("^second-dependency-foo-bar-fee+bar")
        assert not spec.satisfies("^direct-dep-foo-bar+bar")

    def test_concretize_propagate_one_variant(self):
        """Test that you can specify to propagate one variant and not all"""
        spec = Spec("parent-foo-bar ++bar ~foo")
        spec = spack.concretize.concretize_one(spec)

        assert spec.satisfies("~foo") and not spec.satisfies("^dependency-foo-bar~foo")
        assert spec.satisfies("+bar") and spec.satisfies("^dependency-foo-bar+bar")

    def test_concretize_propagate_through_first_level_deps(self):
        """Test that boolean valued variants can be propagated past first level
        dependecies even if the first level dependency does have the variant"""
        spec = Spec("parent-foo-bar-fee ++fee")
        spec = spack.concretize.concretize_one(spec)

        assert spec.satisfies("+fee") and not spec.satisfies("dependency-foo-bar+fee")
        assert spec.satisfies("^second-dependency-foo-bar-fee+fee")

    def test_concretize_propagate_multiple_variants(self):
        """Test that multiple boolean valued variants can be propagated from
        the same source package"""
        spec = Spec("parent-foo-bar-fee ~~foo ++bar")
        spec = spack.concretize.concretize_one(spec)

        assert spec.satisfies("~foo") and spec.satisfies("+bar")
        assert spec.satisfies("^dependency-foo-bar ~foo +bar")
        assert spec.satisfies("^second-dependency-foo-bar-fee ~foo +bar")

    def test_concretize_propagate_multiple_variants_mulitple_sources(self):
        """Test the propagates multiple different variants for multiple sources
        in a diamond dependency"""
        spec = Spec("parent-foo-bar ^dependency-foo-bar++bar ^direct-dep-foo-bar~~foo")
        spec = spack.concretize.concretize_one(spec)

        assert spec.satisfies("^second-dependency-foo-bar-fee+bar")
        assert spec.satisfies("^second-dependency-foo-bar-fee~foo")
        assert not spec.satisfies("^dependency-foo-bar~foo")
        assert not spec.satisfies("^direct-dep-foo-bar+bar")

    def test_concretize_propagate_single_valued_variant(self):
        """Test propagation for single valued variants"""
        spec = Spec("multivalue-variant libs==static")
        spec = spack.concretize.concretize_one(spec)

        assert spec.satisfies("libs=static")
        assert spec.satisfies("^pkg-a libs=static")

    def test_concretize_propagate_multivalue_variant(self):
        """Test that multivalue variants are propagating the specified value(s)
        to their dependecies. The dependencies should not have the default value"""
        spec = Spec("multivalue-variant foo==baz,fee")
        spec = spack.concretize.concretize_one(spec)

        assert spec.satisfies("^pkg-a foo=baz,fee")
        assert spec.satisfies("^pkg-b foo=baz,fee")
        assert not spec.satisfies("^pkg-a foo=bar")
        assert not spec.satisfies("^pkg-b foo=bar")

    def test_concretize_propagate_multiple_multivalue_variant(self):
        """Tests propagating the same mulitvalued variant from different sources allows
        the dependents to accept all propagated values"""
        spec = Spec("multivalue-variant foo==bar ^pkg-a foo==baz")
        spec = spack.concretize.concretize_one(spec)

        assert spec.satisfies("multivalue-variant foo=bar")
        assert spec.satisfies("^pkg-a foo=bar,baz")
        assert spec.satisfies("^pkg-b foo=bar,baz")

    def test_concretize_propagate_variant_not_in_source(self):
        """Test that variant is still propagated even if the source pkg
        doesn't have the variant"""
        spec = Spec("callpath++debug")
        spec = spack.concretize.concretize_one(spec)

        assert spec.satisfies("^mpich+debug")
        assert not spec.satisfies("callpath+debug")
        assert not spec.satisfies("^dyninst+debug")

    def test_concretize_propagate_variant_multiple_deps_not_in_source(self):
        """Test that a variant can be propagated to multiple dependencies
        when the variant is not in the source package"""
        spec = Spec("netlib-lapack++shared")
        spec = spack.concretize.concretize_one(spec)

        assert spec.satisfies("^openblas+shared")
        assert spec.satisfies("^perl+shared")
        assert not spec.satisfies("netlib-lapack+shared")

    def test_concretize_propagate_variant_second_level_dep_not_in_source(self):
        """Test that a variant can be propagated past first level dependencies
        when the variant is not in the source package or any of the first level
        dependencies"""
        spec = Spec("parent-foo-bar ++fee")
        spec = spack.concretize.concretize_one(spec)

        assert spec.satisfies("^second-dependency-foo-bar-fee +fee")
        assert not spec.satisfies("parent-foo-bar +fee")

    def test_no_matching_compiler_specs(self):
        s = Spec("pkg-a %gcc@0.0.0")
        with pytest.raises(spack.solver.asp.UnsatisfiableSpecError):
            spack.concretize.concretize_one(s)

    def test_no_compilers_for_arch(self):
        s = Spec("pkg-a arch=linux-rhel0-x86_64")
        with pytest.raises(spack.error.SpackError):
            s = spack.concretize.concretize_one(s)

    def test_virtual_is_fully_expanded_for_callpath(self):
        # force dependence on fake "zmpi" by asking for MPI 10.0
        spec = Spec("callpath ^mpi@10.0")
        assert len(spec.dependencies(name="mpi")) == 1
        assert "fake" not in spec

        spec = spack.concretize.concretize_one(spec)
        assert len(spec.dependencies(name="zmpi")) == 1
        assert all(not d.dependencies(name="mpi") for d in spec.traverse())
        assert all(x in spec for x in ("zmpi", "mpi"))

        edges_to_zmpi = spec.edges_to_dependencies(name="zmpi")
        assert len(edges_to_zmpi) == 1
        assert "fake" in edges_to_zmpi[0].spec

    def test_virtual_is_fully_expanded_for_mpileaks(self):
        spec = Spec("mpileaks ^mpi@10.0")
        assert len(spec.dependencies(name="mpi")) == 1
        assert "fake" not in spec

        spec = spack.concretize.concretize_one(spec)
        assert len(spec.dependencies(name="zmpi")) == 1
        assert len(spec.dependencies(name="callpath")) == 1

        callpath = spec.dependencies(name="callpath")[0]
        assert len(callpath.dependencies(name="zmpi")) == 1

        zmpi = callpath.dependencies(name="zmpi")[0]
        assert len(zmpi.dependencies(name="fake")) == 1

        assert all(not d.dependencies(name="mpi") for d in spec.traverse())
        assert all(x in spec for x in ("zmpi", "mpi"))

    @pytest.mark.parametrize(
        "spec_str,expected,not_expected",
        [
            # clang (llvm~flang) only provides C, and C++ compilers, while gcc has also fortran
            #
            # If we ask mpileaks%clang, then %gcc must be used for fortran, and since
            # %gcc is preferred to clang in config, it will be used for most nodes
            (
                "mpileaks %clang",
                {"mpileaks": "%clang", "libdwarf": "%gcc", "libelf": "%gcc"},
                {"libdwarf": "%clang", "libelf": "%clang"},
            ),
            (
                "mpileaks %clang@:15.0.0",
                {"mpileaks": "%clang", "libdwarf": "%gcc", "libelf": "%gcc"},
                {"libdwarf": "%clang", "libelf": "%clang"},
            ),
            (
                "mpileaks %gcc",
                {"mpileaks": "%gcc", "libdwarf": "%gcc", "libelf": "%gcc"},
                {"mpileaks": "%clang", "libdwarf": "%clang", "libelf": "%clang"},
            ),
            (
                "mpileaks %gcc@10.2.1",
                {"mpileaks": "%gcc", "libdwarf": "%gcc", "libelf": "%gcc"},
                {"mpileaks": "%clang", "libdwarf": "%clang", "libelf": "%clang"},
            ),
            # dyninst doesn't require fortran, so %clang is propagated
            (
                "dyninst %clang",
                {"dyninst": "%clang", "libdwarf": "%clang", "libelf": "%clang"},
                {"libdwarf": "%gcc", "libelf": "%gcc"},
            ),
        ],
    )
    def test_compiler_inheritance(self, spec_str, expected, not_expected):
        """Spack tries to propagate compilers as much as possible, but prefers using a single
        toolchain on a node, rather than mixing them.
        """
        spec = spack.concretize.concretize_one(spec_str)
        for name, constraint in expected.items():
            assert spec[name].satisfies(constraint)

        for name, constraint in not_expected.items():
            assert not spec[name].satisfies(constraint)

    def test_external_package(self):
        """Tests that an external is preferred, if present, and that it does not
        have dependencies.
        """
        spec = spack.concretize.concretize_one("externaltool")
        assert spec.external_path == os.path.sep + os.path.join("path", "to", "external_tool")
        assert not spec.dependencies()

    def test_nobuild_package(self):
        """Test that a non-buildable package raise an error if no specs
        in packages.yaml are compatible with the request.
        """
        spec = Spec("externaltool%clang")
        with pytest.raises(spack.error.SpecError):
            spec = spack.concretize.concretize_one(spec)

    def test_external_and_virtual(self, mutable_config):
        mutable_config.set("packages:stuff", {"buildable": False})
        spec = spack.concretize.concretize_one("externaltest")
        assert spec["externaltool"].external_path == os.path.sep + os.path.join(
            "path", "to", "external_tool"
        )
        # "stuff" is a virtual provided by externalvirtual
        assert spec["stuff"].external_path == os.path.sep + os.path.join(
            "path", "to", "external_virtual_clang"
        )

    def test_compiler_child(self):
        s = Spec("mpileaks target=x86_64 %clang ^dyninst%gcc")
        s = spack.concretize.concretize_one(s)
        assert s["mpileaks"].satisfies("%clang")
        assert s["dyninst"].satisfies("%gcc")

    def test_conflicts_in_spec(self, conflict_spec):
        s = Spec(conflict_spec)
        with pytest.raises(spack.error.SpackError):
            s = spack.concretize.concretize_one(s)

    def test_conflicts_show_cores(self, conflict_spec, monkeypatch):
        s = Spec(conflict_spec)
        with pytest.raises(spack.error.SpackError) as e:
            s = spack.concretize.concretize_one(s)

        assert "conflict" in e.value.message

    def test_conflict_in_all_directives_true(self):
        s = Spec("when-directives-true")
        with pytest.raises(spack.error.SpackError):
            s = spack.concretize.concretize_one(s)

    @pytest.mark.parametrize("spec_str", ["unsat-provider@1.0+foo"])
    def test_no_conflict_in_external_specs(self, spec_str):
        # Modify the configuration to have the spec with conflict
        # registered as an external
        ext = Spec(spec_str)
        data = {"externals": [{"spec": spec_str, "prefix": "/fake/path"}]}
        spack.config.set("packages::{0}".format(ext.name), data)
        ext = spack.concretize.concretize_one(ext)  # failure raises exception

    def test_regression_issue_4492(self):
        # Constructing a spec which has no dependencies, but is otherwise
        # concrete is kind of difficult. What we will do is to concretize
        # a spec, and then modify it to have no dependency and reset the
        # cache values.

        s = Spec("mpileaks")
        s = spack.concretize.concretize_one(s)

        # Check that now the Spec is concrete, store the hash
        assert s.concrete

        # Remove the dependencies and reset caches
        s.clear_dependencies()
        s._concrete = False

        assert not s.concrete

    @pytest.mark.regression("7239")
    def test_regression_issue_7239(self):
        # Constructing a SpecBuildInterface from another SpecBuildInterface
        # results in an inconsistent MRO

        # Normal Spec
        s = Spec("mpileaks")
        s = spack.concretize.concretize_one(s)

        assert llnl.util.lang.ObjectWrapper not in s.__class__.__mro__

        # Spec wrapped in a build interface
        build_interface = s["mpileaks"]
        assert llnl.util.lang.ObjectWrapper in build_interface.__class__.__mro__

        # Mimics asking the build interface from a build interface
        build_interface = s["mpileaks"]["mpileaks"]
        assert llnl.util.lang.ObjectWrapper in build_interface.__class__.__mro__

    @pytest.mark.regression("7705")
    def test_regression_issue_7705(self):
        # spec.package.provides(name) doesn't account for conditional
        # constraints in the concretized spec
        s = Spec("simple-inheritance~openblas")
        s = spack.concretize.concretize_one(s)

        assert not s.package.provides("lapack")

    @pytest.mark.regression("7941")
    def test_regression_issue_7941(self):
        # The string representation of a spec containing
        # an explicit multi-valued variant and a dependency
        # might be parsed differently than the originating spec
        s = Spec("pkg-a foobar=bar ^pkg-b")
        t = Spec(str(s))

        s = spack.concretize.concretize_one(s)
        t = spack.concretize.concretize_one(t)

        assert s.dag_hash() == t.dag_hash()

    @pytest.mark.parametrize(
        "abstract_specs",
        [
            # Establish a baseline - concretize a single spec
            ("mpileaks",),
            # When concretized together with older version of callpath
            # and dyninst it uses those older versions
            ("mpileaks", "callpath@0.9", "dyninst@8.1.1"),
            # Handle recursive syntax within specs
            ("mpileaks", "callpath@0.9 ^dyninst@8.1.1", "dyninst"),
            # Test specs that have overlapping dependencies but are not
            # one a dependency of the other
            ("mpileaks", "direct-mpich"),
        ],
    )
    def test_simultaneous_concretization_of_specs(self, abstract_specs):
        abstract_specs = [Spec(x) for x in abstract_specs]
        concrete_specs = spack.concretize._concretize_specs_together(abstract_specs)

        # Check there's only one configuration of each package in the DAG
        names = set(
            dep.name for spec in concrete_specs for dep in spec.traverse(deptype=("link", "run"))
        )
        for name in names:
            name_specs = set(spec[name] for spec in concrete_specs if name in spec)
            assert len(name_specs) == 1

        # Check that there's at least one Spec that satisfies the
        # initial abstract request
        for aspec in abstract_specs:
            assert any(cspec.satisfies(aspec) for cspec in concrete_specs)

        # Make sure the concrete spec are top-level specs with no dependents
        for spec in concrete_specs:
            assert not spec.dependents()

    @pytest.mark.parametrize("spec", ["noversion", "noversion-bundle"])
    def test_noversion_pkg(self, spec):
        """Test concretization failures for no-version packages."""
        with pytest.raises(spack.error.SpackError):
            spack.concretize.concretize_one(spec)

    @pytest.mark.not_on_windows("Not supported on Windows (yet)")
    @pytest.mark.parametrize(
        "spec,compiler_spec,best_achievable",
        [
            (
                "mpileaks%gcc@=4.4.7 ^dyninst@=10.2.1 target=x86_64:",
                "gcc@4.4.7 languages=c,c++,fortran",
                "core2",
            ),
            ("mpileaks target=x86_64: %gcc@=4.8", "gcc@4.8 languages=c,c++,fortran", "haswell"),
            (
                "mpileaks target=x86_64: %gcc@=5.3.0",
                "gcc@5.3.0 languages=c,c++,fortran",
                "broadwell",
            ),
        ],
    )
    @pytest.mark.regression("13361", "20537")
    @pytest.mark.usefixtures("mock_targets")
    def test_adjusting_default_target_based_on_compiler(
        self, spec, compiler_spec, best_achievable, current_host, compiler_factory, mutable_config
    ):
        best_achievable = _vendoring.archspec.cpu.TARGETS[best_achievable]
        expected = best_achievable if best_achievable < current_host else current_host
        mutable_config.set(
            "packages", {"gcc": {"externals": [compiler_factory(spec=f"{compiler_spec}")]}}
        )
        s = spack.concretize.concretize_one(spec)
        assert str(s.architecture.target) == str(expected)

    @pytest.mark.parametrize(
        "constraint,expected", [("%gcc@10.2", "@=10.2.1"), ("%gcc@10.2:", "@=10.2.1")]
    )
    def test_compiler_version_matches_any_entry_in_packages_yaml(self, constraint, expected):
        # The behavior here has changed since #8735 / #14730. Now %gcc@10.2 is an abstract
        # compiler spec, and it should first find a matching compiler gcc@=10.2.1
        s = spack.concretize.concretize_one(f"mpileaks {constraint}")
        gcc_deps = s.dependencies(name="gcc", deptype="build")
        assert len(gcc_deps) == 1
        assert gcc_deps[0].satisfies(expected)

    def test_concretize_anonymous(self):
        with pytest.raises(spack.error.SpackError):
            s = Spec("+variant")
            s = spack.concretize.concretize_one(s)

    @pytest.mark.parametrize("spec_str", ["mpileaks ^%gcc", "mpileaks ^cflags=-g"])
    def test_concretize_anonymous_dep(self, spec_str):
        with pytest.raises(spack.error.SpackError):
            s = Spec(spec_str)
            s = spack.concretize.concretize_one(s)

    @pytest.mark.parametrize(
        "spec_str,expected_str",
        [
            # Unconstrained versions select default compiler (gcc@10.2.1)
            ("bowtie@1.4.0", "%gcc@10.2.1"),
            # Version with conflicts and no valid gcc select another compiler
            ("bowtie@1.3.0", "%clang@15.0.0"),
            # If a higher gcc is available, with a worse os, still prefer that,
            # assuming the two operating systems are compatible
            ("bowtie@1.2.2 %gcc", "%gcc@11.1.0"),
        ],
    )
    def test_compiler_conflicts_in_package_py(
        self, spec_str, expected_str, gcc11_with_flags, mutable_config
    ):
        mutable_config.set(
            "concretizer:os_compatible", {"debian6": ["redhat6"], "redhat6": ["debian6"]}
        )
        with spack.config.override("packages", {"gcc": {"externals": [gcc11_with_flags]}}):
            s = spack.concretize.concretize_one(spec_str)
            assert s.satisfies(expected_str)

    @pytest.mark.parametrize(
        "spec_str,expected,unexpected",
        [
            ("conditional-variant-pkg@1.0", ["two_whens"], ["version_based", "variant_based"]),
            ("conditional-variant-pkg@2.0", ["version_based", "variant_based"], ["two_whens"]),
            (
                "conditional-variant-pkg@2.0~version_based",
                ["version_based"],
                ["variant_based", "two_whens"],
            ),
            (
                "conditional-variant-pkg@2.0+version_based+variant_based",
                ["version_based", "variant_based", "two_whens"],
                [],
            ),
        ],
    )
    def test_conditional_variants(self, spec_str, expected, unexpected):
        s = spack.concretize.concretize_one(spec_str)

        for var in expected:
            assert s.satisfies("%s=*" % var)
        for var in unexpected:
            assert not s.satisfies("%s=*" % var)

    @pytest.mark.parametrize(
        "bad_spec",
        [
            "@1.0~version_based",
            "@1.0+version_based",
            "@2.0~version_based+variant_based",
            "@2.0+version_based~variant_based+two_whens",
        ],
    )
    def test_conditional_variants_fail(self, bad_spec):
        with pytest.raises(
            (spack.error.UnsatisfiableSpecError, spack.spec.InvalidVariantForSpecError)
        ):
            _ = spack.concretize.concretize_one("conditional-variant-pkg" + bad_spec)

    @pytest.mark.parametrize(
        "spec_str,expected,unexpected",
        [
            ("py-extension3 ^python@3.5.1", [], ["py-extension1"]),
            ("py-extension3 ^python@2.7.11", ["py-extension1"], []),
            ("py-extension3@1.0 ^python@2.7.11", ["patchelf@0.9"], []),
            ("py-extension3@1.1 ^python@2.7.11", ["patchelf@0.9"], []),
            ("py-extension3@1.0 ^python@3.5.1", ["patchelf@0.10"], []),
        ],
    )
    def test_conditional_dependencies(self, spec_str, expected, unexpected, fuzz_dep_order):
        """Tests that conditional dependencies are correctly attached.

        The original concretizer can be sensitive to the iteration order over the dependencies of
        a package, so we use a fuzzer function to test concretization with dependencies iterated
        forwards and backwards.
        """
        fuzz_dep_order("py-extension3")  # test forwards and backwards

        s = spack.concretize.concretize_one(spec_str)

        for dep in expected:
            msg = '"{0}" is not in "{1}" and was expected'
            assert dep in s, msg.format(dep, spec_str)

        for dep in unexpected:
            msg = '"{0}" is in "{1}" but was unexpected'
            assert dep not in s, msg.format(dep, spec_str)

    @pytest.mark.parametrize(
        "spec_str,patched_deps",
        [
            ("patch-several-dependencies", [("libelf", 1), ("fake", 2)]),
            ("patch-several-dependencies@1.0", [("libelf", 1), ("fake", 2), ("libdwarf", 1)]),
            (
                "patch-several-dependencies@1.0 ^libdwarf@20111030",
                [("libelf", 1), ("fake", 2), ("libdwarf", 2)],
            ),
            ("patch-several-dependencies ^libelf@0.8.10", [("libelf", 2), ("fake", 2)]),
            ("patch-several-dependencies +foo", [("libelf", 2), ("fake", 2)]),
        ],
    )
    def test_patching_dependencies(self, spec_str, patched_deps):
        s = spack.concretize.concretize_one(spec_str)

        for dep, num_patches in patched_deps:
            assert s[dep].satisfies("patches=*")
            assert len(s[dep].variants["patches"].value) == num_patches

    @pytest.mark.regression("267,303,1781,2310,2632,3628")
    @pytest.mark.parametrize(
        "spec_str, expected",
        [
            # Need to understand that this configuration is possible
            # only if we use the +mpi variant, which is not the default
            ("fftw ^mpich", ["+mpi"]),
            # This spec imposes two orthogonal constraints on a dependency,
            # one of which is conditional. The original concretizer fail since
            # when it applies the first constraint, it sets the unknown variants
            # of the dependency to their default values
            ("quantum-espresso", ["^fftw@1.0+mpi"]),
            # This triggers a conditional dependency on ^fftw@1.0
            ("quantum-espresso", ["^openblas"]),
            # This constructs a constraint for a dependency og the type
            # @x.y:x.z where the lower bound is unconditional, the upper bound
            # is conditional to having a variant set
            ("quantum-espresso", ["^libelf@0.8.12"]),
            ("quantum-espresso~veritas", ["^libelf@0.8.13"]),
        ],
    )
    def test_working_around_conflicting_defaults(self, spec_str, expected):
        s = spack.concretize.concretize_one(spec_str)

        assert s.concrete
        for constraint in expected:
            assert s.satisfies(constraint)

    @pytest.mark.regression("5651")
    def test_package_with_constraint_not_met_by_external(self):
        """Check that if we have an external package A at version X.Y in
        packages.yaml, but our spec doesn't allow X.Y as a version, then
        a new version of A is built that meets the requirements.
        """
        packages_yaml = {"libelf": {"externals": [{"spec": "libelf@0.8.13", "prefix": "/usr"}]}}
        spack.config.set("packages", packages_yaml)

        # quantum-espresso+veritas requires libelf@:0.8.12
        s = spack.concretize.concretize_one("quantum-espresso+veritas")
        assert s.satisfies("^libelf@0.8.12")
        assert not s["libelf"].external

    @pytest.mark.regression("9744")
    def test_cumulative_version_ranges_with_different_length(self):
        s = spack.concretize.concretize_one("cumulative-vrange-root")
        assert s.concrete
        assert s.satisfies("^cumulative-vrange-bottom@2.2")

    @pytest.mark.regression("9937")
    def test_dependency_conditional_on_another_dependency_state(self):
        root_str = "variant-on-dependency-condition-root"
        dep_str = "variant-on-dependency-condition-a"
        spec_str = "{0} ^{1}".format(root_str, dep_str)

        s = spack.concretize.concretize_one(spec_str)
        assert s.concrete
        assert s.satisfies("^variant-on-dependency-condition-b")

        s = spack.concretize.concretize_one(spec_str + "+x")
        assert s.concrete
        assert s.satisfies("^variant-on-dependency-condition-b")

        s = spack.concretize.concretize_one(spec_str + "~x")
        assert s.concrete
        assert not s.satisfies("^variant-on-dependency-condition-b")

    def test_external_that_would_require_a_virtual_dependency(self):
        s = spack.concretize.concretize_one("requires-virtual")

        assert s.external
        assert "stuff" not in s

    def test_transitive_conditional_virtual_dependency(self, mutable_config):
        """Test that an external is used as provider if the virtual is non-buildable"""
        mutable_config.set("packages:stuff", {"buildable": False})
        s = spack.concretize.concretize_one("transitive-conditional-virtual-dependency")

        # Test that the default +stuff~mpi is maintained, and the right provider is selected
        assert s.satisfies("^conditional-virtual-dependency +stuff~mpi")
        assert s.satisfies("^[virtuals=stuff] externalvirtual")

    @pytest.mark.regression("20040")
    def test_conditional_provides_or_depends_on(self):
        # Check that we can concretize correctly a spec that can either
        # provide a virtual or depend on it based on the value of a variant
        s = spack.concretize.concretize_one("v1-consumer ^conditional-provider +disable-v1")
        assert "v1-provider" in s
        assert s["v1"].name == "v1-provider"
        assert s["v2"].name == "conditional-provider"

    @pytest.mark.regression("20079")
    @pytest.mark.parametrize(
        "spec_str,tests_arg,with_dep,without_dep",
        [
            # Check that True is treated correctly and attaches test deps
            # to all nodes in the DAG
            ("pkg-a", True, ["pkg-a"], []),
            ("pkg-a foobar=bar", True, ["pkg-a", "pkg-b"], []),
            # Check that a list of names activates the dependency only for
            # packages in that list
            ("pkg-a foobar=bar", ["pkg-a"], ["pkg-a"], ["pkg-b"]),
            ("pkg-a foobar=bar", ["pkg-b"], ["pkg-b"], ["pkg-a"]),
            # Check that False disregard test dependencies
            ("pkg-a foobar=bar", False, [], ["pkg-a", "pkg-b"]),
        ],
    )
    def test_activating_test_dependencies(self, spec_str, tests_arg, with_dep, without_dep):
        s = spack.concretize.concretize_one(spec_str, tests=tests_arg)

        for pkg_name in with_dep:
            msg = "Cannot find test dependency in package '{0}'"
            node = s[pkg_name]
            assert node.dependencies(deptype="test"), msg.format(pkg_name)

        for pkg_name in without_dep:
            msg = "Test dependency in package '{0}' is unexpected"
            node = s[pkg_name]
            assert not node.dependencies(deptype="test"), msg.format(pkg_name)

    @pytest.mark.regression("19981")
    def test_target_ranges_in_conflicts(self):
        with pytest.raises(spack.error.SpackError):
            spack.concretize.concretize_one("impossible-concretization")

    def test_target_compatibility(self):
        with pytest.raises(spack.error.SpackError):
            spack.concretize.concretize_one(
                Spec("libdwarf target=x86_64 ^libelf target=x86_64_v2")
            )

    @pytest.mark.regression("20040")
    def test_variant_not_default(self):
        s = spack.concretize.concretize_one("ecp-viz-sdk")

        # Check default variant value for the package
        assert "+dep" in s["conditional-constrained-dependencies"]

        # Check that non-default variant values are forced on the dependency
        d = s["dep-with-variants"]
        assert "+foo+bar+baz" in d

    def test_all_patches_applied(self):
        uuidpatch = (
            "a60a42b73e03f207433c5579de207c6ed61d58e4d12dd3b5142eb525728d89ea"
            if sys.platform != "win32"
            else "d0df7988457ec999c148a4a2af25ce831bfaad13954ba18a4446374cb0aef55e"
        )
        localpatch = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        spec = Spec("conditionally-patch-dependency+jasper")
        spec = spack.concretize.concretize_one(spec)
        assert (uuidpatch, localpatch) == spec["libelf"].variants["patches"].value

    def test_dont_select_version_that_brings_more_variants_in(self):
        s = spack.concretize.concretize_one("dep-with-variants-if-develop-root")
        assert s["dep-with-variants-if-develop"].satisfies("@1.0")

    @pytest.mark.regression("20244,20736")
    @pytest.mark.parametrize(
        "spec_str,is_external,expected",
        [
            # These are all externals, and 0_8 is a version not in package.py
            ("externaltool@1.0", True, "@1.0"),
            ("externaltool@0.9", True, "@0.9"),
            ("externaltool@0_8", True, "@0_8"),
            # This external package is buildable, has a custom version
            # in packages.yaml that is greater than the ones in package.py
            # and specifies a variant
            ("external-buildable-with-variant +baz", True, "@1.1.special +baz"),
            ("external-buildable-with-variant ~baz", False, "@1.0 ~baz"),
            ("external-buildable-with-variant@1.0: ~baz", False, "@1.0 ~baz"),
            # This uses an external version that meets the condition for
            # having an additional dependency, but the dependency shouldn't
            # appear in the answer set
            ("external-buildable-with-variant@0.9 +baz", True, "@0.9"),
            # This package has an external version declared that would be
            # the least preferred if Spack had to build it
            ("old-external", True, "@1.0.0"),
        ],
    )
    def test_external_package_versions(self, spec_str, is_external, expected):
        s = spack.concretize.concretize_one(spec_str)
        assert s.external == is_external
        assert s.satisfies(expected)

    @pytest.mark.parametrize("dev_first", [True, False])
    @pytest.mark.parametrize(
        "spec", ["dev-build-test-install", "dev-build-test-dependent ^dev-build-test-install"]
    )
    @pytest.mark.parametrize("mock_db", [True, False])
    def test_reuse_does_not_overwrite_dev_specs(
        self, dev_first, spec, mock_db, tmpdir, temporary_store, monkeypatch
    ):
        """Test that reuse does not mix dev specs with non-dev specs.

        Tests for either order (dev specs are not reused for non-dev, and
        non-dev specs are not reused for dev specs)
        Tests for a spec in which the root is developed and a spec in
        which a dep is developed.
        Tests for both reuse from database and reuse from buildcache"""
        # dev and non-dev specs that are otherwise identical
        spec = Spec(spec)
        dev_spec = spec.copy()
        dev_spec["dev-build-test-install"].constrain(f"dev_path={tmpdir.strpath}")

        # run the test in both orders
        first_spec = dev_spec if dev_first else spec
        second_spec = spec if dev_first else dev_spec

        # concretize and setup spack to reuse in the appropriate manner
        first_spec = spack.concretize.concretize_one(first_spec)

        def mock_fn(*args, **kwargs):
            return [first_spec]

        if mock_db:
            temporary_store.db.add(first_spec)
        else:
            monkeypatch.setattr(spack.binary_distribution, "update_cache_and_get_specs", mock_fn)

        # concretize and ensure we did not reuse
        with spack.config.override("concretizer:reuse", True):
            second_spec = spack.concretize.concretize_one(second_spec)
        assert first_spec.dag_hash() != second_spec.dag_hash()

    @pytest.mark.regression("20292")
    @pytest.mark.parametrize(
        "context",
        [
            {"add_variant": True, "delete_variant": False},
            {"add_variant": False, "delete_variant": True},
            {"add_variant": True, "delete_variant": True},
        ],
    )
    def test_reuse_installed_packages_when_package_def_changes(
        self, context, mutable_database, repo_with_changing_recipe
    ):
        # test applies only with reuse turned off in concretizer
        spack.config.set("concretizer:reuse", False)

        # Install a spec
        root = spack.concretize.concretize_one("root")
        dependency = root["changing"].copy()
        PackageInstaller([root.package], fake=True, explicit=True).install()

        # Modify package.py
        repo_with_changing_recipe.change(context)

        # Try to concretize with the spec installed previously
        new_root_with_reuse = spack.concretize.concretize_one(
            Spec("root ^/{0}".format(dependency.dag_hash()))
        )

        new_root_without_reuse = spack.concretize.concretize_one("root")

        # validate that the graphs are the same with reuse, but not without
        assert ht.build_hash(root) == ht.build_hash(new_root_with_reuse)
        assert ht.build_hash(root) != ht.build_hash(new_root_without_reuse)

        # DAG hash should be the same with reuse since only the dependency changed
        assert root.dag_hash() == new_root_with_reuse.dag_hash()

        # Structure and package hash will be different without reuse
        assert root.dag_hash() != new_root_without_reuse.dag_hash()

    @pytest.mark.regression("43663")
    def test_no_reuse_when_variant_condition_does_not_hold(self, mutable_database, mock_packages):
        spack.config.set("concretizer:reuse", True)

        # Install a spec for which the `version_based` variant condition does not hold
        old = spack.concretize.concretize_one("conditional-variant-pkg @1")
        PackageInstaller([old.package], fake=True, explicit=True).install()

        # Then explicitly require a spec with `+version_based`, which shouldn't reuse previous spec
        new1 = spack.concretize.concretize_one("conditional-variant-pkg +version_based")
        assert new1.satisfies("@2 +version_based")

        new2 = spack.concretize.concretize_one("conditional-variant-pkg +two_whens")
        assert new2.satisfies("@2 +two_whens +version_based")

    def test_reuse_with_flags(self, mutable_database, mutable_config):
        spack.config.set("concretizer:reuse", True)
        spec = spack.concretize.concretize_one("pkg-a cflags=-g cxxflags=-g")
        PackageInstaller([spec.package], fake=True, explicit=True).install()
        testspec = spack.concretize.concretize_one("pkg-a cflags=-g")
        assert testspec == spec, testspec.tree()

    @pytest.mark.regression("20784")
    def test_concretization_of_test_dependencies(self):
        # With clingo we emit dependency_conditions regardless of the type
        # of the dependency. We need to ensure that there's at least one
        # dependency type declared to infer that the dependency holds.
        s = spack.concretize.concretize_one("test-dep-with-imposed-conditions")
        assert "c" not in s

    @pytest.mark.parametrize(
        "spec_str", ["wrong-variant-in-conflicts", "wrong-variant-in-depends-on"]
    )
    def test_error_message_for_inconsistent_variants(self, spec_str):
        s = Spec(spec_str)
        with pytest.raises(vt.UnknownVariantError):
            s = spack.concretize.concretize_one(s)

    @pytest.mark.regression("22533")
    @pytest.mark.parametrize(
        "spec_str,variant_name,expected_values",
        [
            # Test the default value 'auto'
            ("mvapich2", "file_systems", ("auto",)),
            # Test setting a single value from the disjoint set
            ("mvapich2 file_systems=lustre", "file_systems", ("lustre",)),
            # Test setting multiple values from the disjoint set
            ("mvapich2 file_systems=lustre,gpfs", "file_systems", ("lustre", "gpfs")),
        ],
    )
    def test_mv_variants_disjoint_sets_from_spec(self, spec_str, variant_name, expected_values):
        s = spack.concretize.concretize_one(spec_str)
        assert set(expected_values) == set(s.variants[variant_name].value)

    @pytest.mark.regression("22533")
    def test_mv_variants_disjoint_sets_from_packages_yaml(self):
        external_mvapich2 = {
            "mvapich2": {
                "buildable": False,
                "externals": [{"spec": "mvapich2@2.3.1 file_systems=nfs,ufs", "prefix": "/usr"}],
            }
        }
        spack.config.set("packages", external_mvapich2)

        s = spack.concretize.concretize_one("mvapich2")
        assert set(s.variants["file_systems"].value) == set(["ufs", "nfs"])

    @pytest.mark.regression("22596")
    def test_external_with_non_default_variant_as_dependency(self):
        # This package depends on another that is registered as an external
        # with 'buildable: true' and a variant with a non-default value set
        s = spack.concretize.concretize_one("trigger-external-non-default-variant")

        assert "~foo" in s["external-non-default-variant"]
        assert "~bar" in s["external-non-default-variant"]
        assert s["external-non-default-variant"].external

    @pytest.mark.regression("22718")
    @pytest.mark.parametrize(
        "spec_str,expected_compiler",
        [("mpileaks", "%gcc@10.2.1"), ("mpileaks ^mpich%clang@15.0.0", "%clang@15.0.0")],
    )
    def test_compiler_is_unique(self, spec_str, expected_compiler):
        s = spack.concretize.concretize_one(spec_str)

        for node in s.traverse():
            if not node.satisfies("^ c"):
                continue
            assert node.satisfies(expected_compiler)

    @pytest.mark.parametrize(
        "spec_str,expected_dict",
        [
            # Check the defaults from the package (libs=shared)
            ("multivalue-variant", {"libs=shared": True, "libs=static": False}),
            # Check that libs=static doesn't extend the default
            ("multivalue-variant libs=static", {"libs=shared": False, "libs=static": True}),
        ],
    )
    def test_multivalued_variants_from_cli(self, spec_str, expected_dict):
        s = spack.concretize.concretize_one(spec_str)

        for constraint, value in expected_dict.items():
            assert s.satisfies(constraint) == value

    @pytest.mark.regression("22351")
    @pytest.mark.parametrize(
        "spec_str,expected",
        [
            # Version 1.1.0 is deprecated and should not be selected, unless we
            # explicitly asked for that
            ("deprecated-versions", "deprecated-versions@1.0.0"),
            ("deprecated-versions@=1.1.0", "deprecated-versions@1.1.0"),
        ],
    )
    def test_deprecated_versions_not_selected(self, spec_str, expected):
        with spack.config.override("config:deprecated", True):
            s = spack.concretize.concretize_one(spec_str)
            s.satisfies(expected)

    @pytest.mark.regression("24196")
    def test_version_badness_more_important_than_default_mv_variants(self):
        # If a dependency had an old version that for some reason pulls in
        # a transitive dependency with a multi-valued variant, that old
        # version was preferred because of the order of our optimization
        # criteria.
        s = spack.concretize.concretize_one("root")
        assert s["gmt"].satisfies("@2.0")

    @pytest.mark.regression("24205")
    def test_provider_must_meet_requirements(self):
        # A package can be a provider of a virtual only if the underlying
        # requirements are met.
        s = Spec("unsat-virtual-dependency")
        with pytest.raises((RuntimeError, spack.error.UnsatisfiableSpecError)):
            s = spack.concretize.concretize_one(s)

    @pytest.mark.regression("23951")
    def test_newer_dependency_adds_a_transitive_virtual(self):
        # Ensure that a package doesn't concretize any of its transitive
        # dependencies to an old version because newer versions pull in
        # a new virtual dependency. The possible concretizations here are:
        #
        # root@1.0 <- middle@1.0 <- leaf@2.0 <- blas
        # root@1.0 <- middle@1.0 <- leaf@1.0
        #
        # and "blas" is pulled in only by newer versions of "leaf"
        s = spack.concretize.concretize_one("root-adds-virtual")
        assert s["leaf-adds-virtual"].satisfies("@2.0")
        assert "blas" in s

    @pytest.mark.regression("26718")
    def test_versions_in_virtual_dependencies(self):
        # Ensure that a package that needs a given version of a virtual
        # package doesn't end up using a later implementation
        s = spack.concretize.concretize_one("hpcviewer@2019.02")
        assert s["java"].satisfies("virtual-with-versions@1.8.0")

    @pytest.mark.regression("26866")
    def test_non_default_provider_of_multiple_virtuals(self):
        s = spack.concretize.concretize_one("many-virtual-consumer ^low-priority-provider")
        assert s["mpi"].name == "low-priority-provider"
        assert s["lapack"].name == "low-priority-provider"

        for virtual_pkg in ("mpi", "lapack"):
            for pkg in spack.repo.PATH.providers_for(virtual_pkg):
                if pkg.name == "low-priority-provider":
                    continue
                assert pkg not in s

    @pytest.mark.regression("27237")
    @pytest.mark.parametrize(
        "spec_str,expect_installed",
        [("mpich", True), ("mpich+debug", False), ("mpich~debug", True)],
    )
    def test_concrete_specs_are_not_modified_on_reuse(
        self, mutable_database, spec_str, expect_installed
    ):
        # Test the internal consistency of solve + DAG reconstruction
        # when reused specs are added to the mix. This prevents things
        # like additional constraints being added to concrete specs in
        # the answer set produced by clingo.
        with spack.config.override("concretizer:reuse", True):
            s = spack.concretize.concretize_one(spec_str)
        assert s.installed is expect_installed
        assert s.satisfies(spec_str)

    @pytest.mark.regression("26721,19736")
    def test_sticky_variant_in_package(self):
        # Here we test that a sticky variant cannot be changed from its default value
        # by the ASP solver if not set explicitly. The package used in the test needs
        # to have +allow-gcc set to be concretized with %gcc and clingo is not allowed
        # to change the default ~allow-gcc
        with pytest.raises(spack.error.SpackError):
            spack.concretize.concretize_one("sticky-variant %gcc")

        s = spack.concretize.concretize_one("sticky-variant+allow-gcc %gcc")
        assert s.satisfies("%gcc") and s.satisfies("+allow-gcc")

        s = spack.concretize.concretize_one("sticky-variant %clang")
        assert s.satisfies("%clang") and s.satisfies("~allow-gcc")

    @pytest.mark.regression("42172")
    @pytest.mark.parametrize(
        "spec,allow_gcc",
        [
            ("sticky-variant@1.0+allow-gcc", True),
            ("sticky-variant@1.0~allow-gcc", False),
            ("sticky-variant@1.0", False),
        ],
    )
    def test_sticky_variant_in_external(self, spec, allow_gcc):
        # setup external for sticky-variant+allow-gcc
        config = {"externals": [{"spec": spec, "prefix": "/fake/path"}], "buildable": False}
        spack.config.set("packages:sticky-variant", config)

        maybe = llnl.util.lang.nullcontext if allow_gcc else pytest.raises
        with maybe(spack.error.SpackError):
            s = spack.concretize.concretize_one("sticky-variant-dependent%gcc")

        if allow_gcc:
            assert s.satisfies("%gcc")
            assert s["sticky-variant"].satisfies("+allow-gcc")
            assert s["sticky-variant"].external

    def test_do_not_invent_new_concrete_versions_unless_necessary(self):
        # ensure we select a known satisfying version rather than creating
        # a new '2.7' version.
        assert ver("=2.7.11") == spack.concretize.concretize_one("python@2.7").version

        # Here there is no known satisfying version - use the one on the spec.
        assert ver("=2.7.21") == spack.concretize.concretize_one("python@=2.7.21").version

    @pytest.mark.parametrize(
        "spec_str,valid",
        [
            ("conditional-values-in-variant@1.62.0 cxxstd=17", False),
            ("conditional-values-in-variant@1.62.0 cxxstd=2a", False),
            ("conditional-values-in-variant@1.72.0 cxxstd=2a", False),
            # Ensure disjoint set of values work too
            ("conditional-values-in-variant@1.72.0 staging=flexpath", False),
            # Ensure conditional values set False fail too
            ("conditional-values-in-variant foo=bar", False),
            ("conditional-values-in-variant foo=foo", True),
        ],
    )
    def test_conditional_values_in_variants(self, spec_str, valid):
        s = Spec(spec_str)
        raises = pytest.raises((RuntimeError, spack.error.UnsatisfiableSpecError))
        with llnl.util.lang.nullcontext() if valid else raises:
            s = spack.concretize.concretize_one(s)

    def test_conditional_values_in_conditional_variant(self):
        """Test that conditional variants play well with conditional possible values"""
        s = spack.concretize.concretize_one("conditional-values-in-variant@1.50.0")
        assert "cxxstd" not in s.variants

        s = spack.concretize.concretize_one("conditional-values-in-variant@1.60.0")
        assert "cxxstd" in s.variants

    def test_target_granularity(self):
        # The test architecture uses core2 as the default target. Check that when
        # we configure Spack for "generic" granularity we concretize for x86_64
        default_target = spack.platforms.test.Test.default
        generic_target = _vendoring.archspec.cpu.TARGETS[default_target].generic.name
        s = Spec("python")
        assert spack.concretize.concretize_one(s).satisfies("target=%s" % default_target)
        with spack.config.override("concretizer:targets", {"granularity": "generic"}):
            assert spack.concretize.concretize_one(s).satisfies("target=%s" % generic_target)

    def test_host_compatible_concretization(self):
        # Check that after setting "host_compatible" to false we cannot concretize.
        # Here we use "k10" to set a target non-compatible with the current host
        # to avoid a lot of boilerplate when mocking the test platform. The issue
        # is that the defaults for the test platform are very old, so there's no
        # compiler supporting e.g. icelake etc.
        s = Spec("python target=k10")
        assert spack.concretize.concretize_one(s)
        with spack.config.override("concretizer:targets", {"host_compatible": True}):
            with pytest.raises(spack.error.SpackError):
                spack.concretize.concretize_one(s)

    def test_add_microarchitectures_on_explicit_request(self):
        # Check that if we consider only "generic" targets, we can still solve for
        # specific microarchitectures on explicit requests
        with spack.config.override("concretizer:targets", {"granularity": "generic"}):
            s = spack.concretize.concretize_one("python target=k10")
        assert s.satisfies("target=k10")

    @pytest.mark.regression("29201")
    def test_delete_version_and_reuse(self, mutable_database, repo_with_changing_recipe):
        """Test that we can reuse installed specs with versions not
        declared in package.py
        """
        root = spack.concretize.concretize_one("root")
        PackageInstaller([root.package], fake=True, explicit=True).install()
        repo_with_changing_recipe.change({"delete_version": True})

        with spack.config.override("concretizer:reuse", True):
            new_root = spack.concretize.concretize_one("root")

        assert root.dag_hash() == new_root.dag_hash()

    @pytest.mark.regression("29201")
    def test_installed_version_is_selected_only_for_reuse(
        self, mutable_database, repo_with_changing_recipe
    ):
        """Test that a version coming from an installed spec is a possible
        version only for reuse
        """
        # Install a dependency that cannot be reused with "root"
        # because of a conflict in a variant, then delete its version
        dependency = spack.concretize.concretize_one("changing@1.0~foo")
        PackageInstaller([dependency.package], fake=True, explicit=True).install()
        repo_with_changing_recipe.change({"delete_version": True})

        with spack.config.override("concretizer:reuse", True):
            new_root = spack.concretize.concretize_one("root")

        assert not new_root["changing"].satisfies("@1.0")

    @pytest.mark.regression("28259")
    def test_reuse_with_unknown_namespace_dont_raise(
        self, temporary_store, mock_custom_repository
    ):
        with spack.repo.use_repositories(mock_custom_repository, override=False):
            s = spack.concretize.concretize_one("pkg-c")
            assert s.namespace != "builtin_mock"
            PackageInstaller([s.package], fake=True, explicit=True).install()

        with spack.config.override("concretizer:reuse", True):
            s = spack.concretize.concretize_one("pkg-c")
        assert s.namespace == "builtin_mock"

    @pytest.mark.regression("45538")
    def test_reuse_from_other_namespace_no_raise(self, tmpdir, temporary_store, monkeypatch):
        myrepo = spack.repo.MockRepositoryBuilder(tmpdir, namespace="mock_repo")
        myrepo.add_package("zlib")

        builtin = spack.concretize.concretize_one("zlib")
        PackageInstaller([builtin.package], fake=True, explicit=True).install()

        with spack.repo.use_repositories(myrepo.root, override=False):
            with spack.config.override("concretizer:reuse", True):
                myrepo = spack.concretize.concretize_one("mock_repo.zlib")

        assert myrepo.namespace == "mock_repo"

    @pytest.mark.regression("28259")
    def test_reuse_with_unknown_package_dont_raise(self, tmpdir, temporary_store, monkeypatch):
        builder = spack.repo.MockRepositoryBuilder(str(tmpdir), namespace="myrepo")
        builder.add_package("pkg-c")
        with spack.repo.use_repositories(builder.root, override=False):
            s = spack.concretize.concretize_one("pkg-c")
            assert s.namespace == "myrepo"
            PackageInstaller([s.package], fake=True, explicit=True).install()
        del sys.modules["spack_repo.myrepo.packages.pkg_c"]
        builder.remove("pkg-c")
        with spack.repo.use_repositories(builder.root, override=False) as repos:
            # TODO (INJECT CONFIGURATION): unclear why the cache needs to be invalidated explicitly
            repos.repos[0]._pkg_checker.invalidate()
            with spack.config.override("concretizer:reuse", True):
                s = spack.concretize.concretize_one("pkg-c")
            assert s.namespace == "builtin_mock"

    @pytest.mark.parametrize(
        "specs,checks",
        [
            (["libelf", "libelf@0.8.10"], {"libelf": 1}),
            (["libdwarf%gcc", "libelf%clang"], {"libdwarf": 1, "libelf": 1}),
            (["libdwarf%gcc", "libdwarf%clang"], {"libdwarf": 2, "libelf": 1}),
            (["libdwarf^libelf@0.8.12", "libdwarf^libelf@0.8.13"], {"libdwarf": 2, "libelf": 2}),
            (["hdf5", "zmpi"], {"zmpi": 1, "fake": 1}),
            (["hdf5", "mpich"], {"mpich": 1}),
            (["hdf5^zmpi", "mpich"], {"mpi": 2, "mpich": 1, "zmpi": 1, "fake": 1}),
            (["mpi", "zmpi"], {"mpi": 1, "mpich": 0, "zmpi": 1, "fake": 1}),
            (["mpi", "mpich"], {"mpi": 1, "mpich": 1, "zmpi": 0}),
        ],
    )
    def test_best_effort_coconcretize(self, specs, checks):
        specs = [Spec(s) for s in specs]
        solver = spack.solver.asp.Solver()
        solver.reuse = False
        concrete_specs = set()
        for result in solver.solve_in_rounds(specs):
            for s in result.specs:
                concrete_specs.update(s.traverse())

        for matching_spec, expected_count in checks.items():
            matches = [x for x in concrete_specs if x.satisfies(matching_spec)]
            assert len(matches) == expected_count

    @pytest.mark.parametrize(
        "specs,expected_spec,occurances",
        [
            # The algorithm is greedy, and it might decide to solve the "best"
            # spec early in which case reuse is suboptimal. In this case the most
            # recent version of libdwarf is selected and concretized to libelf@0.8.13
            (
                [
                    "libdwarf@20111030^libelf@0.8.10",
                    "libdwarf@20130207^libelf@0.8.12",
                    "libdwarf@20130729",
                ],
                "libelf@0.8.12",
                1,
            ),
            # Check we reuse the best libelf in the environment
            (
                [
                    "libdwarf@20130729^libelf@0.8.10",
                    "libdwarf@20130207^libelf@0.8.12",
                    "libdwarf@20111030",
                ],
                "libelf@0.8.12",
                2,
            ),
            (["libdwarf@20130729", "libdwarf@20130207", "libdwarf@20111030"], "libelf@0.8.13", 3),
            # We need to solve in 2 rounds and we expect mpich to be preferred to zmpi
            (["hdf5+mpi", "zmpi", "mpich"], "mpich", 2),
        ],
    )
    def test_best_effort_coconcretize_preferences(self, specs, expected_spec, occurances):
        """Test package preferences during coconcretization."""
        specs = [Spec(s) for s in specs]
        solver = spack.solver.asp.Solver()
        solver.reuse = False
        concrete_specs = {}
        for result in solver.solve_in_rounds(specs):
            concrete_specs.update(result.specs_by_input)

        counter = 0
        for spec in concrete_specs.values():
            if expected_spec in spec:
                counter += 1
        assert counter == occurances, concrete_specs

    def test_solve_in_rounds_all_unsolved(self, monkeypatch, mock_packages):
        specs = [Spec(x) for x in ["libdwarf%gcc", "libdwarf%clang"]]
        solver = spack.solver.asp.Solver()
        solver.reuse = False

        simulate_unsolved_property = list((x, None) for x in specs)
        monkeypatch.setattr(spack.solver.asp.Result, "unsolved_specs", simulate_unsolved_property)
        monkeypatch.setattr(spack.solver.asp.Result, "specs", list())

        with pytest.raises(spack.solver.asp.OutputDoesNotSatisfyInputError):
            list(solver.solve_in_rounds(specs))

    def test_coconcretize_reuse_and_virtuals(self):
        reusable_specs = []
        for s in ["mpileaks ^mpich", "zmpi"]:
            reusable_specs.extend(spack.concretize.concretize_one(s).traverse(root=True))

        root_specs = [Spec("mpileaks"), Spec("zmpi")]

        with spack.config.override("concretizer:reuse", True):
            solver = spack.solver.asp.Solver()
            setup = spack.solver.asp.SpackSolverSetup()
            result, _, _ = solver.driver.solve(setup, root_specs, reuse=reusable_specs)

        for spec in result.specs:
            assert "zmpi" in spec

    @pytest.mark.regression("30864")
    def test_misleading_error_message_on_version(self, mutable_database):
        # For this bug to be triggered we need a reusable dependency
        # that is not optimal in terms of optimization scores.
        # We pick an old version of "b"
        reusable_specs = [spack.concretize.concretize_one("non-existing-conditional-dep@1.0")]
        root_spec = Spec("non-existing-conditional-dep@2.0")

        with spack.config.override("concretizer:reuse", True):
            solver = spack.solver.asp.Solver()
            setup = spack.solver.asp.SpackSolverSetup()
            with pytest.raises(spack.solver.asp.UnsatisfiableSpecError, match="Cannot satisfy"):
                solver.driver.solve(setup, [root_spec], reuse=reusable_specs)

    @pytest.mark.regression("31148")
    def test_version_weight_and_provenance(self):
        """Test package preferences during concretization."""
        reusable_specs = [
            spack.concretize.concretize_one(spec_str) for spec_str in ("pkg-b@0.9", "pkg-b@1.0")
        ]
        root_spec = Spec("pkg-a foobar=bar")

        with spack.config.override("concretizer:reuse", True):
            solver = spack.solver.asp.Solver()
            setup = spack.solver.asp.SpackSolverSetup()
            result, _, _ = solver.driver.solve(setup, [root_spec], reuse=reusable_specs)
            # Version badness should be > 0 only for reused specs. For instance, for pkg-b
            # the version provenance is:
            #
            # version_declared("pkg-b","1.0",0,"package_py").
            # version_declared("pkg-b","0.9",1,"package_py").
            # version_declared("pkg-b","1.0",2,"installed").
            # version_declared("pkg-b","0.9",3,"installed").
            v_weights = [x for x in result.criteria if x[2] == "version badness (non roots)"][0]
            reused_weights, built_weights, _ = v_weights
            assert reused_weights > 2 and built_weights == 0

            result_spec = result.specs[0]
            assert result_spec.satisfies("^pkg-b@1.0")
            assert result_spec["pkg-b"].dag_hash() == reusable_specs[1].dag_hash()

    def test_reuse_succeeds_with_config_compatible_os(self):
        root_spec = Spec("pkg-b")
        s = spack.concretize.concretize_one(root_spec)
        other_os = s.copy()
        mock_os = "ubuntu2204"
        other_os.architecture = spack.spec.ArchSpec(
            "test-{os}-{target}".format(os=mock_os, target=str(s.architecture.target))
        )
        reusable_specs = [other_os]
        overrides = {"concretizer": {"reuse": True, "os_compatible": {s.os: [mock_os]}}}
        custom_scope = spack.config.InternalConfigScope("concretize_override", overrides)
        with spack.config.override(custom_scope):
            solver = spack.solver.asp.Solver()
            setup = spack.solver.asp.SpackSolverSetup()
            result, _, _ = solver.driver.solve(setup, [root_spec], reuse=reusable_specs)
        concrete_spec = result.specs[0]
        assert concrete_spec.satisfies("os={}".format(other_os.architecture.os))

    def test_git_hash_assigned_version_is_preferred(self):
        hash = "a" * 40
        s = Spec("develop-branch-version@%s=develop" % hash)
        c = spack.concretize.concretize_one(s)
        assert hash in str(c)

    @pytest.mark.parametrize("git_ref", ("a" * 40, "0.2.15", "main"))
    def test_git_ref_version_is_equivalent_to_specified_version(self, git_ref):
        s = Spec("develop-branch-version@git.%s=develop" % git_ref)
        c = spack.concretize.concretize_one(s)
        assert git_ref in str(c)
        assert s.satisfies("@develop")
        assert s.satisfies("@0.1:")

    @pytest.mark.parametrize("git_ref", ("a" * 40, "0.2.15", "fbranch"))
    def test_git_ref_version_succeeds_with_unknown_version(self, git_ref):
        # main is not defined in the package.py for this file
        s = Spec("develop-branch-version@git.%s=main" % git_ref)
        s = spack.concretize.concretize_one(s)
        assert s.satisfies("develop-branch-version@main")

    @pytest.mark.regression("31484")
    def test_installed_externals_are_reused(
        self, mutable_database, repo_with_changing_recipe, tmp_path
    ):
        """Tests that external specs that are in the DB can be reused, if they result in a
        better optimization score.
        """
        external_conf = {
            "changing": {
                "buildable": False,
                "externals": [{"spec": "changing@1.0", "prefix": str(tmp_path)}],
            }
        }
        spack.config.set("packages", external_conf)

        # Install the external spec
        middle_pkg = spack.concretize.concretize_one("middle")
        PackageInstaller([middle_pkg.package], fake=True, explicit=True).install()
        assert middle_pkg["changing"].external
        changing_external = middle_pkg["changing"]

        # Modify the package.py file
        repo_with_changing_recipe.change({"delete_variant": True})

        # Try to concretize the external without reuse and confirm the hash changed
        with spack.config.override("concretizer:reuse", False):
            root_no_reuse = spack.concretize.concretize_one("root")
        assert root_no_reuse["changing"].dag_hash() != changing_external.dag_hash()

        # ... while with reuse we have the same hash
        with spack.config.override("concretizer:reuse", True):
            root_with_reuse = spack.concretize.concretize_one("root")
        assert root_with_reuse["changing"].dag_hash() == changing_external.dag_hash()

    @pytest.mark.regression("31484")
    def test_user_can_select_externals_with_require(self, mutable_database, tmp_path):
        """Test that users have means to select an external even in presence of reusable specs."""
        external_conf = {
            "mpi": {"buildable": False},
            "multi-provider-mpi": {
                "externals": [{"spec": "multi-provider-mpi@2.0.0", "prefix": str(tmp_path)}]
            },
        }
        spack.config.set("packages", external_conf)

        # mpich and others are installed, so check that
        # fresh use the external, reuse does not
        with spack.config.override("concretizer:reuse", False):
            mpi_spec = spack.concretize.concretize_one("mpi")
            assert mpi_spec.name == "multi-provider-mpi"

        with spack.config.override("concretizer:reuse", True):
            mpi_spec = spack.concretize.concretize_one("mpi")
            assert mpi_spec.name != "multi-provider-mpi"

        external_conf["mpi"]["require"] = "multi-provider-mpi"
        spack.config.set("packages", external_conf)

        with spack.config.override("concretizer:reuse", True):
            mpi_spec = spack.concretize.concretize_one("mpi")
            assert mpi_spec.name == "multi-provider-mpi"

    @pytest.mark.regression("31484")
    def test_installed_specs_disregard_conflicts(self, mutable_database, monkeypatch):
        """Test that installed specs do not trigger conflicts. This covers for the rare case
        where a conflict is added on a package after a spec matching the conflict was installed.
        """
        # Add a conflict to "mpich" that match an already installed "mpich~debug"
        pkg_cls = spack.repo.PATH.get_pkg_class("mpich")
        monkeypatch.setitem(pkg_cls.conflicts, Spec(), [("~debug", None)])

        # If we concretize with --fresh the conflict is taken into account
        with spack.config.override("concretizer:reuse", False):
            s = spack.concretize.concretize_one("mpich")
            assert s.satisfies("+debug")

        # If we concretize with --reuse it is not, since "mpich~debug" was already installed
        with spack.config.override("concretizer:reuse", True):
            s = spack.concretize.concretize_one("mpich")
            assert s.installed
            assert s.satisfies("~debug"), s

    @pytest.mark.regression("32471")
    def test_require_targets_are_allowed(self, mutable_config, mutable_database):
        """Test that users can set target constraints under the require attribute."""
        # Configuration to be added to packages.yaml
        required_target = _vendoring.archspec.cpu.TARGETS[spack.platforms.test.Test.default].family
        external_conf = {"all": {"require": f"target={required_target}"}}
        mutable_config.set("packages", external_conf)

        with spack.config.override("concretizer:reuse", False):
            spec = spack.concretize.concretize_one("mpich")

        for s in spec.traverse(deptype=("link", "run")):
            assert s.satisfies(f"target={required_target}")

    def test_external_python_extensions_have_dependency(self):
        """Test that python extensions have access to a python dependency

        when python is otherwise in the DAG"""
        external_conf = {
            "py-extension1": {
                "buildable": False,
                "externals": [{"spec": "py-extension1@2.0", "prefix": "/fake"}],
            }
        }
        spack.config.set("packages", external_conf)

        spec = spack.concretize.concretize_one("py-extension2")

        assert "python" in spec["py-extension1"]
        assert spec["python"] == spec["py-extension1"]["python"]

    target = spack.platforms.test.Test.default

    @pytest.mark.parametrize(
        "python_spec",
        [
            "python@configured",
            "python@configured platform=test",
            "python@configured os=debian",
            "python@configured target=%s" % target,
        ],
    )
    def test_external_python_extension_find_dependency_from_config(self, python_spec):
        fake_path = os.path.sep + "fake"

        external_conf = {
            "py-extension1": {
                "buildable": False,
                "externals": [{"spec": "py-extension1@2.0", "prefix": fake_path}],
            },
            "python": {"externals": [{"spec": python_spec, "prefix": fake_path}]},
        }
        spack.config.set("packages", external_conf)

        spec = spack.concretize.concretize_one("py-extension1")

        assert "python" in spec["py-extension1"]
        assert spec["python"].prefix == fake_path
        # The spec is not equal to Spec("python@configured") because it gets a
        # namespace and an external prefix before marking concrete
        assert spec["python"].satisfies(python_spec)

    def test_external_python_extension_find_dependency_from_installed(self, monkeypatch):
        fake_path = os.path.sep + "fake"

        external_conf = {
            "py-extension1": {
                "buildable": False,
                "externals": [{"spec": "py-extension1@2.0", "prefix": fake_path}],
            },
            "python": {
                "buildable": False,
                "externals": [{"spec": "python@installed", "prefix": fake_path}],
            },
        }
        spack.config.set("packages", external_conf)

        # install python external
        python = spack.concretize.concretize_one("python")

        def query(*args, **kwargs):
            return [python]

        monkeypatch.setattr(spack.store.STORE.db, "query", query)

        # ensure that we can't be faking this by getting it from config
        external_conf.pop("python")
        spack.config.set("packages", external_conf)

        spec = spack.concretize.concretize_one("py-extension1")

        assert "python" in spec["py-extension1"]
        assert spec["python"].prefix == fake_path
        # The spec is not equal to Spec("python@configured") because it gets a
        # namespace and an external prefix before marking concrete
        assert spec["python"].satisfies(python)

    def test_external_python_extension_find_dependency_from_detection(self, monkeypatch):
        """Test that python extensions have access to a python dependency

        when python isn't otherwise in the DAG"""
        prefix = os.path.sep + "fake"
        python_spec = Spec.from_detection("python@=detected", external_path=prefix)

        def find_fake_python(classes, path_hints, **kwargs):
            return {
                "python": [Spec.from_detection("python@=detected", external_path=path_hints[0])]
            }

        monkeypatch.setattr(spack.detection, "by_path", find_fake_python)
        external_conf = {
            "py-extension1": {
                "buildable": False,
                "externals": [{"spec": "py-extension1@2.0", "prefix": "%s" % prefix}],
            }
        }
        spack.config.set("packages", external_conf)

        spec = spack.concretize.concretize_one("py-extension1")

        assert "python" in spec["py-extension1"]
        assert spec["python"].prefix == prefix
        assert spec["python"].external
        assert spec["python"].satisfies(python_spec)

    def test_external_python_extension_find_unified_python(self):
        """Test that python extensions use the same python as other specs in unified env"""
        external_conf = {
            "py-extension1": {
                "buildable": False,
                "externals": [{"spec": "py-extension1@2.0", "prefix": os.path.sep + "fake"}],
            }
        }
        spack.config.set("packages", external_conf)

        abstract_specs = [Spec(s) for s in ["py-extension1", "python"]]
        specs = spack.concretize._concretize_specs_together(abstract_specs)
        assert specs[0]["python"] == specs[1]["python"]

    @pytest.mark.regression("36190")
    @pytest.mark.parametrize(
        "specs",
        [
            ["mpileaks^ callpath ^dyninst@8.1.1:8 ^mpich2@1.3:1"],
            ["multivalue-variant ^pkg-a@2:2"],
            ["v1-consumer ^conditional-provider@1:1 +disable-v1"],
        ],
    )
    def test_result_specs_is_not_empty(self, specs):
        """Check that the implementation of "result.specs" is correct in cases where we
        know a concretization exists.
        """
        specs = [Spec(s) for s in specs]
        solver = spack.solver.asp.Solver()
        setup = spack.solver.asp.SpackSolverSetup()
        result, _, _ = solver.driver.solve(setup, specs, reuse=[])

        assert result.specs

    @pytest.mark.regression("38664")
    def test_unsolved_specs_raises_error(self, monkeypatch, mock_packages):
        """Check that the solver raises an exception when input specs are not
        satisfied.
        """
        specs = [Spec("zlib")]
        solver = spack.solver.asp.Solver()
        setup = spack.solver.asp.SpackSolverSetup()

        simulate_unsolved_property = list((x, None) for x in specs)

        monkeypatch.setattr(spack.solver.asp.Result, "unsolved_specs", simulate_unsolved_property)

        with pytest.raises(
            spack.solver.asp.InternalConcretizerError,
            match="the solver completed but produced specs",
        ):
            solver.driver.solve(setup, specs, reuse=[])

    @pytest.mark.regression("43141")
    @pytest.mark.parametrize(
        "spec_str,expected_match",
        [
            # A package does not exist
            ("pkg-a ^foo", "since 'foo' does not exist"),
            # Request a compiler for a package that doesn't need it
            ("pkg-c %gcc", "cannot depend on gcc"),
        ],
    )
    def test_errors_on_statically_checked_preconditions(self, spec_str, expected_match):
        """Tests that the solver can report a case where the compiler cannot be set"""
        with pytest.raises(spack.error.UnsatisfiableSpecError, match=expected_match):
            spack.concretize.concretize_one(spec_str)

    @pytest.mark.regression("36339")
    @pytest.mark.parametrize(
        "compiler_str,expected",
        [
            ("gcc@:9", "@=9.4.0"),
            ("gcc@:10", "@=10.2.1"),
            ("gcc@10", "@=10.2.1"),
            ("gcc@10:", "@=10.2.1"),
        ],
    )
    def test_compiler_match_constraints_when_selected(self, compiler_str, expected):
        """Test that, when multiple compilers with the same name are in the configuration
        we ensure that the selected one matches all the required constraints.
        """
        s = spack.concretize.concretize_one(f"pkg-a %{compiler_str}")
        assert s["gcc"].satisfies(expected)

    @pytest.mark.parametrize("spec_str", ["mpileaks", "mpileaks ^mpich"])
    def test_virtuals_are_annotated_on_edges(self, spec_str):
        """Tests that information on virtuals is annotated on DAG edges"""
        spec = spack.concretize.concretize_one(spec_str)
        mpi_provider = spec["mpi"].name

        edges = spec.edges_to_dependencies(name=mpi_provider)
        assert len(edges) == 1 and edges[0].virtuals == ("mpi",)
        edges = spec.edges_to_dependencies(name="callpath")
        assert len(edges) == 1 and edges[0].virtuals == ()

    @pytest.mark.parametrize("transitive", [True, False])
    def test_explicit_splices(
        self, mutable_config, database_mutable_config, mock_packages, transitive, capfd
    ):
        mpich_spec = database_mutable_config.query("mpich")[0]
        splice_info = {
            "target": "mpi",
            "replacement": f"/{mpich_spec.dag_hash()}",
            "transitive": transitive,
        }
        spack.config.CONFIG.set("concretizer", {"splice": {"explicit": [splice_info]}})

        spec = spack.concretize.concretize_one("hdf5 ^zmpi")

        assert spec.satisfies(f"^mpich@{mpich_spec.version}")
        assert spec.build_spec.dependencies(name="zmpi", deptype="link")
        assert spec["mpi"].build_spec.satisfies(mpich_spec)
        assert not spec.build_spec.satisfies(f"^mpich/{mpich_spec.dag_hash()}")
        assert not spec.dependencies(name="zmpi", deptype="link")

        captured = capfd.readouterr()
        assert "Warning: explicit splice configuration has caused" in captured.err
        assert "hdf5 ^zmpi" in captured.err
        assert str(spec) in captured.err

    def test_explicit_splice_fails_nonexistent(mutable_config, mock_packages, mock_store):
        splice_info = {"target": "mpi", "replacement": "mpich/doesnotexist"}
        spack.config.CONFIG.set("concretizer", {"splice": {"explicit": [splice_info]}})

        with pytest.raises(spack.spec.InvalidHashError):
            _ = spack.concretize.concretize_one("hdf5^zmpi")

    def test_explicit_splice_fails_no_hash(mutable_config, mock_packages, mock_store):
        splice_info = {"target": "mpi", "replacement": "mpich"}
        spack.config.CONFIG.set("concretizer", {"splice": {"explicit": [splice_info]}})

        with pytest.raises(spack.solver.asp.InvalidSpliceError, match="must be specified by hash"):
            _ = spack.concretize.concretize_one("hdf5^zmpi")

    def test_explicit_splice_non_match_nonexistent_succeeds(
        mutable_config, mock_packages, mock_store
    ):
        """When we have a nonexistent splice configured but are not using it, don't fail."""
        splice_info = {"target": "will_not_match", "replacement": "nonexistent/doesnotexist"}
        spack.config.CONFIG.set("concretizer", {"splice": {"explicit": [splice_info]}})
        spec = spack.concretize.concretize_one("zlib")
        # the main test is that it does not raise
        assert not spec.spliced

    @pytest.mark.db
    @pytest.mark.parametrize(
        "spec_str,mpi_name",
        [("mpileaks", "mpich"), ("mpileaks ^mpich2", "mpich2"), ("mpileaks ^zmpi", "zmpi")],
    )
    def test_virtuals_are_reconstructed_on_reuse(self, spec_str, mpi_name, mutable_database):
        """Tests that when we reuse a spec, virtual on edges are reconstructed correctly"""
        with spack.config.override("concretizer:reuse", True):
            spec = spack.concretize.concretize_one(spec_str)
            assert spec.installed
            mpi_edges = spec.edges_to_dependencies(mpi_name)
            assert len(mpi_edges) == 1
            assert "mpi" in mpi_edges[0].virtuals

    def test_dont_define_new_version_from_input_if_checksum_required(self, working_env):
        os.environ["SPACK_CONCRETIZER_REQUIRE_CHECKSUM"] = "yes"
        with pytest.raises(spack.error.UnsatisfiableSpecError):
            # normally spack concretizes to @=3.0 if it's not defined in package.py, except
            # when checksums are required
            spack.concretize.concretize_one("pkg-a@=3.0")

    @pytest.mark.regression("39570")
    @pytest.mark.db
    def test_reuse_python_from_cli_and_extension_from_db(self, mutable_database):
        """Tests that reusing python with and explicit request on the command line, when the spec
        also reuses a python extension from the DB, doesn't fail.
        """
        s = spack.concretize.concretize_one("py-extension1")
        python_hash = s["python"].dag_hash()
        PackageInstaller([s.package], fake=True, explicit=True).install()

        with spack.config.override("concretizer:reuse", True):
            with_reuse = spack.concretize.concretize_one(f"py-extension2 ^/{python_hash}")

        with spack.config.override("concretizer:reuse", False):
            without_reuse = spack.concretize.concretize_one("py-extension2")

        assert with_reuse.dag_hash() == without_reuse.dag_hash()

    @pytest.mark.regression("35536")
    @pytest.mark.parametrize(
        "spec_str,expected_namespaces",
        [
            # Single node with fully qualified namespace
            ("builtin_mock.gmake", {"gmake": "builtin_mock"}),
            # Dependency with fully qualified namespace
            ("hdf5 ^builtin_mock.gmake", {"gmake": "builtin_mock", "hdf5": "duplicates_test"}),
            ("hdf5 ^gmake", {"gmake": "duplicates_test", "hdf5": "duplicates_test"}),
        ],
    )
    def test_select_lower_priority_package_from_repository_stack(
        self, spec_str, expected_namespaces
    ):
        """Tests that a user can explicitly select a lower priority, fully qualified dependency
        from cli.
        """
        # 'builtin_mock" and "duplicates_test" share a 'gmake' package
        additional_repo = os.path.join(
            spack.paths.test_repos_path, "spack_repo", "duplicates_test"
        )
        with spack.repo.use_repositories(additional_repo, override=False):
            s = spack.concretize.concretize_one(spec_str)

        for name, namespace in expected_namespaces.items():
            assert s[name].concrete
            assert s[name].namespace == namespace

    def test_reuse_specs_from_non_available_compilers(self, mutable_config, mutable_database):
        """Tests that we can reuse specs with compilers that are not configured locally."""
        # All the specs in the mutable DB have been compiled with %gcc@10.2.1
        mpileaks = [s for s in mutable_database.query_local() if s.name == "mpileaks"]

        # Remove gcc@10.2.1
        remover = spack.compilers.config.CompilerRemover(mutable_config)
        remover.mark_compilers(match="gcc@=10.2.1")
        remover.flush()
        mutable_config.set("concretizer:reuse", True)

        # mpileaks is in the database, it will be reused with gcc@=10.2.1
        root = spack.concretize.concretize_one("mpileaks")
        assert root.satisfies("%gcc@10.2.1")
        assert any(root.dag_hash() == x.dag_hash() for x in mpileaks)

        # fftw is not in the database, therefore it will be compiled with gcc@=9.4.0
        root = spack.concretize.concretize_one("fftw~mpi")
        assert root.satisfies("%gcc@9.4.0")

    @pytest.mark.regression("43406")
    def test_externals_with_platform_explicitly_set(self, tmp_path):
        """Tests that users can specify platform=xxx in an external spec"""
        external_conf = {
            "mpich": {
                "buildable": False,
                "externals": [{"spec": "mpich@=2.0.0 platform=test", "prefix": str(tmp_path)}],
            }
        }
        spack.config.set("packages", external_conf)
        s = spack.concretize.concretize_one("mpich")
        assert s.external

    @pytest.mark.regression("43267")
    def test_spec_with_build_dep_from_json(self, tmp_path):
        """Tests that we can correctly concretize a spec, when we express its dependency as a
        concrete spec to be read from JSON.

        The bug was triggered by missing virtuals on edges that were trimmed from pure build
        dependencies.
        """
        build_dep = spack.concretize.concretize_one("dttop")
        json_file = tmp_path / "build.json"
        json_file.write_text(build_dep.to_json())
        s = spack.concretize.concretize_one(f"dtuse ^{str(json_file)}")
        assert s["dttop"].dag_hash() == build_dep.dag_hash()

    @pytest.mark.regression("44040")
    def test_exclude_specs_from_reuse(self, monkeypatch):
        r"""Tests that we can exclude a spec from reuse when concretizing, and that the spec
        is not added back to the solve as a dependency of another reusable spec.

        The expected spec is:

        o callpath@1.0
        |\
        o | mpich@3.0.4
        |\ \
        | |\ \
        | | | o dyninst@8.2
        | |_|/|
        |/| |/|
        | |/|/|
        | | | |\
        | | | | o libdwarf@20130729
        | |_|_|/|
        |/| |_|/|
        | |/| |/|
        | | |/|/
        | | | o libelf@0.8.13
        | |_|/|
        |/| |/|
        | |/|/
        | o | gcc-runtime@10.5.0
        |/| |
        | |/
        o | glibc@2.31
         /
        o gcc@10.5.0
        """
        # Prepare a mock mirror that returns an old version of dyninst
        request_str = "callpath ^mpich"
        reused = spack.concretize.concretize_one(f"{request_str} ^dyninst@8.1.1")
        monkeypatch.setattr(spack.solver.asp, "_specs_from_mirror", lambda: [reused])

        # Exclude dyninst from reuse, so we expect that the old version is not taken into account
        with spack.config.override(
            "concretizer:reuse", {"from": [{"type": "buildcache", "exclude": ["dyninst"]}]}
        ):
            result = spack.concretize.concretize_one(request_str)

        assert result.dag_hash() != reused.dag_hash()
        assert result["mpich"].dag_hash() == reused["mpich"].dag_hash()
        assert result["dyninst"].dag_hash() != reused["dyninst"].dag_hash()
        assert result["dyninst"].satisfies("@=8.2")
        for dep in result["dyninst"].traverse(root=False):
            assert dep.dag_hash() == reused[dep.name].dag_hash()

    @pytest.mark.regression("44091")
    @pytest.mark.parametrize(
        "included_externals",
        [
            ["deprecated-versions"],
            # Try the empty list, to ensure that in that case everything will be included
            # since filtering should happen only when the list is non-empty
            [],
        ],
    )
    def test_include_specs_from_externals_and_libcs(
        self, included_externals, mutable_config, tmp_path
    ):
        """Tests that when we include specs from externals, we always include libcs."""
        mutable_config.set(
            "packages",
            {
                "deprecated-versions": {
                    "externals": [{"spec": "deprecated-versions@1.1.0", "prefix": str(tmp_path)}]
                }
            },
        )
        request_str = "deprecated-client"

        # When using the external the version is selected even if deprecated
        with spack.config.override(
            "concretizer:reuse", {"from": [{"type": "external", "include": included_externals}]}
        ):
            result = spack.concretize.concretize_one(request_str)

        assert result["deprecated-versions"].satisfies("@1.1.0")

        # When excluding it, we pick the non-deprecated version
        with spack.config.override(
            "concretizer:reuse",
            {"from": [{"type": "external", "exclude": ["deprecated-versions"]}]},
        ):
            result = spack.concretize.concretize_one(request_str)

        assert result["deprecated-versions"].satisfies("@1.0.0")

    @pytest.mark.regression("44085")
    def test_can_reuse_concrete_externals_for_dependents(self, mutable_config, tmp_path):
        """Test that external specs that are in the DB can be reused. This means they are
        preferred to concretizing another external from packages.yaml
        """
        packages_yaml = {
            "externaltool": {"externals": [{"spec": "externaltool@0.9", "prefix": "/fake/path"}]}
        }
        mutable_config.set("packages", packages_yaml)
        # Concretize with v0.9 to get a suboptimal spec, since we have gcc@10 available
        external_spec = spack.concretize.concretize_one("externaltool@0.9")
        assert external_spec.external

        root_specs = [Spec("sombrero")]
        with spack.config.override("concretizer:reuse", True):
            solver = spack.solver.asp.Solver()
            setup = spack.solver.asp.SpackSolverSetup()
            result, _, _ = solver.driver.solve(setup, root_specs, reuse=[external_spec])

        assert len(result.specs) == 1
        sombrero = result.specs[0]
        assert sombrero["externaltool"].dag_hash() == external_spec.dag_hash()

    def test_cannot_reuse_host_incompatible_libc(self):
        """Test whether reuse concretization correctly fails to reuse a spec with a host
        incompatible libc."""
        if not spack.solver.asp.using_libc_compatibility():
            pytest.skip("This test requires libc nodes")

        # We install b@1 ^glibc@2.30, and b@0 ^glibc@2.28. The former is not host compatible, the
        # latter is.
        fst = spack.concretize.concretize_one("pkg-b@1")
        fst._mark_concrete(False)
        fst.dependencies("glibc")[0].versions = VersionList(["=2.30"])
        fst._mark_concrete(True)
        snd = spack.concretize.concretize_one("pkg-b@0")

        # The spec b@1 ^glibc@2.30 is "more optimal" than b@0 ^glibc@2.28, but due to glibc
        # incompatibility, it should not be reused.
        solver = spack.solver.asp.Solver()
        setup = spack.solver.asp.SpackSolverSetup()
        result, _, _ = solver.driver.solve(setup, [Spec("pkg-b")], reuse=[fst, snd])
        assert len(result.specs) == 1
        assert result.specs[0] == snd

    @pytest.mark.regression("45321")
    @pytest.mark.parametrize(
        "corrupted_str",
        [
            "cmake@3.4.3 foo=bar",  # cmake has no variant "foo"
            "mvdefaults@1.0 foo=a,d",  # variant "foo" has no value "d"
            "cmake %gcc",  # spec has no version
        ],
    )
    def test_corrupted_external_does_not_halt_concretization(self, corrupted_str, mutable_config):
        """Tests that having a wrong variant in an external spec doesn't stop concretization"""
        corrupted_spec = Spec(corrupted_str)
        packages_yaml = {
            f"{corrupted_spec.name}": {
                "externals": [{"spec": corrupted_str, "prefix": "/dev/null"}]
            }
        }
        mutable_config.set("packages", packages_yaml)
        # Assert we don't raise due to the corrupted external entry above
        s = spack.concretize.concretize_one("pkg-a")
        assert s.concrete

    @pytest.mark.regression("44828")
    @pytest.mark.not_on_windows("Tests use linux paths")
    def test_correct_external_is_selected_from_packages_yaml(self, mutable_config):
        """Tests that when filtering external specs, the correct external is selected to
        reconstruct the prefix, and other external attributes.
        """
        packages_yaml = {
            "mpileaks": {
                "externals": [
                    {"spec": "mpileaks@2.3 +opt", "prefix": "/tmp/prefix1"},
                    {"spec": "mpileaks@2.3 ~opt", "prefix": "/tmp/prefix2"},
                ]
            }
        }
        concretizer_yaml = {
            "reuse": {"roots": True, "from": [{"type": "external", "exclude": ["+opt"]}]}
        }
        mutable_config.set("packages", packages_yaml)
        mutable_config.set("concretizer", concretizer_yaml)

        s = spack.concretize.concretize_one("mpileaks")

        # Check that we got the properties from the right external
        assert s.external
        assert s.satisfies("~opt")
        assert s.prefix == "/tmp/prefix2"

    def test_git_based_version_must_exist_to_use_ref(self):
        # gmake should fail, only has sha256
        with pytest.raises(spack.error.UnsatisfiableSpecError) as e:
            spack.concretize.concretize_one(f"gmake commit={'a' * 40}")
            assert "Cannot use commit variant with" in e.value.message


@pytest.fixture()
def duplicates_test_repository():
    repository_path = os.path.join(spack.paths.test_repos_path, "spack_repo", "duplicates_test")
    with spack.repo.use_repositories(repository_path) as mock_repo:
        yield mock_repo


@pytest.mark.usefixtures("mutable_config", "duplicates_test_repository")
class TestConcretizeSeparately:
    """Collects test on separate concretization"""

    @pytest.mark.parametrize("strategy", ["minimal", "full"])
    def test_two_gmake(self, strategy):
        """Tests that we can concretize a spec with nodes using the same build
        dependency pinned at different versions.

        o hdf5@1.0
        |\
        o | pinned-gmake@1.0
        o | gmake@3.0
         /
        o gmake@4.1

        """
        spack.config.CONFIG.set("concretizer:duplicates:strategy", strategy)
        s = spack.concretize.concretize_one("hdf5")

        # Check that hdf5 depends on gmake@=4.1
        hdf5_gmake = s["hdf5"].dependencies(name="gmake", deptype="build")
        assert len(hdf5_gmake) == 1 and hdf5_gmake[0].satisfies("@=4.1")

        # Check that pinned-gmake depends on gmake@=3.0
        pinned_gmake = s["pinned-gmake"].dependencies(name="gmake", deptype="build")
        assert len(pinned_gmake) == 1 and pinned_gmake[0].satisfies("@=3.0")

    @pytest.mark.parametrize("strategy", ["minimal", "full"])
    def test_two_setuptools(self, strategy):
        """Tests that we can concretize separate build dependencies, when we are dealing
        with extensions.

        o py-shapely@1.25.0
        |\
        | |\
        | o | py-setuptools@60
        |/ /
        | o py-numpy@1.25.0
        |/|
        | |\
        | o | py-setuptools@59
        |/ /
        o | python@3.11.2
        o | gmake@3.0
         /
        o gmake@4.1

        """
        spack.config.CONFIG.set("concretizer:duplicates:strategy", strategy)
        s = spack.concretize.concretize_one("py-shapely")
        # Requirements on py-shapely
        setuptools = s["py-shapely"].dependencies(name="py-setuptools", deptype="build")
        assert len(setuptools) == 1 and setuptools[0].satisfies("@=60")

        # Requirements on py-numpy
        setuptools = s["py-numpy"].dependencies(name="py-setuptools", deptype="build")
        assert len(setuptools) == 1 and setuptools[0].satisfies("@=59")
        gmake = s["py-numpy"].dependencies(name="gmake", deptype="build")
        assert len(gmake) == 1 and gmake[0].satisfies("@=4.1")

        # Requirements on python
        gmake = s["python"].dependencies(name="gmake", deptype="build")
        assert len(gmake) == 1 and gmake[0].satisfies("@=3.0")

    def test_solution_without_cycles(self):
        """Tests that when we concretize a spec with cycles, a fallback kicks in to recompute
        a solution without cycles.
        """
        s = spack.concretize.concretize_one("cycle-a")
        assert s["cycle-a"].satisfies("+cycle")
        assert s["cycle-b"].satisfies("~cycle")

        s = spack.concretize.concretize_one("cycle-b")
        assert s["cycle-a"].satisfies("~cycle")
        assert s["cycle-b"].satisfies("+cycle")

    @pytest.mark.parametrize("strategy", ["minimal", "full"])
    def test_pure_build_virtual_dependency(self, strategy):
        """Tests that we can concretize a pure build virtual dependency, and ensures that
        pure build virtual dependencies are accounted in the list of possible virtual
        dependencies.

        virtual-build@1.0
        | [type=build, virtual=pkgconfig]
        pkg-config@1.0
        """
        spack.config.CONFIG.set("concretizer:duplicates:strategy", strategy)

        s = spack.concretize.concretize_one("virtual-build")
        assert s["pkgconfig"].name == "pkg-config"

    @pytest.mark.regression("40595")
    def test_no_multiple_solutions_with_different_edges_same_nodes(self):
        r"""Tests that the root node, which has a dependency on py-setuptools without constraint,
        doesn't randomly pick one of the two setuptools (@=59, @=60) needed by its dependency.

        o py-floating@1.25.0/3baitsp
        |\
        | |\
        | | |\
        | o | | py-shapely@1.25.0/4hep6my
        |/| | |
        | |\| |
        | | |/
        | |/|
        | | o py-setuptools@60/cwhbthc
        | |/
        |/|
        | o py-numpy@1.25.0/5q5fx4d
        |/|
        | |\
        | o | py-setuptools@59/jvsa7sd
        |/ /
        o | python@3.11.2/pdmjekv
        o | gmake@3.0/jv7k2bl
         /
        o gmake@4.1/uo6ot3d
        """
        spec_str = "py-floating"

        root = spack.concretize.concretize_one(spec_str)
        assert root["py-shapely"].satisfies("^py-setuptools@=60")
        assert root["py-numpy"].satisfies("^py-setuptools@=59")

        edges = root.edges_to_dependencies("py-setuptools")
        assert len(edges) == 1
        assert edges[0].spec.satisfies("@=60")

    @pytest.mark.regression("43647")
    def test_specifying_different_versions_build_deps(self):
        """Tests that we can concretize a spec with nodes using the same build
        dependency pinned at different versions, when the constraint is specified
        in the root spec.

        o hdf5@1.0
        |\
        o | pinned-gmake@1.0
        o | gmake@3.0
         /
        o gmake@4.1

        """
        hdf5_str = "hdf5@1.0 ^gmake@4.1"
        pinned_str = "pinned-gmake@1.0 ^gmake@3.0"
        input_specs = [Spec(hdf5_str), Spec(pinned_str)]
        solver = spack.solver.asp.Solver()
        result = solver.solve(input_specs)

        assert any(x.satisfies(hdf5_str) for x in result.specs)
        assert any(x.satisfies(pinned_str) for x in result.specs)

    @pytest.mark.regression("44289")
    def test_all_extensions_depend_on_same_extendee(self):
        """Tests that we don't reuse dependencies that bring in a different extendee"""
        setuptools = spack.concretize.concretize_one("py-setuptools ^python@3.10")

        solver = spack.solver.asp.Solver()
        setup = spack.solver.asp.SpackSolverSetup()
        result, _, _ = solver.driver.solve(
            setup, [Spec("py-floating ^python@3.11")], reuse=list(setuptools.traverse())
        )
        assert len(result.specs) == 1

        floating = result.specs[0]
        assert all(setuptools.dag_hash() != x.dag_hash() for x in floating.traverse())
        pythons = [x for x in floating.traverse() if x.name == "python"]
        assert len(pythons) == 1 and pythons[0].satisfies("@3.11")


@pytest.mark.parametrize(
    "v_str,v_opts,checksummed",
    [
        ("1.2.3", {"sha256": f"{1:064x}"}, True),
        # it's not about the version being "infinite",
        # but whether it has a digest
        ("develop", {"sha256": f"{1:064x}"}, True),
        # other hash types
        ("1.2.3", {"checksum": f"{1:064x}"}, True),
        ("1.2.3", {"md5": f"{1:032x}"}, True),
        ("1.2.3", {"sha1": f"{1:040x}"}, True),
        ("1.2.3", {"sha224": f"{1:056x}"}, True),
        ("1.2.3", {"sha384": f"{1:096x}"}, True),
        ("1.2.3", {"sha512": f"{1:0128x}"}, True),
        # no digest key
        ("1.2.3", {"bogus": f"{1:064x}"}, False),
        # git version with full commit sha
        ("1.2.3", {"commit": f"{1:040x}"}, True),
        (f"{1:040x}=1.2.3", {}, True),
        # git version with short commit sha
        ("1.2.3", {"commit": f"{1:07x}"}, False),
        (f"{1:07x}=1.2.3", {}, False),
        # git tag is a moving target
        ("1.2.3", {"tag": "v1.2.3"}, False),
        ("1.2.3", {"tag": "v1.2.3", "commit": f"{1:07x}"}, False),
        # git branch is a moving target
        ("1.2.3", {"branch": "releases/1.2"}, False),
        # git ref is a moving target
        ("git.branch=1.2.3", {}, False),
    ],
)
def test_drop_moving_targets(v_str, v_opts, checksummed):
    v = Version(v_str)
    assert spack.solver.asp._is_checksummed_version((v, v_opts)) == checksummed


class TestConcreteSpecsByHash:
    """Tests the container of concrete specs"""

    @pytest.mark.parametrize(
        "input_specs", [["pkg-a"], ["pkg-a foobar=bar", "pkg-b"], ["pkg-a foobar=baz", "pkg-b"]]
    )
    def test_adding_specs(self, input_specs, default_mock_concretization):
        """Tests that concrete specs in the container are equivalent, but stored as different
        objects in memory.
        """
        container = spack.solver.asp.ConcreteSpecsByHash()
        input_specs = [spack.concretize.concretize_one(s) for s in input_specs]
        for s in input_specs:
            container.add(s)

        for root in input_specs:
            for node in root.traverse(root=True):
                assert node == container[node.dag_hash()]
                assert node.dag_hash() in container
                assert node is not container[node.dag_hash()]


@pytest.fixture()
def edges_test_repository():
    repository_path = os.path.join(spack.paths.test_repos_path, "spack_repo", "edges_test")
    with spack.repo.use_repositories(repository_path) as mock_repo:
        yield mock_repo


@pytest.mark.usefixtures("mutable_config", "edges_test_repository")
class TestConcretizeEdges:
    """Collects tests on edge properties"""

    @pytest.mark.parametrize(
        "spec_str,expected_satisfies,expected_not_satisfies",
        [
            ("conditional-edge", ["^zlib@2.0"], ["^zlib-api"]),
            ("conditional-edge~foo", ["^zlib@2.0"], ["^zlib-api"]),
            (
                "conditional-edge+foo",
                ["^zlib@1.0", "^zlib-api", "^[virtuals=zlib-api] zlib"],
                ["^[virtuals=mpi] zlib"],
            ),
        ],
    )
    def test_condition_triggered_by_edge_property(
        self, spec_str, expected_satisfies, expected_not_satisfies
    ):
        """Tests that we can enforce constraints based on edge attributes"""
        s = spack.concretize.concretize_one(spec_str)

        for expected in expected_satisfies:
            assert s.satisfies(expected), str(expected)

        for not_expected in expected_not_satisfies:
            assert not s.satisfies(not_expected), str(not_expected)

    def test_virtuals_provided_together_but_only_one_required_in_dag(self):
        """Tests that we can use a provider that provides more than one virtual together,
        and is providing only one, iff the others are not needed in the DAG.

        o blas-only-client
        | [virtual=blas]
        o openblas (provides blas and lapack together)

        """
        s = spack.concretize.concretize_one("blas-only-client ^openblas")
        assert s.satisfies("^[virtuals=blas] openblas")
        assert not s.satisfies("^[virtuals=blas,lapack] openblas")


def test_reusable_externals_match(mock_packages, tmpdir):
    spec = Spec("mpich@4.1~debug build_system=generic arch=linux-ubuntu23.04-zen2 %gcc@13.1.0")
    spec.external_path = tmpdir.strpath
    spec.external_modules = ["mpich/4.1"]
    spec._mark_concrete()
    assert spack.solver.asp._is_reusable(
        spec,
        {
            "mpich": {
                "externals": [
                    {"spec": "mpich@4.1", "prefix": tmpdir.strpath, "modules": ["mpich/4.1"]}
                ]
            }
        },
        local=False,
    )


def test_reusable_externals_match_virtual(mock_packages, tmpdir):
    spec = Spec("mpich@4.1~debug build_system=generic arch=linux-ubuntu23.04-zen2 %gcc@13.1.0")
    spec.external_path = tmpdir.strpath
    spec.external_modules = ["mpich/4.1"]
    spec._mark_concrete()
    assert spack.solver.asp._is_reusable(
        spec,
        {
            "mpi": {
                "externals": [
                    {"spec": "mpich@4.1", "prefix": tmpdir.strpath, "modules": ["mpich/4.1"]}
                ]
            }
        },
        local=False,
    )


def test_reusable_externals_different_prefix(mock_packages, tmpdir):
    spec = Spec("mpich@4.1~debug build_system=generic arch=linux-ubuntu23.04-zen2 %gcc@13.1.0")
    spec.external_path = "/other/path"
    spec.external_modules = ["mpich/4.1"]
    spec._mark_concrete()
    assert not spack.solver.asp._is_reusable(
        spec,
        {
            "mpich": {
                "externals": [
                    {"spec": "mpich@4.1", "prefix": tmpdir.strpath, "modules": ["mpich/4.1"]}
                ]
            }
        },
        local=False,
    )


@pytest.mark.parametrize("modules", [None, ["mpich/4.1", "libfabric/1.19"]])
def test_reusable_externals_different_modules(mock_packages, tmpdir, modules):
    spec = Spec("mpich@4.1~debug build_system=generic arch=linux-ubuntu23.04-zen2 %gcc@13.1.0")
    spec.external_path = tmpdir.strpath
    spec.external_modules = modules
    spec._mark_concrete()
    assert not spack.solver.asp._is_reusable(
        spec,
        {
            "mpich": {
                "externals": [
                    {"spec": "mpich@4.1", "prefix": tmpdir.strpath, "modules": ["mpich/4.1"]}
                ]
            }
        },
        local=False,
    )


def test_reusable_externals_different_spec(mock_packages, tmpdir):
    spec = Spec("mpich@4.1~debug build_system=generic arch=linux-ubuntu23.04-zen2 %gcc@13.1.0")
    spec.external_path = tmpdir.strpath
    spec._mark_concrete()
    assert not spack.solver.asp._is_reusable(
        spec,
        {"mpich": {"externals": [{"spec": "mpich@4.1 +debug", "prefix": tmpdir.strpath}]}},
        local=False,
    )


def test_concretization_version_order():
    versions = [
        (Version("develop"), {}),
        (Version("1.0"), {}),
        (Version("2.0"), {"deprecated": True}),
        (Version("1.1"), {}),
        (Version("1.1alpha1"), {}),
        (Version("0.9"), {"preferred": True}),
    ]
    result = [
        v
        for v, _ in sorted(
            versions, key=spack.solver.version_order.concretization_version_order, reverse=True
        )
    ]
    assert result == [
        Version("0.9"),  # preferred
        Version("1.1"),  # latest non-deprecated final version
        Version("1.0"),  # latest non-deprecated final version
        Version("1.1alpha1"),  # prereleases
        Version("develop"),  # likely development version
        Version("2.0"),  # deprecated
    ]


@pytest.mark.parametrize(
    "roots,reuse_yaml,expected,not_expected,expected_length",
    [
        (
            ["mpileaks"],
            {"roots": True, "include": ["^mpich"]},
            ["^mpich"],
            ["^mpich2", "^zmpi"],
            2,
        ),
        (
            ["mpileaks"],
            {"roots": True, "include": ["externaltest"]},
            ["externaltest"],
            ["^mpich", "^mpich2", "^zmpi"],
            1,
        ),
    ],
)
@pytest.mark.usefixtures("mutable_database", "mock_store", "do_not_check_runtimes_on_reuse")
@pytest.mark.not_on_windows("Expected length is different on Windows")
def test_filtering_reused_specs(
    roots, reuse_yaml, expected, not_expected, expected_length, mutable_config
):
    """Tests that we can select which specs are to be reused, using constraints as filters"""
    # Assume all specs have a runtime dependency
    mutable_config.set("concretizer:reuse", reuse_yaml)
    selector = spack.solver.asp.ReusableSpecsSelector(mutable_config)
    specs = selector.reusable_specs(roots)

    assert len(specs) == expected_length

    for constraint in expected:
        assert all(x.satisfies(constraint) for x in specs)

    for constraint in not_expected:
        assert all(not x.satisfies(constraint) for x in specs)


@pytest.mark.usefixtures("mutable_database", "mock_store")
@pytest.mark.parametrize(
    "reuse_yaml,expected_length",
    [({"from": [{"type": "local"}]}, 19), ({"from": [{"type": "buildcache"}]}, 0)],
)
@pytest.mark.not_on_windows("Expected length is different on Windows")
def test_selecting_reused_sources(
    reuse_yaml, expected_length, mutable_config, do_not_check_runtimes_on_reuse
):
    """Tests that we can turn on/off sources of reusable specs"""
    # Assume all specs have a runtime dependency
    mutable_config.set("concretizer:reuse", reuse_yaml)
    selector = spack.solver.asp.ReusableSpecsSelector(mutable_config)
    specs = selector.reusable_specs(["mpileaks"])
    assert len(specs) == expected_length

    # Compiler wrapper is not reused, as it might have changed from previous installations
    assert not [x for x in specs if x.name == "compiler-wrapper"]


@pytest.mark.parametrize(
    "specs,include,exclude,expected",
    [
        # "foo" discarded by include rules (everything compiled with GCC)
        (["cmake@3.27.9 %gcc", "foo %clang"], ["%gcc"], [], ["cmake@3.27.9 %gcc"]),
        # "cmake" discarded by exclude rules (everything compiled with GCC but cmake)
        (["cmake@3.27.9 %gcc", "foo %gcc"], ["%gcc"], ["cmake"], ["foo %gcc"]),
    ],
)
def test_spec_filters(specs, include, exclude, expected):
    specs = [Spec(x) for x in specs]
    expected = [Spec(x) for x in expected]
    f = spack.solver.asp.SpecFilter(
        factory=lambda: specs, is_usable=lambda x: True, include=include, exclude=exclude
    )
    assert f.selected_specs() == expected


@pytest.mark.regression("38484")
def test_git_ref_version_can_be_reused(install_mockery, do_not_check_runtimes_on_reuse):
    first_spec = spack.concretize.concretize_one(
        spack.spec.Spec("git-ref-package@git.2.1.5=2.1.5~opt")
    )
    PackageInstaller([first_spec.package], fake=True, explicit=True).install()

    with spack.config.override("concretizer:reuse", True):
        # reproducer of the issue is that spack will solve when there is a change to the base spec
        second_spec = spack.concretize.concretize_one(
            spack.spec.Spec("git-ref-package@git.2.1.5=2.1.5+opt")
        )
        assert second_spec.dag_hash() != first_spec.dag_hash()
        # we also want to confirm that reuse actually works so leave variant off to
        # let solver reuse
        third_spec = spack.spec.Spec("git-ref-package@git.2.1.5=2.1.5")
        assert first_spec.satisfies(third_spec)
        third_spec = spack.concretize.concretize_one(third_spec)
        assert third_spec.dag_hash() == first_spec.dag_hash()


@pytest.mark.parametrize("standard_version", ["2.0.0", "2.1.5", "2.1.6"])
def test_reuse_prefers_standard_over_git_versions(
    standard_version, install_mockery, do_not_check_runtimes_on_reuse
):
    """
    order matters in this test. typically reuse would pick the highest versioned installed match
    but we want to prefer the standard version over git ref based versions
    so install git ref last and ensure it is not picked up by reuse
    """
    standard_spec = spack.concretize.concretize_one(
        spack.spec.Spec(f"git-ref-package@{standard_version}")
    )
    PackageInstaller([standard_spec.package], fake=True, explicit=True).install()

    git_spec = spack.concretize.concretize_one("git-ref-package@git.2.1.5=2.1.5")
    PackageInstaller([git_spec.package], fake=True, explicit=True).install()

    with spack.config.override("concretizer:reuse", True):
        test_spec = spack.concretize.concretize_one("git-ref-package@2")
        assert git_spec.dag_hash() != test_spec.dag_hash()
        assert standard_spec.dag_hash() == test_spec.dag_hash()


@pytest.mark.parametrize("unify", [True, "when_possible", False])
def test_spec_unification(unify, mutable_config, mock_packages):
    spack.config.set("concretizer:unify", unify)
    a = "pkg-a"
    a_restricted = "pkg-a^pkg-b foo=baz"
    b = "pkg-b foo=none"

    unrestricted = spack.cmd.parse_specs([a, b], concretize=True)
    a_concrete_unrestricted = [s for s in unrestricted if s.name == "pkg-a"][0]
    b_concrete_unrestricted = [s for s in unrestricted if s.name == "pkg-b"][0]
    assert (a_concrete_unrestricted["pkg-b"] == b_concrete_unrestricted) == (unify is not False)

    maybe_fails = pytest.raises if unify is True else llnl.util.lang.nullcontext
    with maybe_fails(spack.solver.asp.UnsatisfiableSpecError):
        _ = spack.cmd.parse_specs([a_restricted, b], concretize=True)


@pytest.mark.usefixtures("mutable_config", "mock_packages", "do_not_check_runtimes_on_reuse")
@pytest.mark.parametrize(
    "spec_str, error_type",
    [
        (f"git-ref-package@main commit={'a' * 40}", None),
        (f"git-ref-package@main commit={'a' * 39}", AssertionError),
        (f"git-ref-package@2.1.6 commit={'a' * 40}", spack.error.UnsatisfiableSpecError),
        (f"git-ref-package@git.2.1.6=2.1.6 commit={'a' * 40}", None),
        (f"git-ref-package@git.{'a' * 40}=2.1.6 commit={'a' * 40}", None),
    ],
)
def test_spec_containing_commit_variant(spec_str, error_type):
    spec = spack.spec.Spec(spec_str)
    if error_type is None:
        spack.concretize.concretize_one(spec)
    else:
        with pytest.raises(error_type):
            spack.concretize.concretize_one(spec)


@pytest.mark.usefixtures("mutable_config", "mock_packages", "do_not_check_runtimes_on_reuse")
@pytest.mark.parametrize(
    "spec_str",
    [
        f"git-test-commit@git.main commit={'a' * 40}",
        f"git-test-commit@git.v1.0 commit={'a' * 40}",
        "git-test-commit@{sha} commit={sha}",
        "git-test-commit@{sha} commit=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ],
)
def test_spec_with_commit_interacts_with_lookup(mock_git_version_info, monkeypatch, spec_str):
    # This test will be short lived. Technically we could do further checks with a Lookup
    # but skipping impl since we are going to deprecate
    repo_path, filename, commits = mock_git_version_info
    file_url = pathlib.Path(repo_path).as_uri()
    monkeypatch.setattr(spack.package_base.PackageBase, "git", file_url, raising=False)
    spec = spack.spec.Spec(spec_str.format(sha=commits[-1]))
    spack.concretize.concretize_one(spec)


@pytest.mark.usefixtures("mutable_config", "mock_packages", "do_not_check_runtimes_on_reuse")
@pytest.mark.parametrize("version_str", [f"git.{'a' * 40}=main", "git.2.1.5=main"])
def test_relationship_git_versions_and_commit_variant(version_str):
    """
    Confirm that GitVersions auto assign and populates the commit variant correctly
    """
    # This should be a short lived test and can be deleted when we remove GitVersions
    spec = spack.spec.Spec(f"git-ref-package@{version_str}")
    spec = spack.concretize.concretize_one(spec)
    if spec.version.commit_sha:
        assert spec.version.commit_sha == spec.variants["commit"].value
    else:
        assert "commit" not in spec.variants


@pytest.mark.usefixtures("install_mockery", "do_not_check_runtimes_on_reuse")
def test_abstract_commit_spec_reuse():
    commit = "abcd" * 10
    spec_str_1 = f"git-ref-package@develop commit={commit}"
    spec_str_2 = f"git-ref-package commit={commit}"
    spec1 = spack.concretize.concretize_one(spec_str_1)
    PackageInstaller([spec1.package], fake=True, explicit=True).install()

    with spack.config.override("concretizer:reuse", True):
        spec2 = spack.concretize.concretize_one(spec_str_2)
        assert spec2.dag_hash() == spec1.dag_hash()


@pytest.mark.usefixtures("install_mockery", "do_not_check_runtimes_on_reuse")
@pytest.mark.parametrize(
    "installed_commit, incoming_commit, reusable",
    [("a" * 40, "b" * 40, False), (None, "b" * 40, False), ("a" * 40, None, True)],
)
def test_commit_variant_can_be_reused(installed_commit, incoming_commit, reusable):
    # install a non-default variant to test if reuse picks it
    if installed_commit:
        spec_str_1 = f"git-ref-package@develop commit={installed_commit} ~opt"
    else:
        spec_str_1 = "git-ref-package@develop ~opt"

    if incoming_commit:
        spec_str_2 = f"git-ref-package@develop commit={incoming_commit}"
    else:
        spec_str_2 = "git-ref-package@develop"

    spec1 = spack.concretize.concretize_one(spack.spec.Spec(spec_str_1))
    PackageInstaller([spec1.package], fake=True, explicit=True).install()

    with spack.config.override("concretizer:reuse", True):
        spec2 = spack.spec.Spec(spec_str_2)
        spec2 = spack.concretize.concretize_one(spec2)
        assert (spec1.dag_hash() == spec2.dag_hash()) == reusable


def test_concretization_cache_roundtrip(
    mock_packages, use_concretization_cache, monkeypatch, mutable_config
):
    """Tests whether we can write the results of a clingo solve to the cache
    and load the same spec request from the cache to produce identical specs"""
    # Force determinism:
    # Solver setup is normally non-deterministic due to non-determinism in
    # asp solver setup logic generation. The only other inputs to the cache keys are
    # the .lp files, which are invariant over the course of this test.
    # This method forces the same setup to be produced for the same specs
    # which gives us a guarantee of cache hits, as it removes the only
    # element of non deterministic solver setup for the same spec
    # Basically just a quick and dirty memoization
    solver_setup = spack.solver.asp.SpackSolverSetup.setup

    def _setup(self, specs, *, reuse=None, allow_deprecated=False):
        if not getattr(_setup, "cache_setup", None):
            cache_setup = solver_setup(self, specs, reuse=reuse, allow_deprecated=allow_deprecated)
            setattr(_setup, "cache_setup", cache_setup)
        return getattr(_setup, "cache_setup")

    # monkeypatch our forced determinism setup method into solver setup
    monkeypatch.setattr(spack.solver.asp.SpackSolverSetup, "setup", _setup)

    assert spack.config.get("config:concretization_cache:enable")

    # run one standard concretization to populate the cache and the setup method
    # memoization
    h = spack.concretize.concretize_one("hdf5")

    # due to our forced determinism above, we should not be observing
    # cache misses, assert that we're not storing any new cache entries
    def _ensure_no_store(self, problem: str, result, statistics, test=False):
        # always throw, we never want to reach this code path
        assert False, "Concretization cache hit expected"

    # Assert that we're actually hitting the cache
    cache_fetch = spack.solver.asp.ConcretizationCache.fetch

    def _ensure_cache_hits(self, problem: str):
        result, statistics = cache_fetch(self, problem)
        assert result, "Expected successful concretization cache hit"
        assert statistics, "Expected statistics to be non null on cache hit"
        return result, statistics

    monkeypatch.setattr(spack.solver.asp.ConcretizationCache, "store", _ensure_no_store)
    monkeypatch.setattr(spack.solver.asp.ConcretizationCache, "fetch", _ensure_cache_hits)
    # ensure subsequent concretizations of the same spec produce the same spec
    # object
    for _ in range(5):
        assert h == spack.concretize.concretize_one("hdf5")


@pytest.mark.regression("42679")
@pytest.mark.parametrize("compiler_str", ["gcc@=9.4.0", "gcc@=9.4.0-foo"])
def test_selecting_compiler_with_suffix(mutable_config, mock_packages, compiler_str):
    """Tests that we can select compilers whose versions differ only for a suffix."""
    packages_yaml = syaml.load_config(
        """
packages:
  gcc:
    externals:
    - spec: "gcc@9.4.0-foo languages='c,c++'"
      prefix: /path
      extra_attributes:
        compilers:
          c: /path/bin/gcc
          cxx: /path/bin/g++
"""
    )
    mutable_config.set("packages", packages_yaml["packages"])
    s = spack.concretize.concretize_one(f"libelf %{compiler_str}")
    assert s["c"].satisfies(compiler_str)


def test_duplicate_compiler_in_externals(mutable_config, mock_packages):
    """Tests that having duplicate compilers in packages.yaml do not raise and error."""
    packages_yaml = syaml.load_config(
        """
packages:
  gcc:
    externals:
    - spec: "gcc@9.4.0 languages='c,c++'"
      prefix: /path
      extra_attributes:
        compilers:
          c: /path/bin/gcc
          cxx: /path/bin/g++
    - spec: "gcc@9.4.0 languages='c,c++'"
      prefix: /path
      extra_attributes:
        compilers:
          c: /path/bin/gcc
          cxx: /path/bin/g++
"""
    )
    mutable_config.set("packages", packages_yaml["packages"])
    s = spack.concretize.concretize_one("libelf %gcc@9.4")
    assert s["c"].satisfies("gcc@9.4.0")


def test_compiler_can_depend_on_themselves_to_build(config, mock_packages):
    """Tests that a compiler can depend on "itself" to bootstrap."""
    s = spack.concretize.concretize_one("gcc@14 %gcc@9.4.0")
    assert s.satisfies("gcc@14")
    assert s.satisfies("^gcc-runtime@9.4.0")

    gcc_used_to_build = s.dependencies(name="gcc", virtuals=("c",))
    assert len(gcc_used_to_build) == 1 and gcc_used_to_build[0].satisfies("gcc@9.4.0")


def test_compiler_attribute_is_tolerated_in_externals(mutable_config, mock_packages, tmp_path):
    """Tests that we don't error out if an external specifies a compiler, even though externals
    don't have dependencies.
    """
    packages_yaml = syaml.load_config(
        f"""
packages:
  cmake:
    externals:
    - spec: "cmake@3.27.4 %gcc@14.1.0"
      prefix: {tmp_path}
"""
    )
    mutable_config.set("packages", packages_yaml["packages"])
    s = spack.concretize.concretize_one("cmake")
    assert s.external and s.external_path == str(tmp_path)


def test_compiler_can_be_built_with_other_compilers(config, mock_packages):
    """Tests that a compiler can be built also with another compiler."""
    s = spack.concretize.concretize_one("llvm@18 +clang %gcc")
    assert s.satisfies("llvm@18")

    c_compiler = s.dependencies(virtuals=("c",))
    assert len(c_compiler) == 1 and c_compiler[0].satisfies("gcc@10")


@pytest.mark.parametrize(
    "spec_str,expected",
    [
        # Only one compiler is in the DAG, so pick the external associated with it
        ("dyninst %clang", "clang"),
        ("dyninst %gcc", "gcc"),
        # Both compilers are in the DAG, so pick the best external according to other criteria
        ("dyninst %clang ^libdwarf%gcc", "clang"),
        ("dyninst %gcc ^libdwarf%clang", "clang"),
    ],
)
def test_compiler_match_for_externals_is_taken_into_account(
    spec_str, expected, mutable_config, mock_packages, tmp_path
):
    """Tests that compiler annotation for externals are somehow taken into account for a match"""
    packages_yaml = syaml.load_config(
        f"""
packages:
  libelf:
    externals:
    - spec: "libelf@0.8.12 %gcc"
      prefix: {tmp_path / 'gcc'}
    - spec: "libelf@0.8.13 %clang"
      prefix: {tmp_path / 'clang'}
"""
    )
    mutable_config.set("packages", packages_yaml["packages"])
    s = spack.concretize.concretize_one(spec_str)
    libelf = s["libelf"]
    assert libelf.external and libelf.external_path == str(tmp_path / expected)


@pytest.mark.parametrize(
    "spec_str,expected",
    [
        # Only one compiler is in the DAG, so pick the external associated with it
        ("dyninst %gcc@10", "libelf-gcc10"),
        ("dyninst %gcc@9", "libelf-gcc9"),
        # Both compilers are in the DAG, so pick the best external according to other criteria
        ("dyninst %gcc@10 ^libdwarf%gcc@9", "libelf-gcc9"),
    ],
)
def test_compiler_match_for_externals_with_versions(
    spec_str, expected, mutable_config, mock_packages, tmp_path
):
    """Tests that version constraints are taken into account for compiler annotations
    on externals
    """
    packages_yaml = syaml.load_config(
        f"""
packages:
  libelf:
    buildable: false
    externals:
    - spec: "libelf@0.8.12 %gcc@10"
      prefix: {tmp_path / 'libelf-gcc10'}
    - spec: "libelf@0.8.13 %gcc@9"
      prefix: {tmp_path / 'libelf-gcc9'}
"""
    )
    mutable_config.set("packages", packages_yaml["packages"])
    s = spack.concretize.concretize_one(spec_str)
    libelf = s["libelf"]
    assert libelf.external and libelf.external_path == str(tmp_path / expected)


def test_specifying_compilers_with_virtuals_syntax(default_mock_concretization):
    """Tests that we can pin compilers to nodes using the %[virtuals=...] syntax"""
    # clang will be used for both C and C++, since they are provided together
    mpich = default_mock_concretization("mpich %[virtuals=fortran] gcc %clang")

    assert mpich["fortran"].satisfies("gcc")
    assert mpich["c"].satisfies("llvm")
    assert mpich["cxx"].satisfies("llvm")

    # gcc is the default compiler
    mpileaks = default_mock_concretization(
        "mpileaks ^libdwarf %gcc ^mpich %[virtuals=fortran] gcc %clang"
    )

    assert mpileaks["c"].satisfies("gcc")

    libdwarf = mpileaks["libdwarf"]
    assert libdwarf["c"].satisfies("gcc")
    assert libdwarf["c"].satisfies("gcc")

    mpich = mpileaks["mpi"]
    assert mpich["fortran"].satisfies("gcc")
    assert mpich["c"].satisfies("llvm")
    assert mpich["cxx"].satisfies("llvm")


@pytest.mark.regression("49847")
@pytest.mark.xfail(sys.platform == "win32", reason="issues with install mockery")
def test_reuse_when_input_specifies_build_dep(install_mockery, do_not_check_runtimes_on_reuse):
    """Test that we can reuse a spec when specifying build dependencies in the input"""
    pkgb_old = spack.concretize.concretize_one(spack.spec.Spec("pkg-b@0.9 %gcc@9"))
    PackageInstaller([pkgb_old.package], fake=True, explicit=True).install()

    with spack.config.override("concretizer:reuse", True):
        result = spack.concretize.concretize_one("pkg-b %gcc")
        assert pkgb_old.dag_hash() == result.dag_hash()

        result = spack.concretize.concretize_one("pkg-a ^pkg-b %gcc@9")
        assert pkgb_old.dag_hash() == result["pkg-b"].dag_hash()
        assert result.satisfies("%gcc@9")

        result = spack.concretize.concretize_one("pkg-a %gcc@10 ^pkg-b %gcc@9")
        assert pkgb_old.dag_hash() == result["pkg-b"].dag_hash()


@pytest.mark.regression("49847")
def test_reuse_when_requiring_build_dep(
    install_mockery, do_not_check_runtimes_on_reuse, mutable_config
):
    """Test that we can reuse a spec when specifying build dependencies in requirements"""
    mutable_config.set("packages:all:require", "%gcc")
    pkgb_old = spack.concretize.concretize_one(spack.spec.Spec("pkg-b@0.9"))
    PackageInstaller([pkgb_old.package], fake=True, explicit=True).install()

    with spack.config.override("concretizer:reuse", True):
        result = spack.concretize.concretize_one("pkg-b")
        assert pkgb_old.dag_hash() == result.dag_hash(), result.tree()


@pytest.mark.regression("50167")
def test_input_analysis_and_conditional_requirements(default_mock_concretization):
    """Tests that input analysis doesn't account for conditional requirement
    to discard possible dependencies.

    If the requirement is conditional, and impossible to achieve on the current
    platform, the valid search space is still the complement of the condition that
    activates the requirement.
    """
    libceed = default_mock_concretization("libceed")
    assert libceed["libxsmm"].satisfies("@main")
    assert libceed["libxsmm"].satisfies("platform=test")


@pytest.mark.parametrize(
    "compiler_str,expected,not_expected",
    [
        # Compiler queries are as specific as the constraint on the external
        ("gcc@10", ["%gcc", "%gcc@10"], ["%clang", "%gcc@9"]),
        ("gcc", ["%gcc"], ["%clang", "%gcc@9", "%gcc@10"]),
    ],
)
@pytest.mark.regression("49841")
def test_installing_external_with_compilers_directly(
    compiler_str, expected, not_expected, mutable_config, mock_packages, tmp_path
):
    """Tests that version constraints are taken into account for compiler annotations
    on externals
    """
    spec_str = f"libelf@0.8.12 %{compiler_str}"
    packages_yaml = syaml.load_config(
        f"""
packages:
  libelf:
    buildable: false
    externals:
    - spec: {spec_str}
      prefix: {tmp_path / 'libelf'}
"""
    )
    mutable_config.set("packages", packages_yaml["packages"])
    s = spack.concretize.concretize_one(spec_str)

    assert s.external
    assert all(s.satisfies(c) for c in expected)
    assert all(not s.satisfies(c) for c in not_expected)


@pytest.mark.regression("49841")
def test_using_externals_with_compilers(mutable_config, mock_packages, tmp_path):
    """Tests that version constraints are taken into account for compiler annotations
    on externals, even imposed as transitive deps.
    """
    packages_yaml = syaml.load_config(
        f"""
packages:
  libelf:
    buildable: false
    externals:
    - spec: libelf@0.8.12 %gcc@10
      prefix: {tmp_path / 'libelf'}
"""
    )
    mutable_config.set("packages", packages_yaml["packages"])

    with pytest.raises(spack.error.SpackError):
        spack.concretize.concretize_one("dyninst%gcc@10.2.1 ^libelf@0.8.12 %gcc@:9")

    s = spack.concretize.concretize_one("dyninst%gcc@10.2.1 ^libelf@0.8.12 %gcc@10:")

    libelf = s["libelf"]
    assert libelf.external and libelf.satisfies("%gcc")


@pytest.mark.regression("50161")
def test_installed_compiler_and_better_external(
    install_mockery, do_not_check_runtimes_on_reuse, mutable_config
):
    """Tests that we always prefer a higher-priority external compiler, when we have a
    lower-priority compiler installed, and we try to concretize a spec without specifying
    the compiler dependency.
    """
    pkg_b = spack.concretize.concretize_one(spack.spec.Spec("pkg-b %clang"))
    PackageInstaller([pkg_b.package], fake=True, explicit=True).install()

    with spack.config.override("concretizer:reuse", False):
        pkg_a = spack.concretize.concretize_one("pkg-a")
        assert pkg_a["c"].satisfies("gcc@10"), pkg_a.tree()
        assert pkg_a["pkg-b"]["c"].satisfies("gcc@10")

    with spack.config.override("concretizer:reuse", False):
        mpileaks = spack.concretize.concretize_one("mpileaks")
        assert mpileaks.satisfies("%gcc@10")


@pytest.mark.regression("50006")
def test_concrete_multi_valued_variants_in_externals(mutable_config, mock_packages, tmp_path):
    """Tests that concrete multivalued variants in externals cannot be extended with additional
    values when concretizing.
    """
    packages_yaml = syaml.load_config(
        f"""
packages:
  gcc:
    buildable: false
    externals:
    - spec: gcc@12.1.0 languages:='c,c++'
      prefix: {tmp_path / 'gcc-12'}
      extra_attributes:
          compilers:
            c: {tmp_path / 'gcc-12'}/bin/gcc
            cxx: {tmp_path / 'gcc-12'}/bin/g++

    - spec: gcc@14.1.0 languages:=fortran
      prefix: {tmp_path / 'gcc-14'}
      extra_attributes:
        compilers:
            fortran: {tmp_path / 'gcc-14'}/bin/gfortran
"""
    )
    mutable_config.set("packages", packages_yaml["packages"])

    with pytest.raises(spack.solver.asp.UnsatisfiableSpecError):
        spack.concretize.concretize_one("pkg-b %gcc@14")

    s = spack.concretize.concretize_one("pkg-b %gcc")
    assert s["c"].satisfies("gcc@12.1.0"), s.tree()
    assert s["c"].external
    assert s["c"].satisfies("languages=c,c++") and not s["c"].satisfies("languages=fortran")


def test_concrete_multi_valued_in_input_specs(default_mock_concretization):
    """Tests that we can use := to specify exactly multivalued variants in input specs."""
    s = default_mock_concretization("gcc languages:=fortran")
    assert not s.external and s["c"].external
    assert s.satisfies("languages:=fortran")
    assert not s.satisfies("languages=c") and not s.satisfies("languages=c++")


def test_concrete_multi_valued_variants_in_requirements(mutable_config, mock_packages, tmp_path):
    """Tests that concrete multivalued variants can be imposed by requirements."""
    packages_yaml = syaml.load_config(
        """
packages:
  pkg-a:
    require:
    - libs:=static
"""
    )
    mutable_config.set("packages", packages_yaml["packages"])

    with pytest.raises(spack.solver.asp.UnsatisfiableSpecError):
        spack.concretize.concretize_one("pkg-a libs=shared")
        spack.concretize.concretize_one("pkg-a libs=shared,static")

    s = spack.concretize.concretize_one("pkg-a")
    assert s.satisfies("libs:=static")
    assert not s.satisfies("libs=shared")


def test_concrete_multi_valued_variants_in_depends_on(default_mock_concretization):
    """Tests the use of := in depends_on directives"""
    with pytest.raises(spack.solver.asp.UnsatisfiableSpecError):
        default_mock_concretization("gmt-concrete-mv-dependency ^mvdefaults foo:=c")
        default_mock_concretization("gmt-concrete-mv-dependency ^mvdefaults foo:=a,c")
        default_mock_concretization("gmt-concrete-mv-dependency ^mvdefaults foo:=b,c")

    s = default_mock_concretization("gmt-concrete-mv-dependency")
    assert s.satisfies("^mvdefaults foo:=a,b"), s.tree()
    assert not s.satisfies("^mvdefaults foo=c")


def test_concrete_multi_valued_variants_when_args(default_mock_concretization):
    """Tests the use of := in conflicts and when= arguments"""
    # Check conflicts("foo:=a,b", when="@0.9")
    with pytest.raises(spack.solver.asp.UnsatisfiableSpecError):
        default_mock_concretization("mvdefaults@0.9 foo:=a,b")

    for c in ("foo:=a", "foo:=a,b,c", "foo:=a,c", "foo:=b,c"):
        s = default_mock_concretization(f"mvdefaults@0.9 {c}")
        assert s.satisfies(c)

    # Check depends_on("pkg-b", when="foo:=b,c")
    s = default_mock_concretization("mvdefaults foo:=b,c")
    assert s.satisfies("^pkg-b")

    for c in ("foo:=a", "foo:=a,b,c", "foo:=a,b", "foo:=a,c"):
        s = default_mock_concretization(f"mvdefaults {c}")
        assert not s.satisfies("^pkg-b")


@pytest.mark.usefixtures("mock_packages")
@pytest.mark.parametrize(
    "constraint_in_yaml,unsat_request,sat_request",
    [
        # Arch parts
        pytest.param(
            "target=x86_64",
            "target=core2",
            "target=x86_64",
            marks=pytest.mark.skipif(
                platform.machine() != "x86_64", reason="only valid for x86_64"
            ),
        ),
        pytest.param(
            "target=core2",
            "target=x86_64",
            "target=core2",
            marks=pytest.mark.skipif(
                platform.machine() != "x86_64", reason="only valid for x86_64"
            ),
        ),
        ("os=debian6", "os=redhat6", "os=debian6"),
        ("platform=test", "platform=linux", "platform=test"),
        # Variants
        ("~lld", "+lld", "~lld"),
        ("+lld", "~lld", "+lld"),
    ],
)
def test_spec_parts_on_fresh_compilers(
    constraint_in_yaml, unsat_request, sat_request, mutable_config, tmp_path
):
    """Tests that spec parts like targets and variants in `%<package> target=<target> <variants>`
    are associated with `package` for `%` just as they would be for `^`, when we concretize
    without reusing.
    """
    packages_yaml = syaml.load_config(
        f"""
    packages:
      llvm::
        buildable: false
        externals:
        - spec: "llvm+clang@20 {constraint_in_yaml}"
          prefix: {tmp_path / 'llvm-20'}
    """
    )
    mutable_config.set("packages", packages_yaml["packages"])

    # Check the abstract spec is formed correctly
    abstract_spec = Spec(f"pkg-a %llvm@20 +clang {unsat_request}")
    assert abstract_spec["llvm"].satisfies(f"@20 +clang {unsat_request}")

    # Check that we can't concretize the spec, since llvm is not buildable
    with pytest.raises(spack.solver.asp.UnsatisfiableSpecError):
        spack.concretize.concretize_one(abstract_spec)

    # Check we can instead concretize if we use the correct constraint
    s = spack.concretize.concretize_one(f"pkg-a %llvm@20 +clang {sat_request}")
    assert s["c"].external and s["c"].satisfies(f"@20 +clang {sat_request}")


@pytest.mark.usefixtures("mock_packages", "mutable_database")
@pytest.mark.parametrize(
    "constraint_in_yaml,unsat_request,sat_request",
    [
        # Arch parts
        pytest.param(
            "target=x86_64",
            "target=core2",
            "target=x86_64",
            marks=pytest.mark.skipif(
                platform.machine() != "x86_64", reason="only valid for x86_64"
            ),
        ),
        pytest.param(
            "target=core2",
            "target=x86_64",
            "target=core2",
            marks=pytest.mark.skipif(
                platform.machine() != "x86_64", reason="only valid for x86_64"
            ),
        ),
        ("os=debian6", "os=redhat6", "os=debian6"),
        ("platform=test", "platform=linux", "platform=test"),
        # Variants
        ("~lld", "+lld", "~lld"),
        ("+lld", "~lld", "+lld"),
    ],
)
def test_spec_parts_on_reused_compilers(
    constraint_in_yaml, unsat_request, sat_request, mutable_config, tmp_path
):
    """Tests that requests of the form <package>%<compiler> <requests> are considered for reused
    specs, even though build dependency are not part of the ASP problem.
    """
    packages_yaml = syaml.load_config(
        f"""
    packages:
      c:
        require: llvm
      cxx:
        require: llvm
      llvm::
        buildable: false
        externals:
        - spec: "llvm+clang@20 {constraint_in_yaml}"
          prefix: {tmp_path / 'llvm-20'}
      mpileaks:
        buildable: true
    """
    )
    mutable_config.set("packages", packages_yaml["packages"])

    # Install the spec
    installed_spec = spack.concretize.concretize_one(f"mpileaks %llvm@20 {sat_request}")
    PackageInstaller([installed_spec.package], fake=True, explicit=True).install()

    # Make mpileaks not buildable
    mutable_config.set("packages:mpileaks:buildable", False)

    # Check we can't concretize with the unsat request...
    with pytest.raises(spack.solver.asp.UnsatisfiableSpecError):
        spack.concretize.concretize_one(f"mpileaks %llvm@20 {unsat_request}")

    # ...but we can with the original constraint
    with spack.config.override("concretizer:reuse", True):
        s = spack.concretize.concretize_one(f"mpileaks %llvm@20 {sat_request}")

    assert s.dag_hash() == installed_spec.dag_hash()


def test_use_compiler_by_hash(mock_packages, mutable_database, mutable_config):
    """Tests that we can reuse an installed compiler specifying its hash"""
    installed_spec = spack.concretize.concretize_one("gcc@14.0")
    PackageInstaller([installed_spec.package], fake=True, explicit=True).install()

    with spack.config.override("concretizer:reuse", True):
        s = spack.concretize.concretize_one(f"mpileaks %gcc/{installed_spec.dag_hash()}")

    assert s["c"].dag_hash() == installed_spec.dag_hash()


@pytest.mark.parametrize(
    "spec_str,expected,not_expected",
    [
        # Simple build requirement on gcc, as a provider for c
        (
            "mpileaks %gcc",
            ["%[deptypes=build] gcc"],
            ["%[deptypes=link] gcc", "%[deptypes=run] gcc"],
        ),
        # Require mpich as a direct dependency of mpileaks
        (
            "mpileaks %[deptypes=link] mpich",
            ["%[deptypes=build,link] mpich", "^callpath%[deptypes=build,link] mpich"],
            ["%[deptypes=run] mpich"],
        ),
        (
            "mpileaks %[deptypes=link] mpich+debug",  # non-default variant
            ["%[deptypes=build,link] mpich+debug"],
            ["% mpich~debug"],
        ),
        # Require mpich as a direct dependency of two nodes, with compatible constraints
        (
            "mpileaks %mpich+debug ^callpath %mpich@3.0.3",  # non-default variant
            [
                "%[deptypes=build,link] mpich@3.0.3+debug",
                "^callpath %[deptypes=build,link] mpich@3.0.3+debug",
            ],
            ["%mpich~debug"],
        ),
        # Package that has a conditional link dependency on a compiler
        ("emacs +native", ["%[virtuals=c deptypes=build,link] gcc"], []),
        ("emacs +native %gcc", ["%[virtuals=c deptypes=build,link] gcc"], []),
        ("emacs +native %[virtuals=c] gcc", ["%[virtuals=c deptypes=build,link] gcc"], []),
    ],
)
def test_specifying_direct_dependencies(
    spec_str, expected, not_expected, default_mock_concretization
):
    """Tests solving % in different scenarios, either for runtime or buildtime dependencies."""
    concrete_spec = default_mock_concretization(spec_str)

    for c in expected:
        assert concrete_spec.satisfies(c)

    for c in not_expected:
        assert not concrete_spec.satisfies(c)


@pytest.mark.parametrize(
    "spec_str,conditional_spec,expected",
    [
        # Abstract spec is False, cause the set of possible solutions in the rhs is smaller
        ("mpich", "%[when=+debug] llvm", (False, True)),
        # Abstract spec is True, since we know the condition never applies
        ("mpich~debug", "%[when=+debug] llvm", (True, True)),
        # In this case we know the condition applies
        ("mpich+debug", "%[when=+debug] llvm", (False, False)),
        ("mpich+debug %llvm+clang", "%[when=+debug] llvm", (True, True)),
        ("mpich+debug", "%[when=+debug] gcc", (False, True)),
        # Conditional specs on the lhs
        ("mpich %[when=+debug] gcc", "mpich %gcc", (False, True)),
        ("mpich %[when=+debug] gcc", "mpich %llvm", (False, False)),
        ("mpich %[when=+debug] gcc", "mpich %[when=+debug] gcc", (True, True)),
        ("mpileaks ^[when=+opt] callpath@0.9", "mpileaks ^callpath@1.0", (False, True)),
        ("mpileaks ^[when=+opt] callpath@1.0", "mpileaks ^callpath@1.0", (False, True)),
        ("mpileaks ^[when=+opt] callpath@1.0", "mpileaks ^[when=+opt] callpath@1.0", (True, True)),
        # Conditional specs on both sides
        (
            "mpileaks ^[when=+opt] callpath@1.0",
            "mpileaks ^[when=+opt+debug] callpath@1.0",
            (True, True),
        ),
        (
            "mpileaks ^[when=+opt+debug] callpath@1.0",
            "mpileaks ^[when=+opt] callpath@1.0",
            (False, True),
        ),
        (
            "mpileaks ^[when=+opt] callpath@1.0",
            "mpileaks ^[when=~debug] callpath@1.0",
            (False, True),
        ),
        # Different conditional specs associated with different nodes in the DAG, where one does
        # not apply since the condition is not met
        (
            "mpileaks %[when='%mpi' virtuals=mpi] zmpi ^libelf %[when='%mpi' virtuals=mpi] mpich",
            "mpileaks %[virtuals=mpi] zmpi",
            (False, True),
        ),
        (
            "mpileaks %[when='%mpi' virtuals=mpi] mpich ^libelf %[when='%mpi' virtuals=mpi] zmpi",
            "mpileaks %[virtuals=mpi] mpich",
            (False, True),
        ),
    ],
)
def test_satisfies_conditional_spec(
    spec_str, conditional_spec, expected, default_mock_concretization
):
    """Tests satisfies semantic when testing an abstract spec and its concretized counterpart
    with a conditional spec.
    """
    abstract_spec = Spec(spec_str)
    concrete_spec = default_mock_concretization(spec_str)
    expected_abstract, expected_concrete = expected

    assert abstract_spec.satisfies(conditional_spec) is expected_abstract
    assert concrete_spec.satisfies(conditional_spec) is expected_concrete
    assert concrete_spec.satisfies(abstract_spec)
