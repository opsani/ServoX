import abc
import asyncio
import datetime
import enum
import functools
import itertools
import math
import operator
import re
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Tuple, Type, Union

import httpx
import pydantic
import pytz

import servo

DEFAULT_BASE_URL = "http://prometheus:9090"
API_PATH = "/api/v1"


class Absent(str, enum.Enum):
    """An enumeration of behaviors for handling absent metrics.

    Absent metrics do not exist in Prometheus at query evaluation time.
    Metrics can be absent because no target scraped has reported a value
    or due to previously reported data being lost or purged.

    ### Members:
        ignore: Silently ignore the absent metric and continue processing.
        zero: Return a vector of zero for the absent metric.
        warn: Log a warning message when an absent metric is encountered.
        fail: Raise a runtime exception when an absent metric is encountered.
    """
    ignore = "ignore"
    zero = "zero"
    warn = "warn"
    fail = "fail"

class PrometheusMetric(servo.Metric):
    """A metric that can be measured by querying Prometheus.

    ### Attributes:
        query: A PromQL query string that returns the value of the target metric.
            For details on PromQL, see https://prometheus.io/docs/prometheus/latest/querying/basics/.
        step: The time resolution offset between data points in a range query.
            The number of data points within the query result is equal to the duration between the
            start and end times divided by the step. May be a numeric value or Golang duration string.
        absent: The behavior to apply when the queried metric is absent.
        eager: The duration to observe the metric and eagerly return a measurement if it does not change.
            Defaults to `None`, disabling eager measurements.
    """
    query: str = None
    step: servo.Duration = "1m"
    absent: Absent = Absent.ignore
    eager: Optional[servo.Duration] = None

    def build_query(self) -> str:
        """Build and return a complete Prometheus query string.

        The current implementation handles appending the zero vector suffix.
        """
        if self.absent == Absent.zero:
            return self.query + " or on() vector(0)"
        return self.query

    def escaped_query(self, query: str) -> str:
        return re.sub(r"\{(.*?)\}", r"{{\1}}", query)

    def __check__(self) -> servo.Check:
        """Return a Check representation of the metric."""
        return servo.Check(
            name=f"Check {self.name}",
            description=f'Run Prometheus query "{self.query}"',
        )


class ActiveTarget(pydantic.BaseModel):
    """An active endpoint exporting metrics being scraped by Prometheus.

    ### Attributes:
        pool: The group of related targets that the target belongs to.
        url: The URL that metrics were scraped from.
        global_url: An externally accessible URL to the target.
        health: Health status ("up", "down", or "unknown").
        labels: The set of labels after relabelling has occurred.
        discovered_labels: The unmodified set of labels discovered during service
            discovery before relabelling has occurred.
        last_scraped_at: Time that the target was last scraped by Prometheus.
        last_scrape_duration: The amount of time that the last scrape took to complete.
        last_error: The last error that occurred during scraping (if any).
    """
    pool: str = pydantic.Field(..., alias='scrapePool')
    url: str = pydantic.Field(..., alias='scrapeUrl')
    global_url: str = pydantic.Field(..., alias='globalUrl')
    health: Literal['up', 'down', 'unknown']
    labels: Optional[Dict[str, str]]
    discovered_labels: Optional[Dict[str, str]] = pydantic.Field(..., alias='discoveredLabels')
    last_scraped_at: Optional[datetime.datetime] = pydantic.Field(..., alias='lastScrape')
    last_scrape_duration: Optional[servo.Duration] = pydantic.Field(..., alias='lastScrapeDuration')
    last_error: Optional[str] = pydantic.Field(..., alias='lastError')

    def is_healthy(self) -> bool:
        """Return True if the target is healthy."""
        return self.health == 'up'


class DroppedTarget(pydantic.BaseModel):
    """A target that was discovered by Prometheus and then dropped.

    Dropped targets only have the `discovered_labels` attribute.

    ### Attributes:
        discovered_labels: The set of labels discovered about the dropped target.
    """
    discovered_labels: Optional[Dict[str, str]] = pydantic.Field(..., alias='discoveredLabels')


