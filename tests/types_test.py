from datetime import datetime, timedelta
from typing import Optional, Union

import freezegun
import pydantic
import pytest

from servo.types import (
    Adjustment,
    Control,
    DataPoint,
    Duration,
    InstanceTypeUnits,
    Measurement,
    Metric,
    TimeSeries,
    Unit,
)


class TestDuration:
    def test_init_with_seconds(self) -> None:
        duration = Duration(120)
        assert duration.total_seconds() == 120

    def test_init_with_timedelta(self) -> None:
        td = timedelta(seconds=120)
        duration = Duration(td)
        assert duration.total_seconds() == 120

    def test_init_with_duration_str(self) -> None:
        duration = Duration("5m")
        assert duration.total_seconds() == 300

    def test_init_with_invalid_str(self) -> None:
        with pytest.raises(ValueError) as error:
            Duration("invalid")
        assert str(error.value) == "Invalid duration 'invalid'"

    def test_init_with_time_components(self) -> None:
        duration = Duration(hours=10, seconds=25)
        assert duration.total_seconds() == 36025.0

    def test_eq_str(self) -> None:
        duration = Duration(300)
        assert duration == "5m"

    def test_eq_timedelta(self) -> None:
        duration = Duration(18_000)
        assert duration == timedelta(hours=5)

    def test_eq_numeric(self) -> None:
        duration = Duration("5h")
        assert duration == 18_000

    def test_repr(self) -> None:
        duration = Duration("5h")
        assert duration.__repr__() == "Duration('5h')"

    def test_str(self) -> None:
        duration = Duration("5h37m15s")
        assert duration.__str__() == "5h37m15s"

    def test_parse_extended(self) -> None:
        duration = Duration("2y4mm3d8h24m")
        assert duration.__str__() == "2y4mm3d8h24m"
        assert duration.total_seconds() == 73729440.0
        assert Duration(73729440.0).__str__() == "2y4mm3d8h24m"

    def test_pydantic_schema(self) -> None:
        model = pydantic.create_model("duration_model", duration=(Duration, ...))
        schema = model.schema()
        assert schema["properties"]["duration"] == {
            "title": "Duration",
            "type": "string",
            "format": "duration",
            "pattern": "([\\d\\.]+y)?([\\d\\.]+mm)?(([\\d\\.]+w)?[\\d\\.]+d)?([\\d\\.]+h)?([\\d\\.]+m)?([\\d\\.]+s)?([\\d\\.]+ms)?([\\d\\.]+us)?([\\d\\.]+ns)?",
            "examples": [
                "300ms",
                "5m",
                "2h45m",
                "72h3m0.5s",
            ],
        }


def test_adjustment_str() -> None:
    adjustment = Adjustment(component_name="web", setting_name="cpu", value=1.25)
    assert adjustment.__str__() == "web.cpu=1.25"
    assert str(adjustment) == "web.cpu=1.25"
    assert f"adjustment=({adjustment})" == "adjustment=(web.cpu=1.25)"
    assert (
        f"[adjustments=({', '.join(list(map(str, [adjustment])))})]"
        == "[adjustments=(web.cpu=1.25)]"
    )


# TODO: Move to api_test.py
def test_parse_measure_command_response_including_units() -> None:
    from typing import Union

    from pydantic import parse_obj_as

    from servo.api import CommandResponse, MeasureParams, Status

    payload = {
        "cmd": "MEASURE",
        "param": {
            "control": {
                "delay": 10,
                "warmup": 30,
                "duration": 180,
            },
            "metrics": {
                "throughput": {
                    "unit": "rpm",
                },
                "error_rate": {
                    "unit": "%",
                },
                "latency_total": {
                    "unit": "ms",
                },
                "latency_mean": {
                    "unit": "ms",
                },
                "latency_50th": {
                    "unit": "ms",
                },
                "latency_90th": {
                    "unit": "ms",
                },
                "latency_95th": {
                    "unit": "ms",
                },
                "latency_99th": {
                    "unit": "ms",
                },
                "latency_max": {
                    "unit": "ms",
                },
                "latency_min": {
                    "unit": "ms",
                },
            },
        },
    }
    obj = parse_obj_as(Union[CommandResponse, Status], payload)
    assert isinstance(obj, CommandResponse)
    assert isinstance(obj.param, MeasureParams)
    assert obj.param.metrics == [
        "throughput",
        "error_rate",
        "latency_total",
        "latency_mean",
        "latency_50th",
        "latency_90th",
        "latency_95th",
        "latency_99th",
        "latency_max",
        "latency_min",
    ]


