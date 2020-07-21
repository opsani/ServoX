import os
import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from enum import Enum
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import jsonschema
from devtools import pformat
from pydantic import BaseModel, Field, FilePath, root_validator, validator

from servo import (
    HTTP_METHODS,
    BaseConfiguration,
    Check,
    Connector,
    Control,
    Description,
    Duration,
    DurationProgress,
    License,
    Maturity,
    Measurement,
    Metric,
    Numeric,
    TimeSeries,
    Unit,
    cli,
    metadata,
    on_event,
    values_for_keys
)

###
### Vegeta

METRICS = [
    Metric("throughput", Unit.REQUESTS_PER_MINUTE),
    Metric("error_rate", Unit.PERCENTAGE),
    Metric("latency_total", Unit.MILLISECONDS),
    Metric("latency_mean", Unit.MILLISECONDS),
    Metric("latency_50th", Unit.MILLISECONDS),
    Metric("latency_90th", Unit.MILLISECONDS),
    Metric("latency_95th", Unit.MILLISECONDS),
    Metric("latency_99th", Unit.MILLISECONDS),
    Metric("latency_max", Unit.MILLISECONDS),
    Metric("latency_min", Unit.MILLISECONDS),
]


class TargetFormat(str, Enum):
    http = "http"
    json = "json"

    def __str__(self):
        return self.value


class Latencies(BaseModel):
    total: int
    mean: int
    p50: int = Field(alias="50th")
    p90: int = Field(alias="90th")
    p95: int = Field(alias="95th")
    p99: int = Field(alias="99th")
    max: int
    min: int

    @validator("*")
    def convert_nanoseconds_to_milliseconds(cls, latency):
        # Convert Nanonsecond -> Millisecond
        return (latency * 0.000001) if latency is not None else -1


class Bytes(BaseModel):
    total: int
    mean: float