class BaseRequest(pydantic.BaseModel, abc.ABC):
    """Abstract base class for Prometheus HTTP API request types.

    The base class handles serialization of parameters for subclasses.
    The `param_attrs` attribute must return a sequence of attribute names
    that are to be serialized into request parameters.
    """
    endpoint: str
    param_attrs: Sequence[str]

    @property
    def params(self) -> Dict[str, str]:
        """Return the dictionary of parameters for the query request.

        The values serialized as parameters is determined by the sequence
            of attribute names returned by param_attrs attribute.
        """
        def _param_for_attr(attr: str) -> Optional[Tuple[str, str]]:
            value = getattr(self, attr)
            if not value:
                return None
            elif isinstance(value, datetime.datetime):
                value = value.timestamp()
            return (attr, str(value))

        return dict(filter(None, map(_param_for_attr, self.param_attrs)))

    @property
    def url(self) -> httpx.URL:
        """The relative URL for sending the request as an HTTP GET."""
        return httpx.URL(self.endpoint, params=self.params)


class TargetsStateFilter(str, enum.Enum):
    """An enumeration of states to filter Prometheus targets by."""
    active = 'active'
    dropped = 'dropped'
    any = 'any'


class TargetsRequest(BaseRequest):
    """A request for retrieving targets from the Prometheus HTTP API.

    ### Attributes:
        endpoint: A constant value of `/targets`.
        state: Target state to optionally filter by. One of
            `active`, `dropped`, or `any`.
    """
    endpoint: str = pydantic.Field("/targets", const=True)
    state: Optional[TargetsStateFilter] = None
    param_attrs: Tuple[str] = pydantic.Field(('state', ), const=True)


class QueryRequest(BaseRequest, abc.ABC):
    """Abstract base class for Prometheus query types.

    ### Attributes:
        query: A PromQL string to be evaluated by Prometheus.
        timeout: The maximum amount of time to consume while evaluating the query.
    """
    query: str
    timeout: Optional[servo.Duration]


class InstantQuery(QueryRequest):
    """A query that returns a vector result of a metric at a moment in time.

    ### Attributes:
        endpoint: Constant value of `/query`.
        time: An optional time value to evaluate the query at. `None` defers to the
            server current time when the query is evaluated.
    """
    endpoint: str = pydantic.Field("/query", const=True)
    param_attrs: Tuple[str] = pydantic.Field(('query', 'time', 'timeout'), const=True)
    time: Optional[datetime.datetime] = None


class RangeQuery(QueryRequest):
    """A query that returns a matrix result of a metric across a series of moments in time.

    ### Attributes:
        endpoint: Constant value of `/query_range`.
        start: Start time of the time range to query.
        end: End time of the time range to query.
        step: Time interval between data points within the queried time range to return data
            points for. A step of '5m' would return a measurement for the metric every five
            minutes across the queried time range, determining the number of data points returned.
    """
    endpoint: str = pydantic.Field("/query_range", const=True)
    param_attrs: Tuple[str] = pydantic.Field(('query', 'start', 'end', 'step', 'timeout'), const=True)
    start: datetime.datetime
    end: datetime.datetime
    step: servo.Duration

    @pydantic.validator("step", pre=True, always=True)
    @classmethod
    def _default_step_from_metric(cls, step, values) -> str:
        if step is None:
            if metric := values.get("metric"):
                return metric.step

        return step

    @pydantic.validator("end")
    @classmethod
    def _validate_range(cls, end, values) -> dict:
        assert end > values["start"], "start time must be earlier than end time"
        return end


class ResultType(str, enum.Enum):
    """Types of results returned for Prometheus queries.

    See https://prometheus.io/docs/prometheus/latest/querying/api/#expression-query-result-formats

    ### Members:
        matrix: A result consisting of an array of objects with metric and values attributes
            describing a time series of scalar measurements.
        vector: A result consisting of an array of objects with metric and value attributes
            describing a single scalar measurement.
        scalar: A result consisting of a time and a numeric value encoded as a string.
        string: A result consisting of a time and a string value.
    """
    matrix = "matrix"
    vector = "vector"
    scalar = "scalar"
    string = "string"


Scalar = Tuple[datetime.datetime, float]
String = Tuple[datetime.datetime, str]


class BaseVector(abc.ABC, pydantic.BaseModel):
    """Abstract base class for Prometheus vector types.

    Vectors are sized and iterable to enable processing response data
    without conditional handling.

    Subclasses must implement `__len__` and `__iter__`.

    Attributes:
        metric: The Prometheus metric name and labels.
    """
    metric: Dict[str, str]

    @abc.abstractmethod
    def __len__(self) -> int:
        ...

    @abc.abstractmethod
    def __iter__(self) -> Scalar:
        ...


