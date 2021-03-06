import asyncio
import datetime
import hashlib

import kubernetes_asyncio
import kubernetes_asyncio.client
import kubetest.client
import pytest

import servo
import servo.connectors.kubernetes
import tests.helpers

# NOTE: These tests are brittle when run under uvloop. We run these under the default
# asyncio event loop policy to avoid exceptions relating to pytest output capture.
# The exception is: `io.UnsupportedOperation: redirected stdin is pseudofile, has no fileno()`
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.event_loop_policy("default"),
    pytest.mark.integration,
    pytest.mark.usefixtures("kubernetes_asyncio_config")
]

@pytest.mark.applymanifests("manifests", files=["nginx.yaml"])
def test_nginx(kube: kubetest.client.TestClient) -> None:
    # wait for the manifests loaded by the 'applymanifests' marker
    # to be ready on the cluster
    kube.wait_for_registered(timeout=30)

    deployments = kube.get_deployments()
    nginx_deploy = deployments.get("nginx-deployment")
    assert nginx_deploy is not None

    pods = nginx_deploy.get_pods()
    assert len(pods) == 1, "nginx should deploy with one replica"

    for pod in pods:
        containers = pod.get_containers()
        assert len(containers) == 1, "nginx pod should have one container"

        resp = pod.http_proxy_get("/")
        assert "<h1>Welcome to nginx!</h1>" in resp.data


@pytest.mark.applymanifests("manifests", files=["fiber-http-opsani-dev.yaml"])
def test_fiber_http_and_envoy(kube: kubetest.client.TestClient) -> None:
    kube.wait_for_registered(timeout=60)

    deployments = kube.get_deployments()
    web_deploy = deployments.get("fiber-http")
    assert web_deploy is not None

    pods = web_deploy.get_pods()
    assert len(pods) == 1, "fiber-http should deploy with one replica"

    pod = pods[0]
    pod.wait_until_ready(timeout=30)

    # Check containers
    containers = pod.get_containers()
    assert len(containers) == 2, "should have fiber-http and an envoy sidecar"
    assert containers[0].obj.name == "fiber-http"
    assert containers[1].obj.name == "opsani-envoy"

    # Check services
    response = pod.http_proxy_get("/")
    assert "move along, nothing to see here" in response.data

    # TODO: Ugly hack to control port number
    pod.name = pod.name + ":9901"
    response = pod.http_proxy_get("/stats/prometheus")
    assert "envoy_http_downstream_cx_length_ms_count" in response.data


@pytest.mark.applymanifests("manifests", files=["prometheus.yaml"])
@pytest.mark.xfail(reason="kubetest doesn't support the ClusterRole yet")
def test_prometheus(kube: kubetest.client.TestClient) -> None:
    kube.wait_for_registered(timeout=30)

    deployments = kube.get_deployments()
    prom_deploy = deployments.get("prometheus-core")
    assert prom_deploy is not None

    pods = prom_deploy.get_pods()
    assert len(pods) == 1, "prom_deploy should deploy with one replica"

    # Check that Prometheus is there by referencing string in the HTML body
    pod = pods[0]
    pod.name = pod.name + ":9090"
    response = pod.http_proxy_get("/")
    assert "Prometheus Time Series Collection and Processing Server" in response.data

def test_deploy_servo_fiberhttp_vegeta_measure() -> None:
    pass
    # Make servo load test fiber-http, report the outcome in JSON


def test_deploy_servo_fiberhttp_vegeta_adjust() -> None:
    pass
    # Make servo adjust fiber-http memory, report in JSON


# TODO: Tests to write...
# 1. Servo creates canary on start
# canary gets deleted on stop
# failed adjust (can't schedule)
# integration tests: ad-hoc adjust, ad-hoc measure, checks (generate config files in tmp)
# use ktunnel to bridge and return errors, garbage data
# k8s sizing tool

# Integration test k8s describe, adjust


def test_generate_outputs_human_readable_config() -> None:
    ...


def test_supports_nil_container_name() -> None:
    ...


