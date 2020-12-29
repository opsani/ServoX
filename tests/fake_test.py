import datetime
import pathlib
import random
from typing import Optional, Union

import fastapi
import pytest

import servo
import servo.runner
import tests.fake
from tests.fake import AbstractOptimizer


@pytest.mark.parametrize(
    ("state", "expected_command"),
    [
        (tests.fake.StateMachine.States.ready, servo.api.Commands.sleep),
        (tests.fake.StateMachine.States.analyzing, servo.api.Commands.sleep),
        (tests.fake.StateMachine.States.awaiting_description, servo.api.Commands.describe),
        (tests.fake.StateMachine.States.awaiting_measurement, servo.api.Commands.measure),
        (tests.fake.StateMachine.States.awaiting_adjustment, servo.api.Commands.adjust),
        (tests.fake.StateMachine.States.done, servo.api.Commands.sleep),
        (tests.fake.StateMachine.States.failed, servo.api.Commands.sleep),
    ]
)
async def test_command(state: tests.fake.StateMachine.States, expected_command: Optional[servo.api.Commands]) -> None:
    state_machine = await tests.fake.StateMachine.create(state=state)
    assert state_machine.command == expected_command, f"Expected command of {expected_command} but found {state_machine.command}"

async def test_submit_description_stores_description() -> None:
    description = _random_description()
    state_machine = await tests.fake.StateMachine.create(state=tests.fake.StateMachine.States.awaiting_description)
    await state_machine.submit_description(description)
    assert state_machine.description == description

async def test_whats_next_returns_command_response() -> None:
    state_machine = await tests.fake.StateMachine.create(state=tests.fake.StateMachine.States.awaiting_description)
    response = await state_machine.ask_whats_next()
    assert response.command == servo.api.Commands.describe
    assert response.param == {}
    assert state_machine.command == servo.api.Commands.describe

@pytest.fixture()
def state_machine() -> tests.fake.StateMachine:
    return tests.fake.StateMachine()

@pytest.fixture()
def measurement() -> servo.Measurement:
    return servo.Measurement(
        readings=[
            servo.DataPoint(
                value=31337,
                time=datetime.datetime.now(),
                metric=servo.Metric(
                    name="Some Metric",
                    unit=servo.Unit.REQUESTS_PER_MINUTE,
                )
            )
        ]
    )

@pytest.mark.parametrize(
    ("initial_state", "event", "progress", "ending_state"),
    [
        (tests.fake.StateMachine.States.awaiting_measurement, "submit_measurement", 0, tests.fake.StateMachine.States.awaiting_measurement),
        (tests.fake.StateMachine.States.awaiting_measurement, "submit_measurement", None, tests.fake.StateMachine.States.analyzing),
        (tests.fake.StateMachine.States.awaiting_measurement, "submit_measurement", 35, tests.fake.StateMachine.States.awaiting_measurement),
        (tests.fake.StateMachine.States.awaiting_measurement, "submit_measurement", 100, tests.fake.StateMachine.States.analyzing),

        (tests.fake.StateMachine.States.awaiting_adjustment, "complete_adjustments", 0, tests.fake.StateMachine.States.awaiting_adjustment),
        (tests.fake.StateMachine.States.awaiting_adjustment, "complete_adjustments", None, tests.fake.StateMachine.States.analyzing),
        (tests.fake.StateMachine.States.awaiting_adjustment, "complete_adjustments", 35, tests.fake.StateMachine.States.awaiting_adjustment),
        (tests.fake.StateMachine.States.awaiting_adjustment, "complete_adjustments", 100, tests.fake.StateMachine.States.analyzing),
    ]
)
async def test_progress_tracking(state_machine: tests.fake.StateMachine, measurement: servo.Measurement, initial_state, event, progress, ending_state) -> None:
    await state_machine.enter_state(initial_state)
    await state_machine.trigger_event(event, measurement, progress=progress)
    assert state_machine.state == ending_state

async def test_progress_cant_go_backwards(state_machine: tests.fake.StateMachine, measurement: servo.Measurement) -> None:
    await state_machine.enter_state(tests.fake.StateMachine.States.awaiting_measurement)
    await state_machine.submit_measurement(measurement, progress=45)
    with pytest.raises(ValueError, match="progress cannot go backward: new progress value of 22 is less than existing progress value of 45"):
        await state_machine.submit_measurement(measurement, progress=22)

async def test_static_optimizer() -> None:
    static_optimizer = tests.fake.StaticOptimizer(id='dev.opsani.com/big-in-japan', token='31337')
    assert len(static_optimizer.events), "should not be empty"

async def test_hello_and_describe(
    servo_runner: servo.runner.ServoRunner,
    fakeapi_url: str,
    fastapi_app: 'OpsaniAPI',
) -> None:
    static_optimizer = tests.fake.StaticOptimizer(id='dev.opsani.com/big-in-japan', token='31337')
    fastapi_app.optimizer = static_optimizer
    servo_runner.servo.optimizer.base_url = fakeapi_url

    assert static_optimizer.state == tests.fake.StateMachine.States.ready
    await static_optimizer.say_hello(dict(agent=servo.api.USER_AGENT))
    assert static_optimizer.state == tests.fake.StateMachine.States.ready

    response = await servo_runner._post_event(
        servo.api.Events.hello, dict(agent=servo.api.USER_AGENT)
    )
    assert response.status == "ok"

    # manually advance to describe
    await static_optimizer.request_description()
    assert static_optimizer.state == tests.fake.StateMachine.States.awaiting_description

    # get a description from the servo
    description = await servo_runner.describe()
    param = dict(descriptor=description.__opsani_repr__(), status="ok")
    response = await servo_runner._post_event(servo.api.Events.describe, param)
    assert response.status == "ok"

    # description has been accepted and state machine has transitioned into analyzing
    assert static_optimizer.state == tests.fake.StateMachine.States.analyzing