import asyncio

import servo


class TestDurationProgress:
    @pytest.fixture
    def progress(self) -> servo.types.DurationProgress:
        return servo.types.DurationProgress()

    def test_handling_zero_duration(self, progress) -> None:
        progress.duration = Duration(0)
        assert not progress.finished
        progress.start()
        assert progress.finished

    async def test_started(self, progress) -> None:
        assert not progress.started
        progress.start()
        assert progress.started

    async def test_start_when_already_started(self, progress) -> None:
        progress.start()
        assert progress.started
        with pytest.raises(RuntimeError, match="cannot start a progress object that has already been started"):
            progress.start()


    async def test_elapsed_is_none_when_not_started(self, progress) -> None:
        assert not progress.started
        assert progress.elapsed is None

    async def test_elapsed_is_duration_when_started(self, progress) -> None:
        assert not progress.started
        assert progress.elapsed is None
        progress.start()
        assert isinstance(progress.elapsed, Duration)

    async def test_progress_is_zero_when_not_started(self, progress) -> None:
        assert not progress.started
        assert progress.progress == 0.0

    async def test_progress_is_float_when_started(self, progress) -> None:
        assert not progress.started
        assert progress.elapsed is None
        progress.start()
        assert isinstance(progress.progress, float)

    # TODO: Watch, annotate, wait...

class TestEventProgress:
    @pytest.fixture
    def progress(self) -> servo.types.EventProgress:
        return servo.types.EventProgress()

    async def test_timeout(self, progress) -> None:
        progress.timeout = Duration("3ms")
        assert not progress.started
        progress.start()
        assert progress.started
        assert not progress.finished
        await asyncio.sleep(0.3)
        assert progress.finished
        assert not progress.completed

    async def test_grace_time(self) -> None:
        ...

    async def test_start_when_already_started(self) -> None:
        ...

    async def test_started(self) -> None:
        ...

    async def test_elapsed_is_none_when_not_started(self) -> None:
        ...

    async def test_elapsed_is_duration_when_started(self) -> None:
        ...

    async def test_goes_to_100_if_gracetime_is_none(self) -> None:
        ...

    # TODO: Should this just start the count instead?
    async def test_goes_to_50_if_gracetime_is_not_none(self) -> None:
        ...

    async def test_reset_during_gracetime_sets_progress_back_to_zero(self) -> None:
        ...

    async def test_gracetime_expires_sets_progress_to_finished(self) -> None:
        ...

class TestMeasurement:
    @pytest.fixture
    def metric(self) -> Metric:
        return Metric("throughput", Unit.REQUESTS_PER_MINUTE)

    def test_rejects_empty_data_point(self, metric: Metric) -> None:
        with pytest.raises(ValueError) as e:
            readings = [DataPoint(metric, datetime.now(), None)]
            Measurement(readings=readings)
        assert e
        assert "none is not an allowed value" in str(e.value)

    def test_accepts_empty_readings(self, metric: Metric) -> None:
        Measurement(readings=[])

    def test_accepts_empty_time_series(self, metric: Metric) -> None:
        readings = [TimeSeries(metric, [])]
        Measurement(readings=readings)

    @pytest.mark.xfail
    def test_rejects_mismatched_time_series_readings(self, metric: Metric) -> None:
        readings = [
            TimeSeries(metric, [(datetime.now(), 1), (datetime.now(), 2)]),
            TimeSeries(metric, [(datetime.now(), 1), (datetime.now(), 2), (datetime.now(), 3)], id="foo")
        ]
        with pytest.raises(ValueError) as e:
            Measurement(readings=readings)
        assert e
        assert (
            'all TimeSeries readings must contain the same number of values: expected 2 values but found 3 on TimeSeries id "foo"'
            in str(e.value)
        )

    @pytest.mark.xfail
    def test_rejects_mixed_empty_and_nonempty_readings(self, metric: Metric) -> None:
        readings = [
            TimeSeries(metric, [(datetime.now(), 1), (datetime.now(), 2)]),
            TimeSeries(metric=metric, data_points=[]),
        ]
        with pytest.raises(ValueError) as e:
            Measurement(readings=readings)
        assert e
        assert (
            'all TimeSeries readings must contain the same number of values: expected 2 values but found 0 on TimeSeries id "None"'
            in str(e.value)
        )

    def test_rejects_mixed_types_of_readings(self, metric: Metric) -> None:
        readings = [
            TimeSeries(metric, [
                DataPoint(metric, datetime.now(), 1),
                DataPoint(metric, datetime.now(), 2),
            ]),
            DataPoint(metric, datetime.now(), 123),
        ]
        with pytest.raises(ValueError) as e:
            Measurement(readings=readings)
        assert e
        assert (
            'all readings must be of the same type: expected "TimeSeries" but found "DataPoint"'
            in str(e.value)
        )


