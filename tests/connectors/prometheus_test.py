import datetime
import json
import pathlib
import re
from typing import AsyncIterator

import freezegun
import httpx
import pydantic
import pytest
import pytz
import respx
import typer

import servo.connectors.prometheus
import servo.utilities
from servo.connectors.prometheus import (
    Client,
    PrometheusChecks,
    PrometheusConfiguration,
    PrometheusConnector,
    PrometheusMetric,
    RangeQuery,
)
from servo.types import *


class TestPrometheusMetric:
    def test_accepts_step_as_duration(self):
        metric = PrometheusMetric(
            name="test",
            unit=Unit.REQUESTS_PER_MINUTE,
            query="throughput",
            step="45m",
        )
        assert metric.step == datetime.timedelta(seconds=2700)  # 45 mins

    def test_accepts_step_as_integer_of_seconds(self):
        metric = PrometheusMetric(
            name="test",
            unit=Unit.REQUESTS_PER_MINUTE,
            query="throughput",
            step=180,
        )
        assert metric.step
        assert metric.step == datetime.timedelta(seconds=180)

    # Query
    def test_query_required(self):
        try:
            PrometheusMetric(
                name="throughput", unit=Unit.REQUESTS_PER_MINUTE, query=None
            )
        except pydantic.ValidationError as error:
            assert {
                "loc": ("query",),
                "msg": "none is not an allowed value",
                "type": "type_error.none.not_allowed",
            } in error.errors()

    # NOTE: Floating point values may come back as strings?
    def test_conversion_of_floats_from_strings(self):
        pass

    # item[1] == 'NaN':
    def test_handling_nan_values(self):
        pass


class TestPrometheusConfiguration:
    def test_url_required(self):
        try:
            PrometheusConfiguration(base_url=None)
        except pydantic.ValidationError as error:
            assert {
                "loc": ("base_url",),
                "msg": "none is not an allowed value",
                "type": "type_error.none.not_allowed",
            } in error.errors()

    def test_supports_localhost_url(self):
        config = PrometheusConfiguration(base_url="http://localhost:9090", metrics=[])
        assert config.base_url == "http://localhost:9090"

    def test_supports_cluster_url(self):
        config = PrometheusConfiguration(
            base_url="http://prometheus.default.svc.cluster.local:9090", metrics=[]
        )
        assert config.base_url == "http://prometheus.default.svc.cluster.local:9090"

    def test_rejects_invalid_url(self):
        try:
            PrometheusConfiguration(base_url="gopher://this-is-invalid")
        except pydantic.ValidationError as error:
            assert {
                "loc": ("base_url",),
                "msg": "URL scheme not permitted",
                "type": "value_error.url.scheme",
                "ctx": {
                    "allowed_schemes": {
                        "http",
                        "https",
                    },
                },
            } in error.errors()

    # Metrics
    def test_metrics_required(self):
        try:
            PrometheusConfiguration(metrics=None)
        except pydantic.ValidationError as error:
            assert {
                "loc": ("metrics",),
                "msg": "none is not an allowed value",
                "type": "type_error.none.not_allowed",
            } in error.errors()

    # Generation
    def test_generate_default_config(self):
        config = PrometheusConfiguration.generate()
        assert config.yaml() == (
            "description: Update the base_url and metrics to match your Prometheus configuration\n"
            "base_url: http://prometheus:9090\n"
            "metrics:\n"
            "- name: throughput\n"
            "  unit: rps\n"
            "  query: rate(http_requests_total[5m])\n"
            "  step: 1m\n"
            "  absent: ignore\n"
            "  eager: null\n"
            "- name: error_rate\n"
            "  unit: '%'\n"
            "  query: rate(errors[5m])\n"
            "  step: 1m\n"
            "  absent: ignore\n"
            "  eager: null\n"
            "targets: null\n"
        )

    def test_generate_override_metrics(self):
        PrometheusConfiguration.generate(
            metrics=[
                PrometheusMetric(
                    "throughput",
                    servo.Unit.REQUESTS_PER_SECOND,
                    query="sum(rate(envoy_cluster_upstream_rq_total[1m]))",
                    absent=servo.connectors.prometheus.Absent.ZERO,
                    step="1m",
                ),
                PrometheusMetric(
                    "error_rate",
                    servo.Unit.PERCENTAGE,
                    query="sum(rate(envoy_cluster_upstream_rq_xx{opsani_role!=\"tuning\", envoy_response_code_class=~\"4|5\"}[1m]))",
                    absent=servo.connectors.prometheus.Absent.ZERO,
                    step="1m",
                ),
            ],
        )


class TestPrometheusRequest:
    @freezegun.freeze_time("2020-01-01")
    def test_url(self):
        query = RangeQuery(
            start=datetime.datetime.now(),
            end=datetime.datetime.now() + Duration("36h"),
            query="go_memstats_heap_inuse_bytes",
            step="1m"
        )
        assert query.url, "request URL should not be nil"
        assert (
            query.url
            == "/query_range?query=go_memstats_heap_inuse_bytes&start=1577836800.0&end=1577966400.0&step=1m"
        )

    @freezegun.freeze_time("2020-01-01")
    def test_other_url(self):
        request = RangeQuery(
            start=datetime.datetime.now(),
            end=datetime.datetime.now() + Duration("36h"),
            query="go_memstats_heap_inuse_bytes",
            step='1m'
        )

        assert request.url
        assert (
            str(request.url)
            == "/query_range?query=go_memstats_heap_inuse_bytes&start=1577836800.0&end=1577966400.0&step=1m"
        )

