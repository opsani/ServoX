import re
from datetime import datetime
from inspect import Signature
from typing import Callable, Iterable, List, Optional, Tuple, Union

import pytest

from servo.checks import (
    BaseChecks,
    Check,
    CheckFilter,
    CheckHandler,
    CheckHandlerResult,
    ErrorSeverity,
    check,
    create_checks_from_iterable,
    multicheck,
    require,
    warn,
)
from servo.configuration import BaseConfiguration
from servo.utilities.inspect import get_instance_methods

pytestmark = pytest.mark.freeze_time("2020-08-24")


def test_created_at_set_automatically() -> None:
    check = Check(name="Test", success=True)
    assert check.created_at == datetime(2020, 8, 24, 0, 0)


def test_serialize_with_exception() -> None:
    exception = RuntimeError("Testing")
    check = Check(name="Test", success=False, exception=exception)
    assert check.json() == (
        '{"name": "Test", "id": "1bab7e8d", "description": null, "severity": "common", "tags": null, "success": false, "message": null, "exception": "RuntimeError(\'Testing\')", "created_at": "2020-08-24T00:00:00", "run_at": null, "runtime": null}'
    )


async def test_inline_check() -> None:
    check = await Check.run("Test Inline Runner", handler=lambda: True)
    assert check
    assert check.name == "Test Inline Runner"
    assert check.success
    assert check.created_at == datetime(2020, 8, 24, 0, 0)


async def test_inline_check_failure() -> None:
    def failing_check() -> None:
        raise RuntimeError("Testing Failure")

    check = await Check.run("Test Inline Failure", handler=failing_check)
    assert check
    assert check.name == "Test Inline Failure"
    assert not check.success
    assert check.created_at == datetime(2020, 8, 24, 0, 0)


##
## Checks class specific


async def test_raises_on_invalid_method_name() -> None:
    class MeasureChecks(BaseChecks):
        def invalid_check(self) -> Check:
            return Check(name="Test", success=True)

    with pytest.raises(ValueError) as e:
        config = BaseConfiguration()
        await MeasureChecks.run(config)

    assert e
    assert (
        str(e.value)
        == 'invalid method name "invalid_check": method names of Checks subtypes must start with "_" or "check_"'
    )


async def test_allows_underscored_method_names() -> None:
    class MeasureChecks(BaseChecks):
        def _allowed_helper(self) -> Check:
            return Check(name="Test", success=True)

    config = BaseConfiguration()
    results = await MeasureChecks.run(config)
    assert results is not None


async def test_raises_on_invalid_signature() -> None:
    class MeasureChecks(BaseChecks):
        def check_invalid(self) -> int:
            return 123

    with pytest.raises(TypeError) as e:
        config = BaseConfiguration()
        await MeasureChecks.run(config)

    assert e
    assert (
        str(e.value)
        == """invalid check "check_invalid": incompatible return type annotation "<class 'int'>" in callable signature "() -> int", expected "<class 'servo.checks.Check'>\""""
    )

async def test_raises_on_invalid_return_type() -> None:
    class MeasureChecks(BaseChecks):
        def check_invalid(self) -> Check:
            return 123

    with pytest.raises(TypeError) as e:
        config = BaseConfiguration()
        await MeasureChecks.run(config)

    assert e
    assert (
        str(e.value)
        == """invalid check "check_invalid": expected return type "Check" but handler returned "int\""""
    )


async def test_valid_checks() -> None:
    class MeasureChecks(BaseChecks):
        def check_something(self) -> Check:
            return Check(name="Test", success=True)

    config = BaseConfiguration()
    checks = await MeasureChecks.run(config)
    assert checks == [
        Check(name="Test", success=True, created_at=datetime(2020, 8, 24, 0, 0))
    ]


async def test_run_as_instance() -> None:
    class MeasureChecks(BaseChecks):
        def check_something(self) -> Check:
            return Check(name="Test", success=True)

    config = BaseConfiguration()
    checker = MeasureChecks(config)
    checks = await checker.run_all()
    assert checks == [
        Check(name="Test", success=True, created_at=datetime(2020, 8, 24, 0, 0))
    ]


