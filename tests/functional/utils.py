# SPDX-FileCopyrightText: 2025 - Canonical Ltd
# SPDX-License-Identifier: Apache-2.0

import copy
import logging
import os
import subprocess
from pathlib import Path

import yaml


NAMESPACE = "magnum-test-cluster"
CLUSTER_NAME = "test-cluster"
APPCRED_NAME = "test-cluster-appcred"
KEYPAIR_NAME = "sunbeam"  # Name used in DEFAULT_VALUES
FLAVOR_NAME = "sunbeam"
CLOUD_ADMIN = "sunbeam-admin"
CLOUD_NAME = "openstack"

DEFAULT_VALUES = {
    "osDistro": "ubuntu",
    "cloudCredentialsSecretName": "test-cluster-gobepvoc4bvd-cloud-credentials",
    # "clouds": {
    #     "openstack": {
    #         "auth": {
    #             "username": "admin",
    #             "project_name": "admin",
    #             "auth_url": "http://172.16.1.204/openstack-keystone/v3",
    #             "user_domain_name": "admin_domain",
    #             "password": "dummy",
    #             "project_domain_name": "admin_domain",
    #         }
    #     }
    # },
    # Worker node group with autoscaling enabled (min 1, max 2): the
    # MachineDeployment gets the min/max size annotations and no replicas,
    # leaving node count control to cluster-autoscaler
    "nodeGroups": [
        {
            "machineCount": 1,
            "name": "default-worker",
            "machineFlavor": "m1.medium",
            "autoscale": True,
            "machineCountMin": 1,
            "machineCountMax": 2,
        }
    ],
    "cloudName": CLOUD_NAME,
    "machineSSHKeyName": "sunbeam",
    "kubernetesVersion": "1.32.6",
    "nodeGroupDefaults": {"healthCheck": {"enabled": True}},
    "controlPlane": {
        "machineCount": 1,
        "healthCheck": {"enabled": True},
        "machineFlavor": "m1.medium",
    },
    "apiServer": {"loadBalancerProvider": "ovn", "enableLoadBalancer": True},
    "machineImageId": "cc76a1e7-1e8b-470f-9840-781c014b2e30",
    "etcd": {},
    "clusterNetworking": {
        "internalNetwork": {
            'subnetFilter"': None,
            "nodeCidr": "10.0.0.0/24",
            "networkFilter": None,
        },
        "externalNetworkId": "7af20ab4-a12e-49ca-945c-fce56fe2557f",
    },
    "cluster-autoscaler": {
        # CI-only override: with the default 10m unneeded-time the
        # scale-down phase of the functional test would barely fit its
        # 15 minute timeout (it also competes with the 10 minute
        # post-scale-up scale-down cooldown).
        "extraArgs": {"scale-down-unneeded-time": "1m"},
    },
    "addons": {
        "openstack": {
            "csiCinder": {
                "defaultStorageClass": {
                    "allowedTopologies": [],
                    "availabilityZone": "nova",
                    "name": "default",
                    "volumeType": "__DEFAULT__",
                    "enabled": True,
                    "fstype": "ext4",
                    "reclaimPolicy": "Retain",
                    "allowVolumeExpansion": True,
                },
                "additionalStorageClasses": [],
            },
            "cloudConfig": {
                "LoadBalancer": {
                    "lb-method": "SOURCE_IP_PORT",
                    "lb-provider": "ovn",
                    "create-monitor": True,
                }
            },
            "k8sKeystoneAuth": {
                "enabled": True,
                "values": {
                    "openstackAuthUrl": "http://172.16.1.205/openstack-keystone/v3",
                    "projectId": "36b914c628a946708d3792239aeb51bc",
                },
            },
        },
        "ingress": {"enabled": False},
        "kubernetesDashboard": {"enabled": True},
        "monitoring": {"enabled": False},
    },
}

LOG = logging.getLogger(__name__)


def run_command(cmd: list, env: dict | None = None, capture_output=False):
    """Execute commands using subprocess."""
    LOG.debug(f"Running command {cmd}")
    if capture_output:
        output = subprocess.check_output(cmd, env=env, text=True)
        return output.strip()
    else:
        return subprocess.check_call(cmd, env=env, text=True)


def run_openstack_command(cloud_name: str, cmd: list, capture_output=False):
    """Run openstack command using clouds.yaml."""
    env = os.environ.copy()
    env["OS_CLOUD"] = cloud_name

    return run_command(cmd, env=env, capture_output=capture_output)


def create_namespace(name: str):
    cmd = ["sudo", "k8s", "kubectl", "create", "namespace", name]
    run_command(cmd)


