import os
from typing import List, Optional

import servo
import servo.connectors.kubernetes
import servo.connectors.prometheus

PROMETHEUS_SIDECAR_BASE_URL = "http://localhost:9090"
PROMETHEUS_ANNOTATION_NAMES = {
    "prometheus.opsani.com/scrape",
    "prometheus.opsani.com/scheme",
    "prometheus.opsani.com/path",
    "prometheus.opsani.com/port",
}
ENVOY_SIDECAR_LABELS = {
    "sidecar.opsani.com/type": "envoy"
}
ENVOY_SIDECAR_DEFAULT_PORT = 9980

class OpsaniDevConfiguration(servo.AbstractBaseConfiguration):
    namespace: str
    deployment: str
    container: str
    service: str
    config_maps: Optional[List[str]]
    prometheus_base_url: str = PROMETHEUS_SIDECAR_BASE_URL

    @classmethod
    def generate(cls, **kwargs) -> "OpsaniDevConfiguration":
        return cls(
            namespace="default",
            deployment="app-deployment",
            container="main",
            service="app",
        )

    def generate_kubernetes_config(
        self, **kwargs
    ) -> servo.connectors.kubernetes.KubernetesConfiguration:
        """Generate a configuration for running an Opsani Dev optimization under servo.connectors.kubernetes.

        Returns:
            A tuple of connector name and a Kubernetes connector configuration object.
        """
        return servo.connectors.kubernetes.KubernetesConfiguration(
            namespace=self.namespace,
            description="Update the namespace, deployment, etc. to match your Kubernetes cluster",
            deployments=[
                servo.connectors.kubernetes.DeploymentConfiguration(
                    name=self.deployment,
                    replicas=servo.Replicas(
                        min=1,
                        max=2,
                    ),
                    containers=[
                        servo.connectors.kubernetes.ContainerConfiguration(
                            name=self.container,
                            cpu=servo.connectors.kubernetes.CPU(min="250m", max="4000m", step="125m"),
                            memory=servo.connectors.kubernetes.Memory(
                                min="256 MiB", max="4.0 GiB", step="128 MiB"
                            ),
                        )
                    ],
                )
            ],
            **kwargs,
        )

    def generate_prometheus_config(
        self, **kwargs
    ) -> servo.connectors.prometheus.PrometheusConfiguration:
        """Generate a configuration for running an Opsani Dev optimization that utilizes
        Prometheus and Envoy sidecars to produce and aggregate the necessary metrics.

        Returns:
            A tuple of connector name and a Prometheus connector configuration object.
        """
        return servo.connectors.prometheus.PrometheusConfiguration(
            description="A sidecar configuration for aggregating metrics from Envoy sidecar proxies.",
            base_url=PROMETHEUS_SIDECAR_BASE_URL,
            metrics=[
                servo.connectors.prometheus.PrometheusMetric(
                    "main_instance_count",
                    servo.types.Unit.COUNT,
                    query='sum(envoy_cluster_membership_healthy{opsani_role!="tuning"})',
                ),
                servo.connectors.prometheus.PrometheusMetric(
                    "tuning_instance_count",
                    servo.types.Unit.COUNT,
                    query='envoy_cluster_membership_healthy{opsani_role="tuning"}',
                ),
                servo.connectors.prometheus.PrometheusMetric(
                    "main_pod_avg_request_rate",
                    servo.types.Unit.REQUESTS_PER_SECOND,
                    query='avg(rate(envoy_cluster_upstream_rq_total{opsani_role!="tuning"}[3m]))',
                ),
                servo.connectors.prometheus.PrometheusMetric(
                    "total_request_rate",
                    servo.types.Unit.REQUESTS_PER_SECOND,
                    query="sum(rate(envoy_cluster_upstream_rq_total[3m]))",
                ),
                servo.connectors.prometheus.PrometheusMetric(
                    "main_request_rate",
                    servo.types.Unit.REQUESTS_PER_SECOND,
                    query='sum(rate(envoy_cluster_upstream_rq_total{opsani_role!="tuning"}[3m]))',
                ),
                servo.connectors.prometheus.PrometheusMetric(
                    "tuning_request_rate",
                    servo.types.Unit.REQUESTS_PER_SECOND,
                    query='rate(envoy_cluster_upstream_rq_total{opsani_role="tuning"}[3m])',
                ),
                servo.connectors.prometheus.PrometheusMetric(
                    "main_success_rate",
                    servo.types.Unit.REQUESTS_PER_SECOND,
                    query='sum(rate(envoy_cluster_upstream_rq_xx{opsani_role!="tuning", envoy_response_code_class="2"}[3m]))',
                ),
                servo.connectors.prometheus.PrometheusMetric(
                    "tuning_success_rate",
                    servo.types.Unit.REQUESTS_PER_SECOND,
                    query='rate(envoy_cluster_upstream_rq_xx{opsani_role="tuning", envoy_response_code_class="2"}[3m])',
                ),
                servo.connectors.prometheus.PrometheusMetric(
                    "main_error_rate",
                    servo.types.Unit.REQUESTS_PER_SECOND,
                    query='sum(rate(envoy_cluster_upstream_rq_xx{opsani_role!="tuning", envoy_response_code_class=~"4|5"}[3m]))',
                ),
                servo.connectors.prometheus.PrometheusMetric(
                    "tuning_error_rate",
                    servo.types.Unit.REQUESTS_PER_SECOND,
                    query='rate(envoy_cluster_upstream_rq_xx{opsani_role="tuning", envoy_response_code_class=~"4|5"}[3m])',
                ),
                servo.connectors.prometheus.PrometheusMetric(
                    "main_p90_latency",
                    servo.types.Unit.MILLISECONDS,
                    query='avg(histogram_quantile(0.9,rate(envoy_cluster_upstream_rq_time_bucket{opsani_role!="tuning"}[3m])))',
                ),
                servo.connectors.prometheus.PrometheusMetric(
                    "tuning_p90_latency",
                    servo.types.Unit.MILLISECONDS,
                    query='avg(histogram_quantile(0.9,rate(envoy_cluster_upstream_rq_time_bucket{opsani_role="tuning"}[3m])))',
                ),
            ],
            **kwargs,
        )


