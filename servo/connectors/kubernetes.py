"""Optimize services and applications deployed on Kubernetes with Opsani.
"""
from __future__ import annotations, print_function

import abc
import asyncio
import contextlib
import copy
from datetime import date, datetime
import enum
from functools import singledispatch
import itertools
import os
import pathlib
from servo.utilities import strings
from servo.connector import metadata
import time
from traceback import print_exc
from typing import (
    Any,
    Callable,
    ClassVar,
    Collection,
    Dict,
    Generator,
    Iterable,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Type,
    Union,
    cast,
    get_type_hints,
    runtime_checkable,
)

import backoff
import kubernetes_asyncio
import pydantic

import servo
from servo import (
    Adjustment,
    BaseChecks,
    BaseConfiguration,
    BaseConnector,
    Check,
    CheckHandler,
    Component,
    Control,
    Description,
    Duration,
    DurationProgress,
    ErrorSeverity,
    License,
    Maturity,
)


class Condition(servo.logging.Mixin):
    """A Condition is a convenience wrapper around a function and its arguments
    which allows the function to be called at a later time.

    The function is called in the ``check`` method, which resolves the result to
    a boolean value, thus the condition function should return a boolean or
    something that ultimately resolves to a Truthy or Falsey value.

    Args:
        name: The name of the condition to make it easier to identify.
        fn: The condition function that will be checked.
        *args: Any arguments for the condition function.
        **kwargs: Any keyword arguments for the condition function.

    Attributes:
        name (str): The name of the Condition.
        fn (callable): The condition function that will be checked.
        args (tuple): Arguments for the checking function.
        kwargs (dict): Keyword arguments for the checking function.
        last_check (bool): Holds the state of the last condition check.

    Raises:
        ValueError: The given ``fn`` is not callable.
    """

    def __init__(self, name: str, fn: Callable, *args, **kwargs) -> None: # noqa: D107
        if not callable(fn):
            raise ValueError("The Condition function must be callable")

        self.name = name
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

        # last check holds the state of the last check.
        self.last_check = False

    def __str__(self) -> str:
        return f"<Condition (name: {self.name}, met: {self.last_check})>"

    def __repr__(self) -> str:
        return self.__str__()

    async def check(self) -> bool:
        """Check that the condition was met.

        Returns:
            True if the condition was met; False otherwise.
        """
        if asyncio.iscoroutinefunction(self.fn):
            self.last_check = bool(await self.fn(*self.args, **self.kwargs))
        else:
            self.last_check = bool(self.fn(*self.args, **self.kwargs))
        return self.last_check


async def wait_for_condition(
    condition: Condition,
    timeout: int = None,
    interval: Union[int, float] = 1,
    fail_on_api_error: bool = True,
) -> None:
    """Wait for a condition to be met.

    Args:
        condition: The Condition to wait for.
        timeout: The maximum time to wait, in seconds, for the condition to be met.
            If unspecified, this function will wait indefinitely. If specified and
            the timeout is met or exceeded, a TimeoutError will be raised.
        interval: The time, in seconds, to wait before re-checking the condition.
        fail_on_api_error: Fail the condition checks if a Kubernetes API error is
            incurred. An API error can be raised for a number of reasons, including
            a Pod being restarted and temporarily unavailable. Disabling this will
            cause those errors to be ignored, allowing the check to continue until
            timeout or resolution. (default: True).

    Raises:
        TimeoutError: The specified timeout was exceeded.
    """
    servo.logger.info(f"waiting for condition: {condition}")

    # define the maximum time to wait. once this is met, we should
    # stop waiting.
    max_time = None
    if timeout is not None:
        max_time = time.time() + timeout

    # start the wait block
    start = time.time()
    while True:
        if max_time and time.time() >= max_time:
            raise TimeoutError(
                f"timed out ({timeout}s) while waiting for condition {condition}"
            )

        # check if the condition is met and break out if it is
        try:
            servo.logger.debug(f"checking condition {condition}")
            if await condition.check():
                servo.logger.debug(f"condition passed: {condition}")
                break
        except kubernetes_asyncio.client.exceptions.ApiException as e:
            servo.logger.warning(f"encountered API exception while waiting: {e}")
            if fail_on_api_error:
                raise

        # if the condition is not met, sleep for the interval
        # to re-check later
        servo.logger.debug(f"sleeping for {interval}")
        await asyncio.sleep(interval)

    end = time.time()
    servo.logger.info(f"wait completed (total={Duration(end-start)}) {condition}")


class ResourceRequirements(enum.Flag):
    """
    The ResourceRequirement enumeration determines how optimization values are submitted to the
    Kubernetes scheduler to allocate core compute resources. Requests establish the lower bounds
    of the CPU and memory necessary for an application to execute while Limits define the upper
    bounds for resources that can be consumed by a given Pod. The Opsani engine can determine
    optimal values for these settings by identifying performant, low cost configurations that meet
    target SLOs and/or maximizing performance while identifying the point of diminishing returns
    on further resourcing.
    """

    request = enum.auto()
    limit = enum.auto()
    compute = request | limit

    @property
    def flag(self) -> bool:
        """
        Return a boolean value that indicates if the requirements are an individual flag value.

        The implementation relies on the Python `enum.Flag` modeling of individual members of
        the flag enumeration as values that are powers of two (1, 2, 4, 8, …), while combinations
        of flags are not.
        """
        value = self.value
        return bool((value & (value - 1) == 0) and value != 0)

    @property
    def flags(self) -> bool:
        """
        Return a boolean value that indicates if the requirements are a compound set of flag values.
        """
        return self.flag is False

    @property
    def resources_key(self) -> str:
        """
        Return a string value for accessing resource requirements within a Kubernetes Container representation.
        """
        if self == ResourceRequirements.request:
            return "requests"
        elif self == ResourceRequirements.limit:
            return "limits"
        else:
            raise NotImplementedError(
                f'missing key implementation for resource requirement "{self}"'
            )

    def human_readable(self) -> str:
        flags_set = []
        for flag in ResourceRequirements:
            if self.value & flag.value:
                flags_set.append(flag.name)

        return ", ".join(flags_set) if flags_set else "-"


@runtime_checkable
class KubernetesObj(Protocol):
    """
    KubernetesObj is a protocol that defines the common attributes
    of objects retrieved from the Kubernetes API.
    """

    @property
    def api_version(self) -> str:
        ...

    @property
    def kind(self) -> str:
        ...

    @property
    def metadata(self) ->kubernetes_asyncio.client.V1ObjectMeta:
        ...


class KubernetesModel(abc.ABC, servo.logging.Mixin):
    """
    KubernetesModel is an abstract base class for Servo connector
    models that wrap Kubernetes API objects.

    This base class provides common functionality and common object
    properties for all API wrappers. It also defines the following
    abstract methods which all subclasses must implement:

      - ``create``: create the resource on the cluster
      - ``patch``: partially update the resource on the cluster
      - ``delete``: remove the resource from the cluster
      - ``refresh``: refresh the underlying object model
      - ``is_ready``: check if the object is in the ready state

    Args:
         api_object: The underlying Kubernetes API object.

    Attributes:
        obj: The underlying Kubernetes API object.
    """

    obj: KubernetesObj
    """The underlying Kubernetes API object. Subclasses must update
    the type hint to reflect the type that they are wrapping.
    """

    api_clients: ClassVar[Dict[str, Type]]
    """A mapping of all the supported api clients for the API
    object type. Various resources can have multiple versions,
    e.g. "apps/v1", "apps/v1beta1", etc. The preferred version
    for each resource type should be defined under the "preferred"
    key. The preferred API client will be used when the apiVersion
    is not specified for the resource.
    """

    def __init__(self, obj, **kwargs) -> None: # noqa: D107
        self.obj = obj
        self._logger = servo.logger

    def __str__(self) -> str:
        return str(self.obj)

    def __repr__(self) -> str:
        return self.__str__()

    @classmethod
    def obj_type(cls) -> Type:
        """The type of the underlying Kubernetes API object."""
        return get_type_hints(cls)["obj"]

    @property
    def api_version(self) -> str:
        """The API version of the Kubernetes object (`obj.apiVersion``)."""
        return self.obj.api_version

    @property
    def name(self) -> str:
        """The name of the Kubernetes object (``obj.metadata.name``)."""
        return cast(str, self.obj.metadata.name)

    @name.setter
    def name(self, name: str):
        """Set the name of the Kubernetes object (``obj.metadata.name``)."""
        self.obj.metadata.name = name

    @property
    def namespace(self) -> str:
        """The namespace of the Kubernetes object (``obj.metadata.namespace``)."""
        return cast(str, self.obj.metadata.namespace)

    @namespace.setter
    def namespace(self, namespace: str):
        """Set the namespace of the Kubernetes object (``obj.metadata.namespace``)."""
        self.obj.metadata.namespace = namespace

    @contextlib.asynccontextmanager
    async def api_client(self) -> Generator[Any, None, None]:
        """The API client for the Kubernetes object. This is determined
        by the ``apiVersion`` of the object configuration.

        Raises:
            ValueError: The API version is not supported.
        """
        c = self.api_clients.get(self.api_version)
        # If we didn't find the client in the api_clients dict, use the
        # preferred version.
        if c is None:
            self.logger.warning(
                f"unknown version ({self.api_version}), falling back to preferred version"
            )
            c = self.api_clients.get("preferred")
            if c is None:
                raise ValueError(
                    "unknown version specified and no preferred version "
                    f"defined for resource ({self.api_version})"
                )
        # If we did find it, initialize that client version.
        async with kubernetes_asyncio.client.api_client.ApiClient() as api:
            yield c(api)

    @classmethod
    @contextlib.asynccontextmanager
    async def preferred_client(cls) -> Generator[Any, None, None]:
        """The preferred API client type for the Kubernetes object. This is defined in the
        ``api_clients`` class member dict for each object.

        Raises:
             ValueError: No preferred client is defined for the object.
        """
        c = cls.api_clients.get("preferred")
        if c is None:
            raise ValueError(
                f"no preferred api client defined for object {cls.__name__}",
            )
        async with kubernetes_asyncio.client.api_client.ApiClient() as api:
            yield c(api)

    @abc.abstractclassmethod
    async def read(cls, name: str, namespace: str) -> "KubernetesModel":
        """Read the underlying Kubernetes resource from the cluster and
        return a model instance.

        Args:
            name: The name of the resource to read.
            namespace: The namespace to read the resource from.
        """

    @abc.abstractmethod
    async def create(self, namespace: str = None) -> None:
        """Create the underlying Kubernetes resource in the cluster
        under the given namespace.

        Args:
            namespace: The namespace to create the resource under.
                If no namespace is provided, it will use the instance's
                namespace member, which is set when the object is created
                via the kubetestkubernetes_asyncio.client.
        """

    @abc.abstractmethod
    async def patch(self) -> None:
        """Partially update the underlying Kubernetes resource in the cluster."""

    @abc.abstractmethod
    async def delete(self, options:kubernetes_asyncio.client.V1DeleteOptions) ->kubernetes_asyncio.client.V1Status:
        """Delete the underlying Kubernetes resource from the cluster.

        This method expects the resource to have been loaded or otherwise
        assigned a namespace already. If it has not, the namespace will need
        to be set manually.

        Args:
            options: Options for resource deletion.
        """

    @abc.abstractmethod
    async def refresh(self) -> None:
        """Refresh the local state (``obj``) of the underlying Kubernetes resource."""

    @abc.abstractmethod
    async def is_ready(self) -> bool:
        """Check if the resource is in the ready state.

        It is up to the wrapper subclass to define what "ready" means for
        that particular resource.

        Returns:
            True if in the ready state; False otherwise.
        """

    async def wait_until_ready(
        self,
        timeout: int = None,
        interval: Union[int, float] = 1,
        fail_on_api_error: bool = False,
    ) -> None:
        """Wait until the resource is in the ready state.

        Args:
            timeout: The maximum time to wait, in seconds, for the resource
                to reach the ready state. If unspecified, this will wait
                indefinitely. If specified and the timeout is met or exceeded,
                a TimeoutError will be raised.
            interval: The time, in seconds, to wait before re-checking if the
                object is ready.
            fail_on_api_error: Fail if an API error is raised. An API error can
                be raised for a number of reasons, such as 'resource not found',
                which could be the case when a resource is just being started or
                restarted. When waiting for readiness we generally do not want to
                fail on these conditions.

        Raises:
             TimeoutError: The specified timeout was exceeded.
        """
        ready_condition = Condition(
            "api object ready",
            self.is_ready,
        )

        await wait_for_condition(
            condition=ready_condition,
            timeout=timeout,
            interval=interval,
            fail_on_api_error=fail_on_api_error,
        )

    async def wait_until_deleted(
        self, timeout: int = None, interval: Union[int, float] = 1
    ) -> None:
        """Wait until the resource is deleted from the cluster.

        Args:
            timeout: The maximum time to wait, in seconds, for the resource to
                be deleted from the cluster. If unspecified, this will wait
                indefinitely. If specified and the timeout is met or exceeded,
                a TimeoutError will be raised.
            interval: The time, in seconds, to wait before re-checking if the
                object has been deleted.

        Raises:
            TimeoutError: The specified timeout was exceeded.
        """

        async def deleted_fn():
            try:
                await self.refresh()
            except kubernetes_asyncio.client.exceptions.ApiException as e:
                # If we can no longer find the deployment/controller, it is deleted.
                # If we get any other exception, raise it.
                if e.status == 404 and e.reason == "Not Found":
                    return True
                else:
                    self.logger.error("error refreshing object state")
                    raise e
            else:
                # The object was still found, so it has not been deleted
                return False

        delete_condition = Condition("api object deleted", deleted_fn)

        await wait_for_condition(
            condition=delete_condition,
            timeout=timeout,
            interval=interval,
        )