def targets_response_() -> dict:
    return {
        "status": "success",
        "data": {
            "activeTargets": [
                {
                    "discoveredLabels": {
                        "__address__": "192.168.95.123:9901",
                        "__metrics_path__": "/metrics",
                        "__scheme__": "http",
                        "job": "opsani-envoy-sidecars",
                    },
                    "labels": {
                        "app": "web",
                        "instance": "192.168.95.123:9901",
                        "job": "opsani-envoy-sidecars",
                        "pod_template_hash": "6f756468f6",
                    },
                    "scrapePool": "opsani-envoy-sidecars",
                    "scrapeUrl": "http://192.168.95.123:9901/stats/prometheus",
                    "globalUrl": "http://192.168.95.123:9901/stats/prometheus",
                    "lastError": "",
                    "lastScrape": "2020-09-09T10:04:02.662498189Z",
                    "lastScrapeDuration": 0.013974479,
                    "health": "up",
                }
            ]
        },
    }

@pytest.fixture()
def targets_response() -> dict:
    return targets_response_()

class TestPrometheusChecks:
    @pytest.fixture
    def metric(self) -> PrometheusMetric:
        return PrometheusMetric(
            name="test",
            unit=Unit.REQUESTS_PER_MINUTE,
            query="throughput",
            step="45m",
        )

    @pytest.fixture
    def query_matrix_response(self) -> dict:
        return {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {
                            "__name__": "go_memstats_gc_sys_bytes",
                            "instance": "localhost:9090",
                            "job": "prometheus",
                        },
                        "values": [
                            [1595142421.024, "3594504"],
                            [1595142481.024, "3594504"],
                        ],
                    }
                ],
            },
        }

    @pytest.fixture
    def mocked_api(self, query_matrix_response, targets_response):
        with respx.mock(
            base_url="http://localhost:9090", assert_all_called=False
        ) as respx_mock:
            respx_mock.get(
                "/api/v1/targets",
                name="targets"
            ).mock(return_value=httpx.Response(200, json=targets_response))

            respx_mock.get(
                re.compile(r"/api/v1/query_range.+"),
                name="query",
            ).mock(return_value=httpx.Response(200, json=query_matrix_response))
            yield respx_mock

    @pytest.fixture
    def checks(self, metric) -> PrometheusChecks:
        config = PrometheusConfiguration(
            base_url="http://localhost:9090", metrics=[metric]
        )
        return PrometheusChecks(config=config)

    async def test_check_base_url(self, mocked_api, checks) -> None:
        request = mocked_api["targets"]
        check = await checks.check_base_url()
        assert check
        assert check.name == 'Connect to "http://localhost:9090"'
        assert check.id == "check_base_url"
        assert check.critical
        assert check.success
        assert check.message is None
        assert request.called

    @respx.mock
    async def test_check_base_url_failing(self, checks) -> None:
        with respx.mock(base_url="http://localhost:9090") as respx_mock:
            request = respx_mock.get("/api/v1/targets").mock(return_value=httpx.Response(status_code=503))
            check = await checks.check_base_url()
            assert request.called
            assert check
            assert check.name == 'Connect to "http://localhost:9090"'
            assert check.id == "check_base_url"
            assert check.critical
            assert not check.success
            assert check.message is not None
            assert isinstance(check.exception, httpx.HTTPStatusError)

    @respx.mock
    async def test_check_queries(self, mocked_api, checks) -> None:
        request = mocked_api["query"]
        multichecks = await checks._expand_multichecks()
        check = await multichecks[0]()
        assert check
        assert check.name == 'Run query "throughput"'
        assert check.id == "check_queries_item_0"
        assert not check.critical
        assert check.success
        assert check.message == "returned 1 results"
        assert request.called

    @pytest.mark.parametrize(
        "targets, success, message",
        [
            (
                {"status": "success", "data": {"activeTargets": []}},
                False,
                "caught exception (AssertionError): no targets are being scraped by Prometheus",
            ),
            (targets_response_(), True, "found 1 targets"),
        ],
    )
    @respx.mock
    async def test_check_targets(self, checks, targets, success, message) -> str:
        with respx.mock(base_url="http://localhost:9090") as respx_mock:
            request = respx_mock.get("/api/v1/targets").mock(httpx.Response(200, json=targets))
            check = await checks.check_targets()
            assert check
            assert check.name == "Active targets"
            assert check.id == "check_targets"
            assert not check.critical
            assert check.success == success, f"failed: " + check.message
            assert check.message == message
            assert request.called


###
# Integration tests...
# Look at targets
# CLI on targets
# Targets with init container
# Querying for data that is null
# Querying for data that is partially null