class OpsaniDevChecks(servo.BaseChecks):
    config: OpsaniDevConfiguration

    ##
    # Kubernetes essentials

    @servo.checks.require("namespace")
    async def check_kubernetes_namespace(self) -> None:
        await servo.connectors.kubernetes.Namespace.read(self.config.namespace)

    @servo.checks.require("deployment")
    async def check_kubernetes_deployment(self) -> None:
        await servo.connectors.kubernetes.Deployment.read(self.config.deployment, self.config.namespace)

    @servo.checks.require("container")
    async def check_kubernetes_container(self) -> None:
        deployment = await servo.connectors.kubernetes.Deployment.read(
            self.config.deployment, self.config.namespace
        )
        container = deployment.find_container(self.config.container)
        assert (
            container
        ), f"failed reading Container '{self.config.container}' in Deployment '{self.config.deployment}'"

    @servo.checks.require("service")
    async def check_kubernetes_service(self) -> None:
        await servo.connectors.kubernetes.Service.read(self.config.service, self.config.namespace)

    @servo.checks.warn("service type")
    async def check_kubernetes_service_type(self) -> None:
        service = await servo.connectors.kubernetes.Service.read(
            self.config.service, self.config.namespace
        )
        if not service.obj.spec.type in ("ClusterIP", "LoadBalancer"):
            raise ValueError(
                f"expected service type of ClusterIP or LoadBalancer but found {service.spec.type}"
            )

    ##
    # Prometheus sidecar

    @servo.checks.require("Prometheus ConfigMap exists")
    async def check_prometheus_config_map(self) -> None:
        config = await servo.connectors.kubernetes.ConfigMap.read(
            "prometheus-config", self.config.namespace
        )
        self.logger.trace(f"read Prometheus ConfigMap: {repr(config)}")
        assert config, "failed: no config map named 'prometheus-config'"

    @servo.checks.check("Prometheus sidecar is running")
    async def check_prometheus_sidecar_exists(self) -> None:
        pod = await self._read_servo_pod()
        if pod is None:
            raise RuntimeError(f"failed: no servo pod is running in namespace '{self.config.namespace}'")

        if not pod.get_container("prometheus"):
            raise RuntimeError(
                f"failed: no 'prometheus' container found in pod '{pod.name}' in namespace '{self.config.namespace}'"
            )

    @servo.checks.check("Prometheus sidecar is ready")
    async def check_prometheus_sidecar_is_ready(self) -> None:
        pod = await self._read_servo_pod()
        if pod is None:
            raise RuntimeError(f"failed: no servo pod was found")

        if not await pod.is_ready():
            raise RuntimeError(f"failed: pod '{pod.name}' is not ready")

    @servo.checks.warn("Prometheus sidecar is stable")
    async def check_prometheus_restart_count(self) -> None:
        pod = await self._read_servo_pod()
        if pod is None:
            raise RuntimeError(f"failed: no servo pod was found")

        container = pod.get_container("prometheus")
        assert container, "could not find a Prometheus sidecar container"
        restart_count = await container.get_restart_count()
        assert (
            restart_count == 0
        ), f"container 'prometheus' in pod '{pod.name}' has restarted {restart_count} times"

    @servo.checks.require("Prometheus has container port on 9090")
    async def check_prometheus_container_port(self) -> None:
        pod = await self._read_servo_pod()
        if pod is None:
            raise RuntimeError(f"failed: no servo pod was found")

        container = pod.get_container("prometheus")
        assert container, "could not find Prometheus sidecar container"

        assert (
            len(container.obj.ports) == 1
        ), f"expected 1 container port but found {len(container.obj.ports)}"
        port = container.obj.ports[0].container_port
        assert (
            port == 9090
        ), f"expected Prometheus container port on 9090 but found {port}"

    @servo.checks.require("Prometheus is accessible")
    async def check_prometheus_is_accessible(self) -> str:
        pod = await self._read_servo_pod()
        if pod is None:
            raise RuntimeError(f"failed: no servo pod was found")

        container = pod.get_container("prometheus")
        assert container, "could not find a Prometheus sidecar container"
        assert (
            len(container.obj.ports) == 1
        ), f"expected 1 container port but found {len(container.obj.ports)}"

        client = servo.connectors.prometheus.Client(base_url=self.config.prometheus_base_url)
        await client.list_targets()
        return f"Prometheus is accessible at {self.config.prometheus_base_url}"

    async def _read_servo_pod(self) -> Optional[servo.connectors.kubernetes.Pod]:
        return await self._read_servo_pod_from_env() or next(
            reversed(await self._list_servo_pods()), None
        )

    async def _read_servo_pod_from_env(self) -> Optional[servo.connectors.kubernetes.Pod]:
        """Reads the servo Pod from Kubernetes by referencing the `POD_NAME` and
        `POD_NAMESPACE` environment variables.

        Returns:
            The Pod object that was read or None if the Pod could not be read.
        """
        pod_name = os.getenv("POD_NAME")
        pod_namespace = os.getenv("POD_NAMESPACE")
        if None in (pod_name, pod_namespace):
            return None

        return await servo.connectors.kubernetes.Pod.read(pod_name, pod_namespace)

    async def _list_servo_pods(self) -> List[servo.connectors.kubernetes.Pod]:
        """Lists all servo pods in the configured namespace.

        Returns:
            A list of servo pods in the configured namespace.
        """
        async with servo.connectors.kubernetes.Pod.preferred_client() as api_client:
            label_selector = servo.connectors.kubernetes.selector_string(
                {"app.kubernetes.io/name": "servo"}
            )
            pod_list: servo.connectors.kubernetes.client.V1PodList = (
                await api_client.list_namespaced_pod(
                    namespace=self.config.namespace, label_selector=label_selector
                )
            )

        pods = [servo.connectors.kubernetes.Pod(p) for p in pod_list.items]
        return pods

    ##
    # Kubernetes Deployment edits

    @servo.checks.require("annotations")
    async def check_deployment_annotations(self) -> None:
        deployment = await servo.connectors.kubernetes.Deployment.read(
            self.config.deployment,
            self.config.namespace
        )
        assert deployment, f"failed to read deployment '{self.config.deployment}' in namespace '{self.config.namespace}'"

        annotations = deployment.pod_template_spec.metadata.annotations
        assert annotations, f"deployment '{deployment.name}' does not have any annotations"

        # NOTE: Only check for annotation keys
        actual_annotations = set(annotations.keys())
        delta = PROMETHEUS_ANNOTATION_NAMES.difference(actual_annotations)
        assert not delta, f"missing annotations: {sorted(delta)}"

    @servo.checks.require("labels")
    async def check_deployment_labels(self) -> None:
        deployment = await servo.connectors.kubernetes.Deployment.read(
            self.config.deployment,
            self.config.namespace
        )
        assert deployment, f"failed to read deployment '{self.config.deployment}' in namespace '{self.config.namespace}'"

        labels = deployment.pod_template_spec.metadata.labels
        assert labels, f"deployment '{deployment.name}' does not have any labels"

        # NOTE: Check for exact labels as this isn't configurable
        delta = dict(set(ENVOY_SIDECAR_LABELS.items()) - set(labels.items()))
        assert not delta, f"missing labels: {delta}"

    @servo.checks.require("envoy sidecars")
    async def check_deployment_envoy_sidecars(self) -> None:
        deployment = await servo.connectors.kubernetes.Deployment.read(
            self.config.deployment,
            self.config.namespace
        )
        assert deployment, f"failed to read deployment '{self.config.deployment}' in namespace '{self.config.namespace}'"

        # Search the containers list for the sidecar
        for container in deployment.containers:
            if container.name == "opsani-envoy":
                # TODO: Add more heuristics about the image, etc.
                return

        raise AssertionError(f"pods created against the '{deployment.name}' pod spec do not have an Opsani Envoy sidecar container ('opsani-envoy')")

    @servo.checks.require("envoy sidecars in pods")
    async def check_pod_envoy_sidecars(self) -> None:
        deployment = await servo.connectors.kubernetes.Deployment.read(
            self.config.deployment,
            self.config.namespace
        )
        assert deployment, f"failed to read deployment '{self.config.deployment}' in namespace '{self.config.namespace}'"

        for pod in await deployment.get_pods():
            # Search the containers list for the sidecar
            for container in pod.containers:
                if container.name == "opsani-envoy":
                    # TODO: Add more heuristics about the image, etc.
                    return

        raise AssertionError(f"pods created against the '{deployment.name}' pod spec do not have an Opsani Envoy sidecar container ('opsani-envoy')")

    ##
    # Connecting the dots

    @servo.require("prometheus targets")
    async def check_prometheus_targets(self) -> None:
        pod = await self._read_servo_pod()
        if pod is None:
            raise RuntimeError(f"failed: no servo pod was found")

        container = pod.get_container("prometheus")
        assert container, "could not find a Prometheus sidecar container"
        assert (
            len(container.obj.ports) == 1
        ), f"expected 1 container port but found {len(container.obj.ports)}"

        client = servo.connectors.prometheus.Client(base_url=self.config.prometheus_base_url)
        targets = await client.list_targets()
        assert len(targets.active) > 0, "no active targets were found"

        return f"found {targets.active} active targets"

    @servo.check("Envoy proxies are being scraped")
    async def check_envoy_sidecar_metrics(self) -> str:
        metrics = [
            servo.connectors.prometheus.PrometheusMetric(
                "main_request_rate",
                servo.types.Unit.REQUESTS_PER_SECOND,
                query=f'sum(rate(envoy_cluster_upstream_rq_total{{opsani_role!="tuning", kubernetes_namespace="{self.config.namespace}"}}[15s]))',
                step="10s"
            ),
            servo.connectors.prometheus.PrometheusMetric(
                "main_error_rate",
                servo.types.Unit.REQUESTS_PER_SECOND,
                query=f'sum(rate(envoy_cluster_upstream_rq_xx{{opsani_role!="tuning", kubernetes_namespace="{self.config.namespace}", envoy_response_code_class=~"4|5"}}[15s]))',
                step="10s"
            )
        ]
        client = servo.connectors.prometheus.Client(base_url=self.config.prometheus_base_url)
        summaries = []
        for metric in metrics:
            query = servo.connectors.prometheus.InstantQuery(
                query=metric.query
            )

            response = await client.query(metric)
            if response.data:
                assert response.data.result_type == servo.connectors.prometheus.ResultType.vector, f"expected a vector result but found {results.data.result_type}"
                assert len(response.data) == 1, f"expected Prometheus API to return a single result for metric '{metric.name}' but found {len(results.data)}"
                result = response.data[0]

                if metric.name == "main_request_rate":
                    timestamp, value = result.value
                    assert int(value) > 0, f"Envoy is not reporting any traffic to Prometheus for metric '{metric.name}' ({metric.query})"
                    summaries.append(f"{metric.name}={value}{metric.unit}")
                elif metric.name == "main_error_rate":
                    if result.value is None:
                        # NOTE: if no errors are occurring this can legitimately be None
                        value = 0
                    else:
                        timestamp, value = result.value
                        assert int(value) < 10, f"Envoy is reporting an error rate above 10% to Prometheus for metric '{metric.name}' ({metric.query})"

                    summaries.append(f"{metric.name}={0}{metric.unit}")
                else:
                    raise NotImplementedError(f"unexpected metric: {metric.name}")
            else:
                # Empty data indicates a potentially absent metric
                # TODO: Use Client to check for absent metric
                if metric.name == "main_request_rate":
                    raise AssertionError(f"Envoy is not reporting any traffic to Prometheus for metric '{metric.name}' ({metric.query})")
                elif metric.name == "main_error_rate":
                    # no errors sounds delightful
                    pass
                else:
                    raise NotImplementedError(f"unexpected metric: {metric.name}")

        return ", ".join(summaries)

    @servo.check("Traffic is proxied through Envoy")
    async def check_service_proxy(self) -> str:
        proxy_service_port = ENVOY_SIDECAR_DEFAULT_PORT  # TODO: move to configuration
        service = await servo.connectors.kubernetes.Service.read(self.config.service, self.config.namespace)
        for port in service.ports:
            if port.target_port == proxy_service_port:
                return

        raise AssertionError(f"service '{service.name}' is not routing traffic through Envoy sidecar on port {proxy_service_port}")

    @servo.check("Tuning pod is running")
    async def check_canary_is_running(self) -> None:
        deployment = await servo.connectors.kubernetes.Deployment.read(
            self.config.deployment,
            self.config.namespace
        )
        assert deployment, f"failed to read deployment '{self.config.deployment}' in namespace '{self.config.namespace}'"

        try:
            await deployment.ensure_canary_pod()
        except Exception as error:
            raise AssertionError(
                f"could not find tuning pod '{deployment.canary_pod_name}''"
            ) from error

    @servo.check("Pods are processing traffic")
    async def check_traffic_metrics(self) -> str:
        metrics = [
            servo.connectors.prometheus.PrometheusMetric(
                "main_request_rate",
                servo.types.Unit.REQUESTS_PER_SECOND,
                query=f'sum(rate(envoy_cluster_upstream_rq_total{{opsani_role!="tuning", kubernetes_namespace="{self.config.namespace}"}}[10s]))',
            ),
            servo.connectors.prometheus.PrometheusMetric(
                "tuning_request_rate",
                servo.types.Unit.REQUESTS_PER_SECOND,
                query=f'rate(envoy_cluster_upstream_rq_total{{opsani_role="tuning", kubernetes_namespace="{self.config.namespace}"}}[10s])'
            ),
        ]
        client = servo.connectors.prometheus.Client(base_url=self.config.prometheus_base_url)
        summaries = []
        for metric in metrics:
            response = await client.query(metric)
            assert response.data.result_type == servo.connectors.prometheus.ResultType.vector, f"expected a vector result but found {response.data.result_type}"
            assert len(response.data) == 1, f"expected Prometheus API to return a single result for metric '{metric.name}' but found {len(response.data)}"

            result = response.data[0]
            value = result.value[1]
            assert value > 0, f"Envoy is reporting a value of {value} which is not greater than zero for metric '{metric.name}' ({metric.query})"

            summaries.append(f"{metric.name}={value}{metric.unit}")
            return ", ".join(summaries)