class Namespace(KubernetesModel):
    """Kubetest wrapper around a Kubernetes `Namespace`_ API Object.

    The actual ``kubernetes.client.V1Namespace`` instance that this
    wraps can be accessed via the ``obj`` instance member.

    This wrapper provides some convenient functionality around the
    API Object and provides some state management for the `Namespace`_.

    .. _Namespace:
        https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.18/#namespace-v1-core
    """

    obj:kubernetes_asyncio.client.V1Namespace
    api_clients: ClassVar[Dict[str, Type]] = {
        "preferred":kubernetes_asyncio.client.CoreV1Api,
        "v1":kubernetes_asyncio.client.CoreV1Api,
    }

    @classmethod
    def new(cls, name: str) -> "Namespace":
        """Create a new Namespace with object backing.

        Args:
            name: The name of the new Namespace.

        Returns:
            A new Namespace instance.
        """
        return cls(
            obj=kubernetes_asyncio.client.V1Namespace(
                api_version="v1", metadata=kubernetes_asyncio.client.V1ObjectMeta(name=name)
            )
        )

    @classmethod
    async def read(cls, name: str) -> "Namespace":
        """Read a Namespace from the Kubernetes API.

        Args:
            name: The name of the Namespace to read.

        Returns:
            A hydrated Namespace instance.
        """
        namespace = cls.new(name)
        await namespace.refresh()
        return namespace

    async def create(self, name: str = None) -> None:
        """Create the Namespace under the given name.

        Args:
            name: The name to create the Namespace under. If the
                name is not provided, it will be assumed to already be
                in the underlying object spec. If it is not, namespace
                operations will fail.
        """
        if name is not None:
            self.name = name

        self.logger.info(f'creating namespace "{self.name}"')
        self.logger.debug(f"namespace: {self.obj}")

        async with self.api_client() as api_client:
            self.obj = await api_client.create_namespace(
                body=self.obj,
            )

    async def patch(self) -> None:
        """
        TODO: Add docs....
        """
        async with self.api_client() as api_client:
            await api_client.patch_namespace(
                name=self.name,
                body=self.obj,
            )

    async def delete(self, options:kubernetes_asyncio.client.V1DeleteOptions = None) ->kubernetes_asyncio.client.V1Status:
        """Delete the Namespace.

        Args:
             options: Options for Namespace deletion.

        Returns:
            The status of the delete operation.
        """
        if options is None:
            options =kubernetes_asyncio.client.V1DeleteOptions()

        self.logger.info(f'deleting namespace "{self.name}"')
        self.logger.debug(f"delete options: {options}")
        self.logger.debug(f"namespace: {self.obj}")

        async with self.api_client() as api_client:
            return await api_client.delete_namespace(
                name=self.name,
                body=options,
            )

    async def refresh(self) -> None:
        """Refresh the underlying Kubernetes Namespace resource."""
        async with self.api_client() as api_client:
            self.obj = await api_client.read_namespace(
                name=self.name,
            )

    async def is_ready(self) -> bool:
        """Check if the Namespace is in the ready state.

        Returns:
            True if in the ready state; False otherwise.
        """
        await self.refresh()

        status = self.obj.status
        if status is None:
            return False

        return status.phase.lower() == "active"


_DEFAULT_SENTINEL = object()


class Container(servo.logging.Mixin):
    """Kubetest wrapper around a Kubernetes `Container`_ API Object.

    The actual ``kubernetes.client.V1Container`` instance that this
    wraps can be accessed via the ``obj`` instance member.

    This wrapper provides some convenient functionality around the
    API Object and provides some state management for the `Container`_.

    This wrapper does **NOT** subclass the ``objects.ApiObject`` like other
    object wrappers because it is not intended to be created or
    managed from manifest file. It is merely meant to wrap the
    Container spec for a Pod to make Container-targeted actions
    easier.

    .. _Container:
        https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.18/#container-v1-core
    """

    def __init__(self, api_object, pod) -> None: # noqa: D107
        self.obj = api_object
        self.pod = pod

    @property
    def name(self) -> str:
        return self.obj.name

    @property
    def image(self) -> str:
        """
        Returns the container image name from the underlying container object.
        """
        return self.obj.image

    async def get_restart_count(self) -> int:
        """Get the number of times the Container has been restarted.

        Returns:
            The number of times the Container has been restarted.
        """
        container_name = self.obj.name
        pod_status = await self.pod.get_status()

        # If there are no container status, the container hasn't started
        # yet, so there cannot be any restarts.
        if pod_status.container_statuses is None:
            return 0

        for status in pod_status.container_statuses:
            if status.name == container_name:
                return status.restart_count

        raise RuntimeError(f"Unable to determine container status for {container_name}")

    @property
    def resources(self) ->kubernetes_asyncio.client.V1ResourceRequirements:
        """
        Return the resource requirements for the Container.

        Returns:
            The Container resource requirements.
        """
        return self.obj.resources

    @resources.setter
    def resources(self, resources:kubernetes_asyncio.client.V1ResourceRequirements) -> None:
        """
        Set the resource requirements for the Container.

        Args:
            resources: The resource requirements to set.
        """
        self.obj.resources = resources

    def get_resource_requirements(
        self,
        name: str,
        requirements: ResourceRequirements = ResourceRequirements.compute,
        *,
        first: bool = False,
        reverse: bool = False,
        default: Optional[str] = None,
    ) -> Union[str, Tuple[str], None]:
        """
        Retrieve resource requirement values for the Container.

        This method retrieves one or more resource requirement values with a non-exceptional,
        cascading fallback behavior. It is useful for retrieving available requirements from a
        resource that you do not know the configuration of. Requirements are read and returned in
        declaration order in the `ResourceRequirements` enumeration. Values can be retrieved as a
        tuple collection or a single value can be returned by setting the `first` argument to `True`.
        Values are evaluated in `ResourceRequirements` declaration order and the first requirement
        that contains a string value is returned. Evaluation order can be reversed via the
        `reverse=True` argument. This is useful is you want to retrieve the limit if one exists or
        else fallback to the request value.

        Args:
            name: The name of the resource to retrieve the requirements of (e.g. "cpu" or "memory").
            requirements: A `ResourceRequirements` flag enumeration specifying the requirements to retrieve.
                Multiple values can be retrieved by using a bitwise or (`|`) operator. Defaults to
                `ResourceRequirements.compute` which is equal to `ResourceRequirements.request | ResourceRequirements.limit`.
            first: When True, a single value is returned for the first requirement to return a value.
            reverse: When True, the `ResourceRequirements` enumeration evaluated in reverse order.
            default: A default value to return when a resource requirement could not be found.

        Returns:
            A tuple of resource requirement strings or `None` values in input order or a singular, optional string
            value when `first` is True.
        """
        values: List[Union[str, None]] = []
        found_requirements = False
        members = (
            reversed(ResourceRequirements) if reverse else list(ResourceRequirements)
        )

        # iterate all members of the enumeration to support bitwise combinations
        for member in members:
            # skip named combinations of flags
            if member.flags:
                continue

            if requirements & member:
                if not hasattr(self.resources, member.resources_key):
                    raise ValueError(f"unknown resource requirement '{member}'")

                requirement_dict: Dict[str, str] = getattr(
                    self.resources, member.resources_key
                )
                if requirement_dict and name in requirement_dict:
                    value = requirement_dict[name]
                    found_requirements = True

                    if first:
                        return value
                    else:
                        values.append(value)

                else:
                    servo.logger.warning(
                        f"requirement '{member}' is not set for resource '{name}'"
                    )
                    values.append(default)

        if not found_requirements:
            if first:
                # code path only accessible on nothing found due to early exit
                servo.logger.debug(
                    f"no resource requirements found. returning default value: {default}"
                )
                return default
            else:
                servo.logger.debug(
                    f"no resource requirements found. returning default values: {values}"
                )

        return tuple(values)

    def set_resource_requirements(
        self,
        name: str,
        value: Union[str, Sequence[str]],
        requirements: ResourceRequirements = ResourceRequirements.compute,
        *,
        clear_others: bool = False,
    ) -> None:
        """
        Set the value for one or more resource requirements on the underlying Container.

        Args:
            name: The resource to set requirements for (e.g. "cpu" or "memory").
            value: The string value or tuple of string values to assign to the resources. Values are
                assigned in declaration order of members of the `ResourceRequirements` enumeration. If a
                single value is provided, it is assigned to all requirements.
            clear_others: When True, any requirements not specified in the input arguments are cleared.
        """

        values = [value] if isinstance(value, str) else list(value)
        default = values[0]
        for requirement in list(ResourceRequirements):
            # skip named combinations of flags
            if requirement.flags:
                continue

            if not hasattr(self.resources, requirement.resources_key):
                raise ValueError(f"unknown resource requirement '{requirement}'")

            req_dict: Optional[Dict[str, Union[str, None]]] = getattr(
                self.resources, requirement.resources_key
            )
            if req_dict is None:
                # we are establishing the first requirements for this resource, hydrate the model
                req_dict = {}
                setattr(self.resources, requirement.resources_key, req_dict)

            if requirement & requirements:
                req_value = values.pop(0) if len(values) else default
                req_dict[name] = req_value

            else:
                if clear_others:
                    self.logger.debug(f"clearing resource requirement: '{requirement}'")
                    req_dict.pop(name, None)

    def __str__(self) -> str:
        return str(self.obj)

    def __repr__(self) -> str:
        return self.__str__()


class Pod(KubernetesModel):
    """Wrapper around a Kubernetes `Pod`_ API Object.

    The actual ``kubernetes.client.V1Pod`` instance that this
    wraps can be accessed via the ``obj`` instance member.

    This wrapper provides some convenient functionality around the
    API Object and provides some state management for the `Pod`_.

    .. _Pod:
        https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.18/#pod-v1-core
    """

    obj:kubernetes_asyncio.client.V1Pod

    api_clients: ClassVar[Dict[str, Type]] = {
        "preferred":kubernetes_asyncio.client.CoreV1Api,
        "v1":kubernetes_asyncio.client.CoreV1Api,
    }

    @classmethod
    @backoff.on_exception(backoff.expo, asyncio.TimeoutError, max_time=60)
    async def read(cls, name: str, namespace: str) -> "Pod":
        """Read the Pod from the cluster under the given namespace.

        Args:
            name: The name of the Pod to read.
            namespace: The namespace to read the Pod from.
        """
        servo.logger.info(f'reading pod "{name}" in namespace "{namespace}"')

        async with cls.preferred_client() as api_client:
            obj = await asyncio.wait_for(
                api_client.read_namespaced_pod_status(name, namespace), 5.0
            )
            servo.logger.trace("pod: ", obj)
            return Pod(obj)

    async def create(self, namespace: str = None) -> None:
        """Create the Pod under the given namespace.

        Args:
            namespace: The namespace to create the Pod under.
                If the Pod was loaded via the kubetest client, the
                namespace will already be set, so it is not needed
                here. Otherwise, the namespace will need to be provided.
        """
        if namespace is None:
            namespace = self.namespace

        self.logger.info(f'creating pod "{self.name}" in namespace "{self.namespace}"')
        self.logger.trace(f"pod: {self.obj}")

        async with self.preferred_client() as api_client:
            self.obj = await api_client.create_namespaced_pod(
                namespace=namespace,
                body=self.obj,
            )

    async def patch(self) -> None:
        """
        Patches a Pod, applying spec changes to the cluster.
        """
        self.logger.info(f'patching pod "{self.name}"')
        self.logger.trace(f"pod: {self.obj}")
        async with self.api_client() as api_client:
            await api_client.patch_namespaced_pod(
                name=self.name,
                namespace=self.namespace,
                body=self.obj,
            )

    async def delete(self, options:kubernetes_asyncio.client.V1DeleteOptions = None) ->kubernetes_asyncio.client.V1Status:
        """Delete the Pod.

        This method expects the Pod to have been loaded or otherwise
        assigned a namespace already. If it has not, the namespace will
        need to be set manually.

        Args:
            options: Options for Pod deletion.

        Return:
            The status of the delete operation.
        """
        if options is None:
            options =kubernetes_asyncio.client.V1DeleteOptions()

        self.logger.info(f'deleting pod "{self.name}"')
        self.logger.trace(f"delete options: {options}")
        self.logger.trace(f"pod: {self.obj}")

        async with self.api_client() as api_client:
            return await api_client.delete_namespaced_pod(
                name=self.name,
                namespace=self.namespace,
                body=options,
            )

    @backoff.on_exception(backoff.expo, asyncio.TimeoutError, max_time=60)
    async def refresh(self) -> None:
        """Refresh the underlying Kubernetes Pod resource."""
        async with self.api_client() as api_client:
            self.obj = await asyncio.wait_for(
                api_client.read_namespaced_pod_status(
                    name=self.name, namespace=self.namespace
                ),
                5.0,
            )

    async def is_ready(self) -> bool:
        """Check if the Pod is in the ready state.

        Returns:
            True if in the ready state; False otherwise.
        """
        self.logger.trace("refreshing pod status to check is_ready")
        await self.refresh()

        # if there is no status, the pod is definitely not ready
        status = self.obj.status
        self.logger.trace(f"current pod status is {status}")
        if status is None:
            return False

        # check the pod phase to make sure it is running. a pod in
        # the 'failed' or 'success' state will no longer be running,
        # so we only care if the pod is in the 'running' state.
        phase = status.phase
        self.logger.trace(f"current pod phase is {status}")
        if phase.lower() != "running":
            return False

        self.logger.trace(f"checking status conditions {status.conditions}")
        for cond in status.conditions:
            # we only care about the condition type 'ready'
            if cond.type.lower() != "ready":
                continue

            # check that the readiness condition is True
            return cond.status.lower() == "true"

        # Catchall
        self.logger.trace(f"unable to find ready=true, continuing to wait...")
        return False

    async def get_status(self) ->kubernetes_asyncio.client.V1PodStatus:
        """Get the status of the Pod.

        Returns:
            The status of the Pod.
        """
        # first, refresh the pod state to ensure latest status
        await self.refresh()

        # return the status of the pod
        return cast(kubernetes_asyncio.client.V1PodStatus, self.obj.status)

    @property
    def containers(self) -> List[Container]:
        """
        Return a list of Container objects from the underlying pod template spec.
        """
        return list(map(lambda c: Container(c, self), self.obj.spec.containers))

    async def get_containers(self) -> List[Container]:
        """Get the Pod's containers.

        Returns:
            A list of containers that belong to the Pod.
        """
        self.logger.info(f'getting containers for pod "{self.name}"')
        await self.refresh()

        return self.containers

    def get_container(self, name: str) -> Union[Container, None]:
        """Get a container in the Pod by name.

        Args:
            name (str): The name of the Container.

        Returns:
            Container: The Pod's Container with the matching name. If
            no container with the given name is found, ``None`` is returned.
        """
        return next(filter(lambda c: c.name == name, self.containers), None)

    async def get_restart_count(self) -> int:
        """Get the total number of Container restarts for the Pod.

        Returns:
            The total number of Container restarts.
        """
        status = await self.get_status()
        if status.container_statuses is None:
            return 0

        total = 0
        for container_status in status.container_statuses:
            total += container_status.restart_count

        return total

    async def containers_started(self) -> bool:
        """Check if the Pod's Containers have all started.

        Returns:
            True if all Containers have started; False otherwise.
        """
        # start the flag as true - we will check the state and set
        # this to False if any container is not yet running.
        containers_started = True

        status = await self.get_status()
        if status.container_statuses is not None:
            for container_status in status.container_statuses:
                if container_status.state is not None:
                    if container_status.state.running is not None:
                        if container_status.state.running.started_at is not None:
                            # The container is started, so move on to check the
                            # next container
                            continue
                # If we get here, then the container has not started.
                containers_started = containers_started and False
                break

        return containers_started

    def uid(self) -> str:
        """
        Gets the UID for the Pod.

        UID is the unique in time and space value for this object. It is typically generated by the server on successful creation of a resource and is not allowed to change on PUT operations.  Populated by the system. Read-only. More info: http://kubernetes.io/docs/user-guide/identifiers#uids  # noqa: E501
        """
        return self.obj.metadata.uid