class VegetaReport(BaseModel):
    latencies: Latencies
    bytes_in: Bytes
    bytes_out: Bytes
    earliest: datetime
    latest: datetime
    end: datetime
    duration: timedelta
    wait: timedelta
    requests: int
    rate: float
    throughput: float
    success: float
    error_rate: float = None
    status_codes: Dict[str, int]
    errors: List[str]

    @validator("throughput")
    def convert_throughput_to_rpm(cls, throughput: float) -> float:
        return throughput * 60

    @validator("error_rate", always=True, pre=True)
    def calculate_error_rate_from_success(cls, v, values: Dict[str, Any]) -> float:
        success_rate = values["success"]
        return 100 - (
            success_rate * 100
        )  # Fraction of success inverted into % of error

    # TODO: Generalize object for key path
    def get(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        elif "." in key:
            parent_key, child_key = key.split(".", 2)
            child = self.get(parent_key).dict(by_alias=True)
            return child[child_key]
        else:
            raise ValueError(f"unknown key '{key}'")


class VegetaConfiguration(BaseConfiguration):
    """
    Configuration of the Vegeta connector
    """

    rate: str = Field(
        description="Specifies the request rate per time unit to issue against the targets. Given in the format of request/time unit.",
    )
    duration: Duration = Field(
        description="Specifies the amount of time to issue requests to the targets.",
    )
    format: TargetFormat = Field(
        TargetFormat.http,
        description="Specifies the format of the targets input. Valid values are http and json. Refer to the Vegeta docs for details.",
    )
    target: Optional[str] = Field(
        description="Specifies a single formatted Vegeta target to load. See the format option to learn about available target formats. This option is exclusive of the targets option and will provide a target to Vegeta via stdin."
    )
    targets: Optional[FilePath] = Field(
        description="Specifies the file from which to read targets. See the format option to learn about available target formats. This option is exclusive of the target option and will provide targets to via through a file on disk."
    )
    connections: int = Field(
        10000,
        description="Specifies the maximum number of idle open connections per target host.",
    )
    workers: int = Field(
        10,
        description="Specifies the initial number of workers used in the attack. The workers will automatically increase to achieve the target request rate, up to max-workers.",
    )
    max_workers: int = Field(
        18446744073709551615,
        description="The maximum number of workers used to sustain the attack. This can be used to control the concurrency of the attack to simulate a target number of clients.",
    )
    max_body: int = Field(
        -1,
        description="Specifies the maximum number of bytes to capture from the body of each response. Remaining unread bytes will be fully read but discarded.",
    )
    http2: bool = Field(
        True,
        description="Specifies whether to enable HTTP/2 requests to servers which support it.",
    )
    keepalive: bool = Field(
        True,
        description="Specifies whether to reuse TCP connections between HTTP requests.",
    )
    insecure: bool = Field(
        False,
        description="Specifies whether to ignore invalid server TLS certificates.",
    )
    reporting_interval: Duration = Field(
        "15s", description="How often to report metrics during a measurement cycle.",
    )

    @root_validator(pre=True)
    @classmethod
    def validate_target(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        target, targets = values_for_keys(values, ("target", "targets"))
        if target is None and targets is None:
            raise ValueError("target or targets must be configured")

        if target and targets:
            raise ValueError("target and targets cannot both be configured")

        return values

    @staticmethod
    def target_json_schema() -> Dict[str, Any]:
        """
        Returns the parsed JSON Schema for validating Vegeta targets in the JSON format.
        """
        schema_path = Path(__file__).parent / "vegeta_target_schema.json"
        return json.load(open(schema_path))

    @validator("target", "targets")
    @classmethod
    def validate_target_format(
        cls, value: Union[str, FilePath], field: Field, values: Dict[str, Any]
    ) -> str:
        if value is None:
            return value

        format: TargetFormat = values.get("format")
        if field.name == "target":
            value_stream = StringIO(value)
        elif field.name == "targets":
            value_stream = open(value)

        if format == TargetFormat.http:
            # Scan through the targets and run basic heuristics
            # We don't validate ordering to avoid building a full parser
            count = 0
            for line in value_stream:
                count = count + 1
                line = line.strip()
                if len(line) == 0 or line[0] in ("#", "@"):
                    continue

                maybe_method_and_url = line.split(" ", 2)
                if (
                    len(maybe_method_and_url) == 2
                    and maybe_method_and_url[0] in HTTP_METHODS
                ):
                    if re.match("https?://*", maybe_method_and_url[1]):
                        continue

                maybe_header_and_value = line.split(":", 2)
                if len(maybe_header_and_value) == 2 and maybe_header_and_value[1]:
                    continue

                raise ValueError(f"invalid target: {line}")

            if count == 0:
                raise ValueError(f"no targets found")

        elif format == TargetFormat.json:
            try:
                data = json.load(value_stream)
            except json.JSONDecodeError as e:
                raise ValueError(f"{field.name} contains invalid JSON") from e

            # Validate the target data with JSON Schema
            try:
                jsonschema.validate(instance=data, schema=cls.target_json_schema())
            except jsonschema.ValidationError as error:
                raise ValueError(
                    f"Invalid Vegeta JSON target: {error.message}"
                ) from error

        return value

    @validator("rate")
    @classmethod
    def validate_rate(cls, v: Union[int, str]) -> str:
        assert isinstance(
            v, (int, str)
        ), "rate must be an integer or a rate descriptor string"

        # Integer rates
        if isinstance(v, int) or v.isdigit():
            return str(v)

        # Check for hits/interval
        components = v.split("/")
        assert len(components) == 2, "rate strings are of the form hits/interval"

        hits = components[0]
        duration = components[1]
        assert hits.isnumeric(), "rate must have an integer hits component"

        # Try to parse it from Golang duration string
        try:
            Duration(duration)
        except ValueError as e:
            raise ValueError(f"Invalid duration '{duration}' in rate '{v}'") from e

        return v

    @classmethod
    def generate(cls, **kwargs) -> "VegetaConfiguration":
        return cls(
            rate="50/1s",
            duration="5m",
            target="GET https://example.com/",
            description="Update the rate, duration, and target/targets to match your load profile",
            **kwargs,
        )

    class Config:
        json_encoders = BaseConfiguration.json_encoders(
            {TargetFormat: lambda t: t.value()}
        )


@metadata(
    description="Vegeta load testing connector",
    version="0.5.0",
    homepage="https://github.com/opsani/vegeta-connector",
    license=License.APACHE2,
    maturity=Maturity.STABLE,
)
class VegetaConnector(Connector):
    config: VegetaConfiguration
    vegeta_reports: List[VegetaReport] = []
    warmup_until: Optional[datetime] = None

    @on_event()
    def describe(self) -> Description:
        """
        Describe the metrics and components exported by the connector.
        """
        return Description(metrics=METRICS, components=[])

    @on_event()
    def metrics(self) -> List[Metric]:
        return METRICS

    @on_event()
    async def check(self) -> List[Check]:
        # Take the current config and run a 5 second check against it
        self.warmup_until = datetime.now()
        check_config = self.config.copy()
        check_config.duration = "5s"
        check_config.reporting_interval = "1s"

        checks = []
        exit_code, vegeta_cmd = await self._run_vegeta(config=check_config)
        checks.append(Check(
            name="Vegeta execution",
            success=(exit_code == 0),
            comment=f"Vegeta exit code: {exit_code}",
        ))

        # Look at the error rate
        checks.append(Check(
            name="Report aggregation",
            success=(len(self.vegeta_reports) > 0),
            comment=f"Collected {len(self.vegeta_reports)} reports",
        ))
        if self.vegeta_reports:
            vegeta_report = self.vegeta_reports[-1]
            success = (vegeta_report.error_rate < 5.0)
            checks.append(Check(
                name="Error rate < 5.0%",
                success=False,
                comment=f"Vegeta reported an error rate of {vegeta_report.error_rate:.2f}%",
            ))

        return checks

    @on_event()
    async def measure(
        self, *, metrics: List[str] = None, control: Control = Control()
    ) -> Measurement:
        # Handle delay (if any)
        # TODO: Push the delay and warm-up into a reusable before filter
        # TODO: Use an event to signal the connector when warmup is complete
        if control.delay:
            self.logger.info(f"DELAY: sleeping {control.delay} seconds")
            time.sleep(control.delay)

        self.warmup_until = datetime.now() + control.warmup

        number_of_urls = (
            1 if self.config.target else _number_of_lines_in_file(self.config.targets)
        )
        summary = f"Loading {number_of_urls} URL(s) for {self.config.duration} (delay of {control.delay}, warmup of {control.warmup}) at a rate of {self.config.rate} (reporting every {self.config.reporting_interval})"
        self.logger.info(summary)

        # Run the load generation
        exit_code, command = await self._run_vegeta()

        self.logger.info(
            f"Producing time series readings from {len(self.vegeta_reports)} Vegeta reports"
        )
        readings = (
            self._time_series_readings_from_vegeta_reports()
            if self.vegeta_reports
            else []
        )
        measurement = Measurement(
            readings=readings, annotations={"load_profile": summary,}
        )
        self.logger.trace(f"Reporting time series metrics {pformat(measurement)}")

        return measurement

    async def _run_vegeta(
        self, *, config: Optional[VegetaConfiguration] = None
    ) -> (int, str):
        config = config if config else self.config
        vegeta_cmd = self._build_vegeta_command(config)
        self.logger.debug(f"Vegeta started: `{vegeta_cmd}`")

        report_proc = await asyncio.create_subprocess_shell(
            vegeta_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        # track progress against our load test duration
        progress = DurationProgress(config.duration)

        # loop and poll our process pipe to gather report data
        # compile a regex to strip the ANSI escape sequences from the report output (clear screen)
        ansi_escape = re.compile(r"(\x9B|\x1B\[)[0-?]*[ -\/]*[@-~]")
        while True:
            data = await report_proc.stdout.readline()
            if not data:
                break
            else:
                output = data.decode('ascii').rstrip()
                if output:
                    json_report = ansi_escape.sub("", output)
                    vegeta_report = VegetaReport(**json.loads(json_report))

                    if datetime.now() > self.warmup_until:
                        if not progress.is_started():
                            progress.start()

                        self.vegeta_reports.append(vegeta_report)
                        summary = self._summarize_report(vegeta_report)
                        self.logger.info(progress.annotate(summary))
                    else:
                        self.logger.debug(
                            f"Vegeta metrics excluded (warmup in effect): {metrics}"
                        )

        stdout, stderr = await report_proc.communicate()
        exit_code = report_proc.returncode
        if exit_code != 0:
            error = stderr.decode().strip()
            self.logger.error(
                f"Vegeta command `{vegeta_cmd}` failed with exit code {exit_code}: error: {error}"
            )

        self.logger.debug(f"Vegeta exited with exit code: {exit_code}")

        return exit_code, vegeta_cmd

    def _build_vegeta_command(self, config: VegetaConfiguration) -> str:
        vegeta_attack_args = list(
            map(
                str,
                [
                    "vegeta",
                    "attack",
                    "-rate",
                    config.rate,
                    "-duration",
                    config.duration,
                    "-targets",
                    config.targets if config.targets else "stdin",
                    "-format",
                    config.format,
                    "-connections",
                    config.connections,
                    "-workers",
                    config.workers,
                    "-max-workers",
                    config.max_workers,
                    "-http2",
                    config.http2,
                    "-keepalive",
                    config.keepalive,
                    "-insecure",
                    config.insecure,
                    "-max-body",
                    config.max_body,
                ],
            )
        )

        vegeta_report_args = [
            "vegeta",
            "report",
            "-type",
            "json",
            "-every",
            str(config.reporting_interval),
        ]

        echo_args = ["echo", f"{config.target}"]
        echo_cmd = f'echo "{config.target}" | ' if config.target else ""
        vegeta_cmd = (
            echo_cmd
            + " ".join(vegeta_attack_args)
            + " | "
            + " ".join(vegeta_report_args)
        )
        return vegeta_cmd

    # helper:  take the time series metrics gathered during the attack and map them into OCO format
    def _time_series_readings_from_vegeta_reports(self) -> List[TimeSeries]:
        readings = []

        for metric in METRICS:
            if metric.name in ("throughput", "error_rate",):
                key = metric.name
            elif metric.name.startswith("latency_"):
                key = "latencies" + "." + metric.name.replace("latency_", "")
            else:
                raise NameError(f'Unexpected metric name "{metric.name}"')

            values: List[Tuple(datetime, Numeric)] = []
            for report in self.vegeta_reports:
                values.append((report.end, report.get(key),))

            readings.append(TimeSeries(metric, values))

        return readings

    def _summarize_report(self, report: VegetaReport) -> str:
        def format_metric(value: Numeric, unit: Unit) -> str:
            return f"{value:.2f}{unit.value}"

        throughput = format_metric(report.throughput, Unit.REQUESTS_PER_MINUTE)
        error_rate = format_metric(report.error_rate, Unit.PERCENTAGE)
        latency_50th = format_metric(report.latencies.p50, Unit.MILLISECONDS)
        latency_90th = format_metric(report.latencies.p90, Unit.MILLISECONDS)
        latency_95th = format_metric(report.latencies.p95, Unit.MILLISECONDS)
        latency_99th = format_metric(report.latencies.p99, Unit.MILLISECONDS)
        return f'Vegeta attacking "{self.config.target}" @ {self.config.rate}: ~{throughput} ({error_rate} errors) [latencies: 50th={latency_50th}, 90th={latency_90th}, 95th={latency_95th}, 99th={latency_99th}]'


def _number_of_lines_in_file(filename: str):
    count = 0
    with open(filename, "r") as f:
        for line in f:
            count += 1
    return count


app = cli.ConnectorCLI(VegetaConnector, help="Load testing with Vegeta")


@app.command()
def attack(
    context: cli.Context,
):
    """
    Run an adhoc load generation
    """
    context.connector.measure()