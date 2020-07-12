from pathlib import Path
import os
import sys
import yaml
import json
import pytest
from pydantic import Extra, ValidationError
from servo import __version__, connector
from servo.servo import BaseServoSettings, ServoAssembly, Servo
from servo.connector import Connector, Optimizer, ConnectorSettings, on_event, before_event, after_event
from servo.types import Event, EventHandler, EventResult, Preposition, EventError, CancelEventError
from tests.test_helpers import environment_overrides
from connectors.vegeta.vegeta import TargetFormat, VegetaConnector, VegetaSettings

def test_version():
    assert __version__ == "0.1.0"

class FirstTestServoConnector(Connector):
    @on_event()
    def this_is_an_event(self) -> str:
        return "this is the result"
    
    @after_event('adjust')
    def adjust_handler(self) -> str:
        return "adjusting!"
    
    @before_event('measure')
    def do_something_before_measuring(self) -> str:
        return "measuring!"
    
    @before_event('promote')
    def run_before_promotion(self) -> str:
        return "about to promote!"

    @on_event('promote')
    def run_on_promotion(self) -> str:
        return "promoting!"
    
    @after_event('promote')
    def run_after_promotion(self) -> str:
        return "promoted!!"
    
    class Config:
        # NOTE: Necessary to utilize mocking
        extra = Extra.allow

class SecondTestServoConnector(Connector):
    @on_event()
    def this_is_an_event(self) -> str:
        return "this is a different result"

    @on_event()
    def another_event(self) -> None:
        pass

@pytest.fixture()
def assembly(servo_yaml: Path) -> ServoAssembly:
    config = {
        "connectors": ["first_test_servo", "second_test_servo"],
        "first_test_servo": {},
        "second_test_servo": {},
    }
    servo_yaml.write_text(yaml.dump(config))

    optimizer = Optimizer(id="dev.opsani.com/servox", token="1234556789")

    assembly, servo, DynamicServoSettings = ServoAssembly.assemble(
        config_file=servo_yaml, optimizer=optimizer
    )
    return assembly

@pytest.fixture()
def servo(assembly: ServoAssembly) -> Servo:
    return assembly.servo

def test_all_connectors() -> None:
    c = ServoAssembly.construct().all_connectors()
    assert FirstTestServoConnector in c

def test_dispatch_event(servo: Servo) -> None:
    results = servo.dispatch_event("this_is_an_event")
    assert len(results) == 2
    assert results[0].value == "this is the result"

def test_dispatch_event_first(servo: Servo) -> None:
    result = servo.dispatch_event("this_is_an_event", first=True)
    assert isinstance(result, EventResult)
    assert result.value == "this is the result"

def test_dispatch_event_include(servo: Servo) -> None:
    first_connector = servo.connectors[0]
    assert first_connector.name == "FirstTestServo Connector"
    results = servo.dispatch_event("this_is_an_event", include=[first_connector])
    assert len(results) == 1
    assert results[0].value == "this is the result"

def test_dispatch_event_exclude(servo: Servo) -> None:
    assert len(servo.connectors) == 3
    first_connector = servo.connectors[0]
    assert first_connector.name == "FirstTestServo Connector"
    second_connector = servo.connectors[1]
    assert second_connector.name == "SecondTestServo Connector"
    event_names = set(map(lambda e: e.name, second_connector.__events__))
    assert "this_is_an_event" in event_names
    results = servo.dispatch_event("this_is_an_event", exclude=[first_connector])
    assert len(results) == 1
    assert results[0].value == "this is a different result"
    assert results[0].connector == second_connector

def test_before_event(mocker, servo: servo) -> None:
    connector = servo.routes['first_test_servo']
    event_handler = connector.get_event_handlers('measure', Preposition.BEFORE)[0]
    spy = mocker.spy(event_handler, 'handler')
    servo.dispatch_event('measure')
    spy.assert_called_once()

def test_after_event(mocker, servo: servo) -> None:
    connector = servo.routes['first_test_servo']
    event_handler = connector.get_event_handlers('promote', Preposition.AFTER)[0]
    spy = mocker.spy(event_handler, 'handler')
    servo.dispatch_event('promote')
    spy.assert_called_once()