class ControllerModel(KubernetesModel, abc.ABC):
    """Abstract base class for Controllers in Kubernetes
    """

    async def get_pods(self) -> List[Pod]:
        """Get the pods for the controller.

        Returns:
            A list of pods that belong to the deployment.
        """
        self.logger.info(f'getting pods for {self.obj.kind} "{self.name}"')

        async with Pod.preferred_client() as api_client:
            label_selector = self.obj.spec.selector.match_labels
            pod_list:kubernetes_asyncio.client.V1PodList = await api_client.list_namespaced_pod(
                namespace=self.namespace, label_selector=selector_string(label_selector)
            )

        pods = [Pod(p) for p in pod_list.items]
        return pods

    @property
    def resource_version(self) -> str:
        """
        Returns the resource version of the Controller.
        """
        return self.obj.metadata.resource_version

    @property
    def observed_generation(self) -> str:
        """
        Returns the observed generation of the Controller status.

        The generation is observed by the controller.
        """
        return self.obj.status.observed_generation

    async def is_ready(self) -> bool:
        """Check if the Controller is in the ready state.

        Returns:
            True if in the ready state; False otherwise.
        """
        await self.refresh()

        # if there is no status, the deployment is definitely not ready
        status = self.obj.status
        if status is None:
            return False

        # check the status for the number of total replicas and compare
        # it to the number of ready replicas. if the numbers are
        # equal, the deployment is ready; otherwise it is not ready.
        total = status.replicas
        ready = status.ready_replicas

        if total is None:
            return False

        return total == ready

    @property
    def containers(self) -> List[Container]:
        """
        Return a list of Container objects from the underlying pod template spec.
        """
        return list(
            map(lambda c: Container(c, None), self.obj.spec.template.spec.containers)
        )

    def find_container(self, name: str) -> Optional[Container]:
        """
        Return the container with the given name.
        """
        return next(filter(lambda c: c.name == name, self.containers), None)

    @property
    def replicas(self) -> int:
        """
        Return the number of desired pods.
        """
        return self.obj.spec.replicas

    @replicas.setter
    def replicas(self, replicas: int) -> None:
        """
        Set the number of desired pods.
        """
        self.obj.spec.replicas = replicas

    @property
    def label_selector(self) -> str:
        """
        Return a string for matching the Deployment in Kubernetes API calls.
        """
        return selector_string(self.obj.spec.selector.match_labels)

    @abc.abstractmethod
    async def apply(self) -> None: # TODO should this be part of KubernetesModel?
        """Adjust and wait for the underlying Kubernetes resource in the cluster to be finished adjusting."""
        ...

    ##
    # Canary support

    @property
    def canary_pod_name(self) -> str:
        """
        Return the name of canary Pod for this Deployment.
        """
        return f"{self.name}-canary"

    async def get_canary_pod(self) -> Pod:
        """
        Retrieve the canary Pod for this Deployment (if any).

        Will raise a Kubernetes API exception if not found.
        """
        return await Pod.read(self.canary_pod_name, self.namespace)

    async def delete_canary_pod(
        self, *, raise_if_not_found: bool = True, timeout: servo.Numeric = 600
    ) -> Optional[Pod]:
        """
        Delete the canary Pod.
        """
        try:
            canary = await self.get_canary_pod()
            self.logger.warning(
                f"Deleting canary Pod '{canary.name}' from namespace '{canary.namespace}'..."
            )
            await canary.delete()
            await canary.wait_until_deleted(timeout=timeout)
            self.logger.info(
                f"Deleted canary Pod '{canary.name}' from namespace '{canary.namespace}'."
            )
            return canary
        except kubernetes_asyncio.client.exceptions.ApiException as e:
            if e.status != 404 or e.reason != "Not Found" and raise_if_not_found:
                raise

        return None

    async def ensure_canary_pod(self, *, timeout: servo.Numeric = 600) -> Pod:
        """
        Ensures that a canary Pod exists by deleting and recreating an existing Pod or creating one from scratch.

        TODO: docs...
        """
        canary_pod_name = self.canary_pod_name
        namespace = self.namespace
        self.logger.debug(
            f"ensuring existence of canary pod '{canary_pod_name}' based on deployment '{self.name}' in namespace '{namespace}'"
        )

        # Look for an existing canary
        try:
            if canary_pod := await self.get_canary_pod():
                self.logger.info(
                    f"found existing canary pod '{canary_pod_name}' based on deployment '{self.name}' in namespace '{namespace}'"
                )
                return canary_pod
        except kubernetes_asyncio.client.exceptions.ApiException as e:
            if e.status != 404 or e.reason != "Not Found":
                raise

        # Setup the canary Pod -- our settings are updated on the underlying PodSpec template
        self.logger.trace(f"building new canary")
        if isinstance(self.obj, pydantic.BaseModel): # If obj is a custom model, dumpt to dict and cast into k8s client model via __deserialize_model
            pod_dict = {
                'metadata': self.obj.spec.template.metadata.dict(by_alias=True, exclude_none=True),
                'spec': self.obj.spec.template.spec.dict(by_alias=True, exclude_none=True)
            }
            async with kubernetes_asyncio.client.api_client.ApiClient() as api_client:
                pod_obj = api_client._ApiClient__deserialize_model(pod_dict, kubernetes_asyncio.client.V1Pod)
        else:
            pod_obj =kubernetes_asyncio.client.V1Pod(
                metadata=self.obj.spec.template.metadata, spec=self.obj.spec.template.spec
            )
        pod_obj.metadata.name = canary_pod_name
        if pod_obj.metadata.annotations is None:
            pod_obj.metadata.annotations = {}
        pod_obj.metadata.annotations["opsani.com/opsani_tuning_for"] = self.name
        if pod_obj.metadata.labels is None:
            pod_obj.metadata.labels = {}
        pod_obj.metadata.labels["opsani_role"] = "tuning"

        if isinstance(self, Rollout): # Add argo specific label to allow active service to adopt the canary pod
            pod_obj.metadata.labels["rollouts-pod-template-hash"] = self.obj.status.current_pod_hash

        canary_pod = Pod(obj=pod_obj)
        canary_pod.namespace = namespace
        self.logger.trace(f"initialized new canary: {canary_pod}")

        # If the servo is running inside Kubernetes, register self as the controller for the Pod and ReplicaSet
        SERVO_POD_NAME = os.environ.get("POD_NAME")
        SERVO_POD_NAMESPACE = os.environ.get("POD_NAMESPACE")
        if SERVO_POD_NAME is not None and SERVO_POD_NAMESPACE is not None:
            self.logger.debug(
                f"running within Kubernetes, registering as Pod controller... (pod={SERVO_POD_NAME}, namespace={SERVO_POD_NAMESPACE})"
            )
            servo_pod = await Pod.read(SERVO_POD_NAME, SERVO_POD_NAMESPACE)
            pod_controller = next(
                iter(
                    ow
                    for ow in servo_pod.obj.metadata.owner_references
                    if ow.controller
                )
            )

            # # TODO: Create a ReplicaSet class...
            async with kubernetes_asyncio.client.api_client.ApiClient() as api:
                api_client =kubernetes_asyncio.client.AppsV1Api(api)

                servo_rs:kubernetes_asyncio.client.V1ReplicaSet = (
                    await api_client.read_namespaced_replica_set(
                        name=pod_controller.name, namespace=SERVO_POD_NAMESPACE
                    )
                )  # still ephemeral
                rs_controller = next(
                    iter(
                        ow for ow in servo_rs.metadata.owner_references if ow.controller
                    )
                )
                servo_dep:kubernetes_asyncio.client.V1Deployment = (
                    await api_client.read_namespaced_deployment(
                        name=rs_controller.name, namespace=SERVO_POD_NAMESPACE
                    )
                )

            canary_pod.obj.metadata.owner_references = [
               kubernetes_asyncio.client.V1OwnerReference(
                    api_version=servo_dep.api_version,
                    block_owner_deletion=True,
                    controller=True,  # Ensures the pod will not be adopted by another controller
                    kind="Deployment",
                    name=servo_dep.metadata.name,
                    uid=servo_dep.metadata.uid,
                )
            ]

        # Create the Pod and wait for it to get ready
        self.logger.info(
            f"Creating canary Pod '{canary_pod_name}' in namespace '{namespace}'"
        )
        await canary_pod.create()

        self.logger.info(
            f"Created canary Pod '{canary_pod_name}' in namespace '{namespace}', waiting for it to become ready..."
        )
        await canary_pod.wait_until_ready(timeout=timeout)

        # TODO: Check for unexpected changes to version, etc.

        await canary_pod.refresh()
        await canary_pod.get_containers()

        return canary_pod