class TestControl:
    def test_validation_fails_if_delay_past_do_not_agree(self) -> None:
        with pytest.raises(ValueError) as e:
            Control(past=123, delay=456)
        assert e
        assert "past and delay attributes must be equal" in str(e.value)

    def test_past_value_is_coerced_to_delay(self) -> None:
        control = Control(past=123)
        assert control.delay == Duration("2m3s")


import abc

import pydantic

from servo.types import CPU, EnumSetting, InstanceType, Memory, Numeric, RangeSetting, Replicas, Setting


class BasicSetting(Setting):
    name = "foo"
    type = "bar"

    def __opsani_repr__(self) -> dict:
        return {}


class TestSetting:
    def test_is_abstract_base_class(self) -> None:
        assert issubclass(Setting, abc.ABC)

    def test_requires_opsani_repr_implementation(self) -> None:
        assert "__opsani_repr__" in Setting.__abstractmethods__

    def test_validates_all(self) -> None:
        assert Setting.__config__.validate_all

    def test_validates_assignment(self) -> None:
        assert Setting.__config__.validate_assignment

    def test_human_readable_value(self) -> None:
        setting = BasicSetting(value="whatever")
        assert setting.human_readable_value == "whatever"

        class HumanReadableTestValue(str):
            def human_readable(self) -> str:
                return "another-value"

        setting = BasicSetting(value=HumanReadableTestValue("whatever"))
        assert setting.human_readable_value == "another-value"

    @pytest.mark.parametrize(
        ("pinned", "init_value", "new_value", "error_message"),
        [
            (True, None, None, None),
            (False, None, None, None),
            (True, None, 123, None),
            (False, None, 123, None),
            (True, 60, 60, None),
            (False, 60, 60, None),
            (True, 5.0, 5.0, None),
            (False, 5.0, 5.0, None),
            (True, "this", "this", None),
            (False, "this", "this", None),
            # Failures
            (
                True,
                1,
                None,
                "value of pinned settings cannot be changed: assigned value None is not equal to existing value 1",
            ),
            # FIXME: Type compare (True, 1, 1.0, 'value of pinned settings cannot be changed: assigned value 1.0 is not equal to existing value 1'),
            (
                True,
                5.0,
                2.5,
                "value of pinned settings cannot be changed: assigned value 2.5 is not equal to existing value 5.0",
            ),
            (
                True,
                5.0,
                3,
                "value of pinned settings cannot be changed: assigned value 3 is not equal to existing value 5.0",
            ),
            (
                True,
                1,
                "1",
                "value of pinned settings cannot be changed: assigned value '1' is not equal to existing value 1",
            ),
            (
                True,
                "something",
                "another",
                "value of pinned settings cannot be changed: assigned value 'another' is not equal to existing value 'something'",
            ),
            (
                True,
                "something",
                5.0,
                "value of pinned settings cannot be changed: assigned value 5.0 is not equal to existing value 'something'",
            ),
            # Coerced types
            (True, 5.0, 5, None),
            (True, 5, 5.0, None),
            (
                True,
                "5",
                5.0,
                "value of pinned settings cannot be changed: assigned value 5.0 is not equal to existing value '5'",
            ),
        ],
    )
    def test_validate_pinned_value_cannot_change(
        self,
        pinned: bool,
        init_value: Union[str, Numeric],
        new_value: Union[str, Numeric],
        error_message: str,
    ) -> None:
        setting = BasicSetting(value=init_value, pinned=pinned)
        if error_message is not None:
            assert pinned, "Cannot test validation for non-pinned settings"

            with pytest.raises(pydantic.ValidationError) as error:
                setting.value = new_value

            assert error
            assert "1 validation error for BasicSetting" in str(error.value)
            assert error.value.errors()[0]["loc"] == ("value",)
            assert error.value.errors()[0]["msg"] == error_message
        else:
            setting.value = new_value
            assert setting.value == new_value


