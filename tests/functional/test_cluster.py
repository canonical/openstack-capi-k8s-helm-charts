# SPDX-FileCopyrightText: 2025 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

import time
from pathlib import Path

import yaml

from . import utils


CLUSTER_DEPLOY_TIMEOUT = 3600  # 60 minutes
WORKLOAD_NODE_READY_TIMEOUT = 300  # 5 mimutes, this is for worker node pods to settle
POD_WAIT_TIMEOUT = 600  # 10 minutes for pods to settle
SCALE_UP_TIMEOUT = 900  # 15 minutes for a new machine to be provisioned
SCALE_DOWN_TIMEOUT = 900  # 15 minutes for an unneeded node to be removed
AUTOSCALE_TEST_CPU_REQUEST = "1700m"  # ~one pod per 2 vCPU worker node

SCALE_UP_TEST_DEPLOYMENT = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: scale-up-test
  namespace: default
spec:
  replicas: 3
  selector:
    matchLabels:
      app: scale-up-test
  template:
    metadata:
      labels:
        app: scale-up-test
    spec:
      containers:
      - name: pause
        image: registry.k8s.io/pause:3.10
        resources:
          requests:
            cpu: {cpu}
            memory: 512Mi
"""


def _create_cluster(
    namespace: str,
    cluster_name: str,
    helm_repo_path: str,
    openstack_cluster_chart_version: str,
    values_file: Path,
):
    cmd = [
        "sudo",
        "k8s",
        "helm",
        "upgrade",
        cluster_name,
        "openstack-ck8s-cluster",
        "--history-max",
        "10",
        "--install",
        "--timeout",
        "5m",
        "--create-namespace",
        "--namespace",
        namespace,
        "--repo",
        helm_repo_path,
        "--version",
        openstack_cluster_chart_version,
        "--values",
        str(values_file),
    ]
    utils.run_command(cmd, capture_output=False)


def _wait_for_cluster(namespace: str, cluster_name: str, timeout: int):
    cmd = [
        "sudo",
        "k8s",
        "kubectl",
        "wait",
        "--namespace",
        namespace,
        # With cluster-api >= v1.10 the Cluster CRD is served as v1beta2 and
        # conditions live at .status.conditions (the .status.v1beta2 field
        # only exists on the v1beta1 API type).
        '--for=jsonpath={.status.conditions[?(@.type=="Available")].status}=True',
        f"cluster/{cluster_name}",
        f"--timeout={timeout}s",
    ]
    utils.run_command(cmd, capture_output=False)


def _get_management_cluster_kubeconfig(kubeconfig: Path):
    cmd = ["sudo", "k8s", "config"]
    kubeconfig_content = utils.run_command(cmd, capture_output=True)
    with open(kubeconfig, "w", encoding="utf-8") as f:
        f.write(kubeconfig_content)


def _get_workload_kubeconfig(
    namespace: str, cluster_name: str, management_config: Path, workload_config: Path
):
    cmd = [
        "clusterctl",
        "get",
        "kubeconfig",
        "--namespace",
        namespace,
        cluster_name,
        "--kubeconfig",
        str(management_config),
    ]
    kubeconfig_content = utils.run_command(cmd, capture_output=True)
    with open(workload_config, "w", encoding="utf-8") as f:
        f.write(kubeconfig_content)


def _check_workload_nodes_status(
    workload_kubeconfig: Path, expected_nodes: int, timeout: int = WORKLOAD_NODE_READY_TIMEOUT
):
    """Wait until the workload cluster has expected_nodes nodes, all Ready.

    Polls: `kubectl wait node --all --for=condition=Ready` only covers
    already-registered nodes, and a machine takes minutes to register
    after a MachineDeployment scale-up, so a one-shot count check races
    machine provisioning.
    """
    cmd = [
        "sudo",
        "k8s",
        "kubectl",
        "get",
        "nodes",
        "--output",
        "yaml",
        "--kubeconfig",
        str(workload_kubeconfig),
    ]
    deadline = time.monotonic() + timeout
    while True:
        nodes = yaml.safe_load(utils.run_command(cmd, capture_output=True))
        nodes = nodes.get("items", [])
        all_ready = all(
            any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in node.get("status", {}).get("conditions", [])
            )
            for node in nodes
        )
        if len(nodes) == expected_nodes and all_ready:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"expected {expected_nodes} Ready nodes, got {len(nodes)} "
                f"(all_ready={all_ready}) after {timeout}s"
            )
        time.sleep(10)


def _wait_for_machine_deployment_replicas(
    namespace: str, cluster_name: str, replicas: int, timeout: int
):
    """Wait for the worker MachineDeployment to have the given replica count.

    The worker node group is autoscaled in this test, so the autoscaler is
    in control of the replicas.
    """
    cmd = [
        "sudo",
        "k8s",
        "kubectl",
        "wait",
        "--namespace",
        namespace,
        f"--for=jsonpath={{.spec.replicas}}={replicas}",
        f"machinedeployment/{cluster_name}-default-worker",
        f"--timeout={timeout}s",
    ]
    utils.run_command(cmd, capture_output=False)


def _wait_for_scale_up_test_replicas(workload_kubeconfig: Path):
    """Wait for at least one scale-up-test replica to be available."""
    cmd = [
        "sudo",
        "k8s",
        "kubectl",
        "--kubeconfig",
        str(workload_kubeconfig),
        "--namespace",
        "default",
        "get",
        "deployment",
        "scale-up-test",
        "--output",
        "jsonpath={.status.availableReplicas}",
    ]
    deadline = time.monotonic() + POD_WAIT_TIMEOUT
    while True:
        available = utils.run_command(cmd, capture_output=True)
        if available and int(available) >= 1:
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"scale-up-test has {available or 0} available replicas "
                f"after {POD_WAIT_TIMEOUT}s"
            )
        time.sleep(10)


def _test_scale_up_and_down(
    namespace: str,
    cluster_name: str,
    workload_kubeconfig: Path,
    config_path: Path,
):
    """Verify cluster-autoscaler scales the worker node group.

    Deploys pods with a CPU request close to the worker flavor capacity,
    so only one fits per node: with 3 replicas and 2 nodes to start with,
    pods stay Pending until the autoscaler adds a node (up to the node
    group max). Deleting the deployment makes the extra node unneeded
    and the autoscaler removes it.

    Note: the control plane node is schedulable but its system pods
    (keystone-auth, metallb, cinder-csi, coredns, ...) request enough CPU
    that a 1700m pod does not fit on it in the CI topology.
    """
    deployment = SCALE_UP_TEST_DEPLOYMENT.format(cpu=AUTOSCALE_TEST_CPU_REQUEST)
    deployment_file = config_path / "scale-up-test.yaml"
    with open(str(deployment_file), "w") as file:
        file.write(deployment)
    cmd = [
        "sudo",
        "k8s",
        "kubectl",
        "--kubeconfig",
        str(workload_kubeconfig),
        "apply",
        "-f",
        str(deployment_file),
    ]
    utils.run_command(cmd, capture_output=False)

    # Expect the autoscaler to scale the worker machine deployment 1 -> 2
    _wait_for_machine_deployment_replicas(
        namespace, cluster_name, replicas=2, timeout=SCALE_UP_TIMEOUT
    )
    # The machine takes minutes to provision and register after the
    # MachineDeployment replicas are patched: wait for the node.
    _check_workload_nodes_status(
        workload_kubeconfig, 3, timeout=SCALE_UP_TIMEOUT
    )
    # Verify a previously unschedulable pod actually landed on the new
    # node. At most one replica can run in this topology (node group
    # max 2, control plane and first worker too loaded for a 1700m pod),
    # so check for >= 1 available replica, not for the full rollout.
    _wait_for_scale_up_test_replicas(workload_kubeconfig)

    # Scale down: remove the pods and expect the extra node to be removed
    cmd = [
        "sudo",
        "k8s",
        "kubectl",
        "--kubeconfig",
        str(workload_kubeconfig),
        "delete",
        "deployment",
        "scale-up-test",
        "--namespace",
        "default",
    ]
    utils.run_command(cmd, capture_output=False)

    _wait_for_machine_deployment_replicas(
        namespace, cluster_name, replicas=1, timeout=SCALE_DOWN_TIMEOUT
    )
    # The node is drained and the machine deleted after the replicas are
    # patched: wait for the node to go away.
    _check_workload_nodes_status(
        workload_kubeconfig, 2, timeout=SCALE_DOWN_TIMEOUT
    )


def _check_workload_deployments(workload_kubeconfig: Path, namespace: str):
    """Wait for all deployments in the namespace to be Available.

    Waiting on deployments instead of pods is resilient to pods being
    replaced while waiting (e.g. coredns autoscaling by its HPA right
    after the cluster becomes available): a pod in Terminating state
    would never satisfy a pods --all wait, while the deployment only
    reports Available when its live replicas are ready.
    """
    cmd = [
        "sudo",
        "k8s",
        "kubectl",
        "--namespace",
        namespace,
        "wait",
        "deployments",
        "--for",
        "condition=Available=True",
        "--all",
        "--timeout",
        f"{POD_WAIT_TIMEOUT}s",
        "--kubeconfig",
        str(workload_kubeconfig),
    ]
    utils.run_command(cmd, capture_output=False)


def _check_workload_daemonsets(workload_kubeconfig: Path, namespace: str):
    """Wait for all daemonsets in the namespace to have their pods ready.

    Daemonsets do not publish conditions (the AVAILABLE column in
    `kubectl get ds` is a count), so `kubectl wait --for=condition=...`
    cannot be used; `kubectl rollout status` is the supported check.
    """
    cmd = [
        "sudo",
        "k8s",
        "kubectl",
        "--namespace",
        namespace,
        "get",
        "daemonsets",
        "-o",
        "name",
        "--kubeconfig",
        str(workload_kubeconfig),
    ]
    daemonsets = utils.run_command(cmd, capture_output=True).splitlines()
    for daemonset in daemonsets:
        cmd = [
            "sudo",
            "k8s",
            "kubectl",
            "--namespace",
            namespace,
            "rollout",
            "status",
            daemonset,
            "--timeout",
            f"{POD_WAIT_TIMEOUT}s",
            "--kubeconfig",
            str(workload_kubeconfig),
        ]
        utils.run_command(cmd, capture_output=False)


def _dump_pods_status(workload_kubeconfig: Path, namespace: str):
    """Log pod statuses in the namespace to help debugging wait failures."""
    cmd = [
        "sudo",
        "k8s",
        "kubectl",
        "--namespace",
        namespace,
        "get",
        "pods",
        "-o",
        "wide",
        "--kubeconfig",
        str(workload_kubeconfig),
    ]
    output = utils.run_command(cmd, capture_output=True)
    print(f"\nPods in {namespace}:\n{output}", flush=True)


def test_create_cluster(
    setup,
    value_overrides,
    helm_repo_path,
    openstack_cluster_chart_version,
    config_path,
    unique_id,
):
    """Test create cluster.

    Create a workload cluster.
    Verify if the workload cluster pods are in running state.
    """
    values_file = config_path / "values.yaml"
    with open(str(values_file), "w") as file:
        yaml.dump(value_overrides, file)

    namespace = f"{utils.NAMESPACE}-{unique_id}"
    cluster_name = f"{utils.CLUSTER_NAME}-{unique_id}"

    _create_cluster(
        namespace,
        cluster_name,
        helm_repo_path,
        openstack_cluster_chart_version,
        values_file,
    )

    # Wait for cluster to be active
    _wait_for_cluster(namespace, cluster_name, timeout=CLUSTER_DEPLOY_TIMEOUT)

    # Get management cluster and workload cluster kubeconfig files
    management_kc_file = config_path / "mgmt_kubeconfig"
    _get_management_cluster_kubeconfig(management_kc_file)
    workload_kc_file = config_path / "workload_kubeconfig"
    _get_workload_kubeconfig(
        namespace, cluster_name, management_kc_file, workload_kc_file
    )

    # Expected 2 nodes - 1 master and 1 worker
    _check_workload_nodes_status(workload_kc_file, 2)

    # Verify cluster-autoscaler scales the worker node group
    _test_scale_up_and_down(namespace, cluster_name, workload_kc_file, config_path)

    # Check if k8s pods are running fine in kube-system namespace
    # This also verified k8s-keystone-auth pods.
    # Waits on deployments/daemonsets, not raw pods: coredns is scaled by
    # its HPA right after the cluster becomes available, and a pod being
    # replaced during a pods --all wait makes it fail spuriously.
    _dump_pods_status(workload_kc_file, namespace="kube-system")
    _check_workload_deployments(workload_kc_file, namespace="kube-system")
    _check_workload_daemonsets(workload_kc_file, namespace="kube-system")

    # Check openstack cinder and controller manager
    _dump_pods_status(workload_kc_file, namespace="openstack-system")
    _check_workload_deployments(workload_kc_file, namespace="openstack-system")
    _check_workload_daemonsets(workload_kc_file, namespace="openstack-system")

    # Check metallb: the chart deploys a LoadBalancer service for the
    # cluster API server, which depends on metallb being healthy
    _dump_pods_status(workload_kc_file, namespace="metallb-system")
    _check_workload_deployments(workload_kc_file, namespace="metallb-system")
    _check_workload_daemonsets(workload_kc_file, namespace="metallb-system")

    # Check kubernetes dashboard
    # kubernetes-dashboard helm chart is not available
    # failed to fetch https://kubernetes.github.io/dashboard/index.yaml
