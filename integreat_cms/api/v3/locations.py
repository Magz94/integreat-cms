"""
This module includes functions related to the locations/POIs API endpoint.
"""

from __future__ import annotations

from typing import cast, TYPE_CHECKING

from django.conf import settings
from django.db.models import Prefetch
from django.http import JsonResponse
from django.utils import timezone
from django.utils.html import strip_tags

from ...cms.constants import status
from ...cms.models import Contact, POICategoryTranslation
from ...cms.models.pois.poi import get_default_opening_hours
from ...core.utils.strtobool import strtobool
from ..decorators import json_response
from .location_categories import transform_location_category

if TYPE_CHECKING:
    from typing import Any

    from django.http import HttpRequest

    from ...cms.models import POI, POITranslation

from datetime import datetime, time
from zoneinfo import ZoneInfo


def _tz_key(tz_candidate: str | ZoneInfo | None) -> str | None:
    """
    Normalize a timezone value to an IANA key string.

    :param tz_candidate: Timezone name (str) or ZoneInfo (or falsy).
    :return: Canonical IANA tz name (e.g. "Europe/Berlin") or None.
    """
    if not tz_candidate:
        return None
    if isinstance(tz_candidate, str):
        return tz_candidate
    # ZoneInfo has a .key attribute
    key = getattr(tz_candidate, "key", None)
    return key or str(tz_candidate)

def _iso_local_time(hms: str | None, tz_name: str | None) -> str | None:
    """
    Convert 'HH:MM'/'HH:MM:SS' (wall time) to 'HH:MM:SS±HH:MM' for today in a zone.

    :param hms: Local wall time ('HH:MM' or 'HH:MM:SS').
    :param tz_name: IANA timezone to use; falls back to settings.TIME_ZONE.
    :return: ISO-8601 time with numeric offset, or None if input is empty.
    """
    if not hms:
        return None
    try:
        t = time.fromisoformat(hms)
    except ValueError as e:
        raise ValueError(f"Expected 'HH:MM' or 'HH:MM:SS', got {hms!r}") from e

    default_tz: str = cast(str, getattr(settings, "TIME_ZONE", "Europe/Berlin"))
    tz_key = _tz_key(tz_name) or default_tz
    tz = ZoneInfo(tz_key)
    local_today = datetime.now(tz).date()
    dt = datetime(
        local_today.year, local_today.month, local_today.day,
        t.hour, t.minute, t.second, tzinfo=tz
    )

    # Format as 'HH:MM:SS+HH:MM' (e.g., Phoenix -> -07:00)
    base = dt.strftime("%H:%M:%S%z")
    return f"{base[:-5]}{base[-5:-2]}:{base[-2:]}"


def transform_poi(poi: POI | None) -> dict[str, Any]:
    """
    Function to create a JSON from a single poi object.

    :param poi: The poi object which should be converted
    :return: data necessary for API
    """
    if not poi:
        return {
            "id": None,
            "name": None,
            "address": None,
            "town": None,
            "state": None,
            "postcode": None,
            "region": None,
            "country": None,
            "latitude": None,
            "longitude": None,
        }
    return {
        "id": poi.id,
        "name": (
            poi.default_public_translation.title
            if poi.default_public_translation
            else None
        ),
        "address": poi.address,
        "town": poi.city,
        "state": None,
        "postcode": poi.postcode,
        "region": None,
        "country": poi.country,
        "latitude": poi.latitude,
        "longitude": poi.longitude,
    }


