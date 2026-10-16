"""Explicit, credential-free proposal simulation for the public portfolio demo."""

from typing import Any, Mapping


class DemoModelClient:
    def complete(self, prompt: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if payload.get("_response_kind") == "strategy":
            band = payload["group_discount_band"]
            return {
                "approach": "Simulated cohort proposal for the public demo; policy checks run on the server.",
                "option_id": "addon:guest_access_pack",
                "discount_min": str(band["minimum"]),
                "discount_max": str(band["maximum"]),
            }
        options = payload["allowed_options"]
        option = next((item for item in options if item["offer_type"] == "seat_expansion"), options[0])
        response = {
            "option_id": option["option_id"],
            "proposed_discount": str(payload["discount_band"]["minimum"]),
            "reason": "Simulated proposal based on eligible account options. All policy checks run on the server.",
        }
        if option.get("minimum_seats") is not None:
            response["proposed_seats"] = option["minimum_seats"]
        return response