async def test_check_ordering() -> None:
    class MeasureChecks(BaseChecks):
        def check_one(self) -> Check:
            return Check(name="1", success=True)

        def check_two(self) -> Check:
            return Check(name="2", success=False)

        def check_three(self) -> Check:
            return Check(name="3", success=True)

    config = BaseConfiguration()
    checks = await MeasureChecks.run(config)
    values = list(map(lambda c: (c.name, c.success), checks))
    assert values == [("1", True), ("2", False), ("3", True)]


async def test_check_aborts_on_failed_requirement() -> None:
    class MeasureChecks(BaseChecks):
        def check_one(self) -> Check:
            return Check(name="1", success=True)

        def check_two(self) -> Check:
            return Check(name="2", success=False, severity=ErrorSeverity.CRITICAL)

        def check_three(self) -> Check:
            return Check(name="3", success=True)

    config = BaseConfiguration()
    checks = await MeasureChecks.run(config)
    values = list(map(lambda c: (c.name, c.success), checks))
    assert values == [("1", True), ("2", False)]


class NamedChecks(BaseChecks):
    @check("Check connectivity")
    def check_connectivity(self) -> CheckHandlerResult:
        return True

    @check("Verify permissions")
    def check_permissions(self) -> None:
        ...

    @check("Ensure adequate resources")
    def check_resources(self) -> None:
        ...


@pytest.mark.parametrize(
    "return_value, success, message",
    [
        ("this is the message", True, "this is the message"),
        (True, True, None),
        ((False, "didn't work"), False, "didn't work"),
        (None, True, None),
    ],
)
def test_valid_check_decorator_return_values(return_value, success, message) -> None:
    @check("Test decorator")
    def check_test() -> CheckHandlerResult:
        return return_value

    check_ = check_test()
    assert check_
    assert isinstance(check_, Check)
    assert check_.success == success
    assert check_.message == message
    assert check_.exception is None


@pytest.mark.parametrize(
    "return_value, exception_type, message",
    [
        (
            123,
            ValueError,
            ('caught exception (ValueError): check method returned unexpected value of type "int"'),
        ),
        (
            (False, 187),
            ValueError,
            (
                "caught exception (ValidationError): 1 validation error for Check\n"
                "message\n"
                "  str type expected (type=type_error.str)"
            ),
        ),
        (
            (666, "fail"),
            ValueError,
            (
                "caught exception (ValidationError): 1 validation error for Check\n"
                "success\n"
                "  value could not be parsed to a boolean (type=type_error.bool)"
            ),
        ),
    ],
)
def test_invalid_check_decorator_return_values(
    return_value, exception_type, message
) -> None:
    @check("Test decorator")
    def check_test() -> CheckHandlerResult:
        return return_value

    check_ = check_test()
    assert check_
    assert isinstance(check_, Check)
    assert check_.success == False
    assert check_.message == message
    assert check_.exception is not None
    assert isinstance(check_.exception, exception_type)


class ValidHandlerSignatures:
    def check_none(self) -> None:
        ...

    def check_str(self) -> str:
        ...

    def check_bool(self) -> bool:
        ...

    def check_tuple(self) -> Tuple[bool, str]:
        ...

    def check_union(self) -> Union[str, bool]:
        ...

    def check_optional(self) -> Optional[str]:
        ...

    def check_union_of_tuple(self) -> Union[str, Tuple[bool, str]]:
        ...

    def check_optional_tuple(self) -> Optional[Tuple[bool, str]]:
        ...


@pytest.mark.parametrize(
    "method",
    get_instance_methods(ValidHandlerSignatures()).values(),
    ids=get_instance_methods(ValidHandlerSignatures()).keys(),
)
def test_valid_signatures(method) -> None:
    check(method.__name__)(method)


class InvalidHandlerSignatures:
    def check_int(self) -> int:
        ...

    def check_list(self) -> List[Check]:
        ...

    def check_invalid_tuple(self) -> Tuple[int, str]:
        ...

    def check_invalid_optional(self) -> Optional[float]:
        ...

    def check_invalid_union(self) -> Union[bool, str, float]:
        ...

    def check_invalid_union_with_tuple(self) -> Union[bool, Tuple[str, float]]:
        ...


