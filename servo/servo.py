"""The servo module is responsible for interacting with the Opsani optimizer API."""
from __future__ import annotations

import asyncio
import contextvars
import enum
import json
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple, Union

import httpx
import pydantic

import servo.api
import servo.checks
import servo.configuration
import servo.connector
import servo.events
import servo.types
import servo.utilities
import servo.utilities.pydantic

_servo_context_var = contextvars.ContextVar("servo.Servo.current", default=None)


class Events(str, enum.Enum):
    """An enumeration of the names of standard events defined by the servo."""

    # Lifecycle events
    startup = "startup"
    shutdown = "shutdown"

    # Informational events
    metrics = "metrics"
    components = "components"

    # Operational events
    check = "check"
    describe = "describe"
    measure = "measure"
    adjust = "adjust"
    promote = "promote"


class _EventDefinitions(Protocol):
    """Defines the default events. This class is declarative and is never directly referenced.

    The event signature is inferred from the decorated function.
    """

    # Lifecycle events
    @servo.events.event(Events.startup)
    async def startup(self) -> None:
        ...

    @servo.events.event(Events.shutdown)
    async def shutdown(self) -> None:
        ...

    # Informational events
    @servo.events.event(Events.metrics)
    async def metrics(self) -> List[servo.types.Metric]:
        ...

    @servo.events.event(Events.components)
    async def components(self) -> List[servo.types.Component]:
        ...

    # Operational events
    @servo.events.event(Events.measure)
    async def measure(
        self,
        *,
        metrics: List[str] = None,
        control: servo.types.Control = servo.types.Control(),
    ) -> servo.types.Measurement:
        if control.delay:
            await asyncio.sleep(control.delay.total_seconds())
        yield

    @servo.events.event(Events.check)
    async def check(
        self,
        matching: Optional[servo.checks.CheckFilter],
        halt_on: Optional[
            servo.types.ErrorSeverity
        ] = servo.types.ErrorSeverity.critical,
    ) -> List[servo.checks.Check]:
        ...

    @servo.events.event(Events.describe)
    async def describe(self) -> servo.types.Description:
        ...

    @servo.events.event(Events.adjust)
    async def adjust(
        self,
        adjustments: List[servo.types.Adjustment],
        control: servo.types.Control = servo.types.Control(),
    ) -> servo.types.Description:
        ...

    @servo.events.event(Events.promote)
    async def promote(self) -> None:
        ...


_servo_context_var = contextvars.ContextVar("servo.servo", default=None)


class ServoChecks(servo.checks.BaseChecks):
    """Check that a servo is ready to perform optimization.

    Args:
        servo: The servo to be checked.
    """

    async def check_connectivity(self) -> Tuple[bool, str]:
        """Check that the servo has connectivity to the Opsani API.

        Returns:
            A tuple value containing a boolean that indicates if connectivity is
            available and an advisory string describing the status encountered.
        """
        async with self.api_client() as client:
            event_request = servo.api.Request(event=servo.api.Events.hello)
            response = await client.post("servo", data=event_request.json())
            success = response.status_code == httpx.codes.OK
            return (success, f"Response status code: {response.status_code}")


