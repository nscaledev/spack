# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

import pathlib

import pytest

import llnl.util.lang

import spack.concretize
import spack.deptypes as dt
import spack.directives
import spack.error
import spack.paths
import spack.solver.asp
import spack.spec
import spack.spec_parser
import spack.store
import spack.variant
import spack.version as vn
from spack.error import SpecError, UnsatisfiableSpecError
from spack.spec import ArchSpec, DependencySpec, Spec, SpecFormatSigilError, SpecFormatStringError
from spack.variant import (
    InvalidVariantValueError,
    MultipleValuesInExclusiveVariantError,
    UnknownVariantError,
)


@pytest.fixture()
def setup_complex_splice(monkeypatch):
    r"""Fixture to set up splicing for two complex specs.

    a_red is a spec in which every node has the variant color=red
    c_blue is a spec in which every node has the variant color=blue

    a_red structure:
                     a -
                    / \ \
                   b   c \
                  /|\ / \ |
                 e | d   g@2
                  \|/
                  g@1

    c_blue structure:
                    c
                   /|\
                  d f \
                 /  |\ \
               g@2  e \ \
                     \| /
                     g@3

    This is not intended for use in tests that use virtuals, so ``_splice_match`` is monkeypatched
    to avoid needing package files for each spec.
    """

    def splice_match(self, other, self_root, other_root):
        return self.name == other.name

    def virtuals_provided(self, root):
        return []

    monkeypatch.setattr(Spec, "_splice_match", splice_match)
    monkeypatch.setattr(Spec, "_virtuals_provided", virtuals_provided)

    g1_red = Spec("pkg-g color=red")
    g1_red.versions = vn.VersionList([vn.Version("1")])
    g2_red = Spec("pkg-g color=red")
    g2_red.versions = vn.VersionList([vn.Version("2")])
    g2_blue = Spec("pkg-g color=blue")
    g2_blue.versions = vn.VersionList([vn.Version("2")])
    g3_blue = Spec("pkg-g color=blue")
    g3_blue.versions = vn.VersionList([vn.Version("3")])

    depflag = dt.LINK | dt.BUILD
    e_red = Spec("pkg-e color=red")
    e_red._add_dependency(g1_red, depflag=depflag, virtuals=())
    e_blue = Spec("pkg-e color=blue")
    e_blue._add_dependency(g3_blue, depflag=depflag, virtuals=())

    d_red = Spec("pkg-d color=red")
    d_red._add_dependency(g1_red, depflag=depflag, virtuals=())
    d_blue = Spec("pkg-d color=blue")
    d_blue._add_dependency(g2_blue, depflag=depflag, virtuals=())

    b_red = Spec("pkg-b color=red")
    b_red._add_dependency(e_red, depflag=depflag, virtuals=())
    b_red._add_dependency(d_red, depflag=depflag, virtuals=())
    b_red._add_dependency(g1_red, depflag=depflag, virtuals=())

    f_blue = Spec("pkg-f color=blue")
    f_blue._add_dependency(e_blue, depflag=depflag, virtuals=())
    f_blue._add_dependency(g3_blue, depflag=depflag, virtuals=())

    c_red = Spec("pkg-c color=red")
    c_red._add_dependency(d_red, depflag=depflag, virtuals=())
    c_red._add_dependency(g2_red, depflag=depflag, virtuals=())
    c_blue = Spec("pkg-c color=blue")
    c_blue._add_dependency(d_blue, depflag=depflag, virtuals=())
    c_blue._add_dependency(f_blue, depflag=depflag, virtuals=())
    c_blue._add_dependency(g3_blue, depflag=depflag, virtuals=())

    a_red = Spec("pkg-a color=red")
    a_red._add_dependency(b_red, depflag=depflag, virtuals=())
    a_red._add_dependency(c_red, depflag=depflag, virtuals=())
    a_red._add_dependency(g2_red, depflag=depflag, virtuals=())

    for spec in [e_red, e_blue, d_red, d_blue, b_red, f_blue, c_red, c_blue, a_red]:
        spec.versions = vn.VersionList([vn.Version("1")])

        a_red._mark_concrete()
        c_blue._mark_concrete()

    return a_red, c_blue