def test_on_event(mocker, servo: servo) -> None:
    connector = servo.routes['first_test_servo']
    event_handler = connector.get_event_handlers('promote', Preposition.ON)[0]
    spy = mocker.spy(event_handler, 'handler')
    servo.dispatch_event('promote')
    spy.assert_called_once()

def test_cancellation_of_event_from_before_handler(mocker, servo: servo):
    connector = servo.routes['first_test_servo']
    before_handler = connector.get_event_handlers('promote', Preposition.BEFORE)[0]
    on_handler = connector.get_event_handlers('promote', Preposition.ON)[0]
    on_spy = mocker.spy(on_handler, 'handler')    
    after_handler = connector.get_event_handlers('promote', Preposition.AFTER)[0]
    after_spy = mocker.spy(after_handler, 'handler')

    # Mock the before handler to throw a cancel exception
    mock = mocker.patch.object(before_handler, 'handler')
    mock.side_effect = CancelEventError()
    results = servo.dispatch_event('promote')

    # Check that on and after callbacks were never called
    on_spy.assert_not_called()
    after_spy.assert_not_called()

    # Check the results
    assert len(results) == 1
    result = results[0]
    assert isinstance(result.value, CancelEventError)
    assert result.created_at is not None
    assert result.handler.handler == mock
    assert result.connector == connector
    assert result.event.name == 'promote'
    assert result.preposition == Preposition.BEFORE

def test_cannot_cancel_from_on_handlers(mocker, servo: servo):
    connector = servo.routes['first_test_servo']
    event_handler = connector.get_event_handlers('promote', Preposition.ON)[0]

    mock = mocker.patch.object(event_handler, 'handler')
    mock.side_effect = CancelEventError()
    with pytest.raises(TypeError) as error:
        servo.dispatch_event('promote')
    assert str(error.value) == 'Cannot cancel an event from an on handler'

def test_cannot_cancel_from_after_handlers(mocker, servo: servo):
    connector = servo.routes['first_test_servo']
    event_handler = connector.get_event_handlers('promote', Preposition.AFTER)[0]

    mock = mocker.patch.object(event_handler, 'handler')
    mock.side_effect = CancelEventError()
    with pytest.raises(TypeError) as error:
        servo.dispatch_event('promote')
    assert str(error.value) == 'Cannot cancel an event from an after handler'

def test_after_handlers_are_called_on_failure(mocker, servo: servo):
    connector = servo.routes['first_test_servo']
    after_handler = connector.get_event_handlers('promote', Preposition.AFTER)[0]
    spy = mocker.spy(after_handler, 'handler')

    # Mock the before handler to raise an EventError
    on_handler = connector.get_event_handlers('promote', Preposition.ON)[0]
    mock = mocker.patch.object(on_handler, 'handler')
    mock.side_effect = EventError()
    results = servo.dispatch_event('promote')

    spy.assert_called_once()

    # Check the results
    assert len(results) == 1
    result = results[0]
    assert isinstance(result.value, EventError)
    assert result.created_at is not None
    assert result.handler.handler == mock
    assert result.connector == connector
    assert result.event.name == 'promote'
    assert result.preposition == Preposition.ON

