from robot.vision_skills import classify_vision_skill, normalize_vision_skill, skill_prompt


def test_component_identifier_keywords_route_to_component_skill():
    assert classify_vision_skill("what electronic component is this board part?") == "component_identifier"


def test_desk_object_memory_keywords_route_to_object_memory():
    assert classify_vision_skill("remember where I put these scissors") == "desk_object_memory"


def test_unknown_skill_falls_back_to_generic_description():
    assert normalize_vision_skill("made_up_skill") == "generic_description"
    assert classify_vision_skill("what do you see?") == "generic_description"


def test_skill_prompt_contains_original_question_and_skill():
    prompt = skill_prompt("component_identifier", "what component is this?", "focus on the IC")

    assert "Vision skill: component_identifier" in prompt
    assert "what component is this?" in prompt
    assert "focus on the IC" in prompt
