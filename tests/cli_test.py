import json
import os
import re
from pathlib import Path

import pytest
import respx
import yaml
from freezegun import freeze_time
from typer import Typer
from typer.testing import CliRunner

import servo
from servo import Optimizer
from servo.cli import CLI, Context, ServoCLI
from servo.connectors.vegeta import VegetaConnector
from servo.servo import Servo
from tests.helpers import MeasureConnector


@pytest.fixture()
def cli_runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


@pytest.fixture()
def optimizer() -> Optimizer:
    return Optimizer("dev.opsani.com/servox", token="123456789")


@pytest.fixture()
def servo_cli() -> ServoCLI:
    return ServoCLI()


@pytest.fixture(autouse=True)
def servo_yaml(tmp_path: Path) -> Path:
    config_path: Path = tmp_path / "servo.yaml"
    config_path.touch()
    return config_path


@pytest.fixture()
def vegeta_config_file(servo_yaml: Path) -> Path:
    config = {
        "connectors": ["vegeta"],
        "vegeta": {"rate": 0, "target": "https://opsani.com/"},
    }
    servo_yaml.write_text(yaml.dump(config))
    return servo_yaml


def test_help(cli_runner: CliRunner, servo_cli: Typer) -> None:
    result = cli_runner.invoke(servo_cli, "--help")
    assert result.exit_code == 0
    assert "servo [OPTIONS] COMMAND [ARGS]" in result.stdout


def test_run(cli_runner: CliRunner, servo_cli: Typer) -> None:
    """Run the servo"""


def test_console(cli_runner: CliRunner, servo_cli: Typer) -> None:
    """Open an interactive console"""


def test_connectors(
    cli_runner: CliRunner, servo_cli: Typer, optimizer_env: None
) -> None:
    result = cli_runner.invoke(servo_cli, "connectors")
    assert result.exit_code == 0, f"expected exit code 0, but found {result.exit_code}: {result.stderr}"
    assert re.search("^NAME\\s+VERSION\\s+DESCRIPTION\n", result.stdout)


def test_connectors_verbose(
    cli_runner: CliRunner,
    servo_cli: Typer,
    vegeta_config_file: Path,
    optimizer_env: None,
) -> None:
    result = cli_runner.invoke(servo_cli, "connectors -v")
    assert result.exit_code == 0
    assert re.match(
        "NAME\\s+VERSION\\s+DESCRIPTION\\s+HOMEPAGE\\s+MATURITY\\s+LICENSE",
        result.stdout,
    )

def test_check_no_optimizer(cli_runner: CliRunner, servo_cli: Typer) -> None:
    result = cli_runner.invoke(servo_cli, "check")
    assert result.exit_code == 2
    assert "Error: Invalid value: An optimizer must be specified" in result.stderr


@respx.mock
def test_check(
    cli_runner: CliRunner, servo_cli: Typer, optimizer_env: None, stub_servo_yaml: Path
) -> None:
    request = respx.post("https://api.opsani.com/accounts/dev.opsani.com/applications/servox/servo")
    result = cli_runner.invoke(servo_cli, "check")
    assert request.called, f"stdout={result.stdout}, stderr={result.stderr}"
    assert result.exit_code == 0
    assert re.search("CONNECTOR\\s+STATUS", result.stdout)

@respx.mock
def test_check_multiservo(
    cli_runner: CliRunner, servo_cli: Typer, stub_multiservo_yaml: Path,
) -> None:
    request1 = respx.post(
        "https://api.opsani.com/accounts/dev.opsani.com/applications/multi-servox-1/servo"
    )
    request2 = respx.post(
        "https://api.opsani.com/accounts/dev.opsani.com/applications/multi-servox-2/servo"
    )
    result = cli_runner.invoke(servo_cli, "check")
    assert result.exit_code == 0, f"exited with non-zero status code (stdout={result.stdout}, stderr={result.stderr})"
    assert request1.called
    assert request2.called
    assert re.search("CONNECTOR\\s+STATUS", result.stdout)
    assert re.search("dev.opsani.com/multi-servox-1\\s+√ PASSED", result.stdout)
    assert re.search("dev.opsani.com/multi-servox-2\\s+√ PASSED", result.stdout)

@respx.mock
def test_check_multiservo_by_name(
    cli_runner: CliRunner, servo_cli: Typer, stub_multiservo_yaml: Path,
) -> None:
    request1 = respx.post(
        "https://api.opsani.com/accounts/dev.opsani.com/applications/multi-servox-1/servo"
    )
    request2 = respx.post(
        "https://api.opsani.com/accounts/dev.opsani.com/applications/multi-servox-2/servo"
    )
    result = cli_runner.invoke(servo_cli, "-n dev.opsani.com/multi-servox-2 check")
    assert result.exit_code == 0, f"exited with non-zero status code (stdout={result.stdout}, stderr={result.stderr})"
    assert not request1.called
    assert request2.called
    assert re.search("CONNECTOR\\s+STATUS", result.stdout)
    assert re.search("dev.opsani.com/multi-servox-1\\s+√ PASSED", result.stdout) is None
    assert re.search("dev.opsani.com/multi-servox-2\\s+√ PASSED", result.stdout)


@respx.mock
def test_check_verbose(
    cli_runner: CliRunner, servo_cli: Typer, optimizer_env: None, stub_servo_yaml: Path
) -> None:
    request = respx.post("https://api.opsani.com/accounts/dev.opsani.com/applications/servox/servo")
    result = cli_runner.invoke(servo_cli, "check -v", catch_exceptions=False)
    assert request.called
    assert result.exit_code == 0, f"result is: {result.stdout}, {result.stderr}"
    assert re.search(
        "CONNECTOR\\s+CHECK\\s+ID\\s+TAGS\\s+STATUS\\s+MESSAGE", result.stdout
    )

