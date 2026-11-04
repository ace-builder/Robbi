from __future__ import annotations
import re


VISION_SKILLS = {
    "generic_description",
    "component_identifier",
    "desk_object_memory",
    "homework_helper",
    "outfit_helper",
    "monitoring_check",
    "face_person",
}


def person_facing_request(text):
    lowered=str(text or '').casefold().replace('\u2019',"'")
    if re.search(r"\b(don't|do not|stop|never)\b",lowered):return False
    return bool(re.search(r'\b(?:turn\s+towards?\s+me|face\s+me|look\s+(?:at|towards?)\s+me)\b',lowered))


def normalize_vision_skill(value: str | None) -> str:
    skill = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return skill if skill in VISION_SKILLS else "generic_description"


def classify_vision_skill(text: str, requested: str | None = None) -> str:
    explicit = normalize_vision_skill(requested)
    if requested and explicit != "generic_description":
        return explicit
    lowered = str(text or "").strip().lower()
    if person_facing_request(lowered):return 'face_person'
    if any(marker in lowered for marker in ("component", "resistor", "capacitor", "diode", "transistor", "pinout", "sensor", "module", "connector", "ic ", "chip")):
        return "component_identifier"
    if any(marker in lowered for marker in ("remember where", "remember this desk", "what changed", "missing", "object", "keys", "where did i put")):
        return "desk_object_memory"
    if any(marker in lowered for marker in ("homework", "worksheet", "solve this", "math problem", "exercise", "task sheet")):
        return "homework_helper"
    if any(marker in lowered for marker in ("outfit", "wear this", "clothes", "shirt", "pants", "style", "better now")):
        return "outfit_helper"
    if any(marker in lowered for marker in ("door", "parcel", "oven", "window", "is open", "is closed", "watch", "monitor")):
        return "monitoring_check"
    return "generic_description"


def skill_prompt(skill: str, user_text: str, extra_prompt: str = "") -> str:
    skill = normalize_vision_skill(skill)
    user_text = str(user_text or "").strip()
    extra_prompt = str(extra_prompt or "").strip()
    base = [
        "A fresh camera image is attached.",
        f"Original user request: {user_text}",
    ]
    if extra_prompt:
        base.append(f"Additional runtime guidance: {extra_prompt}")
    if skill == 'face_person':
        base.append('Vision skill: face_person. Use this fresh image to locate a clearly visible person. '
            'If one person is clearly off center, request one in-place drive with mode=turn and a conservative '
            'estimated angle_deg up to 45 degrees: positive for image left, negative for image right. '
            'If already centered, answer without moving. If nobody is visible or several people make the target '
            'ambiguous, ask for clarification without driving. Never guess a direction from an old description. '
            'Do not drive forward. A short acknowledgement may accompany the turn; do not claim it completed.')
    elif skill == "component_identifier":
        base.append(
            "Vision skill: component_identifier. Identify the likely electronics/mechanical component, useful markings, common purpose, "
            "polarity or safety warnings if visible, and search terms. Be honest about uncertainty. Use memory_ops only for durable project facts."
        )
    elif skill == "desk_object_memory":
        base.append(
            "Vision skill: desk_object_memory. Describe notable objects and positions, compare if the user asks about change/missing objects, "
            "and store durable object-location facts with memory_ops when explicitly useful."
        )
    elif skill == "homework_helper":
        base.append(
            "Vision skill: homework_helper. Read the worksheet/problem if possible and guide step by step. Do not simply dump final answers unless asked by an adult."
        )
    elif skill == "outfit_helper":
        base.append(
            "Vision skill: outfit_helper. Give concrete, kind but honest style feedback. Mention colors/items only if visible and do not overclaim."
        )
    elif skill == "monitoring_check":
        base.append(
            "Vision skill: monitoring_check. Answer the physical-state question with a confidence level and suggest sending/keeping the image if uncertain."
        )
    else:
        base.append("Vision skill: generic_description. Answer what is visible, concise and uncertainty-aware.")
    base.append("Return Robbi action JSON. Prefer speak plus face. If storing memory, use top-level memory_ops.")
    return "\n".join(base)