class Deployment(ControllerModel):
    """Kubetest wrapper around a Kubernetes `Deployment`_ API Object.

    The actual ``kubernetes.client.V1Deployment`` instance that this
    wraps can be accessed via the ``obj`` instance member.

    This wrapper provides some convenient functionality around the
    API Object and provides some state management for the `Deployment`_.

    .. _Deployment:
        https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.18/#deployment-v1-apps
    """

    obj:kubernetes_asyncio.client.V1Deployment
    api_clients: ClassVar[Dict[str, Type]] = {
        "preferred":kubernetes_asyncio.client.AppsV1Api,
        "apps/v1":kubernetes_asyncio.client.AppsV1Api,
        "apps/v1beta1":kubernetes_asyncio.client.AppsV1beta1Api,
        "apps/v1beta2":kubernetes_asyncio.client.AppsV1beta2Api,
    }

    async def create(self, namespace: str = None) -> None:
        """Create the Deployment under the given namespace.

        Args:
            namespace: The namespace to create the Deployment under.
                If the Deployment was loaded via the kubetest client, the
                namespace will already be set, so it is not needed here.
                Otherwise, the namespace will need to be provided.
        """
        if namespace is None:
            namespace = self.namespace

        self.logger.info(
            f'creating deployment "{self.name}" in namespace "{namespace}"'
        )
        self.logger.debug(f"deployment: {self.obj}")

        async with self.api_client() as api_client:
            self.obj = await api_client.create_namespaced_deployment(
                namespace=namespace,
                body=self.obj,
            )

    @classmethod
    async def read(cls, name: str, namespace: str) -> "Deployment":
        """Read a Deployment by name under the given namespace.

        Args:
            name: The name of the Deployment to read.
            namespace: The namespace to read the Deployment from.
        """

        async with cls.preferred_client() as api_client:
            obj = await api_client.read_namespaced_deployment(name, namespace)
            return Deployment(obj)

    async def patch(self) -> None:
        """Update the changed attributes of the Deployment."""
        async with self.api_client() as api_client:
            self.obj = await api_client.patch_namespaced_deployment(
                name=self.name,
                namespace=self.namespace,
                body=self.obj,
            )

                
    async def apply(self) -> None:
        """
        Apply changes asynchronously and wait for them to roll out to the cluster.

        Kubernetes deployments orchestrate a number of underlying resources. Awaiting the
        outcome of a deployment change requires observation of the `resource_version` which
        indicates if a given patch actually changed the resource, the `observed_generation`
        which is a value managed by the deployments controller and indicates the effective
        version of the deployment exclusive of insignificant changes that do not affect runtime
        (such as label updates), and the `conditions` of the deployment status which reflect
        state at a particular point in time. How these elements change during a rollout is
        dependent on the deployment strategy in effect and its requirementss (max unavailable,
        surge, etc).

        The logic implemented by this method is as follows:
            - Capture the `resource_version` and `observed_generation`.
            - Patch the underlying Deployment object via the Kubernetes API.
            - Check that `resource_version` has been incremented or return early if nothing has changed.
            - Create a Kubernetes Watch on the Deployment targeted by label selector and resource version.
            - Observe events streamed via the watch.
            - Look for the Deployment to report a Status Condition of `"Progressing"`.
            - Wait for the `observed_generation` to increment indicating that the Deployment is applying our changes.
            - Track the value of the `available_replicas`, `ready_replicas`, `unavailable_replicas`,
                and `updated_replicas` attributes of the Deployment Status until `available_replicas`,
                `ready_replicas`, and `updated_replicas` are all equal to the value of the `replicas` attribute of
                the Deployment and `unavailable_replicas` is `None`. Return success.
            - Raise an error upon expiration of an adjustment timeout or encountering a Deployment Status Condition
                where `type=Progressing` and `status=False`.

        This method abstracts the details of adjusting a Deployment and returns once the desired
        changes have been fully rolled out to the cluster or an error has been encountered.

        See https://kubernetes.io/docs/concepts/workloads/controllers/deployment/

        # The resource_version attribute lets us efficiently watch for changes
        # reference: https://kubernetes.io/docs/reference/using-api/api-concepts/#efficient-detection-of-changes
        """
        # Resource version lets us track any change. Observed generation only increments
        # when the controller sees a significant change that requires rollout
        resource_version = self.resource_version
        observed_generation = self.status.observed_generation
        desired_replicas = self.replicas

        # Patch the controller via the Kubernetes API
        await self.patch()

        # Return fast if nothing was changed
        if self.resource_version == resource_version:
            self.logger.info(
                f"adjustments applied to Deployment '{self.name}' made no changes, continuing"
            )
            return

        # Create a Kubernetes watch against the controller under optimization to track changes
        self.logger.info(
            f"Using label_selector={self.label_selector}, resource_version={resource_version}"
        )
        async with kubernetes_asyncio.client.api_client.ApiClient() as api:
            v1 =kubernetes_asyncio.client.AppsV1Api(api)
            # BIG FAT TODO
            async with kubernetes_asyncio.watch.Watch().stream(
                v1.list_namespaced_deployment,
                self.namespace,
                label_selector=self.label_selector,
                # resource_version=resource_version, # FIXME: The resource version might be expired and fail the watch. Decide if we care
            ) as stream:
                async for event in stream:
                    # NOTE: Event types are ADDED, DELETED, MODIFIED, ERROR
                    event_type, deployment = event["type"], event["object"]
                    status:kubernetes_asyncio.client.V1DeploymentStatus = deployment.status

                    self.logger.debug(
                        f"deployment watch yielded event: {event_type} {deployment.kind} {deployment.metadata.name} in {deployment.metadata.namespace}: {status}"
                    )

                    if event_type == "ERROR":
                        stream.stop()
                        raise RuntimeError(str(deployment))

                    # Check that the conditions aren't reporting a failure
                    self._check_conditions(status.conditions)

                    # Early events in the watch may be against previous generation
                    if status.observed_generation == observed_generation:
                        self.logger.debug(
                            "observed generation has not changed, continuing watch"
                        )
                        continue

                    # Check the replica counts. Once available, updated, and ready match
                    # our expected count and the unavailable count is zero we are rolled out
                    if status.unavailable_replicas:
                        self.logger.debug(
                            "found unavailable replicas, continuing watch",
                            status.unavailable_replicas,
                        )
                        continue

                    replica_counts = [
                        status.replicas,
                        status.available_replicas,
                        status.ready_replicas,
                        status.updated_replicas,
                    ]
                    if replica_counts.count(desired_replicas) == len(replica_counts):
                        # We are done: all the counts match. Stop the watch and return
                        self.logger.info("adjustment applied successfully", status)
                        stream.stop()
                        return

    def _check_conditions(self, conditions: List[kubernetes_asyncio.client.V1DeploymentCondition]):
        for condition in conditions:
            if condition.type == "Available":
                if condition.status == "True":
                    # If we hit on this and have not raised yet we are good to go
                    break
                elif condition.status in ("False", "Unknown"):
                    # Condition has not yet been met, log status and continue monitoring
                    self.logger.debug(
                        f"Condition({condition.type}).status == '{condition.status}' ({condition.reason}): {condition.message}"
                    )
                else:
                    raise RuntimeError(
                        f"encountered unexpected Condition status '{condition.status}'"
                    )

            elif condition.type == "ReplicaFailure":
                # TODO: Create a specific error type
                raise RuntimeError(
                    "ReplicaFailure: message='{condition.status.message}', reason='{condition.status.reason}'"
                )

            elif condition.type == "Progressing":
                if condition.status in ("True", "Unknown"):
                    # Still working
                    self.logger.debug("Deployment update is progressing", condition)
                    break
                if condition.status == "False":
                    # TODO: Create specific error type
                    raise RuntimeError(
                        "ProgressionFailure: message='{condition.status.message}', reason='{condition.status.reason}'"
                    )
                else:
                    raise AssertionError(
                        f"unknown deployment status condition: {condition.status}"
                    )

    async def delete(self, options:kubernetes_asyncio.client.V1DeleteOptions = None) ->kubernetes_asyncio.client.V1Status:
        """Delete the Deployment.

        This method expects the Deployment to have been loaded or otherwise
        assigned a namespace already. If it has not, the namespace will need
        to be set manually.

        Args:
            options: Options for Deployment deletion.

        Returns:
            The status of the delete operation.
        """
        if options is None:
            options =kubernetes_asyncio.client.V1DeleteOptions()

        self.logger.info(f'deleting deployment "{self.name}"')
        self.logger.debug(f"delete options: {options}")
        self.logger.trace(f"deployment: {self.obj}")

        async with self.api_client() as api_client:
            return await api_client.delete_namespaced_deployment(
                name=self.name,
                namespace=self.namespace,
                body=options,
            )

    async def refresh(self) -> None:
        """Refresh the underlying Kubernetes Deployment resource."""
        async with self.api_client() as api_client:
            self.obj = await api_client.read_namespaced_deployment_status(
                name=self.name,
                namespace=self.namespace,
            )

    async def rollback(self) -> None:
        """Roll back an unstable Deployment revision to a previous version."""
        async with kubernetes_asyncio.client.api_client.ApiClient() as api:
            api_client =kubernetes_asyncio.client.ExtensionsV1beta1Api(api)
            self.obj = await api_client.create_namespaced_deployment_rollback(
                name=self.name,
                namespace=self.namespace,
                body=self.obj,
            )

    async def get_status(self) ->kubernetes_asyncio.client.V1DeploymentStatus:
        """Get the status of the Deployment.

        Returns:
            The status of the Deployment.
        """
        self.logger.info(f'checking status of deployment "{self.name}"')
        # first, refresh the deployment state to ensure the latest status
        await self.refresh()

        # return the status from the deployment
        return cast(kubernetes_asyncio.client.V1DeploymentStatus, self.obj.status)

    @property
    def status(self) ->kubernetes_asyncio.client.V1DeploymentStatus:
        """Return the status of the Deployment.

        Returns:
            The status of the Deployment.
        """
        return cast(kubernetes_asyncio.client.V1DeploymentStatus, self.obj.status)

# Use alias generator so that lower camel case can be parsed to snake case properties to match k8s python client behaviour
def to_lower_camel(string: str) -> str:
    split = string.split('_')
    return split[0] + ''.join(word.capitalize() for word in split[1:])

class RolloutBaseModel(pydantic.BaseModel):
    class Config:
        # arbitrary_types_allowed = True
        alias_generator = to_lower_camel

# Pydantic type models for argo rollout spec: https://argoproj.github.io/argo-rollouts/features/specification/
# https://github.com/argoproj/argo-rollouts/blob/master/manifests/crds/rollout-crd.yaml
# NOTE/TODO: fields typed with Any should maintain the same form when dumped as when they are parsed. Should the need 
#   arise to interact with such fields, they will need to have an explicit type defined so the alias_generator is applied
class RolloutV1LabelSelector(RolloutBaseModel): # must type out k8s models as well to allow parse_obj to work
    match_expressions: Any
    match_labels: Dict[str, str]

class RolloutV1ObjectMeta(RolloutBaseModel):
    annotations: Dict[str, str]
    cluster_name: Optional[str]
    creation_timestamp: Optional[datetime]
    deletion_grace_period_seconds: Optional[int]
    deletion_timestamp: Optional[datetime]
    finalizers: Optional[List[str]]
    generate_name: Optional[str]
    generation: Optional[int]
    labels: Dict[str, str]
    managed_fields: Any
    name: Optional[str]
    namespace: Optional[str]
    owner_references: Any
    resource_version: Optional[str]
    self_link: Optional[str]
    uid: Optional[str]

class RolloutV1ResourceRequirements(RolloutBaseModel):
    limits: Optional[Dict[str, str]]
    requests: Optional[Dict[str, str]]

class RolloutV1VolumeMount(RolloutBaseModel):
    mount_path: str
    mount_propagation: Optional[str]
    name: str
    read_only: Optional[bool]
    sub_path: Optional[str]
    sub_path_expr: Optional[str]

class RolloutV1Container(RolloutBaseModel):
    args: Optional[List[str]]
    command: Optional[List[str]]
    env: Any
    env_from: Any
    image: str
    image_pull_policy: Optional[str]
    lifecycle: Any
    liveness_probe: Any
    name: str
    ports: Any
    readiness_probe: Any
    resources: RolloutV1ResourceRequirements
    security_context: Any
    startup_probe: Any
    stdin: Optional[bool]
    stdin_once: Optional[bool]
    termination_message_path: Optional[str]
    termination_message_policy: Optional[str]
    tty: Optional[bool]
    volume_devices: Any
    volume_mounts: Optional[List[RolloutV1VolumeMount]]
    working_dir: Optional[str]

class RolloutV1PodSpec(RolloutBaseModel):
    active_deadline_seconds: Optional[int]
    affinity: Any
    automount_service_account_token: Optional[bool]
    containers: List[RolloutV1Container]
    dns_config: Any
    dns_policy: Optional[str]
    enable_service_links: Optional[bool]
    ephemeral_containers: Any
    host_aliases: Any
    host_ipc: Optional[bool]
    host_network: Optional[bool]
    host_pid: Optional[bool]
    hostname: Optional[str]
    image_pull_secrets: Any
    init_containers: Optional[List[RolloutV1Container]]
    node_name: Optional[str]
    node_selector: Optional[Dict[str, str]]
    overhead: Optional[Dict[str, str]]
    preemption_policy: Optional[str]
    priority: Optional[int]
    priority_class_name: Optional[str]
    readiness_gates: Any
    restart_policy: Optional[str]
    runtime_class_name: Optional[str]
    scheduler_name: Optional[str]
    security_context: Any
    service_account: Optional[str]
    service_account_name: Optional[str]
    share_process_namespace: Optional[bool]
    subdomain: Optional[str]
    termination_grace_period_seconds: Optional[int]
    tolerations: Any
    topology_spread_constraints: Any
    volumes: Any

class RolloutV1PodTemplateSpec(RolloutBaseModel):
    metadata: RolloutV1ObjectMeta
    spec: RolloutV1PodSpec

class RolloutSpec(RolloutBaseModel):
    replicas: int
    selector: RolloutV1LabelSelector
    template: RolloutV1PodTemplateSpec
    min_ready_seconds: Optional[int]
    revision_history_limit: int
    paused: Optional[bool]
    progress_deadline_seconds: Optional[int]
    restart_at: Optional[datetime]
    strategy: Any

class RolloutBlueGreenStatus(RolloutBaseModel):
    active_selector: Optional[str]
    post_promotion_analysis_run: Optional[str]
    post_promotion_analysis_run_status: Any
    pre_promotion_analysis_run: Optional[str]
    pre_promotion_analysis_run_status: Any
    preview_selector: Optional[str]
    previous_active_selector: Optional[str]
    scale_down_delay_start_time: Optional[datetime]
    scale_up_preview_check_point: Optional[bool]

class RolloutStatusCondition(RolloutBaseModel):
    last_transition_time: datetime
    last_update_time: datetime
    message: str
    reason: str
    status: str
    type: str

class RolloutStatus(RolloutBaseModel):
    HPA_replicas: int
    abort: Optional[bool]
    aborted_at: Optional[datetime]
    available_replicas: int
    blue_green: RolloutBlueGreenStatus
    canary: Any #  TODO type this out if connector needs to interact with it
    collision_count: Optional[int]
    conditions: List[RolloutStatusCondition]
    controller_pause: Optional[bool]
    current_pod_hash: str
    current_step_hash: Optional[str]
    current_step_index: Optional[int]
    observed_generation: str
    pause_conditions: Any
    ready_replicas: int
    replicas: int
    restarted_at: Optional[datetime]
    selector: str
    stable_RS: Optional[str]
    updated_replicas: int

class RolloutObj(RolloutBaseModel): # TODO is this the right base to inherit from?
    api_version: str
    kind: str
    metadata: RolloutV1ObjectMeta
    spec: RolloutSpec
    status: RolloutStatus

# TODO expose to config if needed
ROLLOUT_GROUP = "argoproj.io"
ROLLOUT_VERSION = "v1alpha1"
ROLLOUT_PURAL = "rollouts"

ROLLOUT_CONST_ARGS = dict(
    group=ROLLOUT_GROUP, 
    version=ROLLOUT_VERSION,
    plural=ROLLOUT_PURAL,
)

