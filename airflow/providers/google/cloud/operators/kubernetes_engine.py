#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""This module contains Google Kubernetes Engine operators."""
from __future__ import annotations

import warnings
from functools import cached_property
from typing import TYPE_CHECKING, Any, Sequence

from deprecated import deprecated
from google.api_core.exceptions import AlreadyExists
from google.cloud.container_v1.types import Cluster

from airflow.configuration import conf
from airflow.exceptions import AirflowException, AirflowProviderDeprecationWarning
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from airflow.providers.cncf.kubernetes.utils.pod_manager import OnFinishAction
from airflow.providers.google.cloud.hooks.kubernetes_engine import GKEHook, GKEPodHook
from airflow.providers.google.cloud.links.kubernetes_engine import (
    KubernetesEngineClusterLink,
    KubernetesEnginePodLink,
)
from airflow.providers.google.cloud.operators.cloud_base import GoogleCloudBaseOperator
from airflow.providers.google.cloud.triggers.kubernetes_engine import GKEOperationTrigger, GKEStartPodTrigger
from airflow.utils.timezone import utcnow

if TYPE_CHECKING:
    from kubernetes.client.models import V1Pod

    from airflow.utils.context import Context

KUBE_CONFIG_ENV_VAR = "KUBECONFIG"


class GKEDeleteClusterOperator(GoogleCloudBaseOperator):
    """
    Deletes the cluster, including the Kubernetes endpoint and all worker nodes.

    To delete a certain cluster, you must specify the ``project_id``, the ``name``
    of the cluster, the ``location`` that the cluster is in, and the ``task_id``.

    **Operator Creation**: ::

        operator = GKEClusterDeleteOperator(
                    task_id='cluster_delete',
                    project_id='my-project',
                    location='cluster-location'
                    name='cluster-name')

    .. seealso::
        For more detail about deleting clusters have a look at the reference:
        https://google-cloud-python.readthedocs.io/en/latest/container/gapic/v1/api.html#google.cloud.container_v1.ClusterManagerClient.delete_cluster

    .. seealso::
        For more information on how to use this operator, take a look at the guide:
        :ref:`howto/operator:GKEDeleteClusterOperator`

    :param project_id: The Google Developers Console [project ID or project number]
    :param name: The name of the resource to delete, in this case cluster name
    :param location: The name of the Google Kubernetes Engine zone or region in which the cluster
        resides.
    :param gcp_conn_id: The connection ID to use connecting to Google Cloud.
    :param api_version: The api version to use
    :param impersonation_chain: Optional service account to impersonate using short-term
        credentials, or chained list of accounts required to get the access_token
        of the last account in the list, which will be impersonated in the request.
        If set as a string, the account must grant the originating account
        the Service Account Token Creator IAM role.
        If set as a sequence, the identities from the list must grant
        Service Account Token Creator IAM role to the directly preceding identity, with first
        account from the list granting this role to the originating account (templated).
    :param deferrable: Run operator in the deferrable mode.
    :param poll_interval: Interval size which defines how often operation status is checked.
    """

    template_fields: Sequence[str] = (
        "project_id",
        "gcp_conn_id",
        "name",
        "location",
        "api_version",
        "impersonation_chain",
    )

    def __init__(
        self,
        *,
        name: str,
        location: str,
        project_id: str | None = None,
        gcp_conn_id: str = "google_cloud_default",
        api_version: str = "v2",
        impersonation_chain: str | Sequence[str] | None = None,
        deferrable: bool = conf.getboolean("operators", "default_deferrable", fallback=False),
        poll_interval: int = 10,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)

        self.project_id = project_id
        self.gcp_conn_id = gcp_conn_id
        self.location = location
        self.api_version = api_version
        self.name = name
        self.impersonation_chain = impersonation_chain
        self.deferrable = deferrable
        self.poll_interval = poll_interval
        self._check_input()

        self._hook: GKEHook | None = None

    def _check_input(self) -> None:
        if not all([self.project_id, self.name, self.location]):
            self.log.error("One of (project_id, name, location) is missing or incorrect")
            raise AirflowException("Operator has incorrect or missing input.")

    def execute(self, context: Context) -> str | None:
        hook = self._get_hook()

        wait_to_complete = not self.deferrable
        operation = hook.delete_cluster(
            name=self.name,
            project_id=self.project_id,
            wait_to_complete=wait_to_complete,
        )

        if self.deferrable and operation is not None:
            self.defer(
                trigger=GKEOperationTrigger(
                    operation_name=operation.name,
                    project_id=self.project_id,
                    location=self.location,
                    gcp_conn_id=self.gcp_conn_id,
                    impersonation_chain=self.impersonation_chain,
                    poll_interval=self.poll_interval,
                ),
                method_name="execute_complete",
            )

        return operation.self_link if operation is not None else None

    def execute_complete(self, context: Context, event: dict) -> str:
        """Execute after trigger job is done."""
        status = event["status"]
        message = event["message"]

        if status in ("failed", "error"):
            self.log.exception("Trigger ended with one of the failed statuses.")
            raise AirflowException(message)

        self.log.info(message)
        operation = self._get_hook().get_operation(
            operation_name=event["operation_name"],
        )
        return operation.self_link

    def _get_hook(self) -> GKEHook:
        if self._hook is None:
            self._hook = GKEHook(
                gcp_conn_id=self.gcp_conn_id,
                location=self.location,
                impersonation_chain=self.impersonation_chain,
            )

        return self._hook


