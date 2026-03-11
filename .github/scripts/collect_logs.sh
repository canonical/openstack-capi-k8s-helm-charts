#!/bin/bash

mkdir -p logs

kubectl_cmd="sudo k8s kubectl"

# Define the list of namespaces to iterate over
# The script will collect logs for all namespaces except the ones in the ignored list.
ignored_namespaces=(
    "cilium-secrets"
    "controller-sunbeam-controller"
    "default"
    "kube-node-lease"
    "kube-public"
    "kube-system"
    "metallb-system"
    "openstack"
)

# Define the CAPI resource types to collect
capi_resource_types=(
    "cluster.cluster.x-k8s.io"
    "ck8scontrolplane.controlplane.cluster.x-k8s.io"
    "machinedeployment.cluster.x-k8s.io"
    "machine.cluster.x-k8s.io"
    "helmchartproxy.addons.cluster.x-k8s.io"
)


# Function to get workload cluster kubeconfigs
get_workload_kubeconfigs() {
    echo "--- Getting Workload Cluster Kubeconfigs ---"

    # Get all CAPI clusters and iterate
    clusters=$($kubectl_cmd get cluster.cluster.x-k8s.io --all-namespaces -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{.metadata.name}{"\n"}{end}')

    if [ -z "$clusters" ]; then
        echo "No CAPI clusters found."
        return
    fi

    echo "$clusters" | while read -r namespace cluster_name; do
        kubeconfig_secret="${cluster_name}-kubeconfig"
        kubeconfig_file="logs/${cluster_name}-kubeconfig"

        # Check if the kubeconfig secret exists
        if $kubectl_cmd -n "$namespace" get secret "$kubeconfig_secret" &> /dev/null; then
            echo "Fetching kubeconfig for cluster '$cluster_name' in namespace '$namespace'..."
            $kubectl_cmd -n "$namespace" get secret "$kubeconfig_secret" -o jsonpath='{.data.value}' | base64 --decode > "$kubeconfig_file"
            if [ $? -eq 0 ]; then
                echo "Successfully saved kubeconfig to $kubeconfig_file"
            else
                echo "Error: Failed to decode or save kubeconfig for cluster '$cluster_name'"
            fi
        else
            echo "Warning: Kubeconfig secret '$kubeconfig_secret' not found for cluster '$cluster_name'."
        fi
    done
}

# Function to get pod YAML and logs from workload clusters
get_workload_pods_info() {
    echo "--- Getting Workload Pod YAML and Logs ---"
    for kubeconfig in logs/*-kubeconfig; do
        if [ -f "$kubeconfig" ]; then
            cluster_name=$(basename "$kubeconfig" | sed 's/-kubeconfig//')
            echo "Processing workload cluster '$cluster_name' via kubeconfig: $kubeconfig"

            # Get and save all pod YAMLs
            $kubectl_cmd --kubeconfig="$kubeconfig" get pods --all-namespaces -o yaml > "logs/${cluster_name}-workload-pods.yaml" 2>/dev/null || \
                echo "Warning: Could not get pod YAML for cluster '$cluster_name'"

	    # Get and save all node YAMLs
            $kubectl_cmd --kubeconfig="$kubeconfig" get nodes -o yaml > "logs/${cluster_name}-workload-nodes.yaml" 2>/dev/null || \
                echo "Warning: Could not get node YAML for cluster '$cluster_name'"

            # Get and save events (helps debugging pod waits vs scaling churn)
            $kubectl_cmd --kubeconfig="$kubeconfig" get events --all-namespaces --sort-by=.lastTimestamp > "logs/${cluster_name}-workload-events.log" 2>/dev/null || \
                echo "Warning: Could not get events for cluster '$cluster_name'"

            # Get and save pod statuses in a compact form
            $kubectl_cmd --kubeconfig="$kubeconfig" get pods --all-namespaces -o wide > "logs/${cluster_name}-workload-pods-status.log" 2>/dev/null || \
                echo "Warning: Could not get pod statuses for cluster '$cluster_name'"

            # Get all pod names and namespaces and save logs
            all_workload_pods=$($kubectl_cmd --kubeconfig="$kubeconfig" get pods --all-namespaces -o=jsonpath='{range .items[*]}{.metadata.namespace}{"/"}{.metadata.name}{" "}{end}')

            if [ -z "$all_workload_pods" ]; then
                echo "No pods found in cluster '$cluster_name'."
                continue
            fi

            for pod_ns_name in $all_workload_pods; do
                namespace=$(echo "$pod_ns_name" | cut -d'/' -f1)
                pod=$(echo "$pod_ns_name" | cut -d'/' -f2)

                # Fetch and save logs for all containers in the pod
                $kubectl_cmd --kubeconfig="$kubeconfig" logs --ignore-errors -n "$namespace" --all-containers "$pod" > logs/"$cluster_name-$pod".log 2>&1 || \
                    echo "Warning: Could not get logs for pod: $pod in cluster '$cluster_name'"
            done
        fi
    done
}

# 1. Collect kubeconfigs from the management cluster
get_workload_kubeconfigs

# 2. Get pod YAML from each workload cluster
get_workload_pods_info

# 3. Collect logs and YAMLs for the management cluster
for namespace in $($kubectl_cmd get ns -o jsonpath='{.items[*].metadata.name}'); do
  # Check if the current namespace is in the ignored list
  is_ignored=false
  for ignored_ns in "${ignored_namespaces[@]}"; do
    if [[ "$namespace" == "$ignored_ns" ]]; then
      echo "--- Skipping logs for ignored namespace: $namespace ---"
      is_ignored=true
      break
    fi
  done
  
  if [ "$is_ignored" = true ]; then
    continue # Skip to the next namespace in the outer loop
  fi

  echo "--- Processing namespace: $namespace ---"

  # Get events for the namespace (helps debugging machine/pod issues)
  $kubectl_cmd -n "$namespace" get events --sort-by=.lastTimestamp > logs/"$namespace"-events.log 2>/dev/null || \
    echo "Warning: Could not get events for namespace: $namespace"

  # Get YAML output for pods
  $kubectl_cmd -n "$namespace" get po -o yaml > logs/"$namespace"-po.yaml
 
  # Fetch and save logs for all pods in the current namespace
  for pod in $($kubectl_cmd get pods -n "$namespace" -o=jsonpath='{.items[*].metadata.name}'); do
    $kubectl_cmd logs --ignore-errors -n "$namespace" --all-containers "$pod" > logs/"$pod".log 2>&1 || \
      echo "Warning: Could not get logs for pod: $pod in namespace: $namespace"
  done

  # Get YAML output for Cluster API resources
  for resource_type in "${capi_resource_types[@]}"; do
    $kubectl_cmd -n "$namespace" get "$resource_type" -o yaml > logs/"$namespace"-"$resource_type".yaml 2>/dev/null || \
      echo "Warning: No $resource_type found in namespace $namespace"
  done
done