class TestEnumSetting:
    @pytest.fixture
    def setting(self) -> EnumSetting:
        return EnumSetting(
            unit="unit", name="the-setting", values=["one", "two", "three"], value="two"
        )

    def test_type(self, setting: EnumSetting) -> None:
        assert setting.type == "enum"

    def test_opsani_repr(self, setting: EnumSetting) -> None:
        assert setting.__opsani_repr__() == {
            "the-setting": {
                "pinned": False,
                "type": "enum",
                "unit": "unit",
                "value": "two",
                "values": ["one", "two", "three"],
            },
        }

    def test_unit_value_are_excluded_from_opsani_repr_if_none(self) -> None:
        setting = EnumSetting(
            unit=None, name="the-setting", values=["one", "two", "three"], value=None
        )
        assert setting.__opsani_repr__() == {
            "the-setting": {
                "pinned": False,
                "type": "enum",
                "values": ["one", "two", "three"],
            },
        }

    def test_validate_type_must_be_enum(self, setting: EnumSetting) -> None:
        with pytest.raises(pydantic.ValidationError) as error:
            EnumSetting(type="foo", name="bar", values=["test"])

        assert error
        assert "1 validation error for EnumSetting" in str(error.value)
        assert error.value.errors()[0]["loc"] == ("type",)
        assert error.value.errors()[0]["type"] == "value_error.const"
        assert error.value.errors()[0]["msg"] == "unexpected value; permitted: 'enum'"

    def test_validate_values_list_is_not_empty(self) -> None:
        with pytest.raises(pydantic.ValidationError) as error:
            EnumSetting(name="bar", values=[])

        assert error
        assert "1 validation error for EnumSetting" in str(error.value)
        assert error.value.errors()[0]["loc"] == ("values",)
        assert error.value.errors()[0]["type"] == "value_error.list.min_items"
        assert (
            error.value.errors()[0]["msg"] == "ensure this value has at least 1 items"
        )

    def test_validate_value_is_included_in_values_list(self) -> None:
        with pytest.raises(pydantic.ValidationError) as error:
            EnumSetting(name="bar", values=["one", "two"], value="three")

        assert error
        assert "1 validation error for EnumSetting" in str(error.value)
        assert error.value.errors()[0]["loc"] == ("__root__",)
        assert error.value.errors()[0]["type"] == "value_error"
        assert (
            error.value.errors()[0]["msg"]
            == "invalid value: 'three' is not in the values list ['one', 'two']"
        )