def transform_poi_translation(poi_translation: POITranslation, *, region_tz_name: str | None) -> dict[str, Any]:
    """
    Create JSON for a POI translation and enrich opening hours with ISO-8601 times.

    :param poi_translation: POI translation to convert.
    :param region_tz_name: IANA timezone (e.g., "America/Phoenix") used for slot offsets;
        falls back to settings.TIME_ZONE.
    :return: Data for the APIv3 locations endpoint.
    """

    poi = poi_translation.poi

    contacts = Contact.objects.filter(location=poi).all()

    # Note(johannes): Remove the primary_contact and the according three fields (phone_number, website, and email) in late 2025
    # https://github.com/digitalfabrik/integreat-cms/issues/3475
    primary_contact = contacts.get_primary_contact()

    contacts = contacts.filter(archived=False)

    contact_data = []
    for contact in contacts:
        contact_data.append(
            {
                "area_of_responsibility": contact.area_of_responsibility
                if contact.area_of_responsibility
                else None,
                "name": contact.name,
                "email": contact.email,
                "phone_number": contact.phone_number,
                "mobile_number": contact.mobile_phone_number,
                "website": contact.website,
                "opening_hours": contact.opening_hours,
                "appointment_url": contact.appointment_url or None,
            }
        )

    # Only return opening hours if they differ from the default value and the location is not temporarily closed
    opening_hours = None
    if not poi.temporarily_closed and poi.opening_hours != get_default_opening_hours():
        # Enrich timeSlots with ISO-8601 times using the region's timezone
        default_tz: str = cast(str, getattr(settings, "TIME_ZONE", "Europe/Berlin"))
        tz_key: str = _tz_key(region_tz_name) or default_tz

        src_days = poi.opening_hours or []
        enriched_days: list[dict[str, Any]] = []
        for day in src_days:
            # copy non-slot keys as-is
            new_day = {k: v for k, v in day.items() if k != "timeSlots"}
            new_slots: list[dict[str, Any]] = []
            for slot in (day.get("timeSlots") or []):
                start = slot.get("start")
                end = slot.get("end")
                new_slots.append(
                    {
                        **slot,  # keep legacy fields (start/end/comment/appointmentOnly/etc.)
                        "start_time": _iso_local_time(start, tz_key),
                        "end_time": _iso_local_time(end, tz_key),
                        "timezone": tz_key,
                    }
                )
            new_day["timeSlots"] = new_slots
            enriched_days.append(new_day)

        opening_hours = enriched_days

    return {
        "id": poi_translation.id,
        "url": settings.BASE_URL + poi_translation.get_absolute_url(),
        "path": poi_translation.get_absolute_url(),
        "title": poi_translation.title,
        "modified_gmt": poi_translation.last_updated,  # deprecated field in the future
        "last_updated": timezone.localtime(poi_translation.last_updated),
        "meta_description": poi_translation.meta_description,
        "excerpt": strip_tags(poi_translation.content),
        "content": poi_translation.content,
        "available_languages": poi_translation.available_languages_dict,
        "icon": poi.icon.url if poi.icon else None,
        "thumbnail": poi.icon.thumbnail_url if poi.icon else None,
        "website": primary_contact.website if primary_contact else None,
        "email": primary_contact.email if primary_contact else None,
        "phone_number": primary_contact.phone_number if primary_contact else None,
        "contacts": contact_data,
        "category": transform_location_category(
            poi.category,
            poi_translation.language.slug,
        ),
        "temporarily_closed": poi.temporarily_closed,
        # Only return opening hours if not temporarily closed and they differ from the default value
        "opening_hours": opening_hours,
        "appointment_url": poi.appointment_url or None,
        "location": transform_poi(poi),
        "hash": None,
        "organization": (
            {
                "id": poi.organization.id,
                "slug": poi.organization.slug,
                "name": poi.organization.name,
                "logo": poi.organization.icon.url,
                "website": poi.organization.website,
            }
            if poi.organization
            else None
        ),
        "barrier_free": poi.barrier_free,
    }


@json_response
def locations(
    request: HttpRequest,
    language_slug: str,
    **kwargs: Any,
) -> JsonResponse:
    """
    List all POIs of the region and transform result into JSON

    :param request: The current request
    :param language_slug: The slug of the requested language
    :return: JSON object according to APIv3 locations endpoint definition
    """
    region = request.region
    # Throw a 404 error when the language does not exist or is disabled
    region.get_language_or_404(language_slug, only_active=True)
    result = []
    pois = (
        region.pois.prefetch_public_translations()
        .filter(
            archived=False,
            # Exclude locations without public translation in the default language
            translations__language=region.default_language,
            translations__status=status.PUBLIC,
        )
        .distinct()
        .select_related("category", "organization__icon")
        .prefetch_related(
            Prefetch(
                "category__translations",
                queryset=POICategoryTranslation.objects.select_related("language"),
            ),
        )
    )

    if "on_map" in request.GET:
        try:
            location_on_map = strtobool(request.GET["on_map"])
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        pois = pois.filter(location_on_map=location_on_map)

    # Compute once (works whether Region.timezone is a str or ZoneInfo)
    region_tz_name = _tz_key(getattr(region, "timezone", None) or getattr(region, "timezone_name", None))
    for poi in pois:
        translation = poi.get_public_translation(language_slug)
        if translation:
            result.append(
                transform_poi_translation(translation, region_tz_name=region_tz_name)
            )

    return JsonResponse(
        result,
        safe=False,
    )  # Turn off Safe-Mode to allow serializing arrays
