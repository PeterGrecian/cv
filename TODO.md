better name.  this is my website publishing core.  better docs about how it works and facilitates projects.  invocation stats

## Major: API Gateway + Lambda Architecture Reorganization
**Problem**: Monolithic cv.py (~3500 lines) handling all endpoints in single Lambda function

### Current issues:
- Single Lambda handles: gardencam, t3, lambda-stats, memspeed, gitinfo, contents, etc.
- Can't scale/deploy endpoints independently
- One bug/timeout affects everything
- Can't set different memory/timeout per endpoint
- Hard to test individual components
- Cold starts affect all endpoints equally
- Difficult to maintain and reason about

### Target architecture options:
1. **Microservices**: Separate Lambda per major service
   - gardencam-api (camera functions)
   - transport-api (t3 bus times)
   - stats-api (lambda-stats)
   - memspeed-api
   - static-content (or move to S3 + CloudFront)

2. **Lambda Function URLs**: Skip API Gateway for simple endpoints

3. **Monorepo with shared code**: Common utilities, separate handlers

4. **Infrastructure as Code**: Terraform modules per service (already have Terraform)

### Staged migration approach:
**Phase 1: Planning & Design**
- Document all current endpoints and dependencies
- Map out new service boundaries
- Design shared infrastructure (auth, logging, common functions)
- Choose IaC strategy (Terraform modules)
- Define success metrics

**Phase 2: Foundation**
- Set up shared library/common code
- Create deployment pipeline for multi-Lambda
- Set up monitoring/observability per service
- Migrate logging to CloudWatch → S3 pattern

**Phase 3: Extract Services (one at a time)**
- Start with most independent service (memspeed or t3?)
- Deploy alongside existing monolith
- Route subset of traffic to new service
- Validate, monitor, iterate
- Remove from monolith once stable

**Phase 4: Iterative Migration**
- Extract one service at a time
- Run in parallel during transition
- Gradual traffic migration
- Roll back capability at each stage

**Phase 5: Cleanup**
- Deprecate monolithic cv.py
- Consolidate API Gateway routes
- Update documentation
- Optimize costs

### Benefits:
- Independent scaling per service
- Faster deployments (only changed service)
- Better separation of concerns
- Easier testing and debugging
- Can use different runtimes/languages per service
- Reduced blast radius of bugs
- Better cost optimization (right-size each Lambda)

### Note:
This is a significant undertaking. Should be planned carefully, not done in haste.
Current system works - this is about maintainability and future scaling.

## Lambda Logging Architecture - Migrate away from DynamoDB
**Current**: Lambda → DynamoDB → manual export → S3 → Athena → Grafana
**Better**: Lambda → CloudWatch Logs → S3 → Athena → Grafana

### Why change:
- CloudWatch Logs already captures everything automatically (duration, memory, errors, print statements)
- DynamoDB is redundant and not designed for log analytics
- CloudWatch → S3 export is a native AWS pattern for log archival
- Richer data (CloudWatch includes stack traces, all print output)
- One less moving part to maintain
- DynamoDB better suited for app state, not logs

### Migration approach:
1. Set up CloudWatch Logs export to S3 (built-in feature, can schedule daily/weekly)
   OR set up CloudWatch Logs → Kinesis Firehose → S3 (near real-time)
2. Update Athena table to parse CloudWatch Logs format
3. Remove DynamoDB logging code from cv.py (log_execution_metrics, log_connection_data)
4. Remove 'lambda-execution-logs' DynamoDB table
5. Note: Grafana can also connect directly to CloudWatch Logs data source

### Cost impact:
- Current: DynamoDB writes + S3 storage
- Future: Just S3 storage (CloudWatch Logs free tier covers 5GB ingestion + 5GB storage)
- For 100 logs/day: essentially free either way

## Terse Transport Times (t3) improvements
- Better logging: capture query parameters (stop=parklands/surbiton) in DynamoDB logs
- This would allow statistics to show breakdowns by:
  - Which stop is being requested (Parklands vs Surbiton)
  - Could potentially track which direction data is being viewed (though currently both directions are fetched together)
- Currently only logging path `/t3` without query parameters, losing granularity