@pytest.mark.usefixtures("optimizer_env")
class TestShow:
    def test_help_does_not_require_optimizer_and_token(
        self, cli_runner: CliRunner, servo_cli: Typer, clean_environment
    ) -> None:
        clean_environment()
        result = cli_runner.invoke(servo_cli, "show --help")
        assert result.exit_code == 2
        assert "Error: Invalid value: An optimizer must be specified" in result.stderr

    def test_help(
        self, cli_runner: CliRunner, servo_cli: Typer
    ) -> None:
        result = cli_runner.invoke(servo_cli, "show --help")
        assert result.exit_code == 0
        assert "Display one or more resources" in result.stdout


    def test_connectors(
        self, cli_runner: CliRunner, servo_cli: Typer
    ) -> None:
        result = cli_runner.invoke(servo_cli, "show connectors", catch_exceptions=False)
        assert result.exit_code == 0
        assert re.match("NAME\\s+TYPE\\s+VERSION\\s+DESCRIPTION\n", result.stdout)


    def test_components(
        self, cli_runner: CliRunner, servo_cli: Typer, stub_servo_yaml: Path
    ) -> None:
        result = cli_runner.invoke(servo_cli, "show components", catch_exceptions=False)
        assert result.exit_code == 0
        assert re.search("COMPONENT\\s+SETTINGS\\s+CONNECTOR", result.stdout)


    def test_events_all(
        self, cli_runner: CliRunner, servo_cli: Typer, servo_yaml: Path
    ) -> None:
        result = cli_runner.invoke(servo_cli, "show events -a", catch_exceptions=False)
        assert result.exit_code == 0
        assert re.match("EVENT\\s+CONNECTORS", result.stdout)
        assert re.search("^check", result.stdout, flags=re.MULTILINE)
        assert re.search("^adjust\\s.+", result.stdout, flags=re.MULTILINE)
        assert re.search("^components\\s.+", result.stdout, flags=re.MULTILINE)


    def test_events_includes_servo(
        self, cli_runner: CliRunner, servo_cli: Typer,
    ) -> None:
        result = cli_runner.invoke(servo_cli, "show events", catch_exceptions=False)
        assert result.exit_code == 0
        assert re.search("Servo", result.stdout)


    def test_events_on(
        self, cli_runner: CliRunner, servo_cli: Typer, stub_servo_yaml: Path
    ) -> None:
        result = cli_runner.invoke(servo_cli, "show events --on", catch_exceptions=False)
        assert result.exit_code == 0
        assert re.match("EVENT\\s+CONNECTORS", result.stdout)
        assert re.search("check\\s+Servo\n", result.stdout)
        assert re.search("measure\\s+Measure\n", result.stdout)
        assert not re.search("before measure\\s+Measure", result.stdout)
        assert not re.search("after measure\\s+Measure", result.stdout)
        assert len(result.stdout.split("\n")) > 3


    def test_events_no_on(
        self, cli_runner: CliRunner, servo_cli: Typer, stub_servo_yaml: Path
    ) -> None:
        result = cli_runner.invoke(servo_cli, "show events --no-on", catch_exceptions=False)
        assert result.exit_code == 0
        assert re.match("EVENT\\s+CONNECTORS", result.stdout)
        assert not re.search("check\\s+Servo\n", result.stdout)
        assert not re.search("^measure\\s+Measure\n", result.stdout)
        assert re.search("after measure\\s+Measure", result.stdout)


    def test_events_after_on(
        self, cli_runner: CliRunner, servo_cli: Typer, stub_servo_yaml: Path
    ) -> None:
        result = cli_runner.invoke(
            servo_cli, "show events --after --on", catch_exceptions=False
        )
        assert result.exit_code == 0
        assert re.match("EVENT\\s+CONNECTORS", result.stdout)
        assert re.search("check\\s+Servo\n", result.stdout)
        assert not re.search("^measure\\s+Measure\n", result.stdout)
        assert re.search("after measure\\s+Measure", result.stdout)


    def test_events_no_on_before(
        self, cli_runner: CliRunner, servo_cli: Typer, stub_servo_yaml: Path
    ) -> None:
        result = cli_runner.invoke(
            servo_cli, "show events --no-on --before", catch_exceptions=False
        )
        assert result.exit_code == 0
        assert re.match("EVENT\\s+CONNECTORS", result.stdout)
        assert not re.search("check\\s+Servo\n", result.stdout)
        assert not re.search("^measure\\s+Measure\n", result.stdout)
        assert re.search("before measure\\s+Measure", result.stdout)
        assert re.search("after measure\\s+Measure", result.stdout)


    def test_events_no_after(
        self, cli_runner: CliRunner, servo_cli: Typer, stub_servo_yaml: Path
    ) -> None:
        result = cli_runner.invoke(
            servo_cli, "show events --no-after", catch_exceptions=False
        )
        assert result.exit_code == 0
        assert re.match("EVENT\\s+CONNECTORS", result.stdout)
        assert re.search("check\\s+Servo\n", result.stdout)
        assert re.search("measure\\s+Measure\n", result.stdout)
        assert re.search("before measure\\s+Measure", result.stdout)
        assert not re.search("after measure\\s+Measure", result.stdout)


    def test_events_by_connector(
        self, cli_runner: CliRunner, servo_cli: Typer, stub_servo_yaml: Path
    ) -> None:
        result = cli_runner.invoke(
            servo_cli, "show events --by-connector", catch_exceptions=False
        )
        assert result.exit_code == 0
        assert re.match("CONNECTOR\\s+EVENTS", result.stdout)
        assert re.search("Servo\\s+check\n", result.stdout)
        assert re.search(
            "Adjust\\s+adjust\n\\s+components\n\\s+describe",
            result.stdout,
            flags=re.MULTILINE,
        )
        assert re.search(
            "Measure\\s+describe\n\\s+before measure\n\\s+measure\n\\s+after measure\n\\s+metrics",
            result.stdout,
            flags=re.MULTILINE,
        )


    def test_events_empty_config_file(
        self, cli_runner: CliRunner, servo_cli: Typer, servo_yaml: Path
    ) -> None:
        result = cli_runner.invoke(servo_cli, "show events", catch_exceptions=False)
        assert result.exit_code == 0
        assert re.match("EVENT\\s+CONNECTORS", result.stdout), f"Failed to match with output: {result.stdout}"
        assert "check    Servo\n" in result.stdout
        assert len(result.stdout.split("\n")) == 4


    def test_metrics(
        self, cli_runner: CliRunner, servo_cli: Typer, stub_servo_yaml: Path
    ) -> None:
        result = cli_runner.invoke(servo_cli, "show metrics", catch_exceptions=False)
        assert result.exit_code == 0
        assert re.match("METRIC\\s+UNIT\\s+CONNECTORS", result.stdout)

    @pytest.mark.usefixtures("stub_multiservo_yaml")
    class TestMultiservo:
        @pytest.fixture
        def optimizer_env(self) -> None:
            # NOTE: zero out the optimizer_env fixture as you can't use them
            # under multiservo
            pass

        def test_connectors(
            self, cli_runner: CliRunner, servo_cli: Typer
        ) -> None:
            result = cli_runner.invoke(servo_cli, "show connectors", catch_exceptions=False)
            assert result.exit_code == 0, f"Non-zero exit status code: stdout={result.stdout}, stderr={result.stderr}"
            assert re.match("dev.opsani.com/multi-servox-1\nNAME\\s+TYPE\\s+VERSION\\s+DESCRIPTION\n", result.stdout)

        def test_connectors_by_name(
            self, cli_runner: CliRunner, servo_cli: Typer
        ) -> None:
            result = cli_runner.invoke(servo_cli, "-n dev.opsani.com/multi-servox-2 show connectors", catch_exceptions=False)
            assert result.exit_code == 0, f"Non-zero exit status code: stdout={result.stdout}, stderr={result.stderr}"
            assert re.match("dev.opsani.com/multi-servox-2\nNAME\\s+TYPE\\s+VERSION\\s+DESCRIPTION\n", result.stdout)

        def test_components(
            self, cli_runner: CliRunner, servo_cli: Typer
        ) -> None:
            result = cli_runner.invoke(servo_cli, "show components", catch_exceptions=False)
            assert result.exit_code == 0
            assert re.search("COMPONENT\\s+SETTINGS\\s+CONNECTOR", result.stdout)
            assert re.search("dev.opsani.com/multi-servox-1", result.stdout)
            assert re.search("dev.opsani.com/multi-servox-2", result.stdout)
            assert re.search("main\\s+cpu=3 RangeSetting\\(range=\\[0..10\\], step=1\\)\\s+adjust", result.stdout)

        def test_components_by_name(
            self, cli_runner: CliRunner, servo_cli: Typer
        ) -> None:
            result = cli_runner.invoke(servo_cli, "-n dev.opsani.com/multi-servox-2 show components", catch_exceptions=False)
            assert result.exit_code == 0
            assert re.search("COMPONENT\\s+SETTINGS\\s+CONNECTOR", result.stdout)
            assert re.search("dev.opsani.com/multi-servox-1", result.stdout) is None
            assert re.search("dev.opsani.com/multi-servox-2", result.stdout)
            assert re.search("main\\s+cpu=3 RangeSetting\\(range=\\[0..10\\], step=1\\)\\s+adjust", result.stdout)

        def test_events(
            self, cli_runner: CliRunner, servo_cli: Typer
        ) -> None:
            result = cli_runner.invoke(servo_cli, "show events", catch_exceptions=False)
            assert result.exit_code == 0
            assert re.search("EVENT\\s+CONNECTORS", result.stdout)
            assert re.search("dev.opsani.com/multi-servox-1", result.stdout)
            assert re.search("dev.opsani.com/multi-servox-2", result.stdout)

        def test_events_by_name(
            self, cli_runner: CliRunner, servo_cli: Typer
        ) -> None:
            result = cli_runner.invoke(servo_cli, "-n dev.opsani.com/multi-servox-2 show events", catch_exceptions=False)
            assert result.exit_code == 0
            assert re.search("EVENT\\s+CONNECTORS", result.stdout)
            assert re.search("dev.opsani.com/multi-servox-1", result.stdout) is None
            assert re.search("dev.opsani.com/multi-servox-2", result.stdout)

        def test_metrics(
            self, cli_runner: CliRunner, servo_cli: Typer
        ) -> None:
            result = cli_runner.invoke(servo_cli, "show metrics", catch_exceptions=False)
            assert result.exit_code == 0
            assert re.search("METRIC\\s+UNIT\\s+CONNECTORS", result.stdout)
            assert re.search("dev.opsani.com/multi-servox-1", result.stdout)
            assert re.search("dev.opsani.com/multi-servox-2", result.stdout)
            assert re.search("error_rate\\s+REQUESTS_PER_MINUTE\\s+\\(rpm\\)\\s+Measure", result.stdout)
            assert re.search("throughput\\s+REQUESTS_PER_MINUTE\\s+\\(rpm\\)\\s+Measure", result.stdout)

        def test_metrics_by_name(
            self, cli_runner: CliRunner, servo_cli: Typer
        ) -> None:
            result = cli_runner.invoke(servo_cli, "-n dev.opsani.com/multi-servox-2 show metrics", catch_exceptions=False)
            assert result.exit_code == 0, f"non-zero exit code. stdout={result.stdout}, stderr={result.stderr}"
            assert re.search("METRIC\\s+UNIT\\s+CONNECTORS", result.stdout)
            assert re.search("dev.opsani.com/multi-servox-1", result.stdout) is None
            assert re.search("dev.opsani.com/multi-servox-2", result.stdout)
            assert re.search("error_rate\\s+REQUESTS_PER_MINUTE\\s+\\(rpm\\)\\s+Measure", result.stdout)
            assert re.search("throughput\\s+REQUESTS_PER_MINUTE\\s+\\(rpm\\)\\s+Measure", result.stdout)