@pytest.mark.parametrize(
    "method", get_instance_methods(InvalidHandlerSignatures()).values()
)
def test_invalid_signatures(method) -> None:
    with pytest.raises(TypeError) as e:
        check(method.__name__)(method)

    sig = Signature.from_callable(method)
    message = f'invalid check handler "{method.__name__}": incompatible return type annotation in signature {repr(sig)}, expected to match <Signature () -> Union[bool, str, Tuple[bool, str], NoneType]>'
    assert str(e.value) == message


def test_decorating_invalid_signatures() -> None:
    with pytest.raises(TypeError) as e:

        @check("Test decorator")
        def check_test() -> int:
            ...

    assert e
    assert str(e.value) == (
        'invalid check handler "check_test": incompatible return type annotation in signature <Signature () -> int>, e'
        "xpected to match <Signature () -> Union[bool, str, Tuple[bool, str], NoneType]>"
    )


@pytest.mark.freeze_time("2020-08-25", auto_tick_seconds=15)
@pytest.mark.event_loop_policy("default")
async def test_check_timer() -> None:
    @check("Check timer")
    def check_test() -> None:
        ...

    check_ = check_test()
    assert check_
    assert isinstance(check_, Check)
    assert check_.run_at == datetime(2020, 8, 25, 0, 0, 15)
    assert check_.runtime == "15s"


@pytest.mark.freeze_time("2020-08-25", auto_tick_seconds=15)
@pytest.mark.event_loop_policy("default")
async def test_decorate_async() -> None:
    @check("Check async")
    async def check_test() -> None:
        ...

    check_ = await check_test()
    assert check_
    assert isinstance(check_, Check)
    assert check_.run_at == datetime(2020, 8, 25, 0, 0, 15)
    assert check_.runtime == "15s"


async def test_run_check_by_name_filter() -> None:
    nc = NamedChecks(BaseConfiguration())
    checks = await nc.run_all(matching=CheckFilter(name="Check connectivity"))
    check = checks[0]
    assert check
    assert check.name == "Check connectivity"
    assert check.success


async def test_run_check_by_name() -> None:
    nc = NamedChecks(BaseConfiguration())
    check = await nc.run_one(name="Check connectivity")
    assert check
    assert check.name == "Check connectivity"
    assert check.success


@pytest.mark.parametrize("attr", ["id", "name"])
async def test_run_check_by_invalid_value(attr) -> None:
    selector = {attr: "INVALID"}
    nc = NamedChecks(BaseConfiguration())
    with pytest.raises(ValueError) as e:
        await nc.run_one(**selector)

    assert (
        str(e.value) == f"failed running check: no check found with {attr} = 'INVALID'"
    )


def test_generate_check_id() -> None:
    check = Check(name="Ensure adequate resources", success=True)
    assert check.id == "c272d5e0"


def test_decorator_sets_id_to_method_name() -> None:
    checks = NamedChecks(BaseConfiguration())
    assert checks.check_connectivity.__check__.id == "check_connectivity"


async def test_run_check_by_id_filter() -> None:
    nc = NamedChecks(BaseConfiguration())
    checks = await nc.run_all(matching=CheckFilter(id="check_connectivity"))
    assert len(checks) == 1
    check = checks[0]
    assert check
    assert check.name == "Check connectivity"
    assert check.success


async def test_run_check_by_id() -> None:
    nc = NamedChecks(BaseConfiguration())
    check = await nc.run_one(id="check_connectivity")
    assert check
    assert check.name == "Check connectivity"
    assert check.success


class FilterableChecks(BaseChecks):
    @check("name-only")
    def check_one(self) -> None:
        ...

    @check("name-and-id", id="explicit-id")
    def check_two(self) -> None:
        ...

    @check("name-and-tags", tags=["one", "two"])
    def check_three(self) -> None:
        ...

    @check("name-and-identicial-tags", tags=["one", "two"])
    def check_four(self) -> None:
        ...

    @check("name-and-exclusive-tags", tags=["three", "four"])
    def check_five(self) -> None:
        ...

    @check("name-and-intersecting-tags", tags=["one", "four"])
    def check_six(self) -> None:
        ...