@pytest.mark.integration
@pytest.mark.usefixtures("kubernetes_asyncio_config")
@pytest.mark.applymanifests(
    "../manifests",
    files=[
        "prometheus.yaml",
    ],
)
@pytest.mark.clusterrolebinding('cluster-admin')
class TestPrometheusIntegration:
    def optimizer(self) -> servo.Optimizer:
        return servo.Optimizer(
            id="dev.opsani.com/blake-ignite",
            token="bfcf94a6e302222eed3c73a5594badcfd53fef4b6d6a703ed32604",
        )

    @pytest.fixture(autouse=True)
    def _wait_for_cluster(self, kube) -> None:
        kube.wait_for_registered(timeout=60)

    async def test_targets(
        self,
        optimizer: servo.Optimizer,
        kube_port_forward: Callable[[str, int], AsyncIterator[str]],
    ) -> None:
        # Deploy Prometheus and take a look at the targets it starts scraping
        async with kube_port_forward("deploy/prometheus", 9090) as url:
            config = PrometheusConfiguration.generate(base_url=url)
            connector = PrometheusConnector(config=config, optimizer=optimizer)
            targets = await asyncio.wait_for(
                asyncio.gather(connector.targets()),
                timeout=10
            )
            debug(targets)

    async def test_target_discovery(self) -> None:
        # Deploy fiber-http with annotations and Prometheus will start scraping it
        ...

    async def test_range_query_empty_returns_zero_vector_in_matrix(
        self,
        kube,
        kube_port_forward: Callable[[str, int], AsyncIterator[str]],
    ) -> None:
        kube.wait_for_registered(timeout=30)
        async with kube_port_forward("deploy/prometheus", 9090) as url:
            query = servo.connectors.prometheus.RangeQuery(
                query="invalid_metric OR on() vector(0)",
                start=datetime.datetime.now() - Duration("3h"),
                end=datetime.datetime.now(),
                step="30s"
            )
            async with httpx.AsyncClient(base_url=url + '/api/v1/') as client:
                response = await client.get(query.url)
                assert response.status_code == 200
                result = response.json()

                assert result['status'] == 'success'
                assert result['data']['resultType'] == 'matrix'
                assert len(result['data']['result']) == 1
                vector = result['data']['result'][0]
                assert vector['metric'] == {}
                assert vector['values'][0][1] == '0'

    async def test_instant_query_empty_returns_zero_vector(
        self,
        kube,
        kube_port_forward: Callable[[str, int], AsyncIterator[str]],
    ) -> None:
        kube.wait_for_registered(timeout=30)
        async with kube_port_forward("deploy/prometheus", 9090) as url:
            metric=PrometheusMetric(
                "invalid_metric",
                Unit.COUNT,
                query="invalid_metric",
                absent=servo.connectors.prometheus.Absent.ZERO
            )
            client = servo.connectors.prometheus.Client(base_url=url)
            response = await client.query(metric)
            response.raise_for_error()
            assert len(response.data) == 1
            assert response.data.is_vector
            result = next(iter(response.data))
            assert result
            assert result.metric == {}
            assert result.value[1] == 0.0

    @pytest.mark.parametrize(
        "absent, readings",
        [
            ("ignore", []),
            ("zero", [('throughput', '15s', 0.0), ('error_rate', '15s', 0.0)]),
        ]
    )
    @pytest.mark.applymanifests(
        "../manifests",
        files=[
            "fiber-http-opsani-dev.yaml",
        ],
    )
    async def test_no_traffic(
        self,
        optimizer: servo.Optimizer,
        kube,
        kube_port_forward: Callable[[str, int], AsyncIterator[str]],
        absent,
        readings
    ) -> None:
        # NOTE: What we are going to do here is deploy Prometheus and fiber-http with no traffic source,
        # port forward so we can talk to them, and then spark up the connector.
        # The measurement duration will expire and report flatlined metrics.
        servo.logging.set_level("DEBUG")
        kube.wait_for_registered(timeout=30)

        async with kube_port_forward("deploy/prometheus", 9090) as prometheus_url:
            async with kube_port_forward("service/fiber-http", 80) as fiber_url:
                config = PrometheusConfiguration.generate(
                    base_url=prometheus_url,
                    metrics=[
                        PrometheusMetric(
                            "throughput",
                            servo.Unit.REQUESTS_PER_SECOND,
                            query="sum(rate(envoy_cluster_upstream_rq_total[5s]))",
                            absent=absent,
                            step="5s",
                        ),
                        PrometheusMetric(
                            "error_rate",
                            servo.Unit.PERCENTAGE,
                            query="sum(rate(envoy_cluster_upstream_rq_xx{opsani_role!=\"tuning\", envoy_response_code_class=~\"4|5\"}[5s]))",
                            absent=absent,
                            step="5s",
                        ),
                    ],
                )
                connector = PrometheusConnector(config=config, optimizer=optimizer)
                measurement = await asyncio.wait_for(
                    connector.measure(control=servo.Control(duration="15s")),
                    timeout=25 # NOTE: Always make timeout exceed control duration
                )
                assert measurement is not None
                assert list(map(lambda r: (r.metric.name, str(r.duration), r.max.value), measurement.readings)) == readings


    @pytest.mark.applymanifests(
        "../manifests",
        files=[
            "fiber-http-opsani-dev.yaml",
        ],
    )
    async def test_bursty_traffic(
        self,
        optimizer: servo.Optimizer,
        event_loop: asyncio.AbstractEventLoop,
        kube_port_forward: Callable[[str, int], AsyncIterator[str]],
    ) -> None:
        # NOTE: What we are going to do here is deploy Prometheus and fiber-http on a k8s cluster,
        # port forward so we can talk to them, and then spark up the connector and it will adapt to
        # changes in traffic.
        #
        # In this scenario, we will let the connector begin collecting metrics with zero traffic
        # and then manually burst it with traffic via httpx, wait for the metrics to begin flowing,
        # then suspend the traffic and let it fall back to zero. If all goes well, the connector will
        # detect this change and enter into the 1 minute settlement time, early return, and report a
        # set of readings that includes the traffic burst, the zero readings on either side of the
        # burst, and will early return once the metrics stabilize without waiting for the full
        # measurement duration as prescribed by the control structure (13 minutes).
        servo.logging.set_level("TRACE")
        async with kube_port_forward("deploy/prometheus", 9090) as prometheus_url:
            async with kube_port_forward("service/fiber-http", 80) as fiber_url:
                config = PrometheusConfiguration.generate(
                    base_url=prometheus_url,
                    metrics=[
                        PrometheusMetric(
                            "throughput",
                            servo.Unit.REQUESTS_PER_SECOND,
                            query='sum(rate(envoy_cluster_upstream_rq_total[15s]))',
                            step="5s",
                            absent="ignore",
                            eager="20s"
                        ),
                        PrometheusMetric(
                            "error_rate",
                            servo.Unit.PERCENTAGE,
                            query=f'sum(rate(envoy_cluster_upstream_rq_xx{{envoy_response_code_class=~"4|5"}}[15s]))',
                            step="5s",
                            absent="ignore"
                        ),
                    ],
                )

                # TODO: Replace this with the load tester fixture
                async def burst_traffic() -> None:
                    burst_until = datetime.datetime.now() + datetime.timedelta(seconds=15)
                    async with httpx.AsyncClient(base_url=fiber_url) as client:
                        servo.logger.info(f"Bursting traffic to {fiber_url} for 15 seconds...")
                        count = 0
                        while datetime.datetime.now() < burst_until:
                            response = await client.get("/")
                            response.raise_for_status()
                            count += 1
                        servo.logger.success(f"Bursted {count} requests to {fiber_url} over 15 seconds.")

                connector = PrometheusConnector(config=config, optimizer=optimizer)
                event_loop.call_later(
                    15,
                    asyncio.create_task,
                    burst_traffic()
                )
                measurement = await asyncio.wait_for(
                    connector.measure(control=servo.Control(duration="10m")),
                    timeout=300 # NOTE: if we haven't returned in 5 minutes all is lost
                )
                assert measurement
                assert len(measurement) == 1, "expected one TimeSeries (error_rate should be absent)"
                time_series = measurement[0]

                # Check that the readings are zero on both sides of the measurement but not in between
                assert len(time_series) >= 5
                assert time_series.min.value == 0.0
                assert time_series.max.value >= 10.0
                assert time_series[0].value == 0.0
                assert time_series[-1].value == 0.0

