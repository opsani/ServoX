import abc
import collections
import random
from typing import List, Optional, Union

import fastapi
import statesman

import servo


class StateMachine(statesman.HistoryMixin, statesman.StateMachine):
    class States(statesman.StateEnum):
        ready = statesman.InitialState("Ready")
        analyzing = "Analyzing"

        awaiting_description = "Awaiting Description" # issued a DESCRIBE, waiting for description
        awaiting_measurement = "Awaiting Measurement" # issued a MEASURE, waiting for measurement
        awaiting_adjustment = "Awaiting Adjustment" # issued an ADJUST, waiting for confirmation

        done = "Done"
        failed = "Failed"

    description: Optional[servo.Description] = None
    connected: bool = False
    progress: Optional[int] = None
    error: Optional[Exception] = None
    command_response: Optional[servo.api.CommandResponse] = None

    @property
    def command(self) -> servo.api.Commands:
        """Return the command for the current state."""
        if self.state == StateMachine.States.awaiting_description:
            return servo.api.Commands.describe
        elif self.state == StateMachine.States.awaiting_measurement:
            return servo.api.Commands.measure
        elif self.state == StateMachine.States.awaiting_adjustment:
            return servo.api.Commands.adjust
        elif self.state in (StateMachine.States.done, StateMachine.States.failed):
            return servo.api.Commands.sleep
        else:
            servo.logging.logger.error(f"in non-operational state ({self.state}): cannot command servo meaningfully")
            return servo.api.Commands.sleep

    @statesman.enter_state(States.ready)
    async def _enter_ready(self) -> None:
        self.clear_history()
        self.description = None
        self.connected = False
        self.progress = None
        self.error = None
        self.command_response = None

    @statesman.enter_state(States.analyzing)
    async def _enter_analyzing(self) -> None:
        self.progress = None
        self.command_response = None

    @statesman.enter_state(States.awaiting_description)
    async def _enter_awaiting_description(self) -> None:
        self.command_response = servo.api.CommandResponse(cmd=servo.api.Commands.describe, param={})

    @statesman.enter_state(States.awaiting_measurement)
    async def _enter_awaiting_measurement(self, metrics: List[servo.Metric] = [], control: servo.Control = servo.Control()) -> None:
        self.command_response = servo.api.CommandResponse(
            cmd=servo.api.Commands.measure,
            param=servo.api.MeasureParams(metrics=metrics, control=control)
        )

    @statesman.enter_state(States.awaiting_adjustment)
    async def _enter_awaiting_adjustment(self, adjustments: List[servo.types.Adjustment] = []) -> None:
        self.command_response = servo.api.CommandResponse(
            cmd=servo.api.Commands.adjust,
            param=servo.api.adjustments_to_descriptor(adjustments)
        )

    @statesman.exit_state([States.awaiting_measurement, States.awaiting_adjustment])
    async def _exiting_operation(self) -> None:
        self.command_response = None

    @statesman.enter_state([States.done, States.failed])
    async def _sleep(self) -> None:
        self.command_response = servo.api.CommandResponse(
            cmd=servo.api.Commands.sleep,
            param={"duration": 60, "data": {"reason": "no active optimization pipeline"}}
        )

    @statesman.event(States.__any__, States.ready)
    async def reset(self) -> None:
        """Reset the Optimizer to an initial ready state."""
        servo.logging.logger.info("Resetting Optimizer")

    @statesman.event(States.__any__, States.__active__, transition_type=statesman.Transition.Types.internal)
    async def say_hello(self) -> servo.api.Status:
        """Say hello to a servo that has connected and greeted us.

        A servo saying hello toggles the connected state to True.
        """
        servo.logging.logger.info("Saying Hello")
        self.connected = True
        return servo.api.Status.ok()

    @statesman.event(States.__any__, States.__active__, transition_type=statesman.Transition.Types.internal)
    async def ask_whats_next(self) -> servo.api.CommandResponse:
        """Answer an inquiry about what the next command to be executed is."""
        servo.logging.logger.info(f"Asking What's Next? => {self.command}: {self.command_response}")
        return self.command_response or servo.api.CommandResponse(cmd=self.command, param={})

    @statesman.event(States.__any__, States.__active__, transition_type=statesman.Transition.Types.internal)
    async def say_goodbye(self) -> servo.api.Status:
        """Say goodbye to a servo that is disconnecting and has bid us farewell.

        A servo saying goodbye toggles the connected state to False.
        """
        servo.logging.logger.info("Saying Goodbye")
        self.connected = False
        return servo.api.Status.ok()

    async def _guard_progress_tracking(self, *, progress: Optional[int] = None) -> bool:
        if isinstance(progress, int) and progress < 100:
            if self.progress and progress < self.progress:
                raise ValueError(f"progress cannot go backward: new progress value of {progress} is less than existing progress value of {self.progress}")
            self.progress = progress
            return False

        return True

    ##
    # Describe

    @statesman.event([States.ready, States.analyzing], States.awaiting_description)
    async def request_description(self) -> None:
        """Request a Description of application state from the servo."""
        servo.logging.logger.info("Requesting Description")

    async def _validate_description(self, description: servo.Description) -> None:
        servo.logging.logger.info(f"Validating Description: {description}")

    @statesman.event(States.awaiting_description, States.analyzing, guard=_validate_description)
    async def submit_description(self, description: servo.Description) -> servo.api.Status:
        """Submit a Description to the optimizer for analysis."""
        servo.logging.logger.info(f"Received Description: {description}")
        self.description = description
        return servo.api.Status.ok()

    ##
    # Measure

    @statesman.event([States.ready, States.analyzing], States.awaiting_measurement)
    async def request_measurement(self, metrics: List[servo.Metric], control: servo.Control) -> None:
        """Request a Measurement from the servo."""
        servo.logging.logger.info(f"Requesting Measurement ({metrics}, {control})")

    async def _validate_measurement(self, measurement: servo.Measurement) -> None:
        servo.logging.logger.info(f"Validating Measurement: {measurement}")

    @statesman.event(States.awaiting_measurement, States.analyzing, guard=[_guard_progress_tracking, _validate_measurement])
    async def submit_measurement(self, measurement: servo.Measurement) -> servo.api.Status:
        """Submit a Measurement to the optimizer for analysis."""
        servo.logging.logger.info(f"Received Measurement: {measurement}")
        return servo.api.Status.ok()

    ##
    # Adjust

    @statesman.event([States.ready, States.analyzing], States.awaiting_adjustment)
    async def recommend_adjustments(self, adjustments: List[servo.types.Adjustment]) -> None:
        """Recommend Adjustments to the Servo."""
        servo.logging.logger.info(f"Recommending Adjustments ({adjustments}")

    async def _validate_adjustments(self, description: servo.Description) -> None:
        servo.logging.logger.info(f"Validating Adjustment: {description}")

    @statesman.event(States.awaiting_adjustment, States.analyzing, guard=[_guard_progress_tracking, _validate_adjustments])
    async def complete_adjustments(self, description: servo.Description) -> servo.api.Status:
        """Complete Adjustment."""
        servo.logging.logger.info(f"Adjustment Completed: {description}")
        return servo.api.Status.ok()

    ##
    # Terminal transitions

    @statesman.event(States.__any__, States.failed)
    async def fail(self, error: Exception) -> None:
        """Fail the Optimization."""
        servo.logging.logger.info(f"Optimization failed: {error}")
        self.error = error

    @statesman.event(States.__any__, States.done)
    async def done(self) -> None:
        """Complete the Optimization."""
        servo.logging.logger.info("Optimization completed")

    class Config:
        arbitrary_types_allowed = True


