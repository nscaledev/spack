# Copyright Spack Project Developers. See COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

"""Test YAML and JSON serialization for specs.

The YAML and JSON formats preserve DAG information in the spec.

"""
import collections
import collections.abc
import gzip
import io
import json
import os
import pickle

import _vendoring.ruamel.yaml
import pytest

import spack.concretize
import spack.config
import spack.hash_types as ht
import spack.paths
import spack.repo
import spack.spec
import spack.util.spack_json as sjson
import spack.util.spack_yaml as syaml
from spack.spec import Spec, save_dependency_specfiles
from spack.util.spack_yaml import SpackYAMLError, syaml_dict


def check_yaml_round_trip(spec):
    yaml_text = spec.to_yaml()
    spec_from_yaml = Spec.from_yaml(yaml_text)
    assert spec.eq_dag(spec_from_yaml)


def check_json_round_trip(spec):
    json_text = spec.to_json()
    spec_from_json = Spec.from_json(json_text)
    assert spec.eq_dag(spec_from_json)


def test_read_spec_from_signed_json():
    spec_dir = os.path.join(spack.paths.test_path, "data", "mirrors", "signed_json")
    file_name = (
        "linux-ubuntu18.04-haswell-gcc-8.4.0-"
        "zlib-1.2.12-g7otk5dra3hifqxej36m5qzm7uyghqgb.spec.json.sig"
    )
    spec_path = os.path.join(spec_dir, file_name)

    def check_spec(spec_to_check):
        assert spec_to_check.name == "zlib"
        assert spec_to_check._hash == "g7otk5dra3hifqxej36m5qzm7uyghqgb"

    with open(spec_path, encoding="utf-8") as fd:
        s = Spec.from_signed_json(fd)
        check_spec(s)

    with open(spec_path, encoding="utf-8") as fd:
        s = Spec.from_signed_json(fd.read())
        check_spec(s)


@pytest.mark.parametrize(
    "invalid_yaml", ["playing_playlist: {{ action }} playlist {{ playlist_name }}"]
)
def test_invalid_yaml_spec(invalid_yaml):
    with pytest.raises(SpackYAMLError, match="error parsing YAML") as e:
        Spec.from_yaml(invalid_yaml)
    assert invalid_yaml in str(e)


@pytest.mark.parametrize("invalid_json, error_message", [("{13:", "Expecting property name")])
def test_invalid_json_spec(invalid_json, error_message):
    with pytest.raises(sjson.SpackJSONError) as e:
        Spec.from_json(invalid_json)
    exc_msg = str(e.value)
    assert exc_msg.startswith("error parsing JSON spec:")
    assert error_message in exc_msg


@pytest.mark.parametrize(
    "abstract_spec",
    [
        # Externals
        "externaltool",
        "externaltest",
        # Ambiguous version spec
        "mpileaks@1.0:5.0,6.1,7.3+debug~opt",
        # Variants
        "mpileaks+debug~opt",
        'multivalue-variant foo="bar,baz"',
        # Virtuals on edges
        "callpath",
        "mpileaks",
    ],
)
def test_roundtrip_concrete_specs(abstract_spec, default_mock_concretization):
    check_yaml_round_trip(Spec(abstract_spec))
    check_json_round_trip(Spec(abstract_spec))
    concrete_spec = default_mock_concretization(abstract_spec)
    check_yaml_round_trip(concrete_spec)
    check_json_round_trip(concrete_spec)


def test_yaml_subdag(config, mock_packages):
    spec = spack.concretize.concretize_one("mpileaks^mpich+debug")
    yaml_spec = Spec.from_yaml(spec.to_yaml())
    json_spec = Spec.from_json(spec.to_json())

    for dep in ("callpath", "mpich", "dyninst", "libdwarf", "libelf"):
        assert spec[dep].eq_dag(yaml_spec[dep])
        assert spec[dep].eq_dag(json_spec[dep])


@pytest.mark.parametrize("spec_str", ["mpileaks ^zmpi", "dttop", "dtuse"])
def test_using_ordered_dict(default_mock_concretization, spec_str):
    """Checks that we use syaml_dicts for spec serialization.

    Necessary to make sure that dag_hash is stable across python
    versions and processes.
    """

    def descend_and_check(iterable, level=0):
        if isinstance(iterable, collections.abc.Mapping):
            assert type(iterable) in (syaml_dict, dict)
            return descend_and_check(iterable.values(), level=level + 1)
        max_level = level
        for value in iterable:
            if isinstance(value, collections.abc.Iterable) and not isinstance(value, str):
                nlevel = descend_and_check(value, level=level + 1)
                if nlevel > max_level:
                    max_level = nlevel
        return max_level

    s = default_mock_concretization(spec_str)
    level = descend_and_check(s.to_node_dict())
    # level just makes sure we are doing something here
    assert level >= 5