@pytest.mark.usefixtures("config", "mock_packages")
class TestSpecSemantics:
    """Test satisfies(), intersects(), constrain() and other semantic operations on specs."""

    @pytest.mark.parametrize(
        "lhs,rhs,expected",
        [
            ("libelf@0.8.13", "@0:1", "libelf@0.8.13"),
            ("libdwarf^libelf@0.8.13", "^libelf@0:1", "libdwarf^libelf@0.8.13"),
            ("libelf", Spec(), "libelf"),
            ("libdwarf", Spec(), "libdwarf"),
            ("%intel", Spec(), "%intel"),
            ("^mpi", Spec(), "^mpi"),
            ("+debug", Spec(), "+debug"),
            ("@3:", Spec(), "@3:"),
            # Versions
            ("libelf@0:2.5", "libelf@2.1:3", "libelf@2.1:2.5"),
            ("libelf@0:2.5%gcc@2:4.6", "libelf@2.1:3%gcc@4.5:4.7", "libelf@2.1:2.5%gcc@4.5:4.6"),
            # Namespaces
            ("builtin.mpich", "mpich", "builtin.mpich"),
            ("builtin.mock.mpich", "mpich", "builtin.mock.mpich"),
            ("builtin.mpich", "builtin.mpich", "builtin.mpich"),
            ("mpileaks ^builtin.mock.mpich", "^mpich", "mpileaks ^builtin.mock.mpich"),
            # Virtual dependencies are fully resolved during concretization, so we can constrain
            # abstract specs but that would result in a new node
            ("mpileaks ^builtin.mock.mpich", "^mpi", "mpileaks ^mpi ^builtin.mock.mpich"),
            (
                "mpileaks ^builtin.mock.mpich",
                "^builtin.mock.mpich",
                "mpileaks ^builtin.mock.mpich",
            ),
            # Compilers
            ("foo%gcc", "%gcc", "foo%gcc"),
            ("foo%intel", "%intel", "foo%intel"),
            ("foo%gcc", "%gcc@4.7.2", "foo%gcc@4.7.2"),
            ("foo%intel", "%intel@4.7.2", "foo%intel@4.7.2"),
            ("foo%gcc@4.5", "%gcc@4.4:4.6", "foo%gcc@4.5"),
            ("foo@2.0%gcc@4.5", "@1:3%gcc@4.4:4.6", "foo@2.0%gcc@4.5"),
            ("foo %gcc@4.7.3", "%gcc@4.7", "foo %gcc@4.7.3"),
            ("libelf %gcc@4.4.7", "libelf %gcc@4.4.7", "libelf %gcc@4.4.7"),
            ("libelf", "libelf %gcc@4.4.7", "libelf %gcc@4.4.7"),
            # Architecture
            ("foo platform=test", "platform=test", "foo platform=test"),
            ("foo platform=linux", "platform=linux", "foo platform=linux"),
            (
                "foo platform=test",
                "platform=test target=frontend",
                "foo platform=test target=frontend",
            ),
            (
                "foo platform=test",
                "platform=test os=frontend target=frontend",
                "foo platform=test os=frontend target=frontend",
            ),
            (
                "foo platform=test os=frontend target=frontend",
                "platform=test",
                "foo platform=test os=frontend target=frontend",
            ),
            ("foo arch=test-None-None", "platform=test", "foo platform=test"),
            (
                "foo arch=test-None-frontend",
                "platform=test target=frontend",
                "foo platform=test target=frontend",
            ),
            (
                "foo arch=test-frontend-frontend",
                "platform=test os=frontend target=frontend",
                "foo platform=test os=frontend target=frontend",
            ),
            (
                "foo arch=test-frontend-frontend",
                "platform=test",
                "foo platform=test os=frontend target=frontend",
            ),
            (
                "foo platform=test target=backend os=backend",
                "platform=test target=backend os=backend",
                "foo platform=test target=backend os=backend",
            ),
            (
                "libelf target=default_target os=default_os",
                "libelf target=default_target os=default_os",
                "libelf target=default_target os=default_os",
            ),
            # Dependencies
            ("mpileaks ^mpich", "^mpich", "mpileaks ^mpich"),
            ("mpileaks ^mpich@2.0", "^mpich@1:3", "mpileaks ^mpich@2.0"),
            (
                "mpileaks ^mpich@2.0 ^callpath@1.5",
                "^mpich@1:3 ^callpath@1.4:1.6",
                "mpileaks^mpich@2.0^callpath@1.5",
            ),
            ("mpileaks ^mpi", "^mpi", "mpileaks ^mpi"),
            ("mpileaks ^mpi", "^mpich", "mpileaks ^mpi ^mpich"),
            ("mpileaks^mpi@1.5", "^mpi@1.2:1.6", "mpileaks^mpi@1.5"),
            ("mpileaks^mpi@2:", "^mpich", "mpileaks^mpi@2: ^mpich"),
            ("mpileaks^mpi@2:", "^mpich@3.0.4", "mpileaks^mpi@2: ^mpich@3.0.4"),
            # Variants
            ("mpich+foo", "mpich+foo", "mpich+foo"),
            ("mpich++foo", "mpich++foo", "mpich++foo"),
            ("mpich~foo", "mpich~foo", "mpich~foo"),
            ("mpich~~foo", "mpich~~foo", "mpich~~foo"),
            ("mpich foo=1", "mpich foo=1", "mpich foo=1"),
            ("mpich foo==1", "mpich foo==1", "mpich foo==1"),
            ("mpich+foo", "mpich foo=True", "mpich+foo"),
            ("mpich++foo", "mpich foo=True", "mpich+foo"),
            ("mpich foo=true", "mpich+foo", "mpich+foo"),
            ("mpich foo==true", "mpich++foo", "mpich++foo"),
            ("mpich~foo", "mpich foo=FALSE", "mpich~foo"),
            ("mpich~~foo", "mpich foo=FALSE", "mpich~foo"),
            ("mpich foo=False", "mpich~foo", "mpich~foo"),
            ("mpich foo==False", "mpich~foo", "mpich~foo"),
            ("mpich foo=*", "mpich~foo", "mpich~foo"),
            ("mpich+foo", "mpich foo=*", "mpich+foo"),
            (
                'multivalue-variant foo="bar,baz"',
                "multivalue-variant foo=bar,baz",
                "multivalue-variant foo=bar,baz",
            ),
            (
                'multivalue-variant foo="bar,baz"',
                "multivalue-variant foo=*",
                "multivalue-variant foo=bar,baz",
            ),
            (
                'multivalue-variant foo="bar,baz"',
                "multivalue-variant foo=bar",
                "multivalue-variant foo=bar,baz",
            ),
            (
                'multivalue-variant foo="bar,baz"',
                "multivalue-variant foo=baz",
                "multivalue-variant foo=bar,baz",
            ),
            (
                'multivalue-variant foo="bar,baz,barbaz"',
                "multivalue-variant foo=bar,baz",
                "multivalue-variant foo=bar,baz,barbaz",
            ),
            (
                'multivalue-variant foo="bar,baz"',
                'foo="baz,bar"',  # Order of values doesn't matter
                "multivalue-variant foo=bar,baz",
            ),
            ("mpich+foo", "mpich", "mpich+foo"),
            ("mpich~foo", "mpich", "mpich~foo"),
            ("mpich foo=1", "mpich", "mpich foo=1"),
            ("mpich", "mpich++foo", "mpich++foo"),
            ("libelf+debug", "libelf+foo", "libelf+debug+foo"),
            ("libelf+debug", "libelf+debug+foo", "libelf+debug+foo"),
            ("libelf debug=2", "libelf foo=1", "libelf debug=2 foo=1"),
            ("libelf debug=2", "libelf debug=2 foo=1", "libelf debug=2 foo=1"),
            ("libelf+debug", "libelf~foo", "libelf+debug~foo"),
            ("libelf+debug", "libelf+debug~foo", "libelf+debug~foo"),
            ("libelf++debug", "libelf+debug+foo", "libelf+debug+foo"),
            ("libelf debug==2", "libelf foo=1", "libelf debug==2 foo=1"),
            ("libelf debug==2", "libelf debug=2 foo=1", "libelf debug=2 foo=1"),
            ("libelf++debug", "libelf++debug~foo", "libelf++debug~foo"),
            ("libelf foo=bar,baz", "libelf foo=*", "libelf foo=bar,baz"),
            ("libelf foo=*", "libelf foo=bar,baz", "libelf foo=bar,baz"),
            (
                'multivalue-variant foo="bar"',
                'multivalue-variant foo="baz"',
                'multivalue-variant foo="bar,baz"',
            ),
            (
                'multivalue-variant foo="bar,barbaz"',
                'multivalue-variant foo="baz"',
                'multivalue-variant foo="bar,baz,barbaz"',
            ),
            # Namespace (special case, but like variants
            ("builtin.libelf", "namespace=builtin", "builtin.libelf"),
            ("libelf", "namespace=builtin", "builtin.libelf"),
            # Flags
            ("mpich ", 'mpich cppflags="-O3"', 'mpich cppflags="-O3"'),
            (
                'mpich cppflags="-O3 -Wall"',
                'mpich cppflags="-O3 -Wall"',
                'mpich cppflags="-O3 -Wall"',
            ),
            ('mpich cppflags=="-O3"', 'mpich cppflags=="-O3"', 'mpich cppflags=="-O3"'),
            (
                'libelf cflags="-O3"',
                'libelf cppflags="-Wall"',
                'libelf cflags="-O3" cppflags="-Wall"',
            ),
            (
                'libelf cflags="-O3"',
                'libelf cppflags=="-Wall"',
                'libelf cflags="-O3" cppflags=="-Wall"',
            ),
            (
                'libelf cflags=="-O3"',
                'libelf cppflags=="-Wall"',
                'libelf cflags=="-O3" cppflags=="-Wall"',
            ),
            (
                'libelf cflags="-O3"',
                'libelf cflags="-O3" cppflags="-Wall"',
                'libelf cflags="-O3" cppflags="-Wall"',
            ),
            (
                "libelf patches=ba5e334fe247335f3a116decfb5284100791dc302b5571ff5e664d8f9a6806c2",
                "libelf patches=ba5e3",  # constrain by a patch sha256 prefix
                # TODO: the result below is not ideal. Prefix satisfies() works for patches, but
                # constrain() isn't similarly special-cased to do the same thing
                (
                    "libelf patches=ba5e3,"
                    "ba5e334fe247335f3a116decfb5284100791dc302b5571ff5e664d8f9a6806c2"
                ),
            ),
            # deptypes on direct deps
            (
                "mpileaks %[deptypes=build] mpich",
                "mpileaks %[deptypes=link] mpich",
                "mpileaks %[deptypes=build,link] mpich",
            ),
            # conditional edges
            (
                "libelf",
                "%[when='%c' virtuals=c]gcc ^[when='+mpi' virtuals=mpi]mpich",
                "libelf %[when='%c' virtuals=c]gcc ^[when='+mpi' virtuals=mpi]mpich",
            ),
            (
                "libelf %[when='%c' virtuals=c]gcc",
                "%[when='%c' virtuals=c]gcc@10.3.1",
                "libelf%[when='%c' virtuals=c]gcc@10.3.1",
            ),
            (
                "libelf %[when='%c' virtuals=c]gcc",
                "%[when='%c' virtuals=c]gcc@10.3.1 ^[when='+mpi'] mpich",
                "libelf%[when='%c' virtuals=c]gcc@10.3.1 ^[when='+mpi']mpich",
            ),
            (
                "libelf %[when='%c' virtuals=c]gcc",
                "%[when='%cxx' virtuals=cxx]gcc@10.3.1",
                "libelf%[when='%c' virtuals=c]gcc %[when='%cxx' virtuals=cxx]gcc@10.3.1",
            ),
            (
                "libelf %[when='+c' virtuals=c]gcc",
                "%[when='%c' virtuals=c]gcc@10.3.1",
                "libelf %[when='+c' virtuals=c]gcc %[when='%c' virtuals=c]gcc@10.3.1",
            ),
        ],
    )
    def test_abstract_specs_can_constrain_each_other(self, lhs, rhs, expected):
        """Test that lhs and rhs intersect with each other, and that they can be constrained
        with each other. Also check that the constrained result match the expected spec.
        """
        lhs, rhs, expected = Spec(lhs), Spec(rhs), Spec(expected)

        assert lhs.intersects(rhs)
        assert rhs.intersects(lhs)

        c1, c2 = lhs.copy(), rhs.copy()
        c1.constrain(rhs)
        c2.constrain(lhs)
        assert c1 == c2
        assert c1 == expected

    @pytest.mark.parametrize(
        "lhs,rhs,expected_lhs,expected_rhs,propagated_lhs,propagated_rhs",
        [
            (
                'mpich cppflags="-O3"',
                'mpich cppflags="-O2"',
                'mpich cppflags="-O3 -O2"',
                'mpich cppflags="-O2 -O3"',
                [],
                [],
            ),
            (
                'mpich cflags="-O3 -g"',
                'mpich cflags=="-O3"',
                'mpich cflags="-O3 -g"',
                'mpich cflags="-O3 -g"',
                [],
                [],
            ),
            (
                'mpich cflags=="-O3 -g"',
                'mpich cflags=="-O3"',
                'mpich cflags=="-O3 -g"',
                'mpich cflags=="-O3 -g"',
                [("cflags", "-O3"), ("cflags", "-g")],
                [("cflags", "-O3"), ("cflags", "-g")],
            ),
        ],
    )
    def test_constrain_compiler_flags(
        self, lhs, rhs, expected_lhs, expected_rhs, propagated_lhs, propagated_rhs
    ):
        """Constraining is asymmetric for compiler flags."""
        lhs, rhs, expected_lhs, expected_rhs = (
            Spec(lhs),
            Spec(rhs),
            Spec(expected_lhs),
            Spec(expected_rhs),
        )

        assert lhs.intersects(rhs)
        assert rhs.intersects(lhs)

        c1, c2 = lhs.copy(), rhs.copy()
        c1.constrain(rhs)
        c2.constrain(lhs)

        assert c1 == expected_lhs
        assert c2 == expected_rhs
        for x in [c1, c2]:
            assert x.satisfies(lhs)
            assert x.satisfies(rhs)

        def _propagated_flags(_spec):
            result = set()
            for flagtype in _spec.compiler_flags:
                for flag in _spec.compiler_flags[flagtype]:
                    if flag.propagate:
                        result.add((flagtype, flag))
            return result

        assert set(propagated_lhs) <= _propagated_flags(c1)
        assert set(propagated_rhs) <= _propagated_flags(c2)

    def test_constrain_specs_by_hash(self, default_mock_concretization, database):
        """Test that Specs specified only by their hashes can constrain eachother."""
        mpich_dag_hash = "/" + database.query_one("mpich").dag_hash()
        spec = Spec(mpich_dag_hash[:7])
        assert spec.constrain(Spec(mpich_dag_hash)) is False
        assert spec.abstract_hash == mpich_dag_hash[1:]

    def test_mismatched_constrain_spec_by_hash(self, default_mock_concretization, database):
        """Test that Specs specified only by their incompatible hashes fail appropriately."""
        lhs = "/" + database.query_one("callpath ^mpich").dag_hash()
        rhs = "/" + database.query_one("callpath ^mpich2").dag_hash()
        with pytest.raises(spack.spec.InvalidHashError):
            Spec(lhs).constrain(Spec(rhs))
        with pytest.raises(spack.spec.InvalidHashError):
            Spec(lhs[:7]).constrain(Spec(rhs))

    @pytest.mark.parametrize(
        "lhs,rhs", [("libelf", Spec()), ("libelf", "@0:1"), ("libelf", "@0:1 %gcc")]
    )
    def test_concrete_specs_which_satisfies_abstract(self, lhs, rhs, default_mock_concretization):
        """Test that constraining an abstract spec by a compatible concrete one makes the
        abstract spec concrete, and equal to the one it was constrained with.
        """
        lhs, rhs = default_mock_concretization(lhs), Spec(rhs)

        assert lhs.intersects(rhs)
        assert rhs.intersects(lhs)
        assert lhs.satisfies(rhs)
        assert not rhs.satisfies(lhs)

        assert lhs.constrain(rhs) is False
        assert rhs.constrain(lhs) is True

        assert rhs.concrete
        assert lhs.satisfies(rhs)
        assert rhs.satisfies(lhs)
        assert lhs == rhs

    @pytest.mark.parametrize(
        "lhs,rhs",
        [
            ("foo platform=linux", "platform=test os=redhat6 target=x86"),
            ("foo os=redhat6", "platform=test os=debian6 target=x86_64"),
            ("foo target=x86_64", "platform=test os=redhat6 target=x86"),
            ("foo%gcc@4.3", "%gcc@4.4:4.6"),
            ("foo@4.0%gcc", "@1:3%gcc"),
            ("foo@4.0%gcc@4.5", "@1:3%gcc@4.4:4.6"),
            ("builtin.mock.mpich", "builtin.mpich"),
            ("mpileaks ^builtin.mock.mpich", "^builtin.mpich"),
            ("mpileaks^mpich@1.2", "^mpich@2.0"),
            ("mpileaks^mpich@4.0^callpath@1.5", "^mpich@1:3^callpath@1.4:1.6"),
            ("mpileaks^mpich@2.0^callpath@1.7", "^mpich@1:3^callpath@1.4:1.6"),
            ("mpileaks^mpich@4.0^callpath@1.7", "^mpich@1:3^callpath@1.4:1.6"),
            ("mpileaks^mpi@3", "^mpi@1.2:1.6"),
            ("mpileaks^mpi@3:", "^mpich2@1.4"),
            ("mpileaks^mpi@3:", "^mpich2"),
            ("mpileaks^mpi@3:", "^mpich@1.0"),
            ("mpich~foo", "mpich+foo"),
            ("mpich+foo", "mpich~foo"),
            ("mpich foo=True", "mpich foo=False"),
            ("mpich~~foo", "mpich++foo"),
            ("mpich++foo", "mpich~~foo"),
            ("mpich foo==True", "mpich foo==False"),
            ("libelf@0:2.0", "libelf@2.1:3"),
            ("libelf@0:2.5%gcc@4.8:4.9", "libelf@2.1:3%gcc@4.5:4.7"),
            ("libelf+debug", "libelf~debug"),
            ("libelf+debug~foo", "libelf+debug+foo"),
            ("libelf debug=True", "libelf debug=False"),
            ("namespace=builtin.mock", "namespace=builtin"),
        ],
    )
    def test_constraining_abstract_specs_with_empty_intersection(self, lhs, rhs):
        """Check that two abstract specs with an empty intersection cannot be constrained
        with each other.
        """
        lhs, rhs = Spec(lhs), Spec(rhs)

        assert not lhs.intersects(rhs)
        assert not rhs.intersects(lhs)

        with pytest.raises(UnsatisfiableSpecError):
            lhs.constrain(rhs)

        with pytest.raises(UnsatisfiableSpecError):
            rhs.constrain(lhs)

    @pytest.mark.parametrize(
        "lhs,rhs",
        [
            ("mpich", "mpich +foo"),
            ("mpich", "mpich~foo"),
            ("mpich", "mpich foo=1"),
            ("multivalue-variant foo=bar", "multivalue-variant +foo"),
            ("multivalue-variant foo=bar", "multivalue-variant ~foo"),
            ("multivalue-variant fee=bar", "multivalue-variant fee=baz"),
        ],
    )
    def test_concrete_specs_which_do_not_satisfy_abstract(
        self, lhs, rhs, default_mock_concretization
    ):
        lhs, rhs = default_mock_concretization(lhs), Spec(rhs)

        assert lhs.intersects(rhs) is False
        assert rhs.intersects(lhs) is False
        assert not lhs.satisfies(rhs)
        assert not rhs.satisfies(lhs)

        with pytest.raises(UnsatisfiableSpecError):
            assert lhs.constrain(rhs)

        with pytest.raises(UnsatisfiableSpecError):
            assert rhs.constrain(lhs)

    @pytest.mark.parametrize(
        "lhs,rhs", [("mpich", "mpich++foo"), ("mpich", "mpich~~foo"), ("mpich", "mpich foo==1")]
    )
    def test_concrete_specs_which_satisfy_abstract(self, lhs, rhs, default_mock_concretization):
        lhs, rhs = default_mock_concretization(lhs), Spec(rhs)

        assert lhs.intersects(rhs)
        assert rhs.intersects(lhs)
        assert lhs.satisfies(rhs)

        s1 = lhs.copy()
        s1.constrain(rhs)
        assert s1 == lhs and s1.satisfies(lhs)

        s2 = rhs.copy()
        s2.constrain(lhs)
        assert s2 == lhs and s2.satisfies(lhs)

    @pytest.mark.parametrize(
        "lhs,rhs,expected,constrained",
        [
            # hdf5++mpi satisfies hdf5, and vice versa, because of the non-contradiction semantic
            ("hdf5++mpi", "hdf5", True, "hdf5++mpi"),
            ("hdf5", "hdf5++mpi", True, "hdf5++mpi"),
            # Same holds true for arbitrary propagated variants
            ("hdf5++mpi", "hdf5++shared", True, "hdf5++mpi++shared"),
            # Here hdf5+mpi satisfies hdf5++mpi but not vice versa
            ("hdf5++mpi", "hdf5+mpi", False, "hdf5+mpi"),
            ("hdf5+mpi", "hdf5++mpi", True, "hdf5+mpi"),
            # Non contradiction is violated
            ("hdf5 ^foo~mpi", "hdf5++mpi", False, "hdf5++mpi ^foo~mpi"),
            ("hdf5++mpi", "hdf5 ^foo~mpi", False, "hdf5++mpi ^foo~mpi"),
        ],
    )
    def test_abstract_specs_with_propagation(self, lhs, rhs, expected, constrained):
        """Tests (and documents) behavior of variant propagation on abstract specs.

        Propagated variants do not comply with subset semantic, making it difficult to give
        precise definitions. Here we document the behavior that has been decided for the
        practical cases we face.
        """
        lhs, rhs, constrained = Spec(lhs), Spec(rhs), Spec(constrained)
        assert lhs.satisfies(rhs) is expected

        c = lhs.copy()
        c.constrain(rhs)
        assert c == constrained

        c = rhs.copy()
        c.constrain(lhs)
        assert c == constrained

    def test_basic_satisfies_conditional_dep(self, default_mock_concretization):
        """Tests basic semantic of satisfies with conditional dependencies, on a concrete spec"""
        concrete = default_mock_concretization("mpileaks ^mpich")

        # This branch exists, so the condition is met, and is satisfied
        assert concrete.satisfies("^[virtuals=mpi] mpich")
        assert concrete.satisfies("^[when='^notapackage' virtuals=mpi] mpich")
        assert concrete.satisfies("^[when='^mpi' virtuals=mpi] mpich")

        # This branch does not exist, but the condition is not met
        assert not concrete.satisfies("^zmpi")
        assert concrete.satisfies("^[when='^notapackage'] zmpi")
        assert not concrete.satisfies("^[when='^mpi'] zmpi")

    def test_satisfies_single_valued_variant(self):
        """Tests that the case reported in
        https://github.com/spack/spack/pull/2386#issuecomment-282147639
        is handled correctly.
        """
        a = spack.concretize.concretize_one("pkg-a foobar=bar")

        assert a.satisfies("foobar=bar")
        assert a.satisfies("foobar=*")

        # Assert that an autospec generated from a literal
        # gives the right result for a single valued variant
        assert "foobar=bar" in a
        assert "foobar==bar" in a
        assert "foobar=baz" not in a
        assert "foobar=fee" not in a

        # ... and for a multi valued variant
        assert "foo=bar" in a

        # Check that conditional dependencies are treated correctly
        assert "^pkg-b" in a

    def test_unsatisfied_single_valued_variant(self):
        a = spack.concretize.concretize_one("pkg-a foobar=baz")
        assert "^pkg-b" not in a

        mv = spack.concretize.concretize_one("multivalue-variant")
        assert "pkg-a@1.0" not in mv

    def test_indirect_unsatisfied_single_valued_variant(self):
        spec = spack.concretize.concretize_one("singlevalue-variant-dependent")
        assert "pkg-a@1.0" not in spec

    def test_satisfied_namespace(self):
        spec = spack.concretize.concretize_one("zlib")
        assert spec.satisfies("namespace=builtin_mock")
        assert not spec.satisfies("namespace=builtin")

    @pytest.mark.parametrize(
        "spec_string",
        [
            "tcl namespace==foobar",
            "tcl arch==foobar",
            "tcl os==foobar",
            "tcl patches==foobar",
            "tcl dev_path==foobar",
        ],
    )
    def test_propagate_reserved_variant_names(self, spec_string):
        with pytest.raises(spack.spec_parser.SpecParsingError, match="Propagation"):
            Spec(spec_string)

    def test_multivalued_variant_1(self, default_mock_concretization):
        # Semantics for a multi-valued variant is different
        # Depending on whether the spec is concrete or not

        a = default_mock_concretization("multivalue-variant foo=bar")
        b = Spec("multivalue-variant foo=bar,baz")
        assert not a.satisfies(b)

    def test_multivalued_variant_2(self):
        a = Spec("multivalue-variant foo=bar")
        b = Spec("multivalue-variant foo=bar,baz")
        # The specs are abstract and they **could** be constrained
        assert b.satisfies(a) and not a.satisfies(b)
        # An abstract spec can instead be constrained
        assert a.constrain(b)

    def test_multivalued_variant_3(self, default_mock_concretization):
        a = default_mock_concretization("multivalue-variant foo=bar,baz")
        b = Spec("multivalue-variant foo=bar,baz,quux")
        assert not a.satisfies(b)

    def test_multivalued_variant_4(self):
        a = Spec("multivalue-variant foo=bar,baz")
        b = Spec("multivalue-variant foo=bar,baz,quux")
        # The specs are abstract and they **could** be constrained
        assert a.intersects(b)
        # An abstract spec can instead be constrained
        assert a.constrain(b)
        # ...but will fail during concretization if there are
        # values in the variant that are not allowed
        with pytest.raises(InvalidVariantValueError):
            spack.concretize.concretize_one(a)

    def test_multivalued_variant_5(self):
        # This time we'll try to set a single-valued variant
        a = Spec("multivalue-variant fee=bar")
        b = Spec("multivalue-variant fee=baz")
        # The specs are abstract and they **could** be constrained,
        # as before concretization I don't know which type of variant
        # I have (if it is not a BV)
        assert a.intersects(b)
        # A variant cannot be parsed as single-valued until we try to
        # concretize. This means that we can constrain the variant above
        assert a.constrain(b)
        # ...but will fail during concretization if there are
        # multiple values set
        with pytest.raises(MultipleValuesInExclusiveVariantError):
            spack.concretize.concretize_one(a)

    def test_copy_satisfies_transitive(self):
        spec = spack.concretize.concretize_one("dttop")
        copy = spec.copy()
        for s, t in zip(spec.traverse(), copy.traverse()):
            assert s.satisfies(t)
            assert t.satisfies(s)

    def test_intersects_virtual(self):
        assert Spec("mpich").intersects(Spec("mpi"))
        assert Spec("mpich2").intersects(Spec("mpi"))
        assert Spec("zmpi").intersects(Spec("mpi"))

    def test_intersects_virtual_providers(self):
        """Tests that we can always intersect virtual providers from abstract specs.
        Concretization will give meaning to virtuals, and eventually forbid certain
        configurations.
        """
        assert Spec("netlib-lapack ^openblas").intersects("netlib-lapack ^openblas")
        assert Spec("netlib-lapack ^netlib-blas").intersects("netlib-lapack ^openblas")
        assert Spec("netlib-lapack ^openblas").intersects("netlib-lapack ^netlib-blas")
        assert Spec("netlib-lapack ^netlib-blas").intersects("netlib-lapack ^netlib-blas")

    def test_intersectable_concrete_specs_must_have_the_same_hash(self):
        """Ensure that concrete specs are matched *exactly* by hash."""
        s1 = spack.concretize.concretize_one("mpileaks")
        s2 = s1.copy()

        assert s1.satisfies(s2)
        assert s2.satisfies(s1)
        assert s1.intersects(s2)

        # Simulate specs that were installed before and after a change to
        # Spack's hashing algorithm.  This just reverses s2's hash.
        s2._hash = s1.dag_hash()[-1::-1]

        assert not s1.satisfies(s2)
        assert not s2.satisfies(s1)
        assert not s1.intersects(s2)

    # ========================================================================
    # Indexing specs
    # ========================================================================
    def test_self_index(self):
        s = Spec("callpath")
        assert s["callpath"] == s

    def test_dep_index(self, default_mock_concretization):
        """Tests __getitem__ and __contains__ for specs."""
        s = default_mock_concretization("callpath")

        assert s["callpath"] == s

        # Real dependencies
        for key in ("dyninst", "libdwarf", "libelf"):
            assert isinstance(s[key], Spec)
            assert s[key].name == key
            assert key in s

        # Virtual dependencies
        assert s["mpi"].name == "mpich"
        assert "mpi" in s

    @pytest.mark.usefixtures("config")
    def test_virtual_index(self):
        s = spack.concretize.concretize_one("callpath")
        s_mpich = spack.concretize.concretize_one("callpath ^mpich")
        s_mpich2 = spack.concretize.concretize_one("callpath ^mpich2")
        s_zmpi = spack.concretize.concretize_one("callpath ^zmpi")

        assert s["mpi"].name != "mpi"
        assert s_mpich["mpi"].name == "mpich"
        assert s_mpich2["mpi"].name == "mpich2"
        assert s_zmpi["zmpi"].name == "zmpi"

        for spec in [s, s_mpich, s_mpich2, s_zmpi]:
            assert "mpi" in spec

    @pytest.mark.parametrize(
        "lhs,rhs",
        [
            ("libelf", "@1.0"),
            ("libelf", "@1.0:5.0"),
            ("libelf", "%gcc"),
            ("libelf%gcc", "%gcc@4.5"),
            ("libelf", "+debug"),
            ("libelf", "debug=*"),
            ("libelf", "~debug"),
            ("libelf", "debug=2"),
            ("libelf", 'cppflags="-O3"'),
            ("libelf", 'cppflags=="-O3"'),
            ("libelf^foo", "libelf^foo@1.0"),
            ("libelf^foo", "libelf^foo@1.0:5.0"),
            ("libelf^foo", "libelf^foo%gcc"),
            ("libelf^foo%gcc", "libelf^foo%gcc@4.5"),
            ("libelf^foo", "libelf^foo+debug"),
            ("libelf^foo", "libelf^foo~debug"),
            ("libelf", "^foo"),
            ("mpileaks ^callpath %gcc@14", "mpileaks ^callpath %gcc@14.1"),
            ("mpileaks %[deptypes=build] mpich", "mpileaks %[deptypes=link] mpich"),
            ("mpileaks %mpich", "mpileaks %[deptypes=link] mpich"),
        ],
    )
    def test_lhs_is_changed_when_constraining(self, lhs, rhs):
        lhs, rhs = Spec(lhs), Spec(rhs)

        assert lhs.intersects(rhs)
        assert rhs.intersects(lhs)
        assert not lhs.satisfies(rhs)

        assert lhs.constrain(rhs) is True
        assert lhs.satisfies(rhs)

    @pytest.mark.parametrize(
        "lhs,rhs",
        [
            ("libelf", "libelf"),
            ("libelf@1.0", "@1.0"),
            ("libelf@1.0:5.0", "@1.0:5.0"),
            ("libelf%gcc", "%gcc"),
            ("libelf%gcc@4.5", "%gcc@4.5"),
            ("libelf+debug", "+debug"),
            ("libelf~debug", "~debug"),
            ("libelf debug=2", "debug=2"),
            ("libelf debug=2", "debug=*"),
            ('libelf cppflags="-O3"', 'cppflags="-O3"'),
            ('libelf cppflags=="-O3"', 'cppflags=="-O3"'),
            ("libelf^foo@1.0", "libelf^foo@1.0"),
            ("libelf^foo@1.0:5.0", "libelf^foo@1.0:5.0"),
            ("libelf^foo%gcc", "libelf^foo%gcc"),
            ("libelf^foo%gcc@4.5", "libelf^foo%gcc@4.5"),
            ("libelf^foo+debug", "libelf^foo+debug"),
            ("libelf^foo~debug", "libelf^foo~debug"),
            ('libelf^foo cppflags="-O3"', 'libelf^foo cppflags="-O3"'),
            ("mpileaks ^callpath %gcc@14.1", "mpileaks ^callpath %gcc@14"),
            ("mpileaks %[deptypes=build] gcc@14.1", "mpileaks %gcc@14"),
        ],
    )
    def test_lhs_is_not_changed_when_constraining(self, lhs, rhs):
        lhs, rhs = Spec(lhs), Spec(rhs)
        assert lhs.intersects(rhs)
        assert rhs.intersects(lhs)
        assert lhs.satisfies(rhs)
        assert lhs.constrain(rhs) is False

    def test_exceptional_paths_for_constructor(self):
        with pytest.raises(TypeError):
            Spec((1, 2))

        with pytest.raises(ValueError):
            Spec("libelf foo")

    def test_spec_formatting(self, default_mock_concretization):
        spec = default_mock_concretization("multivalue-variant cflags=-O2")

        # Testing named strings ie {string} and whether we get
        # the correct component
        # Mixed case intentional to test both
        # Fields are as follow
        # fmt_str: the format string to test
        # sigil: the portion that is a sigil (may be empty string)
        # prop: the property to get
        # component: subcomponent of spec from which to get property
        package_segments = [
            ("{NAME}", "", "name", lambda spec: spec),
            ("{VERSION}", "", "version", lambda spec: spec),
            ("{compiler}", "", "compiler", lambda spec: spec),
            ("{compiler_flags}", "", "compiler_flags", lambda spec: spec),
            ("{variants}", "", "variants", lambda spec: spec),
            ("{architecture}", "", "architecture", lambda spec: spec),
            ("{@VERSIONS}", "@", "versions", lambda spec: spec),
            ("{%compiler}", "%", "compiler", lambda spec: spec),
            ("{arch=architecture}", "arch=", "architecture", lambda spec: spec),
            ("{namespace=namespace}", "namespace=", "namespace", lambda spec: spec),
            ("{compiler.name}", "", "name", lambda spec: spec.compiler),
            ("{compiler.version}", "", "version", lambda spec: spec.compiler),
            (
                "{compiler.version.up_to_1}",
                "",
                "up_to_1",
                lambda spec: spec.compiler.version.up_to(1),
            ),
            ("{%compiler.name}", "%", "name", lambda spec: spec.compiler),
            ("{@compiler.version}", "@", "version", lambda spec: spec.compiler),
            ("{architecture.platform}", "", "platform", lambda spec: spec.architecture),
            ("{architecture.os}", "", "os", lambda spec: spec.architecture),
            ("{architecture.target}", "", "target", lambda spec: spec.architecture),
            ("{prefix}", "", "prefix", lambda spec: spec),
            ("{external}", "", "external", lambda spec: spec),  # test we print "False"
        ]

        hash_segments = [
            ("{hash:7}", "", lambda s: s.dag_hash(7)),
            ("{/hash}", "/", lambda s: "/" + s.dag_hash()),
        ]

        variants_segments = [
            ("{variants.debug}", spec, "debug"),
            ("{variants.foo}", spec, "foo"),
            ("{^pkg-a.variants.bvv}", spec["pkg-a"], "bvv"),
            ("{^pkg-a.variants.foo}", spec["pkg-a"], "foo"),
        ]

        other_segments = [
            ("{spack_root}", spack.paths.spack_root),
            ("{spack_install}", spack.store.STORE.layout.root),
        ]

        def depify(depname, fmt_str, sigil):
            sig = len(sigil)
            opening = fmt_str[: 1 + sig]
            closing = fmt_str[1 + sig :]
            return spec[depname], opening + f"^{depname}." + closing

        def check_prop(check_spec, fmt_str, prop, getter):
            actual = spec.format(fmt_str)
            expected = getter(check_spec)
            assert actual == str(expected).strip()

        for named_str, sigil, prop, get_component in package_segments:
            getter = lambda s: sigil + str(getattr(get_component(s), prop, ""))
            check_prop(spec, named_str, prop, getter)
            mpi, fmt_str = depify("mpi", named_str, sigil)
            check_prop(mpi, fmt_str, prop, getter)

        for named_str, sigil, getter in hash_segments:
            assert spec.format(named_str) == getter(spec)
            callpath, fmt_str = depify("callpath", named_str, sigil)
            assert spec.format(fmt_str) == getter(callpath)

        for named_str, test_spec, variant_name in variants_segments:
            assert test_spec.format(named_str) == str(test_spec.variants[variant_name])
            assert test_spec.format(named_str[:-1] + ".value}") == str(
                test_spec.variants[variant_name].value
            )

        for named_str, expected in other_segments:
            actual = spec.format(named_str)
            assert expected == actual

    @pytest.mark.parametrize(
        "fmt_str",
        [
            "{name}",
            "{version}",
            "{@version}",
            "{namespace}",
            "{ namespace=namespace}",
            "{ namespace =namespace}",
            "{ name space =namespace}",
            "{arch}",
            "{architecture}",
            "{arch=architecture}",
            "{  arch=architecture}",
            "{  arch =architecture}",
        ],
    )
    def test_spec_format_null_attributes(self, fmt_str):
        """Ensure that attributes format to empty strings when their values are null."""
        spec = spack.spec.Spec()
        assert spec.format(fmt_str) == ""

    def test_spec_formatting_spaces_in_key(self, default_mock_concretization):
        spec = default_mock_concretization("multivalue-variant cflags=-O2")

        # test that spaces are preserved, if they come after some other text, otherwise
        # they are trimmed.
        # TODO: should we be trimming whitespace from formats? Probably not.
        assert spec.format("x{ arch=architecture}") == f"x arch={spec.architecture}"
        assert spec.format("x{ namespace=namespace}") == f"x namespace={spec.namespace}"
        assert spec.format("x{ name space =namespace}") == f"x name space ={spec.namespace}"
        assert spec.format("x{ os =os}") == f"x os ={spec.os}"

    @pytest.mark.parametrize(
        "fmt_str", ["{@name}", "{@version.concrete}", "{%compiler.version}", "{/hashd}"]
    )
    def test_spec_formatting_sigil_mismatches(self, default_mock_concretization, fmt_str):
        spec = default_mock_concretization("multivalue-variant cflags=-O2")

        with pytest.raises(SpecFormatSigilError):
            spec.format(fmt_str)

    @pytest.mark.parametrize(
        "fmt_str",
        [
            r"{}",
            r"name}",
            r"\{name}",
            r"{name",
            r"{name\}",
            r"{_concrete}",
            r"{dag_hash}",
            r"{foo}",
            r"{+variants.debug}",
            r"{variants.this_variant_does_not_exist}",
        ],
    )
    def test_spec_formatting_bad_formats(self, default_mock_concretization, fmt_str):
        spec = default_mock_concretization("multivalue-variant cflags=-O2")
        with pytest.raises(SpecFormatStringError):
            spec.format(fmt_str)

    def test_wildcard_is_invalid_variant_value(self):
        """The spec string x=* is parsed as a multi-valued variant with values the empty set.
        That excludes * as a literal variant value."""
        with pytest.raises(spack.spec_parser.SpecParsingError, match="cannot use reserved value"):
            Spec("multivalue-variant foo=*,bar")

    def test_errors_in_variant_directive(self):
        variant = spack.directives.variant.__wrapped__

        class Pkg:
            name = "PKG"

        # We can't use names that are reserved by Spack
        fn = variant("patches")
        with pytest.raises(spack.directives.DirectiveError) as exc_info:
            fn(Pkg())
        assert "The name 'patches' is reserved" in str(exc_info.value)

        # We can't have conflicting definitions for arguments
        fn = variant("foo", values=spack.variant.any_combination_of("fee", "foom"), default="bar")
        with pytest.raises(spack.directives.DirectiveError) as exc_info:
            fn(Pkg())
        assert " it is handled by an attribute of the 'values' " "argument" in str(exc_info.value)

        # We can't leave None as a default value
        fn = variant("foo", default=None)
        with pytest.raises(spack.directives.DirectiveError) as exc_info:
            fn(Pkg())
        assert "either a default was not explicitly set, or 'None' was used" in str(exc_info.value)

        # We can't use an empty string as a default value
        fn = variant("foo", default="")
        with pytest.raises(spack.directives.DirectiveError) as exc_info:
            fn(Pkg())
        assert "the default cannot be an empty string" in str(exc_info.value)

    def test_abstract_spec_prefix_error(self):
        spec = Spec("libelf")

        with pytest.raises(SpecError):
            spec.prefix

    def test_forwarding_of_architecture_attributes(self):
        spec = spack.concretize.concretize_one("libelf target=x86_64")

        # Check that we can still access each member through
        # the architecture attribute
        assert "test" in spec.architecture
        assert "debian" in spec.architecture
        assert "x86_64" in spec.architecture

        # Check that we forward the platform and os attribute correctly
        assert spec.platform == "test"
        assert spec.os == "debian6"

        # Check that the target is also forwarded correctly and supports
        # all the operators we expect
        assert spec.target == "x86_64"
        assert spec.target.family == "x86_64"
        assert "avx512" not in spec.target
        assert spec.target < "broadwell"

    @pytest.mark.parametrize("transitive", [True, False])
    def test_splice(self, transitive, default_mock_concretization):
        # Tests the new splice function in Spec using a somewhat simple case
        # with a variant with a conditional dependency.
        spec = default_mock_concretization("splice-t")
        dep = default_mock_concretization("splice-h+foo")

        # Sanity checking that these are not the same thing.
        assert dep.dag_hash() != spec["splice-h"].dag_hash()

        # Do the splice.
        out = spec.splice(dep, transitive)

        # Returned spec should still be concrete.
        assert out.concrete

        # Traverse the spec and assert that all dependencies are accounted for.
        for node in spec.traverse():
            assert node.name in out

        # If the splice worked, then the dag hash of the spliced dep should
        # now match the dag hash of the build spec of the dependency from the
        # returned spec.
        out_h_build = out["splice-h"].build_spec
        assert out_h_build.dag_hash() == dep.dag_hash()

        # Transitivity should determine whether the transitive dependency was
        # changed.
        expected_z = dep["splice-z"] if transitive else spec["splice-z"]
        assert out["splice-z"].dag_hash() == expected_z.dag_hash()

        # Sanity check build spec of out should be the original spec.
        assert out["splice-t"].build_spec.dag_hash() == spec["splice-t"].dag_hash()

        # Finally, the spec should know it's been spliced:
        assert out.spliced

    def test_splice_intransitive_complex(self, setup_complex_splice):
        a_red, c_blue = setup_complex_splice

        spliced = a_red.splice(c_blue, transitive=False)
        assert spliced.satisfies(
            "pkg-a color=red ^pkg-b color=red ^pkg-c color=blue "
            "^pkg-d color=red ^pkg-e color=red ^pkg-f color=blue ^pkg-g@2 color=red"
        )
        assert set(spliced.dependencies(deptype=dt.BUILD)) == set()
        assert spliced.build_spec == a_red

        # We cannot check spliced["b"].build_spec is spliced["b"] because Spec.__getitem__ creates
        # a new wrapper object on each invocation. So we select once and check on that object
        # For the rest of the unchanged specs we will just check the s._build_spec is None.
        b = spliced["pkg-b"]
        assert b == a_red["pkg-b"]
        assert b.build_spec is b
        assert set(b.dependents()) == {spliced}

        assert spliced["pkg-c"].satisfies(
            "pkg-c color=blue ^pkg-d color=red ^pkg-e color=red "
            "^pkg-f color=blue ^pkg-g@2 color=red"
        )
        assert set(spliced["pkg-c"].dependencies(deptype=dt.BUILD)) == set()
        assert spliced["pkg-c"].build_spec == c_blue
        assert set(spliced["pkg-c"].dependents()) == {spliced}

        assert spliced["pkg-d"] == a_red["pkg-d"]
        assert spliced["pkg-d"]._build_spec is None
        # Since D had a parent changed, it has a split edge for link vs build dependent
        # note: spliced["b"] == b_red, referenced differently to preserve logic
        assert set(spliced["pkg-d"].dependents()) == {
            spliced["pkg-b"],
            spliced["pkg-c"],
            a_red["pkg-c"],
        }
        assert set(spliced["pkg-d"].dependents(deptype=dt.BUILD)) == {
            a_red["pkg-b"],
            a_red["pkg-c"],
        }

        assert spliced["pkg-e"] == a_red["pkg-e"]
        assert spliced["pkg-e"]._build_spec is None
        # Because a copy of e is used, it does not have dependnets in the original specs
        assert set(spliced["pkg-e"].dependents()) == {spliced["pkg-b"], spliced["pkg-f"]}
        # Build dependent edge to f because f originally dependended on the e this was copied from
        assert set(spliced["pkg-e"].dependents(deptype=dt.BUILD)) == {spliced["pkg-b"]}

        assert spliced["pkg-f"].satisfies("pkg-f color=blue ^pkg-e color=red ^pkg-g@2 color=red")
        assert set(spliced["pkg-f"].dependencies(deptype=dt.BUILD)) == set()
        assert spliced["pkg-f"].build_spec == c_blue["pkg-f"]
        assert set(spliced["pkg-f"].dependents()) == {spliced["pkg-c"]}

        # spliced["pkg-g"] is g2, but spliced["pkg-b"]["pkg-g"] is g1
        assert spliced["pkg-g"] == a_red["pkg-g"]
        assert spliced["pkg-g"]._build_spec is None
        assert set(spliced["pkg-g"].dependents(deptype=dt.LINK)) == {
            spliced,
            spliced["pkg-c"],
            spliced["pkg-f"],
            a_red["pkg-c"],
        }

        assert spliced["pkg-b"]["pkg-g"] == a_red["pkg-b"]["pkg-g"]
        assert spliced["pkg-b"]["pkg-g"]._build_spec is None
        assert set(spliced["pkg-b"]["pkg-g"].dependents()) == {
            spliced["pkg-b"],
            spliced["pkg-d"],
            spliced["pkg-e"],
        }

        for edge in spliced.traverse_edges(cover="edges", deptype=dt.LINK | dt.RUN):
            # traverse_edges creates a synthetic edge with no deptypes to the root
            if edge.depflag:
                depflag = dt.LINK
                if not edge.parent.spliced:
                    depflag |= dt.BUILD
                assert edge.depflag == depflag

    def test_splice_transitive_complex(self, setup_complex_splice):
        a_red, c_blue = setup_complex_splice

        spliced = a_red.splice(c_blue, transitive=True)
        assert spliced.satisfies(
            "pkg-a color=red ^pkg-b color=red ^pkg-c color=blue ^pkg-d color=blue "
            "^pkg-e color=blue ^pkg-f color=blue ^pkg-g@3 color=blue"
        )
        assert set(spliced.dependencies(deptype=dt.BUILD)) == set()
        assert spliced.build_spec == a_red

        assert spliced["pkg-b"].satisfies(
            "pkg-b color=red ^pkg-d color=blue ^pkg-e color=blue ^pkg-g@2 color=blue"
        )
        assert set(spliced["pkg-b"].dependencies(deptype=dt.BUILD)) == set()
        assert spliced["pkg-b"].build_spec == a_red["pkg-b"]
        assert set(spliced["pkg-b"].dependents()) == {spliced}

        # We cannot check spliced["c"].build_spec is spliced["c"] because Spec.__getitem__ creates
        # a new wrapper object on each invocation. So we select once and check on that object
        # For the rest of the unchanged specs we will just check the s._build_spec is None.
        c = spliced["pkg-c"]
        assert c == c_blue
        assert c.build_spec is c
        assert set(c.dependents()) == {spliced}

        assert spliced["pkg-d"] == c_blue["pkg-d"]
        assert spliced["pkg-d"]._build_spec is None
        assert set(spliced["pkg-d"].dependents()) == {spliced["pkg-b"], spliced["pkg-c"]}

        assert spliced["pkg-e"] == c_blue["pkg-e"]
        assert spliced["pkg-e"]._build_spec is None
        assert set(spliced["pkg-e"].dependents()) == {spliced["pkg-b"], spliced["pkg-f"]}

        assert spliced["pkg-f"] == c_blue["pkg-f"]
        assert spliced["pkg-f"]._build_spec is None
        assert set(spliced["pkg-f"].dependents()) == {spliced["pkg-c"]}

        # spliced["g"] is g3, but spliced["d"]["g"] is g1
        assert spliced["pkg-g"] == c_blue["pkg-g"]
        assert spliced["pkg-g"]._build_spec is None
        assert set(spliced["pkg-g"].dependents(deptype=dt.LINK)) == {
            spliced,
            spliced["pkg-b"],
            spliced["pkg-c"],
            spliced["pkg-e"],
            spliced["pkg-f"],
        }
        # Because a copy of g3 is used, it does not have dependents in the original specs
        # It has build dependents on these spliced specs because it is an unchanged dependency
        # for them
        assert set(spliced["pkg-g"].dependents(deptype=dt.BUILD)) == {
            spliced["pkg-c"],
            spliced["pkg-e"],
            spliced["pkg-f"],
        }

        assert spliced["pkg-d"]["pkg-g"] == c_blue["pkg-d"]["pkg-g"]
        assert spliced["pkg-d"]["pkg-g"]._build_spec is None
        assert set(spliced["pkg-d"]["pkg-g"].dependents()) == {spliced["pkg-d"]}

        for edge in spliced.traverse_edges(cover="edges", deptype=dt.LINK | dt.RUN):
            # traverse_edges creates a synthetic edge with no deptypes to the root
            if edge.depflag:
                depflag = dt.LINK
                if not edge.parent.spliced:
                    depflag |= dt.BUILD
                assert edge.depflag == depflag

    @pytest.mark.parametrize("transitive", [True, False])
    def test_splice_with_cached_hashes(self, default_mock_concretization, transitive):
        spec = default_mock_concretization("splice-t")
        dep = default_mock_concretization("splice-h+foo")

        # monkeypatch hashes so we can test that they are cached
        spec._hash = "aaaaaa"
        dep._hash = "bbbbbb"
        spec["splice-h"]._hash = "cccccc"
        spec["splice-z"]._hash = "dddddd"
        dep["splice-z"]._hash = "eeeeee"

        out = spec.splice(dep, transitive=transitive)
        out_z_expected = (dep if transitive else spec)["splice-z"]

        assert out.dag_hash() != spec.dag_hash()
        assert (out["splice-h"].dag_hash() == dep.dag_hash()) == transitive
        assert out["splice-z"].dag_hash() == out_z_expected.dag_hash()

    @pytest.mark.parametrize("transitive", [True, False])
    def test_splice_input_unchanged(self, default_mock_concretization, transitive):
        spec = default_mock_concretization("splice-t")
        dep = default_mock_concretization("splice-h+foo")
        orig_spec_hash = spec.dag_hash()
        orig_dep_hash = dep.dag_hash()
        spec.splice(dep, transitive)
        # Post-splice, dag hash should still be different; no changes should be
        # made to these specs.
        assert spec.dag_hash() == orig_spec_hash
        assert dep.dag_hash() == orig_dep_hash

    @pytest.mark.parametrize("transitive", [True, False])
    def test_splice_subsequent(self, default_mock_concretization, transitive):
        spec = default_mock_concretization("splice-t")
        dep = default_mock_concretization("splice-h+foo")
        out = spec.splice(dep, transitive)

        # Now we attempt a second splice.
        dep = default_mock_concretization("splice-z+bar")

        # Transitivity shouldn't matter since Splice Z has no dependencies.
        out2 = out.splice(dep, transitive)
        assert out2.concrete
        assert out2["splice-z"].dag_hash() != spec["splice-z"].dag_hash()
        assert out2["splice-z"].dag_hash() != out["splice-z"].dag_hash()
        assert out2["splice-t"].build_spec.dag_hash() == spec["splice-t"].dag_hash()
        assert out2.spliced

    @pytest.mark.parametrize("transitive", [True, False])
    def test_splice_dict(self, default_mock_concretization, transitive):
        spec = default_mock_concretization("splice-t")
        dep = default_mock_concretization("splice-h+foo")
        out = spec.splice(dep, transitive)

        # Sanity check all hashes are unique...
        assert spec.dag_hash() != dep.dag_hash()
        assert out.dag_hash() != dep.dag_hash()
        assert out.dag_hash() != spec.dag_hash()
        node_list = out.to_dict()["spec"]["nodes"]
        root_nodes = [n for n in node_list if n["hash"] == out.dag_hash()]
        build_spec_nodes = [n for n in node_list if n["hash"] == spec.dag_hash()]
        assert spec.dag_hash() == out.build_spec.dag_hash()
        assert len(root_nodes) == 1
        assert len(build_spec_nodes) == 1

    @pytest.mark.parametrize("transitive", [True, False])
    def test_splice_dict_roundtrip(self, default_mock_concretization, transitive):
        spec = default_mock_concretization("splice-t")
        dep = default_mock_concretization("splice-h+foo")
        out = spec.splice(dep, transitive)

        # Sanity check all hashes are unique...
        assert spec.dag_hash() != dep.dag_hash()
        assert out.dag_hash() != dep.dag_hash()
        assert out.dag_hash() != spec.dag_hash()
        out_rt_spec = Spec.from_dict(out.to_dict())  # rt is "round trip"
        assert out_rt_spec.dag_hash() == out.dag_hash()
        out_rt_spec_bld_hash = out_rt_spec.build_spec.dag_hash()
        out_rt_spec_h_bld_hash = out_rt_spec["splice-h"].build_spec.dag_hash()
        out_rt_spec_z_bld_hash = out_rt_spec["splice-z"].build_spec.dag_hash()

        # In any case, the build spec for splice-t (root) should point to the
        # original spec, preserving build provenance.
        assert spec.dag_hash() == out_rt_spec_bld_hash
        assert out_rt_spec.dag_hash() != out_rt_spec_bld_hash

        # The build spec for splice-h should always point to the introduced
        # spec, since that is the spec spliced in.
        assert dep["splice-h"].dag_hash() == out_rt_spec_h_bld_hash

        # The build spec for splice-z will depend on whether or not the splice
        # was transitive.
        expected_z_bld_hash = (
            dep["splice-z"].dag_hash() if transitive else spec["splice-z"].dag_hash()
        )
        assert expected_z_bld_hash == out_rt_spec_z_bld_hash

    @pytest.mark.parametrize(
        "spec,constraint,expected_result",
        [
            ("libelf target=haswell", "target=broadwell", False),
            ("libelf target=haswell", "target=haswell", True),
            ("libelf target=haswell", "target=x86_64:", True),
            ("libelf target=haswell", "target=:haswell", True),
            ("libelf target=haswell", "target=icelake,:nocona", False),
            ("libelf target=haswell", "target=haswell,:nocona", True),
            # Check that a single target is not treated as the start
            # or the end of an open range
            ("libelf target=haswell", "target=x86_64", False),
            ("libelf target=x86_64", "target=haswell", False),
        ],
    )
    @pytest.mark.regression("13111")
    def test_target_constraints(self, spec, constraint, expected_result):
        s = Spec(spec)
        assert s.intersects(constraint) is expected_result

    @pytest.mark.regression("13124")
    def test_error_message_unknown_variant(self):
        s = Spec("mpileaks +unknown")
        with pytest.raises(UnknownVariantError):
            spack.concretize.concretize_one(s)

    @pytest.mark.regression("18527")
    def test_satisfies_dependencies_ordered(self):
        d = Spec("zmpi ^fake")
        s = Spec("mpileaks")
        s._add_dependency(d, depflag=0, virtuals=())
        assert s.satisfies("mpileaks ^zmpi ^fake")

    @pytest.mark.parametrize("transitive", [True, False])
    def test_splice_swap_names(self, default_mock_concretization, transitive):
        spec = default_mock_concretization("splice-vt")
        dep = default_mock_concretization("splice-a+foo")
        out = spec.splice(dep, transitive)
        assert dep.name in out
        assert transitive == ("+foo" in out["splice-z"])

    @pytest.mark.parametrize("transitive", [True, False])
    def test_splice_swap_names_mismatch_virtuals(self, default_mock_concretization, transitive):
        vt = default_mock_concretization("splice-vt")
        vh = default_mock_concretization("splice-vh+foo")
        with pytest.raises(spack.spec.SpliceError, match="virtual"):
            vt.splice(vh, transitive)

    def test_adaptor_optflags(self):
        """Tests that we can obtain the list of optflags, and debugflags,
        from the compiler adaptor, and that this list is taken from the
        appropriate compiler package.
        """
        # pkg-a depends on c, so only the gcc compiler should be chosen
        spec = spack.concretize.concretize_one(Spec("pkg-a %gcc"))
        assert "-Otestopt" in spec.package.compiler.opt_flags
        # This is not set, make sure we get an empty list
        for x in spec.package.compiler.debug_flags:
            pass

    def test_spec_override(self):
        init_spec = Spec("pkg-a foo=baz foobar=baz cflags=-O3 cxxflags=-O1")
        change_spec = Spec("pkg-a foo=fee cflags=-O2")
        new_spec = spack.concretize.concretize_one(Spec.override(init_spec, change_spec))
        assert "foo=fee" in new_spec
        # This check fails without concretizing: apparently if both specs are
        # abstract, then the spec will always be considered to satisfy
        # 'variant=value' (regardless of whether it in fact does).
        assert "foo=baz" not in new_spec
        assert "foobar=baz" in new_spec
        assert new_spec.compiler_flags["cflags"] == ["-O2"]
        assert new_spec.compiler_flags["cxxflags"] == ["-O1"]

    def test_spec_override_with_nonexisting_variant(self):
        init_spec = Spec("pkg-a foo=baz foobar=baz cflags=-O3 cxxflags=-O1")
        change_spec = Spec("pkg-a baz=fee")
        with pytest.raises(ValueError):
            Spec.override(init_spec, change_spec)

    def test_spec_override_with_variant_not_in_init_spec(self):
        init_spec = Spec("pkg-a foo=baz foobar=baz cflags=-O3 cxxflags=-O1")
        change_spec = Spec("pkg-a +bvv ~lorem_ipsum")
        new_spec = spack.concretize.concretize_one(Spec.override(init_spec, change_spec))
        assert "+bvv" in new_spec
        assert "~lorem_ipsum" in new_spec

    @pytest.mark.parametrize(
        "spec_str,specs_in_dag",
        [
            ("hdf5 ^[virtuals=mpi] mpich", [("mpich", "mpich"), ("mpi", "mpich")]),
            # Try different combinations with packages that provides a
            # disjoint set of virtual dependencies
            (
                "netlib-scalapack ^mpich ^openblas-with-lapack",
                [
                    ("mpi", "mpich"),
                    ("lapack", "openblas-with-lapack"),
                    ("blas", "openblas-with-lapack"),
                ],
            ),
            (
                "netlib-scalapack ^[virtuals=mpi] mpich ^openblas-with-lapack",
                [
                    ("mpi", "mpich"),
                    ("lapack", "openblas-with-lapack"),
                    ("blas", "openblas-with-lapack"),
                ],
            ),
            (
                "netlib-scalapack ^mpich ^[virtuals=lapack] openblas-with-lapack",
                [
                    ("mpi", "mpich"),
                    ("lapack", "openblas-with-lapack"),
                    ("blas", "openblas-with-lapack"),
                ],
            ),
            (
                "netlib-scalapack ^[virtuals=mpi] mpich ^[virtuals=lapack] openblas-with-lapack",
                [
                    ("mpi", "mpich"),
                    ("lapack", "openblas-with-lapack"),
                    ("blas", "openblas-with-lapack"),
                ],
            ),
            # Test that we can mix dependencies that provide an overlapping
            # sets of virtual dependencies
            (
                "netlib-scalapack ^[virtuals=mpi] intel-parallel-studio "
                "^[virtuals=lapack] openblas-with-lapack",
                [
                    ("mpi", "intel-parallel-studio"),
                    ("lapack", "openblas-with-lapack"),
                    ("blas", "openblas-with-lapack"),
                ],
            ),
            (
                "netlib-scalapack ^[virtuals=mpi] intel-parallel-studio ^openblas-with-lapack",
                [
                    ("mpi", "intel-parallel-studio"),
                    ("lapack", "openblas-with-lapack"),
                    ("blas", "openblas-with-lapack"),
                ],
            ),
            (
                "netlib-scalapack ^intel-parallel-studio ^[virtuals=lapack] openblas-with-lapack",
                [
                    ("mpi", "intel-parallel-studio"),
                    ("lapack", "openblas-with-lapack"),
                    ("blas", "openblas-with-lapack"),
                ],
            ),
            # Test that we can bind more than one virtual to the same provider
            (
                "netlib-scalapack ^[virtuals=lapack,blas] openblas-with-lapack",
                [("lapack", "openblas-with-lapack"), ("blas", "openblas-with-lapack")],
            ),
        ],
    )
    def test_virtual_deps_bindings(self, default_mock_concretization, spec_str, specs_in_dag):
        s = default_mock_concretization(spec_str)
        for label, expected in specs_in_dag:
            assert label in s
            assert s[label].satisfies(expected), label

    @pytest.mark.parametrize(
        "spec_str",
        [
            # openblas-with-lapack needs to provide blas and lapack together
            "netlib-scalapack ^[virtuals=blas] intel-parallel-studio ^openblas-with-lapack",
            # intel-* provides blas and lapack together, openblas can provide blas only
            "netlib-scalapack ^[virtuals=lapack] intel-parallel-studio ^openblas",
        ],
    )
    def test_unsatisfiable_virtual_deps_bindings(self, spec_str):
        with pytest.raises(spack.solver.asp.UnsatisfiableSpecError):
            spack.concretize.concretize_one(spec_str)

    @pytest.mark.parametrize(
        "spec_str,abstract_tests,concrete_tests",
        [
            # Ensure the 'when=+debug' is referred to 'callpath', and not to 'mpileaks',
            # and that we can concretize the spec despite 'callpath' has no debug variant
            (
                "mpileaks+debug ^callpath %[when=+debug virtuals=mpi] zmpi",
                [
                    ("^zmpi", False),
                    ("^mpich", False),
                    ("mpileaks+debug  %[when=+debug virtuals=mpi] zmpi", False),
                ],
                [("^zmpi", False), ("^[virtuals=mpi] mpich", True)],
            ),
            # Ensure we don't skip conditional edges when testing because we associate them
            # with the wrong node (e.g. mpileaks instead of mpich)
            (
                "mpileaks~debug ^mpich+debug %[when=+debug virtuals=c] llvm",
                [("^mpich+debug %[when=+debug virtuals=c] gcc", False)],
                [("^mpich %[virtuals=c] gcc", False), ("^mpich %[virtuals=c] llvm", True)],
            ),
        ],
    )
    def test_conditional_dependencies_satisfies(
        self, spec_str, abstract_tests, concrete_tests, default_mock_concretization
    ):
        """Tests satisfaction semantics for conditional specs, in different scenarios."""
        s = Spec(spec_str)
        for c, result in abstract_tests:
            assert s.satisfies(c) is result

        concrete = default_mock_concretization(spec_str)
        for c, result in concrete_tests:
            assert concrete.satisfies(c) is result


