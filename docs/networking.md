# Networking Architecture

All network infrastructure is provisioned by `terraform/networking.tf`. No pre-existing VPC or subnets are required — a full isolated network is created from scratch on `terraform apply`.

---

## Overview diagram

```
                          Internet
                              │
                    ┌─────────▼─────────┐
                    │  Internet Gateway  │
                    └─────────┬─────────┘
                              │
            ┌─────────────────▼──────────────────┐
            │          VPC  10.0.0.0/16           │
            │                                     │
            │  ┌──────────────────────────────┐   │
            │  │       Public Subnets          │   │
            │  │  AZ-0: 10.0.0.0/24           │   │
            │  │  AZ-1: 10.0.1.0/24           │   │
            │  │                              │   │
            │  │   ┌──────────────────┐       │   │
            │  │   │   NAT Gateway    │       │   │
            │  │   │   (Elastic IP)   │       │   │
            │  │   └────────┬─────────┘       │   │
            │  └────────────│─────────────────┘   │
            │               │ (outbound only)      │
            │  ┌────────────▼─────────────────┐   │
            │  │       Private Subnets         │   │
            │  │  AZ-0: 10.0.10.0/24          │   │
            │  │  AZ-1: 10.0.11.0/24          │   │
            │  │                              │   │
            │  │  ┌──────────┐ ┌──────────┐   │   │
            │  │  │ Lambda   │ │   RDS    │   │   │
            │  │  │ (SG)     │ │ (SG)     │   │   │
            │  │  └──────────┘ └──────────┘   │   │
            │  └──────────────────────────────┘   │
            └─────────────────────────────────────┘
```

---

## VPC

**CIDR:** `10.0.0.0/16` — 65,536 addresses, giving plenty of room for future subnets.

**DNS settings:** Both `enable_dns_hostnames` and `enable_dns_support` are on. This is required for RDS — without them, the RDS endpoint hostname won't resolve inside the VPC.

The VPC is fully isolated from other AWS accounts and from the public internet except through the explicitly defined gateways below.

---

## Subnets

There are four subnets split across two Availability Zones. The AZs are discovered automatically using `data.aws_availability_zones`, so the same Terraform config deploys correctly in any AWS region.

| Subnet | AZ | CIDR | Purpose |
|--------|----|------|---------|
| public-0 | AZ-0 | `10.0.0.0/24` | NAT Gateway |
| public-1 | AZ-1 | `10.0.1.0/24` | NAT Gateway (spare / future HA) |
| private-0 | AZ-0 | `10.0.10.0/24` | Lambda functions, RDS primary |
| private-1 | AZ-1 | `10.0.11.0/24` | Lambda functions, RDS standby |

**Why two AZs?** RDS `multi_az = true` requires the DB subnet group to span at least two AZs so AWS can place the standby replica in a separate data centre. Lambda also distributes across both private subnets for availability.

**Why separate public and private subnets?** Lambda functions and RDS must never have public IP addresses. They live in private subnets with no direct path to the internet. The public subnets exist solely to host the NAT Gateway, which provides controlled outbound-only access.

---

## Internet Gateway

The Internet Gateway (IGW) attaches to the VPC and gives the public subnets a route to `0.0.0.0/0`. Resources in public subnets with a public IP (like the NAT Gateway's Elastic IP) can reach the internet through it.

The IGW itself does not give private subnet resources any internet access — that is the NAT Gateway's job.

---

## NAT Gateway

The NAT Gateway sits in `public-0` and holds an Elastic IP (a static public address). It translates outbound connections from the private subnets to that public IP, then forwards the response back.

**Why Lambda needs this:** Lambda functions run in the private subnets and need to make outbound HTTPS calls to:
- **AWS S3** — upload genomics files
- **AWS SNS** — publish pipeline notifications
- **AWS HealthOmics** — list and retrieve ReadSets
- **DNAnexus** — list files and stream downloads

None of these have VPC-internal endpoints configured here, so all traffic exits through the NAT Gateway. Inbound connections to Lambda are never initiated from outside — Lambda is always the client.

**Single NAT Gateway:** One NAT in AZ-0 covers both private subnets. This is sufficient for a demo and reduces cost. For production, add a second NAT in AZ-1 so that an AZ failure doesn't cut off outbound connectivity for the AZ-1 private subnet.

---

## Route tables

Two route tables control where traffic goes.

**Public route table** — attached to both public subnets:

| Destination | Target |
|-------------|--------|
| `10.0.0.0/16` | local (VPC) |
| `0.0.0.0/0` | Internet Gateway |

**Private route table** — attached to both private subnets:

| Destination | Target |
|-------------|--------|
| `10.0.0.0/16` | local (VPC) |
| `0.0.0.0/0` | NAT Gateway |

The private subnets have no route to the IGW directly — all internet-bound traffic is NATed. Inbound connections from the internet cannot reach private subnet resources.

---

## Security groups

Security groups act as stateful firewalls at the resource level. Two are provisioned: one for Lambda, one for RDS.

### Lambda SG

| Direction | Port | Protocol | Destination | Reason |
|-----------|------|----------|-------------|--------|
| Egress | 5432 | TCP | RDS SG | PostgreSQL queries |
| Egress | 443 | TCP | `0.0.0.0/0` | S3, SNS, HealthOmics, DNAnexus |
| Ingress | — | — | — | None — Lambda is always the client |

### RDS SG

| Direction | Port | Protocol | Source | Reason |
|-----------|------|----------|--------|--------|
| Ingress | 5432 | TCP | Lambda SG | Accept queries from Lambda only |
| Egress | — | — | — | None needed — RDS only responds |

The Lambda SG's port 5432 egress rule references the RDS SG directly (not a CIDR range). This means only resources in the Lambda SG can ever reach the database — not any other resource in the VPC, even if it happens to be in the same subnet.

---

## RDS subnet group

The `aws_db_subnet_group` resource registers both private subnets with RDS so it can place the primary instance in one AZ and the standby in the other. This is a prerequisite for `multi_az = true` on the RDS instance in `s3_rds.tf`.

---

## What is not provisioned here

- **VPC endpoints for S3/SNS** — would eliminate NAT Gateway traffic for AWS-internal services and reduce cost and latency in production. Not included to keep the demo simple.
- **Second NAT Gateway** — needed for AZ-redundant outbound connectivity in production.
- **Bastion host / VPN** — there is no way to SSH into the private subnets. In production, use AWS Systems Manager Session Manager for any administrative access.