def test_adjustments_to_descriptor() -> None:
    adjustment1 = servo.Adjustment(component_name="web", setting_name="cpu", value=1.25)
    adjustment2 = servo.Adjustment(component_name="web", setting_name="mem", value=5.0)
    adjustment3 = servo.Adjustment(component_name="db", setting_name="mem", value=4.0)
    descriptor = servo.api.adjustments_to_descriptor([adjustment1, adjustment2, adjustment3])
    assert descriptor == {
        'state': {
            'application': {
                'components': {
                    'web': {
                        'settings': {
                            'cpu': {
                                'value': '1.25',
                            },
                            'mem': {
                                'value': '5.0',
                            },
                        },
                    },
                    'db': {
                        'settings': {
                            'mem': {
                                'value': '4.0',
                            },
                        },
                    },
                },
            },
        },
    }

async def test_state_machine_lifecyle(measurement: servo.Measurement) -> None:
    static_optimizer = tests.fake.StaticOptimizer(id='dev.opsani.com/big-in-japan', token='31337')
    await static_optimizer.say_hello()

    await static_optimizer.request_description()
    await static_optimizer.submit_description(_random_description())

    metric = servo.Metric(
        name="Some Metric",
        unit=servo.Unit.REQUESTS_PER_MINUTE,
    )
    await static_optimizer.request_measurement(metrics=[metric], control=servo.Control())
    await static_optimizer.submit_measurement(measurement)

    adjustment = servo.Adjustment(component_name="web", setting_name="cpu", value=1.25)
    await static_optimizer.recommend_adjustments([adjustment])
    await static_optimizer.complete_adjustments(_random_description())

    await static_optimizer.say_goodbye()

@pytest.fixture()
def assembly(servo_yaml: pathlib.Path) -> servo.assembly.Assembly:
    config_model = servo.assembly._create_config_model_from_routes(
        {
            "adjust": tests.helpers.AdjustConnector,
        }
    )
    config = config_model.generate()
    servo_yaml.write_text(config.yaml())

    optimizer = servo.Optimizer(
        id="dev.opsani.com/blake-ignite",
        token="bfcf94a6e302222eed3c73a5594badcfd53fef4b6d6a703ed32604",
    )
    assembly_ = servo.assembly.Assembly.assemble(
        config_file=servo_yaml, optimizer=optimizer
    )
    return assembly_


@pytest.fixture
def assembly_runner(assembly: servo.Assembly) -> servo.runner.AssemblyRunner:
    """Return an unstarted assembly runner."""
    return servo.runner.AssemblyRunner(assembly)

@pytest.fixture
async def servo_runner(assembly: servo.Assembly) -> servo.runner.ServoRunner:
    """Return an unstarted servo runner."""
    return servo.runner.ServoRunner(assembly.servos[0])


#####


class OpsaniAPI(fastapi.FastAPI):
    optimizer: Optional[AbstractOptimizer] = None

api = OpsaniAPI()

@api.post("/accounts/{account}/applications/{app}/servo")
async def servo_get(account: str, app: str, ev: servo.api.Request) -> Union[servo.api.Status, servo.api.CommandResponse]:
    assert api.optimizer, "an optimizer must be assigned to the OpsaniAPI instance"
    if ev.event == servo.api.Events.hello:
        return await api.optimizer.say_hello()
    elif ev.event == servo.api.Events.goodbye:
        return await api.optimizer.say_goodbye()
    elif ev.event == servo.api.Events.whats_next:
        return await api.optimizer.ask_whats_next()
    elif ev.event == servo.api.Events.describe:
        return await api.optimizer.submit_description(ev.param)
    elif ev.event == servo.api.Events.measure:
        return await api.optimizer.submit_measurement(ev.param)
    elif ev.event == servo.api.Events.adjust:
        return await api.optimizer.complete_adjustments(ev.param)
    else:
        raise ValueError(f"unknown event: {ev.event}")

@pytest.fixture
def fastapi_app() -> fastapi.FastAPI:
    return api

##
# Utilities

METRICS = [
    servo.Metric("throughput", servo.Unit.REQUESTS_PER_MINUTE),
    servo.Metric("error_rate", servo.Unit.PERCENTAGE),
]

COMPONENTS = [
    servo.Component(
        "fake-app",
        settings=[
            servo.CPU(
                min=1,
                max=5
            ),
            servo.Memory(
                min=0.25,
                max=8.0,
                step=0.125
            ),
            servo.Replicas(
                min=1,
                max=10
            )
        ]
    )
]

def _random_value_for_setting(setting: servo.Setting) -> Union[str, servo.Numeric]:
    if isinstance(setting, servo.RangeSetting):
        max = int((setting.max - setting.min) / setting.step)
        return random.randint(0, max) * setting.step + setting.min
    elif isinstance(setting, servo.EnumSetting):
        return random.choice(setting.values)
    else:
        raise ValueError(f"unexpected setting: {repr(setting)}")

def _random_description() -> servo.Description:
    components = COMPONENTS.copy()
    metrics = METRICS.copy()

    for component in components:
        for setting in component.settings:
            setting.value = _random_value_for_setting(setting)

    return servo.Description(metrics=metrics, components=components)