class AbstractOptimizer(StateMachine, abc.ABC):
    id: str
    token: str
    _queue: collections.deque = collections.deque()

    def append(self, item) -> None:
        self._queue.append(item)

    def extend(self, items) -> None:
        self._queue.extend(items)

    @abc.abstractmethod
    async def next_transition(self, *args, **kwargs) -> Optional[statesman.Transition]:
        """Advance the optimizer to the next state."""
        ...

class StaticOptimizer(AbstractOptimizer):
    """A fake optimizer that requires manual state changes."""

    async def next_transition(self, *args, **kwargs) -> Optional[statesman.Transition]:
        return None

class SequencedOptimizer(statesman.SequencingMixin, AbstractOptimizer):
    """A fake optimizer that executes state transitions in a specific order."""

    async def next_transition(self, *args, **kwargs) -> Optional[statesman.Transition]:
        return await super().next_transition(*args, **kwargs)

    async def after_transition(self, transition: statesman.Transition) -> None:
        if self.state == StateMachine.States.analyzing:
            if not await self.next_transition():
                # No more states -- finish optimization
                await self.done()

class RandomOptimizer(AbstractOptimizer):
    """A fake optimizer that executes state transitions in random order."""

    async def next_transition(self, *args, **kwargs) -> Optional[statesman.Transition]:
        if not self._queue:
            return None

        transitionable = random.choice(self._queue)
        self._queue.remove(transitionable)
        return await transitionable

class ChaosOptimizer(AbstractOptimizer):
    """A fake optimizer that generates chaos.

    Provides resilience testing through chaos such as non-sensical metrics,
    invalid adjustment values, etc.
    """

    async def next_transition(self, *args, **kwargs) -> Optional[statesman.Transition]:
        pass





#########

class OpsaniAPI(fastapi.FastAPI):
    optimizer: Optional[AbstractOptimizer] = None

    # TODO: Update for multi-servo?

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