class GKECreateClusterOperator(GoogleCloudBaseOperator):
    """
    Create a Google Kubernetes Engine Cluster of specified dimensions and wait until the cluster is created.

    The **minimum** required to define a cluster to create is:

    ``dict()`` ::
        cluster_def = {"name": "my-cluster-name", "initial_node_count": 1}

    or

    ``Cluster`` proto ::
        from google.cloud.container_v1.types import Cluster

        cluster_def = Cluster(name="my-cluster-name", initial_node_count=1)

    **Operator Creation**: ::

        operator = GKEClusterCreateOperator(
                    task_id='cluster_create',
                    project_id='my-project',
                    location='my-location'
                    body=cluster_def)

    .. seealso::
        For more detail on about creating clusters have a look at the reference:
        :class:`google.cloud.container_v1.types.Cluster`

    .. seealso::
        For more information on how to use this operator, take a look at the guide:
        :ref:`howto/operator:GKECreateClusterOperator`

    :param project_id: The Google Developers Console [project ID or project number]
    :param location: The name of the Google Kubernetes Engine zone or region in which the cluster
        resides.
    :param body: The Cluster definition to create, can be protobuf or python dict, if
        dict it must match protobuf message Cluster
    :param gcp_conn_id: The connection ID to use connecting to Google Cloud.
    :param api_version: The api version to use
    :param impersonation_chain: Optional service account to impersonate using short-term
        credentials, or chained list of accounts required to get the access_token
        of the last account in the list, which will be impersonated in the request.
        If set as a string, the account must grant the originating account
        the Service Account Token Creator IAM role.
        If set as a sequence, the identities from the list must grant
        Service Account Token Creator IAM role to the directly preceding identity, with first
        account from the list granting this role to the originating account (templated).
    :param deferrable: Run operator in the deferrable mode.
    :param poll_interval: Interval size which defines how often operation status is checked.
    """

    template_fields: Sequence[str] = (
        "project_id",
        "gcp_conn_id",
        "location",
        "api_version",
        "body",
        "impersonation_chain",
    )
    operator_extra_links = (KubernetesEngineClusterLink(),)

    def __init__(
        self,
        *,
        location: str,
        body: dict | Cluster,
        project_id: str | None = None,
        gcp_conn_id: str = "google_cloud_default",
        api_version: str = "v2",
        impersonation_chain: str | Sequence[str] | None = None,
        poll_interval: int = 10,
        deferrable: bool = conf.getboolean("operators", "default_deferrable", fallback=False),
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)

        self.project_id = project_id
        self.gcp_conn_id = gcp_conn_id
        self.location = location
        self.api_version = api_version
        self.body = body
        self.impersonation_chain = impersonation_chain
        self.poll_interval = poll_interval
        self.deferrable = deferrable
        self._validate_input()

        self._hook: GKEHook | None = None

    def _validate_input(self) -> None:
        """Primary validation of the input body."""
        self._alert_deprecated_body_fields()

        error_messages: list[str] = []
        if not self._body_field("name"):
            error_messages.append("Field body['name'] is missing or incorrect")

        if self._body_field("initial_node_count"):
            if self._body_field("node_pools"):
                error_messages.append(
                    "Do not use filed body['initial_node_count'] and body['node_pools'] at the same time."
                )

        if self._body_field("node_config"):
            if self._body_field("node_pools"):
                error_messages.append(
                    "Do not use filed body['node_config'] and body['node_pools'] at the same time."
                )

        if self._body_field("node_pools"):
            if any([self._body_field("node_config"), self._body_field("initial_node_count")]):
                error_messages.append(
                    "The field body['node_pools'] should not be set if "
                    "body['node_config'] or body['initial_code_count'] are specified."
                )

        if not any([self._body_field("node_config"), self._body_field("initial_node_count")]):
            if not self._body_field("node_pools"):
                error_messages.append(
                    "Field body['node_pools'] is required if none of fields "
                    "body['initial_node_count'] or body['node_pools'] are specified."
                )

        for message in error_messages:
            self.log.error(message)

        if error_messages:
            raise AirflowException("Operator has incorrect or missing input.")

    def _body_field(self, field_name: str, default_value: Any = None) -> Any:
        """Extract the value of the given field name."""
        if isinstance(self.body, dict):
            return self.body.get(field_name, default_value)
        else:
            return getattr(self.body, field_name, default_value)

    def _alert_deprecated_body_fields(self) -> None:
        """Generate warning messages if deprecated fields were used in the body."""
        deprecated_body_fields_with_replacement = [
            ("initial_node_count", "node_pool.initial_node_count"),
            ("node_config", "node_pool.config"),
            ("zone", "location"),
            ("instance_group_urls", "node_pools.instance_group_urls"),
        ]
        for deprecated_field, replacement in deprecated_body_fields_with_replacement:
            if self._body_field(deprecated_field):
                warnings.warn(
                    f"The body field '{deprecated_field}' is deprecated. Use '{replacement}' instead.",
                    AirflowProviderDeprecationWarning,
                    stacklevel=2,
                )

    def execute(self, context: Context) -> str:
        hook = self._get_hook()
        try:
            wait_to_complete = not self.deferrable
            operation = hook.create_cluster(
                cluster=self.body,
                project_id=self.project_id,
                wait_to_complete=wait_to_complete,
            )

            KubernetesEngineClusterLink.persist(context=context, task_instance=self, cluster=self.body)

            if self.deferrable:
                self.defer(
                    trigger=GKEOperationTrigger(
                        operation_name=operation.name,
                        project_id=self.project_id,
                        location=self.location,
                        gcp_conn_id=self.gcp_conn_id,
                        impersonation_chain=self.impersonation_chain,
                        poll_interval=self.poll_interval,
                    ),
                    method_name="execute_complete",
                )

            return operation.target_link

        except AlreadyExists as error:
            self.log.info("Assuming Success: %s", error.message)
            name = self.body.name if isinstance(self.body, Cluster) else self.body["name"]
            return hook.get_cluster(name=name, project_id=self.project_id).self_link

    def execute_complete(self, context: Context, event: dict) -> str:
        status = event["status"]
        message = event["message"]

        if status in ("failed", "error"):
            self.log.exception("Trigger ended with one of the failed statuses.")
            raise AirflowException(message)

        self.log.info(message)
        operation = self._get_hook().get_operation(
            operation_name=event["operation_name"],
        )
        return operation.target_link

    def _get_hook(self) -> GKEHook:
        if self._hook is None:
            self._hook = GKEHook(
                gcp_conn_id=self.gcp_conn_id,
                location=self.location,
                impersonation_chain=self.impersonation_chain,
            )

        return self._hook