class TestRangeSetting:
    def test_type(self) -> None:
        setting = RangeSetting(name="bar", min=0.0, max=1.0, step=0.1)
        assert setting.type == "range"

    def test_validate_type_must_be_enum(self) -> None:
        with pytest.raises(pydantic.ValidationError) as error:
            RangeSetting(type="foo", name="bar", min=0.0, max=1.0, step=0.1)

        assert error
        assert "1 validation error for RangeSetting" in str(error.value)
        assert error.value.errors()[0]["loc"] == ("type",)
        assert error.value.errors()[0]["type"] == "value_error.const"
        assert error.value.errors()[0]["msg"] == "unexpected value; permitted: 'range'"

    @pytest.mark.parametrize(
        ("min", "max", "step", "error_message"),
        [
            (1, 5, 1, None),
            (1.0, 5.0, 2.0, None),
            (
                1.0,
                2,
                3,
                "invalid range: min, max, and step must all be of the same Numeric type (float: min. int: max, step.)",
            ),
            (
                1,
                2.0,
                3,
                "invalid range: min, max, and step must all be of the same Numeric type (int: min, step. float: max.)",
            ),
            (
                1,
                2,
                3.0,
                "invalid range: min, max, and step must all be of the same Numeric type (int: min, max. float: step.)",
            ),
        ],
    )
    def test_validate_all_elements_of_range_are_same_type(
        self, min: Numeric, max: Numeric, step: Numeric, error_message: str
    ) -> None:
        if error_message is not None:
            with pytest.raises(pydantic.ValidationError) as error:
                RangeSetting(name="invalid", min=min, max=max, step=step)

            assert error
            assert "1 validation error for RangeSetting" in str(error.value)
            assert error.value.errors()[0]["loc"] == ("__root__",)
            assert error.value.errors()[0]["type"] == "type_error"
            assert error.value.errors()[0]["msg"] == error_message
        else:
            RangeSetting(name="valid", min=min, max=max, step=step)

    @pytest.mark.parametrize(
        ("min", "max", "step", "value", "error_message"),
        [
            (1, 3, 1, 2, None),
            (0, 10, 1, 0, None),  # Check == min
            (0, 10, 1, 10, None),  # Check == max
            (1, 5, 1, 10, "invalid value: 10 is outside of the range 1-5"),
            (1, 10, 1, 0, "invalid value: 0 is outside of the range 1-10"),
            # Float values
            (1.0, 3.0, 1.0, 2.0, None),
            (0.0, 10.0, 1.0, 0.0, None),  # Check == min
            (0.0, 10.0, 1.0, 10.0, None),  # Check == max
            (
                1.0,
                5.0,
                1.0,
                10.0,
                "invalid value: 10.0 is outside of the range 1.0-5.0",
            ),
            (
                1.0,
                10.0,
                1.0,
                0.0,
                "invalid value: 0.0 is outside of the range 1.0-10.0",
            ),
        ],
    )
    def test_value_falls_in_range(
        self,
        min: Numeric,
        max: Numeric,
        step: Numeric,
        value: Numeric,
        error_message: str,
    ) -> None:
        if error_message is not None:
            with pytest.raises(pydantic.ValidationError) as error:
                RangeSetting(name="invalid", min=min, max=max, step=step, value=value)

            assert error
            assert "1 validation error for RangeSetting" in str(error.value)
            assert error.value.errors()[0]["loc"] == ("__root__",)
            assert error.value.errors()[0]["type"] == "value_error"
            assert error.value.errors()[0]["msg"] == error_message
        else:
            RangeSetting(name="valid", min=min, max=max, step=step, value=value)

    @pytest.mark.parametrize(
        ("min", "max", "step", "error_message"),
        [
            (1, 3, 1, None),
            (0, 0, 1, "min and max cannot be equal (0 == 0)"),
            (1, 0, 1, "min cannot be greater than max (1 > 0)"),
            (1.0, 3.0, 1.0, None),
            (0.0, 0.0, 1.0, "min and max cannot be equal (0.0 == 0.0)"),
            (1.0, 0.0, 1.0, "min cannot be greater than max (1.0 > 0.0)"),
        ],
    )
    def test_max_validation(
        self, min: Numeric, max: Numeric, step: Numeric, error_message: str
    ) -> None:
        if error_message is not None:
            with pytest.raises(pydantic.ValidationError) as error:
                RangeSetting(name="invalid", min=min, max=max, step=step, value=1)

            assert error
            assert "1 validation error for RangeSetting" in str(error.value)
            assert error.value.errors()[0]["loc"] == ("max",)
            assert error.value.errors()[0]["type"] == "value_error"
            assert error.value.errors()[0]["msg"] == error_message
        else:
            RangeSetting(name="valid", min=min, max=max, step=step, value=1)

    @pytest.mark.parametrize(
        ("min", "max", "step", "value", "error_message"),
        [
            (0, 1, 1, None, None),
            (5, 10, 1, None, None),
            (-5, 10, 15, None, None),
            (1, 2, 5, None, "invalid step: adding step to min is greater than max (1 + 5 > 2)"),
            (1, 5, 5, None, "invalid step: adding step to min is greater than max (1 + 5 > 5)"),
            (1, 5, 3, 2, "invalid range: subtracting step from value is less than min (2 - 3 < 1)"),
            (1, 3, 3, 3, "invalid range: subtracting step from value is less than min (3 - 3 < 1)"),
            (1.0, 5.0, 2.0, 4.0, "invalid range: adding step to value is greater than max (4.0 + 2.0 > 5.0)"),
        ],
    )
    def test_step_and_value_validation(
        self, min: Numeric, max: Numeric, step: Numeric, value: Optional[Numeric], error_message: str
    ) -> None:
        if error_message is not None:
            with pytest.raises(pydantic.ValidationError) as error:
                RangeSetting(name="invalid", min=min, max=max, step=step, value=value)

            assert error
            assert "1 validation error for RangeSetting" in str(error.value)
            assert error.value.errors()[0]["loc"] == ("__root__",)
            assert error.value.errors()[0]["type"] == "value_error"
            assert error.value.errors()[0]["msg"] == error_message
        else:
            RangeSetting(name="valid", min=min, max=max, step=step, value=value)

    def test_validation_on_value_mutation(
        self
    ) -> None:
        setting = RangeSetting(name="range", min=0, max=10, step=1)
        with pytest.raises(pydantic.ValidationError) as error:
            setting.value = 25

        assert error
        assert "1 validation error for RangeSetting" in str(error.value)
        assert error.value.errors()[0]["loc"] == ("__root__",)
        assert error.value.errors()[0]["type"] == "value_error"
        assert error.value.errors()[0]["msg"] == "invalid value: 25 is outside of the range 0-10"

    @pytest.mark.parametrize(
        ("min", "max", "step", "error_message"),
        [
            (1, 3, 0, "step cannot be zero"),
            (1.0, 3.0, 0.0, "step cannot be zero"),
        ],
    )
    def test_step_validation(
        self, min: Numeric, max: Numeric, step: Numeric, error_message: str
    ) -> None:
        if error_message is not None:
            with pytest.raises(pydantic.ValidationError) as error:
                RangeSetting(name="invalid", min=min, max=max, step=step, value=1)

            assert error
            assert "1 validation error for RangeSetting" in str(error.value)
            assert error.value.errors()[0]["loc"] == ("step",)
            assert error.value.errors()[0]["type"] == "value_error"
            assert error.value.errors()[0]["msg"] == error_message
        else:
            RangeSetting(name="valid", min=min, max=max, step=step, value=1)

    # TODO: Step can't be zero

    def test_warning_if_not_multiple_of_step(self) -> None:
        from servo.logging import logger, reset_to_defaults

        try:
            messages = []
            logger.remove(None)
            logger.add(lambda m: messages.append(m), level=0)
            RangeSetting(name="misaligned", min=0.0, max=25.0, step=5.0, value=16.0)
            assert len(messages) == 1
            assert "WARNING" in messages[0]
            assert (
                "RangeSetting('misaligned' 0.0-25.0, 5.0) value is not step aligned: 16.0 is not divisible by 5.0"
                in messages[0]
            )
        finally:
            reset_to_defaults()


