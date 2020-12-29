from __future__ import annotations

import asyncio
import datetime
import enum
import functools
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import textwrap
import time
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Pattern, Set, Tuple, Type, Union

import bullet
import click
import devtools
import kubernetes_asyncio
import loguru
import pydantic
import pygments
import pygments.formatters
import typer
import yaml

# Expose helpers
from tabulate import tabulate
from timeago import format as timeago

import servo
import servo.runner
import servo.utilities.yaml


class Section(str, enum.Enum):
    assembly = "Assembly Commands"
    ops = "Operational Commands"
    config = "Configuration Commands"
    connectors = "Connector Commands"
    commands = "Commands"
    other = "Other Commands"


class LogLevel(str, enum.Enum):
    trace = "TRACE"
    debug = "DEBUG"
    info = "INFO"
    success = "SUCCESS"
    warning = "WARNING"
    error = "ERROR"
    critical = "CRITICAL"


class ConfigOutputFormat(servo.AbstractOutputFormat):
    yaml = servo.YAML_FORMAT
    json = servo.JSON_FORMAT
    dict = servo.DICT_FORMAT
    text = servo.TEXT_FORMAT
    configmap = servo.CONFIGMAP_FORMAT


class SchemaOutputFormat(servo.AbstractOutputFormat):
    json = servo.JSON_FORMAT
    text = servo.TEXT_FORMAT
    dict = servo.DICT_FORMAT
    html = servo.HTML_FORMAT


class VersionOutputFormat(servo.AbstractOutputFormat):
    text = servo.TEXT_FORMAT
    json = servo.JSON_FORMAT


# FIXME: Eliminate the mixin and put our context object onto the Click.obj instance
class Context(typer.Context):
    """
    Context models state required by different CLI invocations.

    Hydration of the state if handled by callbacks on the `CLI` class.
    """

    # Basic configuration
    config_file: Optional[pathlib.Path] = None
    optimizer: Optional[servo.Optimizer] = None
    name: Optional[str] = None

    token: Optional[str] = None
    token_file: Optional[pathlib.Path] = None
    base_url: Optional[str] = None
    url: Optional[str] = None
    limit: Optional[int] = None

    # Assembled servo
    assembly: Optional[servo.Assembly] = None
    servo_: Optional[servo.Servo] = None

    # Active connector
    connector: Optional[servo.BaseConnector] = None

    # NOTE: Section defaults generally only apply to Groups (see notes below)
    section: Section = Section.commands

    @classmethod
    def attributes(cls) -> Set[str]:
        """Returns the names of the attributes to be hydrated by ContextMixin"""
        return {
            "config_file",
            "name",
            "optimizer",
            "assembly",
            "servo_",
            "connector",
            "section",
            "token",
            "token_file",
            "base_url",
            "url",
            "limit",
        }

    @property
    def servo(self) -> servo.Servo:
        return self.servo_

    def __init__(
        self,
        command: "Command",
        *args,
        config_file: Optional[pathlib.Path] = None,
        name: Optional[str] = None,
        optimizer: Optional[servo.Optimizer] = None,
        assembly: Optional[servo.Assembly] = None,
        servo_: Optional[servo.Servo] = None,
        connector: Optional[servo.BaseConnector] = None,
        section: Section = Section.commands,
        token: Optional[str] = None,
        token_file: Optional[pathlib.Path] = None,
        base_url: Optional[str] = None,
        url: Optional[str] = None,
        limit: Optional[int] = None,
        **kwargs,
    ) -> None: # noqa: D107
        self.config_file = config_file
        self.name = name
        self.optimizer = optimizer
        self.assembly = assembly
        self.servo_ = servo_
        self.connector = connector
        self.section = section
        self.token = token
        self.token_file = token_file
        self.base_url = base_url
        self.limit = limit
        return super().__init__(command, *args, **kwargs)


class ContextMixin:
    # NOTE: Override the Click `make_context` base method to inject our class
    def make_context(self, info_name, args, parent=None, **extra):
        if parent and not issubclass(parent.__class__, Context):
            raise ValueError(
                f"Encountered an unexpected parent subclass type '{parent.__class__}' while attempting to create a context"
            )

        for key, value in self.context_settings.items():
            if key not in extra:
                extra[key] = value

        if isinstance(parent, Context):
            for attribute in Context.attributes():
                if attribute not in extra:
                    extra[attribute] = getattr(parent, attribute)

        ctx = Context(self, info_name=info_name, parent=parent, **extra)
        with ctx.scope(cleanup=False):
            self.parse_args(ctx, args)
        return ctx


class Command(click.Command, ContextMixin):
    @property
    def section(self) -> Optional[Section]:
        # NOTE: The `callback` property is the decorated function. See `command()` on CLI
        return getattr(self.callback, "section", None)

    def make_context(self, info_name, args, parent=None, **extra):
        return ContextMixin.make_context(self, info_name, args, parent, **extra)


class Group(click.Group, ContextMixin):
    @property
    def section(self) -> Optional[Section]:
        # NOTE: For Groups, Typer doesn't give us a great way to pass the state (can't decorate callback fn)
        # so instead we hang it on the context and rely on the command() to override it
        if self.context_settings:
            return self.context_settings.get("section", None)
        else:
            return None

    def make_context(self, info_name, args, parent=None, **extra):
        return ContextMixin.make_context(self, info_name, args, parent, **extra)

    def format_commands(self, ctx, formatter):
        """
        Formats all commands into sections
        """

        sections_of_commands: Dict[Section, List[Tuple[str, Command]]] = {}
        for section in Section:
            sections_of_commands[section] = []

        for command_name in self.list_commands(ctx):
            command = self.get_command(ctx, command_name)
            if command.hidden:
                continue

            # Determine the command section
            # NOTE: We may have non-CLI instances so we guard attribute access
            section = getattr(command, "section", Section.commands)

            commands = sections_of_commands.get(section, [])
            commands.append(
                (
                    command_name,
                    command,
                )
            )
            sections_of_commands[section] = commands

        for section, commands in sections_of_commands.items():
            if len(commands) == 0:
                continue

            limit = formatter.width - 6 - max(len(cmd[0]) for cmd in commands)

            # Sort the connector and other commands as ordering isn't explicit
            if section in (
                Section.connectors,
                Section.other,
            ):
                commands = sorted(commands)

            rows = []
            for name, command in commands:
                help = command.get_short_help_str(limit)
                rows.append((name, help))

            with formatter.section(section):
                formatter.write_dl(rows)


class OrderedGroup(Group):
    # NOTE: Rely on ordering of modern Python dicts
    def list_commands(self, ctx):
        return self.commands


