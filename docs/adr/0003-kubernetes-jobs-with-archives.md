# Runs as rendered Kubernetes Jobs, with an Archive that is not a Sink

Each (Site, Location) Run executes as one on-demand Kubernetes Job (ADR-0002). Two choices here would otherwise look odd.

**The Archive is separate from the Sinks.** A pod's Run directory disappears with the pod, yet Replay and `inspect` need its Captures and Extraction Traces. We could have made the S3 Sink upload them too. Instead, `--archive` copies the whole Run directory to its own prefix, because the two have opposite rules. A Sink feeds consumers with Records: it skips `failed` Runs and deletes objects the Run no longer has. An Archive exists for debugging: it is made for every Run, *especially* `failed` and `shutdown` ones, and it must never be pruned by a Sink re-export. Archives are pulled back to a local Run directory (`scrape pull`) rather than read from S3 in place, so every reader stays file-based.

**Kustomize owns the cluster; `scrape deploy job` owns the Run.** Kustomize overlays (`deploy/k8s/overlays/{local,cloud}`) hold the cluster-specific parts: storage (`hostPath` locally, `emptyDir` plus `--archive` in the cloud), image, ServiceAccount, failure policy and grace period. Kustomize cannot take parameters, so the renderer reads an overlay's built Job template on stdin (`kubectl kustomize … | scrape deploy job …`) and fills in only what comes from the Site: name, labels, `scrape run` args, the Site's Secret and browser resources. We rejected a Python-only renderer with environment profiles, because it would move cluster tweaks into CLI flags and code that ops people cannot edit as plain YAML.

## Consequences

- Archive failure exits `4`. Sink failure (`3`) takes precedence over it, and both take precedence over the Run Health code. The Archive is made before the Sinks, so it fits inside the shutdown grace period.
- An Archive must not share a prefix with an S3 Sink; the CLI refuses overlapping URLs.
- Archives hold raw response bodies, so the bucket must be private and encrypted.
- Jobs use `generateName`, so they are created with `kubectl create`, not `apply`.
- Scheduling is out of scope (#44): a scheduler creates these same rendered Jobs.