@pytest.mark.parametrize("spec_str", ["mpileaks ^zmpi", "dttop", "dtuse"])
def test_ordered_read_not_required_for_consistent_dag_hash(
    spec_str, mutable_config: spack.config.Configuration, mock_packages
):
    """Make sure ordered serialization isn't required to preserve hashes.

    For consistent hashes, we require that YAML and JSON serializations have their keys in a
    deterministic order. However, we don't want to require them to be serialized in order. This
    ensures that is not required."""

    # Make sure that `extra_attributes` of externals is order independent for hashing.
    extra_attributes = {
        "compilers": {"c": "/some/path/bin/cc", "cxx": "/some/path/bin/c++"},
        "foo": "bar",
        "baz": "qux",
    }
    mutable_config.set(
        "packages:dtuse",
        {
            "buildable": False,
            "externals": [
                {"spec": "dtuse@=1.0", "prefix": "/usr", "extra_attributes": extra_attributes}
            ],
        },
    )

    spec = spack.concretize.concretize_one(spec_str)

    if spec_str == "dtuse":
        assert spec.external and spec.extra_attributes == extra_attributes

    spec_dict = spec.to_dict(hash=ht.dag_hash)
    spec_yaml = spec.to_yaml()
    spec_json = spec.to_json()

    # Make a spec with dict keys reversed recursively
    spec_dict_rev = reverse_all_dicts(spec_dict)

    # Dump to YAML and JSON
    yaml_string = syaml.dump(spec_dict, default_flow_style=False)
    yaml_string_rev = syaml.dump(spec_dict_rev, default_flow_style=False)
    json_string = sjson.dump(spec_dict)
    json_string_rev = sjson.dump(spec_dict_rev)

    # spec yaml is ordered like the spec dict
    assert yaml_string == spec_yaml
    assert json_string == spec_json

    # reversed string is different from the original, so it *would* generate a different hash
    assert yaml_string != yaml_string_rev
    assert json_string != json_string_rev

    # build specs from the "wrongly" ordered data
    from_yaml = Spec.from_yaml(yaml_string)
    from_json = Spec.from_json(json_string)
    from_yaml_rev = Spec.from_yaml(yaml_string_rev)
    from_json_rev = Spec.from_json(json_string_rev)

    # Strip spec if we stripped the yaml
    spec = spec.copy(deps=ht.dag_hash.depflag)

    # specs and their hashes are equal to the original
    assert (
        spec.dag_hash()
        == from_yaml.dag_hash()
        == from_json.dag_hash()
        == from_yaml_rev.dag_hash()
        == from_json_rev.dag_hash()
    )
    assert spec == from_yaml == from_json == from_yaml_rev == from_json_rev


def reverse_all_dicts(data):
    """Descend into data and reverse all the dictionaries"""
    if isinstance(data, dict):
        return type(data)((k, reverse_all_dicts(v)) for k, v in reversed(list(data.items())))
    elif isinstance(data, (list, tuple)):
        return type(data)(reverse_all_dicts(elt) for elt in data)
    return data


def check_specs_equal(original_spec, spec_yaml_path):
    with open(spec_yaml_path, "r", encoding="utf-8") as fd:
        spec_yaml = fd.read()
        spec_from_yaml = Spec.from_yaml(spec_yaml)
        return original_spec.eq_dag(spec_from_yaml)


def test_save_dependency_spec_jsons_subset(tmpdir, config):
    output_path = str(tmpdir.mkdir("spec_jsons"))

    builder = spack.repo.MockRepositoryBuilder(tmpdir.mkdir("mock-repo"))
    builder.add_package("pkg-g")
    builder.add_package("pkg-f")
    builder.add_package("pkg-e")
    builder.add_package("pkg-d", dependencies=[("pkg-f", None, None), ("pkg-g", None, None)])
    builder.add_package("pkg-c")
    builder.add_package("pkg-b", dependencies=[("pkg-d", None, None), ("pkg-e", None, None)])
    builder.add_package("pkg-a", dependencies=[("pkg-b", None, None), ("pkg-c", None, None)])

    with spack.repo.use_repositories(builder.root):
        spec_a = spack.concretize.concretize_one("pkg-a")
        b_spec = spec_a["pkg-b"]
        c_spec = spec_a["pkg-c"]

        save_dependency_specfiles(spec_a, output_path, [Spec("pkg-b"), Spec("pkg-c")])

        assert check_specs_equal(b_spec, os.path.join(output_path, "pkg-b.json"))
        assert check_specs_equal(c_spec, os.path.join(output_path, "pkg-c.json"))