@pytest.mark.parametrize(
    "spec_str,format_str,expected",
    [
        ("git-test@git.foo/bar", "{name}-{version}", str(pathlib.Path("git-test-git.foo_bar"))),
        ("git-test@git.foo/bar", "{name}-{version}-{/hash}", None),
        ("git-test@git.foo/bar", "{name}/{version}", str(pathlib.Path("git-test", "git.foo_bar"))),
        # {compiler} is 'none' if a package does not depend on C, C++, or Fortran
        (
            f"git-test@{'a' * 40}=1.0%gcc",
            "{name}/{version}/{compiler}",
            str(pathlib.Path("git-test", f"{'a' * 40}_1.0", "none")),
        ),
        (
            "git-test@git.foo/bar=1.0%gcc",
            "{name}/{version}/{compiler}",
            str(pathlib.Path("git-test", "git.foo_bar_1.0", "none")),
        ),
    ],
)
def test_spec_format_path(spec_str, format_str, expected, mock_git_test_package):
    _check_spec_format_path(spec_str, format_str, expected)


def _check_spec_format_path(spec_str, format_str, expected, path_ctor=None):
    spec = Spec(spec_str)
    if not expected:
        with pytest.raises((spack.spec.SpecFormatPathError, spack.spec.SpecFormatStringError)):
            spec.format_path(format_str, _path_ctor=path_ctor)
    else:
        formatted = spec.format_path(format_str, _path_ctor=path_ctor)
        assert formatted == expected


