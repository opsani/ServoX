import abc
import json
import re
from enum import Enum
from pathlib import Path
from typing import (
    Any, 
    ClassVar, 
    Generator, 
    Optional, 
    Set, 
    Type, 
    get_type_hints, 
    TypeVar, 
    Callable,
    Dict
)

import httpx
import semver
import typer
import yaml
import logging
import loguru
from pkg_resources import EntryPoint, iter_entry_points
from pydantic import (
    BaseModel,
    BaseSettings,
    Extra,
    HttpUrl,
    ValidationError,
    constr,
    root_validator,
    validator,
)
from pydantic.fields import ModelField


class Optimizer(BaseSettings):
    """
    An Optimizer models an Opsani optimization engines that the Servo can connect to
    in order to access the Opsani machine learning technology for optimizing system infrastructure
    and application workloads.
    """

    org_domain: constr(
        regex=r"(([\da-zA-Z])([_\w-]{,62})\.){,127}(([\da-zA-Z])[_\w-]{,61})?([\da-zA-Z]\.((xn\-\-[a-zA-Z\d]+)|([a-zA-Z\d]{2,})))"
    )
    """
    The domain name of the Organization tha the optimizer belongs to.

    For example, a domain name of `awesome.com` might belong to Awesome, Inc and all optimizers would be
    deployed under this domain name umbrella for easy access and autocompletion ergonomics.
    """

    app_name: constr(regex=r"^[a-z\-]{3,64}$")
    """
    The symbolic name of the application or servoce under optimization in a string of URL-safe characters between 3 and 64
    characters in length 
    """

    token: str
    """
    An opaque access token for interacting with the Optimizer via HTTP Bearer Token authentication.
    """

    base_url: HttpUrl = "https://api.opsani.com/"
    """
    The base URL for accessing the Opsani API. This optiion is typically only useful for Opsani developers or in the context
    of deployments with specific contractual, firewall, or security mandates that preclude access to the primary API.
    """

    def __init__(self, id: str = None, **kwargs):
        if isinstance(id, str):
            org_domain, app_name = id.split("/")
        else:
            org_domain = kwargs.pop("org_domain", None)
            app_name = kwargs.pop("app_name", None)
        super().__init__(org_domain=org_domain, app_name=app_name, **kwargs)

    @property
    def id(self) -> str:
        """
        Returns the primary identifier of the optimizer. 

        A friendly identifier formed by joining the `org_domain` and the `app_name` with a slash character
        of the form `example.com/my-app` or `another.com/app-2`.
        """
        return f"{self.org_domain}/{self.app_name}"

    class Config:
        env_prefix = "SERVO_OPTIMIZER_"
        env_file = ".env"
        case_sensitive = False  # TODO: Normalize the env vars
        extra = Extra.forbid
        fields = {
            "token": {
                "env": "SERVO_OPTIMIZER_TOKEN",
                "env_names": {"SERVO_OPTIMIZER_TOKEN"},
            }
        }


class ConnectorSettings(BaseSettings):
    """
    ConnectorSettings is the base configuration class for Opsani Servo Connectors.

    ConnectorSettings instances are typically paired 1:1 with a Connector class
    that inherits from `servo.connector.Connector` and provides the business logic
    of the connector. Settings classes are configuration specific specific and designed
    to be initialized from commandline arguments, environment variables, and defaults.
    Connectors are initialized with a valid settings instance capable of providing necessary
    configuration for the connector to function.
    """

    description: Optional[str]
    """An optional textual description of the configyuration stanza useful for differentiating
    between configurations within assemblies.
    """

    # Automatically uppercase env names upon subclassing
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        # Set default environment variable names
        # TODO: we can probably just use env_name
        for name, field in cls.__fields__.items():
            field.field_info.extra["env_names"] = {f"SERVO_{name}".upper()}
        

    class Config:
        env_prefix = "SERVO_"
        env_file = ".env"
        case_sensitive = True
        extra = Extra.forbid
        fields = {
            "description": {
                "env": "SERVO_DESCRIPTION",
                "env_names": {"SERVO_DESCRIPTION"},
            }
        }


EventFunctionType = TypeVar("EventFunctionType", bound=Callable[..., Any])


