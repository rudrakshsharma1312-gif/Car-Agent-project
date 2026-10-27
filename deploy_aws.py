#!/usr/bin/env python3
"""
Automated AWS Deployment for CarCheck Agent
Deploys the full-stack container (React 18 PWA + FastAPI + OpenCV CV engine)
to an AWS EC2 instance with an automated public web link.
"""

import sys
import os
import time
import base64

# Force UTF-8 encoding on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

def get_session():
    try:
        session = boto3.Session()
        sts = session.client("sts")
        caller = sts.get_caller_identity()
        print(f"[+] Authenticated as: {caller['Arn']}")
        print(f"    Account: {caller['Account']}")
        region = session.region_name or "ap-south-1"
        print(f"    Region:  {region}")
        return session, region
    except Exception as e:
        print("\n[!] AWS Authentication Error:")
        print(f"    {e}")
        print("\n    Action required:")
        print("    Please run 'aws login' in your PowerShell/terminal to re-authenticate with AWS,")
        print("    then re-run: python deploy_aws.py\n")
        sys.exit(1)

def get_or_create_security_group(ec2, vpc_id):
    sg_name = "carcheck-agent-sg"
    try:
        sgs = ec2.describe_security_groups(
            Filters=[
                {"Name": "group-name", "Values": [sg_name]},
                {"Name": "vpc-id", "Values": [vpc_id]}
            ]
        )["SecurityGroups"]
        if sgs:
            sg_id = sgs[0]["GroupId"]
            print(f"[*] Found existing security group: {sg_id} ({sg_name})")
            return sg_id
    except ClientError:
        pass

    print(f"[*] Creating security group '{sg_name}' in VPC {vpc_id}...")
    sg = ec2.create_security_group(
        GroupName=sg_name,
        Description="Security group for CarCheck Agent Web App (ports 80, 8000, 22)",
        VpcId=vpc_id,
        TagSpecifications=[{
            "ResourceType": "security-group",
            "Tags": [{"Key": "Name", "Value": sg_name}, {"Key": "Project", "Value": "CarCheckAgent"}]
        }]
    )
    sg_id = sg["GroupId"]

    # Ingress rules
    ip_permissions = [
        {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTP"}]},
        {"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "FastAPI Direct"}]},
        {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH"}]}
    ]
    ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=ip_permissions)
    print(f"[+] Configured inbound rules for port 80 (HTTP) and port 8000 on {sg_id}")
    return sg_id

def get_latest_al2023_ami(ssm):
    param = ssm.get_parameter(Name="/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64")
    ami_id = param["Parameter"]["Value"]
    print(f"[*] Using latest Amazon Linux 2023 AMI: {ami_id}")
    return ami_id

def get_default_vpc_and_subnet(ec2):
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        vpcs = ec2.describe_vpcs()["Vpcs"]
    if not vpcs:
        raise RuntimeError("No VPC found in this AWS region.")
    vpc_id = vpcs[0]["VpcId"]

    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    if not subnets:
        raise RuntimeError(f"No subnets found in VPC {vpc_id}.")
    subnet_id = subnets[0]["SubnetId"]
    return vpc_id, subnet_id

def generate_user_data():
    script = """#!/bin/bash
set -e
exec > /var/log/carcheck-deploy.log 2>&1

echo "=========================================================="
echo " Starting CarCheck Agent Automated Deployment"
echo " Time: $(date)"
echo "=========================================================="

# 1. Setup 4GB Swap file to ensure smooth Docker & npm compilation
if [ ! -f /swapfile ]; then
    echo "Creating 4GB swap space..."
    fallocate -l 4G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=4096
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo "/swapfile swap swap defaults 0 0" >> /etc/fstab
fi

# 2. Install Git and Docker
echo "Installing Docker and Git..."
dnf update -y
dnf install -y docker git
systemctl enable --now docker
usermod -a -G docker ec2-user

# 3. Clone Repository
WORKDIR="/opt/carcheck"
mkdir -p "$WORKDIR"
cd "$WORKDIR"
echo "Cloning CarCheck Agent repository..."
git clone https://github.com/Shourya1507/Car-Agent-project.git repo
cd repo

# 4. Build Docker container
echo "Building Docker container image (compiling frontend + backend)..."
docker build -t carcheck-app .

# 5. Run Container
echo "Starting container on port 80..."
docker run -d \\
    --name carcheck-service \\
    -p 80:8000 \\
    -p 8000:8000 \\
    -e PORT=8000 \\
    --restart always \\
    carcheck-app

echo "=========================================================="
echo " CarCheck Agent Deployment Complete!"
echo " Service running on port 80 and 8000"
echo " Health check at: http://localhost/health"
echo " Time: $(date)"
echo "=========================================================="
"""
    return script

def deploy(instance_type="t3.small"):
    print("=" * 70)
    print(" [*] CarCheck Agent - Automated AWS Deployment")
    print("=" * 70)

    session, region = get_session()
    ec2_client = session.client("ec2", region_name=region)
    ssm_client = session.client("ssm", region_name=region)

    vpc_id, subnet_id = get_default_vpc_and_subnet(ec2_client)
    print(f"[*] Target VPC: {vpc_id} | Subnet: {subnet_id}")

    sg_id = get_or_create_security_group(ec2_client, vpc_id)
    ami_id = get_latest_al2023_ami(ssm_client)
    user_data_script = generate_user_data()

    print(f"[*] Launching EC2 instance ({instance_type}) with CarCheck full-stack container...")
    response = ec2_client.run_instances(
        ImageId=ami_id,
        InstanceType=instance_type,
        MinCount=1,
        MaxCount=1,
        SecurityGroupIds=[sg_id],
        SubnetId=subnet_id,
        UserData=user_data_script,
        BlockDeviceMappings=[{
            "DeviceName": "/dev/xvda",
            "Ebs": {
                "VolumeSize": 30,
                "VolumeType": "gp3",
                "DeleteOnTermination": True
            }
        }],
        TagSpecifications=[{
            "ResourceType": "instance",
            "Tags": [
                {"Key": "Name", "Value": "CarCheck-Agent-Server"},
                {"Key": "Project", "Value": "CarCheckAgent"}
            ]
        }]
    )

    instance_id = response["Instances"][0]["InstanceId"]
    print(f"[*] Instance created: {instance_id}. Waiting for it to become running...")

    waiter = ec2_client.get_waiter("instance_running")
    waiter.wait(InstanceIds=[instance_id])

    desc = ec2_client.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    public_ip = desc.get("PublicIpAddress")
    public_dns = desc.get("PublicDnsName")

    print("\n" + "=" * 70)
    print(" [SUCCESS] AWS INSTANCE LAUNCHED SUCCESSFULLY!")
    print("=" * 70)
    print(f" - Instance ID:   {instance_id}")
    print(f" - Instance Type: {instance_type}")
    print(f" - Public IP:     {public_ip}")
    print(f" - Public DNS:    {public_dns}")
    print("\n [>] DEPLOYED WEB LINKS:")
    print(f"   * Web App URL:      http://{public_ip}")
    print(f"   * Direct DNS URL:   http://{public_dns}")
    print(f"   * API Docs:         http://{public_ip}/docs")
    print(f"   * Health Endpoint:  http://{public_ip}/health")
    print("\n Note: The server is initializing Docker and building the container.")
    print(" The web page will be live in 2-3 minutes once the build completes.")
    print("=" * 70 + "\n")

    return public_ip, public_dns

if __name__ == "__main__":
    itype = sys.argv[1] if len(sys.argv) > 1 else "t3.small"
    deploy(itype)