@pytest.mark.parametrize(
    "spec_str,format_str,expected",
    [
        (
            "git-test@git.foo/bar",
            r"C:\\installroot\{name}\{version}",
            r"C:\installroot\git-test\git.foo_bar",
        ),
        (
            "git-test@git.foo/bar",
            r"\\hostname\sharename\{name}\{version}",
            r"\\hostname\sharename\git-test\git.foo_bar",
        ),
        # leading '/' is preserved on windows but converted to '\'
        # note that it's still not "absolute" -- absolute windows paths start with a drive.
        (
            "git-test@git.foo/bar",
            r"/installroot/{name}/{version}",
            r"\installroot\git-test\git.foo_bar",
        ),
    ],
)
def test_spec_format_path_windows(spec_str, format_str, expected, mock_git_test_package):
    _check_spec_format_path(spec_str, format_str, expected, path_ctor=pathlib.PureWindowsPath)


@pytest.mark.parametrize(
    "spec_str,format_str,expected",
    [
        (
            "git-test@git.foo/bar",
            r"/installroot/{name}/{version}",
            "/installroot/git-test/git.foo_bar",
        ),
        (
            "git-test@git.foo/bar",
            r"//installroot/{name}/{version}",
            "//installroot/git-test/git.foo_bar",
        ),
        # This is likely unintentional on Linux: Firstly, "\" is not a
        # path separator for POSIX, so this is treated as a single path
        # component (containing literal "\" characters); secondly,
        # Spec.format treats "\" as an escape character, so is
        # discarded (unless directly following another "\")
        (
            "git-test@git.foo/bar",
            r"C:\\installroot\package-{name}-{version}",
            r"C__installrootpackage-git-test-git.foo_bar",
        ),
        # "\" is not a POSIX separator, and Spec.format treats "\{" as a literal
        # "{", which means that the resulting format string is invalid
        ("git-test@git.foo/bar", r"package\{name}\{version}", None),
    ],
)
def test_spec_format_path_posix(spec_str, format_str, expected, mock_git_test_package):
    _check_spec_format_path(spec_str, format_str, expected, path_ctor=pathlib.PurePosixPath)