def get_admin_credentials() -> dict:
    """Retrieve admin credentials from clouds.yaml."""
    cloud_file = Path(os.environ["HOME"]) / ".config" / "openstack" / "clouds.yaml"
    try:
        with open(cloud_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data["clouds"][CLOUD_ADMIN]
    except FileNotFoundError:
        print(f"Error: The file {cloud_file} was not found.")
    except yaml.YAMLError as exc:
        print(f"Error parsing YAML file: {exc}")


def create_application_credential_secret(namespace: str, suffix: str) -> str:
    """Generate application credential and create a k8s secret."""
    cmd = [
        "openstack",
        "application",
        "credential",
        "create",
        f"{APPCRED_NAME}-{suffix}",
        "--format",
        "yaml",
    ]
    app_cred_str = run_openstack_command(CLOUD_ADMIN, cmd, capture_output=True)
    app_cred = yaml.safe_load(app_cred_str)
    admin_cred = get_admin_credentials()

    clouds_dict = {
        "clouds": {
            CLOUD_NAME: {
                "identity_api_version": 3,
                "region_name": admin_cred.get("region_name", "RegionOne"),
                "interface": "public",
                "verify": False,
                "auth": {
                    "auth_url": admin_cred["auth"]["auth_url"],
                    "application_credential_id": app_cred["id"],
                    "application_credential_secret": app_cred["secret"],
                },
                "auth_type": "v3applicationcredential",
            },
        },
    }

    clouds_data_string = yaml.safe_dump(clouds_dict, indent=2, default_flow_style=False)

    secret_name = f"{CLUSTER_NAME}-{suffix}-cloud-credentials"
    cmd = [
        "sudo",
        "k8s",
        "kubectl",
        "create",
        "secret",
        "generic",
        secret_name,
        f"--from-literal=clouds.yaml={clouds_data_string}",
        "--namespace",
        namespace,
    ]
    run_command(cmd, capture_output=False)
    return secret_name


def get_openstack_image_id(image: str) -> str:
    """Get ID for a given Image."""
    # Assumption: Only one image with the name exists
    cmd = ["openstack", "image", "show", image, "-c", "id", "-f", "value"]
    return run_openstack_command(CLOUD_ADMIN, cmd, capture_output=True)


def get_openstack_network_id(network_name: str) -> str:
    """Get ID for a given network."""
    # Assumption: Only one network with the name exists
    cmd = ["openstack", "network", "show", network_name, "-c", "id", "-f", "value"]
    return run_openstack_command(CLOUD_ADMIN, cmd, capture_output=True)


def get_openstack_project_id(project: str, domain: str) -> str:
    """Get Project ID based on name and domain name."""
    cmd = [
        "openstack",
        "project",
        "show",
        project,
        "--domain",
        domain,
        "-c",
        "id",
        "-f",
        "value",
    ]
    return run_openstack_command(CLOUD_ADMIN, cmd, capture_output=True)


def create_keypair(config_path: Path, suffix: str) -> str:
    """Create Keypair in openstack and return keypair name."""
    keypair = f"{KEYPAIR_NAME}-{suffix}"
    cmd = [
        "openstack",
        "keypair",
        "create",
        "--private-key",
        f"{str(config_path)}/{keypair}.key",
        keypair,
    ]
    run_openstack_command(CLOUD_ADMIN, cmd, capture_output=False)
    return keypair


def create_flavor(ram: int, disk: int, vcpus: int, suffix: str) -> str:
    """Create flavor in openstack and return flavor name."""
    flavor = f"{FLAVOR_NAME}-{suffix}"
    cmd = [
        "openstack",
        "flavor",
        "create",
        "--ram",
        str(ram),
        "--disk",
        str(disk),
        "--vcpus",
        str(vcpus),
        flavor,
    ]
    run_openstack_command(CLOUD_ADMIN, cmd, capture_output=False)
    return flavor


def _get_project_and_domain_from_clouds_yaml() -> (str, str):
    admin_cred = get_admin_credentials()
    return admin_cred["auth"]["project_name"], admin_cred["auth"]["project_domain_name"]


def generate_values(config_path: Path, unique_id: str) -> dict:
    """Generate values yaml to create cluster."""
    values = copy.deepcopy(DEFAULT_VALUES)
    project, domain = _get_project_and_domain_from_clouds_yaml()

    keypair = create_keypair(config_path, unique_id)
    flavor = create_flavor(4096, 30, 2, unique_id)
    image_id = get_openstack_image_id("ubuntu")
    external_network_id = get_openstack_network_id("external-network")
    project_id = get_openstack_project_id(project, domain)

    namespace = f"{NAMESPACE}-{unique_id}"
    create_namespace(namespace)
    # clouds_dict = generate_application_credential_cloud_config(unique_id)
    appcred_secret = create_application_credential_secret(namespace, unique_id)
    admin_cred = get_admin_credentials()

    # values["clouds"] = clouds_dict["clouds"]
    values["cloudCredentialsSecretName"] = appcred_secret

    values["machineSSHKeyName"] = keypair

    for node in values["nodeGroups"]:
        node["machineFlavor"] = flavor
    values["controlPlane"]["machineFlavor"] = flavor

    values["machineImageId"] = image_id

    values["clusterNetworking"]["externalNetworkId"] = external_network_id

    values["addons"]["openstack"]["k8sKeystoneAuth"]["values"]["openstackAuthUrl"] = (
        admin_cred["auth"]["auth_url"]
    )
    values["addons"]["openstack"]["k8sKeystoneAuth"]["values"]["projectId"] = project_id

    LOG.debug(f"Helm chart Values generated: {values}")
    return values
