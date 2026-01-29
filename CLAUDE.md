# Project Context for Claude

## Overview

This is an AWS Lambda function served via API Gateway that hosts multiple web applications:
- Personal CV/resume website
- Gardencam image viewer (password protected)
- K2 bus times (TfL API integration)
- Lambda execution statistics

## Architecture

- **AWS Lambda** runs `cv.py` as the handler
- **API Gateway** routes requests to the Lambda
- **Terraform** in `terraform/` provisions the infrastructure

## Routes

| Path | Function |
|------|----------|
| `/` (default) | Serves `cv.html` (CV/resume) |
| `/contents` | Home page (`contents.html`) |
| `/gardencam` | Latest 3 images with capture button |
| `/gardencam/gallery` | Thumbnails organized by 4-hour periods |
| `/gardencam/stats` | Chart.js statistics visualization |
| `/gardencam/s3-stats` | S3 storage stats (reads from hourly cache) |
| `/gardencam/capture` | POST endpoint to trigger remote capture |
| `/gardencam/fullres` | Full resolution image view |
| `/gardencam/display` | Display-width image view |
| `/t3` | K2 bus arrivals from TfL API (Parklands stop) |
| `/lambda-stats` | Lambda execution costs and metrics |
| `/event` | Debug info (request details) |
| `/gitinfo` | Git info page |

## AWS Resources

| Resource | Purpose |
|----------|---------|
| S3: `gardencam-berrylands-eu-west-1` | Stores garden images and thumbnails |
| DynamoDB: `cv-access-logs` | Logs page visits |
| DynamoDB: `gardencam-stats` | Image capture statistics |
| DynamoDB: `gardencam-commands` | Remote capture commands |
| DynamoDB: `lambda-execution-logs` | Execution metrics and costs |
| Secrets Manager: `gardencam/password` | Basic auth password for gardencam |
| Secrets Manager: `tfl/api-key` | TfL API key for bus times |

## Key Files

| File | Purpose |
|------|---------|
| `cv.py` | Main Lambda handler with all routes |
| `cv.html` | CV/resume HTML content |
| `contents.html` | Home page HTML |
| `gitinfo.html` | Git info page |
| `terraform/` | Infrastructure as code |
| `update` | Deployment script |

## Authentication

Gardencam endpoints use HTTP Basic Auth. Password is stored in AWS Secrets Manager (`gardencam/password`). Username can be anything.

## Related Projects

- **Gardencam Pi** (`~/Berrylands/gardencam/`): Raspberry Pi that captures images and uploads to S3
- The Pi polls `gardencam-commands` DynamoDB table for remote capture requests

## AWS Region

eu-west-1 (Ireland)

## Development

Run locally with mock data:
```bash
python cv.py
```

Deploy changes:
```bash
./update
```