class TestCPU:
    @pytest.fixture
    def setting(self) -> CPU:
        return CPU(min=1.0, max=10.0)

    def test_is_range_setting(self, setting: CPU) -> None:
        assert isinstance(setting, RangeSetting)

    def test_default_step(self) -> None:
        assert CPU.__fields__["step"].default == 0.125

    def test_name(self) -> None:
        assert CPU.__fields__["name"].default == "cpu"

    def test_validate_name_cannot_be_changed(self) -> None:
        with pytest.raises(pydantic.ValidationError) as error:
            CPU(name="other", min=1.0, max=10.0)

        assert error
        assert "1 validation error for CPU" in str(error.value)
        assert error.value.errors()[0]["loc"] == ("name",)
        assert error.value.errors()[0]["type"] == "value_error.const"
        assert error.value.errors()[0]["msg"] == "unexpected value; permitted: 'cpu'"

    def test_validate_min_cant_be_zero(self) -> None:
        with pytest.raises(pydantic.ValidationError) as error:
            CPU(min=0.0, max=10.0)

        assert error
        assert "1 validation error for CPU" in str(error.value)
        assert error.value.errors()[0]["loc"] == ("min",)
        assert error.value.errors()[0]["type"] == "value_error.number.not_gt"
        assert error.value.errors()[0]["msg"] == "ensure this value is greater than 0"