def test_version(cli_runner: CliRunner, servo_cli: Typer) -> None:
    result = cli_runner.invoke(servo_cli, "version")
    assert result.exit_code == 0
    assert f"Servo v{servo.__version__}" in result.stdout


def test_version_no_optimizer(cli_runner: CliRunner, servo_cli: Typer) -> None:
    result = cli_runner.invoke(servo_cli, "version", catch_exceptions=False)
    assert result.exit_code == 0
    assert f"Servo v{servo.__version__}" in result.stdout


def test_config(
    cli_runner: CliRunner,
    servo_cli: Typer,
    vegeta_config_file: Path,
    optimizer_env: None,
) -> None:
    result = cli_runner.invoke(servo_cli, "config")
    assert result.exit_code == 0
    assert "connectors:" in result.stdout

def test_config_multiservo(
    cli_runner: CliRunner,
    servo_cli: Typer,
    stub_multiservo_yaml: Path,
) -> None:
    result = cli_runner.invoke(servo_cli, "config")
    assert result.exit_code == 0
    assert "connectors:" in result.stdout

def test_config_multiservo_named(
    cli_runner: CliRunner,
    servo_cli: Typer,
    stub_multiservo_yaml: Path,
) -> None:
    result = cli_runner.invoke(servo_cli, "-n dev.opsani.com/multi-servox-2 config")
    assert result.exit_code == 0
    assert "connectors:" in result.stdout