class CLI(typer.Typer, servo.logging.Mixin):
    section: Section = Section.commands

    def __init__(
        self,
        *args,
        name: Optional[str] = None,
        help: Optional[str] = None,
        command_type: Optional[Type[click.Command]] = None,
        callback: Optional[Callable] = typer.models.Default(None),
        section: Section = Section.commands,
        **kwargs,
    ) -> None: # noqa: D107

        # NOTE: Set default command class to get custom context
        if command_type is None:
            command_type = Group
        if isinstance(callback, typer.models.DefaultPlaceholder):
            callback = self.root_callback
        self.section = section
        super().__init__(
            *args, name=name, help=help, cls=command_type, callback=callback, **kwargs
        )

    def command(
        self,
        *args,
        cls: Optional[Type[click.Command]] = None,
        section: Section = None,
        **kwargs,
    ) -> Callable[[typer.models.CommandFunctionType], typer.models.CommandFunctionType]:
        # NOTE: Set default command class to get custom context & section support
        if cls is None:
            cls = Command

        # NOTE: This is a little fancy. We are decorating the function with the
        # section metadata and then returning the Typer decorator implementation
        parent_decorator = super().command(*args, cls=cls, **kwargs)

        def decorator(
            f: typer.models.CommandFunctionType,
        ) -> typer.models.CommandFunctionType:
            f.section = section if section else self.section
            return parent_decorator(f)

        return decorator

    def callback(
        self,
        *args,
        cls: Optional[Type[click.Command]] = None,
        **kwargs,
    ) -> Callable[[typer.models.CommandFunctionType], typer.models.CommandFunctionType]:
        # NOTE: Override the default to inject our Command class
        if cls is None:
            cls = Group
        return super().callback(*args, cls=cls, **kwargs)

    def add_cli(
        self,
        cli: "CLI",
        *args,
        cls: Optional[Type[click.Command]] = None,
        section: Optional[Section] = None,
        context_settings: Optional[Dict[Any, Any]] = None,
        **kwargs,
    ) -> None:
        if not isinstance(cli, CLI):
            raise ValueError(
                f"Cannot add cli of type '{cli.__class__}: not a servo.cli.CLI"
            )
        if cls is None:
            cls = Group
        if context_settings is None:
            context_settings = {}
        section = section if section else cli.section
        # NOTE: Hang section state on the context for `Group` to pick up later
        context_settings["section"] = section
        return self.add_typer(
            cli, *args, cls=cls, context_settings=context_settings, **kwargs
        )

    @staticmethod
    def root_callback(
        ctx: Context,
        optimizer: str = typer.Option(
            None,
            envvar="OPSANI_OPTIMIZER",
            show_envvar=True,
            metavar="OPTIMIZER",
            help="Opsani optimizer to connect to (format is example.com/app)",
        ),
        token: str = typer.Option(
            None,
            envvar="OPSANI_TOKEN",
            show_envvar=True,
            metavar="TOKEN",
            help="Opsani API access token",
        ),
        token_file: pathlib.Path = typer.Option(
            None,
            envvar="OPSANI_TOKEN_FILE",
            show_envvar=True,
            exists=True,
            file_okay=True,
            dir_okay=False,
            writable=False,
            readable=True,
            resolve_path=True,
            help="File to load the access token from",
        ),
        base_url: str = typer.Option(
            "https://api.opsani.com/",
            "--base-url",
            envvar="OPSANI_BASE_URL",
            show_envvar=True,
            show_default=True,
            metavar="URL",
            help="Base URL for connecting to Opsani API",
        ),
        url: str = typer.Option(
            None,
            hidden=True,
            envvar="OPSANI_URL",
            metavar="URL",
            help="Complete URL to reach the Opsani API, overriding the URL computed from the base URL",
        ),
        config_file: pathlib.Path = typer.Option(
            "servo.yaml",
            "--config-file",
            "-c",
            envvar="SERVO_CONFIG_FILE",
            show_envvar=True,
            exists=False,
            file_okay=True,
            dir_okay=False,
            writable=False,
            readable=True,
            resolve_path=True,
            help="Servo configuration file",
        ),
        name: Optional[str] = typer.Option(
            None,
            "--name",
            "-n",
            envvar="SERVO_NAME",
            show_envvar=True,
            help="Name of the servo to use",
        ),
        limit: Optional[int] = typer.Option(
            None,
            "--limit",
            help="Limit multi-servo concurrency",
        ),
        log_level: LogLevel = typer.Option(
            LogLevel.info,
            "--log-level",
            "-l",
            envvar="SERVO_LOG_LEVEL",
            show_envvar=True,
            help="Set the log level",
        ),
        no_color: Optional[bool] = typer.Option(
            None,
            "--no-color",
            envvar=["SERVO_NO_COLOR", "NO_COLOR"],
            help="Disable colored output",
        ),
    ):
        ctx.config_file = config_file
        ctx.name = name
        ctx.optimizer = optimizer
        ctx.token = token
        ctx.token_file = token_file
        ctx.base_url = base_url
        ctx.url = url
        ctx.limit = limit
        servo.logging.set_level(log_level)
        servo.logging.set_colors(not no_color)

        # TODO: This should be pluggable. Base it off of the section?
        if ctx.invoked_subcommand not in {
            "init",
            "connectors",
            "schema",
            "generate",
            "validate",
            "version",
        }:
            try:
                CLI.assemble_from_context(ctx)

            except (ValueError, pydantic.ValidationError) as error:
                typer.echo(f"fatal: invalid configuration: {error}", err=True)
                raise typer.Exit(2)

    @staticmethod
    def assemble_from_context(ctx: Context):
        if ctx.config_file is None:
            raise typer.BadParameter("Config file must be specified")

        if not ctx.config_file.exists():
            raise typer.BadParameter(f"Config file '{ctx.config_file}' does not exist")

        # Conditionalize based on multi-servo options
        optimizer = None
        configs = list(yaml.full_load_all(open(ctx.config_file)))
        if not isinstance(configs, list):
            raise TypeError(
                f'error: config file "{ctx.config_file}" parsed to an unexpected value of type "{configs.__class__}"'
            )

        if len(configs) == 0:
            configs.append({})

        if len(configs) == 1:
            config = configs[0]

            if not isinstance(config, dict):
                raise TypeError(
                    f'error: config file "{ctx.config_file}" parsed to an unexpected value of type "{config.__class__}"'
                )

            if config.get("optimizer", None) == None:
                if ctx.optimizer is None:
                    raise typer.BadParameter("An optimizer must be specified")

                # Resolve token
                if ctx.token is None and ctx.token_file is None:
                    raise typer.BadParameter(
                        "API token must be provided via --token (ENV['OPSANI_TOKEN']) or --token-file (ENV['OPSANI_TOKEN_FILE'])"
                    )

                if ctx.token is not None and ctx.token_file is not None:
                    raise typer.BadParameter("--token and --token-file cannot both be given")

                if ctx.token_file is not None and ctx.token_file.exists():
                    ctx.token = ctx.token_file.read_text().strip()

                if len(ctx.token) == 0 or ctx.token.isspace():
                    raise typer.BadParameter("token cannot be blank")

                optimizer = servo.Optimizer(
                    ctx.optimizer, token=ctx.token, base_url=ctx.base_url, url=ctx.url
                )
        else:
            if ctx.optimizer:
                raise typer.BadParameter(f"An optimizer cannot be specified in a multi-servo configuration (found {ctx.optimizer})")

            if ctx.token or ctx.token_file:
                raise typer.BadParameter("A token cannot be specified in a multi-servo configuration")

            if ctx.limit:
                if len(configs) > ctx.limit:
                    servo.logger.warning(f"concurrent servo execution limited to {ctx.limit}: declining to run {len(configs) - ctx.limit} configured servos")
                    configs = configs[0:ctx.limit]

        # Assemble the Servo
        try:
            assembly = servo.Assembly.assemble(
                config_file=ctx.config_file,
                configs=configs,
                optimizer=optimizer
            )
        except pydantic.ValidationError as error:
            typer.echo(error, err=True)
            raise typer.Exit(2) from error

        # Target a specific servo if possible
        ctx.assembly = assembly
        if assembly.servos:
            if len(assembly.servos) == 1:
                ctx.servo_ = assembly.servos[0]

                if ctx.name and ctx.servo_.name != ctx.name:
                    raise typer.BadParameter(f"No servo was found named \"{ctx.name}\"")

            elif ctx.name:
                for servo_ in assembly.servos:
                    if servo_.name == ctx.name:
                        ctx.servo_ = servo_
                        break

                if ctx.servo_ is None:
                    raise typer.BadParameter(f"No servo was found named \"{ctx.name}\"")

    @staticmethod
    def connectors_named(names: List[str], servo_: servo.Servo) -> List[servo.BaseConnector]:
        connectors: List[servo.BaseConnector] = []
        for name in names:
            size = len(connectors)
            for connector in servo_.all_connectors:
                if connector.name == name:
                    connectors.append(connector)
                    break

            if len(connectors) == size:
                raise typer.BadParameter(f"no connector found named '{name}'")

        return connectors

    @staticmethod
    def connectors_type_callback(
        context: typer.Context, value: Optional[Union[str, List[str]]]
    ) -> Optional[Union[Type[servo.BaseConnector], List[Type[servo.BaseConnector]]]]:
        """
        Transforms a one or more connector key-paths into Connector types
        """
        if value:
            if isinstance(value, str):
                if connector := servo.connector._connector_class_from_string(value):
                    return connector
                else:
                    raise typer.BadParameter(
                        f"no Connector type found for key '{value}'"
                    )
            else:
                connectors: List[servo.BaseConnector] = []
                for key in value:
                    if connector := servo.connector._connector_class_from_string(key):
                        connectors.append(connector)
                    else:
                        raise typer.BadParameter(
                            f"no Connector type found for key '{key}'"
                        )
                return connectors
        else:
            return None

    @staticmethod
    def connector_routes_callback(
        context: typer.Context, value: Optional[List[str]]
    ) -> Optional[Dict[str, Type[servo.BaseConnector]]]:
        """
        Transforms a one or more connector descriptors into a dict of names to Connectors
        """
        if not value:
            return None

        routes: Dict[str, Type[servo.BaseConnector]] = {}
        for key in value:
            if ":" in key:
                # We have an alias descriptor
                name, identifier = key.split(":", 2)
            else:
                # Vanilla key-path or class name
                name = None
                identifier = key

            if connector_class := servo.connector._connector_class_from_string(
                identifier
            ):
                if name is None:
                    name = connector_class.__default_name__
                routes[name] = connector_class
            else:
                raise typer.BadParameter(
                    f"no connector found for identifier '{identifier}'"
                )

        return routes

    @staticmethod
    def duration_callback(
        context: typer.Context, value: Optional[str]
    ) -> Optional[Union[servo.BaseConnector, List[servo.BaseConnector]]]:
        """
        Transform a string into a Duration object.

        Parses duration strings.
        """
        if not value:
            return None

        try:
            return servo.Duration(value)
        except ValueError as e:
            raise typer.BadParameter(f"invalid duration parameter: {e}") from e