class Rollout(ControllerModel):
    """Wrapper around an ArgoCD Kubernetes `Rollout` Object.

    The actual instance that this
    wraps can be accessed via the ``obj`` instance member.

    This wrapper provides some convenient functionality around the
    API Object and provides some state management for the `Rollout`.

    .. Rollout:
        https://argoproj.github.io/argo-rollouts/features/specification/
    """

    obj: RolloutObj

    api_clients: ClassVar[Dict[str, Type]] = {
        "preferred":kubernetes_asyncio.client.CustomObjectsApi,
        "argoproj.io/v1alpha1":kubernetes_asyncio.client.CustomObjectsApi,
    }

    async def create(self, namespace: str = None) -> None:
        """Create the Rollout under the given namespace.

        Args:
            namespace: The namespace to create the Rollout under.
        """
        if namespace is None:
            namespace = self.namespace

        self.logger.info(
            f'creating rollout "{self.name}" in namespace "{namespace}"'
        )
        self.logger.debug(f"rollout: {self.obj}")

        async with self.api_client() as api_client:
            self.obj = RolloutObj.parse_obj(await api_client.create_namespaced_custom_object(
                namespace=namespace,
                body=self.obj.dict(by_alias=True, exclude_none=True),
                **ROLLOUT_CONST_ARGS,
            ))

    @classmethod
    async def read(cls, name: str, namespace: str) -> "Rollout":
        """Read a Rollout by name under the given namespace.

        Args:
            name: The name of the Deployment to read.
            namespace: The namespace to read the Deployment from.
        """

        async with cls.preferred_client() as api_client:
            obj = await api_client.get_namespaced_custom_object(
                namespace=namespace,
                name=name,
                **ROLLOUT_CONST_ARGS,
            )
            return Rollout(RolloutObj.parse_obj(obj))

    async def patch(self) -> None:
        """Update the changed attributes of the Deployment."""
        async with self.api_client() as api_client:
            self.obj = RolloutObj.parse_obj(await api_client.patch_namespaced_custom_object(
                namespace=self.namespace,
                name=self.name,
                body=self.obj.dict(by_alias=True, exclude_none=True),
                **ROLLOUT_CONST_ARGS,
            ))

    async def apply(self) -> None:
        """
        patch an object (patch_namespaced_custom_object)
        returns: rollout-selector
        raises: non ApiExceptions
        """
        # Patch the rollout via the Kubernetes API
        await self.patch()

        start_time = time.time()
        async with self.api_client() as api_client:
            while True:
                # Sleep first to give Argo a chance to sync
                await asyncio.sleep(15) # TODO should this be added to config as 'watch_timeout' or can we conver this logic to a proper watch?
                try:
                    resource_list = await api_client.list_namespaced_custom_object(
                        namespace=self.namespace,
                        watch=False,
                        label_selector=self.label_selector,
                        **ROLLOUT_CONST_ARGS,
                    )
                    # pprint(resource_list, stream=sys.stderr)
                    # if DEBUG:
                    #     pprint(resource_list)
                    #     print("Resource list status:")

                    tgt_status: RolloutStatus = RolloutStatus.parse_obj(resource_list['items'][0]['status'])
                    latest: RolloutStatusCondition = next(iter(sorted(tgt_status.conditions, key= lambda x: x.last_update_time)))
                    if not latest.type in ['Available','Progressing']:
                        reason = 'scheduling-failed' if 'exceeded quota' in latest.message else latest.type
                        raise RuntimeError(latest['message'], reason=reason)
                        # changing = False # code unreachable, slated for removal
                        # if DEBUG:
                        #     print("resource_list for selector:")
                        #     pprint(resource_list['items'][0]['status']['conditions'])
                    else:
                        if tgt_status.blue_green.active_selector == tgt_status.blue_green.preview_selector:
                            # if DEBUG:
                            #     print("Change completed")
                            break

                except kubernetes_asyncio.client.ApiException as e:
                    raise RuntimeError("Exception when calling CustomObjectsApi->list_namespaced_custom_object") from e

                if time.time() - start_time > 600: # TODO either make this configurable or convert to watch
                    raise RuntimeError('Timed out waiting for activeSelector to match previewSelector', status='rejected', reason='unstable')


    async def delete(self, options:kubernetes_asyncio.client.V1DeleteOptions = None) ->kubernetes_asyncio.client.V1Status:
        """Delete the Rollout.

        This method expects the Rollout to have been loaded or otherwise
        assigned a namespace already. If it has not, the namespace will need
        to be set manually.

        Args:
            options: Unsupported, options for Deployment deletion.

        Returns:
            The status of the delete operation.
        """
        if options is not None:
            raise RuntimeError("Rollout deletion does not support V1DeleteOptions")

        self.logger.info(f'deleting rollout "{self.name}"')
        self.logger.trace(f"rollout: {self.obj}")

        async with self.api_client() as api_client:
            return await api_client.delete_namespaced_custom_object(
                namespace=self.namespace,
                name=self.name,
                **ROLLOUT_CONST_ARGS,
            )

    async def refresh(self) -> None:
        """Refresh the underlying Kubernetes Rollout resource."""
        async with self.api_client() as api_client:
            self.obj = RolloutObj.parse_obj(await api_client.get_namespaced_custom_object_status(
                namespace=self.namespace,
                name=self.name,
            ))

    async def rollback(self) -> None:
        # TODO rollbacks are automated in Argo Rollouts, not sure if making this No Op will cause issues
        #   but I was unable to locate a means of triggering a rollout rollback manually
        raise TypeError(
            (
                "rollback is not supported under the optimization of rollouts because rollbacks are applied to "
                "Kubernetes Deployment objects whereas this is automated by argocd"
            )
        )

    async def get_status(self) ->RolloutStatus:
        """Get the status of the Rollout.

        Returns:
            The status of the Rollout.
        """
        self.logger.info(f'checking status of rollout "{self.name}"')
        # first, refresh the rollout state to ensure the latest status
        await self.refresh()

        # return the status from the rollout
        return self.obj.status

    @property
    def status(self) ->RolloutStatus:
        """Return the status of the Rollout.

        Returns:
            The status of the Rollout.
        """
        return self.obj.status

    @property
    def label_selector(self) -> str:
        """Return the label selector of the Rollout for listing.

        Returns:
            The label selector of the Rollout.
        """
        return ','.join(f'{key}={val}' for key, val in self.obj.metadata.labels.items())