class TestServoAssembly:
    def test_warning_ambiguous_connectors(self) -> None:
        # TODO: This can be very hard to debug
        # This is where you have 2 connector classes with the same name
        pass

    def test_assemble_assigns_optimizer_to_connectors(self, servo_yaml: Path):
        config = {
            "connectors": {"vegeta": "vegeta"},
            "vegeta": {"duration": 0, "rate": 0, "target": "https://opsani.com/"},
        }
        servo_yaml.write_text(yaml.dump(config))

        optimizer = Optimizer(id="dev.opsani.com/servox", token="1234556789")

        assembly, servo, DynamicServoSettings = ServoAssembly.assemble(
            config_file=servo_yaml, optimizer=optimizer
        )
        connector = servo.connectors[0]
        assert connector.optimizer == optimizer

    def test_aliased_connectors_produce_schema(self, servo_yaml: Path) -> None:
        config = {
            "connectors": {"vegeta": "vegeta", "other": "vegeta"},
            "vegeta": {"duration": 0, "rate": 0, "target": "https://opsani.com/"},
            "other": {"duration": 0, "rate": 0, "target": "https://opsani.com/"},
        }
        servo_yaml.write_text(yaml.dump(config))

        optimizer = Optimizer(id="dev.opsani.com/servox", token="1234556789")

        assembly, servo, DynamicServoSettings = ServoAssembly.assemble(
            config_file=servo_yaml, optimizer=optimizer
        )
        schema = json.loads(DynamicServoSettings.schema_json())

        # Description on parent class can be squirrely
        assert schema["properties"]["description"]["env_names"] == ["SERVO_DESCRIPTION"]
        assert schema == {
            'title': 'Servo Configuration Schema',
            'description': 'Schema for configuration of Servo v0.0.0 with Vegeta Connector v0.5.0',
            'type': 'object',
            'properties': {
                'description': {
                    'title': 'Description',
                    'description': 'An optional annotation describing the configuration.',
                    'env_names': [
                        'SERVO_DESCRIPTION',
                    ],
                    'type': 'string',
                },
                'connectors': {
                    'title': 'Connectors',
                    'description': (
                        'An optional, explicit configuration of the active connectors.\n'
                        '\n'
                        'Configurable as either an array of connector identifiers (names or class) or\n'
                        'a dictionary where the keys specify the key path to the connectors configuration\n'
                        'and the values identify the connector (by name or class name).'
                    ),
                    'examples': [
                        [
                            'kubernetes',
                            'prometheus',
                        ],
                        {
                            'staging_prom': 'prometheus',
                            'gateway_prom': 'prometheus',
                        },
                    ],
                    'env_names': [
                        'SERVO_CONNECTORS',
                    ],
                    'anyOf': [
                        {
                            'type': 'array',
                            'items': {
                                'type': 'string',
                            },
                        },
                        {
                            'type': 'object',
                            'additionalProperties': {
                                'type': 'string',
                            },
                        },
                    ],
                },
                'other': {
                    'title': 'Other',
                    'env_names': [
                        'SERVO_OTHER',
                    ],
                    'allOf': [
                        {
                            '$ref': '#/definitions/VegetaSettings__other',
                        },
                    ],
                },
                'vegeta': {
                    'title': 'Vegeta',
                    'env_names': [
                        'SERVO_VEGETA',
                    ],
                    'allOf': [
                        {
                            '$ref': '#/definitions/VegetaSettings',
                        },
                    ],
                },
            },
            'required': [
                'other',
                'vegeta',
            ],
            'additionalProperties': False,
            'definitions': {
                'VegetaSettings__other': {
                    'title': 'Vegeta Connector Settings (at key-path other)',
                    'description': 'Configuration of the Vegeta connector',
                    'type': 'object',
                    'properties': {
                        'description': {
                            'title': 'Description',
                            'description': 'An optional annotation describing the configuration.',
                            'env_names': [
                                'SERVO_OTHER_DESCRIPTION',
                            ],
                            'type': 'string',
                        },
                        'rate': {
                            'title': 'Rate',
                            'description': (
                                'Specifies the request rate per time unit to issue against the targets. Given in the forma'
                                't of request/time unit.'
                            ),
                            'env_names': [
                                'SERVO_OTHER_RATE',
                            ],
                            'type': 'string',
                        },
                        'duration': {
                            'title': 'Duration',
                            'description': 'Specifies the amount of time to issue requests to the targets.',
                            'env_names': [
                                'SERVO_OTHER_DURATION',
                            ],
                            'type': 'string',
                        },
                        'format': {
                            'title': 'Format',
                            'description': (
                                'Specifies the format of the targets input. Valid values are http and json. Refer to the V'
                                'egeta docs for details.'
                            ),
                            'default': 'http',
                            'env_names': [
                                'SERVO_OTHER_FORMAT',
                            ],
                            'enum': [
                                'http',
                                'json',
                            ],
                            'type': 'string',
                        },
                        'target': {
                            'title': 'Target',
                            'description': (
                                'Specifies a single formatted Vegeta target to load. See the format option to learn about '
                                'available target formats. This option is exclusive of the targets option and will provide'
                                ' a target to Vegeta via stdin.'
                            ),
                            'env_names': [
                                'SERVO_OTHER_TARGET',
                            ],
                            'type': 'string',
                        },
                        'targets': {
                            'title': 'Targets',
                            'description': (
                                'Specifies the file from which to read targets. See the format option to learn about avail'
                                'able target formats. This option is exclusive of the target option and will provide targe'
                                'ts to via through a file on disk.'
                            ),
                            'env_names': [
                                'SERVO_OTHER_TARGETS',
                            ],
                            'type': 'string',
                            'format': 'file-path',
                        },
                        'connections': {
                            'title': 'Connections',
                            'description': 'Specifies the maximum number of idle open connections per target host.',
                            'default': 10000,
                            'env_names': [
                                'SERVO_OTHER_CONNECTIONS',
                            ],
                            'type': 'integer',
                        },
                        'workers': {
                            'title': 'Workers',
                            'description': (
                                'Specifies the initial number of workers used in the attack. The workers will automaticall'
                                'y increase to achieve the target request rate, up to max-workers.'
                            ),
                            'default': 10,
                            'env_names': [
                                'SERVO_OTHER_WORKERS',
                            ],
                            'type': 'integer',
                        },
                        'max_workers': {
                            'title': 'Max Workers',
                            'description': (
                                'The maximum number of workers used to sustain the attack. This can be used to control the'
                                ' concurrency of the attack to simulate a target number of clients.'
                            ),
                            'default': 18446744073709551615,
                            'env_names': [
                                'SERVO_OTHER_MAX_WORKERS',
                            ],
                            'type': 'integer',
                        },
                        'max_body': {
                            'title': 'Max Body',
                            'description': (
                                'Specifies the maximum number of bytes to capture from the body of each response. Remainin'
                                'g unread bytes will be fully read but discarded.'
                            ),
                            'default': -1,
                            'env_names': [
                                'SERVO_OTHER_MAX_BODY',
                            ],
                            'type': 'integer',
                        },
                        'http2': {
                            'title': 'Http2',
                            'description': 'Specifies whether to enable HTTP/2 requests to servers which support it.',
                            'default': True,
                            'env_names': [
                                'SERVO_OTHER_HTTP2',
                            ],
                            'type': 'boolean',
                        },
                        'keepalive': {
                            'title': 'Keepalive',
                            'description': 'Specifies whether to reuse TCP connections between HTTP requests.',
                            'default': True,
                            'env_names': [
                                'SERVO_OTHER_KEEPALIVE',
                            ],
                            'type': 'boolean',
                        },
                        'insecure': {
                            'title': 'Insecure',
                            'description': 'Specifies whether to ignore invalid server TLS certificates.',
                            'default': False,
                            'env_names': [
                                'SERVO_OTHER_INSECURE',
                            ],
                            'type': 'boolean',
                        },
                    },
                    'required': [
                        'rate',
                        'duration',
                    ],
                    'additionalProperties': False,
                },
                'VegetaSettings': {
                    'title': 'Vegeta Connector Settings (at key-path vegeta)',
                    'description': 'Configuration of the Vegeta connector',
                    'type': 'object',
                    'properties': {
                        'description': {
                            'title': 'Description',
                            'description': 'An optional annotation describing the configuration.',
                            'env_names': [
                                'SERVO_VEGETA_DESCRIPTION',
                            ],
                            'type': 'string',
                        },
                        'rate': {
                            'title': 'Rate',
                            'description': (
                                'Specifies the request rate per time unit to issue against the targets. Given in the forma'
                                't of request/time unit.'
                            ),
                            'env_names': [
                                'SERVO_VEGETA_RATE',
                            ],
                            'type': 'string',
                        },
                        'duration': {
                            'title': 'Duration',
                            'description': 'Specifies the amount of time to issue requests to the targets.',
                            'env_names': [
                                'SERVO_VEGETA_DURATION',
                            ],
                            'type': 'string',
                        },
                        'format': {
                            'title': 'Format',
                            'description': (
                                'Specifies the format of the targets input. Valid values are http and json. Refer to the V'
                                'egeta docs for details.'
                            ),
                            'default': 'http',
                            'env_names': [
                                'SERVO_VEGETA_FORMAT',
                            ],
                            'enum': [
                                'http',
                                'json',
                            ],
                            'type': 'string',
                        },
                        'target': {
                            'title': 'Target',
                            'description': (
                                'Specifies a single formatted Vegeta target to load. See the format option to learn about '
                                'available target formats. This option is exclusive of the targets option and will provide'
                                ' a target to Vegeta via stdin.'
                            ),
                            'env_names': [
                                'SERVO_VEGETA_TARGET',
                            ],
                            'type': 'string',
                        },
                        'targets': {
                            'title': 'Targets',
                            'description': (
                                'Specifies the file from which to read targets. See the format option to learn about avail'
                                'able target formats. This option is exclusive of the target option and will provide targe'
                                'ts to via through a file on disk.'
                            ),
                            'env_names': [
                                'SERVO_VEGETA_TARGETS',
                            ],
                            'type': 'string',
                            'format': 'file-path',
                        },
                        'connections': {
                            'title': 'Connections',
                            'description': 'Specifies the maximum number of idle open connections per target host.',
                            'default': 10000,
                            'env_names': [
                                'SERVO_VEGETA_CONNECTIONS',
                            ],
                            'type': 'integer',
                        },
                        'workers': {
                            'title': 'Workers',
                            'description': (
                                'Specifies the initial number of workers used in the attack. The workers will automaticall'
                                'y increase to achieve the target request rate, up to max-workers.'
                            ),
                            'default': 10,
                            'env_names': [
                                'SERVO_VEGETA_WORKERS',
                            ],
                            'type': 'integer',
                        },
                        'max_workers': {
                            'title': 'Max Workers',
                            'description': (
                                'The maximum number of workers used to sustain the attack. This can be used to control the'
                                ' concurrency of the attack to simulate a target number of clients.'
                            ),
                            'default': 18446744073709551615,
                            'env_names': [
                                'SERVO_VEGETA_MAX_WORKERS',
                            ],
                            'type': 'integer',
                        },
                        'max_body': {
                            'title': 'Max Body',
                            'description': (
                                'Specifies the maximum number of bytes to capture from the body of each response. Remainin'
                                'g unread bytes will be fully read but discarded.'
                            ),
                            'default': -1,
                            'env_names': [
                                'SERVO_VEGETA_MAX_BODY',
                            ],
                            'type': 'integer',
                        },
                        'http2': {
                            'title': 'Http2',
                            'description': 'Specifies whether to enable HTTP/2 requests to servers which support it.',
                            'default': True,
                            'env_names': [
                                'SERVO_VEGETA_HTTP2',
                            ],
                            'type': 'boolean',
                        },
                        'keepalive': {
                            'title': 'Keepalive',
                            'description': 'Specifies whether to reuse TCP connections between HTTP requests.',
                            'default': True,
                            'env_names': [
                                'SERVO_VEGETA_KEEPALIVE',
                            ],
                            'type': 'boolean',
                        },
                        'insecure': {
                            'title': 'Insecure',
                            'description': 'Specifies whether to ignore invalid server TLS certificates.',
                            'default': False,
                            'env_names': [
                                'SERVO_VEGETA_INSECURE',
                            ],
                            'type': 'boolean',
                        },
                    },
                    'required': [
                        'rate',
                        'duration',
                    ],
                    'additionalProperties': False,
                },
            },
        }

    def test_aliased_connectors_get_distinct_env_configuration(
        self, servo_yaml: Path
    ) -> None:
        config = {
            "connectors": {"vegeta": "vegeta", "other": "vegeta"},
            "vegeta": {"duration": 0, "rate": 0, "target": "https://opsani.com/"},
            "other": {"duration": 0, "rate": 0, "target": "https://opsani.com/"},
        }
        servo_yaml.write_text(yaml.dump(config))

        optimizer = Optimizer(id="dev.opsani.com/servox", token="1234556789")

        assembly, servo, DynamicServoSettings = ServoAssembly.assemble(
            config_file=servo_yaml, optimizer=optimizer
        )

        # Grab the vegeta field and check it
        vegeta_field = DynamicServoSettings.__fields__["vegeta"]
        vegeta_settings_type = vegeta_field.type_
        assert vegeta_settings_type.__name__ == "VegetaSettings"
        assert vegeta_field.field_info.extra["env_names"] == {"SERVO_VEGETA"}

        # Grab the other field and check it
        other_field = DynamicServoSettings.__fields__["other"]
        other_settings_type = other_field.type_
        assert other_settings_type.__name__ == "VegetaSettings__other"
        assert other_field.field_info.extra["env_names"] == {"SERVO_OTHER"}

        with environment_overrides({"SERVO_DESCRIPTION": "this description"}):
            assert os.environ["SERVO_DESCRIPTION"] == "this description"
            s = DynamicServoSettings(
                other=other_settings_type.construct(),
                vegeta=vegeta_settings_type(
                    rate=10, duration="10s", target="http://example.com/"
                ),
            )
            assert s.description == "this description"

        # Make sure the incorrect case does pass
        with environment_overrides({"SERVO_DURATION": "5m"}):
            with pytest.raises(ValidationError) as e:
                vegeta_settings_type(rate=0, target="https://foo.com/")
            assert e is not None

        # Try setting values via env
        with environment_overrides(
            {
                "SERVO_VEGETA_DURATION": "5m",
                "SERVO_VEGETA_RATE": "0",
                "SERVO_VEGETA_TARGET": "https://opsani.com/",
            }
        ):
            s = vegeta_settings_type()
            assert s.duration == "5m"
            assert s.rate == "0"
            assert s.target == "https://opsani.com/"

        with environment_overrides(
            {
                "SERVO_OTHER_DURATION": "15m",
                "SERVO_OTHER_RATE": "100/1s",
                "SERVO_OTHER_TARGET": "https://opsani.com/servox",
            }
        ):
            s = other_settings_type()
            assert s.duration == "15m"
            assert s.rate == "100/1s"
            assert s.target == "https://opsani.com/servox"