class TestMemory:
    def test_is_range_setting(self) -> None:
        assert issubclass(Memory, RangeSetting)

    def test_validate_name_cannot_be_changed(self) -> None:
        with pytest.raises(pydantic.ValidationError) as error:
            Memory(name="other", min=1.0, max=10.0, step=1.0)

        assert error
        assert "1 validation error for Memory" in str(error.value)
        assert error.value.errors()[0]["loc"] == ("name",)
        assert error.value.errors()[0]["type"] == "value_error.const"
        assert error.value.errors()[0]["msg"] == "unexpected value; permitted: 'mem'"

    def test_validate_min_cant_be_zero(self) -> None:
        with pytest.raises(pydantic.ValidationError) as error:
            Memory(min=0.0, max=10.0, step=1.0)

        assert error
        assert "1 validation error for Memory" in str(error.value)
        assert error.value.errors()[0]["loc"] == ("min",)
        assert error.value.errors()[0]["type"] == "value_error"
        assert error.value.errors()[0]["msg"] == "min must be greater than zero"


class TestReplicas:
    def test_is_range_setting(self) -> None:
        assert issubclass(Replicas, RangeSetting)

    @pytest.mark.parametrize(
        ("field_name", "required", "allow_none"),
        [
            ("min", True, False),
            ("max", True, False),
            ("step", False, False),
            ("value", False, True),
        ],
    )
    def test_range_fields_strict_integers(
        self, field_name: str, required: bool, allow_none: bool
    ) -> None:
        field = Replicas.__fields__[field_name]
        assert field.type_ == pydantic.StrictInt
        assert field.required == required
        assert field.allow_none == allow_none

    def test_validate_name_cannot_be_changed(self) -> None:
        with pytest.raises(pydantic.ValidationError) as error:
            Replicas(name="other", min=1, max=10)

        assert error
        assert "1 validation error for Replicas" in str(error.value)
        assert error.value.errors()[0]["loc"] == ("name",)
        assert error.value.errors()[0]["type"] == "value_error.const"
        assert (
            error.value.errors()[0]["msg"] == "unexpected value; permitted: 'replicas'"
        )


class TestInstanceType:
    def test_is_enum_setting(self) -> None:
        assert issubclass(InstanceType, EnumSetting)

    def test_validate_name_cannot_be_changed(self) -> None:
        with pytest.raises(pydantic.ValidationError) as error:
            InstanceType(
                name="other", values=["this", "that"], unit=InstanceTypeUnits.EC2
            )

        assert error
        assert "1 validation error for InstanceType" in str(error.value)
        assert error.value.errors()[0]["loc"] == ("name",)
        assert error.value.errors()[0]["type"] == "value_error.const"
        assert (
            error.value.errors()[0]["msg"] == "unexpected value; permitted: 'inst_type'"
        )

    def test_validate_unit(self) -> None:
        field = InstanceType.__fields__["unit"]
        assert field.type_ == InstanceTypeUnits
        assert field.default == InstanceTypeUnits.EC2
        assert field.required == False
        assert field.allow_none == False