@pytest.mark.parametrize(
    "name, id, tags, expected_ids",
    [
        # name cases
        ("name-only", None, None, ["check_one"]),
        ("invalid", None, None, []),
        (("name-only", "name-and-id"), None, None, ["check_one", "explicit-id"]),
        (["name-only", "name-and-id"], None, None, ["check_one", "explicit-id"]),
        (
            re.compile(".*tags"),
            None,
            None,
            ["check_three", "check_four", "check_five", "check_six"],
        ),
        # id cases
        (None, "explicit-id", None, ["explicit-id"]),
        (None, ("explicit-id", "check_three"), None, ["explicit-id", "check_three"]),
        (None, re.compile("[i]+"), None, ["explicit-id", "check_five", "check_six"]),
        # tag cases
        (None, None, ["one"], ["check_three", "check_four", "check_six"]),
        (None, None, set(), []),
        (
            None,
            None,
            ("one", "four"),
            ["check_three", "check_four", "check_five", "check_six"],
        ),
        (None, None, {"invalid"}, []),
        # compound cases
        (None, re.compile("[i]+"), {"four"}, ["check_five", "check_six"]),
        (re.compile("exclusive"), re.compile("[i]+"), None, ["check_five"]),
    ],
)
async def test_filtering(name, id, tags, expected_ids) -> None:
    checks = await FilterableChecks.run(
        BaseConfiguration(), matching=CheckFilter(name=name, id=id, tags=tags)
    )
    ids = list(map(lambda c: c.id, checks))
    assert len(ids) == len(expected_ids)
    assert ids == expected_ids


class RequirementChecks(BaseChecks):
    @check("required-1", severity=ErrorSeverity.CRITICAL)
    def check_one(self) -> None:
        ...

    @check("not-required-1")
    def check_two(self) -> None:
        ...

    @check("not-required-2")
    def check_three(self) -> None:
        raise RuntimeError("fail check")

    @require("required-2")
    def check_four(self) -> None:
        raise RuntimeError("fail check")

    @require("required-3")
    def check_five(self) -> None:
        ...

    @check("not-required-3")
    def check_six(self) -> None:
        ...


@pytest.mark.parametrize(
    "name, halt_on, expected_results",
    [
        # no filter, halt at not-required-2
        (
            None,
            ErrorSeverity.CRITICAL,
            {
                "required-1": True,
                "not-required-1": True,
                "not-required-2": False,
                "required-2": False,
            },
        ),
        # no filter, continue to end
        (
            None,
            None,
            {
                "required-1": True,
                "not-required-1": True,
                "not-required-2": False,
                "required-2": False,
                "required-3": True,
                "not-required-3": True,
            },
        ),
        # run not-required-1, trigger 1 requirement, no failures
        (
            "not-required-1",
            ErrorSeverity.CRITICAL,
            {"not-required-1": True, "required-1": True},
        ),
        # run not-required-2, trigger 1 requirement, fail
        ("not-required-2", None, {"not-required-2": False, "required-1": True}),
        # run required-3, trigger 2 requirements, halt at required-2
        (
            "not-required-3",
            ErrorSeverity.CRITICAL,
            {"required-1": True, "required-2": False},
        ),
        # run all required-3, trigger 2 requirements, required-2 fails
        (
            "not-required-3",
            None,
            {
                "required-1": True,
                "required-2": False,
                "required-3": True,
                "not-required-3": True,
            },
        ),
        # run not-required-1 and not-required-3
        (
            ("not-required-1", "not-required-3"),
            ErrorSeverity.CRITICAL,
            {
                "required-1": True,
                "not-required-1": True,
                "required-2": False,
            },
        ),
        (
            ("not-required-1", "not-required-3"),
            None,
            {
                "required-1": True,
                "not-required-1": True,
                "required-2": False,
                "required-3": True,
                "not-required-3": True,
            },
        ),
    ],
)
async def test_running_requirements(name, halt_on, expected_results) -> None:
    checks = await RequirementChecks.run(
        BaseConfiguration(), matching=CheckFilter(name=name), halt_on=halt_on
    )
    actual_results = dict(map(lambda c: (c.name, c.success), checks))
    assert actual_results == expected_results