class Connector(BaseModel, abc.ABC):
    """
    Connectors expose functionality to Servo assemblies by connecting external services and resources.
    """

    # Global registry of all available connectors
    # TODO: implementation detail, scrub from public API
    __subclasses: ClassVar[Set[Type["Connector"]]] = set()

    # Connector metadata
    name: ClassVar[str] = None
    """Name of the connector, by default derived from the class name.
    """

    version: ClassVar["Version"] = None
    """Semantic Versioning string of the connector.
    """

    description: ClassVar[Optional[str]] = None
    """Optional textual description of the connector.
    """

    homepage: ClassVar[Optional[HttpUrl]] = None
    """Link to the homepage of the connector.
    """

    license: ClassVar[Optional["License"]] = None
    """An enumerated value that identifies the license that the connector is distributed under.
    """

    maturity: ClassVar[Optional["Maturity"]] = None
    """An enumerated value that identifies the self-selected maturity level of the connector, provided for
    advisory purposes.
    """

    # Instance configuration

    settings: ConnectorSettings
    """Settings for the connector set explicitly or loaded from a config file.
    """

    optimizer: Optional[Optimizer]
    """Name of the command for interacting with the connector instance via the CLI.

    Note that optimizers are attached as configuration to Connector instance because
    the settings are not managed as part of the assembly config files and are always
    provided via environment variablesm, commandline arguments, or secrets management.
    """

    config_key_path: str
    """Key-path to the root of the connector's configuration.
    """

    command_name: constr(regex=r"^[a-z\-]{4,16}$")
    """Name of the command for interacting with the connector instance via the CLI.
    """

    @classmethod
    def all(cls) -> Set[Type["Connector"]]:
        """Return a set of all Connector subclasses"""
        return cls.__subclasses

    @root_validator(pre=True)
    @classmethod
    def validate_metadata(cls, v):
        assert cls.name is not None, "name must be provided"
        assert cls.version is not None, "version must be provided"
        if isinstance(cls.version, str):
            # Attempt to parse
            cls.version = Version.parse(cls.version)
        assert isinstance(
            cls.version, (Version, semver.VersionInfo)
        ), "version is not a semantic versioning descriptor"
        return v

    @validator("config_key_path")
    @classmethod
    def validate_config_key_path(cls, v):
        assert bool(
            re.match("^[0-9a-zA-Z-_/\\.]{4,128}$", v)
        ), "key paths may only contain alphanumeric characters, hyphens, slashes, periods, and underscores"
        return v

    @classmethod
    def settings_model(cls) -> Type["Settings"]:
        """Return the settings model backing the connector. 
        
        The effective type of the setting instance is defined by the type hint definitions of the 
        `settings_model` and `settings` level attributes closest in definition to the target class.
        """
        hints = get_type_hints(cls)
        settings_cls = hints["settings"]
        return settings_cls

    @classmethod
    def default_key_path(cls) -> str:
        """
        Returns the default key-path to the root of the configuration structure for the connector within
        the namespace of the servo assembly.
        
        Key-paths are string identifiers that address nodes at arbitrary depth within the structure where
        each component of the path is a valid Python symbol identifier and components are delimited by a
        perioid (`.`) character, denoting that the subpath is a directly addressable child node the the parent.
        """
        name = cls.__name__.replace("Connector", "")
        return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    
    @classmethod
    def responds_to_event(cls, name: str) -> bool:
        """
        Returns True if the Connector responds to the given event name
        """
        return cls.__events__.get(name, False)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.name = cls.__name__.replace("Connector", " Connector")
        cls.version = semver.VersionInfo.parse("0.0.0")
        cls.__subclasses.add(cls)

        # Create the event registry if it doesn't exist
        # if not hasattr(cls, '__events__'):
        #     cls.__events__ = {}
        cls.__events__ = getattr(cls, '__events__', {}).copy()
        
        # Register events for all annotated methods (see `event` decorator)
        for key, value in cls.__dict__.items():                
            if v := getattr(value, '__connector_event__', None):
                if not isinstance(v, EventDescriptor):
                    raise TypeError(f"Unexpected event descriptor of type '{f.__class__}'")

                cls.__events__[key] = v
                
        
    def __init__(
        self,
        settings: ConnectorSettings,
        *,
        config_key_path: Optional[str] = None,
        command_name: Optional[str] = None,
        **kwargs,
    ):
        config_key_path = (
            config_key_path if config_key_path is not None else self.default_key_path()
        )
        command_name = (
            command_name
            if command_name is not None
            else config_key_path.rsplit(".", 1)[-1]
        )
        super().__init__(
            settings=settings,
            config_key_path=config_key_path,
            command_name=command_name,
            **kwargs,
        )

    ##
    # Events

    # TODO: Gather all responses into a collection
    def dispatch_event(self, name: str, *args, **kwargs):
        pass
    
    def invoke_event(self, name: str, target: 'Connector', *args, **kwargs):
        pass

    ##
    # Subclass services

    # TODO: ServoRunner should use this?
    async def api_client(self) -> httpx.AsyncClient:
        """Yields an httpx.AsyncClient instance configured to talk to Opsani API"""
        async with httpx.AsyncClient() as client:
            yield client

    @property
    def logger(self) -> logging.Logger:
        """Returns the logger"""
        return loguru.logger

    # TODO: Do we need this?
    @property
    def cli(self) -> Optional[typer.Typer]:
        """Returns a Typer CLI for the connector"""
        return None