class ConnectorCLI(CLI):
    connector_type: Type[servo.BaseConnector]

    # CLI registry
    __clis__: Set["CLI"] = set()

    def __init__(
        self,
        connector_type: Type[servo.BaseConnector],
        *args,
        name: Optional[str] = None,
        help: Optional[str] = None,
        command_type: Optional[Type[click.Command]] = None,
        callback: Optional[Callable] = typer.models.Default(None),
        section: Section = Section.commands,
        **kwargs,
    ) -> None: # noqa: D107
        # Register for automated inclusion in the ServoCLI
        ConnectorCLI.__clis__.add(self)

        def connector_callback(
            context: Context,
            connector: Optional[str] = typer.Option(
                None,
                "--connector",
                "-c",
                metavar="CONNECTOR",
                help="Connector to activate",
            ),
        ) -> None:
            if context.servo is None:
                raise typer.BadParameter(f"A servo must be selected")

            instances = list(filter(lambda c: isinstance(c, connector_type), context.servo.connectors))
            instance_count = len(instances)
            if instance_count == 0:
                raise typer.BadParameter(f"no instances of \"{connector_type.__name__}\" are active the in servo \"{context.servo.name}\"")
            elif instance_count == 1:
                context.connector = instances[0]
            else:
                names = []
                for instance in instances:
                    if instance.name == connector:
                        context.connector = instance
                        break
                    names.append(instance.name)

                if context.connector is None:
                    if connector is None:
                        raise typer.BadParameter(f"multiple instances of \"{connector_type.__name__}\" found in servo \"{context.servo.name}\": select one of {repr(names)}")
                    else:
                        raise typer.BadParameter(f"no connector named \"{connector}\" of type \"{connector_type.__name__}\" found in servo \"{context.servo.name}\": select one of {repr(names)}")

        if name is None:
            name = servo.utilities.strings.commandify(connector_type.__default_name__)
        if help is None:
            help = connector_type.description
        if isinstance(callback, typer.models.DefaultPlaceholder):
            callback = connector_callback

        super().__init__(
            *args,
            name=name,
            help=help,
            command_type=command_type,
            callback=callback,
            section=section,
            **kwargs,
        )


