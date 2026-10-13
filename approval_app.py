import os
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Growth Agent Approval")
APPROVERS = {x.strip() for x in os.getenv("SLACK_APPROVER_IDS", "").split(",") if x.strip()}
class Decision(BaseModel):
    offer_id: str
    approver_id: str
    approved: bool

@app.post("/slack/approve")
def approve(decision: Decision):
    if decision.approver_id not in APPROVERS:
        raise HTTPException(403, "approver is not allowlisted")
    return {"offer_id": decision.offer_id, "status": "approved" if decision.approved else "rejected"}