@pytest.mark.regression("3887")
@pytest.mark.parametrize("spec_str", ["py-extension2", "extension1", "perl-extension"])
def test_is_extension_after_round_trip_to_dict(config, mock_packages, spec_str):
    # x is constructed directly from string, y from a
    # round-trip to dict representation
    x = spack.concretize.concretize_one(spec_str)
    y = Spec.from_dict(x.to_dict())

    # Using 'y' since the round-trip make us lose build dependencies
    for d in y.traverse():
        assert x[d.name].package.is_extension == y[d.name].package.is_extension


def test_malformed_spec_dict():
    # FIXME: This test was really testing the specific implementation with an ad-hoc test
    with pytest.raises(SpecError, match="malformed"):
        Spec.from_dict(
            {"spec": {"_meta": {"version": 2}, "nodes": [{"dependencies": {"name": "foo"}}]}}
        )


def test_spec_dict_hashless_dep():
    # FIXME: This test was really testing the specific implementation with an ad-hoc test
    with pytest.raises(SpecError, match="Couldn't parse"):
        Spec.from_dict(
            {
                "spec": {
                    "_meta": {"version": 2},
                    "nodes": [
                        {"name": "foo", "hash": "thehash", "dependencies": [{"name": "bar"}]}
                    ],
                }
            }
        )


