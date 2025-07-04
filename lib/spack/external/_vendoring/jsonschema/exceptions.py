"""
Validation errors, and some surrounding helpers.
"""
from collections import defaultdict, deque
import itertools
import pprint
import textwrap

import _vendoring.attr

from _vendoring.jsonschema import _utils
from _vendoring.jsonschema.compat import PY3, iteritems


WEAK_MATCHES = frozenset(["anyOf", "oneOf"])
STRONG_MATCHES = frozenset()

_unset = _utils.Unset()


class _Error(Exception):
    def __init__(
        self,
        message,
        validator=_unset,
        path=(),
        cause=None,
        context=(),
        validator_value=_unset,
        instance=_unset,
        schema=_unset,
        schema_path=(),
        parent=None,
    ):
        super(_Error, self).__init__(
            message,
            validator,
            path,
            cause,
            context,
            validator_value,
            instance,
            schema,
            schema_path,
            parent,
        )
        self.message = message
        self.path = self.relative_path = deque(path)
        self.schema_path = self.relative_schema_path = deque(schema_path)
        self.context = list(context)
        self.cause = self.__cause__ = cause
        self.validator = validator
        self.validator_value = validator_value
        self.instance = instance
        self.schema = schema
        self.parent = parent

        for error in context:
            error.parent = self

    def __repr__(self):
        return "<%s: %r>" % (self.__class__.__name__, self.message)

    def __unicode__(self):
        essential_for_verbose = (
            self.validator, self.validator_value, self.instance, self.schema,
        )
        if any(m is _unset for m in essential_for_verbose):
            return self.message

        pschema = pprint.pformat(self.schema, width=72)
        pinstance = pprint.pformat(self.instance, width=72)
        return self.message + textwrap.dedent("""

            Failed validating %r in %s%s:
            %s

            On %s%s:
            %s
            """.rstrip()
        ) % (
            self.validator,
            self._word_for_schema_in_error_message,
            _utils.format_as_index(list(self.relative_schema_path)[:-1]),
            _utils.indent(pschema),
            self._word_for_instance_in_error_message,
            _utils.format_as_index(self.relative_path),
            _utils.indent(pinstance),
        )

    if PY3:
        __str__ = __unicode__
    else:
        def __str__(self):
            return unicode(self).encode("utf-8")

    @classmethod
    def create_from(cls, other):
        return cls(**other._contents())

    @property
    def absolute_path(self):
        parent = self.parent
        if parent is None:
            return self.relative_path

        path = deque(self.relative_path)
        path.extendleft(reversed(parent.absolute_path))
        return path

    @property
    def absolute_schema_path(self):
        parent = self.parent
        if parent is None:
            return self.relative_schema_path

        path = deque(self.relative_schema_path)
        path.extendleft(reversed(parent.absolute_schema_path))
        return path

    def _set(self, **kwargs):
        for k, v in iteritems(kwargs):
            if getattr(self, k) is _unset:
                setattr(self, k, v)

    def _contents(self):
        attrs = (
            "message", "cause", "context", "validator", "validator_value",
            "path", "schema_path", "instance", "schema", "parent",
        )
        return dict((attr, getattr(self, attr)) for attr in attrs)


class ValidationError(_Error):
    """
    An instance was invalid under a provided schema.
    """

    _word_for_schema_in_error_message = "schema"
    _word_for_instance_in_error_message = "instance"


class SchemaError(_Error):
    """
    A schema was invalid under its corresponding metaschema.
    """

    _word_for_schema_in_error_message = "metaschema"
    _word_for_instance_in_error_message = "schema"


@_vendoring.attr.s(hash=True)
class RefResolutionError(Exception):
    """
    A ref could not be resolved.
    """

    _cause = _vendoring.attr.ib()

    def __str__(self):
        return str(self._cause)


class UndefinedTypeCheck(Exception):
    """
    A type checker was asked to check a type it did not have registered.
    """

    def __init__(self, type):
        self.type = type

    def __unicode__(self):
        return "Type %r is unknown to this type checker" % self.type

    if PY3:
        __str__ = __unicode__
    else:
        def __str__(self):
            return unicode(self).encode("utf-8")


