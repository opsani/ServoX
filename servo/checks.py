import asyncio
import datetime
import functools
import hashlib
import inspect
import sys
import types
from typing import (
    Any,
    Awaitable,
    Callable,
    Coroutine,
    Dict,
    Generator,
    Iterable,
    List,
    Optional,
    Pattern,
    Protocol,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    runtime_checkable,
)

import pydantic

import servo.configuration
import servo.logging
import servo.types
import servo.utilities
from servo.types import Duration, ErrorSeverity

__all__ = [
    "BaseChecks",
    "Check",
    "CheckFilter",
    "CheckHandler",
    "CheckHandlerResult",
    "ErrorSeverity",
    "check",
    "multicheck",
    "require",
    "warn",
]


CheckHandlerResult = Union[bool, str, Tuple[bool, str], None]
CheckHandler = TypeVar(
    "CheckHandler",
    Callable[..., CheckHandlerResult],
    Callable[..., Awaitable[CheckHandlerResult]],
)
CHECK_HANDLER_SIGNATURE = inspect.Signature(return_annotation=CheckHandlerResult)

Tag = pydantic.constr(
    strip_whitespace=True, min_length=1, max_length=32, regex="^([0-9a-z\\.-])*$"
)


class Check(pydantic.BaseModel, servo.logging.Mixin):
    """
    Check objects represent the status of required runtime conditions.

    A check is an atomic verification that a particular aspect
    of a configuration is functional and ready for deployment. Connectors can
    have an arbitrary number of prerequisites and options that need to be
    checked within the runtime environment.

    Checks are used to verify the correctness of servo configuration before
    starting optimization and report on health and readiness during operation.
    """

    name: pydantic.StrictStr
    """An arbitrary descriptive name of the condition being checked.
    """

    id: pydantic.StrictStr = None
    """A short identifier for the check. Generated automatically if unset.
    """

    description: Optional[pydantic.StrictStr]
    """An optional detailed description about the condition being checked.
    """

    severity: ErrorSeverity = ErrorSeverity.COMMON
    """The relative importance of the check determining failure handling.
    """

    tags: Optional[Set[Tag]]
    """
    An optional set of tags for filtering checks.

    Tags are strings between 1 and 32 characters in length and may contain
    only lowercase alphanumeric characters, hyphens '-', and periods '.'.
    """

    success: Optional[bool]
    """
    Indicates if the condition being checked was met or not.
    """

    message: Optional[pydantic.StrictStr]
    """
    An optional message describing the outcome of the check.

    The message is presented to users and should be informative. Long
    messages may be truncated on display.
    """

    exception: Optional[Exception]
    """
    An optional exception encountered while running the check.

    When checks encounter an exception condition, it is recommended to
    store the exception so that diagnostic metadata such as the stack trace
    can be presented to the user.
    """

    created_at: datetime.datetime = None
    """When the check was created (set automatically).
    """

    run_at: Optional[datetime.datetime]
    """An optional timestamp indicating when the check was run.
    """

    runtime: Optional[Duration]
    """An optional duration indicating how long it took for the check to run.
    """

    @classmethod
    async def run(
        cls,
        name: str,
        *,
        handler: CheckHandler,
        description: Optional[str] = None,
        args: List[Any] = [],
        kwargs: Dict[Any, Any] = {},
    ) -> "Check":
        """Run a check handler and return a Check object reporting the outcome.

        This method is useful for quickly implementing checks in connectors that
        do not have enough checkable conditions to warrant implementing a `Checks`
        subclass.

        The handler can be synchronous or asynchronous. An arbitrary number of positional
        and keyword arguments are supported. The values for the argument must be provided
        via the `args` and `kwargs` parameters. The handler must return a `bool`, `str`,
        `Tuple[bool, str]`, or `None` value. Boolean values indicate success or failure
        and string values are assigned to the `message` attribute of the Check object
        returned. Exceptions are rescued, mark the check as a failure, and assigned to
        the `exception` attribute.

        Args:
            name: A name for the check being run.
            handler: The callable to run to perform the check.
            args: A list of positional arguments to pass to the handler.
            kwargs: A dictionary of keyword arguments to pass to the handler.
            description: An optional detailed description about the check being run.

        Returns:
            A check object reporting the outcome of running the handler.
        """
        check = Check(name=name, description=description)
        await run_check_handler(check, handler, *args, **kwargs)
        return check

    @property
    def passed(self) -> bool:
        """Return a boolean value that Indicates if the check passed.

        Checks can pass by evaluating positively or being a warning.
        """
        return self.success or self.warning

    @property
    def failed(self) -> bool:
        """Return a boolean value that indicates if the check failed.
        """
        return not self.success and not self.warning

    @property
    def critical(self) -> bool:
        """Return a boolean value that indicates if the check is of critical severity.
        """
        return self.severity == ErrorSeverity.CRITICAL

    @property
    def warning(self) -> bool:
        """Return a boolean value that indicates if the check is of warning severity.
        """
        return self.severity == ErrorSeverity.WARNING

    @pydantic.validator("created_at", pre=True, always=True)
    @classmethod
    def _set_created_at_now(cls, v):
        return v or datetime.datetime.now()

    @pydantic.validator("id", pre=True, always=True)
    @classmethod
    def _generated_id(cls, v, values):
        return (
            v
            or hashlib.blake2b(
                values["name"].encode("utf-8"), digest_size=4
            ).hexdigest()
        )

    class Config:
        validate_assignment = True
        arbitrary_types_allowed = True
        json_encoders = {
            Exception: lambda v: repr(v),
        }