@pytest.mark.applymanifests("manifests", files=["fiber-http.yaml"])
class TestChecks:
    @pytest.fixture
    async def config(self, kube: kubetest.client.TestClient) -> servo.connectors.kubernetes.KubernetesConfiguration:
        config = servo.connectors.kubernetes.KubernetesConfiguration(
            namespace=kube.namespace,
            description="Update the namespace, deployment, etc. to match your Kubernetes cluster",
            deployments=[
                servo.connectors.kubernetes.DeploymentConfiguration(
                    name="fiber-http",
                    replicas=servo.Replicas(
                        min=1,
                        max=2,
                    ),
                    containers=[
                        servo.connectors.kubernetes.ContainerConfiguration(
                            name="opsani/fiber-http:latest",
                            cpu=servo.connectors.kubernetes.CPU(
                                min="250m", max="4000m", step="125m"
                            ),
                            memory=servo.connectors.kubernetes.Memory(
                                min="64MiB", max="4.0GiB", step="128MiB"
                            ),
                        )
                    ],
                )
            ],
        )
        return config

    @pytest.fixture(autouse=True)
    def wait_for_manifests(self, kube: kubetest.client.TestClient) -> None:
        kube.wait_for_registered(timeout=30)

    async def test_check_version(self, config: servo.connectors.kubernetes.KubernetesConfiguration) -> None:
        checks = servo.connectors.kubernetes.KubernetesChecks(config)
        results = await checks.run_all(
            matching=servo.checks.CheckFilter(id="check_version")
        )
        assert results
        assert results[-1].success

    async def test_check_connectivity_success(self, config) -> None:
        checks = servo.connectors.kubernetes.KubernetesChecks(config)
        results = await checks.run_all(
            matching=servo.checks.CheckFilter(id="check_connectivity")
        )
        assert len(results) == 1
        assert results[0].success

    async def test_check_connectivity_bad_hostname(self, config) -> None:
        async with tests.helpers.kubernetes_asyncio_client_overrides(host="https://localhost:4321"):
            checks = servo.connectors.kubernetes.KubernetesChecks(config)
            results = await checks.run_all(
                matching=servo.checks.CheckFilter(id="check_connectivity")
            )
            assert len(results) == 1
            result = results[0]
            assert not result.success
            assert "Cannot connect to host localhost:4321" in str(result.exception)

    async def test_check_permissions_success(
        self, config: servo.connectors.kubernetes.KubernetesConfiguration
    ) -> None:
        checks = servo.connectors.kubernetes.KubernetesChecks(config)
        results = await checks.run_all(
            matching=servo.checks.CheckFilter(id=["check_permissions"])
        )
        assert len(results)
        result = results[-1]
        assert result.id == "check_permissions"
        assert result.success

    async def test_check_permissions_fails(
        self, config: servo.connectors.kubernetes.KubernetesConfiguration
    ) -> None:
        # TODO: Delete the Role? Remove a permission?
        ...

    async def test_check_namespace_success(self, config: servo.connectors.kubernetes.KubernetesConfiguration) -> None:
        checks = servo.connectors.kubernetes.KubernetesChecks(config)
        results = await checks.run_all(
            matching=servo.checks.CheckFilter(id=["check_namespace"])
        )
        assert len(results)
        result = results[-1]
        assert result.id == "check_namespace"
        assert result.success, f"expected success but failed: {result}"

    async def test_check_namespace_doesnt_exist(self, config: servo.connectors.kubernetes.KubernetesConfiguration) -> None:
        config.namespace = "INVALID"
        checks = servo.connectors.kubernetes.KubernetesChecks(config)
        results = await checks.run_all(
            matching=servo.checks.CheckFilter(id=["check_namespace"])
        )
        assert len(results)
        result = results[-1]
        assert result.id == "check_namespace"
        assert not result.success
        assert result.exception
        assert "Not Found" in str(result.exception)

    async def test_check_deployment(self, config: servo.connectors.kubernetes.KubernetesConfiguration) -> None:
        results = await servo.connectors.kubernetes.KubernetesChecks.run(
            config, matching=servo.checks.CheckFilter(id="check_deployments_item_0")
        )
        assert results
        result = results[-1]
        assert result.id == "check_deployments_item_0"
        assert result.success

    async def test_check_deployment_doesnt_exist(
        self, config: servo.connectors.kubernetes.KubernetesConfiguration
    ) -> None:
        config.deployments[0].name = "INVALID"
        checks = servo.connectors.kubernetes.KubernetesChecks(config)
        results = await checks.run_all(
            matching=servo.checks.CheckFilter(id=["check_deployments_item_0"])
        )
        assert len(results)
        result = results[-1]
        assert result.id == "check_deployments_item_0"
        assert not result.success
        assert result.exception
        assert "Not Found" in str(result.exception)

    async def test_check_resource_requirements(self, config: servo.connectors.kubernetes.KubernetesConfiguration) -> None:
        results = await servo.connectors.kubernetes.KubernetesChecks.run(
            config, matching=servo.checks.CheckFilter(id="check_resource_requirements_item_0")
        )
        assert results
        result = results[-1]
        assert result.id, "check_resource_requirements_item_0"
        assert result.success, f"Checking resource requirements \"{config.deployments[0].name}\" in namespace \"{config.namespace}\" failed: {result.exception or result.message or result}"

    async def test_check_resource_requirements_fail(self, config: servo.connectors.kubernetes.KubernetesConfiguration, kube) -> None:
        # Zero out the CPU settings
        deployment = await servo.connectors.kubernetes.Deployment.read("fiber-http", kube.namespace)
        assert deployment
        container = deployment.containers[0]
        container.resources = kubernetes_asyncio.client.V1ResourceRequirements(limits={"cpu": None}, requests={"cpu": None})
        await deployment.patch()
        await deployment.wait_until_ready()

        # Fail the check because the CPU isn't limited
        results = await servo.connectors.kubernetes.KubernetesChecks.run(
            config, matching=servo.checks.CheckFilter(id="check_resource_requirements_item_0")
        )
        assert results
        result = results[-1]
        assert result.id, "check_resource_requirements_item_0"
        assert not result.success, f"Checking resource requirements \"{config.deployments[0].name}\" in namespace \"{config.namespace}\" failed: {result.exception or result.message or result}"

    async def test_deployments_are_ready(self, config: servo.connectors.kubernetes.KubernetesConfiguration, kube) -> None:
        # Set the CPU request implausibly high to force it into pending
        deployment = await servo.connectors.kubernetes.Deployment.read("fiber-http", kube.namespace)
        assert deployment
        container = deployment.containers[0]
        container.resources = kubernetes_asyncio.client.V1ResourceRequirements(limits={"cpu": None}, requests={"cpu": "500"})
        await deployment.patch()
        try:
            await deployment.wait_until_ready(5)
        except asyncio.TimeoutError:
            pass

        # Fail because the Pod is stuck in pending
        results = await servo.connectors.kubernetes.KubernetesChecks.run(
            config, matching=servo.checks.CheckFilter(id="check_resource_requirements_item_0")
        )
        assert results
        result = results[-1]
        assert result.id, "check_resource_requirements_item_0"
        assert not result.success, f"Checking resource requirements \"{config.deployments[0].name}\" in namespace \"{config.namespace}\" failed: {result.exception or result.message or result}"

@pytest.mark.applymanifests("manifests", files=["fiber-http.yaml"])
class TestService:
    @pytest.fixture(autouse=True)
    async def wait(self, kube) -> None:
        kube.wait_for_registered(timeout=30)
        await asyncio.sleep(0.0001)


    async def test_read_service(self, kube: kubetest.client.TestClient) -> None:
        svc = await servo.connectors.kubernetes.Service.read("fiber-http", kube.namespace)
        assert svc
        assert svc.obj.metadata.name == "fiber-http"
        assert svc.obj.metadata.namespace == kube.namespace


    async def test_patch_service(self, kube: kubetest.client.TestClient) -> None:
        svc = await servo.connectors.kubernetes.Service.read("fiber-http", kube.namespace)
        assert svc
        sentinel_value = hashlib.blake2b(
            str(datetime.datetime.now()).encode("utf-8"), digest_size=4
        ).hexdigest()
        svc.obj.metadata.labels["testing.opsani.com"] = sentinel_value
        await svc.patch()
        await svc.refresh()
        assert svc.obj.metadata.labels["testing.opsani.com"] == sentinel_value