def empty_targets_response() -> Dict[str, Any]:
    return json.load("{'status': 'success', 'data': {'activeTargets': [], 'droppedTargets': []}}")

class TestCLI:
    class TestTargets:
        async def test_no_active_connectors(self) -> None:
            # TODO: Put config into tmpdir without connector
            ...

        @pytest.fixture
        def metric(self) -> PrometheusMetric:
            return PrometheusMetric(
                name="test",
                unit=Unit.REQUESTS_PER_MINUTE,
                query="throughput",
                step="45m",
            )

        @pytest.fixture
        def config(self, metric: PrometheusMetric) -> PrometheusConfiguration:
            return PrometheusConfiguration(
                base_url="http://localhost:9090", metrics=[metric]
            )

        @pytest.fixture
        def connector(self, config: PrometheusConfiguration) -> PrometheusConnector:
            return PrometheusConnector(config=config)

        @respx.mock
        def test_one_active_connector(self, targets_response, optimizer_env: None, connector: PrometheusConnector, config: PrometheusConfiguration, servo_cli: servo.cli.ServoCLI, cli_runner: typer.testing.CliRunner, tmp_path: pathlib.Path) -> None:
            with respx.mock(base_url="http://localhost:9090") as respx_mock:
                request = respx_mock.get("/api/v1/targets").mock(httpx.Response(200, json=targets_response))

                config_file = tmp_path / "servo.yaml"
                import tests.helpers  # TODO: Turn into fixtures!
                tests.helpers.write_config_yaml({"prometheus": config}, config_file)

                result = cli_runner.invoke(servo_cli, "prometheus targets", catch_exceptions=False)
                assert result.exit_code == 0, f"expected exit status 0, got {result.exit_code}: stdout={result.stdout}, stderr={result.stderr}"
                assert request.called
                assert "opsani-envoy-sidecars  up        http://192.168.95.123:9901/stats/prometheus" in result.stdout


        async def test_multiple_active_connector(self) -> None:
            # TODO: Put config into tmpdir with two connectors, invoke both, invoke each one
            ...

# CLI TESTS:
# Test without active target
# Test with multiple targets
# Tests with specific target
# TODO: Add query CLI


class TestConnector:
    @pytest.fixture
    def metric(self) -> PrometheusMetric:
        return PrometheusMetric(
            name="test",
            unit=Unit.REQUESTS_PER_MINUTE,
            query="throughput",
            step="5s",
        )

    @pytest.fixture
    def config(self, metric: PrometheusMetric) -> PrometheusConfiguration:
        return PrometheusConfiguration(
            base_url="http://localhost:9090", metrics=[metric]
        )

    @pytest.fixture
    def connector(self, config: PrometheusConfiguration) -> PrometheusConnector:
        return PrometheusConnector(config=config)

    async def test_describe(self, connector) -> None:
        description = connector.describe()
        assert description
        assert isinstance(description, servo.Description)
        assert len(description.metrics) == 1
        metrics = description.metrics[0]
        assert metrics.absent == "ignore"
        assert metrics.eager is None

    @respx.mock
    async def test_measure(self, connector) -> None:
        respx.mock.get(
            re.compile(r"/api/v1/query_range.+"),
            name="query",
        ).mock(return_value=httpx.Response(200, json={
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": []
            }
        }))
        measurement = await connector.measure(control=servo.Control(duration="1s"))
        assert measurement is not None

    async def test_metrics(self, connector) -> None:
        metrics = connector.metrics()
        assert metrics == [
            PrometheusMetric(
                name='test',
                unit=servo.Unit.REQUESTS_PER_MINUTE,
                query='throughput',
                step=servo.Duration('5s'),
                absent="ignore",
                eager=None,
            )
        ]

    async def test_check(self, connector) -> None:
        # NOTE: majority of tests are in the TestChecks class
        # here we are just verifying wiring to connector
        results = await connector.check()
        assert results
        assert len(results) == 1
        assert results[0].id == 'check_base_url'

    @respx.mock
    async def test_one_active_connector(self, connector: PrometheusConnector, targets_response) -> None:
        with respx.mock(base_url="http://localhost:9090") as respx_mock:
            request = respx_mock.get("/api/v1/targets").mock(return_value=httpx.Response(200, json=targets_response))
            targets = await connector.targets()
            assert request.called
            assert len(targets) == 1
            assert targets.active[0].pool == 'opsani-envoy-sidecars'
            assert targets.active[0].url == 'http://192.168.95.123:9901/stats/prometheus'
            assert targets.active[0].health == 'up'
            assert str(targets.active[0].last_scraped_at) == '2020-09-09 10:04:02.662498+00:00'