@runtime_checkable
class Checkable(Protocol):
    """Checkable objects can be represented as a Check."""

    def __check__() -> Check:
        """Return a Check representation of the object."""
        ...


CheckRunner = TypeVar("CheckRunner", Callable[..., Check], Coroutine[None, None, Check])


def check(
    name: str,
    *,
    description: Optional[str] = None,
    id: Optional[str] = None,
    severity: ErrorSeverity = ErrorSeverity.COMMON,
    tags: Optional[List[str]] = None,
) -> Callable[[CheckHandler], CheckRunner]:
    """
    Transform a function or method into a check.

    Checks are used to test the availability, readiness, and health of resources and
    services that used during optimization. The `Check` class models the status of a
    check that has been run. The `check` function is a decorator that transforms a
    function or method that returns a `bool`, `str`, `Tuple[bool, str]`, or `None`
    into a check function or method.

    The decorator requires a `name` parameter to identify the check as well as an optional
    informative `description`, an `id` for succinctly referencing the check, and a `severity`
    value that determines how failure is reported and affects depdendent checks.
    The body of the decorated function is used to perform the business logic of running
    the check. The decorator wraps the original function body into a handler that runs the
    check and marshalls the value returned or exception caught into a `Check` representation.
    The `run_at` and `runtime` properties are automatically set, providing execution timing of
    the check. The signature of the transformed function is `() -> Check`.

    Args:
        name: Human readable name of the check.
        description: Optional additional details about the check.
        id: A short identifier for referencing the check (e.g. from the CLI interface).
        severity: The severity level of failure.
        tags: An optional list of tags for filtering checks. Tags may contain only lowercase
            alphanumeric characters, hyphens '-', and periods '.'.

    Returns:
        A decorator function for transforming a function into a check.

    Raises:
        TypeError: Raised if the signature of the decorated function is incompatible.
    """

    def decorator(fn: CheckHandler) -> CheckRunner:
        _validate_check_handler(fn)
        __check__ = Check(
            name=name,
            description=description,
            id=(id or fn.__name__),
            severity=severity,
            tags=tags,
        )

        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def run_check(*args, **kwargs) -> Check:
                check = __check__.copy()
                await run_check_handler(check, fn, *args, **kwargs)
                return check

        else:
            @functools.wraps(fn)
            def run_check(*args, **kwargs) -> Check:
                check = __check__.copy()
                run_check_handler_sync(check, fn, *args, **kwargs)
                return check

        # update the wrapped return signature to conform with the protocol
        run_check.__check__ = __check__
        run_check.__annotations__["return"] = Check
        return cast(CheckRunner, run_check)

    return decorator


