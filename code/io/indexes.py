"""
Index builder and RequestCase assembler (Step 2).

Builds dict-based indexes over the loaded data and provides
``build_request_case(request_id) -> RequestCase`` that returns
nested collections — never a flat cross-product join.

Usage::

    from code.io.indexes import load_and_index, build_request_case

    data, idx = load_and_index()
    case = build_request_case("request_63")
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from code.domain.models import (
    Event,
    ExchangeRate,
    ImageRef,
    Message,
    PaymentOption,
    Profile,
    Request,
    RequestCase,
)
from code.io.loaders import LoaderResult, load_all

# ---------------------------------------------------------------------------
# Module-level singletons (populated by load_and_index)
# ---------------------------------------------------------------------------

_data: Optional[LoaderResult] = None

# Primary indexes
profiles_by_user: Dict[str, Profile] = {}
events_by_user: Dict[str, List[Event]] = {}
events_by_id: Dict[str, Event] = {}
options_by_request: Dict[str, List[PaymentOption]] = {}
messages_by_user: Dict[str, List[Message]] = {}
messages_by_request: Dict[str, List[Message]] = {}
messages_by_event: Dict[str, List[Message]] = {}
images_by_request: Dict[str, List[ImageRef]] = {}
images_by_event: Dict[str, List[ImageRef]] = {}

# Request lookup
_requests_by_id: Dict[str, Request] = {}


# ---------------------------------------------------------------------------
# Index builder
# ---------------------------------------------------------------------------

def _build_indexes(data: LoaderResult) -> None:
    """Populate all module-level indexes from loaded data."""
    global profiles_by_user, events_by_user, events_by_id
    global options_by_request
    global messages_by_user, messages_by_request, messages_by_event
    global images_by_request, images_by_event
    global _requests_by_id

    # --- profiles_by_user ---
    profiles_by_user.clear()
    for p in data.profiles:
        profiles_by_user[p.user_id] = p

    # --- events_by_user / events_by_id ---
    events_by_user.clear()
    events_by_id.clear()
    for ev in data.events:
        events_by_user.setdefault(ev.user_id, []).append(ev)
        events_by_id[ev.event_id] = ev

    # --- options_by_request ---
    options_by_request.clear()
    for opt in data.payment_options:
        options_by_request.setdefault(opt.request_id, []).append(opt)

    # --- messages_by_user / messages_by_request / messages_by_event ---
    messages_by_user.clear()
    messages_by_request.clear()
    messages_by_event.clear()
    for msg in data.messages:
        messages_by_user.setdefault(msg.user_id, []).append(msg)
        if msg.request_id is not None:
            messages_by_request.setdefault(msg.request_id, []).append(msg)
        if msg.related_event_id is not None:
            messages_by_event.setdefault(msg.related_event_id, []).append(msg)

    # --- images_by_request / images_by_event ---
    images_by_request.clear()
    images_by_event.clear()
    for img in data.images:
        images_by_request.setdefault(img.request_id, []).append(img)
        images_by_event.setdefault(img.related_event_id, []).append(img)

    # --- request lookup ---
    _requests_by_id.clear()
    for req in data.requests:
        _requests_by_id[req.request_id] = req


# ---------------------------------------------------------------------------
# Loader + index initialisation (call once)
# ---------------------------------------------------------------------------

def load_and_index(dataset_dir: Optional[str] = None) -> Tuple[LoaderResult, None]:
    """Load all data and build indexes. Returns the LoaderResult.

    Safe to call multiple times — subsequent calls are no-ops.
    """
    global _data
    if _data is not None:
        return _data, None

    _data = load_all(dataset_dir)
    _build_indexes(_data)
    return _data, None


def get_data() -> LoaderResult:
    """Return the loaded data, initialising if needed."""
    if _data is None:
        load_and_index()
    return _data  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# RequestCase assembler
# ---------------------------------------------------------------------------

def build_request_case(request_id: str) -> RequestCase:
    """Assemble all data needed to decide one request.

    Returns a RequestCase with nested collections:
    - request: the Request object
    - profile: the user's Profile
    - events: ALL events for this user (not just request-linked ones)
    - payment_options: only options for this request
    - messages: user-level + request-level + event-level messages, deduplicated
    - images: request-level + event-level images, deduplicated

    This never performs a flat cross-product join. Each collection is
    independently gathered and deduplicated by its primary key.
    """
    # Ensure indexes are built
    if _data is None:
        load_and_index()

    request = _requests_by_id.get(request_id)
    if request is None:
        raise KeyError(f"Unknown request_id: {request_id!r}")

    user_id = request.user_id

    # Profile
    profile = profiles_by_user.get(user_id)
    if profile is None:
        raise KeyError(f"No profile for user_id: {user_id!r}")

    # Events: all events for this user
    events = events_by_user.get(user_id, [])

    # Payment options: only for this request
    payment_options = options_by_request.get(request_id, [])

    # Messages: gather from user, request, and event indexes; deduplicate
    # by message_id to avoid counting a message twice when it appears in
    # both the user index and the request/event index.
    seen_msg_ids: set = set()
    messages: List[Message] = []

    # 1. Messages linked to this user
    for msg in messages_by_user.get(user_id, []):
        if msg.message_id not in seen_msg_ids:
            seen_msg_ids.add(msg.message_id)
            messages.append(msg)

    # 2. Messages linked to this request (may already be captured via user)
    for msg in messages_by_request.get(request_id, []):
        if msg.message_id not in seen_msg_ids:
            seen_msg_ids.add(msg.message_id)
            messages.append(msg)

    # 3. Messages linked to any of this user's events
    for ev in events:
        for msg in messages_by_event.get(ev.event_id, []):
            if msg.message_id not in seen_msg_ids:
                seen_msg_ids.add(msg.message_id)
                messages.append(msg)

    # Images: gather from request and event indexes; deduplicate
    seen_img_ids: set = set()
    images: List[ImageRef] = []

    # 1. Images linked to this request
    for img in images_by_request.get(request_id, []):
        if img.image_id not in seen_img_ids:
            seen_img_ids.add(img.image_id)
            images.append(img)

    # 2. Images linked to any of this user's events
    for ev in events:
        for img in images_by_event.get(ev.event_id, []):
            if img.image_id not in seen_img_ids:
                seen_img_ids.add(img.image_id)
                images.append(img)

    return RequestCase(
        request=request,
        profile=profile,
        events=events,
        payment_options=payment_options,
        messages=messages,
        images=images,
    )