class InstantVector(BaseVector):
    """A vector modeling the value of a metric at a moment in time.

    Instant vectors are returned as the result of instant queries in `Response` objects
    with a `result_type` of `vector`.

    ### Attributes:
        value: The time and value of the metric measured.
    """
    value: Scalar

    def __len__(self) -> int:
        return 1

    def __iter__(self) -> Scalar:
        return iter((self.value, ))


class RangeVector(BaseVector):
    """A vector modeling the value of a metric across a series of moments in time.

    Range vectors are returned as the result of range queries in `Response` objects
    with a `result_type` of `matrix`.

    ### Attributes:
        values: A sequence of time and value pairs of the metric across the measured range.
    """
    values: List[Scalar]

    def __len__(self) -> int:
        return len(self.values)

    def __iter__(self) -> Scalar:
        return iter(self.values)


class Status(str, enum.Enum):
    """Prometheus HTTP API response statuses.

    See https://prometheus.io/docs/prometheus/latest/querying/api/#format-overview
    """
    success = "success"
    error = "error"


class Error(pydantic.BaseModel):
    """An error returned in a response from the Prometheus HTTP API.

    ### Attributes:
        type: The type of error that occurred.
        message: A textual description of the error.
    """
    type: str = pydantic.Field(..., alias='errorType')
    message: str = pydantic.Field(..., alias='error')


class QueryData(pydantic.BaseModel):
    """The data component of a response from a query endpoint of the Prometheus HTTP API.

    QueryData is an envelope enclosing the result payload of an evaluated query.
    QueryData objects are sized, sequenced collections of results. Scalar and string results
    are presented as single item collections.

    Attributes:
        result_type: The type of result returned by the query.
        result: The query result. The type is polymorphic based on the result type.
    """
    result_type: ResultType = pydantic.Field(..., alias='resultType')
    result: Union[List[InstantVector], List[RangeVector], Scalar, String]

    def __len__(self) -> int:
        if self.is_vector:
            return len(self.result)
        elif self.is_value:
            return 1
        else:
            raise TypeError(f"unknown data type '{self.result_type}'")

    def __iter__(self):
        if self.is_vector:
            return iter(self.result)
        elif self.is_value:
            return iter((self.result, ))
        else:
            raise TypeError(f"unknown data type '{self.result_type}'")

    def __getitem__(self, index: int):
        return list(iter(self))[index]

    @property
    def is_vector(self) -> bool:
        """Returns True when the result is a vector or matrix."""
        return self.result_type in (servo.connectors.prometheus.ResultType.vector, servo.connectors.prometheus.ResultType.matrix)

    @property
    def is_value(self) -> bool:
        """Returns True when the result is a scalar or string."""
        return self.result_type in (servo.connectors.prometheus.ResultType.scalar, servo.connectors.prometheus.ResultType.string)

class TargetData(pydantic.BaseModel):
    """The data component of a response from the targets endpoint of the Prometheus HTTP API.

    ### Attributes:
        active_targets: The active targets being scraped by Prometheus.
        dropped_targets: Targets that were previously active but are no longer being scraped.
    """
    active_targets: Optional[List[ActiveTarget]] = pydantic.Field(alias="activeTargets")
    dropped_targets: Optional[List[DroppedTarget]] = pydantic.Field(alias="droppedTargets")

    def __len__(self) -> int:
        return len(list(iter(self)))

    def __iter__(self):
        return itertools.chain(self.active_targets or [], self.dropped_targets or [])


Data = Union[QueryData, TargetData]


