# Growth Agent runbook

Create a Secrets Manager secret named `growth-agent` with Gemini and Razorpay test credentials, then run `cdk deploy -c secret_name=growth-agent`. The Fargate service exposes the approval endpoint; restrict ingress to Slack or an API gateway before production.

Set `SLACK_APPROVER_IDS` to a comma separated allowlist. Every offer must pass the deterministic policy engine before a human decision is accepted. Review audit events and reservation balances after incidents.