class MixedChecks(BaseChecks):
    @check("one")
    def check_one(self) -> None:
        ...

    def check_two(self) -> Check:
        return Check(name="two", success=True)

    @check("three")
    def check_three(self) -> None:
        ...

    def check_four(self) -> Check:
        return Check(name="four", success=True)


@pytest.mark.parametrize(
    "name, expected_results",
    [
        ("one", ["one", "two", "four"]),
        ("three", ["two", "three", "four"]),
        ("unknown", ["two", "four"]),
    ],
)
async def test_mixed_checks(name, expected_results) -> None:
    checks = await MixedChecks.run(BaseConfiguration(), matching=CheckFilter(name=name))
    actual_results = list(map(lambda c: c.name, checks))
    assert actual_results == expected_results


async def test_generate_checks() -> None:
    handler = lambda c: f"so_check_it_{c}"
    items = ["one", "two", "three"]
    ItemChecks = create_checks_from_iterable(handler, items)
    checker = ItemChecks(BaseConfiguration())
    results = await checker.run_all()
    assert len(results) == 3
    messages = list(map(lambda c: c.message, results))
    assert messages == ["so_check_it_one", "so_check_it_two", "so_check_it_three"]


async def test_add_checks_to_existing_class() -> None:
    handler = lambda c: f"so_check_it_{c}"
    items = ["five", "six", "seven"]
    ExtendedChecks = create_checks_from_iterable(handler, items, base_class=MixedChecks)
    checker = ExtendedChecks(BaseConfiguration())
    results = await checker.run_all()
    assert len(results) == 7
    attrs = list(map(lambda c: [c.name, c.id, c.message], results))
    assert attrs == [
        [
            "one",
            "check_one",
            None,
        ],
        [
            "two",
            "0b1c4a4d",
            None,
        ],
        [
            "three",
            "check_three",
            None,
        ],
        [
            "four",
            "31d68b28",
            None,
        ],
        [
            "Check five",
            "b3f49d29",
            "so_check_it_five",
        ],
        [
            "Check six",
            "bdc0e261",
            "so_check_it_six",
        ],
        [
            "Check seven",
            "b991c85a",
            "so_check_it_seven",
        ],
    ]


class MultiChecks(BaseChecks):
    @multicheck("Check number {item}")
    def check_numbers(self) -> Tuple[Iterable, CheckHandler]:
        def handler(value: str) -> str:
            return f"Number {value} was checked"

        return ["one", "two", "three"], handler

    @multicheck("Asynchronously check number {item}")
    async def check_numbers_async(self) -> Tuple[Iterable, CheckHandler]:
        async def handler(value: str) -> str:
            return f"Number {value} was checked"

        return ["four", "five", "six"], handler


async def test_multichecks() -> None:
    checker = MultiChecks(BaseConfiguration())
    results = await checker.run_all()
    attrs = list(map(lambda c: [c.name, c.id, c.message], results))
    assert attrs == [
        [
            "Check number one",
            "check_numbers_item_0",
            "Number one was checked",
        ],
        [
            "Check number two",
            "check_numbers_item_1",
            "Number two was checked",
        ],
        [
            "Check number three",
            "check_numbers_item_2",
            "Number three was checked",
        ],
        [
            "Asynchronously check number four",
            "check_numbers_async_item_0",
            "Number four was checked",
        ],
        [
            "Asynchronously check number five",
            "check_numbers_async_item_1",
            "Number five was checked",
        ],
        [
            "Asynchronously check number six",
            "check_numbers_async_item_2",
            "Number six was checked",
        ],
    ]