class UnknownType(Exception):
    """
    A validator was asked to validate an instance against an unknown type.
    """

    def __init__(self, type, instance, schema):
        self.type = type
        self.instance = instance
        self.schema = schema

    def __unicode__(self):
        pschema = pprint.pformat(self.schema, width=72)
        pinstance = pprint.pformat(self.instance, width=72)
        return textwrap.dedent("""
            Unknown type %r for validator with schema:
            %s

            While checking instance:
            %s
            """.rstrip()
        ) % (self.type, _utils.indent(pschema), _utils.indent(pinstance))

    if PY3:
        __str__ = __unicode__
    else:
        def __str__(self):
            return unicode(self).encode("utf-8")


class FormatError(Exception):
    """
    Validating a format failed.
    """

    def __init__(self, message, cause=None):
        super(FormatError, self).__init__(message, cause)
        self.message = message
        self.cause = self.__cause__ = cause

    def __unicode__(self):
        return self.message

    if PY3:
        __str__ = __unicode__
    else:
        def __str__(self):
            return self.message.encode("utf-8")


class ErrorTree(object):
    """
    ErrorTrees make it easier to check which validations failed.
    """

    _instance = _unset

    def __init__(self, errors=()):
        self.errors = {}
        self._contents = defaultdict(self.__class__)

        for error in errors:
            container = self
            for element in error.path:
                container = container[element]
            container.errors[error.validator] = error

            container._instance = error.instance

    def __contains__(self, index):
        """
        Check whether ``instance[index]`` has any errors.
        """

        return index in self._contents

    def __getitem__(self, index):
        """
        Retrieve the child tree one level down at the given ``index``.

        If the index is not in the instance that this tree corresponds to and
        is not known by this tree, whatever error would be raised by
        ``instance.__getitem__`` will be propagated (usually this is some
        subclass of `exceptions.LookupError`.
        """

        if self._instance is not _unset and index not in self:
            self._instance[index]
        return self._contents[index]

    def __setitem__(self, index, value):
        """
        Add an error to the tree at the given ``index``.
        """
        self._contents[index] = value

    def __iter__(self):
        """
        Iterate (non-recursively) over the indices in the instance with errors.
        """

        return iter(self._contents)

    def __len__(self):
        """
        Return the `total_errors`.
        """
        return self.total_errors

    def __repr__(self):
        return "<%s (%s total errors)>" % (self.__class__.__name__, len(self))

    @property
    def total_errors(self):
        """
        The total number of errors in the entire tree, including children.
        """

        child_errors = sum(len(tree) for _, tree in iteritems(self._contents))
        return len(self.errors) + child_errors


def by_relevance(weak=WEAK_MATCHES, strong=STRONG_MATCHES):
    """
    Create a key function that can be used to sort errors by relevance.

    Arguments:
        weak (set):
            a collection of validator names to consider to be "weak".
            If there are two errors at the same level of the instance
            and one is in the set of weak validator names, the other
            error will take priority. By default, :validator:`anyOf` and
            :validator:`oneOf` are considered weak validators and will
            be superseded by other same-level validation errors.

        strong (set):
            a collection of validator names to consider to be "strong"
    """
    def relevance(error):
        validator = error.validator
        return -len(error.path), validator not in weak, validator in strong
    return relevance


relevance = by_relevance()


def best_match(errors, key=relevance):
    """
    Try to find an error that appears to be the best match among given errors.

    In general, errors that are higher up in the instance (i.e. for which
    `ValidationError.path` is shorter) are considered better matches,
    since they indicate "more" is wrong with the instance.

    If the resulting match is either :validator:`oneOf` or :validator:`anyOf`,
    the *opposite* assumption is made -- i.e. the deepest error is picked,
    since these validators only need to match once, and any other errors may
    not be relevant.

    Arguments:
        errors (collections.Iterable):

            the errors to select from. Do not provide a mixture of
            errors from different validation attempts (i.e. from
            different instances or schemas), since it won't produce
            sensical output.

        key (collections.Callable):

            the key to use when sorting errors. See `relevance` and
            transitively `by_relevance` for more details (the default is
            to sort with the defaults of that function). Changing the
            default is only useful if you want to change the function
            that rates errors but still want the error context descent
            done by this function.

    Returns:
        the best matching error, or ``None`` if the iterable was empty

    .. note::

        This function is a heuristic. Its return value may change for a given
        set of inputs from version to version if better heuristics are added.
    """
    errors = iter(errors)
    best = next(errors, None)
    if best is None:
        return
    best = max(itertools.chain([best], errors), key=key)

    while best.context:
        best = min(best.context, key=key)
    return best
