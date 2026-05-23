# Runbook: Pod CrashLoopBackOff

## Overview
Pod repeatedly starts and crashes. Kubernetes restarts with exponential backoff (10s, 20s, 40s... max 5min).

## Diagnosis Steps

### Check Pod Events
    kubectl describe pod <pod> -n <ns>

Look at Events section for common causes:
- OOMKilled: Out of memory
- Error: Application crash
- ImagePullBackOff: Image pull failure

### Check Crash Logs
    kubectl logs <pod> --previous

### Check Configuration
    kubectl get configmap -n <ns>
    kubectl get secret -n <ns>

Verify required ConfigMaps/Secrets exist with correct keys.

## Remediation

### OOMKilled -> Increase Memory
    kubectl set resources deployment/<name> --limits=memory=512Mi

### Application Error -> Rollback
    kubectl rollout undo deployment/<name>

### Missing Config -> Recreate
    kubectl apply -f <config-manifest>.yaml
    kubectl rollout restart deployment/<name>