class TestInstantVector:
    @pytest.fixture
    def vector(self) -> servo.connectors.prometheus.InstantVector:
        return pydantic.parse_obj_as(
            servo.connectors.prometheus.InstantVector,
            {
                'metric': {},
                'value': [
                    1607989427.782,
                    '19.8',
                ],
            }
        )

    def test_parse(self, vector) -> None:
        assert vector
        assert vector.metric == {}
        assert vector.value == (
            datetime.datetime(2020, 12, 14, 23, 43, 47, 782000, tzinfo=datetime.timezone.utc),
            19.8
        )

    def test_len(self, vector) -> None:
        assert vector
        assert len(vector) == 1

    def test_iterate(self, vector) -> None:
        assert vector
        for sample in vector:
            assert sample == (
                datetime.datetime(2020, 12, 14, 23, 43, 47, 782000, tzinfo=datetime.timezone.utc),
                19.8
            )

class TestRangeVector:
    @pytest.fixture
    def vector(self) -> servo.connectors.prometheus.RangeVector:
        return pydantic.parse_obj_as(
            servo.connectors.prometheus.RangeVector,
            {
                "metric": {
                    "__name__": "go_memstats_gc_sys_bytes",
                    "instance": "localhost:9090",
                    "job": "prometheus",
                },
                "values": [
                    [1595142421.024, "3594504"],
                    [1595142481.024, "3594504"],
                    [1595152585.055, "31337"],
                ],
            }
        )

    def test_parse(self, vector) -> None:
        assert vector
        assert vector.metric == {
            "__name__": "go_memstats_gc_sys_bytes",
            "instance": "localhost:9090",
            "job": "prometheus",
        }
        assert vector.values == [
            (datetime.datetime(2020, 7, 19, 7, 7, 1, 24000, tzinfo=datetime.timezone.utc), 3594504.0),
            (datetime.datetime(2020, 7, 19, 7, 8, 1, 24000, tzinfo=datetime.timezone.utc), 3594504.0),
            (datetime.datetime(2020, 7, 19, 9, 56, 25, 55000, tzinfo=datetime.timezone.utc), 31337.0),
        ]

    def test_len(self, vector) -> None:
        assert vector
        assert len(vector) == 3

    def test_iterate(self, vector) -> None:
        assert vector
        expected = [
            (datetime.datetime(2020, 7, 19, 7, 7, 1, 24000, tzinfo=datetime.timezone.utc), 3594504.0),
            (datetime.datetime(2020, 7, 19, 7, 8, 1, 24000, tzinfo=datetime.timezone.utc), 3594504.0),
            (datetime.datetime(2020, 7, 19, 9, 56, 25, 55000, tzinfo=datetime.timezone.utc), 31337.0),
        ]
        for sample in vector:
            assert sample == expected.pop(0)
        assert not expected

class TestResultPrimitives:
    def test_scalar(self) -> None:
        output = pydantic.parse_obj_as(servo.connectors.prometheus.Scalar, [1607989427.782, '1234'])
        assert output
        assert output == (
            datetime.datetime(2020, 12, 14, 23, 43, 47, 782000, tzinfo=datetime.timezone.utc),
            1234.0
        )

    def test_string(self) -> None:
        output = pydantic.parse_obj_as(servo.connectors.prometheus.String, [1607989427.782, 'whatever'])
        assert output
        assert output == (
            datetime.datetime(2020, 12, 14, 23, 43, 47, 782000, tzinfo=datetime.timezone.utc),
            'whatever'
        )

    def test_scalar_parses_as_string(self) -> None:
        output = pydantic.parse_obj_as(servo.connectors.prometheus.String, [1607989427.782, '1234.56'])
        assert output
        assert output == (
            datetime.datetime(2020, 12, 14, 23, 43, 47, 782000, tzinfo=datetime.timezone.utc),
            '1234.56'
        )

    def test_string_does_not_parse_as_scalar(self) -> None:
        with pytest.raises(pydantic.ValidationError, match="value is not a valid float"):
            pydantic.parse_obj_as(servo.connectors.prometheus.Scalar, [1607989427.782, 'thug_life'])