def test_run_with_empty_config_file(
    cli_runner: CliRunner,
    servo_cli: Typer,
    servo_yaml: Path,
    optimizer_env: None,
) -> None:
    result = cli_runner.invoke(servo_cli, "config", catch_exceptions=False)
    assert result.exit_code == 0, f"RESULT: {result.stderr}"
    assert "{}" in result.stdout


def test_run_with_malformed_config_file(
    cli_runner: CliRunner,
    servo_cli: Typer,
    servo_yaml: Path,
    optimizer_env: None,
) -> None:
    servo_yaml.write_text("</\n\n..:989890j\n___*")
    with pytest.raises(TypeError) as e:
        cli_runner.invoke(servo_cli, "config", catch_exceptions=False)
    assert "parsed to an unexpected value of type" in str(e)


def test_config_with_bad_connectors_key(
    cli_runner: CliRunner,
    servo_cli: Typer,
    servo_yaml: Path,
    optimizer_env: None,
) -> None:
    servo_yaml.write_text("connectors: [invalid]\n")
    result = cli_runner.invoke(servo_cli, "config", catch_exceptions=False)
    assert "fatal: invalid configuration: no connector found for the identifier \"invalid\"" in result.stderr


def test_config_yaml(
    cli_runner: CliRunner,
    servo_cli: Typer,
    vegeta_config_file: Path,
    optimizer_env: None,
) -> None:
    result = cli_runner.invoke(servo_cli, "config -f yaml", catch_exceptions=False)
    assert result.exit_code == 0
    assert "connectors:" in result.stdout


def test_config_yaml_file(
    cli_runner: CliRunner,
    servo_cli: Typer,
    vegeta_config_file: Path,
    tmp_path: Path,
    optimizer_env: None,
) -> None:
    path = tmp_path / "settings.yaml"
    result = cli_runner.invoke(servo_cli, f"config -f yaml -o {path}")
    assert result.exit_code == 0
    assert "connectors:" in path.read_text()


@freeze_time("2020-01-01")
def test_config_configmap_file(
    cli_runner: CliRunner,
    servo_cli: Typer,
    vegeta_config_file: Path,
    tmp_path: Path,
    optimizer_env: None,
    mocker,
) -> None:
    mocker.patch.object(Servo, "version", "100.0.0")
    mocker.patch.object(VegetaConnector, "version", "100.0.0")
    path = tmp_path / "settings.yaml"
    result = cli_runner.invoke(servo_cli, f"config -f configmap -o {path}")
    assert result.exit_code == 0
    assert path.read_text() == (
        "---\n"
        "apiVersion: v1\n"
        "kind: ConfigMap\n"
        "metadata:\n"
        "  name: opsani-servo-config\n"
        "  labels:\n"
        "    app.kubernetes.io/name: servo\n"
        "    app.kubernetes.io/version: 100.0.0\n"
        "  annotations:\n"
        "    servo.opsani.com/configured_at: '2020-01-01T00:00:00+00:00'\n"
        '    servo.opsani.com/connectors: \'[{"name": "vegeta", "type": "Vegeta Connector",\n'
        '      "description": "Vegeta load testing connector", "version": "100.0.0", "url":\n'
        '      "https://github.com/opsani/vegeta-connector"}]\'\n'
        "data:\n"
        "  servo.yaml: |\n"
        "    connectors:\n"
        "    - vegeta\n"
        "    vegeta:\n"
        "      rate: '0'\n"
        "      target: https://opsani.com/\n"
    )


def test_config_json(
    cli_runner: CliRunner,
    servo_cli: Typer,
    vegeta_config_file: Path,
    optimizer_env: None,
) -> None:
    result = cli_runner.invoke(servo_cli, "config -f json")
    assert result.exit_code == 0
    settings = json.loads(result.stdout)
    assert settings["connectors"] is not None

@pytest.fixture()
def aliased_connector_cli(optimizer_env: None, servo_yaml: Path) -> ServoCLI:
    aliased_config = {
        "connectors": {
            "first": "measure",
            "second": "measure",
        },
        "first": {},
        "second": {},
    }
    servo_yaml.write_text(yaml.dump(aliased_config))

    cli = servo.cli.ConnectorCLI(MeasureConnector, name="cli-ext", help="A CLI extension")

    @cli.command()
    def attack(
        context: servo.cli.Context,
    ):
        print(f"active connector is: {context.connector.name}")

    return ServoCLI()

def test_aliased_connector_error(cli_runner: CliRunner, aliased_connector_cli: ServoCLI) -> None:
    result = cli_runner.invoke(aliased_connector_cli, f"cli-ext attack")
    assert (
        result.exit_code == 2
    ), f"Expected status code of 1 but got {result.exit_code} -- stdout: {result.stdout}\nstderr: {result.stderr}"
    assert re.search("multiple instances of \"MeasureConnector\" found in servo \"dev.opsani.com/servox\": select one of \\[\'first\', \'second\'\\]", result.stderr)

def test_aliased_connector_resolution(cli_runner: CliRunner, aliased_connector_cli: ServoCLI) -> None:
    result = cli_runner.invoke(aliased_connector_cli, f"cli-ext -c first attack")
    assert (
        result.exit_code == 0
    ), f"Expected status code of 0 but got {result.exit_code} -- stdout: {result.stdout}\nstderr: {result.stderr}"
    assert re.search("active connector is: first", result.stdout)

def test_aliased_connector_invalid_name(cli_runner: CliRunner, aliased_connector_cli: ServoCLI) -> None:
    result = cli_runner.invoke(aliased_connector_cli, f"cli-ext -c INVALID attack")
    assert (
        result.exit_code == 2
    ), f"Expected status code of 2 but got {result.exit_code} -- stdout: {result.stdout}\nstderr: {result.stderr}"
    assert re.search("no connector named \"INVALID\" of type \"MeasureConnector\" found in servo \"dev.opsani.com/servox\": select one of \\[\'first\', \'second\'\\]", result.stderr)

def test_connector_cli_not_active_in_assembly(cli_runner: CliRunner, aliased_connector_cli: ServoCLI) -> None:
    result = cli_runner.invoke(aliased_connector_cli, f"vegeta attack")
    assert (
        result.exit_code == 2
    ), f"Expected status code of 2 but got {result.exit_code} -- stdout: {result.stdout}\nstderr: {result.stderr}"
    assert re.search("no instances of \"VegetaConnector\" are active the in servo \"dev.opsani.com/servox\"", result.stderr)


