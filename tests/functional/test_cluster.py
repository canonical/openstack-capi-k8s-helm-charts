# SPDX-FileCopyrightText: 2025 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path

import yaml

from . import utils


CLUSTER_DEPLOY_TIMEOUT = 3600  # 60 minutes
WORKLOAD_NODE_READY_TIMEOUT = 300  # 5 mimutes, this is for worker node pods to settle
POD_WAIT_TIMEOUT = 600  # 10 minutes for pods to settle


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


def _check_workload_nodes_status(workload_kubeconfig: Path, expected_nodes: int):
    cmd = [
        "sudo",
        "k8s",
        "kubectl",
        "wait",
        "nodes",
        "--for=condition=Ready",
        "--all",
        "--timeout",
        f"{WORKLOAD_NODE_READY_TIMEOUT}s",
        "--kubeconfig",
        str(workload_kubeconfig),
    ]
    utils.run_command(cmd, capture_output=False)

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
    nodes_str = utils.run_command(cmd, capture_output=True)
    nodes = yaml.safe_load(nodes_str)

    # Check if number of nodes are as expected
    assert len(nodes.get("items", [])) == expected_nodes

    # Check if the nodes are in ready state
    for node in nodes.get("items", []):
        for condition in node.get("conditions", []):
            if condition.get("Type") == "Ready":
                assert condition.get("status") is True


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

    # Check kubernetes dashboard
    # kubernetes-dashboard helm chart is not available
    # failed to fetch https://kubernetes.github.io/dashboard/index.yaml