class TestData:
    class TestVector:
        @pytest.fixture
        def obj(self):
            return {
                "resultType" : "vector",
                "result" : [
                    {
                        "metric" : {
                            "__name__" : "up",
                            "job" : "prometheus",
                            "instance" : "localhost:9090"
                        },
                        "value": [ 1435781451.781, "1" ]
                    },
                    {
                        "metric" : {
                            "__name__" : "up",
                            "job" : "node",
                            "instance" : "localhost:9100"
                        },
                        "value" : [ 1435781451.781, "0" ]
                    }
                ]
            }

        def test_parse(self, obj) -> None:
            data = pydantic.parse_obj_as(servo.connectors.prometheus.Data, obj)
            assert data
            assert data.result_type == servo.connectors.prometheus.ResultType.vector
            assert len(data) == 2

        def test_iterate(self, obj) -> None:
            data = pydantic.parse_obj_as(servo.connectors.prometheus.Data, obj)
            assert data
            for vector in data:
                assert isinstance(vector, servo.connectors.prometheus.InstantVector)
                assert vector.metric["__name__"] == "up"
                assert vector.value[0] == datetime.datetime(2015, 7, 1, 20, 10, 51, 781000, tzinfo=datetime.timezone.utc)

    class TestMatrix:
        @pytest.fixture
        def obj(self):
            return {
                "resultType" : "matrix",
                "result" : [
                    {
                        "metric" : {
                            "__name__" : "up",
                            "job" : "prometheus",
                            "instance" : "localhost:9090"
                        },
                        "values" : [
                            [ 1435781430.781, "1" ],
                            [ 1435781445.781, "1" ],
                            [ 1435781460.781, "1" ]
                        ]
                    },
                    {
                        "metric" : {
                            "__name__" : "up",
                            "job" : "node",
                            "instance" : "localhost:9091"
                        },
                        "values" : [
                            [ 1435781430.781, "0" ],
                            [ 1435781445.781, "0" ],
                            [ 1435781460.781, "1" ]
                        ]
                    }
                ]
            }

        def test_parse(self, obj) -> None:
            data = pydantic.parse_obj_as(servo.connectors.prometheus.Data, obj)
            assert data
            assert data.result_type == servo.connectors.prometheus.ResultType.matrix
            assert len(data) == 2

        def test_iterate(self, obj) -> None:
            data = pydantic.parse_obj_as(servo.connectors.prometheus.Data, obj)
            assert data

            values = [
                [(datetime.datetime(2015, 7, 1, 20, 10, 30, 781000, tzinfo=datetime.timezone.utc), 1.0,),
                 (datetime.datetime(2015, 7, 1, 20, 10, 45, 781000, tzinfo=datetime.timezone.utc), 1.0,),
                 (datetime.datetime(2015, 7, 1, 20, 11, 0, 781000, tzinfo=datetime.timezone.utc), 1.0,),],
                [(datetime.datetime(2015, 7, 1, 20, 10, 30, 781000, tzinfo=datetime.timezone.utc), 0.0,),
                 (datetime.datetime(2015, 7, 1, 20, 10, 45, 781000, tzinfo=datetime.timezone.utc), 0.0,),
                 (datetime.datetime(2015, 7, 1, 20, 11, 0, 781000, tzinfo=datetime.timezone.utc), 1.0,),]
            ]
            for vector in data:
                assert isinstance(vector, servo.connectors.prometheus.RangeVector)
                assert vector.metric["__name__"] == "up"
                assert vector.values == values.pop(0)

            assert not values

    class TestScalar:
        @pytest.fixture
        def obj(self):
            return {
                "resultType" : "scalar",
                "result" : [1435781460.781, "1"]
            }

        def test_parse(self, obj) -> None:
            data = pydantic.parse_obj_as(servo.connectors.prometheus.Data, obj)
            assert data
            assert data.result_type == servo.connectors.prometheus.ResultType.scalar
            assert len(data) == 1

        def test_iterate(self, obj) -> None:
            data = pydantic.parse_obj_as(servo.connectors.prometheus.Data, obj)
            assert data
            for scalar in data:
                assert scalar[0] == datetime.datetime(2015, 7, 1, 20, 11, 0, 781000, tzinfo=datetime.timezone.utc)
                assert scalar[1] == 1.0

    class TestString:
        @pytest.fixture
        def obj(self):
            return {
                "resultType" : "string",
                "result" : [1607989427.782, 'thug_life']
            }

        def test_parse(self, obj) -> None:
            data = pydantic.parse_obj_as(servo.connectors.prometheus.Data, obj)
            assert data
            assert data.result_type == servo.connectors.prometheus.ResultType.string
            assert len(data) == 1

        def test_iterate(self, obj) -> None:
            data = pydantic.parse_obj_as(servo.connectors.prometheus.Data, obj)
            assert data
            for string in data:
                assert string[0] == datetime.datetime(2020, 12, 14, 23, 43, 47, 782000, tzinfo=datetime.timezone.utc)
                assert string[1] == "thug_life"


@pytest.fixture
def config() -> PrometheusConfiguration:
    return PrometheusConfiguration(
        base_url="http://prometheus.io/some/path/", metrics=[]
    )

class TestResponse:
    def test_parsing_error(self, config) -> None:
        obj = {
            "status": "error",
            "errorType": "failure",
            "error": "couldn't hang",
            "data": {},
        }
        query = servo.connectors.prometheus.InstantQuery(
            base_url=config.base_url,
            query='rate(envoy_cluster_upstream_rq_total{opsani_role="tuning"}[10s])',
            step="10s"
        )
        response = servo.connectors.prometheus.BaseResponse.parse_obj(dict(request=query, **obj))
        assert response.status == "error"
        assert response.error == { "type": "failure", "message": "couldn't hang" }

    def test_parsing_vector_result(self, query) -> None:
        obj = {
            "status" : "success",
            "data" : {
                "resultType" : "vector",
                "result" : [
                    {
                        "metric" : {
                            "__name__" : "up",
                            "job" : "prometheus",
                            "instance" : "localhost:9090"
                        },
                        "value": [ 1435781451.781, "1" ]
                    },
                    {
                        "metric" : {
                            "__name__" : "up",
                            "job" : "node",
                            "instance" : "localhost:9100"
                        },
                        "value" : [ 1435781451.781, "0" ]
                    }
                ]
            }
        }
        response = servo.connectors.prometheus.BaseResponse.parse_obj(dict(request=query, **obj))
        assert response.status == "success"
        assert response.error is None
        assert response.warnings is None


    def test_parsing_matrix_result(self, config, query) -> None:
        obj = {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {
                            "__name__": "go_memstats_gc_sys_bytes",
                            "instance": "localhost:9090",
                            "job": "prometheus",
                        },
                        "values": [
                            [1595142421.024, "3594504"],
                            [1595142481.024, "3594504"],
                        ],
                    }
                ],
            },
        }
        if response := servo.connectors.prometheus.BaseResponse.parse_obj(dict(request=query, **obj)):
            assert response.status == "success"
            assert response.error is None
            assert response.warnings is None