def test_config_json_file(
    cli_runner: CliRunner,
    servo_cli: Typer,
    vegeta_config_file: Path,
    tmp_path: Path,
    optimizer_env: None,
) -> None:
    path = tmp_path / "settings.json"
    result = cli_runner.invoke(servo_cli, f"config -f json -o {path}")
    assert result.exit_code == 0
    settings = json.loads(path.read_text())
    assert settings["connectors"] is not None


def test_config_dict(
    cli_runner: CliRunner,
    servo_cli: Typer,
    vegeta_config_file: Path,
    optimizer_env: None,
) -> None:
    result = cli_runner.invoke(servo_cli, "config -f dict")
    assert result.exit_code == 0
    settings = eval(result.stdout)
    assert settings["connectors"] is not None


def test_config_dict_file(
    cli_runner: CliRunner,
    servo_cli: Typer,
    vegeta_config_file: Path,
    tmp_path: Path,
    optimizer_env: None,
) -> None:
    path = tmp_path / "settings.py"
    result = cli_runner.invoke(servo_cli, f"config -f dict -o {path}")
    assert result.exit_code == 0, f"failed with output {(result.stdout, result.stderr)}"
    settings = eval(path.read_text())
    assert settings["connectors"] is not None


def test_schema(cli_runner: CliRunner, servo_cli: Typer, optimizer_env: None) -> None:
    result = cli_runner.invoke(servo_cli, "schema", catch_exceptions=False)
    assert (
        result.exit_code == 0
    ), f"Expected status code of 0 but got {result.exit_code} -- stdout: {result.stdout}\nstderr: {result.stderr}"
    schema = json.loads(result.stdout)
    assert schema["title"] == "Servo Configuration Schema"


def test_schema_output_to_file(
    cli_runner: CliRunner, servo_cli: Typer, tmp_path: Path, optimizer_env: None
) -> None:
    output_path = tmp_path / "schema.json"
    result = cli_runner.invoke(servo_cli, f"schema -f json -o {output_path}")
    assert result.exit_code == 0
    schema = json.loads(output_path.read_text())
    assert schema["title"] == "Servo Configuration Schema"

def test_schema_multiservo(cli_runner: CliRunner, servo_cli: Typer, stub_multiservo_yaml: Path) -> None:
    result = cli_runner.invoke(servo_cli, "schema", catch_exceptions=False)
    assert (
        result.exit_code == 1
    ), f"Expected status code of 1 but got {result.exit_code} -- stdout: {result.stdout}\nstderr: {result.stderr}"
    assert re.search("error: schema can only be outputted for a single servo", result.stderr)

def test_schema_multiservo_by_name(cli_runner: CliRunner, servo_cli: Typer, stub_multiservo_yaml: Path) -> None:
    result = cli_runner.invoke(servo_cli, "-n dev.opsani.com/multi-servox-2 schema", catch_exceptions=False)
    assert (
        result.exit_code == 0
    ), f"Expected status code of 1 but got {result.exit_code} -- stdout: {result.stdout}\nstderr: {result.stderr}"
    schema = json.loads(result.stdout)
    assert schema["title"] == "Servo Configuration Schema"

def test_schema_multiservo_top_level(cli_runner: CliRunner, servo_cli: Typer, stub_multiservo_yaml: Path) -> None:
    result = cli_runner.invoke(servo_cli, "schema --top-level", catch_exceptions=False)
    assert (
        result.exit_code == 1
    ), f"Expected status code of 1 but got {result.exit_code} -- stdout: {result.stdout}\nstderr: {result.stderr}"
    assert re.search("error: schema can only be outputted for all connectors or a single servo", result.stderr)

def test_schema_multiservo_top_level_by_name(cli_runner: CliRunner, servo_cli: Typer, stub_multiservo_yaml: Path) -> None:
    result = cli_runner.invoke(servo_cli, "-n dev.opsani.com/multi-servox-2 schema --top-level", catch_exceptions=False)
    assert (
        result.exit_code == 0
    ), f"Expected status code of 1 but got {result.exit_code} -- stdout: {result.stdout}\nstderr: {result.stderr}"
    schema = json.loads(result.stdout)
    assert schema["title"] == "Servo Schema"

def test_schema_all(
    cli_runner: CliRunner, servo_cli: Typer, optimizer_env: None
) -> None:
    result = cli_runner.invoke(servo_cli, ["schema", "--all"])
    assert result.exit_code == 0
    schema = json.loads(result.stdout)
    assert schema["title"] == "Servo Configuration Schema"


def test_schema_top_level(
    cli_runner: CliRunner, servo_cli: Typer, optimizer_env: None
) -> None:
    result = cli_runner.invoke(servo_cli, ["schema", "--top-level"])
    assert result.exit_code == 0
    schema = json.loads(result.stdout)
    assert schema["title"] == "Servo Schema"


def test_schema_all_top_level(
    cli_runner: CliRunner, servo_cli: Typer, optimizer_env: None
) -> None:
    result = cli_runner.invoke(servo_cli, ["schema", "--top-level", "--all"])
    assert result.exit_code == 0
    schema = json.loads(result.stdout)
    assert schema["title"] == "Servo Schema"


def test_schema_top_level_dict(
    servo_cli: Typer, cli_runner: CliRunner, optimizer_env: None
) -> None:
    result = cli_runner.invoke(servo_cli, "schema -f dict --top-level")
    assert result.exit_code == 0, f"stdout: {result.stdout}\n\nstderr: {result.stderr}"
    schema = eval(result.stdout)
    assert schema["title"] == "Servo Schema"


def test_schema_top_level_dict_file_output(
    servo_cli: Typer, cli_runner: CliRunner, tmp_path: Path, optimizer_env: None
) -> None:
    path = tmp_path / "output.dict"
    result = cli_runner.invoke(servo_cli, f"schema -f dict --top-level -o {path}", catch_exceptions=False)
    assert result.exit_code == 0, f"failed with non-zero exit code: stderr={result.stderr}, stdout={result.stdout}"
    schema = eval(path.read_text())
    assert schema["title"] == "Servo Schema"