class Millicore(int):
    """
    The Millicore class represents one one-thousandth of a vCPU or hyperthread in Kubernetes.
    """

    @classmethod
    def __get_validators__(cls) -> pydantic.CallableGenerator:
        yield cls.parse

    @classmethod
    def parse(cls, v: pydantic.StrIntFloat) -> "Millicore":
        """
        Parses a string, integer, or float input value into Millicore units.

        Returns:
            The input value in Millicore units.

        Raises:
            ValueError: Raised if the input cannot be parsed.
        """
        if isinstance(v, str):
            if v[-1] == "m":
                return cls(int(v[:-1]))
            else:
                return cls(int(float(v) * 1000))
        elif isinstance(v, (int, float)):
            return cls(int(v * 1000))
        else:
            raise ValueError("could not parse millicore value")

    def __str__(self) -> str:
        if self % 1000 == 0:
            return str(int(self) // 1000)
        else:
            return f"{int(self)}m"

    def __float__(self) -> float:
        return self / 1000.0

    def __eq__(self, other) -> bool:
        if isinstance(other, str):
            return str(self) == other
        elif isinstance(other, float):
            return float(self) == other
        return super().__eq__(other)

    def human_readable(self) -> str:
        return str(self)


class CPU(servo.CPU):
    """
    The CPU class models a Kubernetes CPU resource in Millicore units.
    """

    min: Millicore
    max: Millicore
    step: Millicore
    value: Optional[Millicore]
    requirements: ResourceRequirements = ResourceRequirements.compute

    def __opsani_repr__(self) -> dict:
        o_dict = super().__opsani_repr__()

        # normalize values into floats (see Millicore __float__)
        for field in ("min", "max", "step", "value"):
            value = getattr(self, field)
            o_dict["cpu"][field] = float(value) if value is not None else None
        return o_dict


# Gibibyte is the base unit of Kubernetes memory
GiB = 1024 * 1024 * 1024


class ShortByteSize(pydantic.ByteSize):
    """Kubernetes omits the 'B' suffix for some reason"""

    @classmethod
    def validate(cls, v: pydantic.StrIntFloat) -> "ShortByteSize":
        if isinstance(v, str):
            try:
                return super().validate(v)
            except:
                # Append the byte suffix and retry parsing
                return super().validate(v + "b")
        elif isinstance(v, float):
            v = v * GiB
        return super().validate(v)


class Memory(servo.Memory):
    """
    The Memory class models a Kubernetes Memory resource.
    """

    value: Optional[ShortByteSize]
    min: ShortByteSize
    max: ShortByteSize
    step: ShortByteSize
    requirements: ResourceRequirements = ResourceRequirements.compute

    def __opsani_repr__(self) -> dict:
        o_dict = super().__opsani_repr__()

        # normalize values into floating point Gibibyte units
        for field in ("min", "max", "step", "value"):
            value = getattr(self, field)
            o_dict["mem"][field] = float(value) / GiB if value is not None else None
        return o_dict


# TODO: The Adjustment needs to marshal value appropriately on ingress
def _qualify(value, unit):
    if unit == "mem":
        return f"{value}Gi"  # if value.isnumeric() else value
    elif unit == "cpu":
        return str(Millicore.parse(value))
    elif unit == "replicas":
        return int(float(value))
    return value


class BaseOptimization(abc.ABC, pydantic.BaseModel, servo.logging.Mixin):
    """
    BaseOptimization is the base class for concrete implementations of optimization strategies.
    """

    name: str
    timeout: Duration

    @abc.abstractclassmethod
    async def create(
        cls, config: "BaseKubernetesConfiguration", *args, **kwargs
    ) -> "BaseOptimization":
        """"""
        ...

    @abc.abstractmethod
    async def adjust(
        self, adjustment: Adjustment, control: Control = Control()
    ) -> Description:
        """
        Adjust a setting on the underlying Deployment/Pod or Container.
        """
        ...

    @abc.abstractmethod
    async def apply(self) -> None:
        """
        Apply the adjusted settings to the Kubernetes cluster.
        """
        ...

    async def handle_error(self, error: Exception, mode: "FailureMode") -> bool:
        """
        Handle an operational failure in accordance with the failure mode configured by the operator.

        Well executed error handling requires context and strategic thinking. The servo base library
        provides a rich set of primitives and patterns for approaching error handling but ultimately
        the experience is reliant on the connector developer who has knowledge of the essential context
        and understands the user needs and expectations.

        The error handling implementation provided in this method handles the general cases out of the
        box and relies on abstract methods (see below) to implement more advanced behaviors such as
        rollback and tear-down.

        Returns:
            A boolean value that indicates if the error was handled.

        Raises:
            NotImplementedError: Raised if there is no handler for a given failure mode. Subclasses
                must filter failure modes before calling the superclass implementation.
        """
        try:
            if mode == FailureMode.CRASH:
                raise RuntimeError(
                    "an unrecoverable failure occurred while interacting with Kubernetes"
                )

            elif mode == FailureMode.IGNORE:
                self.logger.warning(f"ignoring runtime error and continuing: {error}")
                self.logger.opt(exception=error).exception("ignoring Kubernetes error")
                return True

            elif mode == FailureMode.ROLLBACK:
                await self.rollback(error)
                return True

            elif mode == FailureMode.DESTROY:
                await self.destroy(error)
                return True

            else:
                raise NotImplementedError(
                    f"missing error handler for failure mode '{mode}'"
                )
        except Exception as new_error:
            self.logger.exception(f"handle_error failed with unrecoverable error: {new_error}")
            print_exc()  # TODO organize this more gracefully in logging
            raise error

    @abc.abstractmethod
    async def rollback(self, error: Optional[Exception] = None) -> None:
        """
        Asynchronously roll back the Optimization to a previous known
        good state.

        Args:
            error: An optional exception that contextualizes the cause of the rollback.
        """
        ...

    @abc.abstractmethod
    async def destroy(self, error: Optional[Exception] = None) -> None:
        """
        Asynchronously destroy the Optimization.

        Args:
            error: An optional exception that contextualizes the cause of the destruction.
        """
        ...

    @abc.abstractmethod
    def to_components(self) -> List[Component]:
        """
        Return a list of Component representations of the Optimization.

        Components are the canonical representation of optimizations in the Opsani API.
        """
        ...

    def __hash__(self):
        return hash(
            (
                self.name,
                id(self),
            )
        )

    class Config:
        arbitrary_types_allowed = True


class MainlineOptimization(BaseOptimization):
    """
    The MainlineOptimization class implements an optimization strategy based on directly reconfiguring a Kubernetes
    Controller and its associated containers.
    """

    controller_config: Union["DeploymentConfiguration", "RolloutConfiguration"]
    controller: Union[Deployment, Rollout]
    container_config: "ContainerConfiguration"
    container: Container

    @classmethod
    async def create(
        cls, config: "DeploymentConfiguration", **kwargs
    ) -> "MainlineOptimization":
        if isinstance(config, DeploymentConfiguration):
            controller = await Deployment.read(config.name, config.namespace)
        else:
            controller = await Rollout.read(config.name, config.namespace)

        replicas = config.replicas.copy()
        replicas.value = controller.replicas

        # FIXME: Currently only supporting one container
        for container_config in config.containers:
            container = controller.find_container(container_config.name)
            if not container:
                names = servo.utilities.strings.join_to_series(
                    list(map(lambda c: c.name, controller.containers))
                )
                raise ValueError(
                    f'no container named "{container_config.name}" exists in the Pod (found {names})'
                )

            name = container_config.alias or (
                f"{controller.name}/{container.name}" if container else controller.name
            )
            return cls(
                name=name,
                controller_config=config,
                controller=controller,
                container_config=container_config,
                container=container,
                **kwargs,
            )

    @property
    def cpu(self) -> CPU:
        """
        Return the current CPU setting for the optimization.
        """
        cpu = self.container_config.cpu.copy()
        cpu.value = self.container.get_resource_requirements("cpu", first=True)
        return cpu

    @property
    def memory(self) -> Memory:
        """
        Return the current Memory setting for the optimization.
        """
        memory = self.container_config.memory.copy()
        memory.value = self.container.get_resource_requirements("memory", first=True)
        return memory

    @property
    def replicas(self) -> servo.Replicas:
        """
        Return the current Replicas setting for the optimization.
        """
        replicas = self.controller_config.replicas.copy()
        replicas.value = self.controller.replicas
        return replicas

    async def rollback(self, error: Optional[Exception] = None) -> None:
        """
        Initiates an asynchronous rollback to a previous version of the Deployment.

        Args:
            error: An optional error that triggered the rollback.
        """
        self.logger.info(f"adjustment failed: rolling back controller... ({error})")
        await asyncio.wait_for(
            asyncio.gather(self.controller.rollback()),
            timeout=self.timeout.total_seconds(),
        )

    async def destroy(self, error: Optional[Exception] = None) -> None:
        """
        Initiates the asynchronous deletion of the Deployment under optimization.

        Args:
            error: An optional error that triggered the destruction.
        """
        self.logger.info(f"adjustment failed: destroying controller...")
        await asyncio.wait_for(
            asyncio.gather(self.controller.delete()),
            timeout=self.timeout.total_seconds(),
        )

    def to_components(self) -> List[Component]:
        return [
            Component(name=self.name, settings=[self.cpu, self.memory, self.replicas])
        ]

    def adjust(self, adjustment: Adjustment, control: Control = Control()) -> None:
        """
        Adjust the settings on the Deployment or a component Container.

        Adjustments do not take effect on the cluster until the `apply` method is invoked
        to enable aggregation of related adjustments and asynchronous application.
        """
        name = adjustment.setting_name
        value = _qualify(adjustment.value, name)
        self.logger.trace(f"adjusting {name} to {value}")
        if name in ("cpu", "mem"):
            resource_name = "memory" if name == "mem" else name
            requirements = getattr(self.container_config, resource_name).requirements
            self.container.set_resource_requirements(
                resource_name, value, requirements, clear_others=True
            )

        elif name == "replicas":
            self.controller.replicas = int(value)

        else:
            raise RuntimeError(
                f"failed adjustment of unsupported Kubernetes setting '{adjustment.setting_name}'"
            )

    async def apply(self) -> None:
        await self.controller.apply()

class CanaryOptimization(BaseOptimization):
    """CanaryOptimization objects manage the optimization of Containers within a Deployment using
    a canary Pod that is adjusted independently and compared against the performance and cost profile
    of its siblings.
    """

    target_controller: Union[Deployment, Rollout]
    target_controller_config: Union["DeploymentConfiguration", "RolloutConfiguration"]

    target_container: Container
    target_container_config: "ContainerConfiguration"

    # Canary will be created if it does not yet exist
    canary_pod: Pod
    canary_container: Container

    @classmethod
    async def create(
        cls, config: "DeploymentConfiguration", **kwargs
    ) -> "CanaryOptimization":
        if isinstance(config, DeploymentConfiguration):
            controller = await Deployment.read(config.name, cast(str, config.namespace))
        else:
            controller = await Rollout.read(config.name, cast(str, config.namespace))

        if not controller:
            raise ValueError(
                f'cannot create CanaryOptimization: target controller "{config.name}" does not exist in Namespace "{config.namespace}"'
            )

        # Ensure that we have a canary Pod
        canary_pod = await controller.ensure_canary_pod()

        # FIXME: Currently only supporting one container
        for container_config in config.containers:
            target_container = controller.find_container(container_config.name)
            canary_container = canary_pod.get_container(container_config.name)

            name = (
                config.strategy.alias
                if isinstance(config.strategy, CanaryOptimizationStrategyConfiguration)
                and config.strategy.alias
                else f"{controller.name}/{canary_container.name}-canary"
            )

            return cls(
                name=name,
                target_controller_config=config,
                target_controller=controller,
                target_container_config=container_config,
                target_container=target_container,
                canary_pod=canary_pod,
                canary_container=canary_container,
                **kwargs,
            )

        raise AssertionError(
            "controller configuration must have one or more containers"
        )

    def adjust(self, adjustment: Adjustment, control: Control = Control()) -> None:
        name = adjustment.setting_name
        value = _qualify(adjustment.value, name)

        if name in ("cpu", "mem"):
            resource_name = "memory" if name == "mem" else name
            requirements = getattr(
                self.target_container_config, resource_name
            ).requirements
            self.canary_container.set_resource_requirements(
                resource_name, value, requirements, clear_others=True
            )

        elif name == "replicas":
            if int(value) != 1:
                servo.logger.warning(
                    f'ignored attempt to set replicas to "{value}" on canary pod "{self.canary_pod.name}"'
                )

        else:
            raise RuntimeError(
                f"failed adjustment of unsupported Kubernetes setting '{name}'"
            )

    async def apply(self) -> None:
        dep_copy = copy.copy(self.target_controller)
        dep_copy.obj.spec.template.spec.containers[
            0
        ].resources = self.canary_container.resources
        await dep_copy.delete_canary_pod(raise_if_not_found=False)
        self.canary = await dep_copy.ensure_canary_pod()

    @property
    def cpu(self) -> CPU:
        """
        Return the current CPU setting for the optimization.
        """
        cpu = self.target_container_config.cpu.copy()
        cpu.value = self.canary_container.get_resource_requirements("cpu", first=True)
        return cpu

    @property
    def memory(self) -> Memory:
        """
        Return the current Memory setting for the optimization.
        """
        memory = self.target_container_config.memory.copy()
        memory.value = self.canary_container.get_resource_requirements(
            "memory", first=True
        )
        return memory

    @property
    def replicas(self) -> servo.Replicas:
        """
        Return the current Replicas setting for the optimization.
        """
        return servo.Replicas(
            min=0,
            max=1,
            value=1,
            pinned=True,
        )

    def to_components(self) -> List[Component]:
        """
        Return a Component representation of the canary and its reference target.

        Note that all settings on the target are implicitly pinned because only the canary
        is to be modified during optimization.
        """

        # target_name = (
        #     self.target_container_config.alias
        #     or f"{self.target_controller_config.name}/{self.target_container_config.name}"
        # )
        # # implicitly pin the target settings before we return them
        # target_cpu = self.target_container_config.cpu.copy(update={"pinned": True})
        # if value := self.target_container.get_resource_requirements("cpu", first=True):
        #     target_cpu.value = value

        # target_memory = self.target_container_config.memory.copy(
        #     update={"pinned": True}
        # )
        # if value := self.target_container.get_resource_requirements(
        #     "memory", first=True
        # ):
        #     target_memory.value = value

        # target_replicas = self.target_controller_config.replicas.copy(
        #     update={"pinned": True}
        # )
        # target_replicas.value = self.target_controller.replicas

        return [
            # Component(
            #     name=target_name,
            #     settings=[
            #         target_cpu,
            #         target_memory,
            #         target_replicas,
            #     ],
            # ),
            Component(
                name=self.name,
                settings=[
                    self.cpu,
                    self.memory,
                    self.replicas,
                ],
            ),
        ]

    async def rollback(self, error: Optional[Exception] = None) -> None:
        """
        Not supported. Raises a TypeError when called.

        Rollbacks are not supported by the canary optimization strategy
        because they are dependent on Kubernetes Deployments.
        """
        raise TypeError(
            (
                "rollback is not supported under the canary optimization strategy because rollbacks are applied to "
                "Kubernetes Deployment objects and canary optimization is performed against a standalone Pod."
            )
        )

    async def destroy(self, error: Optional[Exception] = None) -> None:
        self.logger.info(f'destroying canary Pod "{self.name}"')
        await self.canary_pod.delete()

        self.logger.debug(f'awaiting deletion of canary Pod "{self.name}"')
        await self.canary_pod.wait_until_deleted()

        self.logger.info(f'destroyed canary Pod "{self.name}"')

    async def handle_error(self, error: Exception, mode: "FailureMode") -> bool:
        try:
            if mode == FailureMode.ROLLBACK or mode == FailureMode.DESTROY:
                if mode == FailureMode.ROLLBACK:
                    self.logger.warning(
                        f"cannot rollback a canary Pod: falling back to destroy: {error}"
                    )
                    self.logger.opt(exception=error).exception("")

                await asyncio.wait_for(self.destroy(), timeout=self.timeout.total_seconds())

                # create a new canary against baseline
                self.logger.info(
                    "creating new canary against baseline following failed adjust"
                )
                self.canary = await self.target_controller.ensure_canary_pod()
                return True

            else:
                return await super().handle_error(error, mode)
        except Exception as new_error:
            self.logger.exception(f"handle_error failed with unrecoverable error: {new_error}")
            print_exc() # TODO organize this more gracefully in logging
            raise error

    class Config:
        arbitrary_types_allowed = True
        extra = pydantic.Extra.allow


class KubernetesOptimizations(pydantic.BaseModel, servo.logging.Mixin):
    """
    Models the state of resources under optimization in a Kubernetes cluster.
    """

    config: "KubernetesConfiguration"
    namespace: Namespace
    optimizations: List[BaseOptimization]
    runtime_id: str
    spec_id: str
    version_id: str

    @classmethod
    async def create(
        cls, config: "KubernetesConfiguration"
    ) -> "KubernetesOptimizations":
        """
        Read the state of all components under optimization from the cluster and return an object representation.
        """
        namespace = await Namespace.read(config.namespace)
        optimizations: List[BaseOptimization] = []
        images = {}
        runtime_ids = {}
        pod_tmpl_specs = {}

        for controller_config in config.deployments + config.rollouts:
            if controller_config.strategy == OptimizationStrategy.DEFAULT:
                if controller_config.replicas is None:
                    raise ValueError("replicas must be set on default optimization")
                optimization = await MainlineOptimization.create(
                    controller_config, timeout=config.timeout
                )
                controller = optimization.controller
                container = optimization.container
            elif controller_config.strategy == OptimizationStrategy.CANARY:
                optimization = await CanaryOptimization.create(
                    controller_config, timeout=config.timeout
                )
                controller = optimization.target_controller
                container = optimization.target_container
            else:
                raise ValueError(
                    f"unknown optimization strategy: {controller_config.strategy}"
                )

            optimizations.append(optimization)

            # compile artifacts for checksum calculation
            pods = await controller.get_pods()
            runtime_ids[optimization.name] = [pod.uid for pod in pods]
            pod_tmpl_specs[controller.name] = controller.obj.spec.template.spec
            images[container.name] = container.image

        # Compute checksums for change detection
        spec_id = servo.utilities.hashing.get_hash([pod_tmpl_specs[k] for k in sorted(pod_tmpl_specs.keys())])
        runtime_id = servo.utilities.hashing.get_hash(runtime_ids)
        version_id = servo.utilities.hashing.get_hash([images[k] for k in sorted(images.keys())])

        return KubernetesOptimizations(
            config=config,
            namespace=namespace,
            optimizations=optimizations,
            spec_id=spec_id,
            runtime_id=runtime_id,
            version_id=version_id,
        )

    def to_components(self) -> List[Component]:
        """
        Return a list of Component objects modeling the state of local optimization activities.

        Components are the canonical representation of systems under optimization. They
        are used for data exchange with the Opsani API
        """
        components = list(map(lambda opt: opt.to_components(), self.optimizations))
        return list(itertools.chain(*components))

    def to_description(self) -> Description:
        """
        Return a representation of the current state as a Description object.

        Description objects are used to report state to the Opsani API in order
        to synchronize with the Optimizer service.

        Returns:
            A Description of the current state.
        """
        return Description(components=self.to_components())

    def find_optimization(self, name: str) -> Optional[BaseOptimization]:
        """
        Find and return an optimization by name.
        """
        return next(filter(lambda a: a.name == name, self.optimizations), None)

    async def apply(self, adjustments: List[Adjustment]) -> None:
        """
        Apply a sequence of adjustments and wait for them to take effect on the cluster.
        """
        # Exit early if there is nothing to do
        if not adjustments:
            self.logger.debug("early exiting from adjust: no adjustments")
            return

        summary = f"[{', '.join(list(map(str, adjustments)))}]"
        self.logger.info(
            f"Applying {len(adjustments)} Kubernetes adjustments: {summary}"
        )

        # Adjust settings on the local data model
        for adjustment in adjustments:
            if adjustable := self.find_optimization(adjustment.component_name):
                self.logger.info(f"adjusting {adjustment.component_name}: {adjustment}")
                adjustable.adjust(adjustment)

            else:
                self.logger.warning(f'ignoring unrecognized adjustment "{adjustment}"')

        # Apply the changes to Kubernetes and wait for the results
        timeout = self.config.timeout
        if self.optimizations:
            self.logger.debug(
                f"waiting for adjustments to take effect on {len(self.optimizations)} optimizations"
            )
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(
                        *list(map(lambda a: a.apply(), self.optimizations)),
                        return_exceptions=True,
                    ),
                    timeout=timeout.total_seconds(),
                )

                for result in results:
                    if isinstance(result, Exception):
                        for optimization in self.optimizations:
                            if await optimization.handle_error(
                                result, self.config.on_failure
                            ):
                                # Stop error propogation once it has been handled
                                break

            except asyncio.exceptions.TimeoutError as error:
                self.logger.error(
                    f"timed out after {timeout} waiting for adjustments to apply"
                )
                for optimization in self.optimizations:
                    if await optimization.handle_error(error, self.config.on_failure):
                        # Stop error propogation once it has been handled
                        break
        else:
            self.logger.warning(f"failed to apply adjustments: no adjustables")

        # TODO: Run sanity checks to look for out of band changes

    class Config:
        arbitrary_types_allowed = True


DNSSubdomainName = pydantic.constr(
    strip_whitespace=True,
    min_length=1,
    max_length=253,
    regex="^[0-9a-zA-Z]([0-9a-zA-Z\\.-])*[0-9A-Za-z]$",
)
DNSSubdomainName.__doc__ = (
    """DNSSubdomainName models a Kubernetes DNS Subdomain Name used as the name for most resource types.

    Valid DNS Subdomain Names conform to [RFC 1123](https://tools.ietf.org/html/rfc1123) and must:
        * contain no more than 253 characters
        * contain only lowercase alphanumeric characters, '-' or '.'
        * start with an alphanumeric character
        * end with an alphanumeric character

    See https://kubernetes.io/docs/concepts/overview/working-with-objects/names/#dns-subdomain-names
    """
)