class TestError:
    @pytest.fixture
    def data(self) -> Dict[str, str]:
        return { "errorType": "failure", "error": "couldn't hang" }

    def test_parsing(self, data) -> None:
        error = servo.connectors.prometheus.Error.parse_obj(data)
        assert error
        assert error.type == "failure"
        assert error.message == "couldn't hang"

    def test_cannot_parse_empty(self) -> None:
        with pytest.raises(pydantic.ValidationError):
            servo.connectors.prometheus.Error.parse_obj({})


@pytest.fixture
def query(config):
    return servo.connectors.prometheus.InstantQuery(
        base_url=config.base_url,
        query='rate(envoy_cluster_upstream_rq_total{opsani_role="tuning"}[10s])',
        step="10s"
    )

class TestAbsentMetrics:
    @pytest.fixture
    def empty_range_query_response(self) -> Dict:
        """Returned for a range query that produces no results.

        It is ambiguous if this is due to the query constraints not matching
        or if the metric doesn't exist.
        """
        return {
            'status': 'success',
            'data': {
                'resultType': 'matrix',
                'result': [],
            },
        }

    @pytest.fixture
    def empty_instant_query_response(self) -> Dict:
        """Returned for an instant query that produces no results.

        It is ambiguous if this is due to the query constraints not matching
        or if the metric doesn't exist.
        """
        return {
            'status': 'success',
            'data': {
                'resultType': 'vector',
                'result': [],
            },
        }

    @pytest.fixture
    def absent_metric_query_response(self) -> Dict:
        """Returned by Prometheus from an absent(metric) query when the metric is absent."""
        return {
            'status': 'success',
            'data': {
                'resultType': 'vector',
                'result': [
                    {
                        'metric': {},
                        'value': [
                            1608522635.537,
                            '1',
                        ],
                    },
                ],
            },
        }

    @pytest.fixture
    def present_metric_query_response(self) -> Dict:
        """Returned by Prometheus from an absent(metric) query when the metric is present."""
        return {
            'status': 'success',
            'data': {
                'resultType': 'vector',
                'result': [],
            },
        }

    @pytest.fixture
    def connector(self) -> servo.connectors.prometheus.PrometheusConnector:
        optimizer = servo.Optimizer(
            id="dev.opsani.com/blake-ignite",
            token="bfcf94a6e302222eed3c73a5594badcfd53fef4b6d6a703ed32604",
        )
        config = PrometheusConfiguration.generate(base_url='https://localhost:9090')
        return PrometheusConnector(config=config, optimizer=optimizer)

    @pytest.fixture
    def routes(self,
        empty_range_query_response,
        absent_metric_query_response,
        present_metric_query_response,
    ) -> None:
        respx.get(
            "https://localhost:9090/api/v1/query_range",
            params={"query": "empty_metric"},
            name="range_query_for_empty_metric"
        ).mock(
            return_value=httpx.Response(
                status_code=200,
                json=empty_range_query_response
            )
        )
        respx.get(
            "https://localhost:9090/api/v1/query_range",
            params={"query": "empty_metric or on() vector(0)"},
            name="range_query_for_empty_metric_or_zero_vector"
        ).mock(
            return_value=httpx.Response(
                status_code=200,
                json=empty_range_query_response
            )
        )


    @pytest.mark.parametrize("absent", list(map(lambda ab: ab, servo.connectors.prometheus.Absent)))
    @respx.mock
    async def test_that_empty_range_query_triggers_absent_check(
        self,
        connector,
        absent,
        routes,
        absent_metric_query_response
    ) -> None:
        metric = PrometheusMetric(
            "empty_metric",
            Unit.COUNT,
            query="empty_metric",
            absent=absent
        )
        assert metric.query

        respx.get(
            "https://localhost:9090/api/v1/query",
            params={"query": "absent(empty_metric)"},
            name="instant_query_for_absent_empty_metric"
        ).mock(
            return_value=httpx.Response(
                status_code=200,
                json=absent_metric_query_response
            )
        )

        start = datetime.datetime.now()
        end = start + Duration("36h")

        if absent == servo.connectors.prometheus.Absent.FAIL:
            with pytest.raises(RuntimeError, match="Required metric 'empty_metric' is absent from Prometheus"):
                await connector._query_prometheus(metric, start, end)

            assert respx.routes["range_query_for_empty_metric"].called
            assert respx.routes["instant_query_for_absent_empty_metric"].called

        elif absent == servo.connectors.prometheus.Absent.ZERO:
            await connector._query_prometheus(metric, start, end)

            assert respx.routes["range_query_for_empty_metric_or_zero_vector"].called
            assert not respx.routes["range_query_for_empty_metric"].called
            assert not respx.routes["instant_query_for_absent_empty_metric"].called

        else:
            await connector._query_prometheus(metric, start, end)
            assert respx.routes["range_query_for_empty_metric"].called

            if absent == servo.connectors.prometheus.Absent.IGNORE:
                assert not respx.routes["instant_query_for_absent_empty_metric"].called
            elif absent == servo.connectors.prometheus.Absent.WARN:
                assert respx.routes["instant_query_for_absent_empty_metric"].called
            else:
                assert False, "unhandled case"


    @pytest.mark.parametrize("absent", list(map(lambda ab: ab, servo.connectors.prometheus.Absent)))
    @respx.mock
    async def test_that_present_metric_returns_empty_results(
        self,
        connector,
        absent,
        routes,
        present_metric_query_response
    ) -> None:
        metric = PrometheusMetric(
            "empty_metric",
            Unit.COUNT,
            query="empty_metric",
            absent=absent
        )

        respx.get(
            "https://localhost:9090/api/v1/query",
            params={"query": "absent(empty_metric)"},
            name="instant_query_for_absent_empty_metric"
        ).mock(
            return_value=httpx.Response(
                status_code=200,
                json=present_metric_query_response
            )
        )

        metric.absent = absent
        start = datetime.datetime.now()
        end = start + Duration("36h")

        result = await connector._query_prometheus(metric, start, end)
        if absent == "zero":
            # NOTE: Zero will append the `or on() vector(0)` suffix and never trigger `absent()`
            assert respx.routes["range_query_for_empty_metric_or_zero_vector"].called
            assert not respx.routes["instant_query_for_absent_empty_metric"].called
            assert result == []
        elif absent == "ignore":
            # NOTE: Ignore doesn't care and won't issue the absent() query
            assert respx.routes["range_query_for_empty_metric"].called
            assert not respx.routes["instant_query_for_absent_empty_metric"].called
        else:
            assert respx.routes["range_query_for_empty_metric"].called
            assert respx.routes["instant_query_for_absent_empty_metric"].called
            assert result == []

    class TestAbsentZero:
        async def test_range_query_includes_or_on_vector(self) -> None:
            metric=PrometheusMetric(
                "envoy_cluster_upstream_rq_total",
                Unit.COUNT,
                query="envoy_cluster_upstream_rq_total",
                absent=servo.connectors.prometheus.Absent.ZERO
            )
            assert metric.build_query().endswith("or on() vector(0)")