@pytest.mark.parametrize(
    "specs,expected",
    [
        # Anonymous specs without dependencies
        (["+baz", "+bar"], "+baz+bar"),
        (["@2.0:", "@:5.1", "+bar"], "@2.0:5.1 +bar"),
        # Anonymous specs with dependencies
        (["^mpich@3.2", "^mpich@:4.0+foo"], "^mpich@3.2 +foo"),
        # Mix a real package with a virtual one. This test
        # should fail if we start using the repository
        (["^mpich@3.2", "^mpi+foo"], "^mpich@3.2 ^mpi+foo"),
    ],
)
def test_merge_abstract_anonymous_specs(specs, expected):
    specs = [Spec(x) for x in specs]
    result = spack.spec.merge_abstract_anonymous_specs(*specs)
    assert result == Spec(expected)


@pytest.mark.parametrize(
    "anonymous,named,expected",
    [
        ("+plumed", "gromacs", "gromacs+plumed"),
        ("+plumed ^plumed%gcc", "gromacs", "gromacs+plumed ^plumed%gcc"),
        ("+plumed", "builtin.gromacs", "builtin.gromacs+plumed"),
    ],
)
def test_merge_anonymous_spec_with_named_spec(anonymous, named, expected):
    s = Spec(anonymous)
    changed = s.constrain(named)
    assert changed
    assert s == Spec(expected)


def test_spec_installed(default_mock_concretization, database):
    """Test whether Spec.installed works."""
    # a known installed spec should say that it's installed
    specs = database.query()
    spec = specs[0]
    assert spec.installed
    assert spec.copy().installed

    # an abstract spec should say it's not installed
    spec = Spec("not-a-real-package")
    assert not spec.installed

    # pkg-a is not in the mock DB and is not installed
    spec = default_mock_concretization("pkg-a")
    assert not spec.installed