def require(
    name: str,
    *,
    description: Optional[str] = None,
    id: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> Callable[[CheckHandler], CheckRunner]:
    """Transform a function or method into a critical check.

    The require decorator is syntactic sugar for the `check` decorator to declare
    a check as being of the `ErrorSeverity.CRITICAL` severity. Refer to the check documentation
    for detailed information.
    """
    return check(
        name, description=description, id=id, tags=tags, severity=ErrorSeverity.CRITICAL
    )


def warn(
    name: str,
    *,
    description: Optional[str] = None,
    id: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> Callable[[CheckHandler], CheckRunner]:
    """Transform a function or method into a warning check.

    The warn decorator is syntactic sugar for the `check` decorator to declare
    a check as being of the `ErrorSeverity.WARNING` severity. Refer to the check documentation
    for detailed information.
    """
    return check(
        name, description=description, id=id, tags=tags, severity=ErrorSeverity.WARNING
    )


CHECK_SIGNATURE = inspect.Signature(return_annotation=Check)

MULTICHECK_SIGNATURE = inspect.Signature(
    return_annotation=Tuple[Iterable, CheckHandler]
)


class CheckFilter(pydantic.BaseModel):
    """CheckFilter objects are used to select a subset of available checks for execution.

    Specific checks can be targeted for execution using the metadata attributes of `name`,
    `id`, and `tags`. Metadata filters are evaluated using AND semantics. Names and ids
    are matched case-sensitively. Tags are always lowercase. Names and ids can be targeted
    using regular expression patterns.
    """

    name: Union[None, str, Sequence[str], Pattern[str]] = None
    """A name, sequence of names, or regex pattern for selecting checks by name.
    """

    id: Union[None, str, Sequence[str], Pattern[str]] = None
    """A name, sequence of names, or regex pattern for selecting checks by name.
    """

    tags: Optional[Set[str]] = None
    """A set of tags for selecting checks to be run. Checks matching any tag in the set
    are selected.
    """

    exclusive: bool = False

    @property
    def any(self) -> bool:
        """Return True if any constraints are in effect."""
        return not self.empty

    @property
    def empty(self) -> bool:
        """Return True if no constraints are in effect."""
        return bool(self.name is None and self.id is None and self.tags is None)

    def matches(self, check: Check) -> bool:
        """Match a check against the filter.

        Args:
            check: The check to match against the filter.

        Returns:
            bool: True if the check meets the name, id, and tags constraints.
        """
        if self.empty:
            return True

        return (
            self._matches_name(check)
            and self._matches_id(check)
            and self._matches_tags(check)
        )

    def _matches_name(self, check: Check) -> bool:
        return self._matches_str_attr(self.name, check.name)

    def _matches_id(self, check: Check) -> bool:
        return self._matches_str_attr(self.id, check.id)

    def _matches_tags(self, check: Check) -> bool:
        if self.tags is None:
            return True

        # exclude untagged checks if filtering by tag
        if check.tags is None:
            return False

        # look for an intersection in our sets
        return bool(self.tags.intersection(check.tags))

    def _matches_str_attr(
        self, attr: Union[None, str, Sequence[str], Pattern[str]], value: str
    ) -> bool:
        if attr is None:
            return True
        elif isinstance(attr, str):
            return value == attr
        elif isinstance(attr, Sequence):
            return value in attr and not self.exclusive
        elif isinstance(attr, Pattern):
            return bool(attr.search(value)) and not self.exclusive
        else:
            raise ValueError(
                f'unexpected value of type "{attr.__class__.__name__}": {attr}'
            )

    class Config:
        arbitrary_types_allowed = True


class BaseChecks(pydantic.BaseModel, servo.logging.Mixin):
    """
    Base class for collections of Check objects.

    This is a convenience class for quickly and cleanly implementing checks
    for a connector. A check is an atomic verification that a particular aspect
    of a configuration is functional and ready for deployment. Connectors can
    have an arbitrary number of prerequisites and options that need to be
    checked within the runtime environment. The BaseChecks class provides a simple
    inheritance based interface for implementing an arbitrary number of checks.

    Checks are implemented through standard instance methods that are prefixed
    with `check_`, accept no arguments, and return an instance of `Check`. The method
    body tests a single aspect of the configuration (rescuing exceptions as necessary)
    and returns a `Check` object that models the results of the check performed.

    Checks are executed in method definition order within the subclass (top to bottom).
    Check methods can be implemented synchronously or asynchronously. Methods that are
    declared as coroutines via the `async def` syntax are run asynchronously.

    By default, check execution is halted upon encountering a failure. This behavior
    allows the user to assume that the runtime environment described by preceding
    checks has been established and implement a narrowly scoped check. Halting execution
    can be overridden via the `halt_on` argument.

    Attributes:
        config: The configuration object for the connector being checked.
    """

    config: servo.configuration.BaseConfiguration
    """The configuration object for the connector being checked.
    """

    @classmethod
    async def run(
        cls,
        config: servo.configuration.BaseConfiguration,
        *,
        matching: Optional[CheckFilter] = None,
        halt_on: Optional[ErrorSeverity] = ErrorSeverity.CRITICAL,
        **kwargs,
    ) -> List[Check]:
        """Run checks and return a list of Check objects reflecting the results.

        Checks are implemented as instance methods prefixed with `check_` that return a `Check`
        object. Please refer to the `BaseChecks` class documentation for details.

        Args:
            config: The connector configuration to initialize the checks instance with.
            matching: An optional filter to limit the set of checks that are run.
            halt_on: The severity of check failure that should halt the run.
            kwargs: Additional arguments to initialize the checks instance with.

        Returns:
            A list of `Check` objects that reflect the outcome of the checks executed.
        """
        return await cls(config).run_all(matching=matching, halt_on=halt_on)

    async def run_all(
        self,
        *,
        matching: Optional[CheckFilter] = None,
        halt_on: Optional[ErrorSeverity] = ErrorSeverity.CRITICAL,
    ) -> List[Check]:
        """Run all checks matching a filter and return the results.

        Args:
            matching: An optional filter to limit the set of checks that are run.
            halt_on: The severity of check failure that should halt the run.

        Returns:
            A list of checks that were run.
        """

        # expand any multicheck methods into instance methods
        await self._expand_multichecks()

        # identify methods that match the filter
        filtered_methods = []
        for method_name, method in self._check_methods():
            if matching and matching.any:
                if isinstance(method, Checkable):
                    spec = method.__check__
                else:
                    self.logger.warning(
                        f'filtering requested but encountered non-filterable check method "{method_name}"'
                    )
                    continue

                if not matching.matches(spec):
                    continue

            filtered_methods.append(method)

        # iterate a second time to run filtered and required checks
        checks = []
        for method_name, method in self._check_methods():
            if method in filtered_methods:
                filtered_methods.remove(method)
            else:
                spec = getattr(method, "__check__", None)
                if spec:
                    # once all filtered methods are removed, only run non-decorated
                    if not spec.critical or not filtered_methods:
                        continue

            check = await method() if asyncio.iscoroutinefunction(method) else method()
            if not isinstance(check, Check):
                raise TypeError(
                    f"invalid check \"{method_name}\": expected return type \"Check\" but handler returned \"{check.__class__.__name__}\""
                )

            checks.append(check)

            # halt the run if necessary
            if check.failed and halt_on:
                if (
                    halt_on == ErrorSeverity.WARNING
                    or (halt_on == ErrorSeverity.COMMON and not check.warning)
                    or (halt_on == ErrorSeverity.CRITICAL and check.critical)
                ):
                    break

        return checks

    async def run_one(
        self,
        *,
        id: Optional[str] = None,
        name: Optional[str] = None,
        halt_on: Optional[ErrorSeverity] = ErrorSeverity.CRITICAL,
        skip_requirements: bool = False,
    ) -> Check:
        """Run a single check by id or name and returns the result.

        Args:
            id: The id of the check to run. Defaults to None.
            name: The name of the check. Defaults to None.
            skip_requirements: When True, prerequisites are skipped.

        Raises:
            ValueError: Raised if no check exists with the given id or name.
            RuntimeError: Raised if the check was not run do to a prerequisite failure.

        Returns:
            A Check object representing the result of the check.
        """
        if id is None and name is None:
            raise ValueError(
                "unable to run check: an id or name must be given (both are None)"
            )

        if id is not None and name is not None:
            raise ValueError(
                f"unable to run check: id and name cannot both be given (id={repr(id)}, name={repr(name)})"
            )

        for attr in ("id", "name"):
            value = locals().get(attr, None)
            if value is not None and not isinstance(value, str):
                raise ValueError(
                    f"unable to run check: {attr} must be a string (got {repr(value)})"
                )

        results = await self.run_all(
            matching=CheckFilter(
                id=id,
                name=name,
                exclusive=skip_requirements,
            ), halt_on=halt_on
        )
        if not results:
            for attr in ("id", "name"):
                value = locals().get(attr, None)
                if value is not None:
                    raise ValueError(
                        f"failed running check: no check found with {attr} = {repr(value)}"
                    )

        result = results[-1]
        if result.id != id and result.name != name:
            for attr in ("id", "name"):
                value = locals().get(attr, None)
                if value is not None:
                    raise RuntimeError(
                        f"failed running check: check {attr} {repr(value)} was not run due to a prerequisite failure: check id '{result.id}' failed: \"{result.message}\""
                    ) from result.exception

        return result

    def _check_methods(self) -> Generator[Tuple[str, CheckRunner], None, None]:
        """Iterate over all check methods and yield the method name and callable method instance in method definition order.

        Check method names are prefixed with "check_", accept no parameters, and return a
        `Check` object reporting the outcome of the check operation.

        Yields:
            A tuple containing a string method name and a callable method that runs a check.
        """
        for name, method in servo.utilities.inspect.get_instance_methods(
            self, stop_at_parent=BaseChecks
        ).items():
            if name.startswith(("_", "run_")) or method.__self__ != self:
                continue

            if not name.startswith(("_", "check_")):
                raise ValueError(
                    f'invalid method name "{name}": method names of Checks subtypes must start with "_" or "check_"'
                )

            # skip multicheck source methods as they are atomized into instance methods
            if hasattr(method, "__multicheck__"):
                _validate_multicheck_handler(method)
                continue

            handler_signature = inspect.Signature.from_callable(method)
            handler_globalns = inspect.currentframe().f_back.f_globals
            handler_localns = inspect.currentframe().f_back.f_locals

            handler_mod_name = handler_localns.get("__module__", None)
            handler_module = sys.modules[handler_mod_name] if handler_mod_name else None
            servo.utilities.inspect.assert_equal_callable_descriptors(
                servo.utilities.inspect.CallableDescriptor(
                    signature=CHECK_SIGNATURE,
                    module=self.__module__,
                    globalns=globals(),
                    localns=locals(),
                ),
                servo.utilities.inspect.CallableDescriptor(
                    signature=handler_signature,
                    module=handler_module,
                    globalns=handler_globalns,
                    localns=handler_localns,
                ),
                name=name,
                callable_description="check"
            )

            yield (name, method)

    def __init__(
        self, config: servo.configuration.BaseConfiguration, *args, **kwargs
    ) -> None: # noqa: D107
        super().__init__(config=config, *args, **kwargs)

    async def _expand_multichecks(self) -> List[types.MethodType]:
        # search for any instance methods decorated by multicheck and expand them
        checks = []
        for method_name, method in servo.utilities.inspect.get_instance_methods(
            self
        ).items():
            if hasattr(method, "__multicheck__"):
                method.__multicheck__
                checks_fns = await method()
                for check_method_name, fn in checks_fns.items():
                    method = types.MethodType(fn, self)
                    setattr(self, check_method_name, method)
                    checks.append(method)

        return checks

    class Config:
        arbitrary_types_allowed = True
        extra = pydantic.Extra.allow


def _validate_check_handler(fn: CheckHandler) -> None:
    """
    Validate that a function or method is usable as a check handler.

    Check handlers accept no arguments and return a `bool`, `str`,
    `Tuple[bool, str]`, or `None`.

    Args:
        fn: The check handler to be validated.

    Raises:
        TypeError: Raised if the handler function is invalid.
    """
    signature = inspect.Signature.from_callable(fn)
    if len(signature.parameters) >= 1:
        for param in signature.parameters.values():
            if param.name == "self" and param.kind == param.POSITIONAL_OR_KEYWORD:
                continue

            raise TypeError(
                f'invalid check handler "{fn.__name__}": unexpected parameter "{param.name}" in signature {repr(signature)}, expected {repr(CHECK_HANDLER_SIGNATURE)}'
            )

    error = TypeError(
        f'invalid check handler "{fn.__name__}": incompatible return type annotation in signature {repr(signature)}, expected to match {repr(CHECK_HANDLER_SIGNATURE)}'
    )
    acceptable_types = set(get_args(CheckHandlerResult))
    origin = get_origin(signature.return_annotation)
    args = get_args(signature.return_annotation)
    if origin is not None:
        if origin == Union:
            handler_types = set(args)
            if handler_types - acceptable_types:
                raise error
        elif origin is tuple:
            if args != (bool, str):
                raise error
        else:
            raise error
    else:
        cls = (
            signature.return_annotation
            if inspect.isclass(signature.return_annotation)
            else signature.return_annotation.__class__
        )
        if not cls in acceptable_types:
            raise error


async def run_check_handler(
    check: Check, handler: CheckHandler, *args, **kwargs
) -> None:
    """Run a check handler and records the result into a Check object.

    The first item in args (if any) is given to the `format` builtin as arguments named "self" and "item"
    in order to support building dynamic, context specific values that are assigned as attributes of
    the Check instance given during execution. More concretely, this means that running a check handler
    with a non-empty arguments list will let you use provide format string input values of the form
    "Check that {item.name} work as expected (v{item.version}, release date: {item.released_At})".

    Args:
        check: The check to record execution results.
        handler: A callable handler to perform the check.
        args: A list of positional arguments to pass to the handler.
        kwargs: A dictionary of keyword arguments to pass to the handler.

    Raises:
        ValueError: Raised if an invalid value is returned by the handler.
    """
    try:
        if len(args):
            check.name = check.name.format(item=args[0], self=args[0])

        check.run_at = datetime.datetime.now()
        if asyncio.iscoroutinefunction(handler):
            result = await handler(*args, **kwargs)
        else:
            result = handler(*args, **kwargs)
        _set_check_result(check, result)
    except Exception as error:
        _set_check_result(check, error)
    finally:
        if check.run_at:
            check.runtime = Duration(datetime.datetime.now() - check.run_at)


def run_check_handler_sync(
    check: Check, handler: CheckHandler, *args, **kwargs
) -> None:
    """Run a check handler and record the result into a Check object.

    Args:
        check: The check to record execution results.
        handler: A callable handler to perform the check.
        args: A list of positional arguments to pass to the handler.
        kwargs: A dictionary of keyword arguments to pass to the handler.

    Raises:
        ValueError: Raised if an invalid value is returned by the handler.
    """
    try:
        check.run_at = datetime.datetime.now()
        _set_check_result(check, handler(*args, **kwargs))
    except Exception as error:
        _set_check_result(check, error)
    finally:
        check.runtime = Duration(datetime.datetime.now() - check.run_at)


def _set_check_result(
    check: Check, result: Union[None, bool, str, Tuple[bool, str], Exception]
) -> None:
    """Sets the result of a check handler run on a check instance."""
    check.success = True

    if isinstance(result, str):
        check.message = result
    elif isinstance(result, bool):
        check.success = result
    elif isinstance(result, tuple):
        check.success, check.message = result
    elif result is None:
        pass
    elif isinstance(result, Exception):
        check.success = False
        check.exception = result
        check.message = f"caught exception ({result.__class__.__name__}): {str(result) or repr(result)}"
    else:
        raise ValueError(
            f'check method returned unexpected value of type "{result.__class__.__name__}"'
        )


def create_checks_from_iterable(
    handler: CheckHandler,
    iterable: Iterable,
    *,
    base_class: Type[BaseChecks] = BaseChecks,
) -> BaseChecks:
    """Return a class wrapping each item in an iterable collection into check instance methods.

    Building a checks subclass implementation with this function is semantically equivalent to
    iterating through every item in the collection, defining a new `check_` prefixed method,
    and passing the item and the handler to the `run_check_handler` function.

    Some connector types such as metrics system integrations wind up exposing collections
    of homogenously typed settings within their configuration. The canonical example is a
    collection of queries against Prometheus. Each query really should be validated for
    correctness early and often, but this can become challenging to manage, audit, and enforce
    as the collection grows and entropy increases. Key challenges include non-obvious
    evolution within the collection and developer fatigue from boilerplate code maintenance.
    This function provides a remedy for these issues by wrapping these sorts of collections into
    fully featured classes that are integrated into the servo checks system.

    Args:
        handler: A callable for performing a check given a single element input.
        iterable: An iterable collection of checkable items to be wrapped into check methods.
        base_class: The base class for the new checks subclass. Enables mixed mode checks where
            some are written by hand and others a are generated.

    Returns:
        A new subclass of `BaseChecks` with instance method check implememntatiomns for each
        item in the `iterable` argument collection.
    """
    cls = type("_IterableChecks", (base_class,), {})

    def create_fn(name, item):
        async def fn(self) -> Check:
            check = fn.__check__.copy()
            await run_check_handler(check, handler, item)
            return check

        return fn

    for item in iterable:
        if isinstance(item, Checkable):
            check = item.__check__().copy()
            fn = create_fn(check.name, item)
            fn.__check__ = check
        else:
            name = item.name if hasattr(item, "name") else str(item)
            check = Check(name=f"Check {name}")
            fn = create_fn(name, item)
            fn.__check__ = check

        method_name = f"check_{check.id}"
        setattr(cls, method_name, fn)

    return cls


MultiCheckHandler = Callable[..., Tuple[Iterable, CheckHandler]]
MultiCheckExpander = Callable[..., Awaitable[Tuple[Iterable, CheckHandler]]]


def _validate_multicheck_handler(fn: MultiCheckHandler) -> None:
    handler_signature = inspect.Signature.from_callable(fn)
    handler_globalns = inspect.currentframe().f_back.f_globals
    handler_localns = inspect.currentframe().f_back.f_locals

    handler_mod_name = handler_localns.get("__module__", None)
    handler_module = sys.modules[handler_mod_name] if handler_mod_name else None

    servo.utilities.inspect.assert_equal_callable_descriptors(
        servo.utilities.inspect.CallableDescriptor(
            signature=MULTICHECK_SIGNATURE,
            module=MULTICHECK_SIGNATURE.__module__,
            globalns=globals(),
            localns=locals(),
        ),
        servo.utilities.inspect.CallableDescriptor(
            signature=handler_signature,
            module=handler_module,
            globalns=handler_globalns,
            localns=handler_localns,
        ),
        name=fn.__name__,
        callable_description="multicheck handler"
    )


def multicheck(
    base_name: str,
    *,
    description: Optional[str] = None,
    severity: ErrorSeverity = ErrorSeverity.COMMON,
    tags: Optional[List[str]] = None,
) -> Callable[[MultiCheckHandler], MultiCheckExpander]:
    """Expand a method into a sequence of checks from a returned iterable and
    check handler.

    This method provides an alternative to `create_checks_from_iterable` that is
    usable in cases where it is desirable to implement a `BaseChecks` subclass
    to hand-roll specific checks but there are also iterable values that can be
    checked by a common handler.

    The decorator works by dynamically creating check instance methods when a
    `BaseChecks` subclass is initialized.

    The decorator requires a `base_name` parameter to identify the checks as
    well as an optional informative `description`, a `severity` value that
    determines how a failure is handled. The `base_name` is interpolated into an
    item specific name by formatting the `base_name` with an item from the
    iterable collection. The item is passed to format as the `item` key,
    enabling the use of property access and subscripting within the format
    string.

    The decorated function must return an iterable collection of objects to be
    checked and a handler function for checking each value.

    Args:
        base_name: Human readable base name of the check. Expanded with each iterable.
        description: Optional additional details about the checks.
        severity: The severity level of failure.
        tags: An optional list of tags for filtering checks. Tags may contain only lowercase
            alphanumeric characters, hyphens '-', and periods '.'.

    Returns:
        A decorator function for transforming a method into a series of check instance methods.

    Raises:
        TypeError: Raised if the signature of the decorated function is incompatible.
    """
    def decorator(fn_: MultiCheckHandler) -> MultiCheckExpander:
        _validate_multicheck_handler(fn_)

        @functools.wraps(fn_)
        async def create_checks(*args, **kwargs) -> Tuple[Iterable, CheckHandler]:
            def create_fn(check, item):
                async def _fn(self) -> Check:
                    result_check = check.copy()
                    await run_check_handler(result_check, handler, item)
                    return result_check

                return _fn

            checks_fns = {}
            if asyncio.iscoroutinefunction(fn_):
                iterable, handler = await fn_(*args, **kwargs)
            else:
                iterable, handler = fn_(*args, **kwargs)

            for index, item in enumerate(iterable):
                check_name = base_name.format(item=item)
                fn_name = f"{fn_.__name__}_item_{index}"
                __check__ = Check(
                    name=check_name,
                    description=description,
                    id=fn_name,
                    severity=severity,
                    tags=tags,
                )
                fn = create_fn(__check__, item)
                fn.__check__ = __check__
                checks_fns[fn_name] = fn

            return checks_fns

        create_checks.__multicheck__ = True
        return create_checks

    return decorator
