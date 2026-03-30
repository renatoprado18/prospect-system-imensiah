"""
Photo Validation Service - AI-powered photo analysis

Uses Claude Vision to validate if a photo is appropriate for a contact profile:
- Is it a single person?
- Is it a professional headshot?
- Is the face clearly visible?
"""
import os
import httpx
import base64
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")


async def fetch_image_as_base64(image_url: str) -> Optional[str]:
    """Fetch image from URL and convert to base64"""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(image_url)
            if response.status_code == 200:
                content_type = response.headers.get("content-type", "image/jpeg")
                if "image" in content_type:
                    return base64.b64encode(response.content).decode("utf-8")
    except Exception as e:
        logger.error(f"Error fetching image: {e}")
    return None


async def validate_profile_photo(image_url: str) -> Dict:
    """
    Validate if an image is suitable as a contact profile photo.

    Returns:
        {
            "valid": bool,
            "is_single_person": bool,
            "is_professional": bool,
            "face_visible": bool,
            "confidence": float (0-1),
            "reason": str,
            "description": str
        }
    """
    if not ANTHROPIC_API_KEY:
        return {
            "valid": True,
            "reason": "API key not configured, skipping validation",
            "confidence": 0
        }

    # Fetch image
    image_base64 = await fetch_image_as_base64(image_url)
    if not image_base64:
        return {
            "valid": False,
            "reason": "Could not fetch image",
            "confidence": 0
        }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                },
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 500,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/jpeg",
                                        "data": image_base64
                                    }
                                },
                                {
                                    "type": "text",
                                    "text": """Analyze this image as a potential contact profile photo. Respond in JSON format:

{
    "is_single_person": true/false (is there exactly ONE person in the photo?),
    "is_professional": true/false (does it look like a professional headshot or acceptable profile photo?),
    "face_visible": true/false (is a face clearly visible?),
    "num_people": number (how many people are in the photo),
    "description": "brief description of what you see",
    "recommendation": "use" | "reject" | "review" (should this be used as a profile photo?)
}

Be strict: a good profile photo should show ONE person with a clearly visible face."""
                                }
                            ]
                        }
                    ]
                }
            )

            if response.status_code == 200:
                result = response.json()
                content = result.get("content", [{}])[0].get("text", "{}")

                # Parse AI response
                import json
                try:
                    # Extract JSON from response
                    json_start = content.find("{")
                    json_end = content.rfind("}") + 1
                    if json_start >= 0 and json_end > json_start:
                        ai_analysis = json.loads(content[json_start:json_end])
                    else:
                        ai_analysis = {}
                except:
                    ai_analysis = {}

                # Ser menos restritivo - só rejeitar se claramente tem múltiplas pessoas
                num_people = ai_analysis.get("num_people", 1)
                recommendation = ai_analysis.get("recommendation", "use")
                face_visible = ai_analysis.get("face_visible", True)

                # Aceitar se: tem face visível E (recomendação não é reject OU tem 1-2 pessoas)
                is_valid = face_visible and (recommendation != "reject" or num_people <= 2)

                return {
                    "valid": is_valid,
                    "is_single_person": ai_analysis.get("is_single_person", False),
                    "is_professional": ai_analysis.get("is_professional", False),
                    "face_visible": ai_analysis.get("face_visible", False),
                    "num_people": ai_analysis.get("num_people", 0),
                    "confidence": 0.9 if is_valid else 0.3,
                    "reason": ai_analysis.get("recommendation", "unknown"),
                    "description": ai_analysis.get("description", "")
                }
            else:
                logger.error(f"AI API error: {response.status_code} - {response.text}")
                return {
                    "valid": True,
                    "reason": f"API error: {response.status_code}",
                    "confidence": 0
                }

    except Exception as e:
        logger.error(f"Photo validation error: {e}")
        return {
            "valid": True,
            "reason": f"Validation error: {str(e)}",
            "confidence": 0
        }


async def should_use_photo(image_url: str) -> tuple[bool, str]:
    """
    Simple helper that returns (should_use, reason)
    """
    result = await validate_profile_photo(image_url)
    return result.get("valid", True), result.get("description", result.get("reason", ""))