def test_legacy_yaml(tmpdir, install_mockery, mock_packages):
    """Tests a simple legacy YAML with a dependency and ensures spec survives
    concretization."""
    yaml = """
spec:
- a:
    version: '2.0'
    arch:
      platform: linux
      platform_os: rhel7
      target: x86_64
    compiler:
      name: gcc
      version: 8.3.0
    namespace: builtin.mock
    parameters:
      bvv: true
      foo:
      - bar
      foobar: bar
      cflags: []
      cppflags: []
      cxxflags: []
      fflags: []
      ldflags: []
      ldlibs: []
    dependencies:
      b:
        hash: iaapywazxgetn6gfv2cfba353qzzqvhn
        type:
        - build
        - link
    hash: obokmcsn3hljztrmctbscmqjs3xclazz
    full_hash: avrk2tqsnzxeabmxa6r776uq7qbpeufv
    build_hash: obokmcsn3hljztrmctbscmqjs3xclazy
- b:
    version: '1.0'
    arch:
      platform: linux
      platform_os: rhel7
      target: x86_64
    compiler:
      name: gcc
      version: 8.3.0
    namespace: builtin.mock
    parameters:
      cflags: []
      cppflags: []
      cxxflags: []
      fflags: []
      ldflags: []
      ldlibs: []
    hash: iaapywazxgetn6gfv2cfba353qzzqvhn
    full_hash: qvsxvlmjaothtpjluqijv7qfnni3kyyg
    build_hash: iaapywazxgetn6gfv2cfba353qzzqvhy
"""
    spec = Spec.from_yaml(yaml)
    concrete_spec = spack.concretize.concretize_one(spec)
    assert concrete_spec.eq_dag(spec)


#: A well ordered Spec dictionary, using ``OrderdDict``.
#: Any operation that transforms Spec dictionaries should
#: preserve this order.
ordered_spec = collections.OrderedDict(
    [
        (
            "arch",
            collections.OrderedDict(
                [
                    ("platform", "darwin"),
                    ("platform_os", "bigsur"),
                    (
                        "target",
                        collections.OrderedDict(
                            [
                                (
                                    "features",
                                    [
                                        "adx",
                                        "aes",
                                        "avx",
                                        "avx2",
                                        "bmi1",
                                        "bmi2",
                                        "clflushopt",
                                        "f16c",
                                        "fma",
                                        "mmx",
                                        "movbe",
                                        "pclmulqdq",
                                        "popcnt",
                                        "rdrand",
                                        "rdseed",
                                        "sse",
                                        "sse2",
                                        "sse4_1",
                                        "sse4_2",
                                        "ssse3",
                                        "xsavec",
                                        "xsaveopt",
                                    ],
                                ),
                                ("generation", 0),
                                ("name", "skylake"),
                                ("parents", ["broadwell"]),
                                ("vendor", "GenuineIntel"),
                            ]
                        ),
                    ),
                ]
            ),
        ),
        ("compiler", collections.OrderedDict([("name", "apple-clang"), ("version", "13.0.0")])),
        ("name", "zlib"),
        ("namespace", "builtin"),
        (
            "parameters",
            collections.OrderedDict(
                [
                    ("cflags", []),
                    ("cppflags", []),
                    ("cxxflags", []),
                    ("fflags", []),
                    ("ldflags", []),
                    ("ldlibs", []),
                    ("optimize", True),
                    ("pic", True),
                    ("shared", True),
                ]
            ),
        ),
        ("version", "1.2.11"),
    ]
)


@pytest.mark.parametrize(
    "specfile,expected_hash,reader_cls",
    [
        # First version supporting JSON format for specs
        ("specfiles/hdf5.v013.json.gz", "vglgw4reavn65vx5d4dlqn6rjywnq76d", spack.spec.SpecfileV1),
        # Introduces full hash in the format, still has 3 hashes
        ("specfiles/hdf5.v016.json.gz", "stp45yvzte43xdauknaj3auxlxb4xvzs", spack.spec.SpecfileV1),
        # Introduces "build_specs", see https://github.com/spack/spack/pull/22845
        ("specfiles/hdf5.v017.json.gz", "xqh5iyjjtrp2jw632cchacn3l7vqzf3m", spack.spec.SpecfileV2),
        # Use "full hash" everywhere, see https://github.com/spack/spack/pull/28504
        ("specfiles/hdf5.v019.json.gz", "iulacrbz7o5v5sbj7njbkyank3juh6d3", spack.spec.SpecfileV3),
        # Add properties on edges, see https://github.com/spack/spack/pull/34821
        ("specfiles/hdf5.v020.json.gz", "vlirlcgazhvsvtundz4kug75xkkqqgou", spack.spec.SpecfileV4),
    ],
)
def test_load_json_specfiles(specfile, expected_hash, reader_cls):
    fullpath = os.path.join(spack.paths.test_path, "data", specfile)
    with gzip.open(fullpath, "rt", encoding="utf-8") as f:
        data = json.load(f)

    s1 = Spec.from_dict(data)
    s2 = reader_cls.load(data)

    assert s2.dag_hash() == expected_hash
    assert s1.dag_hash() == s2.dag_hash()
    assert s1 == s2
    assert Spec.from_json(s2.to_json()).dag_hash() == s2.dag_hash()

    openmpi_edges = s2.edges_to_dependencies(name="openmpi")
    assert len(openmpi_edges) == 1

    # Check that virtuals have been reconstructed for specfiles conforming to
    # version 4 on.
    if reader_cls.SPEC_VERSION >= spack.spec.SpecfileV4.SPEC_VERSION:
        assert "mpi" in openmpi_edges[0].virtuals

        # The virtuals attribute must be a tuple, when read from a
        # JSON or YAML file, not a list
        for edge in s2.traverse_edges():
            assert isinstance(edge.virtuals, tuple), edge

    # Ensure we can format {compiler} tokens
    assert s2.format("{compiler}") != "none"
    assert s2.format("{compiler.name}") == "gcc"
    assert s2.format("{compiler.version}") != "none"

    # Ensure satisfies still works with compilers
    assert s2.satisfies("%gcc")
    assert s2.satisfies("%gcc@9.4.0")