class License(Enum):
    """Defined licenses"""

    MIT = "MIT"
    APACHE2 = "Apache 2.0"
    PROPRIETARY = "Proprietary"

    @classmethod
    def from_str(cls, identifier: str) -> "License":
        """
        Returns a `License` for the given string identifier (e.g. "MIT").
        """
        for _, env in cls.__members__.items():
            if env.value == identifier:
                return env
        raise NameError(f'No license identified by "{identifier}".')

    def __str__(self):
        return self.value


class Maturity(Enum):
    """Connector maturity level"""

    EXPERIMENTAL = "Experimental"
    STABLE = "Stable"
    ROBUST = "Robust"

    @classmethod
    def from_str(cls, identifier: str) -> "Maturity":
        """
        Returns a `License` for the given string identifier (e.g. "MIT").
        """
        for _, env in cls.__members__.items():
            if env.value == identifier:
                return env
        raise NameError(f'No maturity level identified by "{identifier}".')

    def __str__(self):
        return self.value


class Version(semver.VersionInfo):
    pass


def metadata(
    name: Optional[str] = None,
    description: Optional[str] = None,
    version: Optional[semver.VersionInfo] = None,
    homepage: Optional[HttpUrl] = None,
    license: Optional[License] = None,
    maturity: Optional[Maturity] = None,
):
    """Decorate a Connector class with metadata"""

    def decorator(cls):
        if not issubclass(cls, Connector):
            raise TypeError("Metadata can only be attached to Connector subclasses")

        if name:
            cls.name = name
        if description:
            cls.description = description
        if version:
            cls.version = (
                version
                if isinstance(version, semver.VersionInfo)
                else Version.parse(version)
            )
        if homepage:
            cls.homepage = homepage
        if license:
            cls.license = license
        if maturity:
            cls.maturity = maturity
        return cls

    return decorator

class EventDescriptor(BaseModel):
    name: str
    kwargs: Dict[str, Any]
        
def event(**kwargs):
    """
    Registers an event on the Connector
    """
    def decorator(fn: EventFunctionType) -> EventFunctionType:
        # Annotate the function for processing later, see Connector.__init_subclass__
        fn.__connector_event__ = EventDescriptor(
            name=fn.__name__,
            kwargs=kwargs
        )
        return fn

    return decorator

#####

class ConnectorCLI(typer.Typer):
    connector: Connector

    def __init__(self, connector: Connector, **kwargs):
        self.connector = connector
        name = kwargs.pop("name", connector.command_name)
        help = kwargs.pop("help", connector.description)
        add_completion = kwargs.pop("add_completion", False)
        super().__init__(name=name, help=help, add_completion=add_completion, **kwargs)
        self.add_commands()

    # TODO: Converge the commands
    def add_commands(self):
        @self.command()
        def schema():
            """
            Display the schema 
            """
            # TODO: Support output formats (dict, json, yaml)...
            typer.echo(self.connector.settings.schema_json(indent=2))

        # TODO: Test on servo and on connector
        @self.command()
        def generate():
            """Generate a configuration file"""
            # TODO: support output paths/formats
            # NOTE: We have to serialize through JSON first
            schema = json.loads(json.dumps(self.connector.settings.dict(by_alias=True)))
            output_path = Path.cwd() / f"{self.connector.command_name}.yaml"
            output_path.write_text(yaml.dump(schema))
            typer.echo(f"Generated {self.connector.command_name}.yaml")

        @self.command()
        def validate(file: typer.FileText = typer.Argument(...), key: str = ""):
            """
            Validate given file against the JSON Schema
            """
            try:
                config = yaml.load(file, Loader=yaml.FullLoader)
                connector_config = config[key] if key != "" else config
                cls = type(self.connector.settings)
                config = cls.parse_obj(connector_config)
                typer.echo("√ Valid connector configuration")
            except (ValidationError, yaml.scanner.ScannerError) as e:
                typer.echo("X Invalid connector configuration", err=True)
                typer.echo(e, err=True)
                raise typer.Exit(1)

        @self.command()
        def info():
            """
            Display connector info
            """
            typer.echo(
                (
                    f"{self.connector.name} v{self.connector.version} ({self.connector.maturity})\n"
                    f"{self.connector.description}\n"
                    f"{self.connector.homepage}\n"
                    f"Licensed under the terms of {self.connector.license}\n"
                )
            )

        @self.command()
        def version():
            """
            Display version
            """
            typer.echo(f"{self.connector.name} v{self.connector.version}")


#####

ENTRY_POINT_GROUP = "servo.connectors"


class ConnectorLoader:
    """
    Dynamically discovers and loads connectors via Python setuptools entry points
    """

    def __init__(self, group: str = ENTRY_POINT_GROUP) -> None:
        self.group = group

    def iter_entry_points(self) -> Generator[EntryPoint, None, None]:
        yield from iter_entry_points(group=self.group, name=None)

    def load(self) -> Generator[Any, None, None]:
        for entry_point in self.iter_entry_points():
            yield entry_point.resolve()