class ServoCLI(CLI):
    """
    Provides the top-level commandline interface for interacting with the servo.
    """

    def __init__(
        self,
        *args,
        name: Optional[str] = "servo",
        command_type: Optional[Type[click.Command]] = None,
        add_completion: bool = True,
        no_args_is_help: bool = True,
        **kwargs,
    ) -> None: # noqa: D107
        # NOTE: We pass OrderedGroup to suppress sorting of commands alphabetically
        if command_type is None:
            command_type = OrderedGroup
        super().__init__(
            *args,
            command_type=command_type,
            name=name,
            add_completion=add_completion,
            no_args_is_help=no_args_is_help,
            **kwargs,
        )
        self.add_commands()

    def add_commands(self) -> None:
        self.add_ops_commands()
        self.add_config_commands()
        self.add_assembly_commands()
        self.add_connector_commands()

    @property
    def logger(self) -> loguru.Logger:
        return servo.logger

    def add_assembly_commands(self) -> None:
        @self.command(section=Section.assembly)
        def init(
            context: Context,
            dotenv: bool = typer.Option(
                True,
                help="Generate .env file",
            ),
        ) -> None:
            """
            Initialize a servo assembly
            """
            if dotenv:
                dotenv_file = pathlib.Path(".env")
                write_dotenv = True
                if dotenv_file.exists():
                    write_dotenv = typer.confirm(
                        f"File '{dotenv_file}' already exists. Overwrite it?"
                    )

                if write_dotenv:
                    optimizer = typer.prompt(
                        "Opsani optimizer? (format: dev.opsani.com/app-name)",
                        default=context.optimizer,
                    )
                    optimizer != context.optimizer or typer.echo()
                    token = typer.prompt("API token?", default=context.token)
                    token != context.token or typer.echo()
                    dotenv_file.write_text(
                        f"OPSANI_OPTIMIZER={optimizer}\nOPSANI_TOKEN={token}\nSERVO_LOG_LEVEL=DEBUG\n"
                    )
                    typer.echo(".env file initialized")

            customize = typer.confirm(
                f"Generating servo.yaml. Do you want to select the connectors?"
            )
            if customize:
                types = servo.Assembly.all_connector_types()
                types.remove(servo.Servo)

                check = bullet.Check(
                    "\nWhich connectors do you want to activate? [space to (de)select]",
                    choices=list(map(lambda c: c.name, types)),
                    check=" √",
                    indent=0,
                    margin=4,
                    align=4,
                    pad_right=4,
                    check_color=bullet.colors.bright(bullet.colors.foreground["green"]),
                    check_on_switch=bullet.colors.bright(
                        bullet.colors.foreground["black"]
                    ),
                    background_color=bullet.colors.background["black"],
                    background_on_switch=bullet.colors.background["white"],
                    word_color=bullet.colors.foreground["white"],
                    word_on_switch=bullet.colors.foreground["black"],
                )

                result = check.launch()
                connectors = list(
                    filter(
                        None,
                        map(
                            lambda c: c.__default_name__ if c.name in result else None,
                            servo.Assembly.all_connector_types(),
                        ),
                    )
                )
            else:
                connectors = None

            typer_click_object = typer.main.get_group(self)
            context.invoke(
                typer_click_object.commands["generate"], connectors=connectors
            )

        show_cli = CLI(name="show", help="Display one or more resources")

        @show_cli.command()
        def connectors(
            context: Context,
            verbose: bool = typer.Option(
                False, "--verbose", "-v", help="Display verbose info"
            ),
        ) -> None:
            """Manage connectors"""

            headers = ["NAME", "TYPE", "VERSION", "DESCRIPTION"]
            if verbose:
                headers += ["HOMEPAGE", "MATURITY", "LICENSE"]

            for servo_ in context.assembly.servos:
                if context.servo_ and context.servo_ != servo_:
                    continue

                connectors = servo_.all_connectors
                table = []
                connectors_by_type = {}
                for c in connectors:
                    c_type = c.__class__ if isinstance(c, servo.BaseConnector) else c
                    c_list = connectors_by_type.get(c_type, [])
                    c_list.append(c)
                    connectors_by_type[c_type] = c_list

                for connector_type in connectors_by_type.keys():
                    names = list(
                        map(lambda c: c.name, connectors_by_type[connector_type])
                    )
                    row = [
                        "\n".join(names),
                        connector_type.name,
                        connector_type.version,
                        connector_type.description,
                    ]
                    if verbose:
                        row += [
                            connector_type.homepage,
                            connector_type.maturity,
                            connector_type.license,
                        ]
                    table.append(row)

                if len(context.assembly.servos) > 1:
                    typer.echo(f"{servo_.name}")
                typer.echo(tabulate(table, headers, tablefmt="plain") + "\n")

        @show_cli.command()
        def components(context: Context) -> None:
            """Display adjustable components"""
            for servo_ in context.assembly.servos:
                if context.servo_ and context.servo_ != servo_:
                    continue

                results = run_async(servo_.dispatch_event(servo.Events.components))
                headers = ["COMPONENT", "SETTINGS", "CONNECTOR"]
                table = []
                for result in results:
                    for component in result.value:
                        settings_list = sorted(
                            list(
                                map(
                                    lambda s: f"{s.name}={s.human_readable_value} {s.summary()}",
                                    component.settings,
                                )
                            )
                        )
                        row = [
                            component.name,
                            "\n".join(settings_list),
                            result.connector.name,
                        ]
                        table.append(row)

                    if len(context.assembly.servos) > 1:
                        typer.echo(f"{servo_.name}")
                    typer.echo(tabulate(table, headers, tablefmt="plain") + "\n")

        @show_cli.command()
        def events(
            context: Context,
            all: bool = typer.Option(
                None,
                "--all",
                "-a",
                help="Include models from all available connectors",
            ),
            by_connector: bool = typer.Option(
                None,
                "--by-connector",
                "-c",
                help="Display output by connector instead of event",
            ),
            before: bool = typer.Option(
                None,
                help="Display before event handlers",
            ),
            on: bool = typer.Option(
                None,
                help="Display on event handlers",
            ),
            after: bool = typer.Option(
                None,
                help="Display after event handlers",
            ),
        ) -> None:
            """
            Display event handler info
            """
            for servo_ in context.assembly.servos:
                if context.servo and context.servo != servo_:
                    continue

                event_handlers: List[servo.EventHandler] = []
                connectors = (
                    context.assembly.all_connector_types()
                    if all
                    else servo_.all_connectors
                )

                for connector in connectors:
                    event_handlers.extend(connector.__event_handlers__)

                # If we have switched any on the preposition only include explicitly flagged
                preposition_switched = list(
                    filter(lambda s: s is not None, (before, on, after))
                )
                if preposition_switched:
                    if False in preposition_switched:
                        # Handle explicit exclusions
                        prepositions = [
                            servo.Preposition.before,
                            servo.Preposition.on,
                            servo.Preposition.after,
                        ]
                        if before == False:
                            prepositions.remove(servo.Preposition.before)
                        if on == False:
                            prepositions.remove(servo.Preposition.on)
                        if after == False:
                            prepositions.remove(servo.Preposition.after)
                    else:
                        # Add explicit inclusions
                        prepositions = []
                        if before:
                            prepositions.append(servo.Preposition.before)
                        if on:
                            prepositions.append(servo.Preposition.on)
                        if after:
                            prepositions.append(servo.Preposition.after)
                else:
                    prepositions = [
                        servo.Preposition.before,
                        servo.Preposition.on,
                        servo.Preposition.after,
                    ]

                sorted_event_names = sorted(
                    list(set(map(lambda handler: handler.event.name, event_handlers)))
                )
                table = []

                if by_connector:
                    headers = ["CONNECTOR", "EVENTS"]
                    connector_types_by_name = dict(
                        map(
                            lambda handler: (
                                handler.connector_type.name,
                                connector,
                            ),
                            event_handlers,
                        )
                    )
                    sorted_connector_names = sorted(connector_types_by_name.keys())
                    for connector_name in sorted_connector_names:
                        connector_types_by_name[connector_name]
                        event_labels = []
                        for event_name in sorted_event_names:
                            for preposition in prepositions:
                                handlers = list(
                                    filter(
                                        lambda h: h.event.name == event_name
                                        and h.preposition == preposition
                                        and h.connector_type.name == connector_name,
                                        event_handlers,
                                    )
                                )
                                if handlers:
                                    if preposition != servo.Preposition.on:
                                        event_labels.append(f"{preposition} {event_name}")
                                    else:
                                        event_labels.append(event_name)

                        row = [connector_name, "\n".join(event_labels)]
                        table.append(row)
                else:
                    headers = ["EVENT", "CONNECTORS"]
                    for event_name in sorted_event_names:
                        for preposition in prepositions:
                            handlers = list(
                                filter(
                                    lambda h: h.event.name == event_name
                                    and h.preposition == preposition,
                                    event_handlers,
                                )
                            )
                            if handlers:
                                sorted_connector_names = sorted(
                                    list(
                                        set(
                                            map(
                                                lambda handler: handler.connector_type.name,
                                                handlers,
                                            )
                                        )
                                    )
                                )
                                if preposition != servo.Preposition.on:
                                    label = f"{preposition} {event_name}"
                                else:
                                    label = event_name
                                row = [label, "\n".join(sorted(sorted_connector_names))]
                                table.append(row)

                if len(context.assembly.servos) > 1:
                    typer.echo(f"{servo_.name}")
                typer.echo(tabulate(table, headers, tablefmt="plain") + "\n")

        @show_cli.command()
        def metrics(context: Context) -> None:
            """
            Display measurable metrics
            """
            for servo_ in context.assembly.servos:
                if context.servo and context.servo != servo_:
                    continue

                metrics_to_connectors: Dict[str, Tuple[str, Set[str]]] = {}
                results = run_async(servo_.dispatch_event("metrics"))
                for result in results:
                    for metric in result.value:
                        units_and_connectors = metrics_to_connectors.get(
                            metric.name, [metric.unit, set()]
                        )
                        units_and_connectors[1].add(result.connector.__class__.name)
                        metrics_to_connectors[metric.name] = units_and_connectors

                headers = ["METRIC", "UNIT", "CONNECTORS"]
                table = []
                for metric in sorted(metrics_to_connectors.keys()):
                    units_and_connectors = metrics_to_connectors[metric]
                    unit = units_and_connectors[0]
                    unit_str = f"{unit.name} ({unit.value})"
                    row = [metric, unit_str, "\n".join(sorted(units_and_connectors[1]))]
                    table.append(row)

                if len(context.assembly.servos) > 1:
                    typer.echo(f"{servo_.name}")
                typer.echo(tabulate(table, headers, tablefmt="plain") + "\n")

        self.add_cli(show_cli, section=Section.assembly)

        @self.command("list", section=Section.assembly)
        def list_(
            context: Context,
        ) -> None:
            """List servos in the assembly"""
            headers = ["NAME", "OPTIMIZER", "DESCRIPTION"]
            table = []

            for servo_ in context.assembly.servos:
                row = [
                    servo_.name,
                    servo_.optimizer.id,
                    servo_.description or "-",
                ]
                table.append(row)

            typer.echo(tabulate(table, headers, tablefmt="plain"))

        @self.command(section=Section.assembly)
        def connectors(
            context: Context,
            verbose: bool = typer.Option(
                False, "--verbose", "-v", help="Display verbose info"
            ),
        ) -> None:
            """Display active connectors"""
            table = []
            headers = ["NAME", "VERSION", "DESCRIPTION"]
            if verbose:
                headers += ["HOMEPAGE", "MATURITY", "LICENSE"]

            for connector_type in servo.Assembly.all_connector_types():
                row = [
                    connector_type.__default_name__,
                    connector_type.version,
                    connector_type.description,
                ]
                if verbose:
                    row += [
                        connector_type.homepage,
                        connector_type.maturity,
                        connector_type.license,
                    ]
                table.append(row)

            typer.echo(tabulate(table, headers, tablefmt="plain") + "\n")

    def add_ops_commands(self, section=Section.ops) -> None:
        @self.command(section=section)
        def run(
            context: Context,
            check: bool = typer.Option(
                False,
                "--check",
                "-c",
                help="Verify all checks pass before running",
                envvar="SERVO_RUN_CHECK",
            ),
        ) -> None:
            """
            Run the servo
            """
            if check:
                typer_click_object = typer.main.get_group(self)
                context.invoke(
                    typer_click_object.commands["check"], exit_on_success=False
                )

            if context.assembly:
                servo.runner.AssemblyRunner(context.assembly).run()
            else:
                raise typer.Abort("failed to assemble servo")

        def validate_connectors_respond_to_event(
            connectors: Iterable[servo.BaseConnector], event: str
        ) -> None:
            for connector in connectors:
                if not connector.responds_to_event(event):
                    raise typer.BadParameter(
                        f"connectors of type '{connector.__class__.__name__}' do not respond to the event \"{event}\" (name='{connector.name}')"
                    )

        @self.command(section=section)
        def check(
            context: Context,
            connectors: Optional[List[str]] = typer.Argument(
                None,
                help="Connectors to check",
            ),
            name: Optional[List[str]] = typer.Option(
                False, "--name", "-n", help="Filter by name"
            ),
            id: Optional[List[str]] = typer.Option(
                False, "--id", "-i", help="Filter by ID"
            ),
            tag: Optional[List[str]] = typer.Option(
                False, "--tag", "-t", help="Filter by tag"
            ),
            halt_on: Optional[servo.ErrorSeverity] = typer.Option(
                servo.ErrorSeverity.CRITICAL,
                "--halt-on",
                "-h",
                help="Halt running on failure severity",
            ),
            verbose: bool = typer.Option(
                False, "--verbose", "-v", help="Display verbose output"
            ),
            quiet: bool = typer.Option(
                False,
                "--quiet",
                "-q",
                help="Do not echo generated output to stdout",
            ),
            progressive: bool = typer.Option(
                False,
                "--progressive",
                "-p",
                help="Execute checks and emit output progressively",
            ),
            wait: Optional[str] = typer.Option(
                None,
                "--wait",
                "-w",
                help="Wait for checks to pass",
                metavar="[TIMEOUT]",
            ),
            delay: Optional[str] = typer.Option(
                "15s",
                "--delay",
                "-d",
                help="Delay duration. Requires --wait",
                metavar="[DURATION]",
            ),
            exit_on_success: bool = typer.Option(True, hidden=True),
        ) -> None:
            """
            Check that the servo is ready to run
            """
            # FIXME: temporary workaround until I can unwind Context overload
            if isinstance(context, click.core.Context):
                context = context.parent

            def parse_re(
                value: Optional[List[str]],
            ) -> Union[None, List[str], Pattern[str]]:
                if value and len(value) == 1:
                    val = value[0]
                    if val[:1] == "/" and val[-1] == "/":
                        return re.compile(val[1:-1])

                return value

            def parse_csv(
                value: Optional[List[str]],
            ) -> Union[None, List[str], Pattern[str]]:
                if value and len(value) == 1:
                    val = value[0]
                    if "," in val:
                        return list(map(lambda v: v.strip(), val.split(",")))

                return value

            def parse_id(
                value: Optional[List[str]],
            ) -> Union[None, List[str], Pattern[str]]:
                v = parse_re(value)
                if not isinstance(v, Pattern):
                    return parse_csv(v)

                return v

            async def check_servo(servo_: servo.Servo) -> bool:
                # Validate that explicit args support check events
                connector_objs = (
                    self.connectors_named(connectors, servo_) if connectors
                    else list(
                        filter(
                            lambda c: c.responds_to_event(servo.Events.check),
                            servo_.all_connectors,
                        )
                    )

                )
                validate_connectors_respond_to_event(connector_objs, servo.Events.check)

                if os.getenv("KUBERNETES_SERVICE_HOST"):
                    kubernetes_asyncio.config.load_incluster_config()
                else:
                    kubeconfig = os.getenv("KUBECONFIG") or kubernetes_asyncio.config.kube_config.KUBE_CONFIG_DEFAULT_LOCATION
                    kubeconfig_path = pathlib.Path(os.path.expanduser(kubeconfig))
                    if kubeconfig_path.exists():
                        await kubernetes_asyncio.config.load_kube_config(
                            config_file=os.path.expandvars(kubeconfig_path),
                        )

                progress = servo.DurationProgress(servo.Duration(wait or 0))
                while not progress.finished:
                    if not progress.started:
                        # run at least one time
                        progress.start()

                    args = dict(name=parse_re(name), id=parse_id(id), tags=parse_csv(tag))
                    constraints = dict(filter(lambda i: bool(i[1]), args.items()))
                    results: List[servo.EventResult] = await servo_.dispatch_event(
                        servo.Events.check,
                        servo.CheckFilter(**constraints),
                        include=connector_objs,
                        halt_on=halt_on,
                    ) or []

                    if progressive:
                        if result := next(iter(results), None):
                            checks: List[servo.Check] = result.value
                            next_failure = next(filter(lambda c: c.success is False, checks), None)

                            if next_failure:
                                servo.logger.warning(f"Check '{next_failure.name}' failed: {next_failure.message}")
                            else:
                                # nothing is left failing, spike the football
                                typer.echo(f"🔥 All checks are now passing.")
                                done = True
                    else:
                        table = []
                        ready = True
                        if verbose:
                            headers = ["CONNECTOR", "CHECK", "ID", "TAGS", "STATUS", "MESSAGE"]
                            for result in results:
                                checks: List[servo.Check] = result.value
                                names, ids, tags, statuses, comments = [], [], [], [], []
                                for check in checks:
                                    names.append(check.name)
                                    ids.append(check.id)
                                    tags.append(", ".join(check.tags) if check.tags else "-")
                                    statuses.append(_check_status_to_str(check))
                                    comments.append(textwrap.shorten(check.message or "-", 70))
                                    ready &= check.success

                                if not names:
                                    continue

                                row = [
                                    result.connector.name,
                                    "\n".join(names),
                                    "\n".join(ids),
                                    "\n".join(tags),
                                    "\n".join(statuses),
                                    "\n".join(comments),
                                ]
                                table.append(row)
                        else:
                            headers = ["CONNECTOR", "STATUS", "ERRORS"]
                            for result in results:
                                checks: List[servo.Check] = result.value
                                if not checks:
                                    continue

                                success = True
                                errors = []
                                for check in checks:
                                    success &= check.passed
                                    check.success or errors.append(
                                        f"{check.name}: {textwrap.wrap(check.message or '-')}"
                                    )
                                ready &= success
                                status = "√ PASSED" if success else "X FAILED"
                                message = functools.reduce(
                                    lambda m, e: m
                                    + f"({errors.index(e) + 1}/{len(errors)}) {e}\n",
                                    errors,
                                    "",
                                )
                                row = [result.connector.name, status, message]
                                table.append(row)

                        # Output table
                        if not quiet:
                            typer.echo(tabulate(table, headers, tablefmt="plain"))

                        if ready:
                            return True
                        elif progress.finished:
                            # Don't log a timeout if we aren't running in wait mode
                            if progress.duration:
                                self.logger.error(
                                    f"timed out waiting for checks to pass {progress.duration}"
                                )
                            return False

                    if not done and delay is not None:
                        self.logger.info(
                            f"waiting for {delay} before rerunning failing checks"
                        )
                        time.sleep(servo.Duration(delay).total_seconds())

            # Check all targeted servos
            if context.servo:
                ready = run_async(check_servo(context.servo))
            else:
                results = run_async(
                    asyncio.gather(
                        *list(
                            map(
                                lambda s: check_servo(s), context.assembly.servos
                            )
                        ),
                        return_exceptions=True
                    )
                )
                ready = functools.reduce(lambda x, y: x and y, results)

            # Return instead of exiting if we are being invoked
            if ready and not exit_on_success:
                return

            exit_code = 0 if ready else 1
            raise typer.Exit(exit_code)

        @self.command(section=section)
        def describe(
            context: Context,
            connectors: Optional[List[str]] = typer.Argument(
                None,
                help="The connectors to describe"
            ),
        ) -> None:
            """
            Display current state of servo resources
            """

            for servo_ in context.assembly.servos:
                if context.servo_ and context.servo_ != servo_:
                    continue

                # Validate that explicit args support describe events
                connectors_ = (
                    self.connectors_named(connectors, servo_=servo_) if connectors
                    else servo_.all_connectors
                )

                results: List[servo.EventResult] = run_async(
                    servo_.dispatch_event(servo.Events.describe, include=connectors_)
                )
                headers = ["CONNECTOR", "COMPONENTS", "METRICS"]
                table = []
                for result in results:
                    description: servo.Description = result.value
                    components_column = []
                    for component in description.components:
                        for setting in component.settings:
                            components_column.append(
                                f"{component.name}.{setting.name}={setting.human_readable_value}"
                            )

                    metrics_column = []
                    for metric in description.metrics:
                        metrics_column.append(f"{metric.name} ({metric.unit})")

                    result.connector.name
                    row = [
                        result.connector.name,
                        "\n".join(components_column),
                        "\n".join(metrics_column),
                    ]
                    table.append(row)

                if len(context.assembly.servos) > 1:
                    typer.echo(f"{servo_.name}")
                typer.echo(tabulate(table, headers, tablefmt="plain"))

        def metrics_callback(
            context: typer.Context, value: Optional[List[str]]
        ) -> Optional[List[servo.Metric]]:
            if not value:
                return value

            all_metrics_by_name: Dict[str, servo.Metric] = {}
            results = run_async(context.servo.dispatch_event("metrics"))
            for result in results:
                for metric in result.value:
                    all_metrics_by_name[metric.name] = metric

            metrics: List[servo.Metric] = []
            for metric_name in value:
                if metric := all_metrics_by_name.get(metric_name, None):
                    metrics.append(metric)
                else:
                    raise typer.BadParameter(f"no metric found named '{metric_name}'")

            return metrics

        @self.command(section=section)
        def measure(
            context: Context,
            metrics: Optional[List[str]] = typer.Argument(
                None, help="Metrics to measure", callback=metrics_callback
            ),
            connectors: Optional[List[str]] = typer.Option(
                None,
                "--connectors",
                "-c",
                help="Connectors to measure from",
                metavar="[CONNECTORS]...",
            ),
            duration: Optional[str] = typer.Option(
                "0",
                "--duration",
                "-d",
                help="Duration of the measurement",
                metavar="DURATION",
                callback=self.duration_callback,
            ),
            verbose: bool = typer.Option(
                False,
                "--verbose",
                "-v",
                help="Display verbose output",
            ),
            humanize: bool = typer.Option(
                True,
                help="Display human readable output for units",
            ),
        ) -> None:
            """
            Capture measurements for one or more metrics
            """
            for servo_ in context.assembly.servos:
                if context.servo_ and servo_ != context.servo_:
                    continue

                connectors_ = (
                    self.connectors_named(connectors, servo_) if connectors
                    else list(
                        filter(
                            lambda c: c.responds_to_event(servo.Events.measure),
                            servo_.all_connectors,
                        )
                    )
                )

                if metrics:
                    # Filter target connectors by metrics
                    results: List[servo.EventResult] = run_async(
                        servo_.dispatch_event(
                            servo.Events.metrics, include=connectors_
                        )
                    )
                    for result in results:
                        result_metrics: List[servo.Metric] = result.value
                        metric_names: Set[str] = set(map(lambda m: m.name, result_metrics))
                        if not metric_names | set(metrics):
                            connectors.remove(result.connector)

                # Capture the measurements
                results: List[servo.EventResult] = run_async(
                    servo_.dispatch_event(
                        servo.Events.measure,
                        metrics=metrics,
                        control=servo.Control(duration=duration),
                        include=connectors_,
                    )
                )

                # FIXME: The data that is crossing connector boundaries needs to be validated
                aggregated_by_metric: Dict[
                    servo.Metric,
                    Dict[
                        str,
                        Dict[
                            servo.BaseConnector, List[Tuple[servo.Numeric, servo.Reading]]
                        ],
                    ],
                ] = {}
                metric_names = list(map(lambda m: m.name, metrics)) if metrics else None
                headers = ["METRIC", "UNIT", "READINGS"]
                table = []
                for result in results:
                    measurement = result.value
                    if not measurement:
                        continue

                    for reading in measurement.readings:
                        metric = reading.metric

                        if isinstance(reading, servo.TimeSeries):
                            metric_to_timestamp = aggregated_by_metric.get(metric, {})
                            for data_point in reading.data_points:
                                time_key = f"{data_point[0]:%Y-%m-%d %H:%M:%S}"
                                timestamp_to_connector = metric_to_timestamp.get(time_key, {})
                                values = timestamp_to_connector.get(result.connector, [])
                                values.append((data_point[1], reading))
                                timestamp_to_connector[result.connector] = values
                                metric_to_timestamp[time_key] = timestamp_to_connector

                            aggregated_by_metric[metric] = metric_to_timestamp

                        elif isinstance(reading, servo.DataPoint):
                            metric_to_timestamp = aggregated_by_metric.get(metric, {})
                            time_key = f"{reading.time:%Y-%m-%d %H:%M:%S}"
                            timestamp_to_connector = metric_to_timestamp.get(time_key, {})
                            values = timestamp_to_connector.get(result.connector, [])
                            values.append((reading.value, reading))
                            timestamp_to_connector[result.connector] = values
                            metric_to_timestamp[time_key] = timestamp_to_connector
                            aggregated_by_metric[metric] = metric_to_timestamp

                        else:
                            raise TypeError(f"unknown reading type: {reading.__class__.__name__}")


                # Print the table
                def attribute_connector(connector, reading) -> str:
                    return (
                        f"[{connector.name}{reading.id or ''}]"
                        if len(connectors) > 1
                        else ""
                    )

                headers = ["METRIC", "UNIT", "READINGS"]
                table = []
                for metric in sorted(aggregated_by_metric.keys(), key=lambda m: m.name):
                    readings_column = []
                    timestamp_to_connectors = aggregated_by_metric[metric]
                    for timestamp in sorted(timestamp_to_connectors.keys()):
                        for connector, values in timestamp_to_connectors[
                            timestamp
                        ].items():  # Dict[BaseConnector, Tuple[Numeric, Reading]]
                            readings_column.extend(
                                list(
                                    map(
                                        lambda r: f"{r[0]:.2f} ({timeago(timestamp) if humanize else timestamp}) {attribute_connector(connector, r[1])}",
                                        values,
                                    )
                                )
                            )

                    row = [
                        metric.name,
                        metric.unit,
                        "\n".join(readings_column),
                    ]
                    table.append(row)

                if len(context.assembly.servos) > 1:
                    typer.echo(f"{servo_.name}")
                typer.echo(tabulate(table, headers, tablefmt="plain") + "\n")

        @self.command(section=section)
        def inject_sidecar(
            context: Context,
            target: str = typer.Argument(
                ..., help="Deployment or Pod to inject the sidecar on (deployment/NAME or pod/NAME)"
            ),
            service: Optional[str] = typer.Option(
                None, "--service", "-s", help="Service to target"
            ),
            port: Optional[int] = typer.Option(
                None, "--port", "-p", help="Port to target"
            )
        ) -> None:
            """
            Inject an Envoy sidecar to capture metrics
            """
            if not target.startswith(("deploy/", "deployment/", "pod/")):
                raise typer.BadParameter("target must prefixed with Kubernetes object kind of \"deployment\" or \"pod\"")

            if not service or port:
                raise typer.MissingParameter("service or port must be given")

            if target.startswith("deploy"):
                connector = context.servo.get_connector("kubernetes")
                run_async(connector.inject_sidecar(deployment=target, service=service, port=port))

            elif target.startswith("pod"):
                raise typer.BadParameter("Pod sidecar injection is not yet implemented")
            else:
                raise typer.BadParameter(f"unexpected sidecar target: {target}")

        @self.command(section=section)
        def adjust(
            context: Context,
            settings: Optional[List[str]] = typer.Argument(
                None,
                help="The settings to adjust (format is [COMPONENT].[SETTING=[VALUE])",
            ),
        ) -> None:
            """
            Adjust settings for one or more components
            """

            for servo_ in context.assembly.servos:
                if context.servo_ and context.servo_ != servo_:
                    continue

                adjustments: List[servo.Adjustment] = []
                for descriptor in settings:
                    try:
                        component_name, setting_descriptor = descriptor.split(".", 1)
                        setting_name, value = setting_descriptor.split("=", 1)
                    except ValueError:
                        raise typer.BadParameter(
                            f"unable to parse setting descriptor '{descriptor}': expected format is `component.setting=value`"
                        )

                    adjustment = servo.Adjustment(
                        component_name=component_name,
                        setting_name=setting_name,
                        value=value,
                    )
                    adjustments.append(adjustment)

                results: List[servo.EventResult] = run_async(
                    servo_.dispatch_event(servo.Events.adjust, adjustments)
                )
                if not results:
                    typer.echo("adjustment failed: no connector handled the request", err=True)
                    raise typer.Exit(code=1)

                for result in results:
                    outcome = result.value

                    if isinstance(outcome, Exception):
                        message = str(outcome.get("message", "undefined"))
                        raise servo.ConnectorError(
                            f'Adjustment connector failed with error "{outcome}" and message:\n{message}'
                        )

                headers = ["CONNECTOR", "SETTINGS"]
                table = []
                for result in results:
                    description: servo.Description = result.value
                    settings_column = []
                    for component in description.components:
                        for setting in component.settings:
                            settings_column.append(
                                f"{component.name}.{setting.name}={setting.human_readable_value}"
                            )

                    result.connector.name
                    row = [
                        result.connector.name,
                        "\n".join(settings_column)
                    ]
                    table.append(row)

                    if len(context.assembly.servos) > 1:
                        typer.echo(f"{servo_.name}")
                    typer.echo(tabulate(table, headers, tablefmt="plain") + "\n")

    def add_config_commands(self, section=Section.config) -> None:
        @self.command(section=section)
        def config(
            context: Context,
            format: ConfigOutputFormat = typer.Option(
                ConfigOutputFormat.yaml, "--format", "-f", help="Select output format"
            ),
            output: typer.FileTextWrite = typer.Option(
                None, "--output", "-o", help="Output configuration to [FILE]"
            ),
            keys: Optional[List[str]] = typer.Argument(
                None, help="Display settings for specific keys"
            ),
        ) -> None:
            """
            Display configured settings
            """
            include = set(keys) if keys else None
            export_options = dict(exclude_unset=True, include=include, indent=2)

            for servo_ in context.assembly.servos:
                if context.servo_ and context.servo_ != servo_:
                    continue

                if format == ConfigOutputFormat.text:
                    pass
                else:
                    lexer = format.lexer()
                    if format == ConfigOutputFormat.yaml:
                        data = servo_.config.yaml(sort_keys=True, **export_options)
                    elif format == ConfigOutputFormat.json:
                        data = servo_.config.json(**export_options)
                    elif format == ConfigOutputFormat.dict:
                        # NOTE: Round-trip through JSON to produce primitives
                        config_dict = servo_.config.json(**export_options)
                        data = devtools.pformat(json.loads(config_dict))
                    elif format == ConfigOutputFormat.configmap:
                        configured_at = datetime.datetime.now(
                            datetime.timezone.utc
                        ).isoformat()
                        connectors = []
                        for connector in servo_.connectors:
                            connectors.append(
                                {
                                    "name": connector.name,
                                    "type": connector.full_name,
                                    "description": connector.description,
                                    "version": str(connector.version),
                                    "url": str(connector.homepage),
                                }
                            )
                        connectors_json_str = json.dumps(connectors, indent=None)

                        configmap = {
                            "apiVersion": "v1",
                            "kind": "ConfigMap",
                            "metadata": {
                                "name": "opsani-servo-config",
                                "labels": {
                                    "app.kubernetes.io/name": "servo",
                                    "app.kubernetes.io/version": str(servo_.version),
                                },
                                "annotations": {
                                    "servo.opsani.com/configured_at": configured_at,
                                    "servo.opsani.com/connectors": connectors_json_str,
                                },
                            },
                            "data": {
                                "servo.yaml": servo.utilities.yaml.PreservedScalarString(
                                    servo_.config.yaml(
                                        sort_keys=True, **export_options
                                    )
                                )
                            },
                        }
                        data = yaml.dump(
                            configmap, indent=2, sort_keys=False, explicit_start=True
                        )
                    else:
                        raise RuntimeError(
                            "no handler configured for output format {format}"
                        )

                    if output:
                        output.write(data)
                    else:
                        typer.echo(
                            pygments.highlight(
                                data, lexer, pygments.formatters.TerminalFormatter()
                            )
                        )

        @self.command(section=section)
        def schema(
            context: Context,
            all: bool = typer.Option(
                False,
                "--all",
                "-a",
                help="Include models from all available connectors",
            ),
            top_level: bool = typer.Option(
                False,
                "--top-level",
                help="Emit a top-level schema (only connector models)",
            ),
            format: SchemaOutputFormat = typer.Option(
                SchemaOutputFormat.json, "--format", "-f", help="Select output format"
            ),
            output: typer.FileTextWrite = typer.Option(
                None, "--output", "-o", help="Output schema to [FILE]"
            ),
            connector: Optional[str] = typer.Argument(
                None,
                help="Display schema for a specific connector by key or class name",
                callback=self.connectors_type_callback,
            ),
        ) -> None:
            """Display configuration schema"""
            output_data = ""

            if format == SchemaOutputFormat.text or format == SchemaOutputFormat.html:
                typer.echo("error: not yet implemented", err=True)
                raise typer.Exit(1)

            if top_level:
                CLI.assemble_from_context(context)

                if all is False and context.servo_ is None:
                    typer.echo("error: schema can only be outputted for all connectors or a single servo", err=True)
                    raise typer.Exit(1)

                if format == SchemaOutputFormat.json:
                    output_data += context.servo_.top_level_schema_json(all=all)

                elif format == SchemaOutputFormat.dict:
                    output_data += devtools.pformat(
                        context.servo_.top_level_schema(all=all)
                    )

            else:
                if connector:
                    if isinstance(connector, servo.BaseConnector):
                        config_model = connector.config.__class__
                    elif issubclass(connector, servo.BaseConnector):
                        config_model = connector.config_model()
                    else:
                        raise typer.BadParameter(
                            f"unexpected connector type '{connector.__class__.__name__}'"
                        )
                else:
                    CLI.assemble_from_context(context)

                    if context.servo_ is None:
                        typer.echo("error: schema can only be outputted for a single servo", err=True)
                        raise typer.Exit(1)

                    config_model = context.servo_.config.__class__

                if format == SchemaOutputFormat.json:
                    output_data += config_model.schema_json(indent=2)
                elif format == SchemaOutputFormat.dict:
                    output_data += devtools.pformat(config_model.schema())
                else:
                    raise RuntimeError(
                        f"no handler configured for output format {format}"
                    )

            assert output_data, "output_data not assigned"

            if output:
                output.write(output_data)
            else:
                typer.echo(
                    pygments.highlight(
                        output_data,
                        format.lexer(),
                        pygments.formatters.TerminalFormatter(),
                    )
                )

        @self.command(section=section)
        def validate(
            context: Context,
            connectors: Optional[List[str]] = typer.Argument(
                None,
                help="Connectors to validate configuration for. \nFormats: `connector`, `ConnectorClass`, `alias:connector`, `alias:ConnectorClass`",
            ),
            file: pathlib.Path = typer.Option(
                "servo.yaml",
                "--file",
                "-f",
                exists=True,
                file_okay=True,
                dir_okay=False,
                writable=False,
                readable=True,
                help="Output file to validate",
            ),
            quiet: bool = typer.Option(
                False,
                "--quiet",
                "-q",
                help="Do not echo generated output to stdout",
            ),
        ) -> None:
            """Validate a configuration"""
            try:
                configs = list(yaml.load_all(open(file), Loader=yaml.FullLoader))
                if not isinstance(configs, list):
                    raise ValueError(
                        f'error: config file "{file}" parsed to an unexpected value of type "{configs.__class__}"'
                    )

                # If we parsed an empty file, add an empty dict to work with
                if not configs:
                    configs.append({})

                # NOTE: When connector descriptor is provided the validation is constrained
                routes = self.connector_routes_callback(
                    context=context, value=connectors
                )

                for config in configs:
                    config_model, routes = servo.assembly._create_config_model(
                        config=config, routes=routes
                    )
                    config_model.parse_file(file)
            except (pydantic.ValidationError, yaml.scanner.ScannerError, KeyError) as e:
                if not quiet:
                    typer.echo(f"X Invalid configuration in {file}", err=True)
                    typer.echo(e, err=True)
                raise typer.Exit(1)

            if not quiet:
                typer.echo(f"√ Valid configuration in {file}")

        @self.command(section=section)
        def generate(
            context: Context,
            connectors: Optional[List[str]] = typer.Argument(
                None,
                help="Connectors to generate configuration for. \nFormats: `connector`, `ConnectorClass`, `alias:connector`, `alias:ConnectorClass`",
            ),
            file: pathlib.Path = typer.Option(
                "servo.yaml",
                "--file",
                "-f",
                exists=False,
                file_okay=True,
                dir_okay=False,
                writable=True,
                readable=True,
                help="Output file to write",
            ),
            defaults: bool = typer.Option(
                False,
                "--defaults",
                "-d",
                help="Include default values in the generated output",
            ),
            name: Optional[str] = typer.Option(
                None,
                "--name",
                "-n",
                help="Set the name of the generated configuration",
            ),
            standalone: bool = typer.Option(
                False,
                "--standalone",
                "-s",
                help="Exclude connectors descriptor in generated output",
            ),
            quiet: bool = typer.Option(
                False,
                "--quiet",
                "-q",
                help="Do not echo generated output to stdout",
            ),
            force: bool = typer.Option(
                False,
                "--force",
                help="Overwrite output file without prompting",
            ),
            append: bool = typer.Option(
                False,
                "--append",
                help="Append the generated output to an existing file",
            )
        ) -> None:
            """Generate a configuration"""
            exclude_unset = not defaults
            exclude = {"connectors", "servo"} if standalone else {}

            routes = (
                self.connector_routes_callback(context=context, value=connectors)
                if connectors
                else servo.connector._default_routes()
            )

            # Build a settings model from our routes
            config_model = servo.assembly._create_config_model_from_routes(routes)
            config = config_model.generate(name=name)

            if connectors and len(connectors):
                # Check is we have any aliases and assign dictionary
                connectors_dict: Dict[str, str] = {}
                aliased = False
                for identifier in connectors:
                    if ":" in identifier:
                        alias, id = identifier.split(":", 1)
                        connectors_dict[alias] = id
                        aliased = True
                    else:
                        connectors_dict[identifier] = identifier

                if aliased:
                    config.connectors = connectors_dict
                else:
                    # If there are no aliases just assign input values
                    config.connectors = connectors

            config_yaml = config.yaml(
                by_alias=True,
                exclude_unset=exclude_unset,
                exclude_defaults=exclude_unset,
                exclude=exclude,
                exclude_none=True,
            )
            if file.exists():
                if append:
                    config_docs = list(yaml.full_load_all(file.read_text()))
                    incoming_doc = yaml.full_load(config_yaml)
                    config_docs.append(incoming_doc)
                    config_yaml = yaml.dump_all(config_docs)

                elif force == False:
                    delete = typer.confirm(f"File '{file}' already exists. Overwrite it?")
                    if not delete:
                        raise typer.Abort()

            file.write_text(config_yaml)
            if not quiet:
                typer.echo(
                    pygments.highlight(
                        config_yaml,
                        pygments.lexers.YamlLexer(),
                        pygments.formatters.TerminalFormatter(),
                    )
                )
                typer.echo(f"Generated {file}")

    def add_connector_commands(self) -> None:
        for cli in ConnectorCLI.__clis__:
            self.add_cli(cli, section=Section.connectors)

        @self.command(section=Section.other)
        def version(
            context: Context,
            connector: Optional[str] = typer.Argument(
                None,
                help="Display version for a connector",
            ),
            short: bool = typer.Option(
                False,
                "--short",
                "-s",
                help="Display short version details",
            ),
            format: VersionOutputFormat = typer.Option(
                VersionOutputFormat.text, "--format", "-f", help="Select output format"
            ),
        ):
            """
            Display version
            """
            if connector:
                connector_class = servo.connector._connector_class_from_string(
                    connector
                )
                if not connector_class:
                    raise typer.BadParameter(
                        f"no connector found for key '{connector}'"
                    )
            else:
                connector_class = servo.Servo

            if short:
                if format == VersionOutputFormat.text:
                    typer.echo(connector_class.version_summary())
                elif format == VersionOutputFormat.json:
                    version_info = {
                        "name": connector_class.full_name,
                        "version": str(connector_class.version),
                        "cryptonym": connector_class.cryptonym,
                    }
                    typer.echo(json.dumps(version_info, indent=2))
                else:
                    raise typer.BadParameter(f"Unknown format '{format}'")
            else:
                if format == VersionOutputFormat.text:
                    typer.echo(connector_class.summary())
                elif format == VersionOutputFormat.json:
                    version_info = {
                        "name": connector_class.full_name,
                        "version": str(connector_class.version),
                        "cryptonym": connector_class.cryptonym,
                        "maturity": str(connector_class.maturity),
                        "description": connector_class.description,
                        "homepage": connector_class.homepage,
                        "license": str(connector_class.license),
                    }
                    typer.echo(json.dumps(version_info, indent=2))
                else:
                    raise typer.BadParameter(f"Unknown format '{format}'")

            raise typer.Exit(0)


def _run(args: Union[str, List[str]], **kwargs) -> None:
    args = shlex.split(args) if isinstance(args, str) else args
    process = subprocess.run(args, **kwargs)
    if process.returncode != 0:
        sys.exit(process.returncode)


def run_async(future: Union[asyncio.Future, asyncio.Task, Awaitable]) -> Any:
    """Run the asyncio event loop until Future is done.

    This function is a convenience alias for `asyncio.get_event_loop().run_until_complete(future)`.

    Args:
        future: The future to run.

    Returns:
        Any: The Future's result.

    Raises:
        Exception: Any exception raised during execution of the future.
    """
    return asyncio.get_event_loop().run_until_complete(future)

def print_table(table, headers) -> None:
    typer.echo(tabulate(table, headers, tablefmt="plain") + "\n")

def _check_status_to_str(check: servo.Check) -> str:
    if check.success:
        return "√ PASSED"
    else:
        if check.warning:
            return "! WARNING"
        else:
            return "X FAILED"