class TestCommands:
    @pytest.fixture(autouse=True)
    def test_set_defaults_via_env(self) -> None:
        os.environ["OPSANI_OPTIMIZER"] = "dev.opsani.com/test-app"
        os.environ["OPSANI_TOKEN"] = "123456789"

    def test_schema_text(self, servo_cli: Typer, cli_runner: CliRunner) -> None:
        result = cli_runner.invoke(servo_cli, "schema -f text")
        assert result.exit_code == 1
        assert "not yet implemented" in result.stderr

    def test_schema_html(self, servo_cli: Typer, cli_runner: CliRunner) -> None:
        result = cli_runner.invoke(servo_cli, "schema -f html", catch_exceptions=False)
        assert result.exit_code == 1
        assert "not yet implemented" in result.stderr

    def test_schema_dict(self, servo_cli: Typer, cli_runner: CliRunner) -> None:
        result = cli_runner.invoke(servo_cli, "schema -f dict")
        assert result.exit_code == 0
        assert "'title': 'Servo Configuration Schema'" in result.stdout

    def test_schema_dict_file_output(
        self, servo_cli: Typer, cli_runner: CliRunner, tmp_path: Path
    ) -> None:
        path = tmp_path / "output.dict"
        result = cli_runner.invoke(servo_cli, f"schema -f dict -o {path}")
        assert result.exit_code == 0
        content = path.read_text()
        assert "'title': 'Servo Configuration Schema'" in content

    def test_validate(self, cli_runner: CliRunner, servo_cli: Typer, optimizer_env: None, stub_servo_yaml: Path) -> None:
        result = cli_runner.invoke(servo_cli, f"validate -f {stub_servo_yaml}", catch_exceptions=False)
        assert result.exit_code == 0, f"non-zero exit status (result.stdout={result.stdout}, result.stderr={result.stderr})"
        assert re.match(f"√ Valid configuration in {stub_servo_yaml}", result.stdout)

    def test_validate_multiservo(self, cli_runner: CliRunner, servo_cli: Typer, stub_multiservo_yaml: Path) -> None:
        result = cli_runner.invoke(servo_cli, f"validate -f {stub_multiservo_yaml}", catch_exceptions=False)
        assert result.exit_code == 0, f"non-zero exit status (result.stdout={result.stdout}, result.stderr={result.stderr})"
        assert re.match(f"√ Valid configuration in {stub_multiservo_yaml}", result.stdout)

    def test_generate_with_name(
        self, cli_runner: CliRunner, servo_cli: Typer
    ) -> None:
        result = cli_runner.invoke(
            servo_cli, "generate --name foo -f servo.yaml measure", input="y\n"
        )
        assert result.exit_code == 0
        assert "already exists. Overwrite it?" in result.stdout
        content = yaml.full_load(open("servo.yaml"))
        assert content == {"connectors": ["measure"], "measure": {}, "name": "foo"}

    def test_generate_with_append(
        self, cli_runner: CliRunner, servo_cli: Typer, stub_servo_yaml: Path
    ) -> None:
        result = cli_runner.invoke(
            servo_cli, "generate --name foo -f servo.yaml --append measure", input="y\n"
        )
        assert result.exit_code == 0
        content = list(yaml.full_load_all(open("servo.yaml")))
        assert content == [
            {
                'adjust': {},
                'connectors': [
                    'measure',
                    'adjust',
                ],
                'measure': {
                    'description': None,
                    'name': 'stub',
                },
            },
            {
                'connectors': [
                    'measure',
                ],
                'measure': {},
                'name': 'foo',
            },
        ]

    def test_generate_prompts_to_overwrite(
        self, cli_runner: CliRunner, servo_cli: Typer, servo_yaml: Path
    ) -> None:
        result = cli_runner.invoke(
            servo_cli, "generate -f servo.yaml measure", input="y\n"
        )
        assert result.exit_code == 0
        assert "already exists. Overwrite it?" in result.stdout
        content = yaml.full_load(servo_yaml.read_text())
        assert content == {"connectors": ["measure"], "measure": {}}

    def test_generate_prompts_to_overwrite_declined(
        self, cli_runner: CliRunner, servo_cli: Typer, servo_yaml: Path
    ) -> None:
        result = cli_runner.invoke(
            servo_cli, "generate -f servo.yaml measure", input="N\n"
        )
        assert result.exit_code == 1
        assert "already exists. Overwrite it?" in result.stdout
        content = yaml.full_load(servo_yaml.read_text())
        assert content is None

    def test_generate_prompts_to_overwrite_forced(
        self, cli_runner: CliRunner, servo_cli: Typer, servo_yaml: Path
    ) -> None:
        result = cli_runner.invoke(
            servo_cli, "generate -f servo.yaml --force measure", input="y\n"
        )
        assert result.exit_code == 0
        content = yaml.full_load(servo_yaml.read_text())
        assert content == {"connectors": ["measure"], "measure": {}}

    def test_generate_connector_without_settings(
        self,
        cli_runner: CliRunner,
        servo_cli: Typer,
        optimizer_env: None,
        stub_servo_yaml: Path,
    ) -> None:
        pass

    ## CLI Lifecycle tests

    def test_loading_cli_without_specific_connectors_activates_all_optionally(
        self, cli_runner: CliRunner, servo_cli: Typer, servo_yaml: Path
    ) -> None:
        # temp config file, no connectors key
        pass

    def test_loading_cli_with_specific_connectors_only_activates_required(
        self, cli_runner: CliRunner, servo_cli: Typer, servo_yaml: Path
    ) -> None:
        pass

    def test_loading_cli_with_empty_connectors_list_disables_all(
        self, cli_runner: CliRunner, servo_cli: Typer, servo_yaml: Path
    ) -> None:
        servo_yaml.write_text(yaml.dump({"connectors": []}))
        cli_runner.invoke(servo_cli, "info")


