# OCI A1 instance retention: emergency idle safeguard

## Scope and risk

Oracle's [Always Free Compute idle policy](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) states that an A1 VM may be reclaimed when, over seven days, **CPU 95th percentile, network utilization, and memory utilization** are each below 20%. It is not a guarantee of retention when one metric is above 20%, and nothing can prevent an Oracle policy change or tenancy action. Reclaim eligibility is distinct from short-lived Bastion sessions and availability-domain capacity.

Katcha's October 5–12 OCI screenshots showed roughly 2–3% CPU, roughly 16% memory, and low network throughput. Since primary instance capacity required substantial polling to obtain, this temporary measure is justified as a bridge **while useful Katcha background workloads are being developed and measured**. It uses a bounded anonymous RAM reserve and is explicitly *not* a useful workload itself. Evaluate provider policy and replace it with beneficial cache/analysis activity when feasible. Do not represent it as guaranteed protection or provider-approved exemption.

## Operator install, after exact-head PR CI and merge

Run from a clean and updated `main` checkout in Ubuntu WSL. This requires the previously verified Bastion tunnel and `sudo -n` on the production primary, but it never needs OCI instance write access.

```bash
cd ~/src/katcha
bash scripts/katcha-oci-admin.sh connect
python3 scripts/oci-primary-idle-guard.py status
python3 scripts/oci-primary-idle-guard.py install
python3 scripts/oci-primary-idle-guard.py status
```

The operator command strictly pins the existing production SSH host key, targets only local port 22022, and does not expose secrets. The remote installer will refuse if the 12-GB class host, mounted /srv/katcha, active katcha.service, or >=6-GiB available RAM check fails. It does **not** restart or redeploy any Katcha service.

A new systemd service reserves **1,408 MiB** in 64 MiB chunks, touches anonymous pages so they are resident, and has a **1,800 MiB hard cgroup limit**. CPU quota is 10% of one CPU, there is no network access, it runs as `nobody`, and its OOM score makes it expendable under pressure. It releases the reserve when MemAvailable drops below **4,096 MiB** and reacquires it only when MemAvailable is at least 6,144 MiB. Memory accounting provides an actual >1,200-MiB residency verification at installation. If startup cannot verify this, the service is disabled and the installer refuses to claim success. A reboot restarts the guard through systemd.

This is a **memory safety margin, not a CPU miner**, and does not affect OCI OCPU-hour/GB-hour provisioned allowances. Under resource pressure the guard gives way to Katcha, so retention eligibility is less important than production correctness. The security policy is not changed. No new compute, storage, IAM, R2 or Cloudflare resources are created.

## Verify it really helps

After installation, check systemd and the memory figures:

```bash
python3 scripts/oci-primary-idle-guard.py status
bash scripts/katcha-oci-admin.sh audit
```

In OCI Console → Compute → Instances → `katcha-prod-primary-001` → Monitoring, compare `oci_computeagent.MemoryUtilization` before and 30–60 minutes after installation. Expect it to increase above 20%; the guard's Linux RSS alone is **not** evidence that Oracle's monitoring metric crossed the threshold. Recheck a 24-hour interval and continue measuring the rolling seven-day window. If it remains below 20% or production's free memory worsens, stop and diagnose rather than increasing allocation blindly. Configure OCI Notifications/Monitoring alarms separately with actual approved notification destinations; do not create uncontrolled billing or notification costs.

## Immediate disable / recovery

```bash
python3 scripts/oci-primary-idle-guard.py disable
python3 scripts/oci-primary-idle-guard.py status
```

Disable stops only `katcha-idle-guard.service`, frees its memory, and leaves Katcha's other production services and state alone. Installed files remain for investigation; do not delete other host files.

**Do not confuse this with a provider guarantee:** the safest long-term outcome is a functional, actually-utilized content pipeline, independently verified backups, and optionally a separately approved account-tier decision after a cost assessment. Oracle retains discretion under its policy.
