# Runbook: OOM Kill Diagnosis and Scaling

## Overview
OOM Kill occurs when a container exceeds its memory limits. The Linux kernel forcibly terminates the process.

## Severity
- P1: Multiple Pods OOM Killed
- P2: Single Pod occasional OOM

## Diagnosis Steps

### Confirm OOM Kill
    kubectl get pod <pod> -o jsonpath='{.status.containerStatuses[*].lastState}'
    kubectl describe pod <pod> | grep -A3 "Last State"

### Check Memory Usage
    kubectl top pod <pod>
    kubectl get pod <pod> -o jsonpath='{.spec.containers[*].resources}'

### Analyze Memory Trend
Check Grafana dashboard for container_memory_working_set_bytes:
- Gradual increase = memory leak
- Sudden spike = traffic burst
- Stable high = limits set too low

## Remediation

### Immediate: Increase Memory Limits
    kubectl set resources deployment/<name> --limits=memory=512Mi --requests=memory=256Mi

### If Memory Leak Suspected
1. Enable heap profiling
2. Capture heap dump for analysis
3. Fix code and redeploy

### Prevention
- All containers must have memory limits set
- Set monitoring alerts at 80% of limits
- Use VPA for automatic resource adjustment