class TestCLIFoundation:
    class TheTestCLI(CLI):
        pass

    @pytest.fixture()
    def cli(self) -> CLI:
        return TestCLIFoundation.TheTestCLI(help="This is just a test.", callback=None)

    def test_context_class_in_commands(self, cli: CLI, cli_runner: CliRunner) -> None:
        @cli.command()
        def test(context: Context) -> None:
            assert context is not None
            assert isinstance(context, Context)
            assert context.servo is None
            assert context.assembly is None
            assert context.connector is None

        result = cli_runner.invoke(cli, "", catch_exceptions=False)
        assert result.exit_code == 0

    def test_context_class_in_subcommand_groups(
        self, cli: CLI, cli_runner: CliRunner
    ) -> None:
        sub_cli = TestCLIFoundation.TheTestCLI(name="another", callback=None)

        @sub_cli.command()
        def test(context: Context) -> None:
            assert context is not None
            assert isinstance(context, Context)
            assert context.servo == None
            assert context.assembly == None
            assert context.connector == None

        cli.add_cli(sub_cli)

        result = cli_runner.invoke(cli, "another test", catch_exceptions=False)
        assert result.exit_code == 0

    def test_context_inheritance(self, cli: CLI, cli_runner: CliRunner) -> None:
        sub_cli = TestCLIFoundation.TheTestCLI(name="another", callback=None)

        @cli.callback()
        def touch_context(context: Context) -> None:
            context.obj = 31337

        @sub_cli.command()
        def test(context: Context) -> None:
            assert context is not None
            assert context.obj == 31337

        cli.add_cli(sub_cli)

        result = cli_runner.invoke(cli, "another test", catch_exceptions=False)
        assert result.exit_code == 0

    def test_that_servo_cli_commands_are_explicitly_ordered(
        self, cli: CLI, cli_runner: CliRunner
    ) -> None:
        servo_cli = ServoCLI(name="servo", callback=None)

        # Add in explicit non lexical sort order
        @servo_cli.command()
        def zzzz(context: Context) -> None:
            pass

        @servo_cli.command()
        def aaaa(context: Context) -> None:
            pass

        @servo_cli.command()
        def mmmm(context: Context) -> None:
            pass

        result = cli_runner.invoke(servo_cli, "--help", catch_exceptions=False)
        assert result.exit_code == 0
        assert (
            re.search(r"zzzz\n.*aaaa\n.*mmmm\n", result.stdout, flags=re.MULTILINE)
            is not None
        )


def test_command_name_for_nested_connectors() -> None:
    from servo.utilities import strings

    assert strings.commandify("fake") == "fake"
    assert strings.commandify("another_fake") == "another-fake"