@pytest.mark.regression("30678")
def test_call_dag_hash_on_old_dag_hash_spec(mock_packages, default_mock_concretization):
    # create a concrete spec
    a = default_mock_concretization("pkg-a")
    dag_hashes = {spec.name: spec.dag_hash() for spec in a.traverse()}

    # make it look like an old DAG hash spec with no package hash on the spec.
    for spec in a.traverse():
        assert spec.concrete
        spec._package_hash = None

    for spec in a.traverse():
        assert dag_hashes[spec.name] == spec.dag_hash()

        with pytest.raises(ValueError, match="Cannot call package_hash()"):
            spec.package_hash()


def test_spec_trim(mock_packages, config):
    top = spack.concretize.concretize_one("dt-diamond")
    top.trim("dt-diamond-left")
    remaining = {x.name for x in top.traverse()}
    assert {
        "compiler-wrapper",
        "dt-diamond",
        "dt-diamond-right",
        "dt-diamond-bottom",
        "gcc-runtime",
        "gcc",
    } == remaining

    top.trim("dt-diamond-right")
    remaining = {x.name for x in top.traverse()}
    assert {"compiler-wrapper", "dt-diamond", "gcc-runtime", "gcc"} == remaining


@pytest.mark.regression("30861")
def test_concretize_partial_old_dag_hash_spec(mock_packages, config):
    # create an "old" spec with no package hash
    bottom = spack.concretize.concretize_one("dt-diamond-bottom")
    delattr(bottom, "_package_hash")

    dummy_hash = "zd4m26eis2wwbvtyfiliar27wkcv3ehk"
    bottom._hash = dummy_hash

    # add it to an abstract spec as a dependency
    top = Spec("dt-diamond")
    top.add_dependency_edge(bottom, depflag=0, virtuals=())

    # concretize with the already-concrete dependency
    top = spack.concretize.concretize_one(top)

    for spec in top.traverse():
        assert spec.concrete

    # make sure dag_hash is untouched
    assert spec["dt-diamond-bottom"].dag_hash() == dummy_hash
    assert spec["dt-diamond-bottom"]._hash == dummy_hash

    # make sure package hash is NOT recomputed
    assert not getattr(spec["dt-diamond-bottom"], "_package_hash", None)


def test_package_hash_affects_dunder_and_dag_hash(mock_packages, default_mock_concretization):
    a1 = default_mock_concretization("pkg-a")
    a2 = default_mock_concretization("pkg-a")

    assert hash(a1) == hash(a2)
    assert a1.dag_hash() == a2.dag_hash()

    a1.clear_caches()
    a2.clear_caches()

    # tweak the dag hash of one of these specs
    new_hash = "00000000000000000000000000000000"
    if new_hash == a1._package_hash:
        new_hash = "11111111111111111111111111111111"
    a1._package_hash = new_hash

    assert hash(a1) != hash(a2)
    assert a1.dag_hash() != a2.dag_hash()


def test_intersects_and_satisfies_on_concretized_spec(default_mock_concretization):
    """Test that a spec obtained by concretizing an abstract spec, satisfies the abstract spec
    but not vice-versa.
    """
    a1 = default_mock_concretization("pkg-a@1.0")
    a2 = Spec("pkg-a@1.0")

    assert a1.intersects(a2)
    assert a2.intersects(a1)
    assert a1.satisfies(a2)
    assert not a2.satisfies(a1)


@pytest.mark.parametrize(
    "abstract_spec,spec_str",
    [
        ("v1-provider", "v1-consumer ^conditional-provider+disable-v1"),
        ("conditional-provider", "v1-consumer ^conditional-provider+disable-v1"),
        ("^v1-provider", "v1-consumer ^conditional-provider+disable-v1"),
        ("^conditional-provider", "v1-consumer ^conditional-provider+disable-v1"),
    ],
)
@pytest.mark.regression("35597")
def test_abstract_provider_in_spec(abstract_spec, spec_str, default_mock_concretization):
    s = default_mock_concretization(spec_str)
    assert abstract_spec in s


@pytest.mark.parametrize(
    "lhs,rhs,expected", [("a", "a", True), ("a", "a@1.0", True), ("a@1.0", "a", False)]
)
def test_abstract_contains_semantic(lhs, rhs, expected, mock_packages):
    s, t = Spec(lhs), Spec(rhs)
    result = s in t
    assert result is expected


@pytest.mark.parametrize(
    "factory,lhs_str,rhs_str,results",
    [
        # Architecture
        (ArchSpec, "None-ubuntu20.04-None", "None-None-x86_64", (True, False, False)),
        (ArchSpec, "None-ubuntu20.04-None", "linux-None-x86_64", (True, False, False)),
        (ArchSpec, "None-None-x86_64:", "linux-None-haswell", (True, False, True)),
        (ArchSpec, "None-None-x86_64:haswell", "linux-None-icelake", (False, False, False)),
        (ArchSpec, "linux-None-None", "linux-None-None", (True, True, True)),
        (ArchSpec, "darwin-None-None", "linux-None-None", (False, False, False)),
        (ArchSpec, "None-ubuntu20.04-None", "None-ubuntu20.04-None", (True, True, True)),
        (ArchSpec, "None-ubuntu20.04-None", "None-ubuntu22.04-None", (False, False, False)),
        # Compiler
        (Spec, "gcc", "clang", (False, False, False)),
        (Spec, "gcc", "gcc@5", (True, False, True)),
        (Spec, "gcc@5", "gcc@5.3", (True, False, True)),
        (Spec, "gcc@5", "gcc@5-tag", (True, False, True)),
        # Flags (flags are a map, so for convenience we initialize a full Spec)
        # Note: the semantic is that of sv variants, not mv variants
        (Spec, "cppflags=-foo", "cppflags=-bar", (True, False, False)),
        (Spec, "cppflags='-bar -foo'", "cppflags=-bar", (True, True, False)),
        (Spec, "cppflags=-foo", "cppflags=-foo", (True, True, True)),
        (Spec, "cppflags=-foo", "cflags=-foo", (True, False, False)),
        # Versions
        (Spec, "@0.94h", "@:0.94i", (True, True, False)),
        # Different virtuals intersect if there is at least package providing both
        (Spec, "mpi", "lapack", (True, False, False)),
        (Spec, "mpi", "pkgconfig", (False, False, False)),
        # Intersection among target ranges for different architectures
        (Spec, "target=x86_64:", "target=ppc64le:", (False, False, False)),
        (Spec, "target=x86_64:", "target=:power9", (False, False, False)),
        (Spec, "target=:haswell", "target=:power9", (False, False, False)),
        (Spec, "target=:haswell", "target=ppc64le:", (False, False, False)),
        # Intersection among target ranges for the same architecture
        (Spec, "target=:haswell", "target=x86_64:", (True, True, True)),
        (Spec, "target=:haswell", "target=x86_64_v4:", (False, False, False)),
        # Edge case of uarch that split in a diamond structure, from a common ancestor
        (Spec, "target=:cascadelake", "target=:cannonlake", (False, False, False)),
        # Spec with compilers
        (Spec, "mpileaks %gcc@5", "mpileaks %gcc@6", (False, False, False)),
        (Spec, "mpileaks ^callpath %gcc@5", "mpileaks ^callpath %gcc@6", (False, False, False)),
        (Spec, "mpileaks ^callpath %gcc@5", "mpileaks ^callpath %gcc@5.4", (True, False, True)),
    ],
)
def test_intersects_and_satisfies(mock_packages, factory, lhs_str, rhs_str, results):
    lhs = factory(lhs_str)
    rhs = factory(rhs_str)

    intersects, lhs_satisfies_rhs, rhs_satisfies_lhs = results

    assert lhs.intersects(rhs) is intersects
    assert rhs.intersects(lhs) is lhs.intersects(rhs)

    assert lhs.satisfies(rhs) is lhs_satisfies_rhs
    assert rhs.satisfies(lhs) is rhs_satisfies_lhs


@pytest.mark.parametrize(
    "factory,lhs_str,rhs_str,result,constrained_str",
    [
        # Architecture
        (ArchSpec, "None-ubuntu20.04-None", "None-None-x86_64", True, "None-ubuntu20.04-x86_64"),
        (ArchSpec, "None-None-x86_64", "None-None-x86_64", False, "None-None-x86_64"),
        (
            ArchSpec,
            "None-None-x86_64:icelake",
            "None-None-x86_64:icelake",
            False,
            "None-None-x86_64:icelake",
        ),
        (ArchSpec, "None-ubuntu20.04-None", "linux-None-x86_64", True, "linux-ubuntu20.04-x86_64"),
        (
            ArchSpec,
            "None-ubuntu20.04-nocona:haswell",
            "None-None-x86_64:icelake",
            False,
            "None-ubuntu20.04-nocona:haswell",
        ),
        (
            ArchSpec,
            "None-ubuntu20.04-nocona,haswell",
            "None-None-x86_64:icelake",
            False,
            "None-ubuntu20.04-nocona,haswell",
        ),
        # Compiler
        (Spec, "foo %gcc@5", "foo %gcc@5-tag", True, "foo %gcc@5-tag"),
        (Spec, "foo %gcc@5", "foo %gcc@5", False, "foo %gcc@5"),
        # Flags
        (Spec, "cppflags=-foo", "cppflags=-foo", False, "cppflags=-foo"),
        (Spec, "cppflags=-foo", "cflags=-foo", True, "cppflags=-foo cflags=-foo"),
        # Target ranges
        (Spec, "target=x86_64:", "target=x86_64:", False, "target=x86_64:"),
        (Spec, "target=x86_64:", "target=:haswell", True, "target=x86_64:haswell"),
        (
            Spec,
            "target=x86_64:haswell",
            "target=x86_64_v2:icelake",
            True,
            "target=x86_64_v2:haswell",
        ),
    ],
)
def test_constrain(factory, lhs_str, rhs_str, result, constrained_str):
    lhs = factory(lhs_str)
    rhs = factory(rhs_str)

    assert lhs.constrain(rhs) is result
    assert lhs == factory(constrained_str)

    # The intersection must be the same, so check that invariant too
    lhs = factory(lhs_str)
    rhs = factory(rhs_str)
    rhs.constrain(lhs)
    assert rhs == factory(constrained_str)


def test_abstract_hash_intersects_and_satisfies(default_mock_concretization):
    concrete: Spec = default_mock_concretization("pkg-a")
    hash = concrete.dag_hash()
    hash_5 = hash[:5]
    hash_6 = hash[:6]
    # abstract hash that doesn't have a common prefix with the others.
    hash_other = f"{'a' if hash_5[0] == 'b' else 'b'}{hash_5[1:]}"

    abstract_5 = Spec(f"pkg-a/{hash_5}")
    abstract_6 = Spec(f"pkg-a/{hash_6}")
    abstract_none = Spec(f"pkg-a/{hash_other}")
    abstract = Spec("pkg-a")

    def assert_subset(a: Spec, b: Spec):
        assert a.intersects(b) and b.intersects(a) and a.satisfies(b) and not b.satisfies(a)

    def assert_disjoint(a: Spec, b: Spec):
        assert (
            not a.intersects(b)
            and not b.intersects(a)
            and not a.satisfies(b)
            and not b.satisfies(a)
        )

    # left-hand side is more constrained, so its
    # concretization space is a subset of the right-hand side's
    assert_subset(concrete, abstract_5)
    assert_subset(abstract_6, abstract_5)
    assert_subset(abstract_5, abstract)

    # disjoint concretization space
    assert_disjoint(abstract_none, concrete)
    assert_disjoint(abstract_none, abstract_5)


def test_edge_equality_does_not_depend_on_virtual_order():
    """Tests that two edges that are constructed with just a different order of the virtuals in
    the input parameters are equal to each other.
    """
    parent, child = Spec("parent"), Spec("child")
    edge1 = DependencySpec(parent, child, depflag=0, virtuals=("mpi", "lapack"))
    edge2 = DependencySpec(parent, child, depflag=0, virtuals=("lapack", "mpi"))
    assert edge1 == edge2
    assert tuple(sorted(edge1.virtuals)) == edge1.virtuals
    assert tuple(sorted(edge2.virtuals)) == edge1.virtuals


def test_update_virtuals():
    parent, child = Spec("parent"), Spec("child")
    edge = DependencySpec(parent, child, depflag=0, virtuals=("mpi", "lapack"))
    assert edge.update_virtuals("blas")
    assert edge.virtuals == ("blas", "lapack", "mpi")
    assert edge.update_virtuals(("c", "fortran", "mpi", "lapack"))
    assert edge.virtuals == ("blas", "c", "fortran", "lapack", "mpi")
    assert not edge.update_virtuals("mpi")
    assert not edge.update_virtuals(("c", "fortran", "mpi", "lapack"))
    assert edge.virtuals == ("blas", "c", "fortran", "lapack", "mpi")