class TestClient:
    def test_base_url_is_rstripped(self):
        client = Client(
            base_url="http://prometheus.io/some/path/"
        )
        assert client.base_url == "http://prometheus.io/some/path"

    def test_api_url(self):
        client = Client(
            base_url="http://prometheus.default.svc.cluster.local:9090"
        )
        assert (
            client.api_url == "http://prometheus.default.svc.cluster.local:9090/api/v1"
        )

class TestInstantQuery:
    @pytest.fixture
    def query(self) -> servo.connectors.prometheus.InstantQuery:
        return servo.connectors.prometheus.InstantQuery(
            query="testing",
            time=pytz.utc.localize(datetime.datetime(2020, 1, 21, 12, 0, 1)),
        )

    def test_params(self, query) -> None:
        assert query.params == {'query': 'testing', 'time': '1579608001.0'}

    def test_url(self, query) -> None:
        assert query.url == '/query?query=testing&time=1579608001.0'

class TestTargetsRequest:
    def test_(self) -> None:
        request = servo.connectors.prometheus.TargetsRequest()
        assert request.endpoint == '/targets'
        assert request.state is None
        assert request.param_attrs == ('state', )
        assert request.params == {}

@respx.mock
async def test_list_targets() -> None:
    client = Client(base_url="http://localhost:9090/")
    with respx.mock(base_url=client.base_url) as respx_mock:
        request = respx_mock.get("/api/v1/targets").mock(httpx.Response(200, json=targets_response_()))
        response = await client.list_targets()
        assert response.data.active_targets
        assert len(response.data.active_targets) == 1
        target = response.data.active_targets[0]
        assert target == servo.connectors.prometheus.ActiveTarget(
            scrapePool='opsani-envoy-sidecars',
            scrapeUrl='http://192.168.95.123:9901/stats/prometheus',
            globalUrl='http://192.168.95.123:9901/stats/prometheus',
            health='up',
            labels={
                'app': 'web',
                'instance': '192.168.95.123:9901',
                'job': 'opsani-envoy-sidecars',
                'pod_template_hash': '6f756468f6',
            },
            discoveredLabels={
                '__address__': '192.168.95.123:9901',
                '__metrics_path__': '/metrics',
                '__scheme__': 'http',
                'job': 'opsani-envoy-sidecars',
            },
            lastScrape=datetime.datetime(2020, 9, 9, 10, 4, 2, 662498, tzinfo=datetime.timezone.utc),
            lastScrapeDuration=Duration('13ms974us'),
            lastError='',
        )

class TestTargetData:
    @pytest.mark.parametrize(
        "active_targets, dropped_targets, total_len",
        [
            (None, None, 0),
            ([], None, 0),
            (
                [servo.connectors.prometheus.ActiveTarget.construct()],
                [servo.connectors.prometheus.DroppedTarget.construct()],
                2
            ),
            (
                [
                    servo.connectors.prometheus.ActiveTarget.construct(),
                    servo.connectors.prometheus.ActiveTarget.construct(),
                    servo.connectors.prometheus.ActiveTarget.construct()
                ],
                None,
                3
            )
        ],
    )
    def test_len(self, active_targets, dropped_targets, total_len) -> None:
        target_data = servo.connectors.prometheus.TargetData(
            activeTargets=active_targets,
            droppedTargets=dropped_targets,
        )
        assert len(target_data) == total_len