def test_anchorify_1():
    """Test that anchorify replaces duplicate values with references to a single instance, and
    that that results in anchors in the output YAML."""
    before = {"a": [1, 2, 3], "b": [1, 2, 3]}
    after = {"a": [1, 2, 3], "b": [1, 2, 3]}
    syaml.anchorify(after)
    assert before == after
    assert after["a"] is after["b"]

    # Check if anchors are used
    out = io.StringIO()
    _vendoring.ruamel.yaml.YAML().dump(after, out)
    assert (
        out.getvalue()
        == """\
a: &id001
- 1
- 2
- 3
b: *id001
"""
    )


def test_anchorify_2():
    before = {"a": {"b": {"c": True}}, "d": {"b": {"c": True}}, "e": {"c": True}}
    after = {"a": {"b": {"c": True}}, "d": {"b": {"c": True}}, "e": {"c": True}}
    syaml.anchorify(after)
    assert before == after
    assert after["a"] is after["d"]
    assert after["a"]["b"] is after["e"]

    # Check if anchors are used
    out = io.StringIO()
    _vendoring.ruamel.yaml.YAML().dump(after, out)
    assert (
        out.getvalue()
        == """\
a: &id001
  b: &id002
    c: true
d: *id001
e: *id002
"""
    )


@pytest.mark.parametrize(
    "spec_str",
    [
        "hdf5 ++mpi",
        "hdf5 cflags==-g",
        "hdf5 foo==bar",
        "hdf5~~mpi++shared",
        "hdf5 cflags==-g foo==bar cxxflags==-O3",
        "hdf5 cflags=-g foo==bar cxxflags==-O3",
        "hdf5%gcc",
        "hdf5%cmake",
        "hdf5^gcc",
        "hdf5^cmake",
    ],
)
def test_pickle_roundtrip_for_abstract_specs(spec_str):
    """Tests that abstract specs correctly round trip when pickled.

    This test compares both spec objects and their string representation, due to some
    inconsistencies in how `Spec.__eq__` is implemented.
    """
    s = spack.spec.Spec(spec_str)
    t = pickle.loads(pickle.dumps(s))
    assert s == t
    assert str(s) == str(t)


def test_specfile_alias_is_updated():
    """Tests that the SpecfileLatest alias gets updated on a Specfile version bump"""
    specfile_class_name = f"SpecfileV{spack.spec.SPECFILE_FORMAT_VERSION}"
    specfile_cls = getattr(spack.spec, specfile_class_name)
    assert specfile_cls is spack.spec.SpecfileLatest


@pytest.mark.parametrize("spec_str", ["mpileaks %gcc", "mpileaks ^zmpi ^callpath%gcc"])
def test_direct_edges_and_round_tripping_to_dict(spec_str, default_mock_concretization):
    """Tests that we preserve edge information when round-tripping to dict"""
    original = Spec(spec_str)
    reconstructed = Spec.from_dict(original.to_dict())
    assert original == reconstructed
    assert original.to_dict() == reconstructed.to_dict()

    concrete = default_mock_concretization(spec_str)
    concrete_reconstructed = Spec.from_dict(concrete.to_dict())
    assert concrete == concrete_reconstructed
    assert concrete.to_dict() == concrete_reconstructed.to_dict()

    # Ensure we don't get 'direct' in concrete JSON specs, for the time being
    d = concrete.to_dict()
    for node in d["spec"]["nodes"]:
        if "dependencies" not in node:
            continue
        for dependency_data in node["dependencies"]:
            assert "direct" not in dependency_data["parameters"]