async def test_multichecks_filtering() -> None:
    checker = MultiChecks(BaseConfiguration())
    results = await checker.run_all(
        matching=CheckFilter(id=["check_numbers_item_0", "check_numbers_async_item_1"])
    )
    attrs = list(map(lambda c: [c.name, c.id, c.message], results))
    assert attrs == [
        [
            "Check number one",
            "check_numbers_item_0",
            "Number one was checked",
        ],
        [
            "Asynchronously check number five",
            "check_numbers_async_item_1",
            "Number five was checked",
        ],
    ]


async def test_multichecks_async() -> None:
    checker = MultiChecks(BaseConfiguration())
    results = await checker.run_all()
    attrs = list(map(lambda c: [c.name, c.id, c.message], results))
    assert attrs == [
        [
            "Check number one",
            "check_numbers_item_0",
            "Number one was checked",
        ],
        [
            "Check number two",
            "check_numbers_item_1",
            "Number two was checked",
        ],
        [
            "Check number three",
            "check_numbers_item_2",
            "Number three was checked",
        ],
        [
            "Asynchronously check number four",
            "check_numbers_async_item_0",
            "Number four was checked",
        ],
        [
            "Asynchronously check number five",
            "check_numbers_async_item_1",
            "Number five was checked",
        ],
        [
            "Asynchronously check number six",
            "check_numbers_async_item_2",
            "Number six was checked",
        ],
    ]


def test_multicheck_invalid_args() -> None:
    with pytest.raises(TypeError) as e:

        class BadArgs(BaseChecks):
            @multicheck("Check something")
            def check_invalid(self, foo: int) -> int:
                ...

    assert e is not None
    assert (
        str(e.value)
        == """invalid multicheck handler "check_invalid": encountered unexpected parameter "foo" in callable signature "(self, foo: int) -> int", expected "() -> Tuple[Iterable, ~CheckHandler]\""""
    )


def test_multicheck_invalid_return_type() -> None:
    with pytest.raises(TypeError) as e:

        class BadArgs(BaseChecks):
            @multicheck("Check something")
            def check_invalid(self) -> int:
                123

    assert e is not None
    assert (
        str(e.value)
        == """invalid multicheck handler "check_invalid": incompatible return type annotation "<class 'int'>" in callable signature "(self) -> int", expected "typing.Tuple[typing.Iterable, ~CheckHandler]\""""
    )


class InvalidMultichecks(BaseChecks):
    @multicheck("Check number {item}")
    def check_invalid_identifiers(self) -> Tuple[Iterable, CheckHandler]:
        def handler(value: str) -> str:
            return f"Identifier {value} was checked"

        return ["NOT A VALID IDENTIFIER", "•••••"], handler


async def test_invalid_multichecks() -> None:
    checker = InvalidMultichecks(BaseConfiguration())
    results = await checker.run_all()
    attrs = list(map(lambda c: [c.name, c.id, c.message], results))
    assert attrs == [
        [
            "Check number NOT A VALID IDENTIFIER",
            "check_invalid_identifiers_item_0",
            "Identifier NOT A VALID IDENTIFIER was checked",
        ],
        [
            "Check number •••••",
            "check_invalid_identifiers_item_1",
            "Identifier ••••• was checked",
        ],
    ]


async def test_handles_method_attrs() -> None:
    class Other:
        def test(self):
            ...

    class MethodAttrsCheck(BaseChecks):
        other: Callable[..., None]

    checker = MethodAttrsCheck(BaseConfiguration(), other=Other().test)
    await checker.run_all()


class WarningChecks(BaseChecks):
    @check("warning-1", severity=ErrorSeverity.WARNING)
    def check_one(self) -> None:
        raise RuntimeError("Failure")

    @warn("warning-2")
    def check_two(self) -> Tuple[bool, str]:
        return (False, "Something may not be quite right")


async def test_warnings() -> None:
    results = await WarningChecks.run(BaseConfiguration())
    attrs = list(map(lambda c: [c.name, c.id, c.success, c.message], results))
    assert attrs == [
        [
            "warning-1",
            "check_one",
            False,
            "caught exception (RuntimeError): Failure",
        ],
        [
            "warning-2",
            "check_two",
            False,
            "Something may not be quite right",
        ],
    ]

# TODO: add tests for default checks