def test_generating_schema_with_test_connectors(optimizer_env: None, servo_yaml: Path) -> None:
    optimizer = Optimizer(id="dev.opsani.com/servox", token="1234556789")

    assembly, servo, DynamicServoSettings = ServoAssembly.assemble(
        config_file=servo_yaml, optimizer=optimizer
    )
    DynamicServoSettings.schema()
    # NOTE: Covers naming conflicts between settings models -- will raise if misconfigured

class TestServoSettings:
    def test_forbids_extra_attributes(self) -> None:
        with pytest.raises(ValidationError) as e:
            BaseServoSettings(
                forbidden=[]
            )
            assert "extra fields not permitted" in str(e)

    def test_override_optimizer_settings_with_env_vars(self) -> None:
        with environment_overrides({"OPSANI_TOKEN": "abcdefg"}):
            assert os.environ["OPSANI_TOKEN"] is not None
            optimizer = Optimizer(app_name="foo", org_domain="dsada.com")
            assert optimizer.token == "abcdefg"

    def test_set_connectors_with_env_vars(self) -> None:
        with environment_overrides({"SERVO_CONNECTORS": '["measure"]'}):
            assert os.environ["SERVO_CONNECTORS"] is not None
            s = BaseServoSettings()
            assert s is not None
            schema = s.schema()
            assert schema["properties"]["connectors"]["env_names"] == {
                "SERVO_CONNECTORS"
            }
            assert s.connectors is not None
            assert s.connectors == ["measure"]

    def test_connectors_allows_none(self):
        s = BaseServoSettings(
            connectors=None,
        )
        assert s.connectors is None

    def test_connectors_allows_set_of_classes(self):
        class FooConnector(Connector):
            pass

        class BarConnector(Connector):
            pass

        s = BaseServoSettings(
            connectors={FooConnector, BarConnector},
        )
        assert set(s.connectors) == {'FooConnector', 'BarConnector'}

    def test_connectors_rejects_invalid_connector_set_elements(self):
        with pytest.raises(ValidationError) as e:
            BaseServoSettings(
                connectors={BaseServoSettings},
            )
        assert "1 validation error for BaseServoSettings" in str(e.value)
        assert e.value.errors()[0]["loc"] == ("connectors",)
        assert (
            e.value.errors()[0]["msg"]
            == "Invalid connectors value: <class 'servo.servo.BaseServoSettings'>"
        )

    def test_connectors_allows_set_of_class_names(self):
        s = BaseServoSettings(
            connectors={"MeasureConnector", "AdjustConnector"},
        )
        assert set(s.connectors) == {"MeasureConnector", "AdjustConnector"}

    def test_connectors_rejects_invalid_connector_set_class_name_elements(self):
        with pytest.raises(ValidationError) as e:
            BaseServoSettings(
                connectors={"BaseServoSettings"},
            )
        assert "1 validation error for BaseServoSettings" in str(e.value)
        assert e.value.errors()[0]["loc"] == ("connectors",)
        assert (
            e.value.errors()[0]["msg"]
            == "BaseServoSettings is not a Connector subclass"
        )

    def test_connectors_allows_set_of_keys(self):
        s = BaseServoSettings(
            connectors={"vegeta"},
        )
        assert s.connectors == ["vegeta"]

    def test_connectors_allows_dict_of_keys_to_classes(self):
        s = BaseServoSettings(
            connectors={"alias": VegetaConnector},
        )
        assert s.connectors == {"alias": 'VegetaConnector'}

    def test_connectors_allows_dict_of_keys_to_class_names(self):
        s = BaseServoSettings(
            connectors={"alias": "VegetaConnector"},
        )
        assert s.connectors == {"alias": "VegetaConnector"}

    def test_connectors_allows_dict_with_explicit_map_to_default_key_path(self):
        s = BaseServoSettings(
            connectors={"vegeta": "VegetaConnector"},
        )
        assert s.connectors == {"vegeta": "VegetaConnector"}

    def test_connectors_allows_dict_with_explicit_map_to_default_class(self):
        s = BaseServoSettings(
            connectors={"vegeta": VegetaConnector},
        )
        assert s.connectors == {"vegeta": 'VegetaConnector'}

    def test_connectors_forbids_dict_with_existing_key(self):
        with pytest.raises(ValidationError) as e:
            BaseServoSettings(
                connectors={"vegeta": "MeasureConnector"},
            )
        assert "1 validation error for BaseServoSettings" in str(e.value)
        assert e.value.errors()[0]["loc"] == ("connectors",)
        assert (
            e.value.errors()[0]["msg"]
            == 'Key "vegeta" is reserved by `VegetaConnector`'
        )

    @pytest.fixture(autouse=True, scope="session")
    def discover_connectors(self) -> None:
        from servo.connector import ConnectorLoader

        loader = ConnectorLoader()
        for connector in loader.load():
            pass

    def test_connectors_forbids_dict_with_reserved_key(self):
        with pytest.raises(ValidationError) as e:
            BaseServoSettings(
                connectors={"connectors": "VegetaConnector"},
            )
        assert "1 validation error for BaseServoSettings" in str(e.value)
        assert e.value.errors()[0]["loc"] == ("connectors",)
        assert e.value.errors()[0]["msg"] == 'Key "connectors" is reserved'

    def test_connectors_forbids_dict_with_invalid_key(self):
        with pytest.raises(ValidationError) as e:
            BaseServoSettings(
                connectors={"This Is Not Valid": "VegetaConnector"},
            )
        assert "1 validation error for BaseServoSettings" in str(e.value)
        assert e.value.errors()[0]["loc"] == ("connectors",)
        assert (
            e.value.errors()[0]["msg"]
            == 'Key "This Is Not Valid" is not valid: key paths may only contain alphanumeric characters, hyphens, slashes, periods, and underscores'
        )

    def test_connectors_rejects_invalid_connector_dict_values(self):
        with pytest.raises(ValidationError) as e:
            BaseServoSettings(
                connectors={"whatever": "Not a Real Connector"},
            )
        assert "1 validation error for BaseServoSettings" in str(e.value)
        assert e.value.errors()[0]["loc"] == ("connectors",)
        assert (
            e.value.errors()[0]["msg"]
            == "Invalid connectors value: Not a Real Connector"
        )