@servo.metadata(
    description="Run connectors in a specific formation",
    version="0.0.1",
    homepage="https://github.com/opsani/servox",
    license=servo.License.APACHE2,
    maturity=servo.Maturity.EXPERIMENTAL,
)
class OpsaniDevConnector(servo.BaseConnector):
    """Opsani Dev is a turnkey solution for optimizing a single service."""
    config: OpsaniDevConfiguration

    @servo.on_event()
    async def startup(self) -> None:
        servo_ = servo.Servo.current()
        # if self.maturity == servo.Maturity.EXPERIMENTAL:
        #     # Early exit on experimental Opsani Dev bits
        #     return

        await servo_.add_connector(
            "opsani-dev:kubernetes",
            servo.connectors.kubernetes.KubernetesConnector(
                optimizer=self.optimizer,
                config=self.config.generate_kubernetes_config(),
            ),
        )
        await servo_.add_connector(
            "opsani-dev:prometheus",
            servo.connectors.prometheus.PrometheusConnector(
                optimizer=self.optimizer,
                config=self.config.generate_prometheus_config(),
            ),
        )

    @servo.on_event()
    async def check(
        self,
        matching: Optional[servo.CheckFilter],
        halt_on: Optional[servo.ErrorSeverity] = servo.ErrorSeverity.CRITICAL,
    ) -> List[servo.Check]:
        return await OpsaniDevChecks.run(
            self.config, matching=matching, halt_on=halt_on
        )