class GKEStartPodOperator(KubernetesPodOperator):
    """
    Executes a task in a Kubernetes pod in the specified Google Kubernetes Engine cluster.

    This Operator assumes that the system has gcloud installed and has configured a
    connection id with a service account.

    The **minimum** required to define a cluster to create are the variables
    ``task_id``, ``project_id``, ``location``, ``cluster_name``, ``name``,
    ``namespace``, and ``image``

    .. seealso::
        For more detail about Kubernetes Engine authentication have a look at the reference:
        https://cloud.google.com/kubernetes-engine/docs/how-to/cluster-access-for-kubectl#internal_ip

    .. seealso::
        For more information on how to use this operator, take a look at the guide:
        :ref:`howto/operator:GKEStartPodOperator`

    :param location: The name of the Google Kubernetes Engine zone or region in which the
        cluster resides, e.g. 'us-central1-a'
    :param cluster_name: The name of the Google Kubernetes Engine cluster the pod
        should be spawned in
    :param use_internal_ip: Use the internal IP address as the endpoint.
    :param project_id: The Google Developers Console project id
    :param gcp_conn_id: The Google cloud connection id to use. This allows for
        users to specify a service account.
    :param impersonation_chain: Optional service account to impersonate using short-term
        credentials, or list of accounts required to get the access_token
        of the last account in the list, which will be impersonated in the request.
        If set as a string, the account must grant the originating account
        the Service Account Token Creator IAM role.
        If set as a sequence, the identities from the list must grant
        Service Account Token Creator IAM role to the directly preceding identity, with first
        account from the list granting this role to the originating account (templated).
    :param regional: The location param is region name.
    :param deferrable: Run operator in the deferrable mode.
    :param on_finish_action: What to do when the pod reaches its final state, or the execution is interrupted.
        If "delete_pod", the pod will be deleted regardless its state; if "delete_succeeded_pod",
        only succeeded pod will be deleted. You can set to "keep_pod" to keep the pod.
        Current default is `keep_pod`, but this will be changed in the next major release of this provider.
    :param is_delete_operator_pod: What to do when the pod reaches its final
        state, or the execution is interrupted. If True, delete the
        pod; if False, leave the pod. Current default is False, but this will be
        changed in the next major release of this provider.
        Deprecated - use `on_finish_action` instead.
    """

    template_fields: Sequence[str] = tuple(
        {"project_id", "location", "cluster_name"} | set(KubernetesPodOperator.template_fields)
    )
    operator_extra_links = (KubernetesEnginePodLink(),)

    def __init__(
        self,
        *,
        location: str,
        cluster_name: str,
        use_internal_ip: bool = False,
        project_id: str | None = None,
        gcp_conn_id: str = "google_cloud_default",
        impersonation_chain: str | Sequence[str] | None = None,
        regional: bool | None = None,
        on_finish_action: str | None = None,
        is_delete_operator_pod: bool | None = None,
        **kwargs,
    ) -> None:
        if is_delete_operator_pod is not None:
            warnings.warn(
                "`is_delete_operator_pod` parameter is deprecated, please use `on_finish_action`",
                AirflowProviderDeprecationWarning,
                stacklevel=2,
            )
            kwargs["on_finish_action"] = (
                OnFinishAction.DELETE_POD if is_delete_operator_pod else OnFinishAction.KEEP_POD
            )
        else:
            if on_finish_action is not None:
                kwargs["on_finish_action"] = OnFinishAction(on_finish_action)
            else:
                warnings.warn(
                    f"You have not set parameter `on_finish_action` in class {self.__class__.__name__}. "
                    "Currently the default for this parameter is `keep_pod` but in a future release"
                    " the default will be changed to `delete_pod`. To ensure pods are not deleted in"
                    " the future you will need to set `on_finish_action=keep_pod` explicitly.",
                    AirflowProviderDeprecationWarning,
                    stacklevel=2,
                )
                kwargs["on_finish_action"] = OnFinishAction.KEEP_POD

        if regional is not None:
            warnings.warn(
                f"You have set parameter regional in class {self.__class__.__name__}. "
                "In current implementation of the operator the parameter is not used and will "
                "be deleted in future.",
                AirflowProviderDeprecationWarning,
                stacklevel=2,
            )

        super().__init__(**kwargs)
        self.project_id = project_id
        self.location = location
        self.cluster_name = cluster_name
        self.gcp_conn_id = gcp_conn_id
        self.impersonation_chain = impersonation_chain
        self.use_internal_ip = use_internal_ip

        self.pod: V1Pod | None = None
        self._ssl_ca_cert: str | None = None
        self._cluster_url: str | None = None

        if self.gcp_conn_id is None:
            raise AirflowException(
                "The gcp_conn_id parameter has become required. If you want to use Application Default "
                "Credentials (ADC) strategy for authorization, create an empty connection "
                "called `google_cloud_default`.",
            )
        # There is no need to manage the kube_config file, as it will be generated automatically.
        # All Kubernetes parameters (except config_file) are also valid for the GKEStartPodOperator.
        if self.config_file:
            raise AirflowException("config_file is not an allowed parameter for the GKEStartPodOperator.")

    @staticmethod
    @deprecated(
        reason="Please use `fetch_cluster_info` instead to get the cluster info for connecting to it.",
        category=AirflowProviderDeprecationWarning,
    )
    def get_gke_config_file():
        pass

    @cached_property
    def cluster_hook(self) -> GKEHook:
        return GKEHook(
            gcp_conn_id=self.gcp_conn_id,
            location=self.location,
            impersonation_chain=self.impersonation_chain,
        )

    @cached_property
    def hook(self) -> GKEPodHook:
        if self._cluster_url is None or self._ssl_ca_cert is None:
            raise AttributeError(
                "Cluster url and ssl_ca_cert should be defined before using self.hook method. "
                "Try to use self.get_kube_creds method",
            )

        hook = GKEPodHook(
            gcp_conn_id=self.gcp_conn_id,
            cluster_url=self._cluster_url,
            ssl_ca_cert=self._ssl_ca_cert,
            impersonation_chain=self.impersonation_chain,
        )
        return hook

    def execute(self, context: Context):
        """Execute process of creating pod and executing provided command inside it."""
        self.fetch_cluster_info()
        return super().execute(context)

    def fetch_cluster_info(self) -> tuple[str, str | None]:
        """Fetch cluster info for connecting to it."""
        cluster = self.cluster_hook.get_cluster(
            name=self.cluster_name,
            project_id=self.project_id,
        )

        if not self.use_internal_ip:
            self._cluster_url = f"https://{cluster.endpoint}"
        else:
            self._cluster_url = f"https://{cluster.private_cluster_config.private_endpoint}"
        self._ssl_ca_cert = cluster.master_auth.cluster_ca_certificate
        return self._cluster_url, self._ssl_ca_cert

    def invoke_defer_method(self):
        """Redefine triggers which are being used in child classes."""
        trigger_start_time = utcnow()
        self.defer(
            trigger=GKEStartPodTrigger(
                pod_name=self.pod.metadata.name,
                pod_namespace=self.pod.metadata.namespace,
                trigger_start_time=trigger_start_time,
                cluster_url=self._cluster_url,
                ssl_ca_cert=self._ssl_ca_cert,
                get_logs=self.get_logs,
                startup_timeout=self.startup_timeout_seconds,
                cluster_context=self.cluster_context,
                poll_interval=self.poll_interval,
                in_cluster=self.in_cluster,
                base_container_name=self.base_container_name,
                on_finish_action=self.on_finish_action,
                gcp_conn_id=self.gcp_conn_id,
                impersonation_chain=self.impersonation_chain,
            ),
            method_name="execute_complete",
            kwargs={"cluster_url": self._cluster_url, "ssl_ca_cert": self._ssl_ca_cert},
        )

    def execute_complete(self, context: Context, event: dict, **kwargs):
        # It is required for hook to be initialized
        self._cluster_url = kwargs["cluster_url"]
        self._ssl_ca_cert = kwargs["ssl_ca_cert"]

        return super().execute_complete(context, event, **kwargs)
