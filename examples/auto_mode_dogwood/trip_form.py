"""The trip form: the agent asks the user who is traveling, where, and when.

``ask_trip_details`` is a callback tool, so it has no body: the console renders
:class:`TripDetails` as a form, and what the user submits is the tool's result. The evaluator
records that result with the verifier as ``travel-agent/ask_trip_details/result``, and
``policies/travel.dw`` only lets a booking through when it matches a form the user filled in.

No ``from __future__ import annotations``: the callback's result is validated against
``TripDetails`` by a TypeAdapter built from the stub's annotations.
"""

import datetime

from pydantic import BaseModel, Field

from temporal_agent_harness.harness import agent


class TripDetails(BaseModel):
    """The trip, as the user describes it."""

    traveler_name: str = Field(
        title="Traveler name", description="Full name, as it should appear on the booking."
    )
    origin: str = Field(title="From", description="Departure airport code, e.g. SFO.")
    destination: str = Field(title="To", description="Arrival airport code, e.g. JFK.")
    departure_date: datetime.date = Field(
        title="Departure date", description="The day to fly there, as YYYY-MM-DD."
    )
    return_date: datetime.date | None = Field(
        default=None,
        title="Return date",
        description="The day to fly back, as YYYY-MM-DD. Leave empty for a one-way trip.",
    )


@agent.callback_tool_defn()
async def ask_trip_details(reason: str) -> TripDetails:
    """Ask the user for a trip's details: the traveler's name, the route, and the dates there and
    back. The user fills in a form. Use it only when you don't already know them; if the chat or
    a file gives them, book with those. With the form's answer, pass the traveler's name as the
    passenger or guest, and `departure_date` (there) or `return_date` (back) as the flight's
    `date`. `reason` is shown to the user above the form."""
    ...