DNSLabelName = pydantic.constr(
    strip_whitespace=True,
    min_length=1,
    max_length=63,
    regex="^[0-9a-zA-Z]([0-9a-zA-Z-])*[0-9A-Za-z]$",
)
DNSLabelName.__doc__ = (
    """DNSLabelName models a Kubernetes DNS Label Name identified used to name some resource types.

    Valid DNS Label Names conform to [RFC 1123](https://tools.ietf.org/html/rfc1123) and must:
        * contain at most 63 characters
        * contain only lowercase alphanumeric characters or '-'
        * start with an alphanumeric character
        * end with an alphanumeric character

    See https://kubernetes.io/docs/concepts/overview/working-with-objects/names/#dns-label-names
    """
)


ContainerTagName = pydantic.constr(
    strip_whitespace=True,
    min_length=1,
    max_length=128,
    regex="^[0-9a-zA-Z]([0-9a-zA-Z_\\.\\-/:@])*$",
)  # NOTE: This regex is not a full validation
ContainerTagName.__doc__ = (
    """ContainerTagName models the name of a container referenced in a Kubernetes manifest.

    Valid container tags must:
        * be valid ASCII and may contain lowercase and uppercase letters, digits, underscores, periods and dashes.
        * not start with a period or a dash
        * may contain a maximum of 128 characters
    """
)


class EnvironmentConfiguration(BaseConfiguration):
    ...


class CommandConfiguration(BaseConfiguration):
    ...


class ContainerConfiguration(BaseConfiguration):
    """
    The ContainerConfiguration class models the configuration of an optimizable container within a Kubernetes Deployment.
    """

    name: ContainerTagName
    alias: Optional[ContainerTagName]
    command: Optional[str]  # TODO: create model...
    cpu: CPU
    memory: Memory
    env: Optional[List[str]]  # TODO: create model...


class OptimizationStrategy(str, enum.Enum):
    """
    OptimizationStrategy is an enumeration of the possible ways to perform optimization on a Kubernetes Deployment.
    """

    DEFAULT = "default"
    """The default strategy directly applies adjustments to the target Deployment and its containers.
    """

    CANARY = "canary"
    """The canary strategy creates a servo managed standalone canary Pod based on the target Deployment and makes
    adjustments to it instead of the Deployment itself.
    """


class BaseOptimizationStrategyConfiguration(pydantic.BaseModel):
    type: OptimizationStrategy = pydantic.Field(..., const=True)

    def __eq__(self, other) -> bool:
        if isinstance(other, OptimizationStrategy):
            return self.type == other
        return super().__eq__(other)

    class Config:
        extra = pydantic.Extra.forbid


class DefaultOptimizationStrategyConfiguration(BaseOptimizationStrategyConfiguration):
    type = pydantic.Field(OptimizationStrategy.DEFAULT, const=True)


class CanaryOptimizationStrategyConfiguration(BaseOptimizationStrategyConfiguration):
    type = pydantic.Field(OptimizationStrategy.CANARY, const=True)
    alias: Optional[ContainerTagName]


class FailureMode(str, enum.Enum):
    """
    The FailureMode enumeration defines how to handle a failed adjustment of a Kubernetes resource.
    """

    ROLLBACK = "rollback"
    DESTROY = "destroy"
    IGNORE = "ignore"
    CRASH = "crash"

    @classmethod
    def options(cls) -> List[str]:
        """
        Return a list of strings that identifies all failure mode configuration options.
        """
        return list(map(lambda mode: mode.value, cls.__members__.values()))


class PermissionSet(pydantic.BaseModel):
    """Permissions objects model Kubernetes permissions granted through RBAC."""

    group: str
    resources: List[str]
    verbs: List[str]


STANDARD_PERMISSIONS = [
    PermissionSet(
        group="apps",
        resources=["deployments", "replicasets"],
        verbs=["get", "list", "watch", "update", "patch"],
    ),
    PermissionSet(
        group="",
        resources=["namespaces"],
        verbs=["get", "list"],
    ),
    PermissionSet(
        group="",
        resources=["pods", "pods/logs", "pods/status"],
        verbs=["create", "delete", "get", "list", "watch"],
    ),
]


class BaseKubernetesConfiguration(BaseConfiguration):
    """
    BaseKubernetesConfiguration provides a set of configuration primitives for optimizable Kubernetes resources.

    Child classes of `BaseKubernetesConfiguration` such as the `DeploymentConfiguration` can benefit from
    the cascading configuration behavior implemented on the `KubernetesConfiguration` class.

    Common settings will be cascaded from the containing class for attributes if they have not been explicitly set
    and are equal to the default value. Settings that are mandatory in the superclass (such as timeout and namespace)
    but are available for override should be declared as optional on `BaseKubernetesConfiguration` and overridden and
    declared as mandatory in `BaseKubernetesConfiguration`'.
    """

    kubeconfig: Optional[pydantic.FilePath] = pydantic.Field(
        description="Path to the kubeconfig file. If `None`, use the default from the environment.",
    )
    context: Optional[str] = pydantic.Field(description="Name of the kubeconfig context to use.")
    namespace: Optional[DNSSubdomainName] = pydantic.Field(
        description="Kubernetes namespace where the target deployment/controllers are running.",
    )
    settlement: Optional[Duration] = pydantic.Field(
        description="Duration to observe the application after an adjust to ensure the deployment or controller is stable. May be overridden by optimizer supplied `control.adjust.settlement` value."
    )
    on_failure: FailureMode = pydantic.Field(
        FailureMode.ROLLBACK,
        description=f"How to handle a failed adjustment. Options are: {servo.utilities.strings.join_to_series(list(FailureMode.__members__.values()))}",
    )
    timeout: Optional[Duration] = pydantic.Field(
        description="Time interval to wait before considering Kubernetes operations to have failed."
    )


StrategyTypes = Union[
    OptimizationStrategy,
    DefaultOptimizationStrategyConfiguration,
    CanaryOptimizationStrategyConfiguration,
]


class DeploymentConfiguration(BaseKubernetesConfiguration):
    """
    The DeploymentConfiguration class models the configuration of an optimizable Kubernetes Deployment.
    """

    name: DNSSubdomainName
    containers: List[ContainerConfiguration]
    strategy: StrategyTypes = OptimizationStrategy.DEFAULT
    replicas: Optional[servo.Replicas]

class RolloutConfiguration(BaseKubernetesConfiguration): # TODO this may be able to just use KubernetesConfiguration instead
    """
    The RolloutConfiguration class models the configuration of an optimizable ArgoCD Rollout.
    """

    name: DNSSubdomainName
    containers: List[ContainerConfiguration]
    strategy: StrategyTypes = OptimizationStrategy.DEFAULT
    replicas: Optional[servo.Replicas]

class KubernetesConfiguration(BaseKubernetesConfiguration):
    namespace: DNSSubdomainName = DNSSubdomainName("default")
    timeout: Duration = "5m"
    permissions: List[PermissionSet] = pydantic.Field(
        STANDARD_PERMISSIONS,
        description="Permissions required by the connector to operate in Kubernetes.",
    )

    deployments: List[DeploymentConfiguration] = pydantic.Field(
        description="Deployments to be optimized.",
    )

    rollouts: List[RolloutConfiguration] = pydantic.Field(
        description="Argo rollouts to be optimized.",
        default=[],
    )

    @classmethod
    def generate(cls, **kwargs) -> "KubernetesConfiguration":
        return cls(
            namespace="default",
            description="Update the namespace, deployment, etc. to match your Kubernetes cluster",
            deployments=[
                DeploymentConfiguration(
                    name="app",
                    replicas=servo.Replicas(
                        min=1,
                        max=2,
                    ),
                    containers=[
                        ContainerConfiguration(
                            name="opsani/fiber-http:latest",
                            cpu=CPU(min="250m", max=4, step="125m"),
                            memory=Memory(min="256MiB", max="4GiB", step="128MiB"),
                        )
                    ],
                )
            ],
            **kwargs,
        )

    def __init__(self, *args, **kwargs) -> None: # noqa: D107
        super().__init__(*args, **kwargs)
        self.cascade_common_settings()

    def cascade_common_settings(self, *, overwrite: bool = False) -> None:
        """
        Apply common settings to child models that inherit from BaseKubernetesConfiguration.

        This method provides enables hierarchical overrides of common configuration values
        based on shared inheritance. Each attribute is introspected and if it inherits from
        `BaseKubernetesConfiguration`, any common attribute values are copied onto the child
        model, cascading them downward. Only attributes whose value is equal to the default
        and have not been explicitly set are updated.

        # FIXME: Cascaded settings should only be optional if they can be optional at the top level. Right now we are implying that namespace can be None as well.
        """
        for name, field in self.__fields__.items():
            if issubclass(field.type_, BaseKubernetesConfiguration):
                attribute = getattr(self, name)
                for obj in (
                    attribute if isinstance(attribute, Collection) else [attribute]
                ):
                    for (
                        field_name,
                        field,
                    ) in BaseKubernetesConfiguration.__fields__.items():
                        if field_name in BaseConfiguration.__fields__:
                            # don't cascade from the base class
                            continue

                        if field_name in obj.__fields_set__ and not overwrite:
                            self.logger.trace(
                                f"skipping config cascade for unset field '{field_name}'"
                            )
                            continue

                        current_value = getattr(obj, field_name)
                        if overwrite or current_value == field.default:
                            parent_value = getattr(self, field_name)
                            setattr(obj, field_name, parent_value)
                            self.logger.trace(
                                f"cascaded setting '{field_name}' from KubernetesConfiguration to child '{attribute}': value={parent_value}"
                            )

                        else:
                            self.logger.trace(
                                f"declining to cascade value to field '{field_name}': the default value is set and overwrite is false"
                            )

    async def load_kubeconfig(self) -> None:
        """
        Asynchronously load the Kubernetes configuration
        """
        config_file = pathlib.Path(self.kubeconfig or kubernetes_asyncio.config.kube_config.KUBE_CONFIG_DEFAULT_LOCATION).expanduser()
        if config_file.exists():
            await kubernetes_asyncio.config.load_kube_config(
                config_file=str(config_file),
                context=self.context,
            )
        elif os.getenv("KUBERNETES_SERVICE_HOST"):
            kubernetes_asyncio.config.load_incluster_config()
        else:
            raise RuntimeError(
                f"unable to configure Kubernetes client: no kubeconfig file nor in-cluser environment variables found"
            )


KubernetesOptimizations.update_forward_refs()
MainlineOptimization.update_forward_refs()
CanaryOptimization.update_forward_refs()


class KubernetesChecks(BaseChecks):
    """Checks for ensuring that the Kubernetes connector is ready to run."""

    config: KubernetesConfiguration

    @servo.require("Connectivity to Kubernetes")
    async def check_connectivity(self) -> None:
        async with kubernetes_asyncio.client.api_client.ApiClient() as api:
            v1 =kubernetes_asyncio.client.VersionApi(api)
            await v1.get_code()

    @servo.warn("Kubernetes version")
    async def check_version(self) -> None:
        async with kubernetes_asyncio.client.api_client.ApiClient() as api:
            v1 =kubernetes_asyncio.client.VersionApi(api)
            version = await v1.get_code()
            assert int(version.major) >= 1
            # EKS sets minor to "17+"
            assert int(int("".join(c for c in version.minor if c.isdigit()))) >= 16

    @servo.require("Required permissions")
    async def check_permissions(self) -> None:
        async with kubernetes_asyncio.client.api_client.ApiClient() as api:
            v1 =kubernetes_asyncio.client.AuthorizationV1Api(api)
            for permission in self.config.permissions:
                for resource in permission.resources:
                    for verb in permission.verbs:
                        attributes =kubernetes_asyncio.client.models.V1ResourceAttributes(
                            namespace=self.config.namespace,
                            group=permission.group,
                            resource=resource,
                            verb=verb,
                        )

                        # TODO: The below checks an alternative serviceaccount
                        # user = "system:serviceaccount:default:opsani-servo"
                        # spec =kubernetes_asyncio.client.models.V1SubjectAccessReviewSpec(resource_attributes=attributes)
                        # spec.user = user
                        # review =kubernetes_asyncio.client.models.V1SubjectAccessReview(spec=spec)
                        # access_review = await v1.create_subject_access_review(review)

                        spec =kubernetes_asyncio.client.models.V1SelfSubjectAccessReviewSpec(
                            resource_attributes=attributes
                        )
                        review =kubernetes_asyncio.client.models.V1SelfSubjectAccessReview(spec=spec)
                        access_review = await v1.create_self_subject_access_review(
                            body=review
                        )
                        assert (
                            access_review.status.allowed
                        ), f'Not allowed to "{verb}" resource "{resource}"'

    @servo.require('Namespace "{self.config.namespace}" is readable')
    async def check_namespace(self) -> None:
        await Namespace.read(self.config.namespace)

    @servo.multicheck('Deployment "{item.name}" is readable')
    async def check_deployments(self) -> Tuple[Iterable, CheckHandler]:
        async def check_dep(dep_config: DeploymentConfiguration) -> str:
            await Deployment.read(dep_config.name, dep_config.namespace)

        return self.config.deployments, check_dep

    @servo.multicheck('Containers in the "{item.name}" Deployment have resource requirements')
    async def check_resource_requirements(self) -> Tuple[Iterable, CheckHandler]:
        async def check_dep_resource_requirements(
            dep_config: DeploymentConfiguration,
        ) -> str:
            deployment = await Deployment.read(dep_config.name, dep_config.namespace)
            for container in deployment.containers:
                assert container.resources
                assert container.resources.requests
                assert container.resources.requests["cpu"]
                assert container.resources.requests["memory"]
                assert container.resources.limits
                assert container.resources.limits["cpu"]
                assert container.resources.limits["memory"]

        return self.config.deployments, check_dep_resource_requirements

    @servo.multicheck('Deployment "{item.name}" is ready')
    async def check_deployments_are_ready(self) -> Tuple[Iterable, CheckHandler]:
        async def check_deployment(dep_config: DeploymentConfiguration) -> None:
            deployment = await Deployment.read(dep_config.name, dep_config.namespace)
            if not deployment.is_ready:
                raise RuntimeError(f'Deployment "{deployment.name}" is not ready')

        return self.config.deployments, check_deployment


