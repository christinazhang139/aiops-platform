# SOP: Incident Response Procedure

## Severity Definitions
- P0: Service completely unavailable
- P1: Major degradation, most users affected
- P2: Partial degradation, some users affected

## Response Steps

### 1. Acknowledge and Communicate (within 5 minutes)
- Confirm alert is genuine
- Open incident Slack channel
- Notify relevant personnel

### 2. Investigate (5-30 minutes)
- Check recent deployments/changes
- Review Grafana dashboards
- Examine application logs

### 3. Mitigate (restore service first)
- Rollback recent deployment
- Scale up
- Restart pods
- Failover to backup

### 4. Verify Recovery
- Confirm metrics return to normal
- Run smoke tests
- Update incident channel with status

### 5. Post-Mortem (within 48 hours)
- Document timeline
- Root cause analysis (5 Whys)
- Action items with owners and deadlines