@servo.connector.metadata(
    description="Continuous Optimization Orchestrator",
    homepage="https://opsani.com/",
    maturity=servo.types.Maturity.ROBUST,
    license=servo.types.License.APACHE2,
    version=servo.__version__,
    cryptonym=servo.__cryptonym__,
)
class Servo(servo.connector.BaseConnector):
    """A connector that interacts with the Opsani API to perform optimization.

    The `Servo` is a core object of the `servo` package. It manages a set of
    connectors that provide integration and interactivity to external services
    such as metrics collectors, orchestation systems, load generators, etc. The
    Servo acts primarily as an event gateway between the Opsani API and its child
    connectors.

    Servo objects are configured with a dynamically created class that is built by
    the `servo.Assembly` class. Servo objects are typically not created directly
    and are instead built through the `Assembly.assemble` method.
    """

    config: servo.configuration.BaseServoConfiguration
    """Configuration of the Servo assembly.

    Note that the configuration is built dynamically at Servo assembly time.
    The concrete type is created in `Assembly.assemble()` and adds a field for each active
    connector.
    """

    connectors: List[servo.connector.BaseConnector]
    """The active connectors in the Servo.
    """

    @staticmethod
    def current() -> Optional["Servo"]:
        """Return the active servo for the current execution context.

        The value is managed by a contextvar and is concurrency safe.
        """
        return _servo_context_var.get(None)

    @staticmethod
    def set_current(servo_: "Servo") -> contextvars.Token:
        """Set the current servo execution context.

        Returns:
            A Token object object that can be used for restoring the previously active servo.
        """
        return _servo_context_var.set(servo_)

    async def dispatch_event(self, *args, **kwargs) -> Union[Optional[servo.events.EventResult], List[servo.events.EventResult]]:
        prev_servo_token = _servo_context_var.set(self)
        try:
            results = await super().dispatch_event(*args, **kwargs)
        finally:
            _servo_context_var.reset(prev_servo_token)

        return results


    def __init__(
        self, *args, connectors: List[servo.connector.BaseConnector], **kwargs
    ) -> None: # noqa: D107
        super().__init__(*args, connectors=[], **kwargs)

        # Ensure the connectors refer to the same objects by identity (required for eventing)
        self.connectors.extend(connectors)

        # associate shared config with our children
        for connector in (connectors + [self]):
            connector._servo_config = self.config.servo

    @pydantic.root_validator()
    def _initialize_name(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if values["name"] == "servo":
            values["name"] = values["config"].name or values["optimizer"].id

        return values

    @property
    def connector(self) -> Optional[servo.connector.BaseConnector]:
        """Return the active connector in the current execution context."""
        return servo.events._connector_context_var.get()

    @property
    def event(self) -> Optional[servo.events.Event]:
        """Return the active event in the current execution context."""
        return servo.events._event_context_var.get()

    async def startup(self):
        """Notify all active connectors that the servo is starting up."""
        await self.dispatch_event(Events.startup, _prepositions=servo.events.Preposition.on)

    async def shutdown(self):
        """Notify all active connectors that the servo is shutting down."""
        await self.dispatch_event(Events.shutdown, _prepositions=servo.events.Preposition.on)

    @property
    def all_connectors(self) -> List[servo.connector.BaseConnector]:
        """Return a list of all active connectors including the Servo."""
        return [self, *self.connectors]

    def get_connector(
        self, name: Union[str, Sequence[str]]
    ) -> Optional[Union[servo.connector.BaseConnector, List[servo.connector.BaseConnector]]]:
        """Return one or more connectors by name.

        This is a convenience method equivalent to iterating `connectors` and comparing by name.

        When given a single name, returns the connector or `None` if not found.
        When given a sequence of names, returns a list of Connectors for all connectors found.
        """
        if isinstance(name, str):
            for connector in self.connectors:
                if connector.name == name:
                    return connector
            return None
        else:
            connectors = []
            for connector in self.connectors:
                if connector.name == name:
                    connectors.append(connector)
            return connectors

    async def add_connector(
        self, name: str, connector: servo.connector.BaseConnector
    ) -> None:
        """Add a connector to the servo.

        The connector is added to the servo event bus and is initialized with
        the startup event to prepare for execution.

        Args:
            name: A unique name for the connector in the servo.
            connector: The connector to be added to the servo.

        Raises:
            ValueError: Raised if the name is not unique in the servo.
        """
        if self.get_connector(name):
            raise ValueError(
                f"invalid name: a connector named '{name}' already exists in the servo"
            )

        connector.name = name
        connector._servo_config = self.config.servo
        self.connectors.append(connector)
        self.__connectors__.append(connector)

        # Register our name into the config class
        with servo.utilities.pydantic.extra(self.config):
            setattr(self.config, name, connector.config)

        await self.dispatch_event(
            Events.startup, include=[connector], _prepositions=servo.events.Preposition.on
        )

    async def remove_connector(
        self, connector: Union[str, servo.connector.BaseConnector]
    ) -> None:
        """Remove a connector from the servo.

        The connector is removed from the servo event bus and is finalized with
        the shutdown event to prepare for eviction.

        Args:
            connector: The connector or name to remove from the servo.

        Raises:
            ValueError: Raised if the connector does not exist in the servo.
        """
        connector_ = (
            connector
            if isinstance(connector, servo.connector.BaseConnector)
            else self.get_connector(connector)
        )
        if not connector_ in self.connectors:
            name = connector_.name if connector_ else connector
            raise ValueError(
                f"invalid connector: a connector named '{name}' does not exist in the servo"
            )

        await self.dispatch_event(
            Events.shutdown, include=[connector_], _prepositions=servo.events.Preposition.on
        )

        self.connectors.remove(connector_)
        self.__connectors__.remove(connector_)

        with servo.utilities.pydantic.extra(self.config):
            delattr(self.config, connector_.name)

    def top_level_schema(self, *, all: bool = False) -> Dict[str, Any]:
        """Return a schema that only includes connector model definitions"""
        connectors = servo.Assembly.all_connector_types() if all else self.connectors
        config_models = list(map(lambda c: c.config_model(), connectors))
        return pydantic.schema.schema(config_models, title="Servo Schema")

    def top_level_schema_json(self, *, all: bool = False) -> str:
        """Return a JSON string representation of the top level schema"""
        return json.dumps(
            self.top_level_schema(all=all),
            indent=2,
            default=pydantic.json.pydantic_encoder,
        )

    ##
    # Event handlers

    @servo.events.on_event()
    async def check(
        self,
        matching: Optional[servo.checks.CheckFilter],
        halt_on: Optional[servo.types.ErrorSeverity] = servo.types.ErrorSeverity.critical,
    ) -> List[servo.checks.Check]:
        """Check that the servo is ready to perform optimization.

        Args:
            matching: An optional filter to limit the checks that are executed.
            halt_on: The severity level of errors that should halt execution of checks.

        Returns:
            A list of check objects that describe the outcomes of the checks that were run.
        """
        try:
            async with self.api_client() as client:
                event_request = servo.api.Request(event=servo.api.Events.hello)
                response = await client.post("servo", data=event_request.json())
                success = response.status_code == httpx.codes.OK
                return [
                    servo.checks.Check(
                        name="Opsani API connectivity",
                        success=success,
                        message=f"Response status code: {response.status_code}",
                    )
                ]
        except Exception as error:
            return [
                servo.checks.Check(
                    name="Opsani API connectivity",
                    success=False,
                    message=str(error),
                )
            ]