@servo.metadata(
    description="Kubernetes adjust connector",
    version="1.5.0",
    homepage="https://github.com/opsani/kubernetes-connector",
    license=License.APACHE2,
    maturity=Maturity.STABLE,
)
class KubernetesConnector(BaseConnector):
    config: KubernetesConfiguration

    @servo.on_event()
    async def startup(self) -> None:
        # Ensure we are ready to talk to Kubernetes API
        await self.config.load_kubeconfig()

    @servo.on_event()
    async def describe(self) -> Description:
        state = await KubernetesOptimizations.create(self.config)
        return state.to_description()

    @servo.on_event()
    async def components(self) -> List[Component]:
        state = await KubernetesOptimizations.create(self.config)
        return state.to_components()

    @servo.before_event(servo.Events.MEASURE)
    async def before_measure(self) -> None:
        # Build state before a measurement to ensure all necessary setup is done (e.g., canary is up)
        await KubernetesOptimizations.create(self.config)

    @servo.on_event()
    async def adjust(
        self, adjustments: List[Adjustment], control: Control = Control()
    ) -> Description:
        state = await KubernetesOptimizations.create(self.config)
        await state.apply(adjustments)

        settlement = control.settlement or self.config.settlement
        if settlement:
            self.logger.info(
                f"Settlement duration of {settlement} requested, waiting for pods to settle..."
            )
            progress = DurationProgress(settlement)
            progress_logger = lambda p: self.logger.info(
                p.annotate(f"waiting {settlement} for pods to settle...", False),
                progress=p.progress,
            )
            await progress.watch(progress_logger)
            self.logger.info(
                f"Settlement duration of {settlement} has elapsed, resuming optimization."
            )

        description = state.to_description()
        return description

    @servo.on_event()
    async def check(
        self,
        matching: Optional[servo.CheckFilter],
        halt_on: Optional[servo.ErrorSeverity] = ErrorSeverity.CRITICAL,
    ) -> List[Check]:
        return await KubernetesChecks.run(
            self.config, matching=matching, halt_on=halt_on
        )


def selector_string(selectors: Mapping[str, str]) -> str:
    """Create a selector string from the given dictionary of selectors.

    Args:
        selectors: The selectors to stringify.

    Returns:
        The selector string for the given dictionary.
    """
    return ",".join([f"{k}={v}" for k, v in selectors.items()])


def selector_kwargs(
    fields: Mapping[str, str] = None,
    labels: Mapping[str, str] = None,
) -> Dict[str, str]:
    """Create a dictionary of kwargs for Kubernetes object selectors.

    Args:
        fields: A mapping of fields used to restrict the returned collection of
            Objects to only those which match these field selectors. By default,
            no restricting is done.
        labels: A mapping of labels used to restrict the returned collection of
            Objects to only those which match these label selectors. By default,
            no restricting is done.

    Returns:
        A dictionary that can be used as kwargs for many Kubernetes API calls for
        label and field selectors.
    """
    kwargs = {}
    if fields is not None:
        kwargs["field_selector"] = selector_string(fields)
    if labels is not None:
        kwargs["label_selector"] = selector_string(labels)

    return kwargs


class Service(KubernetesModel):
    """Kubetest wrapper around a Kubernetes `Service`_ API Object.

    The actual ``kubernetes.client.V1Service`` instance that this
    wraps can be accessed via the ``obj`` instance member.

    This wrapper provides some convenient functionality around the
    API Object and provides some state management for the `Service`_.

    .. _Service:
        https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.18/#service-v1-core
    """

    obj_type =kubernetes_asyncio.client.V1Service

    api_clients = {
        "preferred":kubernetes_asyncio.client.CoreV1Api,
        "v1":kubernetes_asyncio.client.CoreV1Api,
    }

    async def create(self, namespace: str = None) -> None:
        """Create the underlying Kubernetes resource in the cluster
        under the given namespace.

        Args:
            namespace: The namespace to create the resource under.
                If no namespace is provided, it will use the instance's
                namespace member, which is set when the object is created
                via the kubetestkubernetes_asyncio.client.
        """
        if namespace is None:
            namespace = self.namespace

        servo.logger.info(f'creating service "{self.name}" in namespace "{self.namespace}"')
        servo.logger.debug(f"service: {self.obj}")

        self.obj = self.api_client.create_namespaced_service(
            namespace=namespace,
            body=self.obj,
        )

    async def patch(self) -> None:
        """Partially update the underlying Kubernetes resource in the cluster."""
        servo.logger.info(f'patching service "{self.name}"')
        servo.logger.trace(f"service: {self.obj}")
        async with self.api_client() as api_client:
            await api_client.patch_namespaced_service(
                name=self.name,
                namespace=self.namespace,
                body=self.obj,
            )

    async def delete(self, options:kubernetes_asyncio.client.V1DeleteOptions) ->kubernetes_asyncio.client.V1Status:
        if options is None:
            options =kubernetes_asyncio.client.V1DeleteOptions()

        servo.logger.info(f'deleting service "{self.name}"')
        servo.logger.debug(f"delete options: {options}")
        servo.logger.debug(f"service: {self.obj}")

        return self.api_client.delete_namespaced_service(
            name=self.name,
            namespace=self.namespace,
            body=options,
        )

    @classmethod
    async def read(cls, name: str, namespace: str) -> "Service":
        """Read the Service from the cluster under the given namespace.

        Args:
            name: The name of the Pod to read.
            namespace: The namespace to read the Pod from.
        """
        servo.logger.info(f'reading service "{name}" in namespace "{namespace}"')

        async with cls.preferred_client() as api_client:
            obj = await asyncio.wait_for(
                api_client.read_namespaced_service(name, namespace), 5.0
            )
            servo.logger.trace("service: ", obj)
            return cls(obj)

    async def refresh(self) -> None:
        """Refresh the underlying Kubernetes Service resource."""
        async with self.api_client() as api_client:
            self.obj = await api_client.read_namespaced_service(
                name=self.name,
                namespace=self.namespace,
            )

    async def is_ready(self) -> bool:
        """Check if the Service is in the ready state.

        The readiness state is not clearly available from the Service
        status, so to see whether or not the Service is ready this
        will check whether the endpoints of the Service are ready.

        This comes with the caveat that in order for a Service to
        have endpoints, there needs to be some backend hooked up to it.
        If there is no backend, the Service will never have endpoints,
        so this will never resolve to True.

        Returns:
            True if in the ready state; False otherwise.
        """
        await self.refresh()

        # check the status. if there is no status, the service is
        # definitely not ready.
        if self.obj.status is None:
            return False

        endpoints = await self.get_endpoints()

        # if the Service has no endpoints, its not ready.
        if len(endpoints) == 0:
            return False

        # get the service endpoints and check that they are all ready.
        for endpoint in endpoints:
            # if we have an endpoint, but there are no subsets, we
            # consider the endpoint to be not ready.
            if endpoint.subsets is None:
                return False

            for subset in endpoint.subsets:
                # if the endpoint has no addresses setup yet, its not ready
                if subset.addresses is None or len(subset.addresses) == 0:
                    return False

                # if there are still addresses that are not ready, the
                # service is not ready
                not_ready = subset.not_ready_addresses
                if not_ready is not None and len(not_ready) > 0:
                    return False

        # if we got here, then all endpoints are ready, so the service
        # must also be ready
        return True

    async def status(self) ->kubernetes_asyncio.client.V1ServiceStatus:
        """Get the status of the Service.

        Returns:
            The status of the Service.
        """
        servo.logger.info(f'checking status of service "{self.name}"')
        # first, refresh the service state to ensure the latest status
        await self.refresh()

        # return the status from the service
        return self.obj.status

    async def get_endpoints(self) -> List[kubernetes_asyncio.client.V1Endpoints]:
        """Get the endpoints for the Service.

        This can be useful for checking internal IP addresses used
        in containers, e.g. for container auto-discovery.

        Returns:
            A list of endpoints associated with the Service.
        """
        servo.logger.info(f'getting endpoints for service "{self.name}"')
        endpoints = await self.api_client.list_namespaced_endpoints(
            namespace=self.namespace,
        )

        svc_endpoints = []
        for endpoint in endpoints.items:
            # filter to include only the endpoints with the same
            # name as the service.
            if endpoint.metadata.name == self.name:
                svc_endpoints.append(endpoint)

        servo.logger.debug(f"endpoints: {svc_endpoints}")
        return svc_endpoints

    async def _proxy_http_request(self, method, path, **kwargs) -> tuple:
        """Template request to proxy of a Service.

        Args:
            method: The http request method e.g. 'GET', 'POST' etc.
            path: The URI path for the request.
            kwargs: Keyword arguments for the proxy_http_get function.

        Returns:
            The response data
        """
        path_params = {
            "name": f"{self.name}:{self.obj.spec.ports[0].port}",
            "namespace": self.namespace,
            "path": path,
        }
        return await kubernetes_asyncio.client.CoreV1Api().api_client.call_api(
            "/api/v1/namespaces/{namespace}/services/{name}/proxy/{path}",
            method,
            path_params=path_params,
            **kwargs,
        )

    async def proxy_http_get(self, path: str, **kwargs) -> tuple:
        """Issue a GET request to proxy of a Service.

        Args:
            path: The URI path for the request.
            kwargs: Keyword arguments for the proxy_http_get function.

        Returns:
            The response data
        """
        return await self._proxy_http_request("GET", path, **kwargs)

    async def proxy_http_post(self, path: str, **kwargs) -> tuple:
        """Issue a POST request to proxy of a Service.

        Args:
            path: The URI path for the request.
            kwargs: Keyword arguments for the proxy_http_post function.

        Returns:
            The response data
        """
        return await self._proxy_http_request("POST", path, **kwargs)


class ConfigMap(KubernetesModel):
    """Kubetest wrapper around a Kubernetes `ConfigMap`_ API Object.

    The actual ``kubernetes.client.V1ConfigMap`` instance that this
    wraps can be accessed via the ``obj`` instance member.

    This wrapper provides some convenient functionality around the
    API Object and provides some state management for the `ConfigMap`_.

    .. _ConfigMap:
        https://kubernetes.io/docs/reference/generated/kubernetes-api/v1.18/#configmap-v1-core
    """

    obj_type =kubernetes_asyncio.client.V1ConfigMap

    api_clients = {
        "preferred":kubernetes_asyncio.client.CoreV1Api,
        "v1":kubernetes_asyncio.client.CoreV1Api,
    }

    @classmethod
    async def read(cls, name: str, namespace: str) -> "ConfigMap":
        """Read a ConfigMap by name under the given namespace.

        Args:
            name: The name of the ConfigMap to read.
            namespace: The namespace to read the ConfigMap from.
        """

        async with cls.preferred_client() as api_client:
            obj = await api_client.read_namespaced_config_map(name, namespace)
            return ConfigMap(obj)

    async def create(self, namespace: str = None) -> None:
        """Create the ConfigMap under the given namespace.

        Args:
            namespace: The namespace to create the ConfigMap under.
                If the ConfigMap was loaded via the kubetest client, the
                namespace will already be set, so it is not needed here.
                Otherwise, the namespace will need to be provided.
        """
        if namespace is None:
            namespace = self.namespace

        servo.logger.info(f'creating configmap "{self.name}" in namespace "{self.namespace}"')
        servo.logger.debug(f"configmap: {self.obj}")

        self.obj = await self.api_client.create_namespaced_config_map(
            namespace=namespace,
            body=self.obj,
        )

    async def patch(self) -> None:
        """
        Patches a ConfigMap.
        """
        self.logger.info(f'patching ConfigMap "{self.name}"')
        self.logger.trace(f"ConfigMap: {self.obj}")
        async with self.api_client() as api_client:
            await api_client.patch_namespaced_config_map(
                name=self.name,
                namespace=self.namespace,
                body=self.obj,
            )

    async def delete(self, options:kubernetes_asyncio.client.V1DeleteOptions = None) ->kubernetes_asyncio.client.V1Status:
        """Delete the ConfigMap.

        This method expects the ConfigMap to have been loaded or otherwise
        assigned a namespace already. If it has not, the namespace will need
        to be set manually.

        Args:
             options: Options for ConfigMap deletion.

        Returns:
            The status of the delete operation.
        """
        if options is None:
            options =kubernetes_asyncio.client.V1DeleteOptions()

        servo.logger.info(f'deleting configmap "{self.name}"')
        servo.logger.debug(f"delete options: {options}")
        servo.logger.debug(f"configmap: {self.obj}")

        return await self.api_client.delete_namespaced_config_map(
            name=self.name,
            namespace=self.namespace,
            body=options,
        )

    async def refresh(self) -> None:
        """Refresh the underlying Kubernetes ConfigMap resource."""
        self.obj = await self.api_client.read_namespaced_config_map(
            name=self.name,
            namespace=self.namespace,
        )

    async def is_ready(self) -> bool:
        """Check if the ConfigMap is in the ready state.

        ConfigMaps do not have a "status" field to check, so we will
        measure their readiness status by whether or not they exist
        on the cluster.

        Returns:
            True if in the ready state; False otherwise.
        """
        try:
            await self.refresh()
        except:  # noqa
            return False
        else:
            return True