def test_virtual_queries_work_for_strings_and_lists():
    """Ensure that ``dependencies()`` works with both virtuals=str and virtuals=[str, ...]."""
    parent, child = Spec("parent"), Spec("child")
    parent._add_dependency(
        child, depflag=dt.BUILD, virtuals=("cxx", "fortran")  # multi-char dep names
    )

    assert not parent.dependencies(virtuals="c")  # not in virtuals but shares a char with cxx

    for lang in ["cxx", "fortran"]:
        assert parent.dependencies(virtuals=lang)  # string arg
        assert parent.edges_to_dependencies(virtuals=lang)  # string arg

        assert parent.dependencies(virtuals=[lang])  # list arg
        assert parent.edges_to_dependencies(virtuals=[lang])  # string arg


def test_old_format_strings_trigger_error(default_mock_concretization):
    s = spack.concretize.concretize_one("pkg-a")
    with pytest.raises(SpecFormatStringError):
        s.format("${PACKAGE}-${VERSION}-${HASH}")


@pytest.mark.regression("47362")
@pytest.mark.parametrize(
    "lhs,rhs",
    [
        ("hdf5 +mpi", "hdf5++mpi"),
        ("hdf5 cflags==-g", "hdf5 cflags=-g"),
        ("hdf5 +mpi ++shared", "hdf5+mpi +shared"),
        ("hdf5 +mpi cflags==-g", "hdf5++mpi cflag=-g"),
    ],
)
def test_equality_discriminate_on_propagation(lhs, rhs):
    """Tests that == can discriminate abstract specs based on their 'propagation' status"""
    s, t = Spec(lhs), Spec(rhs)
    assert s != t
    assert len({s, t}) == 2


def test_comparison_multivalued_variants():
    assert Spec("x=a") < Spec("x=a,b") < Spec("x==a,b") < Spec("x==a,b,c")


@pytest.mark.parametrize(
    "specs_in_expected_order",
    [
        ("a", "b", "c", "d", "e"),
        ("a@1.0", "a@2.0", "b", "c@3.0", "c@4.0"),
        ("a^d", "b^c", "c^b", "d^a"),
        ("e^a", "e^b", "e^c", "e^d"),
        ("e^a@1.0", "e^a@2.0", "e^a@3.0", "e^a@4.0"),
        ("e^a@1.0 +a", "e^a@1.0 +b", "e^a@1.0 +c", "e^a@1.0 +c"),
        ("a^b%c", "a^b%d", "a^b%e", "a^b%f"),
        ("a^b%c@1.0", "a^b%c@2.0", "a^b%c@3.0", "a^b%c@4.0"),
        ("a^b%c@1.0 +a", "a^b%c@1.0 +b", "a^b%c@1.0 +c", "a^b%c@1.0 +d"),
        ("a cflags=-O1", "a cflags=-O2", "a cflags=-O3"),
        ("a %cmake@1.0 ^b %cmake@2.0", "a %cmake@2.0 ^b %cmake@1.0"),
        ("a^b^c^d", "a^b^c^e", "a^b^c^f"),
        ("a^b^c^d", "a^b^c^e", "a^b^c^e", "a^b^c^f"),
        ("a%b%c%d", "a%b%c%e", "a%b%c%e", "a%b%c%f"),
        ("d.a", "c.b", "b.c", "a.d"),  # names before namespaces
    ],
)
def test_spec_ordering(specs_in_expected_order):
    specs_in_expected_order = [Spec(s) for s in specs_in_expected_order]
    assert sorted(specs_in_expected_order) == specs_in_expected_order
    assert sorted(reversed(specs_in_expected_order)) == specs_in_expected_order

    for i in range(len(specs_in_expected_order) - 1):
        lhs, rhs = specs_in_expected_order[i : i + 2]
        assert lhs <= rhs
        assert (lhs < rhs and lhs != rhs) or lhs == rhs
        assert rhs >= lhs
        assert (rhs > lhs and rhs != lhs) or rhs == lhs


EMPTY_VER = vn.VersionList(":")
EMPTY_VAR = Spec().variants
EMPTY_FLG = Spec().compiler_flags


@pytest.mark.parametrize(
    "spec,expected_tuplified",
    [
        # simple, no dependencies
        [("a"), ((("a", None, EMPTY_VER, EMPTY_VAR, EMPTY_FLG, None, None, None),), ())],
        # with some node attributes
        [
            ("a@1.0 +foo cflags='-O3 -g'"),
            (
                (
                    (
                        "a",
                        None,
                        vn.VersionList(["1.0"]),
                        Spec("+foo").variants,
                        Spec("cflags='-O3 -g'").compiler_flags,
                        None,
                        None,
                        None,
                    ),
                ),
                (),
            ),
        ],
        # single edge case
        [
            ("a^b"),
            (
                (
                    ("a", None, EMPTY_VER, EMPTY_VAR, EMPTY_FLG, None, None, None),
                    ("b", None, EMPTY_VER, EMPTY_VAR, EMPTY_FLG, None, None, None),
                ),
                ((0, 1, 0, (), False, Spec()),),
            ),
        ],
        # root with multiple deps
        [
            ("a^b^c^d"),
            (
                (
                    ("a", None, EMPTY_VER, EMPTY_VAR, EMPTY_FLG, None, None, None),
                    ("b", None, EMPTY_VER, EMPTY_VAR, EMPTY_FLG, None, None, None),
                    ("c", None, EMPTY_VER, EMPTY_VAR, EMPTY_FLG, None, None, None),
                    ("d", None, EMPTY_VER, EMPTY_VAR, EMPTY_FLG, None, None, None),
                ),
                (
                    (0, 1, 0, (), False, Spec()),
                    (0, 2, 0, (), False, Spec()),
                    (0, 3, 0, (), False, Spec()),
                ),
            ),
        ],
        # root with multiple build deps
        [
            ("a%b%c%d"),
            (
                (
                    ("a", None, EMPTY_VER, EMPTY_VAR, EMPTY_FLG, None, None, None),
                    ("b", None, EMPTY_VER, EMPTY_VAR, EMPTY_FLG, None, None, None),
                    ("c", None, EMPTY_VER, EMPTY_VAR, EMPTY_FLG, None, None, None),
                    ("d", None, EMPTY_VER, EMPTY_VAR, EMPTY_FLG, None, None, None),
                ),
                (
                    (0, 1, 0, (), True, Spec()),
                    (0, 2, 0, (), True, Spec()),
                    (0, 3, 0, (), True, Spec()),
                ),
            ),
        ],
        # dependencies with dependencies
        [
            ("a  ^b %c %d  ^e %f %g"),
            (
                (
                    ("a", None, EMPTY_VER, EMPTY_VAR, EMPTY_FLG, None, None, None),
                    ("b", None, EMPTY_VER, EMPTY_VAR, EMPTY_FLG, None, None, None),
                    ("e", None, EMPTY_VER, EMPTY_VAR, EMPTY_FLG, None, None, None),
                    ("c", None, EMPTY_VER, EMPTY_VAR, EMPTY_FLG, None, None, None),
                    ("d", None, EMPTY_VER, EMPTY_VAR, EMPTY_FLG, None, None, None),
                    ("f", None, EMPTY_VER, EMPTY_VAR, EMPTY_FLG, None, None, None),
                    ("g", None, EMPTY_VER, EMPTY_VAR, EMPTY_FLG, None, None, None),
                ),
                (
                    (0, 1, 0, (), False, Spec()),
                    (0, 2, 0, (), False, Spec()),
                    (1, 3, 0, (), True, Spec()),
                    (1, 4, 0, (), True, Spec()),
                    (2, 5, 0, (), True, Spec()),
                    (2, 6, 0, (), True, Spec()),
                ),
            ),
        ],
    ],
)
def test_spec_canonical_comparison_form(spec, expected_tuplified):
    """Tests a few expected canonical comparison form of specs"""
    assert llnl.util.lang.tuplify(Spec(spec)._cmp_iter) == expected_tuplified


def test_comparison_after_breaking_hash_change():
    # We simulate a breaking change in DAG hash computation in Spack. We have two specs that are
    # entirely equal modulo DAG hash. When deserializing these specs, we don't want them to compare
    # as equal, because DAG hash is used throughout in Spack to distinguish between specs
    # (e.g. database, build caches, install dir).
    s = Spec("example@=1.0")
    s._mark_concrete(True)

    # compute the dag hash and a change to it
    dag_hash = s.dag_hash()
    new_dag_hash = f"{'b' if dag_hash[0] == 'a' else 'a'}{dag_hash[1:]}"

    before_breakage = s.to_dict()
    after_breakage = s.to_dict()
    after_breakage["spec"]["nodes"][0]["hash"] = new_dag_hash
    assert before_breakage != after_breakage

    x = Spec.from_dict(before_breakage)
    y = Spec.from_dict(after_breakage)
    assert x != y
    assert len({x, y}) == 2


def test_satisfies_and_subscript_with_compilers(default_mock_concretization):
    """Tests the semantic of "satisfies" and __getitem__ for the following spec:

    [    ]  multivalue-variant@2.3
    [bl  ]      ^callpath@1.0
    [bl  ]          ^dyninst@8.2
    [bl  ]              ^libdwarf@20130729
    [bl  ]              ^libelf@0.8.13
    [b   ]      ^gcc@10.2.1
    [ l  ]      ^gcc-runtime@10.2.1
    [bl  ]      ^mpich@3.0.4
    [bl  ]      ^pkg-a@2.0
    [b   ]          ^gmake@4.4
    [bl  ]          ^pkg-b@1.0
    """
    s = default_mock_concretization("multivalue-variant")

    # Check a direct build/link dependency
    assert s.satisfies("^pkg-a")
    assert s.dependencies(name="pkg-a")[0] == s["pkg-a"]

    # Transitive build/link dependency
    assert s.satisfies("^libelf")
    assert s["libdwarf"].dependencies(name="libelf")[0] == s["libelf"]

    # Direct build dependencies
    assert s.satisfies("^[virtuals=c] gcc")
    assert s.satisfies("%[virtuals=c] gcc")
    assert s.dependencies(name="gcc")[0] == s["gcc"]
    assert s.dependencies(name="gcc")[0] == s["c"]

    # Transitive build dependencies
    assert not s.satisfies("^gmake")

    # "gmake" is not in the link/run subdag + direct build deps
    with pytest.raises(KeyError):
        _ = s["gmake"]

    # We need to pass through "pkg-a" to get "gmake" with [] notation
    assert s["pkg-a"].dependencies(name="gmake")[0] == s["pkg-a"]["gmake"]


@pytest.mark.parametrize(
    "spec_str,spec_fmt,expected",
    [
        # Depends on C
        ("mpileaks", "{name}-{compiler.name}", "mpileaks-gcc"),
        ("mpileaks", "{name}-{compiler.name}-{compiler.version}", "mpileaks-gcc-10.2.1"),
        # No compiler
        ("pkg-c", "{name}-{compiler.name}", "pkg-c-none"),
        ("pkg-c", "{name}-{compiler.name}-{compiler.version}", "pkg-c-none-none"),
    ],
)
def test_spec_format_with_compiler_adaptors(
    spec_str, spec_fmt, expected, default_mock_concretization
):
    """Tests the output of spec format, when involving `Spec.compiler` adaptors"""
    s = default_mock_concretization(spec_str)
    assert s.format(spec_fmt) == expected


@pytest.mark.parametrize(
    "lhs,rhs,expected",
    [
        ("mpich %gcc", "mpich %gcc", True),
        ("mpich %gcc", "mpich ^gcc", False),
        ("mpich ^callpath %gcc", "mpich %gcc ^callpath", False),
    ],
)
def test_specs_equality(lhs, rhs, expected):
    """Tests the semantic of == for abstract specs"""
    lhs, rhs = Spec(lhs), Spec(rhs)
    assert (lhs == rhs) is expected


def test_edge_equality_accounts_for_when_condition():
    """Tests that edges can be distinguished by their 'when' condition."""
    parent, child = Spec("parent"), Spec("child")
    edge1 = DependencySpec(parent, child, depflag=0, virtuals=(), when=Spec("%c"))
    edge2 = DependencySpec(parent, child, depflag=0, virtuals=())
    assert edge1 != edge2
