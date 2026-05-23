# Runbook: Database Connection Pool Exhaustion

## Overview
All database connections are occupied. New requests cannot acquire connections, causing timeouts and 500 errors.

## Symptoms
- Application logs show "ConnectionPoolExhausted" or "Timeout waiting for connection"
- HTTP 500/503 errors increasing
- Request latency spike

## Diagnosis Steps

### Check Current Connections (PostgreSQL)
    SELECT application_name, state, count(*) FROM pg_stat_activity GROUP BY 1,2 ORDER BY 3 DESC;

### Find Long-Running Queries
    SELECT pid, now() - query_start AS duration, query, state FROM pg_stat_activity WHERE state != 'idle' ORDER BY duration DESC LIMIT 10;

## Remediation

### Immediate: Kill Idle Connections
    SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE state = 'idle' AND query_start < now() - interval '10 minutes';

### Short-term: Increase Pool Size
Increase pool_size and max_overflow in application configuration.

### Long-term: Deploy PgBouncer
Deploy a connection pooling middleware to reuse connections efficiently.