def test_ordering_of_ops_commands(servo_cli: CLI, cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(servo_cli, "--help", catch_exceptions=False)
    assert result.exit_code == 0
    assert (
        re.search(
            r".*run.*\n.*check.*\n.*describe.*\n", result.stdout, flags=re.MULTILINE
        )
        is not None
    )


def test_ordering_of_config_commands(servo_cli: CLI, cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(servo_cli, "--help", catch_exceptions=False)
    assert result.exit_code == 0
    assert (
        re.search(
            r".*settings.*\n.*schema.*\n.*validate.*\n.*generate.*",
            result.stdout,
            flags=re.MULTILINE,
        )
        is not None
    )


def test_init_from_scratch(servo_cli: CLI, cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(
        servo_cli,
        "init",
        catch_exceptions=False,
        input="dev.opsani.com/servox\n123456789\nn\ny\n",
    )
    assert result.exit_code == 0
    dotenv = Path(".env")
    assert (
        dotenv.read_text()
        == "OPSANI_OPTIMIZER=dev.opsani.com/servox\nOPSANI_TOKEN=123456789\nSERVO_LOG_LEVEL=DEBUG\n"
    )
    servo_yaml = Path("servo.yaml")
    assert servo_yaml.read_text() is not None


def test_init_existing(servo_cli: CLI, cli_runner: CliRunner) -> None:
    pass


# TODO: test setting section via initializer, add_cli
# TODO: section settable on CLI class, via @command(), and via add_cli()

# TODO: test passing callback as argument to command, via initializer for root callbacks
# TODO: Test passing of correct context
# TODO: Test trying to generate against a class that doesn't have settings (should be a warning instead of error!)



# TODO: init with multi-servos, init single with CLI options, init single option in the config file
# TODO: test overloading/cascading URL and base URL in multi-servo

def test_list(
    cli_runner: CliRunner, servo_cli: Typer, optimizer_env: None, stub_servo_yaml: Path
) -> None:
    result = cli_runner.invoke(servo_cli, "list", catch_exceptions=False)
    assert result.exit_code == 0
    assert re.match("NAME\\s+OPTIMIZER\\s+DESCRIPTION", result.stdout)
    assert re.search("dev.opsani.com/servox\\s+dev.opsani.com/servox\\s+Continuous Optimization Orchestrator", result.stdout)

def test_list_multiservo(
    cli_runner: CliRunner, servo_cli: Typer, stub_multiservo_yaml: Path
) -> None:
    result = cli_runner.invoke(servo_cli, "list", catch_exceptions=False)
    assert result.exit_code == 0, f"Non-zero exit status code: stdout={result.stdout}, stderr={result.stderr}"
    assert re.match("NAME\\s+OPTIMIZER\\s+DESCRIPTION", result.stdout)
    assert re.search("dev.opsani.com/multi-servox-1\\s+dev.opsani.com/multi-servox-1\\s+Continuous Optimization Orchestrator", result.stdout)
    assert re.search("dev.opsani.com/multi-servox-2\\s+dev.opsani.com/multi-servox-2\\s+Continuous Optimization Orchestrator", result.stdout)

def test_measure(
    cli_runner: CliRunner, servo_cli: Typer, optimizer_env: None, stub_servo_yaml: Path
) -> None:
    result = cli_runner.invoke(servo_cli, "measure", catch_exceptions=False)
    assert result.exit_code == 0
    assert re.match("METRIC\\s+UNIT\\s+READINGS", result.stdout)
    assert re.search("Some Metric\\s+rpm\\s+31337.00 \\(just now\\)", result.stdout)

def test_measure_by_connectors_arg(
    cli_runner: CliRunner, servo_cli: Typer, optimizer_env: None, stub_servo_yaml: Path
) -> None:
    result = cli_runner.invoke(servo_cli, "measure --connectors measure", catch_exceptions=False)
    assert result.exit_code == 0
    assert re.match("METRIC\\s+UNIT\\s+READINGS", result.stdout)
    assert re.search("Some Metric\\s+rpm\\s+31337.00 \\(just now\\)", result.stdout)

def test_measure_multiservo(
    cli_runner: CliRunner, servo_cli: Typer, stub_multiservo_yaml: Path
) -> None:
    result = cli_runner.invoke(servo_cli, "measure", catch_exceptions=False)
    assert result.exit_code == 0, f"Non-zero exit status code: stdout={result.stdout}, stderr={result.stderr}"
    assert re.search("dev.opsani.com/multi-servox-1", result.stdout)
    assert re.search("dev.opsani.com/multi-servox-2", result.stdout)
    assert re.search("METRIC\\s+UNIT\\s+READINGS", result.stdout)
    assert re.search("Some Metric\\s+rpm\\s+31337.00 \\(just now\\)", result.stdout)

def test_measure_multiservo_named(
    cli_runner: CliRunner, servo_cli: Typer, stub_multiservo_yaml: Path
) -> None:
    result = cli_runner.invoke(servo_cli, "-n dev.opsani.com/multi-servox-2 measure", catch_exceptions=False)
    assert result.exit_code == 0, f"Non-zero exit status code: stdout={result.stdout}, stderr={result.stderr}"
    assert re.search("dev.opsani.com/multi-servox-1", result.stdout) is None
    assert re.search("dev.opsani.com/multi-servox-2", result.stdout)
    assert re.search("METRIC\\s+UNIT\\s+READINGS", result.stdout)
    assert re.search("Some Metric\\s+rpm\\s+31337.00 \\(just now\\)", result.stdout)

def test_adjust_incomplete_identifier(
    cli_runner: CliRunner, servo_cli: Typer, optimizer_env: None, stub_servo_yaml: Path
) -> None:
    result = cli_runner.invoke(servo_cli, "adjust setting=value", catch_exceptions=False)
    assert result.exit_code == 2
    assert re.search("Error: Invalid value: unable to parse setting descriptor 'setting=value'", result.stderr)

def test_adjust(
    cli_runner: CliRunner, servo_cli: Typer, optimizer_env: None, stub_servo_yaml: Path
) -> None:
    result = cli_runner.invoke(servo_cli, "adjust component.setting=value", catch_exceptions=False)
    assert result.exit_code == 0
    assert re.match("CONNECTOR\\s+SETTINGS", result.stdout)
    assert re.search("adjust\\s+main.cpu=3", result.stdout)

def test_adjust_multiservo(
    cli_runner: CliRunner, servo_cli: Typer, stub_multiservo_yaml: Path
) -> None:
    result = cli_runner.invoke(servo_cli, "adjust component.setting=value", catch_exceptions=False)
    assert result.exit_code == 0, f"failed with non-zero exit code (stdout={result.stdout}, stderr={result.stderr})"
    assert re.search("CONNECTOR\\s+SETTINGS", result.stdout)
    assert re.search("adjust\\s+main.cpu=3", result.stdout)
    assert re.search("dev.opsani.com/multi-servox-1", result.stdout)
    assert re.search("dev.opsani.com/multi-servox-2", result.stdout)

def test_adjust_multiservo_named(
    cli_runner: CliRunner, servo_cli: Typer, stub_multiservo_yaml: Path
) -> None:
    result = cli_runner.invoke(servo_cli, "-n dev.opsani.com/multi-servox-2 adjust component.setting=value", catch_exceptions=False)
    assert result.exit_code == 0, f"failed with non-zero exit code (stdout={result.stdout}, stderr={result.stderr})"
    assert re.search("CONNECTOR\\s+SETTINGS", result.stdout)
    assert re.search("adjust\\s+main.cpu=3", result.stdout)
    assert re.search("dev.opsani.com/multi-servox-1", result.stdout) is None
    assert re.search("dev.opsani.com/multi-servox-2", result.stdout)


def test_describe(
    cli_runner: CliRunner, servo_cli: Typer, optimizer_env: None, stub_servo_yaml: Path
) -> None:
    result = cli_runner.invoke(servo_cli, "describe", catch_exceptions=False)
    assert result.exit_code == 0
    assert re.search("CONNECTOR\\s+COMPONENTS\\s+METRICS", result.stdout)
    assert re.search('measure\\s+throughput \\(rpm\\)', result.stdout)
    assert re.search('\\s+error_rate \\(rpm\\)', result.stdout)
    assert re.search("adjust\\s+main.cpu=3", result.stdout)

def test_describe_connector(
    cli_runner: CliRunner, servo_cli: Typer, optimizer_env: None, stub_servo_yaml: Path
) -> None:
    result = cli_runner.invoke(servo_cli, "describe adjust", catch_exceptions=False)
    assert result.exit_code == 0, f"failed with non-zero exit code (stdout={result.stdout}, stderr={result.stderr})"
    assert re.search("CONNECTOR\\s+COMPONENTS\\s+METRICS", result.stdout)
    assert re.search('measure\\s+throughput \\(rpm\\)', result.stdout) is None
    assert re.search('\\s+error_rate \\(rpm\\)', result.stdout) is None
    assert re.search("adjust\\s+main.cpu=3", result.stdout)

def test_describe_multiservo(
    cli_runner: CliRunner, servo_cli: Typer, stub_multiservo_yaml: Path
) -> None:
    result = cli_runner.invoke(servo_cli, "describe", catch_exceptions=False)
    assert result.exit_code == 0, f"failed with non-zero exit code (stdout={result.stdout}, stderr={result.stderr})"
    assert re.search("CONNECTOR\\s+COMPONENTS\\s+METRICS", result.stdout)
    assert re.search('measure\\s+throughput \\(rpm\\)', result.stdout)
    assert re.search('\\s+error_rate \\(rpm\\)', result.stdout)
    assert re.search("adjust\\s+main.cpu=3", result.stdout)
    assert re.search("dev.opsani.com/multi-servox-1", result.stdout)
    assert re.search("dev.opsani.com/multi-servox-2", result.stdout)

def test_describe_multiservo_named(
    cli_runner: CliRunner, servo_cli: Typer, stub_multiservo_yaml: Path
) -> None:
    result = cli_runner.invoke(servo_cli, "-n dev.opsani.com/multi-servox-2 describe", catch_exceptions=False)
    assert result.exit_code == 0, f"failed with non-zero exit code (stdout={result.stdout}, stderr={result.stderr})"
    assert re.search("CONNECTOR\\s+COMPONENTS\\s+METRICS", result.stdout)
    assert re.search('measure\\s+throughput \\(rpm\\)', result.stdout)
    assert re.search('\\s+error_rate \\(rpm\\)', result.stdout)
    assert re.search("adjust\\s+main.cpu=3", result.stdout)
    assert re.search("dev.opsani.com/multi-servox-1", result.stdout) is None
    assert re.search("dev.opsani.com/multi-servox-2", result.stdout)
