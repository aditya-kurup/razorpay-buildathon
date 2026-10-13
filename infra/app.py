import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2, aws_ecs as ecs, aws_ecs_patterns as patterns, aws_secretsmanager as sm
from constructs import Construct

class GrowthStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        secret = sm.Secret.from_secret_name_v2(self, "GrowthSecrets", self.node.try_get_context("secret_name") or "growth-agent")
        cluster = ecs.Cluster(self, "Cluster", vpc=ec2.Vpc(self, "Vpc", max_azs=2))
        service = patterns.ApplicationLoadBalancedFargateService(self, "Service", cluster=cluster, cpu=256, memory_limit_mib=512, task_image_options=patterns.ApplicationLoadBalancedTaskImageOptions(image=ecs.ContainerImage.from_asset("."), container_port=8000, secrets={"GEMINI_API_KEY": ecs.Secret.from_secrets_manager(secret, "GEMINI_API_KEY"), "RAZORPAY_KEY_SECRET": ecs.Secret.from_secrets_manager(secret, "RAZORPAY_KEY_SECRET")}))
        service.service.connections.allow_from_any_ipv4(ec2.Port.tcp(8000))
app = cdk.App()
GrowthStack(app, "GrowthAgentStack")
app.synth()