class TestTimeSeries:
    @pytest.fixture
    @freezegun.freeze_time("2020-01-21 12:00:01", auto_tick_seconds=600)
    def time_series(self) -> DataPoint:
        metric = Metric("throughput", Unit.REQUESTS_PER_MINUTE)
        values = (31337.0, 666.0, 187.0, 420.0, 69.0)
        points = list(map(lambda v: DataPoint(metric, datetime.now(), v), values))
        return TimeSeries(metric, points)

    def test_len(self, time_series: TimeSeries) -> None:
        assert len(time_series) == 5

    def test_iteration(self, time_series: TimeSeries) -> None:
        assert list(map(lambda p: repr(p), iter(time_series))) == [
            "DataPoint(throughput (rpm), (2020-01-21 12:00:01, 31337.0))",
            "DataPoint(throughput (rpm), (2020-01-21 12:10:01, 666.0))",
            "DataPoint(throughput (rpm), (2020-01-21 12:20:01, 187.0))",
            "DataPoint(throughput (rpm), (2020-01-21 12:30:01, 420.0))",
            "DataPoint(throughput (rpm), (2020-01-21 12:40:01, 69.0))",
        ]

    def test_indexing(self, time_series: TimeSeries) -> None:
        assert repr(time_series[2]) == "DataPoint(throughput (rpm), (2020-01-21 12:20:01, 187.0))"

    def test_min(self, time_series: TimeSeries) -> None:
        assert repr(time_series.min) == "DataPoint(throughput (rpm), (2020-01-21 12:40:01, 69.0))"

    def test_max(self, time_series: TimeSeries) -> None:
        assert repr(time_series.max) == "DataPoint(throughput (rpm), (2020-01-21 12:00:01, 31337.0))"

    def test_timespan(self, time_series: TimeSeries) -> None:
        assert time_series.timespan == (
            datetime(2020, 1, 21, 12, 0, 1),
            datetime(2020, 1, 21, 12, 40, 1)
        )

    def test_duration(self, time_series: TimeSeries) -> None:
        assert time_series.duration == Duration('40m')

    def test_sorting(self, time_series: TimeSeries) -> None:
        points = list(reversed(time_series))
        assert points[0].time > points[-1].time
        new_time_series = TimeSeries(time_series.metric, points)
        # validator will sort it back into time series
        assert new_time_series.data_points == time_series.data_points
        assert new_time_series.data_points[0].time < new_time_series.data_points[-1].time

    def test_repr(self, time_series: TimeSeries) -> None:
        assert repr(time_series) == "TimeSeries(metric=Metric(name='throughput', unit=<Unit.REQUESTS_PER_MINUTE: 'rpm'>), data_points=[DataPoint(throughput (rpm), (2020-01-21 12:00:01, 31337.0)), DataPoint(throughput (rpm), (2020-01-21 12:10:01, 666.0)), DataPoint(throughput (rpm), (2020-01-21 12:20:01, 187.0)), DataPoint(throughput (rpm), (2020-01-21 12:30:01, 420.0)), DataPoint(throughput (rpm), (2020-01-21 12:40:01, 69.0))], id=None, annotation=None, metadata=None, timespan=(FakeDatetime(2020, 1, 21, 12, 0, 1), FakeDatetime(2020, 1, 21, 12, 40, 1)), duration=Duration('40m'))"

class TestDataPoint:
    @pytest.fixture
    @freezegun.freeze_time("2020-01-21 12:00:01")
    def data_point(self) -> DataPoint:
        metric = Metric("throughput", Unit.REQUESTS_PER_MINUTE)
        return DataPoint(metric, datetime.now(), 31337.0)

    def test_iteration(self, data_point: DataPoint) -> None:
        assert tuple(iter(data_point)) == (datetime(2020, 1, 21, 12, 0, 1), 31337.0)

    def test_indexing(self, data_point: DataPoint) -> None:
        assert data_point[0] == datetime(2020, 1, 21, 12, 0, 1)
        assert data_point[1] == 31337.0
        with pytest.raises(KeyError, match='index out of bounds: 3 not in \\(0, 1\\)'):
            data_point[3]

    def test_str(self, data_point: DataPoint) -> None:
        assert str(data_point) == 'throughput: 31337.00rpm @ 2020-01-21 12:00:01'

    def test_repr(self, data_point: DataPoint) -> None:
        assert repr(data_point) == "DataPoint(throughput (rpm), (2020-01-21 12:00:01, 31337.0))"
