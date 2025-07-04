# coding: utf-8

if False:  # MYPY
    from typing import Dict, Any  # NOQA

_package_data = dict(
    full_package_name='_vendoring.ruamel.yaml',
    version_info=(0, 17, 21),
    __version__='0.17.21',
    version_timestamp='2022-02-12 09:49:22',
    author='Anthon van der Neut',
    author_email='a.van.der.neut@ruamel.eu',
    description='_vendoring.ruamel.yaml is a YAML parser/emitter that supports roundtrip preservation of comments, seq/map flow style, and map key order',  # NOQA
    entry_points=None,
    since=2014,
    extras_require={
        ':platform_python_implementation=="CPython" and python_version<"3.11"': ['_vendoring.ruamel.yaml.clib>=0.2.6'],  # NOQA
        '_vendoring.jinja2': ['_vendoring.ruamel.yaml._vendoring.jinja2>=0.2'],
        'docs': ['ryd'],
    },
    classifiers=[
        'Programming Language :: Python :: 3 :: Only',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3.6',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: Implementation :: CPython',
        'Topic :: Software Development :: Libraries :: Python Modules',
        'Topic :: Text Processing :: Markup',
        'Typing :: Typed',
    ],
    keywords='yaml 1.2 parser round-trip preserve quotes order config',
    read_the_docs='yaml',
    supported=[(3, 5)],  # minimum
    tox=dict(
        env='*f',  # f for 3.5
        fl8excl='_test/lib',
    ),
    # universal=True,
    python_requires='>=3',
    rtfd='yaml',
)  # type: Dict[Any, Any]


version_info = _package_data['version_info']
__version__ = _package_data['__version__']

try:
    from .cyaml import *  # NOQA

    __with_libyaml__ = True
except (ImportError, ValueError):  # for Jython
    __with_libyaml__ = False

from _vendoring.ruamel.yaml.main import *  # NOQA