class BaseResponse(pydantic.BaseModel, abc.ABC):
    """Abstract base class for responses returned by the Prometheus HTTP API.

    All successfully processed Prometheus HTTP API responses contain
    `status` and `data` fields. The response format is documented at:
    https://prometheus.io/docs/prometheus/latest/querying/api/#format-overview

    ### Attributes:
        request: The request that triggered the response.
        status: Whether or not the query was successfully evaluated.
        data: The data payload returned in response to the request.
        error: A description of the error triggering query failure, if any.
        warnings: A list of warnings returned during query evaluation, if any.
    """
    request: BaseRequest
    status: Status
    data: Data
    error: Optional[Error]
    warnings: Optional[List[str]]

    @pydantic.root_validator(pre=True)
    def _parse_error(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        if error := dict(filter(lambda item: item[0].startswith("error"), values.items())):
            values["error"] = error
        return values

    def raise_for_error(self) -> None:
        """Raise an error if the request was not successful."""
        if self.status == Status.error:
            raise RuntimeError(f"Prometheus query request failed with error '{self.error.type}': {self.error.message}")


class TargetsResponse(BaseResponse):
    """A response returned by the targets endpoint of the Prometheus HTTP API.

    `TargetsResponse` objects are sized and iterable. Be aware that the sizing and iteration are
    cumulative of the active and dropped targets collections. Utilize the `active` and `dropped`
    attributes to focus on a particular collection of targets.
    """
    data: TargetData

    def __len__(self) -> int:
        return self.data.__len__()

    def __iter__(self):
        return self.data.__iter__()

    @property
    def active(self) -> List[ActiveTarget]:
        """The active targets in the response data."""
        return self.data.active_targets

    @property
    def dropped(self) -> List[DroppedTarget]:
        """The dropped targets in the response data."""
        return self.data.active_targets


class MetricResponse(BaseResponse):
    """A Prometheus HTTP API response for a servo metric.

    ### Attributes:
        metric: The metric that was queried for.
    """
    metric: PrometheusMetric

    def results(self) -> Optional[List[servo.Reading]]:
        """Return `DataPoint` or `TimeSeries` representations of the query results.

        Response data containing vector and matrix results are serialized into
        `TimeSeries` objects. Scalar and string results are serialized into `DataPoint`.
        """
        if self.status == Status.error:
            return None
        elif not self.data:
            return []

        results_ = []
        for result in self.data:
            if self.data.is_vector:
                results_.append(
                    self._time_series_from_vector(result)
                )
            elif self.data.is_value:
                results_.append(
                    servo.DataPoint(self.metric, **result)
                )
            else:
                raise TypeError(f"unknown Result type '{result.__class__.name}' encountered")

        return results_

    def _time_series_from_vector(self, vector: BaseVector) -> servo.TimeSeries:
        instance = vector.metric.get("instance")
        job = vector.metric.get("job")
        annotation = " ".join(
            map(lambda m: "=".join(m), sorted(vector.metric.items(), key=operator.itemgetter(0)))
        )
        return servo.TimeSeries(
            self.metric,
            list(map(lambda v: servo.DataPoint(self.metric, *v), iter(vector))),
            id=f"{{instance={instance},job={job}}}",
            annotation=annotation,
        )


def _rstrip_slash(cls, base_url):
    return base_url.rstrip("/")


class Client(pydantic.BaseModel):
    """A high level interface for interacting with the Prometheus HTTP API.

    The client supports instant and range queries and retrieving the targets.
    Requests and responses are serialized through an object model to make working
    with Prometheus fast and ergonomic.

    For details about the Prometheus HTTP API see: https://prometheus.io/docs/prometheus/latest/querying/api/

    ### Attributes:
        base_url: The base URL for connecting to Prometheus.
    """
    base_url: pydantic.AnyHttpUrl
    _normalize_base_url = pydantic.validator('base_url', allow_reuse=True)(_rstrip_slash)

    @property
    def api_url(self) -> str:
        """Return the full URL for accessing the Prometheus API."""
        return f"{self.base_url}{API_PATH}"

    async def query(
        self,
        promql: Union[str, PrometheusMetric],
        time: Optional[datetime.datetime] = None,
        *,
        timeout: Optional[servo.DurationDescriptor] = None,
        method: Literal['GET', 'POST'] = 'GET'
    ) -> BaseResponse:
        """Send an instant query to Prometheus for evaluation and return the response.

        Instant queries return the result of a query at a moment in time.
        https://prometheus.io/docs/prometheus/latest/querying/api/#instant-queries

        ### Args:
            promql: A PromQL query string or PrometheusMetric object to query for.
            time: An optional time to evaluate the query at. When `None`, evaluate
                the query expression at the time it was received.
            timeout: Evaluation timeout for the query.
        """
        if isinstance(promql, PrometheusMetric):
            response_type = functools.partial(MetricResponse, metric=promql)
            promql_ = promql.build_query()
        elif isinstance(promql, str):
            response_type = BaseResponse
            promql_ = promql
        else:
            raise TypeError(f"cannot query for type: '{promql.__class__.__name__}'")

        query = InstantQuery(
            query=promql_,
            time=time,
        )
        return await self.send_request(method, query, response_type)

    async def query_range(
        self,
        promql: Union[str, PrometheusMetric],
        start: datetime.datetime,
        end: datetime.datetime,
        step: servo.Duration = None,
        *,
        timeout: Optional[servo.DurationDescriptor] = None,
        method: Literal['GET', 'POST'] = 'GET'
    ) -> BaseResponse:
        """Send a range query to Prometheus for evaluation and return the response."""
        if isinstance(promql, PrometheusMetric):
            promql_ = promql.build_query()
            step_ = step or promql.step
            response_type = functools.partial(MetricResponse, metric=promql)
        elif isinstance(promql, str):
            promql_ = promql
            step_ = step
            response_type = BaseResponse
        else:
            raise TypeError(f"cannot query for type: '{promql.__class__.__name__}'")

        query = RangeQuery(
            query=promql_,
            start=start,
            end=end,
            step=step_,
            timeout=timeout,
        )
        return await self.send_request(method, query, response_type)

    async def list_targets(self, state: Optional[TargetsStateFilter] = None) -> TargetsResponse:
        """List the targets discovered by Prometheus.

            ### Args:
                state: Optionally filter by active or dropped target state.
        """
        return await self.send_request("GET", TargetsRequest(state=state), TargetsResponse)

    async def check_is_metric_absent(self, queryable: Union[str, PrometheusMetric]) -> bool:
        """Check if the metric referenced in a Prometheus expression is absent."""
        query = (
            f"absent({queryable.query})" if isinstance(queryable, PrometheusMetric)
            else f"absent({queryable})"
        )
        response = await self.query(query)
        servo.logger.debug(f"Absent metric introspection returned {query}: {response}")
        if response.data:
            if response.data.result_type != servo.connectors.prometheus.ResultType.vector:
                raise TypeError(f"expected a vector result but found {response.data.result_type}")
            if len(response.data) != 1:
                raise ValueError(f"expected a single result vector but found {len(response.data)}")
            result = next(iter(response.data))
            return int(result.value[1]) == 1

        else:
            servo.logger.info(f"Metric '{query}' is present in Prometheus but returned an empty result set")
            return False

    async def send_request(
        self,
        method: Literal['GET', 'POST'],
        request: QueryRequest,
        response_type: Type[BaseResponse] = BaseResponse
    ) -> BaseResponse:
        """Send a request to the Prometheus HTTP API and return the response.

        ### Args:
            method: The HTTP method to use when sending the request.
            request: An object describing a request to the Prometheus HTTP API.
            response_type: The type of object to parse the response into. Must be `Response` or a subclass thereof.
        """
        servo.logger.trace(
            f"Sending request to Prometheus HTTP API (`{request}`): {method} {request.endpoint}"
        )
        async with httpx.AsyncClient(base_url=self.api_url) as client:
            try:
                kwargs = (
                    dict(params=request.params) if method == 'GET'
                    else dict(data=request.params)
                )
                http_request = client.build_request(method, request.endpoint, **kwargs)
                http_response = await client.send(http_request)
                http_response.raise_for_status()
                return response_type(request=request, **http_response.json())
            except (
                httpx.HTTPError,
                httpx.ReadTimeout,
                httpx.ConnectError,
            ) as error:
                servo.logger.trace(
                    f"HTTP error encountered during GET {request.url}: {error}"
                )
                raise

class PrometheusConfiguration(servo.BaseConfiguration):
    """PrometheusConfiguration objects describe how PrometheusConnector objects
    capture measurements from the Prometheus metrics server.
    """

    base_url: pydantic.AnyHttpUrl = DEFAULT_BASE_URL
    _normalize_base_url = pydantic.validator('base_url', allow_reuse=True)(_rstrip_slash)
    """The base URL for accessing the Prometheus metrics API.

    The URL must point to the root of the Prometheus deployment. Resource paths
    are computed as necessary for API requests.
    """

    metrics: List[PrometheusMetric]
    """The metrics to measure from Prometheus.

    Metrics must include a valid query.
    """

    targets: Optional[List[ActiveTarget]]
    """An optional set of Prometheus target descriptors that are expected to be
    scraped by the Prometheus instance being queried.
    """

    @classmethod
    def generate(cls, **kwargs) -> "PrometheusConfiguration":
        """Generate a default configuration for capturing measurements from the
        Prometheus metrics server.

        Returns:
            A default configuration for PrometheusConnector objects.
        """
        return cls(
            **{**dict(
                description="Update the base_url and metrics to match your Prometheus configuration",
                metrics=[
                    PrometheusMetric(
                        "throughput",
                        servo.Unit.requests_per_second,
                        query="rate(http_requests_total[5m])",
                        absent=Absent.ignore,
                        step="1m",
                    ),
                    PrometheusMetric(
                        "error_rate",
                        servo.Unit.percentage,
                        query="rate(errors[5m])",
                        absent=Absent.ignore,
                        step="1m",
                    ),
                ],
            ), **kwargs}
        )


class PrometheusChecks(servo.BaseChecks):
    """A collection of checks verifying the correctness and health of a Prometheus connector configuration.

    ### Attributes:
        config: The connector configuration being checked.
    """
    config: PrometheusConfiguration

    @property
    def _client(self) -> Client:
        return Client(base_url=self.config.base_url)

    @servo.require('Connect to "{self.config.base_url}"')
    async def check_base_url(self) -> None:
        """Checks that the Prometheus base URL is valid and reachable."""
        await self._client.list_targets()

    @servo.multicheck('Run query "{item.query}"')
    async def check_queries(self) -> Tuple[Iterable, servo.CheckHandler]:
        """Checks that all metrics have valid, well-formed PromQL queries."""
        async def query_for_metric(metric: PrometheusMetric) -> str:
            start, end = (
                datetime.datetime.now() - datetime.timedelta(minutes=10),
                datetime.datetime.now(),
            )

            self.logger.trace(
                f"Querying Prometheus (`{metric.query}`)"
            )
            response = await self._client.query_range(metric, start, end)
            return f"returned {len(response.data)} results"

        return self.config.metrics, query_for_metric

    @servo.check("Active targets")
    async def check_targets(self) -> str:
        """Check that all targets are being scraped by Prometheus and report as healthy."""
        targets = await self._client.list_targets()
        assert len(targets.active) > 0, "no targets are being scraped by Prometheus"
        return f"found {len(targets.active)} targets"


@servo.metadata(
    description="Prometheus Connector for Opsani",
    version="1.5.0",
    homepage="https://github.com/opsani/prometheus-connector",
    license=servo.License.APACHE2,
    maturity=servo.Maturity.STABLE,
)
class PrometheusConnector(servo.BaseConnector):
    """A servo connector that captures measurements from Prometheus.

    ### Attributes:
        config: The configuration of the connector instance.
    """
    config: PrometheusConfiguration

    @servo.on_event()
    async def check(
        self,
        matching: Optional[servo.CheckFilter] = None,
        halt_on: Optional[servo.ErrorSeverity] = servo.ErrorSeverity.critical,
    ) -> List[servo.Check]:
        """Checks that the configuration is valid and the connector can capture
        measurements from Prometheus.

        Checks are implemented in the PrometheusChecks class.

        Args:
            matching (Optional[Filter], optional): A filter for limiting the
                checks that are run. Defaults to None.
            halt_on (Severity, optional): When to halt running checks.
                Defaults to Severity.critical.

        Returns:
            List[Check]: A list of check objects that report the outcomes of the
                checks that were run.
        """
        return await PrometheusChecks.run(
            self.config, matching=matching, halt_on=halt_on
        )

    @servo.on_event()
    def describe(self) -> servo.Description:
        """Describes the current state of Metrics measured by querying Prometheus.

        Returns:
            Description: An object describing the current state of metrics
                queried from Prometheus.
        """
        return servo.Description(metrics=self.config.metrics)

    @servo.on_event()
    def metrics(self) -> List[servo.Metric]:
        """Returns the list of metrics measured by querying Prometheus."""
        return self.config.metrics

    @servo.on_event()
    async def measure(
        self, *, metrics: List[str] = None, control: servo.Control = servo.Control()
    ) -> servo.Measurement:
        """Queries Prometheus for metrics as time series values and returns a
        Measurement object that aggregates the readings for processing by the
        optimizer.

        Args:
            metrics (List[str], optional): A list of the metric names to measure.
                When None, all configured metrics are measured. Defaults to None.
            control (Control, optional): A control descriptor that describes how
                the measurement is to be captured. Defaults to Control().

        Returns:
            Measurement: An object that aggregates the state of the metrics
            queried from Prometheus.
        """
        if metrics:
            metrics__ = list(filter(lambda m: m.name in metrics, self.metrics()))
        else:
            metrics__ = self.metrics()
        measuring_names = list(map(lambda m: m.name, metrics__))

        # TODO: Rationalize these given the streaming metrics support
        start = datetime.datetime.now() + control.warmup
        end = start + control.duration
        measurement_duration = servo.Duration(control.warmup + control.duration)
        self.logger.info(
            f"Measuring {len(metrics__)} metrics for {measurement_duration}: {servo.utilities.join_to_series(measuring_names)}"
        )

        # Handle eager metrics
        eager_metrics = list(filter(lambda m: m.eager, metrics__))
        eager_settlement = max(eager_metrics, key=operator.attrgetter("eager")).eager if eager_metrics else None
        eager_observer = EagerMetricObserver(base_url=self.config.base_url, metrics=eager_metrics, start=start, end=end)
        if eager_metrics:
            servo.logger.info(f"Observing values of {len(eager_metrics)} eager metrics: measurement will return after {eager_settlement} of stability")
        else:
            servo.logger.info(f"No eager metrics found: measurement will proceed for full duration of {measurement_duration}")

        # Allow the maximum settlement time of eager metrics to elapse before eager return (None runs full duration)
        progress = servo.EventProgress(timeout=measurement_duration, settlement=eager_settlement)
        await progress.watch(eager_observer.observe)

        # Capture the measurements
        self.logger.info(f"Querying Prometheus for {len(metrics__)} metrics...")
        readings = await asyncio.gather(
            *list(map(lambda m: self._query_prometheus(m, start, end), metrics__))
        )
        all_readings = (
            functools.reduce(lambda x, y: x + y, readings) if readings else []
        )
        measurement = servo.Measurement(readings=all_readings)
        return measurement

    async def targets(self) -> List[TargetsResponse]:
        """Return the targets discovered by Prometheus."""
        client = Client(base_url=self.config.base_url)
        response = await client.list_targets()
        return response

    async def _query_prometheus(
        self, metric: PrometheusMetric, start: datetime, end: datetime
    ) -> List[servo.TimeSeries]:
        client = Client(base_url=self.config.base_url)
        response = await client.query_range(metric, start, end)
        self.logger.trace(f"Got response data type {response.__class__} for metric {metric}: {response}")
        response.raise_for_error()

        if response.data:
            return response.results()
        else:
            # Handle absent metric cases
            if metric.absent in {Absent.ignore, Absent.zero}:
                # NOTE: metric zeroing is handled at the query level
                pass
            else:
                if await client.check_is_metric_absent(metric):
                    if metric.absent == Absent.warn:
                        servo.logger.warning(
                            f"Found absent metric for query (`{metric.query}`)"
                        )
                    elif metric.absent == Absent.fail:
                        servo.logger.error(f"Required metric '{metric.name}' is absent from Prometheus (query='{metric.query}')")
                        raise RuntimeError(f"Required metric '{metric.name}' is absent from Prometheus")
                    else:
                        raise ValueError(f"unknown metric absent value: {metric.absent}")

            return []

app = servo.cli.ConnectorCLI(PrometheusConnector, help="Metrics from Prometheus")

@app.command()
def targets(
    context: servo.cli.Context,
):
    """Display the targets being scraped."""
    targets = servo.cli.run_async(context.connector.targets())
    headers = ["POOL", "HEALTH", "URL", "LABELS", "LAST SCRAPED", "ERROR"]
    table = []
    for target in targets.active:
        labels = sorted(
            list(
                map(
                    lambda l: f"{l[0]}={l[1]}",
                    target.labels.items(),
                )
            )
        )
        table.append(
            [
                target.pool,
                target.health,
                f"{target.url} ({target.global_url})" if target.url != target.global_url else target.url,
                "\n".join(labels),
                f"{target.last_scraped_at:%Y-%m-%d %H:%M:%S} ({servo.cli.timeago(target.last_scraped_at, pytz.utc.localize(datetime.datetime.now()))} in {target.last_scrape_duration})" if target.last_scraped_at else "-",
                target.last_error or "-",
            ]
        )

    servo.cli.print_table(table, headers)

def _delta(a, b):
    if (a == b):
        return 0
    elif (a < 0) and (b < 0) or (a > 0) and (b > 0):
        if (a < b):
            return (abs(abs(a) - abs(b)))
        else:
            return -(abs(abs(a) - abs(b)))
    else:
        return math.copysign((abs(a) + abs(b)),b)

def _chart_delta(a, b, unit) -> str:
    delta = _delta(round(a), round(b))
    if delta == 0:
        return "♭"
    elif delta < 0:
        return f"📉{delta}{unit}"
    else:
        return f"📈+{delta}{unit}"


class EagerMetricObserver(pydantic.BaseModel):
    base_url: pydantic.AnyHttpUrl
    metrics: List[servo.Metric]
    start: datetime.datetime
    end: datetime.datetime
    data_points: Dict[servo.Metric, servo.DataPoint] = {}

    async def observe(self, progress: servo.EventProgress) -> None:
        if not self.metrics:
            # bail if there are no eager metrics to observe
            return

        servo.logger.info(
            progress.annotate(f"measuring Prometheus metrics for up to {progress.timeout} (eager reporting when stable for {progress.settlement})...", False),
            progress=progress.progress,
        )
        if progress.timed_out:
            servo.logger.info(f"measurement duration of {progress.timeout} elapsed: reporting metrics")
            progress.complete()
            return
        else:
            for metric in self.metrics:
                active_data_point = self.data_points.get(metric)
                readings = await self._query_prometheus(metric)
                if readings:
                    data_point = readings[0][-1]
                    servo.logger.trace(f"Prometheus returned reading for the `{metric.name}` metric: {data_point}")
                    if data_point.value > 0:
                        if active_data_point is None:
                            active_data_point = data_point
                            servo.logger.success(progress.annotate(f"read `{metric.name}` metric value of {round(active_data_point.value)}{metric.unit}, awaiting {progress.settlement} before reporting"))
                            progress.trigger()
                        elif data_point.value != active_data_point.value:
                            previous_reading = active_data_point
                            active_data_point = data_point
                            delta_str = _chart_delta(previous_reading.value, active_data_point.value, metric.unit)
                            if progress.settling:
                                servo.logger.success(progress.annotate(f"read updated `{metric.name}` metric value of {round(active_data_point[1])}{metric.unit} ({delta_str}) during settlement, resetting to capture more data"))
                                progress.reset()
                            else:
                                # TODO: Should this just complete? How would we get here...
                                servo.logger.success(progress.annotate(f"read updated `{metric.name}` metric value of {round(active_data_point[1])}{metric.unit} ({delta_str}), awaiting {progress.settlement} before reporting"))
                                progress.trigger()
                        else:
                            servo.logger.debug(f"metric `{metric.name}` has not changed value, ignoring (reading={active_data_point}, num_readings={len(readings[0].data_points)})")
                    else:
                        if active_data_point:
                            # NOTE: If we had a value and fall back to zero it could be a burst
                            if not progress.settling:
                                servo.logger.warning(f"metric `{metric.name}` has fallen to zero from {active_data_point[1]}: may indicate a bursty traffic pattern. Will report eagerly if metric remains zero after {progress.settlement}")
                                progress.trigger()
                            else:
                                # NOTE: We are waiting out settlement
                                servo.logger.warning(f"metric `{metric.name}` has fallen to zero. Will report eagerly if metric remains zero in {progress.settlement_remaining}")
                        else:
                            servo.logger.debug(f"Prometheus returned zero value for the `{metric.name}` metric")
                else:
                    if active_data_point:
                        servo.logger.warning(progress.annotate(f"Prometheus returned no readings for the `{metric.name}` metric"))
                    else:
                        # NOTE: generally only happens on initialization and we don't care
                        servo.logger.trace(progress.annotate(f"Prometheus returned no readings for the `{metric.name}` metric"))

            if not progress.completed and not progress.timed_out:
                max_step_metric = max(self.metrics, key=operator.attrgetter("step"), default=None)
                servo.logger.debug(f"sleeping for {max_step_metric.step} to allow metrics to aggregate")
                await asyncio.sleep(max_step_metric.step.total_seconds())

    async def _query_prometheus(
        self, metric: PrometheusMetric
    ) -> List[servo.TimeSeries]:
        # TODO: Duplicating controller functionality. Refactor. Likely becomes boundary for Promethean library
        client = Client(base_url=self.base_url)
        response = await client.query_range(metric, self.start, self.end)
        servo.logger.trace(f"Got response data type {response.__class__} for metric {metric}: {response}")
        response.raise_for_error()

        if response.data:
            return response.results()
        else:
            # Handle absent metric cases
            if metric.absent in {Absent.ignore, Absent.zero}:
                # NOTE: metric zeroing is handled at the query level
                pass
            else:
                if await client.check_is_metric_absent(metric):
                    if metric.absent == Absent.warn:
                        servo.logger.warning(
                            f"Found absent metric for query (`{metric.query}`)"
                        )
                    elif metric.absent == Absent.fail:
                        servo.logger.error(f"Required metric '{metric.name}' is absent from Prometheus (query='{metric.query}')")
                        raise RuntimeError(f"Required metric '{metric.name}' is absent from Prometheus")
                    else:
                        raise ValueError(f"unknown metric absent value: {metric.absent}")

            return []
